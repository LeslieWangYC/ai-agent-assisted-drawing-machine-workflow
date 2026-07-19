from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep
from drawingmachine.adapters.hardware.serial_fluidnc import SerialFluidNCFactory
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    PrepareMachineCommand,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineExecutionChain
from drawingmachine.domain.machine import MachineMotionStep, MachinePhase
from drawingmachine.ports.fluidnc import ZCalibrationOutcomeStage
from tests.unit.adapters.hardware.test_serial_fluidnc import FakeSerial, StepClock, _preflight_reads
from tests.unit.application.machine.test_admission import (
    AUTOMATION,
    NOW,
    Reader,
    _authority,
    _config,
    _identities,
    _job,
    _ready_snapshot,
    _seed,
    _uuid,
)
from tests.unit.application.machine.test_home import (
    _chain,
    _home_outcome,
    _home_request,
    _run_action,
    _zcal_outcome,
    _zcal_request,
    _zconfirm_outcome,
    _zconfirm_request,
)


def test_strict_fake_runs_prepare_home_zcal_zconfirm_on_one_session(tmp_path: Path) -> None:
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
        assert factory.connections_opened == 1
        assert factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.HOME_ALL_AXES,
            FakeFluidNCOperation.Z_CALIBRATION,
            FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
        )
        assert tuple(call.request for call in factory.calls[1:]) == (
            _home_request(),
            _zcal_request(),
            _zconfirm_request(),
        )
    finally:
        repository.close()


def test_fake_chain_never_exposes_raw_unlock_jog_or_offset_operations() -> None:
    assert {operation.value for operation in FakeFluidNCOperation}.isdisjoint(
        {"WRITE", "RAW", "UNLOCK", "JOG", "OFFSET", "RESUME"}
    )


@pytest.mark.parametrize(
    ("step_index", "expected_stage", "expected_step"),
    (
        (0, ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED, MachineMotionStep.ZCAL_METRIC_ACKNOWLEDGED),
        (1, ZCalibrationOutcomeStage.ABSOLUTE_ACKNOWLEDGED, MachineMotionStep.ZCAL_ABSOLUTE_ACKNOWLEDGED),
        (
            2,
            ZCalibrationOutcomeStage.WORK_COORDINATE_ACKNOWLEDGED,
            MachineMotionStep.ZCAL_WORK_COORDINATE_ACKNOWLEDGED,
        ),
        (3, ZCalibrationOutcomeStage.PEN_UP_ACKNOWLEDGED, MachineMotionStep.ZCAL_PEN_UP_ACKNOWLEDGED),
        (4, ZCalibrationOutcomeStage.CENTER_ACKNOWLEDGED, MachineMotionStep.ZCAL_CENTER_ACKNOWLEDGED),
        (5, ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED, MachineMotionStep.ZCAL_CONTACT_ACKNOWLEDGED),
    ),
)
def test_serial_post_ack_stage_persists_through_application_sqlite_immutably(
    tmp_path: Path,
    step_index: int,
    expected_stage: ZCalibrationOutcomeStage,
    expected_step: MachineMotionStep,
) -> None:
    reset = b"watchdog reset\n"
    fake = FakeSerial(
        [
            b"",
            *_preflight_reads("<Idle|MPos:0.000,0.000,0.000|WPos:0.000,0.000,0.000|WCO:0.000,0.000,0.000>"),
            b"ok\n",
            *_preflight_reads("<Idle|MPos:192.000,192.000,192.000|WPos:192.000,192.000,192.000|WCO:0.000,0.000,0.000>"),
            *(b"ok\n" for _ in range(step_index)),
        ]
    )

    def ack_with_pending_reset() -> bytes:
        fake.post_ack_waiting = len(reset)
        return b"ok\n"

    fake.reads.extend((ack_with_pending_reset, reset))
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    values = _identities()
    chain = MachineExecutionChain(
        repository,
        Reader(),
        SerialFluidNCFactory(_config(), serial_factory=lambda: fake, monotonic=StepClock(0.01)),
        _config(),
        _authority(),
        identity_factory=lambda: next(values),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    try:
        prepared = chain.admit_action(PrepareMachineCommand("serial-prepare", AUTOMATION, "job-c9", 7))
        execution = chain.drive_admitted_action(prepared)
        assert execution.phase is MachinePhase.AWAITING_HOME_APPROVAL, execution.recovery_evidence.failure  # type: ignore[union-attr]
        _run_action(chain, repository, HomeMachineCommand, execution.execution_id, f"serial-home-{step_index}")
        result = _run_action(
            chain,
            repository,
            ZCalMachineCommand,
            execution.execution_id,
            f"serial-zcal-{step_index}",
        )
        motion = result.recovery_evidence.motion  # type: ignore[attr-defined]
        assert motion is not None
        assert motion.acknowledged_steps == step_index + 1
        assert motion.last_step is expected_step
        persisted = repository.get_execution(execution.execution_id)
        assert persisted is not None and persisted.recovery_evidence is not None
        assert persisted.recovery_evidence.motion == motion
        assert expected_stage.value.replace("_ACKNOWLEDGED", "") in motion.last_step.value
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            repository._get_connection().execute(
                "UPDATE machine_executions SET recovery_evidence_json = '{}' WHERE execution_id = ?",
                (execution.execution_id,),
            )
    finally:
        repository.close()
