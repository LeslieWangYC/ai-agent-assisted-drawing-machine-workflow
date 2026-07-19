from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import cast

from PIL import Image, ImageChops, ImageOps, ImageStat

from drawingmachine.domain.workflow.models import RouteContext, RouteDecision, workflow_error
from drawingmachine.domain.workflow.normalization import (
    binarize,
    connected_components,
    flattened_image_data,
    remove_small_components,
)
from drawingmachine.json_types import JsonObject

BLOCKING_VISUAL_RISKS = {
    "poor_subject_background_separation",
    "complex_background",
    "excessive_binary_transition_density",
    "excessive_components_or_noise",
    "unstable_subject_mask",
    "photo_like_continuous_tone",
    "severe_gradient_or_texture",
    "subject_too_tiny_or_too_full_frame",
}
RAW_VISUAL_SCORE_MAX = 0.80
VISUAL_DIRECT_SCORE_MAX = 0.70


@dataclass(frozen=True, slots=True)
class _ImageStats:
    width: int
    height: int
    aspect_ratio: float
    quantized_color_count: int
    foreground_ratio: float
    edge_ratio: float
    contrast: float
    white_background_likelihood: float


def decide_route(
    image: Image.Image,
    semantic_assessment: JsonObject,
    *,
    context: RouteContext | None = None,
) -> RouteDecision:
    if context is None:
        context = RouteContext()
    source_image = image.copy()
    rgb = source_image.convert("RGB")
    base_stats = image_stats(rgb)
    classification = classify_image(rgb, context.prompt)
    extra = extended_stats(source_image)
    assessment = validate_semantic_assessment(semantic_assessment)
    semantic_style_score, semantic_score_source, semantic_rationale, semantic_blockers = semantic_score_from_assessment(
        assessment
    )
    visual_direct_score, visual_scores, visual_rationale = visual_direct_score_from_stats(extra)
    direct_score = round(min(1.0, semantic_style_score + visual_direct_score), 4)
    risk_flags = route_risk_flags(extra)
    blocking_risks = [flag for flag in risk_flags if flag in BLOCKING_VISUAL_RISKS]
    category = select_category(classification, context.prompt)
    route_override = route_override_from_prompt(context.prompt)
    rationale = semantic_rationale + visual_rationale

    if route_override == "user_requested_preserve_original":
        route = "A_DIRECT"
        direct_route_allowed = True
        next_stage = "prepare_direct_processed_image"
        risk_flags.append("user_forced_direct_route")
        rationale.append("user explicitly requested preserving the current image and avoiding simplification")
        rationale.append("direct route still requires processed-image review and complexity gates")
    elif route_override == "user_requested_image_edit":
        route = "B_IMAGE_EDIT"
        direct_route_allowed = False
        next_stage = "qwen_image_edit"
        if "user_requested_image_edit" not in risk_flags:
            risk_flags.append("user_requested_image_edit")
        rationale.append("user explicitly requested image-edit simplification or redrawing")
    elif direct_score >= 0.80 and not blocking_risks and "uncertain_binarization_risk" not in risk_flags:
        route = "A_DIRECT"
        direct_route_allowed = True
        next_stage = "prepare_direct_processed_image"
    else:
        route = "B_IMAGE_EDIT"
        direct_route_allowed = False
        next_stage = "qwen_image_edit"
        if "uncertain_or_complex" not in risk_flags:
            risk_flags.append("uncertain_or_complex")

    document: dict[str, object] = {
        "schema": "input_route_decision_v1",
        "job_name": context.job_name,
        "input_image": context.input_image,
        "decided_at": context.decided_at,
        "decider": context.decider,
        "route": route,
        "confidence": round(direct_score, 2),
        "image_category": category,
        "rationale": rationale,
        "risk_flags": risk_flags,
        "blocking_risks": blocking_risks,
        "direct_route_allowed": direct_route_allowed,
        "next_stage": next_stage,
        "hardware_touched": False,
        "semantic_style_score": round(semantic_style_score, 4),
        "semantic_score_source": semantic_score_source,
        "semantic_rationale": semantic_rationale,
        "semantic_blockers": semantic_blockers,
        "visual_direct_score": round(visual_direct_score, 4),
        "visual_score_source": "deterministic_image_statistics",
        "direct_score": direct_score,
        "route_override": route_override,
        "subject_mask_ratio": extra["subject_mask_ratio"],
        "binary_transition_density": extra["binary_transition_density"],
        "background_simplicity_score": visual_scores["background_simplicity_score"],
        "subject_background_separation_score": visual_scores["subject_background_separation_score"],
        "visual_scores": visual_scores,
        "stats": {**asdict(base_stats), **extra},
        "classification": classification,
    }
    return RouteDecision.from_document(document)


