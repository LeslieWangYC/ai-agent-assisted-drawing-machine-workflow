from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from drawingmachine.json_types import JsonObject

if TYPE_CHECKING:
    from drawingmachine.domain.jobs import AuditRecord, JobEvent, JobRecord
    from drawingmachine.domain.jobs.models import MachineJobOutcomeTransition
    from drawingmachine.domain.machine import (
        MachineAction,
        MachineApprovalChallenge,
        MachineApprovalDecision,
        MachineAuditPayloadV1,
        MachineAuditPayloadV2,
        MachineEvent,
        MachineExecution,
        MachineOutcome,
        MachineProgress,
        MachineSessionSnapshot,
        RecoveryDisposition,
        RecoveryIntent,
    )

    MachineAuditPayload = MachineAuditPayloadV1 | MachineAuditPayloadV2


@dataclass(frozen=True, slots=True)
class MachineRequestKey:
    principal_uid: int
    principal_gid: int
    request_id: str


@dataclass(frozen=True, slots=True)
class MachineRequestRecord:
    key: MachineRequestKey
    requester_pid: int
    command: str
    argument_digest: str
    execution_id: str | None
    action: str | None
    admission_id: str | None
    dispatch_nonce: str | None
    response: JsonObject | None
    reserved_at: datetime
    completed_at: datetime | None = None
    claimed_at: datetime | None = None
    execution_revision: int | None = None
    service_epoch: str | None = None
    machine_session_epoch: str | None = None
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    recovery_disposition: RecoveryDisposition | None = None
    recovery_intent: RecoveryIntent | None = None
    predecessor_execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class MachineAdmissionCapability:
    key: MachineRequestKey
    admission_id: str
    dispatch_nonce: str
    execution_id: str
    action: MachineAction
    execution_revision: int
    service_epoch: str
    machine_session_epoch: str
    recovery_disposition: RecoveryDisposition | None = None
    recovery_intent: RecoveryIntent | None = None
    predecessor_execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class MachineRequestReservation:
    record: MachineRequestRecord
    created: bool


class MachineRepositoryTransaction(Protocol):
    def get_job(self, job_id: str) -> JobRecord | None: ...

    def get_pending_challenge(
        self,
        execution_id: str,
        action: MachineAction,
    ) -> MachineApprovalChallenge | None: ...

    def get_request(self, key: MachineRequestKey) -> MachineRequestRecord | None: ...

    def get_challenge(self, challenge_id: str) -> MachineApprovalChallenge | None: ...

    def create_execution(
        self,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None: ...

    def record_opened_session(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        session: MachineSessionSnapshot,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None: ...

    def transition_execution(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None: ...

    def issue_challenge(
        self,
        challenge: MachineApprovalChallenge,
        *,
        replacement: MachineApprovalDecision | None = None,
        event: MachineEvent | None = None,
        audit: MachineAuditPayload | None = None,
    ) -> None: ...

    def decide_challenge(self, decision: MachineApprovalDecision) -> None: ...

    def reserve_request(self, record: MachineRequestRecord) -> MachineRequestReservation: ...

    def complete_request(
        self,
        key: MachineRequestKey,
        *,
        response: JsonObject,
        completed_at: datetime,
    ) -> MachineRequestRecord: ...

    def claim_request(
        self,
        capability: MachineAdmissionCapability,
        *,
        claimed_at: datetime,
    ) -> MachineRequestRecord: ...

    def append_progress(self, progress_id: str, progress: MachineProgress) -> None: ...

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
    ) -> None: ...

    def release_owner(
        self,
        expected_revision: int,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None: ...

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
    ) -> None: ...

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
    ) -> None: ...

    def record_lifecycle_event(
        self,
        execution: MachineExecution,
        *,
        event: MachineEvent,
        audit: MachineAuditPayload,
    ) -> None: ...


class MachineRepository(Protocol):
    def initialize(self, *, applied_at: str) -> int: ...

    def transaction(self) -> AbstractContextManager[MachineRepositoryTransaction]: ...

    def get_execution(self, execution_id: str) -> MachineExecution | None: ...

    def get_active_execution(self) -> MachineExecution | None: ...

    def get_session(self, execution_id: str) -> MachineSessionSnapshot | None: ...

    def get_outcome(self, execution_id: str) -> MachineOutcome | None: ...

    def get_challenge(self, challenge_id: str) -> MachineApprovalChallenge | None: ...

    def get_request(self, key: MachineRequestKey) -> MachineRequestRecord | None: ...

    def list_events(self, execution_id: str) -> tuple[MachineEvent, ...]: ...

    def list_progress(self, execution_id: str) -> tuple[MachineProgress, ...]: ...

    def close(self) -> None: ...
