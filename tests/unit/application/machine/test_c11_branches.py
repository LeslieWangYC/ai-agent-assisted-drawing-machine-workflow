from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from drawingmachine.application.machine.commands import MachineCommandMode, StreamMachineCommand
from drawingmachine.config.machine import MachineExecutionConfig
from drawingmachine.domain.machine import (
    MachineFailureEvidenceV1,
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
    HomeZAxisRequest,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressCallback,
)
from tests.unit.application.machine.test_admission import AUTOMATION, _uuid
from tests.unit.application.machine.test_home import _snapshot
from tests.unit.application.machine.test_home_z import (
    _admit_home_z,
    _home_z_success_script,
)
from tests.unit.application.machine.test_stream import (
    _admit_stream,
    _execute_stream,
    _prepare_to_stream,
    _program,
    _progress,
    _stream_script,
)


class _ProbeStreamSession:
    def __init__(
        self,
        epoch: str,
        behavior: Callable[[StreamProgramRequest, StreamProgressCallback], object],
        *,
        close: object | BaseException | None = None,
    ) -> None:
        self.epoch = epoch
        self.behavior = behavior
        self.close_result = CloseOutcome(epoch) if close is None else close
        self.stream_calls = 0
        self.close_calls = 0

    def stream_program(self, request: StreamProgramRequest, progress: StreamProgressCallback) -> object:
        self.stream_calls += 1
        return self.behavior(request, progress)

    def close(self, request: CloseSessionRequest) -> object:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        if isinstance(self.close_result, BaseException):
            raise self.close_result
        return self.close_result


class _ProbeHomeZSession:
    def __init__(self, epoch: str, outcome: object | BaseException) -> None:
        self.epoch = epoch
        self.outcome = outcome
        self.home_z_calls = 0
        self.close_calls = 0

    def home_z_axis(self, request: HomeZAxisRequest) -> object:
        assert request == HomeZAxisRequest()
        self.home_z_calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        assert request == CloseSessionRequest(self.epoch)
        self.close_calls += 1
        return CloseOutcome(self.epoch)


def _success(epoch: str) -> StreamOutcome:
    program = _program()
    return StreamOutcome(
        epoch,
        len(program.commands),
        len(program.commands),
        _snapshot(
            mpos=(192.0, 192.0, 99.5),
            wpos=(96.0, 96.0, 3.5),
            wco=(96.0, 96.0, 96.0),
            g54=(96.0, 96.0, 96.0),
        ),
    )


def _admitted_with_probe(
    tmp_path: Path,
    behavior: Callable[[StreamProgramRequest, StreamProgressCallback], object],
) -> tuple[object, object, str, object, _ProbeStreamSession]:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-branch")
    probe = _ProbeStreamSession(epoch, behavior)
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    return chain, repository, execution_id, admitted, probe


