from __future__ import annotations

import copy
import hashlib
import inspect
import threading
from dataclasses import FrozenInstanceError

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeBarrierPoint,
    FakeBarrierScenario,
    FakeFluidNCBarrier,
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.domain.fluidnc.gates import SessionObservationPhase, classify_session_observation
from drawingmachine.domain.fluidnc.status import PreflightSnapshot, parse_preflight_snapshot
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.stream_program import build_validated_stream_program
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
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
    StreamProgressUpdate,
    ZCalibrationOutcome,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)

_EPOCH = "11111111-1111-4111-8111-111111111111"


def _snapshot(*, mpos: str, wpos: str | None = None) -> PreflightSnapshot:
    positions = f"MPos:{mpos}"
    if wpos is not None:
        positions += f"|WPos:{wpos}|WCO:96.000,96.000,188.500"
    return parse_preflight_snapshot(
        status_line=f"<Idle|{positions}>",
        parser_state=("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]",),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=(),
        missing_ok_commands=(),
    )


def _program_request(lines: bytes = b"G21\nG90\nG0 X1 Y2\n") -> StreamProgramRequest:
    plan = build_send_plan(lines.decode("ascii"))
    program = build_validated_stream_program(lines, plan.to_json(), hashlib.sha256(lines).hexdigest())
    return StreamProgramRequest(program)


def _progress(request: StreamProgramRequest, acknowledged: int) -> tuple[StreamProgressUpdate, ...]:
    return tuple(
        StreamProgressUpdate(
            machine_session_epoch=_EPOCH,
            acknowledged_commands=index,
            source_line=command.source_line,
            command_digest=hashlib.sha256(command.command.encode("ascii")).hexdigest(),
        )
        for index, command in enumerate(request.program.commands[:acknowledged], start=1)
    )


def _fresh_outcome() -> PreflightOutcome:
    snapshot = _snapshot(mpos="0.000,0.000,0.000")
    evidence = ("FluidNC reset",)
    phase = SessionObservationPhase.FRESH_STABILIZATION
    return PreflightOutcome(
        _EPOCH,
        snapshot,
        evidence,
        phase,
        classify_session_observation(evidence, snapshot.status, phase),
    )


def _close_step() -> FakeFluidNCStep:
    request = CloseSessionRequest(_EPOCH)
    return FakeFluidNCStep(FakeFluidNCOperation.CLOSE, request, CloseOutcome(_EPOCH))


def test_strict_fake_records_exact_single_session_and_complete_program() -> None:
    preflight_request = InitialPreflightRequest()
    home_request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    zcal_request = ZCalibrationRequest("G54", 96.0, 96.0, 3.5, 0.0, 1200.0, 100.0)
    zconfirm_request = ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.1)
    stream_request = _program_request()
    home_z_request = HomeZAxisRequest()
    homed = _snapshot(mpos="192.000,192.000,192.000")
    calibrated = _snapshot(mpos="192.000,192.000,192.000", wpos="96.000,96.000,3.500")
    stream_progress = _progress(stream_request, len(stream_request.program.commands))
    steps = (
        FakeFluidNCStep(FakeFluidNCOperation.INITIAL_PREFLIGHT, preflight_request, _fresh_outcome()),
        FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, home_request, HomeOutcome(_EPOCH, home_request, homed)),
        FakeFluidNCStep(
            FakeFluidNCOperation.Z_CALIBRATION,
            zcal_request,
            ZCalibrationOutcome(_EPOCH, zcal_request, 6, None),
        ),
        FakeFluidNCStep(
            FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
            zconfirm_request,
            ZConfirmationOutcome(_EPOCH, zconfirm_request, calibrated),
        ),
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            stream_request,
            StreamOutcome(
                _EPOCH,
                len(stream_request.program.commands),
                len(stream_request.program.commands),
                calibrated,
            ),
            stream_progress,
        ),
        FakeFluidNCStep(FakeFluidNCOperation.HOME_Z_AXIS, home_z_request, HomeZOutcome(_EPOCH, home_z_request, homed)),
        _close_step(),
    )
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), steps)

    session = factory.open_session(OpenSessionRequest(_EPOCH))
    assert session.read_initial_preflight(preflight_request) == steps[0].outcome
    assert session.home_all_axes(home_request) == steps[1].outcome
    assert session.run_z_calibration(zcal_request) == steps[2].outcome
    assert session.raise_after_z_confirmation(zconfirm_request) == steps[3].outcome
    observed: list[StreamProgressUpdate] = []
    assert session.stream_program(stream_request, observed.append) == steps[4].outcome
    assert tuple(observed) == stream_progress
    assert session.home_z_axis(home_z_request) == steps[5].outcome
    assert session.close(CloseSessionRequest(_EPOCH)) == steps[6].outcome
    factory.assert_script_consumed()

    assert factory.connections_opened == 1
    assert factory.session_epoch == _EPOCH
    assert tuple(call.operation for call in factory.calls) == tuple(step.operation for step in steps)
    assert factory.calls[4].request.program is stream_request.program


