from __future__ import annotations

import hashlib
from pathlib import Path

from drawingmachine.adapters.workers.worker_entrypoint import execute_task
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import WorkerInputArtifact, WorkerKind, WorkerOutcomeV1, WorkerStatus, WorkerTaskV1

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures/package_b/golden"
PLANNING: JsonObject = {
    "canvas_width_mm": 120.0,
    "canvas_height_mm": 120.0,
    "pen_width_mm": 0.5,
    "min_gap_mm": 0.8,
    "threshold": None,
    "invert": None,
    "min_component_area_px": 8,
    "simplify_tolerance_mm": 0.12,
    "min_path_length_mm": 0.6,
    "drop_short_stroke_mm": 0.35,
    "merge_endpoint_distance_mm": 0.45,
    "merge_angle_deg": 35.0,
    "dedupe_short_path_length_mm": 2.0,
    "dedupe_distance_mm": 0.3,
    "dedupe_angle_deg": 25.0,
    "dedupe_overlap_ratio": 0.65,
    "hatch_spacing_mm": 0.8,
    "hatch_min_run_mm": 0.8,
    "fill_min_thickness_mm": 0.85,
}
LIMITS: JsonObject = {
    "soft_component_count": 160,
    "soft_skeleton_pixels": 26000,
    "soft_boundary_pixels": 45000,
    "soft_transitions_total": 68000,
    "hard_component_count": 260,
    "hard_skeleton_pixels": 34000,
    "hard_boundary_pixels": 60000,
    "hard_transitions_total": 100000,
    "threshold": None,
    "invert": None,
    "min_component_area_px": 8,
}
DEDUPE: JsonObject = {
    key: PLANNING[key]
    for key in ("dedupe_short_path_length_mm", "dedupe_distance_mm", "dedupe_angle_deg", "dedupe_overlap_ratio")
}


def planning_task(tmp_path: Path, *, limits: JsonObject = LIMITS) -> WorkerTaskV1:
    source = GOLDEN / "outline.png"
    content = source.read_bytes()
    staging = tmp_path / "planning-staging"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        "task-planning",
        "job-1",
        3,
        "attempt-planning",
        WorkerKind.PATHS_PLAN,
        (
            WorkerInputArtifact(
                "processed_image", str(source.resolve()), hashlib.sha256(content).hexdigest(), len(content), "image/png"
            ),
        ),
        {"machine": "a" * 64},
        str(staging.resolve()),
        {
            "planning_profile": {"schema_version": 1, "profile": {"name": "stage1", "planning": dict(PLANNING)}},
            "complexity_limits": limits,
            "base_dedupe_profile": dict(DEDUPE),
        },
    )


def test_planning_worker_writes_only_declared_staging_files(tmp_path: Path) -> None:
    selected = planning_task(tmp_path)
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))
    assert outcome.status is WorkerStatus.SUCCEEDED
    assert {artifact.role for artifact in outcome.artifacts} == {
        "complexity_report",
        "path_plan",
        "preview_stroke_svg",
        "preview_final_svg",
        "planning_report",
    }
    assert {path.name for path in Path(selected.staging_dir).iterdir()} == {
        artifact.relative_path for artifact in outcome.artifacts
    }
    for artifact in outcome.artifacts:
        content = (Path(selected.staging_dir) / artifact.relative_path).read_bytes()
        assert artifact.sha256 == hashlib.sha256(content).hexdigest()
        assert artifact.size_bytes == len(content)


def test_complexity_blocker_stops_before_path_planning(tmp_path: Path) -> None:
    limits = dict(LIMITS)
    limits.update(
        {"hard_component_count": 0, "hard_skeleton_pixels": 0, "hard_boundary_pixels": 0, "hard_transitions_total": 0}
    )
    selected = planning_task(tmp_path, limits=limits)
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.BLOCKED
    assert [artifact.role for artifact in outcome.artifacts] == ["complexity_report"]
    assert outcome.error is not None and outcome.error.code == "WORKER_COMPLEXITY_BLOCKED"
