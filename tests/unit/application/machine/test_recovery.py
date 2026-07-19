from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.adapters.persistence import machine_sql
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import (
    MachineCommandMode,
    MachineRuntimeAuthority,
    RecoverMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult, MachineExecutionChain
from drawingmachine.application.machine.recovery import validate_recovery_disposition
from drawingmachine.domain.jobs import JobRecord, JobState
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachineExecution,
    MachineFailureEvidenceV1,
    MachinePhase,
    MachineRecoveryEvidence,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
    expire_approval,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import CloseOutcome, CloseSessionRequest, InitialPreflightRequest, OpenSessionRequest
from tests.unit.application.machine.test_admission import (
    GCODE,
    NOW,
    ProbeFactory,
    ProbeSession,
    Reader,
    _authority,
    _config,
    _identities,
    _job,
    _late_reset_preflight,
    _preflight,
    _ready_snapshot,
    _RepositoryProxy,
    _seed,
    _uuid,
)

AUTOMATION = AuthenticatedMachinePrincipal(1100, 1100, 31001)
OPERATOR = AuthenticatedMachinePrincipal(2200, 2200, 41001)


class SequencedFactory:
    def __init__(self, factories: tuple[StrictFakeFluidNCFactory, ...]) -> None:
        self.factories = factories
        self.opens = 0

    def open_session(self, request: OpenSessionRequest):
        if self.opens >= len(self.factories):
            raise AssertionError("unexpected Fake FluidNC open")
        factory = self.factories[self.opens]
        self.opens += 1
        return factory.open_session(request)


def _recovery_job() -> JobRecord:
    ready = _job(_ready_snapshot())
    return replace(
        ready,
        state=JobState.RECOVERY_REQUIRED,
        revision=ready.revision + 1,
        updated_at=NOW + timedelta(seconds=1),
    )


def _recovery_execution(intent: RecoveryIntent, *, service_epoch: str | None = None) -> MachineExecution:
    milestone = {
        RecoveryIntent.PRE_STREAM_RESTART: StreamMilestone.NOT_STARTED,
        RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY: StreamMilestone.FIRST_WRITE_POSSIBLE,
        RecoveryIntent.POST_STREAM_SAFE_HOME: StreamMilestone.STREAM_CONFIRMED,
    }[intent]
    failure = MachineFailureEvidenceV1(1, "FIXTURE_RECOVERY", "fixed recovery evidence", "Idle", True)
    evidence = MachineRecoveryEvidence(1, "execution-recovery", intent, milestone, failure.code, failure, NOW)
    return MachineExecution(
        1,
        "execution-recovery",
        "job-c9",
        7,
        1,
        MachinePhase.RECOVERY_REQUIRED,
        _authority().service_epoch if service_epoch is None else service_epoch,
        _uuid(700),
        _ready_snapshot().gcode.sha256,
        "0.2.0.dev0",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        milestone,
        intent,
        None,
        evidence,
        None,
        NOW,
        NOW,
    )


def _seed_execution(repository: SQLiteMachineRepository, execution: MachineExecution) -> None:
    placeholders = ", ".join("?" for _ in range(25))
    repository._get_connection().execute(
        f"INSERT INTO machine_executions ({machine_sql._EXECUTION_COLUMNS}) VALUES ({placeholders})",
        machine_sql._MachineTransaction._execution_values(execution),
    )


def _chain(
    tmp_path: Path,
    intent: RecoveryIntent,
    *,
    identities: Iterator[str] | None = None,
    factories: tuple[StrictFakeFluidNCFactory, ...] = (),
    authority: MachineRuntimeAuthority | None = None,
    wall_now: Callable[[], object] | None = None,
    monotonic_now: Callable[[], float] | None = None,
    execution_service_epoch: str | None = None,
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, SequencedFactory, Reader]:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    _seed_execution(repository, _recovery_execution(intent, service_epoch=execution_service_epoch))
    repository._get_connection().execute(
        "UPDATE jobs SET state = 'RECOVERY_REQUIRED', revision = 8, updated_at = ? WHERE job_id = 'job-c9'",
        ((NOW + timedelta(seconds=1)).isoformat(),),
    )
    reader = Reader(GCODE)
    factory = SequencedFactory(factories)
    values = identities or _identities()
    chain = MachineExecutionChain(
        repository,
        reader,
        factory,
        _config(),
        _authority() if authority is None else authority,
        identity_factory=lambda: next(values),
        wall_now=(lambda: NOW + timedelta(seconds=2)) if wall_now is None else wall_now,  # type: ignore[arg-type]
        monotonic_now=(lambda: 1001.0) if monotonic_now is None else monotonic_now,
    )
    return chain, repository, factory, reader


def _recover(
    *,
    request_id: str,
    requester: AuthenticatedMachinePrincipal,
    disposition: RecoveryDisposition,
    mode: MachineCommandMode,
    challenge_id: str | None = None,
) -> RecoverMachineCommand:
    return RecoverMachineCommand(
        request_id,
        requester,
        "execution-recovery",
        disposition,
        mode,
        challenge_id,
    )


def _challenge_id(result: MachineCommandResult) -> str:
    challenge = cast(dict[str, object], result.response["challenge"])
    return cast(str, challenge["challenge_id"])


def test_release_consumes_operator_challenge_without_artifact_read_or_adapter_open(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        authority=_authority(machine_digest="f" * 64),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="recover-request",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        assert reader.calls == 0 and factory.opens == 0
        released = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="recover-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(requested),
                )
            ),
        )
        assert released.response["released"] is True
        assert reader.calls == 0 and factory.opens == 0
        owner = repository.get_execution("execution-recovery")
        assert owner is not None and owner.retirement is not None
        assert repository.get_active_execution() is None
        job = repository._get_connection().execute("SELECT state FROM jobs WHERE job_id = 'job-c9'").fetchone()
        assert job == (JobState.RECOVERY_REQUIRED.value,)
    finally:
        repository.close()


