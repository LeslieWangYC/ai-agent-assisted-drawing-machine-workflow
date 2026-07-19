from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drawingmachine.domain.fluidnc.gates import evaluate_preflight
from drawingmachine.domain.fluidnc.reports import (
    preflight_human_report,
    preflight_report,
    render_dry_run_report,
    render_preflight_report,
    render_stream_report,
    stream_progress_report,
)
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.machine.progress import MachineProgress


def test_preflight_report_and_human_projection_are_deterministic() -> None:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("[G54:96,96,188.5]", "ok"),
        errors=(),
        missing_ok_commands=(),
    )
    gate = evaluate_preflight(snapshot, require_g54=True)

    assert preflight_report(snapshot, gate) == {
        "schema_version": 1,
        "state": "Idle",
        "status_positions": {"MPos": [192.0, 192.0, 192.0], "WPos": [96.0, 96.0, 3.5], "WCO": [96.0, 96.0, 188.5]},
        "parsed_offsets": {"G54": [96.0, 96.0, 188.5]},
        "computed_wpos_from_mpos_g54": [96.0, 96.0, 3.5],
        "allow_stream": True,
        "disposition": "READY",
        "blockers": [],
        "warnings": [],
    }
    assert preflight_human_report(snapshot, gate) == (
        "FluidNC state: Idle\n"
        "MPos: 192.000,192.000,192.000\n"
        "WPos: 96.000,96.000,3.500\n"
        "WCO: 96.000,96.000,188.500\n"
        "G54: 96.000,96.000,188.500\n"
        "Gate: READY\n"
        "Blockers: none\n"
        "Warnings: none\n"
    )


def _legacy_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = {"job_name": "package-c-fixed"}
    static_result = {"checks": [{"severity": "PASS", "code": "MOTION", "message": "motion commands are supported"}]}
    static_summary = {
        "gate": "PASS",
        "draw_path_count": 1,
        "pen_lift_count": 1,
        "pen_down_count": 1,
        "draw_length_mm": 1.0,
        "travel_length_mm": 2.0,
        "max_feed_seen": 900.0,
        "bounds": {"x": [0.0, 1.0], "y": [0.0, 1.0]},
    }
    responses = [
        {
            "command": "?",
            "expect_ok": False,
            "lines": ["<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>"],
            "ok": False,
            "error": None,
            "status_line": "<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
        },
        {
            "command": "$G",
            "expect_ok": True,
            "lines": ["[GC:G0 G54 G17]", "ok"],
            "ok": True,
            "error": None,
            "status_line": None,
        },
        {
            "command": "$#",
            "expect_ok": True,
            "lines": ["[G54:96,96,188.5]", "ok"],
            "ok": True,
            "error": None,
            "status_line": None,
        },
    ]
    parsed = parse_preflight_snapshot(
        status_line=responses[0]["status_line"],
        parser_state=tuple(responses[1]["lines"]),
        offset_lines=tuple(responses[2]["lines"]),
        errors=(),
        missing_ok_commands=(),
    ).to_json()
    dry = {
        "status": "DRY_RUN_PASS",
        "automation_gate": {"allow_stream": True, "blockers": [], "warnings": []},
        "hardware_touched": False,
        "manual_supervision_required": True,
        "gcode": {"path": "fixed.gcode", "sha256_manifest": "manifest", "sha256_actual": "actual"},
        "static_summary": static_summary,
        "send_plan": {
            "streaming_mode": "line_ack",
            "raw_line_count": 3,
            "sendable_line_count": 2,
            "comment_only_line_count": 1,
            "blank_line_count": 0,
            "estimated_serial_bytes": 12,
            "max_send_line_length": 6,
        },
    }
    preflight = {
        "status": "PREFLIGHT_PASS",
        "offline_gate": {"allow_stream": True},
        "preflight_gate": {"allow_stream": True, "blockers": [], "warnings": []},
        "hardware_touched": True,
        "motion_commands_sent": False,
        "gcode_file_streamed": False,
        "gcode": {"path": "fixed.gcode"},
        "serial_log": "fixed.jsonl",
        "static_summary": static_summary,
        "controller_preflight": {"port": "fixture", "baud": 115200, "parsed": parsed, "responses": responses},
    }
    stream = {
        "status": "STREAM_CONTROLLER_ERROR",
        "offline_gate": {"allow_stream": True, "blockers": []},
        "preflight_gate": {"allow_stream": True, "blockers": []},
        "hardware_touched": True,
        "motion_commands_sent": True,
        "gcode_file_streamed": True,
        "gcode": {"path": "fixed.gcode"},
        "serial_log": "fixed-stream.jsonl",
        "static_summary": static_summary,
        "controller_preflight": {"parsed": parsed},
        "operator_hv_confirmation": {"required": True, "prompted": True, "confirmed": True, "error": None},
        "streaming": {
            "sent_line_count": 1,
            "ok_count": 1,
            "error_count": 1,
            "failed_source_line": 3,
            "failed_command": "G1 X1 Y1",
            "final_status": "<Alarm|MPos:192,192,192>",
            "failed_response": ["error:2"],
        },
        "post_run": {
            "home_z_requested": False,
            "home_z_command": "$HZ",
            "home_z_attempted": False,
            "home_z_completed": False,
            "home_z_response": [],
            "final_status_after_home": [],
            "error": None,
        },
    }
    return manifest, static_result, dry, preflight, stream


