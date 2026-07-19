from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from drawingmachine.domain.jobs import AuditPayloadV1, AuditRecord, JobEvent, JobKind, JobState, RequesterIdentity
from drawingmachine.errors import DrawingMachineError


def event_json() -> dict[str, Any]:
    requester = RequesterIdentity(
        requester_type="LOCAL_PEER",
        pid=101,
        uid=1000,
        gid=1000,
    )
    return {
        "event_id": "event-1",
        "job_id": "job-1",
        "job_revision": 1,
        "event_type": "job.preparing_image",
        "prior_state": "QUEUED",
        "result_state": "PREPARING_IMAGE",
        "request_id": "req-1",
        "requester_type": "LOCAL_PEER",
        "payload": {"requester": requester.to_json()},
        "created_at": "2026-07-10T12:00:01+00:00",
    }


def audit_json() -> dict[str, Any]:
    return {
        "event_id": "event-1",
        "event_type": "job.preparing_image",
        "request_id": "req-1",
        "payload": {
            "schema_version": 1,
            "job_id": "job-1",
            "requester": {"requester_type": "LOCAL_PEER", "pid": 101, "uid": 1000, "gid": 1000},
            "command_summary": {
                "name": "service.advance",
                "event_type": "job.preparing_image",
                "job_kind": "OFFLINE_WORKFLOW",
            },
            "prior_state": "QUEUED",
            "result_state": "PREPARING_IMAGE",
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "0.2.0",
            "protocol_version": 1,
        },
    }


def test_job_event_and_audit_record_round_trip() -> None:
    event = JobEvent.from_json(event_json())
    audit = AuditRecord.from_json(audit_json())

    assert JobEvent.from_json(event.to_json()) == event
    assert AuditRecord.from_json(audit.to_json()) == audit
    assert event.prior_state is JobState.QUEUED
    assert event.result_state is JobState.PREPARING_IMAGE


def test_event_contracts_are_frozen_and_slotted() -> None:
    event = JobEvent.from_json(event_json())
    with pytest.raises(FrozenInstanceError):
        event.event_type = "changed"  # type: ignore[misc]
    assert not hasattr(event, "__dict__")


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (JobEvent.from_json, event_json()),
        (AuditRecord.from_json, audit_json()),
    ],
)
def test_event_from_json_rejects_unknown_keys(factory: Any, payload: dict[str, Any]) -> None:
    payload["unexpected"] = True

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        factory(payload)


@pytest.mark.parametrize(
    ("factory", "payload", "missing"),
    [
        (JobEvent.from_json, event_json(), "event_id"),
        (AuditRecord.from_json, audit_json(), "event_id"),
    ],
)
def test_event_from_json_rejects_missing_keys(factory: Any, payload: dict[str, Any], missing: str) -> None:
    del payload[missing]

    with pytest.raises(DrawingMachineError, match="missing keys"):
        factory(payload)


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (JobEvent.from_json, event_json()),
        (AuditRecord.from_json, audit_json()),
    ],
)
def test_event_payloads_reject_non_json_values(factory: Any, payload: dict[str, Any]) -> None:
    payload["payload"] = {"invalid": object()}

    with pytest.raises(DrawingMachineError, match="JSON-compatible"):
        factory(payload)


def test_job_event_accepts_service_reconciliation_identity() -> None:
    payload = event_json()
    payload["requester_type"] = "SERVICE"
    payload["request_id"] = None
    payload["payload"] = {
        "requester": RequesterIdentity(requester_type="SERVICE", pid=None, uid=None, gid=None).to_json()
    }

    event = JobEvent.from_json(payload)

    assert event.requester_type == "SERVICE"
    assert event.request_id is None


@pytest.mark.parametrize("requester_type", ["OPENCLAW", "HUMAN", ""])
def test_job_event_rejects_unprovable_requester_types(requester_type: str) -> None:
    payload = event_json()
    payload["requester_type"] = requester_type

    with pytest.raises(DrawingMachineError, match="requester_type"):
        JobEvent.from_json(payload)


def test_job_event_rejects_negative_revision_and_naive_timestamp() -> None:
    negative_revision = event_json()
    negative_revision["job_revision"] = -1
    with pytest.raises(DrawingMachineError, match="job_revision"):
        JobEvent.from_json(negative_revision)

    naive_timestamp = event_json()
    naive_timestamp["created_at"] = "2026-07-10T12:00:01"
    with pytest.raises(DrawingMachineError, match="timezone-aware"):
        JobEvent.from_json(naive_timestamp)


@pytest.mark.parametrize("factory", [JobEvent.from_json, AuditRecord.from_json])
def test_event_and_audit_require_nested_requester_identity(factory: Any) -> None:
    payload = event_json() if factory == JobEvent.from_json else audit_json()
    del payload["payload"]["requester"]

    with pytest.raises(DrawingMachineError, match="requester"):
        factory(payload)


def test_job_event_requester_type_must_match_nested_identity() -> None:
    payload = event_json()
    payload["payload"]["requester"] = {
        "requester_type": "SERVICE",
        "pid": None,
        "uid": None,
        "gid": None,
    }

    with pytest.raises(DrawingMachineError, match="match"):
        JobEvent.from_json(payload)


