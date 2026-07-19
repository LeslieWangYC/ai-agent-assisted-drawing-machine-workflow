from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from importlib import resources

import pytest

from drawingmachine.adapters.persistence.migrator import _apply_migration, _bootstrap_ledger, _load_migrations
from drawingmachine.adapters.persistence.sqlite_repository import SQLiteRepository
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.client_data import (
    ClientDataRole,
    PayloadIdentityV1,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)


def _record() -> StagingAdmissionRecordV1:
    return StagingAdmissionRecordV1(
        request_id="12345678-1234-4234-8234-123456789abc",
        role=ClientDataRole.IMAGE,
        bundle_name="image--12345678-1234-4234-8234-123456789abc--" + "a" * 32 + ".stage",
        payload_identity=PayloadIdentityV1(1, 2, 3, "b" * 64),
        source_sha256="b" * 64,
        lease_sha256="c" * 64,
        publisher_boot_id="boot-a",
        published_monotonic_ns=10,
        deadline_monotonic_ns=20,
        state=StagingAdmissionState.REQUEST_DECODED,
        job_id=None,
        revision=0,
        created_at_utc="2026-07-14T00:00:00+00:00",
        updated_at_utc="2026-07-14T00:00:00+00:00",
        recovery_code=None,
    )


def _job_authority() -> tuple[JobRecord, JobEvent, AuditRecord]:
    timestamp = datetime(2026, 7, 14, tzinfo=UTC)
    requester = RequesterIdentity("LOCAL_PEER", 1, 2, 3)
    job = JobRecord(
        1,
        "job-atomic",
        JobKind.OFFLINE_WORKFLOW,
        "atomic",
        JobState.QUEUED,
        0,
        {"input_path": "/private/admitted/payload", "route_mode": "direct"},
        ConfigSnapshot(
            machine_profile="machine",
            provider_profile="provider",
            application_schema_version=1,
            machine_schema_version=1,
            provider_schema_version=1,
            application_digest="a" * 64,
            machine_digest="b" * 64,
            provider_digest="c" * 64,
        ),
        None,
        None,
        None,
        timestamp,
        timestamp,
    )
    event = JobEvent(
        "event-atomic",
        job.job_id,
        0,
        "job.queued",
        None,
        JobState.QUEUED,
        _record().request_id,
        "LOCAL_PEER",
        {"requester": requester.to_json()},
        timestamp,
    )
    audit = AuditRecord(
        event.event_id,
        event.event_type,
        event.request_id,
        {
            "schema_version": 1,
            "requester": requester.to_json(),
            "job_id": job.job_id,
            "command_summary": {
                "name": "workflow.run",
                "event_type": event.event_type,
                "job_kind": job.kind.value,
            },
            "prior_state": None,
            "result_state": JobState.QUEUED.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "0.2.0",
            "protocol_version": 1,
        },
    )
    return job, event, audit


def test_schema_four_registers_and_indexes_request_authority(tmp_path) -> None:
    repository = SQLiteRepository(
        tmp_path / "state.db",
        clock=_Clock(),
    )
    assert repository.initialize() == 5
    record = _record()

    repository.register_decoded_staging(record)

    assert repository.get_staging_admission(record.request_id) == record


def test_request_lookup_uses_staging_authority_index_and_never_scans_jobs(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "lookup.db", clock=_Clock())
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)
    connection = repository._get_connection()
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        assert repository.get_staging_admission(record.request_id) == record
    finally:
        connection.set_trace_callback(None)

    selected = tuple(statement.lower() for statement in statements if statement.lstrip().upper().startswith("SELECT"))
    assert len(selected) == 1
    assert " from staging_admissions where request_id = " in " ".join(selected[0].split())
    assert " from jobs " not in " ".join(selected[0].split())
    plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT request_id FROM staging_admissions WHERE request_id = ?",
        (record.request_id,),
    ).fetchall()
    detail = " ".join(str(row[3]).upper() for row in plan)
    assert "SEARCH STAGING_ADMISSIONS USING" in detail
    assert "SCAN JOBS" not in detail


def test_staging_state_transition_is_revision_cas(tmp_path) -> None:
    repository = SQLiteRepository(
        tmp_path / "state.db",
        clock=_Clock(),
    )
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)

    claimed = repository.mark_staging_claimed(record.request_id, expected_revision=0)

    assert claimed.state is StagingAdmissionState.CLAIMED
    assert claimed.revision == 1
    with pytest.raises(DrawingMachineError):
        repository.mark_staging_claimed(record.request_id, expected_revision=0)


def test_job_event_audit_and_staging_admission_commit_atomically(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)
    claimed = repository.mark_staging_claimed(record.request_id, expected_revision=0)
    job, event, audit = _job_authority()

    with repository.transaction() as transaction:
        admitted = transaction.create_job_with_staging(
            job,
            artifacts=(),
            event=event,
            audit=audit,
            request_id=record.request_id,
            expected_staging_revision=claimed.revision,
        )

    assert admitted.state is StagingAdmissionState.DURABLY_ADMITTED
    assert admitted.job_id == job.job_id
    assert repository.get_job(job.job_id) == job
    assert repository.list_job_events(job.job_id) == (event,)
    assert repository.get_staging_admission(record.request_id) == admitted


