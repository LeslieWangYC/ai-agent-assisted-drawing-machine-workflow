from __future__ import annotations

import math
from typing import cast

from drawingmachine.domain.planning.geometry import polyline_length
from drawingmachine.domain.planning.models import PathPlanConfig, Pixel, Point
from drawingmachine.json_types import JsonObject

PixelPath = tuple[Pixel, ...]


def dedupe_overlapping_short_paths(
    paths: tuple[PixelPath, ...],
    scale: float,
    config: PathPlanConfig,
) -> tuple[tuple[PixelPath, ...], JsonObject]:
    short_limit_px = config.dedupe_short_path_length_mm / max(scale, 0.000001)
    distance_px = config.dedupe_distance_mm / max(scale, 0.000001)
    if short_limit_px <= 0 or distance_px < 0 or config.dedupe_overlap_ratio <= 0:
        return paths, cast(JsonObject, {"deduped_short_count": 0, "deduped_short_length_mm": 0.0})

    lengths_px = [polyline_length(path) for path in paths]
    long_indices = [index for index, length in enumerate(lengths_px) if length >= short_limit_px]
    removed_indices: set[int] = set()
    removed_length_px = 0.0

    for index, path in enumerate(paths):
        length_px = lengths_px[index]
        if length_px <= 0 or length_px >= short_limit_px:
            continue
        if _is_redundant_short_path(index, path, paths, lengths_px, long_indices, distance_px, config):
            removed_indices.add(index)
            removed_length_px += length_px

    kept = tuple(path for index, path in enumerate(paths) if index not in removed_indices)
    return kept, cast(
        JsonObject,
        {
            "deduped_short_count": len(removed_indices),
            "deduped_short_length_mm": round(removed_length_px * scale, 3),
        },
    )


def normalize_direction(delta_x: float, delta_y: float) -> Point:
    length = math.hypot(delta_x, delta_y)
    if length == 0:
        return 0.0, 0.0
    return delta_x / length, delta_y / length


def angle_between(first: Point, second: Point) -> float:
    first_length = math.hypot(first[0], first[1])
    second_length = math.hypot(second[0], second[1])
    if first_length == 0 or second_length == 0:
        return 180.0
    dot = max(-1.0, min(1.0, first[0] * second[0] + first[1] * second[1]))
    return math.degrees(math.acos(dot))


def _is_redundant_short_path(
    index: int,
    path: PixelPath,
    paths: tuple[PixelPath, ...],
    lengths_px: list[float],
    long_indices: list[int],
    distance_px: float,
    config: PathPlanConfig,
) -> bool:
    direction = _path_direction(path)
    for other_index in long_indices:
        if other_index == index:
            continue
        other = paths[other_index]
        if lengths_px[other_index] <= lengths_px[index]:
            continue
        angle = _undirected_angle_between(direction, _path_direction(other))
        if angle > config.dedupe_angle_deg:
            continue
        overlap_ratio = _path_overlap_ratio(path, other, distance_px)
        if overlap_ratio >= config.dedupe_overlap_ratio:
            return True
    return False


def _undirected_angle_between(first: Point, second: Point) -> float:
    angle = angle_between(first, second)
    return min(angle, 180.0 - angle)


def _path_direction(path: PixelPath) -> Point:
    if len(path) < 2:
        return 0.0, 0.0
    start = path[0]
    end = path[-1]
    if start == end and len(path) > 2:
        end = path[len(path) // 2]
    return normalize_direction(end[0] - start[0], end[1] - start[1])


def _path_overlap_ratio(short_path: PixelPath, long_path: PixelPath, max_distance_px: float) -> float:
    samples = _sample_path_points(short_path, spacing_px=max(0.5, max_distance_px / 2.0))
    if not samples:
        return 0.0
    covered = 0
    for point in samples:
        if _min_distance_to_polyline(point, long_path) <= max_distance_px:
            covered += 1
    return covered / len(samples)


def _sample_path_points(path: PixelPath, spacing_px: float) -> tuple[Point, ...]:
    if len(path) < 2:
        return ()
    samples: list[Point] = []
    for index in range(1, len(path)):
        start = path[index - 1]
        end = path[index]
        length = math.dist(start, end)
        steps = max(1, math.ceil(length / max(spacing_px, 0.000001)))
        for step in range(steps):
            fraction = step / steps
            samples.append(
                (
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            )
    samples.append((float(path[-1][0]), float(path[-1][1])))
    return tuple(samples)


def _min_distance_to_polyline(point: Point, path: PixelPath) -> float:
    if len(path) < 2:
        return float("inf")
    return min(_point_segment_distance(point, path[index - 1], path[index]) for index in range(1, len(path)))


def _point_segment_distance(point: Point, start: Pixel, end: Pixel) -> float:
    point_x, point_y = point
    start_x, start_y = start
    end_x, end_y = end
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0:
        return math.hypot(point_x - start_x, point_y - start_y)
    fraction = max(
        0.0,
        min(1.0, ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y) / length_squared),
    )
    projected_x = start_x + fraction * delta_x
    projected_y = start_y + fraction * delta_y
    return math.hypot(point_x - projected_x, point_y - projected_y)


__all__ = ["angle_between", "dedupe_overlapping_short_paths", "normalize_direction"]
