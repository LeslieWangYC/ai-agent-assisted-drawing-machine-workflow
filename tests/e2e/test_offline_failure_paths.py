from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload["data"]["job"]
    assert isinstance(job, dict)
    return job


def _submit_blocked(service: Any, image: Path, *, name: str) -> dict[str, Any]:
    submitted = service.run_json(
        "workflow",
        "run",
        "--job-name",
        name,
        "--input-image",
        str(image),
        "--route-mode",
        "direct",
    )
    return service.wait(submitted["data"]["job_id"])


def test_static_blocker_remains_fail_closed(package_b_service: Any, tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.gcode"
    unsafe.write_text("G21\nG90\nG54\nG0 Z3.5\nG91\nG1 X1 Y1 F900\n; End\n", encoding="utf-8")

    result = package_b_service.run_json("gcode", "check", str(unsafe), "--timeout-seconds", "30")

    assert result["data"]["state"] == "BLOCKED"
    assert result["data"]["static_result"]["summary"]["gate"] == "BLOCKED"


def test_review_digest_tampering_is_rejected(package_b_service: Any, golden_root: Path) -> None:
    blocked = _submit_blocked(package_b_service, golden_root / "outline.png", name="digest-tamper")
    job_id = _job(blocked)["job_id"]
    review_path = package_b_service.write_pass_review(blocked)
    processed = package_b_service.artifact(blocked, "processed_image")
    package_b_service.artifact_path(job_id, processed).write_bytes(b"tampered")

    rejected = package_b_service.run_json(
        "workflow",
        "run",
        "--resume-job",
        job_id,
        "--review-json",
        str(review_path),
        expected_returncode=1,
    )

    assert rejected["ok"] is False
    assert rejected["error"]["code"] in {"WORKFLOW_CONTRACT_INVALID", "ARTIFACT_PATH_INVALID"}
    assert _job(package_b_service.status(job_id))["state"] == "BLOCKED"


def test_blocked_job_can_be_cancelled(package_b_service: Any, golden_root: Path) -> None:
    blocked = _submit_blocked(package_b_service, golden_root / "outline.png", name="cancel-me")
    job_id = _job(blocked)["job_id"]

    accepted = package_b_service.run_json("job", "cancel", job_id)
    cancelled = package_b_service.wait(job_id)

    assert accepted["data"]["accepted"] is True
    assert _job(cancelled)["state"] == "CANCELLED"
    assert _job(cancelled)["ready_snapshot"] is None


def test_service_restart_fails_persisted_inflight_job(package_b_service: Any, golden_root: Path) -> None:
    blocked = _submit_blocked(package_b_service, golden_root / "outline.png", name="restart-failure")
    job_id = _job(blocked)["job_id"]

    package_b_service.simulate_interrupted_state(job_id, "PREPARING_IMAGE")
    restarted = package_b_service.restart()
    status = restarted.status(job_id)

    assert _job(status)["state"] == "FAILED"
    assert _job(status)["error"]["code"] == "SERVICE_RESTARTED"


def test_concurrent_status_calls_return_one_durable_projection(package_b_service: Any, golden_root: Path) -> None:
    blocked = _submit_blocked(package_b_service, golden_root / "outline.png", name="concurrent-status")
    job_id = _job(blocked)["job_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: package_b_service.status(job_id), range(32)))

    serialized = {json.dumps(_job(status), sort_keys=True) for status in statuses}
    assert len(serialized) == 1
    assert _job(statuses[0])["state"] == "BLOCKED"
