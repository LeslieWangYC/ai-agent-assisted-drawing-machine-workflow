import json
from pathlib import Path

from PIL import Image

from drawingmachine.domain.workflow import RouteContext, decide_route

GOLDEN_ROOT = Path("tests/fixtures/package_b/golden")


def semantic(score: float) -> dict[str, object]:
    return {
        "semantic_style_score": score,
        "semantic_score_source": "router_multimodal_model",
        "semantic_rationale": ["fixture assessment"],
        "semantic_blockers": [],
    }


def context(*, prompt: str = "") -> RouteContext:
    return RouteContext(
        job_name="package-b-golden",
        input_image="<FIXTURE_ROOT>/outline.png",
        prompt=prompt,
        decided_at="<CAPTURED_AT>",
    )


def test_new_route_matches_stage1_golden() -> None:
    assessment = json.loads((GOLDEN_ROOT / "route_semantic.json").read_text(encoding="utf-8"))
    expected = json.loads((GOLDEN_ROOT / "expected/route.json").read_text(encoding="utf-8"))

    with Image.open(GOLDEN_ROOT / "outline.png") as image:
        result = decide_route(image, assessment, context=context())

    assert result.to_json() == expected


def test_route_threshold_is_exactly_point_eight() -> None:
    with Image.open(GOLDEN_ROOT / "outline.png") as image:
        accepted = decide_route(image, semantic(0.17), context=context())
    with Image.open(GOLDEN_ROOT / "outline.png") as image:
        rejected = decide_route(image, semantic(0.1699), context=context())

    assert accepted.direct_score == 0.8
    assert accepted.route == "A_DIRECT"
    assert rejected.direct_score == 0.7999
    assert rejected.route == "B_IMAGE_EDIT"


def test_direct_score_is_exact_sum_of_semantic_and_visual_scores() -> None:
    with Image.open(GOLDEN_ROOT / "outline.png") as image:
        result = decide_route(image, semantic(0.1234), context=context())

    assert result.direct_score == round(result.semantic_style_score + result.visual_direct_score, 4)


def test_prompt_overrides_preserve_or_simplify() -> None:
    uncertain = Image.new("L", (100, 100), 255)
    uncertain.paste(0, (0, 0, 50, 100))

    preserved = decide_route(
        uncertain,
        semantic(0.0),
        context=context(prompt="preserve original and do not simplify"),
    )
    simplified = decide_route(
        uncertain,
        semantic(0.3),
        context=context(prompt="please simplify and redraw"),
    )

    assert preserved.route == "A_DIRECT"
    assert preserved.route_override == "user_requested_preserve_original"
    assert "user_forced_direct_route" in preserved.risk_flags
    assert simplified.route == "B_IMAGE_EDIT"
    assert simplified.route_override == "user_requested_image_edit"


def test_uncertain_binarization_and_blocking_risks_prevent_direct_route() -> None:
    uncertain = Image.new("L", (100, 100), 255)
    uncertain.paste(0, (0, 0, 50, 100))

    result = decide_route(uncertain, semantic(0.3), context=context())

    assert result.route == "B_IMAGE_EDIT"
    assert "uncertain_binarization_risk" in result.risk_flags
    assert "unstable_subject_mask" in result.blocking_risks