def test_automation_cannot_consume_its_own_recovery_challenge(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="request",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        with pytest.raises(DrawingMachineError, match="configured operator"):
            chain.admit_action(
                _recover(
                    request_id="execute",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(requested),
                )
            )
        assert reader.calls == 0 and factory.opens == 0
        assert repository.get_active_execution() is not None
    finally:
        repository.close()


def test_restart_successor_transfers_atomically_then_opens_only_after_drive(tmp_path: Path) -> None:
    identities = _identities()
    successor_epoch = _uuid(4)
    successor_factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _preflight(successor_epoch),
            ),
        ),
    )
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        identities=identities,
        factories=(successor_factory,),
    )
    try:
        request_command = _recover(
            request_id="request",
            requester=OPERATOR,
            disposition=RecoveryDisposition.RESTART_SEQUENCE,
            mode=MachineCommandMode.REQUEST,
        )
        requested = cast(
            MachineCommandResult,
            chain.admit_action(request_command),
        )
        execute_command = _recover(
            request_id="execute",
            requester=AuthenticatedMachinePrincipal(2200, 2200, 41999),
            disposition=RecoveryDisposition.RESTART_SEQUENCE,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=_challenge_id(requested),
        )
        admitted = chain.admit_action(execute_command)
        predecessor = repository.get_execution("execution-recovery")
        assert predecessor is not None and predecessor.retirement is not None
        active = repository.get_active_execution()
        assert active is not None and active.predecessor_execution_id == predecessor.execution_id
        assert active.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
        assert active.phase is MachinePhase.PREPARING_SESSION
        assert factory.opens == 0 and reader.calls == 1

        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.AWAITING_HOME_APPROVAL
        assert factory.opens == 1
        replay_request = cast(
            MachineCommandResult,
            chain.admit_action(replace(request_command, requester=AuthenticatedMachinePrincipal(2200, 2200, 42998))),
        )
        replay_execute = cast(
            MachineCommandResult,
            chain.admit_action(replace(execute_command, requester=AuthenticatedMachinePrincipal(2200, 2200, 42997))),
        )
        assert replay_request.deduplicated is True and replay_execute.deduplicated is True
        assert successor_factory.connections_opened == 1
    finally:
        repository.close()


def test_recover_request_after_service_restart_binds_current_epoch_not_executions_stale_epoch(
    tmp_path: Path,
) -> None:
    # Regression for Defect #15: the execution entered RECOVERY_REQUIRED under one service
    # instance (its frozen service_epoch), then the service crashed and restarted before an
    # operator could recover it. `_recovery_binding` must bind the CURRENT coordinator epoch
    # (self._authority.service_epoch), not the stale epoch frozen on the execution row, or the
    # machine-protocol output validator rejects the challenge with MACHINE_PROTOCOL_INVALID
    # once it is round-tripped through JSON and re-validated against the live authority.
    identities = _identities()
    successor_epoch = _uuid(4)
    successor_factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(successor_epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _preflight(successor_epoch),
            ),
        ),
    )
    stale_epoch = _uuid(899)
    current_authority = _authority()
    assert current_authority.service_epoch != stale_epoch
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        identities=identities,
        factories=(successor_factory,),
        authority=current_authority,
        execution_service_epoch=stale_epoch,
    )
    try:
        request_command = _recover(
            request_id="request",
            requester=OPERATOR,
            disposition=RecoveryDisposition.RESTART_SEQUENCE,
            mode=MachineCommandMode.REQUEST,
        )
        requested = cast(
            MachineCommandResult,
            chain.admit_action(request_command),
        )
        challenge = cast(dict[str, object], requested.response["challenge"])
        binding = cast(dict[str, object], challenge["binding"])
        assert binding["service_epoch"] == current_authority.service_epoch
        assert binding["service_epoch"] != stale_epoch

        execute_command = _recover(
            request_id="execute",
            requester=AuthenticatedMachinePrincipal(2200, 2200, 41999),
            disposition=RecoveryDisposition.RESTART_SEQUENCE,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=_challenge_id(requested),
        )
        admitted = chain.admit_action(execute_command)
        predecessor = repository.get_execution("execution-recovery")
        assert predecessor is not None and predecessor.retirement is not None
        active = repository.get_active_execution()
        assert active is not None and active.predecessor_execution_id == predecessor.execution_id
        assert active.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
        assert active.phase is MachinePhase.PREPARING_SESSION
        assert factory.opens == 0 and reader.calls == 1

        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.AWAITING_HOME_APPROVAL
        assert factory.opens == 1
    finally:
        repository.close()


def test_recovery_successor_late_reset_is_durable_recovery_never_home_eligible(tmp_path: Path) -> None:
    identities = _identities()
    successor_epoch = _uuid(4)
    successor_factory = StrictFakeFluidNCFactory(
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
                CloseOutcome(successor_epoch),
            ),
        ),
    )
    chain, repository, factory, _ = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        identities=identities,
        factories=(successor_factory,),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="late-reset-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            _recover(
                request_id="late-reset-execute",
                requester=OPERATOR,
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_challenge_id(requested),
            )
        )
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence is not None
        assert result.recovery_evidence.failure.code == "CONTROLLER_RESET_DETECTED"
        assert factory.opens == 1
    finally:
        repository.close()


