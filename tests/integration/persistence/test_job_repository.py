import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    JobTransition,
    ReadyToRunSnapshot,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.repository import RepositoryTransaction


@dataclass(slots=True)
class MutableClock:
    value: datetime = datetime(2026, 7, 10, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def repository(tmp_path: Path, clock: MutableClock) -> SQLiteRepository:
    instance = SQLiteRepository(tmp_path / "drawingmachine.db", clock=clock)
    assert instance.initialize() == 5
    try:
        yield instance
    finally:
        instance.close()


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def make_config() -> ConfigSnapshot:
    return ConfigSnapshot(
        machine_profile="default",
        provider_profile="local-comfyui",
        application_schema_version=1,
        machine_schema_version=1,
        provider_schema_version=1,
        application_digest="a" * 64,
        machine_digest="b" * 64,
        provider_digest="c" * 64,
    )


def make_artifact(
    role: str,
    relative_path: str | None = None,
    *,
    digest: str = "d" * 64,
) -> ArtifactRef:
    return ArtifactRef(
        role=role,
        relative_path=relative_path or f"artifacts/{role}.json",
        sha256=digest,
        size_bytes=17,
        media_type="application/json",
    )


def make_ready_snapshot(job_id: str, revision: int) -> ReadyToRunSnapshot:
    return ReadyToRunSnapshot(
        schema_version=1,
        job_id=job_id,
        job_revision=revision,
        gcode=make_artifact("gcode", "artifacts/drawing.gcode", digest="e" * 64),
        artifacts=(make_artifact("preview", "artifacts/preview.svg", digest="f" * 64),),
        static_result={"gate": "PASS"},
        send_plan={"line_count": 10},
        readiness={"status": "READY"},
        config=make_config(),
        planning_parameters={"stroke_mode": "stroke"},
        application_version="0.2.0.dev0",
    )


def make_job(
    *,
    job_id: str = "job-1",
    state: JobState = JobState.QUEUED,
    revision: int = 0,
    kind: JobKind = JobKind.OFFLINE_WORKFLOW,
    error: ErrorPayload | None = None,
    ready_snapshot: ReadyToRunSnapshot | None = None,
) -> JobRecord:
    timestamp = datetime(2026, 7, 10, 12, tzinfo=UTC)
    return JobRecord(
        schema_version=1,
        job_id=job_id,
        kind=kind,
        name=f"fixture {job_id}",
        state=state,
        revision=revision,
        request={"zeta": [True, None], "route_mode": "direct", "alpha": {"value": 1}},
        config=make_config(),
        blocker=None,
        error=error,
        ready_snapshot=ready_snapshot,
        created_at=timestamp,
        updated_at=timestamp,
    )


def requester_payload(requester_type: str = "LOCAL_PEER") -> dict[str, Any]:
    if requester_type == "SERVICE":
        requester = RequesterIdentity(requester_type="SERVICE", pid=None, uid=None, gid=None)
    else:
        requester = RequesterIdentity(requester_type="LOCAL_PEER", pid=101, uid=1000, gid=1000)
    return {"requester": requester.to_json(), "zeta": 2, "alpha": 1}


def make_event(
    job: JobRecord,
    *,
    event_id: str = "event-created",
    event_type: str = "job.created",
    prior_state: JobState | None = None,
    result_state: JobState | None = None,
    job_id: str | None = None,
    job_revision: int | None = None,
    request_id: str | None = "request-1",
    requester_type: str = "LOCAL_PEER",
) -> JobEvent:
    return JobEvent(
        event_id=event_id,
        job_id=job.job_id if job_id is None else job_id,
        job_revision=job.revision if job_revision is None else job_revision,
        event_type=event_type,
        prior_state=prior_state,
        result_state=job.state if result_state is None else result_state,
        request_id=request_id,
        requester_type=requester_type,
        payload=requester_payload(requester_type),
        created_at=datetime(2026, 7, 10, 12, 0, 1, tzinfo=UTC),
    )


def make_transition_event(
    job: JobRecord,
    transition: JobTransition,
    *,
    event_id: str = "event-transition",
    requester_type: str = "LOCAL_PEER",
) -> JobEvent:
    return JobEvent(
        event_id=event_id,
        job_id=transition.job_id,
        job_revision=transition.expected_revision + 1,
        event_type=transition.event_type,
        prior_state=job.state,
        result_state=transition.result_state,
        request_id=transition.request_id,
        requester_type=requester_type,
        payload=requester_payload(requester_type),
        created_at=datetime(2026, 7, 10, 12, 0, 2, tzinfo=UTC),
    )


def make_audit(
    event: JobEvent,
    *,
    event_id: str | None = None,
    requester: RequesterIdentity | None = None,
) -> AuditRecord:
    requester_json = event.payload["requester"] if requester is None else requester.to_json()
    return AuditRecord(
        event_id=event.event_id if event_id is None else event_id,
        event_type=event.event_type,
        request_id=event.request_id,
        payload={
            "schema_version": 1,
            "requester": requester_json,
            "job_id": event.job_id,
            "command_summary": {
                "name": "service.advance",
                "event_type": event.event_type,
                "job_kind": "OFFLINE_WORKFLOW",
            },
            "prior_state": None if event.prior_state is None else event.prior_state.value,
            "result_state": event.result_state.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "0.2.0",
            "protocol_version": 1,
        },
    )


def create_job(
    repository: SQLiteRepository,
    job: JobRecord,
    *,
    artifacts: tuple[ArtifactRef, ...] = (),
    event_id: str = "event-created",
) -> JobEvent:
    event = make_event(job, event_id=event_id)
    with repository.transaction() as transaction:
        transaction.create_job(job, artifacts=artifacts, event=event, audit=make_audit(event))
    return event


def test_create_and_transition_persist_canonical_records_atomically(
    repository: SQLiteRepository,
    clock: MutableClock,
) -> None:
    original_artifact = make_artifact("source", "artifacts/source.png", digest="1" * 64)
    job = make_job()
    created = create_job(repository, job, artifacts=(original_artifact,))
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)
    alpha = make_artifact("alpha", digest="2" * 64)
    zeta = make_artifact("zeta", digest="3" * 64)
    clock.value = datetime(2026, 7, 10, 12, 0, 3, tzinfo=UTC)

    with repository.transaction() as transaction:
        updated = transaction.transition_job(
            transition,
            artifacts=(zeta, alpha),
            event=event,
            audit=make_audit(event),
        )

    assert updated.revision == 1
    assert updated.state is JobState.PREPARING_IMAGE
    assert updated.updated_at == clock.value
    assert repository.get_job(job.job_id) == updated
    assert repository.list_artifacts(job.job_id) == (original_artifact, alpha, zeta)
    assert repository.list_job_events(job.job_id) == (created, event)

    connection = repository._get_connection()
    artifact_rows = connection.execute(
        "SELECT job_revision, role FROM job_artifacts WHERE job_id = ? ORDER BY job_revision, role",
        (job.job_id,),
    ).fetchall()
    job_row = connection.execute(
        "SELECT request_json, config_snapshot_json FROM jobs WHERE job_id = ?",
        (job.job_id,),
    ).fetchone()
    event_row = connection.execute(
        "SELECT payload_json FROM job_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()
    audit_row = connection.execute(
        "SELECT payload_json FROM audit_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()

    assert artifact_rows == [(0, "source"), (1, "alpha"), (1, "zeta")]
    assert job_row == (
        canonical_json(updated.to_json()["request"]),
        canonical_json(updated.to_json()["config"]),
    )
    assert event_row == (canonical_json(event.to_json()["payload"]),)
    assert audit_row == (canonical_json(make_audit(event).to_json()["payload"]),)


@pytest.mark.parametrize(
    "invalid_part",
    [
        "event_job_id",
        "event_revision",
        "event_prior_state",
        "event_result_state",
        "audit_event_id",
        "audit_event_type",
        "audit_request_id",
    ],
)
def test_create_job_rejects_inconsistent_event_or_audit(
    repository: SQLiteRepository,
    invalid_part: str,
) -> None:
    job = make_job()
    event = make_event(job)
    audit = make_audit(event)
    if invalid_part == "event_job_id":
        event = make_event(job, job_id="other-job")
    elif invalid_part == "event_revision":
        event = make_event(job, job_revision=1)
    elif invalid_part == "event_prior_state":
        event = make_event(job, prior_state=JobState.QUEUED)
    elif invalid_part == "event_result_state":
        event = make_event(job, result_state=JobState.PREPARING_IMAGE)
    elif invalid_part == "audit_event_id":
        audit = make_audit(event, event_id="other-event")
    elif invalid_part == "audit_event_type":
        valid_payload = make_audit(event).to_json()["payload"]
        audit = AuditRecord(
            event_id=event.event_id,
            event_type="job.changed",
            request_id=event.request_id,
            payload=cast(JsonObject, valid_payload),
        )
    else:
        valid_payload = make_audit(event).to_json()["payload"]
        audit = AuditRecord(
            event_id=event.event_id,
            event_type=event.event_type,
            request_id="other-request",
            payload=cast(JsonObject, valid_payload),
        )

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.create_job(job, artifacts=(), event=event, audit=audit)

    assert repository.get_job(job.job_id) is None


@pytest.mark.parametrize(
    "invalid_part",
    [
        "event_job_id",
        "event_revision",
        "event_prior_state",
        "event_result_state",
        "event_request_id",
        "event_type",
        "audit_event_id",
    ],
)
def test_transition_rejects_inconsistent_transition_event_or_audit(
    repository: SQLiteRepository,
    invalid_part: str,
) -> None:
    job = make_job()
    create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)
    if invalid_part == "event_job_id":
        event = JobEvent.from_json({**event.to_json(), "job_id": "other-job"})
    elif invalid_part == "event_revision":
        event = JobEvent.from_json({**event.to_json(), "job_revision": 2})
    elif invalid_part == "event_prior_state":
        event = JobEvent.from_json({**event.to_json(), "prior_state": "IMAGE_READY"})
    elif invalid_part == "event_result_state":
        event = JobEvent.from_json({**event.to_json(), "result_state": "IMAGE_READY"})
    elif invalid_part == "event_request_id":
        event = JobEvent.from_json({**event.to_json(), "request_id": "other-request"})
    elif invalid_part == "event_type":
        event = JobEvent.from_json({**event.to_json(), "event_type": "job.changed"})
    audit = make_audit(event)
    if invalid_part == "audit_event_id":
        audit = make_audit(event, event_id="other-event")

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.transition_job(transition, artifacts=(), event=event, audit=audit)

    assert repository.get_job(job.job_id) == job
    assert repository.list_job_events(job.job_id) == (make_event(job),)


@pytest.mark.parametrize(
    "audit_requester",
    [
        RequesterIdentity(requester_type="SERVICE", pid=None, uid=None, gid=None),
        RequesterIdentity(requester_type="LOCAL_PEER", pid=202, uid=1000, gid=1000),
    ],
    ids=["requester_type", "peer_credentials"],
)
def test_create_rejects_mismatched_event_and_audit_requester_identity(
    repository: SQLiteRepository,
    audit_requester: RequesterIdentity,
) -> None:
    job = make_job()
    event = make_event(job)
    audit = make_audit(event, requester=audit_requester)

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.create_job(
            job,
            artifacts=(make_artifact("source"),),
            event=event,
            audit=audit,
        )

    assert repository.get_job(job.job_id) is None
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == ()


@pytest.mark.parametrize(
    "audit_requester",
    [
        RequesterIdentity(requester_type="SERVICE", pid=None, uid=None, gid=None),
        RequesterIdentity(requester_type="LOCAL_PEER", pid=202, uid=1000, gid=1000),
    ],
    ids=["requester_type", "peer_credentials"],
)
def test_transition_rejects_mismatched_event_and_audit_requester_identity(
    repository: SQLiteRepository,
    audit_requester: RequesterIdentity,
) -> None:
    job = make_job()
    created = create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)
    audit = make_audit(event, requester=audit_requester)

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.transition_job(
            transition,
            artifacts=(make_artifact("source"),),
            event=event,
            audit=audit,
        )

    assert repository.get_job(job.job_id) == job
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == (created,)


