from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    MachineCommandMode,
    PrepareMachineCommand,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult, MachineExecutionChain
from drawingmachine.domain.fluidnc.status import PreflightSnapshot, parse_preflight_snapshot
from drawingmachine.domain.machine import (
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    MachineAction,
    MachineMotionEvidenceKind,
    MachineMotionStep,
    MachinePhase,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    HomeAllAxesRequest,
    HomeOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    SingleCommandOutcomeStage,
    ZCalibrationOutcome,
    ZCalibrationOutcomeStage,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)
from drawingmachine.ports.machine_repository import MachineRequestKey
from tests.unit.application.machine.test_admission import (
    AUTOMATION,
    NOW,
    Reader,
    _authority,
    _config,
    _identities,
    _job,
    _preflight,
    _ready_snapshot,
    _seed,
    _uuid,
)

OPERATOR = AuthenticatedMachinePrincipal(2200, 2200, 41001)


class _MotionProbeSession:
    def __init__(self, epoch: str, outcome: object | BaseException) -> None:
        self.epoch = epoch
        self.outcome = outcome
        self.home_calls = 0
        self.close_calls = 0

    def home_all_axes(self, request: HomeAllAxesRequest) -> object:
        assert request == _home_request()
        self.home_calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        return CloseOutcome(self.epoch)


def _snapshot(
    *,
    mpos: tuple[float, float, float],
    wpos: tuple[float, float, float],
    wco: tuple[float, float, float],
    g54: tuple[float, float, float],
    state: str = "Idle",
) -> PreflightSnapshot:
    def vector(value: tuple[float, float, float]) -> str:
        return ",".join(f"{item:.3f}" for item in value)

    return parse_preflight_snapshot(
        status_line=f"<{state}|MPos:{vector(mpos)}|WPos:{vector(wpos)}|WCO:{vector(wco)}>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(f"[G54:{vector(g54)}]",),
        errors=(),
        missing_ok_commands=(),
    )


def _home_request() -> HomeAllAxesRequest:
    return HomeAllAxesRequest((192.0, 192.0, 192.0), 0.5)


