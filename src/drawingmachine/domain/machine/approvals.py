from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NoReturn, Self, cast
from uuid import UUID

from drawingmachine.domain.machine.models import (
    SCHEMA_VERSION,
    MachineSessionSnapshot,
    _digest,
    _enum,
    _enum_json,
    _finite,
    _integer,
    _invalid,
    _object,
    _string,
    _timestamp,
)
from drawingmachine.domain.machine.phases import (
    MachineAction,
    MachinePhase,
    RecoveryDisposition,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

_PRINCIPAL_FIELDS = frozenset({"uid", "gid"})
_AUTHENTICATED_PRINCIPAL_FIELDS = frozenset({"uid", "gid", "pid"})
_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "execution_revision",
        "action",
        "recovery_disposition",
        "required_prior_phase",
        "service_epoch",
        "machine_session_epoch",
        "job_id",
        "ready_revision",
        "application_digest",
        "machine_digest",
        "provider_digest",
        "gcode_sha256",
    }
)
_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "machine_session_epoch",
        "controller_state",
        "mpos",
        "wpos",
        "wco",
        "g54",
        "stabilized",
        "observed_at",
    }
)
_EVIDENCE_FIELDS = frozenset({"schema_version", "action", "required_prior_phase", "session"})
_CHALLENGE_FIELDS = frozenset(
    {
        "schema_version",
        "challenge_id",
        "binding",
        "requester",
        "operator_principal",
        "purpose",
        "evidence",
        "issued_at",
        "issued_monotonic",
        "monotonic_deadline",
        "expires_at",
        "status",
        "status_changed_at",
        "consumer",
    }
)

_APPROVAL_PHASE = {
    MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
    MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
    MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
    MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
    MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
    MachineAction.RECOVER: MachinePhase.RECOVERY_REQUIRED,
}


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class ApprovalPurpose(StrEnum):
    HOME = "motor high-voltage connected, physical area clear, approve HOME"
    ZCAL = "physical area clear, approve ZCAL"
    ZCONFIRM = "manual pen-height adjustment complete, physical area clear, approve ZCONFIRM"
    STREAM = "drawing setup verified, physical area clear, approve STREAM"
    HOME_Z = "physical area clear, approve HOME_Z"
    RECOVER = "review recovery evidence and approve RECOVER"


_APPROVAL_PURPOSE = {
    MachineAction.HOME: ApprovalPurpose.HOME,
    MachineAction.ZCAL: ApprovalPurpose.ZCAL,
    MachineAction.ZCONFIRM: ApprovalPurpose.ZCONFIRM,
    MachineAction.STREAM: ApprovalPurpose.STREAM,
    MachineAction.HOME_Z: ApprovalPurpose.HOME_Z,
    MachineAction.RECOVER: ApprovalPurpose.RECOVER,
}

# Computing a monotonic deadline as issued_monotonic + ttl can round by up to one
# ulp of the sum, so recovering the duration by subtraction is not bit-exact under
# real clocks. 100ns tolerates that rounding (below ~1.5e-8s for multi-year
# uptimes) while staying far under the 1-microsecond wall-clock resolution.
_DURATION_EQUIVALENCE_TOLERANCE_SECONDS = 1e-7


def _denied(code: str, message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.PERMISSION,
            message=message,
            retryable=False,
            details={},
        )
    )


def _datetime_json(value: object, field: str) -> datetime:
    text = _string(value, field)
    assert isinstance(text, str)
    try:
        parsed = datetime.fromisoformat(text)
    except (OverflowError, ValueError):
        _invalid(f"{field} must be ISO 8601", field=field)
    return _timestamp(parsed, field)


def _monotonic(value: object, field: str) -> float:
    result = _finite(value, field)
    if result < 0.0:
        _invalid(f"{field} must be a non-negative monotonic value", field=field)
    return result


