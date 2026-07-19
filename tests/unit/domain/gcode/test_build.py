from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import cast

import pytest

import drawingmachine.domain.gcode.models as gcode_models
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.domain.gcode.build import build_gcode
from drawingmachine.domain.gcode.models import (
    GcodeBuildResult,
    MachineBuildProfile,
    PreviewResult,
    parse_machine_build_profile,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject, JsonValue

from .conftest import GCODE_VALUES, PLANNING_VALUES, profile_envelope


def read_json_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def test_machine_build_profile_is_exactly_frozen_and_slotted() -> None:
    assert [field.name for field in fields(MachineBuildProfile)] == list(GCODE_VALUES)
    assert MachineBuildProfile.__slots__ == tuple(GCODE_VALUES)
    profile = MachineBuildProfile.stage1_defaults()
    with pytest.raises(FrozenInstanceError):
        profile.path_mode = "preview"


def test_parse_machine_build_profile_accepts_complete_profile_and_future_sibling_section(
    valid_profile_envelope: ProfileEnvelope,
) -> None:
    envelope = ProfileEnvelope(
        schema_version=valid_profile_envelope.schema_version,
        profile={**valid_profile_envelope.profile, "future_section": {"enabled": True}},
    )

    assert parse_machine_build_profile(envelope) == MachineBuildProfile.stage1_defaults()


def test_gcode_model_private_documents_and_profile_keys_fail_closed(
    valid_profile_envelope: ProfileEnvelope,
) -> None:
    with pytest.raises(RuntimeError, match="not an object"):
        gcode_models._load_safety_json_document("[]")
    with pytest.raises(DrawingMachineError, match="non-string key"):
        gcode_models._object({1: "value"}, "profile")

    incomplete = dict(valid_profile_envelope.profile)
    del incomplete["planning"]
    with pytest.raises(DrawingMachineError, match="missing fields: planning"):
        parse_machine_build_profile(ProfileEnvelope(1, incomplete))


@pytest.mark.parametrize(
    "envelope",
    [
        ProfileEnvelope(schema_version=True, profile={"name": "stage1", "planning": {}, "gcode": {}}),
        profile_envelope(schema_version=2),
        ProfileEnvelope(schema_version=1, profile=cast(JsonObject, [])),
        profile_envelope(profile_changes={"name": ""}),
        profile_envelope(profile_changes={"name": 1}),
        profile_envelope(profile_changes={"planning": []}),
        profile_envelope(profile_changes={"gcode": []}),
    ],
    ids=[
        "bool-schema",
        "schema-version",
        "profile-object",
        "empty-name",
        "name-type",
        "planning-object",
        "gcode-object",
    ],
)
def test_parse_machine_build_profile_rejects_malformed_envelope(envelope: ProfileEnvelope) -> None:
    with pytest.raises(DrawingMachineError, match="profile") as raised:
        parse_machine_build_profile(envelope)

    assert raised.value.payload.code == "MACHINE_BUILD_PROFILE_INVALID"


@pytest.mark.parametrize("section", ["planning", "gcode"])
@pytest.mark.parametrize("kind", ["missing", "unknown"])
def test_parse_machine_build_profile_requires_exact_inner_fields(section: str, kind: str) -> None:
    values = dict(PLANNING_VALUES if section == "planning" else GCODE_VALUES)
    if kind == "missing":
        values.pop(next(iter(values)))
    else:
        values["surprise"] = 1
    envelope = profile_envelope(profile_changes={section: values})

    with pytest.raises(DrawingMachineError, match=f"profile.{section} has {kind} fields"):
        parse_machine_build_profile(envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hardware_canvas_width_mm", 0),
        ("hardware_canvas_height_mm", -1),
        ("machine_width_mm", True),
        ("machine_height_mm", "192"),
        ("paper_center_x", float("nan")),
        ("paper_center_y", float("inf")),
        ("pen_up_z", 10**10000),
        ("pen_down_z", -float("inf")),
        ("feed_travel", 0),
        ("feed_draw", -1),
        ("feed_pen_down", False),
        ("feed_pen_up", float("nan")),
        ("max_feed", float("inf")),
        ("work_coordinate", "G55"),
        ("align_mode", "anchor"),
        ("mirror_y", False),
        ("mirror_y", 1),
        ("safe_start", False),
        ("safe_start", 1),
        ("path_mode", "preview"),
    ],
    ids=[
        "canvas-width-zero",
        "canvas-height-negative",
        "machine-width-bool",
        "machine-height-string",
        "center-x-nan",
        "center-y-inf",
        "pen-up-overflow",
        "pen-down-inf",
        "travel-zero",
        "draw-negative",
        "pen-down-bool",
        "pen-up-nan",
        "max-feed-inf",
        "work-coordinate",
        "align-mode",
        "mirror-false",
        "mirror-non-bool",
        "safe-start-false",
        "safe-start-non-bool",
        "path-mode",
    ],
)
def test_parse_machine_build_profile_rejects_unsafe_or_invalid_gcode_values(field: str, value: JsonValue) -> None:
    with pytest.raises(DrawingMachineError) as raised:
        parse_machine_build_profile(profile_envelope(gcode_changes={field: value}))

    assert raised.value.payload.code == "MACHINE_BUILD_PROFILE_INVALID"
    assert raised.value.payload.details["field"] == f"profile.gcode.{field}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canvas_width_mm", True),
        ("canvas_height_mm", float("nan")),
        ("pen_width_mm", float("inf")),
        ("hatch_spacing_mm", 10**10000),
    ],
    ids=["bool-as-number", "nan", "infinity", "overflow"],
)
def test_parse_machine_build_profile_reuses_strict_b7_planning_validation(field: str, value: JsonValue) -> None:
    with pytest.raises(DrawingMachineError) as raised:
        parse_machine_build_profile(profile_envelope(planning_changes={field: value}))

    assert raised.value.payload.code == "MACHINE_BUILD_PROFILE_INVALID"
    assert raised.value.payload.details["field"] == f"profile.planning.{field}"