def _home_outcome(epoch: str, *, snapshot: PreflightSnapshot | None = None) -> HomeOutcome:
    proof = snapshot or _snapshot(
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    return HomeOutcome(epoch, _home_request(), proof)


def _mpos_only_snapshot(
    *,
    mpos: tuple[float, float, float],
    g54: tuple[float, float, float],
    state: str = "Idle",
) -> PreflightSnapshot:
    # Defect #17: the real controller's status report carries MPos only -- never WPos or WCO
    # alongside it. Only G54 (from `$#`) is separately observed.
    def vector(value: tuple[float, float, float]) -> str:
        return ",".join(f"{item:.3f}" for item in value)

    return parse_preflight_snapshot(
        status_line=f"<{state}|MPos:{vector(mpos)}>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(f"[G54:{vector(g54)}]",),
        errors=(),
        missing_ok_commands=(),
    )


def _wpos_only_snapshot(
    *,
    wpos: tuple[float, float, float],
    wco: tuple[float, float, float],
    g54: tuple[float, float, float],
    state: str = "Idle",
) -> PreflightSnapshot:
    # WPos + WCO present, MPos absent; G54 deliberately differs from WCO to prove the reported
    # WCO (not the G54 fallback) is used when WCO is directly present.
    def vector(value: tuple[float, float, float]) -> str:
        return ",".join(f"{item:.3f}" for item in value)

    return parse_preflight_snapshot(
        status_line=f"<{state}|WPos:{vector(wpos)}|WCO:{vector(wco)}>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(f"[G54:{vector(g54)}]",),
        errors=(),
        missing_ok_commands=(),
    )


def _home_outcome_mpos_only(epoch: str) -> HomeOutcome:
    return HomeOutcome(
        epoch,
        _home_request(),
        _mpos_only_snapshot(mpos=(192.0, 192.0, 192.0), g54=(50.0, 60.0, 70.0)),
    )


def _zcal_outcome_wpos_only(epoch: str) -> ZCalibrationOutcome:
    return ZCalibrationOutcome(
        epoch,
        _zcal_request(),
        6,
        _wpos_only_snapshot(
            wpos=(96.0, 96.0, 3.5),
            wco=(96.0, 96.0, 96.0),
            g54=(1.0, 2.0, 3.0),
        ),
    )


def _zcal_request() -> ZCalibrationRequest:
    return ZCalibrationRequest("G54", 96.0, 96.0, 3.5, 0.0, 1200.0, 100.0)


def _zcal_outcome(epoch: str) -> ZCalibrationOutcome:
    return ZCalibrationOutcome(
        epoch,
        _zcal_request(),
        6,
        _snapshot(
            mpos=(192.0, 192.0, 96.0),
            wpos=(96.0, 96.0, 0.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
    )


def _zconfirm_request() -> ZConfirmationRequest:
    return ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.5)


def _zconfirm_outcome(epoch: str) -> ZConfirmationOutcome:
    return ZConfirmationOutcome(
        epoch,
        _zconfirm_request(),
        _snapshot(
            mpos=(192.0, 192.0, 99.5),
            wpos=(96.0, 96.0, 3.5),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
    )


def _chain(
    tmp_path: Path,
    steps: tuple[FakeFluidNCStep, ...],
    *,
    identities: Iterator[str] | None = None,
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, StrictFakeFluidNCFactory, str]:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    values = identities or _identities()
    epoch = _uuid(2)
    factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(epoch),
        (FakeFluidNCStep(FakeFluidNCOperation.INITIAL_PREFLIGHT, InitialPreflightRequest(), _preflight(epoch)), *steps),
    )
    chain = MachineExecutionChain(
        repository,
        Reader(),
        factory,
        _config(),
        _authority(),
        identity_factory=lambda: next(values),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    prepared = chain.admit_action(PrepareMachineCommand("prepare-c10", AUTOMATION, "job-c9", 7))
    execution = chain.drive_admitted_action(prepared)
    assert execution.phase is MachinePhase.AWAITING_HOME_APPROVAL
    return chain, repository, factory, execution.execution_id


def _challenge_id(result: MachineCommandResult) -> str:
    challenge = cast(dict[str, object], result.response["challenge"])
    return cast(str, challenge["challenge_id"])


def _machine_audits(repository: SQLiteMachineRepository, request_id: str) -> list[dict[str, object]]:
    rows = (
        repository._get_connection()
        .execute(
            "SELECT payload_json FROM audit_events WHERE request_id = ? ORDER BY rowid",
            (request_id,),
        )
        .fetchall()
    )
    payloads = [cast(dict[str, object], json.loads(str(row[0]))) for row in rows]
    return [payload for payload in payloads if payload.get("schema_version") == 2]


def _assert_consumed_approval_audit_chain(
    repository: SQLiteMachineRepository,
    request_id: str,
    challenge_id: str,
    *,
    final_decision: str,
    extra_decisions: tuple[str, ...] = (),
) -> None:
    audits = _machine_audits(repository, request_id)
    assert [audit["decision"] for audit in audits] == ["ADMITTED", final_decision, *extra_decisions]
    authority = {
        (
            audit["approval_challenge_id"],
            audit["challenge_issued_at"],
            audit["approved_at"],
            audit["issuer_pid"],
            audit["consumer_pid"],
        )
        for audit in audits
    }
    assert len(authority) == 1
    approval_id, issued_at, approved_at, issuer_pid, consumer_pid = authority.pop()
    challenge = repository.get_challenge(challenge_id)
    assert challenge is not None and challenge.consumer is not None
    assert (
        approval_id,
        issued_at,
        approved_at,
        issuer_pid,
        consumer_pid,
    ) == (
        challenge.challenge_id,
        challenge.issued_at.isoformat(),
        challenge.status_changed_at.isoformat(),  # type: ignore[union-attr]
        challenge.requester.pid,
        challenge.consumer.pid,
    )


def _run_action(
    chain: MachineExecutionChain,
    repository: SQLiteMachineRepository,
    command_type: type[HomeMachineCommand] | type[ZCalMachineCommand] | type[ZConfirmMachineCommand],
    execution_id: str,
    request_id: str,
) -> object:
    requested = cast(
        MachineCommandResult,
        chain.admit_action(
            command_type(f"{request_id}-request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
        ),
    )
    challenge_id = _challenge_id(requested)
    admitted = chain.admit_action(
        command_type(
            f"{request_id}-execute",
            AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 999),
            execution_id,
            MachineCommandMode.EXECUTE,
            challenge_id,
        )
    )
    in_progress = repository.get_execution(execution_id)
    assert in_progress is not None
    assert (
        in_progress.phase
        is {
            HomeMachineCommand: MachinePhase.HOMING,
            ZCalMachineCommand: MachinePhase.Z_CALIBRATING,
            ZConfirmMachineCommand: MachinePhase.Z_CONFIRMING,
        }[command_type]
    )
    return chain.drive_admitted_action(admitted)


def test_home_command_is_closed_and_separately_approved() -> None:
    command = HomeMachineCommand(
        "home-request",
        AUTOMATION,
        "execution-c10",
        MachineCommandMode.REQUEST,
        None,
    )
    assert command.mode is MachineCommandMode.REQUEST
    with pytest.raises(DrawingMachineError):
        HomeMachineCommand("bad", AUTOMATION, "execution", MachineCommandMode.EXECUTE, None)


def test_home_challenge_commits_before_one_typed_home_and_proves_postcondition(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        result = _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        assert result.phase is MachinePhase.AWAITING_ZCAL_APPROVAL  # type: ignore[attr-defined]
        assert factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.HOME_ALL_AXES,
        )
        consumed = (
            repository._get_connection()
            .execute("SELECT status, consumer_uid, consumer_gid, consumer_pid FROM machine_approval_challenges")
            .fetchall()
        )
        assert consumed == [("CONSUMED", 2200, 2200, 42000)]
        decisions = [
            cast(dict[str, object], event.to_json())["phase"] for event in repository.list_events(execution_id)
        ]
        assert decisions[-2:] == [MachinePhase.HOMING.value, MachinePhase.AWAITING_ZCAL_APPROVAL.value]
        challenge_id = cast(
            str,
            repository._get_connection()
            .execute("SELECT challenge_id FROM machine_approval_challenges WHERE action = 'HOME'")
            .fetchone()[0],
        )
        _assert_consumed_approval_audit_chain(
            repository,
            "home-execute",
            challenge_id,
            final_decision="CONFIRMED",
        )
    finally:
        repository.close()


def test_home_postcondition_derives_wpos_and_wco_from_mpos_only_status_with_g54(tmp_path: Path) -> None:
    # Defect #17: `_motion_session_snapshot` used to require status.mpos, status.wpos, AND
    # status.wco all non-None -- but real Grbl/FluidNC status reports never carry WPos alongside
    # MPos. With MPos + G54 only, wpos must be derived as mpos - g54 and wco must fall back to g54.
    epoch = _uuid(2)
    chain, repository, _factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome_mpos_only(epoch)),),
    )
    try:
        result = _run_action(chain, repository, HomeMachineCommand, execution_id, "home-mpos-only")
        assert result.phase is MachinePhase.AWAITING_ZCAL_APPROVAL  # type: ignore[attr-defined]
        evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
        assert evidence.mpos == (192.0, 192.0, 192.0)
        assert evidence.wco == (50.0, 60.0, 70.0)
        assert evidence.wpos == (142.0, 132.0, 122.0)
        assert evidence.g54 == (50.0, 60.0, 70.0)
    finally:
        repository.close()


