import io
import json
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    ProtocolRequest,
    ProtocolResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
    write_frame,
)


def _frame(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _request_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": 1,
        "command": "service.status",
        "request_id": "req-1",
        "arguments": {},
    }
    payload.update(overrides)
    return payload


def _response_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": 1,
        "schema_version": 1,
        "ok": True,
        "command": "service.status",
        "request_id": "req-1",
        "data": {"state": "RUNNING"},
        "error": None,
    }
    payload.update(overrides)
    return payload


def _assert_input_error(error: DrawingMachineError) -> None:
    assert error.payload.category is ErrorCategory.INPUT
    assert error.payload.retryable is False


def test_protocol_constants_and_models_are_versioned_and_immutable() -> None:
    assert PROTOCOL_VERSION == 1
    assert SCHEMA_VERSION == 1
    assert MAX_MESSAGE_BYTES == 1_048_576

    request = ProtocolRequest(1, "service.status", "req-1", {})
    with pytest.raises(FrozenInstanceError):
        request.command = "service.stop"  # type: ignore[misc]
    assert not hasattr(request, "__dict__")


def test_request_round_trip_and_version_rejection() -> None:
    request = ProtocolRequest(1, "service.status", "req-1", {"label": "\u7ed8\u56fe"})
    encoded = encode_request(request)

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert decode_request(encoded) == request

    raw = b'{"protocol_version":2,"command":"service.status","request_id":"req-1","arguments":{}}\n'
    with pytest.raises(DrawingMachineError, match="unsupported protocol") as error:
        decode_request(raw)
    _assert_input_error(error.value)


def test_request_round_trip_handles_finite_float_and_list_values() -> None:
    request = ProtocolRequest(1, "service.status", "req-values", {"values": [1, 2.5]})

    assert decode_request(encode_request(request)) == request


def test_decode_rejects_float_that_overflows_to_infinity() -> None:
    raw = b'{"protocol_version":1,"command":"service.status","request_id":"req-float","arguments":{"x":1e9999}}\n'

    with pytest.raises(DrawingMachineError) as error:
        decode_request(raw)

    assert error.value.payload.code == "PROTOCOL_INVALID"


def test_response_round_trip_preserves_success_and_error_payloads() -> None:
    success = ProtocolResponse(1, 1, True, "service.status", "req-1", {"state": "RUNNING"}, None)
    failure_payload = ErrorPayload(
        code="SERVICE_UNAVAILABLE",
        category=ErrorCategory.SERVICE,
        message="service unavailable",
        retryable=True,
        details={"socket": "/run/user/1000/drawingmachine/service.sock"},
        request_id="req-2",
        job_id=None,
    )
    failure = ProtocolResponse(1, 1, False, "service.status", "req-2", {}, failure_payload)

    assert decode_response(encode_response(success)) == success
    assert decode_response(encode_response(failure)) == failure


