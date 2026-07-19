from __future__ import annotations

import hashlib
import inspect
from dataclasses import FrozenInstanceError

import pytest

from drawingmachine.domain.fluidnc.gates import SessionObservationPhase, classify_session_observation
from drawingmachine.domain.fluidnc.status import (
    ControllerStatus,
    FluidNCState,
    PreflightSnapshot,
    parse_preflight_snapshot,
)
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.stream_program import build_validated_stream_program
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import (
    FluidNCFailure,
    FluidNCFailureKind,
    FluidNCSession,
    FluidNCSessionFactory,
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
    ZCalibrationOutcomeStage,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)

_EPOCH = "11111111-1111-4111-8111-111111111111"

_FORBIDDEN_CAPABILITY_TOKENS = (
    "command",
    "gcode",
    "jog",
    "offset",
    "raw",
    "reopen",
    "resume",
    "serial",
    "unlock",
    "write",
)


def _public_surface(protocol: type[object]) -> set[str]:
    return {name.casefold() for base in inspect.getmro(protocol) for name in vars(base) if not name.startswith("_")}


def test_fluidnc_port_exposes_only_closed_typed_semantic_capabilities() -> None:
    factory_surface = _public_surface(FluidNCSessionFactory)
    session_surface = _public_surface(FluidNCSession)

    assert factory_surface == {"open_session"}
    assert session_surface == {
        "close",
        "home_all_axes",
        "home_z_axis",
        "raise_after_z_confirmation",
        "read_initial_preflight",
        "run_z_calibration",
        "stream_program",
    }
    for name in factory_surface | session_surface:
        assert not any(token in name for token in _FORBIDDEN_CAPABILITY_TOKENS)


def test_fluidnc_port_has_no_variadic_or_untyped_escape_hatch() -> None:
    for protocol in (FluidNCSessionFactory, FluidNCSession):
        for name in _public_surface(protocol):
            signature = inspect.signature(getattr(protocol, name))
            assert all(
                parameter.kind not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
                for parameter in signature.parameters.values()
            )
            assert signature.return_annotation is not inspect.Signature.empty
            for parameter in signature.parameters.values():
                if parameter.name == "self":
                    continue
                assert parameter.annotation is not inspect.Signature.empty


def test_port_inputs_are_frozen_and_validate_exact_bounded_values() -> None:
    opened = OpenSessionRequest(_EPOCH)
    home = HomeAllAxesRequest((192, 192.0, 192), 0.1)
    zcal = ZCalibrationRequest("G54", 96, 96.0, 3.5, 0, 1200, 100)
    zconfirm = ZConfirmationRequest((96, 96.0, 3.5), 3.5, 400, 0.1)

    assert opened.machine_session_epoch == _EPOCH
    assert home.expected_mpos == (192.0, 192.0, 192.0)
    assert zcal.contact_z == 0.0
    assert zconfirm.expected_work_position == (96.0, 96.0, 3.5)
    with pytest.raises(FrozenInstanceError):
        home.tolerance_mm = 1.0  # type: ignore[misc]
    with pytest.raises(TypeError, match="canonical UUID"):
        OpenSessionRequest("../session")
    with pytest.raises((TypeError, DrawingMachineError), match="finite"):
        HomeAllAxesRequest((True, 192.0, 192.0), 0.1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="G54"):
        ZCalibrationRequest("G55", 96, 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="below"):
        ZCalibrationRequest("G54", 96, 96, 3.5, 3.5, 1200, 100)
    with pytest.raises(TypeError, match="equal"):
        ZConfirmationRequest((96, 96, 0), 3.5, 400, 0.1)


def test_preflight_outcome_binds_epoch_phase_evidence_and_decision() -> None:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=("[GC:G54]",),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC watchdog reset",)
    phase = SessionObservationPhase.FRESH_STABILIZATION
    decision = classify_session_observation(evidence, snapshot.status, phase)
    outcome = PreflightOutcome(_EPOCH, snapshot, evidence, phase, decision)
    assert outcome.decision.disposition.value == "AWAIT_HOME_ONLY"

    wrong = classify_session_observation((), snapshot.status, SessionObservationPhase.STABILIZED)
    with pytest.raises(TypeError, match="contradicts"):
        PreflightOutcome(_EPOCH, snapshot, evidence, phase, wrong)


