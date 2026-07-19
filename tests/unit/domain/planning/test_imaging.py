from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from drawingmachine.domain.planning.imaging import (
    binarize,
    connected_components,
    remove_small_components,
    thin,
)

GOLDEN_ROOT = Path(__file__).resolve().parents[3] / "fixtures/package_b/golden"


def test_binarize_matches_frozen_otsu_and_automatic_inversion() -> None:
    image = Image.new("L", (4, 2))
    image.putdata((0, 0, 0, 0, 0, 0, 255, 255))

    actual = binarize(image, threshold=None, invert=None)
    assert actual.foreground == frozenset({(2, 1), (3, 1)})
    assert (actual.width, actual.height, actual.threshold, actual.inverted) == (4, 2, 1, True)


def test_binarize_matches_legacy_fixed_threshold_without_inversion() -> None:
    image = Image.new("L", (3, 1))
    image.putdata((63, 64, 192))

    actual = binarize(image, threshold=64, invert=False)

    assert actual.foreground == frozenset({(0, 0)})
    assert actual.threshold == 64
    assert actual.inverted is False


def test_connected_components_preserve_eight_neighbor_membership_and_stats() -> None:
    image = Image.new("L", (5, 5), color=255)
    image.putpixel((1, 1), 0)
    image.putpixel((2, 2), 0)
    image.putpixel((4, 4), 0)
    binary = binarize(image, threshold=128, invert=False)

    actual = connected_components(binary)
    assert {(component.pixels, component.area_px) for component in actual} == {
        (frozenset({(1, 1), (2, 2)}), 2),
        (frozenset({(4, 4)}), 1),
    }
    diagonal = next(component for component in actual if component.area_px == 2)
    assert diagonal.pixels == frozenset({(1, 1), (2, 2)})
    assert (diagonal.min_x, diagonal.min_y, diagonal.max_x, diagonal.max_y) == (1, 1, 2, 2)
    assert (diagonal.width_px, diagonal.height_px) == (2, 2)


def test_remove_small_components_preserves_metadata_and_frozen_pixels() -> None:
    image = Image.new("L", (5, 5), color=255)
    for point in ((1, 1), (1, 2), (2, 2), (4, 4)):
        image.putpixel(point, 0)
    binary = binarize(image, threshold=128, invert=False)

    actual = remove_small_components(binary, min_area=3)
    assert actual.foreground == frozenset({(1, 1), (1, 2), (2, 2)})
    assert (actual.width, actual.height, actual.threshold, actual.inverted) == (
        binary.width,
        binary.height,
        binary.threshold,
        binary.inverted,
    )


def test_thinning_preserves_branch_and_loop_pixels() -> None:
    with Image.open(GOLDEN_ROOT / "branch_loop.png") as source:
        image = source.convert("L")
    binary = binarize(image, threshold=None, invert=None)
    filtered = remove_small_components(binary, min_area=8)

    actual = thin(filtered)
    encoded = json.dumps(sorted(actual.foreground), separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "a8dd792a6e777f812e1c6f1bbe8467faf6d21732ef16035b3212a2b66f22f224"
    assert len(actual.foreground) == 360
    assert actual.width == filtered.width
    assert actual.height == filtered.height
