from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.application.machine.commands import MachineCommandMode, StreamMachineCommand
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachinePhase,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.domain.machine.progress import MachineProgress
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    HomeOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    SingleCommandOutcomeStage,
)
from tests.integration.hardware import test_stream_failures as stream_failures
from tests.integration.service import test_machine_commands as machine_commands
from tests.integration.service import test_machine_coordinator as machine_coordinator
from tests.unit.adapters.hardware import test_serial_fluidnc as serial_fluidnc
from tests.unit.application.machine import test_admission as admission
from tests.unit.application.machine import test_home as home
from tests.unit.application.machine import test_home_z as home_z
from tests.unit.application.machine import test_recovery as recovery
from tests.unit.application.machine import test_stream as stream
from tests.unit.application.machine import test_zcal as zcal

C14_STREAM_CHECKPOINTS = (0, 1, 455)


def test_fake_operation_surface_has_no_escape_hatch() -> None:
    assert {item.value for item in FakeFluidNCOperation}.isdisjoint(
        {"RAW", "WRITE", "JOG", "UNLOCK", "OFFSET", "RESUME"}
    )


def test_c14_stream_matrix_includes_the_exact_final_command_not_a_sample() -> None:
    assert C14_STREAM_CHECKPOINTS == (0, 1, 455)


def test_frozen_progress_authority_rejects_each_remaining_hostile_branch() -> None:
    zero = MachineProgress(1, "execution", 0, 0, 0, 0, 0, 100.0, None, None, admission.NOW)
    assert zero.percent == 100.0
    with pytest.raises(DrawingMachineError, match="schema version"):
        MachineProgress(2, "execution", 1, 4, 2, 2, 0, 50.0, 3, "G1 X1", admission.NOW)
    with pytest.raises(DrawingMachineError, match="must be numeric"):
        MachineProgress(1, "execution", 1, 4, 2, 2, 0, "50", 3, "G1 X1", admission.NOW)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="exactly reflect"):
        MachineProgress(1, "execution", 1, 4, 2, 2, 0, 49.0, 3, "G1 X1", admission.NOW)
    with pytest.raises(DrawingMachineError, match="failure must be exact"):
        MachineProgress(1, "execution", 1, 4, 2, 2, 1, 50.0, 3, "G1 X1", admission.NOW, object())  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="failure evidence must match"):
        MachineProgress(1, "execution", 1, 4, 2, 2, 1, 50.0, 3, "G1 X1", admission.NOW)


