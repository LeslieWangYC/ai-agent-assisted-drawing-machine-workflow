from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from PIL import Image

from drawingmachine.domain.planning.models import BinarizedImage


def binarize(gray: Image.Image, threshold: int | None, invert: bool | None) -> BinarizedImage:
    width, height = gray.size
    data = [cast(int, value) for value in _flattened_image_data(gray)]
    threshold_value = otsu_threshold(gray.histogram()) if threshold is None else int(threshold)
    min_value = min(data) if data else 0
    max_value = max(data) if data else 0
    if threshold is None and min_value < max_value and threshold_value <= min_value:
        threshold_value = min_value + 1
    foreground = {(index % width, index // width) for index, value in enumerate(data) if value < threshold_value}
    dark_ratio = len(foreground) / max(1, width * height)
    should_invert = dark_ratio > 0.65 if invert is None else bool(invert)
    if should_invert:
        foreground = {(x, y) for y in range(height) for x in range(width) if (x, y) not in foreground}
    return BinarizedImage(frozenset(foreground), width, height, threshold_value, should_invert)


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


def _flattened_image_data(image: Image.Image) -> list[object]:
    flattened = getattr(image, "get_flattened_data", None)
    values = flattened() if callable(flattened) else image.getdata()
    return list(cast(Iterable[object], values))


__all__ = ["binarize", "otsu_threshold"]