@pytest.mark.parametrize(
    "requester",
    [
        {"requester_type": "LOCAL_PEER", "pid": None, "uid": 1000, "gid": 1000},
        {"requester_type": "SERVICE", "pid": 101, "uid": None, "gid": None},
    ],
)
@pytest.mark.parametrize("factory", [JobEvent.from_json, AuditRecord.from_json])
def test_event_and_audit_reject_inconsistent_nested_requester_credentials(
    factory: Any,
    requester: dict[str, Any],
) -> None:
    payload = event_json() if factory == JobEvent.from_json else audit_json()
    payload["payload"]["requester"] = requester

    with pytest.raises(DrawingMachineError, match="requester identity"):
        factory(payload)


def test_direct_event_and_audit_require_nested_requester_identity() -> None:
    with pytest.raises(DrawingMachineError, match="requester"):
        JobEvent(
            event_id="event-1",
            job_id="job-1",
            job_revision=1,
            event_type="job.preparing_image",
            prior_state=JobState.QUEUED,
            result_state=JobState.PREPARING_IMAGE,
            request_id="req-1",
            requester_type="LOCAL_PEER",
            payload={},
            created_at=datetime(2026, 7, 10, 12, tzinfo=UTC),
        )

    with pytest.raises(DrawingMachineError, match="requester"):
        AuditRecord(event_id="event-1", event_type="job.created", request_id="req-1", payload={})


def test_event_payload_is_deeply_immutable_and_to_json_is_detached() -> None:
    payload = event_json()
    payload["payload"]["nested"] = {"values": [1]}
    event = JobEvent.from_json(payload)

    with pytest.raises((AttributeError, TypeError)):
        event.payload["new"] = True
    with pytest.raises((AttributeError, TypeError)):
        event.payload["nested"]["values"].append(2)  # type: ignore[union-attr]

    encoded = event.to_json()
    assert type(encoded["payload"]) is dict
    assert type(encoded["payload"]["nested"]["values"]) is list  # type: ignore[index]
    encoded["payload"]["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    assert event.payload["nested"]["values"] == [1]  # type: ignore[index]


def test_direct_audit_payload_is_deeply_immutable_and_to_json_is_detached() -> None:
    source = audit_json()["payload"]
    audit = AuditRecord(event_id="event-1", event_type="service.reconciled", request_id=None, payload=source)

    source["command_summary"]["name"] = "changed"
    with pytest.raises((AttributeError, TypeError)):
        audit.payload["command_summary"]["name"] = "changed"  # type: ignore[index]

    encoded = audit.to_json()
    encoded["payload"]["command_summary"]["name"] = "changed"  # type: ignore[index]
    assert audit.payload["command_summary"]["name"] == "service.advance"  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requester", object()),
        ("job_kind", object()),
        ("prior_state", object()),
        ("result_state", object()),
        ("approval_identity", RequesterIdentity("LOCAL_PEER", 101, 1000, 1000)),
    ],
)
def test_direct_audit_payload_rejects_invalid_typed_authority_fields(field: str, value: object) -> None:
    values = {
        "requester": RequesterIdentity("LOCAL_PEER", 101, 1000, 1000),
        "job_id": "job-1",
        "command_name": "service.advance",
        "event_type": "job.preparing_image",
        "job_kind": JobKind.OFFLINE_WORKFLOW,
        "prior_state": JobState.QUEUED,
        "result_state": JobState.PREPARING_IMAGE,
        "approval_identity": None,
        "approved_at": None,
        "error_code": None,
        "blocker_code": None,
        "service_version": "0.2.0",
    }
    values[field] = value
    with pytest.raises(DrawingMachineError):
        AuditPayloadV1(**values)  # type: ignore[arg-type]


def test_direct_audit_payload_rejects_invalid_approval_identity_type() -> None:
    with pytest.raises(DrawingMachineError):
        AuditPayloadV1(
            requester=RequesterIdentity("LOCAL_PEER", 101, 1000, 1000),
            job_id="job-1",
            command_name="workflow.run.review",
            event_type="job.review_accepted",
            job_kind=JobKind.OFFLINE_WORKFLOW,
            prior_state=JobState.BLOCKED,
            result_state=JobState.IMAGE_READY,
            approval_identity=object(),  # type: ignore[arg-type]
            approved_at=datetime(2026, 7, 10, 12, tzinfo=UTC),
            error_code=None,
            blocker_code=None,
            service_version="0.2.0",
        )


@pytest.mark.parametrize(("field", "value"), [("prior_state", object()), ("result_state", object())])
def test_direct_job_event_rejects_invalid_state_types(field: str, value: object) -> None:
    values = {
        "event_id": "event-1",
        "job_id": "job-1",
        "job_revision": 1,
        "event_type": "job.preparing_image",
        "prior_state": JobState.QUEUED,
        "result_state": JobState.PREPARING_IMAGE,
        "request_id": "req-1",
        "requester_type": "LOCAL_PEER",
        "payload": {"requester": RequesterIdentity("LOCAL_PEER", 101, 1000, 1000).to_json()},
        "created_at": datetime(2026, 7, 10, 12, tzinfo=UTC),
    }
    values[field] = value
    with pytest.raises(DrawingMachineError):
        JobEvent(**values)  # type: ignore[arg-type]
