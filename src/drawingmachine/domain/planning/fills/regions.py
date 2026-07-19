from __future__ import annotations

from typing import cast

from drawingmachine.domain.planning.imaging import connected_components
from drawingmachine.domain.planning.models import BinarizedImage, PathPlanConfig, Pixel
from drawingmachine.json_types import JsonObject


def fill_candidate_pixels(
    foreground: frozenset[Pixel],
    width: int,
    height: int,
    mm_per_px: float,
    min_run_mm: float,
    min_thickness_mm: float,
) -> frozenset[Pixel]:
    min_run_px = max(2, round(min_run_mm / mm_per_px))
    min_thickness_px = max(2, round(min_thickness_mm / mm_per_px))
    candidates: set[Pixel] = set()
    for y_position in range(height):
        x_position = 0
        while x_position < width:
            while x_position < width and (x_position, y_position) not in foreground:
                x_position += 1
            start = x_position
            while x_position < width and (x_position, y_position) in foreground:
                x_position += 1
            end = x_position - 1
            run = end - start + 1
            if run >= min_run_px and _run_has_fill_thickness(
                foreground,
                height,
                start,
                end,
                y_position,
                min_thickness_px,
            ):
                for candidate_x in range(start, end + 1):
                    candidates.add((candidate_x, y_position))
    return frozenset(candidates)


def component_stats(
    foreground: frozenset[Pixel],
    skeleton: frozenset[Pixel],
    width: int,
    height: int,
    mm_per_px: float,
    config: PathPlanConfig,
) -> list[JsonObject]:
    regions: list[JsonObject] = []
    image = BinarizedImage(foreground, width, height, 0, False)
    for label_id, component in enumerate(connected_components(image), start=1):
        skeleton_length_px = len(skeleton & component.pixels)
        area_px = component.area_px
        area_mm2 = area_px * mm_per_px * mm_per_px
        average_width_mm = (area_px / max(1, skeleton_length_px)) * mm_per_px
        classification = "fill_candidate" if average_width_mm >= config.pen_width_mm * 1.7 else "stroke_candidate"
        if area_px < config.min_component_area_px:
            classification = "noise"
        regions.append(
            cast(
                JsonObject,
                {
                    "id": f"component_{label_id:04d}",
                    "classification": classification,
                    "area_px": area_px,
                    "area_mm2": round(area_mm2, 4),
                    "bbox_px": [component.min_x, component.min_y, component.max_x, component.max_y],
                    "bbox_mm": [
                        round(component.width_px * mm_per_px, 3),
                        round(component.height_px * mm_per_px, 3),
                    ],
                    "skeleton_pixels": skeleton_length_px,
                    "estimated_avg_width_mm": round(average_width_mm, 3),
                },
            )
        )
    return regions


def hatch_ids_for_region(
    fill_paths: list[JsonObject],
    min_x_px: int,
    min_y_px: int,
    max_x_px: int,
    max_y_px: int,
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
) -> list[str]:
    min_x = offset_x_mm + min_x_px * scale
    max_x = offset_x_mm + max_x_px * scale
    min_y = offset_y_mm + min_y_px * scale
    max_y = offset_y_mm + max_y_px * scale
    path_ids: list[str] = []
    for path in fill_paths:
        raw_points = path.get("points_mm", [])
        if not isinstance(raw_points, list) or not raw_points:
            continue
        points = cast(list[list[float]], raw_points)
        center_x = sum(point[0] for point in points) / len(points)
        center_y = sum(point[1] for point in points) / len(points)
        path_id = path.get("id")
        if min_x <= center_x <= max_x and min_y <= center_y <= max_y and isinstance(path_id, str):
            path_ids.append(path_id)
    return path_ids


def _run_has_fill_thickness(
    foreground: frozenset[Pixel],
    height: int,
    start: int,
    end: int,
    y_position: int,
    min_thickness_px: int,
) -> bool:
    samples = [start, (start + end) // 2, end]
    for x_position in samples:
        top = y_position
        while top > 0 and (x_position, top - 1) in foreground:
            top -= 1
        bottom = y_position
        while bottom < height - 1 and (x_position, bottom + 1) in foreground:
            bottom += 1
        if bottom - top + 1 >= min_thickness_px:
            return True
    return False


__all__ = ["component_stats", "fill_candidate_pixels", "hatch_ids_for_region"]
