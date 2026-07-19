import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from drawingmachine.application.jobs import ReviewContinuationV1, RouteMode, WorkflowSubmissionV1
from drawingmachine.domain.workflow import ProcessedImageReview


def _review() -> ProcessedImageReview:
    return ProcessedImageReview.from_json(
        {
            "schema": "processed_image_review_v1",
            "job_name": "job",
            "reviewed_at": "2026-07-11T00:00:00+00:00",
            "reviewer": "test",
            "processed_image": "processed_image.png",
            "handoff": None,
            "status": "PASS_TO_BUILD",
            "checks": {
                "recognizable_subject": True,
                "background_simplified": True,
                "black_on_white": True,
                "no_edge_clipping": True,
                "line_density_drawable": True,
                "no_soft_gradients": True,
                "limited_tiny_isolated_marks": True,
                "vectorization_complexity_ok": True,
            },
            "issues": [],
            "revised_prompt": None,
            "next_allowed_stage": "build_drawable_job",
        }
    )


def test_submission_and_continuation_are_frozen_contracts() -> None:
    submission = WorkflowSubmissionV1(1, "job", Path("/tmp/input.png"), RouteMode.DIRECT, None, None)
    continuation = ReviewContinuationV1(1, "job-1", _review(), "0" * 64)

    with pytest.raises(FrozenInstanceError):
        submission.job_name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        continuation.job_id = "changed"  # type: ignore[misc]


def test_route_modes_cover_explicit_and_automatic_routing() -> None:
    assert {mode.value for mode in RouteMode} == {"auto", "direct", "image-edit"}


@pytest.mark.parametrize(
    "assessment",
    [
        {"value": math.nan},
        {1: "non-string"},
        {"value": object()},
    ],
)
def test_submission_rejects_non_json_semantic_assessment(assessment: object) -> None:
    with pytest.raises(Exception, match="semantic_assessment"):
        WorkflowSubmissionV1(1, "job", Path("/tmp/input.png"), RouteMode.DIRECT, assessment, None)  # type: ignore[arg-type]


def test_submission_rejects_recursive_semantic_assessment() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(Exception, match="semantic_assessment"):
        WorkflowSubmissionV1(1, "job", Path("/tmp/input.png"), RouteMode.DIRECT, recursive, None)  # type: ignore[arg-type]


def test_submission_rejects_recursive_semantic_array() -> None:
    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(Exception, match="recursive array"):
        WorkflowSubmissionV1(
            1,
            "job",
            Path("/tmp/input.png"),
            RouteMode.DIRECT,
            {"recursive": recursive},
            None,
        )


def test_submission_deep_freezes_nested_semantic_assessment() -> None:
    nested = {"outer": {"values": [1, {"name": "original"}]}}
    submission = WorkflowSubmissionV1(1, "job", Path("/tmp/input.png"), RouteMode.DIRECT, nested, None)
    nested["outer"]["values"][1]["name"] = "mutated"  # type: ignore[index]
    assert submission.semantic_assessment is not None
    assert submission.semantic_assessment["outer"]["values"][1]["name"] == "original"  # type: ignore[index]
    with pytest.raises(TypeError):
        submission.semantic_assessment["outer"]["values"][1]["name"] = "blocked"  # type: ignore[index]


def test_semantic_assessment_immutable_values_do_not_inherit_mutable_builtins() -> None:
    submission = WorkflowSubmissionV1(
        1,
        "job",
        Path("/tmp/input.png"),
        RouteMode.DIRECT,
        {"outer": {"values": [1, {"name": "original"}]}},
        None,
    )
    assert submission.semantic_assessment is not None
    outer = submission.semantic_assessment["outer"]
    values = outer["values"]  # type: ignore[index]
    assert not isinstance(submission.semantic_assessment, dict)
    assert not isinstance(outer, dict)
    assert not isinstance(values, list)
    with pytest.raises(TypeError):
        dict.__setitem__(submission.semantic_assessment, "escape", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list.append(values, "escape")  # type: ignore[arg-type]
