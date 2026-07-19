from __future__ import annotations

import hashlib
import math
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol, cast

import serial  # type: ignore[import-untyped]

from drawingmachine.config.machine import MachineExecutionConfig
from drawingmachine.domain.fluidnc.gates import (
    GateDecision,
    GateDisposition,
    SessionObservationPhase,
    classify_session_observation,
    evaluate_calibrated_pose,
    evaluate_post_home,
)
from drawingmachine.domain.fluidnc.status import (
    ControllerStatus,
    FluidNCState,
    PreflightSnapshot,
    parse_controller_status,
    parse_preflight_snapshot,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    FluidNCSession,
    HomeAllAxesRequest,
    HomeOutcome,
    HomeZAxisRequest,
    HomeZOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    PreflightOutcome,
    SingleCommandOutcomeStage,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressCallback,
    StreamProgressUpdate,
    ZCalibrationOutcome,
    ZCalibrationOutcomeStage,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)

_MAX_LINES = 256
_MAX_LINE_BYTES = 4096
_MAX_TOTAL_BYTES = 64 * 1024


class _CommandObservationStage(StrEnum):
    PRE_ACK_FAILURE = "PRE_ACK_FAILURE"
    ACK_OBSERVED = "ACK_OBSERVED"
    POST_ACK_FAILURE = "POST_ACK_FAILURE"


@dataclass(frozen=True, slots=True)
class _CommandResult:
    ack_observed: bool
    failure: FluidNCFailure | None
    evidence: tuple[str, ...]
    observation_stage: _CommandObservationStage

    def __post_init__(self) -> None:
        if type(self.ack_observed) is not bool or type(self.observation_stage) is not _CommandObservationStage:
            raise TypeError("command result authority must use exact closed fields")
        if self.failure is not None and type(self.failure) is not FluidNCFailure:
            raise TypeError("command result failure must be exact")
        if type(self.evidence) is not tuple or any(type(line) is not str for line in self.evidence):
            raise TypeError("command result evidence must be an exact string tuple")
        legal = {
            _CommandObservationStage.PRE_ACK_FAILURE: not self.ack_observed and self.failure is not None,
            _CommandObservationStage.ACK_OBSERVED: self.ack_observed and self.failure is None,
            _CommandObservationStage.POST_ACK_FAILURE: self.ack_observed and self.failure is not None,
        }[self.observation_stage]
        if not legal:
            raise TypeError("command result stage contradicts ACK/failure evidence")


@dataclass(frozen=True, slots=True)
class _IdleSnapshotResult:
    snapshot: PreflightSnapshot
    evidence: tuple[str, ...]
    failure: FluidNCFailure | None
    idle_observed: bool

    def __post_init__(self) -> None:
        if type(self.snapshot) is not PreflightSnapshot or type(self.evidence) is not tuple:
            raise TypeError("Idle snapshot result requires exact typed evidence")
        if self.failure is not None and type(self.failure) is not FluidNCFailure:
            raise TypeError("Idle snapshot result failure must be exact")
        if type(self.idle_observed) is not bool or (self.failure is None and not self.idle_observed):
            raise TypeError("Idle snapshot result has contradictory observation authority")


class _SerialTransport(Protocol):
    port: str | None
    baudrate: int | None
    timeout: float | None
    write_timeout: float | None
    rtscts: bool | None
    dsrdtr: bool | None
    dtr: bool | None
    rts: bool | None
    is_open: bool
    in_waiting: int

    def open(self) -> None: ...

    def write(self, value: bytes) -> int: ...

    def read_until(self, expected: bytes, size: int | None = None) -> bytes: ...

    def close(self) -> None: ...


SerialFactory = Callable[[], _SerialTransport]
Monotonic = Callable[[], float]


