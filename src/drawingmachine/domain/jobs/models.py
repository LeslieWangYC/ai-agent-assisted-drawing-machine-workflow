from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import NoReturn, Self, TypeVar, cast, overload

from drawingmachine.domain.jobs.states import JobKind, JobState, _job_states
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_FIELDS = frozenset({"role", "relative_path", "sha256", "size_bytes", "media_type"})
_CONFIG_FIELDS = frozenset(
    {
        "machine_profile",
        "provider_profile",
        "application_schema_version",
        "machine_schema_version",
        "provider_schema_version",
        "application_digest",
        "machine_digest",
        "provider_digest",
    }
)
_BLOCKER_FIELDS = frozenset({"code", "category", "message", "details"})
_REQUESTER_FIELDS = frozenset({"requester_type", "pid", "uid", "gid"})
_READY_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "job_revision",
        "gcode",
        "artifacts",
        "static_result",
        "send_plan",
        "readiness",
        "config",
        "planning_parameters",
        "application_version",
    }
)
_HARDWARE_AUTHORITY_FIELDS = frozenset(
    {
        "approval",
        "arm_phrase",
        "authorization",
        "bridge_command",
        "bridge_commands",
        "hardware_authority",
        "serial_device",
        "serial_path",
    }
)
_JOB_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "kind",
        "name",
        "state",
        "revision",
        "request",
        "config",
        "blocker",
        "error",
        "ready_snapshot",
        "created_at",
        "updated_at",
    }
)
_TRANSITION_FIELDS = frozenset(
    {
        "job_id",
        "expected_revision",
        "result_state",
        "request_id",
        "event_type",
        "blocker",
        "error",
        "ready_snapshot",
    }
)
_MACHINE_OUTCOME_TRANSITION_FIELDS = frozenset(
    {"job_id", "expected_revision", "result_state", "event_type", "ready_snapshot"}
)
_ERROR_FIELDS = frozenset({"code", "category", "message", "retryable", "details", "request_id", "job_id"})
_REQUESTER_TYPES = frozenset({"LOCAL_PEER", "SERVICE"})
_EnumType = TypeVar("_EnumType", JobKind, JobState, ErrorCategory)
_FROZEN_INIT_TOKEN = object()


def _contract_error(message: str, details: Mapping[str, JsonValue] | None = None) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code="JOB_CONTRACT_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details={} if details is None else details,
        )
    )


def _raise_invalid(message: str, details: Mapping[str, JsonValue] | None = None) -> NoReturn:
    raise _contract_error(message, details)


