from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import StagingAdmissionRecordV1, StagingRecoveryReasonCode

if TYPE_CHECKING:
    from drawingmachine.domain.jobs import (
        ArtifactRef,
        AuditRecord,
        JobEvent,
        JobRecord,
        JobState,
        JobTransition,
    )


@dataclass(frozen=True, slots=True)
class RepositoryHealth:
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool


class RepositoryTransaction(Protocol):
    def create_job(
        self,
        job: JobRecord,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> None: ...

    def create_job_with_staging(
        self,
        job: JobRecord,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
        request_id: str,
        expected_staging_revision: int,
    ) -> StagingAdmissionRecordV1: ...

    def transition_job(
        self,
        transition: JobTransition,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> JobRecord: ...

    def append_audit(
        self,
        event_id: str,
        event_type: str,
        request_id: str | None,
        payload: JsonObject,
    ) -> None: ...


class Repository(Protocol):
    def initialize(self) -> int: ...

    def health(self) -> RepositoryHealth: ...

    def transaction(self) -> AbstractContextManager[RepositoryTransaction]: ...

    def append_audit(
        self,
        event_id: str,
        event_type: str,
        request_id: str | None,
        payload: JsonObject,
    ) -> None: ...

    def bind_staging_request_envelope(
        self,
        event_id: str,
        request_id: str,
        role: str,
        envelope_sha256: str,
    ) -> str: ...

    def get_job(self, job_id: str) -> JobRecord | None: ...

    def register_decoded_staging(self, record: StagingAdmissionRecordV1) -> None: ...

    def get_staging_admission(self, request_id: str) -> StagingAdmissionRecordV1 | None: ...

    def list_staging_admissions(self) -> tuple[StagingAdmissionRecordV1, ...]: ...

    def mark_staging_claimed(
        self,
        request_id: str,
        *,
        expected_revision: int,
    ) -> StagingAdmissionRecordV1: ...

    def mark_staging_consumed(
        self,
        request_id: str,
        *,
        expected_revision: int,
    ) -> StagingAdmissionRecordV1: ...

    def record_staging_recovery(
        self,
        request_id: str,
        *,
        expected_revision: int,
        recovery_code: StagingRecoveryReasonCode,
        quarantined: bool,
    ) -> StagingAdmissionRecordV1: ...

    def list_jobs_in_states(self, states: frozenset[JobState]) -> tuple[JobRecord, ...]: ...

    def list_artifacts(self, job_id: str) -> tuple[ArtifactRef, ...]: ...

    def list_job_events(self, job_id: str) -> tuple[JobEvent, ...]: ...

    def close(self) -> None: ...