def test_late_reset_outcome_requires_atomic_typed_recovery_evidence() -> None:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=("[GC:G54]",),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("controller reset",)
    phase = SessionObservationPhase.ACTION_ADMITTED
    decision = classify_session_observation(evidence, snapshot.status, phase)
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET",
        "controller reset after admitted motion",
        True,
    )
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
    assert outcome.decision is decision
    with pytest.raises(TypeError, match="atomic"):
        HomeOutcome(_EPOCH, request, snapshot, failure, evidence)
    with pytest.raises(TypeError, match="controller-reset"):
        HomeOutcome(_EPOCH, request, snapshot, None, evidence, phase, decision)


def test_port_rejects_hostile_subclasses_and_mutable_container_aliases() -> None:
    class HostileFailure(FluidNCFailure):
        pass

    hostile = HostileFailure(
        FluidNCFailureKind.TIMEOUT,
        "TIMEOUT",
        "timed out",
        False,
    )
    with pytest.raises(TypeError, match="exact FluidNCFailure"):
        HomeOutcome(_EPOCH, HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1), None, hostile)
    with pytest.raises(TypeError, match="exact tuple"):
        PreflightOutcome(  # type: ignore[arg-type]
            _EPOCH,
            parse_preflight_snapshot(
                status_line="<Idle|MPos:1.000,1.000,1.000>",
                parser_state=(),
                offset_lines=(),
                errors=(),
                missing_ok_commands=(),
            ),
            ["startup"],
            SessionObservationPhase.FRESH_STABILIZATION,
            classify_session_observation(
                ("startup",),
                parse_preflight_snapshot(
                    status_line="<Idle|MPos:1.000,1.000,1.000>",
                    parser_state=(),
                    offset_lines=(),
                    errors=(),
                    missing_ok_commands=(),
                ).status,
                SessionObservationPhase.FRESH_STABILIZATION,
            ),
        )


def test_initial_preflight_cannot_be_aliased_to_a_late_phase() -> None:
    assert InitialPreflightRequest().observation_phase is SessionObservationPhase.FRESH_STABILIZATION
    with pytest.raises(TypeError, match="fresh-stabilization"):
        InitialPreflightRequest(SessionObservationPhase.STABILIZED)


def test_controller_reset_with_mpos_zero_and_empty_banner_tuple_requires_bound_observation() -> None:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET",
        "controller reset after stabilization",
        True,
    )
    with pytest.raises(TypeError, match="observation"):
        HomeOutcome(_EPOCH, HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1), snapshot, failure)
    phase = SessionObservationPhase.STABILIZED
    decision = classify_session_observation((), snapshot.status, phase)
    outcome = HomeOutcome(
        _EPOCH,
        HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1),
        snapshot,
        failure,
        (),
        phase,
        decision,
        acknowledged_steps=0,
        stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
    )
    assert outcome.decision is decision


def test_post_open_reset_snapshot_never_masquerades_as_success_or_an_unrelated_failure() -> None:
    reset_snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out", False)
    zcal = ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="bound session observation"):
        ZCalibrationOutcome(_EPOCH, zcal, 6, reset_snapshot)
    with pytest.raises(TypeError, match="bound session observation"):
        HomeZOutcome(_EPOCH, HomeZAxisRequest(), reset_snapshot)
    with pytest.raises(TypeError, match="bound session observation"):
        StreamOutcome(_EPOCH, 1, 1, reset_snapshot)
    with pytest.raises(TypeError, match="bound session observation"):
        StreamOutcome(_EPOCH, 1, 0, reset_snapshot, timeout)


