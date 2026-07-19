import json
from datetime import datetime
from typing import cast
from uuid import UUID

from drawingmachine.domain.machine import (
    ApprovalPurpose,
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalBinding,
    MachineApprovalChallenge,
    MachineApprovalEvidenceV1,
    MachinePhase,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.domain.machine.transitions import allowed_recovery_disposition
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import ProtocolResponse

_MACHINE_PURPOSES = {
    "machine.home": ApprovalPurpose.HOME.value,
    "machine.zcal": ApprovalPurpose.ZCAL.value,
    "machine.zconfirm": ApprovalPurpose.ZCONFIRM.value,
    "machine.stream": ApprovalPurpose.STREAM.value,
    "machine.home-z": ApprovalPurpose.HOME_Z.value,
    "machine.recover": ApprovalPurpose.RECOVER.value,
}
_MACHINE_DATA_FIELDS = {
    "schema_version",
    "deduplicated",
    "service_epoch",
    "accepting",
    "execution",
    "progress",
    "last_outcome",
    "challenge",
}
_EXECUTION_FIELDS = {
    "schema_version",
    "execution_id",
    "job_id",
    "ready_revision",
    "revision",
    "phase",
    "stream_milestone",
    "recovery_intent",
    "retired",
}
_CHALLENGE_FIELDS = {
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


def render_json(response: ProtocolResponse) -> str:
    envelope: JsonObject = {
        "schema_version": response.schema_version,
        "ok": response.ok,
        "command": response.command,
        "request_id": response.request_id,
        "data": response.data,
        "error": None if response.error is None else response.error.to_json(),
    }
    return json.dumps(envelope, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def render_text(response: ProtocolResponse) -> str:
    if response.ok:
        status_text = _machine_status_text(response)
        if status_text is not None:
            return status_text
        challenge_text = _machine_challenge_text(response)
        if challenge_text is not None:
            return challenge_text
        status = response.data.get("status")
        return f"{response.command}: {status if isinstance(status, str) else 'OK'}"
    if response.error is None:
        return f"{response.command}: FAILED"
    return f"{response.command}: {response.error.code}: {response.error.message}"


def _machine_challenge_text(response: ProtocolResponse) -> str | None:
    expected_purpose = _MACHINE_PURPOSES.get(response.command)
    if expected_purpose is None or not _valid_machine_data(response.data):
        return None
    challenge_json = response.data["challenge"]
    execution = response.data["execution"]
    if type(challenge_json) is not dict or type(execution) is not dict:
        return None
    challenge = _closed_challenge(challenge_json)
    if challenge is None:
        return None
    expected_action, expected_phase = _render_action_phase(response.command)
    if (
        challenge.status is not ApprovalStatus.PENDING
        or challenge.purpose.value != expected_purpose
        or challenge.binding.action is not expected_action
        or challenge.binding.required_prior_phase is not expected_phase
        or challenge.evidence.action is not expected_action
        or challenge.evidence.required_prior_phase is not expected_phase
        or challenge.binding.service_epoch != response.data["service_epoch"]
        or challenge.binding.execution_id != execution["execution_id"]
        or challenge.binding.job_id != execution["job_id"]
        or challenge.binding.ready_revision != execution["ready_revision"]
        or challenge.binding.execution_revision != execution["revision"]
        or execution["phase"] != expected_phase.value
        or not _challenge_recovery_matches_execution(response.command, challenge, execution)
        or not all(
            _canonical_uuid(identifier)
            for identifier in (
                challenge.challenge_id,
                challenge.binding.execution_id,
                challenge.binding.job_id,
                challenge.binding.service_epoch,
                challenge.binding.machine_session_epoch,
            )
        )
    ):
        return None
    challenge_id = challenge.challenge_id
    return (
        f"{response.command}: challenge {challenge_id} created for {expected_purpose}; "
        "challenge creation is not execution"
    )


def _machine_status_text(response: ProtocolResponse) -> str | None:
    if response.command != "machine.status" or not _valid_machine_data(response.data):
        return None
    service_epoch = response.data["service_epoch"]
    accepting = response.data["accepting"]
    execution = response.data["execution"]
    assert isinstance(service_epoch, str) and isinstance(accepting, bool)
    accepting_text = str(accepting).lower()
    if execution is None:
        return f"machine.status: service_epoch={service_epoch} accepting={accepting_text} active_execution=null"
    assert isinstance(execution, dict)
    recovery = execution["recovery_intent"]
    recovery_text = "null" if recovery is None else recovery
    retired = str(execution["retired"]).lower()
    return (
        f"machine.status: service_epoch={service_epoch} accepting={accepting_text} "
        f"execution_id={execution['execution_id']} job_id={execution['job_id']} "
        f"phase={execution['phase']} revision={execution['revision']} "
        f"stream_milestone={execution['stream_milestone']} recovery_intent={recovery_text} retired={retired}"
    )


def _valid_machine_data(data: JsonObject) -> bool:
    if (
        set(data) != _MACHINE_DATA_FIELDS
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or type(data["deduplicated"]) is not bool
        or not _canonical_uuid(data["service_epoch"])
        or type(data["accepting"]) is not bool
    ):
        return False
    execution = data["execution"]
    if execution is None:
        return data["progress"] is None and data["last_outcome"] is None and data["challenge"] is None
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        return False
    valid_scalars = (
        type(execution["schema_version"]) is int
        and execution["schema_version"] == 1
        and _canonical_uuid(execution["execution_id"])
        and _canonical_uuid(execution["job_id"])
        and type(execution["ready_revision"]) is int
        and execution["ready_revision"] >= 1
        and type(execution["revision"]) is int
        and execution["revision"] >= 0
        and type(execution["phase"]) is str
        and type(execution["stream_milestone"]) is str
        and (execution["recovery_intent"] is None or type(execution["recovery_intent"]) is str)
        and type(execution["retired"]) is bool
    )
    if not valid_scalars:
        return False
    try:
        phase = MachinePhase(cast(str, execution["phase"]))
        milestone = StreamMilestone(cast(str, execution["stream_milestone"]))
        intent_value = execution["recovery_intent"]
        intent = None if intent_value is None else RecoveryIntent(cast(str, intent_value))
    except ValueError:
        return False
    return _consistent_execution_authority(phase, milestone, intent)


def _consistent_execution_authority(
    phase: MachinePhase,
    milestone: StreamMilestone,
    intent: RecoveryIntent | None,
) -> bool:
    if phase is MachinePhase.RECOVERY_REQUIRED:
        return (
            intent
            is {
                StreamMilestone.NOT_STARTED: RecoveryIntent.PRE_STREAM_RESTART,
                StreamMilestone.FIRST_WRITE_POSSIBLE: RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
                StreamMilestone.STREAM_CONFIRMED: RecoveryIntent.POST_STREAM_SAFE_HOME,
            }[milestone]
        )
    if intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY:
        return False
    if milestone is StreamMilestone.FIRST_WRITE_POSSIBLE and phase is not MachinePhase.STREAMING:
        return False
    if intent is RecoveryIntent.POST_STREAM_SAFE_HOME:
        return milestone is StreamMilestone.STREAM_CONFIRMED and phase in {
            MachinePhase.PREPARING_SESSION,
            MachinePhase.AWAITING_HOME_APPROVAL,
            MachinePhase.HOMING,
            MachinePhase.COMPLETED,
        }
    if milestone is StreamMilestone.STREAM_CONFIRMED:
        return phase in {
            MachinePhase.AWAITING_HOME_Z_APPROVAL,
            MachinePhase.HOMING_Z,
            MachinePhase.COMPLETED,
        }
    return phase not in {MachinePhase.AWAITING_HOME_Z_APPROVAL, MachinePhase.HOMING_Z, MachinePhase.COMPLETED}


def _challenge_recovery_matches_execution(
    command: str,
    challenge: MachineApprovalChallenge,
    execution: JsonObject,
) -> bool:
    disposition = challenge.binding.recovery_disposition
    if command != "machine.recover":
        return disposition is None
    intent_value = execution["recovery_intent"]
    if type(intent_value) is not str or disposition is None:
        return False
    try:
        intent = RecoveryIntent(intent_value)
    except ValueError:
        return False
    return isinstance(disposition, RecoveryDisposition) and allowed_recovery_disposition(intent, disposition)


def _closed_challenge(payload: JsonObject) -> MachineApprovalChallenge | None:
    try:
        if (
            set(payload) != _CHALLENGE_FIELDS
            or payload["status_changed_at"] is not None
            or payload["consumer"] is not None
        ):
            return None
        binding = MachineApprovalBinding.from_json(payload["binding"])
        requester = AuthenticatedMachinePrincipal.from_json(payload["requester"])
        operator = KernelMachinePrincipal.from_json(payload["operator_principal"])
        evidence = MachineApprovalEvidenceV1.from_json(payload["evidence"])
        issued_at = datetime.fromisoformat(str(payload["issued_at"]))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        purpose = payload["purpose"]
        status = payload["status"]
        if type(purpose) is not str or type(status) is not str:
            return None
        return MachineApprovalChallenge(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            challenge_id=payload["challenge_id"],  # type: ignore[arg-type]
            binding=binding,
            requester=requester,
            operator_principal=operator,
            purpose=ApprovalPurpose(purpose),
            evidence=evidence,
            issued_at=issued_at,
            issued_monotonic=payload["issued_monotonic"],  # type: ignore[arg-type]
            monotonic_deadline=payload["monotonic_deadline"],  # type: ignore[arg-type]
            expires_at=expires_at,
            status=ApprovalStatus(status),
            status_changed_at=None,
            consumer=None,
        )
    except (DrawingMachineError, KeyError, TypeError, ValueError):
        return None


def _render_action_phase(command: str) -> tuple[MachineAction, MachinePhase]:
    return {
        "machine.home": (MachineAction.HOME, MachinePhase.AWAITING_HOME_APPROVAL),
        "machine.zcal": (MachineAction.ZCAL, MachinePhase.AWAITING_ZCAL_APPROVAL),
        "machine.zconfirm": (MachineAction.ZCONFIRM, MachinePhase.AWAITING_ZCONFIRM_APPROVAL),
        "machine.stream": (MachineAction.STREAM, MachinePhase.AWAITING_STREAM_APPROVAL),
        "machine.home-z": (MachineAction.HOME_Z, MachinePhase.AWAITING_HOME_Z_APPROVAL),
        "machine.recover": (MachineAction.RECOVER, MachinePhase.RECOVERY_REQUIRED),
    }[command]


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value
