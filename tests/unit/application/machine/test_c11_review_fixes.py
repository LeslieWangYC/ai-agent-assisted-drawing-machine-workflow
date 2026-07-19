from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import drawingmachine.application.machine.execution as execution_module
from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    MachineCommandMode,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.config.machine import MachineExecutionConfig
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachinePhase,
    MachineSessionSnapshot,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    HomeZAxisRequest,
    HomeZOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressCallback,
)
from tests.unit.application.machine.test_admission import (
    AUTOMATION,
    ProbeFactory,
    ProbeSession,
    _late_reset_preflight,
    _uuid,
)
from tests.unit.application.machine.test_admission import (
    _chain as _prepare_chain,
)
from tests.unit.application.machine.test_admission import (
    _command as _prepare_command,
)
from tests.unit.application.machine.test_c11_branches import (
    _admitted_with_probe,
    _awaiting_home_z,
    _failure,
    _ProbeHomeZSession,
    _ProbeStreamSession,
    _success,
)
from tests.unit.application.machine.test_home import (
    OPERATOR,
    _home_outcome,
    _home_request,
    _run_action,
    _snapshot,
    _zcal_outcome,
    _zcal_request,
)
from tests.unit.application.machine.test_home import (
    _chain as _motion_chain,
)
from tests.unit.application.machine.test_home import (
    _challenge_id as _motion_challenge_id,
)
from tests.unit.application.machine.test_home_z import _admit_home_z
from tests.unit.application.machine.test_recovery import (
    _chain as _recovery_chain,
)
from tests.unit.application.machine.test_recovery import (
    _challenge_id as _recovery_challenge_id,
)
from tests.unit.application.machine.test_recovery import (
    _recover,
)
from tests.unit.application.machine.test_stream import (
    _admit_stream,
    _prepare_to_stream,
    _program,
    _progress,
    _stream_script,
)


def _forged_stream_outcome(epoch: str, *, state: str = "Hold") -> StreamOutcome:
    program = _program()
    forged = object.__new__(StreamOutcome)
    object.__setattr__(forged, "machine_session_epoch", epoch)
    object.__setattr__(forged, "total_commands", len(program.commands))
    object.__setattr__(forged, "acknowledged_commands", len(program.commands))
    object.__setattr__(
        forged,
        "final_snapshot",
        _snapshot(
            mpos=(192.0, 192.0, 99.5),
            wpos=(96.0, 96.0, 3.5),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
            state=state,
        ),
    )
    object.__setattr__(forged, "failure", None)
    object.__setattr__(forged, "evidence", None)
    object.__setattr__(forged, "observation_phase", None)
    object.__setattr__(forged, "decision", None)
    return forged


def _forged_home_z_outcome(epoch: str, *, state: str = "Hold") -> HomeZOutcome:
    forged = object.__new__(HomeZOutcome)
    object.__setattr__(forged, "machine_session_epoch", epoch)
    object.__setattr__(forged, "request", HomeZAxisRequest())
    object.__setattr__(
        forged,
        "snapshot",
        _snapshot(
            mpos=(192.0, 192.0, 192.0),
            wpos=(96.0, 96.0, 96.0),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
            state=state,
        ),
    )
    object.__setattr__(forged, "failure", None)
    object.__setattr__(forged, "evidence", None)
    object.__setattr__(forged, "observation_phase", None)
    object.__setattr__(forged, "decision", None)
    return forged


