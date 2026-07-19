from __future__ import annotations

from drawingmachine.domain.planning.geometry.distance import point_line_distance
from drawingmachine.domain.planning.models import Point


def simplify_polyline(points: tuple[Point, ...], *, tolerance_mm: float) -> tuple[Point, ...]:
    if len(points) <= 2:
        return points
    first = points[0]
    last = points[-1]
    max_distance = -1.0
    index = 0
    for candidate_index, point in enumerate(points[1:-1], start=1):
        distance = point_line_distance(point, first, last)
        if distance > max_distance:
            max_distance = distance
            index = candidate_index
    if max_distance > tolerance_mm:
        left = simplify_polyline(points[: index + 1], tolerance_mm=tolerance_mm)
        right = simplify_polyline(points[index:], tolerance_mm=tolerance_mm)
        return left[:-1] + right
    return (first, last)


__all__ = ["simplify_polyline"]