def test_recovery_issue_and_consume_audits_bind_challenge_pids_and_frozen_ready_revision(
    tmp_path: Path,
) -> None:
    chain, repository, _, _ = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="audit-request",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        challenge_id = _challenge_id(requested)
        chain.admit_action(
            _recover(
                request_id="audit-execute",
                requester=AuthenticatedMachinePrincipal(2200, 2200, 41999),
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=challenge_id,
            )
        )
        payloads = [
            json.loads(row[0])
            for row in repository._get_connection()
            .execute("SELECT payload_json FROM audit_events WHERE event_type = 'machine.authority' ORDER BY rowid")
            .fetchall()
        ]
        issue = next(payload for payload in payloads if payload["decision"] == "CHALLENGE_ISSUED")
        consumed = [payload for payload in payloads if payload["decision"] in {"TRANSFERRED", "ADMITTED"}]
        assert issue["approval_challenge_id"] == challenge_id
        assert issue["challenge_issued_at"] is not None
        assert issue["approved_at"] is None
        assert issue["issuer_pid"] == AUTOMATION.pid and issue["consumer_pid"] is None
        assert issue["frozen_ready_revision"] == 7
        assert len(consumed) == 2
        assert all(payload["approval_challenge_id"] == challenge_id for payload in consumed)
        assert all(payload["challenge_issued_at"] is not None for payload in consumed)
        assert all(payload["approved_at"] is not None for payload in consumed)
        assert all(payload["issuer_pid"] == AUTOMATION.pid for payload in consumed)
        assert all(payload["consumer_pid"] == 41999 for payload in consumed)
        assert all(payload["frozen_ready_revision"] == 7 for payload in consumed)
    finally:
        repository.close()


def test_recovery_successor_wrong_epoch_preflight_enters_recovery_and_retains_close_uncertainty(
    tmp_path: Path,
) -> None:
    identities = _identities()
    expected_epoch = _uuid(4)
    wrong_epoch = _uuid(904)
    session = ProbeSession(_preflight(wrong_epoch), CloseOutcome(wrong_epoch))
    probe_factory = ProbeFactory(expected_epoch, session)
    chain, repository, _, _ = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        identities=identities,
    )
    chain._fluidnc_factory = probe_factory  # type: ignore[attr-defined]
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="request-wrong-epoch",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            _recover(
                request_id="execute-wrong-epoch",
                requester=OPERATOR,
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_challenge_id(requested),
            )
        )
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.predecessor_execution_id == "execution-recovery"
        assert chain.retained_session_epochs() == (expected_epoch,)
        assert probe_factory.opens == 1 and session.close_calls == 1
        second_request = replace(
            _recover(
                request_id="request-after-close-uncertainty",
                requester=OPERATOR,
                disposition=RecoveryDisposition.RESTART_SEQUENCE,
                mode=MachineCommandMode.REQUEST,
            ),
            execution_id=result.execution_id,
        )
        second_challenge = cast(MachineCommandResult, chain.admit_action(second_request))
        with pytest.raises(DrawingMachineError, match="session ownership"):
            chain.admit_action(
                replace(
                    _recover(
                        request_id="execute-after-close-uncertainty",
                        requester=OPERATOR,
                        disposition=RecoveryDisposition.RESTART_SEQUENCE,
                        mode=MachineCommandMode.EXECUTE,
                        challenge_id=_challenge_id(second_challenge),
                    ),
                    execution_id=result.execution_id,
                )
            )
        assert repository.get_active_execution() == result
        assert probe_factory.opens == 1
        assert session.close_calls == 1
    finally:
        repository.close()


@pytest.mark.parametrize("expired_clock", ["wall", "monotonic"])
def test_recovery_approval_expiring_during_artifact_read_cannot_transfer_or_open(
    tmp_path: Path,
    expired_clock: str,
) -> None:
    class Times:
        wall = NOW + timedelta(seconds=2)
        monotonic = 1001.0

    times = Times()
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        wall_now=lambda: times.wall,
        monotonic_now=lambda: times.monotonic,
    )
    original_read = reader.read_bytes

    def expire_during_read(*args: object, **kwargs: object) -> bytes:
        value = original_read(*args, **kwargs)  # type: ignore[arg-type]
        if expired_clock == "wall":
            times.wall += timedelta(seconds=60)
        else:
            times.monotonic += 60.0
        return value

    reader.read_bytes = expire_during_read  # type: ignore[method-assign]
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="ttl-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        with pytest.raises(DrawingMachineError, match="expired"):
            chain.admit_action(
                _recover(
                    request_id="ttl-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(requested),
                )
            )
        owner = repository.get_active_execution()
        assert owner is not None and owner.execution_id == "execution-recovery"
        assert repository.get_challenge(_challenge_id(requested)).status.value == "PENDING"  # type: ignore[union-attr]
        assert factory.opens == 0
    finally:
        repository.close()


def test_concurrent_expiry_during_artifact_read_wins_without_successor_or_open(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    original_read = reader.read_bytes
    challenge_id: str | None = None

    def expire_in_repository(*args: object, **kwargs: object) -> bytes:
        value = original_read(*args, **kwargs)  # type: ignore[arg-type]
        assert challenge_id is not None
        with repository.transaction() as transaction:
            pending = transaction.get_challenge(challenge_id)
            assert pending is not None
            transaction.decide_challenge(
                expire_approval(
                    pending,
                    wall_now=pending.expires_at,
                    monotonic_now=pending.monotonic_deadline,
                )
            )
        return value

    reader.read_bytes = expire_in_repository  # type: ignore[method-assign]
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="concurrent-expiry-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        challenge_id = _challenge_id(requested)
        with pytest.raises(DrawingMachineError, match="pending"):
            chain.admit_action(
                _recover(
                    request_id="concurrent-expiry-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=challenge_id,
                )
            )
        owner = repository.get_active_execution()
        assert owner is not None and owner.execution_id == "execution-recovery"
        assert repository.get_challenge(challenge_id).status.value == "EXPIRED"  # type: ignore[union-attr]
        assert factory.opens == 0
    finally:
        repository.close()


def test_expired_pending_recovery_challenge_is_expired_not_superseded_before_replacement(
    tmp_path: Path,
) -> None:
    class Times:
        wall = NOW + timedelta(seconds=2)
        monotonic = 1001.0

    times = Times()
    chain, repository, _, _ = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        wall_now=lambda: times.wall,
        monotonic_now=lambda: times.monotonic,
    )
    try:
        first = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="expired-first",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        times.wall += timedelta(seconds=60)
        times.monotonic += 60.0
        second = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="expired-second",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        assert repository.get_challenge(_challenge_id(first)).status.value == "EXPIRED"  # type: ignore[union-attr]
        assert repository.get_challenge(_challenge_id(second)).status.value == "PENDING"  # type: ignore[union-attr]
    finally:
        repository.close()