def test_zcal_postcondition_derives_mpos_from_wpos_and_wco_when_mpos_absent(tmp_path: Path) -> None:
    # Defect #17: with WPos + WCO present but MPos absent, mpos must be derived as wpos + wco.
    # G54 is deliberately set to a different value than WCO to prove the reported WCO wins over
    # the G54 fallback when WCO is directly present in the report.
    epoch = _uuid(2)
    chain, repository, _factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome_wpos_only(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-for-zcal-wpos-only")
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-wpos-only")
        assert result.phase is MachinePhase.AWAITING_ZCONFIRM_APPROVAL  # type: ignore[attr-defined]
        evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
        assert evidence.mpos == (192.0, 192.0, 99.5)
        assert evidence.wpos == (96.0, 96.0, 3.5)
        assert evidence.wco == (96.0, 96.0, 96.0)
    finally:
        repository.close()


def test_action_before_prepare_and_out_of_order_never_dispatch(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                HomeMachineCommand("unknown", AUTOMATION, "not-prepared", MachineCommandMode.REQUEST, None)
            )
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                ZCalMachineCommand("zcal-early", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            )
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                ZConfirmMachineCommand("zc-early", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            )
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                HomeMachineCommand("home-repeat", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            )
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    finally:
        repository.close()


def test_request_consume_in_progress_and_drive_have_exact_durable_order(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        request_command = HomeMachineCommand(
            "order-request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None
        )
        requested = cast(
            MachineCommandResult,
            chain.admit_action(request_command),
        )
        request_row = (
            repository._get_connection()
            .execute("SELECT completed_at, claimed_at FROM machine_request_results WHERE request_id = 'order-request'")
            .fetchone()
        )
        assert request_row is not None and request_row[0] is not None and request_row[1] is None
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)

        execute_command = HomeMachineCommand(
            "order-execute",
            OPERATOR,
            execution_id,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
        admitted = chain.admit_action(execute_command)
        execution = repository.get_execution(execution_id)
        assert execution is not None and execution.phase is MachinePhase.HOMING
        execute_row = (
            repository._get_connection()
            .execute("SELECT completed_at, claimed_at FROM machine_request_results WHERE request_id = 'order-execute'")
            .fetchone()
        )
        assert execute_row is not None and execute_row[0] is not None and execute_row[1] is None
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)

        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.AWAITING_ZCAL_APPROVAL
        replay_request = cast(
            MachineCommandResult,
            chain.admit_action(replace(request_command, requester=AuthenticatedMachinePrincipal(1100, 1100, 31998))),
        )
        replay_execute = cast(
            MachineCommandResult,
            chain.admit_action(replace(execute_command, requester=AuthenticatedMachinePrincipal(2200, 2200, 41998))),
        )
        assert replay_request.deduplicated is True and replay_execute.deduplicated is True
        claimed = (
            repository._get_connection()
            .execute("SELECT claimed_at FROM machine_request_results WHERE request_id = 'order-execute'")
            .fetchone()
        )
        assert claimed is not None and claimed[0] is not None
        with pytest.raises(DrawingMachineError, match="already claimed"):
            chain.drive_admitted_action(admitted)
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    finally:
        repository.close()


def test_non_idle_same_session_evidence_cannot_issue_home_challenge(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
        chain._session_evidence[execution_id] = type(evidence)(  # type: ignore[attr-defined]
            evidence.schema_version,
            evidence.execution_id,
            evidence.machine_session_epoch,
            "Run",
            evidence.mpos,
            evidence.wpos,
            evidence.wco,
            evidence.g54,
            evidence.stabilized,
            evidence.observed_at,
        )
        with pytest.raises(DrawingMachineError, match="stale or misbound"):
            chain.admit_action(HomeMachineCommand("unsafe", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None))
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_post_stream_safe_home_successor_can_only_home_then_complete(tmp_path: Path) -> None:
    from drawingmachine.adapters.hardware.fake_fluidnc import StrictFakeFluidNCFactory
    from drawingmachine.domain.machine import RecoveryDisposition, RecoveryIntent
    from drawingmachine.domain.machine.transitions import allowed_phase_transition
    from drawingmachine.ports.fluidnc import CloseOutcome, CloseSessionRequest
    from tests.unit.application.machine.test_recovery import _chain as _recovery_chain
    from tests.unit.application.machine.test_recovery import _recover

    successor_epoch = _uuid(4)
    successor_factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _preflight(successor_epoch),
            ),
            FakeFluidNCStep(
                FakeFluidNCOperation.HOME_ALL_AXES,
                _home_request(),
                _home_outcome(successor_epoch),
            ),
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(successor_epoch),
                CloseOutcome(successor_epoch),
            ),
        ),
    )
    chain, repository, factory, _ = _recovery_chain(
        tmp_path,
        RecoveryIntent.POST_STREAM_SAFE_HOME,
        factories=(successor_factory,),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="recover-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.SAFE_HOME,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        successor_admission = chain.admit_action(
            _recover(
                request_id="recover-execute",
                requester=OPERATOR,
                disposition=RecoveryDisposition.SAFE_HOME,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_challenge_id(requested),
            )
        )
        successor = chain.drive_admitted_action(successor_admission)
        assert successor.phase is MachinePhase.AWAITING_HOME_APPROVAL
        assert not allowed_phase_transition(
            MachinePhase.AWAITING_HOME_APPROVAL,
            MachineAction.STREAM,
            MachinePhase.STREAMING,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        )
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                ZCalMachineCommand("before-home", AUTOMATION, successor.execution_id, MachineCommandMode.REQUEST, None)
            )
        result = _run_action(chain, repository, HomeMachineCommand, successor.execution_id, "safe-home")
        assert result.phase is MachinePhase.COMPLETED  # type: ignore[attr-defined]
        assert not allowed_phase_transition(
            MachinePhase.COMPLETED,
            MachineAction.STREAM,
            MachinePhase.STREAMING,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        )
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                ZCalMachineCommand("after-home", AUTOMATION, successor.execution_id, MachineCommandMode.REQUEST, None)
            )
        with pytest.raises(DrawingMachineError, match="requires the current"):
            chain.admit_action(
                ZConfirmMachineCommand(
                    "after-home-zc", AUTOMATION, successor.execution_id, MachineCommandMode.REQUEST, None
                )
            )
        assert factory.opens == 1
        assert successor_factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.HOME_ALL_AXES,
            FakeFluidNCOperation.CLOSE,
        )
        assert repository._get_connection().execute("SELECT state FROM jobs WHERE job_id = 'job-c9'").fetchone() == (
            "COMPLETED",
        )
        home_challenge = cast(
            str,
            repository._get_connection()
            .execute("SELECT challenge_id FROM machine_approval_challenges WHERE action = 'HOME'")
            .fetchone()[0],
        )
        _assert_consumed_approval_audit_chain(
            repository,
            "safe-home-execute",
            home_challenge,
            final_decision="COMPLETED",
        )
    finally:
        repository.close()


