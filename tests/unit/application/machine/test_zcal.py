from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    MachineCommandMode,
    ZCalMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.machine import (
    MachineAction,
    MachineFailureEvidenceV1,
    MachineMotionEvidenceKind,
    MachineMotionRecoveryEvidenceV1,
    MachineMotionStep,
    MachinePhase,
    MachineRecoveryEvidence,
    MachineSessionSnapshot,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    ZCalibrationOutcome,
    ZCalibrationOutcomeStage,
)
from tests.unit.application.machine.test_admission import AUTOMATION, NOW, _uuid
from tests.unit.application.machine.test_home import (
    OPERATOR,
    _assert_consumed_approval_audit_chain,
    _chain,
    _challenge_id,
    _home_outcome,
    _home_request,
    _run_action,
    _snapshot,
    _zcal_outcome,
    _zcal_request,
)


class _ZCalProbeSession:
    def __init__(self, epoch: str, outcome: object) -> None:
        self.epoch = epoch
        self.outcome = outcome
        self.calls = 0

    def run_z_calibration(self, request: object) -> object:
        assert request == _zcal_request()
        self.calls += 1
        return self.outcome

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        return CloseOutcome(self.epoch)


def test_zcal_command_is_closed_and_separately_approved() -> None:
    command = ZCalMachineCommand("zcal", AUTOMATION, "execution-c10", MachineCommandMode.REQUEST, None)
    assert command.challenge_id is None
    with pytest.raises(DrawingMachineError):
        ZCalMachineCommand("bad", AUTOMATION, "execution", object(), None)  # type: ignore[arg-type]


def test_motion_recovery_evidence_is_closed_versioned_and_json_roundtrips() -> None:
    evidence = MachineMotionRecoveryEvidenceV1(
        schema_version=1,
        execution_id="execution-c10",
        action=MachineAction.ZCAL,
        kind=MachineMotionEvidenceKind.TYPED_FAILURE,
        last_step=MachineMotionStep.ZCAL_CENTER,
        acknowledged_steps=4,
        expected_machine_session_epoch=_uuid(2),
        observed_machine_session_epoch=_uuid(2),
        snapshot=None,
        postcondition_proven=False,
    )
    assert MachineMotionRecoveryEvidenceV1.from_json(evidence.to_json()) == evidence
    with pytest.raises(DrawingMachineError):
        replace(evidence, observed_machine_session_epoch=_uuid(3))


def test_motion_recovery_evidence_rejects_every_malformed_closed_field() -> None:
    snapshot = MachineSessionSnapshot(
        1,
        "execution-c10",
        _uuid(2),
        "Idle",
        (192.0, 192.0, 96.0),
        (96.0, 96.0, 0.0),
        (96.0, 96.0, 96.0),
        (96.0, 96.0, 96.0),
        True,
        NOW,
    )
    valid = MachineMotionRecoveryEvidenceV1(
        1,
        "execution-c10",
        MachineAction.ZCAL,
        MachineMotionEvidenceKind.COMMIT_UNCERTAIN,
        MachineMotionStep.COMMIT_AFTER_SIDE_EFFECT,
        6,
        _uuid(2),
        _uuid(2),
        snapshot,
        True,
    )
    invalid_values = (
        {"schema_version": 2},
        {"action": MachineAction.STREAM},
        {"acknowledged_steps": 7},
        {"snapshot": object()},
        {"expected_machine_session_epoch": _uuid(3)},
        {"postcondition_proven": object()},
        {"snapshot": None},
        {"last_step": MachineMotionStep.HOME_DISPATCH},
        {"snapshot": replace(snapshot, execution_id="other-execution")},
        {"snapshot": replace(snapshot, machine_session_epoch=_uuid(3))},
    )
    for changes in invalid_values:
        with pytest.raises(DrawingMachineError):
            MachineMotionRecoveryEvidenceV1(
                schema_version=changes.get("schema_version", valid.schema_version),  # type: ignore[arg-type]
                execution_id=valid.execution_id,
                action=changes.get("action", valid.action),  # type: ignore[arg-type]
                kind=valid.kind,
                last_step=changes.get("last_step", valid.last_step),  # type: ignore[arg-type]
                acknowledged_steps=changes.get("acknowledged_steps", valid.acknowledged_steps),  # type: ignore[arg-type]
                expected_machine_session_epoch=changes.get(  # type: ignore[arg-type]
                    "expected_machine_session_epoch", valid.expected_machine_session_epoch
                ),
                observed_machine_session_epoch=valid.observed_machine_session_epoch,
                snapshot=changes.get("snapshot", valid.snapshot),  # type: ignore[arg-type]
                postcondition_proven=changes.get("postcondition_proven", valid.postcondition_proven),  # type: ignore[arg-type]
            )

    invalid_json = valid.to_json()
    invalid_json["postcondition_proven"] = "true"
    with pytest.raises(DrawingMachineError):
        MachineMotionRecoveryEvidenceV1.from_json(invalid_json)

    zcal_postcondition = replace(
        valid,
        kind=MachineMotionEvidenceKind.POSTCONDITION_REJECTED,
        last_step=MachineMotionStep.ZCAL_POSTCONDITION,
        postcondition_proven=False,
    )
    assert MachineMotionRecoveryEvidenceV1.from_json(zcal_postcondition.to_json()) == zcal_postcondition

    recovery = MachineRecoveryEvidence(
        1,
        "execution-c10",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        MachineFailureEvidenceV1(1, "FAIL", "fixed failure", None, True),
        NOW,
        valid,
    )
    with pytest.raises(DrawingMachineError, match="exact MachineMotionRecoveryEvidenceV1"):
        MachineRecoveryEvidence(
            1,
            "execution-c10",
            RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.NOT_STARTED,
            "FAIL",
            MachineFailureEvidenceV1(1, "FAIL", "fixed failure", None, True),
            NOW,
            object(),  # type: ignore[arg-type]
        )
    with pytest.raises(DrawingMachineError, match="must be an object"):
        MachineRecoveryEvidence.from_json([])
    legacy = recovery.to_json()
    legacy.pop("motion")
    assert MachineRecoveryEvidence.from_json(legacy).motion is None

    session_json = snapshot.to_json()
    for field, value in (
        ("observed_at", "not-a-timestamp"),
        ("stabilized", 1),
        ("mpos", [1.0, 2.0]),
    ):
        malformed = dict(session_json)
        malformed[field] = value
        with pytest.raises(DrawingMachineError):
            MachineSessionSnapshot.from_json(malformed)


