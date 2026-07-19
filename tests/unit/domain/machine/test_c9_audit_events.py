from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from drawingmachine.domain.machine import (
    MachineAction,
    MachineAuditPayloadV2,
    MachineEvent,
    MachineEventPayloadV1,
    MachineEventPayloadV2,
    MachinePhase,
    RecoveryDisposition,
)
from drawingmachine.errors import DrawingMachineError

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)


def _audit(**changes: Any) -> MachineAuditPayloadV2:
    values: dict[str, Any] = {
        "operation": "machine.recover",
        "decision": "TRANSFERRED",
        "execution_id": "execution-1",
        "job_id": "job-1",
        "execution_revision": 2,
        "frozen_ready_revision": 7,
        "request_id": "request-1",
        "event_id": "event-1",
        "requester_uid": 2200,
        "requester_gid": 2200,
        "requester_pid": 41002,
        "issuer_pid": 41001,
        "consumer_pid": 41002,
        "prior_phase": MachinePhase.RECOVERY_REQUIRED,
        "result_phase": MachinePhase.PREPARING_SESSION,
        "action": MachineAction.RECOVER,
        "recovery_disposition": RecoveryDisposition.RESTART_SEQUENCE,
        "approval_challenge_id": "00000000-0000-4000-8000-000000000001",
        "challenge_issued_at": NOW,
        "approved_at": NOW,
        "application_digest": "a" * 64,
        "machine_digest": "b" * 64,
        "provider_digest": "c" * 64,
        "gcode_sha256": "d" * 64,
        "service_epoch": "service-1",
        "machine_session_epoch": "session-1",
        "application_version": "0.2.0.dev0",
        "protocol_version": 1,
        "command_summary": "one typed recovery transfer",
        "error_code": None,
    }
    values.update(changes)
    return MachineAuditPayloadV2(**values)


def test_v2_event_and_audit_round_trip_preserves_every_closed_authority_field() -> None:
    payload = MachineEventPayloadV2(
        "request-1",
        MachinePhase.RECOVERY_REQUIRED,
        MachinePhase.PREPARING_SESSION,
        None,
    )
    event = MachineEvent(
        1,
        "event-1",
        "execution-1",
        "job-1",
        2,
        "machine.authority",
        MachinePhase.PREPARING_SESSION,
        MachineAction.RECOVER,
        payload,
        NOW,
    )
    assert MachineEvent.from_json(event.to_json()) == event
    audit = _audit()
    assert MachineAuditPayloadV2.from_json(audit.to_json()) == audit
    assert set(audit.to_json()) == {
        "schema_version",
        "operation",
        "decision",
        "execution_id",
        "job_id",
        "execution_revision",
        "frozen_ready_revision",
        "request_id",
        "event_id",
        "requester_uid",
        "requester_gid",
        "requester_pid",
        "issuer_pid",
        "consumer_pid",
        "prior_phase",
        "result_phase",
        "action",
        "recovery_disposition",
        "approval_challenge_id",
        "challenge_issued_at",
        "approved_at",
        "application_digest",
        "machine_digest",
        "provider_digest",
        "gcode_sha256",
        "service_epoch",
        "machine_session_epoch",
        "application_version",
        "protocol_version",
        "command_summary",
        "error_code",
    }


def test_v1_event_payload_remains_readable_without_v2_fields() -> None:
    event = MachineEvent(
        1,
        "legacy-event",
        "execution-1",
        "job-1",
        0,
        "machine.phase",
        MachinePhase.PREPARING_SESSION,
        MachineAction.PREPARE_SESSION,
        MachineEventPayloadV1(1, None, None, None),
        NOW,
    )
    assert MachineEvent.from_json(event.to_json()) == event


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 1},
        {"requester_pid": 0},
        {"frozen_ready_revision": 0},
        {"issuer_pid": 0},
        {"consumer_pid": 0},
        {"prior_phase": "RECOVERY_REQUIRED"},
        {"result_phase": "PREPARING_SESSION"},
        {"action": MachineAction.HOME},
        {"recovery_disposition": None},
        {"approval_challenge_id": None},
        {"challenge_issued_at": None},
        {"approved_at": None},
        {"consumer_pid": None, "approved_at": NOW},
        {"approved_at": NOW - timedelta(seconds=1)},
        {
            "consumer_pid": 41002,
            "approval_challenge_id": None,
            "challenge_issued_at": None,
            "approved_at": None,
        },
        {"application_digest": "A" * 64},
        {"protocol_version": 2},
        {"command_summary": "x" * 4097},
        {"error_code": "x" * 4097},
    ],
)
def test_v2_audit_rejects_malformed_or_inconsistent_authority(changes: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError):
        _audit(**changes)


def test_non_recovery_audit_requires_no_recovery_disposition() -> None:
    audit = _audit(
        operation="machine.prepare",
        action=MachineAction.PREPARE_SESSION,
        recovery_disposition=None,
        issuer_pid=None,
        consumer_pid=None,
        prior_phase=None,
        approval_challenge_id=None,
        challenge_issued_at=None,
        approved_at=None,
    )
    assert MachineAuditPayloadV2.from_json(audit.to_json()) == audit


def test_challenge_issuance_audit_has_issue_time_but_no_operator_approval_time() -> None:
    audit = _audit(consumer_pid=None, approved_at=None)
    assert audit.challenge_issued_at == NOW
    assert audit.approved_at is None
    assert MachineAuditPayloadV2.from_json(audit.to_json()) == audit


def test_event_v2_requires_exact_result_phase_and_plain_versioned_payload() -> None:
    payload = MachineEventPayloadV2("request", None, MachinePhase.AWAITING_HOME_APPROVAL, None)
    with pytest.raises(DrawingMachineError, match="result phase"):
        MachineEvent(
            1,
            "event",
            "execution",
            "job",
            1,
            "machine.authority",
            MachinePhase.PREPARING_SESSION,
            MachineAction.PREPARE_SESSION,
            payload,
            NOW,
        )
    document = MachineEvent(
        1,
        "event",
        "execution",
        "job",
        1,
        "machine.authority",
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.PREPARE_SESSION,
        payload,
        NOW,
    ).to_json()
    document["payload"] = []
    with pytest.raises(DrawingMachineError, match="must be an object"):
        MachineEvent.from_json(document)
    with pytest.raises(DrawingMachineError, match="schema version"):
        replace(payload, schema_version=1)


def test_v2_from_json_rejects_unknown_fields_and_invalid_timestamps() -> None:
    document = _audit().to_json()
    document["unknown"] = True
    with pytest.raises(DrawingMachineError):
        MachineAuditPayloadV2.from_json(document)
    document = _audit().to_json()
    document["approved_at"] = "not-a-time"
    with pytest.raises(DrawingMachineError):
        MachineAuditPayloadV2.from_json(document)