def _validate_approval_duration(
    issued_at: datetime,
    expires_at: datetime,
    issued_monotonic: float,
    monotonic_deadline: float,
) -> None:
    try:
        wall_seconds = (expires_at - issued_at).total_seconds()
        monotonic_seconds = monotonic_deadline - issued_monotonic
    except (OverflowError, ValueError):
        _invalid("approval time arithmetic overflow")
    if not 5.0 <= wall_seconds <= 900.0 or not 5.0 <= monotonic_seconds <= 900.0:
        _invalid("approval duration must be from 5 to 900 seconds")
    if abs(wall_seconds - monotonic_seconds) > _DURATION_EQUIVALENCE_TOLERANCE_SECONDS:
        _invalid("approval wall and monotonic durations must be equivalent")


def _uuid4(value: object, field: str) -> str:
    text = _string(value, field)
    assert isinstance(text, str)
    try:
        parsed = UUID(text)
    except ValueError:
        _invalid(f"{field} must be a canonical UUID4", field=field)
    if parsed.version != 4 or str(parsed) != text:
        _invalid(f"{field} must be a canonical UUID4", field=field)
    return text


def _expected_phase(action: MachineAction) -> MachinePhase:
    try:
        return _APPROVAL_PHASE[action]
    except KeyError:
        _invalid("machine action is not separately approvable", field="approval.action")


def _purpose_for(action: MachineAction) -> ApprovalPurpose:
    try:
        return _APPROVAL_PURPOSE[action]
    except KeyError:
        _invalid("machine action has no fixed approval purpose", field="approval.action")


def _json_position(value: object, field: str) -> tuple[float, float, float]:
    if type(value) is not list or len(value) != 3:
        _invalid(f"{field} must be an exact JSON XYZ array", field=field)
    return (
        _finite(value[0], f"{field}.x"),
        _finite(value[1], f"{field}.y"),
        _finite(value[2], f"{field}.z"),
    )


def _session_to_json(value: MachineSessionSnapshot) -> JsonObject:
    return {
        "schema_version": value.schema_version,
        "execution_id": value.execution_id,
        "machine_session_epoch": value.machine_session_epoch,
        "controller_state": value.controller_state,
        "mpos": list(value.mpos),
        "wpos": list(value.wpos),
        "wco": list(value.wco),
        "g54": list(value.g54),
        "stabilized": value.stabilized,
        "observed_at": value.observed_at.isoformat(),
    }


def _session_from_json(payload: object) -> MachineSessionSnapshot:
    data = _object(payload, _SESSION_FIELDS, "approval session evidence")
    stabilized = data["stabilized"]
    if type(stabilized) is not bool:
        _invalid("approval session evidence stabilized must be a boolean", field="evidence.session.stabilized")
    return MachineSessionSnapshot(
        schema_version=_integer(data["schema_version"], "evidence.session.schema_version", minimum=1),
        execution_id=cast(str, _string(data["execution_id"], "evidence.session.execution_id")),
        machine_session_epoch=cast(
            str,
            _string(data["machine_session_epoch"], "evidence.session.machine_session_epoch"),
        ),
        controller_state=cast(str, _string(data["controller_state"], "evidence.session.controller_state")),
        mpos=_json_position(data["mpos"], "evidence.session.mpos"),
        wpos=_json_position(data["wpos"], "evidence.session.wpos"),
        wco=_json_position(data["wco"], "evidence.session.wco"),
        g54=_json_position(data["g54"], "evidence.session.g54"),
        stabilized=stabilized,
        observed_at=_datetime_json(data["observed_at"], "evidence.session.observed_at"),
    )


