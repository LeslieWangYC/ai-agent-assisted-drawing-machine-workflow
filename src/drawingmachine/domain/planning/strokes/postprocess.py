from __future__ import annotations

import math
from typing import cast

from drawingmachine.domain.planning.geometry import points_px_to_mm, polyline_length, simplify_polyline
from drawingmachine.domain.planning.models import PathPlanConfig, Pixel, Point
from drawingmachine.domain.planning.strokes.dedupe import (
    angle_between,
    dedupe_overlapping_short_paths,
    normalize_direction,
)
from drawingmachine.json_types import JsonObject

PixelPath = tuple[Pixel, ...]


def postprocess_stroke_paths(
    paths_px: tuple[PixelPath, ...],
    scale: float,
    config: PathPlanConfig,
) -> tuple[tuple[PixelPath, ...], JsonObject]:
    min_keep_px = max(0.0, config.drop_short_stroke_mm / max(scale, 0.000001))
    paths = [path for path in paths_px if polyline_length(path) >= min_keep_px]
    dropped_short_count = len(paths_px) - len(paths)
    merged_pair_count = 0

    max_distance_px = config.merge_endpoint_distance_mm / max(scale, 0.000001)
    while True:
        merge = _find_best_merge(paths, max_distance_px, config.merge_angle_deg)
        if merge is None:
            break
        first_index, second_index, first_reverse, second_reverse = merge
        first = tuple(reversed(paths[first_index])) if first_reverse else paths[first_index]
        second = tuple(reversed(paths[second_index])) if second_reverse else paths[second_index]
        merged = _merge_pixel_paths(first, second)
        for index in sorted((first_index, second_index), reverse=True):
            paths.pop(index)
        paths.append(merged)
        merged_pair_count += 1

    deduped_paths, dedupe_stats = dedupe_overlapping_short_paths(tuple(paths), scale, config)

    return deduped_paths, cast(
        JsonObject,
        {
            "dropped_short_count": dropped_short_count,
            "merged_pair_count": merged_pair_count,
            "deduped_short_count": dedupe_stats["deduped_short_count"],
            "deduped_short_length_mm": dedupe_stats["deduped_short_length_mm"],
        },
    )


def build_stroke_records(
    paths_px: tuple[PixelPath, ...],
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
    config: PathPlanConfig,
) -> list[JsonObject]:
    records: list[JsonObject] = []
    for index, path in enumerate(paths_px, start=1):
        record = path_to_record(
            f"stroke_{index:04d}",
            "centerline",
            points_px_to_mm(path, scale, offset_x_mm, offset_y_mm),
            config.simplify_tolerance_mm,
            config.min_path_length_mm,
        )
        if record is not None:
            records.append(record)
    return records


def build_centerline_base_records(
    paths_px: tuple[PixelPath, ...],
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
    config: PathPlanConfig,
) -> list[JsonObject]:
    records: list[JsonObject] = []
    for index, path in enumerate(paths_px, start=1):
        record = path_to_record(
            f"centerline_base_{index:04d}",
            "skeleton_continuous_base",
            points_px_to_mm(path, scale, offset_x_mm, offset_y_mm),
            config.simplify_tolerance_mm,
            config.min_path_length_mm,
        )
        if record is not None:
            records.append(record)
    return records


def path_to_record(
    path_id: str,
    source: str,
    points: tuple[Point, ...],
    simplify_tolerance_mm: float,
    min_path_length_mm: float,
    extra: JsonObject | None = None,
) -> JsonObject | None:
    simplified = simplify_polyline(points, tolerance_mm=simplify_tolerance_mm) if simplify_tolerance_mm > 0 else points
    if len(simplified) < 2 or polyline_length(simplified) < min_path_length_mm:
        return None
    record = cast(
        JsonObject,
        {
            "id": path_id,
            "source": source,
            "points_mm": [[round(point_x, 4), round(point_y, 4)] for point_x, point_y in simplified],
            "length_mm": round(polyline_length(simplified), 4),
        },
    )
    if extra:
        record.update(extra)
    return record


def _find_best_merge(
    paths: list[PixelPath],
    max_distance_px: float,
    max_angle_deg: float,
) -> tuple[int, int, bool, bool] | None:
    best: tuple[float, int, int, bool, bool] | None = None
    for first_index, first in enumerate(paths):
        if len(first) < 2:
            continue
        first_options: list[tuple[bool, Pixel, Point]] = []
        for first_reverse in (False, True):
            oriented_first = tuple(reversed(first)) if first_reverse else first
            first_options.append((first_reverse, oriented_first[-1], _tail_direction(oriented_first)))
        for second_index in range(first_index + 1, len(paths)):
            second = paths[second_index]
            if len(second) < 2:
                continue
            second_options: list[tuple[bool, Pixel, Point]] = []
            for second_reverse in (False, True):
                oriented_second = tuple(reversed(second)) if second_reverse else second
                second_options.append((second_reverse, oriented_second[0], _head_direction(oriented_second)))
            for first_reverse, first_point, first_direction in first_options:
                for second_reverse, second_point, second_direction in second_options:
                    distance = math.dist(first_point, second_point)
                    if distance > max_distance_px:
                        continue
                    angle = angle_between(first_direction, second_direction)
                    if angle > max_angle_deg:
                        continue
                    score = distance + angle * 0.05
                    if best is None or score < best[0]:
                        best = (score, first_index, second_index, first_reverse, second_reverse)
    if best is None:
        return None
    _, first_index, second_index, first_reverse, second_reverse = best
    return first_index, second_index, first_reverse, second_reverse


def _head_direction(path: PixelPath) -> Point:
    if len(path) < 2:
        return 0.0, 0.0
    head = path[0]
    inner = path[min(len(path) - 1, 3)]
    return normalize_direction(inner[0] - head[0], inner[1] - head[1])


def _tail_direction(path: PixelPath) -> Point:
    if len(path) < 2:
        return 0.0, 0.0
    tail = path[-1]
    inner = path[max(0, len(path) - 4)]
    return normalize_direction(tail[0] - inner[0], tail[1] - inner[1])


def _merge_pixel_paths(first: PixelPath, second: PixelPath) -> PixelPath:
    if first[-1] == second[0]:
        return first + second[1:]
    return first + _bridge_pixels(first[-1], second[0]) + second


def _bridge_pixels(start: Pixel, end: Pixel) -> PixelPath:
    start_x, start_y = start
    end_x, end_y = end
    steps = max(abs(end_x - start_x), abs(end_y - start_y))
    if steps <= 1:
        return ()
    bridge: list[Pixel] = []
    for step in range(1, steps):
        fraction = step / steps
        bridge.append(
            (
                round(start_x + (end_x - start_x) * fraction),
                round(start_y + (end_y - start_y) * fraction),
            )
        )
    return tuple(bridge)


__all__ = ["build_centerline_base_records", "build_stroke_records", "path_to_record", "postprocess_stroke_paths"]
