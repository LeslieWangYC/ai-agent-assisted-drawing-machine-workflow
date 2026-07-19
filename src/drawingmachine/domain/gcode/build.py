from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import NoReturn, cast

from drawingmachine.domain.gcode.models import (
    CheckSeverity,
    GcodeBuildResult,
    MachineBuildProfile,
    SafetyCheck,
    validate_machine_build_profile,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_PATH_MODE_DEFINITION = "fill_paths + stroke_paths + fill_boundary_paths"
_GROUPS = (
    ("fill", "fill_paths", "hatch", re.compile(r"fill_[0-9]{4,}\Z")),
    ("stroke", "stroke_paths", "centerline", re.compile(r"stroke_[0-9]{4,}\Z")),
    ("boundary", "fill_boundary_paths", "fill_boundary", re.compile(r"fill_boundary_[0-9]{4,}\Z")),
)


def build_gcode(path_plan: JsonObject, profile: MachineBuildProfile) -> GcodeBuildResult:
    validate_machine_build_profile(profile)
    if profile.path_mode != "stroke":
        _blocked("Only stroke path mode is eligible for machine G-code.", {"path_mode": profile.path_mode})
    selected = _select_production_paths(path_plan)
    if not selected:
        _blocked("Path plan has no production paths.", {"path_mode": profile.path_mode})
    ordered = _ordered_paths(selected)
    transformed = _transform_paths(ordered, profile)
    _validate_output_geometry(transformed)
    metrics = _path_stats(path_plan, selected, transformed, profile)
    checks = _build_checks(path_plan, transformed, profile)
    if any(check.severity is CheckSeverity.BLOCKER for check in checks):
        _blocked("G-code build safety checks blocked the candidate.", {"checks": [check.to_json() for check in checks]})
    gcode = _render_gcode(path_plan, transformed, profile)
    selected_plan = _selected_plan(path_plan, selected, transformed, metrics, checks)
    report = _render_report(path_plan, transformed, metrics, checks)
    return GcodeBuildResult(gcode, deepcopy(selected_plan), report, deepcopy(metrics), checks)


def _select_production_paths(plan: JsonObject) -> list[JsonObject]:
    selected: list[JsonObject] = []
    for group, field, expected_source, identifier_pattern in _GROUPS:
        raw_items = plan.get(field)
        if not isinstance(raw_items, list):
            _invalid(f"path_plan.{field} must be an array", {"field": f"path_plan.{field}"})
        for index, raw_item in enumerate(raw_items):
            item = _object(raw_item, f"path_plan.{field}[{index}]")
            source = item.get("source")
            if not isinstance(source, str) or not source:
                _invalid(
                    "production path source must be a non-empty string", {"field": f"path_plan.{field}[{index}].source"}
                )
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                _invalid("production path id must be a non-empty string", {"field": f"path_plan.{field}[{index}].id"})
            points = _points(item.get("points_mm"), f"path_plan.{field}[{index}].points_mm")
            if source != expected_source or identifier_pattern.fullmatch(identifier) is None:
                _blocked(
                    "A preview centerline or non-production path record cannot become a machine candidate.",
                    {"field": field, "source": source, "id": identifier},
                )
            points_field = f"path_plan.{field}[{index}].points_mm"
            recomputed_length = _polyline_length([list(point) for point in points])
            if not math.isfinite(recomputed_length) or recomputed_length <= 0.0:
                _blocked(
                    "A production path must contain positive finite geometry.",
                    {"field": points_field, "id": identifier},
                )
            length_field = f"path_plan.{field}[{index}].length_mm"
            metadata_length = _plan_number(item.get("length_mm"), length_field)
            if metadata_length != round(recomputed_length, 4):
                _invalid(
                    "production path length_mm must match points_mm at four-decimal precision",
                    {"field": length_field},
                )
            selected.append(
                {"id": identifier, "group": group, "source": source, "points_mm": [list(point) for point in points]}
            )
    return selected


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{field} must be an object", {"field": field})
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _invalid(f"{field} contains a non-string key", {"field": field})
        result[key] = item
    return result


def _points(value: object, field: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or len(value) < 2:
        _invalid(f"{field} must contain at least two points", {"field": field})
    points: list[tuple[float, float]] = []
    for index, raw_point in enumerate(value):
        if not isinstance(raw_point, list) or len(raw_point) != 2:
            _invalid(f"{field}[{index}] must be a two-number point", {"field": f"{field}[{index}]"})
        coordinates: list[float] = []
        for coordinate in raw_point:
            if isinstance(coordinate, bool) or not isinstance(coordinate, int | float):
                _invalid(f"{field}[{index}] coordinates must be numbers", {"field": f"{field}[{index}]"})
            try:
                number = float(coordinate)
            except OverflowError:
                _invalid(f"{field}[{index}] coordinates must be finite", {"field": f"{field}[{index}]"})
            if not math.isfinite(number):
                _invalid(f"{field}[{index}] coordinates must be finite", {"field": f"{field}[{index}]"})
            coordinates.append(number)
        points.append((coordinates[0], coordinates[1]))
    return tuple(points)


def _ordered_paths(paths: list[JsonObject]) -> list[JsonObject]:
    remaining = deepcopy(paths)
    current = [0.0, 0.0]
    ordered: list[JsonObject] = []
    while remaining:
        best_index = 0
        best_reverse = False
        best_distance = float("inf")
        for index, path in enumerate(remaining):
            points = cast(list[list[float]], path["points_mm"])
            start_distance = math.dist(current, points[0])
            end_distance = math.dist(current, points[-1])
            if start_distance < best_distance:
                best_distance = start_distance
                best_index = index
                best_reverse = False
            if end_distance < best_distance:
                best_distance = end_distance
                best_index = index
                best_reverse = True
        path = remaining.pop(best_index)
        points = cast(list[list[float]], path["points_mm"])
        if best_reverse:
            points = list(reversed(points))
            path["points_mm"] = cast(JsonValue, points)
        ordered.append(path)
        current = points[-1]
    return ordered


def _transform_paths(paths: list[JsonObject], profile: MachineBuildProfile) -> list[JsonObject]:
    bounds = _path_bounds(paths)
    source_center_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
    source_center_y = (bounds["min_y"] + bounds["max_y"]) / 2.0
    transformed: list[JsonObject] = []
    for path in paths:
        new_path = deepcopy(path)
        new_points: list[JsonValue] = []
        for x, y in cast(list[list[float]], path["points_mm"]):
            dx = x - source_center_x
            dy = y - source_center_y
            tx = profile.paper_center_x + dx
            ty = profile.paper_center_y - dy
            new_points.append([tx, ty])
        new_path["points_mm"] = new_points
        transformed.append(new_path)
    return transformed


def _validate_output_geometry(paths: list[JsonObject]) -> None:
    for path in paths:
        points = cast(list[list[float]], path["points_mm"])
        output_points = [[float(f"{x:.3f}"), float(f"{y:.3f}")] for x, y in points]
        output_length = _polyline_length(output_points)
        if not math.isfinite(output_length) or output_length <= 0.0:
            _blocked(
                "A production path has no positive finite geometry after G-code coordinate quantization.",
                {"id": cast(str, path["id"])},
            )


def _path_stats(
    plan: JsonObject,
    selected: list[JsonObject],
    ordered: list[JsonObject],
    profile: MachineBuildProfile,
) -> JsonObject:
    draw_length = sum(_polyline_length(cast(list[list[float]], path["points_mm"])) for path in ordered)
    travel_length = _pen_up_travel_length(ordered)
    draw_time_s = draw_length / max(profile.feed_draw, 1.0) * 60.0
    travel_time_s = travel_length / max(profile.feed_travel, 1.0) * 60.0
    canvas = plan.get("canvas")
    canvas_values = canvas if isinstance(canvas, dict) else {}
    return {
        "path_count": len(ordered),
        "selected_path_count_before_ordering": len(selected),
        "point_count": sum(len(cast(list[JsonValue], path["points_mm"])) for path in ordered),
        "draw_length_mm": round(draw_length, 3),
        "pen_up_travel_mm": round(travel_length, 3),
        "estimated_draw_time_s": round(draw_time_s, 2),
        "estimated_travel_time_s": round(travel_time_s, 2),
        "rough_estimated_total_time_s": round(draw_time_s + travel_time_s + len(ordered) * 2.0, 2),
        "pen_lift_count": len(ordered),
        "canvas_width_mm": canvas_values.get("width_mm"),
        "canvas_height_mm": canvas_values.get("height_mm"),
        "bounds": cast(JsonObject, _path_bounds(ordered)),
        "work_coordinate": profile.work_coordinate,
        "xy_airrun": False,
        "coordinate_transform": {
            "align_mode": profile.align_mode,
            "mirror_x": False,
            "mirror_y": profile.mirror_y,
            "anchor_x": 0.0,
            "anchor_y": 0.0,
            "paper_center_x": profile.paper_center_x,
            "paper_center_y": profile.paper_center_y,
        },
    }


def _build_checks(
    plan: JsonObject,
    ordered: list[JsonObject],
    profile: MachineBuildProfile,
) -> tuple[SafetyCheck, ...]:
    checks: list[SafetyCheck] = []
    for path in ordered:
        for x, y in cast(list[list[float]], path["points_mm"]):
            if (
                x < -0.001
                or y < -0.001
                or x > profile.machine_width_mm + 0.001
                or y > profile.machine_height_mm + 0.001
            ):
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER, "machine_coordinate_out_of_bounds", "A path exceeds machine bounds."
                    )
                )
                break
    canvas = plan.get("canvas")
    if not isinstance(canvas, dict):
        _invalid("path_plan.canvas must be an object", {"field": "path_plan.canvas"})
    width = _plan_number(canvas.get("width_mm"), "path_plan.canvas.width_mm")
    height = _plan_number(canvas.get("height_mm"), "path_plan.canvas.height_mm")
    bounds = _path_bounds(ordered)
    if bounds["span_x"] > width + 0.001 or bounds["span_y"] > height + 0.001:
        checks.append(
            SafetyCheck(
                CheckSeverity.WARNING,
                "canvas_span",
                f"Transformed drawing span {bounds['span_x']:.3f} x {bounds['span_y']:.3f} mm exceeds plan canvas {width:.3f} x {height:.3f} mm.",
            )
        )
    short_count = sum(_polyline_length(cast(list[list[float]], path["points_mm"])) < 0.2 for path in ordered)
    if short_count:
        checks.append(
            SafetyCheck(CheckSeverity.INFO, "very_short_paths", f"{short_count} paths are shorter than 0.2 mm.")
        )
    checks.extend(
        (
            SafetyCheck(CheckSeverity.INFO, "z_axis_pen_lift", "G-code uses Z-axis pen up/down commands."),
            SafetyCheck(
                CheckSeverity.INFO,
                "static_only",
                "This is a static generation result; it is not machine-run validation.",
            ),
        )
    )
    return tuple(checks)


