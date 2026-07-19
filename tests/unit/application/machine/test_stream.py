from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    MachineCommandMode,
    StreamMachineCommand,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult, MachineExecutionChain
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.gcode.stream_program import ValidatedStreamProgram, build_validated_stream_program
from drawingmachine.domain.jobs import JobState
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachinePhase,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressUpdate,
)
from tests.unit.application.machine.test_admission import AUTOMATION, GCODE, Reader, _ready_snapshot, _uuid
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


def test_stream_command_is_closed_and_carries_no_resume_or_raw_authority() -> None:
    command = StreamMachineCommand(
        "stream-request",
        AuthenticatedMachinePrincipal(1200, 1200, 41001),
        "execution-c11",
        MachineCommandMode.REQUEST,
        None,
    )
    assert command.mode is MachineCommandMode.REQUEST
    assert {field.name for field in fields(command)} == {
        "request_id",
        "requester",
        "execution_id",
        "mode",
        "challenge_id",
    }


def _program() -> ValidatedStreamProgram:
    snapshot = _ready_snapshot()
    return build_validated_stream_program(GCODE, snapshot.to_json()["send_plan"], snapshot.gcode.sha256)


def _progress(
    program: ValidatedStreamProgram, epoch: str, count: int | None = None
) -> tuple[StreamProgressUpdate, ...]:
    accepted = len(program.commands) if count is None else count
    return tuple(
        StreamProgressUpdate(
            epoch,
            index,
            command.source_line,
            hashlib.sha256(command.command.encode("ascii")).hexdigest(),
        )
        for index, command in enumerate(program.commands[:accepted], start=1)
    )


def _stream_script(
    epoch: str,
    outcome: StreamOutcome,
    *,
    progress_count: int | None = None,
    close: bool = True,
) -> tuple[FakeFluidNCStep, ...]:
    program = _program()
    steps = (
        FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),
        FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)),
        FakeFluidNCStep(
            FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
            _zconfirm_request(),
            _zconfirm_outcome(epoch),
        ),
        FakeFluidNCStep(
            FakeFluidNCOperation.STREAM_PROGRAM,
            StreamProgramRequest(program),
            outcome,
            _progress(program, epoch, progress_count),
        ),
    )
    if not close:
        return steps
    return (
        *steps,
        FakeFluidNCStep(
            FakeFluidNCOperation.CLOSE,
            CloseSessionRequest(epoch),
            CloseOutcome(epoch),
        ),
    )


def _prepare_to_stream(
    tmp_path: Path,
    steps: tuple[FakeFluidNCStep, ...],
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, str]:
    chain, repository, _factory, execution_id = _chain(tmp_path, steps)
    _run_action(chain, repository, HomeMachineCommand, execution_id, "home-c11")
    _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-c11")
    result = _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm-c11")
    assert result.phase is MachinePhase.AWAITING_STREAM_APPROVAL  # type: ignore[attr-defined]
    return chain, repository, execution_id


def _admit_stream(chain: MachineExecutionChain, execution_id: str, request_id: str = "stream-c11") -> object:
    requested = cast(
        MachineCommandResult,
        chain.admit_action(
            StreamMachineCommand(
                f"{request_id}-request",
                AUTOMATION,
                execution_id,
                MachineCommandMode.REQUEST,
                None,
            )
        ),
    )
    admitted = chain.admit_action(
        StreamMachineCommand(
            f"{request_id}-execute",
            AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 123),
            execution_id,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
    )
    durable = chain._repository.get_execution(execution_id)  # type: ignore[attr-defined]
    assert durable is not None
    assert durable.phase is MachinePhase.STREAMING
    assert durable.stream_milestone is StreamMilestone.NOT_STARTED
    return admitted


def _execute_stream(chain: MachineExecutionChain, execution_id: str, request_id: str = "stream-c11") -> object:
    return chain.drive_admitted_action(_admit_stream(chain, execution_id, request_id))


def test_stream_dispatches_complete_program_and_commits_ack_progress_then_completed(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    final = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    outcome = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _stream_script(epoch, outcome))
    try:
        result = _execute_stream(chain, execution_id)
        assert result.phase is MachinePhase.COMPLETED  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert repository.get_outcome(execution_id).kind.value == "COMPLETED"  # type: ignore[union-attr]
        assert repository._get_connection().execute("SELECT state FROM jobs").fetchone()[0] == JobState.COMPLETED.value
        checkpoints = repository.list_progress(execution_id)
        assert checkpoints[-1].acknowledged_lines == len(program.commands)
        assert checkpoints[-1].error_count == 0
        assert chain.progress_projection(execution_id)["acknowledged"] == len(program.commands)  # type: ignore[index]
    finally:
        repository.close()