@pytest.mark.parametrize("message", ("alarm\x1b", "alarm\x85", "alarm\u2028", "alarm\u2029", "alarm\x7f", "alarm\x80"))
def test_failure_message_rejects_every_unicode_control_and_separator(message: str) -> None:
    with pytest.raises(TypeError, match="one bounded line"):
        FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", message, False)


@pytest.mark.parametrize(
    "kind",
    (FluidNCFailureKind.LOST_ACK, FluidNCFailureKind.CONTROLLER_RESET, FluidNCFailureKind.SHORT_WRITE),
)
def test_intrinsically_ambiguous_failures_require_possible_side_effect_evidence(
    kind: FluidNCFailureKind,
) -> None:
    with pytest.raises(TypeError, match="side effect"):
        FluidNCFailure(kind, kind.value, "intrinsically ambiguous failure", False)


def test_contextual_prewrite_failures_preserve_false_side_effect_evidence() -> None:
    for kind in (FluidNCFailureKind.TIMEOUT, FluidNCFailureKind.TRANSPORT_LOSS):
        failure = FluidNCFailure(kind, kind.value, "failed before any write", False)
        assert failure.side_effect_may_have_occurred is False


def test_acknowledged_zcal_or_stream_failure_requires_possible_side_effect_evidence() -> None:
    false_timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out", False)
    zcal = ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="acknowledged ZCAL"):
        ZCalibrationOutcome(_EPOCH, zcal, 1, None, false_timeout)
    with pytest.raises(TypeError, match="acknowledged STREAM"):
        StreamOutcome(_EPOCH, 2, 1, None, false_timeout)
    assert StreamOutcome(_EPOCH, 2, 0, None, false_timeout).acknowledged_commands == 0


def _idle_snapshot(*, errors: tuple[str, ...] = (), missing: tuple[str, ...] = ()) -> PreflightSnapshot:
    return parse_preflight_snapshot(
        status_line="<Idle|MPos:192.000,192.000,192.000>",
        parser_state=(),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=errors,
        missing_ok_commands=missing,
    )


def _stream_request() -> StreamProgramRequest:
    source = b"G21\n"
    plan = build_send_plan(source.decode("ascii"))
    return StreamProgramRequest(
        build_validated_stream_program(source, plan.to_json(), hashlib.sha256(source).hexdigest())
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FluidNCFailure("ALARM", "ALARM", "alarm", False),  # type: ignore[arg-type]
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, 1, "alarm", False),  # type: ignore[arg-type]
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "bad", "alarm", False),
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", 1, False),  # type: ignore[arg-type]
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", "", False),
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", "\ud800", False),
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", "a" * 4097, False),
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", "é" * 3000, False),
        lambda: FluidNCFailure(FluidNCFailureKind.ALARM, "ALARM", "alarm", 1),  # type: ignore[arg-type]
    ],
)
def test_failure_contract_branch_matrix(factory: object) -> None:
    with pytest.raises(TypeError):
        factory()  # type: ignore[operator]