def test_parse_machine_build_profile_requires_all_feeds_at_or_below_maximum() -> None:
    with pytest.raises(DrawingMachineError, match="must not exceed"):
        parse_machine_build_profile(profile_envelope(gcode_changes={"feed_draw": 1200.1}))


@pytest.mark.parametrize(
    "changes",
    [
        {"pen_up_z": 0.0, "pen_down_z": 0.0},
        {"hardware_canvas_width_mm": 193.0},
        {"hardware_canvas_height_mm": 193.0},
        {"paper_center_x": -0.1},
        {"paper_center_y": 192.1},
    ],
)
def test_parse_machine_build_profile_rejects_unsafe_numeric_relationships(changes: dict[str, JsonValue]) -> None:
    with pytest.raises(DrawingMachineError):
        parse_machine_build_profile(profile_envelope(gcode_changes=changes))


def test_gcode_build_result_is_frozen_and_does_not_alias_mutable_plan(golden_root: Path) -> None:
    plan = read_json_object(golden_root / "expected/filled_region.path_plan.json")
    result = build_gcode(plan, MachineBuildProfile.stage1_defaults())
    selected = result.selected_plan
    original = copy.deepcopy(selected)
    plan["canvas"] = {"width_mm": 1.0}

    assert result.selected_plan == original
    assert isinstance(result, GcodeBuildResult)
    with pytest.raises(FrozenInstanceError):
        result.gcode = ""