def test_f1_forged_exact_stream_outcome_with_hold_cannot_complete(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return _forged_stream_outcome(epoch)

    chain, repository, _execution_id, admitted, probe = _admitted_with_probe(tmp_path, behavior)
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
        assert probe.stream_calls == 1
    finally:
        repository.close()


def test_f1_forged_exact_home_z_outcome_with_hold_cannot_complete(tmp_path: Path) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(_uuid(2), _forged_home_z_outcome(_uuid(2)))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert probe.home_z_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize(
    "contradiction",
    [
        "snapshot-type",
        "nested-type",
        "nested-snapshot",
        "failure-type",
        "success-and-failure",
        "binding-epoch",
        "binding-total",
    ],
)
def test_f1_forged_exact_stream_outcome_with_nested_contradiction_cannot_confirm(
    tmp_path: Path,
    contradiction: str,
) -> None:
    epoch = _uuid(2)
    program = _program()
    outcome = _success(epoch)
    if contradiction == "snapshot-type":
        object.__setattr__(outcome, "final_snapshot", object())
    elif contradiction == "nested-type":
        assert outcome.final_snapshot is not None
        object.__setattr__(outcome.final_snapshot, "parser_state", [])
    elif contradiction == "nested-snapshot":
        assert outcome.final_snapshot is not None
        object.__setattr__(outcome.final_snapshot, "offsets", ())
    elif contradiction == "failure-type":
        object.__setattr__(outcome, "failure", object())
    elif contradiction == "success-and-failure":
        object.__setattr__(outcome, "failure", _failure())
    elif contradiction == "binding-epoch":
        object.__setattr__(outcome, "machine_session_epoch", _uuid(999))
    else:
        object.__setattr__(outcome, "total_commands", len(program.commands) + 1)
        object.__setattr__(outcome, "acknowledged_commands", len(program.commands) + 1)

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return outcome

    chain, repository, _execution_id, admitted, probe = _admitted_with_probe(tmp_path, behavior)
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
        assert probe.stream_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize(
    "contradiction",
    [
        "snapshot-type",
        "nested-type",
        "nested-snapshot",
        "failure-type",
        "success-and-failure",
        "request-type",
        "binding-epoch",
    ],
)
def test_f1_forged_exact_home_z_outcome_with_nested_contradiction_cannot_complete(
    tmp_path: Path,
    contradiction: str,
) -> None:
    epoch = _uuid(2)
    snapshot = _snapshot(
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    outcome = HomeZOutcome(epoch, HomeZAxisRequest(), snapshot)
    if contradiction == "snapshot-type":
        object.__setattr__(outcome, "snapshot", object())
    elif contradiction == "nested-type":
        object.__setattr__(snapshot, "parser_state", [])
    elif contradiction == "nested-snapshot":
        object.__setattr__(snapshot, "offsets", ())
    elif contradiction == "failure-type":
        object.__setattr__(outcome, "failure", object())
    elif contradiction == "success-and-failure":
        object.__setattr__(outcome, "failure", _failure())
    elif contradiction == "request-type":
        object.__setattr__(outcome, "request", object())
    else:
        object.__setattr__(outcome, "machine_session_epoch", _uuid(999))
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(epoch, outcome)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert probe.home_z_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize(
    "drift",
    ["execution", "epoch", "unstabilized", "pose", "invalid-closed", "missing-fields", "equality-error"],
)
def test_f2_stream_drive_revalidates_complete_session_binding_before_milestone_or_dispatch(
    tmp_path: Path,
    drift: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _stream_script(epoch, _success(epoch)))
    admitted = _admit_stream(chain, execution_id, f"stream-drift-{drift}")
    probe = _ProbeStreamSession(epoch, lambda _request, _callback: pytest.fail("STREAM must not dispatch"))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
    if drift == "missing-fields":
        changed = object.__new__(MachineSessionSnapshot)
    elif drift == "equality-error":
        changed = replace(evidence)

        def reject_equality(_left: object, _right: object) -> bool:
            raise RuntimeError("hostile session equality secret")

        monkeypatch.setattr(MachineSessionSnapshot, "__eq__", reject_equality)
    elif drift == "invalid-closed":
        changed = replace(evidence)
        object.__setattr__(changed, "mpos", (1.0, 2.0))
    else:
        changed = {
            "execution": replace(evidence, execution_id="foreign-execution"),
            "epoch": replace(evidence, machine_session_epoch=_uuid(999)),
            "unstabilized": replace(evidence, stabilized=False),
            "pose": replace(evidence, wpos=(0.0, 0.0, 0.0)),
        }[drift]
    chain._session_evidence[execution_id] = changed  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.NOT_STARTED
        assert result.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
        assert probe.stream_calls == 0 and probe.close_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize(
    "drift",
    ["execution", "epoch", "unstabilized", "invalid-closed", "missing-fields", "equality-error"],
)
def test_f2_home_z_drive_revalidates_complete_session_binding_before_dispatch(
    tmp_path: Path,
    drift: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(_uuid(2), pytest.fail)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
    if drift == "missing-fields":
        changed = object.__new__(MachineSessionSnapshot)
    elif drift == "equality-error":
        changed = replace(evidence)

        def reject_equality(_left: object, _right: object) -> bool:
            raise RuntimeError("hostile session equality secret")

        monkeypatch.setattr(MachineSessionSnapshot, "__eq__", reject_equality)
    elif drift == "invalid-closed":
        changed = replace(evidence)
        object.__setattr__(changed, "mpos", (1.0, 2.0))
    else:
        changed = {
            "execution": replace(evidence, execution_id="foreign-execution"),
            "epoch": replace(evidence, machine_session_epoch=_uuid(999)),
            "unstabilized": replace(evidence, stabilized=False),
        }[drift]
    chain._session_evidence[execution_id] = changed  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert probe.home_z_calls == 0 and probe.close_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize("acknowledged", [0, 1, 17])
def test_f3_late_stream_callback_after_recovery_cannot_mutate_any_projection(
    tmp_path: Path,
    acknowledged: int,
) -> None:
    epoch = _uuid(2)
    program = _program()
    captured: list[StreamProgressCallback] = []

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        captured.append(callback)
        for update in _progress(program, epoch, acknowledged):
            callback(update)
        return StreamOutcome(epoch, len(program.commands), acknowledged, None, _failure())

    chain, repository, execution_id, admitted, _probe = _admitted_with_probe(tmp_path, behavior)
    try:
        assert chain.drive_admitted_action(admitted).phase is MachinePhase.RECOVERY_REQUIRED
        before_memory = chain.progress_projection(execution_id)
        before_sql = repository.list_progress(execution_id)
        next_update = _progress(program, epoch, acknowledged + 1)[-1]
        with pytest.raises(DrawingMachineError, match="expired"):
            captured[0](next_update)
        assert chain.progress_projection(execution_id) == before_memory
        assert repository.list_progress(execution_id) == before_sql
    finally:
        repository.close()


@pytest.mark.parametrize("terminal_phase", ["completed", "awaiting-home-z"])
def test_f3_late_stream_callback_after_success_cannot_mutate_memory_or_sqlite(
    tmp_path: Path,
    terminal_phase: str,
) -> None:
    epoch = _uuid(2)
    program = _program()
    captured: list[StreamProgressCallback] = []

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        captured.append(callback)
        for update in _progress(program, epoch):
            callback(update)
        return _success(epoch)

    chain, repository, execution_id, admitted, _probe = _admitted_with_probe(tmp_path, behavior)
    if terminal_phase == "awaiting-home-z":
        config = chain._config  # type: ignore[attr-defined]
        assert type(config) is MachineExecutionConfig
        chain._config = replace(config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        expected = MachinePhase.COMPLETED if terminal_phase == "completed" else MachinePhase.AWAITING_HOME_Z_APPROVAL
        assert result.phase is expected
        before_memory = chain.progress_projection(execution_id)
        before_sql = repository.list_progress(execution_id)
        with pytest.raises(DrawingMachineError) as expired:
            captured[0](_progress(program, epoch)[-1])
        assert expired.value.payload.code == "STREAM_PROGRESS_LIFETIME_EXPIRED"
        assert chain.progress_projection(execution_id) == before_memory
        assert repository.list_progress(execution_id) == before_sql
    finally:
        repository.close()


class _PersistentProgressAndRecoveryFailure:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    @contextmanager
    def transaction(self):
        with self.delegate.transaction() as transaction:  # type: ignore[attr-defined]

            class Proxy:
                def __getattr__(self, name: str):
                    return getattr(transaction, name)

                def append_progress(self, progress_id: str, progress: object) -> None:
                    del progress_id, progress
                    raise RuntimeError("persistent progress persistence failure")

                def commit_machine_outcome(self, expected_revision: int, execution: object, **kwargs: object) -> None:
                    del expected_revision, execution, kwargs
                    raise RuntimeError("persistent recovery persistence failure")

            yield Proxy()


class _PersistentOutcomeFailure:
    def __init__(self, delegate: object, *, fail_successor: bool = False) -> None:
        self.delegate = delegate
        self.fail_successor = fail_successor

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
                    if owner.fail_successor and execution.phase is MachinePhase.AWAITING_HOME_Z_APPROVAL:  # type: ignore[attr-defined]
                        raise RuntimeError("persistent STREAM successor persistence failure")
                    transaction.transition_execution(expected_revision, execution, **kwargs)

                def commit_machine_outcome(self, expected_revision: int, execution: object, **kwargs: object) -> None:
                    del expected_revision, execution, kwargs
                    raise RuntimeError("persistent machine outcome persistence failure")

            yield Proxy()


def _assert_closed_fatal(error: pytest.ExceptionInfo[DrawingMachineError]) -> None:
    assert error.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
    assert error.value.payload.message == (
        "machine persistence failed after possible hardware side effects; coordinator termination required"
    )
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_f4_irreversible_fatal_sanitizer_preserves_nonfatal_typed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _stream_script(epoch, _success(epoch)))
    admitted = _admit_stream(chain, execution_id, "stream-nonfatal-error")
    expected = DrawingMachineError(
        ErrorPayload("NON_FATAL_TYPED", ErrorCategory.INPUT, "fixed typed rejection", False, {})
    )

    def reject(_chain: object, _admission: object) -> object:
        raise expected

    monkeypatch.setattr(type(chain), "_drive_stream", reject)
    try:
        with pytest.raises(DrawingMachineError) as raised:
            chain.drive_admitted_action(admitted)
        assert raised.value is expected
    finally:
        repository.close()


def test_f4_persistent_progress_and_recovery_failure_closes_and_raises_closed_fatal(
    tmp_path: Path,
) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return _success(epoch)

    chain, repository, execution_id, admitted, _old_probe = _admitted_with_probe(tmp_path, behavior)
    probe = _ProbeStreamSession(epoch, behavior, close=CloseOutcome(epoch, _failure()))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    chain._repository = _PersistentProgressAndRecoveryFailure(repository)  # type: ignore[assignment,attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None
        assert durable.phase is MachinePhase.STREAMING
        assert durable.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert repository.list_progress(execution_id) == ()
        assert chain.progress_projection(execution_id) is None
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1
        assert chain.retained_session_epochs() == (epoch,)
    finally:
        repository.close()


def test_f4_persistent_stream_recovery_outcome_failure_closes_and_raises_closed_fatal(
    tmp_path: Path,
) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        del callback
        return StreamOutcome(epoch, len(program.commands), 0, None, _failure())

    chain, repository, execution_id, admitted, _old_probe = _admitted_with_probe(tmp_path, behavior)
    probe = _ProbeStreamSession(epoch, behavior)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[assignment,attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.STREAMING
        assert durable.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize("commit_kind", ["terminal", "successor"])
def test_f4_persistent_stream_confirmation_and_recovery_failures_raise_closed_fatal(
    tmp_path: Path,
    commit_kind: str,
) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return _success(epoch)

    chain, repository, execution_id, admitted, _old_probe = _admitted_with_probe(tmp_path, behavior)
    probe = _ProbeStreamSession(epoch, behavior)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    if commit_kind == "successor":
        config = chain._config  # type: ignore[attr-defined]
        assert type(config) is MachineExecutionConfig
        chain._config = replace(config, post_run_home_z=True)  # type: ignore[attr-defined]
    chain._repository = _PersistentOutcomeFailure(  # type: ignore[assignment,attr-defined]
        repository,
        fail_successor=commit_kind == "successor",
    )
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.STREAMING
        assert durable.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize("outcome_kind", ["recovery", "terminal"])
def test_f4_persistent_home_z_outcome_failure_closes_and_raises_closed_fatal(
    tmp_path: Path,
    outcome_kind: str,
) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    if outcome_kind == "recovery":
        outcome = HomeZOutcome(epoch, HomeZAxisRequest(), None, _failure())
    else:
        outcome = HomeZOutcome(
            epoch,
            HomeZAxisRequest(),
            _snapshot(
                mpos=(192.0, 192.0, 192.0),
                wpos=(96.0, 96.0, 96.0),
                wco=(96.0, 96.0, 96.0),
                g54=(96.0, 96.0, 96.0),
            ),
        )
    probe = _ProbeHomeZSession(epoch, outcome)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[assignment,attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.HOMING_Z
        assert durable.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1
    finally:
        repository.close()


def _spoofed_public_fatal_code_error() -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            "MACHINE_COORDINATOR_FATAL_PERSISTENCE",
            ErrorCategory.INTERNAL,
            "adapter spoof raw persistence secret",
            False,
            {"raw": "adapter-secret"},
        )
    )


@pytest.mark.parametrize("action", ["stream", "home-z"])
@pytest.mark.parametrize("recovery_persistence_fails", [False, True])
def test_f4_adapter_cannot_spoof_cleaned_coordinator_fatal_identity(
    tmp_path: Path,
    action: str,
    recovery_persistence_fails: bool,
) -> None:
    epoch = _uuid(2)
    spoof = _spoofed_public_fatal_code_error()
    if action == "stream":

        def behavior(_request: StreamProgramRequest, _callback: StreamProgressCallback) -> object:
            raise spoof

        chain, repository, execution_id, admitted, old_probe = _admitted_with_probe(tmp_path, behavior)
        probe: object = _ProbeStreamSession(epoch, behavior)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
        expected_durable_phase = MachinePhase.STREAMING
        expected_milestone = StreamMilestone.FIRST_WRITE_POSSIBLE
        expected_intent = RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
    else:
        chain, repository, execution_id = _awaiting_home_z(tmp_path)
        admitted = _admit_home_z(chain, execution_id)
        old_probe = None
        probe = _ProbeHomeZSession(epoch, spoof)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
        expected_durable_phase = MachinePhase.HOMING_Z
        expected_milestone = StreamMilestone.STREAM_CONFIRMED
        expected_intent = RecoveryIntent.POST_STREAM_SAFE_HOME
    del old_probe
    if recovery_persistence_fails:
        chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[attr-defined]
    try:
        if recovery_persistence_fails:
            with pytest.raises(DrawingMachineError) as fatal:
                chain.drive_admitted_action(admitted)
            _assert_closed_fatal(fatal)
            assert "adapter spoof" not in str(fatal.value)
            durable = repository.get_execution(execution_id)
            assert durable is not None and durable.phase is expected_durable_phase
            assert durable.stream_milestone is expected_milestone
        else:
            result = chain.drive_admitted_action(admitted)
            assert result.phase is MachinePhase.RECOVERY_REQUIRED
            assert result.stream_milestone is expected_milestone
            assert result.recovery_intent is expected_intent
            assert result.recovery_evidence is not None
            assert result.recovery_evidence.failure.code == "STREAM_OR_HOME_Z_UNCERTAIN"
            assert "adapter spoof" not in result.recovery_evidence.failure.message
            assert repository.get_execution(execution_id) == result
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        if action == "stream":
            assert probe.stream_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
        else:
            assert probe.home_z_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


def _hostile_internal_fatal_lookalike(kind: str) -> Exception:
    if kind == "subclass":

        class HostileSubclass(execution_module._CleanedCoordinatorFatal):
            def __init__(self) -> None:
                Exception.__init__(self, "hostile subclass raw secret")

        return HostileSubclass()
    if kind in {"forged-exact-missing-token", "forged-exact-wrong-token"}:
        signal_type = execution_module._CleanedCoordinatorFatal
        forged = signal_type.__new__(signal_type)
        Exception.__init__(forged, "hostile exact raw secret")
        if kind == "forged-exact-wrong-token":
            object.__setattr__(forged, "_CleanedCoordinatorFatal__token", object())
        return forged

    class TokenLookalike(Exception):
        pass

    lookalike = TokenLookalike("hostile token lookalike raw secret")
    object.__setattr__(lookalike, "_CleanedCoordinatorFatal__token", object())
    return lookalike


@pytest.mark.parametrize("action", ["stream", "home-z"])
@pytest.mark.parametrize(
    "kind",
    ["subclass", "forged-exact-missing-token", "forged-exact-wrong-token", "token-lookalike"],
)
def test_f4_hostile_internal_fatal_lookalikes_cannot_bypass_ambiguity_recovery(
    tmp_path: Path,
    action: str,
    kind: str,
) -> None:
    epoch = _uuid(2)
    hostile = _hostile_internal_fatal_lookalike(kind)
    if action == "stream":

        def behavior(_request: StreamProgramRequest, _callback: StreamProgressCallback) -> object:
            raise hostile

        chain, repository, execution_id, admitted, old_probe = _admitted_with_probe(tmp_path, behavior)
        probe: object = _ProbeStreamSession(epoch, behavior)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
        expected_milestone = StreamMilestone.FIRST_WRITE_POSSIBLE
        expected_intent = RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
    else:
        chain, repository, execution_id = _awaiting_home_z(tmp_path)
        admitted = _admit_home_z(chain, execution_id)
        old_probe = None
        probe = _ProbeHomeZSession(epoch, hostile)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
        expected_milestone = StreamMilestone.STREAM_CONFIRMED
        expected_intent = RecoveryIntent.POST_STREAM_SAFE_HOME
    del old_probe
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is expected_milestone
        assert result.recovery_intent is expected_intent
        assert result.recovery_evidence is not None
        assert result.recovery_evidence.failure.code == "STREAM_OR_HOME_Z_UNCERTAIN"
        assert "hostile" not in result.recovery_evidence.failure.message
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        if action == "stream":
            assert probe.stream_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
        else:
            assert probe.home_z_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_f4_cleaned_fatal_constructor_rejects_foreign_token() -> None:
    with pytest.raises(TypeError, match="application-private"):
        execution_module._CleanedCoordinatorFatal(object())


class _UncertainCloseHomeZSession(_ProbeHomeZSession):
    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        return CloseOutcome(self.epoch, _failure())


@pytest.mark.parametrize("action", ["stream", "home-z"])
def test_f4_spoof_recovery_retains_close_uncertain_session_exactly_once(
    tmp_path: Path,
    action: str,
) -> None:
    epoch = _uuid(2)
    spoof = _spoofed_public_fatal_code_error()
    if action == "stream":

        def behavior(_request: StreamProgramRequest, _callback: StreamProgressCallback) -> object:
            raise spoof

        chain, repository, execution_id, admitted, old_probe = _admitted_with_probe(tmp_path, behavior)
        probe: object = _ProbeStreamSession(epoch, behavior, close=CloseOutcome(epoch, _failure()))
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    else:
        chain, repository, execution_id = _awaiting_home_z(tmp_path)
        admitted = _admit_home_z(chain, execution_id)
        old_probe = None
        probe = _UncertainCloseHomeZSession(epoch, spoof)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    del old_probe
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert chain.retained_session_epochs() == (epoch,)
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


class _CloseVariantHomeZSession(_ProbeHomeZSession):
    def __init__(self, epoch: str, outcome: object | BaseException, close_result: object | BaseException) -> None:
        super().__init__(epoch, outcome)
        self.close_result = close_result

    def close(self, request: CloseSessionRequest) -> object:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        if isinstance(self.close_result, BaseException):
            raise self.close_result
        return self.close_result


class _LifecyclePersistenceFailure:
    def __init__(self, delegate: object, *, spoof_public_code: bool) -> None:
        self.delegate = delegate
        self.spoof_public_code = spoof_public_code

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    @contextmanager
    def transaction(self):
        with self.delegate.transaction() as transaction:  # type: ignore[attr-defined]
            owner = self

            class Proxy:
                def __getattr__(self, name: str):
                    return getattr(transaction, name)

                def record_lifecycle_event(self, execution: object, **kwargs: object) -> None:
                    del execution, kwargs
                    if owner.spoof_public_code:
                        raise _spoofed_public_fatal_code_error()
                    raise RuntimeError("raw retained lifecycle persistence secret")

            yield Proxy()


def _close_variant(epoch: str, kind: str) -> object | BaseException:
    if kind == "exception":
        return RuntimeError("raw close exception secret")
    if kind == "invalid-outcome":
        return object()
    if kind == "epoch-mismatch":
        return CloseOutcome(_uuid(999))
    return CloseOutcome(epoch, _failure())


def _admitted_close_variant(
    tmp_path: Path,
    action: str,
    close_kind: str,
) -> tuple[object, object, str, object, object]:
    epoch = _uuid(2)
    close_result = _close_variant(epoch, close_kind)
    adapter_error = RuntimeError("raw adapter ambiguity secret")
    if action == "stream":

        def behavior(_request: StreamProgramRequest, _callback: StreamProgressCallback) -> object:
            raise adapter_error

        chain, repository, execution_id, admitted, old_probe = _admitted_with_probe(tmp_path, behavior)
        del old_probe
        probe: object = _ProbeStreamSession(epoch, behavior, close=close_result)
    else:
        chain, repository, execution_id = _awaiting_home_z(tmp_path)
        admitted = _admit_home_z(chain, execution_id)
        probe = _CloseVariantHomeZSession(epoch, adapter_error, close_result)
    chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    return chain, repository, execution_id, admitted, probe


@pytest.mark.parametrize("action", ["stream", "home-z"])
@pytest.mark.parametrize("close_kind", ["exception", "invalid-outcome", "epoch-mismatch", "typed-failure"])
@pytest.mark.parametrize("lifecycle_error", ["raw", "public-code-spoof"])
def test_f4_retained_close_lifecycle_failure_raises_only_cleaned_fatal_without_losing_recovery(
    tmp_path: Path,
    action: str,
    close_kind: str,
    lifecycle_error: str,
) -> None:
    chain, repository, execution_id, admitted, probe = _admitted_close_variant(tmp_path, action, close_kind)
    chain._repository = _LifecyclePersistenceFailure(  # type: ignore[attr-defined]
        repository,
        spoof_public_code=lifecycle_error == "public-code-spoof",
    )
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        assert "raw" not in str(fatal.value)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.RECOVERY_REQUIRED
        expected_milestone = (
            StreamMilestone.FIRST_WRITE_POSSIBLE if action == "stream" else StreamMilestone.STREAM_CONFIRMED
        )
        expected_intent = (
            RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY if action == "stream" else RecoveryIntent.POST_STREAM_SAFE_HOME
        )
        assert durable.stream_milestone is expected_milestone
        assert durable.recovery_intent is expected_intent
        assert chain.retained_session_epochs() == (_uuid(2),)  # type: ignore[attr-defined]
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


@pytest.mark.parametrize("action", ["stream", "home-z"])
@pytest.mark.parametrize("close_kind", ["exception", "invalid-outcome", "epoch-mismatch", "typed-failure"])
def test_f4_retained_close_lifecycle_success_returns_normal_durable_recovery(
    tmp_path: Path,
    action: str,
    close_kind: str,
) -> None:
    chain, repository, execution_id, admitted, probe = _admitted_close_variant(tmp_path, action, close_kind)
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert repository.get_execution(execution_id) == result
        assert chain.retained_session_epochs() == (_uuid(2),)  # type: ignore[attr-defined]
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


class _MotionFailureCloseSession:
    def __init__(self, epoch: str, close_result: CloseOutcome | None = None) -> None:
        self.epoch = epoch
        self.close_result = close_result or CloseOutcome(epoch, _failure())
        self.motion_calls = 0
        self.close_calls = 0

    def home_all_axes(self, request: object) -> object:
        del request
        self.motion_calls += 1
        raise RuntimeError("raw HOME adapter ambiguity secret")

    def run_z_calibration(self, request: object) -> object:
        del request
        self.motion_calls += 1
        raise RuntimeError("raw ZCAL adapter ambiguity secret")

    def raise_after_z_confirmation(self, request: object) -> object:
        del request
        self.motion_calls += 1
        raise RuntimeError("raw ZCONFIRM adapter ambiguity secret")

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        return self.close_result


def _admit_motion_without_drive(chain: object, command_type: object, execution_id: str, request_id: str) -> object:
    requested = cast(
        MachineCommandResult,
        chain.admit_action(  # type: ignore[attr-defined]
            command_type(f"{request_id}-request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)  # type: ignore[operator]
        ),
    )
    return chain.admit_action(  # type: ignore[attr-defined]
        command_type(  # type: ignore[operator]
            f"{request_id}-execute",
            AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 700),
            execution_id,
            MachineCommandMode.EXECUTE,
            _motion_challenge_id(requested),
        )
    )


def test_f4_motion_recovery_persistence_failure_is_cleaned_before_public_fatal(
    tmp_path: Path,
) -> None:
    epoch = _uuid(2)
    chain, repository, _factory, execution_id = _motion_chain(tmp_path, ())
    admitted = _admit_motion_without_drive(chain, HomeMachineCommand, execution_id, "home-outcome-fatal")
    probe = _MotionFailureCloseSession(epoch)
    chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.HOMING
        assert durable.stream_milestone is StreamMilestone.NOT_STARTED
        assert durable.recovery_intent is None
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert chain.retained_session_epochs() == (epoch,)  # type: ignore[attr-defined]
        assert probe.motion_calls == 1 and probe.close_calls == 1
    finally:
        repository.close()


def _admitted_recovery_successor(
    tmp_path: Path,
    intent: RecoveryIntent,
    close_outcome: CloseOutcome,
) -> tuple[object, object, str, object, StrictFakeFluidNCFactory]:
    successor_epoch = close_outcome.machine_session_epoch
    factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _late_reset_preflight(successor_epoch),
            ),
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(successor_epoch),
                close_outcome,
            ),
        ),
    )
    chain, repository, _sequenced, _reader = _recovery_chain(tmp_path, intent, factories=(factory,))
    disposition = (
        RecoveryDisposition.RESTART_SEQUENCE
        if intent is RecoveryIntent.PRE_STREAM_RESTART
        else RecoveryDisposition.SAFE_HOME
    )
    requested = cast(
        MachineCommandResult,
        chain.admit_action(  # type: ignore[attr-defined]
            _recover(
                request_id=f"{intent.value}-outcome-fatal-request",
                requester=OPERATOR,
                disposition=disposition,
                mode=MachineCommandMode.REQUEST,
            )
        ),
    )
    admitted = chain.admit_action(  # type: ignore[attr-defined]
        _recover(
            request_id=f"{intent.value}-outcome-fatal-execute",
            requester=AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 701),
            disposition=disposition,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=_recovery_challenge_id(requested),
        )
    )
    return chain, repository, admitted.execution_id, admitted, factory


