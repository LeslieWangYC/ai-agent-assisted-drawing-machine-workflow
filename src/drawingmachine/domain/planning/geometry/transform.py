from __future__ import annotations

from drawingmachine.domain.planning.models import Pixel, Point


def drawing_transform(
    image_width_px: int,
    image_height_px: int,
    canvas_width_mm: float,
    canvas_height_mm: float,
) -> tuple[float, float, float, float, float]:
    scale = min(canvas_width_mm / max(1, image_width_px), canvas_height_mm / max(1, image_height_px))
    draw_width_mm = image_width_px * scale
    draw_height_mm = image_height_px * scale
    offset_x_mm = (canvas_width_mm - draw_width_mm) / 2.0
    offset_y_mm = (canvas_height_mm - draw_height_mm) / 2.0
    return scale, draw_width_mm, draw_height_mm, offset_x_mm, offset_y_mm


def points_px_to_mm(
    path: tuple[Pixel, ...],
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
) -> tuple[Point, ...]:
    return tuple((round(offset_x_mm + x * scale, 4), round(offset_y_mm + y * scale, 4)) for x, y in path)


__all__ = ["drawing_transform", "points_px_to_mm"]