@pytest.mark.parametrize(
    ("action", "kind", "last_step", "acknowledged_steps"),
    (
        (
            MachineAction.ZCAL,
            MachineMotionEvidenceKind.PRECONDITION_REJECTED,
            MachineMotionStep.ZCAL_CONTACT,
            0,
        ),
        (
            MachineAction.ZCAL,
            MachineMotionEvidenceKind.CLOSE_UNCERTAIN,
            MachineMotionStep.ZCAL_METRIC,
            6,
        ),
        (
            MachineAction.HOME,
            MachineMotionEvidenceKind.COMMIT_UNCERTAIN,
            MachineMotionStep.ACTION_ADMITTED,
            1,
        ),
    ),
)
def test_motion_recovery_evidence_rejects_hostile_cross_product(
    action: MachineAction,
    kind: MachineMotionEvidenceKind,
    last_step: MachineMotionStep,
    acknowledged_steps: int,
) -> None:
    with pytest.raises(DrawingMachineError):
        evidence = MachineMotionRecoveryEvidenceV1(
            1,
            "execution-c10",
            action,
            kind,
            last_step,
            acknowledged_steps,
            _uuid(2),
            None if kind is MachineMotionEvidenceKind.PRECONDITION_REJECTED else _uuid(2),
            None,
            False,
        )
        MachineMotionRecoveryEvidenceV1.from_json(evidence.to_json())


def test_motion_recovery_evidence_rejects_foreign_outer_authority() -> None:
    snapshot = MachineSessionSnapshot(
        1,
        "other-execution",
        _uuid(3),
        "Idle",
        (192.0, 192.0, 96.0),
        (96.0, 96.0, 0.0),
        (96.0, 96.0, 96.0),
        (96.0, 96.0, 96.0),
        True,
        NOW,
    )
    foreign = MachineMotionRecoveryEvidenceV1(
        1,
        "other-execution",
        MachineAction.ZCAL,
        MachineMotionEvidenceKind.COMMIT_UNCERTAIN,
        MachineMotionStep.COMMIT_AFTER_SIDE_EFFECT,
        6,
        _uuid(3),
        _uuid(3),
        snapshot,
        True,
    )
    with pytest.raises(DrawingMachineError):
        MachineRecoveryEvidence(
            1,
            "execution-c10",
            RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.NOT_STARTED,
            "FAIL",
            MachineFailureEvidenceV1(1, "FAIL", "fixed failure", None, True),
            NOW,
            foreign,
        )
    future_snapshot = replace(snapshot, observed_at=NOW + timedelta(seconds=1))
    future = replace(foreign, snapshot=future_snapshot)
    with pytest.raises(DrawingMachineError):
        MachineRecoveryEvidence(
            1,
            "other-execution",
            RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.NOT_STARTED,
            "FAIL",
            MachineFailureEvidenceV1(1, "FAIL", "fixed failure", None, True),
            NOW,
            future,
        )