def _claimed_recovery_commit_failure(
    tmp_path: Path,
    action: str,
    close_kind: str,
) -> tuple[object, object, str, object, object, str]:
    epoch = _uuid(2) if not action.startswith("recover-") else _uuid(4)
    close_outcome = CloseOutcome(epoch) if close_kind == "success" else CloseOutcome(epoch, _failure())
    if action == "prepare":
        chain, repository, _factory, _reader = _prepare_chain(tmp_path)
        session = ProbeSession(_late_reset_preflight(epoch), close_outcome)
        probe = ProbeFactory(epoch, session)
        chain._fluidnc_factory = probe  # type: ignore[attr-defined]
        admitted = chain.admit_action(_prepare_command())  # type: ignore[attr-defined]
        execution_id = admitted.execution_id
    elif action.startswith("recover-"):
        intent = RecoveryIntent.PRE_STREAM_RESTART if action == "recover-pre" else RecoveryIntent.POST_STREAM_SAFE_HOME
        chain, repository, execution_id, admitted, probe = _admitted_recovery_successor(
            tmp_path,
            intent,
            close_outcome,
        )
    else:
        steps: list[FakeFluidNCStep] = []
        if action in {"zcal", "zconfirm"}:
            steps.append(FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)))
        if action == "zconfirm":
            steps.append(FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)))
        chain, repository, _factory, execution_id = _motion_chain(tmp_path, tuple(steps))
        if action in {"zcal", "zconfirm"}:
            _run_action(chain, repository, HomeMachineCommand, execution_id, f"{action}-fatal-prior-home")
        if action == "zconfirm":
            _run_action(chain, repository, ZCalMachineCommand, execution_id, "zconfirm-fatal-prior-zcal")
        command_type = {
            "home": HomeMachineCommand,
            "zcal": ZCalMachineCommand,
            "zconfirm": ZConfirmMachineCommand,
        }[action]
        admitted = _admit_motion_without_drive(chain, command_type, execution_id, f"{action}-outcome-fatal")
        probe = _MotionFailureCloseSession(epoch, close_outcome)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    chain._progress[execution_id] = object()  # type: ignore[attr-defined]
    chain._pending_progress[execution_id] = object()  # type: ignore[attr-defined]
    chain._last_progress_checkpoint[execution_id] = (17, 1001.0)  # type: ignore[attr-defined]
    chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[attr-defined]
    return chain, repository, execution_id, admitted, probe, epoch