def test_legacy_report_text_matches_all_c1_differential_goldens() -> None:
    expected = json.loads(Path("tests/fixtures/package_c/golden/reports.json").read_text(encoding="utf-8"))
    manifest, static_result, dry, preflight, stream = _legacy_inputs()

    assert render_dry_run_report(manifest, dry, static_result) == expected["dry_run"]
    assert render_preflight_report(manifest, preflight, static_result) == expected["preflight"]
    assert render_stream_report(manifest, stream, static_result) == expected["stream"]


def test_legacy_report_renderers_cover_blocker_warning_and_operator_gate_text() -> None:
    manifest, static_result, dry, preflight, stream = _legacy_inputs()
    dry["automation_gate"] = {"allow_stream": False, "blockers": ["blocked"], "warnings": ["review"]}
    preflight["preflight_gate"] = {"allow_stream": False, "blockers": ["blocked"], "warnings": ["review"]}
    stream["offline_gate"] = {"allow_stream": False, "blockers": ["offline"]}
    stream["preflight_gate"] = {"allow_stream": False, "blockers": ["controller"]}
    stream["operator_hv_confirmation"] = {
        "required": True,
        "prompted": True,
        "confirmed": False,
        "error": None,
    }
    assert "`BLOCKER`: blocked" in render_dry_run_report(manifest, dry, static_result)
    assert "`WARNING`: review" in render_preflight_report(manifest, preflight, static_result)
    rendered = render_stream_report(manifest, stream, static_result)
    assert "`OFFLINE BLOCKER`: offline" in rendered
    assert "`PREFLIGHT BLOCKER`: controller" in rendered
    assert "`OPERATOR HV BLOCKER`" in rendered


def test_stream_progress_projection_preserves_c1_ack_counts_and_adds_source_evidence() -> None:
    golden = json.loads(Path("tests/fixtures/package_c/golden/stream.json").read_text(encoding="utf-8"))[
        "progress_after_ack"
    ]
    progress = MachineProgress(
        schema_version=1,
        execution_id="execution-1",
        execution_revision=8,
        total_lines=2,
        acknowledged_lines=2,
        ok_count=2,
        error_count=0,
        percent=100.0,
        last_source_line=2,
        last_command="G90",
        last_event_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    report = stream_progress_report(progress)

    assert report["acknowledged"] == golden["sent"]
    assert report["total"] == golden["total"]
    assert report["ok_count"] == golden["ok_count"]
    assert report["error_count"] == golden["error_count"]
    assert report["percent"] == golden["percent"]
    assert report["last_source_line"] == 2
    assert report["last_command"] == "G90"