def _render_gcode(plan: JsonObject, ordered: list[JsonObject], profile: MachineBuildProfile) -> str:
    lines = [
        "; Generated from drawable path_plan.json",
        "; Static candidate only. Review before sending to any CNC controller.",
        f"; Source: {plan.get('input', 'unknown')}",
        f"; Path mode: stroke ({_PATH_MODE_DEFINITION})",
        f"; Coordinate transform: align_mode=center, mirror_x=False, mirror_y=True, anchor_x=0.0, anchor_y=0.0, paper_center_x={profile.paper_center_x}, paper_center_y={profile.paper_center_y}",
        f"; Pen lift: Z-axis, up={profile.pen_up_z}, down={profile.pen_down_z}",
        "G21 ; millimeters",
        "G90 ; absolute coordinates",
        f"{profile.work_coordinate} ; work coordinate",
        f"G1 Z{profile.pen_up_z:.3f} F{profile.feed_pen_up:.1f}",
    ]
    for path in ordered:
        points = cast(list[list[float]], path["points_mm"])
        start = points[0]
        lines.extend(
            (
                f"; path {path['id']} group={path['group']} source={path['source']}",
                f"G0 X{start[0]:.3f} Y{start[1]:.3f} F{profile.feed_travel:.1f}",
                f"G1 Z{profile.pen_down_z:.3f} F{profile.feed_pen_down:.1f}",
            )
        )
        lines.extend(f"G1 X{x:.3f} Y{y:.3f} F{profile.feed_draw:.1f}" for x, y in points[1:])
        lines.append(f"G1 Z{profile.pen_up_z:.3f} F{profile.feed_pen_up:.1f}")
    lines.append("; End")
    return "\n".join(lines) + "\n"


