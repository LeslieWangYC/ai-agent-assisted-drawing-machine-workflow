from dataclasses import dataclass
from datetime import datetime
from typing import Self

from drawingmachine.domain.jobs.models import (
    RequesterIdentity,
    _freeze_json_object,
    _object,
    _parse_datetime,
    _parse_enum,
    _plain_json_object,
    _raise_invalid,
    _require_exact_fields,
    _validate_datetime,
    _validate_int,
    _validate_nullable_string,
    _validate_requester_type,
    _validate_schema_version,
    _validate_string,
)
from drawingmachine.domain.jobs.states import JobKind, JobState
from drawingmachine.json_types import JsonObject

_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "job_id",
        "job_revision",
        "event_type",
        "prior_state",
        "result_state",
        "request_id",
        "requester_type",
        "payload",
        "created_at",
    }
)
_AUDIT_FIELDS = frozenset({"event_id", "event_type", "request_id", "payload"})
_AUDIT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "requester",
        "job_id",
        "command_summary",
        "prior_state",
        "result_state",
        "approval",
        "error_code",
        "blocker_code",
        "service_version",
        "protocol_version",
    }
)
_COMMAND_SUMMARY_FIELDS = frozenset({"name", "event_type", "job_kind"})
_APPROVAL_FIELDS = frozenset({"identity", "approved_at"})


def _payload_requester(payload: JsonObject, context: str) -> RequesterIdentity:
    if "requester" not in payload:
        _raise_invalid(f"{context} must contain a requester identity")
    return RequesterIdentity.from_json(payload["requester"])