def _case(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    return path


def test_forbidden_order_authority_replay_expiry_and_cross_action_are_adapter_zero(tmp_path: Path) -> None:
    home.test_action_before_prepare_and_out_of_order_never_dispatch(_case(tmp_path, "order"))
    home.test_unconfigured_or_automation_consumer_cannot_dispatch(_case(tmp_path, "principal"))
    home.test_motion_request_replay_is_durable_and_argument_conflict_is_closed(_case(tmp_path, "replay"))
    zcal.test_cross_action_reuse_is_rejected_before_second_adapter_call(_case(tmp_path, "cross-action"))
    zcal.test_expired_motion_approval_never_enters_in_progress_or_dispatches(_case(tmp_path, "expiry"))


def test_prepare_rejects_reset_tamper_conflict_cancel_race_and_second_owner_without_motion(tmp_path: Path) -> None:
    admission.test_late_reset_initial_preflight_is_durable_recovery_never_home_eligible(_case(tmp_path, "late-reset"))
    admission.test_static_send_plan_and_readiness_tamper_are_recomputed_exactly(_case(tmp_path, "tamper"))
    admission.test_request_id_replay_is_durable_and_conflict_fails_closed(_case(tmp_path, "request-conflict"))
    admission.test_alarm_preflight_closes_session_and_atomically_marks_machine_and_job_recovery(
        _case(tmp_path, "alarm")
    )
    admission.test_second_prepare_owner_conflict_leaves_only_first_durable_execution_and_zero_open(
        _case(tmp_path, "two-owners")
    )
    admission.test_cancel_committed_after_artifact_read_wins_before_prepare_reservation(_case(tmp_path, "cancel-race"))
    admission.test_frozen_job_revision_profile_digest_and_version_drift_fail_before_open(
        _case(tmp_path, "stale-revision"), admission._command(job_revision=8)
    )
    admission.test_trusted_runtime_authority_drift_fails_before_open(
        _case(tmp_path, "config-drift"), admission._authority(machine_profile="replacement")
    )
    admission.test_descriptor_identity_replacement_fails_before_reservation_or_open(
        _case(tmp_path, "artifact-replaced")
    )
    admission.test_wrong_epoch_preflight_is_never_accepted_and_close_uncertainty_is_retained(
        _case(tmp_path, "wrong-session-epoch")
    )


def test_equal_roles_and_concurrent_challenge_consumers_fail_closed(tmp_path: Path) -> None:
    machine_commands.test_equal_roles_are_rejected_before_handler_registration()
    machine_coordinator.test_real_mailbox_two_consumers_of_one_challenge_dispatch_home_once(
        _case(tmp_path, "concurrent-consumers")
    )


def test_every_stream_transport_failure_and_partial_progress_is_irreversible(tmp_path: Path) -> None:
    typed_failures = (
        (FluidNCFailureKind.TIMEOUT, "STREAM_ACK_TIMEOUT"),
        (FluidNCFailureKind.LOST_ACK, "STREAM_ACK_LOST"),
        (FluidNCFailureKind.TRANSPORT_LOSS, "STREAM_DISCONNECTED"),
        (FluidNCFailureKind.CONTROLLER_RESET, "STREAM_CONTROLLER_RESET"),
    )
    for index, (kind, code) in enumerate(typed_failures):
        stream_failures.test_every_typed_stream_failure_stops_at_zero_ack_and_is_release_only(
            _case(tmp_path, f"typed-{index}"), kind, code
        )
    program = stream._program()
    assert len(program.commands) == C14_STREAM_CHECKPOINTS[-1]
    for acknowledged in C14_STREAM_CHECKPOINTS[:2]:
        stream.test_partial_stream_failure_is_release_only_and_never_retries(
            _case(tmp_path, f"partial-{acknowledged}"), acknowledged
        )
    final_root = _case(tmp_path, "partial-final")
    epoch = admission._uuid(2)
    failure = FluidNCFailure(
        FluidNCFailureKind.TIMEOUT,
        "STREAM_IDLE_TIMEOUT",
        "final Idle proof timed out",
        True,
    )
    outcome = stream.StreamOutcome(epoch, len(program.commands), len(program.commands), None, failure)
    chain, repository, execution_id = stream._prepare_to_stream(
        final_root,
        stream._stream_script(epoch, outcome, progress_count=len(program.commands)),
    )
    try:
        result = stream._execute_stream(chain, execution_id, "c14-stream-final")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
        latest = repository.list_progress(execution_id)[-1]
        assert latest.total_lines == latest.acknowledged_lines == len(program.commands) == 455
        assert latest.last_source_line == program.commands[-1].source_line
        assert latest.last_command == program.commands[-1].command
        assert latest.failure is not None and latest.failure.code == "STREAM_IDLE_TIMEOUT"
    finally:
        repository.close()
    stream.test_stream_milestone_is_sqlite_irreversible_after_partial_write(_case(tmp_path, "irreversible"))


def test_fake_serial_matrix_covers_every_timeout_decode_write_alarm_and_close_error() -> None:
    serial_fluidnc.test_short_write_flush_lost_ack_and_first_controller_error_stop()
    serial_fluidnc.test_close_failure_is_typed_and_does_not_reopen()
    serial_fluidnc.test_startup_is_bounded_and_sanitizes_decode_transport_and_deadline_failures()
    serial_fluidnc.test_eof_timeout_write_flush_alarm_and_decode_failures_are_typed_after_write()
    serial_fluidnc.test_wait_for_idle_stops_on_alarm_reset_and_absolute_deadline_without_parser_queries()


def test_audit_commit_home_z_and_recovery_failures_keep_the_closed_graph(tmp_path: Path) -> None:
    zcal.test_commit_failure_after_zcal_side_effect_enters_recovery_without_adapter_retry(
        _case(tmp_path, "zcal-commit")
    )
    stream_failures.test_final_completed_outcome_commit_failure_is_post_stream_recovery(
        _case(tmp_path, "stream-commit")
    )
    stream_failures.test_confirmed_stream_awaiting_home_z_commit_failure_is_closed_post_stream_recovery(
        _case(tmp_path, "home-z-commit")
    )
    home_z.test_home_z_failure_retains_confirmed_stream_and_post_stream_recovery(_case(tmp_path, "home-z-failure"))


@pytest.mark.parametrize("state", ["Hold", "Door"])
def test_non_idle_home_postcondition_never_advances_or_retries(tmp_path: Path, state: str) -> None:
    epoch = admission._uuid(2)
    snapshot = home._snapshot(
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
        state=state,
    )
    failure = FluidNCFailure(FluidNCFailureKind.CONTROLLER_ERROR, f"HOME_{state.upper()}", state, True)
    failed = HomeOutcome(
        epoch,
        home._home_request(),
        snapshot,
        failure,
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
    )
    chain, repository, factory, execution_id = home._chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, home._home_request(), failed),
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(epoch),
                CloseOutcome(epoch),
            ),
        ),
    )
    try:
        result = home._run_action(chain, repository, home.HomeMachineCommand, execution_id, f"home-{state}")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    finally:
        repository.close()


