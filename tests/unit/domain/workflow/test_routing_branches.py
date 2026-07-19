from __future__ import annotations

from typing import Any, cast

import pytest
from PIL import Image

from drawingmachine.domain.workflow import routing
from drawingmachine.errors import DrawingMachineError


def _extra(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "alpha_channel_presence": False,
        "foreground_background_delta": 0.5,
        "background_color_variance": 0.0,
        "border_color_variance": 0.0,
        "border_dominant_color_ratio": 1.0,
        "background_edge_density": 0.0,
        "binary_transition_density": 0.05,
        "subject_mask_ratio": 0.2,
        "component_count": 2,
        "small_component_count": 0,
        "dominant_color_count": 2,
        "estimated_colorfulness": 0.0,
        "local_color_variance": 0.0,
        "gradient_density": 0.0,
        "post_binarization_readability_proxy": 0.8,
        "largest_component_ratio": 0.8,
        "component_bbox_fill_ratio": 0.5,
        "threshold_variant_subject_ratios": [0.2, 0.21],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"alpha_channel_presence": True}, 0.16),
        ({"foreground_background_delta": 0.5, "background_color_variance": 0.02}, 0.16),
        ({"foreground_background_delta": 0.3, "background_color_variance": 0.04}, 0.12),
        ({"foreground_background_delta": 0.2, "background_color_variance": 1.0}, 0.08),
        ({"foreground_background_delta": 0.1, "background_color_variance": 1.0}, 0.04),
        ({"foreground_background_delta": 0.01, "background_color_variance": 1.0}, 0.0),
    ],
)
def test_score_separation_all_bands(changes: dict[str, object], expected: float) -> None:
    assert routing.score_separation(_extra(**changes)) == expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"alpha_channel_presence": True}, 0.12),
        ({"border_color_variance": 0.0, "border_dominant_color_ratio": 1.0, "background_edge_density": 0.0}, 0.12),
        ({"border_color_variance": 0.003, "border_dominant_color_ratio": 0.95, "background_edge_density": 0.03}, 0.09),
        ({"border_color_variance": 0.01, "border_dominant_color_ratio": 0.8, "background_edge_density": 0.07}, 0.06),
        ({"border_color_variance": 0.03, "border_dominant_color_ratio": 0.1, "background_edge_density": 0.14}, 0.03),
        ({"border_color_variance": 0.2, "border_dominant_color_ratio": 0.1, "background_edge_density": 0.3}, 0.0),
    ],
)
def test_score_background_all_bands(changes: dict[str, object], expected: float) -> None:
    assert routing.score_background_simplicity(_extra(**changes)) == expected


@pytest.mark.parametrize(("value", "expected"), [(0.05, 0.12), (0.1, 0.09), (0.14, 0.06), (0.2, 0.03), (0.3, 0.0)])
def test_score_binary_transition_all_bands(value: float, expected: float) -> None:
    assert routing.score_binary_transition(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(0.2, 0.1), (0.5, 0.08), (0.015, 0.06), (0.6, 0.04), (0.9, 0.0)])
def test_score_subject_ratio_all_bands(value: float, expected: float) -> None:
    assert routing.score_subject_ratio(value) == expected


@pytest.mark.parametrize(
    ("components", "small", "expected"),
    [(10, 2, 0.12), (40, 10, 0.09), (90, 20, 0.06), (150, 100, 0.03), (200, 100, 0.0)],
)
def test_score_components_all_bands(components: int, small: int, expected: float) -> None:
    assert routing.score_components(components, small) == expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"dominant_color_count": 4, "gradient_density": 0.08}, 0.1),
        ({"dominant_color_count": 8, "local_color_variance": 0.03, "gradient_density": 0.1}, 0.08),
        ({"dominant_color_count": 20, "local_color_variance": 0.05, "gradient_density": 0.17}, 0.05),
        (
            {
                "dominant_color_count": 20,
                "local_color_variance": 0.09,
                "gradient_density": 0.27,
                "estimated_colorfulness": 0.1,
            },
            0.02,
        ),
        (
            {
                "dominant_color_count": 20,
                "local_color_variance": 0.2,
                "gradient_density": 0.5,
                "estimated_colorfulness": 0.5,
            },
            0.0,
        ),
    ],
)
def test_score_color_all_bands(changes: dict[str, object], expected: float) -> None:
    assert routing.score_color_complexity(_extra(**changes)) == expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"post_binarization_readability_proxy": 0.8, "largest_component_ratio": 0.8}, 0.08),
        ({"post_binarization_readability_proxy": 0.65, "largest_component_ratio": 0.6}, 0.06),
        ({"post_binarization_readability_proxy": 0.5, "largest_component_ratio": 0.1}, 0.04),
        (
            {
                "post_binarization_readability_proxy": 0.35,
                "largest_component_ratio": 0.1,
                "component_bbox_fill_ratio": 0.01,
            },
            0.02,
        ),
        (
            {
                "post_binarization_readability_proxy": 0.1,
                "largest_component_ratio": 0.1,
                "component_bbox_fill_ratio": 0.01,
            },
            0.0,
        ),
    ],
)
def test_score_clarity_all_bands(changes: dict[str, object], expected: float) -> None:
    assert routing.score_clarity(_extra(**changes)) == expected


