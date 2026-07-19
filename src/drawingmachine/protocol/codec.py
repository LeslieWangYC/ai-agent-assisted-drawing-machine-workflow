import json
import math
from typing import BinaryIO, NoReturn, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol.models import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    ProtocolRequest,
    ProtocolResponse,
)

_REQUEST_FIELDS = frozenset({"protocol_version", "command", "request_id", "arguments"})
_RESPONSE_FIELDS = frozenset({"protocol_version", "schema_version", "ok", "command", "request_id", "data", "error"})
_ERROR_FIELDS = frozenset({"code", "category", "message", "retryable", "details", "request_id", "job_id"})

_MAX_JSON_NESTING_DEPTH = 64
_JSON_QUOTE = ord('"')
_JSON_ESCAPE = ord("\\")
_JSON_OPENERS = frozenset((ord("{"), ord("[")))
_JSON_CLOSERS = frozenset((ord("}"), ord("]")))


def _protocol_error(code: str, message: str, *, details: JsonObject | None = None) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details={} if details is None else details,
        )
    )


def _raise_invalid(message: str) -> NoReturn:
    raise _protocol_error("PROTOCOL_INVALID", message)


def _raise_too_large(size: int) -> NoReturn:
    raise _protocol_error(
        "PROTOCOL_MESSAGE_TOO_LARGE",
        "protocol message too large",
        details={"maximum_bytes": MAX_MESSAGE_BYTES, "actual_bytes": size},
    )


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


# json.loads only rejects deep nesting through the C parser's RecursionError, whose
# ceiling is interpreter-dependent (CPython 3.12 raised it), so depth must be bounded
# explicitly before parsing. Structural bytes cannot occur inside UTF-8 continuation
# sequences, so the scan is safe on undecoded bytes.
def _ensure_nesting_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _JSON_ESCAPE:
                escaped = True
            elif byte == _JSON_QUOTE:
                in_string = False
        elif byte == _JSON_QUOTE:
            in_string = True
        elif byte in _JSON_OPENERS:
            depth += 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise _protocol_error(
                    "PROTOCOL_INVALID",
                    "malformed protocol message",
                    details={"reason": f"nesting depth exceeds {_MAX_JSON_NESTING_DEPTH}"},
                )
        elif byte in _JSON_CLOSERS:
            depth -= 1


def _decode_object(frame: bytes) -> dict[str, object]:
    if len(frame) > MAX_MESSAGE_BYTES:
        _raise_too_large(len(frame))
    if not frame.endswith(b"\n"):
        _raise_invalid("protocol message must be newline terminated")
    if b"\n" in frame[:-1]:
        _raise_invalid("protocol message has trailing bytes")
    _ensure_nesting_depth(frame[:-1])

    try:
        text = frame[:-1].decode("utf-8")
        value = cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
                parse_float=_parse_json_float,
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise _protocol_error(
            "PROTOCOL_INVALID",
            "malformed protocol message",
            details={"reason": str(error)},
        ) from error

    if not isinstance(value, dict):
        _raise_invalid("protocol message must contain a JSON object")
    return cast(dict[str, object], value)


def _validate_json_value(value: object, active_containers: set[int]) -> bool:
    if value is None or isinstance(value, bool):
        return False

    if isinstance(value, int):
        try:
            str(value)
        except ValueError as error:
            raise _protocol_error("PROTOCOL_INVALID", "protocol message integer cannot be serialized") from error
        return False

    if isinstance(value, float):
        return not math.isfinite(value)

    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _protocol_error("PROTOCOL_INVALID", "protocol message contains invalid Unicode") from error
        return False

    if isinstance(value, list):
        marker = id(value)
        if marker in active_containers:
            _raise_invalid("protocol message contains a recursive JSON value")
        active_containers.add(marker)
        contains_non_finite_float = False
        try:
            for item in cast(list[object], value):
                contains_non_finite_float |= _validate_json_value(item, active_containers)
        finally:
            active_containers.remove(marker)
        return contains_non_finite_float

    if isinstance(value, dict):
        marker = id(value)
        if marker in active_containers:
            _raise_invalid("protocol message contains a recursive JSON value")
        active_containers.add(marker)
        contains_non_finite_float = False
        try:
            for key, item in cast(dict[object, object], value).items():
                if not isinstance(key, str):
                    _raise_invalid("protocol message JSON object keys must be strings")
                _validate_json_value(key, active_containers)
                contains_non_finite_float |= _validate_json_value(item, active_containers)
        finally:
            active_containers.remove(marker)
        return contains_non_finite_float

    _raise_invalid(f"protocol message contains unsupported JSON value type: {type(value).__name__}")


def _validate_outbound_object(payload: JsonObject) -> bool:
    try:
        return _validate_json_value(payload, set())
    except RecursionError as error:
        raise _protocol_error("PROTOCOL_INVALID", "protocol message nesting is too deep") from error


def _encode_object(payload: JsonObject) -> bytes:
    contains_non_finite_float = _validate_outbound_object(payload)
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except ValueError as error:
        if contains_non_finite_float:
            raise
        raise _protocol_error("PROTOCOL_INVALID", "invalid outbound protocol message") from error
    except (RecursionError, TypeError, UnicodeEncodeError) as error:
        raise _protocol_error("PROTOCOL_INVALID", "invalid outbound protocol message") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        _raise_too_large(len(encoded))
    return encoded