class SerialFluidNCFactory:
    """The sole production pyserial boundary; construction itself performs no I/O."""

    __slots__ = ("_config", "_monotonic", "_opened", "_serial_factory")

    def __init__(
        self,
        config: MachineExecutionConfig,
        *,
        serial_factory: SerialFactory | None = None,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if type(config) is not MachineExecutionConfig:
            raise TypeError("serial adapter requires an exact validated machine configuration")
        if serial_factory is not None and not callable(serial_factory):
            raise TypeError("serial factory must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic clock must be callable")
        self._config = config
        self._serial_factory = cast(SerialFactory, serial.Serial) if serial_factory is None else serial_factory
        self._monotonic = monotonic
        self._opened = False

    def open_session(self, request: OpenSessionRequest) -> FluidNCSession:
        if type(request) is not OpenSessionRequest:
            raise TypeError("serial open request must be exact")
        if self._opened:
            _deny("FLUIDNC_REOPEN_DENIED", "automatic or repeated FluidNC open is forbidden")
        self._opened = True
        transport: _SerialTransport | None = None
        open_attempted = False
        try:
            transport = self._serial_factory()
            initial_open = cast(object, transport.is_open)
            if type(initial_open) is not bool or initial_open:
                _deny("FLUIDNC_OPEN_FAILED", "FluidNC transport must be closed before configuration")
            _configure_transport(transport, self._config)
            opened_at = self._monotonic()
            open_attempted = True
            transport.open()
        except DrawingMachineError:
            if open_attempted and transport is not None:
                with suppress(Exception):
                    transport.close()
            raise
        except Exception:
            if open_attempted and transport is not None:
                with suppress(Exception):
                    transport.close()
            _deny("FLUIDNC_OPEN_FAILED", "FluidNC serial session could not be opened")
        try:
            opened = cast(object, transport.is_open)
        except Exception:
            with suppress(Exception):
                transport.close()
            _deny("FLUIDNC_OPEN_FAILED", "FluidNC serial session did not become open")
        if type(opened) is not bool or not opened:
            with suppress(Exception):
                transport.close()
            _deny("FLUIDNC_OPEN_FAILED", "FluidNC serial session did not become open")
        if self._monotonic() - opened_at > self._config.open_timeout_seconds:
            with suppress(Exception):
                transport.close()
            _deny("FLUIDNC_OPEN_TIMEOUT", "FluidNC serial open exceeded its bounded deadline")
        return _SerialFluidNCSession(
            transport,
            self._config,
            request.machine_session_epoch,
            self._monotonic,
        )


class _SerialFluidNCSession:
    __slots__ = ("_closed", "_config", "_epoch", "_monotonic", "_startup", "_startup_failure", "_transport")

    def __init__(
        self,
        transport: _SerialTransport,
        config: MachineExecutionConfig,
        epoch: str,
        monotonic: Monotonic,
    ) -> None:
        self._transport = transport
        self._config = config
        self._epoch = epoch
        self._monotonic = monotonic
        self._closed = False
        self._startup, self._startup_failure = self._read_startup()

    def read_initial_preflight(self, request: InitialPreflightRequest) -> PreflightOutcome:
        if type(request) is not InitialPreflightRequest:
            raise TypeError("initial preflight request must be exact")
        if self._startup_failure is not None:
            snapshot = _unavailable_snapshot()
            decision = classify_session_observation(
                self._startup,
                snapshot.status,
                SessionObservationPhase.FRESH_STABILIZATION,
            )
            return PreflightOutcome(
                self._epoch,
                snapshot,
                self._startup,
                SessionObservationPhase.FRESH_STABILIZATION,
                decision,
                self._startup_failure,
            )
        snapshot, evidence, failure, observation_phase = self._preflight(
            self._config.command_timeout_seconds,
            side_effect=False,
        )
        combined = _bounded_evidence((*self._startup, *evidence))
        decision = classify_session_observation(
            combined,
            snapshot.status,
            observation_phase,
        )
        return PreflightOutcome(
            self._epoch,
            snapshot,
            combined,
            observation_phase,
            decision,
            failure,
        )

    def home_all_axes(self, request: HomeAllAxesRequest) -> HomeOutcome:
        if type(request) is not HomeAllAxesRequest:
            raise TypeError("HOME request must be exact")
        self._bind_home(request)
        deadline = self._monotonic() + self._config.home_timeout_seconds
        command_result = self._command("$H", deadline)
        if command_result.failure is not None:
            failure = command_result.failure
            command_evidence = command_result.evidence
            snapshot = _snapshot_from_evidence(command_evidence)
            observation = self._late_observation(snapshot, command_evidence, failure)
            return HomeOutcome(
                self._epoch,
                request,
                snapshot,
                observation[0],
                *observation[1:],
                acknowledged_steps=1 if command_result.ack_observed else 0,
                stage=(
                    SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
                    if command_result.ack_observed
                    else SingleCommandOutcomeStage.DISPATCH_ATTEMPTED
                ),
            )
        command_evidence = command_result.evidence
        idle_result = self._wait_for_idle_snapshot(deadline, command_evidence)
        snapshot, evidence, idle_failure = idle_result.snapshot, idle_result.evidence, idle_result.failure
        if (
            idle_failure is None
            and evaluate_post_home(
                snapshot,
                expected_mpos=request.expected_mpos,
                tolerance_mm=request.tolerance_mm,
            ).disposition
            is not GateDisposition.READY
        ):
            idle_failure = _proof_failure("POST_HOME_PROOF_FAILED", "FluidNC post-HOME proof failed")
        observation = self._late_observation(snapshot, (*command_evidence, *evidence), idle_failure)
        if observation[0] is not None and idle_result.idle_observed:
            observation = (
                _action_postcondition_failure(
                    observation[0],
                    "POST_HOME_PROOF_FAILED",
                    "FluidNC post-HOME proof failed",
                ),
                *observation[1:],
            )
        stage = (
            SingleCommandOutcomeStage.POSTCONDITION_PROVEN
            if idle_failure is None
            else (
                SingleCommandOutcomeStage.IDLE_OBSERVED
                if idle_result.idle_observed
                else SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
            )
        )
        return HomeOutcome(
            self._epoch,
            request,
            snapshot,
            observation[0],
            *observation[1:],
            acknowledged_steps=1,
            stage=stage,
        )

    def run_z_calibration(self, request: ZCalibrationRequest) -> ZCalibrationOutcome:
        if type(request) is not ZCalibrationRequest:
            raise TypeError("ZCAL request must be exact")
        self._bind_zcal(request)
        commands = (
            "G21",
            "G90",
            "G54",
            f"G1 Z{_fixed(request.travel_z)} F{_fixed(request.travel_feed)}",
            f"G0 X{_fixed(request.center_x)} Y{_fixed(request.center_y)} F{_fixed(request.travel_feed)}",
            f"G1 Z{_fixed(request.contact_z)} F{_fixed(request.contact_feed)}",
        )
        attempted_stages = (
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
        acknowledged = 0
        all_evidence: list[str] = []
        failure: FluidNCFailure | None = None
        for step_index, command in enumerate(commands):
            command_deadline = self._monotonic() + self._config.command_timeout_seconds
            command_result = self._command(command, command_deadline)
            all_evidence.extend(command_result.evidence)
            if command_result.failure is not None:
                if command_result.ack_observed:
                    acknowledged += 1
                snapshot = _snapshot_from_evidence(tuple(all_evidence))
                observation = self._late_observation(snapshot, tuple(all_evidence), command_result.failure)
                return ZCalibrationOutcome(
                    self._epoch,
                    request,
                    acknowledged,
                    snapshot,
                    observation[0],
                    *observation[1:],
                    stage=(
                        acknowledged_stages[step_index] if command_result.ack_observed else attempted_stages[step_index]
                    ),
                )
            acknowledged += 1
        idle_deadline = self._monotonic() + self._config.stream_idle_timeout_seconds
        idle_result = self._wait_for_idle_snapshot(idle_deadline, tuple(all_evidence))
        snapshot, evidence, failure = idle_result.snapshot, idle_result.evidence, idle_result.failure
        all_evidence.extend(evidence)
        observation = self._late_observation(snapshot, tuple(all_evidence), failure)
        if observation[0] is not None and idle_result.idle_observed:
            observation = (
                _action_postcondition_failure(
                    observation[0],
                    "POST_ZCAL_PROOF_FAILED",
                    "FluidNC post-ZCAL Idle proof failed",
                ),
                *observation[1:],
            )
        return ZCalibrationOutcome(
            self._epoch,
            request,
            acknowledged,
            snapshot,
            observation[0],
            *observation[1:],
            stage=ZCalibrationOutcomeStage.POSTCONDITION,
        )

    def raise_after_z_confirmation(self, request: ZConfirmationRequest) -> ZConfirmationOutcome:
        if type(request) is not ZConfirmationRequest:
            raise TypeError("ZCONFIRM request must be exact")
        self._bind_zconfirm(request)
        command_deadline = self._monotonic() + self._config.command_timeout_seconds
        command = f"G1 Z{_fixed(request.travel_z)} F{_fixed(request.raise_feed)}"
        command_result = self._command(command, command_deadline)
        if command_result.failure is not None:
            failure = command_result.failure
            command_evidence = command_result.evidence
            snapshot = _snapshot_from_evidence(command_evidence)
            observation = self._late_observation(snapshot, command_evidence, failure)
            return ZConfirmationOutcome(
                self._epoch,
                request,
                snapshot,
                observation[0],
                *observation[1:],
                acknowledged_steps=1 if command_result.ack_observed else 0,
                stage=(
                    SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
                    if command_result.ack_observed
                    else SingleCommandOutcomeStage.DISPATCH_ATTEMPTED
                ),
            )
        command_evidence = command_result.evidence
        idle_deadline = self._monotonic() + self._config.stream_idle_timeout_seconds
        idle_result = self._wait_for_idle_snapshot(idle_deadline, command_evidence)
        snapshot, evidence, idle_failure = idle_result.snapshot, idle_result.evidence, idle_result.failure
        if (
            idle_failure is None
            and evaluate_calibrated_pose(
                snapshot,
                expected_wpos=request.expected_work_position,
                tolerance_mm=request.tolerance_mm,
            ).disposition
            is not GateDisposition.READY
        ):
            idle_failure = _proof_failure("POST_ZCONFIRM_PROOF_FAILED", "FluidNC post-ZCONFIRM proof failed")
        observation = self._late_observation(snapshot, (*command_evidence, *evidence), idle_failure)
        if observation[0] is not None and idle_result.idle_observed:
            observation = (
                _action_postcondition_failure(
                    observation[0],
                    "POST_ZCONFIRM_PROOF_FAILED",
                    "FluidNC post-ZCONFIRM proof failed",
                ),
                *observation[1:],
            )
        stage = (
            SingleCommandOutcomeStage.POSTCONDITION_PROVEN
            if idle_failure is None
            else (
                SingleCommandOutcomeStage.IDLE_OBSERVED
                if idle_result.idle_observed
                else SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
            )
        )
        return ZConfirmationOutcome(
            self._epoch,
            request,
            snapshot,
            observation[0],
            *observation[1:],
            acknowledged_steps=1,
            stage=stage,
        )

    def stream_program(
        self,
        request: StreamProgramRequest,
        progress: StreamProgressCallback,
    ) -> StreamOutcome:
        if type(request) is not StreamProgramRequest:
            raise TypeError("STREAM request must be exact")
        if not callable(progress):
            raise TypeError("STREAM progress callback must be callable")
        acknowledged = 0
        failure: FluidNCFailure | None = None
        all_evidence: list[str] = []
        for command in request.program.commands:
            command_deadline = self._monotonic() + self._config.command_timeout_seconds
            command_result = self._command(command.command, command_deadline)
            all_evidence.extend(command_result.evidence)
            if command_result.failure is not None:
                failure = command_result.failure
                snapshot = _snapshot_from_evidence(tuple(all_evidence))
                observation = self._late_observation(snapshot, tuple(all_evidence), failure)
                return StreamOutcome(
                    self._epoch,
                    len(request.program.commands),
                    acknowledged,
                    snapshot,
                    observation[0],
                    *observation[1:],
                )
            acknowledged += 1
            try:
                progress(
                    StreamProgressUpdate(
                        self._epoch,
                        acknowledged,
                        command.source_line,
                        hashlib.sha256(command.command.encode("ascii")).hexdigest(),
                    )
                )
            except DrawingMachineError:
                raise
            except Exception:
                failure = _failure(
                    FluidNCFailureKind.CONTROLLER_ERROR,
                    "PROGRESS_CALLBACK_FAILED",
                    "STREAM progress callback failed",
                    side_effect=True,
                )
                snapshot = _unavailable_snapshot()
                return StreamOutcome(
                    self._epoch,
                    len(request.program.commands),
                    acknowledged,
                    snapshot,
                    failure,
                )
        idle_deadline = self._monotonic() + self._config.stream_idle_timeout_seconds
        idle_result = self._wait_for_idle_snapshot(idle_deadline, tuple(all_evidence))
        snapshot, evidence, failure = idle_result.snapshot, idle_result.evidence, idle_result.failure
        all_evidence.extend(evidence)
        observation = self._late_observation(snapshot, tuple(all_evidence), failure)
        return StreamOutcome(
            self._epoch,
            len(request.program.commands),
            acknowledged,
            snapshot,
            observation[0],
            *observation[1:],
        )

    def home_z_axis(self, request: HomeZAxisRequest) -> HomeZOutcome:
        if type(request) is not HomeZAxisRequest:
            raise TypeError("HOME_Z request must be exact")
        deadline = self._monotonic() + self._config.home_timeout_seconds
        command_result = self._command("$HZ", deadline)
        if command_result.failure is not None:
            failure = command_result.failure
            command_evidence = command_result.evidence
            snapshot = _snapshot_from_evidence(command_evidence)
            observation = self._late_observation(snapshot, command_evidence, failure)
            return HomeZOutcome(self._epoch, request, snapshot, observation[0], *observation[1:])
        command_evidence = command_result.evidence
        idle_result = self._wait_for_idle_snapshot(deadline, command_evidence)
        snapshot, evidence, idle_failure = idle_result.snapshot, idle_result.evidence, idle_result.failure
        observation = self._late_observation(snapshot, (*command_evidence, *evidence), idle_failure)
        return HomeZOutcome(self._epoch, request, snapshot, observation[0], *observation[1:])

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        if type(request) is not CloseSessionRequest or request.machine_session_epoch != self._epoch:
            raise TypeError("close request must exactly bind the open session")
        if self._closed:
            return CloseOutcome(
                self._epoch,
                _failure(FluidNCFailureKind.CLOSE_ERROR, "SESSION_ALREADY_CLOSED", "FluidNC session is already closed"),
            )
        self._closed = True
        failed = False
        try:
            self._transport.close()
        except Exception:
            failed = True
        if failed:
            return CloseOutcome(
                self._epoch,
                _failure(FluidNCFailureKind.CLOSE_ERROR, "SERIAL_CLOSE_FAILED", "FluidNC serial close failed"),
            )
        return CloseOutcome(self._epoch)

    def _read_startup(self) -> tuple[tuple[str, ...], FluidNCFailure | None]:
        deadline = self._monotonic() + self._config.settle_timeout_seconds
        lines: list[str] = []
        total = 0
        banner_seen = False
        while self._monotonic() < deadline:
            raw, failure = self._read_raw(deadline, side_effect=False)
            if failure is not None:
                if failure.kind is FluidNCFailureKind.TIMEOUT:
                    if not lines or banner_seen:
                        return tuple(lines), None
                    continue
                return tuple(lines), failure
            assert raw is not None
            total += len(raw)
            if len(lines) >= _MAX_LINES or total > _MAX_TOTAL_BYTES:
                return tuple(lines), _failure(
                    FluidNCFailureKind.TIMEOUT,
                    "STARTUP_BOUNDS_EXCEEDED",
                    "FluidNC startup stabilization exceeded its bounds",
                )
            if raw.endswith(b"\n") and raw.strip(b"\r\n") == b"":
                continue
            line, decode_failure = _decode_line(raw, side_effect=False)
            if decode_failure is not None:
                return tuple(lines), decode_failure
            assert line is not None
            lines.append(line)
            if _is_ready_banner(line):
                banner_seen = True
        return tuple(lines), _failure(
            FluidNCFailureKind.TIMEOUT,
            "STARTUP_DEADLINE_EXCEEDED",
            "FluidNC startup stabilization exceeded its deadline",
        )

    def _preflight(
        self,
        timeout_seconds: float,
        *,
        side_effect: bool,
    ) -> tuple[
        PreflightSnapshot,
        tuple[str, ...],
        FluidNCFailure | None,
        SessionObservationPhase,
    ]:
        deadline = self._monotonic() + timeout_seconds
        evidence: list[str] = []
        status_line: str | None = None
        parser_lines: tuple[str, ...] = ()
        offset_lines: tuple[str, ...] = ()
        stabilized = False
        for command, expect_ok in (("?", False), ("$G", True), ("$#", True)):
            command_failure = self._write(
                command,
                newline=command != "?",
                deadline=deadline,
                side_effect=side_effect,
            )
            if command_failure is not None:
                return (
                    _unavailable_snapshot(),
                    tuple(evidence),
                    command_failure,
                    SessionObservationPhase.FRESH_STABILIZATION,
                )
            response_phase = (
                SessionObservationPhase.STABILIZED
                if stabilized
                else (
                    SessionObservationPhase.ACTION_ADMITTED
                    if side_effect
                    else SessionObservationPhase.FRESH_STABILIZATION
                )
            )
            lines, response_failure = self._response(
                expect_ok=expect_ok,
                deadline=deadline,
                side_effect=side_effect,
                phase=response_phase,
            )
            if response_failure is None and command == "?":
                first_status_line = next(
                    (line for line in lines if line.startswith("<") and line.endswith(">")),
                    None,
                )
                first_status = _status_from_line(first_status_line) if first_status_line is not None else None
                stabilized = (
                    first_status is not None
                    and first_status.state is FluidNCState.IDLE
                    and first_status.mpos is not None
                    and first_status.mpos != (0.0, 0.0, 0.0)
                    and first_status.wco is not None
                )
            if response_failure is None:
                drain_phase = SessionObservationPhase.STABILIZED if stabilized else response_phase
                drained, response_failure = self._drain_post_ack(
                    deadline,
                    side_effect=side_effect,
                    phase=drain_phase,
                )
                lines = (*lines, *drained)
            evidence.extend(lines)
            if response_failure is not None:
                snapshot, _ = _safe_snapshot(status_line, parser_lines, offset_lines, side_effect=side_effect)
                if response_failure.kind is FluidNCFailureKind.CONTROLLER_RESET and stabilized:
                    late_status = _last_status(lines)
                    if late_status is not None:
                        snapshot = _snapshot_from_evidence(lines)
                late_reset = response_failure.kind is FluidNCFailureKind.CONTROLLER_RESET and stabilized
                return (
                    snapshot,
                    tuple(evidence),
                    response_failure,
                    (SessionObservationPhase.STABILIZED if late_reset else SessionObservationPhase.FRESH_STABILIZATION),
                )
            if command == "?":
                status_line = next((line for line in lines if line.startswith("<") and line.endswith(">")), None)
            elif command == "$G":
                parser_lines = tuple(line for line in lines if line != "ok")
            else:
                offset_lines = tuple(line for line in lines if line != "ok")
        snapshot, parse_failure = _safe_snapshot(status_line, parser_lines, offset_lines, side_effect=side_effect)
        return snapshot, tuple(evidence), parse_failure, SessionObservationPhase.FRESH_STABILIZATION

    def _command(self, command: str, deadline: float) -> _CommandResult:
        write_failure = self._write(command, newline=True, deadline=deadline, side_effect=True)
        if write_failure is not None:
            return _CommandResult(False, write_failure, (), _CommandObservationStage.PRE_ACK_FAILURE)
        lines, failure = self._response(
            expect_ok=True,
            deadline=deadline,
            side_effect=True,
            phase=SessionObservationPhase.ACTION_ADMITTED,
        )
        if failure is not None:
            return _CommandResult(False, failure, lines, _CommandObservationStage.PRE_ACK_FAILURE)
        drained, drain_failure = self._drain_post_ack(deadline)
        evidence = (*lines, *drained)
        if drain_failure is not None:
            return _CommandResult(True, drain_failure, evidence, _CommandObservationStage.POST_ACK_FAILURE)
        return _CommandResult(True, None, evidence, _CommandObservationStage.ACK_OBSERVED)

    def _write(
        self,
        command: str,
        *,
        newline: bool,
        deadline: float,
        side_effect: bool,
    ) -> FluidNCFailure | None:
        if self._closed or not self._transport.is_open:
            return _failure(
                FluidNCFailureKind.TRANSPORT_LOSS,
                "SERIAL_SESSION_CLOSED",
                "FluidNC serial session is closed",
                side_effect=side_effect,
            )
        remaining = deadline - self._monotonic()
        if remaining <= 0.0:
            return _deadline_failure(side_effect)
        self._transport.write_timeout = min(self._config.command_timeout_seconds, remaining)
        payload = command.encode("ascii") + (self._config.line_ending.encode("ascii") if newline else b"")
        try:
            written = self._transport.write(payload)
        except Exception:
            return _failure(
                FluidNCFailureKind.TRANSPORT_LOSS,
                "SERIAL_WRITE_FAILED",
                "FluidNC serial write failed",
                side_effect=side_effect,
            )
        if self._monotonic() >= deadline:
            return _deadline_failure(side_effect)
        if type(written) is not int or written != len(payload):
            return _failure(
                FluidNCFailureKind.SHORT_WRITE,
                "SERIAL_SHORT_WRITE",
                "FluidNC serial write was incomplete",
                side_effect=True,
            )
        return None

    def _response(
        self,
        *,
        expect_ok: bool,
        deadline: float,
        side_effect: bool,
        phase: SessionObservationPhase,
    ) -> tuple[tuple[str, ...], FluidNCFailure | None]:
        lines: list[str] = []
        total = 0
        while self._monotonic() < deadline:
            raw, read_failure = self._read_raw(deadline, side_effect=side_effect)
            if read_failure is not None:
                if read_failure.code == "SERIAL_READ_TIMEOUT":
                    continue
                return tuple(lines), read_failure
            assert raw is not None
            total += len(raw)
            if len(lines) >= _MAX_LINES or total > _MAX_TOTAL_BYTES:
                return tuple(lines), _failure(
                    FluidNCFailureKind.LOST_ACK if side_effect else FluidNCFailureKind.TIMEOUT,
                    "SERIAL_RESPONSE_BOUNDS_EXCEEDED",
                    "FluidNC response exceeded its bounds",
                    side_effect=side_effect,
                )
            line, decode_failure = _decode_line(raw, side_effect=side_effect)
            if decode_failure is not None:
                return tuple(lines), decode_failure
            assert line is not None
            lines.append(line)
            line_failure = _line_failure(line, phase, side_effect=side_effect)
            if line_failure is not None:
                return tuple(lines), line_failure
            lowered = line.casefold()
            if expect_ok and lowered == "ok":
                return tuple(lines), None
            if not expect_ok and line.startswith("<") and line.endswith(">"):
                return tuple(lines), None
        return tuple(lines), _failure(
            FluidNCFailureKind.LOST_ACK if side_effect else FluidNCFailureKind.TIMEOUT,
            "SERIAL_RESPONSE_TIMEOUT",
            "FluidNC response deadline expired",
            side_effect=side_effect,
        )

    def _read_raw(self, deadline: float, *, side_effect: bool) -> tuple[bytes | None, FluidNCFailure | None]:
        remaining = deadline - self._monotonic()
        if remaining <= 0.0:
            return None, _deadline_failure(side_effect)
        self._transport.timeout = min(self._config.read_timeout_seconds, remaining)
        try:
            raw = self._transport.read_until(b"\n", _MAX_LINE_BYTES + 1)
        except Exception:
            return None, _failure(
                FluidNCFailureKind.TRANSPORT_LOSS,
                "SERIAL_READ_FAILED",
                "FluidNC serial read failed",
                side_effect=side_effect,
            )
        if self._monotonic() >= deadline:
            return None, _deadline_failure(side_effect)
        if type(raw) is not bytes:
            return None, _failure(
                FluidNCFailureKind.DECODE_ERROR,
                "SERIAL_READ_TYPE_INVALID",
                "FluidNC serial read returned an invalid byte value",
                side_effect=side_effect,
            )
        if not raw:
            if not self._transport.is_open:
                return None, _failure(
                    FluidNCFailureKind.TRANSPORT_LOSS,
                    "SERIAL_EOF",
                    "FluidNC serial connection reached EOF",
                    side_effect=side_effect,
                )
            return None, _failure(
                FluidNCFailureKind.LOST_ACK if side_effect else FluidNCFailureKind.TIMEOUT,
                "SERIAL_READ_TIMEOUT",
                "FluidNC serial read timed out",
                side_effect=side_effect,
            )
        return raw, None

    def _drain_post_ack(
        self,
        deadline: float,
        *,
        side_effect: bool = True,
        phase: SessionObservationPhase = SessionObservationPhase.ACTION_ADMITTED,
    ) -> tuple[tuple[str, ...], FluidNCFailure | None]:
        lines: list[str] = []
        total = 0
        while True:
            try:
                waiting = cast(object, self._transport.in_waiting)
            except Exception:
                return tuple(lines), _failure(
                    FluidNCFailureKind.TRANSPORT_LOSS,
                    "SERIAL_WAITING_FAILED",
                    "FluidNC pending-response inspection failed",
                    side_effect=side_effect,
                )
            if self._monotonic() >= deadline:
                return tuple(lines), _deadline_failure(side_effect)
            if type(waiting) is not int or waiting < 0:
                return tuple(lines), _failure(
                    FluidNCFailureKind.DECODE_ERROR,
                    "SERIAL_WAITING_INVALID",
                    "FluidNC pending-response count was invalid",
                    side_effect=side_effect,
                )
            if waiting == 0:
                return tuple(lines), None
            raw, read_failure = self._read_raw(deadline, side_effect=side_effect)
            if read_failure is not None:
                return tuple(lines), read_failure
            assert raw is not None
            total += len(raw)
            if len(lines) >= _MAX_LINES or total > _MAX_TOTAL_BYTES:
                return tuple(lines), _failure(
                    FluidNCFailureKind.LOST_ACK if side_effect else FluidNCFailureKind.TIMEOUT,
                    "SERIAL_RESPONSE_BOUNDS_EXCEEDED",
                    "FluidNC post-ACK response exceeded its bounds",
                    side_effect=side_effect,
                )
            line, decode_failure = _decode_line(raw, side_effect=side_effect)
            if decode_failure is not None:
                return tuple(lines), decode_failure
            assert line is not None
            lines.append(line)
            line_failure = _line_failure(line, phase, side_effect=side_effect)
            if line_failure is not None:
                return tuple(lines), line_failure

    def _wait_for_idle_snapshot(
        self,
        deadline: float,
        initial_evidence: tuple[str, ...],
    ) -> _IdleSnapshotResult:
        evidence = list(initial_evidence)
        status = _last_status(initial_evidence)
        status_line: str | None = None if status is None else status.status_line
        while status is None or status.state is not FluidNCState.IDLE:
            if status is not None and status.state not in {
                FluidNCState.RUN,
                FluidNCState.HOLD,
                FluidNCState.HOME,
            }:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)),
                    tuple(evidence),
                    _proof_failure("CONTROLLER_NOT_IDLE", "FluidNC entered an unsafe non-Idle state"),
                    False,
                )
            write_failure = self._write("?", newline=False, deadline=deadline, side_effect=True)
            if write_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)), tuple(evidence), write_failure, False
                )
            lines, response_failure = self._response(
                expect_ok=False,
                deadline=deadline,
                side_effect=True,
                phase=SessionObservationPhase.ACTION_ADMITTED,
            )
            evidence.extend(lines)
            if response_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)), tuple(evidence), response_failure, False
                )
            status = _last_status(lines)
            assert status is not None
            status_line = status.status_line
            drained, drain_failure = self._drain_post_ack(deadline)
            evidence.extend(drained)
            if drain_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)),
                    tuple(evidence),
                    drain_failure,
                    status.state is FluidNCState.IDLE,
                )
            status = _last_status((*lines, *drained))
            assert status is not None
            status_line = status.status_line

        parser_lines: tuple[str, ...] = ()
        offset_lines: tuple[str, ...] = ()
        for command in ("$G", "$#"):
            write_failure = self._write(command, newline=True, deadline=deadline, side_effect=True)
            if write_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)), tuple(evidence), write_failure, True
                )
            lines, response_failure = self._response(
                expect_ok=True,
                deadline=deadline,
                side_effect=True,
                phase=SessionObservationPhase.ACTION_ADMITTED,
            )
            evidence.extend(lines)
            if response_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)), tuple(evidence), response_failure, True
                )
            drained, drain_failure = self._drain_post_ack(deadline)
            evidence.extend(drained)
            if drain_failure is not None:
                return _IdleSnapshotResult(
                    _snapshot_from_evidence(tuple(evidence)), tuple(evidence), drain_failure, True
                )
            values = tuple(line for line in (*lines, *drained) if line != "ok")
            if command == "$G":
                parser_lines = values
            else:
                offset_lines = values
        snapshot, failure = _safe_snapshot(
            status_line,
            parser_lines,
            offset_lines,
            side_effect=True,
        )
        return _IdleSnapshotResult(snapshot, tuple(evidence), failure, True)

    def _bind_home(self, request: HomeAllAxesRequest) -> None:
        if not _same_vector(request.expected_mpos, self._config.expected_homed_mpos) or not _same_number(
            request.tolerance_mm, self._config.position_tolerance_mm
        ):
            _request_mismatch()

    def _bind_zcal(self, request: ZCalibrationRequest) -> None:
        profile = self._config.gcode
        if (
            request.work_coordinate != profile.work_coordinate
            or not _same_number(request.center_x, profile.paper_center_x)
            or not _same_number(request.center_y, profile.paper_center_y)
            or not _same_number(request.travel_z, profile.pen_up_z)
            or not _same_number(request.contact_z, profile.pen_down_z)
            or not _same_number(request.travel_feed, profile.feed_travel)
            or not _same_number(request.contact_feed, profile.feed_pen_down)
        ):
            _request_mismatch()

    def _bind_zconfirm(self, request: ZConfirmationRequest) -> None:
        profile = self._config.gcode
        expected = (profile.paper_center_x, profile.paper_center_y, profile.pen_up_z)
        if (
            not _same_vector(request.expected_work_position, expected)
            or not _same_number(request.travel_z, profile.pen_up_z)
            or not _same_number(request.raise_feed, profile.feed_pen_up)
            or not _same_number(request.tolerance_mm, self._config.position_tolerance_mm)
        ):
            _request_mismatch()

    def _late_observation(
        self,
        snapshot: PreflightSnapshot,
        evidence: tuple[str, ...],
        failure: FluidNCFailure | None,
    ) -> tuple[
        FluidNCFailure | None,
        tuple[str, ...] | None,
        SessionObservationPhase | None,
        GateDecision | None,
    ]:
        accepted = _bounded_evidence(evidence)
        decision = classify_session_observation(accepted, snapshot.status, SessionObservationPhase.ACTION_ADMITTED)
        if decision.disposition is GateDisposition.RECOVERY_REQUIRED:
            return (
                _failure(
                    FluidNCFailureKind.CONTROLLER_RESET,
                    "CONTROLLER_RESET_DETECTED",
                    "FluidNC reset evidence invalidated the active session",
                    side_effect=True,
                ),
                accepted,
                SessionObservationPhase.ACTION_ADMITTED,
                decision,
            )
        return failure, None, None, None


