from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, cast

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonValue

Pixel = tuple[int, int]
Point = tuple[float, float]

_PROFILE_FIELDS = frozenset({"name", "planning"})
_SUPPORTED_SCHEMA_VERSION = 1
_PLANNING_FIELDS = frozenset(
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


@dataclass(frozen=True, slots=True)
class PathPlanConfig:
    canvas_width_mm: float = 120.0
    canvas_height_mm: float = 120.0
    pen_width_mm: float = 0.5
    min_gap_mm: float = 0.8
    threshold: int | None = None
    invert: bool | None = None
    min_component_area_px: int = 8
    simplify_tolerance_mm: float = 0.12
    min_path_length_mm: float = 0.6
    drop_short_stroke_mm: float = 0.35
    merge_endpoint_distance_mm: float = 0.45
    merge_angle_deg: float = 35.0
    dedupe_short_path_length_mm: float = 2.0
    dedupe_distance_mm: float = 0.3
    dedupe_angle_deg: float = 25.0
    dedupe_overlap_ratio: float = 0.65
    hatch_spacing_mm: float = 0.8
    hatch_min_run_mm: float = 0.8
    fill_min_thickness_mm: float = 0.85


@dataclass(frozen=True, slots=True)
class BinarizedImage:
    foreground: frozenset[Pixel]
    width: int
    height: int
    threshold: int
    inverted: bool


@dataclass(frozen=True, slots=True)
class ConnectedComponent:
    pixels: frozenset[Pixel]
    area_px: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int
    width_px: int
    height_px: int


def parse_path_plan_config(profile: ProfileEnvelope) -> PathPlanConfig:
    if (
        isinstance(profile.schema_version, bool)
        or not isinstance(profile.schema_version, int)
        or profile.schema_version != _SUPPORTED_SCHEMA_VERSION
    ):
        _invalid("profile.schema_version must be 1", {"field": "profile.schema_version"})
    profile_values = _object(profile.profile, "profile")
    _require_fields(profile_values, _PROFILE_FIELDS, "profile")
    _string(profile_values["name"], "profile.name")
    planning = _object(profile_values["planning"], "profile.planning")
    _require_exact_fields(planning, _PLANNING_FIELDS, "profile.planning")

    return PathPlanConfig(
        canvas_width_mm=_positive_number(planning["canvas_width_mm"], "profile.planning.canvas_width_mm"),
        canvas_height_mm=_positive_number(planning["canvas_height_mm"], "profile.planning.canvas_height_mm"),
        pen_width_mm=_positive_number(planning["pen_width_mm"], "profile.planning.pen_width_mm"),
        min_gap_mm=_nonnegative_number(planning["min_gap_mm"], "profile.planning.min_gap_mm"),
        threshold=_threshold(planning["threshold"]),
        invert=_optional_boolean(planning["invert"], "profile.planning.invert"),
        min_component_area_px=_positive_integer(
            planning["min_component_area_px"],
            "profile.planning.min_component_area_px",
        ),
        simplify_tolerance_mm=_nonnegative_number(
            planning["simplify_tolerance_mm"],
            "profile.planning.simplify_tolerance_mm",
        ),
        min_path_length_mm=_nonnegative_number(
            planning["min_path_length_mm"],
            "profile.planning.min_path_length_mm",
        ),
        drop_short_stroke_mm=_nonnegative_number(
            planning["drop_short_stroke_mm"],
            "profile.planning.drop_short_stroke_mm",
        ),
        merge_endpoint_distance_mm=_nonnegative_number(
            planning["merge_endpoint_distance_mm"],
            "profile.planning.merge_endpoint_distance_mm",
        ),
        merge_angle_deg=_bounded_number(planning["merge_angle_deg"], "profile.planning.merge_angle_deg", 0.0, 180.0),
        dedupe_short_path_length_mm=_nonnegative_number(
            planning["dedupe_short_path_length_mm"],
            "profile.planning.dedupe_short_path_length_mm",
        ),
        dedupe_distance_mm=_nonnegative_number(
            planning["dedupe_distance_mm"],
            "profile.planning.dedupe_distance_mm",
        ),
        dedupe_angle_deg=_bounded_number(
            planning["dedupe_angle_deg"],
            "profile.planning.dedupe_angle_deg",
            0.0,
            180.0,
        ),
        dedupe_overlap_ratio=_bounded_number(
            planning["dedupe_overlap_ratio"],
            "profile.planning.dedupe_overlap_ratio",
            0.0,
            1.0,
        ),
        hatch_spacing_mm=_positive_number(planning["hatch_spacing_mm"], "profile.planning.hatch_spacing_mm"),
        hatch_min_run_mm=_nonnegative_number(planning["hatch_min_run_mm"], "profile.planning.hatch_min_run_mm"),
        fill_min_thickness_mm=_nonnegative_number(
            planning["fill_min_thickness_mm"],
            "profile.planning.fill_min_thickness_mm",
        ),
    )


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{field} must be an object", {"field": field})
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _invalid(f"{field} contains a non-string key", {"field": field})
        result[key] = item
    return result


def _require_fields(values: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    missing = sorted(expected - set(values))
    if missing:
        _invalid(
            f"{field} has missing fields: {', '.join(missing)}",
            {"field": field, "missing_fields": cast(list[JsonValue], missing)},
        )


def _require_exact_fields(values: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    actual = set(values)
    unknown = sorted(actual - expected)
    if unknown:
        _invalid(
            f"{field} has unknown fields: {', '.join(unknown)}",
            {"field": field, "unknown_fields": cast(list[JsonValue], unknown)},
        )
    missing = sorted(expected - actual)
    if missing:
        _invalid(
            f"{field} has missing fields: {', '.join(missing)}",
            {"field": field, "missing_fields": cast(list[JsonValue], missing)},
        )


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _invalid(f"{field} must be a non-empty string", {"field": field})
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _invalid(f"{field} must be a number", {"field": field})
    try:
        result = float(value)
    except OverflowError:
        _invalid(f"{field} must be finite", {"field": field})
    if not math.isfinite(result):
        _invalid(f"{field} must be finite", {"field": field})
    return result


def _positive_number(value: object, field: str) -> float:
    result = _number(value, field)
    if result <= 0.0:
        _invalid(f"{field} must be greater than zero", {"field": field})
    return result


def _nonnegative_number(value: object, field: str) -> float:
    result = _number(value, field)
    if result < 0.0:
        _invalid(f"{field} must be zero or greater", {"field": field})
    return result


def _bounded_number(value: object, field: str, minimum: float, maximum: float) -> float:
    result = _number(value, field)
    if not minimum <= result <= maximum:
        _invalid(f"{field} must be between {minimum:g} and {maximum:g}", {"field": field})
    return result


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{field} must be an integer", {"field": field})
    if value < 1:
        _invalid(f"{field} must be at least 1", {"field": field})
    return value


def _threshold(value: object) -> int | None:
    field = "profile.planning.threshold"
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{field} must be an integer or null", {"field": field})
    if not 0 <= value <= 255:
        _invalid(f"{field} must be between 0 and 255", {"field": field})
    return value


def _optional_boolean(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        _invalid(f"{field} must be a Boolean or null", {"field": field})
    return value


def _invalid(message: str, details: Mapping[str, JsonValue]) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="PATH_PLAN_CONFIG_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            retryable=False,
            details=details,
        )
    )


__all__ = [
    "BinarizedImage",
    "ConnectedComponent",
    "PathPlanConfig",
    "Pixel",
    "Point",
    "parse_path_plan_config",
]