@pytest.mark.parametrize(
    "action",
    ["prepare", "recover-pre", "recover-post", "home", "zcal", "zconfirm"],
)
@pytest.mark.parametrize("close_kind", ["success", "uncertain"])
def test_f4_preflight_and_motion_recovery_persistence_failure_share_cleaned_fatal_boundary(
    tmp_path: Path,
    action: str,
    close_kind: str,
) -> None:
    chain, repository, execution_id, admitted, probe, epoch = _claimed_recovery_commit_failure(
        tmp_path,
        action,
        close_kind,
    )
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None
        expected_phase = {
            "prepare": MachinePhase.PREPARING_SESSION,
            "recover-pre": MachinePhase.PREPARING_SESSION,
            "recover-post": MachinePhase.PREPARING_SESSION,
            "home": MachinePhase.HOMING,
            "zcal": MachinePhase.Z_CALIBRATING,
            "zconfirm": MachinePhase.Z_CONFIRMING,
        }[action]
        expected_milestone = (
            StreamMilestone.STREAM_CONFIRMED if action == "recover-post" else StreamMilestone.NOT_STARTED
        )
        expected_intent = (
            RecoveryIntent.POST_STREAM_SAFE_HOME
            if action == "recover-post"
            else RecoveryIntent.PRE_STREAM_RESTART
            if action == "recover-pre"
            else None
        )
        assert durable.phase is expected_phase
        assert durable.stream_milestone is expected_milestone
        assert durable.recovery_intent is expected_intent
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert execution_id not in chain._progress  # type: ignore[attr-defined]
        assert execution_id not in chain._pending_progress  # type: ignore[attr-defined]
        assert execution_id not in chain._last_progress_checkpoint  # type: ignore[attr-defined]
        retained = (epoch,) if close_kind == "uncertain" else ()
        assert chain.retained_session_epochs() == retained  # type: ignore[attr-defined]
        if action == "prepare":
            assert probe.opens == 1 and probe.session.close_calls == 1  # type: ignore[attr-defined]
        elif action.startswith("recover-"):
            assert probe.started_operations.count(FakeFluidNCOperation.INITIAL_PREFLIGHT) == 1  # type: ignore[attr-defined]
            assert probe.started_operations.count(FakeFluidNCOperation.CLOSE) == 1  # type: ignore[attr-defined]
        else:
            assert probe.motion_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_f4_motion_recovery_persistence_failure_without_session_still_cleans_maps(
    tmp_path: Path,
) -> None:
    chain, repository, factory, execution_id = _motion_chain(tmp_path, ())
    admitted = _admit_motion_without_drive(chain, HomeMachineCommand, execution_id, "home-no-session-fatal")
    chain._sessions.pop(execution_id)  # type: ignore[attr-defined]
    chain._session_evidence.pop(execution_id)  # type: ignore[attr-defined]
    chain._progress[execution_id] = object()  # type: ignore[attr-defined]
    chain._pending_progress[execution_id] = object()  # type: ignore[attr-defined]
    chain._last_progress_checkpoint[execution_id] = (17, 1001.0)  # type: ignore[attr-defined]
    chain._repository = _PersistentOutcomeFailure(repository)  # type: ignore[attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.HOMING
        assert durable.stream_milestone is StreamMilestone.NOT_STARTED
        assert durable.recovery_intent is None
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert execution_id not in chain._progress  # type: ignore[attr-defined]
        assert execution_id not in chain._pending_progress  # type: ignore[attr-defined]
        assert execution_id not in chain._last_progress_checkpoint  # type: ignore[attr-defined]
        assert chain.retained_session_epochs() == ()  # type: ignore[attr-defined]
        assert FakeFluidNCOperation.HOME_ALL_AXES not in factory.started_operations
        assert FakeFluidNCOperation.CLOSE not in factory.started_operations
    finally:
        repository.close()


