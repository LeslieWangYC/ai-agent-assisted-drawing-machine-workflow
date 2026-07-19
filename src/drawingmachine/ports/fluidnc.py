from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from drawingmachine.domain.fluidnc.gates import (
    GateDecision,
    GateDisposition,
    SessionObservationPhase,
    classify_session_observation,
    evaluate_calibrated_pose,
    evaluate_post_home,
)
from drawingmachine.domain.fluidnc.status import FluidNCState, PreflightSnapshot, Vector3, validate_vector
from drawingmachine.domain.gcode.stream_program import ValidatedStreamProgram

_SAFE_TEXT = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_MESSAGE_BYTES = 4096
_RESERVED_MOTION_PROOF_FAILURE_CODES = frozenset(
    {
        "POST_HOME_PROOF_FAILED",
        "POST_ZCAL_PROOF_FAILED",
        "POST_ZCONFIRM_PROOF_FAILED",
    }
)


class FluidNCFailureKind(StrEnum):
    ALARM = "ALARM"
    CONTROLLER_ERROR = "CONTROLLER_ERROR"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_LOSS = "TRANSPORT_LOSS"
    LOST_ACK = "LOST_ACK"
    CONTROLLER_RESET = "CONTROLLER_RESET"
    SHORT_WRITE = "SHORT_WRITE"
    DECODE_ERROR = "DECODE_ERROR"
    CLOSE_ERROR = "CLOSE_ERROR"


class SingleCommandOutcomeStage(StrEnum):
    DISPATCH_ATTEMPTED = "DISPATCH_ATTEMPTED"
    COMMAND_ACKNOWLEDGED = "COMMAND_ACKNOWLEDGED"
    IDLE_OBSERVED = "IDLE_OBSERVED"
    POSTCONDITION_PROVEN = "POSTCONDITION_PROVEN"


class ZCalibrationOutcomeStage(StrEnum):
    METRIC_ATTEMPTED = "METRIC_ATTEMPTED"
    METRIC_ACKNOWLEDGED = "METRIC_ACKNOWLEDGED"
    ABSOLUTE_ATTEMPTED = "ABSOLUTE_ATTEMPTED"
    ABSOLUTE_ACKNOWLEDGED = "ABSOLUTE_ACKNOWLEDGED"
    WORK_COORDINATE_ATTEMPTED = "WORK_COORDINATE_ATTEMPTED"
    WORK_COORDINATE_ACKNOWLEDGED = "WORK_COORDINATE_ACKNOWLEDGED"
    PEN_UP_ATTEMPTED = "PEN_UP_ATTEMPTED"
    PEN_UP_ACKNOWLEDGED = "PEN_UP_ACKNOWLEDGED"
    CENTER_ATTEMPTED = "CENTER_ATTEMPTED"
    CENTER_ACKNOWLEDGED = "CENTER_ACKNOWLEDGED"
    CONTACT_ATTEMPTED = "CONTACT_ATTEMPTED"
    CONTACT_ACKNOWLEDGED = "CONTACT_ACKNOWLEDGED"
    POSTCONDITION = "POSTCONDITION"


@dataclass(frozen=True, slots=True)
class FluidNCFailure:
    kind: FluidNCFailureKind
    code: str
    message: str
    side_effect_may_have_occurred: bool

    def __post_init__(self) -> None:
        if type(self.kind) is not FluidNCFailureKind:
            raise TypeError("failure kind must be an exact FluidNCFailureKind")
        if type(self.code) is not str or _SAFE_TEXT.fullmatch(self.code) is None:
            raise TypeError("failure code must be bounded canonical ASCII")
        if type(self.message) is not str or not self.message:
            raise TypeError("failure message must be a non-empty exact string")
        if len(self.message) > _MAX_MESSAGE_BYTES or any(
            _forbidden_line_character(character) for character in self.message
        ):
            raise TypeError("failure message must be one bounded line")
        if len(self.message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise TypeError("failure message must fit the bounded UTF-8 byte limit")
        if type(self.side_effect_may_have_occurred) is not bool:
            raise TypeError("failure side-effect evidence must be an exact boolean")
        if (
            self.kind
            in {
                FluidNCFailureKind.LOST_ACK,
                FluidNCFailureKind.CONTROLLER_RESET,
                FluidNCFailureKind.SHORT_WRITE,
            }
            and not self.side_effect_may_have_occurred
        ):
            raise TypeError("this failure kind intrinsically proves a possible side effect")


@dataclass(frozen=True, slots=True)
class OpenSessionRequest:
    machine_session_epoch: str

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)