def test_request_scalar_and_exact_type_branch_matrix() -> None:
    with pytest.raises(TypeError, match="exact string"):
        OpenSessionRequest(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="canonical UUID"):
        OpenSessionRequest("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
    with pytest.raises(TypeError, match="exact finite"):
        ZCalibrationRequest("G54", "96", 96, 3.5, 0, 1200, 100)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="finite"):
        ZCalibrationRequest("G54", float("nan"), 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="finite"):
        ZCalibrationRequest("G54", 10**400, 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="non-negative"):
        ZCalibrationRequest("G54", -1, 96, 3.5, 0, 1200, 100)
    with pytest.raises(TypeError, match="positive"):
        ZCalibrationRequest("G54", 96, 96, 3.5, 0, 0, 100)
    with pytest.raises(TypeError, match="ValidatedStreamProgram"):
        StreamProgramRequest(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Idle proof"):
        HomeZAxisRequest(False)


def test_preflight_exact_input_failure_branches() -> None:
    snapshot = _idle_snapshot()
    phase = SessionObservationPhase.FRESH_STABILIZATION
    decision = classify_session_observation((), snapshot.status, phase)
    with pytest.raises(TypeError, match="snapshot"):
        PreflightOutcome(_EPOCH, object(), (), phase, decision)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="decision"):
        PreflightOutcome(_EPOCH, snapshot, (), phase, object())  # type: ignore[arg-type]
    reset_snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    reset_decision = classify_session_observation((), reset_snapshot.status, phase)
    reset_failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET",
        "fresh reset",
        True,
    )
    with pytest.raises(TypeError, match="expected unhomed"):
        PreflightOutcome(_EPOCH, reset_snapshot, (), phase, reset_decision, reset_failure)
    stabilized = SessionObservationPhase.STABILIZED
    stabilized_evidence = ("watchdog reset",)
    stabilized_decision = classify_session_observation(
        stabilized_evidence,
        reset_snapshot.status,
        stabilized,
    )
    with pytest.raises(TypeError, match="exact controller-reset"):
        PreflightOutcome(
            _EPOCH,
            reset_snapshot,
            stabilized_evidence,
            stabilized,
            stabilized_decision,
        )


def test_operation_specific_outcome_branch_matrix() -> None:
    zcal = ZCalibrationRequest("G54", 96, 96, 3.5, 0, 1200, 100)
    zconfirm = ZConfirmationRequest((96, 96, 3.5), 3.5, 400, 0.1)
    timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out", False)
    side_timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out after write", True)
    idle = _idle_snapshot()

    with pytest.raises(TypeError, match="HOME outcome request"):
        HomeOutcome(_EPOCH, object(), None, timeout)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ZCAL outcome request"):
        ZCalibrationOutcome(_EPOCH, object(), 0, None, timeout)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cannot exceed"):
        ZCalibrationOutcome(_EPOCH, zcal, 7, None, timeout)
    ZCalibrationOutcome(
        _EPOCH,
        zcal,
        1,
        idle,
        side_timeout,
        stage=ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED,
    )
    ZCalibrationOutcome(
        _EPOCH,
        zcal,
        6,
        idle,
        side_timeout,
        stage=ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED,
    )
    for acknowledged, stage, failure in (
        (0, ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED, timeout),
        (1, ZCalibrationOutcomeStage.POSTCONDITION, side_timeout),
        (6, ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED, None),
        (1, object(), side_timeout),
    ):
        with pytest.raises(TypeError, match="ZCAL outcome stage"):
            ZCalibrationOutcome(
                _EPOCH,
                zcal,
                acknowledged,
                idle,
                failure,
                stage=stage,  # type: ignore[arg-type]
            )
    with pytest.raises(TypeError, match="six fixed"):
        ZCalibrationOutcome(_EPOCH, zcal, 5, None)
    with pytest.raises(TypeError, match="ZCONFIRM outcome request"):
        ZConfirmationOutcome(_EPOCH, object(), None, timeout)  # type: ignore[arg-type]
    ZConfirmationOutcome(
        _EPOCH,
        zconfirm,
        None,
        timeout,
        acknowledged_steps=0,
        stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
    )
    HomeZOutcome(_EPOCH, HomeZAxisRequest(), None, timeout)
    with pytest.raises(TypeError, match="canonical SHA-256"):
        StreamProgressUpdate(_EPOCH, 1, 1, "BAD")
    with pytest.raises(TypeError, match="at least"):
        StreamProgressUpdate(_EPOCH, 0, 1, "a" * 64)
    with pytest.raises(TypeError, match="cannot exceed"):
        StreamOutcome(_EPOCH, 1, 2, None, timeout)
    with pytest.raises(TypeError, match="every ACK"):
        StreamOutcome(_EPOCH, 2, 1, idle)
    StreamOutcome(_EPOCH, 2, 1, None, side_timeout)


