from __future__ import annotations

from typing import Any

import pytest

from drawingmachine.domain.workflow import models
from drawingmachine.errors import DrawingMachineError


def _review(**changes: object) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "processed_image_review_v1",
        "job_name": "job",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "reviewer": "test",
        "processed_image": "image.png",
        "handoff": None,
        "status": "PASS_TO_BUILD",
        "checks": {name: True for name in models.REVIEW_CHECK_NAMES},
        "issues": [],
        "revised_prompt": None,
        "next_allowed_stage": "build_drawable_job",
    }
    value.update(changes)
    return value


def test_workflow_model_private_helper_branches() -> None:
    with pytest.raises(RuntimeError):
        models._load_document("[]")
    for function, value in (
        (models._object, []),
        (models._object, {1: True}),
        (models._string, 1),
        (models._string, ""),
        (models._string_list, [1]),
        (models._number, True),
    ):
        with pytest.raises(DrawingMachineError):
            function(value, "field")


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": True},
        {"reviewer": models._REVIEW_FIELDS},
        {"schema": "bad"},
        {"handoff": 1},
        {"status": "bad"},
        {"checks": {}},
        {"checks": {name: 1 for name in models.REVIEW_CHECK_NAMES}},
        {"revised_prompt": 1},
        {"next_allowed_stage": "stop"},
        {"checks": {**{name: True for name in models.REVIEW_CHECK_NAMES}, "recognizable_subject": False}},
        {"issues": ["unresolved"]},
        {"status": "REVISE_PROMPT", "next_allowed_stage": "repeat_image_edit"},
    ],
)
def test_review_parser_defensive_branches(changes: dict[str, object]) -> None:
    value = _review(**changes)
    if changes == {"reviewer": models._REVIEW_FIELDS}:
        del value["reviewer"]
    with pytest.raises(DrawingMachineError):
        models.ProcessedImageReview.from_json(value)


def test_review_optional_fields_and_digest_validation_branches() -> None:
    review = models.ProcessedImageReview.from_json(_review(handoff="handoff", revised_prompt=""))
    with pytest.raises(DrawingMachineError):
        review.validate_for(job_name="other", processed_image_sha256="a" * 64, reviewed_image_sha256="a" * 64)
    with pytest.raises(DrawingMachineError):
        review.validate_for(job_name="job", processed_image_sha256="bad", reviewed_image_sha256="a" * 64)
    with pytest.raises(DrawingMachineError):
        review.validate_for(job_name="job", processed_image_sha256="a" * 64, reviewed_image_sha256="bad")
    with pytest.raises(DrawingMachineError):
        review.validate_for(job_name="job", processed_image_sha256="a" * 64, reviewed_image_sha256="b" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"direct_score": True},
        {"route_override": 1},
    ],
)
def test_route_decision_rejects_invalid_scores_and_override(changes: dict[str, object]) -> None:
    document: dict[str, object] = {
        "route": "A_DIRECT",
        "direct_score": 1.0,
        "semantic_style_score": 0.0,
        "visual_direct_score": 1.0,
        "risk_flags": [],
        "blocking_risks": [],
        "route_override": None,
    }
    document.update(changes)
    with pytest.raises(DrawingMachineError):
        models.RouteDecision.from_document(document)