@pytest.mark.parametrize("failure_at", ["home", "close"])
def test_post_stream_home_or_close_uncertainty_retains_post_stream_recovery_latch(
    tmp_path: Path,
    failure_at: str,
) -> None:
    from drawingmachine.adapters.hardware.fake_fluidnc import StrictFakeFluidNCFactory
    from drawingmachine.domain.machine import RecoveryDisposition, RecoveryIntent
    from tests.unit.application.machine.test_recovery import _chain as _recovery_chain
    from tests.unit.application.machine.test_recovery import _recover

    successor_epoch = _uuid(4)
    if failure_at == "home":
        home = HomeOutcome(
            successor_epoch,
            _home_request(),
            None,
            FluidNCFailure(
                FluidNCFailureKind.LOST_ACK,
                "SAFE_HOME_ACK_LOST",
                "post-stream HOME acknowledgement was lost",
                True,
            ),
            acknowledged_steps=0,
            stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
        )
        close = CloseOutcome(successor_epoch)
    else:
        home = _home_outcome(successor_epoch)
        close = CloseOutcome(
            successor_epoch,
            FluidNCFailure(
                FluidNCFailureKind.CLOSE_ERROR,
                "SAFE_HOME_CLOSE_UNCERTAIN",
                "post-stream HOME session close was uncertain",
                False,
            ),
        )
    successor_factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _preflight(successor_epoch),
            ),
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), home),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(successor_epoch), close),
        ),
    )
    chain, repository, _, _ = _recovery_chain(
        tmp_path,
        RecoveryIntent.POST_STREAM_SAFE_HOME,
        factories=(successor_factory,),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="recover-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.SAFE_HOME,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        successor = chain.drive_admitted_action(
            chain.admit_action(
                _recover(
                    request_id="recover-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.SAFE_HOME,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(requested),
                )
            )
        )
        result = _run_action(chain, repository, HomeMachineCommand, successor.execution_id, "safe-home")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
        assert result.stream_milestone.value == "STREAM_CONFIRMED"  # type: ignore[attr-defined]
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None and motion.action is MachineAction.HOME
        if failure_at == "home":
            assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
            assert motion.last_step is MachineMotionStep.HOME_DISPATCH
            assert motion.acknowledged_steps == 0 and motion.postcondition_proven is False
        else:
            assert motion.kind is MachineMotionEvidenceKind.CLOSE_UNCERTAIN
            assert motion.last_step is MachineMotionStep.SESSION_CLOSE
            assert motion.acknowledged_steps == 1 and motion.postcondition_proven is True
            assert motion.snapshot is not None and motion.snapshot.mpos == (192.0, 192.0, 192.0)
        assert repository.get_active_execution() == result
        assert successor_factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
        assert successor_factory.started_operations.count(FakeFluidNCOperation.CLOSE) == 1
        if failure_at == "close":
            assert chain.retained_session_epochs() == (successor_epoch,)
        home_challenge = cast(
            str,
            repository._get_connection()
            .execute("SELECT challenge_id FROM machine_approval_challenges WHERE action = 'HOME'")
            .fetchone()[0],
        )
        _assert_consumed_approval_audit_chain(
            repository,
            "safe-home-execute",
            home_challenge,
            final_decision="RECOVERY_REQUIRED",
        )
    finally:
        repository.close()