def _selected_plan(
    plan: JsonObject,
    selected: list[JsonObject],
    ordered: list[JsonObject],
    metrics: JsonObject,
    checks: tuple[SafetyCheck, ...],
) -> JsonObject:
    canvas = plan.get("canvas")
    return {
        "stage": "gcode_selected_path_plan_v1",
        "source_path_plan": plan.get("input", "unknown"),
        "path_mode": "stroke",
        "path_mode_definition": _PATH_MODE_DEFINITION,
        "canvas": deepcopy(canvas) if isinstance(canvas, dict) else {},
        "selected_paths_before_ordering": cast(list[JsonValue], deepcopy(selected)),
        "ordered_paths": cast(list[JsonValue], deepcopy(ordered)),
        "metrics": deepcopy(metrics),
        "checks": [check.to_json() for check in checks],
        "execution": {"allow_run": False, "reason": "static_candidate_requires_review"},
    }


def _render_report(
    plan: JsonObject,
    ordered: list[JsonObject],
    stats: JsonObject,
    checks: tuple[SafetyCheck, ...],
) -> str:
    lines = [
        "# G-code Candidate Report",
        "",
        "## Mode",
        "",
        "- Path mode: `stroke`",
        f"- Definition: `{_PATH_MODE_DEFINITION}`",
        f"- Source image: `{plan.get('input', 'unknown')}`",
        "- G-code: `drawing.gcode`",
        "",
        "## Metrics",
        "",
        f"- Path count: `{len(ordered)}`",
        f"- Point count: `{stats['point_count']}`",
        f"- Draw length: `{stats['draw_length_mm']} mm`",
        f"- Pen-up travel: `{stats['pen_up_travel_mm']} mm`",
        f"- Pen lifts: `{stats['pen_lift_count']}`",
        f"- Rough estimated total time: `{stats['rough_estimated_total_time_s']} s`",
        f"- Bounds: `{stats['bounds']}`",
        f"- Work coordinate: `{stats['work_coordinate']}`",
        "- XY air-run: `False`",
        f"- Coordinate transform: `{stats['coordinate_transform']}`",
        "",
        "## Checks",
        "",
    ]
    lines.extend(f"- `{check.severity.value}` `{check.code}`: {check.message}" for check in checks)
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This report is static analysis only.",
            "- Do not send the generated file to a controller without separate machine validation.",
            "- This file includes pen-down Z moves and should be treated as a pen-contact drawing candidate.",
            "- `preview` mode may retrace parts of the skeleton because it prioritizes a visually continuous centerline base.",
            "- `stroke` mode uses fragmented centerline candidates intended for later path optimization.",
        ]
    )
    return "\n".join(lines) + "\n"


