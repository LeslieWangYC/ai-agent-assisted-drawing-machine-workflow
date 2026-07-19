from datetime import UTC, datetime

import pytest

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolRequest
from drawingmachine.service import (
    CommandDispatcher,
    PeerCredentials,
    RequestContext,
    ServiceStatus,
)


def _status() -> ServiceStatus:
    return ServiceStatus(
        version="0.2.0.dev0",
        protocol_version=PROTOCOL_VERSION,
        database_schema_version=1,
        service_epoch="66f5ce64-24f6-4e77-8dc7-ef1ea6153b67",
        started_at=datetime(2026, 7, 10, tzinfo=UTC),
        pid=42,
    )


def _context() -> RequestContext:
    return RequestContext(
        peer=PeerCredentials(pid=10, uid=1000, gid=1000),
        received_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


def test_service_status_is_registered_and_returns_versioned_data() -> None:
    dispatcher = CommandDispatcher(_status)
    request = ProtocolRequest(PROTOCOL_VERSION, "service.status", "req-status", {})

    response = dispatcher.dispatch(request, _context())

    assert response.ok is True
    assert response.protocol_version == PROTOCOL_VERSION
    assert response.schema_version == SCHEMA_VERSION
    assert response.command == "service.status"
    assert response.request_id == "req-status"
    assert response.data == {
        "status": "RUNNING",
        "version": "0.2.0.dev0",
        "protocol_version": 1,
        "database_schema_version": 1,
        "service_epoch": "66f5ce64-24f6-4e77-8dc7-ef1ea6153b67",
        "started_at": "2026-07-10T00:00:00+00:00",
        "pid": 42,
    }
    assert response.error is None


def test_unknown_command_returns_typed_error() -> None:
    dispatcher = CommandDispatcher(_status)
    request = ProtocolRequest(PROTOCOL_VERSION, "unknown.command", "req-unknown", {})

    response = dispatcher.dispatch(request, _context())

    assert response.ok is False
    assert response.data == {}
    assert response.error is not None
    assert response.error.code == "COMMAND_UNKNOWN"
    assert response.error.category is ErrorCategory.INPUT
    assert response.error.retryable is False
    assert response.error.request_id == "req-unknown"


def test_dispatcher_rejects_duplicate_command_registration() -> None:
    dispatcher = CommandDispatcher(_status)
    dispatcher.register("job.status", lambda request, context: {})

    with pytest.raises(DrawingMachineError, match="already registered") as error:
        dispatcher.register("job.status", lambda request, context: {})

    assert error.value.payload.code == "COMMAND_ALREADY_REGISTERED"


def test_known_drawingmachine_error_is_preserved_in_failed_response() -> None:
    dispatcher = CommandDispatcher(_status)

    def fail_known(request: ProtocolRequest, context: RequestContext) -> JsonObject:
        del request, context
        raise DrawingMachineError(
            ErrorPayload(
                code="SERVICE_BUSY",
                category=ErrorCategory.SERVICE,
                message="service is busy",
                retryable=True,
                details={"state": "STARTING"},
            )
        )

    dispatcher.register("test.known", fail_known)
    request = ProtocolRequest(PROTOCOL_VERSION, "test.known", "req-known", {})

    response = dispatcher.dispatch(request, _context())

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "SERVICE_BUSY"
    assert response.error.retryable is True
    assert response.error.details == {"state": "STARTING"}
    assert response.error.request_id == "req-known"


def test_drawingmachine_error_request_id_is_replaced_with_current_request_id() -> None:
    dispatcher = CommandDispatcher(_status)

    def fail_with_stale_request(request: ProtocolRequest, context: RequestContext) -> JsonObject:
        del request, context
        raise DrawingMachineError(
            ErrorPayload(
                code="SERVICE_BUSY",
                category=ErrorCategory.SERVICE,
                message="service is busy",
                retryable=True,
                details={},
                request_id="req-stale",
            )
        )

    dispatcher.register("test.stale", fail_with_stale_request)
    response = dispatcher.dispatch(
        ProtocolRequest(PROTOCOL_VERSION, "test.stale", "req-current", {}),
        _context(),
    )

    assert response.request_id == "req-current"
    assert response.error is not None
    assert response.error.request_id == "req-current"


def test_unexpected_error_is_masked_without_stdout_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatcher = CommandDispatcher(_status)

    def fail_unexpected(request: ProtocolRequest, context: RequestContext) -> JsonObject:
        del request, context
        raise RuntimeError("secret implementation detail")

    dispatcher.register("test.unexpected", fail_unexpected)
    request = ProtocolRequest(PROTOCOL_VERSION, "test.unexpected", "req-unexpected", {})

    response = dispatcher.dispatch(request, _context())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "INTERNAL_ERROR"
    assert response.error.category is ErrorCategory.INTERNAL
    assert response.error.retryable is False
    assert response.error.details == {}
    assert response.error.request_id == "req-unexpected"
    assert "secret implementation detail" not in response.error.message
