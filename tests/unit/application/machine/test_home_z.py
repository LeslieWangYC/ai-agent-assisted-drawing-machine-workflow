from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep
from drawingmachine.application.machine.commands import (
    HomeZMachineCommand,
    MachineCommandMode,
)
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachinePhase,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    HomeZAxisRequest,
    HomeZOutcome,
    StreamOutcome,
)
from tests.unit.application.machine.test_admission import AUTOMATION, _uuid
from tests.unit.application.machine.test_home import OPERATOR, _challenge_id, _snapshot
from tests.unit.application.machine.test_stream import (
    _execute_stream,
    _prepare_to_stream,
    _program,
    _stream_script,
)


def test_home_z_command_is_closed_and_requires_a_separate_approval() -> None:
    command = HomeZMachineCommand(
        "home-z-request",
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


def test_home_z_outcome_is_bound_to_the_exact_fixed_typed_request() -> None:
    epoch = _uuid(2)
    snapshot = _snapshot(
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    request = HomeZAxisRequest()
    outcome = HomeZOutcome(
        machine_session_epoch=epoch,
        request=request,
        snapshot=snapshot,
    )
    assert outcome.request is request
    with pytest.raises(TypeError, match="request"):
        HomeZOutcome(
            machine_session_epoch=epoch,
            request=object(),  # type: ignore[arg-type]
            snapshot=snapshot,
        )


def _home_z_success_script(epoch: str) -> tuple[FakeFluidNCStep, ...]:
    program = _program()
    final = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    stream = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    home_z_final = _snapshot(
        mpos=(192.0, 192.0, 192.0),
        wpos=(96.0, 96.0, 96.0),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    return (
        *_stream_script(epoch, stream, close=False),
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_Z_AXIS,
            HomeZAxisRequest(),
            HomeZOutcome(epoch, HomeZAxisRequest(), home_z_final),
        ),
        FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
    )


def _admit_home_z(chain: object, execution_id: str) -> object:
    requested = cast(
        MachineCommandResult,
        chain.admit_action(  # type: ignore[attr-defined]
            HomeZMachineCommand("home-z-request", AUTOMATION, execution_id, MachineCommandMode.REQUEST, None)
        ),
    )
    admitted = chain.admit_action(  # type: ignore[attr-defined]
        HomeZMachineCommand(
            "home-z-execute",
            AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 456),
            execution_id,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
    )
    return admitted


def _execute_home_z(chain: object, execution_id: str) -> object:
    return chain.drive_admitted_action(_admit_home_z(chain, execution_id))  # type: ignore[attr-defined]


def test_home_z_requires_new_challenge_and_completes_only_after_one_typed_operation(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _home_z_success_script(epoch))
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        streamed = _execute_stream(chain, execution_id)
        assert streamed.phase is MachinePhase.AWAITING_HOME_Z_APPROVAL  # type: ignore[attr-defined]
        assert streamed.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert repository.get_outcome(execution_id) is None
        completed = _execute_home_z(chain, execution_id)
        assert completed.phase is MachinePhase.COMPLETED  # type: ignore[attr-defined]
        assert repository.get_outcome(execution_id).kind.value == "COMPLETED"  # type: ignore[union-attr]
    finally:
        repository.close()


def test_home_z_failure_retains_confirmed_stream_and_post_stream_recovery(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    final = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    stream = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    failure = FluidNCFailure(FluidNCFailureKind.ALARM, "HOME_Z_ALARM", "HOME_Z alarmed", True)
    steps = (
        *_stream_script(epoch, stream, close=False),
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_Z_AXIS,
            HomeZAxisRequest(),
            HomeZOutcome(epoch, HomeZAxisRequest(), None, failure),
        ),
        FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
    )
    chain, repository, execution_id = _prepare_to_stream(tmp_path, steps)
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        assert _execute_stream(chain, execution_id).phase is MachinePhase.AWAITING_HOME_Z_APPROVAL  # type: ignore[attr-defined]
        failed = _execute_home_z(chain, execution_id)
        assert failed.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert failed.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert failed.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_home_z_close_uncertainty_retains_session_and_post_stream_latch(tmp_path: Path) -> None:
    epoch = _uuid(2)
    steps = list(_home_z_success_script(epoch))
    close_failure = FluidNCFailure(
        FluidNCFailureKind.TRANSPORT_LOSS,
        "HOME_Z_CLOSE_UNCERTAIN",
        "close acknowledgement was uncertain",
        True,
    )
    steps[-1] = FakeFluidNCStep(
        FakeFluidNCOperation.CLOSE,
        CloseSessionRequest(epoch),
        CloseOutcome(epoch, close_failure),
    )
    chain, repository, execution_id = _prepare_to_stream(tmp_path, tuple(steps))
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        assert _execute_stream(chain, execution_id).phase is MachinePhase.AWAITING_HOME_Z_APPROVAL  # type: ignore[attr-defined]
        failed = _execute_home_z(chain, execution_id)
        assert failed.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert failed.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert failed.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
        assert failed.recovery_evidence.failure.code == "HOME_Z_CLOSE_UNCERTAIN"  # type: ignore[attr-defined]
        assert chain.retained_session_epochs() == (epoch,)
    finally:
        repository.close()


def test_home_z_terminal_commit_failure_is_closed_post_stream_recovery(tmp_path: Path) -> None:
    class FailHomeZCompletedOnce:
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
                            raise RuntimeError("injected HOME_Z terminal commit failure")
                        transaction.commit_machine_outcome(expected_revision, execution, **kwargs)

                yield Proxy()

    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _home_z_success_script(epoch))
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        assert _execute_stream(chain, execution_id).phase is MachinePhase.AWAITING_HOME_Z_APPROVAL  # type: ignore[attr-defined]
        chain._repository = FailHomeZCompletedOnce(repository)  # type: ignore[assignment,attr-defined]
        failed = _execute_home_z(chain, execution_id)
        assert failed.phase is MachinePhase.RECOVERY_REQUIRED  # type: ignore[attr-defined]
        assert failed.stream_milestone is StreamMilestone.STREAM_CONFIRMED  # type: ignore[attr-defined]
        assert failed.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME  # type: ignore[attr-defined]
        assert failed.recovery_evidence.failure.code == "HOME_Z_COMPLETION_COMMIT_UNCERTAIN"  # type: ignore[attr-defined]
    finally:
        repository.close()
