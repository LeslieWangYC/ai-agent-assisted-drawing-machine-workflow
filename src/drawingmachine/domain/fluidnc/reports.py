from __future__ import annotations

from typing import Any

from drawingmachine.domain.fluidnc.gates import GateDecision
from drawingmachine.domain.fluidnc.status import PreflightSnapshot, Vector3
from drawingmachine.domain.machine.progress import MachineProgress
from drawingmachine.json_types import JsonObject


def preflight_report(snapshot: PreflightSnapshot, gate: GateDecision) -> JsonObject:
    _exact_inputs(snapshot, gate)
    computed = snapshot.computed_wpos_from_mpos_g54
    return {
        "schema_version": 1,
        "state": snapshot.status.state_text,
        "status_positions": snapshot.status.positions_json(),
        "parsed_offsets": {item.name: list(item.position) for item in snapshot.offsets},
        "computed_wpos_from_mpos_g54": None if computed is None else list(computed),
        "allow_stream": gate.allow_stream,
        "disposition": gate.disposition.value,
        "blockers": list(gate.blockers),
        "warnings": list(gate.warnings),
    }


def preflight_human_report(snapshot: PreflightSnapshot, gate: GateDecision) -> str:
    _exact_inputs(snapshot, gate)
    return "\n".join(
        (
            f"FluidNC state: {snapshot.status.state_text}",
            f"MPos: {_vector(snapshot.status.mpos)}",
            f"WPos: {_vector(snapshot.status.wpos)}",
            f"WCO: {_vector(snapshot.status.wco)}",
            f"G54: {_vector(snapshot.g54)}",
            f"Gate: {gate.disposition.value}",
            f"Blockers: {_messages(gate.blockers)}",
            f"Warnings: {_messages(gate.warnings)}",
            "",
        )
    )


def stream_progress_report(progress: MachineProgress) -> JsonObject:
    if type(progress) is not MachineProgress:
        raise TypeError("progress must be an exact MachineProgress")
    return {
        "schema_version": progress.schema_version,
        "execution_id": progress.execution_id,
        "execution_revision": progress.execution_revision,
        "total": progress.total_lines,
        "acknowledged": progress.acknowledged_lines,
        "ok_count": progress.ok_count,
        "error_count": progress.error_count,
        "percent": progress.percent,
        "last_source_line": progress.last_source_line,
        "last_command": progress.last_command,
        "last_event_at": progress.last_event_at.isoformat(),
        "failure": None if progress.failure is None else progress.failure.to_json(),
    }


