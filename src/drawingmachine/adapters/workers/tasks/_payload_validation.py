from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.domain.gcode.models import MachineBuildProfile, parse_machine_build_profile
from drawingmachine.domain.planning.models import PathPlanConfig, parse_path_plan_config
from drawingmachine.domain.workflow import ComplexityLimits, DedupeProfile, DirectImageConfig, NormalizationConfig
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.providers import MAX_PROVIDER_PROMPT_BYTES
from drawingmachine.ports.workers import WorkerTaskV1

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NORMALIZATION_FIELDS = frozenset(
    {
        "threshold",
        "invert",
        "min_component_area_px",
        "median_filter_size",
        "color_delta_threshold",
        "remove_small_components_px",
    }
)
_PROVIDER_FIELDS = frozenset(
    {
        "name",
        "endpoint",
        "workflow_template",
        "model_family",
        "scale_to_length",
        "timeout_seconds",
        "poll_interval_seconds",
        "free_after_run",
        "workflow_nodes",
        "sampler_defaults",
        "live_execution_requires_execute_flag",
    }
)
_WORKFLOW_NODE_FIELDS = frozenset({"load_image", "prompt", "sampler", "save_image", "scale"})
_SAMPLER_FIELDS = frozenset({"steps", "cfg", "denoise"})
_COMPLEXITY_FIELDS = frozenset(
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
class ValidatedImagePayload:
    mode: str
    source_name: str
    route_mode: str | None
    semantic_assessment: JsonObject | None
    direct_config: DirectImageConfig | None
    normalization_config: NormalizationConfig


@dataclass(frozen=True, slots=True)
class ValidatedProviderPayload:
    prompt: str | None
    profile: ProfileEnvelope
    config_path: Path


@dataclass(frozen=True, slots=True)
class ValidatedPlanningPayload:
    config: PathPlanConfig
    complexity_limits: ComplexityLimits
    base_dedupe_profile: DedupeProfile


@dataclass(frozen=True, slots=True)
class ValidatedGcodeBuildPayload:
    profile: MachineBuildProfile


@dataclass(frozen=True, slots=True)
class ValidatedGcodeCheckPayload:
    profile: MachineBuildProfile
    expected_gcode_sha256: str


def _invalid(task: WorkerTaskV1) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            "WORKER_INPUT_INVALID",
            ErrorCategory.INPUT,
            "worker payload validation failed",
            False,
            {},
            job_id=task.job_id,
        )
    ) from None