def test_motion_request_replay_is_durable_and_argument_conflict_is_closed(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    command = HomeMachineCommand("same", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
    try:
        first = cast(MachineCommandResult, chain.admit_action(command))
        replay = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand(
                    "same",
                    AuthenticatedMachinePrincipal(AUTOMATION.uid, AUTOMATION.gid, AUTOMATION.pid + 1),
                    execution_id,
                    MachineCommandMode.REQUEST,
                    None,
                )
            ),
        )
        assert replay.deduplicated is True and _challenge_id(replay) == _challenge_id(first)
        with pytest.raises(DrawingMachineError, match="different arguments"):
            chain.admit_action(
                HomeMachineCommand("same", AUTOMATION, execution_id, MachineCommandMode.EXECUTE, _challenge_id(first))
            )
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_unconfigured_or_automation_consumer_cannot_dispatch(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        challenge = _challenge_id(requested)
        with pytest.raises(DrawingMachineError, match="configured operator"):
            chain.admit_action(
                HomeMachineCommand("auto", AUTOMATION, execution_id, MachineCommandMode.EXECUTE, challenge)
            )
        with pytest.raises(DrawingMachineError, match="configured machine principal"):
            chain.admit_action(
                HomeMachineCommand(
                    "wrong",
                    AuthenticatedMachinePrincipal(3300, 3300, 1),
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    challenge,
                )
            )
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_missing_motion_challenge_and_missing_session_fail_before_adapter(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        with pytest.raises(DrawingMachineError, match="does not exist"):
            chain.admit_action(
                HomeMachineCommand("missing", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _uuid(999))
            )
        chain._sessions.pop(execution_id)  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="same-session connection"):
            chain.admit_action(
                HomeMachineCommand("no-session", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            )
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_second_motion_request_supersedes_pending_and_expired_replacement_is_exact(tmp_path: Path) -> None:
    chain, repository, _, execution_id = _chain(tmp_path, ())
    try:
        first = cast(
            MachineCommandResult,
            chain.admit_action(HomeMachineCommand("first", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)),
        )
        second = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("second", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        assert repository.get_challenge(_challenge_id(first)).status.value == "SUPERSEDED"  # type: ignore[union-attr]
        current = repository.get_challenge(_challenge_id(second))
        assert current is not None
        chain._wall_now = lambda: current.expires_at  # type: ignore[attr-defined]
        chain._monotonic_now = lambda: current.monotonic_deadline  # type: ignore[attr-defined]
        third = cast(
            MachineCommandResult,
            chain.admit_action(HomeMachineCommand("third", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)),
        )
        assert repository.get_challenge(current.challenge_id).status.value == "EXPIRED"  # type: ignore[union-attr]
        assert repository.get_challenge(_challenge_id(third)).status.value == "PENDING"  # type: ignore[union-attr]
    finally:
        repository.close()


def test_motion_replacement_propagates_nonexpiry_clock_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import drawingmachine.application.machine.execution as execution_module

    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        chain.admit_action(HomeMachineCommand("first", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None))

        def reject(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise DrawingMachineError(
                execution_module.ErrorPayload(
                    code="MACHINE_APPROVAL_CLOCK_REJECTED",
                    category=execution_module.ErrorCategory.PERMISSION,
                    message="approval clock authority rejected",
                    retryable=False,
                    details={},
                )
            )

        monkeypatch.setattr(execution_module, "supersede_approval", reject)
        with pytest.raises(DrawingMachineError, match="clock authority"):
            chain.admit_action(HomeMachineCommand("second", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None))
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_motion_request_transaction_race_replays_durable_challenge(tmp_path: Path) -> None:
    class MaskOuterRequest:
        def __init__(self, delegate: SQLiteMachineRepository) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        def get_request(self, key: object) -> None:
            del key
            return None

    chain, repository, _, execution_id = _chain(tmp_path, ())
    command = HomeMachineCommand("race", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
    try:
        first = cast(MachineCommandResult, chain.admit_action(command))
        chain._repository = MaskOuterRequest(repository)  # type: ignore[assignment,attr-defined]
        replay = cast(MachineCommandResult, chain.admit_action(command))
        assert replay.deduplicated is True
        assert _challenge_id(replay) == _challenge_id(first)
    finally:
        repository.close()


def test_challenge_disappearing_inside_execute_transaction_never_enters_homing(tmp_path: Path) -> None:
    class MissingLiveChallenge:
        def __init__(self, delegate: SQLiteMachineRepository) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def get_challenge(self, challenge_id: str) -> None:
                        del challenge_id
                        return None

                yield Proxy()

    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        chain._repository = MissingLiveChallenge(repository)  # type: ignore[assignment,attr-defined]
        with pytest.raises(DrawingMachineError, match="does not exist"):
            chain.admit_action(
                HomeMachineCommand(
                    "execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested)
                )
            )
        assert repository.get_execution(execution_id).phase is MachinePhase.AWAITING_HOME_APPROVAL  # type: ignore[union-attr]
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


@pytest.mark.parametrize("missing", ["session", "evidence", "retained"])
def test_motion_drive_without_exact_session_ownership_enters_recovery_before_adapter(
    tmp_path: Path,
    missing: str,
) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            HomeMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        if missing == "session":
            chain._sessions.pop(execution_id)  # type: ignore[attr-defined]
        elif missing == "evidence":
            chain._session_evidence.pop(execution_id)  # type: ignore[attr-defined]
        else:
            chain._retained_sessions[_uuid(2)] = chain._sessions[execution_id]  # type: ignore[attr-defined]
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence.failure.code in {"SESSION_OWNERSHIP_UNCERTAIN", "MOTION_PRECONDITION_UNSAFE"}
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("outcome_factory", "expected_code"),
    [
        (lambda epoch: object(), "HOME_OUTCOME_INVALID"),
        (lambda epoch: _home_outcome(_uuid(999)), "MOTION_OUTCOME_EPOCH_MISMATCH"),
        (lambda epoch: RuntimeError("secret adapter detail"), "HOME_AMBIGUOUS"),
    ],
)
def test_invalid_ambiguous_or_wrong_epoch_home_outcome_never_retries(
    tmp_path: Path,
    outcome_factory: object,
    expected_code: str,
) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            HomeMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        probe = _MotionProbeSession(_uuid(2), outcome_factory(_uuid(2)))  # type: ignore[operator]
        chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence.failure.code == expected_code
        motion = result.recovery_evidence.motion
        assert motion is not None and motion.action is MachineAction.HOME
        if expected_code == "HOME_OUTCOME_INVALID":
            assert motion.kind is MachineMotionEvidenceKind.INVALID_OUTCOME
            assert motion.acknowledged_steps is None and motion.snapshot is None
            assert motion.observed_machine_session_epoch is None
        elif expected_code == "MOTION_OUTCOME_EPOCH_MISMATCH":
            assert motion.kind is MachineMotionEvidenceKind.INVALID_OUTCOME
            assert motion.acknowledged_steps is None and motion.snapshot is None
            assert motion.observed_machine_session_epoch == _uuid(999)
        else:
            assert motion.kind is MachineMotionEvidenceKind.ADAPTER_UNCERTAIN
            assert motion.acknowledged_steps is None
            assert motion.last_step is MachineMotionStep.UNKNOWN
        assert probe.home_calls == 1 and probe.close_calls == 1
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_home_postcondition_rejection_retains_exact_acked_idle_stage(tmp_path: Path) -> None:
    epoch = _uuid(2)
    wrong_pose = _snapshot(
        mpos=(191.0, 192.0, 192.0),
        wpos=(95.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_ERROR,
        "POST_HOME_PROOF_FAILED",
        "post-HOME proof failed",
        True,
    )
    failed = HomeOutcome(
        epoch,
        _home_request(),
        wrong_pose,
        failure,
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
    )
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        result = _run_action(chain, repository, HomeMachineCommand, execution_id, "home-proof")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.POSTCONDITION_REJECTED
        assert motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.HOME_POSTCONDITION
        assert motion.snapshot is not None and motion.postcondition_proven is False
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    finally:
        repository.close()


def test_home_incomplete_post_idle_proof_persists_exact_idle_stage_immutably(tmp_path: Path) -> None:
    epoch = _uuid(2)
    incomplete = parse_preflight_snapshot(
        status_line="<Idle|MPos:192.000,192.000,192.000|WPos:96.000,96.000,96.000|WCO:96.000,96.000,96.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    failed = HomeOutcome(
        epoch,
        _home_request(),
        incomplete,
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "POST_HOME_PROOF_FAILED",
            "post-HOME parser proof failed after Idle",
            True,
        ),
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        result = _run_action(chain, repository, HomeMachineCommand, execution_id, "home-idle-proof")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
        assert motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.HOME_IDLE
        assert motion.snapshot is None and motion.postcondition_proven is False
        persisted = repository.get_execution(execution_id)
        assert persisted is not None and persisted.recovery_evidence == result.recovery_evidence  # type: ignore[attr-defined]
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            repository._get_connection().execute(
                "UPDATE machine_executions SET recovery_evidence_json = '{}' WHERE execution_id = ?",
                (execution_id,),
            )
    finally:
        repository.close()


def test_home_post_ack_failure_persists_acknowledged_step(tmp_path: Path) -> None:
    epoch = _uuid(2)
    failed = HomeOutcome(
        epoch,
        _home_request(),
        None,
        FluidNCFailure(
            FluidNCFailureKind.TIMEOUT,
            "HOME_POST_ACK_TIMEOUT",
            "HOME failed after observed ACK",
            True,
        ),
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        result = _run_action(chain, repository, HomeMachineCommand, execution_id, "home-post-ack")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None and motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.HOME_ACKNOWLEDGED
        persisted = repository.get_execution(execution_id)
        assert persisted is not None and persisted.recovery_evidence is not None
        assert persisted.recovery_evidence.motion == motion
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("action", "proof_code", "wrong_stage"),
    (
        (MachineAction.HOME, "POST_ZCAL_PROOF_FAILED", None),
        (MachineAction.HOME, "POST_ZCONFIRM_PROOF_FAILED", None),
        (MachineAction.HOME, "POST_HOME_PROOF_FAILED", SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED),
        (MachineAction.ZCAL, "POST_HOME_PROOF_FAILED", None),
        (MachineAction.ZCAL, "POST_ZCONFIRM_PROOF_FAILED", None),
        (MachineAction.ZCAL, "POST_ZCAL_PROOF_FAILED", ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED),
        (MachineAction.ZCONFIRM, "POST_HOME_PROOF_FAILED", None),
        (MachineAction.ZCONFIRM, "POST_ZCAL_PROOF_FAILED", None),
        (
            MachineAction.ZCONFIRM,
            "POST_ZCONFIRM_PROOF_FAILED",
            SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
        ),
    ),
)
def test_fake_reserved_proof_code_or_stage_is_rejected_and_persisted_as_invalid_outcome(
    tmp_path: Path,
    action: MachineAction,
    proof_code: str,
    wrong_stage: SingleCommandOutcomeStage | ZCalibrationOutcomeStage | None,
) -> None:
    epoch = _uuid(2)
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_ERROR,
        {
            MachineAction.HOME: "POST_HOME_PROOF_FAILED",
            MachineAction.ZCAL: "POST_ZCAL_PROOF_FAILED",
            MachineAction.ZCONFIRM: "POST_ZCONFIRM_PROOF_FAILED",
        }[action],
        "reserved proof failure",
        True,
    )
    if action is MachineAction.HOME:
        invalid: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome = HomeOutcome(
            epoch,
            _home_request(),
            _snapshot(
                mpos=(191.0, 192.0, 192.0),
                wpos=(95.0, 96.0, 96.0),
                wco=(96.0, 96.0, 96.0),
                g54=(96.0, 96.0, 96.0),
            ),
            failure,
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
        )
    elif action is MachineAction.ZCAL:
        invalid = ZCalibrationOutcome(
            epoch,
            _zcal_request(),
            6,
            _snapshot(
                mpos=(192.0, 192.0, 96.0),
                wpos=(96.0, 96.0, 0.0),
                wco=(96.0, 96.0, 96.0),
                g54=(96.0, 96.0, 96.0),
            ),
            failure,
            stage=ZCalibrationOutcomeStage.POSTCONDITION,
        )
    else:
        invalid = ZConfirmationOutcome(
            epoch,
            _zconfirm_request(),
            _snapshot(
                mpos=(192.0, 192.0, 99.0),
                wpos=(96.0, 96.0, 3.0),
                wco=(96.0, 96.0, 96.0),
                g54=(96.0, 96.0, 96.0),
            ),
            failure,
            acknowledged_steps=1,
            stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
        )
    object.__setattr__(failure, "code", proof_code)
    if wrong_stage is not None:
        object.__setattr__(invalid, "stage", wrong_stage)
    steps: list[FakeFluidNCStep] = []
    if action is not MachineAction.HOME:
        steps.append(FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)))
    if action is MachineAction.ZCONFIRM:
        steps.append(FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)))
    operation = {
        MachineAction.HOME: FakeFluidNCOperation.HOME_ALL_AXES,
        MachineAction.ZCAL: FakeFluidNCOperation.Z_CALIBRATION,
        MachineAction.ZCONFIRM: FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
    }[action]
    request = {
        MachineAction.HOME: _home_request(),
        MachineAction.ZCAL: _zcal_request(),
        MachineAction.ZCONFIRM: _zconfirm_request(),
    }[action]
    steps.extend(
        (
            FakeFluidNCStep(operation, request, invalid),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        )
    )
    chain, repository, _, execution_id = _chain(tmp_path, tuple(steps))
    try:
        if action is not MachineAction.HOME:
            _run_action(chain, repository, HomeMachineCommand, execution_id, "reserved-home")
        if action is MachineAction.ZCONFIRM:
            _run_action(chain, repository, ZCalMachineCommand, execution_id, "reserved-zcal")
        command_type = {
            MachineAction.HOME: HomeMachineCommand,
            MachineAction.ZCAL: ZCalMachineCommand,
            MachineAction.ZCONFIRM: ZConfirmMachineCommand,
        }[action]
        result = _run_action(chain, repository, command_type, execution_id, f"reserved-{action.value}")
        assert result.recovery_evidence.failure.code == f"{action.value}_OUTCOME_INVALID"  # type: ignore[attr-defined]
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None and motion.kind is MachineMotionEvidenceKind.INVALID_OUTCOME
        assert motion.acknowledged_steps is None and motion.snapshot is None
        persisted = repository.get_execution(execution_id)
        assert persisted is not None and persisted.recovery_evidence == result.recovery_evidence  # type: ignore[attr-defined]
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            repository._get_connection().execute(
                "UPDATE machine_executions SET recovery_evidence_json = '{}' WHERE execution_id = ?",
                (execution_id,),
            )
    finally:
        repository.close()


