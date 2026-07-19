from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Self, cast

from drawingmachine.domain.machine.models import (
    SCHEMA_VERSION,
    _digest,
    _enum,
    _enum_json,
    _integer,
    _invalid,
    _object,
    _string,
    _timestamp,
)
from drawingmachine.domain.machine.phases import MachineAction, MachinePhase, RecoveryDisposition
from drawingmachine.json_types import JsonObject

_EVENT_PAYLOAD_FIELDS = frozenset({"schema_version", "request_id", "prior_phase", "reason_code"})
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "execution_id",
        "job_id",
        "execution_revision",
        "event_type",
        "phase",
        "action",
        "payload",
        "occurred_at",
    }
)
_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "decision",
        "execution_id",
        "job_id",
        "execution_revision",
        "request_id",
        "reason_code",
        "event_id",
    }
)
_EVENT_PAYLOAD_V2_FIELDS = frozenset({"schema_version", "request_id", "prior_phase", "result_phase", "reason_code"})
_AUDIT_V2_FIELDS = frozenset(
    {
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
)


def _datetime_json(value: object, field: str) -> datetime:
    text = _string(value, field)
    assert isinstance(text, str)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _invalid(f"{field} must be ISO 8601", field=field)
    return _timestamp(parsed, field)


@dataclass(frozen=True, slots=True)
class MachineEventPayloadV1:
    schema_version: int
    request_id: str | None
    prior_phase: MachinePhase | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "event_payload.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("event payload schema version is unsupported")
        _string(self.request_id, "event_payload.request_id", nullable=True)
        if self.prior_phase is not None:
            _enum(self.prior_phase, MachinePhase, "event_payload.prior_phase")
        _string(self.reason_code, "event_payload.reason_code", nullable=True)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "prior_phase": None if self.prior_phase is None else self.prior_phase.value,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _EVENT_PAYLOAD_FIELDS, "machine event payload")
        prior_phase_value = data["prior_phase"]
        prior_phase = (
            None
            if prior_phase_value is None
            else _enum_json(prior_phase_value, MachinePhase, "event_payload.prior_phase")
        )
        return cls(
            schema_version=_integer(data["schema_version"], "event_payload.schema_version", minimum=1),
            request_id=_string(data["request_id"], "event_payload.request_id", nullable=True),
            prior_phase=prior_phase,
            reason_code=_string(data["reason_code"], "event_payload.reason_code", nullable=True),
        )


@dataclass(frozen=True, slots=True)
class MachineEventPayloadV2:
    request_id: str
    prior_phase: MachinePhase | None
    result_phase: MachinePhase
    reason_code: str | None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "event_payload.schema_version", minimum=1) != 2:
            _invalid("event payload v2 schema version is unsupported")
        _string(self.request_id, "event_payload.request_id")
        if self.prior_phase is not None:
            _enum(self.prior_phase, MachinePhase, "event_payload.prior_phase")
        _enum(self.result_phase, MachinePhase, "event_payload.result_phase")
        _string(self.reason_code, "event_payload.reason_code", nullable=True)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "prior_phase": None if self.prior_phase is None else self.prior_phase.value,
            "result_phase": self.result_phase.value,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _EVENT_PAYLOAD_V2_FIELDS, "machine event payload v2")
        prior = data["prior_phase"]
        return cls(
            schema_version=_integer(data["schema_version"], "event_payload.schema_version", minimum=1),
            request_id=cast(str, _string(data["request_id"], "event_payload.request_id")),
            prior_phase=None if prior is None else _enum_json(prior, MachinePhase, "event_payload.prior_phase"),
            result_phase=_enum_json(data["result_phase"], MachinePhase, "event_payload.result_phase"),
            reason_code=_string(data["reason_code"], "event_payload.reason_code", nullable=True),
        )