def _configure_transport(transport: _SerialTransport, config: MachineExecutionConfig) -> None:
    transport.port = config.serial_port
    transport.baudrate = config.baud
    transport.timeout = config.read_timeout_seconds
    transport.write_timeout = config.open_timeout_seconds
    transport.rtscts = False
    transport.dsrdtr = False
    transport.dtr = False
    transport.rts = False


def _is_ready_banner(line: str) -> bool:
    return line.casefold().startswith("grbl ")


def _decode_line(raw: bytes, *, side_effect: bool) -> tuple[str | None, FluidNCFailure | None]:
    if len(raw) > _MAX_LINE_BYTES:
        return None, _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "SERIAL_LINE_TOO_LONG",
            "FluidNC returned an oversized line",
            side_effect=side_effect,
        )
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "SERIAL_DECODE_FAILED",
            "FluidNC returned invalid UTF-8",
            side_effect=side_effect,
        )
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    else:
        return None, _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "SERIAL_LINE_ENDING_INVALID",
            "FluidNC returned a line without a complete ending",
            side_effect=side_effect,
        )
    if not value or any(_forbidden_character(character) for character in value):
        return None, _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "SERIAL_LINE_INVALID",
            "FluidNC returned an invalid line",
            side_effect=side_effect,
        )
    return value, None