class _FrozenJsonObject(Mapping[str, JsonValue]):
    __slots__ = ("_items",)
    _items: tuple[tuple[str, JsonValue], ...]

    def __init__(
        self,
        token: object,
        items: tuple[tuple[str, JsonValue], ...],
    ) -> None:
        if token is not _FROZEN_INIT_TOKEN or hasattr(self, "_items"):
            raise TypeError("domain JSON mapping is immutable")
        object.__setattr__(self, "_items", items)

    @classmethod
    def _create(cls, items: Iterable[tuple[str, JsonValue]]) -> Self:
        return cls(_FROZEN_INIT_TOKEN, tuple(items))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("domain JSON mapping is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("domain JSON mapping is immutable")

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
        if token is not _FROZEN_INIT_TOKEN or hasattr(self, "_values"):
            raise TypeError("domain JSON array is immutable")
        object.__setattr__(self, "_values", values)

    @classmethod
    def _create(cls, values: Iterable[JsonValue]) -> Self:
        return cls(_FROZEN_INIT_TOKEN, tuple(values))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("domain JSON array is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("domain JSON array is immutable")

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
        if isinstance(other, Sequence) and not isinstance(other, str | bytes | bytearray):
            return self._values == tuple(other)
        return False

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        memo[id(self)] = self
        return self


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _raise_invalid(f"{context} must be a JSON object", {"field": context})
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _raise_invalid(f"{context} contains a non-string key", {"field": context})
        result[key] = item
    return result


def _require_exact_fields(payload: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    actual = set(payload)
    unknown = sorted(actual - expected)
    if unknown:
        unknown_values: list[JsonValue] = list(unknown)
        _raise_invalid(
            f"{context} has unknown keys: {', '.join(unknown)}",
            {"unknown_keys": unknown_values},
        )
    missing = sorted(expected - actual)
    if missing:
        missing_values: list[JsonValue] = list(missing)
        _raise_invalid(
            f"{context} has missing keys: {', '.join(missing)}",
            {"missing_keys": missing_values},
        )


def _validate_string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _raise_invalid(f"{field} must be a string", {"field": field})
    if not allow_empty and not value:
        _raise_invalid(f"{field} must not be empty", {"field": field})
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _contract_error(f"{field} contains invalid Unicode", {"field": field}) from error
    return value


def _validate_nullable_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _validate_string(value, field)


def _validate_int(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise_invalid(f"{field} must be an integer", {"field": field})
    if value < minimum:
        _raise_invalid(f"{field} must be at least {minimum}", {"field": field, "minimum": minimum})
    try:
        str(value)
    except ValueError as error:
        raise _contract_error(f"{field} integer cannot be serialized", {"field": field}) from error
    return value


def _validate_nullable_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _validate_int(value, field, minimum=0)


def _validate_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _raise_invalid(f"{field} must be a boolean", {"field": field})
    return value


def _validate_schema_version(value: object, field: str) -> int:
    version = _validate_int(value, field, minimum=1)
    if version != _SCHEMA_VERSION:
        _raise_invalid(
            f"{field} has unsupported schema version: {version}",
            {"field": field, "expected": _SCHEMA_VERSION, "received": version},
        )
    return version


def _validate_digest(value: object, field: str) -> str:
    digest = _validate_string(value, field)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        _raise_invalid(f"{field} must be a lowercase SHA-256 digest", {"field": field})
    return digest


def _validate_relative_path(value: object, field: str) -> str:
    relative_path = _validate_string(value, field)
    path = PurePosixPath(relative_path)
    if (
        "\\" in relative_path
        or path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or str(path) != relative_path
    ):
        _raise_invalid(f"{field} must be a canonical relative artifact path", {"field": field})
    return relative_path


def _freeze_json_value(value: object, context: str, active_containers: set[int]) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        try:
            str(value)
        except ValueError as error:
            raise _contract_error(f"{context} integer cannot be serialized", {"field": context}) from error
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise_invalid(f"{context} must be finite", {"field": context})
        return value
    if isinstance(value, str):
        return _validate_string(value, context, allow_empty=True)
    if isinstance(value, list | _FrozenJsonArray):
        marker = id(value)
        if marker in active_containers:
            _raise_invalid(f"{context} contains a recursive JSON value", {"field": context})
        active_containers.add(marker)
        try:
            return cast(
                JsonValue,
                _FrozenJsonArray._create(
                    _freeze_json_value(item, f"{context}[]", active_containers)
                    for item in cast(Iterable[object], value)
                ),
            )
        finally:
            active_containers.remove(marker)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active_containers:
            _raise_invalid(f"{context} contains a recursive JSON value", {"field": context})
        active_containers.add(marker)
        try:
            items: list[tuple[str, JsonValue]] = []
            for key, item in cast(Mapping[object, object], value).items():
                if not isinstance(key, str):
                    _raise_invalid(f"{context} contains a non-string key", {"field": context})
                _validate_string(key, context, allow_empty=True)
                items.append((key, _freeze_json_value(item, f"{context}.{key}", active_containers)))
            return cast(JsonValue, _FrozenJsonObject._create(items))
        finally:
            active_containers.remove(marker)
    _raise_invalid(
        f"{context} must be JSON-compatible",
        {"field": context, "type": type(value).__name__},
    )


def _freeze_json_object(value: object, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        _raise_invalid(f"{context} must be a JSON object", {"field": context})
    try:
        return cast(JsonObject, _freeze_json_value(value, context, set()))
    except RecursionError as error:
        raise _contract_error(f"{context} nesting is too deep", {"field": context}) from error


def _freeze_json_mapping(value: object, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        _raise_invalid(f"{context} must be a JSON object", {"field": context})
    return _freeze_json_object(value, context)


def _plain_json_value(value: object) -> JsonValue:
    if isinstance(value, list | _FrozenJsonArray):
        return [_plain_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain_json_value(item) for key, item in value.items()}
    return cast(JsonValue, value)


def _plain_json_object(value: Mapping[str, JsonValue]) -> JsonObject:
    return cast(JsonObject, _plain_json_value(value))


def _reject_hardware_authority(value: object, context: str) -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(set(value) & _HARDWARE_AUTHORITY_FIELDS)
        if forbidden:
            forbidden_values: list[JsonValue] = list(forbidden)
            _raise_invalid(
                f"{context} contains hardware authority fields: {', '.join(forbidden)}",
                {"forbidden_fields": forbidden_values},
            )
        for key, item in value.items():
            _reject_hardware_authority(item, f"{context}.{key}")
    elif isinstance(value, list | _FrozenJsonArray):
        for item in value:
            _reject_hardware_authority(item, f"{context}[]")


def _validate_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        _raise_invalid(f"{field} must be a datetime", {"field": field})
    if value.tzinfo is None or value.utcoffset() is None:
        _raise_invalid(f"{field} must be timezone-aware", {"field": field})
    return value


def _parse_datetime(value: object, field: str) -> datetime:
    text = _validate_string(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _contract_error(f"{field} must be an ISO 8601 datetime", {"field": field}) from error
    return _validate_datetime(parsed, field)


def _validate_requester_type(value: object, field: str = "requester_type") -> str:
    requester_type = _validate_string(value, field)
    if requester_type not in _REQUESTER_TYPES:
        _raise_invalid(
            f"{field} must be LOCAL_PEER or SERVICE",
            {"field": field, "value": requester_type},
        )
    return requester_type


def _parse_enum(
    enum_type: type[_EnumType],
    value: object,
    field: str,
) -> _EnumType:
    text = _validate_string(value, field)
    try:
        return enum_type(text)
    except ValueError as error:
        raise _contract_error(f"{field} has an invalid value: {text}", {"field": field, "value": text}) from error


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    role: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        _validate_string(self.role, "artifact.role")
        _validate_relative_path(self.relative_path, "artifact.relative_path")
        _validate_digest(self.sha256, "artifact.sha256")
        _validate_int(self.size_bytes, "artifact.size_bytes", minimum=0)
        _validate_string(self.media_type, "artifact.media_type")

    def to_json(self) -> JsonObject:
        return {
            "role": self.role,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "artifact")
        _require_exact_fields(data, _ARTIFACT_FIELDS, "artifact")
        return cls(
            role=_validate_string(data["role"], "artifact.role"),
            relative_path=_validate_relative_path(data["relative_path"], "artifact.relative_path"),
            sha256=_validate_digest(data["sha256"], "artifact.sha256"),
            size_bytes=_validate_int(data["size_bytes"], "artifact.size_bytes", minimum=0),
            media_type=_validate_string(data["media_type"], "artifact.media_type"),
        )


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    machine_profile: str
    provider_profile: str
    application_schema_version: int
    machine_schema_version: int
    provider_schema_version: int
    application_digest: str
    machine_digest: str
    provider_digest: str

    def __post_init__(self) -> None:
        _validate_string(self.machine_profile, "config.machine_profile")
        _validate_string(self.provider_profile, "config.provider_profile")
        _validate_schema_version(self.application_schema_version, "config.application_schema_version")
        _validate_schema_version(self.machine_schema_version, "config.machine_schema_version")
        _validate_schema_version(self.provider_schema_version, "config.provider_schema_version")
        _validate_digest(self.application_digest, "config.application_digest")
        _validate_digest(self.machine_digest, "config.machine_digest")
        _validate_digest(self.provider_digest, "config.provider_digest")

    def to_json(self) -> JsonObject:
        return {
            "machine_profile": self.machine_profile,
            "provider_profile": self.provider_profile,
            "application_schema_version": self.application_schema_version,
            "machine_schema_version": self.machine_schema_version,
            "provider_schema_version": self.provider_schema_version,
            "application_digest": self.application_digest,
            "machine_digest": self.machine_digest,
            "provider_digest": self.provider_digest,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "config snapshot")
        _require_exact_fields(data, _CONFIG_FIELDS, "config snapshot")
        return cls(
            machine_profile=_validate_string(data["machine_profile"], "config.machine_profile"),
            provider_profile=_validate_string(data["provider_profile"], "config.provider_profile"),
            application_schema_version=_validate_schema_version(
                data["application_schema_version"],
                "config.application_schema_version",
            ),
            machine_schema_version=_validate_schema_version(
                data["machine_schema_version"],
                "config.machine_schema_version",
            ),
            provider_schema_version=_validate_schema_version(
                data["provider_schema_version"],
                "config.provider_schema_version",
            ),
            application_digest=_validate_digest(data["application_digest"], "config.application_digest"),
            machine_digest=_validate_digest(data["machine_digest"], "config.machine_digest"),
            provider_digest=_validate_digest(data["provider_digest"], "config.provider_digest"),
        )


@dataclass(frozen=True, slots=True)
class JobBlocker:
    code: str
    category: ErrorCategory
    message: str
    details: JsonObject

    def __post_init__(self) -> None:
        _validate_string(self.code, "blocker.code")
        if not isinstance(self.category, ErrorCategory):
            _raise_invalid("blocker.category must be an ErrorCategory", {"field": "blocker.category"})
        _validate_string(self.message, "blocker.message")
        object.__setattr__(self, "details", _freeze_json_object(self.details, "blocker.details"))

    def to_json(self) -> JsonObject:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "details": _plain_json_object(self.details),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "job blocker")
        _require_exact_fields(data, _BLOCKER_FIELDS, "job blocker")
        return cls(
            code=_validate_string(data["code"], "blocker.code"),
            category=_parse_enum(ErrorCategory, data["category"], "blocker.category"),
            message=_validate_string(data["message"], "blocker.message"),
            details=_freeze_json_object(data["details"], "blocker.details"),
        )


@dataclass(frozen=True, slots=True)
class RequesterIdentity:
    requester_type: str
    pid: int | None
    uid: int | None
    gid: int | None

    def __post_init__(self) -> None:
        requester_type = _validate_requester_type(self.requester_type)
        pid = _validate_nullable_int(self.pid, "requester.pid")
        uid = _validate_nullable_int(self.uid, "requester.uid")
        gid = _validate_nullable_int(self.gid, "requester.gid")
        credentials = (pid, uid, gid)
        if requester_type == "LOCAL_PEER" and any(value is None for value in credentials):
            _raise_invalid("LOCAL_PEER requester identity requires PID, UID, and GID")
        if requester_type == "SERVICE" and any(value is not None for value in credentials):
            _raise_invalid("SERVICE requester identity must not contain peer credentials")

    def to_json(self) -> JsonObject:
        return {
            "requester_type": self.requester_type,
            "pid": self.pid,
            "uid": self.uid,
            "gid": self.gid,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "requester identity")
        _require_exact_fields(data, _REQUESTER_FIELDS, "requester identity")
        return cls(
            requester_type=_validate_requester_type(data["requester_type"]),
            pid=_validate_nullable_int(data["pid"], "requester.pid"),
            uid=_validate_nullable_int(data["uid"], "requester.uid"),
            gid=_validate_nullable_int(data["gid"], "requester.gid"),
        )


@dataclass(frozen=True, slots=True)
class ReadyToRunSnapshot:
    schema_version: int
    job_id: str
    job_revision: int
    gcode: ArtifactRef
    artifacts: tuple[ArtifactRef, ...]
    static_result: JsonObject
    send_plan: JsonObject
    readiness: JsonObject
    config: ConfigSnapshot
    planning_parameters: JsonObject
    application_version: str

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "ready_snapshot.schema_version")
        _validate_string(self.job_id, "ready_snapshot.job_id")
        _validate_int(self.job_revision, "ready_snapshot.job_revision", minimum=1)
        if not isinstance(self.gcode, ArtifactRef):
            _raise_invalid("ready_snapshot.gcode must be an ArtifactRef")
        if self.gcode.role != "gcode":
            _raise_invalid("ready_snapshot.gcode must have role gcode")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(artifact, ArtifactRef) for artifact in self.artifacts
        ):
            _raise_invalid("ready_snapshot.artifacts must be a tuple of ArtifactRef values")
        roles = [artifact.role for artifact in self.artifacts]
        if len(roles) != len(set(roles)):
            _raise_invalid("ready_snapshot contains a duplicate artifact role")
        if "gcode" in roles:
            _raise_invalid("ready_snapshot.artifacts must not contain the dedicated gcode role")
        object.__setattr__(
            self,
            "static_result",
            _freeze_json_object(self.static_result, "ready_snapshot.static_result"),
        )
        object.__setattr__(self, "send_plan", _freeze_json_object(self.send_plan, "ready_snapshot.send_plan"))
        object.__setattr__(self, "readiness", _freeze_json_object(self.readiness, "ready_snapshot.readiness"))
        if not isinstance(self.config, ConfigSnapshot):
            _raise_invalid("ready_snapshot.config must be a ConfigSnapshot")
        object.__setattr__(
            self,
            "planning_parameters",
            _freeze_json_object(self.planning_parameters, "ready_snapshot.planning_parameters"),
        )
        _reject_hardware_authority(self.static_result, "ready_snapshot.static_result")
        _reject_hardware_authority(self.send_plan, "ready_snapshot.send_plan")
        _reject_hardware_authority(self.readiness, "ready_snapshot.readiness")
        _reject_hardware_authority(self.planning_parameters, "ready_snapshot.planning_parameters")
        _validate_string(self.application_version, "ready_snapshot.application_version")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "gcode": self.gcode.to_json(),
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
            "static_result": _plain_json_object(self.static_result),
            "send_plan": _plain_json_object(self.send_plan),
            "readiness": _plain_json_object(self.readiness),
            "config": self.config.to_json(),
            "planning_parameters": _plain_json_object(self.planning_parameters),
            "application_version": self.application_version,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "ready snapshot")
        forbidden = sorted(set(data) & _HARDWARE_AUTHORITY_FIELDS)
        if forbidden:
            forbidden_values: list[JsonValue] = list(forbidden)
            _raise_invalid(
                f"ready snapshot contains hardware authority fields: {', '.join(forbidden)}",
                {"forbidden_fields": forbidden_values},
            )
        _require_exact_fields(data, _READY_FIELDS, "ready snapshot")
        artifacts_value = data["artifacts"]
        if not isinstance(artifacts_value, list):
            _raise_invalid("ready_snapshot.artifacts must be a JSON array")
        artifacts = tuple(ArtifactRef.from_json(item) for item in cast(list[object], artifacts_value))
        return cls(
            schema_version=_validate_schema_version(data["schema_version"], "ready_snapshot.schema_version"),
            job_id=_validate_string(data["job_id"], "ready_snapshot.job_id"),
            job_revision=_validate_int(data["job_revision"], "ready_snapshot.job_revision", minimum=1),
            gcode=ArtifactRef.from_json(data["gcode"]),
            artifacts=artifacts,
            static_result=_freeze_json_object(data["static_result"], "ready_snapshot.static_result"),
            send_plan=_freeze_json_object(data["send_plan"], "ready_snapshot.send_plan"),
            readiness=_freeze_json_object(data["readiness"], "ready_snapshot.readiness"),
            config=ConfigSnapshot.from_json(data["config"]),
            planning_parameters=_freeze_json_object(
                data["planning_parameters"],
                "ready_snapshot.planning_parameters",
            ),
            application_version=_validate_string(
                data["application_version"],
                "ready_snapshot.application_version",
            ),
        )


def _parse_optional_blocker(value: object) -> JobBlocker | None:
    return None if value is None else JobBlocker.from_json(value)


def _parse_error(value: object) -> ErrorPayload | None:
    if value is None:
        return None
    data = _object(value, "error payload")
    _require_exact_fields(data, _ERROR_FIELDS, "error payload")
    return _clone_error(
        ErrorPayload(
            code=_validate_string(data["code"], "error.code"),
            category=_parse_enum(ErrorCategory, data["category"], "error.category"),
            message=_validate_string(data["message"], "error.message"),
            retryable=_validate_bool(data["retryable"], "error.retryable"),
            details=_freeze_json_object(data["details"], "error.details"),
            request_id=_validate_nullable_string(data["request_id"], "error.request_id"),
            job_id=_validate_nullable_string(data["job_id"], "error.job_id"),
        ),
        "error",
    )


def _clone_error(value: object, context: str) -> ErrorPayload:
    if not isinstance(value, ErrorPayload):
        _raise_invalid(f"{context} must be an ErrorPayload", {"field": context})
    if not isinstance(value.category, ErrorCategory):
        _raise_invalid(f"{context}.category must be an ErrorCategory", {"field": f"{context}.category"})
    return ErrorPayload(
        code=_validate_string(value.code, f"{context}.code"),
        category=value.category,
        message=_validate_string(value.message, f"{context}.message"),
        retryable=_validate_bool(value.retryable, f"{context}.retryable"),
        details=_freeze_json_mapping(value.details, f"{context}.details"),
        request_id=_validate_nullable_string(value.request_id, f"{context}.request_id"),
        job_id=_validate_nullable_string(value.job_id, f"{context}.job_id"),
    )


def _error_json(value: ErrorPayload) -> JsonObject:
    error = _clone_error(value, "error")
    return {
        "code": error.code,
        "category": error.category.value,
        "message": error.message,
        "retryable": error.retryable,
        "details": _plain_json_object(cast(JsonObject, error.details)),
        "request_id": error.request_id,
        "job_id": error.job_id,
    }


def _parse_optional_ready_snapshot(value: object) -> ReadyToRunSnapshot | None:
    return None if value is None else ReadyToRunSnapshot.from_json(value)


def _validated_state_payloads(
    state: JobState,
    blocker: object,
    error: object,
    ready_snapshot: object,
    context: str,
    *,
    retain_ready_snapshot_on_cancel: bool = False,
    retain_machine_outcome_snapshot: bool = False,
) -> tuple[JobBlocker | None, ErrorPayload | None, ReadyToRunSnapshot | None]:
    if blocker is not None and not isinstance(blocker, JobBlocker):
        _raise_invalid(f"{context}.blocker must be a JobBlocker", {"field": f"{context}.blocker"})
    if error is not None and not isinstance(error, ErrorPayload):
        _raise_invalid(f"{context}.error must be an ErrorPayload", {"field": f"{context}.error"})
    if ready_snapshot is not None and type(ready_snapshot) is not ReadyToRunSnapshot:
        _raise_invalid(
            f"{context}.ready_snapshot must be exact ReadyToRunSnapshot",
            {"field": f"{context}.ready_snapshot"},
        )
    validated_error = None if error is None else _clone_error(error, f"{context}.error")
    if (state is JobState.BLOCKED) != (blocker is not None):
        _raise_invalid(f"{context} BLOCKED state must carry exactly one blocker")
    if (state is JobState.FAILED) != (validated_error is not None):
        _raise_invalid(f"{context} FAILED state must carry exactly one error")
    machine_outcome = retain_machine_outcome_snapshot and state in {
        JobState.COMPLETED,
        JobState.RECOVERY_REQUIRED,
    }
    ready_snapshot_allowed = (
        state is JobState.READY_TO_RUN
        or machine_outcome
        or (retain_ready_snapshot_on_cancel and state is JobState.CANCELLED)
    )
    if ((state is JobState.READY_TO_RUN or machine_outcome) and ready_snapshot is None) or (
        not ready_snapshot_allowed and ready_snapshot is not None
    ):
        _raise_invalid(
            f"{context} READY_TO_RUN, COMPLETED, and RECOVERY_REQUIRED states must retain exactly one ready snapshot"
        )
    return blocker, validated_error, ready_snapshot


@dataclass(frozen=True, slots=True)
class JobRecord:
    schema_version: int
    job_id: str
    kind: JobKind
    name: str
    state: JobState
    revision: int
    request: JsonObject
    config: ConfigSnapshot
    blocker: JobBlocker | None
    error: ErrorPayload | None
    ready_snapshot: ReadyToRunSnapshot | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "job.schema_version")
        _validate_string(self.job_id, "job.job_id")
        if type(self.kind) is not JobKind:
            _raise_invalid("job.kind must be a JobKind")
        _validate_string(self.name, "job.name")
        if type(self.state) is not JobState:
            _raise_invalid("job.state must be a JobState")
        if self.state not in _job_states(self.kind):
            _raise_invalid(f"{self.kind.value} jobs cannot use the {self.state.value} state")
        _validate_int(self.revision, "job.revision", minimum=0)
        object.__setattr__(self, "request", _freeze_json_object(self.request, "job.request"))
        if not isinstance(self.config, ConfigSnapshot):
            _raise_invalid("job.config must be a ConfigSnapshot")
        blocker, error, ready_snapshot = _validated_state_payloads(
            self.state,
            self.blocker,
            self.error,
            self.ready_snapshot,
            "job",
            retain_ready_snapshot_on_cancel=True,
            retain_machine_outcome_snapshot=self.kind is JobKind.OFFLINE_WORKFLOW,
        )
        object.__setattr__(self, "blocker", blocker)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "ready_snapshot", ready_snapshot)
        if error is not None and error.job_id != self.job_id:
            _raise_invalid("job.error.job_id must explicitly match job.job_id")
        if self.ready_snapshot is not None:
            revision_matches = (
                self.ready_snapshot.job_revision == self.revision
                if self.state is JobState.READY_TO_RUN
                else self.ready_snapshot.job_revision < self.revision
            )
            if self.ready_snapshot.job_id != self.job_id or not revision_matches:
                _raise_invalid("job ready snapshot must match job ID and its immutable ready revision")
        created_at = _validate_datetime(self.created_at, "job.created_at")
        updated_at = _validate_datetime(self.updated_at, "job.updated_at")
        if updated_at < created_at:
            _raise_invalid("job.updated_at must not precede job.created_at")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "kind": self.kind.value,
            "name": self.name,
            "state": self.state.value,
            "revision": self.revision,
            "request": _plain_json_object(self.request),
            "config": self.config.to_json(),
            "blocker": None if self.blocker is None else self.blocker.to_json(),
            "error": None if self.error is None else _error_json(self.error),
            "ready_snapshot": None if self.ready_snapshot is None else self.ready_snapshot.to_json(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "job record")
        _require_exact_fields(data, _JOB_FIELDS, "job record")
        return cls(
            schema_version=_validate_schema_version(data["schema_version"], "job.schema_version"),
            job_id=_validate_string(data["job_id"], "job.job_id"),
            kind=_parse_enum(JobKind, data["kind"], "job.kind"),
            name=_validate_string(data["name"], "job.name"),
            state=_parse_enum(JobState, data["state"], "job.state"),
            revision=_validate_int(data["revision"], "job.revision", minimum=0),
            request=_freeze_json_object(data["request"], "job.request"),
            config=ConfigSnapshot.from_json(data["config"]),
            blocker=_parse_optional_blocker(data["blocker"]),
            error=_parse_error(data["error"]),
            ready_snapshot=_parse_optional_ready_snapshot(data["ready_snapshot"]),
            created_at=_parse_datetime(data["created_at"], "job.created_at"),
            updated_at=_parse_datetime(data["updated_at"], "job.updated_at"),
        )


@dataclass(frozen=True, slots=True)
class JobTransition:
    job_id: str
    expected_revision: int
    result_state: JobState
    request_id: str | None
    event_type: str
    blocker: JobBlocker | None = None
    error: ErrorPayload | None = None
    ready_snapshot: ReadyToRunSnapshot | None = None

    def __post_init__(self) -> None:
        _validate_string(self.job_id, "transition.job_id")
        _validate_int(self.expected_revision, "transition.expected_revision", minimum=0)
        if type(self.result_state) is not JobState:
            _raise_invalid("transition.result_state must be a JobState")
        if self.result_state is JobState.RECOVERY_REQUIRED:
            _raise_invalid("JobTransition cannot produce RECOVERY_REQUIRED")
        _validate_nullable_string(self.request_id, "transition.request_id")
        _validate_string(self.event_type, "transition.event_type")
        blocker, error, ready_snapshot = _validated_state_payloads(
            self.result_state,
            self.blocker,
            self.error,
            self.ready_snapshot,
            "transition",
        )
        object.__setattr__(self, "blocker", blocker)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "ready_snapshot", ready_snapshot)
        if error is not None:
            if error.job_id != self.job_id:
                _raise_invalid("transition.error.job_id must explicitly match transition.job_id")
            if self.request_id is None or error.request_id != self.request_id:
                _raise_invalid("transition.error.request_id must explicitly match transition.request_id")
        if ready_snapshot is not None:
            if ready_snapshot.job_id != self.job_id:
                _raise_invalid("transition ready snapshot must match job ID")
            if ready_snapshot.job_revision != self.expected_revision + 1:
                _raise_invalid("transition ready snapshot job_revision must equal expected_revision + 1")

    def to_json(self) -> JsonObject:
        return {
            "job_id": self.job_id,
            "expected_revision": self.expected_revision,
            "result_state": self.result_state.value,
            "request_id": self.request_id,
            "event_type": self.event_type,
            "blocker": None if self.blocker is None else self.blocker.to_json(),
            "error": None if self.error is None else _error_json(self.error),
            "ready_snapshot": None if self.ready_snapshot is None else self.ready_snapshot.to_json(),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "job transition")
        _require_exact_fields(data, _TRANSITION_FIELDS, "job transition")
        return cls(
            job_id=_validate_string(data["job_id"], "transition.job_id"),
            expected_revision=_validate_int(
                data["expected_revision"],
                "transition.expected_revision",
                minimum=0,
            ),
            result_state=_parse_enum(JobState, data["result_state"], "transition.result_state"),
            request_id=_validate_nullable_string(data["request_id"], "transition.request_id"),
            event_type=_validate_string(data["event_type"], "transition.event_type"),
            blocker=_parse_optional_blocker(data["blocker"]),
            error=_parse_error(data["error"]),
            ready_snapshot=_parse_optional_ready_snapshot(data["ready_snapshot"]),
        )