@pytest.mark.parametrize(
    "response",
    [
        ProtocolResponse(
            1,
            1,
            True,
            "service.status",
            "req-success-with-error",
            {},
            ErrorPayload("FAILED", ErrorCategory.SERVICE, "failed", False, {}),
        ),
        ProtocolResponse(1, 1, False, "service.status", "req-failure-without-error", {}, None),
    ],
    ids=["success-with-error", "failure-without-error"],
)
def test_encode_response_rejects_inconsistent_ok_and_error(response: ProtocolResponse) -> None:
    with pytest.raises(DrawingMachineError) as error:
        encode_response(response)

    assert error.value.payload.code == "PROTOCOL_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        _response_payload(
            ok=True,
            error={
                "code": "FAILED",
                "category": "service",
                "message": "failed",
                "retryable": False,
                "details": {},
                "request_id": "req-1",
                "job_id": None,
            },
        ),
        _response_payload(ok=False, error=None),
    ],
    ids=["success-with-error", "failure-without-error"],
)
def test_decode_response_rejects_inconsistent_ok_and_error(payload: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        decode_response(_frame(payload))

    assert error.value.payload.code == "PROTOCOL_INVALID"


def test_oversized_or_nan_message_is_rejected() -> None:
    with pytest.raises(DrawingMachineError, match="message too large") as error:
        decode_request(b"{" + b"x" * MAX_MESSAGE_BYTES)
    _assert_input_error(error.value)

    with pytest.raises(ValueError, match="Out of range float values"):
        encode_request(ProtocolRequest(1, "service.status", "req-2", {"value": float("nan")}))

    oversized = ProtocolRequest(1, "service.status", "req-3", {"value": "x" * MAX_MESSAGE_BYTES})
    with pytest.raises(DrawingMachineError, match="message too large") as encode_error:
        encode_request(oversized)
    _assert_input_error(encode_error.value)


def _invalid_outbound_values() -> list[tuple[str, JsonValue]]:
    return [
        ("nested-tuple", cast(JsonValue, (1, 2))),
        ("non-string-key", cast(JsonValue, {1: "value"})),
        ("unsupported-object", cast(JsonValue, object())),
    ]


@pytest.mark.parametrize(
    ("case", "value"),
    _invalid_outbound_values(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_encode_request_maps_invalid_nested_argument_values_to_typed_errors(case: str, value: JsonValue) -> None:
    request = ProtocolRequest(1, "service.status", f"req-{case}", {"nested": value})

    with pytest.raises(DrawingMachineError) as error:
        encode_request(request)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


@pytest.mark.parametrize(
    ("case", "value"),
    _invalid_outbound_values(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_encode_response_maps_invalid_nested_data_values_to_typed_errors(case: str, value: JsonValue) -> None:
    response = ProtocolResponse(1, 1, True, "service.status", f"req-{case}", {"nested": value}, None)

    with pytest.raises(DrawingMachineError) as error:
        encode_response(response)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


@pytest.mark.parametrize(
    ("case", "value"),
    _invalid_outbound_values(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_encode_response_maps_invalid_error_details_to_typed_errors(case: str, value: JsonValue) -> None:
    payload = ErrorPayload(
        code="SERVICE_UNAVAILABLE",
        category=ErrorCategory.SERVICE,
        message="service unavailable",
        retryable=True,
        details={"nested": value},
    )
    response = ProtocolResponse(1, 1, False, "service.status", f"req-{case}", {}, payload)

    with pytest.raises(DrawingMachineError) as error:
        encode_response(response)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_encode_preserves_value_error_for_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        encode_request(ProtocolRequest(1, "service.status", "req-non-finite", {"value": value}))


def test_encode_request_maps_lone_surrogate_to_typed_error() -> None:
    request = ProtocolRequest(1, "service.status", "req-surrogate", {"value": "\ud800"})

    with pytest.raises(DrawingMachineError) as error:
        encode_request(request)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_encode_request_maps_huge_integer_to_typed_error() -> None:
    request = ProtocolRequest(1, "service.status", "req-huge-int", {"value": 10**5000})

    with pytest.raises(DrawingMachineError) as error:
        encode_request(request)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_encode_response_maps_lone_surrogate_error_detail_to_typed_error() -> None:
    payload = ErrorPayload(
        code="SERVICE_UNAVAILABLE",
        category=ErrorCategory.SERVICE,
        message="service unavailable",
        retryable=True,
        details={"value": "\ud800"},
    )
    response = ProtocolResponse(1, 1, False, "service.status", "req-surrogate", {}, payload)

    with pytest.raises(DrawingMachineError) as error:
        encode_response(response)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_encode_response_maps_huge_integer_data_to_typed_error() -> None:
    response = ProtocolResponse(1, 1, True, "service.status", "req-huge-int", {"value": 10**5000}, None)

    with pytest.raises(DrawingMachineError) as error:
        encode_response(response)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_encode_maps_recursive_json_values_to_typed_errors() -> None:
    recursive: JsonObject = {}
    recursive["self"] = recursive

    with pytest.raises(DrawingMachineError) as error:
        encode_request(ProtocolRequest(1, "service.status", "req-recursive", recursive))

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_encode_maps_recursive_json_lists_to_typed_errors() -> None:
    recursive: list[JsonValue] = []
    recursive.append(recursive)

    with pytest.raises(DrawingMachineError) as error:
        encode_request(ProtocolRequest(1, "service.status", "req-recursive-list", {"value": recursive}))

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b"{}\ntrailing",
        b"{}\n{}\n",
        b"{]\n",
        b"[]\n",
        b'\xff{"protocol_version":1}\n',
        b'{"protocol_version":1,"command":"service.status","request_id":"req-1","arguments":{"x":NaN}}\n',
        b'{"protocol_version":1,"protocol_version":1,"command":"service.status","request_id":"req-1","arguments":{}}\n',
    ],
    ids=[
        "empty",
        "missing-newline",
        "bytes-after-newline",
        "second-object",
        "malformed-json",
        "non-object",
        "invalid-utf8",
        "nan",
        "duplicate-field",
    ],
)
def test_decode_request_maps_malformed_frames_to_typed_errors(raw: bytes) -> None:
    with pytest.raises(DrawingMachineError) as error:
        decode_request(raw)
    _assert_input_error(error.value)


def test_decode_maps_excessive_json_nesting_to_typed_malformed_error() -> None:
    nested_value = b'{"nested":[' * 600 + b"0" + b"]}" * 600
    raw = (
        b'{"protocol_version":1,"command":"service.status","request_id":"req-deep","arguments":{"value":'
        + nested_value
        + b"}}\n"
    )
    assert len(raw) < MAX_MESSAGE_BYTES

    with pytest.raises(DrawingMachineError, match="malformed protocol message") as error:
        decode_request(raw)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


def test_decode_accepts_nesting_at_the_explicit_depth_boundary() -> None:
    nested_value = b"[" * 62 + b"0" + b"]" * 62
    raw = (
        b'{"protocol_version":1,"command":"service.status","request_id":"req-depth","arguments":{"value":'
        + nested_value
        + b',"text":"literal \\" [ { brackets"}}\n'
    )

    request = decode_request(raw)

    assert request.arguments["text"] == 'literal " [ { brackets'


def test_decode_rejects_nesting_one_level_beyond_the_depth_boundary() -> None:
    nested_value = b"[" * 63 + b"0" + b"]" * 63
    raw = (
        b'{"protocol_version":1,"command":"service.status","request_id":"req-depth","arguments":{"value":'
        + nested_value
        + b"}}\n"
    )

    with pytest.raises(DrawingMachineError, match="malformed protocol message") as error:
        decode_request(raw)

    assert error.value.payload.code == "PROTOCOL_INVALID"
    _assert_input_error(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": 1, "command": "service.status", "request_id": "req-1"},
        _request_payload(extra=True),
        _request_payload(protocol_version=True),
        _request_payload(protocol_version="1"),
        _request_payload(command=1),
        _request_payload(request_id=None),
        _request_payload(arguments=[]),
    ],
    ids=[
        "missing-field",
        "extra-field",
        "boolean-version",
        "string-version",
        "command-type",
        "request-id-type",
        "arguments-type",
    ],
)
def test_decode_request_rejects_missing_extra_or_invalid_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        decode_request(_frame(payload))
    _assert_input_error(error.value)


@pytest.mark.parametrize("version", [2, True, "1"])
def test_decode_response_rejects_unsupported_or_invalid_schema_versions(version: object) -> None:
    with pytest.raises(DrawingMachineError) as error:
        decode_response(_frame(_response_payload(schema_version=version)))
    _assert_input_error(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        _response_payload(extra=True),
        {key: value for key, value in _response_payload().items() if key != "data"},
        _response_payload(protocol_version=True),
        _response_payload(ok=1),
        _response_payload(command=None),
        _response_payload(request_id=3),
        _response_payload(data=[]),
        _response_payload(error=[]),
        _response_payload(
            ok=False,
            error={
                "code": "FAILED",
                "category": "service",
                "message": "failed",
                "retryable": False,
                "details": {},
                "request_id": 7,
                "job_id": None,
            },
        ),
        _response_payload(
            ok=False,
            error={
                "code": "FAILED",
                "category": "unknown",
                "message": "failed",
                "retryable": False,
                "details": {},
                "request_id": None,
                "job_id": None,
            },
        ),
        _response_payload(
            ok=False,
            error={
                "code": "FAILED",
                "category": "service",
                "message": "failed",
                "retryable": False,
                "details": {},
                "request_id": None,
                "job_id": None,
                "extra": True,
            },
        ),
    ],
    ids=[
        "extra-field",
        "missing-field",
        "boolean-protocol-version",
        "ok-type",
        "command-type",
        "request-id-type",
        "data-type",
        "error-type",
        "error-request-id-type",
        "error-category",
        "error-extra-field",
    ],
)
def test_decode_response_rejects_missing_extra_or_invalid_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        decode_response(_frame(payload))
    _assert_input_error(error.value)


def test_read_and_write_frame_use_one_bounded_newline_terminated_object() -> None:
    encoded = encode_request(ProtocolRequest(1, "service.status", "req-1", {}))
    stream = io.BytesIO()

    write_frame(stream, encoded)
    stream.seek(0)

    assert read_frame(stream) == encoded


@pytest.mark.parametrize("raw", [b"", b'{"protocol_version":1}'])
def test_read_frame_maps_early_eof_to_typed_protocol_error(raw: bytes) -> None:
    with pytest.raises(DrawingMachineError, match="early EOF") as error:
        read_frame(io.BytesIO(raw))
    _assert_input_error(error.value)


def test_read_frame_rejects_oversized_input_before_json_parsing() -> None:
    with pytest.raises(DrawingMachineError, match="message too large") as error:
        read_frame(io.BytesIO(b"x" * (MAX_MESSAGE_BYTES + 1)))
    _assert_input_error(error.value)


@pytest.mark.parametrize("raw", [b"{}", b"{}\ntrailing", b"[]\n", b"\xff\n"])
def test_write_frame_rejects_invalid_frames(raw: bytes) -> None:
    with pytest.raises(DrawingMachineError) as error:
        write_frame(io.BytesIO(), raw)
    _assert_input_error(error.value)


def test_write_frame_rejects_zero_length_write() -> None:
    class ZeroWriter(io.BytesIO):
        def write(self, data: bytes) -> int:
            del data
            return 0

    with pytest.raises(DrawingMachineError) as error:
        write_frame(ZeroWriter(), encode_request(ProtocolRequest(1, "service.status", "req-write", {})))

    assert error.value.payload.code == "PROTOCOL_WRITE_FAILED"
