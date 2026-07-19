from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

from drawingmachine.adapters.persistence.migrator import migrate
from drawingmachine.adapters.persistence.sqlite_repository import (
    _assert_audit_matches_event,
    _canonical_json,
    _job_from_row,
    _plain_audit_payload,
)
from drawingmachine.domain.jobs import AuditRecord, JobEvent, JobRecord, ReadyToRunSnapshot
from drawingmachine.domain.jobs.models import MachineJobOutcomeTransition
from drawingmachine.domain.machine import (
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalChallenge,
    MachineApprovalDecision,
    MachineAuditPayloadV1,
    MachineAuditPayloadV2,
    MachineEvent,
    MachineExecution,
    MachineFailureEvidenceV1,
    MachineOutcome,
    MachineOutcomeKind,
    MachinePhase,
    MachineProgress,
    MachineRecoveryEvidence,
    MachineSessionSnapshot,
    OwnerRetirementEvidence,
    RecoveryDisposition,
    RecoveryIntent,
    RetirementDisposition,
    StreamMilestone,
)
from drawingmachine.domain.machine.models import ensure_revision_progression
from drawingmachine.domain.machine.transitions import allowed_recovery_disposition
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.machine_repository import (
    MachineAdmissionCapability,
    MachineRepositoryTransaction,
    MachineRequestKey,
    MachineRequestRecord,
    MachineRequestReservation,
)

_EXECUTION_COLUMNS = """
    execution_id, schema_version, job_id, ready_revision, revision, phase,
    service_epoch, machine_session_epoch, gcode_sha256, application_version,
    application_digest, machine_digest, provider_digest, stream_milestone,
    recovery_intent, predecessor_execution_id, recovery_evidence_json,
    retirement_disposition, retired_at, successor_execution_id,
    outcome_kind, outcome_json, outcome_at, created_at, updated_at
"""

_REQUEST_COLUMNS = """
    principal_uid, principal_gid, request_id, requester_pid, command,
    argument_digest, execution_id, action, execution_revision, service_epoch,
    machine_session_epoch, recovery_disposition, recovery_intent,
    predecessor_execution_id, admission_id, dispatch_nonce, response_json,
    reserved_at, completed_at, claimed_at, invalidated_at, invalidation_reason
"""

_ADMITTED_PHASE = {
    MachineAction.PREPARE_SESSION: MachinePhase.PREPARING_SESSION,
    MachineAction.HOME: MachinePhase.HOMING,
    MachineAction.ZCAL: MachinePhase.Z_CALIBRATING,
    MachineAction.ZCONFIRM: MachinePhase.Z_CONFIRMING,
    MachineAction.STREAM: MachinePhase.STREAMING,
    MachineAction.HOME_Z: MachinePhase.HOMING_Z,
    MachineAction.RECOVER: MachinePhase.PREPARING_SESSION,
}

_MACHINE_TABLES = frozenset(
    {
        "machine_approval_challenges",
        "machine_events",
        "machine_executions",
        "machine_progress_events",
        "machine_request_results",
        "machine_sessions",
    }
)

MachineAuditPayload = MachineAuditPayloadV1 | MachineAuditPayloadV2


def _error(
    code: str, message: str, *, retryable: bool = False, details: JsonObject | None = None
) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.INTERNAL,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
        )
    )


def _conflict(error: sqlite3.IntegrityError) -> DrawingMachineError:
    message = str(error)
    if "machine_executions_one_global_active_owner" in message:
        return _error("MACHINE_OWNER_CONFLICT", "another execution owns the machine safety latch")
    if (
        "machine_approval_one_pending_action" in message
        or "machine_approval_challenges.execution_id, machine_approval_challenges.action" in message
    ):
        return _error("MACHINE_APPROVAL_PENDING_CONFLICT", "one pending challenge already exists for the action")
    if "active machine ownership prevents job cancellation" in message:
        return _error("MACHINE_ACTIVE_OWNER_CANCEL_DENIED", "active machine ownership prevents job cancellation")
    if "machine execution revision" in message:
        return _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed")
    if "machine approval decision is one-time" in message:
        return _error("MACHINE_APPROVAL_REPLAY", "machine approval challenge is no longer pending")
    if "machine approval authority is stale" in message:
        return _error("MACHINE_APPROVAL_STALE", "machine approval no longer matches current execution authority")
    if "machine progress requires current STREAMING authority" in message:
        return _error("MACHINE_PROGRESS_STALE", "machine progress does not match current STREAMING authority")
    if "successor machine execution must preserve predecessor frozen authority" in message:
        return _error("MACHINE_TRANSFER_INVALID", "successor changed predecessor frozen authority")
    return _error("MACHINE_PERSISTENCE_CONFLICT", "machine persistence constraint rejected the operation")


def _json_object(payload: str) -> JsonObject:
    value = cast(object, json.loads(payload))
    if not isinstance(value, Mapping):
        raise _error("MACHINE_PERSISTENCE_CORRUPT", "persisted machine JSON must be an object")
    return cast(JsonObject, dict(value))


def _session_json(session: MachineSessionSnapshot) -> JsonObject:
    return {
        "schema_version": session.schema_version,
        "execution_id": session.execution_id,
        "machine_session_epoch": session.machine_session_epoch,
        "controller_state": session.controller_state,
        "mpos": list(session.mpos),
        "wpos": list(session.wpos),
        "wco": list(session.wco),
        "g54": list(session.g54),
        "stabilized": session.stabilized,
        "observed_at": session.observed_at.isoformat(),
    }


def _session_from_json(payload: str) -> MachineSessionSnapshot:
    data = _json_object(payload)

    def position(name: str) -> tuple[float, float, float]:
        value = data[name]
        if not isinstance(value, list) or len(value) != 3:
            raise _error("MACHINE_PERSISTENCE_CORRUPT", f"persisted {name} must be XYZ")
        return (cast(float, value[0]), cast(float, value[1]), cast(float, value[2]))

    return MachineSessionSnapshot(
        schema_version=int(cast(int, data["schema_version"])),
        execution_id=str(data["execution_id"]),
        machine_session_epoch=str(data["machine_session_epoch"]),
        controller_state=str(data["controller_state"]),
        mpos=position("mpos"),
        wpos=position("wpos"),
        wco=position("wco"),
        g54=position("g54"),
        stabilized=cast(bool, data["stabilized"]),
        observed_at=datetime.fromisoformat(str(data["observed_at"])),
    )


def _progress_json(progress: MachineProgress) -> JsonObject:
    return {
        "schema_version": progress.schema_version,
        "execution_id": progress.execution_id,
        "execution_revision": progress.execution_revision,
        "total_lines": progress.total_lines,
        "acknowledged_lines": progress.acknowledged_lines,
        "ok_count": progress.ok_count,
        "error_count": progress.error_count,
        "percent": progress.percent,
        "last_source_line": progress.last_source_line,
        "last_command": progress.last_command,
        "last_event_at": progress.last_event_at.isoformat(),
        "failure": None if progress.failure is None else progress.failure.to_json(),
    }


