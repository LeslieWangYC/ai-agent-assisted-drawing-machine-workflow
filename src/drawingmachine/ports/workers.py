from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath, PurePosixPath
from typing import NoReturn, Protocol, Self, cast, overload

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.artifacts import WorkerArtifact

_SCHEMA_VERSION = 1
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_FROZEN_INIT_TOKEN = object()
_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "job_id",
        "job_revision",
        "attempt_id",
        "kind",
        "input_artifacts",
        "config_digests",
        "staging_dir",
        "payload",
    }
)
_INPUT_FIELDS = frozenset({"role", "absolute_path", "sha256", "size_bytes", "media_type"})
_OUTCOME_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "job_id",
        "job_revision",
        "attempt_id",
        "status",
        "artifacts",
        "metrics",
        "error",
    }
)
_ARTIFACT_FIELDS = frozenset({"role", "relative_path", "sha256", "size_bytes", "media_type"})
_ERROR_FIELDS = frozenset({"code", "category", "message", "retryable", "details", "request_id", "job_id"})


def _contract_error(message: str, *, field: str | None = None) -> DrawingMachineError:
    details: JsonObject = {} if field is None else {"field": field}
    return DrawingMachineError(
        ErrorPayload(
            code="WORKER_CONTRACT_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details=details,
        )
    )