def test_stale_motion_capability_is_rejected_before_dispatch(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            HomeMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        stale = replace(
            admitted,
            capability=replace(
                admitted.capability,  # type: ignore[attr-defined]
                execution_revision=admitted.capability.execution_revision + 1,  # type: ignore[attr-defined]
            ),
        )
        with pytest.raises(DrawingMachineError, match="authority changed"):
            chain._drive_motion(stale)  # type: ignore[attr-defined]
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_execute_transaction_race_replays_winning_durable_admission(tmp_path: Path) -> None:
    import drawingmachine.application.machine.execution as execution_module

    chain, repository, _, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        command = HomeMachineCommand(
            "execute",
            OPERATOR,
            execution_id,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
        prior = repository.get_execution(execution_id)
        assert prior is not None
        validated = chain._validate_motion_ready(prior)  # type: ignore[attr-defined]
        first = chain.admit_action(command)
        raced = chain._execute_motion(  # type: ignore[attr-defined]
            command,
            prior,
            MachineAction.HOME,
            execution_module._request_key(command),
            execution_module._argument_digest(command),
            validated,
        )
        assert raced.response["deduplicated"] is True  # type: ignore[attr-defined]
        assert raced.execution_id == first.execution_id  # type: ignore[attr-defined]
        assert raced.approval == first.approval  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_motion_job_authority_mismatch_is_rejected_before_adapter(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        execution = repository.get_execution(execution_id)
        assert execution is not None
        with pytest.raises(DrawingMachineError, match="runtime authority"):
            chain._validate_motion_job(None, execution)  # type: ignore[attr-defined]
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_internal_non_c10_motion_capability_fails_closed_without_adapter_dispatch(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            HomeMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        with pytest.raises(DrawingMachineError, match="retain exactly one"):
            replace(admitted, approval=None)
        pending = replace(
            admitted.approval,  # type: ignore[attr-defined]
            status=ApprovalStatus.PENDING,
            status_changed_at=None,
            consumer=None,
        )
        with pytest.raises(DrawingMachineError, match="requires one consumed"):
            replace(admitted, approval=pending)
        with pytest.raises(DrawingMachineError, match="does not match"):
            replace(admitted, capability=replace(admitted.capability, action=MachineAction.ZCAL))  # type: ignore[attr-defined]
        assert FakeFluidNCOperation.HOME_ALL_AXES not in factory.started_operations
    finally:
        repository.close()


def test_motion_replay_requires_persisted_consumed_challenge_metadata_and_record(tmp_path: Path) -> None:
    class ReplayProxy:
        def __init__(self, delegate: SQLiteMachineRepository, record: object, *, hide_challenge: bool) -> None:
            self.delegate = delegate
            self.record = record
            self.hide_challenge = hide_challenge

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        def get_request(self, key: object) -> object:
            del key
            return self.record

        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:
                if not self.hide_challenge:
                    yield transaction
                    return

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def get_challenge(self, challenge_id: str) -> None:
                        del challenge_id
                        return None

                yield Proxy()

    chain, repository, _, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        execute = HomeMachineCommand(
            "execute",
            OPERATOR,
            execution_id,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
        chain.admit_action(execute)
        record = repository.get_request(MachineRequestKey(OPERATOR.uid, OPERATOR.gid, "execute"))
        assert record is not None and record.response is not None
        missing_metadata = replace(record, response={"schema_version": 1})
        chain._repository = ReplayProxy(repository, missing_metadata, hide_challenge=False)  # type: ignore[assignment,attr-defined]
        with pytest.raises(DrawingMachineError, match="lacks consumed challenge"):
            chain.admit_action(execute)
        chain._repository = ReplayProxy(repository, record, hide_challenge=True)  # type: ignore[assignment,attr-defined]
        with pytest.raises(DrawingMachineError, match="challenge is unavailable"):
            chain.admit_action(execute)
    finally:
        repository.close()


@pytest.mark.parametrize("concurrent", ["missing", "recovery", "changed"])
def test_concurrent_authority_change_after_adapter_exception_never_retries_or_overwrites(
    tmp_path: Path,
    concurrent: str,
) -> None:
    class ConcurrentProjection:
        def __init__(self, delegate: SQLiteMachineRepository, current: object) -> None:
            self.delegate = delegate
            self.current = current
            self.calls = 0

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        def get_execution(self, execution_id: str) -> object:
            del execution_id
            self.calls += 1
            if self.calls == 1:
                return self.current
            if concurrent == "missing":
                return None
            if concurrent == "recovery":
                return SimpleNamespace(phase=MachinePhase.RECOVERY_REQUIRED)
            return SimpleNamespace(phase=self.current.phase, revision=self.current.revision + 1)  # type: ignore[attr-defined]

    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            HomeMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        current = repository.get_execution(execution_id)
        assert current is not None
        probe = _MotionProbeSession(_uuid(2), RuntimeError("adapter failed"))
        chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
        chain._repository = ConcurrentProjection(repository, current)  # type: ignore[assignment,attr-defined]
        if concurrent == "recovery":
            result = chain._drive_motion(admitted)  # type: ignore[attr-defined]
            assert result.phase is MachinePhase.RECOVERY_REQUIRED
        else:
            with pytest.raises(RuntimeError, match="adapter failed"):
                chain._drive_motion(admitted)  # type: ignore[attr-defined]
        assert probe.home_calls == 1
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


__all__ = [
    "OPERATOR",
    "_chain",
    "_challenge_id",
    "_home_outcome",
    "_home_request",
    "_run_action",
    "_snapshot",
    "_zcal_outcome",
    "_zcal_request",
    "_zconfirm_outcome",
    "_zconfirm_request",
]