def _progress_from_json(payload: str) -> MachineProgress:
    data = _json_object(payload)
    failure = data["failure"]
    return MachineProgress(
        schema_version=int(cast(int, data["schema_version"])),
        execution_id=str(data["execution_id"]),
        execution_revision=int(cast(int, data["execution_revision"])),
        total_lines=int(cast(int, data["total_lines"])),
        acknowledged_lines=int(cast(int, data["acknowledged_lines"])),
        ok_count=int(cast(int, data["ok_count"])),
        error_count=int(cast(int, data["error_count"])),
        percent=float(cast(float, data["percent"])),
        last_source_line=None if data["last_source_line"] is None else int(cast(int, data["last_source_line"])),
        last_command=None if data["last_command"] is None else str(data["last_command"]),
        last_event_at=datetime.fromisoformat(str(data["last_event_at"])),
        failure=None if failure is None else MachineFailureEvidenceV1.from_json(failure),
    )


def _execution_from_row(row: tuple[object, ...]) -> MachineExecution:
    recovery = None if row[16] is None else MachineRecoveryEvidence.from_json(_json_object(str(row[16])))
    retirement = None
    if row[18] is not None:
        retirement = OwnerRetirementEvidence(
            predecessor_execution_id=str(row[0]),
            disposition=RetirementDisposition(str(row[17])),
            retired_at=datetime.fromisoformat(str(row[18])),
            successor_execution_id=None if row[19] is None else str(row[19]),
        )
    return MachineExecution(
        execution_id=str(row[0]),
        schema_version=int(cast(int, row[1])),
        job_id=str(row[2]),
        ready_revision=int(cast(int, row[3])),
        revision=int(cast(int, row[4])),
        phase=MachinePhase(str(row[5])),
        service_epoch=str(row[6]),
        machine_session_epoch=str(row[7]),
        gcode_sha256=str(row[8]),
        application_version=str(row[9]),
        application_digest=str(row[10]),
        machine_digest=str(row[11]),
        provider_digest=str(row[12]),
        stream_milestone=StreamMilestone(str(row[13])),
        recovery_intent=None if row[14] is None else RecoveryIntent(str(row[14])),
        predecessor_execution_id=None if row[15] is None else str(row[15]),
        recovery_evidence=recovery,
        retirement=retirement,
        created_at=datetime.fromisoformat(str(row[23])),
        updated_at=datetime.fromisoformat(str(row[24])),
    )


def _request_from_row(row: tuple[object, ...]) -> MachineRequestRecord:
    response = None if row[16] is None else _json_object(str(row[16]))
    return MachineRequestRecord(
        key=MachineRequestKey(int(cast(int, row[0])), int(cast(int, row[1])), str(row[2])),
        requester_pid=int(cast(int, row[3])),
        command=str(row[4]),
        argument_digest=str(row[5]),
        execution_id=None if row[6] is None else str(row[6]),
        action=None if row[7] is None else str(row[7]),
        admission_id=None if row[14] is None else str(row[14]),
        dispatch_nonce=None if row[15] is None else str(row[15]),
        response=response,
        reserved_at=datetime.fromisoformat(str(row[17])),
        completed_at=None if row[18] is None else datetime.fromisoformat(str(row[18])),
        claimed_at=None if row[19] is None else datetime.fromisoformat(str(row[19])),
        execution_revision=None if row[8] is None else int(cast(int, row[8])),
        service_epoch=None if row[9] is None else str(row[9]),
        machine_session_epoch=None if row[10] is None else str(row[10]),
        invalidated_at=None if row[20] is None else datetime.fromisoformat(str(row[20])),
        invalidation_reason=None if row[21] is None else str(row[21]),
        recovery_disposition=None if row[11] is None else RecoveryDisposition(str(row[11])),
        recovery_intent=None if row[12] is None else RecoveryIntent(str(row[12])),
        predecessor_execution_id=None if row[13] is None else str(row[13]),
    )


def _outcome_from_json(payload: str) -> MachineOutcome:
    data = _json_object(payload)
    recovery = data["recovery_evidence"]
    return MachineOutcome(
        schema_version=int(cast(int, data["schema_version"])),
        execution_id=str(data["execution_id"]),
        job_id=str(data["job_id"]),
        execution_revision=int(cast(int, data["execution_revision"])),
        kind=MachineOutcomeKind(str(data["kind"])),
        ready_snapshot=ReadyToRunSnapshot.from_json(data["ready_snapshot"]),
        recovery_evidence=None if recovery is None else MachineRecoveryEvidence.from_json(recovery),
        occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
    )


def _challenge_from_row(row: tuple[object, ...]) -> MachineApprovalChallenge:
    requester = AuthenticatedMachinePrincipal(
        int(cast(int, row[1])),
        int(cast(int, row[2])),
        int(cast(int, row[3])),
    )
    operator = KernelMachinePrincipal(int(cast(int, row[4])), int(cast(int, row[5])))
    consumer = (
        None
        if row[6] is None
        else AuthenticatedMachinePrincipal(
            int(cast(int, row[6])),
            int(cast(int, row[7])),
            int(cast(int, row[8])),
        )
    )
    automation = requester.stable_principal
    if automation == operator:
        automation = KernelMachinePrincipal(operator.uid + 1, operator.gid + 1)
    return MachineApprovalChallenge.from_json(
        _json_object(str(row[0])),
        automation_principal=automation,
        operator_principal=operator,
        trusted_requester=requester,
        trusted_consumer=consumer,
    )


class _Capability:
    def __init__(self) -> None:
        self.active = True

    def require(self) -> None:
        if not self.active:
            raise _error("MACHINE_TRANSACTION_INACTIVE", "machine repository transaction is no longer active")


