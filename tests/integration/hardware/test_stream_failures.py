from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from drawingmachine.application.machine.commands import HomeZMachineCommand, StreamMachineCommand
from drawingmachine.domain.fluidnc.gates import SessionObservationPhase, classify_session_observation
from drawingmachine.domain.machine import MachinePhase, RecoveryIntent, StreamMilestone
from drawingmachine.ports.fluidnc import FluidNCFailure, FluidNCFailureKind, OpenSessionRequest, StreamOutcome
from tests.unit.adapters.hardware.test_serial_fluidnc import FakeSerial, StepClock, _factory
from tests.unit.application.machine.test_admission import _uuid
from tests.unit.application.machine.test_home import _snapshot
from tests.unit.application.machine.test_stream import (
    _admit_stream,
    _execute_stream,
    _prepare_to_stream,
    _program,
    _stream_script,
)


def test_c11_closed_stream_and_home_z_commands_are_available() -> None:
    assert StreamMachineCommand is not HomeZMachineCommand


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (FluidNCFailureKind.CONTROLLER_ERROR, "STREAM_CONTROLLER_ERROR"),
        (FluidNCFailureKind.ALARM, "STREAM_ALARM"),
        (FluidNCFailureKind.TIMEOUT, "STREAM_ACK_TIMEOUT"),
        (FluidNCFailureKind.LOST_ACK, "STREAM_ACK_LOST"),
        (FluidNCFailureKind.TRANSPORT_LOSS, "STREAM_DISCONNECTED"),
        (FluidNCFailureKind.CONTROLLER_RESET, "STREAM_CONTROLLER_RESET"),
    ],
)
def test_every_typed_stream_failure_stops_at_zero_ack_and_is_release_only(
    tmp_path: Path,
    kind: FluidNCFailureKind,
    code: str,
) -> None:
    epoch = _uuid(2)
    program = _program()
    failure = FluidNCFailure(kind, code, "typed STREAM failure", True)
    if kind is FluidNCFailureKind.CONTROLLER_RESET:
        reset = _snapshot(
            mpos=(0.0, 0.0, 0.0),
            wpos=(0.0, 0.0, 0.0),
            wco=(0.0, 0.0, 0.0),
            g54=(0.0, 0.0, 0.0),
        )
        evidence = ("FluidNC reset after stabilized STREAM admission",)
        phase = SessionObservationPhase.ACTION_ADMITTED
        decision = classify_session_observation(evidence, reset.status, phase)
        outcome = StreamOutcome(
            epoch,
            len(program.commands),
            0,
            reset,
            failure,
            evidence,
            phase,
            decision,
        )
    else:
        outcome = StreamOutcome(epoch, len(program.commands), 0, None, failure)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, outcome, progress_count=0),
    )
    try:
        result = _execute_stream(chain, execution_id, f"stream-{kind.value.lower()}")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == code  # type: ignore[attr-defined]
        progress = repository.list_progress(execution_id)[-1]
        assert progress.acknowledged_lines == 0 and progress.error_count == 1
        assert progress.last_source_line == program.commands[0].source_line
    finally:
        repository.close()


def _successful_outcome(epoch: str) -> StreamOutcome:
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


class _FailSelectedProgress:
    def __init__(self, delegate: object, fail_on_append: int) -> None:
        self.delegate = delegate
        self.fail_on_append = fail_on_append
        self.append_calls = 0
        self.projection: Callable[[], object] | None = None
        self.projection_before_failure: object = object()

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    @contextmanager
    def transaction(self):
        with self.delegate.transaction() as transaction:  # type: ignore[attr-defined]
            owner = self

            class Proxy:
                def __getattr__(self, name: str):
                    return getattr(transaction, name)

                def append_progress(self, progress_id: str, progress: object) -> None:
                    owner.append_calls += 1
                    if owner.append_calls == owner.fail_on_append:
                        assert owner.projection is not None
                        owner.projection_before_failure = owner.projection()
                        raise RuntimeError("injected progress persistence failure")
                    transaction.append_progress(progress_id, progress)

            yield Proxy()


