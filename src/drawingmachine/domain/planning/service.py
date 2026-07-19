from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image, ImageOps

from drawingmachine.domain.planning.fills import (
    build_fill_records,
    build_fill_regions,
    component_stats,
    fill_candidate_pixels,
    serpentine_fill_paths,
)
from drawingmachine.domain.planning.geometry import drawing_transform, estimate_nearest_neighbor_travel, polyline_length
from drawingmachine.domain.planning.imaging import binarize, remove_small_components, thin
from drawingmachine.domain.planning.models import BinarizedImage, PathPlanConfig, Pixel, Point
from drawingmachine.domain.planning.strokes import (
    build_centerline_base_records,
    build_stroke_records,
    continuous_skeleton_base_paths,
    postprocess_stroke_paths,
    trace_skeleton_paths,
)
from drawingmachine.json_types import JsonObject


@dataclass(frozen=True, slots=True)
class PlanningResult:
    plan: JsonObject
    foreground: frozenset[Pixel]
    skeleton: frozenset[Pixel]
    production_paths: tuple[JsonObject, ...]


def build_path_plan(
    image: Image.Image,
    *,
    source_name: str,
    config: PathPlanConfig,
) -> PlanningResult:
    gray = ImageOps.grayscale(image.convert("RGB"))
    detected = binarize(gray, config.threshold, config.invert)
    filtered = remove_small_components(detected, min_area=config.min_component_area_px)
    foreground = filtered.foreground
    width = filtered.width
    height = filtered.height

    scale, draw_width_mm, draw_height_mm, offset_x_mm, offset_y_mm = drawing_transform(
        width,
        height,
        config.canvas_width_mm,
        config.canvas_height_mm,
    )

    fill_candidates = fill_candidate_pixels(
        foreground,
        width,
        height,
        scale,
        config.hatch_min_run_mm,
        config.fill_min_thickness_mm,
    )
    stroke_foreground = foreground - fill_candidates
    stroke_image = BinarizedImage(stroke_foreground, width, height, filtered.threshold, filtered.inverted)
    thinned = thin(stroke_image)
    skeleton = thinned.foreground

    raw_stroke_paths_px = trace_skeleton_paths(skeleton, width, height)
    processed_stroke_paths_px, stroke_postprocess = postprocess_stroke_paths(raw_stroke_paths_px, scale, config)
    stroke_paths = build_stroke_records(processed_stroke_paths_px, scale, offset_x_mm, offset_y_mm, config)
    preview_centerline_paths = build_centerline_base_records(
        continuous_skeleton_base_paths(skeleton, width, height),
        scale,
        offset_x_mm,
        offset_y_mm,
        config,
    )
    centerline_base_paths = deepcopy(preview_centerline_paths)
    fill_paths = build_fill_records(
        serpentine_fill_paths(
            foreground,
            width,
            height,
            scale,
            config.hatch_spacing_mm,
            config.hatch_min_run_mm,
            config.fill_min_thickness_mm,
        ),
        scale,
        offset_x_mm,
        offset_y_mm,
        config,
    )
    components = component_stats(foreground, skeleton, width, height, scale, config)
    fill_regions, fill_boundary_paths = build_fill_regions(
        foreground,
        width,
        height,
        scale,
        offset_x_mm,
        offset_y_mm,
        config,
        fill_paths,
    )
    production_paths = tuple(deepcopy(path) for path in fill_paths + stroke_paths + fill_boundary_paths)

    total_stroke_length = sum(polyline_length(_record_points(path)) for path in stroke_paths)
    total_base_length = sum(polyline_length(_record_points(path)) for path in preview_centerline_paths)
    total_fill_length = sum(polyline_length(_record_points(path)) for path in fill_paths)
    total_boundary_length = sum(polyline_length(_record_points(path)) for path in fill_boundary_paths)
    travel_estimate = estimate_nearest_neighbor_travel(tuple(_record_points(path) for path in production_paths))

    plan = cast(
        JsonObject,
        {
            "stage": "drawable_path_plan_mvp",
            "input": str(Path(source_name)),
            "method": {
                "binarization": "otsu" if config.threshold is None else "fixed",
                "threshold": detected.threshold,
                "inverted": detected.inverted,
                "centerline": "zhang_suen_thinning",
                "stroke_postprocess": {
                    "drop_short_stroke_mm": config.drop_short_stroke_mm,
                    "merge_endpoint_distance_mm": config.merge_endpoint_distance_mm,
                    "merge_angle_deg": config.merge_angle_deg,
                    "dedupe_short_path_length_mm": config.dedupe_short_path_length_mm,
                    "dedupe_distance_mm": config.dedupe_distance_mm,
                    "dedupe_angle_deg": config.dedupe_angle_deg,
                    "dedupe_overlap_ratio": config.dedupe_overlap_ratio,
                },
                "fill": "serpentine_boustrophedon_from_black_runs",
                "routing": "nearest_neighbor_estimate_only",
            },
            "canvas": {
                "width_mm": round(config.canvas_width_mm, 4),
                "height_mm": round(config.canvas_height_mm, 4),
                "draw_width_mm": round(draw_width_mm, 4),
                "draw_height_mm": round(draw_height_mm, 4),
                "offset_x_mm": round(offset_x_mm, 4),
                "offset_y_mm": round(offset_y_mm, 4),
                "mm_per_px": round(scale, 6),
                "pen_width_mm": round(config.pen_width_mm, 4),
                "min_gap_mm": round(config.min_gap_mm, 4),
            },
            "stroke_paths": stroke_paths,
            "preview_centerline_paths": preview_centerline_paths,
            "centerline_base_paths": centerline_base_paths,
            "fill_paths": fill_paths,
            "fill_boundary_paths": fill_boundary_paths,
            "fill_regions": fill_regions,
            "regions": components,
            "metrics": {
                "source_size_px": [width, height],
                "foreground_pixels": len(foreground),
                "skeleton_pixels": len(skeleton),
                "stroke_path_count": len(stroke_paths),
                "preview_centerline_path_count": len(preview_centerline_paths),
                "centerline_base_path_count": len(preview_centerline_paths),
                "raw_stroke_path_count": len(raw_stroke_paths_px),
                "dropped_short_stroke_count": stroke_postprocess["dropped_short_count"],
                "merged_stroke_pair_count": stroke_postprocess["merged_pair_count"],
                "deduped_short_stroke_count": stroke_postprocess["deduped_short_count"],
                "deduped_short_stroke_length_mm": stroke_postprocess["deduped_short_length_mm"],
                "fill_path_count": len(fill_paths),
                "fill_boundary_path_count": len(fill_boundary_paths),
                "fill_region_count": len(fill_regions),
                "preview_centerline_length_mm": round(total_base_length, 3),
                "centerline_base_length_mm": round(total_base_length, 3),
                "stroke_length_mm": round(total_stroke_length, 3),
                "fill_length_mm": round(total_fill_length, 3),
                "fill_boundary_length_mm": round(total_boundary_length, 3),
                "preview_draw_length_mm": round(total_base_length + total_fill_length + total_boundary_length, 3),
                "draw_length_mm": round(total_stroke_length + total_fill_length + total_boundary_length, 3),
                "estimated_pen_up_travel_mm": round(travel_estimate, 3),
            },
            "gcode_policy": {
                "status": "not_generated_by_path_plan_script",
                "pen_lift": "z_axis",
                "default_pen_up_z": 5.0,
                "default_pen_down_z": 0.0,
                "source_for_gcode": "path_plan.json",
            },
            "checks": _path_plan_checks(
                foreground,
                skeleton,
                width,
                height,
                stroke_paths,
                fill_paths,
                components,
            ),
        },
    )
    return PlanningResult(plan, foreground, skeleton, production_paths)