def test_zcal_uses_exact_fixed_typed_sequence_and_all_six_acknowledgements(tmp_path: Path) -> None:
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
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        assert result.phase is MachinePhase.AWAITING_ZCONFIRM_APPROVAL  # type: ignore[attr-defined]
        assert factory.calls[-1].request == _zcal_request()
        assert factory.calls[-1].outcome.acknowledged_steps == 6  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_zcal_stops_on_first_failed_ack_and_enters_exact_recovery_without_retry(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zcal_request()
    failure = FluidNCFailure(
        FluidNCFailureKind.CONTROLLER_ERROR,
        "ZCAL_STEP_FAILED",
        "fixed Z calibration step failed",
        True,
    )
    failed = ZCalibrationOutcome(
        epoch,
        request,
        2,
        _snapshot(
            mpos=(192.0, 192.0, 192.0),
            wpos=(96.0, 96.0, 96.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
        failure,
        stage=ZCalibrationOutcomeStage.WORK_COORDINATE_ATTEMPTED,
    )
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "ZCAL_STEP_FAILED"  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.side_effect_possible is True  # type: ignore[attr-defined]
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CALIBRATION) == 1
        assert factory.started_operations[-1] is FakeFluidNCOperation.CLOSE
        challenge_id = cast(
            str,
            repository._get_connection()
            .execute("SELECT challenge_id FROM machine_approval_challenges WHERE action = 'ZCAL'")
            .fetchone()[0],
        )
        _assert_consumed_approval_audit_chain(
            repository,
            "zcal-execute",
            challenge_id,
            final_decision="RECOVERY_REQUIRED",
        )
    finally:
        repository.close()


def test_failed_zcal_close_uncertainty_audit_retains_same_consumed_challenge(tmp_path: Path) -> None:
    epoch = _uuid(2)
    request = _zcal_request()
    failed = ZCalibrationOutcome(
        epoch,
        request,
        1,
        _snapshot(
            mpos=(192.0, 192.0, 192.0),
            wpos=(96.0, 96.0, 96.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "ZCAL_FAILED_BEFORE_CLOSE",
            "fixed Z calibration failed before uncertain close",
            True,
        ),
        stage=ZCalibrationOutcomeStage.ABSOLUTE_ATTEMPTED,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, request, failed),
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(epoch),
                CloseOutcome(
                    epoch,
                    FluidNCFailure(
                        FluidNCFailureKind.CLOSE_ERROR,
                        "ZCAL_CLOSE_UNCERTAIN",
                        "failed ZCAL session close was uncertain",
                        False,
                    ),
                ),
            ),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-close-audit")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-close-audit")
        challenge_id = cast(
            str,
            repository._get_connection()
            .execute("SELECT challenge_id FROM machine_approval_challenges WHERE action = 'ZCAL'")
            .fetchone()[0],
        )
        _assert_consumed_approval_audit_chain(
            repository,
            "zcal-close-audit-execute",
            challenge_id,
            final_decision="RECOVERY_REQUIRED",
            extra_decisions=("SESSION_OWNERSHIP_RETAINED",),
        )
        assert chain.retained_session_epochs() == (epoch,)
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("acknowledged", "outcome_stage", "expected_step"),
    (
        (0, ZCalibrationOutcomeStage.METRIC_ATTEMPTED, MachineMotionStep.ZCAL_METRIC),
        (1, ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED, MachineMotionStep.ZCAL_METRIC_ACKNOWLEDGED),
        (1, ZCalibrationOutcomeStage.ABSOLUTE_ATTEMPTED, MachineMotionStep.ZCAL_ABSOLUTE),
        (2, ZCalibrationOutcomeStage.ABSOLUTE_ACKNOWLEDGED, MachineMotionStep.ZCAL_ABSOLUTE_ACKNOWLEDGED),
        (2, ZCalibrationOutcomeStage.WORK_COORDINATE_ATTEMPTED, MachineMotionStep.ZCAL_WORK_COORDINATE),
        (
            3,
            ZCalibrationOutcomeStage.WORK_COORDINATE_ACKNOWLEDGED,
            MachineMotionStep.ZCAL_WORK_COORDINATE_ACKNOWLEDGED,
        ),
        (3, ZCalibrationOutcomeStage.PEN_UP_ATTEMPTED, MachineMotionStep.ZCAL_PEN_UP),
        (4, ZCalibrationOutcomeStage.PEN_UP_ACKNOWLEDGED, MachineMotionStep.ZCAL_PEN_UP_ACKNOWLEDGED),
        (4, ZCalibrationOutcomeStage.CENTER_ATTEMPTED, MachineMotionStep.ZCAL_CENTER),
        (5, ZCalibrationOutcomeStage.CENTER_ACKNOWLEDGED, MachineMotionStep.ZCAL_CENTER_ACKNOWLEDGED),
        (5, ZCalibrationOutcomeStage.CONTACT_ATTEMPTED, MachineMotionStep.ZCAL_CONTACT),
        (6, ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED, MachineMotionStep.ZCAL_CONTACT_ACKNOWLEDGED),
        (6, ZCalibrationOutcomeStage.POSTCONDITION, MachineMotionStep.ZCAL_POSTCONDITION),
    ),
)
def test_zcal_recovery_persists_exact_zero_through_six_ack_evidence_immutably(
    tmp_path: Path,
    acknowledged: int,
    outcome_stage: ZCalibrationOutcomeStage,
    expected_step: MachineMotionStep,
) -> None:
    epoch = _uuid(2)
    request = _zcal_request()
    failed = ZCalibrationOutcome(
        epoch,
        request,
        acknowledged,
        _snapshot(
            mpos=(192.0, 192.0, 96.0),
            wpos=(96.0, 96.0, 0.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
        FluidNCFailure(
            FluidNCFailureKind.LOST_ACK if acknowledged else FluidNCFailureKind.CONTROLLER_ERROR,
            f"ZCAL_ACK_{acknowledged}",
            "fixed Z calibration evidence",
            acknowledged > 0,
        ),
        stage=outcome_stage,
    )
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, request, failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, f"home-{acknowledged}")
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, f"zcal-{acknowledged}")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.action is MachineAction.ZCAL
        assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
        assert motion.last_step is expected_step
        assert motion.acknowledged_steps == acknowledged
        assert motion.expected_machine_session_epoch == epoch
        assert motion.observed_machine_session_epoch == epoch
        assert motion.snapshot is not None
        foreign_epoch = _uuid(99)
        foreign_motion = replace(
            motion,
            expected_machine_session_epoch=foreign_epoch,
            observed_machine_session_epoch=foreign_epoch,
            snapshot=replace(motion.snapshot, machine_session_epoch=foreign_epoch),
        )
        with pytest.raises(DrawingMachineError, match="execution session authority"):
            replace(
                result,
                recovery_evidence=replace(
                    result.recovery_evidence,
                    motion=foreign_motion,
                ),
            )
        early_snapshot = replace(motion.snapshot, observed_at=result.created_at - timedelta(seconds=1))
        with pytest.raises(DrawingMachineError, match="recovery timeline"):
            replace(
                result,
                recovery_evidence=replace(
                    result.recovery_evidence,
                    motion=replace(motion, snapshot=early_snapshot),
                ),
            )
        assert motion.snapshot.wpos == (96.0, 96.0, 0.0)
        assert motion.postcondition_proven is False
        assert MachineRecoveryEvidence.from_json(result.recovery_evidence.to_json()) == result.recovery_evidence  # type: ignore[attr-defined]
        persisted = repository.get_execution(execution_id)
        assert persisted is not None and persisted.recovery_evidence is not None
        assert persisted.recovery_evidence.motion == motion
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            repository._get_connection().execute(
                "UPDATE machine_executions SET recovery_evidence_json = '{}' WHERE execution_id = ?",
                (execution_id,),
            )
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CALIBRATION) == 1
    finally:
        repository.close()


