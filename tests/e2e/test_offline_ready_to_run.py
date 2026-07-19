from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload["data"]["job"]
    assert isinstance(job, dict)
    return job


def test_fixed_image_reaches_immutable_ready_to_run(
    installed_drawingmachine: Path,
    package_b_service: Any,
    golden_root: Path,
) -> None:
    assert installed_drawingmachine == package_b_service.executable
    submitted = package_b_service.run_json(
        "workflow",
        "run",
        "--job-name",
        "offline-acceptance",
        "--input-image",
        str(golden_root / "filled_region.png"),
        "--route-mode",
        "direct",
    )
    job_id = submitted["data"]["job_id"]

    blocked = package_b_service.wait(job_id)
    assert _job(blocked)["state"] == "BLOCKED"
    assert _job(blocked)["blocker"]["code"] == "REVIEW_REQUIRED"

    review_path = package_b_service.write_pass_review(blocked)
    resumed = package_b_service.run_json(
        "workflow",
        "run",
        "--resume-job",
        job_id,
        "--review-json",
        str(review_path),
    )
    assert resumed["data"]["state"] == "IMAGE_READY"

    final = package_b_service.wait(job_id)
    final_job = _job(final)
    assert final_job["state"] == "READY_TO_RUN"
    snapshot = final_job["ready_snapshot"]
    assert snapshot["job_id"] == job_id
    assert snapshot["job_revision"] == final_job["revision"]
    gcode = snapshot["gcode"]
    contents = package_b_service.artifact_path(job_id, gcode).read_bytes()
    assert gcode["sha256"] == hashlib.sha256(contents).hexdigest()
    assert gcode["size_bytes"] == len(contents)
    assert package_b_service.forbidden_hardware_keys(snapshot) == set()

    repeated = package_b_service.status(job_id)
    assert json.dumps(_job(repeated)["ready_snapshot"], sort_keys=True) == json.dumps(snapshot, sort_keys=True)


def test_same_name_jobs_have_isolated_artifacts(package_b_service: Any, golden_root: Path) -> None:
    blocked: list[dict[str, Any]] = []
    for _ in range(2):
        submitted = package_b_service.run_json(
            "workflow",
            "run",
            "--job-name",
            "same-name",
            "--input-image",
            str(golden_root / "outline.png"),
            "--route-mode",
            "direct",
        )
        blocked.append(package_b_service.wait(submitted["data"]["job_id"]))

    job_ids = [_job(payload)["job_id"] for payload in blocked]
    assert len(set(job_ids)) == 2
    processed = [package_b_service.artifact(payload, "processed_image") for payload in blocked]
    paths = [
        package_b_service.artifact_path(job_id, artifact) for job_id, artifact in zip(job_ids, processed, strict=True)
    ]
    assert paths[0] != paths[1]
    assert paths[0].is_relative_to(package_b_service.jobs_dir / job_ids[0])
    assert paths[1].is_relative_to(package_b_service.jobs_dir / job_ids[1])
    assert paths[0].read_bytes() == paths[1].read_bytes()
