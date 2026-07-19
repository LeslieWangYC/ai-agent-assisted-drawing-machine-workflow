from dataclasses import replace

from PIL import Image

from drawingmachine.domain.workflow import (
    ComplexityLimits,
    DedupeProfile,
    analyze_complexity,
    apply_single_dedupe_profile,
)


def test_complexity_soft_limit_selects_single_dedupe_profile() -> None:
    image = Image.new("L", (32, 32), 255)
    image.putpixel((8, 8), 0)
    image.putpixel((24, 24), 0)
    limits = replace(ComplexityLimits(), soft_component_count=1, hard_component_count=3, min_component_area_px=1)

    result = analyze_complexity(image, limits)
    updated = apply_single_dedupe_profile(DedupeProfile(), result)

    assert result.status == "PASS_WITH_SINGLE_DEDUPE"
    assert updated.dedupe_short_path_length_mm == 3.0
    assert updated.dedupe_distance_mm == 0.45
    assert updated.dedupe_angle_deg == 35.0
    assert updated.dedupe_overlap_ratio == 0.75


def test_complexity_hard_limit_does_not_apply_dedupe_profile() -> None:
    image = Image.new("L", (32, 32), 255)
    image.putpixel((8, 8), 0)
    image.putpixel((24, 24), 0)
    limits = replace(ComplexityLimits(), soft_component_count=0, hard_component_count=1, min_component_area_px=1)
    base = DedupeProfile()

    result = analyze_complexity(image, limits)

    assert result.status == "REJECT_COMPLEXITY"
    assert apply_single_dedupe_profile(base, result) == base
