from __future__ import annotations

import hashlib
import re
from dataclasses import FrozenInstanceError, replace

import pytest

from drawingmachine.application.gcode.check_candidate import check_candidate
from drawingmachine.domain.gcode.models import (
    CheckSeverity,
    MachineBuildProfile,
    ReadinessResult,
    ReadinessStatus,
    SafetyCheck,
    StaticCheckOptions,
    StaticCheckResult,
    StaticGate,
)
from drawingmachine.domain.gcode.readiness import evaluate_readiness
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.static_check import analyze_gcode
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject

SAFE_GCODE = """; Path mode: stroke (fill_paths + stroke_paths + fill_boundary_paths)
G21
G90
G54
G1 Z3.500 F400.0
G0 X96.000 Y96.000 F1200.0
G1 Z0.000 F100.0
G1 X97.000 Y97.000 F900.0
G1 Z3.500 F400.0
; End
"""


def options() -> StaticCheckOptions:
    return StaticCheckOptions.stage1_defaults(input_name="candidate.gcode")


def passing_inputs() -> tuple[object, object, str]:
    static_result = analyze_gcode(SAFE_GCODE, options())
    send_plan = build_send_plan(SAFE_GCODE)
    return static_result, send_plan, hashlib.sha256(SAFE_GCODE.encode()).hexdigest()


def with_summary(static_result: StaticCheckResult, summary: JsonObject) -> StaticCheckResult:
    return StaticCheckResult(
        summary=summary,
        checks=static_result.checks,
        raw_line_count=static_result.raw_line_count,
        actual_sha256=static_result.actual_sha256,
        max_feed_limit=static_result.max_feed_limit,
    )


