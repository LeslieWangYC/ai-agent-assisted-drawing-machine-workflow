from __future__ import annotations

import json
from pathlib import Path

import pytest

from drawingmachine.domain.fluidnc.gates import (
    GateDecision,
    GateDisposition,
    SessionObservationPhase,
    classify_session_observation,
    evaluate_calibrated_pose,
    evaluate_post_home,
    evaluate_preflight,
)
from drawingmachine.domain.fluidnc.status import (
    ControllerStatus,
    FluidNCState,
    PreflightSnapshot,
    WorkOffset,
    parse_preflight_snapshot,
)
from drawingmachine.errors import DrawingMachineError

GOLDEN_ROOT = Path("tests/fixtures/package_c/golden")


def _preflight(*, status_line: str | None = None, offsets: tuple[str, ...] | None = None):  # type: ignore[no-untyped-def]
    return parse_preflight_snapshot(
        status_line=status_line or "<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=offsets if offsets is not None else ("[G54:96,96,188.5]", "ok"),
        errors=(),
        missing_ok_commands=(),
    )


def test_preflight_preserves_safe_legacy_result_but_makes_missing_g54_a_blocker() -> None:
    legacy = json.loads((GOLDEN_ROOT / "preflight.json").read_text(encoding="utf-8"))

    accepted = evaluate_preflight(_preflight(), require_g54=True)
    missing = evaluate_preflight(_preflight(offsets=("ok",)), require_g54=True)

    assert accepted.allow_stream is legacy["legacy_gate"]["allow_stream"]
    assert accepted.blockers == tuple(legacy["legacy_gate"]["blockers"])
    assert missing.allow_stream is False
    assert missing.disposition is GateDisposition.BLOCKED
    assert missing.blockers == ("required G54 offset was not observed",)
    assert legacy["missing_g54_legacy_gate"]["allow_stream"] is True


def test_post_home_requires_idle_mpos_and_g54_proof() -> None:
    assert evaluate_post_home(_preflight(), expected_mpos=(192.0, 192.0, 192.0), tolerance_mm=1.0).allow_stream

    missing_mpos = _preflight(status_line="<Idle|WPos:96,96,3.5|WCO:96,96,188.5>")
    assert evaluate_post_home(missing_mpos, expected_mpos=(192.0, 192.0, 192.0), tolerance_mm=1.0).blockers == (
        "post-HOME MPos proof is required",
    )
    displaced = _preflight(status_line="<Idle|MPos:190,192,192|WPos:94,96,3.5|WCO:96,96,188.5>")
    assert not evaluate_post_home(displaced, expected_mpos=(192.0, 192.0, 192.0), tolerance_mm=1.0).allow_stream


def test_calibrated_pose_checks_wpos_without_reapplying_homed_mpos_gate() -> None:
    calibrated = _preflight(status_line="<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>")
    result = evaluate_calibrated_pose(calibrated, expected_wpos=(96.0, 96.0, 3.5), tolerance_mm=0.01)
    assert result.allow_stream

    wrong = _preflight(status_line="<Idle|MPos:192,192,192|WPos:96,96,3.6|WCO:96,96,188.4>")
    assert not evaluate_calibrated_pose(wrong, expected_wpos=(96.0, 96.0, 3.5), tolerance_mm=0.01).allow_stream


def test_calibrated_pose_rejects_wpos_wco_and_computed_pose_contradictions() -> None:
    contradictory_wpos = _preflight(status_line="<Idle|MPos:192,192,192|WPos:95,96,3.5|WCO:96,96,188.5>")
    contradictory_wco = _preflight(status_line="<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,95,188.5>")
    wrong_computed_pose = _preflight(status_line="<Idle|MPos:191,192,192|WPos:95,96,3.5|WCO:96,96,188.5>")
    for snapshot in (contradictory_wpos, contradictory_wco, wrong_computed_pose):
        assert not evaluate_calibrated_pose(snapshot, expected_wpos=(96.0, 96.0, 3.5), tolerance_mm=0.01).allow_stream


def test_fresh_and_late_reset_evidence_have_opposite_results() -> None:
    golden = json.loads((GOLDEN_ROOT / "reset.json").read_text(encoding="utf-8"))
    evidence = tuple(golden["fresh_session"]["evidence"])
    status = _preflight(status_line="<Idle|MPos:0,0,0|WPos:0,0,0|WCO:0,0,0>").status

    fresh = classify_session_observation(evidence, status, SessionObservationPhase.FRESH_STABILIZATION)
    late = classify_session_observation(evidence, status, SessionObservationPhase.STABILIZED)
    admitted = classify_session_observation(evidence, status, SessionObservationPhase.ACTION_ADMITTED)

    assert fresh.disposition.value == golden["fresh_session"]["package_c_expected"]
    assert late.disposition.value == golden["after_stabilization_or_action"]["package_c_expected"]
    assert admitted.disposition is GateDisposition.RECOVERY_REQUIRED