def test_gcode_build_result_json_inputs_and_projections_are_deeply_detached() -> None:
    selected_plan: JsonObject = {"nested": {"value": 1}}
    metrics: JsonObject = {"nested": {"value": 2}}
    result = GcodeBuildResult("G21\n", selected_plan, "report", metrics, ())

    cast(dict[str, JsonValue], selected_plan["nested"])["value"] = 10
    cast(dict[str, JsonValue], metrics["nested"])["value"] = 20
    returned_plan = result.selected_plan
    returned_metrics = result.metrics
    cast(dict[str, JsonValue], returned_plan["nested"])["value"] = 100
    cast(dict[str, JsonValue], returned_metrics["nested"])["value"] = 200

    assert result.selected_plan == {"nested": {"value": 1}}
    assert result.metrics == {"nested": {"value": 2}}


def test_preview_result_report_input_and_projection_are_deeply_detached() -> None:
    report: JsonObject = {"metrics": {"bounds": {"min_x": 1.0}}}
    result = PreviewResult("<svg />", report)

    input_metrics = cast(dict[str, JsonValue], report["metrics"])
    cast(dict[str, JsonValue], input_metrics["bounds"])["min_x"] = 10.0
    returned_report = result.report
    returned_metrics = cast(dict[str, JsonValue], returned_report["metrics"])
    cast(dict[str, JsonValue], returned_metrics["bounds"])["min_x"] = 100.0

    assert result.report == {"metrics": {"bounds": {"min_x": 1.0}}}


def test_build_rejects_empty_production_plan() -> None:
    plan: JsonObject = {
        "input": "empty.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0},
        "fill_paths": [],
        "stroke_paths": [],
        "fill_boundary_paths": [],
    }

    with pytest.raises(DrawingMachineError, match="no production paths") as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_BLOCKED"


@pytest.mark.parametrize(
    "changes",
    [
        {"feed_draw": 1200.1},
        {"safe_start": False},
        {"work_coordinate": "G55"},
        {"paper_center_x": float("nan")},
    ],
)
def test_build_revalidates_directly_constructed_machine_profile(changes: dict[str, object], golden_root: Path) -> None:
    plan = read_json_object(golden_root / "expected/outline.path_plan.json")
    profile = replace(MachineBuildProfile.stage1_defaults(), **changes)  # type: ignore[arg-type]

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, profile)

    assert raised.value.payload.code == "MACHINE_BUILD_PROFILE_INVALID"


@pytest.mark.parametrize("group", ["fill_paths", "stroke_paths", "fill_boundary_paths"])
def test_build_rejects_preview_centerline_records_in_every_production_group(group: str) -> None:
    plan: JsonObject = {
        "input": "unsafe.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0},
        "fill_paths": [],
        "stroke_paths": [],
        "fill_boundary_paths": [],
    }
    cast(list[JsonValue], plan[group]).append(
        {"id": "preview_0001", "source": "preview_centerline", "points_mm": [[1.0, 1.0], [2.0, 2.0]]}
    )

    with pytest.raises(DrawingMachineError, match="preview centerline"):
        build_gcode(plan, MachineBuildProfile.stage1_defaults())


def test_build_rejects_actual_b8_preview_record_copied_into_stroke_paths(golden_root: Path) -> None:
    plan = read_json_object(golden_root / "expected/filled_region.path_plan.json")
    preview_record = copy.deepcopy(cast(list[JsonValue], plan["centerline_base_paths"])[0])
    plan["fill_paths"] = []
    plan["stroke_paths"] = [preview_record]
    plan["fill_boundary_paths"] = []

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_BLOCKED"