@dataclass(frozen=True, slots=True)
class KernelMachinePrincipal:
    uid: int
    gid: int

    def __post_init__(self) -> None:
        _integer(self.uid, "principal.uid")
        _integer(self.gid, "principal.gid")

    def to_json(self) -> JsonObject:
        return {"uid": self.uid, "gid": self.gid}

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _PRINCIPAL_FIELDS, "machine principal")
        return cls(
            uid=_integer(data["uid"], "principal.uid"),
            gid=_integer(data["gid"], "principal.gid"),
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedMachinePrincipal:
    uid: int
    gid: int
    pid: int

    def __post_init__(self) -> None:
        _integer(self.uid, "authenticated_principal.uid")
        _integer(self.gid, "authenticated_principal.gid")
        _integer(self.pid, "authenticated_principal.pid", minimum=1)

    @property
    def stable_principal(self) -> KernelMachinePrincipal:
        return KernelMachinePrincipal(self.uid, self.gid)

    def to_json(self) -> JsonObject:
        return {"uid": self.uid, "gid": self.gid, "pid": self.pid}

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _AUTHENTICATED_PRINCIPAL_FIELDS, "authenticated machine principal")
        return cls(
            uid=_integer(data["uid"], "authenticated_principal.uid"),
            gid=_integer(data["gid"], "authenticated_principal.gid"),
            pid=_integer(data["pid"], "authenticated_principal.pid", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class MachineApprovalBinding:
    execution_id: str
    execution_revision: int
    action: MachineAction
    recovery_disposition: RecoveryDisposition | None
    required_prior_phase: MachinePhase
    service_epoch: str
    machine_session_epoch: str
    job_id: str
    ready_revision: int
    application_digest: str
    machine_digest: str
    provider_digest: str
    gcode_sha256: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "approval_binding.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("approval binding schema version is unsupported", field="approval_binding.schema_version")
        _string(self.execution_id, "approval_binding.execution_id")
        _integer(self.execution_revision, "approval_binding.execution_revision")
        _enum(self.action, MachineAction, "approval_binding.action")
        expected_phase = _expected_phase(self.action)
        _enum(self.required_prior_phase, MachinePhase, "approval_binding.required_prior_phase")
        if self.required_prior_phase is not expected_phase:
            _invalid(
                "approval binding has the wrong required prior phase", field="approval_binding.required_prior_phase"
            )
        if self.action is MachineAction.RECOVER:
            if self.recovery_disposition is None:
                _invalid("RECOVER approval requires a disposition", field="approval_binding.recovery_disposition")
            _enum(self.recovery_disposition, RecoveryDisposition, "approval_binding.recovery_disposition")
        elif self.recovery_disposition is not None:
            _invalid("only RECOVER approval may bind a recovery disposition")
        _string(self.service_epoch, "approval_binding.service_epoch")
        _string(self.machine_session_epoch, "approval_binding.machine_session_epoch")
        _string(self.job_id, "approval_binding.job_id")
        _integer(self.ready_revision, "approval_binding.ready_revision", minimum=1)
        _digest(self.application_digest, "approval_binding.application_digest")
        _digest(self.machine_digest, "approval_binding.machine_digest")
        _digest(self.provider_digest, "approval_binding.provider_digest")
        _digest(self.gcode_sha256, "approval_binding.gcode_sha256")

    def to_python(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "execution_revision": self.execution_revision,
            "action": self.action,
            "recovery_disposition": self.recovery_disposition,
            "required_prior_phase": self.required_prior_phase,
            "service_epoch": self.service_epoch,
            "machine_session_epoch": self.machine_session_epoch,
            "job_id": self.job_id,
            "ready_revision": self.ready_revision,
            "application_digest": self.application_digest,
            "machine_digest": self.machine_digest,
            "provider_digest": self.provider_digest,
            "gcode_sha256": self.gcode_sha256,
        }

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "execution_revision": self.execution_revision,
            "action": self.action.value,
            "recovery_disposition": (None if self.recovery_disposition is None else self.recovery_disposition.value),
            "required_prior_phase": self.required_prior_phase.value,
            "service_epoch": self.service_epoch,
            "machine_session_epoch": self.machine_session_epoch,
            "job_id": self.job_id,
            "ready_revision": self.ready_revision,
            "application_digest": self.application_digest,
            "machine_digest": self.machine_digest,
            "provider_digest": self.provider_digest,
            "gcode_sha256": self.gcode_sha256,
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _BINDING_FIELDS, "machine approval binding")
        disposition_value = data["recovery_disposition"]
        disposition = (
            None
            if disposition_value is None
            else _enum_json(disposition_value, RecoveryDisposition, "approval_binding.recovery_disposition")
        )
        return cls(
            schema_version=_integer(data["schema_version"], "approval_binding.schema_version", minimum=1),
            execution_id=cast(str, _string(data["execution_id"], "approval_binding.execution_id")),
            execution_revision=_integer(data["execution_revision"], "approval_binding.execution_revision"),
            action=_enum_json(data["action"], MachineAction, "approval_binding.action"),
            recovery_disposition=disposition,
            required_prior_phase=_enum_json(
                data["required_prior_phase"],
                MachinePhase,
                "approval_binding.required_prior_phase",
            ),
            service_epoch=cast(str, _string(data["service_epoch"], "approval_binding.service_epoch")),
            machine_session_epoch=cast(
                str,
                _string(data["machine_session_epoch"], "approval_binding.machine_session_epoch"),
            ),
            job_id=cast(str, _string(data["job_id"], "approval_binding.job_id")),
            ready_revision=_integer(data["ready_revision"], "approval_binding.ready_revision", minimum=1),
            application_digest=_digest(data["application_digest"], "approval_binding.application_digest"),
            machine_digest=_digest(data["machine_digest"], "approval_binding.machine_digest"),
            provider_digest=_digest(data["provider_digest"], "approval_binding.provider_digest"),
            gcode_sha256=_digest(data["gcode_sha256"], "approval_binding.gcode_sha256"),
        )


@dataclass(frozen=True, slots=True)
class MachineApprovalEvidenceV1:
    schema_version: int
    action: MachineAction
    required_prior_phase: MachinePhase
    session: MachineSessionSnapshot | None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "approval_evidence.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("approval evidence schema version is unsupported", field="approval_evidence.schema_version")
        _enum(self.action, MachineAction, "approval_evidence.action")
        expected_phase = _expected_phase(self.action)
        _enum(self.required_prior_phase, MachinePhase, "approval_evidence.required_prior_phase")
        if self.required_prior_phase is not expected_phase:
            _invalid("approval evidence has the wrong required prior phase")
        if self.action is MachineAction.RECOVER:
            if self.session is not None:
                _invalid("RECOVER approval evidence does not use session evidence")
            return
        if type(self.session) is not MachineSessionSnapshot:
            _invalid("motion approval requires exact typed session evidence")
        if not self.session.stabilized:
            _invalid("approval session evidence must be stabilized")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "action": self.action.value,
            "required_prior_phase": self.required_prior_phase.value,
            "session": None if self.session is None else _session_to_json(self.session),
        }

    @classmethod
    def from_json(cls, payload: object) -> Self:
        data = _object(payload, _EVIDENCE_FIELDS, "machine approval evidence")
        session_value = data["session"]
        return cls(
            schema_version=_integer(data["schema_version"], "approval_evidence.schema_version", minimum=1),
            action=_enum_json(data["action"], MachineAction, "approval_evidence.action"),
            required_prior_phase=_enum_json(
                data["required_prior_phase"],
                MachinePhase,
                "approval_evidence.required_prior_phase",
            ),
            session=None if session_value is None else _session_from_json(session_value),
        )


