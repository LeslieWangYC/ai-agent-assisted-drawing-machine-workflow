from __future__ import annotations

import math
import re
from typing import cast

from drawingmachine.domain.gcode.models import (
    CheckSeverity,
    ReadinessResult,
    ReadinessStatus,
    SafetyCheck,
    SendPlan,
    StaticCheckResult,
    StaticGate,
)
from drawingmachine.json_types import JsonObject

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def evaluate_readiness(
    *,
    static_result: StaticCheckResult,
    send_plan: SendPlan,
    expected_gcode_sha256: str,
) -> ReadinessResult:
    checks: list[SafetyCheck] = []
    if static_result.gate is StaticGate.BLOCKED:
        messages = [check for check in static_result.checks if check.severity is CheckSeverity.BLOCKER]
        detail = "; ".join(f"{check.code}: {check.message}" for check in messages)
        checks.append(SafetyCheck(CheckSeverity.BLOCKER, "STATIC_CHECK_BLOCKED", detail))
    if not _SHA256_RE.fullmatch(expected_gcode_sha256):
        checks.append(
            SafetyCheck(CheckSeverity.BLOCKER, "GCODE_DIGEST_INVALID", "Expected G-code SHA-256 is not canonical.")
        )
    elif send_plan.actual_sha256 != expected_gcode_sha256 or static_result.actual_sha256 != expected_gcode_sha256:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_DIGEST_MISMATCH",
                "G-code digest does not match the expected manifest digest.",
            )
        )
    if static_result.raw_line_count != send_plan.raw_line_count:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_LINE_COUNT_MISMATCH",
                f"Static and send-plan raw line counts differ: {static_result.raw_line_count} != {send_plan.raw_line_count}.",
            )
        )
    if send_plan.sendable_line_count <= 0:
        checks.append(SafetyCheck(CheckSeverity.BLOCKER, "GCODE_EMPTY_SEND_PLAN", "no sendable G-code lines"))
    if send_plan.candidate_source != "stroke":
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_NON_STROKE_SOURCE",
                f"G-code candidate source is {send_plan.candidate_source!r}, not 'stroke'.",
            )
        )
    summary = static_result.summary
    draw_segments = cast(int, summary["draw_segment_count"])
    draw_length = cast(float, summary["draw_length_mm"])
    pen_downs = cast(int, summary["pen_down_count"])
    max_feed_seen = cast(float, summary["max_feed_seen"])
    if draw_segments <= 0:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_NO_DRAW_SEGMENTS",
                "pen-contact drawing candidate has zero draw segments",
            )
        )
    if not math.isfinite(draw_length) or draw_length <= 0.0:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_DRAW_LENGTH_INVALID",
                f"draw length {draw_length} is not positive finite geometry",
            )
        )
    if pen_downs <= 0:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_NO_PEN_DOWN",
                "pen-contact drawing candidate has zero pen-down events",
            )
        )
    if max_feed_seen <= 0.0 or max_feed_seen > static_result.max_feed_limit:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "GCODE_FEED_MISMATCH",
                f"max feed {max_feed_seen} is outside the positive limit {static_result.max_feed_limit}",
            )
        )
    pen_lifts = cast(int, summary["pen_lift_count"])
    lift_down_delta = pen_lifts - pen_downs
    if lift_down_delta not in (0, 1):
        checks.append(
            SafetyCheck(
                CheckSeverity.WARNING,
                "PEN_LIFT_DOWN_DELTA",
                f"pen lift/down count differs by {lift_down_delta}; review path structure",
            )
        )
    if pen_lifts > 500:
        checks.append(
            SafetyCheck(
                CheckSeverity.WARNING,
                "MANY_PEN_LIFTS",
                f"pen lift count {pen_lifts} is high; review runtime impact",
            )
        )
    status = (
        ReadinessStatus.BLOCKED
        if any(check.severity is CheckSeverity.BLOCKER for check in checks)
        else ReadinessStatus.READY
    )
    assessment: JsonObject = {
        "pen_lift_count": pen_lifts,
        "pen_down_count": pen_downs,
        "draw_path_count": cast(int, summary["draw_path_count"]),
        "lift_down_delta": lift_down_delta,
        "hard_gate": False,
    }
    return ReadinessResult(status=status, checks=tuple(checks), pen_motion_assessment=assessment)


__all__ = ["evaluate_readiness"]
