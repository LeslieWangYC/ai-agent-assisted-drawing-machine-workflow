import json
from pathlib import Path

import pytest

from drawingmachine.domain.workflow import parse_review
from drawingmachine.errors import DrawingMachineError

GOLDEN_REVIEW = Path("tests/fixtures/package_b/golden/review_pass.json")


def test_review_round_trip_preserves_stage1_schema() -> None:
    payload = json.loads(GOLDEN_REVIEW.read_text(encoding="utf-8"))

    review = parse_review(payload)

    assert review.to_json() == payload
    assert review.status == "PASS_TO_BUILD"
    assert review.next_allowed_stage == "build_drawable_job"


def test_review_binds_job_and_processed_image_digest() -> None:
    review = parse_review(json.loads(GOLDEN_REVIEW.read_text(encoding="utf-8")))
    review.validate_for(
        job_name="package-b-golden",
        processed_image_sha256="b" * 64,
        reviewed_image_sha256="b" * 64,
    )

    with pytest.raises(DrawingMachineError, match="digest"):
        review.validate_for(
            job_name="package-b-golden",
            processed_image_sha256="b" * 64,
            reviewed_image_sha256="c" * 64,
        )
    with pytest.raises(DrawingMachineError, match="job_name"):
        review.validate_for(
            job_name="different-job",
            processed_image_sha256="b" * 64,
            reviewed_image_sha256="b" * 64,
        )


def test_pass_review_rejects_failed_checks_or_issues() -> None:
    payload = json.loads(GOLDEN_REVIEW.read_text(encoding="utf-8"))
    payload["checks"]["black_on_white"] = False

    with pytest.raises(DrawingMachineError, match="failed checks"):
        parse_review(payload)

    payload = json.loads(GOLDEN_REVIEW.read_text(encoding="utf-8"))
    payload["issues"] = ["gradient remains"]
    with pytest.raises(DrawingMachineError, match="issues"):
        parse_review(payload)