def _snapshot_or_unavailable(
    status_line: str | None,
    parser_lines: tuple[str, ...],
    offset_lines: tuple[str, ...],
) -> PreflightSnapshot:
    return parse_preflight_snapshot(
        status_line="<Unavailable>" if status_line is None else status_line,
        parser_state=parser_lines,
        offset_lines=offset_lines,
        errors=(),
        missing_ok_commands=(),
    )


def _snapshot_from_evidence(evidence: tuple[str, ...]) -> PreflightSnapshot:
    status = _last_status(evidence)
    if status is None:
        return _unavailable_snapshot()
    return parse_preflight_snapshot(
        status_line=status.status_line,
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )


def _unavailable_snapshot() -> PreflightSnapshot:
    return _snapshot_or_unavailable(None, (), ())


def _safe_snapshot(
    status_line: str | None,
    parser_lines: tuple[str, ...],
    offset_lines: tuple[str, ...],
    *,
    side_effect: bool,
) -> tuple[PreflightSnapshot, FluidNCFailure | None]:
    try:
        return _snapshot_or_unavailable(status_line, parser_lines, offset_lines), None
    except DrawingMachineError:
        return _unavailable_snapshot(), _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "CONTROLLER_SNAPSHOT_INVALID",
            "FluidNC returned an invalid preflight snapshot",
            side_effect=side_effect,
        )


