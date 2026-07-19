from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drawingmachine.application import jobs
from drawingmachine.domain.workflow import ProcessedImageReview
from drawingmachine.errors import DrawingMachineError


@pytest.mark.parametrize(
    "value",
    [
        {"recursive": None},
        {1: True},
        {"bad": object()},
        {"bad": float("nan")},
        {"bad": "\ud800"},
    ],
)
def test_submission_semantic_freezing_defensive_branches(value: dict[Any, object]) -> None:
    if set(value) == {"recursive"} and value["recursive"] is None:
        value["recursive"] = value
    with pytest.raises(DrawingMachineError):
        jobs.WorkflowSubmissionV1(1, "job", Path("/tmp/input.png"), jobs.RouteMode.DIRECT, value, None)


@pytest.mark.parametrize(
    "values",
    [
        (2, "job", Path("/tmp/input.png"), jobs.RouteMode.DIRECT, None, None),
        (1, "", Path("/tmp/input.png"), jobs.RouteMode.DIRECT, None, None),
        (1, "job", Path("relative"), jobs.RouteMode.DIRECT, None, None),
        (1, "job", Path("/tmp/input.png"), "direct", None, None),
        (1, "job", Path("/tmp/input.png"), jobs.RouteMode.DIRECT, [], None),
        (1, "job", Path("/tmp/input.png"), jobs.RouteMode.DIRECT, None, 1),
    ],
)
def test_workflow_submission_scalar_branches(values: tuple[object, ...]) -> None:
    with pytest.raises(DrawingMachineError):
        jobs.WorkflowSubmissionV1(*values)  # type: ignore[arg-type]


def test_review_continuation_scalar_branches() -> None:
    review = ProcessedImageReview.from_json(
        {
            "schema": "processed_image_review_v1",
            "job_name": "job",
            "reviewed_at": "now",
            "reviewer": "test",
            "processed_image": "image.png",
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
    for values in (
        (2, "job", review, "a" * 64),
        (1, "", review, "a" * 64),
        (1, "job", object(), "a" * 64),
        (1, "job", review, "bad"),
    ):
        with pytest.raises(DrawingMachineError):
            jobs.ReviewContinuationV1(*values)  # type: ignore[arg-type]