def validate_semantic_assessment(assessment: JsonObject) -> dict[str, object]:
    score = assessment.get("semantic_style_score")
    if not isinstance(score, int | float) or isinstance(score, bool) or not math.isfinite(float(score)):
        raise workflow_error("semantic_style_score must be a finite number")
    if not 0.0 <= float(score) <= 0.30:
        raise workflow_error("semantic_style_score must be between 0.00 and 0.30")
    source = assessment.get("semantic_score_source", "router_multimodal_model")
    if source != "router_multimodal_model":
        raise workflow_error("semantic_score_source must be router_multimodal_model")
    rationale = assessment.get("semantic_rationale", [])
    if not isinstance(rationale, list) or not all(isinstance(item, str) for item in rationale):
        raise workflow_error("semantic_rationale must be an array of strings")
    blockers = assessment.get("semantic_blockers", [])
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        raise workflow_error("semantic_blockers must be an array of strings")
    return {
        "semantic_style_score": round(float(score), 4),
        "semantic_score_source": source,
        "semantic_rationale": list(cast(list[str], rationale)),
        "semantic_blockers": list(cast(list[str], blockers)),
    }


def semantic_score_from_assessment(assessment: dict[str, object]) -> tuple[float, str, list[str], list[str]]:
    return (
        float(cast(float, assessment["semantic_style_score"])),
        str(assessment["semantic_score_source"]),
        list(cast(list[str], assessment["semantic_rationale"])),
        list(cast(list[str], assessment["semantic_blockers"])),
    )


def image_stats(image: Image.Image, sample_size: int = 256) -> _ImageStats:
    small = image.copy()
    small.thumbnail((sample_size, sample_size))
    gray = ImageOps.grayscale(small)
    histogram = gray.histogram()
    total = sum(histogram) or 1
    mean = sum(index * value for index, value in enumerate(histogram)) / total
    variance = sum(((index - mean) ** 2) * value for index, value in enumerate(histogram)) / total
    contrast = math.sqrt(variance) / 255.0
    threshold = otsu_threshold(histogram)
    binary = gray.point(lambda pixel: 0 if pixel < threshold else 255, mode="L")
    foreground_ratio = sum(binary.histogram()[:128]) / total
    edges = simple_edge_mask(binary)
    edge_ratio = sum(1 for pixel in flattened_image_data(edges) if pixel == 0) / total
    quantized = small.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    color_count = len(quantized.getcolors(maxcolors=4096) or [])
    border: list[int] = []
    width, height = gray.size
    for x in range(width):
        border.append(gray_pixel(gray, x, 0))
        border.append(gray_pixel(gray, x, height - 1))
    for y in range(height):
        border.append(gray_pixel(gray, 0, y))
        border.append(gray_pixel(gray, width - 1, y))
    white_background = sum(1 for value in border if value > 220) / max(1, len(border))
    source_width, source_height = image.size
    return _ImageStats(
        width=source_width,
        height=source_height,
        aspect_ratio=round(source_width / max(1, source_height), 4),
        quantized_color_count=color_count,
        foreground_ratio=round(foreground_ratio, 4),
        edge_ratio=round(edge_ratio, 4),
        contrast=round(contrast, 4),
        white_background_likelihood=round(white_background, 4),
    )


