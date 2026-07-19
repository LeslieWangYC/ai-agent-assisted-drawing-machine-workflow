import logging
from collections.abc import Callable
from dataclasses import replace

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolRequest, ProtocolResponse
from drawingmachine.service.models import RequestContext, ServiceStatus

CommandHandler = Callable[[ProtocolRequest, RequestContext], JsonObject]
StatusProvider = Callable[[], ServiceStatus]

_LOGGER = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(self, status_provider: StatusProvider) -> None:
        self._status_provider = status_provider
        self._handlers: dict[str, CommandHandler] = {}
        self.register("service.status", self._service_status)

    def register(self, command: str, handler: CommandHandler) -> None:
        if command in self._handlers:
            raise DrawingMachineError(
                ErrorPayload(
                    code="COMMAND_ALREADY_REGISTERED",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"command is already registered: {command}",
                    retryable=False,
                    details={"command": command},
                )
            )
        self._handlers[command] = handler

    def dispatch(self, request: ProtocolRequest, context: RequestContext) -> ProtocolResponse:
        handler = self._handlers.get(request.command)
        if handler is None:
            return self._failure(
                request,
                ErrorPayload(
                    code="COMMAND_UNKNOWN",
                    category=ErrorCategory.INPUT,
                    message=f"unknown command: {request.command}",
                    retryable=False,
                    details={"command": request.command},
                    request_id=request.request_id,
                ),
            )

        try:
            data = handler(request, context)
        except DrawingMachineError as error:
            return self._failure(request, error.payload)
        except Exception:
            _LOGGER.error(
                "unexpected command failure command=%s request_id=%s",
                request.command,
                request.request_id,
            )
            return self._failure(
                request,
                ErrorPayload(
                    code="INTERNAL_ERROR",
                    category=ErrorCategory.INTERNAL,
                    message="internal service error",
                    retryable=False,
                    details={},
                    request_id=request.request_id,
                ),
            )

        return ProtocolResponse(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            ok=True,
            command=request.command,
            request_id=request.request_id,
            data=data,
            error=None,
        )

    def _service_status(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        del request, context
        status = self._status_provider()
        return {
            "status": "RUNNING",
            "version": status.version,
            "protocol_version": status.protocol_version,
            "database_schema_version": status.database_schema_version,
            "service_epoch": status.service_epoch,
            "started_at": status.started_at.isoformat(),
            "pid": status.pid,
        }

    @staticmethod
    def _failure(request: ProtocolRequest, payload: ErrorPayload) -> ProtocolResponse:
        payload = replace(payload, request_id=request.request_id)
        return ProtocolResponse(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            ok=False,
            command=request.command,
            request_id=request.request_id,
            data={},
            error=payload,
        )
