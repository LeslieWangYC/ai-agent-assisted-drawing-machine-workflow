from __future__ import annotations

from typing import cast

from drawingmachine.json_types import JsonObject


def render_layers_svg(plan: JsonObject, *, colored: bool) -> str:
    canvas = _object(plan, "canvas")
    width = _number(canvas, "width_mm")
    height = _number(canvas, "height_mm")
    pen_width = _number(canvas, "pen_width_mm")
    stroke_color = "#0057ff" if colored else "#000000"
    fill_color = "#d62828" if colored else "#000000"
    boundary_color = "#f77f00" if colored else "#000000"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" viewBox="0 0 {width:.3f} {height:.3f}">',
        '<rect x="0" y="0" width="100%" height="100%" fill="white"/>',
        f'<g id="fill_hatch" fill="none" stroke="{fill_color}" stroke-width="{pen_width:.3f}" stroke-linecap="round" stroke-linejoin="round">',
    ]
    lines.extend(_svg_path(_points(item)) for item in _records(plan, "fill_paths"))
    lines.extend(
        [
            "</g>",
            f'<g id="centerline_strokes" fill="none" stroke="{stroke_color}" stroke-width="{pen_width:.3f}" stroke-linecap="round" stroke-linejoin="round">',
        ]
    )
    lines.extend(_svg_path(_points(item)) for item in _centerline_records(plan))
    lines.append("</g>")
    lines.append(
        f'<g id="fill_boundaries" fill="none" stroke="{boundary_color}" stroke-width="{pen_width:.3f}" stroke-linecap="round" stroke-linejoin="round">'
    )
    lines.extend(_svg_path(_points(item)) for item in _optional_records(plan, "fill_boundary_paths"))
    lines.append("</g>")
    if colored:
        lines.append('<text x="2" y="5" font-size="3" fill="#0057ff">blue=centerline</text>')
        lines.append('<text x="2" y="9" font-size="3" fill="#d62828">red=hatch fill</text>')
        lines.append('<text x="2" y="13" font-size="3" fill="#f77f00">orange=fill boundary</text>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_centerline_only_svg(plan: JsonObject) -> str:
    canvas = _object(plan, "canvas")
    width = _number(canvas, "width_mm")
    height = _number(canvas, "height_mm")
    pen_width = _number(canvas, "pen_width_mm")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" viewBox="0 0 {width:.3f} {height:.3f}">',
        '<rect x="0" y="0" width="100%" height="100%" fill="white"/>',
        f'<g id="preview_centerline" fill="none" stroke="#000000" stroke-width="{pen_width:.3f}" stroke-linecap="round" stroke-linejoin="round">',
    ]
    lines.extend(_svg_path(_points(item)) for item in _centerline_records(plan))
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def render_stroke_only_svg(plan: JsonObject) -> str:
    return render_stroke_records_svg(plan, _records(plan, "stroke_paths"), group_id="stroke_paths")


def render_stroke_records_svg(plan: JsonObject, records: list[JsonObject], *, group_id: str) -> str:
    canvas = _object(plan, "canvas")
    width = _number(canvas, "width_mm")
    height = _number(canvas, "height_mm")
    pen_width = _number(canvas, "pen_width_mm")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" viewBox="0 0 {width:.3f} {height:.3f}">',
        '<rect x="0" y="0" width="100%" height="100%" fill="white"/>',
        f'<g id="{group_id}" fill="none" stroke="#000000" stroke-width="{pen_width:.3f}" stroke-linecap="round" stroke-linejoin="round">',
    ]
    lines.extend(_svg_path(_points(item)) for item in records)
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def _svg_path(points: list[list[float]]) -> str:
    if len(points) < 2:
        return ""
    head = f"M{points[0][0]:.4f},{points[0][1]:.4f}"
    tail = " ".join(f"L{point[0]:.4f},{point[1]:.4f}" for point in points[1:])
    return f'<path d="{head} {tail}" />'


def _object(value: JsonObject, key: str) -> JsonObject:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"planning result {key} must be an object")
    return item


def _number(value: JsonObject, key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise ValueError(f"planning result {key} must be a number")
    return float(item)


def _records(plan: JsonObject, key: str) -> list[JsonObject]:
    value = plan.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"planning result {key} must be a list of objects")
    return cast(list[JsonObject], value)


def _optional_records(plan: JsonObject, key: str) -> list[JsonObject]:
    return [] if key not in plan else _records(plan, key)


def _centerline_records(plan: JsonObject) -> list[JsonObject]:
    for key in ("preview_centerline_paths", "centerline_base_paths", "stroke_paths"):
        records = _records(plan, key)
        if records:
            return records
    return []


def _points(record: JsonObject) -> list[list[float]]:
    value = record.get("points_mm")
    if not isinstance(value, list) or not all(
        isinstance(point, list)
        and len(point) == 2
        and all(isinstance(coordinate, int | float) and not isinstance(coordinate, bool) for coordinate in point)
        for point in value
    ):
        raise ValueError("planning path points_mm must contain numeric coordinate pairs")
    return cast(list[list[float]], value)


__all__ = [
    "render_centerline_only_svg",
    "render_layers_svg",
    "render_stroke_only_svg",
    "render_stroke_records_svg",
]
