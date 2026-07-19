from __future__ import annotations

import hashlib
import inspect
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import drawingmachine.adapters.hardware.serial_fluidnc as serial_module
from drawingmachine.adapters.hardware.serial_fluidnc import SerialFluidNCFactory
from drawingmachine.config.machine import MachineExecutionConfig, parse_machine_execution_config
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.domain.fluidnc.gates import SessionObservationPhase
from drawingmachine.domain.gcode.stream_program import build_validated_stream_program
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.fluidnc import (
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    HomeAllAxesRequest,
    HomeZAxisRequest,
    InitialPreflightRequest,
    OpenSessionRequest,
    SingleCommandOutcomeStage,
    StreamProgramRequest,
    ZCalibrationOutcomeStage,
    ZCalibrationRequest,
    ZConfirmationRequest,
)

EPOCH = str(UUID(int=8))
GRBL_READY_BANNER = b"Grbl 4.0 [FluidNC v4.0.2 (esp32-wifi) '$' for help]\n"


class FakeSerial:
    def __init__(self, reads: list[object]) -> None:
        self.port: str | None = None
        self.baudrate: int | None = None
        self.timeout: float | None = None
        self.write_timeout: float | None = None
        self.rtscts: bool | None = None
        self.dsrdtr: bool | None = None
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.is_open = False
        self.in_waiting = 0
        self.post_ack_waiting = 0
        self.reads = list(reads)
        self.writes: list[bytes] = []
        self.open_count = 0
        self.configured_at_open: tuple[object, ...] | None = None
        self.close_count = 0
        self.flush_count = 0
        self.read_bounds: list[tuple[bytes, int | None]] = []
        self.read_timeouts: list[float | None] = []
        self.write_timeouts: list[float | None] = []
        self.short_write: int | None = None
        self.write_error: BaseException | None = None
        self.open_error: BaseException | None = None
        self.flush_error: BaseException | None = None
        self.close_error: BaseException | None = None

    def open(self) -> None:
        self.open_count += 1
        self.configured_at_open = (
            self.port,
            self.baudrate,
            self.timeout,
            self.write_timeout,
            self.rtscts,
            self.dsrdtr,
            self.dtr,
            self.rts,
        )
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        self.write_timeouts.append(self.write_timeout)
        if self.write_error is not None:
            raise self.write_error
        return len(value) if self.short_write is None else self.short_write

    def flush(self) -> None:
        self.flush_count += 1
        if self.flush_error is not None:
            raise self.flush_error

    def read_until(self, expected: bytes, size: int | None = None) -> Any:
        self.read_bounds.append((expected, size))
        self.read_timeouts.append(self.timeout)
        if not self.reads:
            return b""
        value = self.reads.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value()
        if isinstance(value, bytes) and value.lower() == b"ok\n" and self.post_ack_waiting:
            self.in_waiting = self.post_ack_waiting
            self.post_ack_waiting = 0
        elif self.in_waiting > 0 and isinstance(value, bytes):
            self.in_waiting = max(0, self.in_waiting - len(value))
        return value

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error
        self.is_open = False


class StepClock:
    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        result = self.value
        self.value += self.step
        return result


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        return self.values.pop(0) if self.values else 1000.0


def _profile() -> ProfileEnvelope:
    return ProfileEnvelope(
        1,
        {
            "name": "default",
            "planning": {
                "canvas_width_mm": 120.0,
                "canvas_height_mm": 120.0,
                "pen_width_mm": 0.5,
                "min_gap_mm": 0.8,
                "threshold": 128,
                "invert": False,
                "min_component_area_px": 8,
                "simplify_tolerance_mm": 0.12,
                "min_path_length_mm": 0.6,
                "drop_short_stroke_mm": 0.35,
                "merge_endpoint_distance_mm": 0.45,
                "merge_angle_deg": 35.0,
                "dedupe_short_path_length_mm": 2.0,
                "dedupe_distance_mm": 0.3,
                "dedupe_angle_deg": 25.0,
                "dedupe_overlap_ratio": 0.65,
                "hatch_spacing_mm": 0.8,
                "hatch_min_run_mm": 0.8,
                "fill_min_thickness_mm": 0.85,
            },
            "gcode": {
                "hardware_canvas_width_mm": 144.0,
                "hardware_canvas_height_mm": 144.0,
                "machine_width_mm": 192.0,
                "machine_height_mm": 192.0,
                "paper_center_x": 96.0,
                "paper_center_y": 96.0,
                "pen_up_z": 3.5,
                "pen_down_z": 0.0,
                "feed_travel": 1200.0,
                "feed_draw": 900.0,
                "feed_pen_down": 100.0,
                "feed_pen_up": 400.0,
                "max_feed": 1200.0,
                "work_coordinate": "G54",
                "align_mode": "center",
                "mirror_y": True,
                "safe_start": True,
                "path_mode": "stroke",
            },
            "hardware": {
                "firmware": "FluidNC",
                "transport": "serial",
                "serial_port": "/dev/serial/by-id/fluidnc-controller",
                "baud": 115200,
                "line_ending": "\n",
                "open_timeout_seconds": 2.0,
                "settle_timeout_seconds": 15.0,
                "read_timeout_seconds": 1.0,
                "command_timeout_seconds": 10.0,
                "home_timeout_seconds": 300.0,
                "stream_idle_timeout_seconds": 30.0,
                "approval_ttl_seconds": 60,
                "require_g54_offset": True,
                "expected_homed_mpos": [192.0, 192.0, 192.0],
                "position_tolerance_mm": 0.5,
                "post_run_home_z": True,
                "automation_principal": {"uid": 1100, "gid": 1100},
                "operator_principal": {"uid": 2200, "gid": 2200},
            },
        },
    )


def _config() -> MachineExecutionConfig:
    return parse_machine_execution_config(_profile(), profile_path=Path("/config/machines/default.toml"))


def _factory(fake: FakeSerial, *, clock: Callable[[], float] | None = None) -> SerialFluidNCFactory:
    return SerialFluidNCFactory(_config(), serial_factory=lambda: fake, monotonic=clock or StepClock(0.1))


def _preflight_reads(status: str = "<Idle|MPos:0.000,0.000,0.000|WCO:0.000,0.000,0.000>") -> list[bytes]:
    return [
        (status + "\n").encode(),
        b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\n",
        b"ok\n",
        b"[G54:0.000,0.000,0.000]\n",
        b"ok\n",
    ]


def _open(fake: FakeSerial, *, clock: Callable[[], float] | None = None) -> Any:
    return _factory(fake, clock=clock).open_session(OpenSessionRequest(EPOCH))


def _program() -> Any:
    source = b"G21\nG90\n"
    summary = {
        "raw_line_count": 2,
        "blank_line_count": 0,
        "comment_only_line_count": 0,
        "sendable_line_count": 2,
        "estimated_serial_bytes": 8,
        "max_send_line_length": 3,
        "first_sendable_lines": [{"source_line": 1, "command": "G21"}, {"source_line": 2, "command": "G90"}],
        "last_sendable_lines": [{"source_line": 1, "command": "G21"}, {"source_line": 2, "command": "G90"}],
        "streaming_mode": "conservative_send_line_wait_ok",
    }
    return build_validated_stream_program(source, summary, hashlib.sha256(source).hexdigest())


