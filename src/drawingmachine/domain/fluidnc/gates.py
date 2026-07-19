from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from drawingmachine.domain.fluidnc.status import (
    ControllerStatus,
    FluidNCState,
    PreflightSnapshot,
    Vector3,
    _has_forbidden_line_character,
    validate_positive_finite,
    validate_vector,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

_MAX_EVIDENCE_LINES = 256
_MAX_EVIDENCE_LINE_BYTES = 4096
_RESET_MARKERS = ("grbl", "fluidnc", "watchdog", "reset", "invalid header", "rst:")


class GateDisposition(StrEnum):
    READY = "READY"
    AWAIT_HOME_ONLY = "AWAIT_HOME_ONLY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BLOCKED = "BLOCKED"


class SessionObservationPhase(StrEnum):
    FRESH_STABILIZATION = "FRESH_STABILIZATION"
    STABILIZED = "STABILIZED"
    ACTION_ADMITTED = "ACTION_ADMITTED"


@dataclass(frozen=True, slots=True)
class GateDecision:
    disposition: GateDisposition
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def allow_stream(self) -> bool:
        return self.disposition is GateDisposition.READY and not self.blockers


def evaluate_preflight(snapshot: object, *, require_g54: object) -> GateDecision:
    accepted = _snapshot(snapshot)
    if type(require_g54) is not bool:
        _invalid("require_g54 must be a boolean", "require_g54")
    blockers: list[str] = []
    if accepted.status.state is not FluidNCState.IDLE or accepted.status.state_text != FluidNCState.IDLE.value:
        blockers.append(f"controller state is {accepted.status.state_text!r}, expected 'Idle'")
    if accepted.errors:
        blockers.append("controller returned one or more preflight errors")
    if accepted.missing_ok_commands:
        blockers.append("one or more read-only preflight commands did not return ok")
    if require_g54 and accepted.g54 is None:
        blockers.append("required G54 offset was not observed")
    return _blocked_or_ready(blockers)


def evaluate_post_home(
    snapshot: object,
    *,
    expected_mpos: object,
    tolerance_mm: object,
) -> GateDecision:
    accepted = _snapshot(snapshot)
    expected = validate_vector(expected_mpos, "expected_mpos")
    tolerance = validate_positive_finite(tolerance_mm, "tolerance_mm")
    preflight = evaluate_preflight(accepted, require_g54=True)
    blockers = list(preflight.blockers)
    if accepted.status.mpos is None:
        blockers.append("post-HOME MPos proof is required")
    elif not _within(accepted.status.mpos, expected, tolerance):
        blockers.append("post-HOME MPos is outside the configured tolerance")
    return _blocked_or_ready(blockers)


def evaluate_calibrated_pose(
    snapshot: object,
    *,
    expected_wpos: object,
    tolerance_mm: object,
) -> GateDecision:
    accepted = _snapshot(snapshot)
    expected = validate_vector(expected_wpos, "expected_wpos")
    tolerance = validate_positive_finite(tolerance_mm, "tolerance_mm")
    preflight = evaluate_preflight(accepted, require_g54=True)
    blockers = list(preflight.blockers)
    computed = accepted.computed_wpos_from_mpos_g54
    if accepted.status.mpos is None or computed is None:
        blockers.append("post-ZCONFIRM MPos/G54 pose proof is required")
    else:
        if not _within(computed, expected, tolerance):
            blockers.append("post-ZCONFIRM computed work pose is outside the configured tolerance")
        if accepted.status.wpos is not None and not _within(accepted.status.wpos, computed, tolerance):
            blockers.append("reported WPos contradicts MPos minus G54")
        if (
            accepted.status.wco is not None
            and accepted.g54 is not None
            and not _within(accepted.status.wco, accepted.g54, tolerance)
        ):
            blockers.append("reported WCO contradicts the observed G54 offset")
    return _blocked_or_ready(blockers)


def classify_session_observation(
    evidence: object,
    status: object,
    phase: object,
) -> GateDecision:
    if type(status) is not ControllerStatus:
        _invalid("status must be an exact ControllerStatus", "status")
    if type(phase) is not SessionObservationPhase:
        _invalid("phase must be a SessionObservationPhase", "phase")
    accepted_lines = _evidence(evidence)
    reset_banner = any(any(marker in line.casefold() for marker in _RESET_MARKERS) for line in accepted_lines)
    reset_position = status.mpos == (0.0, 0.0, 0.0)
    if not reset_banner and not reset_position:
        return GateDecision(GateDisposition.READY)
    if phase is SessionObservationPhase.FRESH_STABILIZATION:
        return GateDecision(
            GateDisposition.AWAIT_HOME_ONLY,
            blockers=("fresh controller session is unhomed; approved HOME is required",),
        )
    return GateDecision(
        GateDisposition.RECOVERY_REQUIRED,
        blockers=("controller reset evidence invalidated the active session",),
    )


def _snapshot(value: object) -> PreflightSnapshot:
    if type(value) is not PreflightSnapshot:
        _invalid("snapshot must be an exact PreflightSnapshot", "snapshot")
    return value


def _within(actual: Vector3, expected: Vector3, tolerance: float) -> bool:
    return all(abs(left - right) <= tolerance for left, right in zip(actual, expected, strict=True))


def _blocked_or_ready(blockers: list[str]) -> GateDecision:
    return GateDecision(
        GateDisposition.BLOCKED if blockers else GateDisposition.READY,
        blockers=tuple(blockers),
    )


def _evidence(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_EVIDENCE_LINES:
        _invalid("session evidence must be a bounded exact tuple", "evidence")
    accepted: list[str] = []
    for item in value:
        if type(item) is not str:
            _invalid("session evidence must contain exact strings", "evidence")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError:
            _invalid("session evidence contains invalid Unicode", "evidence")
        if not item or len(encoded) > _MAX_EVIDENCE_LINE_BYTES or _has_forbidden_line_character(item):
            _invalid("session evidence contains an invalid line", "evidence")
        accepted.append(item)
    return tuple(accepted)


def _invalid(message: str, field: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="FLUIDNC_GATE_INVALID",
            category=ErrorCategory.HARDWARE,
            message=message,
            retryable=False,
            details={"field": field},
        )
    )


__all__ = [
    "GateDecision",
    "GateDisposition",
    "SessionObservationPhase",
    "classify_session_observation",
    "evaluate_calibrated_pose",
    "evaluate_post_home",
    "evaluate_preflight",
]
