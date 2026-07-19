from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.service import build_path_plan
from drawingmachine.json_types import JsonObject

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "fixtures/package_b/golden"
PATH_GROUPS = ("fill_paths", "stroke_paths", "fill_boundary_paths")
PATH_BEARING_GROUPS = (*PATH_GROUPS, "preview_centerline_paths", "centerline_base_paths")


def read_json_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def path_records(plan: JsonObject, group: str) -> list[JsonObject]:
    records = plan[group]
    assert isinstance(records, list)
    assert all(isinstance(record, dict) for record in records)
    return cast(list[JsonObject], records)


def record_points(record: JsonObject) -> list[list[float]]:
    points = record["points_mm"]
    assert isinstance(points, list)
    assert all(isinstance(point, list) for point in points)
    return cast(list[list[float]], points)


def assert_path_geometry_close(
    actual: JsonObject,
    expected: JsonObject,
    *,
    coordinate_abs_tol: float,
    length_abs_tol: float,
) -> None:
    for group in PATH_BEARING_GROUPS:
        actual_records = path_records(actual, group)
        expected_records = path_records(expected, group)
        assert [record["id"] for record in actual_records] == [record["id"] for record in expected_records]
        for actual_record, expected_record in zip(actual_records, expected_records, strict=True):
            actual_length = actual_record["length_mm"]
            expected_length = expected_record["length_mm"]
            assert isinstance(actual_length, int | float) and not isinstance(actual_length, bool)
            assert isinstance(expected_length, int | float) and not isinstance(expected_length, bool)
            assert actual_length == pytest.approx(expected_length, abs=length_abs_tol)
            actual_points = actual_record["points_mm"]
            expected_points = expected_record["points_mm"]
            assert isinstance(actual_points, list)
            assert isinstance(expected_points, list)
            assert len(actual_points) == len(expected_points)
            for actual_point, expected_point in zip(actual_points, expected_points, strict=True):
                assert isinstance(actual_point, list)
                assert isinstance(expected_point, list)
                assert actual_point == pytest.approx(expected_point, abs=coordinate_abs_tol)


@pytest.mark.parametrize("group", ["preview_centerline_paths", "centerline_base_paths"])
def test_geometry_assertion_covers_nonproduction_path_groups(group: str) -> None:
    expected = read_json_object(GOLDEN_ROOT / "expected/branch_loop.path_plan.json")
    actual = copy.deepcopy(expected)
    record_points(path_records(actual, group)[0])[0][0] += 1.0

    with pytest.raises(AssertionError):
        assert_path_geometry_close(
            actual,
            expected,
            coordinate_abs_tol=1e-4,
            length_abs_tol=1e-3,
        )


def aggregate_lengths(plan: JsonObject) -> tuple[float, ...]:
    totals: list[float] = []
    for group in PATH_BEARING_GROUPS:
        lengths = [record["length_mm"] for record in path_records(plan, group)]
        assert all(isinstance(length, int | float) and not isinstance(length, bool) for length in lengths)
        totals.append(sum(cast(list[float], lengths)))
    return tuple(totals)


def structure_without_path_geometry(plan: JsonObject) -> JsonObject:
    result = copy.deepcopy(plan)
    result.pop("artifacts", None)
    for group in PATH_BEARING_GROUPS:
        for record in path_records(result, group):
            record.pop("points_mm", None)
            record.pop("length_mm", None)
    return result


@pytest.mark.parametrize(
    "fixture_name",
    ["outline", "filled_region", "branch_loop", "dedupe_overlap", "complex_gradient"],
)
def test_path_plan_matches_stage1_geometry(fixture_name: str) -> None:
    expected = read_json_object(GOLDEN_ROOT / f"expected/{fixture_name}.path_plan.json")
    source_name = f"{fixture_name}.png"
    expected["input"] = source_name
    expected.pop("artifacts", None)
    with Image.open(GOLDEN_ROOT / f"{fixture_name}.png") as image:
        actual_result = build_path_plan(image, source_name=source_name, config=PathPlanConfig())
    actual = actual_result.plan

    assert actual["input"] == source_name
    assert structure_without_path_geometry(actual) == structure_without_path_geometry(expected)
    assert_path_geometry_close(
        actual,
        expected,
        coordinate_abs_tol=1e-4,
        length_abs_tol=1e-3,
    )
    assert aggregate_lengths(actual) == pytest.approx(aggregate_lengths(expected), abs=1e-3)
    expected_production = tuple(record for group in PATH_GROUPS for record in path_records(actual, group))
    assert actual_result.production_paths == expected_production
    assert all(
        record not in actual_result.production_paths for record in path_records(actual, "preview_centerline_paths")
    )


def test_production_paths_use_fill_stroke_boundary_concatenation_order() -> None:
    expected = read_json_object(GOLDEN_ROOT / "expected/filled_region.path_plan.json")
    with Image.open(GOLDEN_ROOT / "filled_region.png") as image:
        result = build_path_plan(image, source_name="filled_region.png", config=PathPlanConfig())

    expected_ids = [cast(str, record["id"]) for group in PATH_GROUPS for record in path_records(expected, group)]
    production_ids = [cast(str, record["id"]) for record in result.production_paths]
    preview_ids = {cast(str, record["id"]) for record in path_records(result.plan, "preview_centerline_paths")}

    assert production_ids == expected_ids
    assert preview_ids.isdisjoint(production_ids)


def test_planning_result_path_views_are_deeply_mutation_isolated() -> None:
    with Image.open(GOLDEN_ROOT / "filled_region.png") as image:
        result = build_path_plan(image, source_name="filled_region.png", config=PathPlanConfig())

    plan_records_by_id = {
        cast(str, record["id"]): record for group in PATH_GROUPS for record in path_records(result.plan, group)
    }
    for production_record in result.production_paths:
        assert production_record is not plan_records_by_id[cast(str, production_record["id"])]

    production_record = result.production_paths[0]
    plan_record = plan_records_by_id[cast(str, production_record["id"])]
    production_points = record_points(production_record)
    plan_points = record_points(plan_record)
    assert production_points is not plan_points
    assert production_points[0] is not plan_points[0]

    original_plan_x = plan_points[0][0]
    production_points[0][0] += 1.0
    assert plan_points[0][0] == original_plan_x

    original_production_y = production_points[0][1]
    plan_points[0][1] += 1.0
    assert production_points[0][1] == original_production_y

    preview_records = path_records(result.plan, "preview_centerline_paths")
    centerline_base_records = path_records(result.plan, "centerline_base_paths")
    assert preview_records is not centerline_base_records
    assert preview_records[0] is not centerline_base_records[0]

    preview_points = record_points(preview_records[0])
    centerline_base_points = record_points(centerline_base_records[0])
    assert preview_points is not centerline_base_points
    assert preview_points[0] is not centerline_base_points[0]

    original_base_x = centerline_base_points[0][0]
    preview_points[0][0] += 1.0
    assert centerline_base_points[0][0] == original_base_x

    original_preview_y = preview_points[0][1]
    centerline_base_points[0][1] += 1.0
    assert preview_points[0][1] == original_preview_y