@dataclass(frozen=True, slots=True)
class AuditPayloadV1:
    requester: RequesterIdentity
    job_id: str
    command_name: str
    event_type: str
    job_kind: JobKind
    prior_state: JobState | None
    result_state: JobState
    approval_identity: RequesterIdentity | None
    approved_at: datetime | None
    error_code: str | None
    blocker_code: str | None
    service_version: str
    schema_version: int = 1
    protocol_version: int = 1

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "audit.payload.schema_version")
        if not isinstance(self.requester, RequesterIdentity):
            _raise_invalid("audit.payload.requester must be a requester identity")
        _validate_string(self.job_id, "audit.payload.job_id")
        _validate_string(self.command_name, "audit.payload.command_summary.name")
        _validate_string(self.event_type, "audit.payload.command_summary.event_type")
        if not isinstance(self.job_kind, JobKind):
            _raise_invalid("audit.payload.command_summary.job_kind must be a JobKind")
        if self.prior_state is not None and not isinstance(self.prior_state, JobState):
            _raise_invalid("audit.payload.prior_state must be a JobState or null")
        if not isinstance(self.result_state, JobState):
            _raise_invalid("audit.payload.result_state must be a JobState")
        if (self.approval_identity is None) != (self.approved_at is None):
            _raise_invalid("audit.payload approval identity and time must both be present or null")
        if self.approval_identity is not None:
            if not isinstance(self.approval_identity, RequesterIdentity):
                _raise_invalid("audit.payload.approval.identity must be a requester identity")
            assert self.approved_at is not None
            _validate_datetime(self.approved_at, "audit.payload.approval.approved_at")
        _validate_nullable_string(self.error_code, "audit.payload.error_code")
        _validate_nullable_string(self.blocker_code, "audit.payload.blocker_code")
        _validate_string(self.service_version, "audit.payload.service_version")
        _validate_schema_version(self.protocol_version, "audit.payload.protocol_version")

    def to_json(self) -> JsonObject:
        approval: JsonObject | None = None
        if self.approval_identity is not None:
            assert self.approved_at is not None
            approval = {
                "identity": self.approval_identity.to_json(),
                "approved_at": self.approved_at.isoformat(),
            }
        return {
            "schema_version": self.schema_version,
            "requester": self.requester.to_json(),
            "job_id": self.job_id,
            "command_summary": {
                "name": self.command_name,
                "event_type": self.event_type,
                "job_kind": self.job_kind.value,
            },
            "prior_state": None if self.prior_state is None else self.prior_state.value,
            "result_state": self.result_state.value,
            "approval": approval,
            "error_code": self.error_code,
            "blocker_code": self.blocker_code,
            "service_version": self.service_version,
            "protocol_version": self.protocol_version,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "audit payload")
        _require_exact_fields(data, _AUDIT_PAYLOAD_FIELDS, "audit payload")
        command = _object(data["command_summary"], "audit payload command_summary")
        _require_exact_fields(command, _COMMAND_SUMMARY_FIELDS, "audit payload command_summary")
        approval_value = data["approval"]
        approval_identity: RequesterIdentity | None = None
        approved_at: datetime | None = None
        if approval_value is not None:
            approval = _object(approval_value, "audit payload approval")
            _require_exact_fields(approval, _APPROVAL_FIELDS, "audit payload approval")
            approval_identity = RequesterIdentity.from_json(approval["identity"])
            approved_at = _parse_datetime(approval["approved_at"], "audit.payload.approval.approved_at")
        prior_value = data["prior_state"]
        return cls(
            requester=RequesterIdentity.from_json(data["requester"]),
            job_id=_validate_string(data["job_id"], "audit.payload.job_id"),
            command_name=_validate_string(command["name"], "audit.payload.command_summary.name"),
            event_type=_validate_string(command["event_type"], "audit.payload.command_summary.event_type"),
            job_kind=_parse_enum(JobKind, command["job_kind"], "audit.payload.command_summary.job_kind"),
            prior_state=(
                None if prior_value is None else _parse_enum(JobState, prior_value, "audit.payload.prior_state")
            ),
            result_state=_parse_enum(JobState, data["result_state"], "audit.payload.result_state"),
            approval_identity=approval_identity,
            approved_at=approved_at,
            error_code=_validate_nullable_string(data["error_code"], "audit.payload.error_code"),
            blocker_code=_validate_nullable_string(data["blocker_code"], "audit.payload.blocker_code"),
            service_version=_validate_string(data["service_version"], "audit.payload.service_version"),
            schema_version=_validate_schema_version(data["schema_version"], "audit.payload.schema_version"),
            protocol_version=_validate_schema_version(data["protocol_version"], "audit.payload.protocol_version"),
        )


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_id: str
    job_id: str
    job_revision: int
    event_type: str
    prior_state: JobState | None
    result_state: JobState
    request_id: str | None
    requester_type: str
    payload: JsonObject
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_string(self.event_id, "event.event_id")
        _validate_string(self.job_id, "event.job_id")
        _validate_int(self.job_revision, "event.job_revision", minimum=0)
        _validate_string(self.event_type, "event.event_type")
        if self.prior_state is not None and not isinstance(self.prior_state, JobState):
            _raise_invalid("event.prior_state must be a JobState or None")
        if not isinstance(self.result_state, JobState):
            _raise_invalid("event.result_state must be a JobState")
        _validate_nullable_string(self.request_id, "event.request_id")
        requester_type = _validate_requester_type(self.requester_type, "event.requester_type")
        payload = _freeze_json_object(self.payload, "event.payload")
        object.__setattr__(self, "payload", payload)
        requester = _payload_requester(payload, "event.payload")
        if requester.requester_type != requester_type:
            _raise_invalid("event.requester_type must match payload requester identity")
        _validate_datetime(self.created_at, "event.created_at")

    def to_json(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "event_type": self.event_type,
            "prior_state": None if self.prior_state is None else self.prior_state.value,
            "result_state": self.result_state.value,
            "request_id": self.request_id,
            "requester_type": self.requester_type,
            "payload": _plain_json_object(self.payload),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "job event")
        _require_exact_fields(data, _EVENT_FIELDS, "job event")
        prior_value = data["prior_state"]
        prior_state = None if prior_value is None else _parse_enum(JobState, prior_value, "event.prior_state")
        return cls(
            event_id=_validate_string(data["event_id"], "event.event_id"),
            job_id=_validate_string(data["job_id"], "event.job_id"),
            job_revision=_validate_int(data["job_revision"], "event.job_revision", minimum=0),
            event_type=_validate_string(data["event_type"], "event.event_type"),
            prior_state=prior_state,
            result_state=_parse_enum(JobState, data["result_state"], "event.result_state"),
            request_id=_validate_nullable_string(data["request_id"], "event.request_id"),
            requester_type=_validate_requester_type(data["requester_type"], "event.requester_type"),
            payload=_freeze_json_object(data["payload"], "event.payload"),
            created_at=_parse_datetime(data["created_at"], "event.created_at"),
        )


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event_id: str
    event_type: str
    request_id: str | None
    payload: JsonObject

    def __post_init__(self) -> None:
        _validate_string(self.event_id, "audit.event_id")
        _validate_string(self.event_type, "audit.event_type")
        _validate_nullable_string(self.request_id, "audit.request_id")
        payload = _freeze_json_object(AuditPayloadV1.from_json(self.payload).to_json(), "audit.payload")
        object.__setattr__(self, "payload", payload)
        _payload_requester(payload, "audit.payload")

    def to_json(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "request_id": self.request_id,
            "payload": _plain_json_object(self.payload),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, "audit record")
        _require_exact_fields(data, _AUDIT_FIELDS, "audit record")
        return cls(
            event_id=_validate_string(data["event_id"], "audit.event_id"),
            event_type=_validate_string(data["event_type"], "audit.event_type"),
            request_id=_validate_nullable_string(data["request_id"], "audit.request_id"),
            payload=_freeze_json_object(data["payload"], "audit.payload"),
        )