def test_replacement_propagates_non_expiry_clock_authority_failure(tmp_path: Path) -> None:
    chain, repository, _, _ = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        chain.admit_action(
            _recover(
                request_id="future-first",
                requester=OPERATOR,
                disposition=RecoveryDisposition.RELEASE,
                mode=MachineCommandMode.REQUEST,
            )
        )
        chain._wall_now = lambda: NOW + timedelta(seconds=1)  # type: ignore[attr-defined]
        chain._monotonic_now = lambda: 1000.0  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="future"):
            chain.admit_action(
                _recover(
                    request_id="future-second",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            )
    finally:
        repository.close()


def test_live_challenge_disappearing_inside_release_transaction_does_not_retire_owner(tmp_path: Path) -> None:
    class MissingLiveChallenge(_RepositoryProxy):
        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def get_challenge(self, challenge_id: str):
                        del challenge_id
                        return None

                yield Proxy()

    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="missing-live-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        masked = _proxy_recovery_chain(MissingLiveChallenge(repository), reader, factory)
        with pytest.raises(DrawingMachineError, match="does not exist"):
            masked.admit_action(
                _recover(
                    request_id="missing-live-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(requested),
                )
            )
        owner = repository.get_active_execution()
        assert owner is not None and owner.execution_id == "execution-recovery"
    finally:
        repository.close()


def test_recovery_successor_withdrawal_after_validation_preserves_predecessor_and_pending_challenge(
    tmp_path: Path,
) -> None:
    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        challenge_id = _challenge_id(requested)
        checks = iter((True, True, False))
        with pytest.raises(DrawingMachineError, match="withdrawn"):
            chain.admit_action(
                _recover(
                    request_id="execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=challenge_id,
                ),
                still_requested=lambda: next(checks),
            )
        owner = repository.get_active_execution()
        assert owner is not None and owner.execution_id == "execution-recovery"
        assert repository.get_challenge(challenge_id).status.value == "PENDING"  # type: ignore[union-attr]
        assert reader.calls == 1 and factory.opens == 0
    finally:
        repository.close()


def test_invalid_trusted_config_blocks_successor_but_keeps_nonmotion_release_available(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        authority=_authority(machine_digest="f" * 64),
    )
    try:
        restart = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="restart-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        with pytest.raises(DrawingMachineError, match="runtime authority"):
            chain.admit_action(
                _recover(
                    request_id="restart-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(restart),
                )
            )
        release = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="release-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        result = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="release-execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_challenge_id(release),
                )
            ),
        )
        assert result.response["released"] is True
        assert reader.calls == 0 and factory.opens == 0
    finally:
        repository.close()


def test_post_stream_safe_home_transfers_only_confirmed_milestone_successor(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.POST_STREAM_SAFE_HOME)
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="safe-home-request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.SAFE_HOME,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            _recover(
                request_id="safe-home-execute",
                requester=AuthenticatedMachinePrincipal(2200, 2200, 41999),
                disposition=RecoveryDisposition.SAFE_HOME,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_challenge_id(requested),
            )
        )
        successor = repository.get_execution(cast(object, admitted).execution_id)  # type: ignore[attr-defined]
        assert successor is not None
        assert successor.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        assert successor.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert successor.phase is MachinePhase.PREPARING_SESSION
        assert reader.calls == 1 and factory.opens == 0
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("intent", "disposition"),
    [
        (RecoveryIntent.PRE_STREAM_RESTART, RecoveryDisposition.SAFE_HOME),
        (RecoveryIntent.POST_STREAM_SAFE_HOME, RecoveryDisposition.RESTART_SEQUENCE),
        (RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY, RecoveryDisposition.RESTART_SEQUENCE),
        (RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY, RecoveryDisposition.SAFE_HOME),
    ],
)
def test_disposition_specific_graph_rejects_every_wrong_successor(
    tmp_path: Path,
    intent: RecoveryIntent,
    disposition: RecoveryDisposition,
) -> None:
    chain, repository, factory, reader = _chain(tmp_path, intent)
    try:
        with pytest.raises(DrawingMachineError, match="not legal"):
            chain.admit_action(
                _recover(
                    request_id="wrong",
                    requester=OPERATOR,
                    disposition=disposition,
                    mode=MachineCommandMode.REQUEST,
                )
            )
        assert reader.calls == 0 and factory.opens == 0
    finally:
        repository.close()


def test_validate_recovery_disposition_accepts_release_for_all_intents() -> None:
    for intent in RecoveryIntent:
        validate_recovery_disposition(intent, RecoveryDisposition.RELEASE)


def test_recover_command_rejects_invalid_closed_fields() -> None:
    valid = {
        "request_id": "request",
        "requester": OPERATOR,
        "execution_id": "execution-recovery",
        "disposition": RecoveryDisposition.RELEASE,
        "mode": MachineCommandMode.REQUEST,
        "challenge_id": None,
    }
    cases = (
        {"disposition": object()},
        {"mode": object()},
        {"mode": MachineCommandMode.REQUEST, "challenge_id": _uuid(1)},
        {"mode": MachineCommandMode.EXECUTE, "challenge_id": None},
        {"mode": MachineCommandMode.EXECUTE, "challenge_id": "bad\nchallenge"},
        {"requester": object()},
    )
    for changes in cases:
        values = {**valid, **changes}
        with pytest.raises(DrawingMachineError):
            RecoverMachineCommand(**values)  # type: ignore[arg-type]