@dataclass(frozen=True, slots=True)
class InitialPreflightRequest:
    observation_phase: SessionObservationPhase = SessionObservationPhase.FRESH_STABILIZATION

    def __post_init__(self) -> None:
        if self.observation_phase is not SessionObservationPhase.FRESH_STABILIZATION:
            raise TypeError("initial preflight must use the fresh-stabilization phase")


@dataclass(frozen=True, slots=True)
class HomeAllAxesRequest:
    expected_mpos: Vector3
    tolerance_mm: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_mpos", validate_vector(self.expected_mpos, "expected_mpos"))
        object.__setattr__(self, "tolerance_mm", _positive(self.tolerance_mm, "tolerance_mm"))


@dataclass(frozen=True, slots=True)
class ZCalibrationRequest:
    work_coordinate: str
    center_x: float
    center_y: float
    travel_z: float
    contact_z: float
    travel_feed: float
    contact_feed: float

    def __post_init__(self) -> None:
        if type(self.work_coordinate) is not str or self.work_coordinate != "G54":
            raise TypeError("work coordinate must be exactly G54")
        for field in ("center_x", "center_y", "travel_z"):
            object.__setattr__(self, field, _non_negative(getattr(self, field), field))
        object.__setattr__(self, "contact_z", _non_negative(self.contact_z, "contact_z"))
        if self.contact_z >= self.travel_z:
            raise TypeError("contact_z must be below travel_z")
        object.__setattr__(self, "travel_feed", _positive(self.travel_feed, "travel_feed"))
        object.__setattr__(self, "contact_feed", _positive(self.contact_feed, "contact_feed"))


@dataclass(frozen=True, slots=True)
class ZConfirmationRequest:
    expected_work_position: Vector3
    travel_z: float
    raise_feed: float
    tolerance_mm: float

    def __post_init__(self) -> None:
        expected = validate_vector(self.expected_work_position, "expected_work_position")
        travel = _non_negative(self.travel_z, "travel_z")
        if expected[2] != travel:
            raise TypeError("expected work Z must equal travel_z")
        object.__setattr__(self, "expected_work_position", expected)
        object.__setattr__(self, "travel_z", travel)
        object.__setattr__(self, "raise_feed", _positive(self.raise_feed, "raise_feed"))
        object.__setattr__(self, "tolerance_mm", _positive(self.tolerance_mm, "tolerance_mm"))


@dataclass(frozen=True, slots=True)
class StreamProgramRequest:
    program: ValidatedStreamProgram

    def __post_init__(self) -> None:
        if type(self.program) is not ValidatedStreamProgram:
            raise TypeError("stream request requires an exact ValidatedStreamProgram")


@dataclass(frozen=True, slots=True)
class HomeZAxisRequest:
    require_idle_proof: bool = True

    def __post_init__(self) -> None:
        if self.require_idle_proof is not True:
            raise TypeError("HOME_Z always requires an Idle proof")


@dataclass(frozen=True, slots=True)
class CloseSessionRequest:
    machine_session_epoch: str

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)


