from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

import drawingmachine.domain.gcode.build as build_module
from drawingmachine.domain.gcode.build import (
    _build_checks,
    _object,
    _path_bounds,
    _points,
    _select_production_paths,
    build_gcode,
)
from drawingmachine.domain.gcode.models import CheckSeverity, MachineBuildProfile
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject


def _record(points: list[list[float]]) -> JsonObject:
    return {
        "id": "stroke_0001",
        "source": "centerline",
        "points_mm": points,
        "length_mm": round(sum(abs(points[index][0] - points[index - 1][0]) for index in range(1, len(points))), 4),
    }


def _plan(points: list[list[float]] | None = None, *, canvas: object = None) -> JsonObject:
    points = points or [[0.0, 0.0], [1.0, 0.0]]
    return {
        "input": "input.png",
        "canvas": {"width_mm": 120.0, "height_mm": 120.0} if canvas is None else cast(JsonObject, canvas),
        "fill_paths": [],
        "stroke_paths": [_record(points)],
        "fill_boundary_paths": [],
    }


def test_build_rejects_non_stroke_mode_after_explicit_validation_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_module, "validate_machine_build_profile", lambda profile: None)
    profile = replace(MachineBuildProfile.stage1_defaults(), path_mode="preview")

    with pytest.raises(DrawingMachineError) as error:
        build_gcode(_plan(), profile)

    assert error.value.payload.code == "GCODE_BUILD_BLOCKED"


def test_path_selection_rejects_nonarray_group_and_nonstring_mapping_key() -> None:
    plan = _plan()
    plan["fill_paths"] = {}
    with pytest.raises(DrawingMachineError):
        _select_production_paths(plan)
    with pytest.raises(DrawingMachineError):
        _object({1: "bad"}, "path")

    missing_id = _plan()
    cast(JsonObject, cast(list[object], missing_id["stroke_paths"])[0])["id"] = ""
    with pytest.raises(DrawingMachineError):
        _select_production_paths(missing_id)


def test_point_parser_rejects_non_pair_entries() -> None:
    with pytest.raises(DrawingMachineError):
        _points([[0.0, 0.0], [1.0]], "points")


def test_out_of_machine_bounds_produces_blocker_and_blocks_build() -> None:
    plan = _plan(
        [[0.0, 0.0], [300.0, 0.0]],
        canvas={"width_mm": 400.0, "height_mm": 120.0},
    )

    with pytest.raises(DrawingMachineError) as error:
        build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert error.value.payload.code == "GCODE_BUILD_BLOCKED"
    assert error.value.payload.details["checks"][0]["code"] == "machine_coordinate_out_of_bounds"  # type: ignore[index]


def test_build_checks_rejects_nonobject_canvas() -> None:
    with pytest.raises(DrawingMachineError):
        _build_checks(
            _plan(canvas=[]),
            [{"id": "stroke_0001", "points_mm": [[0.0, 0.0], [1.0, 0.0]]}],
            MachineBuildProfile.stage1_defaults(),
        )


def test_build_checks_report_canvas_span_and_very_short_paths() -> None:
    paths: list[JsonObject] = [
        {"id": "stroke_0001", "points_mm": [[0.0, 0.0], [10.0, 0.0]]},
        {"id": "stroke_0002", "points_mm": [[1.0, 1.0], [1.1, 1.0]]},
    ]
    checks = _build_checks(
        _plan(canvas={"width_mm": 1.0, "height_mm": 1.0}),
        paths,
        MachineBuildProfile.stage1_defaults(),
    )

    assert {check.code for check in checks if check.severity is not CheckSeverity.BLOCKER} >= {
        "canvas_span",
        "very_short_paths",
    }


def test_empty_path_bounds_have_zero_extent() -> None:
    assert _path_bounds([]) == {
        "min_x": 0,
        "min_y": 0,
        "max_x": 0,
        "max_y": 0,
        "span_x": 0,
        "span_y": 0,
    }
