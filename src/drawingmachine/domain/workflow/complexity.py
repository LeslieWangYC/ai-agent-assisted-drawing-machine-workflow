from __future__ import annotations

from dataclasses import replace

from PIL import Image, ImageOps

from drawingmachine.domain.workflow.models import ComplexityLimits, ComplexityResult, DedupeProfile
from drawingmachine.domain.workflow.normalization import (
    Pixel,
    binarize,
    connected_components,
    remove_small_components,
    zhang_suen_thinning,
)

_SINGLE_DEDUPE_PROFILE = DedupeProfile(3.0, 0.45, 35.0, 0.75)


def analyze_complexity(image: Image.Image, limits: ComplexityLimits) -> ComplexityResult:
    gray = ImageOps.grayscale(image.convert("RGB"))
    foreground, width, height, threshold, inverted = binarize(gray, limits.threshold, limits.invert)
    foreground = remove_small_components(foreground, width, height, limits.min_component_area_px)
    components = connected_components(foreground, width, height)
    skeleton = zhang_suen_thinning(foreground, width, height)
    metrics = build_metrics(foreground, skeleton, components, width, height)
    decision = evaluate(metrics, limits)
    report: dict[str, object] = {
        "schema": "processed_image_complexity_report_v1",
        "created_at": limits.created_at,
        "input": limits.input_path,
        "size_px": [width, height],
        "binarization": {
            "method": "otsu" if limits.threshold is None else "fixed",
            "threshold": threshold,
            "inverted": inverted,
            "min_component_area_px": limits.min_component_area_px,
        },
        "metrics": metrics,
        "limits": {"soft": limits.soft(), "hard": limits.hard()},
        "decision": decision,
        "hardware_touched": False,
    }
    return ComplexityResult.from_document(report)


def apply_single_dedupe_profile(base: DedupeProfile, result: ComplexityResult) -> DedupeProfile:
    if result.status != "PASS_WITH_SINGLE_DEDUPE":
        return base
    return replace(
        base,
        dedupe_short_path_length_mm=_SINGLE_DEDUPE_PROFILE.dedupe_short_path_length_mm,
        dedupe_distance_mm=_SINGLE_DEDUPE_PROFILE.dedupe_distance_mm,
        dedupe_angle_deg=_SINGLE_DEDUPE_PROFILE.dedupe_angle_deg,
        dedupe_overlap_ratio=_SINGLE_DEDUPE_PROFILE.dedupe_overlap_ratio,
    )


def build_metrics(
    foreground: set[Pixel],
    skeleton: set[Pixel],
    components: list[tuple[set[Pixel], dict[str, int]]],
    width: int,
    height: int,
) -> dict[str, object]:
    total_pixels = max(1, width * height)
    component_areas = sorted((len(pixels) for pixels, _ in components), reverse=True)
    bbox = content_bbox(foreground)
    boundary_pixels = count_boundary_pixels(foreground, width, height)
    horizontal_transitions, vertical_transitions = count_transitions(foreground, width, height)
    return {
        "foreground_pixels": len(foreground),
        "foreground_ratio": round(len(foreground) / total_pixels, 8),
        "component_count": len(components),
        "largest_component_pixels": component_areas[0] if component_areas else 0,
        "top5_component_pixels": component_areas[:5],
        "skeleton_pixels": len(skeleton),
        "skeleton_ratio": round(len(skeleton) / total_pixels, 8),
        "boundary_pixels": boundary_pixels,
        "boundary_ratio": round(boundary_pixels / total_pixels, 8),
        "horizontal_transitions": horizontal_transitions,
        "vertical_transitions": vertical_transitions,
        "transitions_total": horizontal_transitions + vertical_transitions,
        "transitions_per_megapixel": round(
            (horizontal_transitions + vertical_transitions) / total_pixels * 1_000_000,
            3,
        ),
        "content_bbox_px": bbox,
        "content_bbox_fill_ratio": round(content_bbox_fill_ratio(foreground, bbox), 8),
    }


def evaluate(metrics: dict[str, object], limits: ComplexityLimits) -> dict[str, object]:
    soft_hits: list[dict[str, object]] = []
    hard_hits: list[dict[str, object]] = []
    soft_limits = limits.soft()
    hard_limits = limits.hard()
    for name, soft_limit in soft_limits.items():
        raw_value = metrics[name]
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise TypeError(f"complexity metric {name} is not numeric")
        value = int(raw_value)
        if value > soft_limit:
            soft_hits.append({"metric": name, "value": value, "limit": soft_limit})
        hard_limit = hard_limits[name]
        if value > hard_limit:
            hard_hits.append({"metric": name, "value": value, "limit": hard_limit})
    if hard_hits:
        return {
            "status": "REJECT_COMPLEXITY",
            "reason": "processed image complexity exceeds current planner hard limits",
            "soft_limit_hits": soft_hits,
            "hard_limit_hits": hard_hits,
            "next_allowed_stage": "repeat_image_edit",
            "dedupe_profile": None,
        }
    if soft_hits:
        return {
            "status": "PASS_WITH_SINGLE_DEDUPE",
            "reason": "processed image is above default complexity limits; apply one stronger dedupe profile before planning",
            "soft_limit_hits": soft_hits,
            "hard_limit_hits": [],
            "next_allowed_stage": "build_drawable_job",
            "dedupe_profile": {
                "dedupe_short_path_length_mm": _SINGLE_DEDUPE_PROFILE.dedupe_short_path_length_mm,
                "dedupe_distance_mm": _SINGLE_DEDUPE_PROFILE.dedupe_distance_mm,
                "dedupe_angle_deg": _SINGLE_DEDUPE_PROFILE.dedupe_angle_deg,
                "dedupe_overlap_ratio": _SINGLE_DEDUPE_PROFILE.dedupe_overlap_ratio,
            },
        }
    return {
        "status": "PASS",
        "reason": "processed image complexity is within default planner limits",
        "soft_limit_hits": [],
        "hard_limit_hits": [],
        "next_allowed_stage": "build_drawable_job",
        "dedupe_profile": None,
    }


def count_boundary_pixels(foreground: set[Pixel], width: int, height: int) -> int:
    count = 0
    for x, y in foreground:
        if (
            x == 0
            or y == 0
            or x == width - 1
            or y == height - 1
            or (x - 1, y) not in foreground
            or (x + 1, y) not in foreground
            or (x, y - 1) not in foreground
            or (x, y + 1) not in foreground
        ):
            count += 1
    return count


def count_transitions(foreground: set[Pixel], width: int, height: int) -> tuple[int, int]:
    horizontal = 0
    for y in range(height):
        previous = False
        for x in range(width):
            current = (x, y) in foreground
            if x and current != previous:
                horizontal += 1
            previous = current
    vertical = 0
    for x in range(width):
        previous = False
        for y in range(height):
            current = (x, y) in foreground
            if y and current != previous:
                vertical += 1
            previous = current
    return horizontal, vertical


def content_bbox(foreground: set[Pixel]) -> list[int] | None:
    if not foreground:
        return None
    x_values = [pixel[0] for pixel in foreground]
    y_values = [pixel[1] for pixel in foreground]
    return [min(x_values), min(y_values), max(x_values), max(y_values)]


def content_bbox_fill_ratio(foreground: set[Pixel], bbox: list[int] | None) -> float:
    if not bbox:
        return 0.0
    width = bbox[2] - bbox[0] + 1
    height = bbox[3] - bbox[1] + 1
    return len(foreground) / max(1, width * height)


__all__ = ["analyze_complexity", "apply_single_dedupe_profile"]