def test_every_recovery_intent_has_only_its_authorized_successor_or_release(tmp_path: Path) -> None:
    recovery.test_release_consumes_operator_challenge_without_artifact_read_or_adapter_open(_case(tmp_path, "release"))
    recovery.test_automation_cannot_consume_its_own_recovery_challenge(_case(tmp_path, "automation"))
    recovery.test_restart_successor_transfers_atomically_then_opens_only_after_drive(_case(tmp_path, "restart"))
    recovery.test_invalid_trusted_config_blocks_successor_but_keeps_nonmotion_release_available(
        _case(tmp_path, "invalid-artifact-release")
    )
    recovery.test_post_stream_safe_home_transfers_only_confirmed_milestone_successor(_case(tmp_path, "safe-home"))
    wrong = (
        (RecoveryIntent.PRE_STREAM_RESTART, RecoveryDisposition.SAFE_HOME),
        (RecoveryIntent.POST_STREAM_SAFE_HOME, RecoveryDisposition.RESTART_SEQUENCE),
        (RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY, RecoveryDisposition.RESTART_SEQUENCE),
        (RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY, RecoveryDisposition.SAFE_HOME),
    )
    for index, (intent, disposition) in enumerate(wrong):
        recovery.test_disposition_specific_graph_rejects_every_wrong_successor(
            _case(tmp_path, f"wrong-{index}"), intent, disposition
        )
    home.test_post_stream_safe_home_successor_can_only_home_then_complete(_case(tmp_path, "home-only"))


