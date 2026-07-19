from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from io import BytesIO
from typing import cast

from PIL import Image, ImageFilter, ImageOps

from drawingmachine.domain.workflow.models import (
    DirectImageConfig,
    NormalizationConfig,
    NormalizedImage,
    PreparedImage,
)

Pixel = tuple[int, int]


def flattened_image_data(image: Image.Image) -> list[object]:
    flattened = getattr(image, "get_flattened_data", None)
    values = flattened() if callable(flattened) else image.getdata()
    return list(cast(Iterable[object], values))


def prepare_direct_image(
    image: Image.Image,
    config: DirectImageConfig | None = None,
) -> PreparedImage:
    if config is None:
        config = DirectImageConfig()
    gray = ImageOps.grayscale(image.convert("RGB"))
    if config.median_filter_size > 1:
        gray = gray.filter(ImageFilter.MedianFilter(size=config.median_filter_size))
    foreground, width, height, threshold, inverted = binarize(gray, config.threshold, config.invert)
    components_before = connected_components(foreground, width, height)
    foreground = remove_small_components(foreground, width, height, config.min_component_area_px)
    components_after = connected_components(foreground, width, height)
    output = Image.new("L", (width, height), 255)
    output.putdata([0 if (x, y) in foreground else 255 for y in range(height) for x in range(width)])
    removed_pixels = sum(len(component) for component, _ in components_before) - sum(
        len(component) for component, _ in components_after
    )
    report: dict[str, object] = {
        "schema": "direct_processed_image_report_v1",
        "created_at": config.created_at,
        "input": config.input_path,
        "output": config.output_path,
        "size_px": [width, height],
        "binarization": {
            "method": "otsu" if config.threshold is None else "fixed",
            "threshold": threshold,
            "inverted": inverted,
            "min_component_area_px": config.min_component_area_px,
            "median_filter_size": config.median_filter_size,
        },
        "components_before": len(components_before),
        "components_after": len(components_after),
        "small_component_pixels_removed": removed_pixels,
        "foreground_pixels": len(foreground),
        "foreground_ratio": round(len(foreground) / max(1, width * height), 8),
        "content_bbox_px": content_bbox_pixels(foreground),
        "hardware_touched": False,
    }
    return PreparedImage.create(render_png(output), report)


def normalize_image(image: Image.Image, config: NormalizationConfig) -> NormalizedImage:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = [cast(tuple[int, int, int], value) for value in flattened_image_data(rgb)]
    total = width * height
    colored_pixels = 0
    black_mask = bytearray(total)
    for index, (red, green, blue) in enumerate(pixels):
        if max(red, green, blue) - min(red, green, blue) > config.color_delta_threshold:
            colored_pixels += 1
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue
        if luminance <= config.threshold:
            black_mask[index] = 1
    components_removed = 0
    removed_pixels = 0
    if config.remove_small_components_px > 0:
        black_mask, components_removed, removed_pixels = remove_small_mask_components(
            black_mask,
            width=width,
            height=height,
            min_area=config.remove_small_components_px,
        )
    output = Image.new("L", (width, height), 255)
    output.putdata([0 if value else 255 for value in black_mask])
    black_pixels = sum(1 for value in black_mask if value)
    report: dict[str, object] = {
        "schema": "processed_image_normalize_report_v1",
        "created_at": config.created_at,
        "input": config.input_path,
        "output": config.output_path,
        "size_px": [width, height],
        "threshold": float(config.threshold),
        "color_delta_threshold": config.color_delta_threshold,
        "input_colored_pixels": colored_pixels,
        "input_colored_ratio": round(colored_pixels / total, 8),
        "output_black_pixels": black_pixels,
        "output_black_ratio": round(black_pixels / total, 8),
        "small_components_removed": components_removed,
        "small_component_pixels_removed": removed_pixels,
        "content_bbox_px": content_bbox_mask(black_mask, width, height),
        "hardware_touched": False,
    }
    return NormalizedImage.create(render_png(output), report)


