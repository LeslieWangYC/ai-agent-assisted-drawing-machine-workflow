from __future__ import annotations

import json
from typing import Any, NoReturn, cast

from drawingmachine.domain.gcode.build import build_gcode
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.gcode.preview import render_preview
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerTaskV1

from . import TaskOutput, blocked, read_inputs, succeeded, write_bundle
from ._payload_validation import validate_gcode_build_payload, validate_gcode_check_payload


def _json_bytes(value: JsonObject) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def _object_document(content: bytes, task: WorkerTaskV1) -> JsonObject:
    try:
        value: Any = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _invalid_input(task, "worker JSON input is invalid", error)
    if not isinstance(value, dict):
        _invalid_input(task, "worker JSON input must be an object")
    return cast(JsonObject, value)


def _invalid_input(task: WorkerTaskV1, message: str, cause: BaseException | None = None) -> NoReturn:
    error = DrawingMachineError(
        ErrorPayload("WORKER_INPUT_INVALID", ErrorCategory.INPUT, message, False, {}, job_id=task.job_id)
    )
    if cause is None:
        raise error
    raise error from cause


def run_gcode_build(task: WorkerTaskV1) -> WorkerOutcomeV1:
    payload = validate_gcode_build_payload(task)
    plan = _object_document(read_inputs(task, frozenset({"path_plan"}))["path_plan"], task)
    result = build_gcode(plan, payload.profile)
    preview = render_preview(result.gcode, payload.profile)
    outputs = (
        TaskOutput(
            "selected_path_plan", "selected_path_plan.json", "application/json", _json_bytes(result.selected_plan)
        ),
        TaskOutput("gcode", "drawing.gcode", "text/x.gcode", result.gcode.encode("utf-8")),
        TaskOutput(
            "gcode_build_report", "gcode_build_report.md", "text/markdown; charset=utf-8", result.report.encode("utf-8")
        ),
        TaskOutput("gcode_preview_svg", "gcode_preview.svg", "image/svg+xml", preview.svg.encode("utf-8")),
        TaskOutput(
            "gcode_preview_report", "gcode_preview_report.json", "application/json", _json_bytes(preview.report)
        ),
    )
    return succeeded(task, write_bundle(task, outputs), result.metrics)


def run_gcode_check(task: WorkerTaskV1) -> WorkerOutcomeV1:
    payload = validate_gcode_check_payload(task)
    content = read_inputs(task, frozenset({"gcode"}))["gcode"]
    try:
        gcode = content.decode("utf-8")
    except UnicodeDecodeError as error:
        _invalid_input(task, "worker G-code input is not UTF-8", error)
    result = check_candidate(
        gcode,
        profile=payload.profile,
        expected_gcode_sha256=payload.expected_gcode_sha256,
    )
    outputs = (
        TaskOutput(
            "gcode_static", "gcode_static.json", "application/json", _json_bytes(result.static_result.to_json())
        ),
        TaskOutput("send_plan", "send_plan.json", "application/json", _json_bytes(result.send_plan.to_json())),
        TaskOutput("readiness", "readiness.json", "application/json", _json_bytes(result.readiness.to_json())),
    )
    artifacts = write_bundle(task, outputs)
    if not result.readiness.allow_stream:
        return blocked(
            task,
            artifacts,
            code="WORKER_GCODE_BLOCKED",
            message="G-code readiness checks blocked the candidate",
        )
    return succeeded(task, artifacts, {"static_gate": result.static_result.gate.value})


__all__ = ["run_gcode_build", "run_gcode_check"]