def _object(task: WorkerTaskV1, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _invalid(task)
    return cast(Mapping[str, object], value)


def _exact(task: WorkerTaskV1, value: Mapping[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        _invalid(task)


def _no_unknown(task: WorkerTaskV1, value: Mapping[str, object], allowed: frozenset[str]) -> None:
    if not set(value) <= allowed:
        _invalid(task)


def _string(
    task: WorkerTaskV1,
    value: object,
    *,
    maximum_bytes: int = 4096,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _invalid(task)
    result = value
    try:
        encoded = result.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(task)
    if len(encoded) > maximum_bytes or (
        not allow_controls and any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        _invalid(task)
    return result


def _integer(task: WorkerTaskV1, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        _invalid(task)
    result = value
    if not minimum <= result <= maximum:
        _invalid(task)
    return result


def _number(task: WorkerTaskV1, value: object, *, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        _invalid(task)
    try:
        result = float(cast(int | float, value))
    except OverflowError:
        _invalid(task)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        _invalid(task)
    return result


def _boolean(task: WorkerTaskV1, value: object) -> bool:
    if type(value) is not bool:
        _invalid(task)
    return value


def _optional_boolean(task: WorkerTaskV1, value: object) -> bool | None:
    if value is None:
        return None
    return _boolean(task, value)


def _sequence(task: WorkerTaskV1, value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        _invalid(task)
    return cast(Sequence[object], value)


def _string_array(task: WorkerTaskV1, value: object) -> list[JsonValue]:
    items = _sequence(task, value)
    return cast(
        list[JsonValue],
        [_string(task, item, maximum_bytes=4096, allow_empty=True, allow_controls=True) for item in items],
    )


def _plain_json(task: WorkerTaskV1, value: object) -> JsonValue:
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            _invalid(task)
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        return {str(key): _plain_json(task, item) for key, item in _object(task, value).items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_plain_json(task, item) for item in value]
    _invalid(task)


def _plain_object(task: WorkerTaskV1, value: object) -> JsonObject:
    result = _plain_json(task, value)
    if not isinstance(result, dict):
        _invalid(task)
    return result


def _normalization_values(
    task: WorkerTaskV1,
    value: object,
    *,
    direct: bool,
) -> tuple[DirectImageConfig | None, NormalizationConfig]:
    values = _object(task, value)
    _no_unknown(task, values, _NORMALIZATION_FIELDS)
    raw_threshold = values.get("threshold")
    if direct:
        if raw_threshold is None:
            direct_threshold = None
            normalization_threshold = 220.0
        else:
            direct_threshold = _integer(task, raw_threshold, minimum=0, maximum=255)
            normalization_threshold = float(direct_threshold)
    else:
        direct_threshold = None
        normalization_threshold = (
            220.0 if "threshold" not in values else _number(task, raw_threshold, minimum=0.0, maximum=255.0)
        )
    invert = _optional_boolean(task, values.get("invert"))
    minimum_area = (
        8
        if "min_component_area_px" not in values
        else _integer(task, values["min_component_area_px"], minimum=1, maximum=2_147_483_647)
    )
    median = (
        3
        if "median_filter_size" not in values
        else _integer(task, values["median_filter_size"], minimum=1, maximum=255)
    )
    if median % 2 == 0:
        _invalid(task)
    color_delta = (
        10
        if "color_delta_threshold" not in values
        else _integer(task, values["color_delta_threshold"], minimum=0, maximum=255)
    )
    remove_small = (
        8
        if "remove_small_components_px" not in values
        else _integer(task, values["remove_small_components_px"], minimum=0, maximum=2_147_483_647)
    )
    direct_config = None
    if direct:
        direct_config = DirectImageConfig(
            threshold=direct_threshold,
            invert=invert,
            min_component_area_px=minimum_area,
            median_filter_size=median,
        )
    return direct_config, NormalizationConfig(
        threshold=normalization_threshold,
        color_delta_threshold=color_delta,
        remove_small_components_px=remove_small,
    )


def validate_image_payload(task: WorkerTaskV1) -> ValidatedImagePayload:
    payload = _object(task, task.payload)
    mode = _string(task, payload.get("mode"), maximum_bytes=32)
    if mode == "direct":
        _exact(task, payload, frozenset({"mode", "source_name", "route_mode", "semantic_assessment", "normalization"}))
        source_name = _string(task, payload["source_name"])
        route_mode = _string(task, payload["route_mode"], maximum_bytes=16)
        if route_mode not in {"auto", "direct"}:
            _invalid(task)
        raw_assessment = payload["semantic_assessment"]
        assessment: JsonObject | None = None
        if raw_assessment is not None:
            semantic = _object(task, raw_assessment)
            _exact(
                task,
                semantic,
                frozenset({"semantic_style_score", "semantic_score_source", "semantic_rationale", "semantic_blockers"}),
            )
            source = _string(task, semantic["semantic_score_source"], maximum_bytes=64)
            if source != "router_multimodal_model":
                _invalid(task)
            assessment = {
                "semantic_style_score": _number(task, semantic["semantic_style_score"], minimum=0.0, maximum=0.30),
                "semantic_score_source": source,
                "semantic_rationale": _string_array(task, semantic["semantic_rationale"]),
                "semantic_blockers": _string_array(task, semantic["semantic_blockers"]),
            }
        direct_config, normalization = _normalization_values(task, payload["normalization"], direct=True)
        return ValidatedImagePayload(mode, source_name, route_mode, assessment, direct_config, normalization)
    if mode == "normalize_existing":
        _exact(task, payload, frozenset({"mode", "source_name", "normalization"}))
        source_name = _string(task, payload["source_name"])
        _unused, normalization = _normalization_values(task, payload["normalization"], direct=False)
        return ValidatedImagePayload(mode, source_name, None, None, None, normalization)
    _invalid(task)


def _provider_endpoint(task: WorkerTaskV1, value: object) -> str:
    endpoint = _string(task, value, maximum_bytes=4096).rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        _invalid(task)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _invalid(task)
    return endpoint


def validate_provider_payload(task: WorkerTaskV1) -> ValidatedProviderPayload:
    payload = _object(task, task.payload)
    _exact(task, payload, frozenset({"prompt", "provider_profile", "provider_config_path"}))
    prompt_value = payload["prompt"]
    prompt = (
        None
        if prompt_value is None
        else _string(task, prompt_value, maximum_bytes=MAX_PROVIDER_PROMPT_BYTES, allow_controls=True)
    )
    envelope_value = _object(task, payload["provider_profile"])
    _exact(task, envelope_value, frozenset({"schema_version", "profile"}))
    schema_version = _integer(task, envelope_value["schema_version"], minimum=1, maximum=1)
    profile = _object(task, envelope_value["profile"])
    _exact(task, profile, _PROVIDER_FIELDS)
    if _string(task, profile["name"], maximum_bytes=64) != "local-comfyui":
        _invalid(task)
    _provider_endpoint(task, profile["endpoint"])
    workflow_name = _string(task, profile["workflow_template"])
    workflow_path = PurePosixPath(workflow_name)
    if (
        workflow_name in {".", ".."}
        or not workflow_path.parts
        or "\\" in workflow_name
        or workflow_path.is_absolute()
        or str(workflow_path) != workflow_name
        or any(part in {".", ".."} for part in workflow_path.parts)
    ):
        _invalid(task)
    _string(task, profile["model_family"], maximum_bytes=255)
    _integer(task, profile["scale_to_length"], minimum=1, maximum=16_384)
    timeout = _number(task, profile["timeout_seconds"], minimum=0.001, maximum=3600.0)
    poll = _number(task, profile["poll_interval_seconds"], minimum=0.001, maximum=300.0)
    if poll > timeout or math.ceil(timeout / poll) > 10_000:
        _invalid(task)
    _boolean(task, profile["free_after_run"])
    nodes = _object(task, profile["workflow_nodes"])
    _exact(task, nodes, _WORKFLOW_NODE_FIELDS)
    for node in nodes.values():
        identifier = _string(task, node, maximum_bytes=255)
        if _NODE_ID.fullmatch(identifier) is None:
            _invalid(task)
    sampler = _object(task, profile["sampler_defaults"])
    _exact(task, sampler, _SAMPLER_FIELDS)
    if sampler["steps"] is not None:
        _integer(task, sampler["steps"], minimum=1, maximum=10_000)
    if sampler["cfg"] is not None:
        _number(task, sampler["cfg"], minimum=0.0, maximum=100.0)
    if sampler["denoise"] is not None:
        _number(task, sampler["denoise"], minimum=0.0, maximum=1.0)
    if not _boolean(task, profile["live_execution_requires_execute_flag"]):
        _invalid(task)
    config_path_text = _string(task, payload["provider_config_path"], maximum_bytes=4096)
    config_path = Path(config_path_text)
    if (
        not config_path.is_absolute()
        or str(config_path) != config_path_text
        or any(part in {".", ".."} for part in config_path.parts)
    ):
        _invalid(task)
    return ValidatedProviderPayload(
        prompt,
        ProfileEnvelope(schema_version, _plain_object(task, profile)),
        config_path,
    )


def _profile_envelope(task: WorkerTaskV1, value: object) -> ProfileEnvelope:
    envelope = _object(task, value)
    _exact(task, envelope, frozenset({"schema_version", "profile"}))
    schema_version = _integer(task, envelope["schema_version"], minimum=1, maximum=1)
    return ProfileEnvelope(schema_version, _plain_object(task, envelope["profile"]))


def validate_planning_payload(task: WorkerTaskV1) -> ValidatedPlanningPayload:
    payload = _object(task, task.payload)
    _exact(task, payload, frozenset({"planning_profile", "complexity_limits", "base_dedupe_profile"}))
    envelope = _profile_envelope(task, payload["planning_profile"])
    try:
        config = parse_path_plan_config(envelope)
    except (DrawingMachineError, TypeError, ValueError, OverflowError, KeyError):
        _invalid(task)
    raw_limits = _object(task, payload["complexity_limits"])
    _exact(task, raw_limits, _COMPLEXITY_FIELDS)
    counts = {
        name: _integer(task, raw_limits[name], minimum=0, maximum=2_147_483_647)
        for name in (
            "soft_component_count",
            "soft_skeleton_pixels",
            "soft_boundary_pixels",
            "soft_transitions_total",
            "hard_component_count",
            "hard_skeleton_pixels",
            "hard_boundary_pixels",
            "hard_transitions_total",
        )
    }
    threshold_value = raw_limits["threshold"]
    threshold = None if threshold_value is None else _integer(task, threshold_value, minimum=0, maximum=255)
    invert = _optional_boolean(task, raw_limits["invert"])
    minimum_area = _integer(task, raw_limits["min_component_area_px"], minimum=1, maximum=2_147_483_647)
    limits = ComplexityLimits(
        soft_component_count=counts["soft_component_count"],
        soft_skeleton_pixels=counts["soft_skeleton_pixels"],
        soft_boundary_pixels=counts["soft_boundary_pixels"],
        soft_transitions_total=counts["soft_transitions_total"],
        hard_component_count=counts["hard_component_count"],
        hard_skeleton_pixels=counts["hard_skeleton_pixels"],
        hard_boundary_pixels=counts["hard_boundary_pixels"],
        hard_transitions_total=counts["hard_transitions_total"],
        threshold=threshold,
        invert=invert,
        min_component_area_px=minimum_area,
    )
    raw_dedupe = _object(task, payload["base_dedupe_profile"])
    _exact(task, raw_dedupe, _DEDUPE_FIELDS)
    base = DedupeProfile(
        dedupe_short_path_length_mm=_number(
            task, raw_dedupe["dedupe_short_path_length_mm"], minimum=0.0, maximum=1_000_000.0
        ),
        dedupe_distance_mm=_number(task, raw_dedupe["dedupe_distance_mm"], minimum=0.0, maximum=1_000_000.0),
        dedupe_angle_deg=_number(task, raw_dedupe["dedupe_angle_deg"], minimum=0.0, maximum=180.0),
        dedupe_overlap_ratio=_number(task, raw_dedupe["dedupe_overlap_ratio"], minimum=0.0, maximum=1.0),
    )
    return ValidatedPlanningPayload(config, limits, base)


def _machine_profile(task: WorkerTaskV1, value: object) -> MachineBuildProfile:
    envelope = _profile_envelope(task, value)
    try:
        return parse_machine_build_profile(envelope)
    except (DrawingMachineError, TypeError, ValueError, OverflowError, KeyError):
        _invalid(task)


def validate_gcode_build_payload(task: WorkerTaskV1) -> ValidatedGcodeBuildPayload:
    payload = _object(task, task.payload)
    _exact(task, payload, frozenset({"machine_build_profile"}))
    return ValidatedGcodeBuildPayload(_machine_profile(task, payload["machine_build_profile"]))


def validate_gcode_check_payload(task: WorkerTaskV1) -> ValidatedGcodeCheckPayload:
    payload = _object(task, task.payload)
    _exact(task, payload, frozenset({"machine_build_profile", "expected_gcode_sha256"}))
    expected = _string(task, payload["expected_gcode_sha256"], maximum_bytes=64)
    if _SHA256.fullmatch(expected) is None:
        _invalid(task)
    return ValidatedGcodeCheckPayload(_machine_profile(task, payload["machine_build_profile"]), expected)


__all__ = [
    "ValidatedGcodeBuildPayload",
    "ValidatedGcodeCheckPayload",
    "ValidatedImagePayload",
    "ValidatedPlanningPayload",
    "ValidatedProviderPayload",
    "validate_gcode_build_payload",
    "validate_gcode_check_payload",
    "validate_image_payload",
    "validate_planning_payload",
    "validate_provider_payload",
]