@pytest.mark.parametrize(
    ("operation", "operation_input"),
    [
        (FakeFluidNCOperation.HOME_ALL_AXES, HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)),
        (FakeFluidNCOperation.Z_CALIBRATION, ZCalibrationRequest("G54", 96.0, 96.0, 3.5, 0.0, 1200.0, 100.0)),
        (FakeFluidNCOperation.Z_CONFIRMATION_RAISE, ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.1)),
        (FakeFluidNCOperation.HOME_Z_AXIS, HomeZAxisRequest()),
    ],
)
def test_fake_denies_undeclared_operations(operation: FakeFluidNCOperation, operation_input: object) -> None:
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (_close_step(),))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    method = {
        FakeFluidNCOperation.HOME_ALL_AXES: session.home_all_axes,
        FakeFluidNCOperation.Z_CALIBRATION: session.run_z_calibration,
        FakeFluidNCOperation.Z_CONFIRMATION_RAISE: session.raise_after_z_confirmation,
        FakeFluidNCOperation.HOME_Z_AXIS: session.home_z_axis,
    }[operation]
    with pytest.raises(DrawingMachineError, match="script order"):
        method(operation_input)  # type: ignore[arg-type]


def test_fake_denies_reordered_mismatched_repeated_and_reopened_calls() -> None:
    expected = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    step = FakeFluidNCStep(
        FakeFluidNCOperation.HOME_ALL_AXES,
        expected,
        HomeOutcome(_EPOCH, expected, _snapshot(mpos="192.000,192.000,192.000")),
    )
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    with pytest.raises(DrawingMachineError, match="undeclared FluidNC connection"):
        factory.open_session(OpenSessionRequest("22222222-2222-4222-8222-222222222222"))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="exactly one connection"):
        factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="arguments"):
        session.home_all_axes(HomeAllAxesRequest((192.0, 192.0, 192.0), 0.2))
    session.home_all_axes(expected)
    with pytest.raises(DrawingMachineError, match="script order"):
        session.home_all_axes(expected)
    session.close(CloseSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="closed"):
        session.close(CloseSessionRequest(_EPOCH))


def test_fake_fails_when_script_is_unconsumed_or_session_not_closed() -> None:
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (_close_step(),))
    with pytest.raises(DrawingMachineError, match="not opened"):
        factory.assert_script_consumed()
    factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="not consumed"):
        factory.assert_script_consumed()

    no_close = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), ())
    no_close.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="not closed"):
        no_close.assert_script_consumed()