@dataclass(frozen=True, slots=True)
class MachineAuditPayloadV2:
    operation: str
    decision: str
    execution_id: str
    job_id: str
    execution_revision: int
    frozen_ready_revision: int
    request_id: str
    event_id: str
    requester_uid: int
    requester_gid: int
    requester_pid: int
    issuer_pid: int | None
    consumer_pid: int | None
    prior_phase: MachinePhase | None
    result_phase: MachinePhase
    action: MachineAction
    recovery_disposition: RecoveryDisposition | None
    approval_challenge_id: str | None
    challenge_issued_at: datetime | None
    approved_at: datetime | None
    application_digest: str
    machine_digest: str
    provider_digest: str
    gcode_sha256: str
    service_epoch: str
    machine_session_epoch: str
    application_version: str
    protocol_version: int
    command_summary: str
    error_code: str | None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "audit.schema_version", minimum=1) != 2:
            _invalid("machine audit v2 schema version is unsupported")
        for field in (
            "operation",
            "decision",
            "execution_id",
            "job_id",
            "request_id",
            "event_id",
            "application_digest",
            "machine_digest",
            "provider_digest",
            "gcode_sha256",
            "service_epoch",
            "machine_session_epoch",
            "application_version",
            "command_summary",
        ):
            _string(getattr(self, field), f"audit.{field}")
        for field in ("application_digest", "machine_digest", "provider_digest", "gcode_sha256"):
            _digest(getattr(self, field), f"audit.{field}")
        for field in ("operation", "decision", "command_summary", "error_code"):
            value = getattr(self, field)
            if value is not None and len(value.encode("utf-8")) > 4096:
                _invalid(f"audit.{field} exceeds its bounded UTF-8 limit")
        _integer(self.execution_revision, "audit.execution_revision")
        _integer(self.frozen_ready_revision, "audit.frozen_ready_revision", minimum=1)
        _integer(self.requester_uid, "audit.requester_uid")
        _integer(self.requester_gid, "audit.requester_gid")
        _integer(self.requester_pid, "audit.requester_pid", minimum=1)
        for field in ("issuer_pid", "consumer_pid"):
            value = getattr(self, field)
            if value is not None:
                _integer(value, f"audit.{field}", minimum=1)
        if self.prior_phase is not None:
            _enum(self.prior_phase, MachinePhase, "audit.prior_phase")
        _enum(self.result_phase, MachinePhase, "audit.result_phase")
        _enum(self.action, MachineAction, "audit.action")
        if self.recovery_disposition is not None:
            _enum(self.recovery_disposition, RecoveryDisposition, "audit.recovery_disposition")
        if (self.action is MachineAction.RECOVER) != (self.recovery_disposition is not None):
            _invalid("only RECOVER audit authority binds one recovery disposition")
        _string(self.approval_challenge_id, "audit.approval_challenge_id", nullable=True)
        if self.challenge_issued_at is not None:
            _timestamp(self.challenge_issued_at, "audit.challenge_issued_at")
        if self.approved_at is not None:
            _timestamp(self.approved_at, "audit.approved_at")
        if (self.approval_challenge_id is None) != (self.challenge_issued_at is None):
            _invalid("audit challenge identity and issuance time must be atomic")
        if self.consumer_pid is not None and (self.approval_challenge_id is None or self.approved_at is None):
            _invalid("audit consumer PID requires consumed operator approval evidence")
        if self.consumer_pid is None and self.approved_at is not None:
            _invalid("audit approval decision time requires a consumer PID")
        if (
            self.challenge_issued_at is not None
            and self.approved_at is not None
            and self.approved_at < self.challenge_issued_at
        ):
            _invalid("audit approval decision cannot precede challenge issuance")
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            _invalid("audit protocol_version must be exactly 1")
        _string(self.error_code, "audit.error_code", nullable=True)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "decision": self.decision,
            "execution_id": self.execution_id,
            "job_id": self.job_id,
            "execution_revision": self.execution_revision,
            "frozen_ready_revision": self.frozen_ready_revision,
            "request_id": self.request_id,
            "event_id": self.event_id,
            "requester_uid": self.requester_uid,
            "requester_gid": self.requester_gid,
            "requester_pid": self.requester_pid,
            "issuer_pid": self.issuer_pid,
            "consumer_pid": self.consumer_pid,
            "prior_phase": None if self.prior_phase is None else self.prior_phase.value,
            "result_phase": self.result_phase.value,
            "action": self.action.value,
            "recovery_disposition": (None if self.recovery_disposition is None else self.recovery_disposition.value),
            "approval_challenge_id": self.approval_challenge_id,
            "challenge_issued_at": (None if self.challenge_issued_at is None else self.challenge_issued_at.isoformat()),
            "approved_at": None if self.approved_at is None else self.approved_at.isoformat(),
            "application_digest": self.application_digest,
            "machine_digest": self.machine_digest,
            "provider_digest": self.provider_digest,
            "gcode_sha256": self.gcode_sha256,
            "service_epoch": self.service_epoch,
            "machine_session_epoch": self.machine_session_epoch,
            "application_version": self.application_version,
            "protocol_version": self.protocol_version,
            "command_summary": self.command_summary,
            "error_code": self.error_code,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _AUDIT_V2_FIELDS, "machine audit v2")
        prior = data["prior_phase"]
        disposition = data["recovery_disposition"]
        challenge_issued_at = data["challenge_issued_at"]
        approved_at = data["approved_at"]
        return cls(
            schema_version=_integer(data["schema_version"], "audit.schema_version", minimum=1),
            operation=cast(str, _string(data["operation"], "audit.operation")),
            decision=cast(str, _string(data["decision"], "audit.decision")),
            execution_id=cast(str, _string(data["execution_id"], "audit.execution_id")),
            job_id=cast(str, _string(data["job_id"], "audit.job_id")),
            execution_revision=_integer(data["execution_revision"], "audit.execution_revision"),
            frozen_ready_revision=_integer(
                data["frozen_ready_revision"],
                "audit.frozen_ready_revision",
                minimum=1,
            ),
            request_id=cast(str, _string(data["request_id"], "audit.request_id")),
            event_id=cast(str, _string(data["event_id"], "audit.event_id")),
            requester_uid=_integer(data["requester_uid"], "audit.requester_uid"),
            requester_gid=_integer(data["requester_gid"], "audit.requester_gid"),
            requester_pid=_integer(data["requester_pid"], "audit.requester_pid", minimum=1),
            issuer_pid=None
            if data["issuer_pid"] is None
            else _integer(data["issuer_pid"], "audit.issuer_pid", minimum=1),
            consumer_pid=None
            if data["consumer_pid"] is None
            else _integer(data["consumer_pid"], "audit.consumer_pid", minimum=1),
            prior_phase=None if prior is None else _enum_json(prior, MachinePhase, "audit.prior_phase"),
            result_phase=_enum_json(data["result_phase"], MachinePhase, "audit.result_phase"),
            action=_enum_json(data["action"], MachineAction, "audit.action"),
            recovery_disposition=(
                None
                if disposition is None
                else _enum_json(disposition, RecoveryDisposition, "audit.recovery_disposition")
            ),
            approval_challenge_id=_string(data["approval_challenge_id"], "audit.approval_challenge_id", nullable=True),
            challenge_issued_at=(
                None
                if challenge_issued_at is None
                else _datetime_json(challenge_issued_at, "audit.challenge_issued_at")
            ),
            approved_at=None if approved_at is None else _datetime_json(approved_at, "audit.approved_at"),
            application_digest=cast(str, _string(data["application_digest"], "audit.application_digest")),
            machine_digest=cast(str, _string(data["machine_digest"], "audit.machine_digest")),
            provider_digest=cast(str, _string(data["provider_digest"], "audit.provider_digest")),
            gcode_sha256=cast(str, _string(data["gcode_sha256"], "audit.gcode_sha256")),
            service_epoch=cast(str, _string(data["service_epoch"], "audit.service_epoch")),
            machine_session_epoch=cast(str, _string(data["machine_session_epoch"], "audit.machine_session_epoch")),
            application_version=cast(str, _string(data["application_version"], "audit.application_version")),
            protocol_version=_integer(data["protocol_version"], "audit.protocol_version", minimum=1),
            command_summary=cast(str, _string(data["command_summary"], "audit.command_summary")),
            error_code=_string(data["error_code"], "audit.error_code", nullable=True),
        )


