from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from drawingmachine.domain.planning.outputs import render_planning_artifacts
from drawingmachine.domain.planning.service import build_path_plan
from drawingmachine.domain.workflow import analyze_complexity, apply_single_dedupe_profile
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerTaskV1

from . import TaskOutput, blocked, open_bounded_image, read_inputs, succeeded, write_bundle
from ._payload_validation import validate_planning_payload


def _json_bytes(value: JsonObject) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def run_path_planning(task: WorkerTaskV1) -> WorkerOutcomeV1:
    payload = validate_planning_payload(task)
    content = read_inputs(task, frozenset({"processed_image"}))["processed_image"]
    image = open_bounded_image(task, content)
    complexity = analyze_complexity(image, payload.complexity_limits)
    complexity_output = TaskOutput(
        "complexity_report",
        "complexity_report.json",
        "application/json",
        _json_bytes(complexity.to_json()),
    )
    if complexity.status == "REJECT_COMPLEXITY":
        artifacts = write_bundle(task, (complexity_output,))
        return blocked(
            task,
            artifacts,
            code="WORKER_COMPLEXITY_BLOCKED",
            message="processed image complexity blocks path planning",
        )
    selected = apply_single_dedupe_profile(payload.base_dedupe_profile, complexity)
    config = replace(
        payload.config,
        dedupe_short_path_length_mm=selected.dedupe_short_path_length_mm,
        dedupe_distance_mm=selected.dedupe_distance_mm,
        dedupe_angle_deg=selected.dedupe_angle_deg,
        dedupe_overlap_ratio=selected.dedupe_overlap_ratio,
    )
    result = build_path_plan(image, source_name=Path(task.input_artifacts[0].absolute_path).name, config=config)
    rendered = {artifact.role: artifact for artifact in render_planning_artifacts(result)}
    outputs = (
        complexity_output,
        TaskOutput("path_plan", "path_plan.json", "application/json", rendered["path_plan"].content),
        TaskOutput(
            "preview_stroke_svg",
            "preview_stroke.svg",
            "image/svg+xml",
            rendered["preview_stroke_only_svg"].content,
        ),
        TaskOutput(
            "preview_final_svg",
            "preview_final.svg",
            "image/svg+xml",
            rendered["preview_final_svg"].content,
        ),
        TaskOutput(
            "planning_report",
            "planning_report.md",
            "text/markdown; charset=utf-8",
            rendered["report_md"].content,
        ),
    )
    return succeeded(task, write_bundle(task, outputs), {"complexity_status": complexity.status})


__all__ = ["run_path_planning"]