def test_single_command_outcome_stage_and_ack_cross_product_is_closed() -> None:
    request = HomeAllAxesRequest((192, 192, 192), 0.1)
    prewrite = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out before ACK", False)
    postwrite = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out after ACK", True)
    wrong_pose = parse_preflight_snapshot(
        status_line="<Idle|MPos:191.000,192.000,192.000>",
        parser_state=(),
        offset_lines=("[G54:96.000,96.000,188.500]",),
        errors=(),
        missing_ok_commands=(),
    )
    proof_failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_ERROR,
        "POST_HOME_PROOF_FAILED",
        "post-HOME proof failed",
        True,
    )
    assert (
        HomeOutcome(
            _EPOCH,
            request,
            None,
            prewrite,
            acknowledged_steps=0,
            stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
        ).acknowledged_steps
        == 0
    )
    assert (
        HomeOutcome(
            _EPOCH,
            request,
            None,
            postwrite,
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
        ).acknowledged_steps
        == 1
    )
    assert (
        HomeOutcome(
            _EPOCH,
            request,
            wrong_pose,
            proof_failure,
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
        ).stage
        is SingleCommandOutcomeStage.IDLE_OBSERVED
    )

    invalid = (
        (0, SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED, prewrite),
        (1, SingleCommandOutcomeStage.DISPATCH_ATTEMPTED, postwrite),
        (2, SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED, postwrite),
        (1, object(), postwrite),
        (1, SingleCommandOutcomeStage.POSTCONDITION_PROVEN, postwrite),
        (1, SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED, None),
        (1, SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED, proof_failure),
        (1, SingleCommandOutcomeStage.IDLE_OBSERVED, postwrite),
    )
    for acknowledged, stage, failure in invalid:
        with pytest.raises(TypeError):
            HomeOutcome(
                _EPOCH,
                request,
                wrong_pose,
                failure,
                acknowledged_steps=acknowledged,
                stage=stage,  # type: ignore[arg-type]
            )
    with pytest.raises(TypeError, match="requires typed failure"):
        HomeOutcome(
            _EPOCH,
            request,
            _idle_snapshot(),
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
        )
    with pytest.raises(TypeError, match="possible side-effect"):
        HomeOutcome(
            _EPOCH,
            request,
            _idle_snapshot(),
            prewrite,
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
        )


@pytest.mark.parametrize(
    ("action", "proof_code", "stage"),
    (
        ("HOME", "POST_ZCAL_PROOF_FAILED", SingleCommandOutcomeStage.IDLE_OBSERVED),
        ("HOME", "POST_ZCONFIRM_PROOF_FAILED", SingleCommandOutcomeStage.IDLE_OBSERVED),
        ("HOME", "POST_HOME_PROOF_FAILED", SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED),
        ("ZCAL", "POST_HOME_PROOF_FAILED", ZCalibrationOutcomeStage.POSTCONDITION),
        ("ZCAL", "POST_ZCONFIRM_PROOF_FAILED", ZCalibrationOutcomeStage.POSTCONDITION),
        ("ZCAL", "POST_ZCAL_PROOF_FAILED", ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED),
        ("ZCONFIRM", "POST_HOME_PROOF_FAILED", SingleCommandOutcomeStage.IDLE_OBSERVED),
        ("ZCONFIRM", "POST_ZCAL_PROOF_FAILED", SingleCommandOutcomeStage.IDLE_OBSERVED),
        ("ZCONFIRM", "POST_ZCONFIRM_PROOF_FAILED", SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED),
    ),
)
def test_reserved_motion_proof_codes_require_the_exact_action_and_stage(
    action: str,
    proof_code: str,
    stage: SingleCommandOutcomeStage | ZCalibrationOutcomeStage,
) -> None:
    failure = FluidNCFailure(FluidNCFailureKind.CONTROLLER_ERROR, proof_code, "reserved proof failure", True)
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:191.000,192.000,192.000|WPos:95.000,96.000,96.000|WCO:96.000,96.000,96.000>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=("[G54:96.000,96.000,96.000]",),
        errors=(),
        missing_ok_commands=(),
    )
    with pytest.raises(TypeError, match="proof failure"):
        if action == "HOME":
            HomeOutcome(
                _EPOCH,
                HomeAllAxesRequest((192.0, 192.0, 192.0), 0.5),
                snapshot,
                failure,
                acknowledged_steps=1,
                stage=stage,  # type: ignore[arg-type]
            )
        elif action == "ZCAL":
            ZCalibrationOutcome(
                _EPOCH,
                ZCalibrationRequest("G54", 96.0, 96.0, 3.5, 0.0, 1200.0, 100.0),
                6,
                snapshot,
                failure,
                stage=stage,  # type: ignore[arg-type]
            )
        else:
            ZConfirmationOutcome(
                _EPOCH,
                ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.5),
                snapshot,
                failure,
                acknowledged_steps=1,
                stage=stage,  # type: ignore[arg-type]
            )