def _proxy_recovery_chain(
    repository: object,
    reader: Reader,
    factory: object,
) -> MachineExecutionChain:
    identities = _identities()
    return MachineExecutionChain(
        repository,  # type: ignore[arg-type]
        reader,
        factory,  # type: ignore[arg-type]
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=2),
        monotonic_now=lambda: 1001.0,
    )


def test_recovery_request_replay_and_transaction_race_return_durable_challenge(tmp_path: Path) -> None:
    class MaskOuterRequest(_RepositoryProxy):
        def get_request(self, key):
            del key
            return None

    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    command = _recover(
        request_id="request",
        requester=OPERATOR,
        disposition=RecoveryDisposition.RELEASE,
        mode=MachineCommandMode.REQUEST,
    )
    try:
        first = cast(MachineCommandResult, chain.admit_action(command))
        replay = cast(
            MachineCommandResult,
            chain.admit_action(replace(command, requester=AuthenticatedMachinePrincipal(2200, 2200, 41999))),
        )
        assert replay.deduplicated is True
        masked = _proxy_recovery_chain(MaskOuterRequest(repository), reader, factory)
        raced = cast(MachineCommandResult, masked.admit_action(command))
        assert raced.response["deduplicated"] is True
        assert _challenge_id(first) == _challenge_id(replay) == _challenge_id(raced)
    finally:
        repository.close()


def test_second_recovery_request_supersedes_pending_challenge(tmp_path: Path) -> None:
    chain, repository, _, _ = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        first = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="first",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        second = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="second",
                    requester=AUTOMATION,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        third = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="third",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        assert _challenge_id(first) != _challenge_id(second)
        assert repository.get_challenge(_challenge_id(first)).status.value == "SUPERSEDED"  # type: ignore[union-attr]
        assert repository.get_challenge(_challenge_id(second)).status.value == "SUPERSEDED"  # type: ignore[union-attr]
        assert repository.get_challenge(_challenge_id(third)).status.value == "PENDING"  # type: ignore[union-attr]
    finally:
        repository.close()


def test_missing_challenge_and_missing_owner_fail_before_artifact_or_adapter(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    try:
        with pytest.raises(DrawingMachineError, match="does not exist"):
            chain.admit_action(
                _recover(
                    request_id="execute",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.EXECUTE,
                    challenge_id=_uuid(999),
                )
            )
        with pytest.raises(DrawingMachineError, match="active recovery latch owner"):
            chain.admit_action(
                replace(
                    _recover(
                        request_id="wrong-owner",
                        requester=OPERATOR,
                        disposition=RecoveryDisposition.RELEASE,
                        mode=MachineCommandMode.REQUEST,
                    ),
                    execution_id="not-the-owner",
                )
            )
        assert reader.calls == 0 and factory.opens == 0
    finally:
        repository.close()


def test_recovery_transaction_rechecks_job_authority(tmp_path: Path) -> None:
    class MaskJob(_RepositoryProxy):
        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def get_job(self, job_id: str):
                        del job_id
                        return None

                yield Proxy()

    _chain_value, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    masked = _proxy_recovery_chain(MaskJob(repository), reader, factory)
    try:
        with pytest.raises(DrawingMachineError, match="no longer matches"):
            masked.admit_action(
                _recover(
                    request_id="masked",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RELEASE,
                    mode=MachineCommandMode.REQUEST,
                )
            )
        assert reader.calls == 0 and factory.opens == 0
    finally:
        repository.close()


def test_release_transaction_race_replays_without_second_retirement(tmp_path: Path) -> None:
    class MaskRelease(_RepositoryProxy):
        def __init__(self, delegate, owner, challenge) -> None:
            super().__init__(delegate)
            self.owner = owner
            self.challenge = challenge

        def get_request(self, key):
            del key
            return None

        def get_active_execution(self):
            return self.owner

        def get_challenge(self, challenge_id: str):
            del challenge_id
            return self.challenge

    chain, repository, factory, reader = _chain(tmp_path, RecoveryIntent.PRE_STREAM_RESTART)
    owner = cast(MachineExecution, repository.get_active_execution())
    request = _recover(
        request_id="request",
        requester=OPERATOR,
        disposition=RecoveryDisposition.RELEASE,
        mode=MachineCommandMode.REQUEST,
    )
    execute: RecoverMachineCommand
    try:
        requested = cast(MachineCommandResult, chain.admit_action(request))
        pending = repository.get_challenge(_challenge_id(requested))
        assert pending is not None
        execute = _recover(
            request_id="execute",
            requester=OPERATOR,
            disposition=RecoveryDisposition.RELEASE,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=pending.challenge_id,
        )
        chain.admit_action(execute)
        direct_request_replay = cast(
            MachineCommandResult,
            chain.admit_action(replace(request, requester=AuthenticatedMachinePrincipal(2200, 2200, 42996))),
        )
        direct_execute_replay = cast(
            MachineCommandResult,
            chain.admit_action(replace(execute, requester=AuthenticatedMachinePrincipal(2200, 2200, 42995))),
        )
        assert direct_request_replay.deduplicated is True and direct_execute_replay.deduplicated is True
        masked = _proxy_recovery_chain(MaskRelease(repository, owner, pending), reader, factory)
        replay = cast(MachineCommandResult, masked.admit_action(execute))
        assert replay.response["deduplicated"] is True
        assert repository.get_active_execution() is None
    finally:
        repository.close()


def test_transfer_transaction_race_replays_without_second_successor(tmp_path: Path) -> None:
    class MaskTransfer(_RepositoryProxy):
        def __init__(self, delegate, owner, challenge) -> None:
            super().__init__(delegate)
            self.owner = owner
            self.challenge = challenge

        def get_request(self, key):
            del key
            return None

        def get_active_execution(self):
            return self.owner

        def get_challenge(self, challenge_id: str):
            del challenge_id
            return self.challenge

    identities = _identities()
    successor_epoch = _uuid(4)
    successor_factory = StrictFakeFluidNCFactory(OpenSessionRequest(successor_epoch), ())
    chain, repository, factory, reader = _chain(
        tmp_path,
        RecoveryIntent.PRE_STREAM_RESTART,
        identities=identities,
        factories=(successor_factory,),
    )
    owner = cast(MachineExecution, repository.get_active_execution())
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id="request",
                    requester=OPERATOR,
                    disposition=RecoveryDisposition.RESTART_SEQUENCE,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        pending = repository.get_challenge(_challenge_id(requested))
        assert pending is not None
        execute = _recover(
            request_id="execute",
            requester=OPERATOR,
            disposition=RecoveryDisposition.RESTART_SEQUENCE,
            mode=MachineCommandMode.EXECUTE,
            challenge_id=pending.challenge_id,
        )
        first = chain.admit_action(execute)
        active_id = cast(object, first).execution_id  # type: ignore[attr-defined]
        masked = _proxy_recovery_chain(MaskTransfer(repository, owner, pending), reader, factory)
        replay = masked.admit_action(execute)
        assert cast(object, replay).response["deduplicated"] is True  # type: ignore[attr-defined]
        assert repository.get_active_execution().execution_id == active_id  # type: ignore[union-attr]
    finally:
        repository.close()


def _lifecycle_chain(
    tmp_path: Path,
    *,
    phase: MachinePhase,
    milestone: StreamMilestone,
    close_result: object | BaseException | None = None,
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, ProbeFactory, ProbeSession, MachineExecution]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = SQLiteMachineRepository(tmp_path / "lifecycle.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    current = MachineExecution(
        1,
        "execution-lifecycle",
        "job-c9",
        7,
        3,
        phase,
        _uuid(899),
        _uuid(898),
        _ready_snapshot().gcode.sha256,
        "0.2.0.dev0",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        milestone,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
    )
    _seed_execution(repository, current)
    session = ProbeSession(
        _preflight(current.machine_session_epoch),
        CloseOutcome(current.machine_session_epoch) if close_result is None else close_result,
    )
    factory = ProbeFactory(current.machine_session_epoch, session)
    identities = _identities()
    chain = MachineExecutionChain(
        repository,
        Reader(GCODE),
        factory,
        _config(),
        _authority(service_epoch=_uuid(900)),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=10),
        monotonic_now=lambda: 1010.0,
    )
    return chain, repository, factory, session, current