@pytest.mark.parametrize(
    ("group", "record"),
    [
        ("fill_paths", {"id": "stroke_0001", "source": "hatch", "points_mm": [[1.0, 1.0], [2.0, 2.0]]}),
        ("fill_paths", {"id": "fill_0001", "source": "centerline", "points_mm": [[1.0, 1.0], [2.0, 2.0]]}),
        ("stroke_paths", {"id": "fill_0001", "source": "centerline", "points_mm": [[1.0, 1.0], [2.0, 2.0]]}),
        ("stroke_paths", {"id": "stroke_0001", "source": "hatch", "points_mm": [[1.0, 1.0], [2.0, 2.0]]}),
        (
            "fill_boundary_paths",
            {"id": "fill_0001", "source": "fill_boundary", "points_mm": [[1.0, 1.0], [2.0, 2.0]]},
        ),
        (
            "fill_boundary_paths",
            {"id": "fill_boundary_0001", "source": "hatch", "points_mm": [[1.0, 1.0], [2.0, 2.0]]},
        ),
    ],
    ids=[
        "fill-id",
        "fill-source",
        "stroke-id",
        "stroke-source",
        "boundary-id",
        "boundary-source",
    ],
)
def test_build_requires_context_specific_production_source_and_id_schema(group: str, record: JsonObject) -> None:
    plan: JsonObject = {
        "input": "unsafe.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0},
        "fill_paths": [],
        "stroke_paths": [],
        "fill_boundary_paths": [],
    }
    cast(list[JsonValue], plan[group]).append(record)

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_BLOCKED"


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"id": "bad", "source": "stroke", "points_mm": "not-points"},
        {"id": "bad", "source": "stroke", "points_mm": [[0.0, 0.0]]},
        {"id": "bad", "source": "stroke", "points_mm": [[0.0, 0.0], [True, 1.0]]},
        {"id": "bad", "source": "stroke", "points_mm": [[0.0, 0.0], [float("inf"), 1.0]]},
        {"id": "bad", "source": "stroke", "points_mm": [[0.0, 0.0], [10**10000, 1.0]]},
        {"id": "bad", "source": 1, "points_mm": [[0.0, 0.0], [1.0, 1.0]]},
    ],
)
def test_build_rejects_malformed_external_path_records(record: JsonValue) -> None:
    plan: JsonObject = {
        "input": "bad.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0},
        "fill_paths": [],
        "stroke_paths": [record],
        "fill_boundary_paths": [],
    }

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_INVALID"


def production_plan(record: JsonObject) -> JsonObject:
    return {
        "input": "production.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0},
        "fill_paths": [],
        "stroke_paths": [record],
        "fill_boundary_paths": [],
    }


def test_build_blocks_identical_point_production_geometry_even_with_spoofed_positive_metadata() -> None:
    plan = production_plan(
        {
            "id": "stroke_0001",
            "source": "centerline",
            "points_mm": [[10.0, 10.0], [10.0, 10.0]],
            "length_mm": 1.0,
        }
    )

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_BLOCKED"


def test_build_blocks_distinct_input_points_that_collapse_after_gcode_quantization() -> None:
    plan = production_plan(
        {
            "id": "stroke_0001",
            "source": "centerline",
            "points_mm": [[0.0, 0.0], [0.0004, 0.0]],
            "length_mm": 0.0004,
        }
    )

    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_BLOCKED"


@pytest.mark.parametrize(
    "record",
    [
        {"id": "stroke_0001", "source": "centerline", "points_mm": [[0.0, 0.0], [1.0, 0.0]]},
        {
            "id": "stroke_0001",
            "source": "centerline",
            "points_mm": [[0.0, 0.0], [1.0, 0.0]],
            "length_mm": 0.0,
        },
        {
            "id": "stroke_0001",
            "source": "centerline",
            "points_mm": [[0.0, 0.0], [1.0, 0.0]],
            "length_mm": float("inf"),
        },
        {
            "id": "stroke_0001",
            "source": "centerline",
            "points_mm": [[0.0, 0.0], [1.0, 0.0]],
            "length_mm": 2.0,
        },
    ],
    ids=["missing", "zero", "nonfinite", "inconsistent"],
)
def test_build_requires_positive_finite_consistent_b8_length_metadata(record: JsonObject) -> None:
    with pytest.raises(DrawingMachineError) as raised:
        build_gcode(production_plan(record), MachineBuildProfile.stage1_defaults())

    assert raised.value.payload.code == "GCODE_BUILD_INVALID"
