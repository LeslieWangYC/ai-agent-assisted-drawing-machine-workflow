from __future__ import annotations

import json
import math
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import ExpectedArtifact, WorkerArtifact

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    worker_by_path: dict[str, WorkerArtifact]
    expected_by_path: dict[str, ExpectedArtifact]
    declared_paths: tuple[PurePosixPath, ...]
    allowed_directories: frozenset[str]


def artifact_error(
    message: str,
    *,
    code: str = "ARTIFACT_VALIDATION_FAILED",
    job_id: str | None = None,
    details: JsonObject | None = None,
) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.VALIDATION,
            message=message,
            retryable=False,
            details={} if details is None else details,
            job_id=job_id,
        )
    )


def raise_artifact(
    message: str,
    *,
    code: str = "ARTIFACT_VALIDATION_FAILED",
    job_id: str | None = None,
    details: JsonObject | None = None,
) -> NoReturn:
    raise artifact_error(message, code=code, job_id=job_id, details=details)


def validate_component(value: str, field: str) -> str:
    if not isinstance(value, str) or value in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(value) is None:
        raise_artifact(
            f"{field} must be one safe ASCII path component",
            code="ARTIFACT_PATH_INVALID",
            details={"field": field},
        )
    return value


def validate_text(value: str, field: str, *, job_id: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise_artifact(
            f"{field} must be a non-empty string without control characters",
            job_id=job_id,
            details={"field": field},
        )
    return value


def validate_relative_path(value: str, *, field: str, job_id: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise_artifact(
            f"{field} must be a canonical relative path",
            code="ARTIFACT_PATH_INVALID",
            job_id=job_id,
            details={"field": field},
        )
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise_artifact(
            f"{field} must be a canonical relative path without path escape",
            code="ARTIFACT_PATH_INVALID",
            job_id=job_id,
            details={"field": field, "relative_path": value},
        )
    return path


def _parent_directories(paths: tuple[PurePosixPath, ...]) -> frozenset[str]:
    directories: set[str] = set()
    for path in paths:
        parent = path.parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return frozenset(directories)


def validate_manifests(
    artifacts: tuple[WorkerArtifact, ...],
    expected: tuple[ExpectedArtifact, ...],
    *,
    job_id: str,
) -> ValidatedManifest:
    worker_roles: set[str] = set()
    worker_paths: set[str] = set()
    worker_descriptors: set[tuple[str, str, str]] = set()
    worker_by_path: dict[str, WorkerArtifact] = {}
    declared_paths: list[PurePosixPath] = []
    for artifact in artifacts:
        role = validate_text(artifact.role, "worker artifact role", job_id=job_id)
        relative = validate_relative_path(
            artifact.relative_path,
            field="worker artifact relative path",
            job_id=job_id,
        )
        media_type = validate_text(artifact.media_type, "worker artifact media type", job_id=job_id)
        if role in worker_roles:
            raise_artifact("worker artifacts contain a duplicate role", job_id=job_id)
        if relative.as_posix() in worker_paths:
            raise_artifact("worker artifacts contain a duplicate path", job_id=job_id)
        if not isinstance(artifact.size_bytes, int) or isinstance(artifact.size_bytes, bool) or artifact.size_bytes < 0:
            raise_artifact("worker artifact size must be a nonnegative integer", job_id=job_id)
        if not isinstance(artifact.sha256, str) or _SHA256.fullmatch(artifact.sha256) is None:
            raise_artifact("worker artifact digest must be a lowercase SHA-256 value", job_id=job_id)
        worker_roles.add(role)
        worker_paths.add(relative.as_posix())
        worker_descriptors.add((role, relative.as_posix(), media_type))
        worker_by_path[relative.as_posix()] = artifact
        declared_paths.append(relative)

    expected_roles: set[str] = set()
    expected_paths: set[str] = set()
    expected_descriptors: set[tuple[str, str, str]] = set()
    expected_by_path: dict[str, ExpectedArtifact] = {}
    for declaration in expected:
        role = validate_text(declaration.role, "expected artifact role", job_id=job_id)
        relative = validate_relative_path(
            declaration.relative_path,
            field="expected artifact relative path",
            job_id=job_id,
        )
        media_type = validate_text(declaration.media_type, "expected artifact media type", job_id=job_id)
        if role in expected_roles:
            raise_artifact("expected artifacts contain a duplicate role", job_id=job_id)
        if relative.as_posix() in expected_paths:
            raise_artifact("expected artifacts contain a duplicate path", job_id=job_id)
        if not isinstance(declaration.json_object_required, bool):
            raise_artifact("expected artifact JSON requirement must be boolean", job_id=job_id)
        json_keys: set[str] = set()
        for key, value in declaration.required_json_values:
            if not isinstance(key, str) or not key or key in json_keys:
                raise_artifact("expected artifact JSON discriminator keys must be unique", job_id=job_id)
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise_artifact(
                    "expected artifact JSON discriminator values must be strings or integers",
                    job_id=job_id,
                )
            json_keys.add(key)
        if declaration.required_json_values and not declaration.json_object_required:
            raise_artifact("JSON discriminators require an object artifact", job_id=job_id)
        expected_roles.add(role)
        expected_paths.add(relative.as_posix())
        expected_descriptors.add((role, relative.as_posix(), media_type))
        expected_by_path[relative.as_posix()] = declaration

    if worker_descriptors != expected_descriptors:
        raise_artifact(
            "expected and worker artifact role/path/media-type sets do not match",
            job_id=job_id,
        )
    paths = tuple(declared_paths)
    return ValidatedManifest(
        worker_by_path=worker_by_path,
        expected_by_path=expected_by_path,
        declared_paths=paths,
        allowed_directories=_parent_directories(paths),
    )


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _strict_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON float is forbidden: {value}")
    return parsed


def strict_json_object(contents: bytes, *, relative_path: str, job_id: str) -> JsonObject:
    try:
        text = contents.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise artifact_error(
            f"artifact must contain valid JSON and satisfy strict JSON rules: {relative_path}",
            job_id=job_id,
            details={"relative_path": relative_path},
        ) from error
    if not isinstance(payload, dict):
        raise_artifact(
            f"artifact strict JSON root must be an object: {relative_path}",
            job_id=job_id,
            details={"relative_path": relative_path},
        )
    return cast(JsonObject, payload)


def _validate_json(contents: bytes, expected: ExpectedArtifact, *, job_id: str) -> None:
    if not expected.json_object_required:
        return
    payload = strict_json_object(contents, relative_path=expected.relative_path, job_id=job_id)
    for key, expected_value in expected.required_json_values:
        actual = payload.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise_artifact(
                f"artifact JSON discriminator {key} does not match: {expected.relative_path}",
                job_id=job_id,
                details={"relative_path": expected.relative_path, "json_key": key},
            )


def copy_declared_tree(
    source_fd: int,
    prepared_fd: int,
    manifest: ValidatedManifest,
    *,
    job_id: str,
) -> None:
    source_before = _snapshot_source_tree(source_fd, manifest, job_id=job_id)
    seen: set[str] = set()

    def visit(source_directory_fd: int, prepared_directory_fd: int, prefix: PurePosixPath) -> None:
        for name in sorted(os.listdir(source_directory_fd)):
            relative = PurePosixPath(name) if prefix == PurePosixPath(".") else prefix / name
            relative_text = relative.as_posix()
            path_status = _secure_fs.stat_at(source_directory_fd, name)
            if stat.S_ISLNK(path_status.st_mode):
                raise_artifact(f"staging tree contains a symlink: {relative_text}", job_id=job_id)
            if stat.S_ISDIR(path_status.st_mode):
                if relative_text not in manifest.allowed_directories:
                    raise_artifact(
                        f"staging tree contains an undeclared directory: {relative_text}",
                        job_id=job_id,
                    )
                source_child_fd = _secure_fs.open_directory_at(source_directory_fd, name)
                prepared_child_fd = _secure_fs.create_directory_at(prepared_directory_fd, name)
                try:
                    visit(source_child_fd, prepared_child_fd, relative)
                    _secure_fs.verify_entry_identity(
                        source_directory_fd,
                        name,
                        source_child_fd,
                        directory=True,
                    )
                    _secure_fs.fsync_directory(prepared_child_fd)
                finally:
                    os.close(prepared_child_fd)
                    os.close(source_child_fd)
                continue
            if not stat.S_ISREG(path_status.st_mode):
                raise_artifact(f"staging entry must be a regular file: {relative_text}", job_id=job_id)
            worker = manifest.worker_by_path.get(relative_text)
            if worker is None:
                raise_artifact(
                    f"staging tree contains an undeclared regular file: {relative_text}",
                    job_id=job_id,
                )
            copied = _secure_fs.copy_regular_file(
                source_directory_fd,
                name,
                prepared_directory_fd,
                name,
            )
            if copied.size_bytes != worker.size_bytes:
                raise_artifact(f"artifact size mismatch: {relative_text}", job_id=job_id)
            if copied.sha256 != worker.sha256:
                raise_artifact(f"artifact digest mismatch: {relative_text}", job_id=job_id)
            seen.add(relative_text)
        _secure_fs.fsync_directory(prepared_directory_fd)

    visit(source_fd, prepared_fd, PurePosixPath("."))
    missing = sorted(set(manifest.worker_by_path) - seen)
    if missing:
        raise_artifact(f"staging tree is missing declared artifact: {missing[0]}", job_id=job_id)
    try:
        source_after = _snapshot_source_tree(source_fd, manifest, job_id=job_id)
    except DrawingMachineError as error:
        raise artifact_error(
            f"staging tree changed after declared copies: {error}",
            job_id=job_id,
        ) from error
    if source_after != source_before:
        raise_artifact("staging tree changed after declared copies", job_id=job_id)


def _snapshot_source_tree(
    source_fd: int,
    manifest: ValidatedManifest,
    *,
    job_id: str,
) -> dict[str, tuple[int, int, int, int, int, int]]:
    entries: dict[str, tuple[int, int, int, int, int, int]] = {}

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        for name in sorted(os.listdir(directory_fd)):
            relative = PurePosixPath(name) if prefix == PurePosixPath(".") else prefix / name
            relative_text = relative.as_posix()
            path_status = _secure_fs.stat_at(directory_fd, name)
            if stat.S_ISLNK(path_status.st_mode):
                raise_artifact(f"staging tree contains a symlink: {relative_text}", job_id=job_id)
            if stat.S_ISDIR(path_status.st_mode):
                if relative_text not in manifest.allowed_directories:
                    raise_artifact(
                        f"staging tree contains an undeclared directory: {relative_text}",
                        job_id=job_id,
                    )
                entries[relative_text] = _secure_fs.stable_file_identity(path_status)
                child_fd = _secure_fs.open_directory_at(directory_fd, name)
                try:
                    visit(child_fd, relative)
                    _secure_fs.verify_entry_identity(directory_fd, name, child_fd, directory=True)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(path_status.st_mode):
                raise_artifact(f"staging entry must be a regular file: {relative_text}", job_id=job_id)
            if relative_text not in manifest.worker_by_path:
                raise_artifact(
                    f"staging tree contains an undeclared regular file: {relative_text}",
                    job_id=job_id,
                )
            entries[relative_text] = _secure_fs.stable_file_identity(path_status)

    visit(source_fd, PurePosixPath("."))
    expected_entries = set(manifest.worker_by_path) | set(manifest.allowed_directories)
    missing = sorted(expected_entries - set(entries))
    if missing:
        raise_artifact(f"staging tree is missing declared entry: {missing[0]}", job_id=job_id)
    return entries


def validate_prepared_tree(
    prepared_fd: int,
    manifest: ValidatedManifest,
    *,
    job_id: str,
    attempt_id: str,
) -> tuple[ArtifactRef, ...]:
    found: dict[str, ArtifactRef] = {}

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        for name in sorted(os.listdir(directory_fd)):
            relative = PurePosixPath(name) if prefix == PurePosixPath(".") else prefix / name
            relative_text = relative.as_posix()
            path_status = _secure_fs.stat_at(directory_fd, name)
            if stat.S_ISLNK(path_status.st_mode):
                raise_artifact(f"prepared tree contains a symlink: {relative_text}", job_id=job_id)
            if stat.S_ISDIR(path_status.st_mode):
                if relative_text not in manifest.allowed_directories:
                    raise_artifact(
                        f"prepared tree contains an undeclared directory: {relative_text}",
                        job_id=job_id,
                    )
                child_fd = _secure_fs.open_directory_at(directory_fd, name)
                try:
                    visit(child_fd, relative)
                    _secure_fs.verify_entry_identity(directory_fd, name, child_fd, directory=True)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(path_status.st_mode):
                raise_artifact(f"prepared entry must be a regular file: {relative_text}", job_id=job_id)
            expected = manifest.expected_by_path.get(relative_text)
            if expected is None:
                raise_artifact(
                    f"prepared tree contains an undeclared regular file: {relative_text}",
                    job_id=job_id,
                )
            copied, contents = _secure_fs.read_stable_regular(directory_fd, name)
            _validate_json(contents, expected, job_id=job_id)
            found[relative_text] = ArtifactRef(
                role=expected.role,
                relative_path=(PurePosixPath("artifacts") / attempt_id / relative).as_posix(),
                sha256=copied.sha256,
                size_bytes=copied.size_bytes,
                media_type=expected.media_type,
            )

    visit(prepared_fd, PurePosixPath("."))
    missing = sorted(set(manifest.expected_by_path) - set(found))
    if missing:
        raise_artifact(f"prepared tree is missing declared artifact: {missing[0]}", job_id=job_id)
    return tuple(sorted(found.values(), key=lambda artifact: (artifact.role, artifact.relative_path)))


def verify_source_chain(
    root: _secure_fs.ManagedJobsRoot,
    staging_root_fd: int,
    staging_job_fd: int,
    staging_fd: int,
    job_id: str,
    attempt_id: str,
) -> None:
    root.verify()
    _secure_fs.verify_entry_identity(root.jobs_fd, ".staging", staging_root_fd, directory=True)
    _secure_fs.verify_entry_identity(staging_root_fd, job_id, staging_job_fd, directory=True)
    _secure_fs.verify_entry_identity(staging_job_fd, attempt_id, staging_fd, directory=True)


def stage_external_file(
    data_path: Path,
    job_id: str,
    attempt_id: str,
    source: Path,
    source_fd: int,
    source_before: os.stat_result,
    parts: tuple[str, ...],
    *,
    max_bytes: int,
) -> _secure_fs.CopiedFile:
    def track(stack: ExitStack, descriptor: int) -> int:
        stack.callback(os.close, descriptor)
        return descriptor

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise _secure_fs.SecureFilesystemError("artifact import maximum must be a positive integer")
    if not stat.S_ISREG(source_before.st_mode) or source_before.st_size > max_bytes:
        raise _secure_fs.SecureFilesystemError("external artifact source exceeds the maximum import size")
    with _secure_fs.managed_jobs_root(data_path) as root, ExitStack() as stack:
        staging_root_fd = track(stack, _secure_fs.ensure_directory_at(root.jobs_fd, ".staging"))
        staging_job_fd = track(stack, _secure_fs.ensure_directory_at(staging_root_fd, job_id))
        staging_fd = track(stack, _secure_fs.create_directory_at(staging_job_fd, attempt_id))
        try:
            parent_fd = staging_fd
            directory_fds = [staging_fd]
            for part in parts[:-1]:
                parent_fd = track(stack, _secure_fs.ensure_directory_at(parent_fd, part))
                directory_fds.append(parent_fd)
            copied = _secure_fs.copy_external_file(source_fd, parent_fd, parts[-1], max_bytes=max_bytes)
            _secure_fs.verify_external_regular(source, source_fd, source_before)
            for descriptor in reversed(directory_fds):
                _secure_fs.fsync_directory(descriptor)
            verify_source_chain(
                root,
                staging_root_fd,
                staging_job_fd,
                staging_fd,
                job_id,
                attempt_id,
            )
            return copied
        except BaseException:
            _secure_fs.remove_tree_at(staging_job_fd, attempt_id)
            raise
