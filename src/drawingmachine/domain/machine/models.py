from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn, TypeVar, cast

from drawingmachine.domain.jobs.models import ReadyToRunSnapshot
from drawingmachine.domain.jobs.states import JobState
from drawingmachine.domain.machine.phases import MachineAction, MachinePhase, RecoveryIntent
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAILURE_FIELDS = frozenset({"schema_version", "code", "message", "controller_state", "side_effect_possible"})
_RECOVERY_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "execution_id",
        "recovery_intent",
        "stream_milestone",
        "reason_code",
        "failure",
        "occurred_at",
    }
)
_RECOVERY_FIELDS_WITH_MOTION = _RECOVERY_FIELDS_V1 | {"motion"}
_MOTION_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "action",
        "kind",
        "last_step",
        "acknowledged_steps",
        "expected_machine_session_epoch",
        "observed_machine_session_epoch",
        "snapshot",
        "postcondition_proven",
    }
)
_E = TypeVar("_E", bound=StrEnum)


def _invalid(message: str, *, field: str | None = None) -> NoReturn:
    details: JsonObject = {} if field is None else {"field": field}
    raise DrawingMachineError(
        ErrorPayload(
            code="MACHINE_CONTRACT_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details=details,
        )
    )


def _string(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value:
        _invalid(f"{field} must be a non-empty plain string", field=field)
    assert isinstance(value, str)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{field} contains invalid Unicode", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid(f"{field} contains control characters", field=field)
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _invalid(f"{field} must be an integer at least {minimum}", field=field)
    return value


def _finite(value: object, field: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{field} must be numeric", field=field)
    result = float(cast(int | float, value))
    if not math.isfinite(result):
        _invalid(f"{field} must be finite", field=field)
    return result


def _timestamp(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        _invalid(f"{field} must be an exact datetime", field=field)
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field} must be timezone-aware", field=field)
    return value


def _digest(value: object, field: str) -> str:
    result = _string(value, field)
    assert isinstance(result, str)
    if _SHA256.fullmatch(result) is None:
        _invalid(f"{field} must be a lowercase SHA-256 digest", field=field)
    return result


def _enum(value: object, enum_type: type[_E], field: str) -> _E:
    if type(value) is not enum_type:
        _invalid(f"{field} must be a {enum_type.__name__}", field=field)
    return value


def _enum_json(value: object, enum_type: type[_E], field: str) -> _E:
    text = _string(value, field)
    assert isinstance(text, str)
    try:
        return enum_type(text)
    except ValueError:
        _invalid(f"{field} has an unknown value", field=field)


def _object(value: object, fields: frozenset[str], context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{context} must be an object", field=context)
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if type(key) is not str:
            _invalid(f"{context} keys must be plain strings", field=context)
        result[key] = item
    actual = set(result)
    if actual != fields:
        _invalid(f"{context} has unknown or missing fields", field=context)
    return result


def _position(value: object, field: str) -> tuple[float, float, float]:
    if type(value) is not tuple or len(value) != 3:
        _invalid(f"{field} must be an exact XYZ tuple", field=field)
    return (
        _finite(value[0], f"{field}.x"),
        _finite(value[1], f"{field}.y"),
        _finite(value[2], f"{field}.z"),
    )


class StreamMilestone(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    FIRST_WRITE_POSSIBLE = "FIRST_WRITE_POSSIBLE"
    STREAM_CONFIRMED = "STREAM_CONFIRMED"


class MachineOutcomeKind(StrEnum):
    COMPLETED = "COMPLETED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RetirementDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    RELEASED = "RELEASED"
    TRANSFERRED = "TRANSFERRED"


class MachineMotionEvidenceKind(StrEnum):
    PRECONDITION_REJECTED = "PRECONDITION_REJECTED"
    TYPED_FAILURE = "TYPED_FAILURE"
    INVALID_OUTCOME = "INVALID_OUTCOME"
    POSTCONDITION_REJECTED = "POSTCONDITION_REJECTED"
    ADAPTER_UNCERTAIN = "ADAPTER_UNCERTAIN"
    COMMIT_UNCERTAIN = "COMMIT_UNCERTAIN"
    CLOSE_UNCERTAIN = "CLOSE_UNCERTAIN"


class MachineMotionStep(StrEnum):
    UNKNOWN = "UNKNOWN"
    ACTION_ADMITTED = "ACTION_ADMITTED"
    HOME_DISPATCH = "HOME_DISPATCH"
    HOME_ACKNOWLEDGED = "HOME_ACKNOWLEDGED"
    HOME_IDLE = "HOME_IDLE"
    HOME_POSTCONDITION = "HOME_POSTCONDITION"
    ZCAL_METRIC = "ZCAL_METRIC"
    ZCAL_METRIC_ACKNOWLEDGED = "ZCAL_METRIC_ACKNOWLEDGED"
    ZCAL_ABSOLUTE = "ZCAL_ABSOLUTE"
    ZCAL_ABSOLUTE_ACKNOWLEDGED = "ZCAL_ABSOLUTE_ACKNOWLEDGED"
    ZCAL_WORK_COORDINATE = "ZCAL_WORK_COORDINATE"
    ZCAL_WORK_COORDINATE_ACKNOWLEDGED = "ZCAL_WORK_COORDINATE_ACKNOWLEDGED"
    ZCAL_PEN_UP = "ZCAL_PEN_UP"
    ZCAL_PEN_UP_ACKNOWLEDGED = "ZCAL_PEN_UP_ACKNOWLEDGED"
    ZCAL_CENTER = "ZCAL_CENTER"
    ZCAL_CENTER_ACKNOWLEDGED = "ZCAL_CENTER_ACKNOWLEDGED"
    ZCAL_CONTACT = "ZCAL_CONTACT"
    ZCAL_CONTACT_ACKNOWLEDGED = "ZCAL_CONTACT_ACKNOWLEDGED"
    ZCAL_POSTCONDITION = "ZCAL_POSTCONDITION"
    ZCONFIRM_RAISE = "ZCONFIRM_RAISE"
    ZCONFIRM_ACKNOWLEDGED = "ZCONFIRM_ACKNOWLEDGED"
    ZCONFIRM_IDLE = "ZCONFIRM_IDLE"
    ZCONFIRM_POSTCONDITION = "ZCONFIRM_POSTCONDITION"
    SESSION_CLOSE = "SESSION_CLOSE"
    COMMIT_AFTER_SIDE_EFFECT = "COMMIT_AFTER_SIDE_EFFECT"


@dataclass(frozen=True, slots=True)
class MachineFailureEvidenceV1:
    schema_version: int
    code: str
    message: str
    controller_state: str | None
    side_effect_possible: bool

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "failure.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("failure schema version is unsupported", field="failure.schema_version")
        _string(self.code, "failure.code")
        _string(self.message, "failure.message")
        _string(self.controller_state, "failure.controller_state", nullable=True)
        if type(self.side_effect_possible) is not bool:
            _invalid("failure.side_effect_possible must be a boolean")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "code": self.code,
            "message": self.message,
            "controller_state": self.controller_state,
            "side_effect_possible": self.side_effect_possible,
        }

    @classmethod
    def from_json(cls, payload: object) -> MachineFailureEvidenceV1:
        data = _object(payload, _FAILURE_FIELDS, "machine failure evidence")
        controller_state = _string(data["controller_state"], "failure.controller_state", nullable=True)
        side_effect_possible = data["side_effect_possible"]
        if type(side_effect_possible) is not bool:
            _invalid("failure.side_effect_possible must be a boolean")
        return cls(
            schema_version=_integer(data["schema_version"], "failure.schema_version", minimum=1),
            code=cast(str, _string(data["code"], "failure.code")),
            message=cast(str, _string(data["message"], "failure.message")),
            controller_state=controller_state,
            side_effect_possible=side_effect_possible,
        )


@dataclass(frozen=True, slots=True)
class MachineSessionSnapshot:
    schema_version: int
    execution_id: str
    machine_session_epoch: str
    controller_state: str
    mpos: tuple[float, float, float]
    wpos: tuple[float, float, float]
    wco: tuple[float, float, float]
    g54: tuple[float, float, float]
    stabilized: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "session.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("session schema version is unsupported", field="session.schema_version")
        _string(self.execution_id, "session.execution_id")
        _string(self.machine_session_epoch, "session.machine_session_epoch")
        _string(self.controller_state, "session.controller_state")
        object.__setattr__(self, "mpos", _position(self.mpos, "session.mpos"))
        object.__setattr__(self, "wpos", _position(self.wpos, "session.wpos"))
        object.__setattr__(self, "wco", _position(self.wco, "session.wco"))
        object.__setattr__(self, "g54", _position(self.g54, "session.g54"))
        if type(self.stabilized) is not bool:
            _invalid("session.stabilized must be a boolean", field="session.stabilized")
        _timestamp(self.observed_at, "session.observed_at")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "machine_session_epoch": self.machine_session_epoch,
            "controller_state": self.controller_state,
            "mpos": list(self.mpos),
            "wpos": list(self.wpos),
            "wco": list(self.wco),
            "g54": list(self.g54),
            "stabilized": self.stabilized,
            "observed_at": self.observed_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: object) -> MachineSessionSnapshot:
        fields = frozenset(
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
        data = _object(payload, fields, "machine session snapshot")
        observed_text = cast(str, _string(data["observed_at"], "session.observed_at"))
        try:
            observed_at = datetime.fromisoformat(observed_text)
        except ValueError:
            _invalid("session.observed_at must be ISO 8601", field="session.observed_at")
        stabilized = data["stabilized"]
        if type(stabilized) is not bool:
            _invalid("session.stabilized must be a boolean", field="session.stabilized")

        def position(name: str) -> tuple[float, float, float]:
            value = data[name]
            if type(value) is not list or len(value) != 3:
                _invalid(f"session.{name} must be an exact JSON XYZ array", field=f"session.{name}")
            return (
                _finite(value[0], f"session.{name}.x"),
                _finite(value[1], f"session.{name}.y"),
                _finite(value[2], f"session.{name}.z"),
            )

        return cls(
            schema_version=_integer(data["schema_version"], "session.schema_version", minimum=1),
            execution_id=cast(str, _string(data["execution_id"], "session.execution_id")),
            machine_session_epoch=cast(
                str,
                _string(data["machine_session_epoch"], "session.machine_session_epoch"),
            ),
            controller_state=cast(str, _string(data["controller_state"], "session.controller_state")),
            mpos=position("mpos"),
            wpos=position("wpos"),
            wco=position("wco"),
            g54=position("g54"),
            stabilized=stabilized,
            observed_at=_timestamp(observed_at, "session.observed_at"),
        )


@dataclass(frozen=True, slots=True)
class MachineMotionRecoveryEvidenceV1:
    schema_version: int
    execution_id: str
    action: MachineAction
    kind: MachineMotionEvidenceKind
    last_step: MachineMotionStep
    acknowledged_steps: int | None
    expected_machine_session_epoch: str
    observed_machine_session_epoch: str | None
    snapshot: MachineSessionSnapshot | None
    postcondition_proven: bool

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "motion.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("motion evidence schema version is unsupported", field="motion.schema_version")
        _string(self.execution_id, "motion.execution_id")
        if self.action not in {MachineAction.HOME, MachineAction.ZCAL, MachineAction.ZCONFIRM}:
            _invalid("motion evidence action must be HOME, ZCAL, or ZCONFIRM", field="motion.action")
        _enum(self.kind, MachineMotionEvidenceKind, "motion.kind")
        _enum(self.last_step, MachineMotionStep, "motion.last_step")
        maximum = {MachineAction.HOME: 1, MachineAction.ZCAL: 6, MachineAction.ZCONFIRM: 1}[self.action]
        acknowledged = (
            None if self.acknowledged_steps is None else _integer(self.acknowledged_steps, "motion.acknowledged_steps")
        )
        if acknowledged is not None and acknowledged > maximum:
            _invalid("motion acknowledged step count exceeds the closed action sequence")
        _string(self.expected_machine_session_epoch, "motion.expected_machine_session_epoch")
        _string(
            self.observed_machine_session_epoch,
            "motion.observed_machine_session_epoch",
            nullable=True,
        )
        if self.snapshot is not None:
            if type(self.snapshot) is not MachineSessionSnapshot:
                _invalid("motion snapshot must be exact MachineSessionSnapshot")
            if self.snapshot.execution_id != self.execution_id:
                _invalid("motion snapshot must match the motion execution authority")
            if self.snapshot.machine_session_epoch != self.observed_machine_session_epoch:
                _invalid("motion snapshot must match the observed motion session epoch")
        if type(self.postcondition_proven) is not bool:
            _invalid("motion postcondition flag must be a boolean")
        _validate_motion_cross_product(self, maximum)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "action": self.action.value,
            "kind": self.kind.value,
            "last_step": self.last_step.value,
            "acknowledged_steps": self.acknowledged_steps,
            "expected_machine_session_epoch": self.expected_machine_session_epoch,
            "observed_machine_session_epoch": self.observed_machine_session_epoch,
            "snapshot": None if self.snapshot is None else self.snapshot.to_json(),
            "postcondition_proven": self.postcondition_proven,
        }

    @classmethod
    def from_json(cls, payload: object) -> MachineMotionRecoveryEvidenceV1:
        data = _object(payload, _MOTION_EVIDENCE_FIELDS, "machine motion recovery evidence")
        snapshot = data["snapshot"]
        proven = data["postcondition_proven"]
        if type(proven) is not bool:
            _invalid("motion postcondition flag must be a boolean")
        return cls(
            schema_version=_integer(data["schema_version"], "motion.schema_version", minimum=1),
            execution_id=cast(str, _string(data["execution_id"], "motion.execution_id")),
            action=_enum_json(data["action"], MachineAction, "motion.action"),
            kind=_enum_json(data["kind"], MachineMotionEvidenceKind, "motion.kind"),
            last_step=_enum_json(data["last_step"], MachineMotionStep, "motion.last_step"),
            acknowledged_steps=(
                None
                if data["acknowledged_steps"] is None
                else _integer(data["acknowledged_steps"], "motion.acknowledged_steps")
            ),
            expected_machine_session_epoch=cast(
                str,
                _string(data["expected_machine_session_epoch"], "motion.expected_machine_session_epoch"),
            ),
            observed_machine_session_epoch=_string(
                data["observed_machine_session_epoch"],
                "motion.observed_machine_session_epoch",
                nullable=True,
            ),
            snapshot=None if snapshot is None else MachineSessionSnapshot.from_json(snapshot),
            postcondition_proven=proven,
        )


def _validate_motion_cross_product(evidence: MachineMotionRecoveryEvidenceV1, maximum: int) -> None:
    action = evidence.action
    kind = evidence.kind
    acknowledged = evidence.acknowledged_steps
    step = evidence.last_step
    snapshot = evidence.snapshot
    expected_epoch = evidence.expected_machine_session_epoch
    observed_epoch = evidence.observed_machine_session_epoch
    proven = evidence.postcondition_proven
    if kind is MachineMotionEvidenceKind.PRECONDITION_REJECTED:
        valid = acknowledged == 0 and step is MachineMotionStep.ACTION_ADMITTED
        valid = valid and snapshot is None and observed_epoch is None and not proven
    elif kind is MachineMotionEvidenceKind.ADAPTER_UNCERTAIN:
        valid = acknowledged is None and step is MachineMotionStep.UNKNOWN
        valid = valid and snapshot is None and observed_epoch is None and not proven
    elif kind is MachineMotionEvidenceKind.INVALID_OUTCOME:
        valid = acknowledged is None and step is MachineMotionStep.UNKNOWN
        valid = valid and snapshot is None and not proven
    elif kind is MachineMotionEvidenceKind.TYPED_FAILURE:
        valid = acknowledged is not None and observed_epoch == expected_epoch and not proven
        if action is MachineAction.ZCAL:
            attempted_steps = (
                MachineMotionStep.ZCAL_METRIC,
                MachineMotionStep.ZCAL_ABSOLUTE,
                MachineMotionStep.ZCAL_WORK_COORDINATE,
                MachineMotionStep.ZCAL_PEN_UP,
                MachineMotionStep.ZCAL_CENTER,
                MachineMotionStep.ZCAL_CONTACT,
            )
            acknowledged_steps = (
                MachineMotionStep.ZCAL_METRIC_ACKNOWLEDGED,
                MachineMotionStep.ZCAL_ABSOLUTE_ACKNOWLEDGED,
                MachineMotionStep.ZCAL_WORK_COORDINATE_ACKNOWLEDGED,
                MachineMotionStep.ZCAL_PEN_UP_ACKNOWLEDGED,
                MachineMotionStep.ZCAL_CENTER_ACKNOWLEDGED,
                MachineMotionStep.ZCAL_CONTACT_ACKNOWLEDGED,
            )
            zcal_valid = acknowledged is not None and acknowledged < 6 and step is attempted_steps[acknowledged]
            if acknowledged is not None and acknowledged > 0:
                zcal_valid = zcal_valid or step is acknowledged_steps[acknowledged - 1]
            if acknowledged == 6:
                zcal_valid = step in {MachineMotionStep.ZCAL_CONTACT_ACKNOWLEDGED, MachineMotionStep.ZCAL_POSTCONDITION}
            valid = valid and zcal_valid
        elif action is MachineAction.HOME:
            expected_step = (
                MachineMotionStep.HOME_DISPATCH if acknowledged == 0 else MachineMotionStep.HOME_ACKNOWLEDGED
            )
            valid = (
                valid
                and acknowledged in {0, 1}
                and (step is expected_step or (acknowledged == 1 and step is MachineMotionStep.HOME_IDLE))
            )
        else:
            expected_step = (
                MachineMotionStep.ZCONFIRM_RAISE if acknowledged == 0 else MachineMotionStep.ZCONFIRM_ACKNOWLEDGED
            )
            valid = (
                valid
                and acknowledged in {0, 1}
                and (step is expected_step or (acknowledged == 1 and step is MachineMotionStep.ZCONFIRM_IDLE))
            )
    elif kind is MachineMotionEvidenceKind.POSTCONDITION_REJECTED:
        expected_step = {
            MachineAction.HOME: MachineMotionStep.HOME_POSTCONDITION,
            MachineAction.ZCAL: MachineMotionStep.ZCAL_POSTCONDITION,
            MachineAction.ZCONFIRM: MachineMotionStep.ZCONFIRM_POSTCONDITION,
        }[action]
        valid = acknowledged == maximum and step is expected_step
        valid = valid and snapshot is not None and observed_epoch == expected_epoch and not proven
    elif kind is MachineMotionEvidenceKind.COMMIT_UNCERTAIN:
        valid = acknowledged == maximum and step is MachineMotionStep.COMMIT_AFTER_SIDE_EFFECT
        valid = valid and snapshot is not None and observed_epoch == expected_epoch and proven
    else:
        assert kind is MachineMotionEvidenceKind.CLOSE_UNCERTAIN
        valid = action is MachineAction.HOME and acknowledged == 1 and step is MachineMotionStep.SESSION_CLOSE
        valid = valid and snapshot is not None and observed_epoch == expected_epoch and proven
    if proven and (
        snapshot is None
        or snapshot.controller_state != "Idle"
        or not snapshot.stabilized
        or observed_epoch != expected_epoch
    ):
        valid = False
    if not valid:
        _invalid("motion evidence fields do not form an exact closed action state")


@dataclass(frozen=True, slots=True)
class MachineRecoveryEvidence:
    schema_version: int
    execution_id: str
    recovery_intent: RecoveryIntent
    stream_milestone: StreamMilestone
    reason_code: str
    failure: MachineFailureEvidenceV1
    occurred_at: datetime
    motion: MachineMotionRecoveryEvidenceV1 | None = None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "recovery.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("recovery schema version is unsupported", field="recovery.schema_version")
        _string(self.execution_id, "recovery.execution_id")
        _enum(self.recovery_intent, RecoveryIntent, "recovery.recovery_intent")
        _enum(self.stream_milestone, StreamMilestone, "recovery.stream_milestone")
        _string(self.reason_code, "recovery.reason_code")
        if type(self.failure) is not MachineFailureEvidenceV1:
            _invalid("recovery.failure must be exact MachineFailureEvidenceV1")
        _timestamp(self.occurred_at, "recovery.occurred_at")
        if self.motion is not None and type(self.motion) is not MachineMotionRecoveryEvidenceV1:
            _invalid("recovery motion evidence must be exact MachineMotionRecoveryEvidenceV1")
        if self.motion is not None:
            if self.motion.execution_id != self.execution_id:
                _invalid("recovery motion evidence must match the outer execution authority")
            if self.motion.snapshot is not None and self.motion.snapshot.observed_at > self.occurred_at:
                _invalid("motion snapshot must not occur after its recovery evidence")
        expected = recovery_intent_for_milestone(self.stream_milestone)
        if expected is not self.recovery_intent:
            _invalid("recovery intent must match the irreversible stream milestone")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "recovery_intent": self.recovery_intent.value,
            "stream_milestone": self.stream_milestone.value,
            "reason_code": self.reason_code,
            "failure": self.failure.to_json(),
            "occurred_at": self.occurred_at.isoformat(),
            "motion": None if self.motion is None else self.motion.to_json(),
        }

    @classmethod
    def from_json(cls, payload: object) -> MachineRecoveryEvidence:
        if not isinstance(payload, Mapping):
            _invalid("machine recovery evidence must be an object", field="machine recovery evidence")
        fields = frozenset(cast(Mapping[object, object], payload).keys())
        accepted_fields = _RECOVERY_FIELDS_V1 if fields == _RECOVERY_FIELDS_V1 else _RECOVERY_FIELDS_WITH_MOTION
        data = _object(payload, accepted_fields, "machine recovery evidence")
        timestamp = _string(data["occurred_at"], "recovery.occurred_at")
        assert isinstance(timestamp, str)
        try:
            occurred_at = datetime.fromisoformat(timestamp)
        except ValueError:
            _invalid("recovery.occurred_at must be ISO 8601", field="recovery.occurred_at")
        return cls(
            schema_version=_integer(data["schema_version"], "recovery.schema_version", minimum=1),
            execution_id=cast(str, _string(data["execution_id"], "recovery.execution_id")),
            recovery_intent=_enum_json(data["recovery_intent"], RecoveryIntent, "recovery.recovery_intent"),
            stream_milestone=_enum_json(data["stream_milestone"], StreamMilestone, "recovery.stream_milestone"),
            reason_code=cast(str, _string(data["reason_code"], "recovery.reason_code")),
            failure=MachineFailureEvidenceV1.from_json(data["failure"]),
            occurred_at=_timestamp(occurred_at, "recovery.occurred_at"),
            motion=(
                None
                if "motion" not in data or data["motion"] is None
                else MachineMotionRecoveryEvidenceV1.from_json(data["motion"])
            ),
        )


@dataclass(frozen=True, slots=True)
class OwnerRetirementEvidence:
    predecessor_execution_id: str
    disposition: RetirementDisposition
    retired_at: datetime
    successor_execution_id: str | None = None

    def __post_init__(self) -> None:
        _string(self.predecessor_execution_id, "retirement.predecessor_execution_id")
        _enum(self.disposition, RetirementDisposition, "retirement.disposition")
        _timestamp(self.retired_at, "retirement.retired_at")
        _string(self.successor_execution_id, "retirement.successor_execution_id", nullable=True)
        has_successor = self.successor_execution_id is not None
        if has_successor != (self.disposition is RetirementDisposition.TRANSFERRED):
            _invalid("only transferred retirement carries one successor execution ID")
        if self.successor_execution_id == self.predecessor_execution_id:
            _invalid("ownership cannot transfer to the predecessor itself")


@dataclass(frozen=True, slots=True)
class MachineExecution:
    schema_version: int
    execution_id: str
    job_id: str
    ready_revision: int
    revision: int
    phase: MachinePhase
    service_epoch: str
    machine_session_epoch: str
    gcode_sha256: str
    application_version: str
    application_digest: str
    machine_digest: str
    provider_digest: str
    stream_milestone: StreamMilestone
    recovery_intent: RecoveryIntent | None
    predecessor_execution_id: str | None
    recovery_evidence: MachineRecoveryEvidence | None
    retirement: OwnerRetirementEvidence | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "execution.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("execution schema version is unsupported", field="execution.schema_version")
        _string(self.execution_id, "execution.execution_id")
        _string(self.job_id, "execution.job_id")
        _integer(self.ready_revision, "execution.ready_revision", minimum=1)
        _integer(self.revision, "execution.revision")
        _enum(self.phase, MachinePhase, "execution.phase")
        _string(self.service_epoch, "execution.service_epoch")
        _string(self.machine_session_epoch, "execution.machine_session_epoch")
        _digest(self.gcode_sha256, "execution.gcode_sha256")
        _string(self.application_version, "execution.application_version")
        _digest(self.application_digest, "execution.application_digest")
        _digest(self.machine_digest, "execution.machine_digest")
        _digest(self.provider_digest, "execution.provider_digest")
        _enum(self.stream_milestone, StreamMilestone, "execution.stream_milestone")
        if self.recovery_intent is not None:
            _enum(self.recovery_intent, RecoveryIntent, "execution.recovery_intent")
        _string(self.predecessor_execution_id, "execution.predecessor_execution_id", nullable=True)
        if self.predecessor_execution_id == self.execution_id:
            _invalid("execution cannot be its own predecessor")
        if self.recovery_evidence is not None:
            if type(self.recovery_evidence) is not MachineRecoveryEvidence:
                _invalid("execution.recovery_evidence must be exact MachineRecoveryEvidence")
            if self.recovery_evidence.execution_id != self.execution_id:
                _invalid("recovery evidence must identify its execution")
            motion = self.recovery_evidence.motion
            if motion is not None and motion.expected_machine_session_epoch != self.machine_session_epoch:
                _invalid("motion evidence must match the execution session authority")
        if self.retirement is not None:
            if type(self.retirement) is not OwnerRetirementEvidence:
                _invalid("execution.retirement must be exact OwnerRetirementEvidence")
            if self.retirement.predecessor_execution_id != self.execution_id:
                _invalid("retirement must identify its execution")
            if (
                self.retirement.disposition is RetirementDisposition.COMPLETED
                and self.phase is not MachinePhase.COMPLETED
            ):
                _invalid("completed retirement requires COMPLETED phase")
            if (
                self.retirement.disposition in {RetirementDisposition.RELEASED, RetirementDisposition.TRANSFERRED}
                and self.phase is not MachinePhase.RECOVERY_REQUIRED
            ):
                _invalid("release or transfer retirement requires RECOVERY_REQUIRED phase")
        if self.phase is MachinePhase.COMPLETED and (
            self.retirement is None or self.retirement.disposition is not RetirementDisposition.COMPLETED
        ):
            _invalid("COMPLETED requires immutable completed retirement evidence")
        created = _timestamp(self.created_at, "execution.created_at")
        updated = _timestamp(self.updated_at, "execution.updated_at")
        if updated < created:
            _invalid("execution.updated_at must not precede created_at")
        if self.recovery_evidence is not None and not (created <= self.recovery_evidence.occurred_at <= updated):
            _invalid("execution recovery_evidence.occurred_at must be within created_at and updated_at")
        if (
            self.recovery_evidence is not None
            and self.recovery_evidence.motion is not None
            and self.recovery_evidence.motion.snapshot is not None
            and not created <= self.recovery_evidence.motion.snapshot.observed_at <= self.recovery_evidence.occurred_at
        ):
            _invalid("motion snapshot must be within the execution recovery timeline")
        if self.retirement is not None and not created <= self.retirement.retired_at <= updated:
            _invalid("execution retirement.retired_at must be within created_at and updated_at")
        if self.phase is MachinePhase.RECOVERY_REQUIRED:
            if self.recovery_evidence is None:
                _invalid("RECOVERY_REQUIRED must carry recovery evidence")
            if self.recovery_intent is None:
                _invalid("RECOVERY_REQUIRED must carry its milestone-derived recovery intent")
            if self.recovery_evidence.stream_milestone is not self.stream_milestone:
                _invalid("execution stream milestone must match recovery evidence")
            if (
                self.predecessor_execution_id is None
                and self.recovery_evidence.recovery_intent is not self.recovery_intent
            ):
                _invalid("initial execution recovery intent must match recovery evidence")
        elif self.recovery_evidence is not None:
            _invalid("recovery evidence is only valid in RECOVERY_REQUIRED")
        elif self.predecessor_execution_id is None and self.recovery_intent is not None:
            _invalid("recovery intent outside RECOVERY_REQUIRED requires predecessor evidence")
        if self.predecessor_execution_id is not None:
            if self.recovery_intent is None:
                _invalid("recovery successor requires a recovery intent")
            self._validate_successor_subgraph()
        self._validate_phase_milestone()

    def _validate_successor_subgraph(self) -> None:
        assert self.recovery_intent is not None
        post_stream_phases = {
            MachinePhase.PREPARING_SESSION,
            MachinePhase.AWAITING_HOME_APPROVAL,
            MachinePhase.HOMING,
            MachinePhase.COMPLETED,
        }
        if self.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY:
            _invalid("STREAM_AMBIGUOUS_RELEASE_ONLY is release-only and cannot bind a successor")
        if self.phase is MachinePhase.RECOVERY_REQUIRED:
            return
        if (
            self.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
            and self.stream_milestone is StreamMilestone.STREAM_CONFIRMED
            and self.phase
            not in {
                MachinePhase.AWAITING_HOME_Z_APPROVAL,
                MachinePhase.HOMING_Z,
                MachinePhase.COMPLETED,
            }
        ):
            _invalid("confirmed stream milestone cannot re-enter PRE_STREAM_RESTART phases")
        if self.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME:
            if self.stream_milestone is not StreamMilestone.STREAM_CONFIRMED:
                _invalid("POST_STREAM_SAFE_HOME requires the confirmed stream milestone")
            if self.phase not in post_stream_phases:
                _invalid("POST_STREAM_SAFE_HOME execution left its safe-home subgraph")

    def _validate_phase_milestone(self) -> None:
        if self.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE and self.phase not in {
            MachinePhase.STREAMING,
            MachinePhase.RECOVERY_REQUIRED,
        }:
            _invalid("first-write milestone is only valid while STREAMING or in recovery")
        confirmed_phases = {
            MachinePhase.AWAITING_HOME_APPROVAL,
            MachinePhase.HOMING,
            MachinePhase.AWAITING_HOME_Z_APPROVAL,
            MachinePhase.HOMING_Z,
            MachinePhase.COMPLETED,
            MachinePhase.RECOVERY_REQUIRED,
        }
        if self.predecessor_execution_id is not None and self.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME:
            confirmed_phases.add(MachinePhase.PREPARING_SESSION)
        if self.stream_milestone is StreamMilestone.STREAM_CONFIRMED and self.phase not in confirmed_phases:
            _invalid("confirmed stream milestone contradicts the current phase")
        if (
            self.stream_milestone is StreamMilestone.STREAM_CONFIRMED
            and self.phase
            in {
                MachinePhase.AWAITING_HOME_APPROVAL,
                MachinePhase.HOMING,
            }
            and (
                self.predecessor_execution_id is None
                or self.recovery_intent is not RecoveryIntent.POST_STREAM_SAFE_HOME
            )
        ):
            _invalid("confirmed HOME phases require an exact POST_STREAM_SAFE_HOME successor")
        if self.stream_milestone is not StreamMilestone.STREAM_CONFIRMED and self.phase in {
            MachinePhase.AWAITING_HOME_Z_APPROVAL,
            MachinePhase.HOMING_Z,
            MachinePhase.COMPLETED,
        }:
            _invalid("post-stream phase requires the confirmed stream milestone")


@dataclass(frozen=True, slots=True)
class MachineOutcome:
    schema_version: int
    execution_id: str
    job_id: str
    execution_revision: int
    kind: MachineOutcomeKind
    ready_snapshot: ReadyToRunSnapshot
    recovery_evidence: MachineRecoveryEvidence | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "outcome.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("outcome schema version is unsupported", field="outcome.schema_version")
        _string(self.execution_id, "outcome.execution_id")
        _string(self.job_id, "outcome.job_id")
        _integer(self.execution_revision, "outcome.execution_revision", minimum=1)
        _enum(self.kind, MachineOutcomeKind, "outcome.kind")
        if type(self.ready_snapshot) is not ReadyToRunSnapshot:
            _invalid("outcome.ready_snapshot must be exact ReadyToRunSnapshot")
        if self.ready_snapshot.job_id != self.job_id:
            _invalid("outcome ready snapshot must match job ID")
        if (self.kind is MachineOutcomeKind.RECOVERY_REQUIRED) != (self.recovery_evidence is not None):
            _invalid("only RECOVERY_REQUIRED outcomes carry recovery evidence")
        if self.recovery_evidence is not None and type(self.recovery_evidence) is not MachineRecoveryEvidence:
            _invalid("outcome.recovery_evidence must be exact MachineRecoveryEvidence")
        if self.recovery_evidence is not None and self.recovery_evidence.execution_id != self.execution_id:
            _invalid("outcome recovery evidence must match execution ID")
        occurred_at = _timestamp(self.occurred_at, "outcome.occurred_at")
        if self.recovery_evidence is not None and occurred_at < self.recovery_evidence.occurred_at:
            _invalid("outcome.occurred_at must not precede recovery evidence")

    @property
    def job_state(self) -> JobState:
        return JobState(self.kind.value)


def recovery_intent_for_milestone(milestone: StreamMilestone) -> RecoveryIntent:
    _enum(milestone, StreamMilestone, "stream_milestone")
    if milestone is StreamMilestone.NOT_STARTED:
        return RecoveryIntent.PRE_STREAM_RESTART
    if milestone is StreamMilestone.FIRST_WRITE_POSSIBLE:
        return RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY
    return RecoveryIntent.POST_STREAM_SAFE_HOME


def ensure_revision_progression(prior: MachineExecution, successor: MachineExecution) -> None:
    if type(prior) is not MachineExecution or type(successor) is not MachineExecution:
        _invalid("revision progression requires exact MachineExecution values")
    if successor.execution_id != prior.execution_id or successor.revision != prior.revision + 1:
        _invalid("machine execution revision must increase exactly one")
    if successor.created_at != prior.created_at or successor.updated_at < prior.updated_at:
        _invalid("machine execution timestamps must progress monotonically")
    permanent_bindings = (
        "schema_version",
        "job_id",
        "ready_revision",
        "gcode_sha256",
        "application_version",
        "application_digest",
        "machine_digest",
        "provider_digest",
        "service_epoch",
        "machine_session_epoch",
        "predecessor_execution_id",
    )
    if any(getattr(successor, field) != getattr(prior, field) for field in permanent_bindings):
        _invalid("machine execution permanent binding changed across revisions")
    if successor.retirement is not None and not (
        prior.updated_at <= successor.retirement.retired_at <= successor.updated_at
    ):
        _invalid("retirement.retired_at must be within the committing revision timeline")
    if prior.retirement is not None and successor.retirement != prior.retirement:
        _invalid("retirement evidence is permanent once recorded")
    if prior.phase is MachinePhase.RECOVERY_REQUIRED:
        if (
            successor.phase is not MachinePhase.RECOVERY_REQUIRED
            or successor.stream_milestone is not prior.stream_milestone
            or successor.recovery_intent is not prior.recovery_intent
            or successor.recovery_evidence != prior.recovery_evidence
        ):
            _invalid("RECOVERY_REQUIRED evidence is frozen for the execution")
        if prior.retirement is not None or successor.retirement is None:
            _invalid("RECOVERY_REQUIRED permits exactly one monotonic retirement change")
        return
    from drawingmachine.domain.machine.transitions import (
        allowed_phase_transition,
        allowed_stream_milestone_transition,
    )

    if not allowed_stream_milestone_transition(prior.stream_milestone, successor.stream_milestone):
        _invalid("stream milestone must advance monotonically without skipping")
    if successor.phase is MachinePhase.COMPLETED and (
        successor.retirement is None or successor.retirement.disposition is not RetirementDisposition.COMPLETED
    ):
        _invalid("COMPLETED progression requires immutable completed retirement evidence")
    if prior.predecessor_execution_id is not None:
        if successor.recovery_intent is not prior.recovery_intent:
            _invalid("recovery successor intent is a frozen predecessor-derived binding")
    elif successor.recovery_intent is not prior.recovery_intent and (
        successor.phase is not MachinePhase.RECOVERY_REQUIRED
        or successor.recovery_evidence is None
        or successor.recovery_intent is not successor.recovery_evidence.recovery_intent
    ):
        _invalid("initial execution recovery intent can change only on derived recovery entry")
    if successor.phase is prior.phase:
        return
    if successor.phase is MachinePhase.RECOVERY_REQUIRED:
        if prior.phase in {MachinePhase.RECOVERY_REQUIRED, MachinePhase.COMPLETED}:
            _invalid("terminal phase cannot re-enter recovery")
        return
    recovery_intent: RecoveryIntent | None = None
    if prior.predecessor_execution_id is not None:
        recovery_intent = prior.recovery_intent
    if not any(
        allowed_phase_transition(prior.phase, action, successor.phase, recovery_intent=recovery_intent)
        for action in MachineAction
    ):
        _invalid("machine execution phase must follow one legal edge without a phase jump")
