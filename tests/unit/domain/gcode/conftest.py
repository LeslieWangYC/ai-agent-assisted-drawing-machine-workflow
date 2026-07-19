from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.json_types import JsonObject, JsonValue

GOLDEN_ROOT = Path(__file__).resolve().parents[3] / "fixtures/package_b/golden"

PLANNING_VALUES: dict[str, JsonValue] = {
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

GCODE_VALUES: dict[str, JsonValue] = {
    "hardware_canvas_width_mm": 144.0,
    "hardware_canvas_height_mm": 144.0,
    "machine_width_mm": 192.0,
    "machine_height_mm": 192.0,
    "paper_center_x": 96.0,
    "paper_center_y": 96.0,
    "pen_up_z": 3.5,
    "pen_down_z": 0.0,
    "feed_travel": 1200.0,
    "feed_draw": 900.0,
    "feed_pen_down": 100.0,
    "feed_pen_up": 400.0,
    "max_feed": 1200.0,
    "work_coordinate": "G54",
    "align_mode": "center",
    "mirror_y": True,
    "safe_start": True,
    "path_mode": "stroke",
}


def profile_envelope(
    *,
    profile_changes: Mapping[str, JsonValue] | None = None,
    planning_changes: Mapping[str, JsonValue] | None = None,
    gcode_changes: Mapping[str, JsonValue] | None = None,
    schema_version: int = 1,
) -> ProfileEnvelope:
    planning = dict(PLANNING_VALUES)
    planning.update(planning_changes or {})
    gcode = dict(GCODE_VALUES)
    gcode.update(gcode_changes or {})
    profile: JsonObject = {"name": "stage1", "planning": planning, "gcode": gcode}
    profile.update(profile_changes or {})
    return ProfileEnvelope(schema_version=schema_version, profile=profile)


@pytest.fixture
def golden_root() -> Path:
    return GOLDEN_ROOT


@pytest.fixture
def valid_profile_envelope() -> ProfileEnvelope:
    return profile_envelope()


def json_object(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return cast(JsonObject, value)