def _validate_evidence_binding(
    evidence: MachineApprovalEvidenceV1,
    binding: MachineApprovalBinding,
) -> None:
    if evidence.action is not binding.action or evidence.required_prior_phase is not binding.required_prior_phase:
        _invalid("approval evidence does not match its authority binding")
    if evidence.session is not None and (
        evidence.session.execution_id != binding.execution_id
        or evidence.session.machine_session_epoch != binding.machine_session_epoch
    ):
        _invalid("approval session evidence does not match execution/session binding")


@dataclass(frozen=True, slots=True)
class MachineApprovalChallenge:
    schema_version: int
    challenge_id: str
    binding: MachineApprovalBinding
    requester: AuthenticatedMachinePrincipal
    operator_principal: KernelMachinePrincipal
    purpose: ApprovalPurpose
    evidence: MachineApprovalEvidenceV1
    issued_at: datetime
    issued_monotonic: float
    monotonic_deadline: float
    expires_at: datetime
    status: ApprovalStatus
    status_changed_at: datetime | None
    consumer: AuthenticatedMachinePrincipal | None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "approval.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("approval schema version is unsupported", field="approval.schema_version")
        _uuid4(self.challenge_id, "approval.challenge_id")
        if type(self.binding) is not MachineApprovalBinding:
            _invalid("approval.binding must be exact MachineApprovalBinding")
        if type(self.requester) is not AuthenticatedMachinePrincipal:
            _invalid("approval.requester must be exact AuthenticatedMachinePrincipal")
        if type(self.operator_principal) is not KernelMachinePrincipal:
            _invalid("approval.operator_principal must be exact KernelMachinePrincipal")
        _enum(self.purpose, ApprovalPurpose, "approval.purpose")
        if self.purpose is not _purpose_for(self.binding.action):
            _invalid("approval purpose does not match the action")
        if type(self.evidence) is not MachineApprovalEvidenceV1:
            _invalid("approval.evidence must be exact MachineApprovalEvidenceV1")
        _validate_evidence_binding(self.evidence, self.binding)
        issued_at = _timestamp(self.issued_at, "approval.issued_at")
        if self.evidence.session is not None and self.evidence.session.observed_at > issued_at:
            _invalid("approval session evidence cannot be observed after challenge issue time")
        issued_monotonic = _monotonic(self.issued_monotonic, "approval.issued_monotonic")
        monotonic_deadline = _monotonic(self.monotonic_deadline, "approval.monotonic_deadline")
        object.__setattr__(self, "issued_monotonic", issued_monotonic)
        object.__setattr__(self, "monotonic_deadline", monotonic_deadline)
        expires_at = _timestamp(self.expires_at, "approval.expires_at")
        _validate_approval_duration(issued_at, expires_at, issued_monotonic, monotonic_deadline)
        _enum(self.status, ApprovalStatus, "approval.status")
        if self.status_changed_at is not None:
            changed_at = _timestamp(self.status_changed_at, "approval.status_changed_at")
            if changed_at < issued_at:
                _invalid("approval status change cannot precede issue time")
        if self.consumer is not None and type(self.consumer) is not AuthenticatedMachinePrincipal:
            _invalid("approval.consumer must be exact AuthenticatedMachinePrincipal")
        if self.status is ApprovalStatus.PENDING:
            if self.status_changed_at is not None or self.consumer is not None:
                _invalid("pending approval cannot carry decision metadata")
        elif self.status is ApprovalStatus.CONSUMED:
            if self.status_changed_at is None or self.consumer is None:
                _invalid("consumed approval requires consumer and decision time")
            if self.consumer.stable_principal != self.operator_principal:
                _invalid("consumed approval principal must be the configured operator")
            if self.status_changed_at >= expires_at:
                _invalid("consumed approval decision must precede wall expiry")
        else:
            if self.status_changed_at is None:
                _invalid("non-pending approval requires a decision time")
            if self.consumer is not None:
                _invalid("non-consumed approval cannot carry a consumer")
            if self.status is ApprovalStatus.SUPERSEDED and self.status_changed_at >= expires_at:
                _invalid("SUPERSEDED decision must strictly precede wall expiry")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "challenge_id": self.challenge_id,
            "binding": self.binding.to_json(),
            "requester": self.requester.to_json(),
            "operator_principal": self.operator_principal.to_json(),
            "purpose": self.purpose.value,
            "evidence": self.evidence.to_json(),
            "issued_at": self.issued_at.isoformat(),
            "issued_monotonic": self.issued_monotonic,
            "monotonic_deadline": self.monotonic_deadline,
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "status_changed_at": None if self.status_changed_at is None else self.status_changed_at.isoformat(),
            "consumer": None if self.consumer is None else self.consumer.to_json(),
        }

    @classmethod
    def from_json(
        cls,
        payload: object,
        *,
        automation_principal: KernelMachinePrincipal,
        operator_principal: KernelMachinePrincipal,
        trusted_requester: AuthenticatedMachinePrincipal,
        trusted_consumer: AuthenticatedMachinePrincipal | None,
    ) -> Self:
        if type(automation_principal) is not KernelMachinePrincipal:
            _invalid("trusted automation principal must be exact KernelMachinePrincipal")
        if type(operator_principal) is not KernelMachinePrincipal:
            _invalid("trusted operator principal must be exact KernelMachinePrincipal")
        if type(trusted_requester) is not AuthenticatedMachinePrincipal:
            _invalid("trusted requester must be exact AuthenticatedMachinePrincipal")
        if trusted_consumer is not None and type(trusted_consumer) is not AuthenticatedMachinePrincipal:
            _invalid("trusted consumer must be exact AuthenticatedMachinePrincipal")
        if automation_principal.uid == operator_principal.uid or automation_principal.gid == operator_principal.gid:
            _denied(
                "MACHINE_APPROVAL_ROLES_NOT_DISJOINT",
                "trusted automation and operator UID/GID roles must be disjoint",
            )
        if trusted_requester.stable_principal not in {automation_principal, operator_principal}:
            _denied(
                "MACHINE_APPROVAL_TRUSTED_REQUESTER_DENIED",
                "trusted authenticated requester is not a configured principal",
            )
        if trusted_consumer is not None and trusted_consumer.stable_principal != operator_principal:
            _denied(
                "MACHINE_APPROVAL_TRUSTED_CONSUMER_DENIED",
                "trusted authenticated consumer is not the configured operator",
            )
        data = _object(payload, _CHALLENGE_FIELDS, "machine approval challenge")
        changed_value = data["status_changed_at"]
        consumer_value = data["consumer"]
        result = cls(
            schema_version=_integer(data["schema_version"], "approval.schema_version", minimum=1),
            challenge_id=_uuid4(data["challenge_id"], "approval.challenge_id"),
            binding=MachineApprovalBinding.from_json(data["binding"]),
            requester=AuthenticatedMachinePrincipal.from_json(data["requester"]),
            operator_principal=KernelMachinePrincipal.from_json(data["operator_principal"]),
            purpose=_enum_json(data["purpose"], ApprovalPurpose, "approval.purpose"),
            evidence=MachineApprovalEvidenceV1.from_json(data["evidence"]),
            issued_at=_datetime_json(data["issued_at"], "approval.issued_at"),
            issued_monotonic=_monotonic(data["issued_monotonic"], "approval.issued_monotonic"),
            monotonic_deadline=_monotonic(data["monotonic_deadline"], "approval.monotonic_deadline"),
            expires_at=_datetime_json(data["expires_at"], "approval.expires_at"),
            status=_enum_json(data["status"], ApprovalStatus, "approval.status"),
            status_changed_at=(
                None if changed_value is None else _datetime_json(changed_value, "approval.status_changed_at")
            ),
            consumer=(None if consumer_value is None else AuthenticatedMachinePrincipal.from_json(consumer_value)),
        )
        if result.operator_principal != operator_principal:
            _denied(
                "MACHINE_APPROVAL_OPERATOR_SPOOFED",
                "JSON operator identity does not match the trusted configured operator",
            )
        if result.requester != trusted_requester:
            _denied(
                "MACHINE_APPROVAL_REQUESTER_SPOOFED",
                "JSON requester identity does not match the trusted authenticated requester",
            )
        if result.consumer != trusted_consumer:
            _denied(
                "MACHINE_APPROVAL_CONSUMER_SPOOFED",
                "JSON consumer identity does not match the trusted authenticated consumer",
            )
        return result


