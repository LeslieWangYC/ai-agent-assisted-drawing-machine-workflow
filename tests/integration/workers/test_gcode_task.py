from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

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
GCODE: JsonObject = {
    "hardware_canvas_width_mm": 144.0,
    "hardware_canvas_height_mm": 144.0,
    "machine_width_mm": 192.0,
    "machine_height_mm": 192.0,
    "paper_center_x": 96.0,
    "paper_center_y": 96.0,
    "pen_up_z": 3.5,
    "pen_down_z": 0.0,
    "feed_travel": 1200.0,
    "feed_draw": 900.0,
    "feed_pen_down": 100.0,
    "feed_pen_up": 400.0,
    "max_feed": 1200.0,
    "work_coordinate": "G54",
    "align_mode": "center",
    "mirror_y": True,
    "safe_start": True,
    "path_mode": "stroke",
}
PROFILE: JsonObject = {"schema_version": 1, "profile": {"name": "stage1", "planning": PLANNING, "gcode": GCODE}}


def make_task(tmp_path: Path, kind: WorkerKind, source: Path, role: str, payload: JsonObject) -> WorkerTaskV1:
    content = source.read_bytes()
    staging = tmp_path / f"staging-{kind.value.replace('.', '-')}"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        f"task-{kind.value.replace('.', '-')}",
        "job-1",
        5,
        f"attempt-{kind.value.replace('.', '-')}",
        kind,
        (
            WorkerInputArtifact(
                role,
                str(source.resolve()),
                hashlib.sha256(content).hexdigest(),
                len(content),
                "application/json" if role == "path_plan" else "text/x.gcode",
            ),
        ),
        {"machine": "a" * 64},
        str(staging.resolve()),
        payload,
    )


def test_gcode_build_emits_validated_bundle(tmp_path: Path) -> None:
    selected = make_task(
        tmp_path,
        WorkerKind.GCODE_BUILD,
        GOLDEN / "expected/filled_region.path_plan.json",
        "path_plan",
        {"machine_build_profile": PROFILE},
    )
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert {artifact.role for artifact in outcome.artifacts} == {
        "selected_path_plan",
        "gcode",
        "gcode_build_report",
        "gcode_preview_svg",
        "gcode_preview_report",
    }
    assert {path.name for path in Path(selected.staging_dir).iterdir()} == {
        artifact.relative_path for artifact in outcome.artifacts
    }


def test_gcode_check_emits_static_send_plan_and_readiness(tmp_path: Path) -> None:
    source = GOLDEN / "expected/drawing.gcode"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    selected = make_task(
        tmp_path,
        WorkerKind.GCODE_CHECK,
        source,
        "gcode",
        {"machine_build_profile": PROFILE, "expected_gcode_sha256": digest},
    )
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert {artifact.role for artifact in outcome.artifacts} == {"gcode_static", "send_plan", "readiness"}
    readiness = next(artifact for artifact in outcome.artifacts if artifact.role == "readiness")
    document = json.loads((Path(selected.staging_dir) / readiness.relative_path).read_text(encoding="utf-8"))
    assert document["allow_stream"] is True


@pytest.mark.parametrize("contents", [b"[]", b"\xff"], ids=["non-object", "non-utf8"])
def test_gcode_build_rejects_invalid_path_plan_documents(tmp_path: Path, contents: bytes) -> None:
    source = tmp_path / "invalid-path-plan.json"
    source.write_bytes(contents)
    selected = make_task(
        tmp_path,
        WorkerKind.GCODE_BUILD,
        source,
        "path_plan",
        {"machine_build_profile": PROFILE},
    )

    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "WORKER_INPUT_INVALID"
    assert tuple(Path(selected.staging_dir).iterdir()) == ()