def test_zcal_incomplete_post_idle_proof_persists_exact_postcondition_stage_immutably(tmp_path: Path) -> None:
    epoch = _uuid(2)
    incomplete = parse_preflight_snapshot(
        status_line="<Idle|MPos:192.000,192.000,96.000|WPos:96.000,96.000,0.000|WCO:96.000,96.000,96.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    failed = ZCalibrationOutcome(
        epoch,
        _zcal_request(),
        6,
        incomplete,
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_ERROR,
            "POST_ZCAL_PROOF_FAILED",
            "post-ZCAL parser proof failed after Idle",
            True,
        ),
        stage=ZCalibrationOutcomeStage.POSTCONDITION,
    )
    chain, repository, _, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), failed),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-idle-proof")
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-idle-proof")
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.TYPED_FAILURE
        assert motion.acknowledged_steps == 6
        assert motion.last_step is MachineMotionStep.ZCAL_POSTCONDITION
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


def test_cross_action_reuse_is_rejected_before_second_adapter_call(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
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
        chain.drive_admitted_action(admitted)
        with pytest.raises(DrawingMachineError, match=r"pending|binding"):
            chain.admit_action(
                ZCalMachineCommand(
                    "cross-action",
                    OPERATOR,
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    _challenge_id(requested),
                )
            )
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
        assert FakeFluidNCOperation.Z_CALIBRATION not in factory.started_operations
    finally:
        repository.close()


def test_expired_motion_approval_never_enters_in_progress_or_dispatches(tmp_path: Path) -> None:
    chain, repository, factory, execution_id = _chain(tmp_path, ())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        chain._wall_now = lambda: requested.response["challenge"]["expires_at"]  # type: ignore[attr-defined,index,assignment]
        challenge = repository.get_challenge(_challenge_id(requested))
        assert challenge is not None
        chain._wall_now = lambda: challenge.expires_at  # type: ignore[attr-defined]
        chain._monotonic_now = lambda: challenge.monotonic_deadline  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="expired"):
            chain.admit_action(
                HomeMachineCommand(
                    "execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, challenge.challenge_id
                )
            )
        assert repository.get_execution(execution_id).phase is MachinePhase.AWAITING_HOME_APPROVAL  # type: ignore[union-attr]
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
    finally:
        repository.close()


