from __future__ import annotations

import multiprocessing.util
import os
import struct
import time
from typing import Any

from drawingmachine.errors import ErrorCategory, ErrorPayload
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerStatus, WorkerTaskV1


def _error(task: WorkerTaskV1, code: str, message: str) -> ErrorPayload:
    return ErrorPayload(
        code=code,
        category=ErrorCategory.VALIDATION,
        message=message,
        retryable=False,
        details={"fixture": True},
        job_id=task.job_id,
    )


def _outcome(task: WorkerTaskV1, status: WorkerStatus, error: ErrorPayload | None = None) -> WorkerOutcomeV1:
    return WorkerOutcomeV1(
        schema_version=1,
        task_id=task.task_id,
        job_id=task.job_id,
        job_revision=task.job_revision,
        attempt_id=task.attempt_id,
        status=status,
        artifacts=(),
        metrics={"input_count": len(task.input_artifacts)},
        error=error,
    )


def run(task: WorkerTaskV1) -> WorkerOutcomeV1 | dict[str, Any]:
    mode = task.payload["mode"]
    if mode == "cooperative_cancel":
        from drawingmachine.adapters.workers.process_supervisor import worker_cancellation_requested

        while not worker_cancellation_requested():
            time.sleep(0.01)
        return _outcome(
            task,
            WorkerStatus.CANCELLED,
            _error(task, "FIXTURE_CANCELLED_COOPERATIVELY", "fixture observed cancellation"),
        )
    if mode == "large_after_cancel":
        from drawingmachine.adapters.workers.process_supervisor import worker_cancellation_requested

        while not worker_cancellation_requested():
            time.sleep(0.01)
        outcome = _outcome(task, WorkerStatus.SUCCEEDED).to_json()
        outcome["metrics"] = {"blob": "x" * 900_000}
        return outcome
    if mode == "partial_frame_stall":
        from multiprocessing.connection import Connection

        def send_partial_frame(
            connection: Connection,
            buffer: bytes,
            offset: int = 0,
            size: int | None = None,
        ) -> None:
            payload = memoryview(buffer)[offset : None if size is None else offset + size]
            connection._send(struct.pack("!i", len(payload)))  # type: ignore[attr-defined]
            connection._send(payload[:1024])  # type: ignore[attr-defined]
            while True:
                time.sleep(1.0)

        Connection.send_bytes = send_partial_frame  # type: ignore[method-assign]
        outcome = _outcome(task, WorkerStatus.SUCCEEDED).to_json()
        outcome["metrics"] = {"blob": "x" * 64_000}
        return outcome
    if mode == "linger":
        multiprocessing.util.Finalize(None, time.sleep, args=(0.2,), exitpriority=1)
        return _outcome(task, WorkerStatus.SUCCEEDED)
    if mode == "blocker":
        return _outcome(task, WorkerStatus.BLOCKED, _error(task, "REVIEW_REQUIRED", "review required"))
    if mode == "failure":
        return _outcome(task, WorkerStatus.FAILED, _error(task, "FIXTURE_FAILURE", "fixture failure"))
    if mode == "raise":
        raise RuntimeError("fixture-secret must not cross the process boundary")
    if mode == "malformed":
        return {"malformed": True}
    if mode == "oversized":
        return {"oversized": "x" * 1_100_000}
    if mode == "crash":
        os._exit(23)
    if mode == "nonzero_after_send":
        multiprocessing.util.Finalize(None, os._exit, args=(31,), exitpriority=1)
        return _outcome(task, WorkerStatus.SUCCEEDED)
    if mode == "identity_mismatch":
        outcome = _outcome(task, WorkerStatus.SUCCEEDED).to_json()
        outcome["job_id"] = "wrong-job"
        return outcome
    if mode == "hang":
        while True:
            time.sleep(1.0)
    return _outcome(task, WorkerStatus.SUCCEEDED)