@dataclass(frozen=True, slots=True)
class MachineJobOutcomeTransition:
    """The only Package C boundary allowed to produce an offline machine outcome."""

    job_id: str
    expected_revision: int
    result_state: JobState
    event_type: str
    ready_snapshot: ReadyToRunSnapshot

    def __post_init__(self) -> None:
        _validate_string(self.job_id, "machine_outcome.job_id")
        _validate_int(self.expected_revision, "machine_outcome.expected_revision", minimum=1)
        if type(self.result_state) is not JobState:
            _raise_invalid("machine outcome result_state must be exact JobState")
        if self.result_state not in {JobState.COMPLETED, JobState.RECOVERY_REQUIRED}:
            _raise_invalid("machine outcome must be COMPLETED or RECOVERY_REQUIRED")
        _validate_string(self.event_type, "machine_outcome.event_type")
        if type(self.ready_snapshot) is not ReadyToRunSnapshot:
            _raise_invalid("machine_outcome.ready_snapshot must be exact ReadyToRunSnapshot")
        if self.ready_snapshot.job_id != self.job_id:
            _raise_invalid("machine outcome ready snapshot must match job ID")
        if self.ready_snapshot.job_revision >= self.expected_revision + 1:
            _raise_invalid("machine outcome must retain an earlier immutable ready revision")

    def to_json(self) -> JsonObject:
        return {
            "job_id": self.job_id,
            "expected_revision": self.expected_revision,
            "result_state": self.result_state.value,
            "event_type": self.event_type,
            "ready_snapshot": self.ready_snapshot.to_json(),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "machine outcome transition")
        _require_exact_fields(
            data,
            _MACHINE_OUTCOME_TRANSITION_FIELDS,
            "machine outcome transition",
        )
        return cls(
            job_id=_validate_string(data["job_id"], "machine_outcome.job_id"),
            expected_revision=_validate_int(data["expected_revision"], "machine_outcome.expected_revision", minimum=1),
            result_state=_parse_enum(JobState, data["result_state"], "machine_outcome.result_state"),
            event_type=_validate_string(data["event_type"], "machine_outcome.event_type"),
            ready_snapshot=ReadyToRunSnapshot.from_json(data["ready_snapshot"]),
        )