def test_progress_projection_handles_unknown_empty_and_durable_reload(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    try:
        assert chain.progress_projection("missing") is None
        assert chain.progress_projection(execution_id) is None
        _execute_stream(chain, execution_id, "stream-progress-projection")
        projected = chain.progress_projection(execution_id)
        assert projected is not None and projected["acknowledged"] == len(_program().commands)
        chain._progress.clear()  # type: ignore[attr-defined]
        assert chain.progress_projection(execution_id) == projected
    finally:
        repository.close()


def test_stream_challenge_rejects_changed_calibrated_pose_before_consumption(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    try:
        evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
        chain._session_evidence[execution_id] = replace(evidence, wpos=(0.0, 0.0, 0.0))  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="calibrated pose"):
            chain.admit_action(
                StreamMachineCommand("unsafe-pose", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
            )
    finally:
        repository.close()


@pytest.mark.parametrize("kind", ["type", "sequence", "digest"])
def test_stream_rejects_invalid_progress_callbacks_and_never_sends_a_second_program(
    tmp_path: Path,
    kind: str,
) -> None:
    epoch = _uuid(2)
    valid = _progress(_program(), epoch, 1)[0]

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        if kind == "type":
            callback(object())  # type: ignore[arg-type]
        elif kind == "sequence":
            callback(replace(valid, acknowledged_commands=2))
        else:
            callback(replace(valid, command_digest="0" * 64))
        raise AssertionError("invalid progress must stop dispatch")

    chain, repository, _execution_id, admitted, probe = _admitted_with_probe(tmp_path, behavior)
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
        assert probe.stream_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize("kind", ["wrong-type", "ack-mismatch", "raw-exception"])
def test_stream_invalid_or_ambiguous_outcome_is_release_only(
    tmp_path: Path,
    kind: str,
) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        if kind == "wrong-type":
            return object()
        if kind == "raw-exception":
            raise RuntimeError("raw adapter failure")
        callback(_progress(program, epoch, 1)[0])
        return StreamOutcome(
            epoch,
            len(program.commands),
            0,
            None,
            # The callback/outcome contradiction is the boundary under test.
            failure=_failure(),
        )

    chain, repository, _execution_id, admitted, probe = _admitted_with_probe(tmp_path, behavior)
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        assert probe.stream_calls == 1
    finally:
        repository.close()


def _failure():
    from drawingmachine.ports.fluidnc import FluidNCFailure, FluidNCFailureKind

    return FluidNCFailure(
        FluidNCFailureKind.LOST_ACK,
        "STREAM_ACK_LOST",
        "acknowledgement was lost",
        True,
    )


def test_stream_success_with_incomplete_final_position_proof_enters_post_stream_recovery(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    incomplete = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    incomplete = replace(incomplete, offset_lines=(), offsets=())

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return StreamOutcome(epoch, len(program.commands), len(program.commands), incomplete)

    chain, repository, _execution_id, admitted, _probe = _admitted_with_probe(tmp_path, behavior)
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
    finally:
        repository.close()


def test_reusing_a_driven_stream_capability_is_stale_and_cannot_dispatch_again(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-once")
    try:
        assert chain.drive_admitted_action(admitted).phase is MachinePhase.COMPLETED
        with pytest.raises(DrawingMachineError, match="changed before dispatch"):
            chain._drive_stream(admitted)  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_stream_missing_same_session_is_pre_stream_recovery_without_adapter_call(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-missing-session")
    chain._sessions.pop(execution_id)  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.NOT_STARTED
        assert result.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
    finally:
        repository.close()


def test_stream_missing_session_evidence_closes_owned_handle_before_pre_stream_recovery(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-missing-evidence")
    probe = _ProbeStreamSession(epoch, lambda _request, _progress: pytest.fail("STREAM must not dispatch"))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    chain._session_evidence.pop(execution_id)  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert probe.stream_calls == 0
        assert probe.close_calls == 1
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_stream_pose_change_after_admission_recovers_before_adapter_dispatch(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-late-pose")
    probe = _ProbeStreamSession(epoch, lambda _request, _progress: pytest.fail("STREAM must not dispatch"))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    evidence = chain._session_evidence[execution_id]  # type: ignore[attr-defined]
    chain._session_evidence[execution_id] = replace(evidence, controller_state="Hold")  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.NOT_STARTED
        assert probe.stream_calls == 0 and probe.close_calls == 1
    finally:
        repository.close()


def test_confirmed_stream_close_failure_retains_handle_and_post_stream_evidence(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()

    def behavior(_request: StreamProgramRequest, callback: StreamProgressCallback) -> object:
        for update in _progress(program, epoch):
            callback(update)
        return _success(epoch)

    close_failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "STREAM_CLOSE_UNCERTAIN",
        "close was uncertain",
        True,
    )
    chain, repository, execution_id, admitted, _probe = _admitted_with_probe(tmp_path, behavior)
    probe = _ProbeStreamSession(epoch, behavior, close=CloseOutcome(epoch, close_failure))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert chain.retained_session_epochs() == (epoch,)
    finally:
        repository.close()


def _awaiting_home_z(tmp_path: Path) -> tuple[object, object, str]:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _home_z_success_script(epoch))
    config = chain._config  # type: ignore[attr-defined]
    assert type(config) is MachineExecutionConfig
    chain._config = replace(config, post_run_home_z=True)  # type: ignore[attr-defined]
    assert _execute_stream(chain, execution_id, "stream-for-home-z").phase is MachinePhase.AWAITING_HOME_Z_APPROVAL  # type: ignore[attr-defined]
    return chain, repository, execution_id


def test_home_z_wrong_runtime_outcome_type_is_closed_recovery_not_raw_exception(tmp_path: Path) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(_uuid(2), object())
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert probe.home_z_calls == 1
    finally:
        repository.close()


def test_home_z_missing_session_is_post_stream_recovery_without_dispatch(tmp_path: Path) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    chain._sessions.pop(execution_id)  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
    finally:
        repository.close()


def test_home_z_missing_session_evidence_closes_owned_handle_before_recovery(tmp_path: Path) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(_uuid(2), object())
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    chain._session_evidence.pop(execution_id)  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert probe.home_z_calls == 0
        assert probe.close_calls == 1
        assert execution_id not in chain._sessions  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_reusing_driven_home_z_capability_is_stale_and_never_dispatches_twice(tmp_path: Path) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    try:
        assert chain.drive_admitted_action(admitted).phase is MachinePhase.COMPLETED  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="changed before dispatch"):
            chain._drive_home_z(admitted)  # type: ignore[attr-defined]
    finally:
        repository.close()


class _ConcurrentExecutionProjection:
    def __init__(self, delegate: object, current: object, replacement: object) -> None:
        self.delegate = delegate
        self.current = current
        self.replacement = replacement
        self.calls = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def get_execution(self, execution_id: str) -> object:
        del execution_id
        self.calls += 1
        return self.current if self.calls == 1 else self.replacement


@pytest.mark.parametrize("concurrent", ["missing", "recovery", "terminal"])
def test_stream_adapter_exception_never_overwrites_concurrent_authority(
    tmp_path: Path,
    concurrent: str,
) -> None:
    def behavior(_request: StreamProgramRequest, _callback: StreamProgressCallback) -> object:
        raise RuntimeError("adapter failed")

    chain, repository, execution_id, admitted, probe = _admitted_with_probe(tmp_path, behavior)
    current = repository.get_execution(execution_id)
    assert current is not None
    replacement = {
        "missing": None,
        "recovery": SimpleNamespace(
            phase=MachinePhase.RECOVERY_REQUIRED,
            execution_id=execution_id,
            machine_session_epoch=_uuid(2),
        ),
        "terminal": SimpleNamespace(phase=MachinePhase.COMPLETED),
    }[concurrent]
    chain._repository = _ConcurrentExecutionProjection(repository, current, replacement)  # type: ignore[assignment,attr-defined]
    try:
        if concurrent == "recovery":
            result = chain.drive_admitted_action(admitted)
            assert result.phase is MachinePhase.RECOVERY_REQUIRED
        else:
            with pytest.raises(RuntimeError, match="adapter failed"):
                chain.drive_admitted_action(admitted)
        assert probe.stream_calls == 1
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.STREAMING
    finally:
        repository.close()


@pytest.mark.parametrize("concurrent", ["missing", "recovery", "terminal"])
def test_home_z_adapter_exception_never_overwrites_concurrent_authority(
    tmp_path: Path,
    concurrent: str,
) -> None:
    chain, repository, execution_id = _awaiting_home_z(tmp_path)
    admitted = _admit_home_z(chain, execution_id)
    probe = _ProbeHomeZSession(_uuid(2), RuntimeError("HOME_Z adapter failed"))
    chain._sessions[execution_id] = probe  # type: ignore[assignment,attr-defined]
    current = repository.get_execution(execution_id)
    assert current is not None
    replacement = {
        "missing": None,
        "recovery": SimpleNamespace(
            phase=MachinePhase.RECOVERY_REQUIRED,
            execution_id=execution_id,
            machine_session_epoch=_uuid(2),
        ),
        "terminal": SimpleNamespace(phase=MachinePhase.COMPLETED),
    }[concurrent]
    chain._repository = _ConcurrentExecutionProjection(repository, current, replacement)  # type: ignore[assignment,attr-defined]
    try:
        if concurrent == "recovery":
            result = chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
            assert result.phase is MachinePhase.RECOVERY_REQUIRED
        else:
            with pytest.raises(RuntimeError, match="HOME_Z adapter failed"):
                chain.drive_admitted_action(admitted)  # type: ignore[attr-defined]
        assert probe.home_z_calls == 1
        durable = repository.get_execution(execution_id)
        assert durable is not None and durable.phase is MachinePhase.HOMING_Z
    finally:
        repository.close()


def test_failed_progress_rejects_hostile_ack_prefix_beyond_complete_program(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _success(epoch)),
    )
    admitted = _admit_stream(chain, execution_id, "stream-hostile-progress-bound")
    current = repository.get_execution(execution_id)
    assert current is not None
    hostile = object.__new__(StreamOutcome)
    object.__setattr__(hostile, "acknowledged_commands", len(_program().commands) + 1)
    failure = MachineFailureEvidenceV1(1, "HOSTILE", "hostile progress", None, True)
    try:
        with pytest.raises(DrawingMachineError, match="outside the immutable program"):
            chain._persist_stream_failure_progress(  # type: ignore[attr-defined]
                current,
                admitted,
                failure,
                hostile,
                current.updated_at,
            )
    finally:
        repository.close()


def test_identical_failed_progress_checkpoint_is_idempotent(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    typed_failure = _failure()
    outcome = StreamOutcome(epoch, len(program.commands), 1, None, typed_failure)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, outcome, progress_count=1),
    )
    admitted = _admit_stream(chain, execution_id, "stream-idempotent-failure")
    try:
        result = chain.drive_admitted_action(admitted)
        persisted = repository.list_progress(execution_id)
        latest = persisted[-1]
        assert latest.failure is not None
        same_revision = replace(result, revision=latest.execution_revision)
        chain._persist_stream_failure_progress(  # type: ignore[attr-defined]
            same_revision,
            admitted,
            latest.failure,
            outcome,
            latest.last_event_at,
        )
        assert repository.list_progress(execution_id) == persisted
    finally:
        repository.close()