@dataclass(frozen=True, slots=True)
class MachineAuditPayloadV1:
    schema_version: int
    operation: str
    decision: str
    execution_id: str
    job_id: str
    execution_revision: int
    request_id: str | None
    reason_code: str | None
    event_id: str

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "audit.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("audit schema version is unsupported", field="audit.schema_version")
        _string(self.operation, "audit.operation")
        _string(self.decision, "audit.decision")
        _string(self.execution_id, "audit.execution_id")
        _string(self.job_id, "audit.job_id")
        _integer(self.execution_revision, "audit.execution_revision")
        _string(self.request_id, "audit.request_id", nullable=True)
        _string(self.reason_code, "audit.reason_code", nullable=True)
        _string(self.event_id, "audit.event_id")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "decision": self.decision,
            "execution_id": self.execution_id,
            "job_id": self.job_id,
            "execution_revision": self.execution_revision,
            "request_id": self.request_id,
            "reason_code": self.reason_code,
            "event_id": self.event_id,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _AUDIT_FIELDS, "machine audit payload")
        return cls(
            schema_version=_integer(data["schema_version"], "audit.schema_version", minimum=1),
            operation=cast(str, _string(data["operation"], "audit.operation")),
            decision=cast(str, _string(data["decision"], "audit.decision")),
            execution_id=cast(str, _string(data["execution_id"], "audit.execution_id")),
            job_id=cast(str, _string(data["job_id"], "audit.job_id")),
            execution_revision=_integer(data["execution_revision"], "audit.execution_revision"),
            request_id=_string(data["request_id"], "audit.request_id", nullable=True),
            reason_code=_string(data["reason_code"], "audit.reason_code", nullable=True),
            event_id=cast(str, _string(data["event_id"], "audit.event_id")),
        )


