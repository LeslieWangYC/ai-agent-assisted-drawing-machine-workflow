from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import NoReturn

from PIL import Image

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import MAX_IMAGE_IMPORT_BYTES, WorkerArtifact
from drawingmachine.ports.workers import WorkerInputArtifact, WorkerOutcomeV1, WorkerStatus, WorkerTaskV1

from .. import resource_limits

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
MAX_WORKER_INPUT_BYTES = MAX_IMAGE_IMPORT_BYTES


@dataclass(frozen=True, slots=True)
class TaskOutput:
    role: str
    relative_path: str
    media_type: str
    content: bytes


def _task_error(code: str, category: ErrorCategory, message: str, *, job_id: str | None = None) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload(code, category, message, False, {}, job_id=job_id))


def _raise_input(message: str, task: WorkerTaskV1) -> NoReturn:
    raise _task_error("WORKER_INPUT_INVALID", ErrorCategory.INPUT, message, job_id=task.job_id)


def _raise_limit(code: str, message: str, task: WorkerTaskV1) -> NoReturn:
    raise _task_error(code, ErrorCategory.VALIDATION, message, job_id=task.job_id)


def read_inputs(task: WorkerTaskV1, expected_roles: frozenset[str]) -> dict[str, bytes]:
    by_role = {artifact.role: artifact for artifact in task.input_artifacts}
    if set(by_role) != expected_roles:
        _raise_input("worker input artifact roles do not match the task contract", task)
    return {role: _read_input(task, by_role[role]) for role in sorted(expected_roles)}