@dataclass(frozen=True, slots=True)
class MachineApprovalDecision:
    expected: MachineApprovalChallenge
    result: MachineApprovalChallenge

    def __post_init__(self) -> None:
        if type(self.expected) is not MachineApprovalChallenge or type(self.result) is not MachineApprovalChallenge:
            _invalid("approval decision requires exact challenge values")
        if self.expected.status is not ApprovalStatus.PENDING:
            _invalid("approval decision expected state must be pending")
        if self.result.status is ApprovalStatus.PENDING:
            _invalid("approval decision result must be terminal")
        expected_authority = replace(
            self.result,
            status=ApprovalStatus.PENDING,
            status_changed_at=None,
            consumer=None,
        )
        if expected_authority != self.expected:
            _invalid("approval decision cannot mutate challenge authority")


def _exact_challenge(value: object) -> MachineApprovalChallenge:
    if type(value) is not MachineApprovalChallenge:
        _invalid("approval decision requires an exact challenge")
    return value


def _current_times(
    challenge: MachineApprovalChallenge,
    wall_now: object,
    monotonic_now: object,
) -> tuple[datetime, float, bool]:
    wall = _timestamp(wall_now, "approval.wall_now")
    monotonic = _monotonic(monotonic_now, "approval.monotonic_now")
    if wall < challenge.issued_at or monotonic < challenge.issued_monotonic:
        _denied("MACHINE_APPROVAL_FUTURE", "approval issue time is in the future")
    expired = wall >= challenge.expires_at or monotonic >= challenge.monotonic_deadline
    return wall, monotonic, expired


