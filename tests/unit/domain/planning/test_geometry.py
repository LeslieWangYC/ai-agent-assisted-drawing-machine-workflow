from __future__ import annotations

from dataclasses import fields
from math import inf, nan

import pytest

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.domain.planning.geometry import (
    drawing_transform,
    estimate_nearest_neighbor_travel,
    euclidean_distance,
    point_line_distance,
    points_px_to_mm,
    polyline_length,
    simplify_polyline,
)
from drawingmachine.domain.planning.models import PathPlanConfig, parse_path_plan_config
from drawingmachine.errors import DrawingMachineError, ErrorCategory

PLANNING_VALUES: dict[str, object] = {
    "canvas_width_mm": 120.0,
    "canvas_height_mm": 120.0,
    "pen_width_mm": 0.5,
    "min_gap_mm": 0.8,
    "threshold": None,
    "invert": None,
    "min_component_area_px": 8,
    "simplify_tolerance_mm": 0.12,
    "min_path_length_mm": 0.6,
    "drop_short_stroke_mm": 0.35,
    "merge_endpoint_distance_mm": 0.45,
    "merge_angle_deg": 35.0,
    "dedupe_short_path_length_mm": 2.0,
    "dedupe_distance_mm": 0.3,
    "dedupe_angle_deg": 25.0,
    "dedupe_overlap_ratio": 0.65,
    "hatch_spacing_mm": 0.8,
    "hatch_min_run_mm": 0.8,
    "fill_min_thickness_mm": 0.85,
}


def profile_with(**changes: object) -> ProfileEnvelope:
    planning = dict(PLANNING_VALUES)
    planning.update(changes)
    return ProfileEnvelope(schema_version=1, profile={"name": "default", "planning": planning})


def test_path_plan_config_preserves_exact_legacy_fields_defaults_and_order() -> None:
    assert [(field.name, field.default) for field in fields(PathPlanConfig)] == list(PLANNING_VALUES.items())
    assert parse_path_plan_config(profile_with()) == PathPlanConfig()


def test_parse_path_plan_config_accepts_unrelated_profile_sections() -> None:
    profile = ProfileEnvelope(
        schema_version=1,
        profile={
            "name": "default",
            "planning": dict(PLANNING_VALUES),
            "gcode": {"feed_rate_mm_min": 1200.0},
        },
    )

    assert parse_path_plan_config(profile) == PathPlanConfig()


@pytest.mark.parametrize(
    "profile",
    [
        ProfileEnvelope(
            schema_version=2,
            profile={"name": "default", "planning": dict(PLANNING_VALUES)},
        ),
        ProfileEnvelope(schema_version=1, profile={"planning": dict(PLANNING_VALUES)}),
        ProfileEnvelope(schema_version=1, profile={"name": "default"}),
        ProfileEnvelope(
            schema_version=1,
            profile={"name": "default", "planning": {**PLANNING_VALUES, "unexpected": 1}},
        ),
        profile_with(canvas_width_mm=True),
        profile_with(min_component_area_px=False),
        profile_with(threshold=True),
        profile_with(dedupe_distance_mm=nan),
        profile_with(canvas_height_mm=inf),
    ],
)
def test_parse_path_plan_config_rejects_incomplete_unknown_boolean_and_non_finite_values(
    profile: ProfileEnvelope,
) -> None:
    with pytest.raises(DrawingMachineError) as raised:
        parse_path_plan_config(profile)

    assert raised.value.payload.category is ErrorCategory.CONFIGURATION
    assert raised.value.payload.code == "PATH_PLAN_CONFIG_INVALID"
    assert raised.value.payload.retryable is False


def test_parse_path_plan_config_rejects_integer_too_large_for_float_conversion() -> None:
    field = "profile.planning.canvas_width_mm"

    with pytest.raises(DrawingMachineError) as raised:
        parse_path_plan_config(profile_with(canvas_width_mm=10**10000))

    assert raised.value.payload.code == "PATH_PLAN_CONFIG_INVALID"
    assert raised.value.payload.category is ErrorCategory.CONFIGURATION
    assert raised.value.payload.message == f"{field} must be finite"
    assert raised.value.payload.retryable is False
    assert raised.value.payload.details == {"field": field}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canvas_width_mm", 0.0),
        ("threshold", -1),
        ("threshold", 256),
        ("min_component_area_px", 0),
        ("simplify_tolerance_mm", -0.01),
        ("merge_angle_deg", 181.0),
        ("dedupe_overlap_ratio", 1.01),
    ],
)
def test_parse_path_plan_config_rejects_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(DrawingMachineError, match=field):
        parse_path_plan_config(profile_with(**{field: value}))


def test_distance_primitives_match_frozen_stage1_values() -> None:
    points = ((0.0, 0.0), (3.0, 4.0), (6.0, 4.0))

    assert euclidean_distance(points[0], points[1]) == 5.0
    assert point_line_distance((2.0, 3.0), (0.0, 0.0), (4.0, 0.0)) == 3.0
    assert point_line_distance((2.0, 3.0), (1.0, 1.0), (1.0, 1.0)) == pytest.approx(2.23606797749979)
    assert polyline_length(points) == 8.0


def test_drawing_transform_and_point_rounding_match_frozen_values() -> None:
    actual_transform = drawing_transform(7, 3, 10.0, 8.0)
    expected_transform = (1.4285714285714286, 10.0, 4.285714285714286, 0.0, 1.8571428571428572)

    assert actual_transform == expected_transform
    scale, _, _, offset_x_mm, offset_y_mm = actual_transform
    pixels = ((0, 0), (1, 2), (6, 2))
    assert points_px_to_mm(pixels, scale, offset_x_mm, offset_y_mm) == (
        (0.0, 1.8571),
        (1.4286, 4.7143),
        (8.5714, 4.7143),
    )


def test_rdp_preserves_frozen_threshold_comparison_and_immutable_output() -> None:
    points = ((0.0, 0.0), (0.10004, 0.00004), (1.0, 0.0))

    assert simplify_polyline(points, tolerance_mm=0.12) == ((0.0, 0.0), (1.0, 0.0))
    assert simplify_polyline(points, tolerance_mm=0.00001) == points


def test_nearest_neighbor_travel_matches_frozen_tie_order_and_reversal() -> None:
    paths = (
        ((5.0, 0.0), (4.0, 0.0)),
        ((0.0, 3.0), (0.0, 2.0)),
        ((4.0, 4.0), (4.0, 3.0)),
    )
    assert estimate_nearest_neighbor_travel(paths) == 10.0