def render_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def binarize(gray: Image.Image, threshold: int | None, invert: bool | None) -> tuple[set[Pixel], int, int, int, bool]:
    width, height = gray.size
    data = [cast(int, value) for value in flattened_image_data(gray)]
    threshold_value = otsu_threshold(gray.histogram()) if threshold is None else int(threshold)
    min_value = min(data) if data else 0
    max_value = max(data) if data else 0
    if threshold is None and min_value < max_value and threshold_value <= min_value:
        threshold_value = min_value + 1
    foreground = {(index % width, index // width) for index, value in enumerate(data) if value < threshold_value}
    dark_ratio = len(foreground) / max(1, width * height)
    should_invert = dark_ratio > 0.65 if invert is None else invert
    if should_invert:
        foreground = {(x, y) for y in range(height) for x in range(width) if (x, y) not in foreground}
    return foreground, width, height, threshold_value, should_invert


def otsu_threshold(histogram: list[int]) -> int:
    total = sum(histogram)
    sum_total = sum(index * histogram[index] for index in range(256))
    sum_background = 0.0
    weight_background = 0
    best_variance = -1.0
    best_threshold = 128
    for value in range(256):
        weight_background += histogram[value]
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break
        sum_background += value * histogram[value]
        mean_background = sum_background / weight_background
        mean_foreground = (sum_total - sum_background) / weight_foreground
        variance = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = value
    return best_threshold


def remove_small_components(foreground: set[Pixel], width: int, height: int, min_area: int) -> set[Pixel]:
    if min_area <= 1:
        return foreground
    kept: set[Pixel] = set()
    for pixels, _ in connected_components(foreground, width, height):
        if len(pixels) >= min_area:
            kept.update(pixels)
    return kept


def connected_components(foreground: set[Pixel], width: int, height: int) -> list[tuple[set[Pixel], dict[str, int]]]:
    unvisited = set(foreground)
    components: list[tuple[set[Pixel], dict[str, int]]] = []
    while unvisited:
        start = unvisited.pop()
        stack = [start]
        pixels = {start}
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        while stack:
            current_x, current_y = stack.pop()
            min_x = min(min_x, current_x)
            max_x = max(max_x, current_x)
            min_y = min(min_y, current_y)
            max_y = max(max_y, current_y)
            for neighbor in neighbors8(current_x, current_y, width, height):
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    pixels.add(neighbor)
                    stack.append(neighbor)
        components.append(
            (
                pixels,
                {
                    "area_px": len(pixels),
                    "min_x": min_x,
                    "min_y": min_y,
                    "max_x": max_x,
                    "max_y": max_y,
                    "width_px": max_x - min_x + 1,
                    "height_px": max_y - min_y + 1,
                },
            )
        )
    return components


def neighbors8(x: int, y: int, width: int, height: int) -> list[Pixel]:
    values: list[Pixel] = []
    for delta_y in (-1, 0, 1):
        for delta_x in (-1, 0, 1):
            if delta_x == 0 and delta_y == 0:
                continue
            neighbor_x = x + delta_x
            neighbor_y = y + delta_y
            if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
                values.append((neighbor_x, neighbor_y))
    return values


def zhang_suen_thinning(
    foreground: set[Pixel],
    width: int,
    height: int,
    max_iterations: int = 500,
) -> set[Pixel]:
    result = {pixel for pixel in foreground if 0 < pixel[0] < width - 1 and 0 < pixel[1] < height - 1}
    for _ in range(max_iterations):
        changed = False
        for step in (0, 1):
            to_remove: set[Pixel] = set()
            for x, y in result:
                p2 = (x, y - 1) in result
                p3 = (x + 1, y - 1) in result
                p4 = (x + 1, y) in result
                p5 = (x + 1, y + 1) in result
                p6 = (x, y + 1) in result
                p7 = (x - 1, y + 1) in result
                p8 = (x - 1, y) in result
                p9 = (x - 1, y - 1) in result
                values = [p2, p3, p4, p5, p6, p7, p8, p9]
                count = sum(values)
                if count < 2 or count > 6:
                    continue
                transitions = sum(
                    (not current) and following
                    for current, following in zip(values, values[1:] + values[:1], strict=True)
                )
                if transitions != 1:
                    continue
                keep = (
                    (p2 and p4 and p6) or (p4 and p6 and p8),
                    (p2 and p4 and p8) or (p2 and p6 and p8),
                )[step]
                if not keep:
                    to_remove.add((x, y))
            if to_remove:
                result.difference_update(to_remove)
                changed = True
        if not changed:
            break
    return result


def remove_small_mask_components(
    mask: bytearray,
    *,
    width: int,
    height: int,
    min_area: int,
) -> tuple[bytearray, int, int]:
    visited = bytearray(len(mask))
    keep = bytearray(len(mask))
    components_removed = 0
    removed_pixels = 0
    for start, value in enumerate(mask):
        if not value or visited[start]:
            continue
        component = collect_mask_component(mask, visited, start, width, height)
        if len(component) >= min_area:
            for index in component:
                keep[index] = 1
        else:
            components_removed += 1
            removed_pixels += len(component)
    return keep, components_removed, removed_pixels


def collect_mask_component(mask: bytearray, visited: bytearray, start: int, width: int, height: int) -> list[int]:
    component: list[int] = []
    queue: deque[int] = deque([start])
    visited[start] = 1
    while queue:
        index = queue.popleft()
        component.append(index)
        x = index % width
        y = index // width
        for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
            row_offset = neighbor_y * width
            for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                neighbor = row_offset + neighbor_x
                if neighbor == index or visited[neighbor] or not mask[neighbor]:
                    continue
                visited[neighbor] = 1
                queue.append(neighbor)
    return component


def content_bbox_pixels(foreground: set[Pixel]) -> list[int] | None:
    if not foreground:
        return None
    x_values = [x for x, _ in foreground]
    y_values = [y for _, y in foreground]
    return [min(x_values), min(y_values), max(x_values), max(y_values)]


def content_bbox_mask(mask: bytearray, width: int, height: int) -> list[int] | None:
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for index, value in enumerate(mask):
        if not value:
            continue
        x = index % width
        y = index // width
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
    if max_x < 0:
        return None
    return [min_x, min_y, max_x, max_y]


__all__ = ["normalize_image", "prepare_direct_image"]