@pytest.mark.parametrize("scenario", tuple(FakeBarrierScenario))
def test_fake_barriers_are_deterministic_for_every_required_race(scenario: FakeBarrierScenario) -> None:
    if scenario is FakeBarrierScenario.PARTIAL_STREAM:
        stream_request = _program_request()
        barrier = FakeFluidNCBarrier(scenario, acknowledged_prefix=1)
        stream_failure = FluidNCFailure(
            FluidNCFailureKind.TRANSPORT_LOSS,
            "TRANSPORT_LOSS",
            "connection lost after partial stream",
            True,
        )
        step = FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            stream_request,
            StreamOutcome(_EPOCH, len(stream_request.program.commands), 1, None, stream_failure),
            _progress(stream_request, 1),
            barrier,
        )
    else:
        barrier = FakeFluidNCBarrier(scenario)
        home_request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
        if scenario is FakeBarrierScenario.CONTROLLER_RESET:
            reset_snapshot = _snapshot(mpos="0.000,0.000,0.000")
            reset_evidence: tuple[str, ...] = ()
            reset_phase = SessionObservationPhase.ACTION_ADMITTED
            reset_failure = FluidNCFailure(
                FluidNCFailureKind.CONTROLLER_RESET,
                "CONTROLLER_RESET",
                "controller reset after admitted HOME",
                True,
            )
            home_outcome = HomeOutcome(
                _EPOCH,
                home_request,
                reset_snapshot,
                reset_failure,
                reset_evidence,
                reset_phase,
                classify_session_observation(reset_evidence, reset_snapshot.status, reset_phase),
                acknowledged_steps=0,
                stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
            )
        elif scenario in {FakeBarrierScenario.LOST_ACK, FakeBarrierScenario.CONNECTION_LOSS}:
            failure_kind = (
                FluidNCFailureKind.LOST_ACK
                if scenario is FakeBarrierScenario.LOST_ACK
                else FluidNCFailureKind.TRANSPORT_LOSS
            )
            home_outcome = HomeOutcome(
                _EPOCH,
                home_request,
                None,
                FluidNCFailure(failure_kind, failure_kind.value, "scripted ambiguous HOME failure", True),
                acknowledged_steps=0,
                stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
            )
        else:
            home_outcome = HomeOutcome(
                _EPOCH,
                home_request,
                _snapshot(mpos="192.000,192.000,192.000"),
            )
        step = FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, home_request, home_outcome, barrier=barrier)
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    completed = threading.Event()

    if scenario is FakeBarrierScenario.PARTIAL_STREAM:
        observed: list[StreamProgressUpdate] = []
        thread = threading.Thread(
            target=lambda: (session.stream_program(step.request, observed.append), completed.set())
        )  # type: ignore[arg-type]
    else:
        thread = threading.Thread(target=lambda: (session.home_all_axes(step.request), completed.set()))  # type: ignore[arg-type]
    thread.start()
    assert barrier.wait_until_entered()
    assert not completed.is_set()
    if barrier.point is FakeBarrierPoint.BEFORE_OPERATION:
        assert factory.started_operations == ()
        assert factory.calls == ()
    else:
        assert factory.started_operations == (step.operation,)
        assert factory.calls == ()
    barrier.release()
    thread.join(5.0)
    assert completed.is_set()
    session.close(CloseSessionRequest(_EPOCH))
    factory.assert_script_consumed()


def test_fake_models_partial_stream_lost_ack_and_complete_program_identity() -> None:
    request = _program_request()
    lost_ack = FluidNCFailure(
        FluidNCFailureKind.LOST_ACK,
        "STREAM_ACK_LOST",
        "acknowledgement was lost after write",
        True,
    )
    outcome = StreamOutcome(_EPOCH, len(request.program.commands), 1, None, lost_ack)
    step = FakeFluidNCStep(
        FakeFluidNCOperation.STREAM_PROGRAM,
        request,
        outcome,
        _progress(request, 1),
        FakeFluidNCBarrier(FakeBarrierScenario.LOST_ACK),
    )
    assert step.outcome.failure is lost_ack
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    observed: list[StreamProgressUpdate] = []
    completed = threading.Event()
    thread = threading.Thread(target=lambda: (session.stream_program(request, observed.append), completed.set()))
    thread.start()
    assert step.barrier is not None
    assert step.barrier.wait_until_entered()
    assert tuple(observed) == _progress(request, 1)
    assert factory.started_operations == (FakeFluidNCOperation.STREAM_PROGRAM,)
    assert factory.calls == ()
    step.barrier.release()
    thread.join(5.0)
    assert completed.is_set()
    session.close(CloseSessionRequest(_EPOCH))
    factory.assert_script_consumed()
    with pytest.raises(TypeError, match="complete validated program"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            _program_request(b"G21\n"),
            outcome,
            (),
        )


def test_fake_models_late_reset_as_recovery_not_fresh_session() -> None:
    snapshot = _snapshot(mpos="0.000,0.000,0.000")
    evidence = ("watchdog reset",)
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET",
        "controller reset after action admission",
        True,
    )
    for phase in (SessionObservationPhase.STABILIZED, SessionObservationPhase.ACTION_ADMITTED):
        decision = classify_session_observation(evidence, snapshot.status, phase)
        request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
        outcome = HomeOutcome(
            _EPOCH,
            request,
            snapshot,
            failure,
            evidence,
            phase,
            decision,
            acknowledged_steps=0,
            stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
        )
        assert outcome.decision is not None
        assert outcome.decision.disposition.value == "RECOVERY_REQUIRED"

    fresh = _fresh_outcome()
    assert fresh.decision.disposition.value == "AWAIT_HOME_ONLY"
    with pytest.raises(TypeError, match="post-open"):
        HomeOutcome(
            _EPOCH,
            HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1),
            snapshot,
            failure,
            evidence,
            SessionObservationPhase.FRESH_STABILIZATION,
            fresh.decision,
        )


