from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from drawingmachine.json_types import JsonObject


def render_report(plan: JsonObject, artifacts: Mapping[str, str]) -> str:
    metrics = _object(plan, "metrics")
    canvas = _object(plan, "canvas")
    lines = [
        "# Drawable Path Plan Report",
        "",
        "## Input",
        "",
        f"- Source: `{plan['input']}`",
        f"- Canvas: `{canvas['width_mm']} x {canvas['height_mm']} mm`",
        f"- Draw area: `{canvas['draw_width_mm']} x {canvas['draw_height_mm']} mm`",
        f"- Pen width: `{canvas['pen_width_mm']} mm`",
        "- Pen lift: `Z-axis`",
        "",
        "## Paths",
        "",
        f"- Raw centerline fragments: `{metrics.get('raw_stroke_path_count', metrics['stroke_path_count'])}`",
        f"- Dropped short stroke fragments: `{metrics.get('dropped_short_stroke_count', 0)}`",
        f"- Merged stroke pairs: `{metrics.get('merged_stroke_pair_count', 0)}`",
        f"- Deduped overlapping short strokes: `{metrics.get('deduped_short_stroke_count', 0)}`",
        f"- Deduped overlapping short stroke length: `{metrics.get('deduped_short_stroke_length_mm', 0.0)} mm`",
        f"- Preview centerline paths: `{metrics.get('preview_centerline_path_count', metrics.get('centerline_base_path_count', 0))}`",
        f"- Centerline stroke paths: `{metrics['stroke_path_count']}`",
        f"- Hatch fill paths: `{metrics['fill_path_count']}`",
        f"- Fill boundary paths: `{metrics['fill_boundary_path_count']}`",
        f"- Fill regions: `{metrics['fill_region_count']}`",
        f"- Preview draw length: `{metrics.get('preview_draw_length_mm', metrics['draw_length_mm'])} mm`",
        f"- Candidate stroke path length: `{metrics['stroke_length_mm']} mm`",
        f"- Estimated pen-up travel: `{metrics['estimated_pen_up_travel_mm']} mm`",
        "",
        "## Artifacts",
        "",
        f"- Path plan: `{artifacts['path_plan']}`",
        f"- Stroke-only SVG: `{artifacts['preview_stroke_only_svg']}`",
        f"- Centerline-only SVG: `{artifacts['preview_centerline_only_svg']}`",
        f"- Layered SVG: `{artifacts['preview_layers_svg']}`",
        f"- Final SVG preview: `{artifacts['preview_final_svg']}`",
        f"- Binary PNG: `{artifacts['binary_png']}`",
        f"- Skeleton debug PNG: `{artifacts['skeleton_debug_png']}`",
        "",
        "## Checks",
        "",
    ]
    for check in _objects(plan, "checks"):
        lines.append(f"- `{check['severity']}` `{check['code']}`: {check['message']}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- SVG output is a drawing preview, not the only source of truth.",
            "- G-code should be generated from `path_plan.json` after visual review.",
            "- Thin source strokes are represented as centerline paths.",
            "- Filled black regions are approximated with internal hatch paths plus boundary outline paths.",
        ]
    )
    return "\n".join(lines) + "\n"


def _object(value: JsonObject, key: str) -> JsonObject:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"planning result {key} must be an object")
    return item


def _objects(value: JsonObject, key: str) -> list[JsonObject]:
    item = value.get(key)
    if not isinstance(item, list) or not all(isinstance(entry, dict) for entry in item):
        raise ValueError(f"planning result {key} must be a list of objects")
    return cast(list[JsonObject], item)


__all__ = ["render_report"]