@dataclass(frozen=True, slots=True)
class PreflightOutcome:
    machine_session_epoch: str
    snapshot: PreflightSnapshot
    evidence: tuple[str, ...]
    observation_phase: SessionObservationPhase
    decision: GateDecision
    failure: FluidNCFailure | None = None

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        _snapshot(self.snapshot)
        evidence = _evidence(self.evidence)
        if self.observation_phase not in {
            SessionObservationPhase.FRESH_STABILIZATION,
            SessionObservationPhase.STABILIZED,
        }:
            raise TypeError("initial preflight outcome must use fresh or stabilized observation")
        if type(self.decision) is not GateDecision:
            raise TypeError("preflight decision must be exact")
        if self.decision != classify_session_observation(evidence, self.snapshot.status, self.observation_phase):
            raise TypeError("preflight decision contradicts its reset evidence")
        _failure(self.failure)
        if (
            self.observation_phase is SessionObservationPhase.FRESH_STABILIZATION
            and self.failure is not None
            and self.failure.kind is FluidNCFailureKind.CONTROLLER_RESET
        ):
            raise TypeError("fresh-session reset evidence is an expected unhomed observation, not a reset failure")
        if self.observation_phase is SessionObservationPhase.STABILIZED and (
            self.decision.disposition is not GateDisposition.RECOVERY_REQUIRED
            or self.failure is None
            or self.failure.kind is not FluidNCFailureKind.CONTROLLER_RESET
        ):
            raise TypeError("stabilized preflight evidence requires an exact controller-reset failure")
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True, slots=True)
class HomeOutcome:
    machine_session_epoch: str
    request: HomeAllAxesRequest
    snapshot: PreflightSnapshot | None
    failure: FluidNCFailure | None = None
    evidence: tuple[str, ...] | None = None
    observation_phase: SessionObservationPhase | None = None
    decision: GateDecision | None = None
    acknowledged_steps: int = 1
    stage: SingleCommandOutcomeStage = SingleCommandOutcomeStage.POSTCONDITION_PROVEN

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        if type(self.request) is not HomeAllAxesRequest:
            raise TypeError("HOME outcome request must be exact")
        if self.snapshot is not None:
            _snapshot(self.snapshot)
        _failure(self.failure)
        _validate_optional_observation(
            snapshot=self.snapshot,
            evidence=self.evidence,
            phase=self.observation_phase,
            decision=self.decision,
            failure=self.failure,
        )
        proof_ready = self.snapshot is not None and (
            evaluate_post_home(
                self.snapshot,
                expected_mpos=self.request.expected_mpos,
                tolerance_mm=self.request.tolerance_mm,
            ).disposition
            is GateDisposition.READY
        )
        if self.failure is None and not proof_ready:
            raise TypeError("successful HOME outcome requires an exact post-HOME proof")
        _validate_single_command_progress(
            acknowledged_steps=self.acknowledged_steps,
            stage=self.stage,
            snapshot=self.snapshot,
            failure=self.failure,
            proof_ready=proof_ready,
            proof_failure_code="POST_HOME_PROOF_FAILED",
        )


@dataclass(frozen=True, slots=True)
class ZCalibrationOutcome:
    machine_session_epoch: str
    request: ZCalibrationRequest
    acknowledged_steps: int
    snapshot: PreflightSnapshot | None
    failure: FluidNCFailure | None = None
    evidence: tuple[str, ...] | None = None
    observation_phase: SessionObservationPhase | None = None
    decision: GateDecision | None = None
    stage: ZCalibrationOutcomeStage = ZCalibrationOutcomeStage.POSTCONDITION

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        if type(self.request) is not ZCalibrationRequest:
            raise TypeError("ZCAL outcome request must be exact")
        acknowledged = _count(self.acknowledged_steps, "acknowledged_steps", minimum=0)
        if acknowledged > 6:
            raise TypeError("ZCAL acknowledged steps cannot exceed the fixed sequence")
        if self.snapshot is not None:
            _snapshot(self.snapshot)
        _failure(self.failure)
        _validate_optional_observation(
            snapshot=self.snapshot,
            evidence=self.evidence,
            phase=self.observation_phase,
            decision=self.decision,
            failure=self.failure,
        )
        if self.failure is None and acknowledged != 6:
            raise TypeError("successful ZCAL outcome requires all six fixed acknowledgements")
        if self.failure is not None and acknowledged > 0 and not self.failure.side_effect_may_have_occurred:
            raise TypeError("failed acknowledged ZCAL requires possible side-effect evidence")
        _validate_zcal_progress(acknowledged, self.stage, self.failure)


@dataclass(frozen=True, slots=True)
class ZConfirmationOutcome:
    machine_session_epoch: str
    request: ZConfirmationRequest
    snapshot: PreflightSnapshot | None
    failure: FluidNCFailure | None = None
    evidence: tuple[str, ...] | None = None
    observation_phase: SessionObservationPhase | None = None
    decision: GateDecision | None = None
    acknowledged_steps: int = 1
    stage: SingleCommandOutcomeStage = SingleCommandOutcomeStage.POSTCONDITION_PROVEN

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        if type(self.request) is not ZConfirmationRequest:
            raise TypeError("ZCONFIRM outcome request must be exact")
        if self.snapshot is not None:
            _snapshot(self.snapshot)
        _failure(self.failure)
        _validate_optional_observation(
            snapshot=self.snapshot,
            evidence=self.evidence,
            phase=self.observation_phase,
            decision=self.decision,
            failure=self.failure,
        )
        proof_ready = self.snapshot is not None and (
            evaluate_calibrated_pose(
                self.snapshot,
                expected_wpos=self.request.expected_work_position,
                tolerance_mm=self.request.tolerance_mm,
            ).disposition
            is GateDisposition.READY
        )
        if self.failure is None and not proof_ready:
            raise TypeError("successful ZCONFIRM outcome requires an exact ZCONFIRM proof from calibrated pose")
        _validate_single_command_progress(
            acknowledged_steps=self.acknowledged_steps,
            stage=self.stage,
            snapshot=self.snapshot,
            failure=self.failure,
            proof_ready=proof_ready,
            proof_failure_code="POST_ZCONFIRM_PROOF_FAILED",
        )