def test_final_idle_proof_without_any_position_or_g54_evidence_still_rejects_postcondition(
    tmp_path: Path,
) -> None:
    # Defect #17 guard-still-fires check: `_machine_session_snapshot_from_preflight` must still
    # reject a final Idle proof that carries no derivable position evidence at all (no MPos, no
    # WPos, no WCO, no G54) -- the derivation helper has nothing to complete positions from, so
    # the existing STREAM_POSTCONDITION_INVALID failure path must remain intact after the fix.
    epoch = _uuid(2)
    program = _program()
    final = parse_preflight_snapshot(
        status_line="<Idle>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    outcome = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _stream_script(epoch, outcome))
    try:
        result = _execute_stream(chain, execution_id, "stream-no-position-evidence")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_evidence is not None  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "STREAM_POSTCONDITION_INVALID"  # type: ignore[attr-defined]
    finally:
        repository.close()


@pytest.mark.parametrize("acknowledged", [0, 1, 17])
def test_partial_stream_failure_is_release_only_and_never_retries(
    tmp_path: Path,
    acknowledged: int,
) -> None:
    epoch = _uuid(2)
    program = _program()
    failure = FluidNCFailure(
        FluidNCFailureKind.LOST_ACK,
        "STREAM_ACK_LOST",
        "acknowledgement was lost",
        True,
    )
    outcome = StreamOutcome(epoch, len(program.commands), acknowledged, None, failure)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, outcome, progress_count=acknowledged),
    )
    try:
        result = _execute_stream(chain, execution_id, f"stream-fail-{acknowledged}")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY  # type: ignore[attr-defined]
        assert repository.get_outcome(execution_id).kind.value == "RECOVERY_REQUIRED"  # type: ignore[union-attr]
        failed_progress = repository.list_progress(execution_id)[-1]
        failed_index = min(acknowledged, len(program.commands) - 1)
        assert failed_progress.acknowledged_lines == acknowledged
        assert failed_progress.error_count == 1
        assert failed_progress.last_source_line == program.commands[failed_index].source_line
        assert failed_progress.last_command == program.commands[failed_index].command
        assert failed_progress.failure is not None
        with pytest.raises(DrawingMachineError) as denied:
            chain.admit_action(
                StreamMachineCommand(
                    "stream-again",
                    AUTOMATION,
                    execution_id,
                    MachineCommandMode.REQUEST,
                    None,
                )
            )
        assert denied.value.payload.code == "MACHINE_ACTION_ORDER_INVALID"
    finally:
        repository.close()


def test_final_idle_failure_retains_all_acks_and_exact_final_command_failure(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    failure = FluidNCFailure(
        FluidNCFailureKind.TIMEOUT,
        "STREAM_IDLE_TIMEOUT",
        "final Idle proof timed out",
        True,
    )
    outcome = StreamOutcome(epoch, len(program.commands), len(program.commands), None, failure)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, outcome, progress_count=len(program.commands)),
    )
    try:
        result = _execute_stream(chain, execution_id, "stream-final-idle")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        latest = repository.list_progress(execution_id)[-1]
        assert latest.acknowledged_lines == len(program.commands)
        assert latest.error_count == 1
        assert latest.last_source_line == program.commands[-1].source_line
        assert latest.last_command == program.commands[-1].command
        assert latest.failure is not None and latest.failure.code == "STREAM_IDLE_TIMEOUT"
    finally:
        repository.close()


def test_stream_milestone_is_sqlite_irreversible_after_partial_write(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    outcome = StreamOutcome(
        epoch,
        len(program.commands),
        1,
        None,
        FluidNCFailure(FluidNCFailureKind.LOST_ACK, "STREAM_ACK_LOST", "ack lost", True),
    )
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, outcome, progress_count=1),
    )
    try:
        failed = _execute_stream(chain, execution_id, "stream-milestone")
        assert failed.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE  # type: ignore[attr-defined]
        with pytest.raises(sqlite3.IntegrityError, match="milestone cannot regress"):
            repository._get_connection().execute(
                """UPDATE machine_executions
                   SET stream_milestone = 'NOT_STARTED', revision = revision + 1
                   WHERE execution_id = ?""",
                (execution_id,),
            )
    finally:
        repository.close()


def test_stream_rereads_artifact_immediately_before_consuming_approval(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    final = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    outcome = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _stream_script(epoch, outcome))
    try:
        reader = cast(Reader, chain._artifact_reader)  # type: ignore[attr-defined]
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                StreamMachineCommand("stream-request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        calls_before_execute = reader.calls
        reader.data = GCODE.replace(b"G21", b"G20", 1)
        with pytest.raises(DrawingMachineError) as rejected:
            chain.admit_action(
                StreamMachineCommand(
                    "stream-execute",
                    OPERATOR,
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    _challenge_id(requested),
                )
            )
        assert rejected.value.payload.code == "MACHINE_GCODE_IDENTITY_MISMATCH"
        assert reader.calls == calls_before_execute + 1
        challenge = repository.get_challenge(_challenge_id(requested))
        assert challenge is not None and challenge.status.value == "PENDING"
    finally:
        repository.close()