def render_dry_run_report(
    manifest: dict[str, Any],
    dry_summary: dict[str, Any],
    static_result: dict[str, Any],
) -> str:
    gate = dry_summary["automation_gate"]
    send_plan = dry_summary["send_plan"]
    static_summary = dry_summary["static_summary"]
    lines = [
        "# G-code Dry Run Report",
        "",
        "## Result",
        "",
        f"- Status: `{dry_summary['status']}`",
        f"- Allow stream: `{gate['allow_stream']}`",
        f"- Hardware touched: `{dry_summary['hardware_touched']}`",
        f"- Manual supervision required: `{dry_summary['manual_supervision_required']}`",
        "",
        "## Job",
        "",
        f"- Job name: `{manifest['job_name']}`",
        f"- G-code: `{dry_summary['gcode']['path']}`",
        f"- Manifest SHA256: `{dry_summary['gcode']['sha256_manifest']}`",
        f"- Actual SHA256: `{dry_summary['gcode']['sha256_actual']}`",
        "",
        "## Static Summary",
        "",
        f"- Static gate: `{static_summary['gate']}`",
        f"- Draw paths: `{static_summary['draw_path_count']}`",
        f"- Pen lifts: `{static_summary['pen_lift_count']}`",
        f"- Pen downs: `{static_summary['pen_down_count']}`",
        f"- Draw length: `{static_summary['draw_length_mm']} mm`",
        f"- Travel length: `{static_summary['travel_length_mm']} mm`",
        f"- Max feed seen: `{static_summary['max_feed_seen']}`",
        f"- Bounds: `{static_summary['bounds']}`",
        "",
        "## Send Plan",
        "",
        f"- Streaming mode: `{send_plan['streaming_mode']}`",
        f"- Raw lines: `{send_plan['raw_line_count']}`",
        f"- Sendable lines: `{send_plan['sendable_line_count']}`",
        f"- Comment-only lines skipped: `{send_plan['comment_only_line_count']}`",
        f"- Blank lines skipped: `{send_plan['blank_line_count']}`",
        f"- Estimated serial bytes: `{send_plan['estimated_serial_bytes']}`",
        f"- Max send line length: `{send_plan['max_send_line_length']}`",
        "",
        "## Automation Gate",
        "",
    ]
    if gate["blockers"]:
        for blocker in gate["blockers"]:
            lines.append(f"- `BLOCKER`: {blocker}")
    else:
        lines.append("- No blockers.")
    if gate["warnings"]:
        for warning in gate["warnings"]:
            lines.append(f"- `WARNING`: {warning}")
    else:
        lines.append("- No advisory warnings.")
    lines.extend(["", "## Static Checks", ""])
    for check in static_result["checks"]:
        lines.append(f"- `{check['severity']}` `{check['code']}`: {check['message']}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This was a dry run only.",
            "- No serial port was opened.",
            "- No G-code was sent to FluidNC.",
            "- Pen-up and pen-down counts are advisory unless structurally impossible.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_preflight_report(
    manifest: dict[str, Any],
    summary: dict[str, Any],
    static_result: dict[str, Any],
) -> str:
    preflight = summary.get("controller_preflight") or {}
    parsed = preflight.get("parsed") or {}
    gate = summary["preflight_gate"]
    offline_gate = summary["offline_gate"]
    static_summary = summary["static_summary"]
    lines = [
        "# FluidNC Preflight Report",
        "",
        "## Result",
        "",
        f"- Status: `{summary['status']}`",
        f"- Offline allow stream: `{offline_gate['allow_stream']}`",
        f"- Controller allow stream: `{gate['allow_stream']}`",
        f"- Hardware touched: `{summary['hardware_touched']}`",
        f"- Motion commands sent: `{summary['motion_commands_sent']}`",
        f"- G-code file streamed: `{summary['gcode_file_streamed']}`",
        "",
        "## Job",
        "",
        f"- Job name: `{manifest['job_name']}`",
        f"- G-code: `{summary['gcode']['path']}`",
        f"- Serial log: `{summary['serial_log']}`",
        "",
        "## Static Summary",
        "",
        f"- Static gate: `{static_summary['gate']}`",
        f"- Draw paths: `{static_summary['draw_path_count']}`",
        f"- Pen lifts: `{static_summary['pen_lift_count']}`",
        f"- Pen downs: `{static_summary['pen_down_count']}`",
        f"- Max feed seen: `{static_summary['max_feed_seen']}`",
        f"- Bounds: `{static_summary['bounds']}`",
        "",
        "## Controller",
        "",
        f"- Port: `{preflight.get('port')}`",
        f"- Baud: `{preflight.get('baud')}`",
        f"- Status line: `{parsed.get('status_line')}`",
        f"- State: `{parsed.get('state')}`",
        f"- Status positions: `{parsed.get('status_positions')}`",
        f"- Parsed offsets: `{parsed.get('parsed_offsets')}`",
        f"- Computed WPos from MPos-G54: `{parsed.get('computed_wpos_from_mpos_g54')}`",
        f"- Has G54 offset line: `{parsed.get('has_g54_offset')}`",
        "",
        "## Preflight Gate",
        "",
    ]
    if gate["blockers"]:
        for blocker in gate["blockers"]:
            lines.append(f"- `BLOCKER`: {blocker}")
    else:
        lines.append("- No controller blockers.")
    if gate["warnings"]:
        for warning in gate["warnings"]:
            lines.append(f"- `WARNING`: {warning}")
    else:
        lines.append("- No controller warnings.")
    lines.extend(["", "## Responses", ""])
    for response in preflight.get("responses", []):
        lines.append(f"- `{response['command']}`: `{response['lines']}`")
    lines.extend(["", "## Static Checks", ""])
    for check in static_result["checks"]:
        lines.append(f"- `{check['severity']}` `{check['code']}`: {check['message']}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This preflight opened the serial port and sent only read-only commands.",
            "- No G-code file lines were streamed.",
            "- No motion commands were sent by this script.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_stream_report(
    manifest: dict[str, Any],
    summary: dict[str, Any],
    static_result: dict[str, Any],
) -> str:
    static_summary = summary.get("static_summary", {})
    offline_gate = summary.get("offline_gate", {})
    preflight_gate = summary.get("preflight_gate") or {}
    preflight = summary.get("controller_preflight") or {}
    parsed_preflight = preflight.get("parsed") or {}
    operator_hv = summary.get("operator_hv_confirmation") or {}
    streaming = summary.get("streaming") or {}
    post_run = summary.get("post_run") or {}
    lines = [
        "# FluidNC Stream Report",
        "",
        "## Result",
        "",
        f"- Status: `{summary['status']}`",
        f"- Offline allow stream: `{offline_gate.get('allow_stream')}`",
        f"- Controller allow stream: `{preflight_gate.get('allow_stream')}`",
        f"- Hardware touched: `{summary.get('hardware_touched')}`",
        f"- Motion commands sent: `{summary.get('motion_commands_sent')}`",
        f"- G-code file streamed: `{summary.get('gcode_file_streamed')}`",
        "",
        "## Job",
        "",
        f"- Job name: `{manifest['job_name']}`",
        f"- G-code: `{summary.get('gcode', {}).get('path')}`",
        f"- Serial log: `{summary.get('serial_log')}`",
        "",
        "## Static Summary",
        "",
        f"- Static gate: `{static_summary.get('gate')}`",
        f"- Draw paths: `{static_summary.get('draw_path_count')}`",
        f"- Pen lifts: `{static_summary.get('pen_lift_count')}`",
        f"- Pen downs: `{static_summary.get('pen_down_count')}`",
        f"- Max feed seen: `{static_summary.get('max_feed_seen')}`",
        f"- Bounds: `{static_summary.get('bounds')}`",
        "",
        "## Controller Preflight",
        "",
        f"- Status line: `{parsed_preflight.get('status_line')}`",
        f"- State: `{parsed_preflight.get('state')}`",
        f"- Status positions: `{parsed_preflight.get('status_positions')}`",
        f"- Parsed offsets: `{parsed_preflight.get('parsed_offsets')}`",
        f"- Computed WPos from MPos-G54: `{parsed_preflight.get('computed_wpos_from_mpos_g54')}`",
        "",
        "## Operator HV Confirmation",
        "",
        f"- Required: `{operator_hv.get('required')}`",
        f"- Prompted: `{operator_hv.get('prompted')}`",
        f"- Confirmed: `{operator_hv.get('confirmed')}`",
        f"- Error: `{operator_hv.get('error')}`",
        "",
        "## Streaming",
        "",
        f"- Sent lines: `{streaming.get('sent_line_count')}`",
        f"- OK count: `{streaming.get('ok_count')}`",
        f"- Error count: `{streaming.get('error_count')}`",
        f"- Failed source line: `{streaming.get('failed_source_line')}`",
        f"- Failed command: `{streaming.get('failed_command')}`",
        f"- Final status before post-run: `{streaming.get('final_status')}`",
        "",
        "## Post Run",
        "",
        f"- Home Z requested: `{post_run.get('home_z_requested')}`",
        f"- Home Z command: `{post_run.get('home_z_command')}`",
        f"- Home Z attempted: `{post_run.get('home_z_attempted')}`",
        f"- Home Z completed: `{post_run.get('home_z_completed')}`",
        f"- Home Z response: `{post_run.get('home_z_response')}`",
        f"- Final status after home: `{post_run.get('final_status_after_home')}`",
        f"- Error: `{post_run.get('error')}`",
        "",
        "## Gates",
        "",
    ]
    if offline_gate.get("blockers"):
        for blocker in offline_gate["blockers"]:
            lines.append(f"- `OFFLINE BLOCKER`: {blocker}")
    if preflight_gate.get("blockers"):
        for blocker in preflight_gate["blockers"]:
            lines.append(f"- `PREFLIGHT BLOCKER`: {blocker}")
    if operator_hv.get("required") and not operator_hv.get("confirmed"):
        lines.append("- `OPERATOR HV BLOCKER`: high-voltage confirmation was not completed")
    if (
        not offline_gate.get("blockers")
        and not preflight_gate.get("blockers")
        and not (operator_hv.get("required") and not operator_hv.get("confirmed"))
    ):
        lines.append("- No gate blockers.")
    if streaming.get("failed_response"):
        lines.append(f"- `STREAM FAILURE RESPONSE`: {streaming['failed_response']}")
    lines.extend(["", "## Static Checks", ""])
    for check in static_result["checks"]:
        lines.append(f"- `{check['severity']}` `{check['code']}`: {check['message']}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Conservative streaming mode sends one line and waits for controller response before sending the next line.",
            "- Post-run Z homing is an execution-layer action and is not embedded into the G-code file.",
            "- Do not auto-unlock alarms; inspect the machine first.",
        ]
    )
    return "\n".join(lines) + "\n"


def _exact_inputs(snapshot: PreflightSnapshot, gate: GateDecision) -> None:
    if type(snapshot) is not PreflightSnapshot:
        raise TypeError("snapshot must be an exact PreflightSnapshot")
    if type(gate) is not GateDecision:
        raise TypeError("gate must be an exact GateDecision")


def _vector(value: Vector3 | None) -> str:
    if value is None:
        return "not observed"
    return ",".join(f"{item:.3f}" for item in value)


def _messages(value: tuple[str, ...]) -> str:
    return "none" if not value else "; ".join(value)


__all__ = [
    "preflight_human_report",
    "preflight_report",
    "render_dry_run_report",
    "render_preflight_report",
    "render_stream_report",
    "stream_progress_report",
]