class _MachineTransaction:
    def __init__(self, connection: sqlite3.Connection, capability: _Capability) -> None:
        self._connection = connection
        self._capability = capability

    def get_job(self, job_id: str) -> JobRecord | None:
        self._capability.require()
        row = self._connection.execute(
            """SELECT job_id, schema_version, kind, name, state, revision, request_json,
                      config_snapshot_json, blocker_json, error_json, ready_snapshot_json,
                      created_at, updated_at FROM jobs WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        return None if row is None else _job_from_row(cast(tuple[object, ...], row))

    def get_pending_challenge(
        self,
        execution_id: str,
        action: MachineAction,
    ) -> MachineApprovalChallenge | None:
        self._capability.require()
        row = self._connection.execute(
            """SELECT challenge_json, requester_uid, requester_gid, requester_pid,
                      operator_uid, operator_gid
               FROM machine_approval_challenges
               WHERE execution_id = ? AND action = ? AND status = 'PENDING'""",
            (execution_id, action.value),
        ).fetchone()
        if row is None:
            return None
        requester = AuthenticatedMachinePrincipal(int(row[1]), int(row[2]), int(row[3]))
        operator = KernelMachinePrincipal(int(row[4]), int(row[5]))
        automation = requester.stable_principal
        if automation == operator:
            automation = KernelMachinePrincipal(operator.uid + 1, operator.gid + 1)
        return MachineApprovalChallenge.from_json(
            _json_object(str(row[0])),
            automation_principal=automation,
            operator_principal=operator,
            trusted_requester=requester,
            trusted_consumer=None,
        )

    def get_request(self, key: MachineRequestKey) -> MachineRequestRecord | None:
        self._capability.require()
        return self._get_request(key)

    def get_challenge(self, challenge_id: str) -> MachineApprovalChallenge | None:
        self._capability.require()
        row = self._connection.execute(
            """SELECT challenge_json, requester_uid, requester_gid, requester_pid,
                      operator_uid, operator_gid, consumer_uid, consumer_gid, consumer_pid
               FROM machine_approval_challenges WHERE challenge_id = ?""",
            (challenge_id,),
        ).fetchone()
        return None if row is None else _challenge_from_row(cast(tuple[object, ...], row))

    def create_execution(
        self,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        self._capability.require()
        if (
            execution.revision != 0
            or execution.phase is not MachinePhase.PREPARING_SESSION
            or execution.retirement is not None
            or execution.predecessor_execution_id is not None
        ):
            raise _error("MACHINE_EXECUTION_INVALID", "new execution must start unretired in PREPARING_SESSION")
        try:
            self._insert_execution(execution)
            self._insert_event(execution, event, audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def record_opened_session(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        session: MachineSessionSnapshot,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        self._capability.require()
        prior = self._get_execution(execution.execution_id)
        if prior is None or prior.revision != expected_revision:
            raise _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed before session record")
        if (
            prior.phase is not MachinePhase.PREPARING_SESSION
            or execution.phase is not MachinePhase.AWAITING_HOME_APPROVAL
            or session.execution_id != execution.execution_id
            or session.machine_session_epoch != execution.machine_session_epoch
        ):
            raise _error("MACHINE_SESSION_BINDING_MISMATCH", "observed session does not match PREPARING execution")
        ensure_revision_progression(prior, execution)
        try:
            self._insert_session(session)
            self._update_execution(expected_revision, execution)
            self._insert_event(execution, event, audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def transition_execution(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        self._capability.require()
        prior = self._get_execution(execution.execution_id)
        if prior is None or prior.revision != expected_revision:
            raise _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed")
        ensure_revision_progression(prior, execution)
        try:
            self._update_execution(expected_revision, execution)
            self._insert_event(execution, event, audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def issue_challenge(
        self,
        challenge: MachineApprovalChallenge,
        *,
        replacement: MachineApprovalDecision | None = None,
        event: MachineEvent | None = None,
        audit: MachineAuditPayload | None = None,
    ) -> None:
        self._capability.require()
        if (event is None) != (audit is None):
            raise _error("MACHINE_EVENT_BINDING_MISMATCH", "challenge event and audit must be atomic")
        if challenge.status is not ApprovalStatus.PENDING:
            raise _error("MACHINE_APPROVAL_INVALID", "new challenge must be pending")
        self._require_current_approval_binding(challenge)
        try:
            if replacement is not None:
                if replacement.result.status not in {ApprovalStatus.SUPERSEDED, ApprovalStatus.EXPIRED}:
                    raise _error(
                        "MACHINE_APPROVAL_INVALID",
                        "challenge replacement requires exact SUPERSEDED or EXPIRED decision",
                    )
                self._require_current_approval_binding(replacement.expected)
                cursor = self._connection.execute(
                    """
                    UPDATE machine_approval_challenges
                    SET status = ?, status_changed_at = ?, challenge_json = ?
                    WHERE challenge_id = ? AND status = 'PENDING' AND challenge_json = ?
                    """,
                    (
                        replacement.result.status.value,
                        replacement.result.status_changed_at.isoformat()
                        if replacement.result.status_changed_at is not None
                        else None,
                        _canonical_json(replacement.result.to_json()),
                        replacement.result.challenge_id,
                        _canonical_json(replacement.expected.to_json()),
                    ),
                )
                if cursor.rowcount != 1:
                    raise _error("MACHINE_APPROVAL_REPLAY", "challenge to replace is no longer pending")
            self._insert_challenge(challenge)
            if event is not None and audit is not None:
                execution = self._get_execution(challenge.binding.execution_id)
                assert execution is not None
                self._insert_event(execution, event, audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def decide_challenge(self, decision: MachineApprovalDecision) -> None:
        self._capability.require()
        self._require_current_approval_binding(decision.expected)
        result = decision.result
        consumer = result.consumer
        try:
            cursor = self._connection.execute(
                """
                UPDATE machine_approval_challenges
                SET status = ?, challenge_json = ?, consumer_uid = ?, consumer_gid = ?,
                    consumer_pid = ?, status_changed_at = ?
                WHERE challenge_id = ? AND status = 'PENDING' AND challenge_json = ?
                """,
                (
                    result.status.value,
                    _canonical_json(result.to_json()),
                    None if consumer is None else consumer.uid,
                    None if consumer is None else consumer.gid,
                    None if consumer is None else consumer.pid,
                    None if result.status_changed_at is None else result.status_changed_at.isoformat(),
                    result.challenge_id,
                    _canonical_json(decision.expected.to_json()),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error
        if cursor.rowcount != 1:
            raise _error("MACHINE_APPROVAL_REPLAY", "machine approval challenge is no longer pending")

    def reserve_request(self, record: MachineRequestRecord) -> MachineRequestReservation:
        self._capability.require()
        self._validate_request(record)
        existing = self._get_request(record.key)
        if existing is not None:
            if existing.command != record.command or existing.argument_digest != record.argument_digest:
                raise _error("REQUEST_ID_CONFLICT", "request ID was already used with different authority")
            return MachineRequestReservation(existing, created=False)
        try:
            self._connection.execute(
                """
                INSERT INTO machine_request_results (
                    principal_uid, principal_gid, request_id, requester_pid, command,
                    argument_digest, execution_id, action, execution_revision, service_epoch,
                    machine_session_epoch, recovery_disposition, recovery_intent,
                    predecessor_execution_id, admission_id, dispatch_nonce, response_json,
                    reserved_at, completed_at, claimed_at, invalidated_at, invalidation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._request_values(record),
            )
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error
        return MachineRequestReservation(record, created=True)

    def complete_request(
        self,
        key: MachineRequestKey,
        *,
        response: JsonObject,
        completed_at: datetime,
    ) -> MachineRequestRecord:
        self._capability.require()
        cursor = self._connection.execute(
            """
            UPDATE machine_request_results
            SET response_json = ?, completed_at = ?
            WHERE principal_uid = ? AND principal_gid = ? AND request_id = ?
              AND response_json IS NULL AND invalidated_at IS NULL
            """,
            (_canonical_json(response), completed_at.isoformat(), key.principal_uid, key.principal_gid, key.request_id),
        )
        if cursor.rowcount != 1:
            raise _error("MACHINE_REQUEST_RESULT_CONFLICT", "machine request result is missing or already completed")
        result = self._get_request(key)
        assert result is not None
        return result

    def claim_request(
        self,
        capability: MachineAdmissionCapability,
        *,
        claimed_at: datetime,
    ) -> MachineRequestRecord:
        self._capability.require()
        self._validate_admission_capability(capability)
        request = self._get_request(capability.key)
        if request is None:
            raise _error("MACHINE_ADMISSION_MISMATCH", "machine admission capability does not exist")
        if (
            request.admission_id != capability.admission_id
            or request.dispatch_nonce != capability.dispatch_nonce
            or request.execution_id != capability.execution_id
            or request.action != capability.action.value
            or request.execution_revision != capability.execution_revision
            or request.service_epoch != capability.service_epoch
            or request.machine_session_epoch != capability.machine_session_epoch
            or request.recovery_disposition is not capability.recovery_disposition
            or request.recovery_intent is not capability.recovery_intent
            or request.predecessor_execution_id != capability.predecessor_execution_id
        ):
            raise _error("MACHINE_ADMISSION_MISMATCH", "machine admission capability binding does not match")
        if request.claimed_at is not None:
            raise _error("MACHINE_ADMISSION_ALREADY_CLAIMED", "machine admission was already claimed")
        if request.invalidated_at is not None:
            raise _error("MACHINE_ADMISSION_STALE", "machine admission was invalidated")
        required_phase = _ADMITTED_PHASE.get(capability.action)
        execution = self._get_execution(capability.execution_id)
        if (
            required_phase is None
            or execution is None
            or execution.revision != capability.execution_revision
            or execution.phase is not required_phase
            or execution.service_epoch != capability.service_epoch
            or execution.machine_session_epoch != capability.machine_session_epoch
            or execution.retirement is not None
            or execution.phase in {MachinePhase.RECOVERY_REQUIRED, MachinePhase.COMPLETED}
        ):
            raise _error("MACHINE_ADMISSION_STALE", "machine admission is no longer current active authority")
        cursor = self._connection.execute(
            """
            UPDATE machine_request_results
            SET claimed_at = ?
            WHERE principal_uid = ? AND principal_gid = ? AND request_id = ?
              AND admission_id = ? AND dispatch_nonce = ?
              AND execution_id = ? AND action = ? AND execution_revision = ?
              AND service_epoch = ? AND machine_session_epoch = ?
              AND recovery_disposition IS ? AND recovery_intent IS ?
              AND predecessor_execution_id IS ?
              AND claimed_at IS NULL AND invalidated_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM machine_executions
                  WHERE execution_id = ? AND revision = ? AND phase = ?
                    AND service_epoch = ? AND machine_session_epoch = ?
                    AND retired_at IS NULL
                    AND (
                        ? != 'RECOVER'
                        OR (predecessor_execution_id = ? AND recovery_intent = ?)
                    )
              )
              AND (
                  ? != 'RECOVER'
                  OR EXISTS (
                      SELECT 1 FROM machine_executions AS predecessor
                      WHERE predecessor.execution_id = ?
                        AND predecessor.phase = 'RECOVERY_REQUIRED'
                        AND predecessor.recovery_intent = ?
                        AND predecessor.retirement_disposition = 'TRANSFERRED'
                        AND predecessor.retired_at IS NOT NULL
                        AND predecessor.successor_execution_id = ?
                  )
              )
            """,
            (
                claimed_at.isoformat(),
                capability.key.principal_uid,
                capability.key.principal_gid,
                capability.key.request_id,
                capability.admission_id,
                capability.dispatch_nonce,
                capability.execution_id,
                capability.action.value,
                capability.execution_revision,
                capability.service_epoch,
                capability.machine_session_epoch,
                None if capability.recovery_disposition is None else capability.recovery_disposition.value,
                None if capability.recovery_intent is None else capability.recovery_intent.value,
                capability.predecessor_execution_id,
                capability.execution_id,
                capability.execution_revision,
                required_phase.value,
                capability.service_epoch,
                capability.machine_session_epoch,
                capability.action.value,
                capability.predecessor_execution_id,
                None if capability.recovery_intent is None else capability.recovery_intent.value,
                capability.action.value,
                capability.predecessor_execution_id,
                None if capability.recovery_intent is None else capability.recovery_intent.value,
                capability.execution_id,
            ),
        )
        if cursor.rowcount != 1:
            raise _error("MACHINE_ADMISSION_STALE", "machine admission is no longer current active authority")
        result = self._get_request(capability.key)
        assert result is not None
        return result

    def append_progress(self, progress_id: str, progress: MachineProgress) -> None:
        self._capability.require()
        execution = self._get_execution(progress.execution_id)
        if (
            execution is None
            or execution.revision != progress.execution_revision
            or execution.phase is not MachinePhase.STREAMING
            or execution.retirement is not None
        ):
            raise _error("MACHINE_PROGRESS_STALE", "progress does not match current STREAMING authority")
        try:
            self._connection.execute(
                """
                INSERT INTO machine_progress_events (
                    progress_id, execution_id, execution_revision, progress_json, last_event_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    progress_id,
                    progress.execution_id,
                    progress.execution_revision,
                    _canonical_json(_progress_json(progress)),
                    progress.last_event_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def commit_machine_outcome(
        self,
        expected_execution_revision: int,
        execution: MachineExecution,
        *,
        outcome: MachineOutcome,
        job_transition: MachineJobOutcomeTransition,
        machine_event: MachineEvent,
        machine_audit: MachineAuditPayload,
        job_event: JobEvent,
        job_audit: AuditRecord,
        session: MachineSessionSnapshot | None = None,
    ) -> None:
        self._capability.require()
        prior = self._get_execution(execution.execution_id)
        if prior is None or prior.revision != expected_execution_revision:
            raise _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed")
        ensure_revision_progression(prior, execution)
        self._validate_outcome(execution, outcome, job_transition)
        job_row = self._connection.execute(
            """SELECT job_id, schema_version, kind, name, state, revision, request_json,
                      config_snapshot_json, blocker_json, error_json, ready_snapshot_json,
                      created_at, updated_at FROM jobs WHERE job_id = ?""",
            (job_transition.job_id,),
        ).fetchone()
        if job_row is None:
            raise _error("MACHINE_JOB_CONFLICT", "machine outcome job does not exist")
        job = _job_from_row(cast(tuple[object, ...], job_row))
        if job.revision != job_transition.expected_revision or job.ready_snapshot != job_transition.ready_snapshot:
            raise _error("MACHINE_JOB_CONFLICT", "machine outcome job authority changed")
        if job_event.job_id != job.job_id or job_event.job_revision != job.revision + 1:
            raise _error("MACHINE_JOB_CONFLICT", "machine outcome job event does not match transition")
        if (
            job_event.prior_state is not job.state
            or job_event.result_state is not job_transition.result_state
            or job_event.event_type != job_transition.event_type
        ):
            raise _error("MACHINE_JOB_CONFLICT", "machine outcome job event state does not match transition")
        _assert_audit_matches_event(job_event, job_audit)
        try:
            if session is not None:
                if (
                    session.execution_id != execution.execution_id
                    or session.machine_session_epoch != execution.machine_session_epoch
                ):
                    raise _error("MACHINE_SESSION_BINDING_MISMATCH", "recovery session evidence is misbound")
                self._insert_session(session)
            self._update_execution(expected_execution_revision, execution, outcome=outcome)
            job_cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = ?, revision = ?, ready_snapshot_json = ?, updated_at = ?
                WHERE job_id = ? AND revision = ?
                """,
                (
                    job_transition.result_state.value,
                    job_transition.expected_revision + 1,
                    _canonical_json(job_transition.ready_snapshot.to_json()),
                    outcome.occurred_at.isoformat(),
                    job_transition.job_id,
                    job_transition.expected_revision,
                ),
            )
            assert job_cursor.rowcount == 1
            self._insert_event(execution, machine_event, machine_audit)
            self._insert_job_event(job_event, job_audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def release_owner(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        if execution.retirement is None or execution.retirement.disposition is not RetirementDisposition.RELEASED:
            raise _error("MACHINE_RELEASE_INVALID", "owner release requires immutable RELEASED evidence")
        self.transition_execution(expected_revision, execution, event=event, audit=audit)

    def transfer_owner(
        self,
        expected_predecessor_revision: int,
        predecessor: MachineExecution,
        successor: MachineExecution,
        *,
        predecessor_event: MachineEvent,
        successor_event: MachineEvent,
        predecessor_audit: MachineAuditPayload,
        successor_audit: MachineAuditPayload,
    ) -> None:
        self._capability.require()
        prior = self._get_execution(predecessor.execution_id)
        if prior is None or prior.revision != expected_predecessor_revision:
            raise _error("MACHINE_REVISION_CONFLICT", "predecessor machine execution revision changed")
        ensure_revision_progression(prior, predecessor)
        retirement = predecessor.retirement
        if (
            retirement is None
            or retirement.disposition is not RetirementDisposition.TRANSFERRED
            or retirement.successor_execution_id != successor.execution_id
            or successor.predecessor_execution_id != predecessor.execution_id
        ):
            raise _error("MACHINE_TRANSFER_INVALID", "ownership transfer bindings do not match")
        frozen_fields = (
            "schema_version",
            "job_id",
            "ready_revision",
            "gcode_sha256",
            "application_version",
            "application_digest",
            "machine_digest",
            "provider_digest",
            "stream_milestone",
            "recovery_intent",
        )
        if (
            prior.phase is not MachinePhase.RECOVERY_REQUIRED
            or prior.recovery_intent not in {RecoveryIntent.PRE_STREAM_RESTART, RecoveryIntent.POST_STREAM_SAFE_HOME}
            or successor.revision != 0
            or successor.phase is not MachinePhase.PREPARING_SESSION
            or successor.machine_session_epoch == prior.machine_session_epoch
            or any(getattr(successor, field) != getattr(prior, field) for field in frozen_fields)
        ):
            raise _error("MACHINE_TRANSFER_INVALID", "successor must preserve predecessor frozen authority")
        try:
            self._update_execution(expected_predecessor_revision, predecessor)
            self._insert_execution(successor)
            self._insert_event(predecessor, predecessor_event, predecessor_audit)
            self._insert_event(successor, successor_event, successor_audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def reconcile_restart(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
        outcome: MachineOutcome | None = None,
        job_transition: MachineJobOutcomeTransition | None = None,
        job_event: JobEvent | None = None,
        job_audit: AuditRecord | None = None,
    ) -> None:
        self._capability.require()
        prior = self._get_execution(execution.execution_id)
        if prior is None or prior.revision != expected_revision:
            raise _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed before reconciliation")
        repeated = prior.phase is MachinePhase.RECOVERY_REQUIRED
        if repeated:
            latest_event_at = self._connection.execute(
                "SELECT MAX(occurred_at) FROM machine_events WHERE execution_id = ?",
                (prior.execution_id,),
            ).fetchone()
            if (
                prior.retirement is not None
                or execution != prior
                or latest_event_at is None
                or latest_event_at[0] is None
                or event.occurred_at <= datetime.fromisoformat(str(latest_event_at[0]))
            ):
                raise _error(
                    "MACHINE_RECONCILIATION_INVALID",
                    "repeated reconciliation must preserve frozen recovery authority and advance event time",
                )
        else:
            ensure_revision_progression(prior, execution)
        if execution.phase is not MachinePhase.RECOVERY_REQUIRED or execution.recovery_evidence is None:
            raise _error("MACHINE_RECONCILIATION_INVALID", "restart reconciliation must freeze execution in recovery")
        job_values = (outcome, job_transition, job_event, job_audit)
        has_job_outcome = any(value is not None for value in job_values)
        if has_job_outcome and any(value is None for value in job_values):
            raise _error(
                "MACHINE_RECONCILIATION_INVALID",
                "restart job outcome authority must be supplied all-or-none",
            )
        if repeated and has_job_outcome:
            raise _error(
                "MACHINE_RECONCILIATION_INVALID",
                "repeated reconciliation cannot repeat the durable job outcome",
            )
        accepted_outcome: MachineOutcome | None = None
        accepted_transition: MachineJobOutcomeTransition | None = None
        accepted_job_event: JobEvent | None = None
        accepted_job_audit: AuditRecord | None = None
        if has_job_outcome:
            accepted_outcome = cast(MachineOutcome, outcome)
            accepted_transition = cast(MachineJobOutcomeTransition, job_transition)
            accepted_job_event = cast(JobEvent, job_event)
            accepted_job_audit = cast(AuditRecord, job_audit)
            self._validate_outcome(execution, accepted_outcome, accepted_transition)
            job_row = self._connection.execute(
                """SELECT job_id, schema_version, kind, name, state, revision, request_json,
                          config_snapshot_json, blocker_json, error_json, ready_snapshot_json,
                          created_at, updated_at FROM jobs WHERE job_id = ?""",
                (accepted_transition.job_id,),
            ).fetchone()
            if job_row is None:
                raise _error("MACHINE_JOB_CONFLICT", "restart reconciliation job does not exist")
            current_job = _job_from_row(cast(tuple[object, ...], job_row))
            if (
                current_job.revision != accepted_transition.expected_revision
                or current_job.ready_snapshot != accepted_transition.ready_snapshot
                or accepted_job_event.job_id != current_job.job_id
                or accepted_job_event.job_revision != current_job.revision + 1
                or accepted_job_event.prior_state is not current_job.state
                or accepted_job_event.result_state is not accepted_transition.result_state
                or accepted_job_event.event_type != accepted_transition.event_type
            ):
                raise _error(
                    "MACHINE_JOB_CONFLICT",
                    "restart reconciliation job authority changed",
                )
            _assert_audit_matches_event(accepted_job_event, accepted_job_audit)
        changed_at = event.occurred_at.isoformat()
        try:
            self._connection.execute(
                """
                UPDATE machine_approval_challenges
                SET status = 'EXPIRED', status_changed_at = ?,
                    challenge_json = json_set(
                        challenge_json,
                        '$.status', 'EXPIRED',
                        '$.status_changed_at', ?
                    )
                WHERE execution_id = ? AND status = 'PENDING'
                """,
                (changed_at, changed_at, execution.execution_id),
            )
            self._connection.execute(
                """
                UPDATE machine_request_results
                SET invalidated_at = ?, invalidation_reason = 'RESTART_RECONCILIATION'
                WHERE execution_id = ? AND admission_id IS NOT NULL
                  AND claimed_at IS NULL AND invalidated_at IS NULL
                """,
                (changed_at, execution.execution_id),
            )
            if not repeated:
                self._update_execution(expected_revision, execution, outcome=accepted_outcome)
            if accepted_transition is not None:
                job_cursor = self._connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, revision = ?, ready_snapshot_json = ?, updated_at = ?
                    WHERE job_id = ? AND revision = ?
                    """,
                    (
                        accepted_transition.result_state.value,
                        accepted_transition.expected_revision + 1,
                        _canonical_json(accepted_transition.ready_snapshot.to_json()),
                        cast(MachineOutcome, accepted_outcome).occurred_at.isoformat(),
                        accepted_transition.job_id,
                        accepted_transition.expected_revision,
                    ),
                )
                if job_cursor.rowcount != 1:
                    raise _error("MACHINE_JOB_CONFLICT", "restart reconciliation job revision changed")
            self._insert_event(execution, event, audit)
            if accepted_job_event is not None and accepted_job_audit is not None:
                self._insert_job_event(accepted_job_event, accepted_job_audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def record_lifecycle_event(
        self,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        self._capability.require()
        current = self._get_execution(execution.execution_id)
        if (
            current != execution
            or execution.phase is not MachinePhase.RECOVERY_REQUIRED
            or execution.recovery_evidence is None
            or execution.retirement is not None
        ):
            raise _error(
                "MACHINE_LIFECYCLE_EVENT_INVALID",
                "lifecycle uncertainty must bind the exact active recovery execution",
            )
        try:
            self._insert_event(execution, event, audit)
        except sqlite3.IntegrityError as error:
            raise _conflict(error) from error

    def _insert_execution(self, execution: MachineExecution) -> None:
        self._connection.execute(
            f"INSERT INTO machine_executions ({_EXECUTION_COLUMNS}) VALUES ({', '.join('?' for _ in range(25))})",
            self._execution_values(execution),
        )

    def _update_execution(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        outcome: MachineOutcome | None = None,
    ) -> None:
        retirement = execution.retirement
        cursor = self._connection.execute(
            """
            UPDATE machine_executions
            SET revision = ?, phase = ?, stream_milestone = ?, recovery_intent = ?,
                recovery_evidence_json = ?, retirement_disposition = ?, retired_at = ?,
                successor_execution_id = ?,
                outcome_kind = COALESCE(?, outcome_kind),
                outcome_json = COALESCE(?, outcome_json),
                outcome_at = COALESCE(?, outcome_at),
                updated_at = ?
            WHERE execution_id = ? AND revision = ?
            """,
            (
                execution.revision,
                execution.phase.value,
                execution.stream_milestone.value,
                None if execution.recovery_intent is None else execution.recovery_intent.value,
                None if execution.recovery_evidence is None else _canonical_json(execution.recovery_evidence.to_json()),
                None if retirement is None else retirement.disposition.value,
                None if retirement is None else retirement.retired_at.isoformat(),
                None if retirement is None else retirement.successor_execution_id,
                None if outcome is None else outcome.kind.value,
                None if outcome is None else _canonical_json(self._outcome_json(outcome)),
                None if outcome is None else outcome.occurred_at.isoformat(),
                execution.updated_at.isoformat(),
                execution.execution_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise _error("MACHINE_REVISION_CONFLICT", "machine execution revision changed")

    def _insert_session(self, session: MachineSessionSnapshot) -> None:
        self._connection.execute(
            """
            INSERT INTO machine_sessions (execution_id, machine_session_epoch, snapshot_json, observed_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                session.execution_id,
                session.machine_session_epoch,
                _canonical_json(_session_json(session)),
                session.observed_at.isoformat(),
            ),
        )

    def _insert_event(
        self,
        execution: MachineExecution,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None:
        if (
            event.execution_id != execution.execution_id
            or event.job_id != execution.job_id
            or event.execution_revision != execution.revision
            or event.phase is not execution.phase
            or audit.execution_id != execution.execution_id
            or audit.job_id != execution.job_id
            or audit.execution_revision != execution.revision
            or (type(audit) is MachineAuditPayloadV2 and audit.frozen_ready_revision != execution.ready_revision)
            or audit.event_id != event.event_id
            or audit.request_id != event.payload.request_id
        ):
            raise _error("MACHINE_EVENT_BINDING_MISMATCH", "machine event and audit must match execution revision")
        self._connection.execute(
            """
            INSERT INTO machine_events (event_id, execution_id, execution_revision, event_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.execution_id,
                event.execution_revision,
                _canonical_json(event.to_json()),
                event.occurred_at.isoformat(),
            ),
        )
        self._connection.execute(
            """INSERT INTO audit_events (event_id, event_type, request_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                audit.event_id,
                event.event_type,
                audit.request_id,
                _canonical_json(audit.to_json()),
                event.occurred_at.isoformat(),
            ),
        )

    def _insert_job_event(self, event: JobEvent, audit: AuditRecord) -> None:
        event_json = event.to_json()
        self._connection.execute(
            """
            INSERT INTO job_events (
                event_id, job_id, job_revision, event_type, prior_state,
                result_state, request_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self._connection.execute(
            """INSERT INTO audit_events (event_id, event_type, request_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                audit.event_id,
                audit.event_type,
                audit.request_id,
                _canonical_json(_plain_audit_payload(audit)),
                event.created_at.isoformat(),
            ),
        )

    def _insert_challenge(self, challenge: MachineApprovalChallenge) -> None:
        consumer = challenge.consumer
        self._connection.execute(
            """
            INSERT INTO machine_approval_challenges (
                challenge_id, execution_id, execution_revision, action, status,
                challenge_json, requester_uid, requester_gid, requester_pid,
                operator_uid, operator_gid, consumer_uid, consumer_gid, consumer_pid,
                issued_at, expires_at, status_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                challenge.challenge_id,
                challenge.binding.execution_id,
                challenge.binding.execution_revision,
                challenge.binding.action.value,
                challenge.status.value,
                _canonical_json(challenge.to_json()),
                challenge.requester.uid,
                challenge.requester.gid,
                challenge.requester.pid,
                challenge.operator_principal.uid,
                challenge.operator_principal.gid,
                None if consumer is None else consumer.uid,
                None if consumer is None else consumer.gid,
                None if consumer is None else consumer.pid,
                challenge.issued_at.isoformat(),
                challenge.expires_at.isoformat(),
                None if challenge.status_changed_at is None else challenge.status_changed_at.isoformat(),
            ),
        )

    def _get_execution(self, execution_id: str) -> MachineExecution | None:
        row = self._connection.execute(
            f"SELECT {_EXECUTION_COLUMNS} FROM machine_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return None if row is None else _execution_from_row(cast(tuple[object, ...], row))

    def _require_current_approval_binding(self, challenge: MachineApprovalChallenge) -> MachineExecution:
        binding = challenge.binding
        execution = self._get_execution(binding.execution_id)
        if (
            execution is None
            or execution.revision != binding.execution_revision
            or execution.phase is not binding.required_prior_phase
            or execution.retirement is not None
            or execution.phase is MachinePhase.COMPLETED
            or (binding.action is not MachineAction.RECOVER and execution.service_epoch != binding.service_epoch)
            or execution.machine_session_epoch != binding.machine_session_epoch
            or execution.job_id != binding.job_id
            or execution.ready_revision != binding.ready_revision
            or execution.application_digest != binding.application_digest
            or execution.machine_digest != binding.machine_digest
            or execution.provider_digest != binding.provider_digest
            or execution.gcode_sha256 != binding.gcode_sha256
        ):
            raise _error("MACHINE_APPROVAL_STALE", "machine approval no longer matches current execution authority")
        return execution

    def _get_request(self, key: MachineRequestKey) -> MachineRequestRecord | None:
        row = self._connection.execute(
            f"""SELECT {_REQUEST_COLUMNS} FROM machine_request_results
                WHERE principal_uid = ? AND principal_gid = ? AND request_id = ?""",
            (key.principal_uid, key.principal_gid, key.request_id),
        ).fetchone()
        return None if row is None else _request_from_row(cast(tuple[object, ...], row))

    @staticmethod
    def _execution_values(execution: MachineExecution) -> tuple[object, ...]:
        retirement = execution.retirement
        return (
            execution.execution_id,
            execution.schema_version,
            execution.job_id,
            execution.ready_revision,
            execution.revision,
            execution.phase.value,
            execution.service_epoch,
            execution.machine_session_epoch,
            execution.gcode_sha256,
            execution.application_version,
            execution.application_digest,
            execution.machine_digest,
            execution.provider_digest,
            execution.stream_milestone.value,
            None if execution.recovery_intent is None else execution.recovery_intent.value,
            execution.predecessor_execution_id,
            None if execution.recovery_evidence is None else _canonical_json(execution.recovery_evidence.to_json()),
            None if retirement is None else retirement.disposition.value,
            None if retirement is None else retirement.retired_at.isoformat(),
            None if retirement is None else retirement.successor_execution_id,
            None,
            None,
            None,
            execution.created_at.isoformat(),
            execution.updated_at.isoformat(),
        )

    @staticmethod
    def _request_values(record: MachineRequestRecord) -> tuple[object, ...]:
        return (
            record.key.principal_uid,
            record.key.principal_gid,
            record.key.request_id,
            record.requester_pid,
            record.command,
            record.argument_digest,
            record.execution_id,
            record.action,
            record.execution_revision,
            record.service_epoch,
            record.machine_session_epoch,
            None if record.recovery_disposition is None else record.recovery_disposition.value,
            None if record.recovery_intent is None else record.recovery_intent.value,
            record.predecessor_execution_id,
            record.admission_id,
            record.dispatch_nonce,
            None if record.response is None else _canonical_json(record.response),
            record.reserved_at.isoformat(),
            None if record.completed_at is None else record.completed_at.isoformat(),
            None if record.claimed_at is None else record.claimed_at.isoformat(),
            None if record.invalidated_at is None else record.invalidated_at.isoformat(),
            record.invalidation_reason,
        )

    @staticmethod
    def _outcome_json(outcome: MachineOutcome) -> JsonObject:
        return {
            "schema_version": outcome.schema_version,
            "execution_id": outcome.execution_id,
            "job_id": outcome.job_id,
            "execution_revision": outcome.execution_revision,
            "kind": outcome.kind.value,
            "ready_snapshot": outcome.ready_snapshot.to_json(),
            "recovery_evidence": (None if outcome.recovery_evidence is None else outcome.recovery_evidence.to_json()),
            "occurred_at": outcome.occurred_at.isoformat(),
        }

    @staticmethod
    def _validate_outcome(
        execution: MachineExecution,
        outcome: MachineOutcome,
        transition: MachineJobOutcomeTransition,
    ) -> None:
        if (
            outcome.execution_id != execution.execution_id
            or outcome.job_id != execution.job_id
            or outcome.execution_revision != execution.revision
            or outcome.ready_snapshot != transition.ready_snapshot
            or outcome.job_state is not transition.result_state
            or execution.phase.value != outcome.kind.value
        ):
            raise _error("MACHINE_OUTCOME_BINDING_MISMATCH", "machine outcome authority does not match execution/job")

    @staticmethod
    def _validate_request(record: MachineRequestRecord) -> None:
        if (
            type(record.key.principal_uid) is not int
            or record.key.principal_uid < 0
            or type(record.key.principal_gid) is not int
            or record.key.principal_gid < 0
            or type(record.requester_pid) is not int
            or record.requester_pid <= 0
            or not record.key.request_id
            or not record.command
            or len(record.argument_digest) != 64
        ):
            raise _error("MACHINE_REQUEST_INVALID", "machine request reservation is invalid")
        binding_fields = (
            record.execution_id,
            record.action,
            record.execution_revision,
            record.service_epoch,
            record.machine_session_epoch,
        )
        has_binding = record.execution_id is not None
        has_capability = record.admission_id is not None
        if (
            record.claimed_at is not None
            or record.invalidated_at is not None
            or record.invalidation_reason is not None
            or any(value is None for value in binding_fields) != (not has_binding)
            or (record.dispatch_nonce is None) != (not has_capability)
            or (has_capability and not has_binding)
        ):
            raise _error("MACHINE_REQUEST_INVALID", "machine request must reserve one initially unclaimed capability")
        if has_binding:
            if type(record.execution_revision) is not int or record.execution_revision < 0:
                raise _error("MACHINE_REQUEST_INVALID", "machine admission revision must be a non-negative integer")
            if any(
                type(value) is not str or not value
                for value in (record.execution_id, record.action, record.service_epoch, record.machine_session_epoch)
            ):
                raise _error("MACHINE_REQUEST_INVALID", "machine admission binding strings must be non-empty")
            try:
                action = MachineAction(cast(str, record.action))
            except ValueError as error:
                raise _error("MACHINE_REQUEST_INVALID", "machine admission action is unknown") from error
            recovery_fields = (
                record.recovery_disposition,
                record.recovery_intent,
                record.predecessor_execution_id,
            )
            if action is MachineAction.RECOVER:
                if (
                    type(record.recovery_disposition) is not RecoveryDisposition
                    or type(record.recovery_intent) is not RecoveryIntent
                    or not allowed_recovery_disposition(record.recovery_intent, record.recovery_disposition)
                    or (
                        has_capability
                        and (
                            record.recovery_disposition is RecoveryDisposition.RELEASE
                            or type(record.predecessor_execution_id) is not str
                            or not record.predecessor_execution_id
                        )
                    )
                    or (not has_capability and record.predecessor_execution_id is not None)
                ):
                    raise _error("MACHINE_REQUEST_INVALID", "RECOVER request authority is inconsistent")
            elif any(value is not None for value in recovery_fields):
                raise _error("MACHINE_REQUEST_INVALID", "only RECOVER may bind recovery authority")
        elif any(
            value is not None
            for value in (
                record.recovery_disposition,
                record.recovery_intent,
                record.predecessor_execution_id,
            )
        ):
            raise _error("MACHINE_REQUEST_INVALID", "unbound request cannot carry recovery authority")
        if has_capability and (
            type(record.admission_id) is not str
            or not record.admission_id
            or type(record.dispatch_nonce) is not str
            or not record.dispatch_nonce
        ):
            raise _error("MACHINE_REQUEST_INVALID", "machine admission ID and nonce must be non-empty strings")

    @staticmethod
    def _validate_admission_capability(capability: MachineAdmissionCapability) -> None:
        if (
            type(capability.key.principal_uid) is not int
            or capability.key.principal_uid < 0
            or type(capability.key.principal_gid) is not int
            or capability.key.principal_gid < 0
            or type(capability.key.request_id) is not str
            or not capability.key.request_id
            or type(capability.action) is not MachineAction
            or type(capability.execution_revision) is not int
            or capability.execution_revision < 0
            or any(
                type(value) is not str or not value
                for value in (
                    capability.admission_id,
                    capability.dispatch_nonce,
                    capability.execution_id,
                    capability.service_epoch,
                    capability.machine_session_epoch,
                )
            )
        ):
            raise _error("MACHINE_ADMISSION_MISMATCH", "machine admission capability is malformed")
        recovery_fields = (
            capability.recovery_disposition,
            capability.recovery_intent,
            capability.predecessor_execution_id,
        )
        if capability.action is MachineAction.RECOVER:
            if (
                type(capability.recovery_disposition) is not RecoveryDisposition
                or capability.recovery_disposition is RecoveryDisposition.RELEASE
                or type(capability.recovery_intent) is not RecoveryIntent
                or not allowed_recovery_disposition(capability.recovery_intent, capability.recovery_disposition)
                or type(capability.predecessor_execution_id) is not str
                or not capability.predecessor_execution_id
            ):
                raise _error("MACHINE_ADMISSION_MISMATCH", "recovery admission capability is malformed")
        elif any(value is not None for value in recovery_fields):
            raise _error("MACHINE_ADMISSION_MISMATCH", "non-recovery capability carries recovery authority")


class SQLiteMachineRepository:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._connection_thread: int | None = None

    def initialize(self, *, applied_at: str) -> int:
        return migrate(self._get_connection(), applied_at=applied_at)

    def schema_tables(self) -> frozenset[str]:
        rows = (
            self._get_connection()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'machine_%'")
            .fetchall()
        )
        return frozenset(str(row[0]) for row in rows)

    @contextmanager
    def transaction(self) -> Iterator[MachineRepositoryTransaction]:
        connection = self._get_connection()
        capability = _Capability()
        connection.execute("BEGIN IMMEDIATE")
        try:
            try:
                yield _MachineTransaction(connection, capability)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            capability.active = False

    def get_execution(self, execution_id: str) -> MachineExecution | None:
        row = (
            self._get_connection()
            .execute(
                f"SELECT {_EXECUTION_COLUMNS} FROM machine_executions WHERE execution_id = ?",
                (execution_id,),
            )
            .fetchone()
        )
        return None if row is None else _execution_from_row(cast(tuple[object, ...], row))

    def get_active_execution(self) -> MachineExecution | None:
        rows = (
            self._get_connection()
            .execute(
                f"""SELECT {_EXECUTION_COLUMNS} FROM machine_executions
                WHERE retired_at IS NULL AND phase != 'COMPLETED'"""
            )
            .fetchall()
        )
        if len(rows) > 1:
            raise _error("MACHINE_PERSISTENCE_CORRUPT", "more than one global active machine owner exists")
        return None if not rows else _execution_from_row(cast(tuple[object, ...], rows[0]))

    def get_session(self, execution_id: str) -> MachineSessionSnapshot | None:
        row = (
            self._get_connection()
            .execute(
                "SELECT snapshot_json FROM machine_sessions WHERE execution_id = ?",
                (execution_id,),
            )
            .fetchone()
        )
        return None if row is None else _session_from_json(str(row[0]))

    def get_outcome(self, execution_id: str) -> MachineOutcome | None:
        row = (
            self._get_connection()
            .execute(
                "SELECT outcome_json FROM machine_executions WHERE execution_id = ?",
                (execution_id,),
            )
            .fetchone()
        )
        if row is None or row[0] is None:
            return None
        return _outcome_from_json(str(row[0]))

    def get_challenge(self, challenge_id: str) -> MachineApprovalChallenge | None:
        row = (
            self._get_connection()
            .execute(
                """
            SELECT challenge_json, requester_uid, requester_gid, requester_pid,
                   operator_uid, operator_gid, consumer_uid, consumer_gid, consumer_pid
            FROM machine_approval_challenges WHERE challenge_id = ?
            """,
                (challenge_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return _challenge_from_row(cast(tuple[object, ...], row))

    def get_request(self, key: MachineRequestKey) -> MachineRequestRecord | None:
        row = (
            self._get_connection()
            .execute(
                f"""SELECT {_REQUEST_COLUMNS} FROM machine_request_results
                WHERE principal_uid = ? AND principal_gid = ? AND request_id = ?""",
                (key.principal_uid, key.principal_gid, key.request_id),
            )
            .fetchone()
        )
        return None if row is None else _request_from_row(cast(tuple[object, ...], row))

    def list_events(self, execution_id: str) -> tuple[MachineEvent, ...]:
        rows = (
            self._get_connection()
            .execute(
                "SELECT event_json FROM machine_events WHERE execution_id = ? ORDER BY rowid",
                (execution_id,),
            )
            .fetchall()
        )
        return tuple(MachineEvent.from_json(_json_object(str(row[0]))) for row in rows)

    def list_progress(self, execution_id: str) -> tuple[MachineProgress, ...]:
        rows = (
            self._get_connection()
            .execute(
                "SELECT progress_json FROM machine_progress_events WHERE execution_id = ? ORDER BY rowid",
                (execution_id,),
            )
            .fetchall()
        )
        return tuple(_progress_from_json(str(row[0])) for row in rows)

    def close(self) -> None:
        if self._connection is None:
            return
        self._require_connection_thread()
        self._connection.close()
        self._connection = None
        self._connection_thread = None

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
            self._connection_thread = threading.get_ident()
        else:
            self._require_connection_thread()
        return self._connection

    def _require_connection_thread(self) -> None:
        if self._connection_thread != threading.get_ident():
            raise _error(
                "MACHINE_REPOSITORY_THREAD_VIOLATION",
                "SQLite machine connection is owned by a different coordinator thread",
            )
