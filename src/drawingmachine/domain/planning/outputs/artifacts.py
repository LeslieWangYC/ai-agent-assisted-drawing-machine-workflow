from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from typing import cast

from PIL import Image

from drawingmachine.domain.planning.models import Pixel
from drawingmachine.domain.planning.outputs.reports import render_report
from drawingmachine.domain.planning.outputs.svg import (
    render_centerline_only_svg,
    render_layers_svg,
    render_stroke_only_svg,
)
from drawingmachine.domain.planning.service import PlanningResult
from drawingmachine.json_types import JsonObject

_ARTIFACT_PATHS = {
    "path_plan": "path_plan.json",
    "binary_png": "binary.png",
    "skeleton_debug_png": "skeleton_debug.png",
    "preview_stroke_only_svg": "preview_stroke_only.svg",
    "preview_centerline_only_svg": "preview_centerline_only.svg",
    "preview_layers_svg": "preview_layers.svg",
    "preview_final_svg": "preview_final.svg",
    "report_md": "report.md",
}


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    role: str
    relative_path: str
    media_type: str
    content: bytes


def render_planning_artifacts(result: PlanningResult) -> tuple[GeneratedArtifact, ...]:
    plan = deepcopy(result.plan)
    plan["artifacts"] = dict(_ARTIFACT_PATHS)
    width, height = _source_size(plan)
    artifacts = (
        GeneratedArtifact(
            "path_plan",
            _ARTIFACT_PATHS["path_plan"],
            "application/json",
            json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"),
        ),
        GeneratedArtifact(
            "binary_png",
            _ARTIFACT_PATHS["binary_png"],
            "image/png",
            _render_mask_png(result.foreground, width, height),
        ),
        GeneratedArtifact(
            "skeleton_debug_png",
            _ARTIFACT_PATHS["skeleton_debug_png"],
            "image/png",
            _render_mask_png(result.skeleton, width, height),
        ),
        GeneratedArtifact(
            "preview_stroke_only_svg",
            _ARTIFACT_PATHS["preview_stroke_only_svg"],
            "image/svg+xml",
            render_stroke_only_svg(plan).encode("utf-8"),
        ),
        GeneratedArtifact(
            "preview_centerline_only_svg",
            _ARTIFACT_PATHS["preview_centerline_only_svg"],
            "image/svg+xml",
            render_centerline_only_svg(plan).encode("utf-8"),
        ),
        GeneratedArtifact(
            "preview_layers_svg",
            _ARTIFACT_PATHS["preview_layers_svg"],
            "image/svg+xml",
            render_layers_svg(plan, colored=True).encode("utf-8"),
        ),
        GeneratedArtifact(
            "preview_final_svg",
            _ARTIFACT_PATHS["preview_final_svg"],
            "image/svg+xml",
            render_layers_svg(plan, colored=False).encode("utf-8"),
        ),
        GeneratedArtifact(
            "report_md",
            _ARTIFACT_PATHS["report_md"],
            "text/markdown; charset=utf-8",
            render_report(plan, _ARTIFACT_PATHS).encode("utf-8"),
        ),
    )
    return artifacts


def _source_size(plan: JsonObject) -> tuple[int, int]:
    metrics = plan.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("planning result metrics must be an object")
    source_size = metrics.get("source_size_px")
    if (
        not isinstance(source_size, list)
        or len(source_size) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in source_size)
    ):
        raise ValueError("planning result source_size_px must contain two integers")
    width, height = cast(list[int], source_size)
    if width <= 0 or height <= 0:
        raise ValueError("planning result source_size_px must be positive")
    return width, height


def _render_mask_png(mask: frozenset[Pixel], width: int, height: int) -> bytes:
    data = bytearray([255]) * (width * height)
    for pixel_x, pixel_y in mask:
        if 0 <= pixel_x < width and 0 <= pixel_y < height:
            data[pixel_y * width + pixel_x] = 0
    image = Image.frombytes("L", (width, height), bytes(data))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


__all__ = ["GeneratedArtifact", "render_planning_artifacts"]