def test_fake_script_and_port_values_are_deeply_immutable_and_reject_subclasses() -> None:
    request = _program_request()
    outcome = StreamOutcome(
        _EPOCH,
        len(request.program.commands),
        len(request.program.commands),
        _snapshot(mpos="192.000,192.000,192.000"),
    )
    step = FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, request, outcome, _progress(request, 3))
    assert copy.copy(step) == step
    assert copy.deepcopy(step) == step
    with pytest.raises(FrozenInstanceError):
        step.outcome = outcome  # type: ignore[misc]
    with pytest.raises(TypeError, match="exact tuple"):
        StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), [step])  # type: ignore[arg-type]

    class HostileOpen(OpenSessionRequest):
        pass

    with pytest.raises(TypeError, match="expected-open"):
        StrictFakeFluidNCFactory(HostileOpen(_EPOCH), ())


def test_fake_has_no_serial_transport_or_raw_exception_injection_surface() -> None:
    source = inspect.getsource(inspect.getmodule(StrictFakeFluidNCFactory))
    assert "import serial" not in source
    assert "pyserial" not in source
    signature = inspect.signature(StrictFakeFluidNCFactory)
    assert set(signature.parameters) == {"expected_open", "steps"}
    with pytest.raises(TypeError):
        StrictFakeFluidNCFactory(  # type: ignore[call-arg]
            OpenSessionRequest(_EPOCH),
            (),
            serial_path="/dev/ttyUSB0",
        )


def test_fake_validates_close_position_preflight_phase_and_barrier_types() -> None:
    home = FakeFluidNCStep(
        FakeFluidNCOperation.HOME_ALL_AXES,
        HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1),
        HomeOutcome(
            _EPOCH,
            HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1),
            _snapshot(mpos="192.000,192.000,192.000"),
        ),
    )
    with pytest.raises(TypeError, match="only at the end"):
        StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (_close_step(), home))
    with pytest.raises(TypeError, match="barrier scenario"):
        FakeFluidNCBarrier("SHUTDOWN")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="barrier point"):
        FakeFluidNCBarrier(FakeBarrierScenario.SHUTDOWN, "BEFORE")  # type: ignore[arg-type]
    immutable_barrier = FakeFluidNCBarrier(FakeBarrierScenario.COMMIT_AFTER_SIDE_EFFECT)
    with pytest.raises(AttributeError):
        immutable_barrier.point = FakeBarrierPoint.BEFORE_OPERATION  # type: ignore[misc]
    with pytest.raises(AttributeError):
        immutable_barrier.scenario = FakeBarrierScenario.SHUTDOWN  # type: ignore[misc]

    snapshot = _snapshot(mpos="0.000,0.000,0.000")
    evidence = ("watchdog reset",)
    phase = SessionObservationPhase.STABILIZED
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET_DETECTED",
        "FluidNC reset evidence invalidated the active session",
        True,
    )
    stabilized = PreflightOutcome(
        _EPOCH,
        snapshot,
        evidence,
        phase,
        classify_session_observation(evidence, snapshot.status, phase),
        failure,
    )
    assert stabilized.observation_phase is SessionObservationPhase.STABILIZED
    with pytest.raises(TypeError, match="fresh or stabilized"):
        PreflightOutcome(
            _EPOCH,
            snapshot,
            evidence,
            SessionObservationPhase.ACTION_ADMITTED,
            classify_session_observation(evidence, snapshot.status, SessionObservationPhase.ACTION_ADMITTED),
            failure,
        )


def test_fake_wraps_raw_progress_callback_failures() -> None:
    request = _program_request(b"G21\n")
    outcome = StreamOutcome(_EPOCH, 1, 1, _snapshot(mpos="192.000,192.000,192.000"))
    step = FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, request, outcome, _progress(request, 1))
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    session = factory.open_session(OpenSessionRequest(_EPOCH))

    def raw_failure(_update: StreamProgressUpdate) -> None:
        raise RuntimeError("secret raw adapter error")

    with pytest.raises(DrawingMachineError, match="without exposing") as captured:
        session.stream_program(request, raw_failure)
    assert "secret" not in str(captured.value)
    session.close(CloseSessionRequest(_EPOCH))
    factory.assert_script_consumed()