def _polyline_length(points: list[list[float]]) -> float:
    return sum(math.dist(points[index - 1], points[index]) for index in range(1, len(points)))


def _pen_up_travel_length(paths: list[JsonObject]) -> float:
    current = [0.0, 0.0]
    total = 0.0
    for path in paths:
        points = cast(list[list[float]], path["points_mm"])
        total += math.dist(current, points[0])
        current = points[-1]
    return total


def _path_bounds(paths: list[JsonObject]) -> dict[str, float]:
    points = [point for path in paths for point in cast(list[list[float]], path["points_mm"])]
    if not points:
        return {"min_x": 0, "min_y": 0, "max_x": 0, "max_y": 0, "span_x": 0, "span_y": 0}
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
    return {
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "span_x": max_x - min_x,
        "span_y": max_y - min_y,
    }


def _plan_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _invalid(f"{field} must be a finite positive number", {"field": field})
    try:
        result = float(value)
    except OverflowError:
        _invalid(f"{field} must be a finite positive number", {"field": field})
    if not math.isfinite(result) or result <= 0.0:
        _invalid(f"{field} must be a finite positive number", {"field": field})
    return result


def _invalid(message: str, details: Mapping[str, JsonValue]) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("GCODE_BUILD_INVALID", ErrorCategory.INPUT, message, False, details))


def _blocked(message: str, details: Mapping[str, JsonValue]) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("GCODE_BUILD_BLOCKED", ErrorCategory.VALIDATION, message, False, details))


__all__ = ["build_gcode"]