def _invalid(message: str, *, field: str | None = None) -> NoReturn:
    raise _contract_error(message, field=field)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{context} must be a JSON object", field=context)
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _invalid(f"{context} contains a non-string key", field=context)
        result[key] = item
    return result


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        _invalid(f"{context} has unknown keys: {', '.join(unknown)}", field=context)
    missing = sorted(expected - set(value))
    if missing:
        _invalid(f"{context} has missing keys: {', '.join(missing)}", field=context)


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _invalid(f"{field} must be a string", field=field)
    if not allow_empty and not value:
        _invalid(f"{field} must not be empty", field=field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _contract_error(f"{field} contains invalid Unicode", field=field) from error
    return value


def _identifier(value: object, field: str) -> str:
    identifier = _string(value, field)
    if identifier in {".", ".."} or _IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        _invalid(f"{field} must be a safe ASCII identifier", field=field)
    return identifier


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{field} must be an integer", field=field)
    if value < minimum:
        _invalid(f"{field} must be at least {minimum}", field=field)
    try:
        str(value)
    except ValueError as error:
        raise _contract_error(f"{field} integer cannot be serialized", field=field) from error
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _invalid(f"{field} must be a boolean", field=field)
    return value


def _schema(value: object, field: str) -> int:
    version = _integer(value, field, minimum=1)
    if version != _SCHEMA_VERSION:
        _invalid(f"{field} has unsupported schema version: {version}", field=field)
    return version


def _digest(value: object, field: str) -> str:
    digest = _string(value, field)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        _invalid(f"{field} must be a lowercase SHA-256 digest", field=field)
    return digest


def _absolute_path(value: object, field: str) -> str:
    text = _string(value, field)
    path = PurePath(text)
    if "\x00" in text or not path.is_absolute() or str(path) != text or any(part in {".", ".."} for part in path.parts):
        _invalid(f"{field} must be a canonical absolute path", field=field)
    return text


def _relative_path(value: object, field: str) -> str:
    text = _string(value, field)
    path = PurePosixPath(text)
    if (
        "\x00" in text
        or "\\" in text
        or path.is_absolute()
        or not path.parts
        or str(path) != text
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a canonical relative path", field=field)
    return text


class _FrozenJsonObject(Mapping[str, JsonValue]):
    __slots__ = ("_items",)
    _items: tuple[tuple[str, JsonValue], ...]

    def __init__(self, token: object, items: tuple[tuple[str, JsonValue], ...]) -> None:
        if token is not _FROZEN_INIT_TOKEN or hasattr(self, "_items"):
            raise TypeError("worker JSON mapping is immutable")
        object.__setattr__(self, "_items", items)

    @classmethod
    def _create(cls, items: Iterable[tuple[str, JsonValue]]) -> Self:
        return cls(_FROZEN_INIT_TOKEN, tuple(items))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("worker JSON mapping is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("worker JSON mapping is immutable")

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
            raise TypeError("worker JSON array is immutable")
        object.__setattr__(self, "_values", values)

    @classmethod
    def _create(cls, values: Iterable[JsonValue]) -> Self:
        return cls(_FROZEN_INIT_TOKEN, tuple(values))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("worker JSON array is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("worker JSON array is immutable")

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


class _FrozenStringMap(Mapping[str, str]):
    __slots__ = ("_items",)
    _items: tuple[tuple[str, str], ...]

    def __init__(self, token: object, items: tuple[tuple[str, str], ...]) -> None:
        if token is not _FROZEN_INIT_TOKEN or hasattr(self, "_items"):
            raise TypeError("worker config mapping is immutable")
        object.__setattr__(self, "_items", tuple(items))

    @classmethod
    def _create(cls, items: Iterable[tuple[str, str]]) -> Self:
        return cls(_FROZEN_INIT_TOKEN, tuple(items))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("worker config mapping is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise TypeError("worker config mapping is immutable")

    def __getitem__(self, key: str) -> str:
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


def _freeze_json(value: object, context: str, active: set[int]) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        try:
            str(value)
        except ValueError as error:
            raise _contract_error(f"{context} integer cannot be serialized", field=context) from error
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid(f"{context} must be finite", field=context)
        return value
    if isinstance(value, str):
        return _string(value, context, allow_empty=True)
    if isinstance(value, list | _FrozenJsonArray):
        marker = id(value)
        if marker in active:
            _invalid(f"{context} contains a recursive JSON value", field=context)
        active.add(marker)
        try:
            return cast(
                JsonValue,
                _FrozenJsonArray._create(_freeze_json(item, f"{context}[]", active) for item in value),
            )
        finally:
            active.remove(marker)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            _invalid(f"{context} contains a recursive JSON value", field=context)
        active.add(marker)
        try:
            items: list[tuple[str, JsonValue]] = []
            for key, item in cast(Mapping[object, object], value).items():
                if not isinstance(key, str):
                    _invalid(f"{context} contains a non-string key", field=context)
                items.append((key, _freeze_json(item, f"{context}.{key}", active)))
            return cast(JsonValue, _FrozenJsonObject._create(items))
        finally:
            active.remove(marker)
    _invalid(f"{context} must be JSON-compatible", field=context)


def _freeze_object(value: object, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        _invalid(f"{context} must be a JSON object", field=context)
    try:
        return cast(JsonObject, _freeze_json(value, context, set()))
    except RecursionError as error:
        raise _contract_error(f"{context} nesting is too deep", field=context) from error


def _plain_json(value: object) -> JsonValue:
    if isinstance(value, list | _FrozenJsonArray):
        return [_plain_json(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    return cast(JsonValue, value)


def _plain_object(value: Mapping[str, JsonValue]) -> JsonObject:
    return cast(JsonObject, _plain_json(value))


def _enum(enum_type: type[WorkerKind] | type[WorkerStatus], value: object, field: str) -> WorkerKind | WorkerStatus:
    text = _string(value, field)
    try:
        return enum_type(text)
    except ValueError as error:
        raise _contract_error(f"{field} has an invalid value: {text}", field=field) from error


def _config_digests(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        _invalid("worker_task.config_digests must be a JSON object", field="worker_task.config_digests")
    items: list[tuple[str, str]] = []
    for key, digest in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _invalid("worker_task.config_digests contains a non-string key", field="worker_task.config_digests")
        items.append((_string(key, "worker_task.config_digests key"), _digest(digest, f"config_digests.{key}")))
    if not items:
        _invalid("worker_task.config_digests must not be empty", field="worker_task.config_digests")
    return _FrozenStringMap._create(items)


def _error_payload(value: object, context: str) -> ErrorPayload:
    if isinstance(value, ErrorPayload):
        category = value.category
        if not isinstance(category, ErrorCategory):
            _invalid(f"{context}.category must be an ErrorCategory", field=f"{context}.category")
        return ErrorPayload(
            code=_string(value.code, f"{context}.code"),
            category=category,
            message=_string(value.message, f"{context}.message"),
            retryable=_boolean(value.retryable, f"{context}.retryable"),
            details=_freeze_object(value.details, f"{context}.details"),
            request_id=None if value.request_id is None else _string(value.request_id, f"{context}.request_id"),
            job_id=None if value.job_id is None else _identifier(value.job_id, f"{context}.job_id"),
        )
    data = _object(value, context)
    _exact_fields(data, _ERROR_FIELDS, context)
    category_text = _string(data["category"], f"{context}.category")
    try:
        category = ErrorCategory(category_text)
    except ValueError as error:
        raise _contract_error(f"{context}.category has an invalid value", field=f"{context}.category") from error
    request_id = data["request_id"]
    job_id = data["job_id"]
    return ErrorPayload(
        code=_string(data["code"], f"{context}.code"),
        category=category,
        message=_string(data["message"], f"{context}.message"),
        retryable=_boolean(data["retryable"], f"{context}.retryable"),
        details=_freeze_object(data["details"], f"{context}.details"),
        request_id=None if request_id is None else _string(request_id, f"{context}.request_id"),
        job_id=None if job_id is None else _identifier(job_id, f"{context}.job_id"),
    )


def _error_json(value: ErrorPayload) -> JsonObject:
    error = _error_payload(value, "worker_outcome.error")
    return {
        "code": error.code,
        "category": error.category.value,
        "message": error.message,
        "retryable": error.retryable,
        "details": _plain_object(error.details),
        "request_id": error.request_id,
        "job_id": error.job_id,
    }


def _worker_artifact(value: object, context: str) -> WorkerArtifact:
    role: object
    relative_path: object
    sha256: object
    size_bytes: object
    media_type: object
    if isinstance(value, WorkerArtifact):
        role = value.role
        relative_path = value.relative_path
        sha256 = value.sha256
        size_bytes = value.size_bytes
        media_type = value.media_type
    else:
        data = _object(value, context)
        _exact_fields(data, _ARTIFACT_FIELDS, context)
        role = data["role"]
        relative_path = data["relative_path"]
        sha256 = data["sha256"]
        size_bytes = data["size_bytes"]
        media_type = data["media_type"]
    return WorkerArtifact(
        role=_string(role, f"{context}.role"),
        relative_path=_relative_path(relative_path, f"{context}.relative_path"),
        sha256=_digest(sha256, f"{context}.sha256"),
        size_bytes=_integer(size_bytes, f"{context}.size_bytes"),
        media_type=_string(media_type, f"{context}.media_type"),
    )


def _artifact_json(value: WorkerArtifact) -> JsonObject:
    artifact = _worker_artifact(value, "worker_outcome.artifact")
    return {
        "role": artifact.role,
        "relative_path": artifact.relative_path,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "media_type": artifact.media_type,
    }


class WorkerKind(StrEnum):
    IMAGE_PREPARE = "image.prepare"
    PROVIDER_LOCAL_COMFYUI = "provider.local_comfyui"
    PATHS_PLAN = "paths.plan"
    GCODE_BUILD = "gcode.build"
    GCODE_CHECK = "gcode.check"
    TEST_ECHO = "test.echo"
    TEST_HANG = "test.hang"


class WorkerStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_IMAGE_DIRECT_PAYLOAD_FIELDS = frozenset({"mode", "source_name", "route_mode", "semantic_assessment", "normalization"})
_IMAGE_NORMALIZE_PAYLOAD_FIELDS = frozenset({"mode", "source_name", "normalization"})
_PROVIDER_PAYLOAD_FIELDS = frozenset({"prompt", "provider_profile", "provider_config_path"})
_PLANNING_PAYLOAD_FIELDS = frozenset({"planning_profile", "complexity_limits", "base_dedupe_profile"})
_GCODE_BUILD_PAYLOAD_FIELDS = frozenset({"machine_build_profile"})
_GCODE_CHECK_PAYLOAD_FIELDS = frozenset({"machine_build_profile", "expected_gcode_sha256"})
_TEST_PAYLOAD_FIELDS = frozenset({"mode", "nested"})

_PLANNING_PROFILE_FIELDS = frozenset(
    {
        "canvas_width_mm",
        "canvas_height_mm",
        "pen_width_mm",
        "min_gap_mm",
        "threshold",
        "invert",
        "min_component_area_px",
        "simplify_tolerance_mm",
        "min_path_length_mm",
        "drop_short_stroke_mm",
        "merge_endpoint_distance_mm",
        "merge_angle_deg",
        "dedupe_short_path_length_mm",
        "dedupe_distance_mm",
        "dedupe_angle_deg",
        "dedupe_overlap_ratio",
        "hatch_spacing_mm",
        "hatch_min_run_mm",
        "fill_min_thickness_mm",
    }
)
_MACHINE_GCODE_FIELDS = frozenset(
    {
        "hardware_canvas_width_mm",
        "hardware_canvas_height_mm",
        "machine_width_mm",
        "machine_height_mm",
        "paper_center_x",
        "paper_center_y",
        "pen_up_z",
        "pen_down_z",
        "feed_travel",
        "feed_draw",
        "feed_pen_down",
        "feed_pen_up",
        "max_feed",
        "work_coordinate",
        "align_mode",
        "mirror_y",
        "safe_start",
        "path_mode",
    }
)
_COMPLEXITY_LIMIT_FIELDS = frozenset(
    {
        "soft_component_count",
        "soft_skeleton_pixels",
        "soft_boundary_pixels",
        "soft_transitions_total",
        "hard_component_count",
        "hard_skeleton_pixels",
        "hard_boundary_pixels",
        "hard_transitions_total",
        "threshold",
        "invert",
        "min_component_area_px",
    }
)
_DEDUPE_FIELDS = frozenset(
    {
        "dedupe_short_path_length_mm",
        "dedupe_distance_mm",
        "dedupe_angle_deg",
        "dedupe_overlap_ratio",
    }
)


@dataclass(frozen=True, slots=True)
class _PayloadSchema:
    leaf_fields: frozenset[str]
    object_fields: Mapping[str, _PayloadSchema]

    @property
    def fields(self) -> frozenset[str]:
        return self.leaf_fields | frozenset(self.object_fields)


def _payload_schema(*leaf_fields: str, **object_fields: _PayloadSchema) -> _PayloadSchema:
    return _PayloadSchema(frozenset(leaf_fields), object_fields)


_SEMANTIC_ASSESSMENT_SCHEMA = _payload_schema(
    "semantic_style_score",
    "semantic_score_source",
    "semantic_rationale",
    "semantic_blockers",
)
_NORMALIZATION_SCHEMA = _payload_schema(
    "threshold",
    "invert",
    "min_component_area_px",
    "median_filter_size",
    "color_delta_threshold",
    "remove_small_components_px",
)
_IMAGE_DIRECT_SCHEMA = _payload_schema(
    "mode",
    "source_name",
    "route_mode",
    semantic_assessment=_SEMANTIC_ASSESSMENT_SCHEMA,
    normalization=_NORMALIZATION_SCHEMA,
)
_IMAGE_NORMALIZE_SCHEMA = _payload_schema(
    "mode",
    "source_name",
    normalization=_NORMALIZATION_SCHEMA,
)

_PROVIDER_WORKFLOW_NODES_SCHEMA = _payload_schema("load_image", "prompt", "sampler", "save_image", "scale")
_PROVIDER_SAMPLER_SCHEMA = _payload_schema("steps", "cfg", "denoise")
_PROVIDER_PROFILE_BODY_SCHEMA = _payload_schema(
    "name",
    "endpoint",
    "workflow_template",
    "model_family",
    "scale_to_length",
    "timeout_seconds",
    "poll_interval_seconds",
    "free_after_run",
    "live_execution_requires_execute_flag",
    workflow_nodes=_PROVIDER_WORKFLOW_NODES_SCHEMA,
    sampler_defaults=_PROVIDER_SAMPLER_SCHEMA,
)
_PROVIDER_PROFILE_SCHEMA = _payload_schema("schema_version", profile=_PROVIDER_PROFILE_BODY_SCHEMA)
_PROVIDER_SCHEMA = _payload_schema(
    "prompt",
    "provider_config_path",
    provider_profile=_PROVIDER_PROFILE_SCHEMA,
)

_PLANNING_VALUES_SCHEMA = _payload_schema(*_PLANNING_PROFILE_FIELDS)
_PLANNING_PROFILE_BODY_SCHEMA = _payload_schema("name", planning=_PLANNING_VALUES_SCHEMA)
_PLANNING_PROFILE_SCHEMA = _payload_schema("schema_version", profile=_PLANNING_PROFILE_BODY_SCHEMA)
_COMPLEXITY_LIMIT_SCHEMA = _payload_schema(*_COMPLEXITY_LIMIT_FIELDS)
_DEDUPE_SCHEMA = _payload_schema(*_DEDUPE_FIELDS)
_PLANNING_SCHEMA = _payload_schema(
    planning_profile=_PLANNING_PROFILE_SCHEMA,
    complexity_limits=_COMPLEXITY_LIMIT_SCHEMA,
    base_dedupe_profile=_DEDUPE_SCHEMA,
)

_MACHINE_GCODE_SCHEMA = _payload_schema(*_MACHINE_GCODE_FIELDS)
_MACHINE_PROFILE_BODY_SCHEMA = _payload_schema(
    "name",
    planning=_PLANNING_VALUES_SCHEMA,
    gcode=_MACHINE_GCODE_SCHEMA,
)
_MACHINE_PROFILE_SCHEMA = _payload_schema("schema_version", profile=_MACHINE_PROFILE_BODY_SCHEMA)
_GCODE_BUILD_SCHEMA = _payload_schema(machine_build_profile=_MACHINE_PROFILE_SCHEMA)
_GCODE_CHECK_SCHEMA = _payload_schema(
    "expected_gcode_sha256",
    machine_build_profile=_MACHINE_PROFILE_SCHEMA,
)

_TEST_NESTED_SCHEMA = _payload_schema("enabled", "pretty_print", "bounds_normalized", "redistribute_paths")
_TEST_SCHEMA = _payload_schema("mode", nested=_TEST_NESTED_SCHEMA)
_PAYLOAD_SCHEMAS: Mapping[WorkerKind, _PayloadSchema] = {
    WorkerKind.PROVIDER_LOCAL_COMFYUI: _PROVIDER_SCHEMA,
    WorkerKind.PATHS_PLAN: _PLANNING_SCHEMA,
    WorkerKind.GCODE_BUILD: _GCODE_BUILD_SCHEMA,
    WorkerKind.GCODE_CHECK: _GCODE_CHECK_SCHEMA,
    WorkerKind.TEST_ECHO: _TEST_SCHEMA,
    WorkerKind.TEST_HANG: _TEST_SCHEMA,
}
_EXACT_PAYLOAD_FIELDS: Mapping[WorkerKind, frozenset[str]] = {
    WorkerKind.PROVIDER_LOCAL_COMFYUI: _PROVIDER_PAYLOAD_FIELDS,
    WorkerKind.PATHS_PLAN: _PLANNING_PAYLOAD_FIELDS,
    WorkerKind.GCODE_BUILD: _GCODE_BUILD_PAYLOAD_FIELDS,
    WorkerKind.GCODE_CHECK: _GCODE_CHECK_PAYLOAD_FIELDS,
}


def _payload_field_error(context: str, fields: Iterable[str]) -> NoReturn:
    names = ", ".join(sorted(fields))
    _invalid(
        f"{context} contains forbidden worker fields or unknown worker payload fields: {names}",
        field=context,
    )


def _reject_objects_in_payload_leaf(value: object, context: str) -> None:
    if isinstance(value, Mapping):
        data = cast(Mapping[str, object], value)
        if data:
            _payload_field_error(context, data)
    elif isinstance(value, list | _FrozenJsonArray):
        for item in value:
            _reject_objects_in_payload_leaf(item, f"{context}[]")


def _validate_payload_schema(value: object, schema: _PayloadSchema, context: str) -> None:
    if isinstance(value, Mapping):
        data = cast(Mapping[str, object], value)
        unknown = set(data) - schema.fields
        if unknown:
            _payload_field_error(context, unknown)
        for key, item in data.items():
            child_schema = schema.object_fields.get(key)
            if child_schema is None:
                _reject_objects_in_payload_leaf(item, f"{context}.{key}")
            else:
                _validate_payload_schema(item, child_schema, f"{context}.{key}")
    elif isinstance(value, list | _FrozenJsonArray):
        for item in value:
            _validate_payload_schema(item, schema, f"{context}[]")


def _require_payload_fields(
    payload: Mapping[str, JsonValue],
    *,
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(payload)
    unknown = actual - expected
    if unknown:
        _payload_field_error(context, unknown)
    missing = expected - actual
    if missing:
        _invalid(f"{context} has missing worker payload fields: {', '.join(sorted(missing))}", field=context)


def _validate_worker_payload(kind: WorkerKind, payload: Mapping[str, JsonValue]) -> None:
    if kind is WorkerKind.IMAGE_PREPARE:
        mode = _string(payload.get("mode"), "worker_task.payload.mode")
        if mode == "direct":
            expected = _IMAGE_DIRECT_PAYLOAD_FIELDS
            schema = _IMAGE_DIRECT_SCHEMA
        elif mode == "normalize_existing":
            expected = _IMAGE_NORMALIZE_PAYLOAD_FIELDS
            schema = _IMAGE_NORMALIZE_SCHEMA
        else:
            _invalid("worker_task.payload.mode has an invalid image preparation mode", field="worker_task.payload.mode")
        _validate_payload_schema(payload, schema, "worker_task.payload")
        _require_payload_fields(payload, expected=expected, context="worker_task.payload")
        return
    _validate_payload_schema(payload, _PAYLOAD_SCHEMAS[kind], "worker_task.payload")
    if kind in {WorkerKind.TEST_ECHO, WorkerKind.TEST_HANG}:
        _string(payload.get("mode"), "worker_task.payload.mode")
        missing = {"mode"} - set(payload)
        if missing:
            _invalid("worker_task.payload has missing worker payload fields: mode", field="worker_task.payload")
        unknown = set(payload) - _TEST_PAYLOAD_FIELDS
        if unknown:
            _payload_field_error("worker_task.payload", unknown)
        return
    _require_payload_fields(payload, expected=_EXACT_PAYLOAD_FIELDS[kind], context="worker_task.payload")


@dataclass(frozen=True, slots=True)
class WorkerInputArtifact:
    role: str
    absolute_path: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        _string(self.role, "worker_input.role")
        _absolute_path(self.absolute_path, "worker_input.absolute_path")
        _digest(self.sha256, "worker_input.sha256")
        _integer(self.size_bytes, "worker_input.size_bytes")
        _string(self.media_type, "worker_input.media_type")

    def to_json(self) -> JsonObject:
        return {
            "role": self.role,
            "absolute_path": self.absolute_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "worker input artifact")
        _exact_fields(data, _INPUT_FIELDS, "worker input artifact")
        return cls(
            role=_string(data["role"], "worker_input.role"),
            absolute_path=_absolute_path(data["absolute_path"], "worker_input.absolute_path"),
            sha256=_digest(data["sha256"], "worker_input.sha256"),
            size_bytes=_integer(data["size_bytes"], "worker_input.size_bytes"),
            media_type=_string(data["media_type"], "worker_input.media_type"),
        )


@dataclass(frozen=True, slots=True)
class WorkerTaskV1:
    schema_version: int
    task_id: str
    job_id: str
    job_revision: int
    attempt_id: str
    kind: WorkerKind
    input_artifacts: tuple[WorkerInputArtifact, ...]
    config_digests: Mapping[str, str]
    staging_dir: str
    payload: JsonObject

    def __post_init__(self) -> None:
        _schema(self.schema_version, "worker_task.schema_version")
        _identifier(self.task_id, "worker_task.task_id")
        _identifier(self.job_id, "worker_task.job_id")
        _integer(self.job_revision, "worker_task.job_revision")
        _identifier(self.attempt_id, "worker_task.attempt_id")
        if not isinstance(self.kind, WorkerKind):
            _invalid("worker_task.kind must be a WorkerKind", field="worker_task.kind")
        artifacts = tuple(self.input_artifacts)
        if any(not isinstance(artifact, WorkerInputArtifact) for artifact in artifacts):
            _invalid("worker_task.input_artifacts must contain WorkerInputArtifact values")
        roles = [artifact.role for artifact in artifacts]
        paths = [artifact.absolute_path for artifact in artifacts]
        if len(roles) != len(set(roles)):
            _invalid("worker_task has duplicate input artifact role")
        if len(paths) != len(set(paths)):
            _invalid("worker_task has duplicate input artifact path")
        object.__setattr__(self, "input_artifacts", artifacts)
        object.__setattr__(self, "config_digests", _config_digests(self.config_digests))
        _absolute_path(self.staging_dir, "worker_task.staging_dir")
        frozen_payload = _freeze_object(self.payload, "worker_task.payload")
        _validate_worker_payload(self.kind, frozen_payload)
        object.__setattr__(self, "payload", frozen_payload)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "attempt_id": self.attempt_id,
            "kind": self.kind.value,
            "input_artifacts": [artifact.to_json() for artifact in self.input_artifacts],
            "config_digests": dict(self.config_digests),
            "staging_dir": self.staging_dir,
            "payload": _plain_object(self.payload),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "worker task")
        _exact_fields(data, _TASK_FIELDS, "worker task")
        inputs = data["input_artifacts"]
        if not isinstance(inputs, list):
            _invalid("worker_task.input_artifacts must be a JSON array")
        return cls(
            schema_version=_schema(data["schema_version"], "worker_task.schema_version"),
            task_id=_identifier(data["task_id"], "worker_task.task_id"),
            job_id=_identifier(data["job_id"], "worker_task.job_id"),
            job_revision=_integer(data["job_revision"], "worker_task.job_revision"),
            attempt_id=_identifier(data["attempt_id"], "worker_task.attempt_id"),
            kind=cast(WorkerKind, _enum(WorkerKind, data["kind"], "worker_task.kind")),
            input_artifacts=tuple(WorkerInputArtifact.from_json(item) for item in inputs),
            config_digests=_config_digests(data["config_digests"]),
            staging_dir=_absolute_path(data["staging_dir"], "worker_task.staging_dir"),
            payload=_freeze_object(data["payload"], "worker_task.payload"),
        )


@dataclass(frozen=True, slots=True)
class WorkerOutcomeV1:
    schema_version: int
    task_id: str
    job_id: str
    job_revision: int
    attempt_id: str
    status: WorkerStatus
    artifacts: tuple[WorkerArtifact, ...]
    metrics: JsonObject
    error: ErrorPayload | None

    def __post_init__(self) -> None:
        _schema(self.schema_version, "worker_outcome.schema_version")
        _identifier(self.task_id, "worker_outcome.task_id")
        _identifier(self.job_id, "worker_outcome.job_id")
        _integer(self.job_revision, "worker_outcome.job_revision")
        _identifier(self.attempt_id, "worker_outcome.attempt_id")
        if not isinstance(self.status, WorkerStatus):
            _invalid("worker_outcome.status must be a WorkerStatus", field="worker_outcome.status")
        artifacts = tuple(_worker_artifact(item, "worker_outcome.artifact") for item in self.artifacts)
        roles = [artifact.role for artifact in artifacts]
        paths = [artifact.relative_path for artifact in artifacts]
        if len(roles) != len(set(roles)):
            _invalid("worker_outcome has duplicate artifact role")
        if len(paths) != len(set(paths)):
            _invalid("worker_outcome has duplicate artifact path")
        if self.status in {WorkerStatus.FAILED, WorkerStatus.CANCELLED} and artifacts:
            _invalid("failed or cancelled worker outcomes must not publish artifacts")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "metrics", _freeze_object(self.metrics, "worker_outcome.metrics"))
        if self.status is WorkerStatus.SUCCEEDED:
            if self.error is not None:
                _invalid("successful worker outcome must not contain an error")
        elif self.error is None:
            _invalid("non-success worker outcome must contain an error")
        if self.error is not None:
            cloned_error = _error_payload(self.error, "worker_outcome.error")
            if cloned_error.job_id is not None and cloned_error.job_id != self.job_id:
                _invalid("worker_outcome.error job_id must match outcome job_id")
            object.__setattr__(self, "error", cloned_error)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "attempt_id": self.attempt_id,
            "status": self.status.value,
            "artifacts": [_artifact_json(artifact) for artifact in self.artifacts],
            "metrics": _plain_object(self.metrics),
            "error": None if self.error is None else _error_json(self.error),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "worker outcome")
        _exact_fields(data, _OUTCOME_FIELDS, "worker outcome")
        artifacts = data["artifacts"]
        if not isinstance(artifacts, list):
            _invalid("worker_outcome.artifacts must be a JSON array")
        error_value = data["error"]
        return cls(
            schema_version=_schema(data["schema_version"], "worker_outcome.schema_version"),
            task_id=_identifier(data["task_id"], "worker_outcome.task_id"),
            job_id=_identifier(data["job_id"], "worker_outcome.job_id"),
            job_revision=_integer(data["job_revision"], "worker_outcome.job_revision"),
            attempt_id=_identifier(data["attempt_id"], "worker_outcome.attempt_id"),
            status=cast(WorkerStatus, _enum(WorkerStatus, data["status"], "worker_outcome.status")),
            artifacts=tuple(_worker_artifact(item, "worker_outcome.artifact") for item in artifacts),
            metrics=_freeze_object(data["metrics"], "worker_outcome.metrics"),
            error=None if error_value is None else _error_payload(error_value, "worker_outcome.error"),
        )


class WorkerHandle(Protocol):
    @property
    def task_id(self) -> str: ...

    @property
    def attempt_id(self) -> str: ...

    def is_alive(self) -> bool: ...


class WorkerSupervisor(Protocol):
    def submit(self, task: WorkerTaskV1) -> WorkerHandle: ...

    def wait(
        self,
        handle: WorkerHandle,
        *,
        timeout_seconds: float,
        cancel_requested: Callable[[], bool],
    ) -> WorkerOutcomeV1: ...

    def cancel(self, handle: WorkerHandle, *, grace_seconds: float = 2.0) -> WorkerOutcomeV1: ...

    def close(self) -> None: ...


__all__ = [
    "WorkerHandle",
    "WorkerInputArtifact",
    "WorkerKind",
    "WorkerOutcomeV1",
    "WorkerStatus",
    "WorkerSupervisor",
    "WorkerTaskV1",
]