def test_fake_rejects_success_without_operation_specific_postcondition_proofs() -> None:
    unsafe = _snapshot(mpos="1.000,2.000,3.000")
    home = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    with pytest.raises(TypeError, match="HOME proof"):
        HomeOutcome(_EPOCH, home, unsafe)

    zconfirm = ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.1)
    with pytest.raises(TypeError, match="ZCONFIRM proof"):
        ZConfirmationOutcome(_EPOCH, zconfirm, unsafe)

    non_idle = parse_preflight_snapshot(
        status_line="<Run|MPos:192.000,192.000,192.000>",
        parser_state=(),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=(),
        missing_ok_commands=(),
    )
    with pytest.raises(TypeError, match=r"HOME_Z.*Idle"):
        HomeZOutcome(_EPOCH, HomeZAxisRequest(), non_idle)

    with pytest.raises(TypeError, match=r"STREAM.*Idle"):
        StreamOutcome(_EPOCH, 1, 1, non_idle)


def test_partial_stream_barrier_arrives_after_configured_progress_prefix() -> None:
    request = _program_request()
    failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "TRANSPORT_LOSS",
        "connection lost after acknowledged prefix",
        True,
    )
    outcome = StreamOutcome(_EPOCH, len(request.program.commands), 1, None, failure)
    barrier = FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=1)
    step = FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, request, outcome, _progress(request, 1), barrier)
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    observed: list[StreamProgressUpdate] = []
    completed = threading.Event()
    thread = threading.Thread(target=lambda: (session.stream_program(request, observed.append), completed.set()))
    thread.start()
    assert barrier.wait_until_entered()
    assert len(observed) == 1
    assert not completed.is_set()
    barrier.release()
    thread.join(5.0)
    session.close(CloseSessionRequest(_EPOCH))
    factory.assert_script_consumed()


def test_fake_step_and_factory_exact_type_branch_matrix() -> None:
    home_request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    home_outcome = HomeOutcome(_EPOCH, home_request, _snapshot(mpos="192.000,192.000,192.000"))
    with pytest.raises(TypeError, match="operation must be exact"):
        FakeFluidNCStep("HOME_ALL_AXES", home_request, home_outcome)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="types do not match"):
        FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, CloseSessionRequest(_EPOCH), CloseOutcome(_EPOCH))
    stream_request = _program_request(b"G21\n")
    stream_outcome = StreamOutcome(_EPOCH, 1, 1, _snapshot(mpos="192.000,192.000,192.000"))
    update = _progress(stream_request, 1)[0]
    with pytest.raises(TypeError, match="exact tuple"):
        FakeFluidNCStep(  # type: ignore[arg-type]
            FakeFluidNCOperation.STREAM_PROGRAM,
            stream_request,
            stream_outcome,
            [update],
        )
    with pytest.raises(TypeError, match="exact updates"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            stream_request,
            stream_outcome,
            (object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="only STREAM"):
        FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, home_request, home_outcome, (update,))
    with pytest.raises(TypeError, match="barrier must be exact"):
        FakeFluidNCStep(  # type: ignore[arg-type]
            FakeFluidNCOperation.HOME_ALL_AXES,
            home_request,
            home_outcome,
            barrier=object(),
        )
    with pytest.raises(TypeError, match="exact tuple"):
        StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (object(),))  # type: ignore[arg-type]
    different_epoch = "22222222-2222-4222-8222-222222222222"
    other_close = FakeFluidNCStep(
        FakeFluidNCOperation.CLOSE,
        CloseSessionRequest(different_epoch),
        CloseOutcome(different_epoch),
    )
    with pytest.raises(TypeError, match="opened session epoch"):
        StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (other_close,))
    with pytest.raises(TypeError, match="only at the end"):
        StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (_close_step(), _close_step()))