def _pending(value: object) -> MachineApprovalChallenge:
    challenge = _exact_challenge(value)
    if challenge.status is not ApprovalStatus.PENDING:
        _denied("MACHINE_APPROVAL_REPLAY", "approval challenge must be pending")
    return challenge


def issue_approval(
    *,
    challenge_id: str,
    binding: MachineApprovalBinding,
    requester: AuthenticatedMachinePrincipal,
    automation_principal: KernelMachinePrincipal,
    operator_principal: KernelMachinePrincipal,
    evidence: MachineApprovalEvidenceV1,
    issued_at: datetime,
    monotonic_now: float,
    ttl_seconds: int,
) -> MachineApprovalChallenge:
    if type(binding) is not MachineApprovalBinding:
        _invalid("approval binding must be exact MachineApprovalBinding")
    if type(requester) is not AuthenticatedMachinePrincipal:
        _invalid("approval requester must be exact AuthenticatedMachinePrincipal")
    if type(automation_principal) is not KernelMachinePrincipal:
        _invalid("automation principal must be exact KernelMachinePrincipal")
    if type(operator_principal) is not KernelMachinePrincipal:
        _invalid("operator principal must be exact KernelMachinePrincipal")
    if automation_principal.uid == operator_principal.uid or automation_principal.gid == operator_principal.gid:
        _denied(
            "MACHINE_APPROVAL_ROLES_NOT_DISJOINT",
            "automation and operator UID/GID roles must be disjoint",
        )
    if requester.stable_principal not in {automation_principal, operator_principal}:
        _denied(
            "MACHINE_APPROVAL_REQUESTER_DENIED",
            "approval requester is not configured automation or operator",
        )
    if type(evidence) is not MachineApprovalEvidenceV1:
        _invalid("approval evidence must be exact MachineApprovalEvidenceV1")
    _validate_evidence_binding(evidence, binding)
    issue_time = _timestamp(issued_at, "approval.issued_at")
    monotonic = _monotonic(monotonic_now, "approval.monotonic_now")
    if type(ttl_seconds) is not int or not 5 <= ttl_seconds <= 900:
        _invalid("approval TTL must be an integer from 5 to 900", field="approval.ttl_seconds")
    try:
        monotonic_deadline = monotonic + ttl_seconds
        expires_at = issue_time + timedelta(seconds=ttl_seconds)
    except (OverflowError, ValueError):
        _invalid("approval time arithmetic overflow")
    return MachineApprovalChallenge(
        schema_version=SCHEMA_VERSION,
        challenge_id=_uuid4(challenge_id, "approval.challenge_id"),
        binding=binding,
        requester=requester,
        operator_principal=operator_principal,
        purpose=_purpose_for(binding.action),
        evidence=evidence,
        issued_at=issue_time,
        issued_monotonic=monotonic,
        monotonic_deadline=monotonic_deadline,
        expires_at=expires_at,
        status=ApprovalStatus.PENDING,
        status_changed_at=None,
        consumer=None,
    )


