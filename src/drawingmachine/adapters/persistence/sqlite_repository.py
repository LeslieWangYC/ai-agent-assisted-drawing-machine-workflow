import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from drawingmachine.adapters.persistence.migrator import migrate
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    JobEvent,
    JobRecord,
    JobState,
    JobTransition,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import (
    ClientDataRole,
    PayloadIdentityV1,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingRecoveryReasonCode,
)
from drawingmachine.ports.clock import Clock
from drawingmachine.ports.repository import RepositoryHealth, RepositoryTransaction

_JOB_SELECT_COLUMNS = """
    job_id,
    schema_version,
    kind,
    name,
    state,
    revision,
    request_json,
    config_snapshot_json,
    blocker_json,
    error_json,
    ready_snapshot_json,
    created_at,
    updated_at
"""

_STAGING_SELECT_COLUMNS = """
    schema_version,
    request_id,
    role,
    bundle_name,
    payload_device,
    payload_inode,
    payload_size_bytes,
    source_sha256,
    lease_sha256,
    publisher_boot_id,
    published_monotonic_ns,
    deadline_monotonic_ns,
    state,
    job_id,
    revision,
    created_at_utc,
    updated_at_utc,
    recovery_code
"""


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_json(payload: str) -> object:
    return cast(object, json.loads(payload))


def _consistency_error(message: str, *, job_id: str | None = None) -> DrawingMachineError:
    details: JsonObject = {}
    if job_id is not None:
        details["job_id"] = job_id
    return DrawingMachineError(
        ErrorPayload(
            code="REPOSITORY_CONSISTENCY_ERROR",
            category=ErrorCategory.INTERNAL,
            message=message,
            retryable=False,
            details=details,
            job_id=job_id,
        )
    )


def _assert_audit_matches_event(event: JobEvent, audit: AuditRecord) -> None:
    event_requester = RequesterIdentity.from_json(event.payload["requester"])
    audit_requester = RequesterIdentity.from_json(audit.payload["requester"])
    if event_requester.requester_type != event.requester_type:
        raise _consistency_error(
            "job event requester type does not match its nested requester identity",
            job_id=event.job_id,
        )
    if audit.event_id != event.event_id or audit.event_type != event.event_type or audit.request_id != event.request_id:
        raise _consistency_error(
            "audit identity, type, and request must match the job event",
            job_id=event.job_id,
        )
    if audit_requester != event_requester:
        raise _consistency_error(
            "audit requester identity must match the job event requester identity",
            job_id=event.job_id,
        )


def _plain_audit_payload(audit: AuditRecord) -> JsonObject:
    payload = audit.to_json()["payload"]
    if not isinstance(payload, dict):
        raise _consistency_error("audit payload must be a JSON object")
    return payload


def _assert_unique_artifact_roles(
    artifacts: tuple[ArtifactRef, ...],
    *,
    job_id: str,
) -> None:
    roles = [artifact.role for artifact in artifacts]
    if len(roles) != len(set(roles)):
        raise _consistency_error(
            "artifact roles must be unique within a job revision",
            job_id=job_id,
        )


def _job_from_row(row: tuple[object, ...]) -> JobRecord:
    return JobRecord.from_json(
        {
            "job_id": row[0],
            "schema_version": row[1],
            "kind": row[2],
            "name": row[3],
            "state": row[4],
            "revision": row[5],
            "request": _decode_json(str(row[6])),
            "config": _decode_json(str(row[7])),
            "blocker": None if row[8] is None else _decode_json(str(row[8])),
            "error": None if row[9] is None else _decode_json(str(row[9])),
            "ready_snapshot": None if row[10] is None else _decode_json(str(row[10])),
            "created_at": row[11],
            "updated_at": row[12],
        }
    )


def _artifact_from_row(row: tuple[object, ...]) -> ArtifactRef:
    return ArtifactRef.from_json(
        {
            "role": row[0],
            "relative_path": row[1],
            "sha256": row[2],
            "size_bytes": row[3],
            "media_type": row[4],
        }
    )