def _claimed_drive_with_retained_lifecycle_failure(
    tmp_path: Path,
    action: str,
) -> tuple[object, object, str, object, object]:
    epoch = _uuid(2)
    if action == "prepare":
        chain, repository, _factory, _reader = _prepare_chain(tmp_path)
        probe = ProbeSession(_late_reset_preflight(epoch), CloseOutcome(epoch, _failure()))
        chain._fluidnc_factory = ProbeFactory(epoch, probe)  # type: ignore[attr-defined]
        admitted = chain.admit_action(_prepare_command())
        execution_id = admitted.execution_id
    elif action == "recover":
        successor_epoch = _uuid(4)
        factory = StrictFakeFluidNCFactory(
            OpenSessionRequest(successor_epoch),
            (
                FakeFluidNCStep(
                    FakeFluidNCOperation.INITIAL_PREFLIGHT,
                    InitialPreflightRequest(),
                    _late_reset_preflight(successor_epoch),
                ),
                FakeFluidNCStep(
                    FakeFluidNCOperation.CLOSE,
                    CloseSessionRequest(successor_epoch),
                    CloseOutcome(successor_epoch, _failure()),
                ),
            ),
        )
        chain, repository, _sequenced, _reader = _recovery_chain(
            tmp_path,
            RecoveryIntent.PRE_STREAM_RESTART,
            factories=(factory,),
        )
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="recover-lifecycle-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            _recover(
                request_id="recover-lifecycle-execute",
                requester=OPERATOR,
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_recovery_challenge_id(requested),
            )
        )
        execution_id = admitted.execution_id
        probe = factory
    else:
        steps: list[FakeFluidNCStep] = []
        if action in {"zcal", "zconfirm"}:
            steps.append(FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)))
        if action == "zconfirm":
            steps.append(FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, _zcal_request(), _zcal_outcome(epoch)))
        chain, repository, _factory, execution_id = _motion_chain(tmp_path, tuple(steps))
        if action in {"zcal", "zconfirm"}:
            _run_action(chain, repository, HomeMachineCommand, execution_id, f"{action}-prior-home")
        if action == "zconfirm":
            _run_action(chain, repository, ZCalMachineCommand, execution_id, "zconfirm-prior-zcal")
        command_type = {
            "home": HomeMachineCommand,
            "zcal": ZCalMachineCommand,
            "zconfirm": ZConfirmMachineCommand,
        }[action]
        admitted = _admit_motion_without_drive(chain, command_type, execution_id, f"{action}-lifecycle")
        probe = _MotionFailureCloseSession(epoch)
        chain._sessions[execution_id] = probe  # type: ignore[attr-defined]
    chain._repository = _LifecyclePersistenceFailure(repository, spoof_public_code=False)  # type: ignore[attr-defined]
    return chain, repository, execution_id, admitted, probe


