from datetime import datetime

from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.clock import Clock


def test_error_payload_is_json_safe() -> None:
    payload = ErrorPayload(
        code="CONFIG_INVALID",
        category=ErrorCategory.CONFIGURATION,
        message="invalid config",
        retryable=False,
        details={"field": "schema_version"},
        request_id="req-1",
        job_id=None,
    )
    assert payload.to_json()["category"] == "configuration"


def test_error_payload_to_json_includes_all_fields() -> None:
    payload = ErrorPayload(
        code="INPUT_INVALID",
        category=ErrorCategory.INPUT,
        message="invalid input",
        retryable=True,
        details={"field": "source", "position": 3},
    )

    assert payload.to_json() == {
        "code": "INPUT_INVALID",
        "category": "input",
        "message": "invalid input",
        "retryable": True,
        "details": {"field": "source", "position": 3},
        "request_id": None,
        "job_id": None,
    }


def test_drawing_machine_error_exposes_payload_and_message() -> None:
    payload = ErrorPayload(
        code="INTERNAL_ERROR",
        category=ErrorCategory.INTERNAL,
        message="unexpected failure",
        retryable=False,
        details={},
    )

    error = DrawingMachineError(payload)

    assert error.payload is payload
    assert str(error) == "unexpected failure"


def test_error_categories_have_stable_wire_values() -> None:
    assert {category.value for category in ErrorCategory} == {
        "input",
        "configuration",
        "provider",
        "planning",
        "validation",
        "service",
        "hardware",
        "permission",
        "internal",
    }


def test_system_clock_returns_timezone_aware_seconds() -> None:
    clock: Clock = SystemClock()

    current_time = clock.now()

    assert isinstance(current_time, datetime)
    assert current_time.tzinfo is not None
    assert current_time.microsecond == 0
