from __future__ import annotations

from pathlib import Path
from typing import Any


def test_workers_do_not_write_repository(package_b_service: Any, golden_root: Path, repository_root: Path) -> None:
    before = package_b_service.repository_snapshot(repository_root)
    submitted = package_b_service.run_json(
        "workflow",
        "run",
        "--job-name",
        "repository-isolation",
        "--input-image",
        str(golden_root / "outline.png"),
        "--route-mode",
        "direct",
    )
    package_b_service.wait(submitted["data"]["job_id"])
    after = package_b_service.repository_snapshot(repository_root)

    assert after == before


def test_installed_direct_workflow_has_no_live_boundaries(package_b_guarded_service: Any, golden_root: Path) -> None:
    service = package_b_guarded_service
    submitted = service.run_json(
        "workflow",
        "run",
        "--job-name",
        "no-live-boundaries",
        "--input-image",
        str(golden_root / "outline.png"),
        "--route-mode",
        "direct",
    )
    blocked = service.wait(submitted["data"]["job_id"])

    assert blocked["data"]["job"]["blocker"]["code"] == "REVIEW_REQUIRED"
    assert service.socket_path.is_relative_to(service.runtime_root)
    assert service.forbidden_events() == []


def test_worker_and_service_sources_have_no_package_b_live_authority(repository_root: Path) -> None:
    protected = (
        repository_root / "src/drawingmachine/adapters/workers",
        repository_root / "src/drawingmachine/application/offline_job_chain.py",
        repository_root / "src/drawingmachine/service/job_coordinator.py",
    )
    forbidden = ("import serial", "from serial", "import openclaw", "from openclaw", "fluidnc", "hardware_bridge")
    matches: list[str] = []
    for root in protected:
        paths = root.rglob("*.py") if root.is_dir() else (root,)
        for path in paths:
            lowered = path.read_text(encoding="utf-8").lower()
            matches.extend(f"{path.relative_to(repository_root)}:{token}" for token in forbidden if token in lowered)
    assert matches == []