def test_exact_idle_proof_rejects_every_independent_contradiction() -> None:
    timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out", False)
    run = parse_preflight_snapshot(
        status_line="<Run|MPos:192.000,192.000,192.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    inconsistent_status = ControllerStatus(
        FluidNCState.IDLE,
        "Run",
        (192.0, 192.0, 192.0),
        None,
        None,
        "<Run|MPos:192.000,192.000,192.000>",
    )
    inconsistent = PreflightSnapshot(inconsistent_status, (), (), (), (), ())
    for snapshot in (
        run,
        inconsistent,
        _idle_snapshot(errors=("error:1",)),
        _idle_snapshot(missing=("?",)),
    ):
        with pytest.raises(TypeError, match="Idle proof"):
            HomeZOutcome(_EPOCH, HomeZAxisRequest(), snapshot)
    HomeZOutcome(_EPOCH, HomeZAxisRequest(), run, timeout)


def test_observation_container_and_binding_branch_matrix() -> None:
    request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.1)
    timeout = FluidNCFailure(FluidNCFailureKind.TIMEOUT, "TIMEOUT", "timed out", False)
    reset = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_RESET,
        "CONTROLLER_RESET",
        "late reset",
        True,
    )
    snapshot = _idle_snapshot()
    phase = SessionObservationPhase.STABILIZED
    ready = classify_session_observation((), snapshot.status, phase)
    with pytest.raises(TypeError, match="requires exact"):
        HomeOutcome(_EPOCH, request, None, timeout, (), phase, ready)
    with pytest.raises(TypeError, match="requires exact"):
        HomeOutcome(_EPOCH, request, snapshot, timeout, [], phase, ready)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="requires exact"):
        HomeOutcome(_EPOCH, request, snapshot, timeout, (), "STABILIZED", ready)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="requires exact"):
        HomeOutcome(_EPOCH, request, snapshot, timeout, (), phase, object())  # type: ignore[arg-type]
    reset_snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    contradictory = classify_session_observation((), reset_snapshot.status, phase)
    with pytest.raises(TypeError, match="contradicts"):
        HomeOutcome(_EPOCH, request, snapshot, timeout, (), phase, contradictory)
    with pytest.raises(TypeError, match="bound controller-reset"):
        HomeOutcome(_EPOCH, request, snapshot, reset, (), phase, ready)


@pytest.mark.parametrize(
    "evidence",
    [
        ["line"],
        tuple("line" for _ in range(257)),
        (1,),
        ("",),
        ("bad\nline",),
        ("\ud800",),
        tuple("x" * 300 for _ in range(256)),
    ],
)
def test_observation_evidence_is_exact_bounded_utf8(evidence: object) -> None:
    snapshot = _idle_snapshot()
    with pytest.raises(TypeError, match="evidence"):
        PreflightOutcome(
            _EPOCH,
            snapshot,
            evidence,  # type: ignore[arg-type]
            SessionObservationPhase.FRESH_STABILIZATION,
            classify_session_observation((), snapshot.status, SessionObservationPhase.FRESH_STABILIZATION),
        )