def _fixed(value: float) -> str:
    return format(value, ".3f")


def _forbidden_character(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("C") or category in {"Zl", "Zp"}


def _line_failure(
    line: str,
    phase: SessionObservationPhase,
    *,
    side_effect: bool,
) -> FluidNCFailure | None:
    lowered = line.casefold()
    if lowered.startswith("alarm"):
        return _failure(
            FluidNCFailureKind.ALARM,
            "CONTROLLER_ALARM",
            "FluidNC returned an alarm",
            side_effect=side_effect,
        )
    if lowered.startswith("error"):
        return _failure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "CONTROLLER_ERROR",
            "FluidNC returned an error",
            side_effect=side_effect,
        )
    status = _status_from_line(line)
    if line.startswith("<") and line.endswith(">") and status is None:
        return _failure(
            FluidNCFailureKind.DECODE_ERROR,
            "CONTROLLER_STATUS_INVALID",
            "FluidNC returned an invalid status line",
            side_effect=side_effect,
        )
    if status is not None and status.state is FluidNCState.ALARM:
        return _failure(
            FluidNCFailureKind.ALARM,
            "CONTROLLER_ALARM",
            "FluidNC reported the Alarm state",
            side_effect=side_effect,
        )
    probe = _unavailable_snapshot().status if status is None else status
    decision = classify_session_observation((line,), probe, phase)
    if decision.disposition is GateDisposition.RECOVERY_REQUIRED:
        return _failure(
            FluidNCFailureKind.CONTROLLER_RESET,
            "CONTROLLER_RESET_DETECTED",
            "FluidNC reset evidence invalidated the active session",
            side_effect=True,
        )
    return None


