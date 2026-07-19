from __future__ import annotations

from drawingmachine.adapters.workers.task_registry import build_task_registry
from drawingmachine.adapters.workers.tasks.image import ProviderTransportFactory
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerStatus, WorkerTaskV1


def _failed(task: WorkerTaskV1, error: DrawingMachineError | None = None) -> WorkerOutcomeV1:
    if error is None:
        payload = ErrorPayload(
            "WORKER_TASK_FAILED",
            ErrorCategory.INTERNAL,
            "worker task execution failed",
            False,
            {},
            job_id=task.job_id,
        )
    else:
        source = error.payload
        blocked = "BLOCKED" in source.code
        payload = ErrorPayload(
            source.code,
            source.category,
            "worker task was blocked" if blocked else "worker task execution failed",
            source.retryable,
            {},
            request_id=source.request_id,
            job_id=task.job_id,
        )
    return WorkerOutcomeV1(
        1,
        task.task_id,
        task.job_id,
        task.job_revision,
        task.attempt_id,
        WorkerStatus.BLOCKED if error is not None and "BLOCKED" in error.payload.code else WorkerStatus.FAILED,
        (),
        {},
        payload,
    )


def execute_task(
    task_json: JsonObject | WorkerTaskV1,
    *,
    provider_transport_factory: ProviderTransportFactory | None = None,
) -> JsonObject:
    task = task_json if isinstance(task_json, WorkerTaskV1) else WorkerTaskV1.from_json(task_json)
    try:
        outcome = build_task_registry(provider_transport_factory=provider_transport_factory).execute(task)
    except DrawingMachineError as error:
        outcome = _failed(task, error)
    except BaseException:
        outcome = _failed(task)
    return outcome.to_json()


__all__ = ["execute_task"]