def test_fake_runtime_default_denial_branch_matrix() -> None:
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), ())
    assert factory.calls == ()
    assert factory.started_operations == ()
    assert factory.session_epoch is None
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="undeclared"):
        session.home_all_axes(HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1))

    class HostileHome(HomeAllAxesRequest):
        pass

    request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    step = FakeFluidNCStep(
        FakeFluidNCOperation.HOME_ALL_AXES,
        request,
        HomeOutcome(_EPOCH, request, _snapshot(mpos="192.000,192.000,192.000")),
    )
    exact_factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    exact_session = exact_factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="arguments"):
        exact_session.home_all_axes(HostileHome((192.0, 192.0, 192.0), 0.1))
    exact_session.home_all_axes(request)
    exact_session.close(CloseSessionRequest(_EPOCH))


def test_fake_stream_callback_and_zero_prefix_barrier_branches() -> None:
    request = _program_request(b"G21\n")
    failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "TRANSPORT_LOSS",
        "stream failed after first acknowledgement",
        True,
    )
    outcome = StreamOutcome(_EPOCH, 1, 1, None, failure)
    step = FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, request, outcome, _progress(request, 1))
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (step, _close_step()))
    session = factory.open_session(OpenSessionRequest(_EPOCH))
    with pytest.raises(DrawingMachineError, match="must be callable"):
        session.stream_program(request, None)  # type: ignore[arg-type]

    typed = DrawingMachineError(
        ErrorPayload("PROGRESS_FAILED", ErrorCategory.HARDWARE, "typed progress failure", False, {})
    )

    def typed_failure(_update: StreamProgressUpdate) -> None:
        raise typed

    with pytest.raises(DrawingMachineError) as captured:
        session.stream_program(request, typed_failure)
    assert captured.value is typed
    session.close(CloseSessionRequest(_EPOCH))

    zero_barrier = FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=0)
    zero_outcome = StreamOutcome(_EPOCH, 1, 0, None, failure)
    zero_step = FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, request, zero_outcome, (), zero_barrier)
    zero_factory = StrictFakeFluidNCFactory(OpenSessionRequest(_EPOCH), (zero_step, _close_step()))
    zero_session = zero_factory.open_session(OpenSessionRequest(_EPOCH))
    completed = threading.Event()
    thread = threading.Thread(
        target=lambda: (zero_session.stream_program(request, lambda _update: None), completed.set())
    )
    thread.start()
    assert zero_barrier.wait_until_entered()
    assert zero_factory.started_operations == (FakeFluidNCOperation.STREAM_PROGRAM,)
    assert zero_factory.calls == ()
    zero_barrier.release()
    thread.join(5.0)
    assert completed.is_set()
    zero_session.close(CloseSessionRequest(_EPOCH))


def test_fake_epoch_argument_and_program_progress_mismatch_branches() -> None:
    different_epoch = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(TypeError, match="session epochs differ"):
        FakeFluidNCStep(
            FakeFluidNCOperation.CLOSE,
            CloseSessionRequest(different_epoch),
            CloseOutcome(_EPOCH),
        )
    request_a = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    request_b = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.2)
    with pytest.raises(TypeError, match="different exact arguments"):
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_ALL_AXES,
            request_b,
            HomeOutcome(_EPOCH, request_a, _snapshot(mpos="192.000,192.000,192.000")),
        )

    stream_request = _program_request()
    failure = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "stream timed out", True)
    outcome = StreamOutcome(_EPOCH, len(stream_request.program.commands), 1, None, failure)
    with pytest.raises(TypeError, match="progress count"):
        FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, stream_request, outcome, ())
    valid = _progress(stream_request, 1)[0]
    bad_updates = (
        StreamProgressUpdate(different_epoch, 1, valid.source_line, valid.command_digest),
        StreamProgressUpdate(_EPOCH, 2, valid.source_line, valid.command_digest),
        StreamProgressUpdate(_EPOCH, 1, valid.source_line + 1, valid.command_digest),
        StreamProgressUpdate(_EPOCH, 1, valid.source_line, "f" * 64),
    )
    for update in bad_updates:
        with pytest.raises(TypeError, match="exactly map"):
            FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, stream_request, outcome, (update,))