@pytest.mark.parametrize("checkpoint", ["first", "hundred", "final"])
def test_progress_persistence_failure_recovers_exact_private_observed_ack_without_early_projection(
    tmp_path: Path,
    checkpoint: str,
) -> None:

    epoch = _uuid(2)
    program = _program()
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _successful_outcome(epoch)),
    )
    target_ack = {"first": 1, "hundred": 100, "final": len(program.commands)}[checkpoint]
    fail_on_append = (target_ack - 1) // 100 + 1 if checkpoint == "final" else 1
    failing = _FailSelectedProgress(repository, fail_on_append)
    failing.projection = lambda: chain.progress_projection(execution_id)
    chain._repository = failing  # type: ignore[assignment,attr-defined]
    admitted = _admit_stream(chain, execution_id, f"stream-progress-{checkpoint}-commit-fail")
    if checkpoint == "first":
        ticks = iter((0.0, 3.0))
        chain._monotonic_now = lambda: next(ticks)  # type: ignore[attr-defined]
    else:
        chain._monotonic_now = lambda: 0.0  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE  # type: ignore[attr-defined]
        expected_public_ack = ((target_ack - 1) // 100) * 100 if checkpoint == "final" else None
        before = failing.projection_before_failure
        if expected_public_ack is None:
            assert before is None
        else:
            assert before["acknowledged"] == expected_public_ack  # type: ignore[index]
        latest = repository.list_progress(execution_id)[-1]
        assert latest.acknowledged_lines == target_ack
        assert latest.last_source_line == program.commands[target_ack - 1].source_line
        assert latest.last_command == program.commands[target_ack - 1].command
        assert latest.failure is not None
        assert chain.progress_projection(execution_id)["acknowledged"] == target_ack  # type: ignore[index]
    finally:
        repository.close()


@pytest.mark.parametrize("checkpoint", ["first", "hundred", "final"])
def test_serial_progress_persistence_failure_matches_fake_exact_private_ack_recovery(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    epoch = _uuid(2)
    program = _program()
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _successful_outcome(epoch)),
    )
    target_ack = {"first": 1, "hundred": 100, "final": len(program.commands)}[checkpoint]
    fake_serial = FakeSerial([b"", *(b"ok\n" for _ in range(target_ack))])
    serial_session = _factory(fake_serial, clock=StepClock(0.001)).open_session(OpenSessionRequest(epoch))
    chain._sessions[execution_id] = serial_session  # type: ignore[attr-defined]
    fail_on_append = (target_ack - 1) // 100 + 1 if checkpoint == "final" else 1
    failing = _FailSelectedProgress(repository, fail_on_append)
    failing.projection = lambda: chain.progress_projection(execution_id)
    chain._repository = failing  # type: ignore[assignment,attr-defined]
    admitted = _admit_stream(chain, execution_id, f"serial-progress-{checkpoint}-commit-fail")
    if checkpoint == "first":
        ticks = iter((0.0, 3.0))
        chain._monotonic_now = lambda: next(ticks)  # type: ignore[attr-defined]
    else:
        chain._monotonic_now = lambda: 0.0  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY  # type: ignore[attr-defined]
        expected_public_ack = ((target_ack - 1) // 100) * 100 if checkpoint == "final" else None
        before = failing.projection_before_failure
        if expected_public_ack is None:
            assert before is None
        else:
            assert before["acknowledged"] == expected_public_ack  # type: ignore[index]
        latest = repository.list_progress(execution_id)[-1]
        assert latest.acknowledged_lines == target_ack
        assert latest.last_source_line == program.commands[target_ack - 1].source_line
        assert latest.last_command == program.commands[target_ack - 1].command
        assert latest.failure is not None and latest.failure.code == "PROGRESS_CALLBACK_FAILED"
        assert fake_serial.close_count == 1
    finally:
        repository.close()


def test_final_completed_outcome_commit_failure_is_post_stream_recovery(tmp_path: Path) -> None:
    class FailCompletedOnce:
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

                    def commit_machine_outcome(
                        self, expected_revision: int, execution: object, **kwargs: object
                    ) -> None:
                        if not owner.failed and execution.phase is MachinePhase.COMPLETED:  # type: ignore[attr-defined]
                            owner.failed = True
                            raise RuntimeError("injected terminal commit failure")
                        transaction.commit_machine_outcome(expected_revision, execution, **kwargs)

                yield Proxy()

    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _successful_outcome(epoch)),
    )
    chain._repository = FailCompletedOnce(repository)  # type: ignore[assignment,attr-defined]
    try:
        result = _execute_stream(chain, execution_id, "stream-final-commit-fail")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "STREAM_COMPLETION_COMMIT_UNCERTAIN"  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_confirmed_stream_awaiting_home_z_commit_failure_is_closed_post_stream_recovery(tmp_path: Path) -> None:
    class FailAwaitingHomeZOnce:
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
                        if not owner.failed and execution.phase is MachinePhase.AWAITING_HOME_Z_APPROVAL:  # type: ignore[attr-defined]
                            owner.failed = True
                            raise RuntimeError("injected awaiting-HOME_Z commit failure")
                        transaction.transition_execution(expected_revision, execution, **kwargs)

                yield Proxy()

    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(
        tmp_path,
        _stream_script(epoch, _successful_outcome(epoch), close=False),
    )
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    chain._repository = FailAwaitingHomeZOnce(repository)  # type: ignore[assignment,attr-defined]
    try:
        result = _execute_stream(chain, execution_id, "stream-awaiting-home-z-commit-fail")
        assert result.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert result.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert result.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
        assert result.recovery_evidence.failure.code == "STREAM_CONFIRMATION_COMMIT_UNCERTAIN"  # type: ignore[attr-defined]
    finally:
        repository.close()
