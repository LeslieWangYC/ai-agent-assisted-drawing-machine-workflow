from __future__ import annotations

import math

from drawingmachine.domain.planning.models import Point


def euclidean_distance(start: Point, end: Point) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def point_line_distance(point: Point, start: Point, end: Point) -> float:
    point_x, point_y = point
    start_x, start_y = start
    end_x, end_y = end
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    if delta_x == 0 and delta_y == 0:
        return math.hypot(point_x - start_x, point_y - start_y)
    return abs(delta_y * point_x - delta_x * point_y + end_x * start_y - end_y * start_x) / math.hypot(
        delta_x,
        delta_y,
    )


def polyline_length(points: tuple[Point, ...]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(
        math.hypot(points[index][0] - points[index - 1][0], points[index][1] - points[index - 1][1])
        for index in range(1, len(points))
    )


def estimate_nearest_neighbor_travel(paths: tuple[tuple[Point, ...], ...]) -> float:
    remaining = [list(path) for path in paths if len(path) >= 2]
    current = [0.0, 0.0]
    travel = 0.0
    while remaining:
        best_index = 0
        best_reverse = False
        best_distance = float("inf")
        for index, path in enumerate(remaining):
            start_distance = math.dist(current, path[0])
            end_distance = math.dist(current, path[-1])
            if start_distance < best_distance:
                best_distance = start_distance
                best_index = index
                best_reverse = False
            if end_distance < best_distance:
                best_distance = end_distance
                best_index = index
                best_reverse = True
        path = remaining.pop(best_index)
        if best_reverse:
            path = list(reversed(path))
        travel += best_distance
        current = list(path[-1])
    return travel


__all__ = ["estimate_nearest_neighbor_travel", "euclidean_distance", "point_line_distance", "polyline_length"]