def _read_input(task: WorkerTaskV1, artifact: WorkerInputArtifact) -> bytes:
    try:
        path_before = os.lstat(artifact.absolute_path)
    except OSError:
        _raise_input("worker input artifact is unavailable", task)
    if not stat.S_ISREG(path_before.st_mode):
        _raise_input("worker input artifact is not a regular file", task)
    descriptor: int | None = None
    try:
        descriptor = os.open(artifact.absolute_path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            path_before.st_dev,
            path_before.st_ino,
        ):
            _raise_input("worker input artifact identity changed before reading", task)
        maximum = resource_limits.policy_for(task.kind).max_input_file_bytes
        if before.st_size != artifact.size_bytes or not before.st_size >= 0:
            _raise_input("worker input artifact size is outside its declared bounded contract", task)
        if before.st_size > maximum:
            _raise_input("worker input artifact exceeds its resource limit", task)
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        size_bytes = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
            size_bytes += len(chunk)
        after = os.fstat(descriptor)
        path_after = os.lstat(artifact.absolute_path)
        stable_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        stable_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if stable_before != stable_after or (after.st_dev, after.st_ino) != (path_after.st_dev, path_after.st_ino):
            _raise_input("worker input artifact identity changed while reading", task)
        if size_bytes != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            _raise_input("worker input artifact identity does not match its declaration", task)
        return b"".join(chunks)
    except DrawingMachineError:
        raise
    except OSError:
        _raise_input("worker input artifact could not be read safely", task)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_bundle(task: WorkerTaskV1, outputs: tuple[TaskOutput, ...]) -> tuple[WorkerArtifact, ...]:
    roles = [output.role for output in outputs]
    paths = [output.relative_path for output in outputs]
    if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
        raise _task_error(
            "WORKER_OUTPUT_INVALID",
            ErrorCategory.INTERNAL,
            "worker output declarations are invalid",
            job_id=task.job_id,
        )
    policy = resource_limits.policy_for(task.kind)
    total_output_bytes = 0
    for output in outputs:
        output_bytes = len(output.content)
        if output_bytes > policy.max_output_file_bytes:
            _raise_limit("WORKER_OUTPUT_LIMIT_EXCEEDED", "worker output exceeds its per-file resource limit", task)
        total_output_bytes += output_bytes
        if total_output_bytes > policy.max_output_bundle_bytes:
            _raise_limit("WORKER_OUTPUT_LIMIT_EXCEEDED", "worker output bundle exceeds its resource limit", task)
    for path in paths:
        relative = PurePosixPath(path)
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != path or path in {".", ".."}:
            raise _task_error(
                "WORKER_OUTPUT_INVALID", ErrorCategory.INTERNAL, "worker output path is invalid", job_id=task.job_id
            )

    directory_fd: int | None = None
    created: list[str] = []
    try:
        path_before = os.lstat(task.staging_dir)
        if not stat.S_ISDIR(path_before.st_mode):
            raise OSError("staging path is not a directory")
        directory_fd = os.open(task.staging_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (path_before.st_dev, path_before.st_ino):
            raise OSError("staging directory identity mismatch")
        if os.listdir(directory_fd):
            raise OSError("staging directory is not empty")
        artifacts: list[WorkerArtifact] = []
        output_identities: dict[str, tuple[int, int]] = {}
        for output in outputs:
            descriptor = os.open(
                output.relative_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            created.append(output.relative_path)
            try:
                view = memoryview(output.content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("output write made no progress")
                    view = view[written:]
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
                path_metadata = os.stat(output.relative_path, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not stat.S_ISREG(path_metadata.st_mode)
                    or metadata.st_size != len(output.content)
                    or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
                ):
                    raise OSError("output is not a stable regular file")
                output_identities[output.relative_path] = (metadata.st_dev, metadata.st_ino)
            finally:
                os.close(descriptor)
            artifacts.append(
                WorkerArtifact(
                    output.role,
                    output.relative_path,
                    hashlib.sha256(output.content).hexdigest(),
                    len(output.content),
                    output.media_type,
                )
            )
        current = os.lstat(task.staging_dir)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("staging directory identity changed")
        for output in outputs:
            path_metadata = os.stat(output.relative_path, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or (path_metadata.st_dev, path_metadata.st_ino) != output_identities[output.relative_path]
                or path_metadata.st_size != len(output.content)
            ):
                raise OSError("output changed before bundle completion")
        os.fsync(directory_fd)
        return tuple(artifacts)
    except DrawingMachineError:
        raise
    except (OSError, ValueError) as error:
        if directory_fd is not None:
            for path in created:
                with suppress(OSError):
                    os.unlink(path, dir_fd=directory_fd)
            with suppress(OSError):
                os.fsync(directory_fd)
        raise _task_error(
            "WORKER_OUTPUT_WRITE_FAILED",
            ErrorCategory.INTERNAL,
            "worker output bundle could not be written safely",
            job_id=task.job_id,
        ) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def open_bounded_image(task: WorkerTaskV1, contents: bytes) -> Image.Image:
    with Image.open(BytesIO(contents)) as opened:
        width, height = opened.size
        maximum = resource_limits.policy_for(task.kind).max_image_pixels
        if width <= 0 or height <= 0 or width * height > maximum:
            _raise_limit("WORKER_IMAGE_LIMIT_EXCEEDED", "worker image dimensions exceed the resource limit", task)
        opened.load()
        return opened.copy()


def validate_bounded_image(task: WorkerTaskV1, contents: bytes) -> None:
    image = open_bounded_image(task, contents)
    image.close()


def succeeded(
    task: WorkerTaskV1, artifacts: tuple[WorkerArtifact, ...], metrics: JsonObject | None = None
) -> WorkerOutcomeV1:
    return WorkerOutcomeV1(
        1,
        task.task_id,
        task.job_id,
        task.job_revision,
        task.attempt_id,
        WorkerStatus.SUCCEEDED,
        artifacts,
        {} if metrics is None else metrics,
        None,
    )


def blocked(
    task: WorkerTaskV1,
    artifacts: tuple[WorkerArtifact, ...],
    *,
    code: str,
    message: str,
) -> WorkerOutcomeV1:
    return WorkerOutcomeV1(
        1,
        task.task_id,
        task.job_id,
        task.job_revision,
        task.attempt_id,
        WorkerStatus.BLOCKED,
        artifacts,
        {},
        ErrorPayload(code, ErrorCategory.VALIDATION, message, False, {}, job_id=task.job_id),
    )


__all__ = [
    "MAX_WORKER_INPUT_BYTES",
    "TaskOutput",
    "blocked",
    "open_bounded_image",
    "read_inputs",
    "succeeded",
    "validate_bounded_image",
    "write_bundle",
]