def test_failed_atomic_admission_rolls_back_job_event_audit_and_staging(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)
    claimed = repository.mark_staging_claimed(record.request_id, expected_revision=0)
    job, event, audit = _job_authority()
    invalid_audit = AuditRecord("different-event", audit.event_type, audit.request_id, audit.payload)

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.create_job_with_staging(
            job,
            artifacts=(),
            event=event,
            audit=invalid_audit,
            request_id=record.request_id,
            expected_staging_revision=claimed.revision,
        )

    assert repository.get_job(job.job_id) is None
    assert repository.list_job_events(job.job_id) == ()
    assert repository.get_staging_admission(record.request_id) == claimed


def test_schema_three_upgrades_to_current_without_changing_prior_migrations(tmp_path) -> None:
    database = tmp_path / "upgrade.db"
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        _bootstrap_ledger(connection)
        migrations = _load_migrations()
        for migration in migrations[:3]:
            _apply_migration(connection, migration, applied_at="2026-07-14T00:00:00+00:00")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (3,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'staging_admissions'").fetchone() is None
    finally:
        connection.close()

    repository = SQLiteRepository(database, clock=_Clock())
    assert repository.initialize() == 5
    assert repository.get_staging_admission(_record().request_id) is None

    root = resources.files("drawingmachine.adapters.persistence.migrations")
    assert {
        name: hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest()
        for name in ("0001_foundation.sql", "0002_offline_job_chain.sql", "0003_hardware_execution.sql")
    } == {
        "0001_foundation.sql": "65449ddd1aeb5c1fa3d4b85500ddd9de5f6444be33f1b12101949959232ccac6",
        "0002_offline_job_chain.sql": "49de58e965a88da77d6ff10b22a3f69fa8abdc886a9163063b75f56d81f4f495",
        "0003_hardware_execution.sql": "d25849bc7feb0e57daba961ba4fbdd96b74f93bb4e22dc704da42681adc231f5",
    }


def test_staging_schema_rejects_duplicate_authority_and_hostile_mutation(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)

    with pytest.raises(sqlite3.IntegrityError):
        repository.register_decoded_staging(record)

    connection = repository._get_connection()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE staging_admissions SET state = 'CLAIMED', source_sha256 = ?, "
            "revision = revision + 1 WHERE request_id = ?",
            ("d" * 64, record.request_id),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "UPDATE staging_admissions SET state = 'RECOVERY_REQUIRED', recovery_code = 'OPEN_VALUE', "
            "revision = revision + 1 WHERE request_id = ?",
            (record.request_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="illegal"):
        connection.execute(
            "UPDATE staging_admissions SET state = 'CONSUMED', revision = revision + 1 WHERE request_id = ?",
            (record.request_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="durable authority"):
        connection.execute("DELETE FROM staging_admissions WHERE request_id = ?", (record.request_id,))
    connection.rollback()
    assert repository.get_staging_admission(record.request_id) == record


@pytest.mark.parametrize("statement", ["jobs", "job_artifacts", "job_events", "audit_events", "admission"])
def test_each_atomic_admission_statement_failure_rolls_back_all_authority(tmp_path, statement: str) -> None:
    repository = SQLiteRepository(tmp_path / f"{statement}.db", clock=_Clock())
    repository.initialize()
    record = _record()
    repository.register_decoded_staging(record)
    claimed = repository.mark_staging_claimed(record.request_id, expected_revision=0)
    job, event, audit = _job_authority()
    connection = repository._get_connection()
    if statement == "admission":
        connection.execute(
            "CREATE TRIGGER fail_admission BEFORE UPDATE ON staging_admissions "
            "BEGIN SELECT RAISE(ABORT, 'forced admission failure'); END"
        )
    else:
        connection.execute(
            f"CREATE TRIGGER fail_{statement} BEFORE INSERT ON {statement} "
            f"BEGIN SELECT RAISE(ABORT, 'forced {statement} failure'); END"
        )
    artifact = ArtifactRef("input_image", "inputs/source.png", "d" * 64, 17, "image/png")

    with pytest.raises(sqlite3.IntegrityError, match="forced"), repository.transaction() as transaction:
        transaction.create_job_with_staging(
            job,
            artifacts=(artifact,),
            event=event,
            audit=audit,
            request_id=record.request_id,
            expected_staging_revision=claimed.revision,
        )

    assert repository.get_job(job.job_id) is None
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == ()
    assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_id = ?", (event.event_id,)).fetchone() == (
        0,
    )
    assert repository.get_staging_admission(record.request_id) == claimed