def test_private_command_and_idle_results_reject_every_contradictory_closed_field() -> None:
    failure = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "fixed timeout", True)
    command_stage = serial_module._CommandObservationStage
    for values in (
        (1, failure, (), command_stage.PRE_ACK_FAILURE),
        (False, object(), (), command_stage.PRE_ACK_FAILURE),
        (False, failure, [], command_stage.PRE_ACK_FAILURE),
        (False, failure, (1,), command_stage.PRE_ACK_FAILURE),
        (True, failure, (), object()),
        (True, failure, (), command_stage.PRE_ACK_FAILURE),
        (False, None, (), command_stage.ACK_OBSERVED),
        (False, failure, (), command_stage.POST_ACK_FAILURE),
    ):
        with pytest.raises(TypeError):
            serial_module._CommandResult(*values)

    snapshot = serial_module._unavailable_snapshot()
    for values in (
        (object(), (), None, True),
        (snapshot, [], None, True),
        (snapshot, (), object(), True),
        (snapshot, (), None, object()),
        (snapshot, (), None, False),
    ):
        with pytest.raises(TypeError):
            serial_module._IdleSnapshotResult(*values)


def test_open_configures_injected_unopened_transport_before_exactly_one_open() -> None:
    fake = FakeSerial([])
    session = _open(fake)
    assert fake.open_count == 1
    assert (fake.port, fake.baudrate, fake.timeout, fake.write_timeout) == (
        "/dev/serial/by-id/fluidnc-controller",
        115200,
        1.0,
        2.0,
    )
    assert (fake.rtscts, fake.dsrdtr, fake.dtr, fake.rts) == (False, False, False, False)
    assert fake.configured_at_open == (
        "/dev/serial/by-id/fluidnc-controller",
        115200,
        1.0,
        2.0,
        False,
        False,
        False,
        False,
    )
    assert not hasattr(session, "send_command")
    assert fake.read_bounds == [(b"\n", 4097)]


def test_initial_preflight_classifies_fresh_reset_as_await_home() -> None:
    fake = FakeSerial([b"FluidNC reset\n", GRBL_READY_BANNER, b"", *_preflight_reads()])
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is None
    assert outcome.decision.disposition.value == "AWAIT_HOME_ONLY"
    assert outcome.evidence[0] == "FluidNC reset"
    assert fake.writes == [b"?", b"$G\n", b"$#\n"]


def test_initial_preflight_first_zero_position_without_startup_banner_remains_fresh() -> None:
    fake = FakeSerial([b"", *_preflight_reads()])
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is None
    assert outcome.observation_phase is SessionObservationPhase.FRESH_STABILIZATION
    assert outcome.decision.disposition.value == "AWAIT_HOME_ONLY"
    assert fake.writes == [b"?", b"$G\n", b"$#\n"]


