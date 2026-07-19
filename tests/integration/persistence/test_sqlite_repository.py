import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

import drawingmachine.adapters.persistence.sqlite_repository as repository_module
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.persistence.migrator import (
    Migration,
    _apply_migration,
    _bootstrap_ledger,
    _migration_statements,
    _verify_migration_ledger,
)
from drawingmachine.domain.jobs import (
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    JobTransition,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import StagingRecoveryReasonCode
from drawingmachine.ports.repository import RepositoryTransaction


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: datetime = datetime(2026, 7, 10, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class CommitFailingConnection(sqlite3.Connection):
    fail_next_commit = False
    fail_next_rollback = False
    rollback_calls = 0

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("forced commit failure")
        super().commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.fail_next_rollback:
            self.fail_next_rollback = False
            raise sqlite3.OperationalError("forced rollback failure")
        super().rollback()


class ControlledSQLiteRepository(SQLiteRepository):
    def __init__(self, path: Path, *, clock: FixedClock, connection: sqlite3.Connection) -> None:
        super().__init__(path, clock=clock)
        self._controlled_connection = connection

    def _get_connection(self) -> sqlite3.Connection:
        return self._controlled_connection


def test_bootstrap_ledger_commit_failure_rolls_back_table(tmp_path: Path) -> None:
    connection = sqlite3.connect(
        tmp_path / "bootstrap-commit.db",
        isolation_level=None,
        factory=CommitFailingConnection,
    )
    assert isinstance(connection, CommitFailingConnection)
    connection.fail_next_commit = True
    try:
        with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
            _bootstrap_ledger(connection)

        assert connection.rollback_calls == 1
        assert connection.in_transaction is False
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() is None
    finally:
        sqlite3.Connection.rollback(connection)
        connection.close()


def test_initialize_is_idempotent_and_configures_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock(), busy_timeout_ms=2345)

    assert repository.initialize() == 5
    assert repository.initialize() == 5

    health = repository.health()
    assert health.schema_version == 5
    assert health.journal_mode == "wal"
    assert health.foreign_keys_enabled is True
    assert repository._get_connection().execute("PRAGMA busy_timeout").fetchone() == (2345,)

    with sqlite3.connect(database_path) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        migration_rows = connection.execute(
            "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()

    migration_root = resources.files("drawingmachine.adapters.persistence.migrations")
    foundation_bytes = migration_root.joinpath("0001_foundation.sql").read_bytes()
    offline_job_chain_bytes = migration_root.joinpath("0002_offline_job_chain.sql").read_bytes()
    hardware_execution_bytes = migration_root.joinpath("0003_hardware_execution.sql").read_bytes()
    staging_admissions_bytes = migration_root.joinpath("0004_staging_admissions.sql").read_bytes()
    recover_binding_current_epoch_bytes = migration_root.joinpath("0005_recover_binding_current_epoch.sql").read_bytes()
    assert table_names == {
        "audit_events",
        "job_artifacts",
        "job_events",
        "jobs",
        "machine_approval_challenges",
        "machine_events",
        "machine_executions",
        "machine_progress_events",
        "machine_request_results",
        "machine_sessions",
        "schema_migrations",
        "staging_admissions",
    }
    assert migration_rows == [
        (1, "foundation", hashlib.sha256(foundation_bytes).hexdigest()),
        (2, "offline_job_chain", hashlib.sha256(offline_job_chain_bytes).hexdigest()),
        (3, "hardware_execution", hashlib.sha256(hardware_execution_bytes).hexdigest()),
        (4, "staging_admissions", hashlib.sha256(staging_admissions_bytes).hexdigest()),
        (5, "recover_binding_current_epoch", hashlib.sha256(recover_binding_current_epoch_bytes).hexdigest()),
    ]

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_append_audit_persists_canonical_payload_and_clock_time(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()

    repository.append_audit(
        "evt-1",
        "service.started",
        "req-1",
        {"pid": 7, "flags": [True, None], "context": {"mode": "local"}},
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT event_id, event_type, request_id, payload_json, created_at FROM audit_events"
        ).fetchone()

    assert row == (
        "evt-1",
        "service.started",
        "req-1",
        json.dumps(
            {"pid": 7, "flags": [True, None], "context": {"mode": "local"}},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "2026-07-10T00:00:00+00:00",
    )


def test_transaction_commits_and_rolls_back_audit_events(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()

    with repository.transaction() as transaction:
        transaction.append_audit("evt-commit", "service.started", "req-commit", {"pid": 7})

    with pytest.raises(RuntimeError, match="abort transaction"), repository.transaction() as transaction:
        transaction.append_audit("evt-rollback", "service.stopped", "req-rollback", {"pid": 7})
        raise RuntimeError("abort transaction")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT event_id, event_type FROM audit_events ORDER BY rowid").fetchall()
    assert rows == [("evt-commit", "service.started")]


def test_append_audit_uses_the_typed_transaction_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = SQLiteRepository(tmp_path / "drawingmachine.db", clock=FixedClock())
    calls: list[tuple[str, str, str | None, JsonObject]] = []

    class RecordingTransaction:
        def append_audit(
            self,
            event_id: str,
            event_type: str,
            request_id: str | None,
            payload: JsonObject,
        ) -> None:
            calls.append((event_id, event_type, request_id, payload))

    @contextmanager
    def recording_transaction() -> Iterator[RepositoryTransaction]:
        yield RecordingTransaction()

    monkeypatch.setattr(repository, "transaction", recording_transaction)

    repository.append_audit("evt-typed", "service.started", "req-typed", {"pid": 7})

    assert calls == [("evt-typed", "service.started", "req-typed", {"pid": 7})]


def _assert_inactive_transaction(transaction: RepositoryTransaction, event_id: str) -> None:
    with pytest.raises(DrawingMachineError) as error:
        transaction.append_audit(event_id, "service.stale", "req-stale", {"pid": 99})

    assert error.value.payload.code == "REPOSITORY_TRANSACTION_INACTIVE"
    assert error.value.payload.category is ErrorCategory.INTERNAL
    assert error.value.payload.retryable is False
    assert error.value.payload.details == {}


def test_transaction_handle_is_invalid_after_successful_context_exit(tmp_path: Path) -> None:
    database_path = tmp_path / "success.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()

    with repository.transaction() as retained:
        retained.append_audit("evt-commit", "service.started", None, {"pid": 7})

    _assert_inactive_transaction(retained, "evt-stale-success")

    with sqlite3.connect(database_path) as connection:
        events = connection.execute("SELECT event_id FROM audit_events ORDER BY rowid").fetchall()
    assert events == [("evt-commit",)]


def test_transaction_handle_is_invalid_after_body_rollback(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    retained: RepositoryTransaction | None = None

    with pytest.raises(RuntimeError, match="abort transaction"), repository.transaction() as transaction:
        retained = transaction
        transaction.append_audit("evt-rollback", "service.started", None, {"pid": 7})
        raise RuntimeError("abort transaction")

    assert retained is not None
    _assert_inactive_transaction(retained, "evt-stale-rollback")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_transaction_handle_is_invalid_after_commit_failure(tmp_path: Path) -> None:
    database_path = tmp_path / "commit-failure.db"
    connection = sqlite3.connect(database_path, isolation_level=None, factory=CommitFailingConnection)
    assert isinstance(connection, CommitFailingConnection)
    repository = ControlledSQLiteRepository(database_path, clock=FixedClock(), connection=connection)
    retained: RepositoryTransaction | None = None
    try:
        repository.initialize()
        connection.fail_next_commit = True

        with (
            pytest.raises(sqlite3.OperationalError, match="forced commit failure"),
            repository.transaction() as transaction,
        ):
            retained = transaction
            transaction.append_audit("evt-commit-failure", "service.started", None, {"pid": 7})

        assert retained is not None
        _assert_inactive_transaction(retained, "evt-stale-commit-failure")
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
    finally:
        sqlite3.Connection.rollback(connection)
        connection.close()


def test_transaction_handle_is_invalid_after_rollback_failure(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback-failure.db"
    connection = sqlite3.connect(database_path, isolation_level=None, factory=CommitFailingConnection)
    assert isinstance(connection, CommitFailingConnection)
    repository = ControlledSQLiteRepository(database_path, clock=FixedClock(), connection=connection)
    retained: RepositoryTransaction | None = None
    try:
        repository.initialize()
        connection.fail_next_rollback = True

        with (
            pytest.raises(sqlite3.OperationalError, match="forced rollback failure"),
            repository.transaction() as transaction,
        ):
            retained = transaction
            transaction.append_audit("evt-rollback-failure", "service.started", None, {"pid": 7})
            raise RuntimeError("abort transaction")

        assert retained is not None
        _assert_inactive_transaction(retained, "evt-stale-rollback-failure")
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_id = 'evt-stale-rollback-failure'"
        ).fetchone() == (0,)
    finally:
        sqlite3.Connection.rollback(connection)
        connection.close()

    with sqlite3.connect(database_path) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_stale_handle_cannot_write_during_or_after_a_subsequent_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "generation.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()

    with repository.transaction() as stale:
        pass

    with repository.transaction() as current:
        _assert_inactive_transaction(stale, "evt-stale-during-next")
        current.append_audit("evt-current", "service.started", None, {"pid": 7})

    _assert_inactive_transaction(stale, "evt-stale-after-next")

    with sqlite3.connect(database_path) as connection:
        events = connection.execute("SELECT event_id FROM audit_events ORDER BY rowid").fetchall()
    assert events == [("evt-current",)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET event_type = 'changed' WHERE event_id = 'evt-1'",
        "DELETE FROM audit_events WHERE event_id = 'evt-1'",
    ],
)
def test_audit_rows_reject_update_and_delete_at_database_level(tmp_path: Path, statement: str) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    repository.append_audit("evt-1", "service.started", None, {"pid": 7})

    with sqlite3.connect(database_path) as connection, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute(statement)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT event_type FROM audit_events WHERE event_id = 'evt-1'").fetchone() == (
            "service.started",
        )


def test_audit_rows_reject_insert_or_replace_conflicts_without_recursive_triggers(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    repository.append_audit("evt-1", "service.started", "req-1", {"pid": 7})

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO audit_events
                    (event_id, event_type, request_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("evt-1", "service.changed", "req-2", '{"pid":8}', "2026-07-10T00:01:00+00:00"),
            )
        connection.execute(
            """
            INSERT OR REPLACE INTO audit_events
                (event_id, event_type, request_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("evt-2", "service.stopped", "req-3", "{}", "2026-07-10T00:02:00+00:00"),
        )

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event_id, event_type, request_id, payload_json, created_at FROM audit_events ORDER BY event_id"
        ).fetchall()

    assert rows == [
        ("evt-1", "service.started", "req-1", '{"pid":7}', "2026-07-10T00:00:00+00:00"),
        ("evt-2", "service.stopped", "req-3", "{}", "2026-07-10T00:02:00+00:00"),
    ]


def test_initialize_rejects_a_stored_migration_checksum_mismatch(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    repository.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET sha256 = ? WHERE version = 1", ("0" * 64,))

    reopened = SQLiteRepository(database_path, clock=FixedClock())
    with pytest.raises(DrawingMachineError) as error:
        reopened.initialize()

    assert error.value.payload.code == "DATABASE_MIGRATION_CHECKSUM_MISMATCH"
    assert error.value.payload.category is ErrorCategory.INTERNAL
    assert error.value.payload.retryable is False
    assert error.value.payload.details == {"migration_version": 1, "migration_name": "foundation"}


def test_initialize_rejects_a_stored_migration_name_mismatch(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    repository.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET name = 'renamed' WHERE version = 1")

    with pytest.raises(DrawingMachineError) as error:
        SQLiteRepository(database_path, clock=FixedClock()).initialize()

    assert error.value.payload.code == "DATABASE_MIGRATION_IDENTITY_MISMATCH"


def test_complete_ledger_verification_rejects_missing_packaged_migration(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "missing.db", isolation_level=None)
    try:
        _bootstrap_ledger(connection)
        migration = Migration(1, "missing", "0" * 64, "SELECT 1;")

        with pytest.raises(DrawingMachineError) as error:
            _verify_migration_ledger(connection, (migration,), require_complete=True)

        assert error.value.payload.code == "DATABASE_MIGRATION_MISSING"
    finally:
        connection.close()


def test_migration_statement_parser_rejects_incomplete_sql() -> None:
    migration = Migration(1, "incomplete", "0" * 64, "CREATE TABLE incomplete (id INTEGER)")

    with pytest.raises(DrawingMachineError) as error:
        tuple(_migration_statements(migration))

    assert error.value.payload.code == "DATABASE_MIGRATION_INVALID"


def test_initialize_rejects_a_stored_migration_without_a_packaged_resource(tmp_path: Path) -> None:
    database_path = tmp_path / "drawingmachine.db"
    repository = SQLiteRepository(database_path, clock=FixedClock())
    repository.initialize()
    repository.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, sha256, applied_at) VALUES (?, ?, ?, ?)",
            (6, "unbundled", "0" * 64, "2026-07-10T00:00:00+00:00"),
        )

    reopened = SQLiteRepository(database_path, clock=FixedClock())
    with pytest.raises(DrawingMachineError) as error:
        reopened.initialize()

    assert error.value.payload.code == "DATABASE_MIGRATION_UNKNOWN"
    assert error.value.payload.category is ErrorCategory.INTERNAL
    assert error.value.payload.retryable is False
    assert error.value.payload.details == {"migration_version": 6, "migration_name": "unbundled"}


def test_migration_commit_failure_rolls_back_schema_and_ledger(tmp_path: Path) -> None:
    connection = sqlite3.connect(
        tmp_path / "migration-commit.db",
        isolation_level=None,
        factory=CommitFailingConnection,
    )
    assert isinstance(connection, CommitFailingConnection)
    try:
        _bootstrap_ledger(connection)
        connection.fail_next_commit = True

        with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
            _apply_migration(
                connection,
                Migration(
                    version=1,
                    name="commit_failure",
                    sha256="0" * 64,
                    sql="CREATE TABLE should_rollback (id INTEGER);",
                ),
                applied_at="2026-07-10T00:00:00+00:00",
            )

        state = (
            connection.rollback_calls,
            connection.in_transaction,
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'should_rollback'").fetchone(),
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone(),
        )
        assert state == (1, False, None, (0,))
    finally:
        sqlite3.Connection.rollback(connection)
        connection.close()


def test_append_audit_commit_failure_rolls_back_event(tmp_path: Path) -> None:
    connection = sqlite3.connect(
        tmp_path / "audit-commit.db",
        isolation_level=None,
        factory=CommitFailingConnection,
    )
    assert isinstance(connection, CommitFailingConnection)
    repository = ControlledSQLiteRepository(
        tmp_path / "audit-commit.db",
        clock=FixedClock(),
        connection=connection,
    )
    try:
        repository.initialize()
        rollback_calls_before = connection.rollback_calls
        connection.fail_next_commit = True

        with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
            repository.append_audit("evt-commit", "service.started", None, {"pid": 7})

        state = (
            connection.rollback_calls - rollback_calls_before,
            connection.in_transaction,
            connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_id = 'evt-commit'").fetchone(),
        )
        assert state == (1, False, (0,))
    finally:
        sqlite3.Connection.rollback(connection)
        connection.close()


def test_close_is_idempotent_and_initialize_can_reopen(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "drawingmachine.db", clock=FixedClock())
    repository.initialize()

    repository.close()
    repository.close()

    assert repository.initialize() == 5
    assert repository.health().schema_version == 5


def _authority_job() -> JobRecord:
    return JobRecord(
        1,
        "job-authority",
        JobKind.OFFLINE_WORKFLOW,
        "job",
        JobState.QUEUED,
        0,
        {"route_mode": "direct"},
        ConfigSnapshot("machine", "provider", 1, 1, 1, "1" * 64, "2" * 64, "3" * 64),
        None,
        None,
        None,
        FixedClock().now(),
        FixedClock().now(),
    )


def _authority_event(job: JobRecord) -> JobEvent:
    requester = RequesterIdentity("LOCAL_PEER", 1, 2, 3)
    return JobEvent(
        "event-authority",
        job.job_id,
        job.revision,
        "job.created",
        None,
        job.state,
        "request-authority",
        requester.requester_type,
        {"requester": requester.to_json()},
        FixedClock().now(),
    )


def _authority_audit(event: JobEvent) -> AuditRecord:
    return AuditRecord(
        event.event_id,
        event.event_type,
        event.request_id,
        {
            "schema_version": 1,
            "requester": event.payload["requester"],
            "job_id": event.job_id,
            "command_summary": {"name": "test", "event_type": event.event_type, "job_kind": "OFFLINE_WORKFLOW"},
            "prior_state": None if event.prior_state is None else event.prior_state.value,
            "result_state": event.result_state.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "test",
            "protocol_version": 1,
        },
    )


def test_repository_rejects_invalid_constructor_binding_registration_and_missing_recovery(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        SQLiteRepository(tmp_path / "negative.db", clock=FixedClock(), busy_timeout_ms=-1)

    repository = SQLiteRepository(tmp_path / "authority.db", clock=FixedClock())
    repository.initialize()
    with pytest.raises(DrawingMachineError, match="binding is invalid"):
        repository.bind_staging_request_envelope("", "request", "image", "0" * 64)
    with pytest.raises(DrawingMachineError, match="exact REQUEST_DECODED"):
        repository.register_decoded_staging(object())  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="does not exist"):
        repository.record_staging_recovery(
            "missing",
            expected_revision=0,
            recovery_code=StagingRecoveryReasonCode.IDENTITY_DRIFT,
            quarantined=False,
        )


@pytest.mark.parametrize(
    "event_type,request_id,payload",
    [
        ("staging.request-envelope.v1", "request", "{"),
        ("other", "request", "{}"),
        (
            "staging.request-envelope.v1",
            "request",
            '{"schema_version":2,"role":"image","envelope_sha256":"' + "0" * 64 + '"}',
        ),
        ("staging.request-envelope.v1", "request", '{"schema_version":1,"role":"image","envelope_sha256":"bad"}'),
    ],
)
def test_request_envelope_binding_rejects_corrupt_durable_authority_and_rolls_back(
    tmp_path: Path,
    event_type: str,
    request_id: str,
    payload: str,
) -> None:
    repository = SQLiteRepository(tmp_path / "binding.db", clock=FixedClock())
    repository.initialize()
    connection = repository._get_connection()
    connection.execute(
        "INSERT INTO audit_events (event_id, event_type, request_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        ("event", event_type, request_id, payload, FixedClock().now().isoformat()),
    )
    with pytest.raises(DrawingMachineError, match="authority is corrupt"):
        repository.bind_staging_request_envelope("event", "request", "image", "1" * 64)
    assert connection.in_transaction is False
    assert connection.execute("SELECT payload_json FROM audit_events WHERE event_id='event'").fetchone() == (payload,)


def test_nested_event_and_audit_identity_corruption_is_rejected() -> None:
    job = _authority_job()
    event = _authority_event(job)
    audit = _authority_audit(event)
    object.__setattr__(event, "requester_type", "SERVICE")
    with pytest.raises(DrawingMachineError, match="requester type"):
        repository_module._assert_audit_matches_event(event, audit)

    class NonObjectAudit:
        def to_json(self) -> dict[str, object]:
            return {"payload": []}

    with pytest.raises(DrawingMachineError, match="JSON object"):
        repository_module._plain_audit_payload(NonObjectAudit())  # type: ignore[arg-type]

    row = (
        "event",
        job.job_id,
        0,
        "job.created",
        None,
        "QUEUED",
        "10000000-0000-4000-8000-000000000001",
        "{}",
        FixedClock().now().isoformat(),
    )
    with pytest.raises(DrawingMachineError):
        repository_module._event_from_row(row)


def test_connection_configuration_failure_closes_unpublished_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        closed = False

        def execute(self, statement: str) -> None:
            del statement
            raise sqlite3.OperationalError("pragma rejected")

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(repository_module.sqlite3, "connect", lambda *args, **kwargs: connection)
    repository = SQLiteRepository(tmp_path / "broken.db", clock=FixedClock())
    with pytest.raises(sqlite3.OperationalError, match="pragma rejected"):
        repository._get_connection()
    assert connection.closed is True
    assert repository._connection is None


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None = None, *, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _ScriptedConnection:
    def __init__(self, *results: _Cursor | BaseException) -> None:
        self.results = list(results)

    def execute(self, statement: str, parameters: object = ()) -> _Cursor:
        del statement, parameters
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _ActiveCapability:
    def require_active_ownership(self) -> None:
        return


def _staging_row(state: str, revision: int = 0) -> tuple[object, ...]:
    timestamp = FixedClock().now().isoformat()
    return (
        1,
        "10000000-0000-4000-8000-000000000001",
        "image",
        "image--10000000-0000-4000-8000-000000000001--" + "0" * 32 + ".stage",
        1,
        2,
        3,
        "1" * 64,
        "2" * 64,
        "boot",
        10,
        20,
        state,
        None,
        revision,
        timestamp,
        timestamp,
        None,
    )


@pytest.mark.parametrize(
    "connection,expected",
    [
        (_ScriptedConnection(_Cursor()), "does not exist"),
        (_ScriptedConnection(_Cursor(_staging_row("REQUEST_DECODED"))), "expected claimed revision"),
    ],
)
def test_atomic_staging_admission_rejects_missing_or_unclaimed_authority(
    connection: _ScriptedConnection,
    expected: str,
) -> None:
    transaction = repository_module._SQLiteRepositoryTransaction(  # type: ignore[arg-type]
        connection, FixedClock(), _ActiveCapability()
    )
    with pytest.raises(DrawingMachineError, match=expected):
        transaction.create_job_with_staging(
            _authority_job(),
            artifacts=(),
            event=_authority_event(_authority_job()),
            audit=_authority_audit(_authority_event(_authority_job())),
            request_id="10000000-0000-4000-8000-000000000001",
            expected_staging_revision=0,
        )


def test_atomic_staging_admission_rejects_claim_changed_after_job_write() -> None:
    connection = _ScriptedConnection(_Cursor(_staging_row("CLAIMED")), _Cursor(rowcount=0))
    transaction = repository_module._SQLiteRepositoryTransaction(  # type: ignore[arg-type]
        connection, FixedClock(), _ActiveCapability()
    )
    transaction.create_job = lambda *args, **kwargs: None  # type: ignore[method-assign]
    with pytest.raises(DrawingMachineError, match="changed during atomic"):
        transaction.create_job_with_staging(
            _authority_job(),
            artifacts=(),
            event=_authority_event(_authority_job()),
            audit=_authority_audit(_authority_event(_authority_job())),
            request_id="10000000-0000-4000-8000-000000000001",
            expected_staging_revision=0,
        )


def _transition_contract() -> tuple[JobRecord, JobTransition, JobEvent, AuditRecord]:
    job = _authority_job()
    transition = JobTransition(job.job_id, 0, JobState.PREPARING_IMAGE, "request-transition", "job.preparing")
    requester = RequesterIdentity("LOCAL_PEER", 1, 2, 3)
    event = JobEvent(
        "event-transition",
        job.job_id,
        1,
        transition.event_type,
        job.state,
        transition.result_state,
        transition.request_id,
        requester.requester_type,
        {"requester": requester.to_json()},
        FixedClock().now(),
    )
    return job, transition, event, _authority_audit(event)


def test_transition_rejects_missing_job_without_writing() -> None:
    connection = _ScriptedConnection(_Cursor())
    transaction = repository_module._SQLiteRepositoryTransaction(  # type: ignore[arg-type]
        connection, FixedClock(), _ActiveCapability()
    )
    _, transition, event, audit = _transition_contract()
    with pytest.raises(DrawingMachineError, match="job does not exist"):
        transaction.transition_job(transition, artifacts=(), event=event, audit=audit)
    assert connection.results == []


@pytest.mark.parametrize(
    "update_result,exception",
    [
        (sqlite3.IntegrityError("unrelated constraint"), sqlite3.IntegrityError),
        (_Cursor(rowcount=0), DrawingMachineError),
    ],
)
def test_transition_rejects_unrelated_integrity_or_revision_race(
    monkeypatch: pytest.MonkeyPatch,
    update_result: _Cursor | BaseException,
    exception: type[BaseException],
) -> None:
    job, transition, event, audit = _transition_contract()
    connection = _ScriptedConnection(_Cursor(("ignored",)), update_result)
    transaction = repository_module._SQLiteRepositoryTransaction(  # type: ignore[arg-type]
        connection, FixedClock(), _ActiveCapability()
    )
    monkeypatch.setattr(repository_module, "_job_from_row", lambda row: job)
    with pytest.raises(exception):
        transaction.transition_job(transition, artifacts=(), event=event, audit=audit)
    assert connection.results == []
