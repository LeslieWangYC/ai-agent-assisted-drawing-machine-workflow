from __future__ import annotations

import hashlib
import importlib
import json
import math
import multiprocessing
import os
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event as EventType
from typing import NoReturn, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import (
    WorkerHandle,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)

from . import resource_limits

MAX_OUTCOME_BYTES = 1_048_576
_POLL_INTERVAL_SECONDS = 0.05
_FORCED_JOIN_SECONDS = 1.0
_worker_cancel_event: EventType | None = None


class _InputVerificationError(Exception):
    pass


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_loads(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_object_pairs,
    )


def _wire_bytes(value: object) -> bytes:
    if isinstance(value, WorkerOutcomeV1):
        value = value.to_json()
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def worker_cancellation_requested() -> bool:
    event = _worker_cancel_event
    return event is not None and event.is_set()


def _verify_staging_dir(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _InputVerificationError("worker staging directory is unavailable") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _InputVerificationError("worker staging path is not a directory")
    finally:
        os.close(descriptor)


def _verify_input_artifacts(task: WorkerTaskV1) -> None:
    _verify_staging_dir(task.staging_dir)
    maximum = resource_limits.policy_for(task.kind).max_input_file_bytes
    for artifact in task.input_artifacts:
        try:
            path_metadata = os.lstat(artifact.absolute_path)
        except OSError as error:
            raise _InputVerificationError("worker input artifact is unavailable") from error
        if not stat.S_ISREG(path_metadata.st_mode):
            raise _InputVerificationError("worker input artifact is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(artifact.absolute_path, flags)
        except OSError as error:
            raise _InputVerificationError("worker input artifact is unavailable") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _InputVerificationError("worker input artifact is not a regular file")
            if (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino):
                raise _InputVerificationError("worker input artifact changed before verification")
            if metadata.st_size > maximum or artifact.size_bytes > maximum:
                raise _InputVerificationError("worker input artifact exceeds its resource limit")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            identity_before = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if identity_before != identity_after:
                raise _InputVerificationError("worker input artifact changed during verification")
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise _InputVerificationError("worker input artifact identity does not match its declaration")
        finally:
            os.close(descriptor)


def _child_failure(task: WorkerTaskV1, *, code: str, message: str) -> WorkerOutcomeV1:
    return WorkerOutcomeV1(
        schema_version=1,
        task_id=task.task_id,
        job_id=task.job_id,
        job_revision=task.job_revision,
        attempt_id=task.attempt_id,
        status=WorkerStatus.FAILED,
        artifacts=(),
        metrics={},
        error=ErrorPayload(
            code=code,
            category=ErrorCategory.INTERNAL,
            message=message,
            retryable=False,
            details={},
            job_id=task.job_id,
        ),
    )


def _worker_process_main(
    task_payload: JsonObject,
    entrypoint_module: str,
    entrypoint_name: str,
    sender: Connection,
    cancel_event: EventType,
) -> None:
    global _worker_cancel_event
    _worker_cancel_event = cancel_event
    task: WorkerTaskV1 | None = None
    try:
        task = WorkerTaskV1.from_json(task_payload)
        try:
            resource_limits.apply_process_limits(resource_limits.policy_for(task.kind))
        except resource_limits.WorkerResourceLimitError:
            result: object = _child_failure(
                task,
                code="WORKER_RESOURCE_LIMIT_SETUP_FAILED",
                message="worker process resource limits could not be installed",
            )
        else:
            try:
                _verify_input_artifacts(task)
            except _InputVerificationError:
                result = _child_failure(
                    task,
                    code="WORKER_INPUT_INVALID",
                    message="worker input validation failed",
                )
            else:
                module = importlib.import_module(entrypoint_module)
                entrypoint = getattr(module, entrypoint_name)
                if not callable(entrypoint):
                    raise TypeError("worker entrypoint is not callable")
                result = cast(Callable[[WorkerTaskV1], object], entrypoint)(task)
        payload = _wire_bytes(result)
    except BaseException:
        if task is None:
            return
        payload = _wire_bytes(
            _child_failure(
                task,
                code="WORKER_UNCAUGHT_EXCEPTION",
                message="worker entrypoint failed",
            )
        )
    try:
        sender.send_bytes(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


class SpawnProcessHandle:
    __slots__ = ("_attempt_id", "_joined", "_process", "_task_id")

    def __init__(self, task_id: str, attempt_id: str, process: BaseProcess) -> None:
        self._task_id = task_id
        self._attempt_id = attempt_id
        self._process = process
        self._joined = False

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def pid(self) -> int:
        if self._process.pid is None:
            raise RuntimeError("worker process has not started")
        return self._process.pid

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode

    @property
    def joined(self) -> bool:
        return self._joined

    def is_alive(self) -> bool:
        return self._process.is_alive()


@dataclass(slots=True)
class _WorkerRecord:
    task: WorkerTaskV1
    handle: SpawnProcessHandle
    receiver: Connection
    cancel_event: EventType
    outcome: WorkerOutcomeV1 | None = None
    read_complete: threading.Event = field(default_factory=threading.Event)
    read_error: str | None = None
    read_payload: bytes | None = None
    reader_thread: threading.Thread | None = None


class SpawnProcessSupervisor:
    def __init__(self, *, entrypoint_module: str, entrypoint_name: str) -> None:
        if not entrypoint_module or not entrypoint_name:
            raise ValueError("worker entrypoint module and name must not be empty")
        self._context: SpawnContext = multiprocessing.get_context("spawn")
        self._entrypoint_module = entrypoint_module
        self._entrypoint_name = entrypoint_name
        self._records: dict[str, _WorkerRecord] = {}
        self._closed = False

    @property
    def start_method(self) -> str:
        return self._context.get_start_method()

    def submit(self, task: WorkerTaskV1) -> SpawnProcessHandle:
        if self._closed:
            raise self._error("WORKER_SUPERVISOR_CLOSED", "worker supervisor is closed")
        validated_task = WorkerTaskV1.from_json(task.to_json())
        if validated_task.task_id in self._records:
            raise self._error("WORKER_TASK_DUPLICATE", "duplicate worker task_id")
        receiver, sender = self._context.Pipe(duplex=False)
        cancel_event = self._context.Event()
        process = self._context.Process(
            target=_worker_process_main,
            args=(validated_task.to_json(), self._entrypoint_module, self._entrypoint_name, sender, cancel_event),
            name=f"drawingmachine-worker-{validated_task.task_id}",
            daemon=False,
        )
        try:
            process.start()
        except BaseException:
            receiver.close()
            sender.close()
            raise
        sender.close()
        handle = SpawnProcessHandle(validated_task.task_id, validated_task.attempt_id, process)
        self._records[validated_task.task_id] = _WorkerRecord(validated_task, handle, receiver, cancel_event)
        return handle

    def wait(
        self,
        handle: WorkerHandle,
        *,
        timeout_seconds: float,
        cancel_requested: Callable[[], bool],
    ) -> WorkerOutcomeV1:
        record = self._record_for(handle)
        if record.outcome is not None:
            return record.outcome
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                should_cancel = cancel_requested()
            except BaseException:
                self._terminate_and_reap(record)
                self._finish_failure(
                    record,
                    "WORKER_CANCEL_CHECK_FAILED",
                    "worker cancellation state could not be checked",
                )
                raise
            if should_cancel:
                return self.cancel(record.handle)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_and_reap(record)
                return self._finish_failure(record, "WORKER_TIMEOUT", "worker timed out")
            payload, read_error = self._receive_pending(
                record,
                timeout_seconds=min(_POLL_INTERVAL_SECONDS, remaining),
            )
            if read_error is not None:
                if not self._join_before_deadline(record, deadline):
                    return self._finish_failure(
                        record,
                        "WORKER_DID_NOT_EXIT",
                        "worker closed its outcome pipe but did not exit",
                    )
                if read_error == "WORKER_OUTCOME_TOO_LARGE":
                    return self._finish_failure(
                        record,
                        "WORKER_OUTCOME_TOO_LARGE",
                        "worker outcome exceeded the maximum size",
                    )
                return self._finish_failure(
                    record,
                    "WORKER_PROCESS_CRASHED",
                    "worker exited without a complete outcome",
                )
            if payload is not None:
                if not self._join_before_deadline(record, deadline):
                    return self._finish_failure(
                        record,
                        "WORKER_DID_NOT_EXIT",
                        "worker produced an outcome but did not exit",
                    )
                if record.handle.exitcode != 0:
                    return self._finish_failure(
                        record,
                        "WORKER_PROCESS_CRASHED",
                        "worker exited abnormally after producing an outcome",
                    )
                return self._decode_outcome(record, payload)
            if not record.handle.is_alive():
                record.handle._process.join()
                self._receive_pending(record, timeout_seconds=0.0)
                exitcode = record.handle.exitcode
                self._mark_joined(record)
                pending_payload = record.read_payload
                pending_error = record.read_error
                if exitcode != 0:
                    return self._finish_failure(
                        record,
                        "WORKER_PROCESS_CRASHED",
                        "worker exited abnormally after producing an outcome",
                    )
                if pending_payload is not None:
                    return self._decode_outcome(record, pending_payload)
                if pending_error == "WORKER_OUTCOME_TOO_LARGE":
                    return self._finish_failure(
                        record,
                        "WORKER_OUTCOME_TOO_LARGE",
                        "worker outcome exceeded the maximum size",
                    )
                return self._finish_failure(
                    record,
                    "WORKER_PROCESS_CRASHED",
                    "worker exited without an outcome",
                )

    def cancel(self, handle: WorkerHandle, *, grace_seconds: float = 2.0) -> WorkerOutcomeV1:
        record = self._record_for(handle)
        if record.outcome is not None:
            return record.outcome
        if not math.isfinite(grace_seconds) or grace_seconds < 0 or grace_seconds > 2.0:
            raise ValueError("grace_seconds must be finite and between zero and at most two seconds")
        record.cancel_event.set()
        deadline = time.monotonic() + grace_seconds
        pending_payload: bytes | None = None
        pending_error: str | None = None
        while record.handle.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            interval = min(_POLL_INTERVAL_SECONDS, remaining)
            if pending_payload is None and pending_error is None:
                pending_payload, pending_error = self._receive_pending(record, timeout_seconds=interval)
            else:
                record.handle._process.join(interval)
        if record.handle.is_alive():
            self._terminate_and_reap(record)
            return self._finish_failure(
                record,
                "WORKER_CANCELLED",
                "worker was cancelled",
                status=WorkerStatus.CANCELLED,
            )

        record.handle._process.join()
        if pending_payload is None and pending_error is None:
            self._receive_pending(record, timeout_seconds=0.0)
        self._mark_joined(record)
        pending_payload = record.read_payload
        pending_error = record.read_error
        if record.handle.exitcode != 0:
            return self._finish_failure(
                record,
                "WORKER_PROCESS_CRASHED",
                "worker exited abnormally after producing an outcome",
            )
        if pending_payload is not None:
            return self._decode_outcome(record, pending_payload)
        if pending_error == "WORKER_OUTCOME_TOO_LARGE":
            return self._finish_failure(
                record,
                "WORKER_OUTCOME_TOO_LARGE",
                "worker outcome exceeded the maximum size",
            )
        return self._finish_failure(
            record,
            "WORKER_PROCESS_CRASHED",
            "worker exited without a complete outcome",
        )

    def _receive_pending(
        self,
        record: _WorkerRecord,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes | None, str | None]:
        deadline = time.monotonic() + timeout_seconds
        if record.reader_thread is None:
            if not record.receiver.poll(timeout_seconds):
                return None, None
            reader = threading.Thread(
                target=self._read_outcome,
                args=(record,),
                name=f"drawingmachine-outcome-{record.task.task_id}",
                daemon=False,
            )
            record.reader_thread = reader
            reader.start()
        remaining = max(0.0, deadline - time.monotonic())
        if not record.read_complete.wait(remaining):
            return None, None
        return record.read_payload, record.read_error

    @staticmethod
    def _read_outcome(record: _WorkerRecord) -> None:
        try:
            record.read_payload = record.receiver.recv_bytes(MAX_OUTCOME_BYTES)
        except OSError as error:
            if str(error) == "bad message length":
                record.read_error = "WORKER_OUTCOME_TOO_LARGE"
            else:
                record.read_error = "WORKER_PROCESS_CRASHED"
        except EOFError:
            record.read_error = "WORKER_PROCESS_CRASHED"
        finally:
            record.read_complete.set()

    def close(self) -> None:
        if self._closed:
            return
        for record in tuple(self._records.values()):
            if record.outcome is None:
                self.cancel(record.handle, grace_seconds=0.0)
            else:
                self._close_receiver(record)
        self._closed = True

    def _record_for(self, handle: WorkerHandle) -> _WorkerRecord:
        if not isinstance(handle, SpawnProcessHandle):
            raise self._error("WORKER_HANDLE_INVALID", "worker handle is not owned by this supervisor")
        record = self._records.get(handle.task_id)
        if record is None or record.handle is not handle or record.task.attempt_id != handle.attempt_id:
            raise self._error("WORKER_HANDLE_INVALID", "worker handle is not owned by this supervisor")
        return record

    def _join_before_deadline(self, record: _WorkerRecord, deadline: float) -> bool:
        remaining = max(0.0, deadline - time.monotonic())
        record.handle._process.join(remaining)
        if record.handle.is_alive():
            self._terminate_and_reap(record)
            return False
        self._mark_joined(record)
        return True

    def _terminate_and_reap(self, record: _WorkerRecord) -> None:
        process = record.handle._process
        if process.is_alive():
            process.terminate()
            process.join(_FORCED_JOIN_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(_FORCED_JOIN_SECONDS)
        if process.is_alive():
            raise self._error("WORKER_REAP_FAILED", "worker process could not be reaped")
        self._mark_joined(record)

    def _mark_joined(self, record: _WorkerRecord) -> None:
        if record.handle._process.is_alive():
            raise RuntimeError("cannot mark a live worker process as joined")
        record.handle._process.join()
        self._join_outcome_reader(record)
        self._close_receiver(record)
        record.handle._joined = True

    def _join_outcome_reader(self, record: _WorkerRecord) -> None:
        if record.reader_thread is None and record.receiver.poll(0):
            self._receive_pending(record, timeout_seconds=0.0)
        reader = record.reader_thread
        if reader is None:
            return
        reader.join(_FORCED_JOIN_SECONDS)
        if reader.is_alive():
            self._close_receiver(record)
            reader.join(_FORCED_JOIN_SECONDS)
        if reader.is_alive():
            raise self._error("WORKER_READER_REAP_FAILED", "worker outcome reader could not be reaped")

    @staticmethod
    def _close_receiver(record: _WorkerRecord) -> None:
        with suppress(OSError):
            record.receiver.close()

    def _decode_outcome(self, record: _WorkerRecord, payload: bytes) -> WorkerOutcomeV1:
        self._assert_reaped(record)
        try:
            decoded = _strict_loads(payload)
            outcome = WorkerOutcomeV1.from_json(decoded)
        except (DrawingMachineError, UnicodeDecodeError, ValueError, TypeError):
            return self._finish_failure(
                record,
                "WORKER_OUTCOME_INVALID",
                "worker returned an invalid outcome",
            )
        task = record.task
        if (
            outcome.task_id != task.task_id
            or outcome.job_id != task.job_id
            or outcome.job_revision != task.job_revision
            or outcome.attempt_id != task.attempt_id
        ):
            return self._finish_failure(
                record,
                "WORKER_IDENTITY_MISMATCH",
                "worker outcome identity did not match its task",
            )
        try:
            resource_limits.validate_outcome(task, outcome)
        except resource_limits.WorkerResourceLimitError:
            return self._finish_failure(
                record,
                "WORKER_OUTPUT_LIMIT_EXCEEDED",
                "worker outcome exceeded its artifact resource limit",
            )
        record.outcome = outcome
        return outcome

    def _finish_failure(
        self,
        record: _WorkerRecord,
        code: str,
        message: str,
        *,
        status: WorkerStatus = WorkerStatus.FAILED,
    ) -> WorkerOutcomeV1:
        self._assert_reaped(record)
        outcome = WorkerOutcomeV1(
            schema_version=1,
            task_id=record.task.task_id,
            job_id=record.task.job_id,
            job_revision=record.task.job_revision,
            attempt_id=record.task.attempt_id,
            status=status,
            artifacts=(),
            metrics={},
            error=ErrorPayload(
                code=code,
                category=ErrorCategory.INTERNAL,
                message=message,
                retryable=False,
                details={},
                job_id=record.task.job_id,
            ),
        )
        record.outcome = outcome
        return outcome

    @staticmethod
    def _assert_reaped(record: _WorkerRecord) -> None:
        reader = record.reader_thread
        if not record.handle.joined or record.handle.is_alive() or (reader is not None and reader.is_alive()):
            raise RuntimeError("worker outcome cannot be returned before the producer and outcome reader are reaped")

    @staticmethod
    def _error(code: str, message: str) -> DrawingMachineError:
        return DrawingMachineError(
            ErrorPayload(
                code=code,
                category=ErrorCategory.SERVICE,
                message=message,
                retryable=False,
                details={},
            )
        )


__all__ = [
    "MAX_OUTCOME_BYTES",
    "SpawnProcessHandle",
    "SpawnProcessSupervisor",
    "worker_cancellation_requested",
]