@dataclass(frozen=True, slots=True)
class HomeZOutcome:
    machine_session_epoch: str
    request: HomeZAxisRequest
    snapshot: PreflightSnapshot | None
    failure: FluidNCFailure | None = None
    evidence: tuple[str, ...] | None = None
    observation_phase: SessionObservationPhase | None = None
    decision: GateDecision | None = None

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        if type(self.request) is not HomeZAxisRequest:
            raise TypeError("HOME_Z outcome request must be exact")
        if self.snapshot is not None:
            _snapshot(self.snapshot)
        _failure(self.failure)
        _validate_optional_observation(
            snapshot=self.snapshot,
            evidence=self.evidence,
            phase=self.observation_phase,
            decision=self.decision,
            failure=self.failure,
        )
        if self.failure is None and not _is_exact_idle_proof(self.snapshot):
            raise TypeError("successful HOME_Z outcome requires an exact Idle proof")


@dataclass(frozen=True, slots=True)
class StreamProgressUpdate:
    machine_session_epoch: str
    acknowledged_commands: int
    source_line: int
    command_digest: str

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        _count(self.acknowledged_commands, "acknowledged_commands", minimum=1)
        _count(self.source_line, "source_line", minimum=1)
        if type(self.command_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", self.command_digest) is None:
            raise TypeError("progress command digest must be a canonical SHA-256")


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    machine_session_epoch: str
    total_commands: int
    acknowledged_commands: int
    final_snapshot: PreflightSnapshot | None
    failure: FluidNCFailure | None = None
    evidence: tuple[str, ...] | None = None
    observation_phase: SessionObservationPhase | None = None
    decision: GateDecision | None = None

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        total = _count(self.total_commands, "total_commands", minimum=1)
        acknowledged = _count(self.acknowledged_commands, "acknowledged_commands", minimum=0)
        if acknowledged > total:
            raise TypeError("acknowledged commands cannot exceed total commands")
        if self.final_snapshot is not None:
            _snapshot(self.final_snapshot)
        _failure(self.failure)
        if self.failure is not None and acknowledged > 0 and not self.failure.side_effect_may_have_occurred:
            raise TypeError("failed acknowledged STREAM requires possible side-effect evidence")
        if self.failure is None and (acknowledged != total or not _is_exact_idle_proof(self.final_snapshot)):
            raise TypeError("successful STREAM requires every ACK and an exact final Idle proof")
        _validate_optional_observation(
            snapshot=self.final_snapshot,
            evidence=self.evidence,
            phase=self.observation_phase,
            decision=self.decision,
            failure=self.failure,
        )


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    machine_session_epoch: str
    failure: FluidNCFailure | None = None

    def __post_init__(self) -> None:
        _epoch(self.machine_session_epoch)
        _failure(self.failure)


StreamProgressCallback = Callable[[StreamProgressUpdate], None]


class FluidNCSession(Protocol):
    def read_initial_preflight(self, request: InitialPreflightRequest) -> PreflightOutcome: ...

    def home_all_axes(self, request: HomeAllAxesRequest) -> HomeOutcome: ...

    def run_z_calibration(self, request: ZCalibrationRequest) -> ZCalibrationOutcome: ...

    def raise_after_z_confirmation(self, request: ZConfirmationRequest) -> ZConfirmationOutcome: ...

    def stream_program(
        self,
        request: StreamProgramRequest,
        progress: StreamProgressCallback,
    ) -> StreamOutcome: ...

    def home_z_axis(self, request: HomeZAxisRequest) -> HomeZOutcome: ...

    def close(self, request: CloseSessionRequest) -> CloseOutcome: ...


class FluidNCSessionFactory(Protocol):
    def open_session(self, request: OpenSessionRequest) -> FluidNCSession: ...


def _epoch(value: object) -> str:
    if type(value) is not str:
        raise TypeError("machine session epoch must be an exact string")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as error:
        raise TypeError("machine session epoch must be a canonical UUID") from error
    if str(parsed) != value:
        raise TypeError("machine session epoch must be a canonical UUID")
    return value


def _number(value: object, field: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{field} must be an exact finite number")
    try:
        result = float(cast(int | float, value))
    except OverflowError as error:
        raise TypeError(f"{field} must be an exact finite number") from error
    if not math.isfinite(result):
        raise TypeError(f"{field} must be an exact finite number")
    return result


def _positive(value: object, field: str) -> float:
    result = _number(value, field)
    if result <= 0.0:
        raise TypeError(f"{field} must be positive")
    return result


def _non_negative(value: object, field: str) -> float:
    result = _number(value, field)
    if result < 0.0:
        raise TypeError(f"{field} must be non-negative")
    return result


def _count(value: object, field: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(f"{field} must be an exact integer of at least {minimum}")
    return value


def _snapshot(value: object) -> PreflightSnapshot:
    if type(value) is not PreflightSnapshot:
        raise TypeError("snapshot must be an exact PreflightSnapshot")
    return value


def _failure(value: object) -> FluidNCFailure | None:
    if value is not None and type(value) is not FluidNCFailure:
        raise TypeError("failure must be an exact FluidNCFailure")
    return value


def _evidence(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > 256:
        raise TypeError("session evidence must be a bounded exact tuple")
    accepted: list[str] = []
    total = 0
    for line in value:
        if type(line) is not str or not line or any(character in line for character in "\r\n\x00"):
            raise TypeError("session evidence must contain exact one-line strings")
        try:
            total += len(line.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise TypeError("session evidence must be valid UTF-8") from error
        if total > 64 * 1024:
            raise TypeError("session evidence exceeds its byte bound")
        accepted.append(line)
    return tuple(accepted)


def _validate_optional_observation(
    *,
    snapshot: PreflightSnapshot | None,
    evidence: tuple[str, ...] | None,
    phase: SessionObservationPhase | None,
    decision: GateDecision | None,
    failure: FluidNCFailure | None,
) -> None:
    supplied = (evidence is not None, phase is not None, decision is not None)
    if any(supplied) and not all(supplied):
        raise TypeError("session observation evidence, phase, and decision are atomic")
    if not any(supplied):
        snapshot_proves_reset = (
            type(snapshot) is PreflightSnapshot
            and classify_session_observation(
                (),
                snapshot.status,
                SessionObservationPhase.ACTION_ADMITTED,
            ).disposition
            is GateDisposition.RECOVERY_REQUIRED
        )
        if snapshot_proves_reset or (failure is not None and failure.kind is FluidNCFailureKind.CONTROLLER_RESET):
            raise TypeError("controller-reset evidence requires a complete bound session observation")
        return
    if (
        snapshot is None
        or type(evidence) is not tuple
        or type(phase) is not SessionObservationPhase
        or type(decision) is not GateDecision
    ):
        raise TypeError("session observation requires exact snapshot, phase, and decision")
    accepted = _evidence(evidence)
    if decision != classify_session_observation(accepted, snapshot.status, phase):
        raise TypeError("session observation decision contradicts its reset evidence")
    if phase is SessionObservationPhase.FRESH_STABILIZATION:
        raise TypeError("post-open operation cannot report a fresh-stabilization observation")
    reset_failure = failure is not None and failure.kind is FluidNCFailureKind.CONTROLLER_RESET
    recovery_decision = decision.disposition is GateDisposition.RECOVERY_REQUIRED
    if reset_failure != recovery_decision:
        raise TypeError("late reset observation requires a bound controller-reset failure and recovery decision")


def _validate_single_command_progress(
    *,
    acknowledged_steps: object,
    stage: object,
    snapshot: PreflightSnapshot | None,
    failure: FluidNCFailure | None,
    proof_ready: bool,
    proof_failure_code: str,
) -> None:
    acknowledged = _count(acknowledged_steps, "acknowledged_steps", minimum=0)
    if acknowledged > 1:
        raise TypeError("single-command outcome acknowledgements must be zero or one")
    if type(stage) is not SingleCommandOutcomeStage:
        raise TypeError("single-command outcome stage must be exact")
    expected_acknowledgements = {
        SingleCommandOutcomeStage.DISPATCH_ATTEMPTED: 0,
        SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED: 1,
        SingleCommandOutcomeStage.IDLE_OBSERVED: 1,
        SingleCommandOutcomeStage.POSTCONDITION_PROVEN: 1,
    }[stage]
    if acknowledged != expected_acknowledgements:
        raise TypeError("single-command outcome stage contradicts its acknowledgement count")
    if stage is SingleCommandOutcomeStage.POSTCONDITION_PROVEN:
        if failure is not None or not proof_ready:
            raise TypeError("postcondition-proven stage requires an exact successful proof")
        return
    if failure is None:
        raise TypeError("incomplete single-command stage requires typed failure evidence")
    if acknowledged > 0 and not failure.side_effect_may_have_occurred:
        raise TypeError("acknowledged single-command failure requires possible side-effect evidence")
    if failure.code in _RESERVED_MOTION_PROOF_FAILURE_CODES and (
        failure.code != proof_failure_code or stage is not SingleCommandOutcomeStage.IDLE_OBSERVED
    ):
        raise TypeError("reserved proof failure requires its exact action and Idle-observed stage")
    if stage is SingleCommandOutcomeStage.IDLE_OBSERVED:
        idle_status_observed = (
            type(snapshot) is PreflightSnapshot
            and snapshot.status.state is FluidNCState.IDLE
            and snapshot.status.state_text == FluidNCState.IDLE.value
        )
        if not idle_status_observed or failure.code != proof_failure_code:
            raise TypeError("Idle-observed stage requires its exact postcondition failure")


def _validate_zcal_progress(
    acknowledged: int,
    stage: object,
    failure: FluidNCFailure | None,
) -> None:
    if type(stage) is not ZCalibrationOutcomeStage:
        raise TypeError("ZCAL outcome stage must be exact")
    if (
        failure is not None
        and failure.code in _RESERVED_MOTION_PROOF_FAILURE_CODES
        and (failure.code != "POST_ZCAL_PROOF_FAILED" or stage is not ZCalibrationOutcomeStage.POSTCONDITION)
    ):
        raise TypeError("reserved proof failure requires the exact ZCAL postcondition stage")
    attempted = (
        ZCalibrationOutcomeStage.METRIC_ATTEMPTED,
        ZCalibrationOutcomeStage.ABSOLUTE_ATTEMPTED,
        ZCalibrationOutcomeStage.WORK_COORDINATE_ATTEMPTED,
        ZCalibrationOutcomeStage.PEN_UP_ATTEMPTED,
        ZCalibrationOutcomeStage.CENTER_ATTEMPTED,
        ZCalibrationOutcomeStage.CONTACT_ATTEMPTED,
    )
    acknowledged_stages = (
        ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.ABSOLUTE_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.WORK_COORDINATE_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.PEN_UP_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.CENTER_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED,
    )
    if failure is None:
        valid = acknowledged == 6 and stage is ZCalibrationOutcomeStage.POSTCONDITION
    else:
        valid = acknowledged < 6 and stage is attempted[acknowledged]
        if acknowledged > 0:
            valid = valid or stage is acknowledged_stages[acknowledged - 1]
        if acknowledged == 6:
            valid = stage in {ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED, ZCalibrationOutcomeStage.POSTCONDITION}
    if not valid:
        raise TypeError("ZCAL outcome stage contradicts the exact fixed-sequence ACK boundary")


def _is_exact_idle_proof(snapshot: PreflightSnapshot | None) -> bool:
    return (
        type(snapshot) is PreflightSnapshot
        and snapshot.status.state is FluidNCState.IDLE
        and snapshot.status.state_text == FluidNCState.IDLE.value
        and not snapshot.errors
        and not snapshot.missing_ok_commands
    )


def _forbidden_line_character(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("C") or category in {"Zl", "Zp"}


__all__ = [
    "CloseOutcome",
    "CloseSessionRequest",
    "FluidNCFailure",
    "FluidNCFailureKind",
    "FluidNCSession",
    "FluidNCSessionFactory",
    "HomeAllAxesRequest",
    "HomeOutcome",
    "HomeZAxisRequest",
    "HomeZOutcome",
    "InitialPreflightRequest",
    "OpenSessionRequest",
    "PreflightOutcome",
    "SingleCommandOutcomeStage",
    "StreamOutcome",
    "StreamProgramRequest",
    "StreamProgressCallback",
    "StreamProgressUpdate",
    "ZCalibrationOutcome",
    "ZCalibrationOutcomeStage",
    "ZCalibrationRequest",
    "ZConfirmationOutcome",
    "ZConfirmationRequest",
]