@pytest.mark.parametrize(
    ("phase", "milestone", "intent"),
    [
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
    ],
)
def test_c12_startup_reconciles_exact_milestone_without_adapter_open(
    tmp_path: Path,
    phase: MachinePhase,
    milestone: StreamMilestone,
    intent: RecoveryIntent,
) -> None:
    chain, repository, factory, session, current = _lifecycle_chain(
        tmp_path,
        phase=phase,
        milestone=milestone,
    )
    try:
        projection = chain.initialize_and_reconcile()

        recovered = repository.get_active_execution()
        assert recovered is not None
        assert recovered.execution_id == current.execution_id
        assert recovered.phase is MachinePhase.RECOVERY_REQUIRED
        assert recovered.stream_milestone is milestone
        assert recovered.recovery_intent is intent
        assert recovered.recovery_evidence is not None
        assert recovered.recovery_evidence.reason_code == "SERVICE_RESTARTED"
        assert repository.get_outcome(current.execution_id) is not None
        job = (
            repository._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (current.job_id,),
            )
            .fetchone()
        )
        assert job == (JobState.RECOVERY_REQUIRED.value, 8)
        assert projection["execution"]["recovery_intent"] == intent.value  # type: ignore[index]
        assert factory.opens == 0
        assert session.close_calls == 0
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("intent", "disposition"),
    [
        (RecoveryIntent.PRE_STREAM_RESTART, RecoveryDisposition.RESTART_SEQUENCE),
        (RecoveryIntent.POST_STREAM_SAFE_HOME, RecoveryDisposition.SAFE_HOME),
    ],
)
@pytest.mark.parametrize("lifecycle", ["startup", "shutdown"])
@pytest.mark.parametrize(
    "active_phase",
    [MachinePhase.PREPARING_SESSION, MachinePhase.AWAITING_HOME_APPROVAL],
)
def test_c15_lifecycle_reconciles_active_recovery_successor_without_duplicate_job_outcome(
    tmp_path: Path,
    intent: RecoveryIntent,
    disposition: RecoveryDisposition,
    lifecycle: str,
    active_phase: MachinePhase,
) -> None:
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
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(successor_epoch),
                CloseOutcome(successor_epoch),
            ),
        ),
    )
    chain, repository, factory, _ = _chain(
        tmp_path,
        intent,
        factories=(successor_factory,),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                _recover(
                    request_id=f"{lifecycle}-request",
                    requester=OPERATOR,
                    disposition=disposition,
                    mode=MachineCommandMode.REQUEST,
                )
            ),
        )
        admitted = chain.admit_action(
            _recover(
                request_id=f"{lifecycle}-execute",
                requester=OPERATOR,
                disposition=disposition,
                mode=MachineCommandMode.EXECUTE,
                challenge_id=_challenge_id(requested),
            )
        )
        successor_id = cast(object, admitted).execution_id  # type: ignore[attr-defined]
        if active_phase is MachinePhase.AWAITING_HOME_APPROVAL:
            driven = chain.drive_admitted_action(admitted)
            assert driven.phase is active_phase
        successor = repository.get_active_execution()
        assert successor is not None
        assert successor.execution_id == successor_id
        assert successor.predecessor_execution_id == "execution-recovery"
        assert successor.phase is active_phase
        before_job = (
            repository._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (successor.job_id,),
            )
            .fetchone()
        )
        before_job_events = (
            repository._get_connection()
            .execute(
                "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
                (successor.job_id,),
            )
            .fetchone()[0]
        )

        if lifecycle == "startup":
            database_path = repository._path
            repository.close()
            repository = SQLiteMachineRepository(database_path)
            fresh_factory = SequencedFactory(())
            fresh = MachineExecutionChain(
                repository,
                Reader(GCODE),
                fresh_factory,
                _config(),
                _authority(service_epoch=_uuid(999)),
                identity_factory=lambda: _uuid(998),
                wall_now=lambda: NOW + timedelta(seconds=20),
                monotonic_now=lambda: 1020.0,
            )
            projection = fresh.initialize_and_reconcile()
            assert fresh_factory.opens == 0
        else:
            chain._wall_now = lambda: NOW + timedelta(seconds=20)  # type: ignore[attr-defined]
            projection = chain.shutdown_for_recovery()

        recovered = repository.get_active_execution()
        assert recovered is not None
        assert recovered.execution_id == successor_id
        assert recovered.predecessor_execution_id == "execution-recovery"
        assert recovered.phase is MachinePhase.RECOVERY_REQUIRED
        assert recovered.recovery_intent is intent
        assert recovered.recovery_evidence is not None
        assert recovered.recovery_evidence.reason_code == (
            "SERVICE_RESTARTED" if lifecycle == "startup" else "SERVICE_SHUTDOWN"
        )
        assert projection["execution"]["execution_id"] == successor_id  # type: ignore[index]
        assert repository.get_outcome(successor_id) is None
        after_job = (
            repository._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (successor.job_id,),
            )
            .fetchone()
        )
        after_job_events = (
            repository._get_connection()
            .execute(
                "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
                (successor.job_id,),
            )
            .fetchone()[0]
        )
        assert before_job == after_job == (JobState.RECOVERY_REQUIRED.value, 8)
        assert before_job_events == after_job_events
        assert factory.opens == (1 if active_phase is MachinePhase.AWAITING_HOME_APPROVAL else 0)
        if lifecycle == "shutdown" and active_phase is MachinePhase.AWAITING_HOME_APPROVAL:
            assert successor_factory.started_operations.count(FakeFluidNCOperation.CLOSE) == 1
    finally:
        repository.close()