def test_transition_rolls_back_job_event_and_artifacts_on_audit_conflict(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)
    repository.append_audit(event.event_id, event.event_type, event.request_id, {"existing": True})

    with pytest.raises(sqlite3.IntegrityError), repository.transaction() as transaction:
        transaction.transition_job(
            transition,
            artifacts=(make_artifact("source"),),
            event=event,
            audit=make_audit(event),
        )

    assert repository.get_job(job.job_id) == job
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == (make_event(job),)


def test_create_rolls_back_job_artifacts_and_event_on_late_audit_conflict(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    artifact = make_artifact("source")
    event = make_event(job)
    repository.append_audit(event.event_id, event.event_type, event.request_id, {"existing": True})

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), repository.transaction() as transaction:
        transaction.create_job(
            job,
            artifacts=(artifact,),
            event=event,
            audit=make_audit(event),
        )

    assert repository.get_job(job.job_id) is None
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == ()
    assert repository._get_connection().execute(
        "SELECT payload_json FROM audit_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone() == ('{"existing":true}',)


def test_transition_rolls_back_update_and_artifacts_on_late_event_conflict(
    repository: SQLiteRepository,
) -> None:
    conflict_job = make_job(job_id="job-conflict")
    create_job(repository, conflict_job, event_id="event-transition")
    job = make_job()
    created = create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), repository.transaction() as transaction:
        transaction.transition_job(
            transition,
            artifacts=(make_artifact("source"),),
            event=event,
            audit=make_audit(event),
        )

    assert repository.get_job(job.job_id) == job
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == (created,)
    assert repository.list_job_events(conflict_job.job_id) == (make_event(conflict_job, event_id=event.event_id),)