def otsu_threshold(histogram: list[int]) -> int:
    total = sum(histogram)
    sum_total = sum(index * histogram[index] for index in range(256))
    sum_background = 0.0
    weight_background = 0
    max_variance = -1.0
    threshold = 128
    for index in range(256):
        weight_background += histogram[index]
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break
        sum_background += index * histogram[index]
        mean_background = sum_background / weight_background
        mean_foreground = (sum_total - sum_background) / weight_foreground
        variance = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance > max_variance:
            max_variance = variance
            threshold = index
    return threshold


def simple_edge_mask(binary: Image.Image) -> Image.Image:
    width, height = binary.size
    output = Image.new("L", binary.size, 255)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            value = gray_pixel(binary, x, y)
            if (
                value != gray_pixel(binary, x + 1, y)
                or value != gray_pixel(binary, x - 1, y)
                or value != gray_pixel(binary, x, y + 1)
                or value != gray_pixel(binary, x, y - 1)
            ):
                output.putpixel((x, y), 0)
    return output


def gray_pixel(image: Image.Image, x: int, y: int) -> int:
    value = image.getpixel((x, y))
    if value is None or isinstance(value, tuple):
        raise TypeError("expected a single-band integer pixel")
    return int(value)


def rgb_pixel(image: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    value = image.getpixel((x, y))
    if not isinstance(value, tuple) or len(value) != 3:
        raise TypeError("expected a three-band RGB pixel")
    red, green, blue = value
    if not all(isinstance(channel, int) for channel in (red, green, blue)):
        raise TypeError("expected integer RGB channels")
    return int(red), int(green), int(blue)


def classify_image(image: Image.Image, prompt: str = "") -> dict[str, object]:
    stats = image_stats(image)
    lowered = prompt.lower()
    portrait_intent = any(
        word in lowered
        for word in (
            "portrait",
            "face",
            "anime",
            "manga",
            "character",
            "fullbody",
            "person",
            "human",
            "girl",
            "boy",
            "figure",
            "人物",
            "人像",
            "头像",
            "脸",
            "动漫",
            "漫画",
            "角色",
            "少女",
        )
    )
    technical_intent = any(
        word in lowered
        for word in (
            "technical",
            "dimension",
            "diagram",
            "schematic",
            "circuit",
            "pcb",
            "blueprint",
            "mechanical",
            "engineering",
            "工程",
            "图纸",
            "尺寸",
            "技术",
            "电路",
            "机械",
        )
    )
    if portrait_intent:
        style, confidence = "portrait_line_art", 0.78
        rationale = "User intent indicates a portrait or character line-art route."
    elif technical_intent:
        style, confidence = "technical_trace", 0.76
        rationale = "User intent indicates a technical trace route."
    elif any(word in lowered for word in ("logo", "标志", "商标")):
        style, confidence = "logo_outline", 0.86
        rationale = "User intent indicates a logo outline route."
    elif stats.quantized_color_count <= 8 and stats.contrast >= 0.18 and stats.edge_ratio <= 0.16:
        style, confidence = "logo_outline", 0.78
        rationale = "Image has few colors, clear contrast, and relatively sparse edges."
    elif stats.white_background_likelihood >= 0.75 and stats.edge_ratio >= 0.10 and stats.foreground_ratio <= 0.35:
        style, confidence = "technical_trace", 0.66
        rationale = "Image appears line-heavy on a light background."
    else:
        style, confidence = "clean_contour", 0.58
        rationale = "No stronger route matched; clean_contour is the conservative default."
    return {
        "style_family": style,
        "confidence": round(confidence, 2),
        "rationale": rationale,
        "uncertainty_note": None
        if confidence >= 0.7
        else "Classification is preliminary; inspect preview before proceeding.",
        "stats": asdict(stats),
    }


def extended_stats(image: Image.Image, sample_size: int = 256) -> dict[str, object]:
    small = image.convert("RGBA")
    small.thumbnail((sample_size, sample_size))
    width, height = small.size
    total = max(1, width * height)
    rgb = small.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    alpha = small.getchannel("A")
    alpha_values = [cast(int, value) for value in flattened_image_data(alpha)]
    alpha_background_ratio = sum(1 for value in alpha_values if value <= 16) / total
    has_alpha_background = alpha_background_ratio >= 0.05
    subject_mask, threshold, inverted, variant_ratios = build_subject_mask(
        gray,
        alpha,
        has_alpha_background=has_alpha_background,
    )
    subject_mask = remove_small_components(subject_mask, width, height, 8)
    components = connected_components(subject_mask, width, height)
    small_components = sum(1 for pixels, _ in components if len(pixels) < 16)
    subject_mask_ratio = len(subject_mask) / total
    binary_transition_density = transition_density(subject_mask, width, height)
    border_points = border_pixels(rgb, alpha)
    background_color, border_variance, dominant_ratio = border_background_stats(border_points)
    foreground_background_delta = subject_background_delta(rgb, subject_mask, background_color)
    background_edge_density = border_edge_density(rgb)
    pixels = [cast(tuple[int, int, int], value) for value in flattened_image_data(rgb)]
    colorfulness = sum(abs(red - green) + abs(red - blue) + abs(green - blue) for red, green, blue in pixels) / (
        total * 3 * 255
    )
    quantized = rgb.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    quantized_colors = quantized.convert("RGB").getcolors(maxcolors=4096) or []
    dominant_color_count = len(quantized_colors)
    local_color_variance, gradient_density = color_variance_metrics(rgb)
    largest_component_ratio = max((len(pixels_) for pixels_, _ in components), default=0) / max(1, len(subject_mask))
    bbox = content_bbox(subject_mask)
    component_bbox_fill_ratio = len(subject_mask) / bbox_area(bbox) if bbox else 0.0
    skeleton_density = binary_transition_density / max(0.001, subject_mask_ratio)
    return {
        "component_count": len(components),
        "small_component_count": small_components,
        "estimated_colorfulness": round(colorfulness, 4),
        "foreground_background_delta": round(foreground_background_delta, 4),
        "background_color_variance": round(border_variance, 4),
        "border_color_variance": round(border_variance, 4),
        "border_dominant_color_ratio": round(dominant_ratio, 4),
        "background_edge_density": round(background_edge_density, 4),
        "alpha_background_ratio": round(alpha_background_ratio, 4),
        "alpha_channel_presence": has_alpha_background,
        "threshold_margin": round(abs(foreground_background_delta), 4),
        "subject_mask_ratio": round(subject_mask_ratio, 4),
        "binary_transition_density": round(binary_transition_density, 4),
        "binarization_threshold": threshold,
        "auto_inverted": inverted,
        "threshold_variant_subject_ratios": [round(value, 4) for value in variant_ratios],
        "dominant_color_count": dominant_color_count,
        "dominant_quantized_color_count": dominant_color_count,
        "local_color_variance": round(local_color_variance, 4),
        "gradient_density": round(gradient_density, 4),
        "shadow_likelihood": round(max(0.0, min(1.0, gradient_density + border_variance)), 4),
        "largest_component_ratio": round(largest_component_ratio, 4),
        "skeleton_density": round(skeleton_density, 4),
        "component_bbox_fill_ratio": round(component_bbox_fill_ratio, 4),
        "post_binarization_readability_proxy": round(
            max(
                0.0,
                min(
                    1.0,
                    largest_component_ratio * 0.5
                    + component_bbox_fill_ratio * 0.3
                    + (1.0 - binary_transition_density) * 0.2,
                ),
            ),
            4,
        ),
    }


def build_subject_mask(
    gray: Image.Image,
    alpha: Image.Image,
    *,
    has_alpha_background: bool,
) -> tuple[set[tuple[int, int]], int, bool, list[float]]:
    width, height = gray.size
    total = max(1, width * height)
    if has_alpha_background:
        subject = {(index % width, index // width) for index, value in enumerate(alpha.getdata()) if value > 16}
        return subject, 0, False, [len(subject) / total]
    foreground, _, _, threshold, inverted = binarize(gray, None, None)
    ratios: list[float] = []
    data = [cast(int, value) for value in flattened_image_data(gray)]
    for candidate in (max(0, threshold - 20), threshold, min(255, threshold + 20)):
        mask = {(index % width, index // width) for index, value in enumerate(data) if value < candidate}
        if len(mask) / total > 0.65:
            mask = {(x, y) for y in range(height) for x in range(width) if (x, y) not in mask}
        ratios.append(len(mask) / total)
    return foreground, threshold, inverted, ratios


def transition_density(mask: set[tuple[int, int]], width: int, height: int) -> float:
    transitions = 0
    comparisons = 0
    for y in range(height):
        for x in range(width - 1):
            transitions += ((x, y) in mask) != ((x + 1, y) in mask)
            comparisons += 1
    for y in range(height - 1):
        for x in range(width):
            transitions += ((x, y) in mask) != ((x, y + 1) in mask)
            comparisons += 1
    return transitions / max(1, comparisons)


def border_pixels(rgb: Image.Image, alpha: Image.Image) -> list[tuple[int, int, int]]:
    width, height = rgb.size
    points: list[tuple[int, int, int]] = []
    for x in range(width):
        for y in (0, height - 1):
            if gray_pixel(alpha, x, y) > 16:
                points.append(rgb_pixel(rgb, x, y))
    for y in range(height):
        for x in (0, width - 1):
            if gray_pixel(alpha, x, y) > 16:
                points.append(rgb_pixel(rgb, x, y))
    return points


def border_background_stats(points: list[tuple[int, int, int]]) -> tuple[tuple[float, float, float], float, float]:
    if not points:
        return (255.0, 255.0, 255.0), 0.0, 1.0
    channels = (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
        sum(point[2] for point in points) / len(points),
    )
    variance = sum(
        sum((point[index] - channels[index]) ** 2 for index in range(3)) / (3 * 255 * 255) for point in points
    ) / len(points)
    buckets: dict[tuple[int, int, int], int] = {}
    for point in points:
        bucket = (int(point[0] // 32), int(point[1] // 32), int(point[2] // 32))
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return channels, variance, max(buckets.values()) / len(points)


def subject_background_delta(
    rgb: Image.Image,
    subject_mask: set[tuple[int, int]],
    background_color: tuple[float, float, float],
) -> float:
    if not subject_mask:
        return 0.0
    sample = list(subject_mask)
    stride = max(1, len(sample) // 4000)
    distances = [
        sum(abs(rgb_pixel(rgb, x, y)[index] - background_color[index]) for index in range(3)) / (3 * 255)
        for x, y in sample[::stride]
    ]
    return sum(distances) / max(1, len(distances))


def border_edge_density(rgb: Image.Image) -> float:
    gray = ImageOps.grayscale(rgb)
    width, height = gray.size
    transitions = 0
    comparisons = 0
    for y in (0, height - 1):
        for x in range(width - 1):
            transitions += abs(gray_pixel(gray, x, y) - gray_pixel(gray, x + 1, y)) > 16
            comparisons += 1
    for x in (0, width - 1):
        for y in range(height - 1):
            transitions += abs(gray_pixel(gray, x, y) - gray_pixel(gray, x, y + 1)) > 16
            comparisons += 1
    return transitions / max(1, comparisons)


def color_variance_metrics(rgb: Image.Image) -> tuple[float, float]:
    statistics = ImageStat.Stat(rgb)
    local_variance = sum((standard_deviation / 255) ** 2 for standard_deviation in statistics.stddev) / 3
    gray = ImageOps.grayscale(rgb)
    difference = ImageChops.difference(gray, ImageChops.offset(gray, 1, 0))
    values = [cast(int, value) for value in flattened_image_data(difference)]
    gradient_density = sum(1 for value in values if value > 12) / max(1, len(values))
    return local_variance, gradient_density


def content_bbox(mask: set[tuple[int, int]]) -> list[int] | None:
    if not mask:
        return None
    x_values = [x for x, _ in mask]
    y_values = [y for _, y in mask]
    return [min(x_values), min(y_values), max(x_values), max(y_values)]


def bbox_area(bbox: list[int] | None) -> int:
    if not bbox:
        return 0
    return max(1, (bbox[2] - bbox[0] + 1) * (bbox[3] - bbox[1] + 1))


def route_risk_flags(extra: dict[str, object]) -> list[str]:
    flags: list[str] = []
    if float(cast(float, extra["subject_background_separation_score"])) < 0.04:
        flags.append("poor_subject_background_separation")
    if float(cast(float, extra["background_simplicity_score"])) == 0.0:
        flags.append("complex_background")
    if float(cast(float, extra["binary_transition_density"])) > 0.28:
        flags.append("excessive_binary_transition_density")
    if int(cast(int, extra["component_count"])) > 220 or int(cast(int, extra["small_component_count"])) > 80:
        flags.append("excessive_components_or_noise")
    ratios = cast(list[float], extra["threshold_variant_subject_ratios"])
    if len(ratios) >= 2 and max(ratios) - min(ratios) > 0.30:
        flags.extend(("unstable_subject_mask", "uncertain_binarization_risk"))
    if (
        int(cast(int, extra["dominant_color_count"])) >= 12
        and float(cast(float, extra["local_color_variance"])) >= 0.045
        and float(cast(float, extra["binary_transition_density"])) >= 0.12
    ):
        flags.append("photo_like_continuous_tone")
    if float(cast(float, extra["gradient_density"])) >= 0.20 or (
        float(cast(float, extra["local_color_variance"])) >= 0.07
        and float(cast(float, extra["estimated_colorfulness"])) >= 0.08
    ):
        flags.append("severe_gradient_or_texture")
    subject_ratio = float(cast(float, extra["subject_mask_ratio"]))
    if subject_ratio < 0.005 or subject_ratio > 0.80:
        flags.append("subject_too_tiny_or_too_full_frame")
    return flags


def select_category(classification: dict[str, object], prompt: str) -> str:
    style = classification.get("style_family")
    if style in {"logo_outline", "clean_contour", "portrait_line_art", "technical_trace"}:
        return str(style)
    lowered = prompt.lower()
    if any(word in lowered for word in ("logo", "icon", "mark", "text", "标志", "商标", "图标", "文字")):
        return "logo_outline"
    return "unknown"


def visual_direct_score_from_stats(extra: dict[str, object]) -> tuple[float, dict[str, float], list[str]]:
    separation = score_separation(extra)
    background = score_background_simplicity(extra)
    transition = score_binary_transition(float(cast(float, extra["binary_transition_density"])))
    subject = score_subject_ratio(float(cast(float, extra["subject_mask_ratio"])))
    components = score_components(
        int(cast(int, extra["component_count"])),
        int(cast(int, extra["small_component_count"])),
    )
    color = score_color_complexity(extra)
    clarity = score_clarity(extra)
    scores = {
        "subject_background_separation_score": separation,
        "background_simplicity_score": background,
        "binary_transition_density_score": transition,
        "subject_mask_ratio_score": subject,
        "component_noise_score": components,
        "color_gradient_shadow_complexity_score": color,
        "line_subject_clarity_score": clarity,
    }
    raw_score = sum(scores.values())
    normalized_score = min(VISUAL_DIRECT_SCORE_MAX, raw_score / RAW_VISUAL_SCORE_MAX * VISUAL_DIRECT_SCORE_MAX)
    extra["subject_background_separation_score"] = separation
    extra["background_simplicity_score"] = background
    return (
        round(normalized_score, 4),
        scores,
        [f"visual direct score {round(normalized_score, 4)} from deterministic image statistics"],
    )


def score_separation(extra: dict[str, object]) -> float:
    delta = float(cast(float, extra["foreground_background_delta"]))
    variance = float(cast(float, extra["background_color_variance"]))
    if bool(extra["alpha_channel_presence"]):
        return 0.16
    if delta >= 0.42 and variance <= 0.025:
        return 0.16
    if delta >= 0.28 and variance <= 0.05:
        return 0.12
    if delta >= 0.16:
        return 0.08
    if delta >= 0.08:
        return 0.04
    return 0.0


def score_background_simplicity(extra: dict[str, object]) -> float:
    if bool(extra["alpha_channel_presence"]):
        return 0.12
    variance = float(cast(float, extra["border_color_variance"]))
    dominant = float(cast(float, extra["border_dominant_color_ratio"]))
    edge = float(cast(float, extra["background_edge_density"]))
    if variance <= 0.001 and dominant >= 0.98 and edge <= 0.02:
        return 0.12
    if variance <= 0.004 and dominant >= 0.90 and edge <= 0.04:
        return 0.09
    if variance <= 0.012 and dominant >= 0.70 and edge <= 0.08:
        return 0.06
    if variance <= 0.035 and edge <= 0.15:
        return 0.03
    return 0.0


def score_binary_transition(density: float) -> float:
    if density <= 0.08:
        return 0.12
    if density <= 0.12:
        return 0.09
    if density <= 0.16:
        return 0.06
    if density <= 0.22:
        return 0.03
    return 0.0


def score_subject_ratio(ratio: float) -> float:
    if 0.02 <= ratio <= 0.40:
        return 0.10
    if 0.40 < ratio <= 0.55:
        return 0.08
    if 0.01 <= ratio < 0.02:
        return 0.06
    if 0.55 < ratio <= 0.70:
        return 0.04
    return 0.0


def score_components(component_count: int, small_component_count: int) -> float:
    if component_count <= 20 and small_component_count <= 5:
        return 0.12
    if component_count <= 50 and small_component_count <= 15:
        return 0.09
    if component_count <= 100 and small_component_count <= 30:
        return 0.06
    if component_count <= 160:
        return 0.03
    return 0.0


def score_color_complexity(extra: dict[str, object]) -> float:
    colors = int(cast(int, extra["dominant_color_count"]))
    colorfulness = float(cast(float, extra["estimated_colorfulness"]))
    local_variance = float(cast(float, extra["local_color_variance"]))
    gradient_density = float(cast(float, extra["gradient_density"]))
    if colors <= 4 and gradient_density <= 0.08:
        return 0.10
    if colors <= 8 and local_variance <= 0.035 and gradient_density <= 0.12:
        return 0.08
    if local_variance <= 0.06 and gradient_density <= 0.18:
        return 0.05
    if local_variance <= 0.10 and gradient_density <= 0.28 and colorfulness <= 0.20:
        return 0.02
    return 0.0


def score_clarity(extra: dict[str, object]) -> float:
    readability = float(cast(float, extra["post_binarization_readability_proxy"]))
    largest = float(cast(float, extra["largest_component_ratio"]))
    fill = float(cast(float, extra["component_bbox_fill_ratio"]))
    if readability >= 0.72 and largest >= 0.70:
        return 0.08
    if readability >= 0.60 and largest >= 0.55:
        return 0.06
    if readability >= 0.45 or fill >= 0.12:
        return 0.04
    if readability >= 0.30:
        return 0.02
    return 0.0


def route_override_from_prompt(prompt: str) -> str | None:
    lowered = prompt.lower()
    if direct_override_requested(lowered):
        return "user_requested_preserve_original"
    if any(
        word in lowered
        for word in (
            "use image edit",
            "image-edit",
            "qwen",
            "simplify",
            "redraw",
            "rewrite",
            "clean up",
            "图像模型",
            "简化",
            "重绘",
            "清理",
            "改成线稿",
        )
    ):
        return "user_requested_image_edit"
    return None


def direct_override_requested(lowered_prompt: str) -> bool:
    return any(
        word in lowered_prompt
        for word in (
            "preserve original",
            "keep original",
            "avoid simplification",
            "do not simplify",
            "don't simplify",
            "no image edit",
            "without image edit",
            "direct deterministic",
            "directly convert",
            "direct route",
            "保留原图",
            "不再简化",
            "不要简化",
            "不经过图像模型",
            "不要 image edit",
            "直接按当前图",
            "直接转换",
            "直接走确定性流程",
        )
    )


__all__ = ["decide_route"]