def _expect_fields(payload: dict[str, object], fields: frozenset[str], kind: str) -> None:
    if payload.keys() != fields:
        _raise_invalid(f"{kind} has missing or extra fields")


def _expect_version(value: object, expected: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise_invalid(f"{name} must be an integer")
    if value != expected:
        label = "protocol" if name == "protocol_version" else "schema"
        raise _protocol_error(
            "PROTOCOL_UNSUPPORTED_VERSION",
            f"unsupported {label} version: {value}",
            details={"expected": expected, "received": value},
        )
    return value


def _expect_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _raise_invalid(f"{name} must be a string")
    return value


def _expect_object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        _raise_invalid(f"{name} must be a JSON object")
    return cast(JsonObject, value)


def _expect_boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        _raise_invalid(f"{name} must be a boolean")
    return value


def _expect_nullable_string(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _raise_invalid(f"{name} must be a string or null")
    return value


def _parse_error_payload(value: object) -> ErrorPayload | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _raise_invalid("error must be an object or null")

    error_object = cast(dict[str, object], value)
    _expect_fields(error_object, _ERROR_FIELDS, "error payload")
    category_value = _expect_string(error_object["category"], "error.category")
    try:
        category = ErrorCategory(category_value)
    except ValueError as error:
        raise _protocol_error(
            "PROTOCOL_INVALID",
            "error.category is invalid",
            details={"category": category_value},
        ) from error

    return ErrorPayload(
        code=_expect_string(error_object["code"], "error.code"),
        category=category,
        message=_expect_string(error_object["message"], "error.message"),
        retryable=_expect_boolean(error_object["retryable"], "error.retryable"),
        details=_expect_object(error_object["details"], "error.details"),
        request_id=_expect_nullable_string(error_object["request_id"], "error.request_id"),
        job_id=_expect_nullable_string(error_object["job_id"], "error.job_id"),
    )


def _parse_request(payload: dict[str, object]) -> ProtocolRequest:
    _expect_fields(payload, _REQUEST_FIELDS, "protocol request")
    return ProtocolRequest(
        protocol_version=_expect_version(payload["protocol_version"], PROTOCOL_VERSION, "protocol_version"),
        command=_expect_string(payload["command"], "command"),
        request_id=_expect_string(payload["request_id"], "request_id"),
        arguments=_expect_object(payload["arguments"], "arguments"),
    )


def _parse_response(payload: dict[str, object]) -> ProtocolResponse:
    _expect_fields(payload, _RESPONSE_FIELDS, "protocol response")
    ok = _expect_boolean(payload["ok"], "ok")
    error = _parse_error_payload(payload["error"])
    if ok and error is not None:
        _raise_invalid("successful protocol response must not contain an error")
    if not ok and error is None:
        _raise_invalid("failed protocol response must contain an error")
    return ProtocolResponse(
        protocol_version=_expect_version(payload["protocol_version"], PROTOCOL_VERSION, "protocol_version"),
        schema_version=_expect_version(payload["schema_version"], SCHEMA_VERSION, "schema_version"),
        ok=ok,
        command=_expect_string(payload["command"], "command"),
        request_id=_expect_string(payload["request_id"], "request_id"),
        data=_expect_object(payload["data"], "data"),
        error=error,
    )


def encode_request(request: ProtocolRequest) -> bytes:
    payload: JsonObject = {
        "protocol_version": request.protocol_version,
        "command": request.command,
        "request_id": request.request_id,
        "arguments": request.arguments,
    }
    _parse_request(cast(dict[str, object], payload))
    return _encode_object(payload)


def decode_request(frame: bytes) -> ProtocolRequest:
    return _parse_request(_decode_object(frame))


def encode_response(response: ProtocolResponse) -> bytes:
    error: JsonObject | None = None if response.error is None else response.error.to_json()
    payload: JsonObject = {
        "protocol_version": response.protocol_version,
        "schema_version": response.schema_version,
        "ok": response.ok,
        "command": response.command,
        "request_id": response.request_id,
        "data": response.data,
        "error": error,
    }
    _parse_response(cast(dict[str, object], payload))
    return _encode_object(payload)


def decode_response(frame: bytes) -> ProtocolResponse:
    return _parse_response(_decode_object(frame))


def read_frame(stream: BinaryIO) -> bytes:
    frame = stream.readline(MAX_MESSAGE_BYTES + 1)
    if len(frame) > MAX_MESSAGE_BYTES:
        _raise_too_large(len(frame))
    if not frame or not frame.endswith(b"\n"):
        raise _protocol_error("PROTOCOL_EARLY_EOF", "early EOF while reading protocol message")
    _decode_object(frame)
    return frame


def write_frame(stream: BinaryIO, frame: bytes) -> None:
    _decode_object(frame)
    remaining = frame
    while remaining:
        written = stream.write(remaining)
        if written <= 0:
            raise _protocol_error("PROTOCOL_WRITE_FAILED", "failed to write protocol message")
        remaining = remaining[written:]
    stream.flush()