def test_commit_failure_after_zcal_side_effect_enters_recovery_without_adapter_retry(tmp_path: Path) -> None:
    class FailNextSuccessTransition:
        def __init__(self, delegate: object) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:  # type: ignore[attr-defined]
                owner = self

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def transition_execution(self, expected_revision: int, execution: object, **kwargs: object) -> None:
                        if not owner.failed and execution.phase is MachinePhase.AWAITING_ZCONFIRM_APPROVAL:  # type: ignore[attr-defined]
                            owner.failed = True
                            raise RuntimeError("injected post-side-effect commit failure")
                        transaction.transition_execution(expected_revision, execution, **kwargs)

                yield Proxy()

    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (
            FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
            FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
        ),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        chain._repository = FailNextSuccessTransition(repository)  # type: ignore[assignment,attr-defined]
        result = _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "ZCAL_AMBIGUOUS"  # type: ignore[attr-defined]
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.kind is MachineMotionEvidenceKind.COMMIT_UNCERTAIN
        assert motion.last_step is MachineMotionStep.COMMIT_AFTER_SIDE_EFFECT
        assert motion.acknowledged_steps == 6
        assert motion.snapshot is not None and motion.postcondition_proven is True
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CALIBRATION) == 1
    finally:
        repository.close()


@pytest.mark.parametrize("outcome_kind", ["wrong-type", "missing-snapshot", "incomplete-snapshot"])
def test_zcal_requires_exact_complete_idle_outcome_before_advancing(
    tmp_path: Path,
    outcome_kind: str,
) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        _run_action(chain, repository, HomeMachineCommand, execution_id, "home")
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                ZCalMachineCommand("request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        admitted = chain.admit_action(
            ZCalMachineCommand("execute", OPERATOR, execution_id, MachineCommandMode.EXECUTE, _challenge_id(requested))
        )
        if outcome_kind == "wrong-type":
            outcome: object = object()
            expected = "ZCAL_OUTCOME_INVALID"
        elif outcome_kind == "missing-snapshot":
            outcome = ZCalibrationOutcome(epoch, _zcal_request(), 6, None)
            expected = "MOTION_POSTCONDITION_INVALID"
        else:
            incomplete = parse_preflight_snapshot(
                status_line="<Idle|MPos:192.000,192.000,96.000|WPos:96.000,96.000,0.000|WCO:96.000,96.000,96.000>",
                parser_state=("[GC:G0 G54 G17 G21 G90]",),
                offset_lines=(),
                errors=(),
                missing_ok_commands=(),
            )
            outcome = ZCalibrationOutcome(epoch, _zcal_request(), 6, incomplete)
            expected = "MOTION_POSTCONDITION_INVALID"
        probe = _ZCalProbeSession(epoch, outcome)
        chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence.failure.code == expected
        motion = result.recovery_evidence.motion
        assert motion is not None
        assert motion.action is MachineAction.ZCAL
        assert motion.kind is MachineMotionEvidenceKind.INVALID_OUTCOME
        assert motion.acknowledged_steps is None and motion.snapshot is None
        assert motion.last_step is MachineMotionStep.UNKNOWN
        assert motion.postcondition_proven is False
        assert probe.calls == 1
        assert factory.started_operations.count(FakeFluidNCOperation.Z_CALIBRATION) == 0
    finally:
        repository.close()