def consume_approval(
    value: MachineApprovalChallenge,
    *,
    current_binding: MachineApprovalBinding,
    consumer: AuthenticatedMachinePrincipal,
    operator_principal: KernelMachinePrincipal,
    wall_now: datetime,
    monotonic_now: float,
) -> MachineApprovalDecision:
    challenge = _pending(value)
    if type(current_binding) is not MachineApprovalBinding or current_binding != challenge.binding:
        _denied("MACHINE_APPROVAL_BINDING_MISMATCH", "approval current binding does not match challenge binding")
    if type(operator_principal) is not KernelMachinePrincipal or operator_principal != challenge.operator_principal:
        _denied("MACHINE_APPROVAL_OPERATOR_CHANGED", "approval configured operator binding changed")
    if type(consumer) is not AuthenticatedMachinePrincipal or consumer.stable_principal != operator_principal:
        _denied("MACHINE_APPROVAL_CONSUMER_DENIED", "only the configured operator may consume approval")
    wall, _, expired = _current_times(challenge, wall_now, monotonic_now)
    if expired:
        _denied("MACHINE_APPROVAL_EXPIRED", "approval challenge is expired")
    result = replace(
        challenge,
        status=ApprovalStatus.CONSUMED,
        status_changed_at=wall,
        consumer=consumer,
    )
    return MachineApprovalDecision(expected=challenge, result=result)