@pytest.mark.parametrize("action", ["prepare", "recover", "home", "zcal", "zconfirm"])
def test_f4_all_claimed_side_effect_drives_sanitize_retained_lifecycle_failure(
    tmp_path: Path,
    action: str,
) -> None:
    chain, repository, execution_id, admitted, probe = _claimed_drive_with_retained_lifecycle_failure(
        tmp_path,
        action,
    )
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        _assert_closed_fatal(fatal)
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.RECOVERY_REQUIRED
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
        assert execution_id not in chain._session_evidence  # type: ignore[attr-defined]
        assert len(chain.retained_session_epochs()) == 1  # type: ignore[attr-defined]
        if action == "prepare":
            assert probe.close_calls == 1  # type: ignore[attr-defined]
        elif action == "recover":
            assert probe.started_operations.count(FakeFluidNCOperation.CLOSE) == 1  # type: ignore[attr-defined]
        else:
            assert probe.motion_calls == 1 and probe.close_calls == 1  # type: ignore[attr-defined]
    finally:
        repository.close()


@pytest.mark.parametrize("action", ["prepare", "recover", "home", "zcal", "zconfirm", "stream", "home-z"])
def test_f4_unified_claimed_drive_sanitizer_preserves_each_nonfatal_typed_error(
    tmp_path: Path,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if action in {"prepare", "recover", "home", "zcal", "zconfirm"}:
        chain, repository, _execution_id, admitted, _probe = _claimed_drive_with_retained_lifecycle_failure(
            tmp_path,
            action,
        )
    elif action == "stream":
        epoch = _uuid(2)
        chain, repository, _execution_id, admitted, _probe = _admitted_with_probe(
            tmp_path,
            lambda _request, _callback: _success(epoch),
        )
    else:
        chain, repository, execution_id = _awaiting_home_z(tmp_path)
        admitted = _admit_home_z(chain, execution_id)
    action_code = action.upper().replace("-", "_")
    expected = DrawingMachineError(
        ErrorPayload(f"NON_FATAL_{action_code}", ErrorCategory.INPUT, "fixed ordinary typed error", False, {})
    )

    def reject(_chain: object, _admission: object) -> object:
        raise expected

    monkeypatch.setattr(type(chain), "_dispatch_claimed_action", reject)
    try:
        with pytest.raises(DrawingMachineError) as raised:
            chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert raised.value is expected
        assert raised.value.payload.code != "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
    finally:
        repository.close()