def _record_points(record: JsonObject) -> tuple[Point, ...]:
    raw_points = record.get("points_mm")
    if not isinstance(raw_points, list):
        return ()
    points = cast(list[list[float]], raw_points)
    return tuple((point[0], point[1]) for point in points)


def _path_plan_checks(
    foreground: frozenset[Pixel],
    skeleton: frozenset[Pixel],
    width: int,
    height: int,
    stroke_paths: list[JsonObject],
    fill_paths: list[JsonObject],
    components: list[JsonObject],
) -> list[JsonObject]:
    checks: list[JsonObject] = []
    if not foreground:
        checks.append(
            cast(
                JsonObject,
                {"severity": "BLOCKER", "code": "empty_binary", "message": "No foreground pixels were detected."},
            )
        )
    if not skeleton:
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "BLOCKER",
                    "code": "empty_skeleton",
                    "message": "No centerline skeleton was generated.",
                },
            )
        )
    if len(stroke_paths) > 2500:
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "WARNING",
                    "code": "many_stroke_paths",
                    "message": "Centerline tracing produced many paths; simplify or reduce detail.",
                },
            )
        )
    if len(fill_paths) > 1200:
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "WARNING",
                    "code": "many_fill_paths",
                    "message": "Hatch fill produced many paths; increase hatch spacing or reduce filled areas.",
                },
            )
        )
    fill_candidates = [region for region in components if region.get("classification") == "fill_candidate"]
    if fill_candidates and not fill_paths:
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "WARNING",
                    "code": "fill_without_hatch",
                    "message": "Thick regions were detected but no hatch paths were generated.",
                },
            )
        )
    if len(foreground) / max(1, width * height) > 0.20:
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "WARNING",
                    "code": "dense_foreground",
                    "message": "Foreground coverage is high for pen plotting; consider a simpler processed image.",
                },
            )
        )
    checks.append(
        cast(
            JsonObject,
            {
                "severity": "INFO",
                "code": "z_axis_pen_lift",
                "message": "Future G-code generation should use Z-axis pen up/down commands.",
            },
        )
    )
    if not any(check.get("severity") in {"BLOCKER", "WARNING"} for check in checks):
        checks.append(
            cast(
                JsonObject,
                {
                    "severity": "INFO",
                    "code": "path_plan_generated",
                    "message": "Path plan generated for visual review.",
                },
            )
        )
    return checks


__all__ = ["PlanningResult", "build_path_plan"]