def test_gate_inputs_reject_booleans_nonfinite_and_hostile_vectors() -> None:
    class HostileTuple(tuple[float, ...]):
        pass

    for vector, tolerance in (
        ((True, 192.0, 192.0), 1.0),
        ((192.0, 192.0, float("nan")), 1.0),
        (HostileTuple((192.0, 192.0, 192.0)), 1.0),
        ((192.0, 192.0, 192.0), float("inf")),
    ):
        with pytest.raises(DrawingMachineError):
            evaluate_post_home(_preflight(), expected_mpos=vector, tolerance_mm=tolerance)  # type: ignore[arg-type]


def test_preflight_collects_nonidle_error_and_missing_ack_blockers() -> None:
    snapshot = parse_preflight_snapshot(
        status_line="<Hold:0|MPos:192,192,192>",
        parser_state=("[GC:G0 G54 G17]",),
        offset_lines=("[G54:96,96,188.5]",),
        errors=("normalized error",),
        missing_ok_commands=("$G",),
    )
    decision = evaluate_preflight(snapshot, require_g54=False)
    assert len(decision.blockers) == 3
    assert not decision.allow_stream
    assert not GateDecision(GateDisposition.READY, blockers=("blocked",)).allow_stream
    with pytest.raises(DrawingMachineError):
        evaluate_preflight(snapshot, require_g54=1)


def test_calibrated_pose_requires_mpos_g54_but_can_compute_when_wpos_is_absent() -> None:
    no_wpos = _preflight(status_line="<Idle|MPos:192,192,192|WCO:96,96,188.5>")
    assert evaluate_calibrated_pose(no_wpos, expected_wpos=(96.0, 96.0, 3.5), tolerance_mm=0.01).allow_stream
    no_mpos = _preflight(status_line="<Idle|WPos:96,96,3.5|WCO:96,96,188.5>")
    assert evaluate_calibrated_pose(no_mpos, expected_wpos=(96.0, 96.0, 3.5), tolerance_mm=0.01).blockers[-1] == (
        "post-ZCONFIRM MPos/G54 pose proof is required"
    )


def test_session_observation_validates_types_bounds_and_stable_evidence() -> None:
    stable = _preflight().status
    assert classify_session_observation(
        ("ordinary startup text",), stable, SessionObservationPhase.STABILIZED
    ).allow_stream
    assert (
        classify_session_observation(
            ("ordinary", "watchdog reset"), stable, SessionObservationPhase.FRESH_STABILIZATION
        ).disposition
        is GateDisposition.AWAIT_HOME_ONLY
    )
    zero = _preflight(status_line="<Idle|MPos:0,0,0>").status
    assert (
        classify_session_observation((), zero, SessionObservationPhase.STABILIZED).disposition
        is GateDisposition.RECOVERY_REQUIRED
    )

    invalid: tuple[tuple[object, object, object], ...] = (
        (("ok",), object(), SessionObservationPhase.STABILIZED),
        (("ok",), stable, "STABILIZED"),
        (["ok"], stable, SessionObservationPhase.STABILIZED),
        (tuple("ok" for _ in range(257)), stable, SessionObservationPhase.STABILIZED),
        ((1,), stable, SessionObservationPhase.STABILIZED),
        (("bad\ud800",), stable, SessionObservationPhase.STABILIZED),
        (("",), stable, SessionObservationPhase.STABILIZED),
        (("x" * 4097,), stable, SessionObservationPhase.STABILIZED),
        (("bad\nline",), stable, SessionObservationPhase.STABILIZED),
    )
    for evidence, status, phase in invalid:
        with pytest.raises(DrawingMachineError):
            classify_session_observation(evidence, status, phase)


def test_gate_rejects_non_snapshot() -> None:
    with pytest.raises(DrawingMachineError):
        evaluate_preflight(object(), require_g54=True)


def test_forged_idle_suffix_cannot_pass_preflight_or_post_home() -> None:
    status = ControllerStatus(
        state=FluidNCState.IDLE,
        state_text="Idle:corrupt",
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 188.5),
        status_line="<Idle:corrupt|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
    )
    snapshot = PreflightSnapshot(
        status=status,
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("[G54:96,96,188.5]", "ok"),
        offsets=(WorkOffset("G54", (96.0, 96.0, 188.5)),),
        errors=(),
        missing_ok_commands=(),
    )
    assert not evaluate_preflight(snapshot, require_g54=True).allow_stream
    assert not evaluate_post_home(snapshot, expected_mpos=(192.0, 192.0, 192.0), tolerance_mm=1.0).allow_stream


@pytest.mark.parametrize("control", ("\u0085", "\u007f", "\u009f", "\u200b", "\ufeff", "\u2028", "\u2029", "\r", "\n"))
def test_reset_evidence_rejects_every_control_separator(control: str) -> None:
    with pytest.raises(DrawingMachineError):
        classify_session_observation((f"watchdog{control}",), _preflight().status, SessionObservationPhase.STABILIZED)
