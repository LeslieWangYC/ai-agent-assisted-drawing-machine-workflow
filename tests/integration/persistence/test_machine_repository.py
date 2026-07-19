from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import drawingmachine.adapters.persistence.machine_sql as machine_sql
import drawingmachine.ports.machine_repository as machine_repository
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.adapters.persistence.migrator import _apply_migration, _bootstrap_ledger, _load_migrations
from drawingmachine.adapters.persistence.sqlite_repository import SQLiteRepository
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobState,
    JobTransition,
    ReadyToRunSnapshot,
    RequesterIdentity,
)
from drawingmachine.domain.jobs.models import MachineJobOutcomeTransition
from drawingmachine.domain.machine import (
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalBinding,
    MachineApprovalEvidenceV1,
    MachineAuditPayloadV1,
    MachineEvent,
    MachineEventPayloadV1,
    MachineExecution,
    MachineFailureEvidenceV1,
    MachineOutcome,
    MachineOutcomeKind,
    MachinePhase,
    MachineProgress,
    MachineRecoveryEvidence,
    MachineSessionSnapshot,
    OwnerRetirementEvidence,
    RecoveryDisposition,
    RecoveryIntent,
    RetirementDisposition,
    StreamMilestone,
    consume_approval,
    expire_approval,
    issue_approval,
    supersede_approval,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.machine_repository import MachineRequestKey, MachineRequestRecord

NOW = datetime(2026, 7, 12, 10, tzinfo=UTC)
DIGEST = "a" * 64
AUTOMATION = KernelMachinePrincipal(1100, 1100)
OPERATOR = KernelMachinePrincipal(2200, 2200)
AUTOMATION_PEER = AuthenticatedMachinePrincipal(1100, 1100, 31001)
OPERATOR_PEER = AuthenticatedMachinePrincipal(2200, 2200, 41001)


def canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def ready_snapshot(job_id: str = "job-1") -> ReadyToRunSnapshot:
    config = ConfigSnapshot("machine", "provider", 1, 1, 1, DIGEST, "b" * 64, "c" * 64)
    return ReadyToRunSnapshot(
        1,
        job_id,
        7,
        ArtifactRef("gcode", f"jobs/{job_id}/drawing.gcode", "d" * 64, 12, "text/x.gcode"),
        (),
        {"allowed": True},
        {"line_count": 2},
        {"allows_stream": True},
        config,
        {"stroke": "fixed"},
        "2.0.0",
    )


def seed_job(repository: SQLiteMachineRepository, job_id: str = "job-1") -> None:
    ready = ready_snapshot(job_id)
    repository._get_connection().execute(
        """
        INSERT INTO jobs (
            job_id, schema_version, kind, name, state, revision, request_json,
            config_snapshot_json, blocker_json, error_json, ready_snapshot_json,
            created_at, updated_at
        ) VALUES (?, 1, 'OFFLINE_WORKFLOW', ?, 'READY_TO_RUN', 7, ?, ?, NULL, NULL, ?, ?, ?)
        """,
        (
            job_id,
            f"fixture {job_id}",
            canonical({"source": "fixture"}),
            canonical(ready.config.to_json()),
            canonical(ready.to_json()),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )


def execution(
    execution_id: str = "execution-1",
    job_id: str = "job-1",
    **changes: Any,
) -> MachineExecution:
    values: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": execution_id,
        "job_id": job_id,
        "ready_revision": 7,
        "revision": 0,
        "phase": MachinePhase.PREPARING_SESSION,
        "service_epoch": "service-1",
        "machine_session_epoch": f"session-{execution_id}",
        "gcode_sha256": "d" * 64,
        "application_version": "2.0.0",
        "application_digest": DIGEST,
        "machine_digest": "b" * 64,
        "provider_digest": "c" * 64,
        "stream_milestone": StreamMilestone.NOT_STARTED,
        "recovery_intent": None,
        "predecessor_execution_id": None,
        "recovery_evidence": None,
        "retirement": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return MachineExecution(**values)


def session(value: MachineExecution) -> MachineSessionSnapshot:
    position = (0.0, 0.0, 0.0)
    return MachineSessionSnapshot(
        1,
        value.execution_id,
        value.machine_session_epoch,
        "Idle",
        position,
        position,
        position,
        position,
        True,
        value.updated_at,
    )


def event(
    value: MachineExecution,
    event_id: str,
    *,
    action: MachineAction | None = MachineAction.PREPARE_SESSION,
    request_id: str | None = None,
) -> MachineEvent:
    return MachineEvent(
        1,
        event_id,
        value.execution_id,
        value.job_id,
        value.revision,
        "machine.phase",
        value.phase,
        action,
        MachineEventPayloadV1(1, request_id, None, None),
        value.updated_at,
    )


def audit(value: MachineExecution, event_id: str, request_id: str | None = None) -> MachineAuditPayloadV1:
    return MachineAuditPayloadV1(
        1,
        "machine.phase",
        "allowed",
        value.execution_id,
        value.job_id,
        value.revision,
        request_id,
        None,
        event_id,
    )


def create(repository: SQLiteMachineRepository, value: MachineExecution, event_id: str = "machine-event-0") -> None:
    with repository.transaction() as transaction:
        transaction.create_execution(value, event=event(value, event_id), audit=audit(value, event_id))


def open_session(
    repository: SQLiteMachineRepository,
    value: MachineExecution,
    event_id: str = "session-opened",
) -> MachineExecution:
    opened = replace(
        value,
        revision=value.revision + 1,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        updated_at=value.updated_at + timedelta(seconds=1),
    )
    with repository.transaction() as transaction:
        transaction.record_opened_session(
            value.revision,
            opened,
            session=session(opened),
            event=event(opened, event_id),
            audit=audit(opened, event_id),
        )
    return opened


def advance_to_streaming(repository: SQLiteMachineRepository, value: MachineExecution) -> MachineExecution:
    current = open_session(repository, value, "stream-opened")
    steps = (
        (MachinePhase.HOMING, MachineAction.HOME),
        (MachinePhase.AWAITING_ZCAL_APPROVAL, MachineAction.HOME),
        (MachinePhase.Z_CALIBRATING, MachineAction.ZCAL),
        (MachinePhase.AWAITING_ZCONFIRM_APPROVAL, MachineAction.ZCAL),
        (MachinePhase.Z_CONFIRMING, MachineAction.ZCONFIRM),
        (MachinePhase.AWAITING_STREAM_APPROVAL, MachineAction.ZCONFIRM),
        (MachinePhase.STREAMING, MachineAction.STREAM),
    )
    for phase, action in steps:
        successor = replace(
            current,
            revision=current.revision + 1,
            phase=phase,
            updated_at=current.updated_at + timedelta(seconds=1),
        )
        event_id = f"stream-step-{successor.revision}"
        with repository.transaction() as transaction:
            transaction.transition_execution(
                current.revision,
                successor,
                event=event(successor, event_id, action=action),
                audit=audit(successor, event_id),
            )
        current = successor
    return current


def approval(value: MachineExecution, challenge_id: str = "123e4567-e89b-42d3-a456-426614174000"):
    binding = MachineApprovalBinding(
        value.execution_id,
        value.revision,
        MachineAction.HOME,
        None,
        MachinePhase.AWAITING_HOME_APPROVAL,
        value.service_epoch,
        value.machine_session_epoch,
        value.job_id,
        value.ready_revision,
        value.application_digest,
        value.machine_digest,
        value.provider_digest,
        value.gcode_sha256,
    )
    evidence = MachineApprovalEvidenceV1(1, MachineAction.HOME, MachinePhase.AWAITING_HOME_APPROVAL, session(value))
    return issue_approval(
        challenge_id=challenge_id,
        binding=binding,
        requester=AUTOMATION_PEER,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        evidence=evidence,
        issued_at=value.updated_at,
        monotonic_now=1000.0,
        ttl_seconds=60,
    )


def recovery_approval(
    value: MachineExecution,
    challenge_id: str,
    disposition: RecoveryDisposition,
):
    binding = MachineApprovalBinding(
        value.execution_id,
        value.revision,
        MachineAction.RECOVER,
        disposition,
        MachinePhase.RECOVERY_REQUIRED,
        value.service_epoch,
        value.machine_session_epoch,
        value.job_id,
        value.ready_revision,
        value.application_digest,
        value.machine_digest,
        value.provider_digest,
        value.gcode_sha256,
    )
    return issue_approval(
        challenge_id=challenge_id,
        binding=binding,
        requester=AUTOMATION_PEER,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        evidence=MachineApprovalEvidenceV1(
            1,
            MachineAction.RECOVER,
            MachinePhase.RECOVERY_REQUIRED,
            None,
        ),
        issued_at=value.updated_at,
        monotonic_now=1000.0,
        ttl_seconds=60,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteMachineRepository:
    instance = SQLiteMachineRepository(tmp_path / "machine.db")
    assert instance.initialize(applied_at=NOW.isoformat()) == 5
    seed_job(instance)
    try:
        yield instance
    finally:
        instance.close()


def test_machine_repository_migrates_hardware_authority_schema_and_is_idempotent(tmp_path: Path) -> None:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    try:
        assert repository.initialize(applied_at=NOW.isoformat()) == 5
        assert repository.initialize(applied_at=NOW.isoformat()) == 5
        assert repository.schema_tables() == frozenset(
            {
                "machine_approval_challenges",
                "machine_events",
                "machine_executions",
                "machine_progress_events",
                "machine_request_results",
                "machine_sessions",
            }
        )
    finally:
        repository.close()


def test_upgrade_from_0001_0002_to_final_schema_preserves_foundation(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        _bootstrap_ledger(connection)
        for migration in _load_migrations()[:2]:
            _apply_migration(connection, migration, applied_at=NOW.isoformat())
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (2,)
    finally:
        connection.close()

    repository = SQLiteMachineRepository(path)
    try:
        assert repository.initialize(applied_at=NOW.isoformat()) == 5
        assert repository._get_connection().execute("SELECT name FROM sqlite_master WHERE name = 'jobs'").fetchone()
    finally:
        repository.close()


def test_create_reads_session_event_and_enforces_global_owner(repository: SQLiteMachineRepository) -> None:
    first = execution()
    create(repository, first)

    assert repository.get_execution(first.execution_id) == first
    assert repository.get_active_execution() == first
    assert repository.get_session(first.execution_id) is None
    assert repository.get_outcome(first.execution_id) is None
    assert repository.list_events(first.execution_id) == (event(first, "machine-event-0"),)

    seed_job(repository, "job-2")
    second = execution("execution-2", "job-2")
    with pytest.raises(DrawingMachineError) as error, repository.transaction() as transaction:
        transaction.create_execution(
            second,
            event=event(second, "machine-event-2"),
            audit=audit(second, "machine-event-2"),
        )
    assert error.value.payload.code == "MACHINE_OWNER_CONFLICT"
    assert repository.get_execution(second.execution_id) is None


def test_phase_cas_is_one_step_and_late_event_conflict_rolls_back(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    advanced = open_session(repository, original, "machine-event-1")
    assert repository.get_execution(original.execution_id) == advanced

    stale = replace(advanced, revision=2, phase=MachinePhase.HOMING, updated_at=NOW + timedelta(seconds=2))
    with pytest.raises(DrawingMachineError) as error, repository.transaction() as transaction:
        transaction.transition_execution(
            0,
            stale,
            event=event(stale, "machine-event-stale", action=MachineAction.HOME),
            audit=audit(stale, "machine-event-stale"),
        )
    assert error.value.payload.code == "MACHINE_REVISION_CONFLICT"

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.transition_execution(
            1,
            stale,
            event=event(stale, "machine-event-0", action=MachineAction.HOME),
            audit=audit(stale, "machine-event-0"),
        )
    assert repository.get_execution(original.execution_id) == advanced


def test_challenge_issue_consume_and_supersede_are_exactly_once(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "awaiting")

    first = approval(awaiting)
    with repository.transaction() as transaction:
        transaction.issue_challenge(first)
    assert repository.get_challenge(first.challenge_id) == first

    decision = consume_approval(
        first,
        current_binding=first.binding,
        consumer=OPERATOR_PEER,
        operator_principal=OPERATOR,
        wall_now=awaiting.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    with repository.transaction() as transaction:
        transaction.decide_challenge(decision)
    assert repository.get_challenge(first.challenge_id) == decision.result
    with pytest.raises(DrawingMachineError) as replay, repository.transaction() as transaction:
        transaction.decide_challenge(decision)
    assert replay.value.payload.code == "MACHINE_APPROVAL_REPLAY"

    second = approval(awaiting, "123e4567-e89b-42d3-a456-426614174001")
    with repository.transaction() as transaction:
        transaction.issue_challenge(second)
    replacement = approval(awaiting, "123e4567-e89b-42d3-a456-426614174002")
    superseded = supersede_approval(
        second,
        wall_now=awaiting.updated_at + timedelta(seconds=2),
        monotonic_now=1002.0,
    )
    with repository.transaction() as transaction:
        transaction.issue_challenge(replacement, replacement=superseded)
    assert repository.get_challenge(second.challenge_id) == superseded.result
    assert repository.get_challenge(replacement.challenge_id) == replacement

    operator_requested = issue_approval(
        challenge_id="123e4567-e89b-42d3-a456-426614174003",
        binding=replacement.binding,
        requester=OPERATOR_PEER,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        evidence=replacement.evidence,
        issued_at=awaiting.updated_at,
        monotonic_now=1000.0,
        ttl_seconds=60,
    )
    later_superseded = supersede_approval(
        replacement,
        wall_now=awaiting.updated_at + timedelta(seconds=3),
        monotonic_now=1003.0,
    )
    with repository.transaction() as transaction:
        transaction.issue_challenge(operator_requested, replacement=later_superseded)
    assert repository.get_challenge(operator_requested.challenge_id) == operator_requested


def test_challenge_event_and_audit_must_be_atomic_before_insert(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "awaiting-atomic-audit")
    challenge = approval(awaiting)
    with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
        transaction.issue_challenge(
            challenge,
            event=event(awaiting, "challenge-event", action=MachineAction.HOME),
        )
    assert mismatch.value.payload.code == "MACHINE_EVENT_BINDING_MISMATCH"
    assert repository.get_challenge(challenge.challenge_id) is None


def test_request_dedup_completion_and_dispatch_claim_are_durable(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    key = MachineRequestKey(1100, 1100, "request-1")
    record = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.prepare",
        argument_digest=DIGEST,
        execution_id=original.execution_id,
        action=MachineAction.PREPARE_SESSION.value,
        admission_id="admission-1",
        dispatch_nonce="nonce-1",
        response=None,
        reserved_at=NOW,
        execution_revision=original.revision,
        service_epoch=original.service_epoch,
        machine_session_epoch=original.machine_session_epoch,
    )
    capability = machine_repository.MachineAdmissionCapability(
        key,
        "admission-1",
        "nonce-1",
        original.execution_id,
        MachineAction.PREPARE_SESSION,
        original.revision,
        original.service_epoch,
        original.machine_session_epoch,
    )
    with repository.transaction() as transaction:
        assert transaction.reserve_request(record).created is True
    with repository.transaction() as transaction:
        replay = transaction.reserve_request(replace(record, requester_pid=99999))
    assert replay.created is False
    assert replay.record == record

    with repository.transaction() as transaction:
        completed = transaction.complete_request(
            key, response={"accepted": True}, completed_at=NOW + timedelta(seconds=1)
        )
    assert completed.response == {"accepted": True}
    with repository.transaction() as transaction:
        claimed = transaction.claim_request(capability, claimed_at=NOW + timedelta(seconds=2))
    assert claimed.claimed_at == NOW + timedelta(seconds=2)
    assert repository.get_request(key) == claimed

    with pytest.raises(DrawingMachineError) as duplicate, repository.transaction() as transaction:
        transaction.claim_request(capability, claimed_at=NOW + timedelta(seconds=3))
    assert duplicate.value.payload.code == "MACHINE_ADMISSION_ALREADY_CLAIMED"
    with pytest.raises(DrawingMachineError) as conflict, repository.transaction() as transaction:
        transaction.reserve_request(replace(record, command="machine.stream"))
    assert conflict.value.payload.code == "REQUEST_ID_CONFLICT"


def test_injected_late_admission_conflict_cannot_claim_partial_authority(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    key = MachineRequestKey(1100, 1100, "late-claim-conflict")
    capability = machine_repository.MachineAdmissionCapability(
        key,
        "late-claim-admission",
        "late-claim-nonce",
        original.execution_id,
        MachineAction.PREPARE_SESSION,
        original.revision,
        original.service_epoch,
        original.machine_session_epoch,
    )
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.prepare",
        argument_digest=DIGEST,
        execution_id=original.execution_id,
        action=MachineAction.PREPARE_SESSION.value,
        admission_id=capability.admission_id,
        dispatch_nonce=capability.dispatch_nonce,
        response=None,
        reserved_at=NOW,
        execution_revision=original.revision,
        service_epoch=original.service_epoch,
        machine_session_epoch=original.machine_session_epoch,
    )
    with repository.transaction() as transaction:
        transaction.reserve_request(request)
    repository._get_connection().execute(
        """CREATE TRIGGER ignore_late_admission_claim
           BEFORE UPDATE OF claimed_at ON machine_request_results
           WHEN OLD.request_id = 'late-claim-conflict'
           BEGIN SELECT RAISE(IGNORE); END"""
    )
    try:
        with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
            transaction.claim_request(capability, claimed_at=NOW + timedelta(seconds=1))
        assert stale.value.payload.code == "MACHINE_ADMISSION_STALE"
    finally:
        repository._get_connection().execute("DROP TRIGGER ignore_late_admission_claim")
    persisted = repository.get_request(key)
    assert persisted is not None and persisted.claimed_at is None


def test_progress_is_append_only_and_rolls_back_with_transaction(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    streaming = advance_to_streaming(repository, original)
    progress = MachineProgress(
        1,
        original.execution_id,
        streaming.revision,
        2,
        1,
        1,
        0,
        50.0,
        4,
        "G1 X1",
        streaming.updated_at,
    )
    with repository.transaction() as transaction:
        transaction.append_progress("progress-1", progress)
    assert repository.list_progress(original.execution_id) == (progress,)

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.append_progress("progress-2", replace(progress, last_event_at=NOW + timedelta(seconds=1)))
        transaction.append_progress("progress-1", progress)
    assert repository.list_progress(original.execution_id) == (progress,)


def recovery(value: MachineExecution) -> MachineExecution:
    occurred = value.updated_at + timedelta(seconds=1)
    evidence = MachineRecoveryEvidence(
        1,
        value.execution_id,
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "SESSION_LOST",
        MachineFailureEvidenceV1(1, "SESSION_LOST", "session lost", "Idle", True),
        occurred,
    )
    return replace(
        value,
        revision=value.revision + 1,
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        recovery_evidence=evidence,
        updated_at=occurred,
    )


def post_stream_recovery(
    repository: SQLiteMachineRepository,
    value: MachineExecution,
) -> MachineExecution:
    streaming = advance_to_streaming(repository, value)
    first_write = replace(
        streaming,
        revision=streaming.revision + 1,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        updated_at=streaming.updated_at + timedelta(seconds=1),
    )
    with repository.transaction() as transaction:
        transaction.transition_execution(
            streaming.revision,
            first_write,
            event=event(first_write, "post-stream-first-write", action=MachineAction.STREAM),
            audit=audit(first_write, "post-stream-first-write"),
        )
    confirmed = replace(
        first_write,
        revision=first_write.revision + 1,
        phase=MachinePhase.AWAITING_HOME_Z_APPROVAL,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        updated_at=first_write.updated_at + timedelta(seconds=1),
    )
    with repository.transaction() as transaction:
        transaction.transition_execution(
            first_write.revision,
            confirmed,
            event=event(confirmed, "post-stream-confirmed", action=MachineAction.STREAM),
            audit=audit(confirmed, "post-stream-confirmed"),
        )
    occurred = confirmed.updated_at + timedelta(seconds=1)
    evidence = MachineRecoveryEvidence(
        1,
        confirmed.execution_id,
        RecoveryIntent.POST_STREAM_SAFE_HOME,
        StreamMilestone.STREAM_CONFIRMED,
        "SESSION_LOST",
        MachineFailureEvidenceV1(1, "SESSION_LOST", "session lost", "Idle", True),
        occurred,
    )
    failed = replace(
        confirmed,
        revision=confirmed.revision + 1,
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        recovery_evidence=evidence,
        updated_at=occurred,
    )
    with repository.transaction() as transaction:
        transaction.transition_execution(
            confirmed.revision,
            failed,
            event=event(failed, "post-stream-recovery", action=MachineAction.RECOVER),
            audit=audit(failed, "post-stream-recovery"),
        )
    return failed


def mark_job_recovery(repository: SQLiteMachineRepository, job_id: str = "job-1") -> None:
    repository._get_connection().execute(
        "UPDATE jobs SET state = 'RECOVERY_REQUIRED', revision = 8, updated_at = ? WHERE job_id = ?",
        ((NOW + timedelta(seconds=1)).isoformat(), job_id),
    )


def test_release_retires_latch_without_deleting_evidence(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            0,
            failed,
            event=event(failed, "failed"),
            audit=audit(failed, "failed"),
        )
    released_at = failed.updated_at + timedelta(seconds=1)
    released = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.RELEASED,
            released_at,
        ),
        updated_at=released_at,
    )
    with repository.transaction() as transaction:
        transaction.release_owner(
            1,
            released,
            event=event(released, "released", action=MachineAction.RECOVER),
            audit=audit(released, "released"),
        )
    assert repository.get_active_execution() is None
    assert repository.get_execution(original.execution_id) == released


def test_atomic_predecessor_successor_transfer(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            0,
            failed,
            event=event(failed, "failed"),
            audit=audit(failed, "failed"),
        )
    mark_job_recovery(repository)
    transfer_at = failed.updated_at + timedelta(seconds=1)
    successor = execution(
        "execution-2",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        phase=MachinePhase.PREPARING_SESSION,
        service_epoch="successor-service-epoch",
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    predecessor = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            successor.execution_id,
        ),
        updated_at=transfer_at,
    )
    with repository.transaction() as transaction:
        transaction.transfer_owner(
            1,
            predecessor,
            successor,
            predecessor_event=event(predecessor, "transferred", action=MachineAction.RECOVER),
            successor_event=event(successor, "successor", action=MachineAction.RECOVER),
            predecessor_audit=audit(predecessor, "transferred"),
            successor_audit=audit(successor, "successor"),
        )
    assert repository.get_execution(predecessor.execution_id) == predecessor
    assert repository.get_active_execution() == successor
    assert repository.get_session(successor.execution_id) is None
    opened = open_session(repository, successor, "successor-session-opened")
    assert repository.get_active_execution() == opened
    assert repository.get_session(successor.execution_id) == session(opened)


def test_recovery_admission_claims_only_the_exact_transferred_restart_successor(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            original.revision,
            failed,
            event=event(failed, "recover-admission-failed"),
            audit=audit(failed, "recover-admission-failed"),
        )
    mark_job_recovery(repository)
    transfer_at = failed.updated_at + timedelta(seconds=1)
    successor = execution(
        "recover-admission-successor",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        service_epoch="recover-admission-service",
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    predecessor = replace(
        failed,
        revision=failed.revision + 1,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            successor.execution_id,
        ),
        updated_at=transfer_at,
    )
    key = MachineRequestKey(1100, 1100, "recover-successor-admission")
    capability = machine_repository.MachineAdmissionCapability(
        key=key,
        admission_id="recover-successor-admission-id",
        dispatch_nonce="recover-successor-dispatch-nonce",
        execution_id=successor.execution_id,
        action=MachineAction.RECOVER,
        execution_revision=successor.revision,
        service_epoch=successor.service_epoch,
        machine_session_epoch=successor.machine_session_epoch,
        recovery_disposition=RecoveryDisposition.RESTART_SEQUENCE,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id=failed.execution_id,
    )
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.recover",
        argument_digest=DIGEST,
        execution_id=successor.execution_id,
        action=MachineAction.RECOVER.value,
        execution_revision=successor.revision,
        service_epoch=successor.service_epoch,
        machine_session_epoch=successor.machine_session_epoch,
        admission_id=capability.admission_id,
        dispatch_nonce=capability.dispatch_nonce,
        response=None,
        reserved_at=transfer_at,
        recovery_disposition=RecoveryDisposition.RESTART_SEQUENCE,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id=failed.execution_id,
    )
    phase_key = MachineRequestKey(1100, 1100, "recover-successor-phase-drift")
    phase_capability = replace(
        capability,
        key=phase_key,
        admission_id="recover-phase-admission-id",
        dispatch_nonce="recover-phase-dispatch-nonce",
    )
    phase_request = replace(
        request,
        key=phase_key,
        admission_id=phase_capability.admission_id,
        dispatch_nonce=phase_capability.dispatch_nonce,
    )
    with repository.transaction() as transaction:
        transaction.transfer_owner(
            failed.revision,
            predecessor,
            successor,
            predecessor_event=event(predecessor, "recover-admission-transferred", action=MachineAction.RECOVER),
            successor_event=event(successor, "recover-admission-successor", action=MachineAction.RECOVER),
            predecessor_audit=audit(predecessor, "recover-admission-transferred"),
            successor_audit=audit(successor, "recover-admission-successor"),
        )
        transaction.reserve_request(request)
        transaction.reserve_request(phase_request)
    mismatches = (
        replace(capability, recovery_disposition=RecoveryDisposition.RELEASE),
        replace(
            capability,
            recovery_disposition=RecoveryDisposition.SAFE_HOME,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        ),
        replace(capability, predecessor_execution_id="wrong-predecessor"),
        replace(capability, service_epoch="wrong-service-epoch"),
        replace(capability, machine_session_epoch="wrong-session-epoch"),
        replace(
            capability,
            recovery_disposition=RecoveryDisposition.RELEASE,
            recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
        ),
    )
    for mismatch in mismatches:
        with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
            transaction.claim_request(mismatch, claimed_at=transfer_at + timedelta(seconds=1))
        assert rejected.value.payload.code == "MACHINE_ADMISSION_MISMATCH"
    with repository.transaction() as transaction:
        claimed = transaction.claim_request(capability, claimed_at=transfer_at + timedelta(seconds=1))
    assert claimed.claimed_at == transfer_at + timedelta(seconds=1)
    assert claimed.recovery_disposition is RecoveryDisposition.RESTART_SEQUENCE
    assert claimed.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
    assert claimed.predecessor_execution_id == failed.execution_id
    open_session(repository, successor, "recover-successor-opened")
    with pytest.raises(DrawingMachineError) as phase_stale, repository.transaction() as transaction:
        transaction.claim_request(phase_capability, claimed_at=transfer_at + timedelta(seconds=2))
    assert phase_stale.value.payload.code == "MACHINE_ADMISSION_STALE"


def test_safe_home_recovery_admission_claims_exact_post_stream_successor(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = post_stream_recovery(repository, original)
    mark_job_recovery(repository)
    transfer_at = failed.updated_at + timedelta(seconds=1)
    successor = execution(
        "safe-home-admission-successor",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        service_epoch="safe-home-admission-service",
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    predecessor = replace(
        failed,
        revision=failed.revision + 1,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            successor.execution_id,
        ),
        updated_at=transfer_at,
    )
    key = MachineRequestKey(1100, 1100, "safe-home-successor-admission")
    capability = machine_repository.MachineAdmissionCapability(
        key=key,
        admission_id="safe-home-successor-admission-id",
        dispatch_nonce="safe-home-successor-dispatch-nonce",
        execution_id=successor.execution_id,
        action=MachineAction.RECOVER,
        execution_revision=successor.revision,
        service_epoch=successor.service_epoch,
        machine_session_epoch=successor.machine_session_epoch,
        recovery_disposition=RecoveryDisposition.SAFE_HOME,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        predecessor_execution_id=failed.execution_id,
    )
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.recover",
        argument_digest=DIGEST,
        execution_id=successor.execution_id,
        action=MachineAction.RECOVER.value,
        execution_revision=successor.revision,
        service_epoch=successor.service_epoch,
        machine_session_epoch=successor.machine_session_epoch,
        admission_id=capability.admission_id,
        dispatch_nonce=capability.dispatch_nonce,
        response=None,
        reserved_at=transfer_at,
        recovery_disposition=RecoveryDisposition.SAFE_HOME,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        predecessor_execution_id=failed.execution_id,
    )
    with repository.transaction() as transaction:
        transaction.transfer_owner(
            failed.revision,
            predecessor,
            successor,
            predecessor_event=event(predecessor, "safe-home-admission-transferred", action=MachineAction.RECOVER),
            successor_event=event(successor, "safe-home-admission-successor", action=MachineAction.RECOVER),
            predecessor_audit=audit(predecessor, "safe-home-admission-transferred"),
            successor_audit=audit(successor, "safe-home-admission-successor"),
        )
        transaction.reserve_request(request)
    with repository.transaction() as transaction:
        claimed = transaction.claim_request(capability, claimed_at=transfer_at + timedelta(seconds=1))
    assert claimed.claimed_at == transfer_at + timedelta(seconds=1)
    assert claimed.recovery_disposition is RecoveryDisposition.SAFE_HOME
    assert claimed.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
    for assignment in (
        "recovery_disposition = 'RELEASE'",
        "recovery_intent = 'PRE_STREAM_RESTART'",
        "predecessor_execution_id = execution_id",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="authority is immutable"):
            repository._get_connection().execute(
                f"""UPDATE machine_request_results SET {assignment}
                    WHERE principal_uid = 1100 AND principal_gid = 1100 AND request_id = ?""",
                (key.request_id,),
            )


def job_outcome_audit(job_event: JobEvent) -> AuditRecord:
    requester = RequesterIdentity("SERVICE", None, None, None)
    return AuditRecord(
        job_event.event_id,
        job_event.event_type,
        job_event.request_id,
        {
            "schema_version": 1,
            "requester": requester.to_json(),
            "job_id": job_event.job_id,
            "command_summary": {
                "name": "machine.outcome",
                "event_type": job_event.event_type,
                "job_kind": "OFFLINE_WORKFLOW",
            },
            "prior_state": job_event.prior_state.value if job_event.prior_state is not None else None,
            "result_state": job_event.result_state.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "2.0.0",
            "protocol_version": 1,
        },
    )


def outcome_parts(original: MachineExecution):
    failed = recovery(original)
    ready = ready_snapshot(original.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_recovery_required",
        ready,
    )
    job_event = JobEvent(
        "job-outcome",
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    return failed, outcome, transition, job_event


def test_machine_and_retained_snapshot_job_outcome_commit_together(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    failed, outcome, transition, job_event = outcome_parts(original)
    with repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    jobs = SQLiteRepository(repository._path, clock=type("Clock", (), {"now": lambda self: NOW})())
    try:
        job = jobs.get_job(failed.job_id)
        assert job is not None
        assert job.state is JobState.RECOVERY_REQUIRED
        assert job.ready_snapshot == transition.ready_snapshot
    finally:
        jobs.close()
    row = (
        repository._get_connection()
        .execute(
            "SELECT outcome_kind, outcome_json FROM machine_executions WHERE execution_id = ?",
            (failed.execution_id,),
        )
        .fetchone()
    )
    assert row is not None and row[0] == "RECOVERY_REQUIRED" and row[1] is not None
    assert repository.get_outcome(failed.execution_id) == outcome

    completed = MachineOutcome(
        1,
        "completed-execution",
        "job-1",
        1,
        MachineOutcomeKind.COMPLETED,
        ready_snapshot(),
        None,
        NOW,
    )
    completed_json = machine_sql._MachineTransaction._outcome_json(completed)
    assert machine_sql._outcome_from_json(canonical(completed_json)) == completed


def test_machine_outcome_rejects_stale_and_mismatched_authority(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    failed, outcome, transition, job_event = outcome_parts(original)

    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            99,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    assert stale.value.payload.code == "MACHINE_REVISION_CONFLICT"

    wrong_transition = replace(transition, expected_revision=8)
    with pytest.raises(DrawingMachineError) as job_authority, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=wrong_transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    assert job_authority.value.payload.code == "MACHINE_JOB_CONFLICT"

    wrong_identity = replace(job_event, job_id="other-job")
    with pytest.raises(DrawingMachineError) as event_identity, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=wrong_identity,
            job_audit=job_outcome_audit(wrong_identity),
        )
    assert event_identity.value.payload.code == "MACHINE_JOB_CONFLICT"

    wrong_state = replace(job_event, prior_state=JobState.RECOVERY_REQUIRED)
    with pytest.raises(DrawingMachineError) as event_state, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=wrong_state,
            job_audit=job_outcome_audit(wrong_state),
        )
    assert event_state.value.payload.code == "MACHINE_JOB_CONFLICT"

    wrong_outcome = replace(outcome, execution_revision=2)
    with pytest.raises(DrawingMachineError) as outcome_binding:
        machine_sql._MachineTransaction._validate_outcome(failed, wrong_outcome, transition)
    assert outcome_binding.value.payload.code == "MACHINE_OUTCOME_BINDING_MISMATCH"


def test_machine_outcome_missing_job_and_late_conflict_leave_no_partial_rows(tmp_path: Path) -> None:
    missing_repository = SQLiteMachineRepository(tmp_path / "missing-job.db")
    assert missing_repository.initialize(applied_at=NOW.isoformat()) == 5
    seed_job(missing_repository)
    original = execution()
    create(missing_repository, original)
    failed, outcome, transition, job_event = outcome_parts(original)
    connection = missing_repository._get_connection()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM jobs WHERE job_id = 'job-1'")
    with pytest.raises(DrawingMachineError) as missing, missing_repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-outcome"),
            machine_audit=audit(failed, "machine-outcome"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    assert missing.value.payload.code == "MACHINE_JOB_CONFLICT"
    missing_repository.close()

    repository = SQLiteMachineRepository(tmp_path / "late-conflict.db")
    assert repository.initialize(applied_at=NOW.isoformat()) == 5
    seed_job(repository)
    original = execution()
    create(repository, original)
    failed, outcome, transition, job_event = outcome_parts(original)
    with pytest.raises(DrawingMachineError) as late, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            0,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "machine-event-0"),
            machine_audit=audit(failed, "machine-event-0"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    assert late.value.payload.code == "MACHINE_PERSISTENCE_CONFLICT"
    assert repository.get_execution(original.execution_id) == original
    jobs = SQLiteRepository(repository._path, clock=type("Clock", (), {"now": lambda self: NOW})())
    try:
        job = jobs.get_job("job-1")
        assert job is not None and job.state is JobState.READY_TO_RUN
    finally:
        jobs.close()
    repository.close()


def test_machine_recovery_outcome_rejects_misbound_session_evidence(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed, outcome, transition, job_event = outcome_parts(original)
    wrong_session = replace(session(failed), execution_id="different-execution")
    with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
        transaction.commit_machine_outcome(
            original.revision,
            failed,
            outcome=outcome,
            job_transition=transition,
            machine_event=event(failed, "misbound-session"),
            machine_audit=audit(failed, "misbound-session"),
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
            session=wrong_session,
        )
    assert mismatch.value.payload.code == "MACHINE_SESSION_BINDING_MISMATCH"
    assert repository.get_execution(original.execution_id) == original


def test_database_rejects_cancellation_while_owner_is_active(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    clock = type("Clock", (), {"now": lambda self: NOW + timedelta(seconds=1)})()
    jobs = SQLiteRepository(repository._path, clock=clock)
    job = jobs.get_job("job-1")
    assert job is not None
    transition = JobTransition("job-1", 7, JobState.CANCELLED, "cancel-1", "job.cancelled")
    requester = RequesterIdentity("LOCAL_PEER", 31001, 1100, 1100)
    job_event = JobEvent(
        "cancel-event",
        "job-1",
        8,
        "job.cancelled",
        JobState.READY_TO_RUN,
        JobState.CANCELLED,
        "cancel-1",
        "LOCAL_PEER",
        {"requester": requester.to_json()},
        NOW + timedelta(seconds=1),
    )
    job_audit = AuditRecord(
        "cancel-event",
        "job.cancelled",
        "cancel-1",
        {
            "schema_version": 1,
            "requester": requester.to_json(),
            "job_id": "job-1",
            "command_summary": {
                "name": "job.cancel",
                "event_type": "job.cancelled",
                "job_kind": "OFFLINE_WORKFLOW",
            },
            "prior_state": "READY_TO_RUN",
            "result_state": "CANCELLED",
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "2.0.0",
            "protocol_version": 1,
        },
    )
    try:
        with pytest.raises(DrawingMachineError) as denied, jobs.transaction() as transaction:
            transaction.transition_job(transition, artifacts=(), event=job_event, audit=job_audit)
        assert denied.value.payload.code == "MACHINE_ACTIVE_OWNER_CANCEL_DENIED"
        assert jobs.get_job("job-1") == job
    finally:
        jobs.close()


def test_db_evidence_tables_are_immutable_and_foreign_keys_are_enabled(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    open_session(repository, original, "opened-for-immutability")
    connection = repository._get_connection()
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    statements = (
        "UPDATE machine_sessions SET observed_at = 'later'",
        "DELETE FROM machine_events",
        "UPDATE machine_executions SET job_id = 'other', revision = 1",
    )
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO machine_sessions VALUES ('missing', 'epoch', '{}', ?)",
            (NOW.isoformat(),),
        )


def test_transaction_handle_is_inactive_after_exit(repository: SQLiteMachineRepository) -> None:
    original = execution()
    with repository.transaction() as retained:
        retained.create_execution(
            original,
            event=event(original, "created"),
            audit=audit(original, "created"),
        )
    with pytest.raises(DrawingMachineError) as inactive:
        retained.append_progress(
            "late",
            MachineProgress(1, original.execution_id, 0, 0, 0, 0, 0, 100.0, None, None, NOW),
        )
    assert inactive.value.payload.code == "MACHINE_TRANSACTION_INACTIVE"


def test_lifecycle_event_requires_exact_active_recovery_and_maps_duplicate_event_conflict(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    with pytest.raises(DrawingMachineError) as invalid, repository.transaction() as transaction:
        transaction.record_lifecycle_event(
            original,
            event=event(original, "invalid-lifecycle"),
            audit=audit(original, "invalid-lifecycle"),
        )
    assert invalid.value.payload.code == "MACHINE_LIFECYCLE_EVENT_INVALID"

    failed = recovery(original)
    duplicate_id = "duplicate-lifecycle"
    with repository.transaction() as transaction:
        transaction.transition_execution(
            original.revision,
            failed,
            event=event(failed, duplicate_id),
            audit=audit(failed, duplicate_id),
        )
    with pytest.raises(DrawingMachineError) as duplicate, repository.transaction() as transaction:
        transaction.record_lifecycle_event(
            failed,
            event=event(failed, duplicate_id),
            audit=audit(failed, duplicate_id),
        )
    assert duplicate.value.payload.code == "MACHINE_PERSISTENCE_CONFLICT"


def test_connection_is_owned_by_its_creator_thread(repository: SQLiteMachineRepository) -> None:
    errors: list[DrawingMachineError] = []

    def cross_thread_read() -> None:
        try:
            repository.get_active_execution()
        except DrawingMachineError as error:
            errors.append(error)

    thread = threading.Thread(target=cross_thread_read)
    thread.start()
    thread.join()
    assert [error.payload.code for error in errors] == ["MACHINE_REPOSITORY_THREAD_VIOLATION"]


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("machine_approval_one_pending_action", "MACHINE_APPROVAL_PENDING_CONFLICT"),
        ("active machine ownership prevents job cancellation", "MACHINE_ACTIVE_OWNER_CANCEL_DENIED"),
        ("machine execution revision", "MACHINE_REVISION_CONFLICT"),
        ("machine approval decision is one-time", "MACHINE_APPROVAL_REPLAY"),
        ("machine approval authority is stale", "MACHINE_APPROVAL_STALE"),
        ("machine progress requires current STREAMING authority", "MACHINE_PROGRESS_STALE"),
        (
            "successor machine execution must preserve predecessor frozen authority",
            "MACHINE_TRANSFER_INVALID",
        ),
        ("some other constraint", "MACHINE_PERSISTENCE_CONFLICT"),
    ],
)
def test_integrity_conflicts_have_typed_fail_closed_mappings(message: str, code: str) -> None:
    assert machine_sql._conflict(sqlite3.IntegrityError(message)).payload.code == code


def test_corrupt_json_and_session_projection_fail_closed() -> None:
    with pytest.raises(DrawingMachineError) as scalar:
        machine_sql._json_object("[]")
    assert scalar.value.payload.code == "MACHINE_PERSISTENCE_CORRUPT"
    payload = machine_sql._session_json(session(execution()))
    payload["mpos"] = [0.0, 0.0]
    with pytest.raises(DrawingMachineError) as position:
        machine_sql._session_from_json(canonical(payload))
    assert position.value.payload.code == "MACHINE_PERSISTENCE_CORRUPT"


def test_create_rejects_noninitial_authority_and_session_mismatch(repository: SQLiteMachineRepository) -> None:
    original = execution()
    invalid_values = (
        replace(original, revision=1, phase=MachinePhase.AWAITING_HOME_APPROVAL),
        replace(original, phase=MachinePhase.AWAITING_HOME_APPROVAL),
        replace(
            original,
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            predecessor_execution_id="predecessor",
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        ),
    )
    for invalid in invalid_values:
        with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
            transaction.create_execution(
                invalid,
                event=event(invalid, f"invalid-{invalid.phase.value}"),
                audit=audit(invalid, f"invalid-{invalid.phase.value}"),
            )
        assert rejected.value.payload.code == "MACHINE_EXECUTION_INVALID"

    create(repository, original, "valid-create")
    awaiting = replace(
        original,
        revision=1,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        updated_at=NOW + timedelta(seconds=1),
    )
    wrong_session = replace(session(awaiting), machine_session_epoch="different-session")
    with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
        transaction.record_opened_session(
            0,
            awaiting,
            session=wrong_session,
            event=event(awaiting, "mismatch"),
            audit=audit(awaiting, "mismatch"),
        )
    assert mismatch.value.payload.code == "MACHINE_SESSION_BINDING_MISMATCH"


def test_event_binding_mismatch_rolls_back_execution(repository: SQLiteMachineRepository) -> None:
    original = execution()
    with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
        transaction.create_execution(
            original,
            event=event(original, "event"),
            audit=replace(audit(original, "event"), job_id="other-job"),
        )
    assert mismatch.value.payload.code == "MACHINE_EVENT_BINDING_MISMATCH"
    assert repository.get_execution(original.execution_id) is None


def test_challenge_conflict_and_invalid_decisions_are_typed(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "await")
    first = approval(awaiting)
    consumed = consume_approval(
        first,
        current_binding=first.binding,
        consumer=OPERATOR_PEER,
        operator_principal=OPERATOR,
        wall_now=awaiting.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    with pytest.raises(DrawingMachineError) as invalid, repository.transaction() as transaction:
        transaction.issue_challenge(consumed.result)
    assert invalid.value.payload.code == "MACHINE_APPROVAL_INVALID"

    with repository.transaction() as transaction:
        transaction.issue_challenge(first)
    second = approval(awaiting, "123e4567-e89b-42d3-a456-426614174010")
    with pytest.raises(DrawingMachineError) as pending, repository.transaction() as transaction:
        transaction.issue_challenge(second)
    assert pending.value.payload.code == "MACHINE_APPROVAL_PENDING_CONFLICT"

    with pytest.raises(DrawingMachineError) as bad_decision, repository.transaction() as transaction:
        transaction.issue_challenge(second, replacement=consumed)
    assert bad_decision.value.payload.code == "MACHINE_APPROVAL_INVALID"

    missing = approval(awaiting, "123e4567-e89b-42d3-a456-426614174011")
    missing_supersede = supersede_approval(
        missing,
        wall_now=awaiting.updated_at + timedelta(seconds=2),
        monotonic_now=1002.0,
    )
    with pytest.raises(DrawingMachineError) as replay, repository.transaction() as transaction:
        transaction.issue_challenge(second, replacement=missing_supersede)
    assert replay.value.payload.code == "MACHINE_APPROVAL_REPLAY"

    decision = consume_approval(
        first,
        current_binding=first.binding,
        consumer=OPERATOR_PEER,
        operator_principal=OPERATOR,
        wall_now=awaiting.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    repository._get_connection().execute(
        """CREATE TRIGGER force_challenge_integrity BEFORE UPDATE ON machine_approval_challenges
           BEGIN SELECT RAISE(ABORT, 'machine approval decision is one-time'); END"""
    )
    try:
        with pytest.raises(DrawingMachineError) as forced, repository.transaction() as transaction:
            transaction.decide_challenge(decision)
        assert forced.value.payload.code == "MACHINE_APPROVAL_REPLAY"
    finally:
        repository._get_connection().execute("DROP TRIGGER force_challenge_integrity")


def test_request_invalid_completion_and_foreign_key_conflicts(repository: SQLiteMachineRepository) -> None:
    valid = MachineRequestRecord(
        MachineRequestKey(1100, 1100, "request"),
        31001,
        "machine.home",
        DIGEST,
        None,
        None,
        None,
        None,
        None,
        NOW,
    )
    invalid_values = (
        replace(valid, key=MachineRequestKey(True, 1100, "request")),
        replace(valid, key=MachineRequestKey(-1, 1100, "request")),
        replace(valid, key=MachineRequestKey(1100, True, "request")),
        replace(valid, key=MachineRequestKey(1100, -1, "request")),
        replace(valid, requester_pid=True),
        replace(valid, requester_pid=0),
        replace(valid, key=MachineRequestKey(1100, 1100, "")),
        replace(valid, command=""),
        replace(valid, argument_digest="bad"),
        replace(valid, claimed_at=NOW),
        replace(valid, invalidated_at=NOW, invalidation_reason="old"),
        replace(valid, invalidation_reason="old"),
        replace(valid, execution_id="partial"),
        replace(valid, dispatch_nonce="nonce-without-admission"),
        replace(valid, admission_id="admission-without-binding", dispatch_nonce="nonce"),
    )
    for invalid in invalid_values:
        with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
            transaction.reserve_request(invalid)
        assert rejected.value.payload.code == "MACHINE_REQUEST_INVALID"

    with repository.transaction() as transaction:
        assert transaction.reserve_request(valid).created is True

    bound = replace(
        valid,
        key=MachineRequestKey(1100, 1100, "bound-invalid"),
        execution_id="missing",
        action=MachineAction.PREPARE_SESSION.value,
        execution_revision=0,
        service_epoch="service-1",
        machine_session_epoch="session-1",
    )
    bound_invalid_values = (
        replace(bound, execution_revision=True),
        replace(bound, execution_revision=-1),
        replace(bound, execution_id=""),
        replace(bound, action=""),
        replace(bound, action="UNKNOWN"),
        replace(bound, service_epoch=""),
        replace(bound, machine_session_epoch=""),
        replace(bound, admission_id="", dispatch_nonce="nonce"),
        replace(bound, admission_id="admission", dispatch_nonce=""),
        replace(bound, recovery_intent=RecoveryIntent.PRE_STREAM_RESTART),
        replace(
            bound,
            action=MachineAction.RECOVER.value,
            recovery_disposition=RecoveryDisposition.RESTART_SEQUENCE,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        ),
    )
    for invalid in bound_invalid_values:
        with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
            transaction.reserve_request(invalid)
        assert rejected.value.payload.code == "MACHINE_REQUEST_INVALID"

    with pytest.raises(DrawingMachineError) as unbound_recovery, repository.transaction() as transaction:
        transaction.reserve_request(replace(valid, recovery_disposition=RecoveryDisposition.RELEASE))
    assert unbound_recovery.value.payload.code == "MACHINE_REQUEST_INVALID"

    missing_execution = replace(
        valid,
        key=MachineRequestKey(1100, 1100, "missing-execution"),
        execution_id="missing",
        action=MachineAction.PREPARE_SESSION.value,
        execution_revision=0,
        service_epoch="service-1",
        machine_session_epoch="missing-session",
        admission_id="missing-admission",
        dispatch_nonce="missing-nonce",
    )
    with pytest.raises(DrawingMachineError) as foreign_key, repository.transaction() as transaction:
        transaction.reserve_request(missing_execution)
    assert foreign_key.value.payload.code == "MACHINE_PERSISTENCE_CONFLICT"

    missing_key = MachineRequestKey(1100, 1100, "missing-result")
    with pytest.raises(DrawingMachineError) as missing_result, repository.transaction() as transaction:
        transaction.complete_request(missing_key, response={}, completed_at=NOW)
    assert missing_result.value.payload.code == "MACHINE_REQUEST_RESULT_CONFLICT"


def test_release_and_transfer_validate_authority_before_writes(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(0, failed, event=event(failed, "failed"), audit=audit(failed, "failed"))
    mark_job_recovery(repository)
    with pytest.raises(DrawingMachineError) as release, repository.transaction() as transaction:
        transaction.release_owner(1, failed, event=event(failed, "release"), audit=audit(failed, "release"))
    assert release.value.payload.code == "MACHINE_RELEASE_INVALID"

    transfer_at = failed.updated_at + timedelta(seconds=1)
    successor = execution(
        "successor",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        phase=MachinePhase.PREPARING_SESSION,
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    predecessor = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            successor.execution_id,
        ),
        updated_at=transfer_at,
    )
    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.transfer_owner(
            0,
            predecessor,
            successor,
            predecessor_event=event(predecessor, "pre"),
            successor_event=event(successor, "successor"),
            predecessor_audit=audit(predecessor, "pre"),
            successor_audit=audit(successor, "successor"),
        )
    assert stale.value.payload.code == "MACHINE_REVISION_CONFLICT"

    wrong_successor = replace(successor, predecessor_execution_id="different")
    with pytest.raises(DrawingMachineError) as binding, repository.transaction() as transaction:
        transaction.transfer_owner(
            1,
            predecessor,
            wrong_successor,
            predecessor_event=event(predecessor, "pre"),
            successor_event=event(wrong_successor, "successor"),
            predecessor_audit=audit(predecessor, "pre"),
            successor_audit=audit(wrong_successor, "successor"),
        )
    assert binding.value.payload.code == "MACHINE_TRANSFER_INVALID"

    same_session_successor = replace(successor, machine_session_epoch=failed.machine_session_epoch)
    with pytest.raises(DrawingMachineError) as successor_invalid, repository.transaction() as transaction:
        transaction.transfer_owner(
            1,
            predecessor,
            same_session_successor,
            predecessor_event=event(predecessor, "pre"),
            successor_event=event(same_session_successor, "successor"),
            predecessor_audit=audit(predecessor, "pre"),
            successor_audit=audit(same_session_successor, "successor"),
        )
    assert successor_invalid.value.payload.code == "MACHINE_TRANSFER_INVALID"

    with pytest.raises(DrawingMachineError) as late, repository.transaction() as transaction:
        transaction.transfer_owner(
            1,
            predecessor,
            successor,
            predecessor_event=event(predecessor, "pre"),
            successor_event=event(successor, "machine-event-0"),
            predecessor_audit=audit(predecessor, "pre"),
            successor_audit=audit(successor, "machine-event-0"),
        )
    assert late.value.payload.code == "MACHINE_PERSISTENCE_CONFLICT"
    assert repository.get_execution(failed.execution_id) == failed
    assert repository.get_execution(successor.execution_id) is None

    with pytest.raises(DrawingMachineError) as private_stale, repository.transaction() as transaction:
        transaction._update_execution(99, failed)  # type: ignore[attr-defined]
    assert private_stale.value.payload.code == "MACHINE_REVISION_CONFLICT"


def test_empty_reads_close_and_constructor_edges(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SQLiteMachineRepository(tmp_path / "negative.db", busy_timeout_ms=-1)
    repository = SQLiteMachineRepository(tmp_path / "empty.db")
    repository.close()
    assert repository.initialize(applied_at=NOW.isoformat()) == 5
    assert repository.get_execution("missing") is None
    assert repository.get_active_execution() is None
    assert repository.get_session("missing") is None
    assert repository.get_outcome("missing") is None
    assert repository.get_challenge("missing") is None
    assert repository.get_request(MachineRequestKey(1, 1, "missing")) is None
    assert repository.list_events("missing") == ()
    assert repository.list_progress("missing") == ()
    repository.close()


def test_corrupt_multiple_active_owners_are_detected(repository: SQLiteMachineRepository) -> None:
    original = execution()
    create(repository, original)
    connection = repository._get_connection()
    connection.execute("DROP INDEX machine_executions_one_global_active_owner")
    connection.execute(
        f"""
        INSERT INTO machine_executions ({machine_sql._EXECUTION_COLUMNS})
        SELECT 'execution-corrupt', schema_version, job_id, ready_revision, revision, phase,
               service_epoch, 'session-corrupt', gcode_sha256, application_version,
               application_digest, machine_digest, provider_digest, stream_milestone,
               recovery_intent, predecessor_execution_id, recovery_evidence_json,
               retirement_disposition, retired_at, successor_execution_id,
               outcome_kind, outcome_json, outcome_at, created_at, updated_at
        FROM machine_executions WHERE execution_id = ?
        """,
        (original.execution_id,),
    )
    with pytest.raises(DrawingMachineError) as corrupt:
        repository.get_active_execution()
    assert corrupt.value.payload.code == "MACHINE_PERSISTENCE_CORRUPT"


def test_c15_completed_row_without_retirement_is_rejected_during_rehydration(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original, "completed-created")
    repository._get_connection().execute(
        """
        UPDATE machine_executions
        SET revision = revision + 1, phase = 'COMPLETED', stream_milestone = 'STREAM_CONFIRMED',
            retirement_disposition = NULL, retired_at = NULL
        WHERE execution_id = ?
        """,
        (original.execution_id,),
    )

    with pytest.raises(DrawingMachineError, match=r"COMPLETED.*retirement"):
        repository.get_execution(original.execution_id)


def test_connection_setup_failure_closes_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingConnection:
        closed = False

        def execute(self, _statement: str) -> None:
            raise RuntimeError("pragma failed")

        def close(self) -> None:
            self.closed = True

    candidate = FailingConnection()
    monkeypatch.setattr(machine_sql.sqlite3, "connect", lambda *args, **kwargs: candidate)
    repository = SQLiteMachineRepository(tmp_path / "failure.db")
    with pytest.raises(RuntimeError, match="pragma failed"):
        repository.initialize(applied_at=NOW.isoformat())
    assert candidate.closed is True


def test_two_connection_prepare_cancel_race_has_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "prepare-cancel.db"
    setup = SQLiteMachineRepository(path)
    assert setup.initialize(applied_at=NOW.isoformat()) == 5
    seed_job(setup)
    setup.close()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def prepare() -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        barrier.wait(timeout=5)
        try:
            create(repository, execution(), "prepared")
            results.append("prepare")
        except DrawingMachineError:
            results.append("prepare-denied")
        finally:
            repository.close()

    def cancel() -> None:
        connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        barrier.wait(timeout=5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET state = 'CANCELLED', revision = 8, updated_at = ? WHERE job_id = 'job-1'",
                ((NOW + timedelta(seconds=1)).isoformat(),),
            )
            connection.commit()
            results.append("cancel")
        except sqlite3.IntegrityError:
            connection.rollback()
            results.append("cancel-denied")
        finally:
            connection.close()

    threads = [threading.Thread(target=prepare), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(results) in (["cancel", "prepare-denied"], ["cancel-denied", "prepare"])

    check = SQLiteMachineRepository(path)
    check.initialize(applied_at=NOW.isoformat())
    job_row = check._get_connection().execute("SELECT state FROM jobs WHERE job_id = 'job-1'").fetchone()
    active = check.get_active_execution()
    assert (job_row == ("CANCELLED",) and active is None) or (job_row == ("READY_TO_RUN",) and active is not None)
    check.close()


def test_two_connection_phase_approval_and_request_races_allow_one_mutation(tmp_path: Path) -> None:
    path = tmp_path / "authority-races.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    awaiting = open_session(setup, original, "await")
    challenge = approval(awaiting)
    with setup.transaction() as transaction:
        transaction.issue_challenge(challenge)
    decision = consume_approval(
        challenge,
        current_binding=challenge.binding,
        consumer=OPERATOR_PEER,
        operator_principal=OPERATOR,
        wall_now=awaiting.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    setup.close()

    def race(operation: str) -> list[str]:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def worker(index: int) -> None:
            repository = SQLiteMachineRepository(path)
            repository.initialize(applied_at=NOW.isoformat())
            barrier.wait(timeout=5)
            try:
                if operation == "approval":
                    with repository.transaction() as transaction:
                        transaction.decide_challenge(decision)
                elif operation == "request":
                    record = MachineRequestRecord(
                        MachineRequestKey(1100, 1100, "raced-request"),
                        31001 + index,
                        "machine.home",
                        DIGEST,
                        awaiting.execution_id,
                        MachineAction.HOME.value,
                        None,
                        None,
                        None,
                        NOW,
                        execution_revision=awaiting.revision,
                        service_epoch=awaiting.service_epoch,
                        machine_session_epoch=awaiting.machine_session_epoch,
                    )
                    with repository.transaction() as transaction:
                        reservation = transaction.reserve_request(record)
                    results.append("created" if reservation.created else "replayed")
                    return
                else:
                    homing = replace(
                        awaiting,
                        revision=2,
                        phase=MachinePhase.HOMING,
                        updated_at=NOW + timedelta(seconds=2),
                    )
                    with repository.transaction() as transaction:
                        transaction.transition_execution(
                            1,
                            homing,
                            event=event(homing, f"homing-{index}", action=MachineAction.HOME),
                            audit=audit(homing, f"homing-{index}"),
                        )
                results.append("won")
            except DrawingMachineError:
                results.append("lost")
            finally:
                repository.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        return results

    assert sorted(race("approval")) == ["lost", "won"]
    assert sorted(race("request")) == ["created", "replayed"]
    assert sorted(race("phase")) == ["lost", "won"]


def test_two_connection_release_transfer_race_has_one_latch_result(tmp_path: Path) -> None:
    path = tmp_path / "release-transfer.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    failed = recovery(original)
    with setup.transaction() as transaction:
        transaction.transition_execution(0, failed, event=event(failed, "failed"), audit=audit(failed, "failed"))
    mark_job_recovery(setup)
    transfer_at = failed.updated_at + timedelta(seconds=1)
    released = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.RELEASED,
            transfer_at,
        ),
        updated_at=transfer_at,
    )
    successor = execution(
        "race-successor",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        phase=MachinePhase.PREPARING_SESSION,
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    predecessor = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            successor.execution_id,
        ),
        updated_at=transfer_at,
    )
    setup.close()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def release() -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        barrier.wait(timeout=5)
        try:
            with repository.transaction() as transaction:
                transaction.release_owner(
                    1,
                    released,
                    event=event(released, "race-release", action=MachineAction.RECOVER),
                    audit=audit(released, "race-release"),
                )
            results.append("release")
        except DrawingMachineError:
            results.append("release-lost")
        finally:
            repository.close()

    def transfer() -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        barrier.wait(timeout=5)
        try:
            with repository.transaction() as transaction:
                transaction.transfer_owner(
                    1,
                    predecessor,
                    successor,
                    predecessor_event=event(predecessor, "race-transfer", action=MachineAction.RECOVER),
                    successor_event=event(successor, "race-successor", action=MachineAction.RECOVER),
                    predecessor_audit=audit(predecessor, "race-transfer"),
                    successor_audit=audit(successor, "race-successor"),
                )
            results.append("transfer")
        except DrawingMachineError:
            results.append("transfer-lost")
        finally:
            repository.close()

    threads = [threading.Thread(target=release), threading.Thread(target=transfer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(results) in (["release", "transfer-lost"], ["release-lost", "transfer"])

    check = SQLiteMachineRepository(path)
    check.initialize(applied_at=NOW.isoformat())
    active = check.get_active_execution()
    if "transfer" in results:
        assert active == successor
    else:
        assert active is None
    check.close()


class CommitFailingConnection(sqlite3.Connection):
    fail_next_commit = False

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("forced machine commit failure")
        super().commit()


def test_commit_failure_rolls_back_all_machine_rows(tmp_path: Path) -> None:
    path = tmp_path / "commit-failure.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    setup.close()

    connection = sqlite3.connect(path, isolation_level=None, factory=CommitFailingConnection)
    assert isinstance(connection, CommitFailingConnection)
    connection.execute("PRAGMA foreign_keys = ON")
    repository = SQLiteMachineRepository(path)
    repository._connection = connection
    repository._connection_thread = threading.get_ident()
    connection.fail_next_commit = True
    original = execution()
    with pytest.raises(sqlite3.OperationalError, match="forced machine commit failure"):
        create(repository, original, "uncommitted")
    assert repository.get_execution(original.execution_id) is None
    assert repository.list_events(original.execution_id) == ()
    assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
    repository.close()


def test_initial_admission_has_no_fabricated_session_and_records_real_open_once(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    with repository.transaction() as transaction:
        transaction.create_execution(
            original,
            event=event(original, "created-without-session"),
            audit=audit(original, "created-without-session"),
        )
    assert repository.get_session(original.execution_id) is None
    awaiting = replace(
        original,
        revision=1,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        updated_at=NOW + timedelta(seconds=1),
    )
    observed = session(awaiting)
    with repository.transaction() as transaction:
        transaction.record_opened_session(
            0,
            awaiting,
            session=observed,
            event=event(awaiting, "session-opened"),
            audit=audit(awaiting, "session-opened"),
        )
    assert repository.get_session(original.execution_id) == observed
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.record_opened_session(
            0,
            awaiting,
            session=observed,
            event=event(awaiting, "session-opened-again"),
            audit=audit(awaiting, "session-opened-again"),
        )


def test_admission_capability_cannot_claim_after_recovery_or_with_mismatched_nonce(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    with repository.transaction() as transaction:
        transaction.create_execution(
            original,
            event=event(original, "created"),
            audit=audit(original, "created"),
        )
    key = MachineRequestKey(1100, 1100, "capability-request")
    capability = machine_repository.MachineAdmissionCapability(
        key=key,
        admission_id="admission-capability",
        dispatch_nonce="dispatch-nonce",
        execution_id=original.execution_id,
        action=MachineAction.PREPARE_SESSION,
        execution_revision=0,
        service_epoch=original.service_epoch,
        machine_session_epoch=original.machine_session_epoch,
    )
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.prepare",
        argument_digest=DIGEST,
        execution_id=original.execution_id,
        action=MachineAction.PREPARE_SESSION.value,
        execution_revision=0,
        service_epoch=original.service_epoch,
        machine_session_epoch=original.machine_session_epoch,
        admission_id=capability.admission_id,
        dispatch_nonce=capability.dispatch_nonce,
        response=None,
        reserved_at=NOW,
    )
    with pytest.raises(DrawingMachineError) as missing, repository.transaction() as transaction:
        transaction.claim_request(capability, claimed_at=NOW)
    assert missing.value.payload.code == "MACHINE_ADMISSION_MISMATCH"
    with repository.transaction() as transaction:
        transaction.reserve_request(request)
    malformed_capabilities = (
        replace(capability, key=MachineRequestKey(True, 1100, "capability-request")),
        replace(capability, action="PREPARE_SESSION"),  # type: ignore[arg-type]
        replace(capability, execution_revision=True),
        replace(capability, admission_id=""),
        replace(capability, dispatch_nonce=""),
        replace(capability, execution_id=""),
        replace(capability, service_epoch=""),
        replace(capability, machine_session_epoch=""),
        replace(capability, recovery_intent=RecoveryIntent.PRE_STREAM_RESTART),
    )
    for malformed in malformed_capabilities:
        with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
            transaction.claim_request(malformed, claimed_at=NOW + timedelta(seconds=1))
        assert mismatch.value.payload.code == "MACHINE_ADMISSION_MISMATCH"
    mismatches = (
        replace(capability, admission_id="different-admission"),
        replace(capability, dispatch_nonce="different-nonce"),
        replace(capability, execution_revision=1),
        replace(capability, service_epoch="different-service"),
        replace(capability, machine_session_epoch="different-session"),
    )
    for mismatched in mismatches:
        with pytest.raises(DrawingMachineError) as mismatch, repository.transaction() as transaction:
            transaction.claim_request(mismatched, claimed_at=NOW + timedelta(seconds=1))
        assert mismatch.value.payload.code == "MACHINE_ADMISSION_MISMATCH"
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            0,
            failed,
            event=event(failed, "recovery"),
            audit=audit(failed, "recovery"),
        )
    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.claim_request(capability, claimed_at=NOW + timedelta(seconds=2))
    assert stale.value.payload.code == "MACHINE_ADMISSION_STALE"


def test_challenge_cannot_be_consumed_after_execution_enters_recovery(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "await")
    challenge = approval(awaiting)
    with repository.transaction() as transaction:
        transaction.issue_challenge(challenge)
    decision = consume_approval(
        challenge,
        current_binding=challenge.binding,
        consumer=OPERATOR_PEER,
        operator_principal=OPERATOR,
        wall_now=awaiting.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    failed = recovery(awaiting)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            1,
            failed,
            event=event(failed, "recovery"),
            audit=audit(failed, "recovery"),
        )
    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.decide_challenge(decision)
    assert stale.value.payload.code == "MACHINE_APPROVAL_STALE"
    assert repository.get_challenge(challenge.challenge_id) == challenge


def test_progress_rejects_future_revision_and_non_streaming_phase(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    future = MachineProgress(
        1,
        original.execution_id,
        999,
        2,
        1,
        1,
        0,
        50.0,
        4,
        "G1 X1",
        NOW,
    )
    with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
        transaction.append_progress("future-progress", future)
    assert rejected.value.payload.code == "MACHINE_PROGRESS_STALE"
    assert repository.list_progress(original.execution_id) == ()


def test_opened_session_late_conflict_rolls_back_snapshot_and_phase(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    awaiting = replace(
        original,
        revision=1,
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        updated_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.record_opened_session(
            0,
            awaiting,
            session=session(awaiting),
            event=event(awaiting, "machine-event-0"),
            audit=audit(awaiting, "machine-event-0"),
        )
    assert repository.get_execution(original.execution_id) == original
    assert repository.get_session(original.execution_id) is None


@pytest.mark.parametrize(
    "mismatch",
    [
        "job",
        "ready_revision",
        "gcode",
        "application_version",
        "application_digest",
        "machine_digest",
        "provider_digest",
        "milestone_intent",
        "session_epoch",
    ],
)
def test_transfer_rejects_successor_frozen_authority_drift_atomically(
    repository: SQLiteMachineRepository,
    mismatch: str,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(0, failed, event=event(failed, "failed"), audit=audit(failed, "failed"))
    mark_job_recovery(repository)
    transfer_at = failed.updated_at + timedelta(seconds=1)
    successor = execution(
        "drift-successor",
        predecessor_execution_id=failed.execution_id,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        phase=MachinePhase.PREPARING_SESSION,
        service_epoch="new-service-epoch",
        created_at=transfer_at,
        updated_at=transfer_at,
    )
    changes: dict[str, object] = {
        "job": {"job_id": "job-2"},
        "ready_revision": {"ready_revision": 6},
        "gcode": {"gcode_sha256": "e" * 64},
        "application_version": {"application_version": "3.0.0"},
        "application_digest": {"application_digest": "e" * 64},
        "machine_digest": {"machine_digest": "e" * 64},
        "provider_digest": {"provider_digest": "e" * 64},
        "milestone_intent": {
            "stream_milestone": StreamMilestone.STREAM_CONFIRMED,
            "recovery_intent": RecoveryIntent.POST_STREAM_SAFE_HOME,
        },
        "session_epoch": {"machine_session_epoch": failed.machine_session_epoch},
    }
    drifted = replace(successor, **cast(dict[str, Any], changes[mismatch]))
    predecessor = replace(
        failed,
        revision=2,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.TRANSFERRED,
            transfer_at,
            drifted.execution_id,
        ),
        updated_at=transfer_at,
    )
    with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
        transaction.transfer_owner(
            1,
            predecessor,
            drifted,
            predecessor_event=event(predecessor, f"predecessor-{mismatch}"),
            successor_event=event(drifted, f"successor-{mismatch}"),
            predecessor_audit=audit(predecessor, f"predecessor-{mismatch}"),
            successor_audit=audit(drifted, f"successor-{mismatch}"),
        )
    assert rejected.value.payload.code == "MACHINE_TRANSFER_INVALID"
    assert repository.get_execution(failed.execution_id) == failed
    assert repository.get_execution(drifted.execution_id) is None


def restart_fixture(repository: SQLiteMachineRepository):
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "restart-awaiting")
    challenge = approval(awaiting)
    with repository.transaction() as transaction:
        transaction.issue_challenge(challenge)
    key = MachineRequestKey(1100, 1100, "restart-admission")
    capability = machine_repository.MachineAdmissionCapability(
        key,
        "restart-admission-id",
        "restart-dispatch-nonce",
        awaiting.execution_id,
        MachineAction.HOME,
        awaiting.revision,
        awaiting.service_epoch,
        awaiting.machine_session_epoch,
    )
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.home",
        argument_digest=DIGEST,
        execution_id=awaiting.execution_id,
        action=MachineAction.HOME.value,
        admission_id=capability.admission_id,
        dispatch_nonce=capability.dispatch_nonce,
        response=None,
        reserved_at=awaiting.updated_at,
        execution_revision=awaiting.revision,
        service_epoch=awaiting.service_epoch,
        machine_session_epoch=awaiting.machine_session_epoch,
    )
    with repository.transaction() as transaction:
        transaction.reserve_request(request)
    return awaiting, challenge, capability


def restart_job_authority(
    failed: MachineExecution,
    *,
    event_id: str,
) -> tuple[MachineOutcome, MachineJobOutcomeTransition, JobEvent, AuditRecord]:
    ready = ready_snapshot(failed.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_restart_recovery_required",
        ready,
    )
    job_event = JobEvent(
        event_id,
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    return outcome, transition, job_event, job_outcome_audit(job_event)


def test_c12_release_preserves_restart_outcome_columns_exactly(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    outcome, transition, job_event, job_audit = restart_job_authority(
        failed,
        event_id="c12-release-outcome-job",
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            original.revision,
            failed,
            event=event(failed, "c12-release-outcome-recovery"),
            audit=audit(failed, "c12-release-outcome-recovery"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_audit,
        )
    before = (
        repository._get_connection()
        .execute(
            "SELECT outcome_kind, outcome_json, outcome_at FROM machine_executions WHERE execution_id = ?",
            (failed.execution_id,),
        )
        .fetchone()
    )
    released_at = failed.updated_at + timedelta(seconds=1)
    released = replace(
        failed,
        revision=failed.revision + 1,
        retirement=OwnerRetirementEvidence(
            failed.execution_id,
            RetirementDisposition.RELEASED,
            released_at,
        ),
        updated_at=released_at,
    )
    with repository.transaction() as transaction:
        transaction.release_owner(
            failed.revision,
            released,
            event=event(released, "c12-release-outcome-released", action=MachineAction.RECOVER),
            audit=audit(released, "c12-release-outcome-released"),
        )
    after = (
        repository._get_connection()
        .execute(
            "SELECT outcome_kind, outcome_json, outcome_at FROM machine_executions WHERE execution_id = ?",
            (failed.execution_id,),
        )
        .fetchone()
    )
    assert before is not None and after == before
    assert repository.get_active_execution() is None


def test_restart_reconciliation_freezes_execution_and_invalidates_pending_authority(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "restart-reconciled"),
            audit=audit(failed, "restart-reconciled"),
        )
    assert repository.get_execution(awaiting.execution_id) == failed
    invalidated_challenge = repository.get_challenge(challenge.challenge_id)
    assert invalidated_challenge is not None
    assert invalidated_challenge.status is ApprovalStatus.EXPIRED
    invalidated_request = repository.get_request(capability.key)
    assert invalidated_request is not None
    assert invalidated_request.invalidated_at == failed.updated_at
    assert invalidated_request.invalidation_reason == "RESTART_RECONCILIATION"
    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.claim_request(capability, claimed_at=failed.updated_at + timedelta(seconds=1))
    assert stale.value.payload.code == "MACHINE_ADMISSION_STALE"


def test_c12_restart_reconciliation_atomically_freezes_machine_job_and_invalidates_authority(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    ready = ready_snapshot(failed.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_restart_recovery_required",
        ready,
    )
    job_event = JobEvent(
        "c12-restart-job-outcome",
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-restart-machine-outcome"),
            audit=audit(failed, "c12-restart-machine-outcome"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )

    jobs = SQLiteRepository(repository._path, clock=type("Clock", (), {"now": lambda self: NOW})())
    try:
        job = jobs.get_job(failed.job_id)
        assert job is not None
        assert job.state is JobState.RECOVERY_REQUIRED
        assert job.revision == 8
        assert job.ready_snapshot == ready
    finally:
        jobs.close()
    assert repository.get_execution(failed.execution_id) == failed
    assert repository.get_outcome(failed.execution_id) == outcome
    assert repository.get_challenge(challenge.challenge_id).status is ApprovalStatus.EXPIRED  # type: ignore[union-attr]
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidation_reason == "RESTART_RECONCILIATION"


def test_c12_restart_job_or_event_failure_rolls_back_machine_job_and_every_invalidation(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    ready = ready_snapshot(failed.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_restart_recovery_required",
        ready,
    )
    job_event = JobEvent(
        "c12-rollback-job-outcome",
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "machine-event-0"),
            audit=audit(failed, "machine-event-0"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )

    jobs = SQLiteRepository(repository._path, clock=type("Clock", (), {"now": lambda self: NOW})())
    try:
        job = jobs.get_job(failed.job_id)
        assert job is not None and job.state is JobState.READY_TO_RUN and job.revision == 7
    finally:
        jobs.close()
    assert repository.get_execution(failed.execution_id) == awaiting
    assert repository.get_outcome(failed.execution_id) is None
    assert repository.get_challenge(challenge.challenge_id) == challenge
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidated_at is None


def test_c12_restart_optional_job_authority_is_closed_all_or_none(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    ready = ready_snapshot(failed.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_restart_recovery_required",
        ready,
    )
    job_event = JobEvent(
        "c12-all-or-none-job",
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    values = {
        "outcome": outcome,
        "job_transition": transition,
        "job_event": job_event,
        "job_audit": job_outcome_audit(job_event),
    }
    for missing in values:
        partial = {key: value for key, value in values.items() if key != missing}
        with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
            transaction.reconcile_restart(
                awaiting.revision,
                failed,
                event=event(failed, f"c12-all-or-none-{missing}"),
                audit=audit(failed, f"c12-all-or-none-{missing}"),
                **partial,  # type: ignore[arg-type]
            )
        assert rejected.value.payload.code == "MACHINE_RECONCILIATION_INVALID"
    assert repository.get_execution(awaiting.execution_id) == awaiting
    assert repository.get_challenge(challenge.challenge_id) == challenge
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidated_at is None


def test_c12_repeated_restart_rejects_a_second_job_outcome(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, _challenge, _capability = restart_fixture(repository)
    failed = recovery(awaiting)
    outcome, transition, job_event, job_audit = restart_job_authority(
        failed,
        event_id="c12-repeat-authority-job",
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-repeat-authority-initial"),
            audit=audit(failed, "c12-repeat-authority-initial"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_audit,
        )
    repeated_event = replace(
        event(failed, "c12-repeat-authority-lifecycle", action=MachineAction.RECOVER),
        occurred_at=failed.updated_at + timedelta(seconds=1),
    )
    with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
        transaction.reconcile_restart(
            failed.revision,
            failed,
            event=repeated_event,
            audit=audit(failed, "c12-repeat-authority-lifecycle"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_audit,
        )
    assert rejected.value.payload.code == "MACHINE_RECONCILIATION_INVALID"


def test_c12_restart_rejects_missing_or_changed_job_authority(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    outcome, transition, job_event, job_audit = restart_job_authority(
        failed,
        event_id="c12-missing-job",
    )
    repository._get_connection().execute("PRAGMA foreign_keys = OFF")
    repository._get_connection().execute("DELETE FROM jobs WHERE job_id = ?", (failed.job_id,))
    repository._get_connection().execute("PRAGMA foreign_keys = ON")
    with pytest.raises(DrawingMachineError) as missing, repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-missing-job-machine"),
            audit=audit(failed, "c12-missing-job-machine"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_audit,
        )
    assert missing.value.payload.code == "MACHINE_JOB_CONFLICT"

    seed_job(repository)
    changed_event = replace(job_event, prior_state=JobState.VALIDATING_GCODE)
    with pytest.raises(DrawingMachineError) as changed, repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-changed-job-machine"),
            audit=audit(failed, "c12-changed-job-machine"),
            outcome=outcome,
            job_transition=transition,
            job_event=changed_event,
            job_audit=job_audit,
        )
    assert changed.value.payload.code == "MACHINE_JOB_CONFLICT"
    assert repository.get_execution(awaiting.execution_id) == awaiting
    assert repository.get_challenge(challenge.challenge_id) == challenge
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidated_at is None


def test_c12_restart_job_update_conflict_rolls_back_every_change(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    outcome, transition, job_event, job_audit = restart_job_authority(
        failed,
        event_id="c12-triggered-job-conflict",
    )
    repository._get_connection().execute(
        """
        CREATE TEMP TRIGGER c12_ignore_job_restart_update
        BEFORE UPDATE ON jobs
        BEGIN
            SELECT RAISE(IGNORE);
        END
        """
    )
    with pytest.raises(DrawingMachineError) as conflict, repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-triggered-job-conflict-machine"),
            audit=audit(failed, "c12-triggered-job-conflict-machine"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_audit,
        )
    assert conflict.value.payload.code == "MACHINE_JOB_CONFLICT"
    assert repository.get_execution(awaiting.execution_id) == awaiting
    assert repository.get_challenge(challenge.challenge_id) == challenge
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidated_at is None
    assert repository._get_connection().execute(
        "SELECT state, revision FROM jobs WHERE job_id = ?",
        (failed.job_id,),
    ).fetchone() == (JobState.READY_TO_RUN.value, 7)


def test_c12_repeated_restart_invalidates_authority_without_repeating_job_outcome(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, _challenge, _capability = restart_fixture(repository)
    failed = recovery(awaiting)
    ready = ready_snapshot(failed.job_id)
    outcome = MachineOutcome(
        1,
        failed.execution_id,
        failed.job_id,
        failed.revision,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        failed.recovery_evidence,
        failed.updated_at,
    )
    transition = MachineJobOutcomeTransition(
        failed.job_id,
        7,
        JobState.RECOVERY_REQUIRED,
        "job.machine_restart_recovery_required",
        ready,
    )
    job_event = JobEvent(
        "c12-repeat-job-outcome",
        failed.job_id,
        8,
        transition.event_type,
        JobState.READY_TO_RUN,
        JobState.RECOVERY_REQUIRED,
        None,
        "SERVICE",
        {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
        failed.updated_at,
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "c12-repeat-initial"),
            audit=audit(failed, "c12-repeat-initial"),
            outcome=outcome,
            job_transition=transition,
            job_event=job_event,
            job_audit=job_outcome_audit(job_event),
        )
    before_job_events = (
        repository._get_connection()
        .execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
            (failed.job_id,),
        )
        .fetchone()
    )
    repeated_event = replace(
        event(failed, "c12-repeat-lifecycle", action=MachineAction.RECOVER),
        occurred_at=failed.updated_at + timedelta(seconds=1),
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            failed.revision,
            failed,
            event=repeated_event,
            audit=audit(failed, "c12-repeat-lifecycle"),
        )
    after_job_events = (
        repository._get_connection()
        .execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
            (failed.job_id,),
        )
        .fetchone()
    )
    job = (
        repository._get_connection()
        .execute(
            "SELECT state, revision FROM jobs WHERE job_id = ?",
            (failed.job_id,),
        )
        .fetchone()
    )
    assert before_job_events == after_job_events
    assert job == (JobState.RECOVERY_REQUIRED.value, 8)
    assert repository.get_execution(failed.execution_id) == failed
    assert repository.get_outcome(failed.execution_id) == outcome


def test_c12_two_connection_restart_reconciliation_commits_machine_and_job_once(tmp_path: Path) -> None:
    path = tmp_path / "c12-restart-race.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    setup.close()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def reconcile(index: int) -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        try:
            current = repository.get_active_execution()
            assert current == original
            failed = recovery(current)
            ready = ready_snapshot(failed.job_id)
            outcome = MachineOutcome(
                1,
                failed.execution_id,
                failed.job_id,
                failed.revision,
                MachineOutcomeKind.RECOVERY_REQUIRED,
                ready,
                failed.recovery_evidence,
                failed.updated_at,
            )
            transition = MachineJobOutcomeTransition(
                failed.job_id,
                7,
                JobState.RECOVERY_REQUIRED,
                "job.machine_restart_recovery_required",
                ready,
            )
            job_event = JobEvent(
                f"c12-race-job-{index}",
                failed.job_id,
                8,
                transition.event_type,
                JobState.READY_TO_RUN,
                JobState.RECOVERY_REQUIRED,
                None,
                "SERVICE",
                {"requester": RequesterIdentity("SERVICE", None, None, None).to_json()},
                failed.updated_at,
            )
            barrier.wait(timeout=5.0)
            with repository.transaction() as transaction:
                transaction.reconcile_restart(
                    current.revision,
                    failed,
                    event=event(failed, f"c12-race-machine-{index}"),
                    audit=audit(failed, f"c12-race-machine-{index}"),
                    outcome=outcome,
                    job_transition=transition,
                    job_event=job_event,
                    job_audit=job_outcome_audit(job_event),
                )
            results.append("committed")
        except DrawingMachineError as error:
            assert error.payload.code == "MACHINE_REVISION_CONFLICT"
            results.append("rejected")
        finally:
            repository.close()

    threads = [threading.Thread(target=reconcile, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    check = SQLiteMachineRepository(path)
    try:
        owner = check.get_active_execution()
        assert owner is not None and owner.phase is MachinePhase.RECOVERY_REQUIRED
        job = (
            check._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = ?",
                (original.job_id,),
            )
            .fetchone()
        )
        assert sorted(results) == ["committed", "rejected"]
        assert job == (JobState.RECOVERY_REQUIRED.value, 8)
        assert check._get_connection().execute("SELECT COUNT(*) FROM job_events").fetchone() == (1,)
        assert check._get_connection().execute("SELECT COUNT(*) FROM machine_events").fetchone() == (2,)
    finally:
        check.close()


def test_repeated_restart_reconciliation_invalidates_new_recovery_authority_without_revision_churn(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            original.revision,
            failed,
            event=event(failed, "initial-restart-reconciliation"),
            audit=audit(failed, "initial-restart-reconciliation"),
        )
    challenge = recovery_approval(
        failed,
        "123e4567-e89b-42d3-a456-426614174030",
        RecoveryDisposition.RESTART_SEQUENCE,
    )
    admission_key = MachineRequestKey(1100, 1100, "pending-recovery-reconciliation")
    pending_admission = MachineRequestRecord(
        key=admission_key,
        requester_pid=31001,
        command="machine.recover",
        argument_digest=DIGEST,
        execution_id=failed.execution_id,
        action=MachineAction.RECOVER.value,
        execution_revision=failed.revision,
        service_epoch=failed.service_epoch,
        machine_session_epoch=failed.machine_session_epoch,
        admission_id="pending-recovery-admission",
        dispatch_nonce="pending-recovery-nonce",
        response=None,
        reserved_at=failed.updated_at,
        recovery_disposition=RecoveryDisposition.RESTART_SEQUENCE,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id=failed.execution_id,
    )
    with repository.transaction() as transaction:
        transaction.issue_challenge(challenge)
        transaction.reserve_request(pending_admission)

    repeated_event = replace(
        event(failed, "repeated-restart-reconciliation", action=MachineAction.RECOVER),
        occurred_at=failed.updated_at + timedelta(seconds=1),
    )
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            failed.revision,
            failed,
            event=repeated_event,
            audit=audit(failed, "repeated-restart-reconciliation"),
        )

    assert repository.get_execution(failed.execution_id) == failed
    invalidated = repository.get_challenge(challenge.challenge_id)
    assert invalidated is not None
    assert invalidated.status is ApprovalStatus.EXPIRED
    invalidated_admission = repository.get_request(admission_key)
    assert invalidated_admission is not None
    assert invalidated_admission.invalidated_at == repeated_event.occurred_at
    assert invalidated_admission.invalidation_reason == "RESTART_RECONCILIATION"
    persisted_events = repository.list_events(failed.execution_id)
    assert persisted_events[-1] == repeated_event
    assert persisted_events[-2].occurred_at < persisted_events[-1].occurred_at


def test_repeated_restart_reconciliation_late_event_conflict_rolls_back_new_authority(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            original.revision,
            failed,
            event=event(failed, "repeat-conflict-initial"),
            audit=audit(failed, "repeat-conflict-initial"),
        )
    challenge = recovery_approval(
        failed,
        "123e4567-e89b-42d3-a456-426614174031",
        RecoveryDisposition.RESTART_SEQUENCE,
    )
    key = MachineRequestKey(1100, 1100, "repeat-conflict-admission")
    request = MachineRequestRecord(
        key=key,
        requester_pid=31001,
        command="machine.recover",
        argument_digest=DIGEST,
        execution_id=failed.execution_id,
        action=MachineAction.RECOVER.value,
        execution_revision=failed.revision,
        service_epoch=failed.service_epoch,
        machine_session_epoch=failed.machine_session_epoch,
        admission_id="repeat-conflict-admission-id",
        dispatch_nonce="repeat-conflict-nonce",
        response=None,
        reserved_at=failed.updated_at,
        recovery_disposition=RecoveryDisposition.RESTART_SEQUENCE,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id=failed.execution_id,
    )
    with repository.transaction() as transaction:
        transaction.issue_challenge(challenge)
        transaction.reserve_request(request)
    duplicate_event = replace(
        event(failed, "repeat-conflict-initial", action=MachineAction.RECOVER),
        occurred_at=failed.updated_at + timedelta(seconds=1),
    )
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.reconcile_restart(
            failed.revision,
            failed,
            event=duplicate_event,
            audit=audit(failed, "repeat-conflict-initial"),
        )
    assert repository.get_execution(failed.execution_id) == failed
    assert repository.get_challenge(challenge.challenge_id) == challenge
    persisted_request = repository.get_request(key)
    assert persisted_request is not None and persisted_request.invalidated_at is None


def test_restart_reconciliation_late_conflict_rolls_back_every_invalidation(
    repository: SQLiteMachineRepository,
) -> None:
    awaiting, challenge, capability = restart_fixture(repository)
    failed = recovery(awaiting)
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "machine-event-0"),
            audit=audit(failed, "machine-event-0"),
        )
    assert repository.get_execution(awaiting.execution_id) == awaiting
    assert repository.get_challenge(challenge.challenge_id) == challenge
    request = repository.get_request(capability.key)
    assert request is not None and request.invalidated_at is None


def test_restart_reconciliation_rejects_stale_revision_and_nonrecovery_result(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "reconcile-invalid-open")
    failed = recovery(awaiting)
    with pytest.raises(DrawingMachineError) as stale, repository.transaction() as transaction:
        transaction.reconcile_restart(
            99,
            failed,
            event=event(failed, "stale-reconcile"),
            audit=audit(failed, "stale-reconcile"),
        )
    assert stale.value.payload.code == "MACHINE_REVISION_CONFLICT"
    homing = replace(
        awaiting,
        revision=awaiting.revision + 1,
        phase=MachinePhase.HOMING,
        updated_at=awaiting.updated_at + timedelta(seconds=1),
    )
    with pytest.raises(DrawingMachineError) as invalid, repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            homing,
            event=event(homing, "invalid-reconcile", action=MachineAction.HOME),
            audit=audit(homing, "invalid-reconcile"),
        )
    assert invalid.value.payload.code == "MACHINE_RECONCILIATION_INVALID"
    with repository.transaction() as transaction:
        transaction.reconcile_restart(
            awaiting.revision,
            failed,
            event=event(failed, "reconcile-frozen"),
            audit=audit(failed, "reconcile-frozen"),
        )
    with pytest.raises(DrawingMachineError) as nonmonotonic, repository.transaction() as transaction:
        transaction.reconcile_restart(
            failed.revision,
            failed,
            event=event(failed, "reconcile-nonmonotonic", action=MachineAction.RECOVER),
            audit=audit(failed, "reconcile-nonmonotonic"),
        )
    assert nonmonotonic.value.payload.code == "MACHINE_RECONCILIATION_INVALID"


def test_two_connections_can_claim_one_exact_admission_only_once(tmp_path: Path) -> None:
    path = tmp_path / "claim-race.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    key = MachineRequestKey(1100, 1100, "claim-race")
    capability = machine_repository.MachineAdmissionCapability(
        key,
        "claim-race-admission",
        "claim-race-nonce",
        original.execution_id,
        MachineAction.PREPARE_SESSION,
        original.revision,
        original.service_epoch,
        original.machine_session_epoch,
    )
    with setup.transaction() as transaction:
        transaction.reserve_request(
            MachineRequestRecord(
                key=key,
                requester_pid=31001,
                command="machine.prepare",
                argument_digest=DIGEST,
                execution_id=original.execution_id,
                action=MachineAction.PREPARE_SESSION.value,
                admission_id=capability.admission_id,
                dispatch_nonce=capability.dispatch_nonce,
                response=None,
                reserved_at=NOW,
                execution_revision=original.revision,
                service_epoch=original.service_epoch,
                machine_session_epoch=original.machine_session_epoch,
            )
        )
    setup.close()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def claim() -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        barrier.wait(timeout=5)
        try:
            with repository.transaction() as transaction:
                transaction.claim_request(capability, claimed_at=NOW + timedelta(seconds=1))
            results.append("claimed")
        except DrawingMachineError:
            results.append("rejected")
        finally:
            repository.close()

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(results) == ["claimed", "rejected"]


@pytest.mark.parametrize("drift", ["revision", "service_epoch", "session_epoch", "digest"])
def test_challenge_issue_rejects_stale_revision_and_epoch_bindings(
    repository: SQLiteMachineRepository,
    drift: str,
) -> None:
    original = execution()
    create(repository, original)
    awaiting = open_session(repository, original, "challenge-drift-open")
    challenge = approval(awaiting)
    binding_changes: dict[str, object] = {
        "revision": {"execution_revision": awaiting.revision + 1},
        "service_epoch": {"service_epoch": "old-service-epoch"},
        "session_epoch": {"machine_session_epoch": "old-session-epoch"},
        "digest": {"machine_digest": "e" * 64},
    }
    binding = replace(challenge.binding, **cast(dict[str, Any], binding_changes[drift]))
    evidence = challenge.evidence
    if drift == "session_epoch":
        assert evidence.session is not None
        evidence = replace(
            evidence,
            session=replace(evidence.session, machine_session_epoch="old-session-epoch"),
        )
    stale = replace(challenge, binding=binding, evidence=evidence)
    with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
        transaction.issue_challenge(stale)
    assert rejected.value.payload.code == "MACHINE_APPROVAL_STALE"
    assert repository.get_challenge(stale.challenge_id) is None


def test_replacement_recovery_challenge_may_change_only_its_current_typed_disposition(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    failed = recovery(original)
    with repository.transaction() as transaction:
        transaction.transition_execution(0, failed, event=event(failed, "failed"), audit=audit(failed, "failed"))

    def recover_challenge(challenge_id: str, disposition: RecoveryDisposition):
        binding = MachineApprovalBinding(
            failed.execution_id,
            failed.revision,
            MachineAction.RECOVER,
            disposition,
            MachinePhase.RECOVERY_REQUIRED,
            failed.service_epoch,
            failed.machine_session_epoch,
            failed.job_id,
            failed.ready_revision,
            failed.application_digest,
            failed.machine_digest,
            failed.provider_digest,
            failed.gcode_sha256,
        )
        return issue_approval(
            challenge_id=challenge_id,
            binding=binding,
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=MachineApprovalEvidenceV1(
                1,
                MachineAction.RECOVER,
                MachinePhase.RECOVERY_REQUIRED,
                None,
            ),
            issued_at=failed.updated_at,
            monotonic_now=1000.0,
            ttl_seconds=60,
        )

    first = recover_challenge("123e4567-e89b-42d3-a456-426614174020", RecoveryDisposition.RELEASE)
    with repository.transaction() as transaction:
        transaction.issue_challenge(first)
    superseded = supersede_approval(
        first,
        wall_now=failed.updated_at + timedelta(seconds=1),
        monotonic_now=1001.0,
    )
    replacement = recover_challenge(
        "123e4567-e89b-42d3-a456-426614174021",
        RecoveryDisposition.RESTART_SEQUENCE,
    )
    with repository.transaction() as transaction:
        transaction.issue_challenge(replacement, replacement=superseded)
    assert repository.get_challenge(first.challenge_id) == superseded.result
    assert repository.get_challenge(replacement.challenge_id) == replacement


def test_two_connection_expired_replacement_race_expires_old_and_leaves_one_pending_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expired-replacement-race.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    failed = recovery(original)
    with setup.transaction() as transaction:
        transaction.transition_execution(
            0,
            failed,
            event=event(failed, "expired-race-failed"),
            audit=audit(failed, "expired-race-failed"),
        )

    binding = MachineApprovalBinding(
        failed.execution_id,
        failed.revision,
        MachineAction.RECOVER,
        RecoveryDisposition.RELEASE,
        MachinePhase.RECOVERY_REQUIRED,
        failed.service_epoch,
        failed.machine_session_epoch,
        failed.job_id,
        failed.ready_revision,
        failed.application_digest,
        failed.machine_digest,
        failed.provider_digest,
        failed.gcode_sha256,
    )

    def challenge(challenge_id: str, issued_at: datetime, monotonic: float):
        return issue_approval(
            challenge_id=challenge_id,
            binding=binding,
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=MachineApprovalEvidenceV1(
                1,
                MachineAction.RECOVER,
                MachinePhase.RECOVERY_REQUIRED,
                None,
            ),
            issued_at=issued_at,
            monotonic_now=monotonic,
            ttl_seconds=60,
        )

    expired = challenge("123e4567-e89b-42d3-a456-426614174030", failed.updated_at, 1000.0)
    with setup.transaction() as transaction:
        transaction.issue_challenge(expired)
    now = failed.updated_at + timedelta(seconds=60)
    replacements = (
        challenge("123e4567-e89b-42d3-a456-426614174031", now, 1060.0),
        challenge("123e4567-e89b-42d3-a456-426614174032", now, 1060.0),
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def replace_pending(replacement_challenge) -> None:
        repository = SQLiteMachineRepository(path)
        try:
            barrier.wait(timeout=5)
            with repository.transaction() as transaction:
                pending = transaction.get_pending_challenge(failed.execution_id, MachineAction.RECOVER)
                assert pending is not None
                try:
                    decision = supersede_approval(pending, wall_now=now, monotonic_now=1060.0)
                except DrawingMachineError as error:
                    assert error.payload.code == "MACHINE_APPROVAL_EXPIRED"
                    decision = expire_approval(pending, wall_now=now, monotonic_now=1060.0)
                transaction.issue_challenge(replacement_challenge, replacement=decision)
            outcomes.append("committed")
        finally:
            repository.close()

    threads = [threading.Thread(target=replace_pending, args=(item,)) for item in replacements]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert outcomes == ["committed", "committed"]
    assert setup.get_challenge(expired.challenge_id).status is ApprovalStatus.EXPIRED  # type: ignore[union-attr]
    statuses = [setup.get_challenge(item.challenge_id).status for item in replacements]  # type: ignore[union-attr]
    assert sorted(status.value for status in statuses) == ["PENDING", "SUPERSEDED"]
    setup.close()


def test_two_connection_late_progress_after_recovery_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "late-progress.db"
    setup = SQLiteMachineRepository(path)
    setup.initialize(applied_at=NOW.isoformat())
    seed_job(setup)
    original = execution()
    create(setup, original)
    streaming = advance_to_streaming(setup, original)
    failed = recovery(streaming)
    progress = MachineProgress(
        1,
        streaming.execution_id,
        streaming.revision,
        2,
        1,
        1,
        0,
        50.0,
        4,
        "G1 X1",
        streaming.updated_at,
    )
    setup.close()
    recovery_committed = threading.Event()
    results: list[str] = []

    def freeze() -> None:
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        try:
            with repository.transaction() as transaction:
                transaction.transition_execution(
                    streaming.revision,
                    failed,
                    event=event(failed, "late-progress-recovery"),
                    audit=audit(failed, "late-progress-recovery"),
                )
            results.append("recovery")
        finally:
            recovery_committed.set()
            repository.close()

    def append_late() -> None:
        recovery_committed.wait(timeout=5)
        repository = SQLiteMachineRepository(path)
        repository.initialize(applied_at=NOW.isoformat())
        try:
            with pytest.raises(DrawingMachineError) as rejected, repository.transaction() as transaction:
                transaction.append_progress("late-progress", progress)
            assert rejected.value.payload.code == "MACHINE_PROGRESS_STALE"
            results.append("progress-rejected")
        finally:
            repository.close()

    threads = [threading.Thread(target=freeze), threading.Thread(target=append_late)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(results) == ["progress-rejected", "recovery"]


def test_database_guards_session_challenge_progress_and_admission_authority(
    repository: SQLiteMachineRepository,
) -> None:
    original = execution()
    create(repository, original)
    connection = repository._get_connection()
    with pytest.raises(sqlite3.IntegrityError, match="observed machine session"):
        connection.execute(
            "UPDATE machine_executions SET revision = 1, phase = 'AWAITING_HOME_APPROVAL' WHERE execution_id = ?",
            (original.execution_id,),
        )
    awaiting = open_session(repository, original, "db-guard-open")
    challenge = approval(awaiting)
    with repository.transaction() as transaction:
        transaction.issue_challenge(challenge)
    key = MachineRequestKey(1100, 1100, "db-guard-admission")
    with repository.transaction() as transaction:
        transaction.reserve_request(
            MachineRequestRecord(
                key=key,
                requester_pid=31001,
                command="machine.home",
                argument_digest=DIGEST,
                execution_id=awaiting.execution_id,
                action=MachineAction.HOME.value,
                admission_id="db-guard-id",
                dispatch_nonce="db-guard-nonce",
                response=None,
                reserved_at=awaiting.updated_at,
                execution_revision=awaiting.revision,
                service_epoch=awaiting.service_epoch,
                machine_session_epoch=awaiting.machine_session_epoch,
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="authority is immutable"):
        connection.execute(
            """UPDATE machine_request_results SET service_epoch = 'mutated'
               WHERE principal_uid = 1100 AND principal_gid = 1100 AND request_id = ?""",
            (key.request_id,),
        )
    failed = recovery(awaiting)
    with repository.transaction() as transaction:
        transaction.transition_execution(
            awaiting.revision,
            failed,
            event=event(failed, "db-guard-recovery"),
            audit=audit(failed, "db-guard-recovery"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="approval authority is stale"):
        connection.execute(
            """
            UPDATE machine_approval_challenges
            SET status = 'CONSUMED', status_changed_at = ?,
                consumer_uid = 2200, consumer_gid = 2200, consumer_pid = 41001,
                challenge_json = json_set(
                    challenge_json,
                    '$.status', 'CONSUMED',
                    '$.status_changed_at', ?,
                    '$.consumer', json('{"uid":2200,"gid":2200,"pid":41001}')
                )
            WHERE challenge_id = ?
            """,
            (
                (failed.updated_at + timedelta(seconds=1)).isoformat(),
                (failed.updated_at + timedelta(seconds=1)).isoformat(),
                challenge.challenge_id,
            ),
        )
    future = MachineProgress(1, failed.execution_id, 999, 0, 0, 0, 0, 100.0, None, None, failed.updated_at)
    with pytest.raises(sqlite3.IntegrityError, match="current STREAMING authority"):
        connection.execute(
            """INSERT INTO machine_progress_events (
                   progress_id, execution_id, execution_revision, progress_json, last_event_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                "db-future-progress",
                future.execution_id,
                future.execution_revision,
                canonical(machine_sql._progress_json(future)),
                future.last_event_at.isoformat(),
            ),
        )
