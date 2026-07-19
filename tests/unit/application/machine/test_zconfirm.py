from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    MachineCommandMode,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.machine import (
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
    SingleCommandOutcomeStage,
    ZConfirmationOutcome,
)
from tests.unit.application.machine.test_admission import AUTOMATION, _uuid
from tests.unit.application.machine.test_home import (
    OPERATOR,
    _chain,
    _challenge_id,
    _home_outcome,
    _home_request,
    _run_action,
    _snapshot,
    _zcal_outcome,
    _zcal_request,
    _zconfirm_outcome,
    _zconfirm_request,
)


class _ZConfirmProbeSession:
    def __init__(self, epoch: str) -> None:
        self.epoch = epoch
        self.calls = 0

    def raise_after_z_confirmation(self, request: object) -> object:
        assert request == _zconfirm_request()
        self.calls += 1
        return object()

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        return CloseOutcome(self.epoch)


def test_zconfirm_command_is_closed_and_separately_approved() -> None:
    command = ZConfirmMachineCommand("zconfirm", AUTOMATION, "execution-c10", MachineCommandMode.REQUEST, None)
    assert command.challenge_id is None
    with pytest.raises(DrawingMachineError):
        ZConfirmMachineCommand("bad", object(), "execution", MachineCommandMode.REQUEST, None)  # type: ignore[arg-type]


def test_zconfirm_requires_same_session_zcal_and_proves_calibrated_work_pose(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(
                FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
                _zconfirm_request(),
                _zconfirm_outcome(epoch),
            ),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm")
        assert result.phase is MachinePhase.AWAITING_STREAM_APPROVAL  # type: ignore[attr-defined]
        assert factory.calls[-1].request == _zconfirm_request()
        assert factory.connections_opened == 1
    finally:
        repository.close()


def test_zconfirm_uncertainty_enters_recovery_and_never_retries_raise(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zconfirm_request()
    failure = FluidNCFailure(
        FluidNCFailureKind.LOST_ACK,
        "ZCONFIRM_ACK_LOST",
        "raise acknowledgement was lost",
        True,
    )
    failed = ZConfirmationOutcome(
        epoch,
        request,
        _snapshot(
            mpos=(192.0, 192.0, 97.0),
            wpos=(96.0, 96.0, 1.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
        failure,
        acknowledged_steps=0,
        stage=SingleCommandOutcomeStage.DISPATCH_ATTEMPTED,
    )
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "ZCONFIRM_ACK_LOST"  # type: ignore[attr-defined]
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.action is MachineAction.ZCONFIRM
        assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
        assert motion.last_step is MachineMotionStep.ZCONFIRM_RAISE
        assert motion.acknowledged_steps == 0
        assert motion.snapshot is not None
        assert motion.snapshot.wpos == (96.0, 96.0, 1.0)
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CONFIRMATION_RAISE) == 1
    finally:
        repository.close()


def test_zconfirm_postcondition_rejection_retains_exact_acked_idle_stage(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zconfirm_request()
    failed = ZConfirmationOutcome(
        epoch,
        request,
        _snapshot(
            mpos=(192.0, 192.0, 99.0),
            wpos=(96.0, 96.0, 3.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "POST_ZCONFIRM_PROOF_FAILED",
            "post-ZCONFIRM proof failed",
            True,
        ),
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
    )
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-proof")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-proof")
        result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm-proof")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.POSTCONDITION_REJECTED
        assert motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.ZCONFIRM_POSTCONDITION
        assert motion.snapshot is not None and motion.postcondition_proven is False
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CONFIRMATION_RAISE) == 1
    finally:
        repository.close()


def test_zconfirm_incomplete_post_idle_proof_persists_exact_idle_stage_immutably(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zconfirm_request()
    incomplete = parse_preflight_snapshot(
        status_line="<Idle|MPos:192.000,192.000,99.500|WPos:96.000,96.000,3.500|WCO:96.000,96.000,96.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    failed = ZConfirmationOutcome(
        epoch,
        request,
        incomplete,
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "POST_ZCONFIRM_PROOF_FAILED",
            "post-ZCONFIRM parser proof failed after Idle",
            True,
        ),
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.IDLE_OBSERVED,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-idle-proof")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-idle-proof")
        result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm-idle-proof")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
        assert motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.ZCONFIRM_IDLE
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


def test_zconfirm_post_ack_failure_persists_acknowledged_step(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zconfirm_request()
    failed = ZConfirmationOutcome(
        epoch,
        request,
        None,
        FluidNCFailure(
            FluidNCFailureKind.TIMEOUT,
            "ZCONFIRM_POST_ACK_TIMEOUT",
            "ZCONFIRM failed after observed ACK",
            True,
        ),
        acknowledged_steps=1,
        stage=SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-post-ack")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-post-ack")
        result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm-post-ack")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None and motion.acknowledged_steps == 1
        assert motion.last_step is MachineMotionStep.ZCONFIRM_ACKNOWLEDGED
        persisted = repository.get_execution(execution_id)
        assert persisted is not None and persisted.recovery_evidence is not None
        assert persisted.recovery_evidence.motion == motion
    finally:
        repository.close()


def test_changed_session_evidence_epoch_blocks_challenge_before_adapter(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
        chain._session_evidence[execution_id] = type(evidence)(  # type: ignore[attr-defined]
            evidence.schema_version,
            evidence.execution_id,
            _uuid(999),
            evidence.controller_state,
            evidence.mpos,
            evidence.wpos,
            evidence.wco,
            evidence.g54,
            evidence.stabilized,
            evidence.observed_at,
        )
        with pytest.raises(DrawingMachineError, match="stale or misbound"):
            chain.admit_action(HomeMachineCommand("home", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None))
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_invalid_zconfirm_outcome_enters_recovery_without_second_raise(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                ZConfirmMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            ZConfirmMachineCommand(
                "execute",
                OPERATOR,
                execution_id,
                MachineCommandMode.EXECUTE,
                _challenge_id(requested),
            )
        )
        probe = _ZConfirmProbeSession(epoch)
        chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence.failure.code == "ZCONFIRM_OUTCOME_INVALID"
        assert probe.calls == 1
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CONFIRMATION_RAISE) == 0
    finally:
        repository.close()