@dataclass(frozen=True, slots=True)
class MachineEvent:
    schema_version: int
    event_id: str
    execution_id: str
    job_id: str
    execution_revision: int
    event_type: str
    phase: MachinePhase
    action: MachineAction | None
    payload: MachineEventPayloadV1 | MachineEventPayloadV2
    occurred_at: datetime

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "event.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("event schema version is unsupported", field="event.schema_version")
        _string(self.event_id, "event.event_id")
        _string(self.execution_id, "event.execution_id")
        _string(self.job_id, "event.job_id")
        _integer(self.execution_revision, "event.execution_revision")
        _string(self.event_type, "event.event_type")
        _enum(self.phase, MachinePhase, "event.phase")
        if self.action is not None:
            _enum(self.action, MachineAction, "event.action")
        if type(self.payload) not in {MachineEventPayloadV1, MachineEventPayloadV2}:
            _invalid("event.payload must be exact MachineEventPayloadV1 or MachineEventPayloadV2")
        if type(self.payload) is MachineEventPayloadV2 and self.payload.result_phase is not self.phase:
            _invalid("event payload result phase must match event phase")
        _timestamp(self.occurred_at, "event.occurred_at")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "execution_id": self.execution_id,
            "job_id": self.job_id,
            "execution_revision": self.execution_revision,
            "event_type": self.event_type,
            "phase": self.phase.value,
            "action": None if self.action is None else self.action.value,
            "payload": self.payload.to_json(),
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _EVENT_FIELDS, "machine event")
        action_value = data["action"]
        action = None if action_value is None else _enum_json(action_value, MachineAction, "event.action")
        payload_value = data["payload"]
        if not isinstance(payload_value, dict):
            _invalid("machine event payload must be an object", field="event.payload")
        payload_version = payload_value.get("schema_version")
        event_payload = (
            MachineEventPayloadV2.from_json(payload_value)
            if payload_version == 2
            else MachineEventPayloadV1.from_json(payload_value)
        )
        return cls(
            schema_version=_integer(data["schema_version"], "event.schema_version", minimum=1),
            event_id=cast(str, _string(data["event_id"], "event.event_id")),
            execution_id=cast(str, _string(data["execution_id"], "event.execution_id")),
            job_id=cast(str, _string(data["job_id"], "event.job_id")),
            execution_revision=_integer(data["execution_revision"], "event.execution_revision"),
            event_type=cast(str, _string(data["event_type"], "event.event_type")),
            phase=_enum_json(data["phase"], MachinePhase, "event.phase"),
            action=action,
            payload=event_payload,
            occurred_at=_datetime_json(data["occurred_at"], "event.occurred_at"),
        )