@pytest.mark.parametrize(
    "phase",
    [
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachinePhase.AWAITING_STREAM_APPROVAL,
    ],
)
def test_c12_shutdown_persists_recovery_before_closing_owned_session_once(
    tmp_path: Path,
    phase: MachinePhase,
) -> None:
    chain, repository, factory, session, current = _lifecycle_chain(
        tmp_path,
        phase=phase,
        milestone=StreamMilestone.NOT_STARTED,
    )
    chain._sessions[current.execution_id] = session  # type: ignore[attr-defined]
    try:
        projection = chain.shutdown_for_recovery()

        recovered = repository.get_active_execution()
        assert recovered is not None and recovered.phase is MachinePhase.RECOVERY_REQUIRED
        assert recovered.recovery_evidence is not None
        assert recovered.recovery_evidence.reason_code == "SERVICE_SHUTDOWN"
        assert projection["execution"]["phase"] == MachinePhase.RECOVERY_REQUIRED.value  # type: ignore[index]
        assert session.close_calls == 1
        assert factory.opens == 0
        assert chain.retained_session_epochs() == ()
        assert chain._sessions == {}  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_c12_shutdown_retains_uncertain_close_then_close_releases_repository(tmp_path: Path) -> None:
    chain, repository, factory, session, current = _lifecycle_chain(
        tmp_path,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        milestone=StreamMilestone.NOT_STARTED,
        close_result=RuntimeError("uncertain close"),
    )
    chain._sessions[current.execution_id] = session  # type: ignore[attr-defined]

    chain.shutdown_for_recovery()

    assert session.close_calls == 1
    assert factory.opens == 0
    assert chain.retained_session_epochs() == (current.machine_session_epoch,)
    chain.close()
    assert repository._connection is None
    assert chain.retained_session_epochs() == ()


def test_c12_shutdown_close_callback_observes_durable_machine_and_job_recovery(tmp_path: Path) -> None:
    chain, repository, factory, _session, current = _lifecycle_chain(
        tmp_path,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        milestone=StreamMilestone.NOT_STARTED,
    )

    class InspectingSession(ProbeSession):
        def close(self, request: CloseSessionRequest) -> object:
            owner = repository.get_active_execution()
            assert owner is not None and owner.phase is MachinePhase.RECOVERY_REQUIRED
            job = (
                repository._get_connection()
                .execute(
                    "SELECT state FROM jobs WHERE job_id = ?",
                    (current.job_id,),
                )
                .fetchone()
            )
            assert job == (JobState.RECOVERY_REQUIRED.value,)
            return super().close(request)

    inspecting = InspectingSession(
        _preflight(current.machine_session_epoch),
        CloseOutcome(current.machine_session_epoch),
    )
    chain._sessions[current.execution_id] = inspecting  # type: ignore[attr-defined]
    try:
        chain.shutdown_for_recovery()
        assert inspecting.close_calls == 1
        assert factory.opens == 0
    finally:
        repository.close()