def test_fake_rejects_impossible_barrier_scenario_stage_combinations() -> None:
    with pytest.raises(TypeError, match="incompatible"):
        FakeFluidNCBarrier(FakeBarrierScenario.SHUTDOWN, FakeBarrierPoint.AFTER_SIDE_EFFECT)
    with pytest.raises(TypeError, match="non-negative"):
        FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM)
    with pytest.raises(TypeError, match="non-negative"):
        FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=-1)
    with pytest.raises(TypeError, match="only a partial"):
        FakeFluidNCBarrier(FakeBarrierScenario.SHUTDOWN, acknowledged_prefix=0)

    home_request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    home_outcome = HomeOutcome(_EPOCH, home_request, _snapshot(mpos="192.000,192.000,192.000"))
    with pytest.raises(TypeError, match="only for STREAM"):
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_ALL_AXES,
            home_request,
            home_outcome,
            barrier=FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=0),
        )

    request = _program_request(b"G21\n")
    timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "stream timed out", True)
    failed = StreamOutcome(_EPOCH, 1, 0, None, timeout)
    with pytest.raises(TypeError, match="exceeds scripted progress"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            request,
            failed,
            (),
            FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=1),
        )
    success = StreamOutcome(_EPOCH, 1, 1, _snapshot(mpos="192.000,192.000,192.000"))
    with pytest.raises(TypeError, match="failed STREAM"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            request,
            success,
            _progress(request, 1),
            FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=1),
        )
    with pytest.raises(TypeError, match="failed acknowledged prefix"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            request,
            failed,
            _progress(request, 1),
            FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=1),
        )

    with pytest.raises(TypeError, match="motion-capable"):
        FakeFluidNCStep(
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            InitialPreflightRequest(),
            _fresh_outcome(),
            barrier=FakeFluidNCBarrier(FakeBarrierScenario.LOST_ACK),
        )
    with pytest.raises(TypeError, match="exact typed failure"):
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_ALL_AXES,
            home_request,
            HomeOutcome(
                _EPOCH,
                home_request,
                None,
                timeout,
                acknowledged_steps=0,
                stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
            ),
            barrier=FakeFluidNCBarrier(FakeBarrierScenario.LOST_ACK),
        )


def test_fake_barrier_timeout_is_a_typed_fail_closed_error() -> None:
    barrier = FakeFluidNCBarrier(FakeBarrierScenario.SHUTDOWN)
    with pytest.raises(DrawingMachineError, match="not released"):
        barrier._cross()


@pytest.mark.parametrize(
    ("scenario", "operation"),
    [
        (FakeBarrierScenario.COMMIT_AFTER_SIDE_EFFECT, FakeFluidNCOperation.HOME_ALL_AXES),
        (FakeBarrierScenario.CONNECTION_LOSS, FakeFluidNCOperation.HOME_ALL_AXES),
    ],
)
def test_after_side_effect_barrier_rejects_false_side_effect_failure(
    scenario: FakeBarrierScenario,
    operation: FakeFluidNCOperation,
) -> None:
    request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    false_failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "TRANSPORT_LOSS",
        "transport failed before write",
        False,
    )
    with pytest.raises(TypeError, match=r"barrier.*side effect"):
        FakeFluidNCStep(
            operation,
            request,
            HomeOutcome(
                _EPOCH,
                request,
                None,
                false_failure,
                acknowledged_steps=0,
                stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
            ),
            barrier=FakeFluidNCBarrier(scenario),
        )


def test_partial_stream_barrier_rejects_false_side_effect_failure_even_at_prefix_zero() -> None:
    request = _program_request(b"G21\n")
    false_failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "TRANSPORT_LOSS",
        "transport failed before write",
        False,
    )
    outcome = StreamOutcome(_EPOCH, 1, 0, None, false_failure)
    with pytest.raises(TypeError, match=r"barrier.*side effect"):
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            request,
            outcome,
            (),
            FakeFluidNCBarrier(FakeBarrierScenario.PARTIAL_STREAM, acknowledged_prefix=0),
        )


def test_preoperation_barrier_and_unscripted_prewrite_failure_preserve_false_evidence() -> None:
    request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    false_timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out before write", False)
    outcome = HomeOutcome(
        _EPOCH,
        request,
        None,
        false_timeout,
        acknowledged_steps=0,
        stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
    )
    plain = FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, request, outcome)
    before = FakeFluidNCStep(
        FakeFluidNCOperation.HOME_ALL_AXES,
        request,
        outcome,
        barrier=FakeFluidNCBarrier(FakeBarrierScenario.SHUTDOWN),
    )
    assert plain.outcome.failure is false_timeout
    assert before.outcome.failure is false_timeout