def _event_from_row(row: tuple[object, ...]) -> JobEvent:
    payload = _decode_json(str(row[7]))
    if not isinstance(payload, Mapping) or "requester" not in payload:
        return JobEvent.from_json(
            {
                "event_id": row[0],
                "job_id": row[1],
                "job_revision": row[2],
                "event_type": row[3],
                "prior_state": row[4],
                "result_state": row[5],
                "request_id": row[6],
                "requester_type": "",
                "payload": payload,
                "created_at": row[8],
            }
        )
    requester = RequesterIdentity.from_json(payload["requester"])
    return JobEvent.from_json(
        {
            "event_id": row[0],
            "job_id": row[1],
            "job_revision": row[2],
            "event_type": row[3],
            "prior_state": row[4],
            "result_state": row[5],
            "request_id": row[6],
            "requester_type": requester.requester_type,
            "payload": payload,
            "created_at": row[8],
        }
    )


def _staging_from_row(row: tuple[object, ...]) -> StagingAdmissionRecordV1:
    return StagingAdmissionRecordV1(
        request_id=str(row[1]),
        role=ClientDataRole(str(row[2])),
        bundle_name=str(row[3]),
        payload_identity=PayloadIdentityV1(
            cast(int, row[4]),
            cast(int, row[5]),
            cast(int, row[6]),
            str(row[7]),
        ),
        source_sha256=str(row[7]),
        lease_sha256=str(row[8]),
        publisher_boot_id=str(row[9]),
        published_monotonic_ns=cast(int, row[10]),
        deadline_monotonic_ns=cast(int, row[11]),
        state=StagingAdmissionState(str(row[12])),
        job_id=None if row[13] is None else str(row[13]),
        revision=cast(int, row[14]),
        created_at_utc=str(row[15]),
        updated_at_utc=str(row[16]),
        recovery_code=None if row[17] is None else StagingRecoveryReasonCode(str(row[17])),
        schema_version=cast(int, row[0]),
    )


class _TransactionCapability:
    def __init__(self, generation: int, current_generation: Callable[[], int | None]) -> None:
        self._generation = generation
        self._current_generation = current_generation
        self._active = True

    def require_active_ownership(self) -> None:
        if not self._active or self._current_generation() != self._generation:
            raise DrawingMachineError(
                ErrorPayload(
                    code="REPOSITORY_TRANSACTION_INACTIVE",
                    category=ErrorCategory.INTERNAL,
                    message="repository transaction is no longer active",
                    retryable=False,
                    details={},
                )
            )

    def invalidate(self) -> None:
        self._active = False