def test_c12_shutdown_recovery_persistence_failure_closes_once_and_reports_fixed_fatal(tmp_path: Path) -> None:
    _chain_value, repository, factory, session, current = _lifecycle_chain(
        tmp_path,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        milestone=StreamMilestone.NOT_STARTED,
    )

    class FailReconcile(_RepositoryProxy):
        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def reconcile_restart(self, *args: object, **kwargs: object) -> None:
                        del args, kwargs
                        raise RuntimeError("secret persistence failure")

                yield Proxy()

    chain = _proxy_recovery_chain(FailReconcile(repository), Reader(GCODE), factory)
    chain._sessions[current.execution_id] = session  # type: ignore[attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            chain.shutdown_for_recovery()

        assert fatal.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
        assert "secret" not in str(fatal.value)
        assert fatal.value.__cause__ is None
        assert fatal.value.__context__ is None
        assert session.close_calls == 1
        owner = repository.get_active_execution()
        assert owner is not None and owner.phase is MachinePhase.AWAITING_HOME_APPROVAL
        job = (
            repository._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (current.job_id,),
            )
            .fetchone()
        )
        assert job == (JobState.READY_TO_RUN.value, 7)
        assert chain._sessions == {}  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_c12_startup_no_owner_repeated_recovery_and_clock_rollback_boundaries(tmp_path: Path) -> None:
    empty_repository = SQLiteMachineRepository(tmp_path / "empty-lifecycle.db")
    empty_factory = SequencedFactory(())
    empty_chain = MachineExecutionChain(
        empty_repository,
        Reader(GCODE),
        empty_factory,
        _config(),
        _authority(),
        wall_now=lambda: NOW + timedelta(seconds=1),
    )
    try:
        assert empty_chain.initialize_and_reconcile()["execution"] is None
        assert empty_factory.opens == 0
    finally:
        empty_chain.close()

    first, repository, factory, _session, _current = _lifecycle_chain(
        tmp_path / "repeat",
        phase=MachinePhase.PREPARING_SESSION,
        milestone=StreamMilestone.NOT_STARTED,
    )
    first.initialize_and_reconcile()
    first.close()
    repeated_repository = SQLiteMachineRepository(repository._path)
    repeated_factory = SequencedFactory(())
    repeated = MachineExecutionChain(
        repeated_repository,
        Reader(GCODE),
        repeated_factory,
        _config(),
        _authority(service_epoch=_uuid(901)),
        identity_factory=lambda: _uuid(999),
        wall_now=lambda: NOW + timedelta(seconds=20),
    )
    try:
        before = repeated_repository.get_active_execution()
        assert before is not None
        projection = repeated.initialize_and_reconcile()
        after = repeated_repository.get_active_execution()
        assert after == before
        assert projection["execution"]["revision"] == before.revision  # type: ignore[index]
        job = (
            repeated_repository._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (before.job_id,),
            )
            .fetchone()
        )
        assert job == (JobState.RECOVERY_REQUIRED.value, 8)
        assert factory.opens == 0 and repeated_factory.opens == 0
    finally:
        repeated.close()

    chain, repository, factory, _session, _current = _lifecycle_chain(
        tmp_path / "clock",
        phase=MachinePhase.PREPARING_SESSION,
        milestone=StreamMilestone.NOT_STARTED,
    )
    chain._wall_now = lambda: NOW  # type: ignore[attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as rollback:
            chain.initialize_and_reconcile()
        assert rollback.value.payload.code == "MACHINE_LIFECYCLE_CLOCK_INVALID"
        assert factory.opens == 0
    finally:
        repository.close()


@pytest.mark.parametrize("invalid_kind", ["state", "snapshot"])
def test_c12_startup_rejects_invalid_retained_job_authority_without_adapter_open(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    _chain_value, repository, factory, _session, _current = _lifecycle_chain(
        tmp_path,
        phase=MachinePhase.PREPARING_SESSION,
        milestone=StreamMilestone.NOT_STARTED,
    )
    base_job = _job(_ready_snapshot())
    if invalid_kind == "state":
        invalid_job = replace(
            base_job,
            state=JobState.CANCELLED,
            revision=8,
            ready_snapshot=None,
            updated_at=NOW + timedelta(seconds=1),
        )
    else:
        snapshot = _ready_snapshot()
        invalid_job = _job(replace(snapshot, gcode=replace(snapshot.gcode, sha256="e" * 64)))

    class InvalidJobRepository(_RepositoryProxy):
        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def get_job(self, job_id: str) -> JobRecord:
                        assert job_id == invalid_job.job_id
                        return invalid_job

                yield Proxy()

    chain = _proxy_recovery_chain(InvalidJobRepository(repository), Reader(GCODE), factory)
    try:
        with pytest.raises(DrawingMachineError) as rejected:
            chain.initialize_and_reconcile()
        assert rejected.value.payload.code == "MACHINE_LIFECYCLE_JOB_INVALID"
        assert factory.opens == 0
    finally:
        repository.close()


def test_c12_unbound_close_and_close_lifecycle_persistence_failure_are_fatal(tmp_path: Path) -> None:
    chain, repository, factory, session, current = _lifecycle_chain(
        tmp_path / "unbound",
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        milestone=StreamMilestone.NOT_STARTED,
        close_result=RuntimeError("unbound close"),
    )
    chain._sessions[current.execution_id] = session  # type: ignore[attr-defined]
    try:
        assert chain._close_owned_sessions(None) is True  # type: ignore[attr-defined]
        assert session.close_calls == 0
        assert factory.opens == 0
    finally:
        repository.close()

    _base, repository, factory, session, current = _lifecycle_chain(
        tmp_path / "lifecycle-failure",
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        milestone=StreamMilestone.NOT_STARTED,
        close_result=RuntimeError("uncertain close"),
    )

    class FailLifecycleEvent(_RepositoryProxy):
        @contextmanager
        def transaction(self):
            with self.delegate.transaction() as transaction:

                class Proxy:
                    def __getattr__(self, name: str):
                        return getattr(transaction, name)

                    def record_lifecycle_event(self, *args: object, **kwargs: object) -> None:
                        del args, kwargs
                        raise RuntimeError("secret lifecycle persistence")

                yield Proxy()

    failed_chain = _proxy_recovery_chain(FailLifecycleEvent(repository), Reader(GCODE), factory)
    failed_chain._sessions[current.execution_id] = session  # type: ignore[attr-defined]
    try:
        with pytest.raises(DrawingMachineError) as fatal:
            failed_chain.shutdown_for_recovery()
        assert fatal.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
        assert fatal.value.__cause__ is None and fatal.value.__context__ is None
        assert session.close_calls == 1
        owner = repository.get_active_execution()
        assert owner is not None and owner.phase is MachinePhase.RECOVERY_REQUIRED
        assert failed_chain.retained_session_epochs() == (current.machine_session_epoch,)
    finally:
        repository.close()