def test_send_plan_counts_and_digest_are_deterministic_and_frozen() -> None:
    gcode = "; comment\n\nG21 ; units\nG90\n"
    result = build_send_plan(gcode)

    assert result.raw_line_count == 4
    assert result.blank_line_count == 1
    assert result.comment_only_line_count == 1
    assert result.sendable_line_count == 2
    assert result.estimated_serial_bytes == len("G21") + 1 + len("G90") + 1
    assert result.first_sendable_lines == result.last_sendable_lines
    assert result.actual_sha256 == hashlib.sha256(gcode.encode()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        result.raw_line_count = 0


def test_send_plan_rejects_non_stroke_candidate_source_fail_closed() -> None:
    gcode = SAFE_GCODE.replace("Path mode: stroke", "Path mode: preview")

    result = build_send_plan(gcode)

    assert result.candidate_source == "preview"


def test_manifest_digest_mismatch_blocks_readiness() -> None:
    static_result, send_plan, _ = passing_inputs()
    result = evaluate_readiness(
        static_result=static_result,  # type: ignore[arg-type]
        send_plan=send_plan,  # type: ignore[arg-type]
        expected_gcode_sha256="b" * 64,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert [check.code for check in result.checks] == ["GCODE_DIGEST_MISMATCH"]


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64, "A" * 64])
def test_invalid_expected_digest_blocks_readiness(digest: str) -> None:
    static_result, send_plan, _ = passing_inputs()

    result = evaluate_readiness(
        static_result=static_result,  # type: ignore[arg-type]
        send_plan=send_plan,  # type: ignore[arg-type]
        expected_gcode_sha256=digest,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert [check.code for check in result.checks] == ["GCODE_DIGEST_INVALID"]


def test_static_blocker_blocks_readiness() -> None:
    static_result = analyze_gcode(SAFE_GCODE.replace("G1 X97.000", "G2 X97.000"), options())
    send_plan = build_send_plan(SAFE_GCODE.replace("G1 X97.000", "G2 X97.000"))

    result = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=send_plan.actual_sha256,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert any(check.code == "STATIC_CHECK_BLOCKED" for check in result.checks)


def test_static_result_safety_summary_is_immutable_and_gate_is_derived_from_checks() -> None:
    unsafe_gcode = SAFE_GCODE.replace("G1 X97.000", "G2 X97.000")
    analyzed = analyze_gcode(unsafe_gcode, options())
    supplied_summary = analyzed.summary
    supplied_summary.update(
        {
            "gate": StaticGate.PASS_STATIC.value,
            "draw_segment_count": 1,
            "pen_down_count": 1,
            "max_feed_seen": 900.0,
        }
    )
    result = StaticCheckResult(
        summary=supplied_summary,
        checks=analyzed.checks,
        raw_line_count=analyzed.raw_line_count,
        actual_sha256=analyzed.actual_sha256,
        max_feed_limit=analyzed.max_feed_limit,
    )
    supplied_summary["gate"] = StaticGate.PASS_STATIC.value
    supplied_summary["draw_segment_count"] = 77
    returned_summary = result.summary
    returned_summary["gate"] = StaticGate.PASS_STATIC.value
    returned_summary["draw_segment_count"] = 99
    returned_bounds = returned_summary["bounds"]
    assert isinstance(returned_bounds, dict)
    returned_bounds["min_x"] = 999
    send_plan = build_send_plan(unsafe_gcode)

    readiness = evaluate_readiness(
        static_result=result,
        send_plan=send_plan,
        expected_gcode_sha256=send_plan.actual_sha256,
    )

    assert result.gate is StaticGate.BLOCKED
    assert result.summary["gate"] == StaticGate.BLOCKED.value
    assert result.summary["draw_segment_count"] == 1
    assert result.summary["bounds"] != returned_bounds
    assert readiness.status is ReadinessStatus.BLOCKED
    assert "STATIC_CHECK_BLOCKED" in {check.code for check in readiness.checks}


def test_readiness_result_pen_motion_assessment_is_detached() -> None:
    assessment: JsonObject = {"pen_lift_count": 2, "nested": {"count": 1}}
    result = ReadinessResult(status=ReadinessStatus.READY, checks=(), pen_motion_assessment=assessment)
    assessment["pen_lift_count"] = 999
    returned = result.pen_motion_assessment
    returned["pen_lift_count"] = 888
    nested = returned["nested"]
    assert isinstance(nested, dict)
    nested["count"] = 777

    assert result.pen_motion_assessment == {"pen_lift_count": 2, "nested": {"count": 1}}
    assert result.to_json()["pen_motion_assessment"] == {"pen_lift_count": 2, "nested": {"count": 1}}


def test_readiness_result_cannot_grant_authority_when_checks_contain_blocker() -> None:
    blocker = SafetyCheck(CheckSeverity.BLOCKER, "DIRECT_BLOCKER", "direct blocker")

    result = ReadinessResult(
        status=ReadinessStatus.READY,
        checks=(blocker,),
        pen_motion_assessment={"hard_gate": False},
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert result.allow_stream is False
    assert result.to_json()["blockers"] == ["direct blocker"]


def test_readiness_result_remains_ready_with_only_advisory_checks() -> None:
    checks = (
        SafetyCheck(CheckSeverity.WARNING, "DIRECT_WARNING", "direct warning"),
        SafetyCheck(CheckSeverity.INFO, "DIRECT_INFO", "direct info"),
    )

    result = ReadinessResult(
        status=ReadinessStatus.BLOCKED,
        checks=checks,
        pen_motion_assessment={"hard_gate": False},
    )

    assert result.status is ReadinessStatus.READY
    assert result.allow_stream is True
    assert result.to_json()["warnings"] == ["direct warning"]


@pytest.mark.parametrize(
    "premodal_line",
    [
        "X97 Y96 F900",
        "Z0 F100",
        "G21 X97 Y96 F900",
    ],
    ids=["xy", "z", "units-with-xy"],
)
def test_matching_digest_premodal_axis_candidate_is_blocked(premodal_line: str) -> None:
    preamble = "G90\nG54" if premodal_line.startswith("G21 ") else "G21\nG90\nG54"
    gcode = "\n".join(
        [
            "; Path mode: stroke (fill_paths + stroke_paths + fill_boundary_paths)",
            premodal_line,
            preamble,
            "G0 Z3.500 F400.0",
            "G0 X96.000 Y96.000 F1200.0",
            "G1 Z0.000 F100.0",
            "G1 X97.000 Y97.000 F900.0",
            "G1 Z3.500 F400.0",
            "; End",
            "",
        ]
    )
    static_result = analyze_gcode(gcode, options())
    send_plan = build_send_plan(gcode)

    result = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=send_plan.actual_sha256,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert result.allow_stream is False
    assert static_result.gate is StaticGate.BLOCKED
    assert "axis_without_local_motion" in {check.code for check in static_result.checks}


def test_check_candidate_redacts_overflowing_preview_g_word_as_domain_error() -> None:
    huge_word = "9" * 400
    gcode = SAFE_GCODE.replace("G21", f"G{huge_word}")

    with pytest.raises(DrawingMachineError) as raised:
        check_candidate(
            gcode,
            profile=MachineBuildProfile.stage1_defaults(),
            expected_gcode_sha256=hashlib.sha256(gcode.encode()).hexdigest(),
        )

    assert raised.value.payload.code == "GCODE_PREVIEW_INVALID"
    assert huge_word not in str(raised.value)
    assert huge_word not in str(dict(raised.value.payload.details))


def test_matching_digest_zero_feed_candidate_is_blocked() -> None:
    gcode = re.sub(r"F\d+(?:\.\d+)?", "F0", SAFE_GCODE)
    static_result = analyze_gcode(gcode, options())
    send_plan = build_send_plan(gcode)

    result = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=send_plan.actual_sha256,
    )

    assert static_result.gate is StaticGate.BLOCKED
    assert result.status is ReadinessStatus.BLOCKED
    assert {"STATIC_CHECK_BLOCKED", "GCODE_FEED_MISMATCH"} <= {check.code for check in result.checks}


def test_matching_digest_zero_displacement_pen_down_candidate_is_blocked() -> None:
    gcode = SAFE_GCODE.replace("G1 X97.000 Y97.000 F900.0", "G1 X96.000 Y96.000 F900.0")
    static_result = analyze_gcode(gcode, options())
    send_plan = build_send_plan(gcode)

    result = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=send_plan.actual_sha256,
    )

    assert static_result.gate is StaticGate.BLOCKED
    assert result.status is ReadinessStatus.BLOCKED
    assert {"STATIC_CHECK_BLOCKED", "GCODE_NO_DRAW_SEGMENTS", "GCODE_DRAW_LENGTH_INVALID"} <= {
        check.code for check in result.checks
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"raw_line_count": 999}, "GCODE_LINE_COUNT_MISMATCH"),
        ({"sendable_line_count": 0}, "GCODE_EMPTY_SEND_PLAN"),
        ({"candidate_source": "preview"}, "GCODE_NON_STROKE_SOURCE"),
    ],
)
def test_send_plan_safety_mismatch_blocks_readiness(mutation: dict[str, object], expected_code: str) -> None:
    static_result, send_plan, digest = passing_inputs()
    changed = replace(send_plan, **mutation)  # type: ignore[arg-type]

    result = evaluate_readiness(
        static_result=static_result,  # type: ignore[arg-type]
        send_plan=changed,
        expected_gcode_sha256=digest,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert expected_code in {check.code for check in result.checks}


@pytest.mark.parametrize(
    ("summary_changes", "expected_code"),
    [
        ({"draw_segment_count": 0}, "GCODE_NO_DRAW_SEGMENTS"),
        ({"draw_length_mm": 0.0}, "GCODE_DRAW_LENGTH_INVALID"),
        ({"pen_down_count": 0}, "GCODE_NO_PEN_DOWN"),
        ({"max_feed_seen": 1200.1}, "GCODE_FEED_MISMATCH"),
    ],
)
def test_static_summary_safety_mismatch_blocks_readiness(
    summary_changes: dict[str, object], expected_code: str
) -> None:
    static_result, send_plan, digest = passing_inputs()
    summary = dict(static_result.summary)  # type: ignore[union-attr]
    summary.update(summary_changes)
    changed = with_summary(static_result, summary)

    result = evaluate_readiness(
        static_result=changed,
        send_plan=send_plan,  # type: ignore[arg-type]
        expected_gcode_sha256=digest,
    )

    assert result.status is ReadinessStatus.BLOCKED
    assert expected_code in {check.code for check in result.checks}


def test_advisory_lift_delta_and_many_lifts_are_warnings_not_blockers() -> None:
    static_result, send_plan, digest = passing_inputs()
    summary = dict(static_result.summary)  # type: ignore[union-attr]
    summary.update({"pen_lift_count": 502, "pen_down_count": 500})
    changed = with_summary(static_result, summary)

    result = evaluate_readiness(
        static_result=changed,
        send_plan=send_plan,  # type: ignore[arg-type]
        expected_gcode_sha256=digest,
    )

    assert result.status is ReadinessStatus.READY
    assert [check.code for check in result.checks] == ["PEN_LIFT_DOWN_DELTA", "MANY_PEN_LIFTS"]


def test_matching_safe_candidate_is_ready() -> None:
    static_result, send_plan, digest = passing_inputs()

    result = evaluate_readiness(
        static_result=static_result,  # type: ignore[arg-type]
        send_plan=send_plan,  # type: ignore[arg-type]
        expected_gcode_sha256=digest,
    )

    assert result.status is ReadinessStatus.READY
    assert result.allow_stream is True
    assert result.checks == ()