class _SQLiteRepositoryTransaction:
    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        capability: _TransactionCapability,
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._capability = capability

    def create_job(
        self,
        job: JobRecord,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> None:
        self._capability.require_active_ownership()
        if (
            event.job_id != job.job_id
            or event.job_revision != job.revision
            or event.prior_state is not None
            or event.result_state is not job.state
        ):
            raise _consistency_error(
                "created job and job event state identity must match",
                job_id=job.job_id,
            )
        _assert_audit_matches_event(event, audit)
        _assert_unique_artifact_roles(artifacts, job_id=job.job_id)
        job_json = job.to_json()
        self._connection.execute(
            """
            INSERT INTO jobs (
                job_id,
                schema_version,
                kind,
                name,
                state,
                revision,
                request_json,
                config_snapshot_json,
                blocker_json,
                error_json,
                ready_snapshot_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.schema_version,
                job.kind.value,
                job.name,
                job.state.value,
                job.revision,
                _canonical_json(job_json["request"]),
                _canonical_json(job_json["config"]),
                None if job.blocker is None else _canonical_json(job_json["blocker"]),
                None if job.error is None else _canonical_json(job_json["error"]),
                None if job.ready_snapshot is None else _canonical_json(job_json["ready_snapshot"]),
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )
        self._insert_artifacts(job.job_id, job.revision, artifacts)
        self._insert_event(event)
        self.append_audit(
            audit.event_id,
            audit.event_type,
            audit.request_id,
            _plain_audit_payload(audit),
        )

    def create_job_with_staging(
        self,
        job: JobRecord,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
        request_id: str,
        expected_staging_revision: int,
    ) -> StagingAdmissionRecordV1:
        self._capability.require_active_ownership()
        row = self._connection.execute(
            f"SELECT {_STAGING_SELECT_COLUMNS} FROM staging_admissions WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise _consistency_error("staging admission does not exist", job_id=job.job_id)
        current = _staging_from_row(cast(tuple[object, ...], row))
        if current.state is not StagingAdmissionState.CLAIMED or current.revision != expected_staging_revision:
            raise _consistency_error("staging admission is not the expected claimed revision", job_id=job.job_id)
        self.create_job(job, artifacts=artifacts, event=event, audit=audit)
        now = self._clock.now().isoformat()
        cursor = self._connection.execute(
            """
            UPDATE staging_admissions
            SET state = 'DURABLY_ADMITTED', job_id = ?, revision = revision + 1, updated_at_utc = ?
            WHERE request_id = ? AND state = 'CLAIMED' AND revision = ?
            """,
            (job.job_id, now, request_id, expected_staging_revision),
        )
        if cursor.rowcount != 1:
            raise _consistency_error("staging admission changed during atomic job admission", job_id=job.job_id)
        updated = self._connection.execute(
            f"SELECT {_STAGING_SELECT_COLUMNS} FROM staging_admissions WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        assert updated is not None
        return _staging_from_row(cast(tuple[object, ...], updated))

    def transition_job(
        self,
        transition: JobTransition,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        self._capability.require_active_ownership()
        row = self._connection.execute(
            f"""
            SELECT {_JOB_SELECT_COLUMNS}
            FROM jobs
            WHERE job_id = ?
            """,
            (transition.job_id,),
        ).fetchone()
        if row is None:
            raise _consistency_error("job does not exist", job_id=transition.job_id)
        current = _job_from_row(cast(tuple[object, ...], row))
        if current.revision != transition.expected_revision:
            raise _consistency_error(
                "job revision does not match the expected revision",
                job_id=transition.job_id,
            )
        if (
            event.job_id != transition.job_id
            or event.job_revision != transition.expected_revision + 1
            or event.prior_state is not current.state
            or event.result_state is not transition.result_state
            or event.request_id != transition.request_id
            or event.event_type != transition.event_type
        ):
            raise _consistency_error(
                "job transition and job event must describe the same revision change",
                job_id=transition.job_id,
            )
        _assert_audit_matches_event(event, audit)
        _assert_unique_artifact_roles(artifacts, job_id=transition.job_id)

        transition_json = transition.to_json()
        updated_json = current.to_json()
        ready_snapshot = transition_json["ready_snapshot"]
        if transition.result_state is JobState.CANCELLED and current.ready_snapshot is not None:
            ready_snapshot = updated_json["ready_snapshot"]
        updated_json.update(
            {
                "state": transition.result_state.value,
                "revision": transition.expected_revision + 1,
                "blocker": transition_json["blocker"],
                "error": transition_json["error"],
                "ready_snapshot": ready_snapshot,
                "updated_at": self._clock.now().isoformat(),
            }
        )
        updated = JobRecord.from_json(updated_json)
        try:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = ?,
                    revision = ?,
                    blocker_json = ?,
                    error_json = ?,
                    ready_snapshot_json = ?,
                    updated_at = ?
                WHERE job_id = ? AND revision = ?
                """,
                (
                    updated.state.value,
                    updated.revision,
                    None if updated.blocker is None else _canonical_json(updated_json["blocker"]),
                    None if updated.error is None else _canonical_json(updated_json["error"]),
                    None if updated.ready_snapshot is None else _canonical_json(updated_json["ready_snapshot"]),
                    updated.updated_at.isoformat(),
                    updated.job_id,
                    transition.expected_revision,
                ),
            )
        except sqlite3.IntegrityError as error:
            if "active machine ownership prevents job cancellation" not in str(error):
                raise
            raise DrawingMachineError(
                ErrorPayload(
                    code="MACHINE_ACTIVE_OWNER_CANCEL_DENIED",
                    category=ErrorCategory.INTERNAL,
                    message="active machine ownership prevents job cancellation",
                    retryable=False,
                    details={"job_id": updated.job_id},
                    job_id=updated.job_id,
                )
            ) from error
        if cursor.rowcount != 1:
            raise _consistency_error(
                "job revision changed during transition",
                job_id=transition.job_id,
            )
        self._insert_artifacts(updated.job_id, updated.revision, artifacts)
        self._insert_event(event)
        self.append_audit(
            audit.event_id,
            audit.event_type,
            audit.request_id,
            _plain_audit_payload(audit),
        )
        return updated

    def append_audit(
        self,
        event_id: str,
        event_type: str,
        request_id: str | None,
        payload: JsonObject,
    ) -> None:
        self._capability.require_active_ownership()
        payload_json = _canonical_json(payload)
        self._connection.execute(
            """
            INSERT INTO audit_events (event_id, event_type, request_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, event_type, request_id, payload_json, self._clock.now().isoformat()),
        )

    def _insert_artifacts(
        self,
        job_id: str,
        job_revision: int,
        artifacts: tuple[ArtifactRef, ...],
    ) -> None:
        for artifact in artifacts:
            artifact_json = artifact.to_json()
            self._connection.execute(
                """
                INSERT INTO job_artifacts (
                    job_id,
                    job_revision,
                    role,
                    relative_path,
                    sha256,
                    size_bytes,
                    media_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_revision,
                    artifact_json["role"],
                    artifact_json["relative_path"],
                    artifact_json["sha256"],
                    artifact_json["size_bytes"],
                    artifact_json["media_type"],
                ),
            )

    def _insert_event(self, event: JobEvent) -> None:
        event_json = event.to_json()
        self._connection.execute(
            """
            INSERT INTO job_events (
                event_id,
                job_id,
                job_revision,
                event_type,
                prior_state,
                result_state,
                request_id,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.job_id,
                event.job_revision,
                event.event_type,
                None if event.prior_state is None else event.prior_state.value,
                event.result_state.value,
                event.request_id,
                _canonical_json(event_json["payload"]),
                event.created_at.isoformat(),
            ),
        )


class SQLiteRepository:
    def __init__(self, path: Path, *, clock: Clock, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self._path = path
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._transaction_generation = 0
        self._active_transaction_generation: int | None = None

    def initialize(self) -> int:
        connection = self._get_connection()
        return migrate(connection, applied_at=self._clock.now().isoformat())

    def health(self) -> RepositoryHealth:
        connection = self._get_connection()
        version_row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        journal_row = connection.execute("PRAGMA journal_mode").fetchone()
        foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
        return RepositoryHealth(
            schema_version=int(version_row[0]) if version_row is not None else 0,
            journal_mode=str(journal_row[0]).lower() if journal_row is not None else "",
            foreign_keys_enabled=bool(foreign_keys_row[0]) if foreign_keys_row is not None else False,
        )

    @contextmanager
    def transaction(self) -> Iterator[RepositoryTransaction]:
        connection = self._get_connection()
        connection.execute("BEGIN IMMEDIATE")
        self._transaction_generation += 1
        generation = self._transaction_generation
        self._active_transaction_generation = generation
        capability = _TransactionCapability(generation, lambda: self._active_transaction_generation)
        transaction = _SQLiteRepositoryTransaction(connection, self._clock, capability)
        try:
            try:
                yield transaction
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            capability.invalidate()
            self._active_transaction_generation = None

    def append_audit(
        self,
        event_id: str,
        event_type: str,
        request_id: str | None,
        payload: JsonObject,
    ) -> None:
        with self.transaction() as transaction:
            transaction.append_audit(event_id, event_type, request_id, payload)

    def bind_staging_request_envelope(
        self,
        event_id: str,
        request_id: str,
        role: str,
        envelope_sha256: str,
    ) -> str:
        if (
            type(event_id) is not str
            or not event_id
            or type(request_id) is not str
            or not request_id
            or role not in {item.value for item in ClientDataRole}
            or type(envelope_sha256) is not str
            or len(envelope_sha256) != 64
            or any(character not in "0123456789abcdef" for character in envelope_sha256)
        ):
            raise _consistency_error("staging request-envelope binding is invalid")
        payload: JsonObject = {
            "schema_version": 1,
            "role": role,
            "envelope_sha256": envelope_sha256,
        }
        connection = self._get_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT event_type, request_id, payload_json FROM audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO audit_events (event_id, event_type, request_id, payload_json, created_at)
                    VALUES (?, 'staging.request-envelope.v1', ?, ?, ?)
                    """,
                    (event_id, request_id, _canonical_json(payload), self._clock.now().isoformat()),
                )
                bound = envelope_sha256
            else:
                event_type, existing_request_id, payload_json = cast(tuple[object, object, object], row)
                try:
                    existing = _decode_json(cast(str, payload_json))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise _consistency_error("staging request-envelope audit authority is corrupt") from error
                if (
                    event_type != "staging.request-envelope.v1"
                    or existing_request_id != request_id
                    or type(existing) is not dict
                    or set(cast(dict[object, object], existing)) != {"schema_version", "role", "envelope_sha256"}
                ):
                    raise _consistency_error("staging request-envelope audit authority is corrupt")
                values = cast(dict[str, object], existing)
                if values["schema_version"] != 1 or values["role"] != role:
                    raise _consistency_error("staging request-envelope audit authority is corrupt")
                bound_value = values["envelope_sha256"]
                if (
                    type(bound_value) is not str
                    or len(bound_value) != 64
                    or any(character not in "0123456789abcdef" for character in bound_value)
                ):
                    raise _consistency_error("staging request-envelope audit authority is corrupt")
                bound = bound_value
            connection.commit()
            return bound
        except BaseException:
            connection.rollback()
            raise

    def get_job(self, job_id: str) -> JobRecord | None:
        row = (
            self._get_connection()
            .execute(
                f"""
            SELECT {_JOB_SELECT_COLUMNS}
            FROM jobs
            WHERE job_id = ?
            """,
                (job_id,),
            )
            .fetchone()
        )
        return None if row is None else _job_from_row(cast(tuple[object, ...], row))

    def register_decoded_staging(self, record: StagingAdmissionRecordV1) -> None:
        if type(record) is not StagingAdmissionRecordV1 or record.state is not StagingAdmissionState.REQUEST_DECODED:
            raise _consistency_error("decoded staging registration must be an exact REQUEST_DECODED record")
        connection = self._get_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO staging_admissions (
                    schema_version, request_id, role, bundle_name,
                    payload_device, payload_inode, payload_size_bytes,
                    source_sha256, lease_sha256, publisher_boot_id,
                    published_monotonic_ns, deadline_monotonic_ns,
                    state, job_id, revision, created_at_utc, updated_at_utc, recovery_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.schema_version,
                    record.request_id,
                    record.role.value,
                    record.bundle_name,
                    record.payload_identity.device,
                    record.payload_identity.inode,
                    record.payload_identity.size_bytes,
                    record.source_sha256,
                    record.lease_sha256,
                    record.publisher_boot_id,
                    record.published_monotonic_ns,
                    record.deadline_monotonic_ns,
                    record.state.value,
                    record.job_id,
                    record.revision,
                    record.created_at_utc,
                    record.updated_at_utc,
                    None if record.recovery_code is None else record.recovery_code.value,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def get_staging_admission(self, request_id: str) -> StagingAdmissionRecordV1 | None:
        row = (
            self._get_connection()
            .execute(
                f"SELECT {_STAGING_SELECT_COLUMNS} FROM staging_admissions WHERE request_id = ?",
                (request_id,),
            )
            .fetchone()
        )
        return None if row is None else _staging_from_row(cast(tuple[object, ...], row))

    def list_staging_admissions(self) -> tuple[StagingAdmissionRecordV1, ...]:
        rows = (
            self._get_connection()
            .execute(f"SELECT {_STAGING_SELECT_COLUMNS} FROM staging_admissions ORDER BY request_id")
            .fetchall()
        )
        return tuple(_staging_from_row(cast(tuple[object, ...], row)) for row in rows)

    def mark_staging_claimed(
        self,
        request_id: str,
        *,
        expected_revision: int,
    ) -> StagingAdmissionRecordV1:
        return self._transition_staging(
            request_id,
            expected_revision=expected_revision,
            expected_state=StagingAdmissionState.REQUEST_DECODED,
            result_state=StagingAdmissionState.CLAIMED,
        )

    def mark_staging_consumed(
        self,
        request_id: str,
        *,
        expected_revision: int,
    ) -> StagingAdmissionRecordV1:
        return self._transition_staging(
            request_id,
            expected_revision=expected_revision,
            expected_state=StagingAdmissionState.DURABLY_ADMITTED,
            result_state=StagingAdmissionState.CONSUMED,
        )

    def record_staging_recovery(
        self,
        request_id: str,
        *,
        expected_revision: int,
        recovery_code: StagingRecoveryReasonCode,
        quarantined: bool,
    ) -> StagingAdmissionRecordV1:
        current = self.get_staging_admission(request_id)
        if current is None:
            raise _consistency_error("staging admission does not exist")
        result = StagingAdmissionState.QUARANTINED if quarantined else StagingAdmissionState.RECOVERY_REQUIRED
        return self._transition_staging(
            request_id,
            expected_revision=expected_revision,
            expected_state=current.state,
            result_state=result,
            recovery_code=None if quarantined else recovery_code,
        )

    def _transition_staging(
        self,
        request_id: str,
        *,
        expected_revision: int,
        expected_state: StagingAdmissionState,
        result_state: StagingAdmissionState,
        recovery_code: StagingRecoveryReasonCode | None = None,
    ) -> StagingAdmissionRecordV1:
        connection = self._get_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE staging_admissions
                SET state = ?, revision = revision + 1, updated_at_utc = ?, recovery_code = ?
                WHERE request_id = ? AND state = ? AND revision = ?
                """,
                (
                    result_state.value,
                    self._clock.now().isoformat(),
                    None if recovery_code is None else recovery_code.value,
                    request_id,
                    expected_state.value,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise _consistency_error("staging admission state or revision changed")
            row = connection.execute(
                f"SELECT {_STAGING_SELECT_COLUMNS} FROM staging_admissions WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            assert row is not None
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return _staging_from_row(cast(tuple[object, ...], row))

    def list_jobs_in_states(self, states: frozenset[JobState]) -> tuple[JobRecord, ...]:
        if not states:
            return ()
        state_values = sorted(state.value for state in states)
        placeholders = ", ".join("?" for _state in state_values)
        rows = (
            self._get_connection()
            .execute(
                f"""
            SELECT {_JOB_SELECT_COLUMNS}
            FROM jobs
            WHERE state IN ({placeholders})
            ORDER BY job_id
            """,
                state_values,
            )
            .fetchall()
        )
        return tuple(_job_from_row(cast(tuple[object, ...], row)) for row in rows)

    def list_artifacts(self, job_id: str) -> tuple[ArtifactRef, ...]:
        rows = (
            self._get_connection()
            .execute(
                """
            SELECT role, relative_path, sha256, size_bytes, media_type
            FROM job_artifacts
            WHERE job_id = ?
            ORDER BY job_revision, role
            """,
                (job_id,),
            )
            .fetchall()
        )
        return tuple(_artifact_from_row(cast(tuple[object, ...], row)) for row in rows)

    def list_job_events(self, job_id: str) -> tuple[JobEvent, ...]:
        rows = (
            self._get_connection()
            .execute(
                """
            SELECT
                event_id,
                job_id,
                job_revision,
                event_type,
                prior_state,
                result_state,
                request_id,
                payload_json,
                created_at
            FROM job_events
            WHERE job_id = ?
            ORDER BY job_revision, event_id
            """,
                (job_id,),
            )
            .fetchall()
        )
        return tuple(_event_from_row(cast(tuple[object, ...], row)) for row in rows)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(
                self._path,
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
            )
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection
