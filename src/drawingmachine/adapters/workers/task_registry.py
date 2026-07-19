from __future__ import annotations

from collections.abc import Callable
from functools import partial

from drawingmachine.adapters.workers.tasks import gcode, image, planning
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.workers import WorkerKind, WorkerOutcomeV1, WorkerTaskV1

TaskHandler = Callable[[WorkerTaskV1], WorkerOutcomeV1]


def _registry_error(code: str, message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload(code, ErrorCategory.CONFIGURATION, message, False, {}))


class TaskRegistry:
    def __init__(self) -> None:
        self._handlers: dict[WorkerKind, TaskHandler] = {}

    @property
    def kinds(self) -> tuple[WorkerKind, ...]:
        return tuple(self._handlers)

    def register(self, kind: WorkerKind, handler: TaskHandler) -> None:
        if not isinstance(kind, WorkerKind) or kind in {WorkerKind.TEST_ECHO, WorkerKind.TEST_HANG}:
            raise _registry_error("WORKER_KIND_NOT_REGISTERABLE", "worker kind is not registerable")
        if kind in self._handlers:
            raise _registry_error("WORKER_KIND_ALREADY_REGISTERED", "worker kind is already registered")
        if not callable(handler):
            raise _registry_error("WORKER_HANDLER_INVALID", "worker handler must be callable")
        self._handlers[kind] = handler

    def execute(self, task: WorkerTaskV1) -> WorkerOutcomeV1:
        try:
            handler = self._handlers[task.kind]
        except KeyError as error:
            raise _registry_error("WORKER_KIND_NOT_REGISTERED", "worker kind is not registered") from error
        outcome = handler(task)
        if (
            outcome.task_id,
            outcome.job_id,
            outcome.job_revision,
            outcome.attempt_id,
        ) != (task.task_id, task.job_id, task.job_revision, task.attempt_id):
            raise _registry_error("WORKER_IDENTITY_MISMATCH", "worker outcome identity does not match task identity")
        return outcome


def build_task_registry(
    *,
    provider_transport_factory: image.ProviderTransportFactory | None = None,
) -> TaskRegistry:
    registry = TaskRegistry()
    registry.register(WorkerKind.IMAGE_PREPARE, image.run_image_prepare)
    if provider_transport_factory is None:
        registry.register(WorkerKind.PROVIDER_LOCAL_COMFYUI, image.run_local_comfyui)
    else:
        registry.register(
            WorkerKind.PROVIDER_LOCAL_COMFYUI,
            partial(image.run_local_comfyui, transport_factory=provider_transport_factory),
        )
    registry.register(WorkerKind.PATHS_PLAN, planning.run_path_planning)
    registry.register(WorkerKind.GCODE_BUILD, gcode.run_gcode_build)
    registry.register(WorkerKind.GCODE_CHECK, gcode.run_gcode_check)
    return registry


__all__ = ["TaskHandler", "TaskRegistry", "build_task_registry"]