def test_ambiguous_stream_release_is_nonmotion_artifact_independent_and_idempotent(tmp_path: Path) -> None:
    chain, repository, factory, reader = recovery._chain(
        tmp_path,
        RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
        authority=recovery._authority(machine_digest="f" * 64),
    )
    request = recovery._recover(
        request_id="ambiguous-release-request",
        requester=recovery.AUTOMATION,
        disposition=RecoveryDisposition.RELEASE,
        mode=MachineCommandMode.REQUEST,
    )
    try:
        requested = cast(MachineCommandResult, chain.admit_action(request))
        execute = recovery._recover(
            request_id="ambiguous-release-execute",
            requester=recovery.OPERATOR,
            disposition=RecoveryDisposition.RELEASE,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=home._challenge_id(requested),
        )
        released = cast(MachineCommandResult, chain.admit_action(execute))
        assert released.response["released"] is True
        owner = repository.get_execution("execution-recovery")
        assert owner is not None and owner.retirement is not None
        assert repository.get_active_execution() is None
        replay_request = cast(
            MachineCommandResult,
            chain.admit_action(
                replace(
                    request,
                    requester=AuthenticatedMachinePrincipal(
                        recovery.AUTOMATION.uid,
                        recovery.AUTOMATION.gid,
                        recovery.AUTOMATION.pid + 1,
                    ),
                )
            ),
        )
        replay_execute = cast(
            MachineCommandResult,
            chain.admit_action(
                replace(
                    execute,
                    requester=AuthenticatedMachinePrincipal(
                        recovery.OPERATOR.uid,
                        recovery.OPERATOR.gid,
                        recovery.OPERATOR.pid + 1,
                    ),
                )
            ),
        )
        assert replay_request.deduplicated is True and replay_execute.deduplicated is True
        with pytest.raises(DrawingMachineError):
            chain.admit_action(
                StreamMachineCommand(
                    "ambiguous-second-stream",
                    recovery.AUTOMATION,
                    "execution-recovery",
                    MachineCommandMode.REQUEST,
                    None,
                )
            )
        assert reader.calls == 0
        assert factory.opens == 0
    finally:
        repository.close()


def test_pre_stream_restart_successor_repeats_full_sequence_before_one_stream(tmp_path: Path) -> None:
    successor_epoch = admission._uuid(4)
    program = stream._program()
    final = home._snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    stream_outcome = stream.StreamOutcome(
        successor_epoch,
        len(program.commands),
        len(program.commands),
        final,
    )
    successor_factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                admission._preflight(successor_epoch),
            ),
            *stream._stream_script(successor_epoch, stream_outcome),
        ),
    )
    chain, repository, factory, _reader = recovery._chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        factories=(successor_factory,),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                recovery._recover(
                    request_id="restart-request",
                    requester=recovery.OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            recovery._recover(
                request_id="restart-execute",
                requester=recovery.OPERATOR,
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=home._challenge_id(requested),
            )
        )
        successor = chain.drive_admitted_action(admitted)
        home._run_action(chain, repository, home.HomeMachineCommand, successor.execution_id, "restart-home")
        home._run_action(chain, repository, zcal.ZCalMachineCommand, successor.execution_id, "restart-zcal")
        home._run_action(
            chain,
            repository,
            home.ZConfirmMachineCommand,
            successor.execution_id,
            "restart-zconfirm",
        )
        completed = stream._execute_stream(chain, successor.execution_id, "restart-stream")
        assert completed.phase is MachinePhase.COMPLETED
        assert factory.opens == 1
        assert successor_factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.HOME_ALL_AXES,
            FakeFluidNCOperation.Z_CALIBRATION,
            FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
            FakeFluidNCOperation.STREAM_PROGRAM,
            FakeFluidNCOperation.CLOSE,
        )
        assert successor_factory.started_operations.count(FakeFluidNCOperation.STREAM_PROGRAM) == 1
        successor_factory.assert_script_consumed()
    finally:
        repository.close()


def test_restart_and_shutdown_freeze_each_irreversible_phase_before_adapter_io(tmp_path: Path) -> None:
    startup_cases = (
        (MachinePhase.PREPARING_SESSION, StreamMilestone.NOT_STARTED, RecoveryIntent.PRE_STREAM_RESTART),
        (
            MachinePhase.STREAMING,
            StreamMilestone.FIRST_WRITE_POSSIBLE,
            RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
        ),
        (
            MachinePhase.AWAITING_HOME_Z_APPROVAL,
            StreamMilestone.STREAM_CONFIRMED,
            RecoveryIntent.POST_STREAM_SAFE_HOME,
        ),
    )
    for index, (phase, milestone, intent) in enumerate(startup_cases):
        recovery.test_c12_startup_reconciles_exact_milestone_without_adapter_open(
            _case(tmp_path, f"restart-{index}"), phase, milestone, intent
        )
    for phase in (
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachinePhase.AWAITING_STREAM_APPROVAL,
    ):
        recovery.test_c12_shutdown_persists_recovery_before_closing_owned_session_once(
            _case(tmp_path, f"shutdown-{phase.value}"), phase
        )