def _status_from_line(line: str) -> ControllerStatus | None:
    if not line.startswith("<") or not line.endswith(">"):
        return None
    try:
        return parse_controller_status(line)
    except DrawingMachineError:
        return None


def _last_status(lines: tuple[str, ...]) -> ControllerStatus | None:
    for line in reversed(lines):
        status = _status_from_line(line)
        if status is not None:
            return status
    return None


def _same_number(left: float, right: float) -> bool:
    return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)


def _same_vector(left: tuple[float, float, float], right: tuple[float, float, float]) -> bool:
    return all(_same_number(actual, expected) for actual, expected in zip(left, right, strict=True))


def _request_mismatch() -> NoReturn:
    _deny(
        "FLUIDNC_REQUEST_CONFIG_MISMATCH",
        "typed FluidNC request does not match the validated machine configuration",
    )


def _deadline_failure(side_effect: bool) -> FluidNCFailure:
    return _failure(
        FluidNCFailureKind.LOST_ACK if side_effect else FluidNCFailureKind.TIMEOUT,
        "SERIAL_RESPONSE_TIMEOUT",
        "FluidNC absolute operation deadline expired",
        side_effect=side_effect,
    )


def _bounded_evidence(lines: tuple[str, ...]) -> tuple[str, ...]:
    accepted: list[str] = []
    total = 0
    for line in lines:
        encoded = line.encode("utf-8")
        if len(accepted) >= _MAX_LINES or total + len(encoded) > _MAX_TOTAL_BYTES:
            break
        accepted.append(line)
        total += len(encoded)
    return tuple(accepted)


def _failure(
    kind: FluidNCFailureKind,
    code: str,
    message: str,
    *,
    side_effect: bool = False,
) -> FluidNCFailure:
    return FluidNCFailure(kind, code, message, side_effect)


def _proof_failure(code: str, message: str) -> FluidNCFailure:
    return _failure(FluidNCFailureKind.CONTROLLER_ERROR, code, message, side_effect=True)


def _action_postcondition_failure(
    failure: FluidNCFailure,
    code: str,
    message: str,
) -> FluidNCFailure:
    return FluidNCFailure(failure.kind, code, message, True)


def _deny(code: str, message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.HARDWARE,
            message=message,
            retryable=False,
            details={},
        )
    )


__all__ = ["SerialFluidNCFactory"]
