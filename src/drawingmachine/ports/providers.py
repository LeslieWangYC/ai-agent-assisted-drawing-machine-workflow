from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath, PurePosixPath
from typing import NoReturn, Protocol, Self, cast, overload

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_SCHEMA_VERSION = 1

MAX_PROVIDER_PROMPT_BYTES = 1_048_576
MAX_PROVIDER_IDENTIFIER_BYTES = 255

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ROLE = re.compile(r"[a-z][a-z0-9_.-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; [A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9._-]+)*")
_FROZEN_TOKEN = object()


def _error(message: str, *, field: str | None = None) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code="PROVIDER_CONTRACT_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details={} if field is None else {"field": field},
        )
    )


def _invalid(message: str, *, field: str | None = None) -> NoReturn:
    raise _error(message, field=field)


def _schema(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{field} must be an integer", field=field)
    if value != _SCHEMA_VERSION:
        _invalid(f"{field} has unsupported schema version: {value}", field=field)
    return value


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _invalid(f"{field} must be a string", field=field)
    if not allow_empty and not value:
        _invalid(f"{field} must not be empty", field=field)
    invalid_unicode = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        invalid_unicode = True
    if invalid_unicode:
        raise _error(f"{field} contains invalid Unicode", field=field)
    return value


def _bounded_string(value: object, field: str, *, maximum_utf8_bytes: int, unit: str) -> str:
    if type(value) is not str:
        _invalid(f"{field} must be a string", field=field)
    if not value:
        _invalid(f"{field} must not be empty", field=field)
    if len(value) > maximum_utf8_bytes:
        _invalid(f"{field} must be at most {maximum_utf8_bytes} {unit} bytes", field=field)
    invalid_unicode = False
    encoded = b""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        invalid_unicode = True
    if invalid_unicode:
        raise _error(f"{field} contains invalid Unicode", field=field)
    if len(encoded) > maximum_utf8_bytes:
        _invalid(f"{field} must be at most {maximum_utf8_bytes} {unit} bytes", field=field)
    return value


def _identifier(value: object, field: str) -> str:
    result = _bounded_string(
        value,
        field,
        maximum_utf8_bytes=MAX_PROVIDER_IDENTIFIER_BYTES,
        unit="ASCII",
    )
    if result in {".", ".."} or _IDENTIFIER.fullmatch(result) is None:
        _invalid(
            f"{field} must be a safe identifier of at most {MAX_PROVIDER_IDENTIFIER_BYTES} ASCII bytes",
            field=field,
        )
    return result


def _prompt(value: object, field: str) -> str:
    return _bounded_string(
        value,
        field,
        maximum_utf8_bytes=MAX_PROVIDER_PROMPT_BYTES,
        unit="UTF-8",
    )


def _absolute_path(value: object, field: str) -> str:
    result = _string(value, field)
    path = PurePath(result)
    if (
        "\x00" in result
        or not path.is_absolute()
        or str(path) != result
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a canonical absolute path", field=field)
    return result


def _relative_path(value: object, field: str) -> str:
    result = _string(value, field)
    path = PurePosixPath(result)
    if (
        "\x00" in result
        or "\\" in result
        or path.is_absolute()
        or str(path) != result
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a canonical relative_path", field=field)
    return result


def _digest(value: object, field: str) -> str:
    result = _string(value, field)
    if _SHA256.fullmatch(result) is None:
        _invalid(f"{field} must be a lowercase SHA-256 digest", field=field)
    return result


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        _invalid(f"{context} has unknown keys: {', '.join(unknown)}", field=context)
    missing = sorted(expected - set(value))
    if missing:
        _invalid(f"{context} has missing keys: {', '.join(missing)}", field=context)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{context} must be a JSON object", field=context)
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if type(key) is not str:
            _invalid(f"{context} contains a non-string key", field=context)
        result[key] = item
    return result


class _FrozenJsonObject(Mapping[str, JsonValue]):
    __slots__ = ("_items",)
    _items: tuple[tuple[str, JsonValue], ...]

    def __init__(self, token: object, items: tuple[tuple[str, JsonValue], ...]) -> None:
        if token is not _FROZEN_TOKEN or hasattr(self, "_items"):
            raise TypeError("provider JSON mapping is immutable")
        object.__setattr__(self, "_items", items)

    @classmethod
    def create(cls, items: Iterable[tuple[str, JsonValue]]) -> Self:
        return cls(_FROZEN_TOKEN, tuple(items))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("provider JSON mapping is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("provider JSON mapping is immutable")

    def __getitem__(self, key: str) -> JsonValue:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        memo[id(self)] = self
        return self


class _FrozenJsonArray(Sequence[JsonValue]):
    __slots__ = ("_values",)
    _values: tuple[JsonValue, ...]

    def __init__(self, token: object, values: tuple[JsonValue, ...]) -> None:
        if token is not _FROZEN_TOKEN or hasattr(self, "_values"):
            raise TypeError("provider JSON array is immutable")
        object.__setattr__(self, "_values", values)

    @classmethod
    def create(cls, values: Iterable[JsonValue]) -> Self:
        return cls(_FROZEN_TOKEN, tuple(values))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("provider JSON array is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("provider JSON array is immutable")

    @overload
    def __getitem__(self, index: int) -> JsonValue: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[JsonValue, ...]: ...

    def __getitem__(self, index: int | slice) -> JsonValue | tuple[JsonValue, ...]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[JsonValue]:
        return iter(self._values)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, str | bytes | bytearray)
            and self._values == tuple(other)
        )

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        memo[id(self)] = self
        return self


def _freeze_json(value: object, context: str, active: set[int] | None = None) -> JsonValue:
    seen = set() if active is None else active
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _string(value, context, allow_empty=True)
    if isinstance(value, int):
        try:
            str(value)
        except ValueError as error:
            raise _error(f"{context} integer cannot be serialized", field=context) from error
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid(f"{context} must be finite", field=context)
        return value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        marker = id(value)
        if marker in seen:
            _invalid(f"{context} contains a recursive JSON value", field=context)
        seen.add(marker)
        try:
            return cast(JsonValue, _FrozenJsonArray.create(_freeze_json(item, f"{context}[]", seen) for item in value))
        finally:
            seen.remove(marker)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            _invalid(f"{context} contains a recursive JSON value", field=context)
        seen.add(marker)
        try:
            items: list[tuple[str, JsonValue]] = []
            for key, item in cast(Mapping[object, object], value).items():
                if type(key) is not str:
                    _invalid(f"{context} contains a non-string key", field=context)
                items.append((key, _freeze_json(item, f"{context}.{key}", seen)))
            return cast(JsonValue, _FrozenJsonObject.create(items))
        finally:
            seen.remove(marker)
    _invalid(f"{context} contains a non-JSON value", field=context)


def _freeze_json_boundary(value: object, context: str) -> JsonValue:
    recursion_failed = False
    frozen: JsonValue = None
    try:
        frozen = _freeze_json(value, context)
    except RecursionError:
        recursion_failed = True
    if recursion_failed:
        raise _error(f"{context} exceeds the supported JSON depth", field=context)
    return frozen


def _thaw_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_error(value: ErrorPayload | None, field: str) -> ErrorPayload | None:
    if value is None:
        return None
    if not isinstance(value, ErrorPayload):
        _invalid(f"{field} must be an ErrorPayload or null", field=field)
    if not isinstance(value.category, ErrorCategory):
        _invalid(f"{field}.category must be an ErrorCategory", field=f"{field}.category")
    if not isinstance(value.retryable, bool):
        _invalid(f"{field}.retryable must be a boolean", field=f"{field}.retryable")
    details = _freeze_json_boundary(value.details, f"{field}.details")
    if not isinstance(details, Mapping):
        _invalid(f"{field}.details must be a JSON object", field=field)
    return ErrorPayload(
        code=_identifier(value.code, f"{field}.code"),
        category=value.category,
        message=_string(value.message, f"{field}.message"),
        retryable=value.retryable,
        details=cast(Mapping[str, JsonValue], details),
        request_id=None if value.request_id is None else _identifier(value.request_id, f"{field}.request_id"),
        job_id=None if value.job_id is None else _identifier(value.job_id, f"{field}.job_id"),
    )


def _error_to_json(value: ErrorPayload | None) -> JsonObject | None:
    if value is None:
        return None
    result = value.to_json()
    details = result["details"]
    if isinstance(details, Mapping):
        result["details"] = cast(JsonObject, _thaw_json(cast(JsonValue, details)))
    return result


class ProviderStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ProviderPollState(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProviderRequestV1:
    schema_version: int
    request_id: str
    input_path: str
    input_sha256: str
    prompt: str | None
    output_prefix: str

    def __post_init__(self) -> None:
        _schema(self.schema_version, "provider_request.schema_version")
        _identifier(self.request_id, "provider_request.request_id")
        _absolute_path(self.input_path, "provider_request.input_path")
        _digest(self.input_sha256, "provider_request.input_sha256")
        if self.prompt is not None:
            _prompt(self.prompt, "provider_request.prompt")
        _identifier(self.output_prefix, "provider_request.output_prefix")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "input_path": self.input_path,
            "input_sha256": self.input_sha256,
            "prompt": self.prompt,
            "output_prefix": self.output_prefix,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        data = _object(value, "provider_request")
        _exact_fields(
            data,
            frozenset({"schema_version", "request_id", "input_path", "input_sha256", "prompt", "output_prefix"}),
            "provider_request",
        )
        prompt = data["prompt"]
        return cls(
            schema_version=_schema(data["schema_version"], "provider_request.schema_version"),
            request_id=_identifier(data["request_id"], "provider_request.request_id"),
            input_path=_absolute_path(data["input_path"], "provider_request.input_path"),
            input_sha256=_digest(data["input_sha256"], "provider_request.input_sha256"),
            prompt=None if prompt is None else _prompt(prompt, "provider_request.prompt"),
            output_prefix=_identifier(data["output_prefix"], "provider_request.output_prefix"),
        )


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    role: str
    relative_path: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if _ROLE.fullmatch(_string(self.role, "provider_record.role")) is None:
            _invalid("provider_record.role must be a safe provider role", field="provider_record.role")
        _relative_path(self.relative_path, "provider_record.relative_path")
        if _MEDIA_TYPE.fullmatch(_string(self.media_type, "provider_record.media_type")) is None:
            _invalid("provider_record.media_type must be a valid media type", field="provider_record.media_type")
        if not isinstance(self.content, bytes):
            _invalid("provider_record.content must be bytes", field="provider_record.content")


@dataclass(frozen=True, slots=True)
class PreparedProviderRequest:
    schema_version: int
    request_id: str
    workflow: JsonObject
    upload_name: str
    record: ProviderRecord

    def __post_init__(self) -> None:
        _schema(self.schema_version, "prepared_request.schema_version")
        _identifier(self.request_id, "prepared_request.request_id")
        workflow = _freeze_json_boundary(self.workflow, "prepared_request.workflow")
        if not isinstance(workflow, Mapping):
            _invalid("prepared_request.workflow must be a JSON object", field="prepared_request.workflow")
        object.__setattr__(self, "workflow", workflow)
        upload_name = _string(self.upload_name, "prepared_request.upload_name")
        if (
            PurePath(upload_name).name != upload_name
            or upload_name in {".", ".."}
            or any(character in upload_name for character in ("\x00", "\r", "\n", '"'))
        ):
            _invalid("prepared_request.upload_name must be a filename", field="prepared_request.upload_name")
        if not isinstance(self.record, ProviderRecord):
            _invalid("prepared_request.record must be a ProviderRecord", field="prepared_request.record")


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    schema_version: int
    request_id: str
    provider_job_id: str

    def __post_init__(self) -> None:
        _schema(self.schema_version, "provider_submission.schema_version")
        _identifier(self.request_id, "provider_submission.request_id")
        job_id = _string(self.provider_job_id, "provider_submission.provider_job_id")
        if "\x00" in job_id or len(job_id) > 512:
            _invalid(
                "provider_submission.provider_job_id must be a bounded external identity",
                field="provider_submission.provider_job_id",
            )


@dataclass(frozen=True, slots=True)
class ProviderPollResult:
    schema_version: int
    state: ProviderPollState
    retry_after_seconds: float | None
    error: ErrorPayload | None

    def __post_init__(self) -> None:
        _schema(self.schema_version, "provider_poll.schema_version")
        if not isinstance(self.state, ProviderPollState):
            _invalid("provider_poll.state must be a ProviderPollState", field="provider_poll.state")
        retry = self.retry_after_seconds
        if self.state is ProviderPollState.PENDING:
            if isinstance(retry, bool) or not isinstance(retry, int | float):
                _invalid(
                    "pending provider_poll requires retry_after_seconds", field="provider_poll.retry_after_seconds"
                )
            normalized = float(retry)
            if not math.isfinite(normalized) or normalized <= 0 or normalized > 10_000:
                _invalid(
                    "provider_poll.retry_after_seconds must be finite, positive, and bounded",
                    field="provider_poll.retry_after_seconds",
                )
            object.__setattr__(self, "retry_after_seconds", normalized)
            if self.error is not None:
                _invalid("pending provider_poll must not have an error", field="provider_poll.error")
        elif self.state is ProviderPollState.SUCCEEDED:
            if retry is not None or self.error is not None:
                _invalid("succeeded provider_poll must not have retry or error", field="provider_poll")
        else:
            if retry is not None or self.error is None:
                _invalid("failed provider_poll requires only an error", field="provider_poll")
        object.__setattr__(self, "error", _freeze_error(self.error, "provider_poll.error"))


@dataclass(frozen=True, slots=True)
class ProviderResultV1:
    schema_version: int
    status: ProviderStatus
    processed_image: bytes | None
    records: tuple[ProviderRecord, ...]
    error: ErrorPayload | None

    def __post_init__(self) -> None:
        _schema(self.schema_version, "provider_result.schema_version")
        if not isinstance(self.status, ProviderStatus):
            _invalid("provider_result.status must be a ProviderStatus", field="provider_result.status")
        if self.processed_image is not None and not isinstance(self.processed_image, bytes):
            _invalid("provider_result.processed_image must be bytes or null", field="provider_result.processed_image")
        try:
            records = tuple(self.records)
        except TypeError as error:
            raise _error(
                "provider_result.records must be an iterable of ProviderRecord", field="provider_result.records"
            ) from error
        if any(not isinstance(record, ProviderRecord) for record in records):
            _invalid("provider_result.records must contain only ProviderRecord", field="provider_result.records")
        object.__setattr__(self, "records", records)
        frozen_error = _freeze_error(self.error, "provider_result.error")
        object.__setattr__(self, "error", frozen_error)
        if self.status is ProviderStatus.SUCCEEDED:
            if self.processed_image is None or frozen_error is not None:
                _invalid("succeeded provider_result requires an image and no error", field="provider_result")
        elif self.processed_image is not None or frozen_error is None:
            _invalid("failed or blocked provider_result requires no image and an error", field="provider_result")


class ImageProvider(Protocol):
    @property
    def name(self) -> str: ...

    def validate_config(self) -> None: ...

    def create_request(self, request: ProviderRequestV1) -> PreparedProviderRequest: ...

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission: ...

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult: ...

    def retrieve(self, submission: ProviderSubmission) -> ProviderResultV1: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ImageProvider] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register(self, provider: ImageProvider) -> None:
        name = _identifier(provider.name, "provider.name")
        if name in self._providers:
            raise DrawingMachineError(
                ErrorPayload(
                    code="PROVIDER_ALREADY_REGISTERED",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"provider {name!r} is already registered",
                    retryable=False,
                    details={"provider": name},
                )
            )
        self._providers[name] = provider

    def get(self, name: str) -> ImageProvider:
        normalized = _identifier(name, "provider.name")
        try:
            return self._providers[normalized]
        except KeyError as error:
            raise DrawingMachineError(
                ErrorPayload(
                    code="PROVIDER_NOT_REGISTERED",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"provider {normalized!r} is not registered",
                    retryable=False,
                    details={"provider": normalized},
                )
            ) from error