def test_route_risks_cover_every_condition_and_clean_case() -> None:
    risky = _extra(
        foreground_background_delta=0.0,
        background_color_variance=1.0,
        border_color_variance=1.0,
        border_dominant_color_ratio=0.0,
        background_edge_density=1.0,
        binary_transition_density=0.5,
        component_count=300,
        small_component_count=100,
        threshold_variant_subject_ratios=[0.1, 0.9],
        dominant_color_count=16,
        local_color_variance=0.2,
        gradient_density=0.4,
        estimated_colorfulness=0.3,
        subject_mask_ratio=0.9,
    )
    routing.visual_direct_score_from_stats(risky)
    flags = set(routing.route_risk_flags(risky))
    assert flags >= routing.BLOCKING_VISUAL_RISKS
    assert "uncertain_binarization_risk" in flags
    clean = _extra()
    routing.visual_direct_score_from_stats(clean)
    assert routing.route_risk_flags(clean) == []


@pytest.mark.parametrize(
    ("classification", "prompt", "expected"),
    [
        ({"style_family": "technical_trace"}, "", "technical_trace"),
        ({"style_family": "other"}, "logo please", "logo_outline"),
        ({"style_family": "other"}, "plain", "unknown"),
    ],
)
def test_select_category_branches(classification: dict[str, object], prompt: str, expected: str) -> None:
    assert routing.select_category(classification, prompt) == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("preserve original", "user_requested_preserve_original"),
        ("please image-edit", "user_requested_image_edit"),
        ("plain request", None),
    ],
)
def test_route_override_branches(prompt: str, expected: str | None) -> None:
    assert routing.route_override_from_prompt(prompt) == expected


@pytest.mark.parametrize(
    "assessment",
    [
        {"semantic_style_score": True},
        {"semantic_style_score": 0.31},
        {"semantic_style_score": 0.1, "semantic_score_source": "bad"},
        {"semantic_style_score": 0.1, "semantic_rationale": [1]},
        {"semantic_style_score": 0.1, "semantic_blockers": [1]},
    ],
)
def test_semantic_assessment_rejects_each_invalid_branch(assessment: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError):
        routing.validate_semantic_assessment(assessment)


def test_image_helpers_cover_empty_alpha_and_type_checks() -> None:
    rgba = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    gray = rgba.getchannel("R")
    alpha = rgba.getchannel("A")
    with pytest.warns(DeprecationWarning):
        subject, threshold, inverted, ratios = routing.build_subject_mask(gray, alpha, has_alpha_background=True)
    assert (subject, threshold, inverted, ratios) == (set(), 0, False, [0.0])
    assert routing.border_background_stats([]) == ((255.0, 255.0, 255.0), 0.0, 1.0)
    assert routing.subject_background_delta(Image.new("RGB", (1, 1)), set(), (255.0, 255.0, 255.0)) == 0.0
    assert routing.content_bbox(set()) is None
    assert routing.bbox_area(None) == 0
    assert routing.bbox_area([1, 1, 2, 3]) == 6
    with pytest.raises(TypeError):
        routing.gray_pixel(Image.new("RGB", (1, 1)), 0, 0)
    with pytest.raises(TypeError):
        routing.rgb_pixel(Image.new("L", (1, 1)), 0, 0)


def test_classification_intent_branches_and_default_context() -> None:
    image = Image.new("RGB", (4, 4), "white")
    assert routing.classify_image(image, "portrait")["style_family"] == "portrait_line_art"
    assert routing.classify_image(image, "technical diagram")["style_family"] == "technical_trace"
    assert routing.classify_image(image, "logo")["style_family"] == "logo_outline"
    decision = routing.decide_route(
        image,
        {
            "semantic_style_score": 0.0,
            "semantic_score_source": "router_multimodal_model",
            "semantic_rationale": [],
            "semantic_blockers": [],
        },
    )
    assert decision.to_json()["job_name"] == ""


def test_remaining_route_and_image_stat_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("RGB", (4, 4), "white")
    assessment = {
        "semantic_style_score": 0.0,
        "semantic_score_source": "router_multimodal_model",
        "semantic_rationale": [],
        "semantic_blockers": [],
    }
    monkeypatch.setattr(routing, "route_risk_flags", lambda extra: ["user_requested_image_edit"])
    assert (
        routing.decide_route(image, assessment, context=routing.RouteContext(prompt="simplify")).route == "B_IMAGE_EDIT"
    )
    monkeypatch.setattr(routing, "route_risk_flags", lambda extra: ["uncertain_or_complex"])
    assert routing.decide_route(image, assessment).route == "B_IMAGE_EDIT"
    assert routing.otsu_threshold([0] * 256) == 128
    binary = Image.new("L", (3, 3), 255)
    binary.putpixel((1, 1), 0)
    assert routing.simple_edge_mask(binary).getpixel((1, 1)) == 0
    gray = Image.new("L", (2, 2), 0)
    _foreground, _threshold, _inverted, ratios = routing.build_subject_mask(
        gray, Image.new("L", (2, 2), 255), has_alpha_background=False
    )
    assert ratios == [0.0, 0.0, 0.0]
    assert routing.border_pixels(Image.new("RGB", (2, 2)), Image.new("L", (2, 2), 0)) == []

    class BadRgb:
        def getpixel(self, point: tuple[int, int]) -> tuple[object, object, object]:
            del point
            return 1, "bad", 3

    with pytest.raises(TypeError):
        routing.rgb_pixel(cast(Any, BadRgb()), 0, 0)


def test_classify_line_heavy_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = routing._ImageStats(10, 10, 1.0, 10, 0.2, 0.12, 0.1, 0.9)
    monkeypatch.setattr(routing, "image_stats", lambda image: stats)
    assert routing.classify_image(Image.new("RGB", (1, 1)))["style_family"] == "technical_trace"