def supersede_approval(
    value: MachineApprovalChallenge,
    *,
    wall_now: datetime,
    monotonic_now: float,
) -> MachineApprovalDecision:
    challenge = _pending(value)
    wall, _, expired = _current_times(challenge, wall_now, monotonic_now)
    if expired:
        _denied("MACHINE_APPROVAL_EXPIRED", "expired approval must be expired, not superseded")
    result = replace(
        challenge,
        status=ApprovalStatus.SUPERSEDED,
        status_changed_at=wall,
    )
    return MachineApprovalDecision(expected=challenge, result=result)


def expire_approval(
    value: MachineApprovalChallenge,
    *,
    wall_now: datetime,
    monotonic_now: float,
) -> MachineApprovalDecision:
    challenge = _pending(value)
    wall, _, expired = _current_times(challenge, wall_now, monotonic_now)
    if not expired:
        _denied("MACHINE_APPROVAL_NOT_EXPIRED", "approval challenge is not expired")
    result = replace(
        challenge,
        status=ApprovalStatus.EXPIRED,
        status_changed_at=wall,
    )
    return MachineApprovalDecision(expected=challenge, result=result)


def apply_approval_decision(
    current: MachineApprovalChallenge,
    decision: MachineApprovalDecision,
) -> MachineApprovalChallenge:
    challenge = _exact_challenge(current)
    if type(decision) is not MachineApprovalDecision:
        _invalid("approval apply requires an exact decision")
    if challenge.challenge_id != decision.expected.challenge_id:
        _denied("MACHINE_APPROVAL_WRONG_CHALLENGE", "approval decision is for another challenge")
    if challenge != decision.expected:
        _denied("MACHINE_APPROVAL_STALE_DECISION", "approval decision is stale")
    return decision.result