def test_transition_rolls_back_update_on_late_artifact_conflict(repository: SQLiteRepository) -> None:
    job = make_job()
    created = create_job(repository, job)
    artifact = make_artifact("source")
    repository._get_connection().execute(
        """
        INSERT INTO job_artifacts (
            job_id, job_revision, role, relative_path, sha256, size_bytes, media_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.job_id,
            1,
            artifact.role,
            artifact.relative_path,
            artifact.sha256,
            artifact.size_bytes,
            artifact.media_type,
        ),
    )
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    event = make_transition_event(job, transition)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), repository.transaction() as transaction:
        transaction.transition_job(
            transition,
            artifacts=(artifact,),
            event=event,
            audit=make_audit(event),
        )

    assert repository.get_job(job.job_id) == job
    assert repository.list_artifacts(job.job_id) == (artifact,)
    assert repository.list_job_events(job.job_id) == (created,)
    assert repository._get_connection().execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone() == (0,)


def test_transition_rejects_stale_expected_revision_without_partial_writes(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    create_job(repository, job)
    first = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    first_event = make_transition_event(job, first)
    with repository.transaction() as transaction:
        updated = transaction.transition_job(first, artifacts=(), event=first_event, audit=make_audit(first_event))

    stale_event = make_transition_event(job, first, event_id="event-stale")
    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.transition_job(
            first,
            artifacts=(make_artifact("stale"),),
            event=stale_event,
            audit=make_audit(stale_event),
        )

    assert repository.get_job(job.job_id) == updated
    assert repository.list_artifacts(job.job_id) == ()
    assert repository.list_job_events(job.job_id) == (make_event(job), first_event)


def test_transition_persists_b2_valid_future_state_change_without_graph_policy(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.IMAGE_READY,
        request_id="request-2",
        event_type="job.image_ready",
    )
    event = make_transition_event(job, transition)

    with repository.transaction() as transaction:
        updated = transaction.transition_job(transition, artifacts=(), event=event, audit=make_audit(event))

    assert updated.state is JobState.IMAGE_READY
    assert updated.revision == 1
    assert repository.get_job(job.job_id) == updated
    assert repository.list_job_events(job.job_id) == (make_event(job), event)


def test_duplicate_artifact_roles_in_one_revision_roll_back_create(repository: SQLiteRepository) -> None:
    job = make_job()
    event = make_event(job)
    duplicate_roles = (
        make_artifact("source", "artifacts/source-a.png", digest="1" * 64),
        make_artifact("source", "artifacts/source-b.png", digest="2" * 64),
    )

    with pytest.raises(DrawingMachineError), repository.transaction() as transaction:
        transaction.create_job(job, artifacts=duplicate_roles, event=event, audit=make_audit(event))

    assert repository.get_job(job.job_id) is None


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        ("job_events", "UPDATE job_events SET event_type = 'changed' WHERE event_id = 'event-created'"),
        ("job_events", "DELETE FROM job_events WHERE event_id = 'event-created'"),
        ("job_artifacts", "UPDATE job_artifacts SET role = 'changed' WHERE role = 'source'"),
        ("job_artifacts", "DELETE FROM job_artifacts WHERE role = 'source'"),
    ],
)
def test_job_events_and_artifacts_are_append_only_at_database_level(
    repository: SQLiteRepository,
    table: str,
    statement: str,
) -> None:
    job = make_job()
    create_job(repository, job, artifacts=(make_artifact("source"),))

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        repository._get_connection().execute(statement)

    assert repository._get_connection().execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)


@pytest.mark.parametrize("table", ["job_events", "job_artifacts"])
def test_job_events_and_artifacts_reject_duplicate_insert_or_replace(
    repository: SQLiteRepository,
    table: str,
) -> None:
    job = make_job()
    artifact = make_artifact("source")
    event = create_job(repository, job, artifacts=(artifact,))

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        if table == "job_events":
            repository._get_connection().execute(
                """
                INSERT OR REPLACE INTO job_events (
                    event_id, job_id, job_revision, event_type, prior_state,
                    result_state, request_id, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.job_id,
                    event.job_revision,
                    "job.changed",
                    None,
                    event.result_state.value,
                    event.request_id,
                    canonical_json(event.to_json()["payload"]),
                    event.created_at.isoformat(),
                ),
            )
        else:
            repository._get_connection().execute(
                """
                INSERT OR REPLACE INTO job_artifacts (
                    job_id, job_revision, role, relative_path, sha256, size_bytes, media_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.revision,
                    artifact.role,
                    "artifacts/replaced.json",
                    "9" * 64,
                    99,
                    "application/json",
                ),
            )

    assert repository.list_job_events(job.job_id) == (event,)
    assert repository.list_artifacts(job.job_id) == (artifact,)


@pytest.mark.parametrize("replacement", [None, "{}"])
def test_ready_snapshot_cannot_be_changed_or_cleared(
    repository: SQLiteRepository,
    replacement: str | None,
) -> None:
    snapshot = make_ready_snapshot("job-ready", 7)
    job = make_job(
        job_id="job-ready",
        state=JobState.READY_TO_RUN,
        revision=7,
        ready_snapshot=snapshot,
    )
    create_job(repository, job)

    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        repository._get_connection().execute(
            "UPDATE jobs SET revision = 8, ready_snapshot_json = ? WHERE job_id = ?",
            (replacement, job.job_id),
        )

    assert repository.get_job(job.job_id) == job


def test_ready_snapshot_trigger_allows_null_to_remain_null(repository: SQLiteRepository) -> None:
    job = make_job()
    create_job(repository, job)

    repository._get_connection().execute(
        "UPDATE jobs SET revision = 1 WHERE job_id = ?",
        (job.job_id,),
    )

    assert repository._get_connection().execute(
        "SELECT revision, ready_snapshot_json FROM jobs WHERE job_id = ?",
        (job.job_id,),
    ).fetchone() == (1, None)


def test_ready_snapshot_trigger_allows_same_value_with_next_revision(repository: SQLiteRepository) -> None:
    snapshot = make_ready_snapshot("job-ready", 7)
    job = make_job(
        job_id="job-ready",
        state=JobState.READY_TO_RUN,
        revision=7,
        ready_snapshot=snapshot,
    )
    create_job(repository, job)
    connection = repository._get_connection()
    stored_snapshot = connection.execute(
        "SELECT ready_snapshot_json FROM jobs WHERE job_id = ?",
        (job.job_id,),
    ).fetchone()
    assert stored_snapshot is not None

    connection.execute(
        "UPDATE jobs SET revision = 8, ready_snapshot_json = ? WHERE job_id = ?",
        (stored_snapshot[0], job.job_id),
    )

    assert connection.execute(
        "SELECT revision, ready_snapshot_json FROM jobs WHERE job_id = ?",
        (job.job_id,),
    ).fetchone() == (8, stored_snapshot[0])


def test_job_revision_updates_must_advance_exactly_one(repository: SQLiteRepository) -> None:
    job = make_job()
    create_job(repository, job)

    with pytest.raises(sqlite3.DatabaseError, match="revision"):
        repository._get_connection().execute(
            "UPDATE jobs SET revision = 2 WHERE job_id = ?",
            (job.job_id,),
        )

    assert repository.get_job(job.job_id) == job


def test_job_event_requester_type_round_trips_from_validated_nested_payload(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    event = make_event(job, requester_type="SERVICE", request_id=None)
    with repository.transaction() as transaction:
        transaction.create_job(job, artifacts=(), event=event, audit=make_audit(event))

    stored = repository.list_job_events(job.job_id)

    assert stored == (event,)
    assert stored[0].requester_type == "SERVICE"
    assert repository._get_connection().execute("PRAGMA table_info(job_events)").fetchall()[0] is not None
    columns = {str(row[1]) for row in repository._get_connection().execute("PRAGMA table_info(job_events)").fetchall()}
    assert "requester_type" not in columns


def test_reads_reconstruct_through_strict_job_codec(repository: SQLiteRepository) -> None:
    job = make_job()
    create_job(repository, job)
    config_payload = job.config.to_json()
    config_payload["unknown"] = True
    repository._get_connection().execute(
        "UPDATE jobs SET revision = 1, config_snapshot_json = ? WHERE job_id = ?",
        (canonical_json(config_payload), job.job_id),
    )

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        repository.get_job(job.job_id)


def _assert_inactive(error: pytest.ExceptionInfo[DrawingMachineError]) -> None:
    assert error.value.payload.code == "REPOSITORY_TRANSACTION_INACTIVE"
    assert error.value.payload.category is ErrorCategory.INTERNAL


def test_new_transaction_methods_are_invalid_after_lexical_scope(
    repository: SQLiteRepository,
) -> None:
    job = make_job()
    event = make_event(job)
    retained: RepositoryTransaction
    with repository.transaction() as retained:
        pass

    with pytest.raises(DrawingMachineError) as create_error:
        retained.create_job(job, artifacts=(), event=event, audit=make_audit(event))
    _assert_inactive(create_error)

    create_job(repository, job)
    transition = JobTransition(
        job_id=job.job_id,
        expected_revision=0,
        result_state=JobState.PREPARING_IMAGE,
        request_id="request-2",
        event_type="job.preparing_image",
    )
    transition_event = make_transition_event(job, transition)
    with pytest.raises(DrawingMachineError) as transition_error:
        retained.transition_job(
            transition,
            artifacts=(),
            event=transition_event,
            audit=make_audit(transition_event),
        )
    _assert_inactive(transition_error)


def test_restart_reconciliation_uses_list_and_ordinary_atomic_transitions(
    repository: SQLiteRepository,
) -> None:
    queued = make_job(job_id="job-queued")
    stable = make_job(job_id="job-stable", state=JobState.IMAGE_READY, revision=4)
    create_job(repository, queued, event_id="event-created-queued")
    create_job(repository, stable, event_id="event-created-stable")
    restart_states = frozenset(
        {
            JobState.QUEUED,
            JobState.PREPARING_IMAGE,
            JobState.PLANNING_PATHS,
            JobState.BUILDING_GCODE,
            JobState.VALIDATING_GCODE,
        }
    )

    jobs_to_fail = repository.list_jobs_in_states(restart_states)
    assert jobs_to_fail == (queued,)
    for current in jobs_to_fail:
        request_id = "service-restart"
        transition = JobTransition(
            job_id=current.job_id,
            expected_revision=current.revision,
            result_state=JobState.FAILED,
            request_id=request_id,
            event_type="job.service_restarted",
            error=ErrorPayload(
                code="SERVICE_RESTARTED",
                category=ErrorCategory.SERVICE,
                message="service restarted before the job completed",
                retryable=False,
                details={"prior_state": current.state.value},
                request_id=request_id,
                job_id=current.job_id,
            ),
        )
        event = make_transition_event(
            current,
            transition,
            event_id=f"event-restart-{current.job_id}",
            requester_type="SERVICE",
        )
        with repository.transaction() as transaction:
            transaction.transition_job(transition, artifacts=(), event=event, audit=make_audit(event))

    reconciled = repository.get_job(queued.job_id)
    assert reconciled is not None
    assert reconciled.state is JobState.FAILED
    assert reconciled.error is not None
    assert reconciled.error.code == "SERVICE_RESTARTED"
    assert repository.get_job(stable.job_id) == stable
    assert repository.list_jobs_in_states(frozenset()) == ()


def test_initialize_rejects_offline_job_chain_checksum_mismatch(tmp_path: Path, clock: MutableClock) -> None:
    database_path = tmp_path / "checksum.db"
    repository = SQLiteRepository(database_path, clock=clock)
    repository.initialize()
    repository.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET sha256 = ? WHERE version = 2", ("0" * 64,))

    with pytest.raises(DrawingMachineError) as error:
        SQLiteRepository(database_path, clock=clock).initialize()

    assert error.value.payload.code == "DATABASE_MIGRATION_CHECKSUM_MISMATCH"
    assert error.value.payload.details == {
        "migration_version": 2,
        "migration_name": "offline_job_chain",
    }