@pytest.mark.parametrize(
    "late_line",
    [
        b"FluidNC reset\n",
        b"<Idle|MPos:0.000,0.000,0.000|WCO:0.000,0.000,0.000>\n",
    ],
)
def test_initial_preflight_rejects_reset_or_zero_position_after_stable_status(late_line: bytes) -> None:
    fake = FakeSerial(
        [
            b"FluidNC reset\n",
            GRBL_READY_BANNER,
            b"",
            b"<Idle|MPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>\n",
            late_line,
            b"ok\n",
            b"[G54:0.000,0.000,0.000]\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.observation_phase is SessionObservationPhase.STABILIZED
    assert outcome.decision.disposition.value == "RECOVERY_REQUIRED"
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert late_line.decode().strip() in outcome.evidence
    assert fake.writes == [b"?", b"$G\n"]


def test_initial_preflight_rejects_reset_during_offsets_after_stable_status() -> None:
    fake = FakeSerial(
        [
            b"FluidNC reset\n",
            GRBL_READY_BANNER,
            b"",
            b"<Idle|MPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>\n",
            b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\n",
            b"ok\n",
            b"FluidNC reset\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.observation_phase is SessionObservationPhase.STABILIZED
    assert outcome.decision.disposition.value == "RECOVERY_REQUIRED"
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert fake.writes == [b"?", b"$G\n", b"$#\n"]


def test_initial_preflight_drains_post_ack_reset_before_offsets_write() -> None:
    reset = b"watchdog reset\n"
    fake = FakeSerial(
        [
            b"FluidNC reset\n",
            GRBL_READY_BANNER,
            b"",
            b"<Idle|MPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>\n",
            b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\n",
            b"ok\n",
            reset,
            b"[G54:0.000,0.000,0.000]\n",
            b"ok\n",
        ]
    )
    fake.post_ack_waiting = len(reset)
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.observation_phase is SessionObservationPhase.STABILIZED
    assert outcome.decision.disposition.value == "RECOVERY_REQUIRED"
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert fake.writes == [b"?", b"$G\n"]


def test_fixed_operations_format_typed_numbers_and_never_accept_raw_command() -> None:
    reads = [
        b"",
        b"ok\n",
        *_preflight_reads("<Idle|MPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>"),
        *(b"ok\n" for _ in range(6)),
        *_preflight_reads("<Idle|MPos:96.000,96.000,0.000|WCO:0.000,0.000,0.000>"),
        b"ok\n",
        *_preflight_reads("<Idle|MPos:96.000,96.000,3.500|WPos:96.000,96.000,3.500|WCO:0.000,0.000,0.000>"),
    ]
    fake = FakeSerial(reads)
    session = _open(fake)
    home = session.home_all_axes(HomeAllAxesRequest((192.0, 192.0, 192.0), 0.5))
    zcal = session.run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    zconfirm = session.raise_after_z_confirmation(ZConfirmationRequest((96, 96, 3.5), 3.5, 400, 0.5))
    assert home.failure is zcal.failure is zconfirm.failure is None
    assert home.acknowledged_steps == zconfirm.acknowledged_steps == 1
    assert home.stage is zconfirm.stage is SingleCommandOutcomeStage.POSTCONDITION_PROVEN
    assert fake.writes[:1] == [b"$H\n"]
    assert b"G0 X96.000 Y96.000 F1200.000\n" in fake.writes
    assert b"G1 Z3.500 F400.000\n" in fake.writes


def test_stream_is_one_line_one_ack_reports_progress_and_proves_final_idle() -> None:
    fake = FakeSerial([b"", b"ok\n", b"ok\n", *_preflight_reads("<Idle|MPos:96,96,3.5|WCO:0,0,0>")])
    updates: list[Any] = []
    outcome = _open(fake).stream_program(StreamProgramRequest(_program()), updates.append)
    assert outcome.failure is None
    assert outcome.acknowledged_commands == 2
    assert [item.source_line for item in updates] == [1, 2]
    assert [item.command_digest for item in updates] == [
        hashlib.sha256(b"G21").hexdigest(),
        hashlib.sha256(b"G90").hexdigest(),
    ]
    assert fake.writes[:2] == [b"G21\n", b"G90\n"]


def test_stream_post_ack_drain_failure_preserves_c8_zero_ack_and_zero_progress() -> None:
    reset = b"watchdog reset\n"
    fake = FakeSerial([b""])

    def ack_with_pending_reset() -> bytes:
        fake.post_ack_waiting = len(reset)
        return b"ok\n"

    fake.reads.extend((ack_with_pending_reset, reset))
    updates: list[Any] = []
    outcome = _open(fake).stream_program(StreamProgramRequest(_program()), updates.append)
    assert outcome.failure is not None and outcome.failure.kind is FluidNCFailureKind.CONTROLLER_RESET
    assert outcome.acknowledged_commands == 0
    assert updates == []
    assert fake.writes == [b"G21\n"]


def test_short_write_flush_lost_ack_and_first_controller_error_stop() -> None:
    fake = FakeSerial([])
    fake.short_write = 1
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "SHORT_WRITE"
    assert outcome.failure.side_effect_may_have_occurred

    fake = FakeSerial([b"", b"error:20\n", b"ok\n"])
    outcome = _open(fake).stream_program(StreamProgramRequest(_program()), lambda update: None)
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_ERROR"
    assert outcome.acknowledged_commands == 0
    assert fake.writes[0] == b"G21\n"
    assert b"G90\n" not in fake.writes


def test_close_failure_is_typed_and_does_not_reopen() -> None:
    fake = FakeSerial([])
    fake.close_error = OSError("secret device detail")
    session = _open(fake)
    outcome = session.close(CloseSessionRequest(EPOCH))
    assert outcome.failure is not None and outcome.failure.kind.value == "CLOSE_ERROR"
    assert "secret" not in outcome.failure.message
    assert fake.open_count == 1


def test_factory_validates_inputs_fails_closed_on_open_and_denies_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TypeError, match="configuration"):
        SerialFluidNCFactory(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="serial factory"):
        SerialFluidNCFactory(_config(), serial_factory=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        SerialFluidNCFactory(_config(), monotonic=object())  # type: ignore[arg-type]

    monkeypatch.setattr(serial_module.serial, "Serial", lambda: (_ for _ in ()).throw(AssertionError("default denied")))
    SerialFluidNCFactory(_config())  # default construction must not instantiate or open pyserial

    fake = FakeSerial([])
    factory = _factory(fake)
    with pytest.raises(TypeError, match="open request"):
        factory.open_session(object())  # type: ignore[arg-type]
    factory.open_session(OpenSessionRequest(EPOCH))
    with pytest.raises(DrawingMachineError, match="repeated"):
        factory.open_session(OpenSessionRequest(EPOCH))

    fake = FakeSerial([])
    fake.open_error = OSError("raw secret")
    with pytest.raises(DrawingMachineError, match="could not be opened") as captured:
        _factory(fake).open_session(OpenSessionRequest(EPOCH))
    assert "secret" not in str(captured.value)

    class NotOpen(FakeSerial):
        def open(self) -> None:
            self.open_count += 1

    with pytest.raises(DrawingMachineError, match="did not become open"):
        _factory(NotOpen([])).open_session(OpenSessionRequest(EPOCH))

    fake = FakeSerial([])
    fake.is_open = 1  # type: ignore[assignment]
    with pytest.raises(DrawingMachineError, match="closed before configuration"):
        _factory(fake).open_session(OpenSessionRequest(EPOCH))

    class InvalidAfterOpen(FakeSerial):
        def open(self) -> None:
            self.open_count += 1
            self.is_open = 1  # type: ignore[assignment]

    with pytest.raises(DrawingMachineError, match="did not become open"):
        _factory(InvalidAfterOpen([])).open_session(OpenSessionRequest(EPOCH))

    error = DrawingMachineError(ErrorPayload("EXACT_OPEN_DENIAL", ErrorCategory.HARDWARE, "typed denial", False, {}))
    with pytest.raises(DrawingMachineError, match="typed denial"):
        SerialFluidNCFactory(_config(), serial_factory=lambda: (_ for _ in ()).throw(error)).open_session(
            OpenSessionRequest(EPOCH)
        )

    fake = FakeSerial([])
    with pytest.raises(DrawingMachineError, match="bounded deadline"):
        _factory(fake, clock=StepClock(3.0)).open_session(OpenSessionRequest(EPOCH))
    assert fake.close_count == 1
    assert fake.read_bounds == []

    fake = FakeSerial([])
    fake.close_error = OSError("secret")
    with pytest.raises(DrawingMachineError, match="bounded deadline") as captured:
        _factory(fake, clock=StepClock(3.0)).open_session(OpenSessionRequest(EPOCH))
    assert "secret" not in str(captured.value)
    assert fake.read_bounds == []


@pytest.mark.parametrize("post_open_state", [False, 1])
def test_c15_failed_open_attempt_closes_owned_transport_once(post_open_state: object) -> None:
    class PartialOpen(FakeSerial):
        def open(self) -> None:
            self.open_count += 1
            self.is_open = True
            raise OSError("secret partial-open detail")

    partial = PartialOpen([])
    partial.close_error = OSError("secret close detail")
    with pytest.raises(DrawingMachineError) as failed:
        _factory(partial).open_session(OpenSessionRequest(EPOCH))
    assert failed.value.payload.code == "FLUIDNC_OPEN_FAILED"
    assert "secret" not in str(failed.value)
    assert partial.close_count == 1

    typed_error = DrawingMachineError(
        ErrorPayload("EXACT_PARTIAL_OPEN_DENIAL", ErrorCategory.HARDWARE, "typed partial-open denial", False, {})
    )

    class TypedPartialOpen(FakeSerial):
        def open(self) -> None:
            self.open_count += 1
            self.is_open = True
            raise typed_error

    typed_partial = TypedPartialOpen([])
    typed_partial.close_error = OSError("secret close detail")
    with pytest.raises(DrawingMachineError) as typed_failed:
        _factory(typed_partial).open_session(OpenSessionRequest(EPOCH))
    assert typed_failed.value is typed_error
    assert typed_partial.close_count == 1

    with pytest.raises(DrawingMachineError) as construction_failed:
        SerialFluidNCFactory(
            _config(),
            serial_factory=lambda: (_ for _ in ()).throw(OSError("secret construction detail")),
        ).open_session(OpenSessionRequest(EPOCH))
    assert construction_failed.value.payload.code == "FLUIDNC_OPEN_FAILED"
    assert "secret" not in str(construction_failed.value)

    class InvalidAfterOpen(FakeSerial):
        def open(self) -> None:
            self.open_count += 1
            self.is_open = post_open_state  # type: ignore[assignment]

    invalid = InvalidAfterOpen([])
    invalid.close_error = OSError("secret close detail")
    with pytest.raises(DrawingMachineError) as rejected:
        _factory(invalid).open_session(OpenSessionRequest(EPOCH))
    assert rejected.value.payload.code == "FLUIDNC_OPEN_FAILED"
    assert "secret" not in str(rejected.value)
    assert invalid.close_count == 1

    class UnreadableAfterOpen(FakeSerial):
        def __init__(self) -> None:
            self._is_open = False
            super().__init__([])

        @property
        def is_open(self) -> bool:
            if self.open_count:
                raise OSError("secret post-open state detail")
            return self._is_open

        @is_open.setter
        def is_open(self, value: bool) -> None:
            self._is_open = value

        def open(self) -> None:
            self.open_count += 1
            self._is_open = True

        def close(self) -> None:
            self.close_count += 1
            raise OSError("secret close detail")

    unreadable = UnreadableAfterOpen()
    with pytest.raises(DrawingMachineError) as unreadable_state:
        _factory(unreadable).open_session(OpenSessionRequest(EPOCH))
    assert unreadable_state.value.payload.code == "FLUIDNC_OPEN_FAILED"
    assert "secret" not in str(unreadable_state.value)
    assert unreadable.close_count == 1


def test_every_operation_rejects_wrong_request_and_callback_types() -> None:
    session = _open(FakeSerial([]))
    with pytest.raises(TypeError, match="initial"):
        session.read_initial_preflight(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="HOME request"):
        session.home_all_axes(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ZCAL"):
        session.run_z_calibration(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ZCONFIRM"):
        session.raise_after_z_confirmation(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="STREAM request"):
        session.stream_program(object(), lambda update: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callback"):
        session.stream_program(StreamProgramRequest(_program()), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="HOME_Z"):
        session.home_z_axis(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="close request"):
        session.close(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="close request"):
        session.close(CloseSessionRequest(str(UUID(int=9))))


def test_startup_is_bounded_and_sanitizes_decode_transport_and_deadline_failures() -> None:
    cases: list[tuple[list[object], Callable[[], float] | None, str]] = [
        ([OSError("secret")], None, "TRANSPORT_LOSS"),
        ([object()], None, "DECODE_ERROR"),
        ([b"\xff\n"], None, "DECODE_ERROR"),
        ([b"x" * 4097 + b"\n"], None, "DECODE_ERROR"),
        ([b"unterminated"], None, "DECODE_ERROR"),
        ([b"bad\x00line\n"], None, "DECODE_ERROR"),
        ([b"\r\n", b"Grbl 4.0 [FluidNC v4.0.2 (esp32-wifi) '$' for help]\r\n", b"", *_preflight_reads()], None, "NONE"),
        ([], SequenceClock([0.0, 0.0, 0.0, 20.0]), "TIMEOUT"),
        ([b"noise\n"] * 257, StepClock(0.001), "TIMEOUT"),
    ]
    for reads, clock, expected in cases:
        fake = FakeSerial(reads)
        session = _open(fake, clock=clock)
        if expected == "NONE":
            outcome = session.read_initial_preflight(InitialPreflightRequest())
            assert outcome.failure is None
            continue
        # Supply a complete preflight after startup failures when the failure did not consume it.
        fake.reads.extend(_preflight_reads())
        outcome = session.read_initial_preflight(InitialPreflightRequest())
        assert outcome.failure is not None
        assert outcome.failure.kind.value == expected
        assert "secret" not in outcome.failure.message
        assert fake.writes == []


def test_startup_skips_blank_banner_lines_from_real_boot_capture() -> None:
    banner = [
        "ets Jul 29 2019 12:21:46",
        "",
        "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
        "configsip: 0, SPIWP:0xee",
        "mode:DIO, clock div:1",
        "load:0x3fff0030,len:1184",
        "entry 0x400805e4",
        "[MSG:INFO: uart_channel0 created]",
        "",
        "[MSG:RST]",
        "[MSG:INFO: FluidNC v4.0.2 https://github.com/bdring/FluidNC]",
        "[MSG:INFO: Machine Default (Test Drive no I/O)]",
        "[MSG:INFO: Axis count 3]",
        "",
        "Grbl 4.0 [FluidNC v4.0.2 (esp32-wifi) '$' for help]",
        "",
    ]
    reads: list[object] = [(line + "\r\n").encode() for line in banner]
    reads.append(b"")
    reads.extend(_preflight_reads())
    fake = FakeSerial(reads)
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is None
    non_empty_banner_lines = [line for line in banner if line]
    for banner_line in non_empty_banner_lines:
        assert banner_line in outcome.evidence
    assert "" not in outcome.evidence


def test_startup_bounds_exceeded_when_blank_lines_exhaust_byte_budget() -> None:
    blank_line_count = serial_module._MAX_TOTAL_BYTES // 2 + 1
    fake = FakeSerial([b"\r\n"] * blank_line_count)
    outcome = _open(fake, clock=StepClock(0.0)).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None
    assert outcome.failure.kind.value == "TIMEOUT"
    assert outcome.failure.code == "STARTUP_BOUNDS_EXCEEDED"


def test_startup_waits_through_mid_boot_quiet_gaps_across_wifi_connect_stalls() -> None:
    # Real-hardware boot: ROM/MSG output arrives, then the ESP32 goes silent for
    # multiple whole read_timeout_seconds windows while it attempts WiFi connect
    # (t+0.5s, t+1.0s, t+3.0s in the real capture), before the rest of the boot
    # output and the Grbl ready banner finally arrive.
    early_boot = [
        "ets Jul 29 2019 12:21:46",
        "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
        "[MSG:INFO: uart_channel0 created]",
    ]
    late_boot = [
        "[MSG:INFO: FluidNC v4.0.2 https://github.com/bdring/FluidNC]",
        "[MSG:INFO: Machine Default (Test Drive no I/O)]",
        "Grbl 4.0 [FluidNC v4.0.2 (esp32-wifi) '$' for help]",
    ]
    reads: list[object] = [(line + "\r\n").encode() for line in early_boot]
    reads.extend([b"", b"", b""])
    reads.extend((line + "\r\n").encode() for line in late_boot)
    reads.append(b"")
    reads.extend(_preflight_reads())
    fake = FakeSerial(reads)
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is None
    for line in (*early_boot, *late_boot):
        assert line in outcome.evidence
    assert fake.writes == [b"?", b"$G\n", b"$#\n"]


def test_startup_deadline_exceeded_when_boot_output_seen_but_banner_never_arrives() -> None:
    fake = FakeSerial([b"ets Jul 29 2019 12:21:46\r\n"])
    outcome = _open(fake, clock=StepClock(1.0)).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None
    assert outcome.failure.kind.value == "TIMEOUT"
    assert outcome.failure.code == "STARTUP_DEADLINE_EXCEEDED"
    assert fake.writes == []


def test_eof_timeout_write_flush_alarm_and_decode_failures_are_typed_after_write() -> None:
    def eof() -> bytes:
        fake.is_open = False
        return b""

    scenarios: list[tuple[object, str]] = [
        (OSError("read secret"), "TRANSPORT_LOSS"),
        (object(), "DECODE_ERROR"),
        (b"\xff\n", "DECODE_ERROR"),
        (b"unterminated", "DECODE_ERROR"),
        (b"\r\n", "DECODE_ERROR"),
        (b"ALARM:1\n", "ALARM"),
    ]
    for response, kind in scenarios:
        fake = FakeSerial([b"", response])
        outcome = _open(fake).home_z_axis(HomeZAxisRequest())
        assert outcome.failure is not None and outcome.failure.kind.value == kind
        assert outcome.failure.side_effect_may_have_occurred

    fake = FakeSerial([b"", eof])
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"

    fake = FakeSerial([])
    fake.write_error = OSError("write secret")
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"

    fake = FakeSerial([])
    fake.flush_error = OSError("flush secret")
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert fake.flush_count == 0

    fake = FakeSerial([b"", *(b"noise\n" for _ in range(257))])
    outcome = _open(fake, clock=StepClock(0.001)).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.code == "SERIAL_RESPONSE_BOUNDS_EXCEEDED"

    # Content is observed ("noise") but no "ok" ever follows; subsequent quiet reads
    # are retried (not terminal) until the absolute deadline expires.
    fake = FakeSerial([b"", b"noise\n"])
    outcome = _open(fake, clock=StepClock(1.0)).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert outcome.failure.code == "SERIAL_RESPONSE_TIMEOUT"

    fake = FakeSerial([b"", b"error:9\n"])
    outcome = _open(fake).run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    assert outcome.failure is not None and outcome.acknowledged_steps == 0


def test_home_command_ack_arrives_after_multiple_quiet_response_gaps() -> None:
    # $H only ever "ok"s once homing completes; a single quiet read must not be
    # treated as a lost ack while the overall deadline has not expired.
    fake = FakeSerial(
        [
            b"",
            b"",
            b"",
            b"ok\n",
            *_preflight_reads("<Idle|MPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>"),
        ]
    )
    outcome = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is None
    assert outcome.stage is SingleCommandOutcomeStage.POSTCONDITION_PROVEN
    assert fake.writes == [b"$H\n", b"?", b"$G\n", b"$#\n"]


def test_response_total_silence_reaches_deadline_as_response_timeout_not_read_timeout() -> None:
    fake = FakeSerial([b""])
    outcome = _open(fake, clock=StepClock(1.0)).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None
    assert outcome.failure.kind.value == "LOST_ACK"
    assert outcome.failure.code == "SERIAL_RESPONSE_TIMEOUT"
    assert outcome.failure.side_effect_may_have_occurred
    assert fake.writes == [b"$HZ\n"]


def test_late_reset_is_recovery_and_progress_callback_failure_stops_stream() -> None:
    fake = FakeSerial([b"", b"ok\n", *_preflight_reads()])
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert outcome.decision is not None and outcome.decision.disposition.value == "RECOVERY_REQUIRED"

    fake = FakeSerial([b"", b"ok\n", *_preflight_reads("<Idle|MPos:96,96,3.5|WCO:0,0,0>")])
    outcome = _open(fake).stream_program(
        StreamProgramRequest(_program()),
        lambda update: (_ for _ in ()).throw(RuntimeError("callback secret")),
    )
    assert outcome.failure is not None and outcome.failure.code == "PROGRESS_CALLBACK_FAILED"
    assert b"G90\n" not in fake.writes

    typed = DrawingMachineError(ErrorPayload("CALLBACK_DENIED", ErrorCategory.INTERNAL, "typed callback", False, {}))
    fake = FakeSerial([b"", b"ok\n"])
    with pytest.raises(DrawingMachineError, match="typed callback"):
        _open(fake).stream_program(
            StreamProgramRequest(_program()),
            lambda update: (_ for _ in ()).throw(typed),
        )


def test_each_motion_operation_returns_typed_failure_for_missing_final_proof() -> None:
    fake = FakeSerial([b"", b"ok\n", *_preflight_reads("<Idle|MPos:191,192,192|WCO:0,0,0>")])
    home = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert home.failure is not None and home.failure.code == "POST_HOME_PROOF_FAILED"
    assert home.acknowledged_steps == 1 and home.stage is SingleCommandOutcomeStage.IDLE_OBSERVED

    fake = FakeSerial(
        [
            b"",
            *(b"ok\n" for _ in range(6)),
            *_preflight_reads("<Run|MPos:96,96,0|WCO:0,0,0>"),
        ]
    )
    zcal = _open(fake).run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    assert zcal.failure is not None and zcal.failure.kind.value == "LOST_ACK"
    assert zcal.acknowledged_steps == 6 and zcal.stage is ZCalibrationOutcomeStage.POSTCONDITION

    fake = FakeSerial([b"", b"ok\n", *_preflight_reads("<Idle|MPos:95,96,3.5|WCO:0,0,0>")])
    zconfirm = _open(fake).raise_after_z_confirmation(ZConfirmationRequest((96, 96, 3.5), 3.5, 400, 0.5))
    assert zconfirm.failure is not None and zconfirm.failure.code == "POST_ZCONFIRM_PROOF_FAILED"
    assert zconfirm.acknowledged_steps == 1 and zconfirm.stage is SingleCommandOutcomeStage.IDLE_OBSERVED

    fake = FakeSerial([b"", b"ok\n", b"ok\n", *_preflight_reads("<Run|MPos:96,96,3.5|WCO:0,0,0>")])
    stream = _open(fake).stream_program(StreamProgramRequest(_program()), lambda update: None)
    assert stream.failure is not None and stream.failure.kind.value == "LOST_ACK"

    fake = FakeSerial([b"", b"ok\n", *_preflight_reads("<Run|MPos:96,96,3.5|WCO:0,0,0>")])
    home_z = _open(fake).home_z_axis(HomeZAxisRequest())
    assert home_z.failure is not None and home_z.failure.kind.value == "LOST_ACK"

    fake = FakeSerial(
        [
            b"",
            *(b"ok\n" for _ in range(6)),
            b"<Idle|MPos:96,96,0|WPos:96,96,0|WCO:0,0,0>\n",
            b"error:9\n",
        ]
    )
    zcal = _open(fake).run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    assert zcal.failure is not None and zcal.failure.code == "POST_ZCAL_PROOF_FAILED"
    assert zcal.acknowledged_steps == 6 and zcal.stage is ZCalibrationOutcomeStage.POSTCONDITION


@pytest.mark.parametrize("action", ["HOME", "ZCONFIRM"])
def test_single_command_parser_failure_after_idle_has_action_specific_stage(action: str) -> None:
    status = (
        b"<Idle|MPos:192,192,192|WPos:192,192,192|WCO:0,0,0>\n"
        if action == "HOME"
        else b"<Idle|MPos:96,96,3.5|WPos:96,96,3.5|WCO:0,0,0>\n"
    )
    fake = FakeSerial([b"", b"ok\n", status, b"error:9\n"])
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None
    assert outcome.failure.code == f"POST_{action}_PROOF_FAILED"
    assert outcome.acknowledged_steps == 1
    assert outcome.stage is SingleCommandOutcomeStage.IDLE_OBSERVED


@pytest.mark.parametrize("action", ["HOME", "ZCAL", "ZCONFIRM"])
def test_pending_reset_after_idle_status_retains_action_specific_post_idle_stage(action: str) -> None:
    reset = b"watchdog reset\n"
    status = b"<Idle|MPos:192,192,192|WPos:96,96,0|WCO:96,96,192>\n"
    prefix = [b""]
    if action == "ZCAL":
        prefix.extend(b"ok\n" for _ in range(6))
    else:
        prefix.append(b"ok\n")
    fake = FakeSerial(prefix)

    def idle_with_pending_reset() -> bytes:
        fake.in_waiting = len(status) + len(reset)
        return status

    fake.reads.extend((idle_with_pending_reset, reset))
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None
    assert outcome.failure.code == f"POST_{action}_PROOF_FAILED"
    if action == "ZCAL":
        assert outcome.acknowledged_steps == 6
        assert outcome.stage is ZCalibrationOutcomeStage.POSTCONDITION
    else:
        assert outcome.acknowledged_steps == 1
        assert outcome.stage is SingleCommandOutcomeStage.IDLE_OBSERVED


def test_close_success_repeat_and_nonblocking_no_flush_policy_are_explicit() -> None:
    fake = FakeSerial([])
    session = _open(fake)
    assert session.close(CloseSessionRequest(EPOCH)).failure is None
    assert fake.close_count == 1
    repeat = session.close(CloseSessionRequest(EPOCH))
    assert repeat.failure is not None and repeat.failure.code == "SESSION_ALREADY_CLOSED"

    fake = FakeSerial([])
    fake.flush_error = OSError("secret")
    session = _open(fake)
    outcome = session.close(CloseSessionRequest(EPOCH))
    assert outcome.failure is None
    assert fake.flush_count == 0
    assert fake.close_count == 1


def test_parser_helpers_bound_lines_evidence_and_crlf_without_raw_leakage() -> None:
    assert serial_module._decode_line(b"ok\r\n", side_effect=False) == ("ok", None)
    for raw in (
        b"x" * 4097 + b"\n",
        b"\xff\n",
        b"unterminated",
        b"\n",
        b"bad\rline\n",
        b"bad\x1bline\n",
        "bad\u2028line\n".encode(),
    ):
        line, failure = serial_module._decode_line(raw, side_effect=True)
        assert line is None and failure is not None and failure.kind.value == "DECODE_ERROR"
    many = tuple("x" for _ in range(300))
    assert len(serial_module._bounded_evidence(many)) == 256
    huge = ("x" * 4096,) * 20
    assert len(serial_module._bounded_evidence(huge)) == 16


def test_malformed_status_and_duplicate_offsets_become_typed_decode_outcomes() -> None:
    fake = FakeSerial(
        [
            b"",
            b"<Idle|MPos:bad>\n",
            b"[GC:G54]\n",
            b"ok\n",
            b"[G54:0,0,0]\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None and outcome.failure.code == "CONTROLLER_STATUS_INVALID"

    fake = FakeSerial(
        [
            b"",
            b"<Idle|MPos:1,1,1>\n",
            b"[GC:G54]\n",
            b"ok\n",
            b"[G54:0,0,0]\n",
            b"[G54:1,1,1]\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "DECODE_ERROR"

    fake = FakeSerial(
        [
            b"",
            b"ok\n",
            b"ok\n",
            b"<Idle|MPos:bad>\n",
            b"[GC:G54]\n",
            b"ok\n",
            b"[G54:0,0,0]\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).stream_program(StreamProgramRequest(_program()), lambda update: None)
    assert outcome.failure is not None and outcome.failure.kind.value == "DECODE_ERROR"
    assert outcome.failure.side_effect_may_have_occurred


def _invoke_action(session: Any, action: str) -> Any:
    if action == "HOME":
        return session.home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    if action == "ZCAL":
        return session.run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    if action == "ZCONFIRM":
        return session.raise_after_z_confirmation(ZConfirmationRequest((96, 96, 3.5), 3.5, 400, 0.5))
    if action == "STREAM":
        return session.stream_program(StreamProgramRequest(_program()), lambda update: None)
    if action == "HOME_Z":
        return session.home_z_axis(HomeZAxisRequest())
    raise AssertionError(action)


@pytest.mark.parametrize(
    ("action", "first_write"),
    [
        ("HOME", b"$H\n"),
        ("ZCAL", b"G21\n"),
        ("ZCONFIRM", b"G1 Z3.500 F400.000\n"),
        ("STREAM", b"G21\n"),
        ("HOME_Z", b"$HZ\n"),
    ],
)
@pytest.mark.parametrize(
    "response",
    [b"FluidNC reset\n", b"<Run|MPos:0.000,0.000,0.000>\n"],
    ids=["reset-banner-before-ack", "reset-position-before-ack"],
)
def test_every_action_stops_without_another_write_on_reset_before_ack(
    action: str,
    first_write: bytes,
    response: bytes,
) -> None:
    fake = FakeSerial([b"", response])
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert outcome.decision is not None and outcome.decision.disposition.value == "RECOVERY_REQUIRED"
    assert fake.writes == [first_write]


@pytest.mark.parametrize(
    ("action", "first_write"),
    [
        ("HOME", b"$H\n"),
        ("ZCAL", b"G21\n"),
        ("ZCONFIRM", b"G1 Z3.500 F400.000\n"),
        ("STREAM", b"G21\n"),
        ("HOME_Z", b"$HZ\n"),
    ],
)
@pytest.mark.parametrize("reset_line", [b"watchdog reset\n", b"<Run|MPos:0,0,0>\n"])
def test_every_action_inspects_post_ack_reset_before_emitting_status_query(
    action: str, first_write: bytes, reset_line: bytes
) -> None:
    fake = FakeSerial([b"", b"ok\n", reset_line])
    fake.post_ack_waiting = len(reset_line)
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert outcome.decision is not None and outcome.decision.disposition.value == "RECOVERY_REQUIRED"
    if action in {"HOME", "ZCONFIRM"}:
        assert outcome.acknowledged_steps == 1
        assert outcome.stage is SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
    elif action == "ZCAL":
        assert outcome.acknowledged_steps == 1
    assert fake.writes == [first_write]


@pytest.mark.parametrize("step_index", range(6))
def test_each_zcal_step_retains_ack_observed_before_post_ack_reset(step_index: int) -> None:
    fake = FakeSerial([b"", *(b"ok\n" for _ in range(step_index))])
    reset = b"watchdog reset\n"

    def ack_with_pending_reset() -> bytes:
        fake.post_ack_waiting = len(reset)
        return b"ok\n"

    fake.reads.extend((ack_with_pending_reset, reset))
    outcome = _open(fake).run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert outcome.evidence[-2:] == ("ok", "watchdog reset")
    assert outcome.acknowledged_steps == step_index + 1
    acknowledged_stages = (
        ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.ABSOLUTE_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.WORK_COORDINATE_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.PEN_UP_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.CENTER_ACKNOWLEDGED,
        ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED,
    )
    assert outcome.stage is acknowledged_stages[step_index]


@pytest.mark.parametrize("step_index", range(6))
def test_each_zcal_step_retains_current_attempt_when_ack_is_not_observed(step_index: int) -> None:
    fake = FakeSerial([b"", *(b"ok\n" for _ in range(step_index)), b"error:9\n"])
    outcome = _open(fake).run_z_calibration(ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100))
    attempted_stages = (
        ZCalibrationOutcomeStage.METRIC_ATTEMPTED,
        ZCalibrationOutcomeStage.ABSOLUTE_ATTEMPTED,
        ZCalibrationOutcomeStage.WORK_COORDINATE_ATTEMPTED,
        ZCalibrationOutcomeStage.PEN_UP_ATTEMPTED,
        ZCalibrationOutcomeStage.CENTER_ATTEMPTED,
        ZCalibrationOutcomeStage.CONTACT_ATTEMPTED,
    )
    assert outcome.failure is not None
    assert outcome.acknowledged_steps == step_index
    assert outcome.stage is attempted_stages[step_index]


@pytest.mark.parametrize(
    ("action", "first_write"),
    [
        ("HOME", b"$H\n"),
        ("ZCAL", b"G21\n"),
        ("ZCONFIRM", b"G1 Z3.500 F400.000\n"),
        ("STREAM", b"G21\n"),
        ("HOME_Z", b"$HZ\n"),
    ],
)
def test_every_action_drains_all_pending_post_ack_lines_before_next_write(action: str, first_write: bytes) -> None:
    pending = (b"<Run|MPos:192,192,192>\n", b"watchdog reset\n")
    fake = FakeSerial([b"", b"ok\n", *pending])
    fake.post_ack_waiting = sum(map(len, pending))
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert fake.writes == [first_write]


def test_idle_status_and_proof_ack_each_drain_pending_reset_before_next_write() -> None:
    reset = b"watchdog reset\n"
    run_status = b"<Run|MPos:192,192,192>\n"

    def run_with_pending_reset() -> bytes:
        fake.in_waiting = len(reset) + len(run_status)
        return run_status

    fake = FakeSerial([b"", b"ok\n", run_with_pending_reset, reset])
    outcome = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert fake.writes == [b"$H\n", b"?"]

    def parser_ack_with_pending_reset() -> bytes:
        fake.in_waiting = len(reset) + len(b"ok\n")
        return b"ok\n"

    fake = FakeSerial(
        [
            b"",
            b"ok\n",
            b"<Idle|MPos:192,192,192>\n",
            b"[GC:G54]\n",
            parser_ack_with_pending_reset,
            reset,
        ]
    )
    outcome = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_RESET"
    assert outcome.acknowledged_steps == 1
    assert outcome.stage is SingleCommandOutcomeStage.IDLE_OBSERVED
    assert fake.writes == [b"$H\n", b"?", b"$G\n"]


@pytest.mark.parametrize("action", ["HOME", "ZCAL", "ZCONFIRM", "STREAM", "HOME_Z"])
@pytest.mark.parametrize(
    ("response", "kind"),
    [(b"error:20\n", "CONTROLLER_ERROR"), (b"ALARM:1\n", "ALARM"), (b"\xff\n", "DECODE_ERROR")],
)
def test_every_action_first_failure_forbids_post_action_queries(action: str, response: bytes, kind: str) -> None:
    fake = FakeSerial([b"", response])
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None and outcome.failure.kind.value == kind
    if action in {"HOME", "ZCONFIRM"}:
        assert outcome.acknowledged_steps == 0
        assert outcome.stage is SingleCommandOutcomeStage.DISPATCH_ATTEMPTED
    assert len(fake.writes) == 1
    assert b"?" not in fake.writes


@pytest.mark.parametrize("action", ["HOME", "ZCAL", "ZCONFIRM", "STREAM", "HOME_Z"])
@pytest.mark.parametrize(
    ("mode", "kind"),
    [("short", "SHORT_WRITE"), ("lost-ack", "LOST_ACK"), ("transport", "TRANSPORT_LOSS")],
)
def test_every_action_short_write_lost_ack_and_transport_failure_emit_no_followup(
    action: str, mode: str, kind: str
) -> None:
    response: object = b"" if mode == "lost-ack" else OSError("secret")
    fake = FakeSerial([b"", response])
    if mode == "short":
        fake.short_write = 1
    outcome = _invoke_action(_open(fake), action)
    assert outcome.failure is not None and outcome.failure.kind.value == kind
    assert len(fake.writes) == 1 and b"?" not in fake.writes


def test_acknowledged_action_polls_run_and_hold_until_idle_then_reads_parser_and_offsets() -> None:
    fake = FakeSerial(
        [
            b"",
            b"ok\n",
            b"<Run|MPos:192,192,192>\n",
            b"<Hold:0|MPos:192,192,192>\n",
            b"<Idle|MPos:192,192,192>\n",
            b"[GC:G54]\n",
            b"ok\n",
            b"[G54:0,0,0]\n",
            b"ok\n",
        ]
    )
    outcome = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is None
    assert fake.writes == [b"$H\n", b"?", b"?", b"?", b"$G\n", b"$#\n"]


def test_wait_for_idle_stops_on_alarm_reset_and_absolute_deadline_without_parser_queries() -> None:
    for status, expected in (
        (b"<Alarm|MPos:192,192,192>\n", "ALARM"),
        (b"<Run|MPos:0,0,0>\n", "CONTROLLER_RESET"),
    ):
        fake = FakeSerial([b"", b"ok\n", status])
        outcome = _open(fake).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
        assert outcome.failure is not None and outcome.failure.kind.value == expected
        assert outcome.acknowledged_steps == 1
        assert outcome.stage is SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED
        assert fake.writes == [b"$H\n", b"?"]

    fake = FakeSerial([b"", b"ok\n", *(b"<Run|MPos:192,192,192>\n" for _ in range(20))])
    clock = SequenceClock([0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 1000])
    outcome = _open(fake, clock=clock).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert outcome.acknowledged_steps == 0
    assert outcome.stage is SingleCommandOutcomeStage.DISPATCH_ATTEMPTED
    assert b"$G\n" not in fake.writes and b"$#\n" not in fake.writes


@pytest.mark.parametrize(
    ("action", "operation_request"),
    [
        ("HOME", HomeAllAxesRequest((191, 192, 192), 0.5)),
        ("HOME", HomeAllAxesRequest((192, 191, 192), 0.5)),
        ("HOME", HomeAllAxesRequest((192, 192, 191), 0.5)),
        ("HOME", HomeAllAxesRequest((192, 192, 192), 0.25)),
        ("ZCAL", ZCalibrationRequest("G54", 95, 96, 3.5, 0, 1200, 100)),
        ("ZCAL", ZCalibrationRequest("G54", 96, 95, 3.5, 0, 1200, 100)),
        ("ZCAL", ZCalibrationRequest("G54", 96, 96, 3.4, 0, 1200, 100)),
        ("ZCAL", ZCalibrationRequest("G54", 96, 96, 3.5, -0.0, 1200, 100)),
        ("ZCAL", ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1199, 100)),
        ("ZCAL", ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 99)),
        ("ZCONFIRM", ZConfirmationRequest((95, 96, 3.5), 3.5, 400, 0.5)),
        ("ZCONFIRM", ZConfirmationRequest((96, 95, 3.5), 3.5, 400, 0.5)),
        ("ZCONFIRM", ZConfirmationRequest((96, 96, 3.4), 3.4, 400, 0.5)),
        ("ZCONFIRM", ZConfirmationRequest((96, 96, 3.5), 3.5, 399, 0.5)),
        ("ZCONFIRM", ZConfirmationRequest((96, 96, 3.5), 3.5, 400, 0.25)),
    ],
)
def test_request_authority_must_exactly_match_validated_machine_config_before_write(
    action: str, operation_request: object
) -> None:
    fake = FakeSerial([])
    session = _open(fake)
    with pytest.raises(DrawingMachineError, match="validated machine configuration"):
        if action == "HOME":
            session.home_all_axes(operation_request)  # type: ignore[arg-type]
        elif action == "ZCAL":
            session.run_z_calibration(operation_request)  # type: ignore[arg-type]
        else:
            session.raise_after_z_confirmation(operation_request)  # type: ignore[arg-type]
    assert fake.writes == []


def test_request_binding_rejects_drifted_validated_config_object_before_serial_factory() -> None:
    config = replace(_config(), expected_homed_mpos=(191.0, 192.0, 192.0))
    fake = FakeSerial([])
    factory = SerialFluidNCFactory(config, serial_factory=lambda: fake, monotonic=StepClock(0.1))
    session = factory.open_session(OpenSessionRequest(EPOCH))
    with pytest.raises(DrawingMachineError, match="validated machine configuration"):
        session.home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert fake.writes == []


def test_open_requires_closed_transport_and_linux_pyserial_uses_nonblocking_os_open() -> None:
    fake = FakeSerial([])
    fake.is_open = True
    with pytest.raises(DrawingMachineError, match="closed before configuration"):
        _factory(fake).open_session(OpenSessionRequest(EPOCH))
    assert fake.open_count == 0

    if sys.platform.startswith("linux"):
        import serial.serialposix  # type: ignore[import-untyped]

        source = inspect.getsource(serial.serialposix.Serial.open)
        assert "O_NONBLOCK" in source


def test_remaining_deadlines_are_applied_before_each_read_and_write_without_flush_or_helper_threads() -> None:
    before = {thread.ident for thread in threading.enumerate()}
    fake = FakeSerial(
        [
            b"",
            b"ok\n",
            b"<Run|MPos:192,192,192>\n",
            b"<Idle|MPos:192,192,192>\n",
            b"[GC:G54]\n",
            b"ok\n",
            b"[G54:0,0,0]\n",
            b"ok\n",
        ]
    )
    clock = StepClock(0.01)
    outcome = _open(fake, clock=clock).home_all_axes(HomeAllAxesRequest((192, 192, 192), 0.5))
    assert outcome.failure is None
    assert fake.flush_count == 0
    assert all(value is not None and 0.0 < value <= 300.0 for value in fake.write_timeouts)
    assert all(value is not None and 0.0 < value <= 1.0 for value in fake.read_timeouts)
    assert {thread.ident for thread in threading.enumerate()} == before


def test_cooperative_blocking_fake_obeys_configured_read_and_write_timeout_without_thread_leak() -> None:
    before = {thread.ident for thread in threading.enumerate()}
    config = replace(
        _config(),
        command_timeout_seconds=0.05,
        home_timeout_seconds=0.05,
        read_timeout_seconds=0.01,
        stream_idle_timeout_seconds=0.05,
    )

    fake = FakeSerial([b""])

    def blocked_read() -> bytes:
        time.sleep(fake.timeout or 0.0)
        return b""

    fake.reads.append(blocked_read)
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    started = time.monotonic()
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert time.monotonic() - started < 0.2
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"

    fake = FakeSerial([b""])

    def blocked_write(value: bytes) -> int:
        del value
        time.sleep(fake.write_timeout or 0.0)
        raise TimeoutError("cooperative write timeout")

    fake.write = blocked_write  # type: ignore[method-assign]
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    started = time.monotonic()
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert time.monotonic() - started < 0.2
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"
    assert {thread.ident for thread in threading.enumerate()} == before


def test_optional_post_ack_and_idle_proof_failures_stop_at_the_exact_boundary() -> None:
    for response, expected in ((OSError("secret"), "TRANSPORT_LOSS"), (b"\xff\n", "DECODE_ERROR")):
        fake = FakeSerial([b"", b"ok\n", response])
        fake.post_ack_waiting = 1
        outcome = _open(fake).home_z_axis(HomeZAxisRequest())
        assert outcome.failure is not None and outcome.failure.kind.value == expected
        assert fake.writes == [b"$HZ\n"]

    fake = FakeSerial([b"", b"ok\n", b"<Door:0|MPos:192,192,192>\n"])
    fake.post_ack_waiting = len(b"<Door:0|MPos:192,192,192>\n")
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.code == "CONTROLLER_NOT_IDLE"
    assert fake.writes == [b"$HZ\n"]

    def idle_then_close() -> bytes:
        fake.is_open = False
        return b"<Idle|MPos:192,192,192>\n"

    fake = FakeSerial([b"", b"ok\n", idle_then_close])
    fake.post_ack_waiting = len(b"<Idle|MPos:192,192,192>\n")
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"
    assert fake.writes == [b"$HZ\n"]

    fake = FakeSerial([b"", b"ok\n", b"<Idle|MPos:192,192,192>\n", b"error:9\n"])
    fake.post_ack_waiting = len(b"<Idle|MPos:192,192,192>\n")
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "CONTROLLER_ERROR"
    assert fake.writes == [b"$HZ\n", b"$G\n"]


def test_closed_preflight_and_private_deadline_edges_return_typed_outcomes_without_io() -> None:
    fake = FakeSerial([])
    session = _open(fake)
    session.close(CloseSessionRequest(EPOCH))
    outcome = session.read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"
    assert fake.writes == []

    fake = FakeSerial([b"", b"noise\n"])
    session = _open(fake)
    session._monotonic = SequenceClock([0.0, 0.0, 0.5, 2.0])
    lines, failure = session._response(
        expect_ok=True,
        deadline=1.0,
        side_effect=True,
        phase=serial_module.SessionObservationPhase.ACTION_ADMITTED,
    )
    assert lines == ("noise",)
    assert failure is not None and failure.code == "SERIAL_RESPONSE_TIMEOUT"

    session._monotonic = lambda: 2.0
    writes_before = tuple(fake.writes)
    failure = session._write("$HZ", newline=True, deadline=1.0, side_effect=True)
    assert failure is not None and failure.code == "SERIAL_RESPONSE_TIMEOUT"
    assert tuple(fake.writes) == writes_before
    raw, failure = session._read_raw(1.0, side_effect=True)
    assert raw is None and failure is not None and failure.code == "SERIAL_RESPONSE_TIMEOUT"


def test_post_ack_waiting_metadata_bounds_and_pre_status_write_failures_are_typed() -> None:
    class ExplodingWaitingFake(FakeSerial):
        explode = False

        @property
        def in_waiting(self) -> int:
            if self.explode:
                raise OSError("secret waiting failure")
            return self._waiting

        @in_waiting.setter
        def in_waiting(self, value: int) -> None:
            self._waiting = value

        def read_until(self, expected: bytes, size: int | None = None) -> Any:
            exploded = self.explode
            self.explode = False
            try:
                return super().read_until(expected, size)
            finally:
                self.explode = exploded

    fake = ExplodingWaitingFake([b"", b"ok\n"])
    session = _open(fake)
    fake.explode = True
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.code == "SERIAL_WAITING_FAILED"

    fake = FakeSerial([b"", b"ok\n"])
    session = _open(fake)
    fake.in_waiting = -1
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.code == "SERIAL_WAITING_INVALID"

    pending = [b"noise\n"] * 257
    fake = FakeSerial([b"", b"ok\n", *pending])
    fake.post_ack_waiting = sum(map(len, pending))
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.code == "SERIAL_RESPONSE_BOUNDS_EXCEEDED"
    assert fake.writes == [b"$HZ\n"]

    def ack_then_close() -> bytes:
        fake.is_open = False
        return b"ok\n"

    fake = FakeSerial([b"", ack_then_close])
    outcome = _open(fake).home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TRANSPORT_LOSS"
    assert fake.writes == [b"$HZ\n"]


def test_late_successful_action_read_cannot_authorize_next_stream_command() -> None:
    config = replace(
        _config(),
        command_timeout_seconds=0.03,
        home_timeout_seconds=0.03,
        read_timeout_seconds=0.03,
        stream_idle_timeout_seconds=0.03,
    )
    fake = FakeSerial([b""])

    def late_ok() -> bytes:
        time.sleep(0.08)
        return b"ok\n"

    fake.reads.append(late_ok)
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.stream_program(StreamProgramRequest(_program()), lambda update: None)
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert outcome.failure.side_effect_may_have_occurred
    assert fake.writes == [b"G21\n"]


def test_late_write_read_status_and_drain_returns_fail_before_any_followup() -> None:
    config = replace(
        _config(),
        command_timeout_seconds=0.03,
        home_timeout_seconds=0.03,
        read_timeout_seconds=0.03,
        stream_idle_timeout_seconds=0.03,
    )

    fake = FakeSerial([b""])

    def late_write(value: bytes) -> int:
        time.sleep(0.08)
        return len(value)

    fake.write = late_write  # type: ignore[method-assign]
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert fake.read_bounds == [(b"\n", 4097)]

    for late_line in (b"error:9\n", b"<Idle|MPos:192,192,192>\n"):
        fake = FakeSerial([b""])

        def late_read(value: bytes = late_line) -> bytes:
            time.sleep(0.08)
            return value

        fake.reads.append(late_read)
        session = SerialFluidNCFactory(config, serial_factory=lambda current=fake: current).open_session(
            OpenSessionRequest(EPOCH)
        )
        outcome = session.home_z_axis(HomeZAxisRequest())
        assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
        assert fake.writes == [b"$HZ\n"]

    fake = FakeSerial([b"", b"ok\n"])

    def late_status() -> bytes:
        time.sleep(0.08)
        return b"<Idle|MPos:192,192,192>\n"

    fake.reads.append(late_status)
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert fake.writes == [b"$HZ\n", b"?"]

    fake = FakeSerial([b"", b"ok\n"])
    fake.post_ack_waiting = len(b"watchdog reset\n")

    def late_pending() -> bytes:
        time.sleep(0.08)
        return b"watchdog reset\n"

    fake.reads.append(late_pending)
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert fake.writes == [b"$HZ\n"]

    class LateWaitingFake(FakeSerial):
        slow_waiting = False

        @property
        def in_waiting(self) -> int:
            if self.slow_waiting:
                time.sleep(0.08)
            return self._waiting

        @in_waiting.setter
        def in_waiting(self, value: int) -> None:
            self._waiting = value

        def read_until(self, expected: bytes, size: int | None = None) -> Any:
            slow = self.slow_waiting
            self.slow_waiting = False
            try:
                return super().read_until(expected, size)
            finally:
                self.slow_waiting = slow

    fake = LateWaitingFake([b"", b"ok\n"])
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    fake.slow_waiting = True
    outcome = session.home_z_axis(HomeZAxisRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "LOST_ACK"
    assert fake.writes == [b"$HZ\n"]


def test_late_side_effect_free_preflight_read_is_timeout_and_stops_before_metadata_queries() -> None:
    config = replace(_config(), command_timeout_seconds=0.03, read_timeout_seconds=0.03)
    fake = FakeSerial([b""])

    def late_status() -> bytes:
        time.sleep(0.08)
        return b"<Idle|MPos:192,192,192>\n"

    fake.reads.append(late_status)
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TIMEOUT"
    assert not outcome.failure.side_effect_may_have_occurred
    assert outcome.evidence == ()
    assert fake.writes == [b"?"]

    fake = FakeSerial([b""])

    def late_preflight_write(value: bytes) -> int:
        fake.writes.append(value)
        time.sleep(0.08)
        return len(value)

    fake.write = late_preflight_write  # type: ignore[method-assign]
    session = SerialFluidNCFactory(config, serial_factory=lambda: fake).open_session(OpenSessionRequest(EPOCH))
    outcome = session.read_initial_preflight(InitialPreflightRequest())
    assert outcome.failure is not None and outcome.failure.kind.value == "TIMEOUT"
    assert not outcome.failure.side_effect_may_have_occurred
    assert fake.writes == [b"?"]
    assert fake.read_bounds == [(b"\n", 4097)]
