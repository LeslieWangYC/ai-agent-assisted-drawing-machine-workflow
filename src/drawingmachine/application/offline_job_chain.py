from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast
from uuid import uuid4

from drawingmachine import __version__
from drawingmachine.application.jobs import (
    ReviewContinuationV1,
    RouteMode,
    WorkflowSubmissionV1,
    semantic_assessment_to_json,
)
from drawingmachine.config import ConfigBundle
from drawingmachine.domain.gcode import parse_machine_build_profile
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditPayloadV1,
    AuditRecord,
    ConfigSnapshot,
    JobBlocker,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    JobTransition,
    ReadyToRunSnapshot,
    RequesterIdentity,
    allowed_transition,
)
from drawingmachine.domain.jobs.artifact_contracts import (
    ArtifactContractContext,
    validate_gcode_safety_bundle,
    validate_json_artifact,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import (
    MAX_GCODE_IMPORT_BYTES,
    MAX_IMAGE_IMPORT_BYTES,
    ArtifactStore,
    ExpectedArtifact,
    PromotedBundle,
    WorkerArtifact,
)
from drawingmachine.ports.client_data import StagingAdmissionRecordV1, StagingAdmissionState, staging_job_id
from drawingmachine.ports.clock import Clock
from drawingmachine.ports.repository import Repository
from drawingmachine.ports.workers import (
    WorkerInputArtifact,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerSupervisor,
    WorkerTaskV1,
)

_TERMINAL = frozenset({JobState.READY_TO_RUN, JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED})
_INFLIGHT = frozenset(
    {
        JobState.QUEUED,
        JobState.PREPARING_IMAGE,
        JobState.PLANNING_PATHS,
        JobState.BUILDING_GCODE,
        JobState.VALIDATING_GCODE,
        JobState.IMAGE_READY,
        JobState.PATH_PLAN_READY,
    }
)
_REHYDRATABLE = frozenset({JobState.BLOCKED})
_WORKER_CRASH_CODE = "WORKER_PROCESS_CRASHED"
_SERVICE = RequesterIdentity("SERVICE", None, None, None)
_MAX_JSON_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_GCODE_VALIDATION_BYTES = 64 * 1024 * 1024


class _CoordinatorCommitAbandoned(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _WorkerResult:
    outcome: WorkerOutcomeV1
    bundle: PromotedBundle | None


_ARTIFACT_SPECS: dict[WorkerKind, dict[str, tuple[str, str]]] = {
    WorkerKind.IMAGE_PREPARE: {
        "route_decision": ("route_decision.json", "application/json"),
        "processed_image_raw": ("processed_image_raw.png", "image/png"),
        "direct_report": ("direct_report.json", "application/json"),
        "processed_image": ("processed_image.png", "image/png"),
        "normalization_report": ("normalization_report.json", "application/json"),
    },
    WorkerKind.PROVIDER_LOCAL_COMFYUI: {
        "provider_request": ("provider_request.json", "application/json"),
        "provider_response": ("provider_response.json", "application/json"),
        "provider_handoff": ("provider_handoff.json", "application/json"),
        "processed_image_raw": ("processed_image_raw.png", "image/png"),
    },
    WorkerKind.PATHS_PLAN: {
        "complexity_report": ("complexity_report.json", "application/json"),
        "path_plan": ("path_plan.json", "application/json"),
        "preview_stroke_svg": ("preview_stroke.svg", "image/svg+xml"),
        "preview_final_svg": ("preview_final.svg", "image/svg+xml"),
        "planning_report": ("planning_report.md", "text/markdown; charset=utf-8"),
    },
    WorkerKind.GCODE_BUILD: {
        "selected_path_plan": ("selected_path_plan.json", "application/json"),
        "gcode": ("drawing.gcode", "text/x.gcode"),
        "gcode_build_report": ("gcode_build_report.md", "text/markdown; charset=utf-8"),
        "gcode_preview_svg": ("gcode_preview.svg", "image/svg+xml"),
        "gcode_preview_report": ("gcode_preview_report.json", "application/json"),
    },
    WorkerKind.GCODE_CHECK: {
        "gcode_static": ("gcode_static.json", "application/json"),
        "send_plan": ("send_plan.json", "application/json"),
        "readiness": ("readiness.json", "application/json"),
    },
}

_SUCCESS_ROLES: dict[tuple[WorkerKind, str | None], frozenset[str]] = {
    (WorkerKind.IMAGE_PREPARE, "direct"): frozenset(
        {"route_decision", "processed_image_raw", "direct_report", "processed_image", "normalization_report"}
    ),
    (WorkerKind.IMAGE_PREPARE, "normalize_existing"): frozenset({"processed_image", "normalization_report"}),
    (WorkerKind.PROVIDER_LOCAL_COMFYUI, None): frozenset(
        {"provider_request", "provider_response", "provider_handoff", "processed_image_raw"}
    ),
    (WorkerKind.PATHS_PLAN, None): frozenset(
        {"complexity_report", "path_plan", "preview_stroke_svg", "preview_final_svg", "planning_report"}
    ),
    (WorkerKind.GCODE_BUILD, None): frozenset(
        {"selected_path_plan", "gcode", "gcode_build_report", "gcode_preview_svg", "gcode_preview_report"}
    ),
    (WorkerKind.GCODE_CHECK, None): frozenset({"gcode_static", "send_plan", "readiness"}),
}

_BLOCKED_ROLES: dict[WorkerKind, frozenset[str]] = {
    WorkerKind.IMAGE_PREPARE: frozenset(),
    WorkerKind.PROVIDER_LOCAL_COMFYUI: frozenset(),
    WorkerKind.PATHS_PLAN: frozenset({"complexity_report"}),
    WorkerKind.GCODE_BUILD: frozenset(),
    WorkerKind.GCODE_CHECK: frozenset({"gcode_static", "send_plan", "readiness"}),
}

_ROUTE_FIELDS = frozenset(
    {
        "schema",
        "job_name",
        "input_image",
        "decided_at",
        "decider",
        "route",
        "confidence",
        "image_category",
        "rationale",
        "risk_flags",
        "blocking_risks",
        "direct_route_allowed",
        "next_stage",
        "hardware_touched",
        "semantic_style_score",
        "semantic_score_source",
        "semantic_rationale",
        "semantic_blockers",
        "visual_direct_score",
        "visual_score_source",
        "direct_score",
        "route_override",
        "subject_mask_ratio",
        "binary_transition_density",
        "background_simplicity_score",
        "subject_background_separation_score",
        "visual_scores",
        "stats",
        "classification",
    }
)


class OfflineJobChain:
    def __init__(
        self,
        *,
        repository: Repository,
        artifacts: ArtifactStore,
        workers: WorkerSupervisor,
        config: ConfigBundle,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._workers = workers
        self._config = config
        self._clock = clock
        self._absolute_artifacts: dict[str, dict[str, Path]] = {}
        self._artifact_metadata: dict[str, dict[str, ArtifactRef]] = {}
        self._cancellation: dict[str, threading.Event] = {}
        self._cancellation_metadata: dict[str, tuple[str, RequesterIdentity]] = {}
        self._cancellation_lock = threading.Lock()

    def initialize(self) -> int:
        version = self._repository.initialize()
        self._recover_uncommitted_staging_promotions()
        for job in self._repository.list_jobs_in_states(_REHYDRATABLE):
            if job.blocker is None or job.blocker.code != "REVIEW_REQUIRED":
                continue
            try:
                self._rehydrate_artifacts(job)
            except DrawingMachineError:
                self._fail(
                    job,
                    "ARTIFACT_REHYDRATION_FAILED",
                    "persisted job artifacts could not be safely rehydrated",
                )
        return version

    def register_cancellation(self, job_id: str, token: threading.Event) -> None:
        with self._cancellation_lock:
            self._cancellation[job_id] = token

    def register_cancellation_request(
        self,
        job_id: str,
        token: threading.Event,
        request_id: str,
        requester: RequesterIdentity,
    ) -> None:
        with self._cancellation_lock:
            self._cancellation[job_id] = token
            self._cancellation_metadata[job_id] = (request_id, requester)
            token.set()

    def submit(
        self,
        submission: WorkflowSubmissionV1,
        *,
        request_id: str,
        job_id: str,
        requester: RequesterIdentity,
    ) -> JobRecord:
        job = self._coordinator_submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
            commit_allowed=lambda: True,
            committed=lambda _job: None,
        )
        assert job is not None
        self._write_durable_projection(job)
        return job

    def _coordinator_submit(
        self,
        submission: WorkflowSubmissionV1,
        *,
        request_id: str,
        job_id: str,
        requester: RequesterIdentity,
        commit_allowed: Callable[[], bool],
        committed: Callable[[JobRecord], None],
    ) -> JobRecord | None:
        if self._repository.get_job(job_id) is not None:
            self._raise("JOB_ALREADY_EXISTS", "job already exists", job_id=job_id)
        now = self._clock.now()
        request: JsonObject = {
            "input_path": str(submission.input_path),
            "route_mode": submission.route_mode.value,
            "semantic_assessment": semantic_assessment_to_json(submission.semantic_assessment),
            "prompt": submission.prompt,
        }
        job = JobRecord(
            1,
            job_id,
            JobKind.OFFLINE_WORKFLOW,
            submission.job_name,
            JobState.QUEUED,
            0,
            request,
            self._config_snapshot(),
            None,
            None,
            None,
            now,
            now,
        )
        event, audit = self._event_pair(job, None, JobState.QUEUED, "job.queued", request_id, requester)
        staging = self._get_staging_admission(request_id)
        initial_artifacts: tuple[ArtifactRef, ...] = ()
        imported: PromotedBundle | None = None
        if not commit_allowed():
            return None
        if staging is not None:
            if staging.state is not StagingAdmissionState.CLAIMED or staging.role.value != "image":
                self._raise("STAGING_ADMISSION_INVALID", "workflow staging authority is not claimed", job_id=job_id)
            initial_artifacts, imported = self._import_or_recover_staged(
                job.job_id,
                request_id,
                submission.input_path,
                staging,
                role="input_image",
                relative_path=f"inputs/{submission.input_path.name}",
                media_type="image/png",
                max_bytes=MAX_IMAGE_IMPORT_BYTES,
            )
        if not commit_allowed():
            if imported is not None:
                self._discard_uncommitted_bundle(imported)
            return None
        try:
            with self._repository.transaction() as transaction:
                if staging is None:
                    transaction.create_job(job, artifacts=(), event=event, audit=audit)
                else:
                    transaction.create_job_with_staging(
                        job,
                        artifacts=initial_artifacts,
                        event=event,
                        audit=audit,
                        request_id=request_id,
                        expected_staging_revision=staging.revision,
                    )
                if not commit_allowed():
                    raise _CoordinatorCommitAbandoned
        except BaseException as error:
            if imported is not None:
                try:
                    self._discard_uncommitted_bundle(imported)
                except BaseException as cleanup_error:
                    raise cleanup_error from error
            if isinstance(error, _CoordinatorCommitAbandoned):
                return None
            raise
        if imported is not None:
            self._remember_bundle(imported)
        committed(job)
        return job

    def continue_review(
        self,
        continuation: ReviewContinuationV1,
        *,
        request_id: str,
        requester: RequesterIdentity,
    ) -> JobRecord:
        job = self._coordinator_continue_review(
            continuation,
            request_id=request_id,
            requester=requester,
            commit_allowed=lambda: True,
            committed=lambda _job: None,
        )
        assert job is not None
        self._write_durable_projection(job)
        return job

    def _coordinator_continue_review(
        self,
        continuation: ReviewContinuationV1,
        *,
        request_id: str,
        requester: RequesterIdentity,
        commit_allowed: Callable[[], bool],
        committed: Callable[[JobRecord], None],
    ) -> JobRecord | None:
        job = self.get(continuation.job_id)
        if job.state is not JobState.BLOCKED or job.blocker is None or job.blocker.code != "REVIEW_REQUIRED":
            self._raise("REVIEW_NOT_ALLOWED", "job is not blocked for processed-image review", job_id=job.job_id)
        if continuation.review.status != "PASS_TO_BUILD":
            self._raise(
                "REVIEW_NOT_APPROVED", "processed-image review did not approve path planning", job_id=job.job_id
            )
        processed = self._artifact(job.job_id, "processed_image")
        continuation.review.validate_for(
            job_name=job.name,
            processed_image_sha256=processed.sha256,
            reviewed_image_sha256=continuation.reviewed_image_sha256,
        )
        if not commit_allowed():
            return None
        try:
            updated = self._transition(
                job,
                JobState.IMAGE_READY,
                "job.review_accepted",
                request_id,
                requester,
                projection_deferred=True,
                commit_allowed=commit_allowed,
            )
        except _CoordinatorCommitAbandoned:
            return None
        committed(updated)
        return updated

    def submit_gcode_check(
        self,
        path: Path,
        *,
        request_id: str,
        job_id: str,
        requester: RequesterIdentity,
    ) -> JobRecord:
        job = self._coordinator_submit_gcode_check(
            path,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
            commit_allowed=lambda: True,
            committed=lambda _job: None,
        )
        assert job is not None
        self._write_durable_projection(job)
        return job

    def _coordinator_submit_gcode_check(
        self,
        path: Path,
        *,
        request_id: str,
        job_id: str,
        requester: RequesterIdentity,
        commit_allowed: Callable[[], bool],
        committed: Callable[[JobRecord], None],
    ) -> JobRecord | None:
        if not path.is_absolute():
            self._raise("GCODE_CHECK_INPUT_INVALID", "G-code candidate path must be absolute")
        if self._repository.get_job(job_id) is not None:
            self._raise("JOB_ALREADY_EXISTS", "job already exists", job_id=job_id)
        now = self._clock.now()
        request: JsonObject = {"input_path": str(path)}
        job = JobRecord(
            1,
            job_id,
            JobKind.GCODE_CHECK,
            path.name,
            JobState.QUEUED,
            0,
            request,
            self._config_snapshot(),
            None,
            None,
            None,
            now,
            now,
        )
        event, audit = self._event_pair(job, None, JobState.QUEUED, "gcode_check.queued", request_id, requester)
        staging = self._get_staging_admission(request_id)
        initial_artifacts: tuple[ArtifactRef, ...] = ()
        imported: PromotedBundle | None = None
        if not commit_allowed():
            return None
        if staging is not None:
            if staging.state is not StagingAdmissionState.CLAIMED or staging.role.value != "gcode":
                self._raise("STAGING_ADMISSION_INVALID", "G-code staging authority is not claimed", job_id=job_id)
            initial_artifacts, imported = self._import_or_recover_staged(
                job.job_id,
                request_id,
                path,
                staging,
                role="gcode",
                relative_path=f"inputs/{path.name}",
                media_type="text/x.gcode",
                max_bytes=MAX_GCODE_IMPORT_BYTES,
            )
        if not commit_allowed():
            if imported is not None:
                self._discard_uncommitted_bundle(imported)
            return None
        try:
            with self._repository.transaction() as transaction:
                if staging is None:
                    transaction.create_job(job, artifacts=(), event=event, audit=audit)
                else:
                    transaction.create_job_with_staging(
                        job,
                        artifacts=initial_artifacts,
                        event=event,
                        audit=audit,
                        request_id=request_id,
                        expected_staging_revision=staging.revision,
                    )
                if not commit_allowed():
                    raise _CoordinatorCommitAbandoned
        except BaseException as error:
            if imported is not None:
                try:
                    self._discard_uncommitted_bundle(imported)
                except BaseException as cleanup_error:
                    raise cleanup_error from error
            if isinstance(error, _CoordinatorCommitAbandoned):
                return None
            raise
        if imported is not None:
            self._remember_bundle(imported)
        committed(job)
        return job

    def _get_staging_admission(self, request_id: str) -> StagingAdmissionRecordV1 | None:
        try:
            getter = self._repository.get_staging_admission
        except AttributeError:
            return None
        return getter(request_id)

    def _recover_uncommitted_staging_promotions(self) -> None:
        try:
            admissions = self._repository.list_staging_admissions()
        except AttributeError:
            return
        for admission in admissions:
            if admission.state is not StagingAdmissionState.CLAIMED:
                continue
            job_id = staging_job_id(admission.request_id, admission.role)
            if self._repository.get_job(job_id) is not None:
                self._raise(
                    "STAGING_ADMISSION_INVALID",
                    "claimed staging authority unexpectedly has a durable job",
                    job_id=job_id,
                )
            attempt_id = f"input-{admission.request_id}"
            role = "input_image" if admission.role.value == "image" else "gcode"
            media_type = "image/png" if admission.role.value == "image" else "text/x.gcode"
            maximum = MAX_IMAGE_IMPORT_BYTES if admission.role.value == "image" else MAX_GCODE_IMPORT_BYTES
            expected = ArtifactRef(
                role,
                f"artifacts/{attempt_id}/inputs/payload",
                admission.source_sha256,
                admission.payload_identity.size_bytes,
                media_type,
            )
            try:
                self._artifacts.read_bytes(
                    job_id,
                    expected,
                    expected_media_type=media_type,
                    max_bytes=maximum,
                )
            except DrawingMachineError as error:
                if error.payload.code == "ARTIFACT_PATH_INVALID":
                    continue
                raise
            self._artifacts.discard_promoted_bundle(job_id, attempt_id, (expected,))

    def _discard_uncommitted_bundle(self, bundle: PromotedBundle) -> None:
        self._artifacts.discard_promoted_bundle(bundle.job_id, bundle.attempt_id, bundle.artifacts)

    def _import_or_recover_staged(
        self,
        job_id: str,
        request_id: str,
        source: Path,
        staging: StagingAdmissionRecordV1,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> tuple[tuple[ArtifactRef, ...], PromotedBundle | None]:
        attempt_id = f"input-{request_id}"
        expected = ArtifactRef(
            role,
            f"artifacts/{attempt_id}/{relative_path}",
            staging.source_sha256,
            staging.payload_identity.size_bytes,
            media_type,
        )
        try:
            self._artifacts.read_bytes(
                job_id,
                expected,
                expected_media_type=media_type,
                max_bytes=max_bytes,
            )
        except DrawingMachineError as error:
            if error.payload.code != "ARTIFACT_PATH_INVALID":
                raise
            imported = self._artifacts.import_file(
                job_id,
                attempt_id,
                source,
                role=role,
                relative_path=relative_path,
                media_type=media_type,
                max_bytes=max_bytes,
            )
            return imported.artifacts, imported
        return (expected,), None

    def _write_durable_projection(self, job: JobRecord) -> None:
        self._artifacts.write_projection(job, self._repository.list_artifacts(job.job_id))

    def advance(self, job_id: str) -> JobRecord:
        job = self.get(job_id)
        if self._cancel_requested(job_id):
            request_id, requester = self._cancellation_identity(job_id)
            return self.cancel(job_id, request_id=request_id, requester=requester)
        if job.kind is JobKind.GCODE_CHECK:
            return self._advance_gcode_check(job)
        if job.state is JobState.QUEUED:
            submission_path = Path(cast(str, job.request["input_path"]))
            if any(artifact.role == "input_image" for artifact in self._repository.list_artifacts(job.job_id)):
                self._rehydrate_artifacts(job)
                artifacts: tuple[ArtifactRef, ...] = ()
            else:
                bundle = self._artifacts.import_file(
                    job.job_id,
                    f"input-{uuid4()}",
                    submission_path,
                    role="input_image",
                    relative_path=f"inputs/{submission_path.name}",
                    media_type="image/png",
                    max_bytes=MAX_IMAGE_IMPORT_BYTES,
                )
                self._remember_bundle(bundle)
                artifacts = bundle.artifacts
            return self._transition(job, JobState.PREPARING_IMAGE, "job.image_preparing", None, _SERVICE, artifacts)
        if job.state is JobState.PREPARING_IMAGE:
            return self._prepare_image(job)
        if job.state is JobState.IMAGE_READY:
            return self._transition(job, JobState.PLANNING_PATHS, "job.path_planning", None, _SERVICE)
        if job.state is JobState.PLANNING_PATHS:
            result = self._execute(job, WorkerKind.PATHS_PLAN, ("processed_image",), self._planning_payload())
            return self._finish_worker(job, result, JobState.PATH_PLAN_READY, "job.path_plan_ready")
        if job.state is JobState.PATH_PLAN_READY:
            return self._transition(job, JobState.BUILDING_GCODE, "job.gcode_building", None, _SERVICE)
        if job.state is JobState.BUILDING_GCODE:
            result = self._execute(
                job,
                WorkerKind.GCODE_BUILD,
                ("path_plan",),
                {"machine_build_profile": self._machine_profile()},
            )
            return self._finish_worker(job, result, JobState.VALIDATING_GCODE, "job.gcode_validating")
        if job.state is JobState.VALIDATING_GCODE:
            gcode = self._artifact(job.job_id, "gcode")
            result = self._execute(
                job,
                WorkerKind.GCODE_CHECK,
                ("gcode",),
                {"machine_build_profile": self._machine_profile(), "expected_gcode_sha256": gcode.sha256},
            )
            if result.outcome.status is not WorkerStatus.SUCCEEDED:
                return self._finish_worker(job, result, JobState.READY_TO_RUN, "job.ready_to_run")
            assert result.bundle is not None
            all_artifacts = self._repository.list_artifacts(job.job_id) + result.bundle.artifacts
            by_role = {artifact.role: artifact for artifact in all_artifacts}
            snapshot = ReadyToRunSnapshot(
                1,
                job.job_id,
                job.revision + 1,
                by_role["gcode"],
                tuple(artifact for artifact in all_artifacts if artifact.role != "gcode"),
                self._read_json(result.bundle, "gcode_static"),
                self._read_json(result.bundle, "send_plan"),
                self._read_json(result.bundle, "readiness"),
                job.config,
                cast(JsonObject, self._config.machine.profile.get("planning", {})),
                __version__,
            )
            return self._transition(
                job,
                JobState.READY_TO_RUN,
                "job.ready_to_run",
                None,
                _SERVICE,
                result.bundle.artifacts,
                ready_snapshot=snapshot,
            )
        return job

    def _advance_gcode_check(self, job: JobRecord) -> JobRecord:
        if job.state is JobState.QUEUED:
            source = Path(cast(str, job.request["input_path"]))
            artifacts: tuple[ArtifactRef, ...]
            if any(artifact.role == "gcode" for artifact in self._repository.list_artifacts(job.job_id)):
                self._rehydrate_artifacts(job)
                artifacts = ()
            else:
                bundle = self._artifacts.import_file(
                    job.job_id,
                    f"input-{uuid4()}",
                    source,
                    role="gcode",
                    relative_path=f"inputs/{source.name}",
                    media_type="text/x.gcode",
                    max_bytes=MAX_GCODE_IMPORT_BYTES,
                )
                self._remember_bundle(bundle)
                artifacts = bundle.artifacts
            return self._transition(
                job,
                JobState.VALIDATING_GCODE,
                "gcode_check.validating",
                None,
                _SERVICE,
                artifacts,
            )
        if job.state is JobState.VALIDATING_GCODE:
            gcode = self._artifact(job.job_id, "gcode")
            result = self._execute(
                job,
                WorkerKind.GCODE_CHECK,
                ("gcode",),
                {"machine_build_profile": self._machine_profile(), "expected_gcode_sha256": gcode.sha256},
            )
            return self._finish_worker(job, result, JobState.COMPLETED, "gcode_check.completed")
        return job

    def run_to_intervention_or_terminal(self, job_id: str) -> JobRecord:
        job = self.get(job_id)
        while job.state not in _TERMINAL and job.state is not JobState.BLOCKED:
            job = self.advance(job_id)
        return job

    def cancel(
        self,
        job_id: str,
        *,
        request_id: str,
        requester: RequesterIdentity,
    ) -> JobRecord:
        job = self.get(job_id)
        if job.state in {JobState.CANCELLED, JobState.FAILED, JobState.COMPLETED}:
            return job
        return self._transition(job, JobState.CANCELLED, "job.cancelled", request_id, requester)

    def get(self, job_id: str) -> JobRecord:
        job = self._repository.get_job(job_id)
        if job is None:
            self._raise("JOB_NOT_FOUND", "job does not exist", job_id=job_id)
        return job

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        return self._repository.list_job_events(job_id)

    def artifact_refs(self, job_id: str) -> tuple[ArtifactRef, ...]:
        return self._repository.list_artifacts(job_id)

    def jobs(self) -> tuple[JobRecord, ...]:
        return self._repository.list_jobs_in_states(frozenset(JobState))

    def reconcile_inflight_jobs(self) -> tuple[JobRecord, ...]:
        reconciled: list[JobRecord] = []
        for job in self._repository.list_jobs_in_states(_INFLIGHT):
            request_id = f"service-restart-{uuid4()}"
            error = ErrorPayload(
                "SERVICE_RESTARTED",
                ErrorCategory.SERVICE,
                "service restarted while the job was in flight",
                False,
                {},
                request_id=request_id,
                job_id=job.job_id,
            )
            reconciled.append(
                self._transition(job, JobState.FAILED, "job.service_restarted", request_id, _SERVICE, error=error)
            )
        return tuple(reconciled)

    def fail_unexpected(self, job_id: str, *, request_id: str, error: BaseException) -> JobRecord:
        del error
        job = self.get(job_id)
        if job.state in {JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED}:
            return job
        return self._fail(
            job,
            "ORCHESTRATION_FAILED",
            "offline job orchestration failed",
            request_id=request_id,
            projection_required=False,
        )

    def close(self) -> None:
        try:
            self._workers.close()
        finally:
            self._repository.close()

    def _prepare_image(self, job: JobRecord) -> JobRecord:
        mode = RouteMode(cast(str, job.request["route_mode"]))
        if mode is RouteMode.IMAGE_EDIT:
            return self._prepare_image_edit(job)
        prepared = self._execute(
            job,
            WorkerKind.IMAGE_PREPARE,
            ("input_image",),
            {
                "mode": "direct",
                "source_name": Path(cast(str, job.request["input_path"])).name,
                "route_mode": mode.value,
                "semantic_assessment": cast(JsonObject, job.to_json()["request"])["semantic_assessment"],
                "normalization": {},
            },
        )
        if prepared.outcome.status is not WorkerStatus.SUCCEEDED:
            return self._finish_worker(job, prepared, JobState.BLOCKED, "job.image_prepared")
        assert prepared.bundle is not None
        promoted = prepared.bundle.artifacts
        if mode is RouteMode.AUTO:
            try:
                route = self._validated_route(prepared.bundle, job)
            except DrawingMachineError:
                return self._fail(job, "ROUTE_DECISION_INVALID", "promoted route decision is invalid")
            if route == "B_IMAGE_EDIT":
                return self._prepare_image_edit(job, prefix_artifacts=prepared.bundle.artifacts)
            direct = self._execute(
                job,
                WorkerKind.IMAGE_PREPARE,
                ("input_image",),
                {
                    "mode": "direct",
                    "source_name": Path(cast(str, job.request["input_path"])).name,
                    "route_mode": RouteMode.DIRECT.value,
                    "semantic_assessment": job.request["semantic_assessment"],
                    "normalization": {},
                },
            )
            if direct.outcome.status is not WorkerStatus.SUCCEEDED:
                return self._finish_worker(
                    job,
                    direct,
                    JobState.BLOCKED,
                    "job.image_prepared",
                    prepared.bundle.artifacts,
                )
            assert direct.bundle is not None
            promoted = prepared.bundle.artifacts + tuple(
                replace(artifact, role="direct_route_decision") if artifact.role == "route_decision" else artifact
                for artifact in direct.bundle.artifacts
            )
        blocker = JobBlocker("REVIEW_REQUIRED", ErrorCategory.VALIDATION, "processed image review is required", {})
        return self._transition(
            job,
            JobState.BLOCKED,
            "job.review_required",
            None,
            _SERVICE,
            promoted,
            blocker=blocker,
        )

    def _prepare_image_edit(
        self,
        job: JobRecord,
        *,
        prefix_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> JobRecord:
        provider = self._execute(
            job,
            WorkerKind.PROVIDER_LOCAL_COMFYUI,
            ("input_image",),
            {
                "prompt": job.request["prompt"],
                "provider_profile": self._provider_profile(),
                "provider_config_path": str(self._config.provider_path),
            },
        )
        if provider.outcome.status is not WorkerStatus.SUCCEEDED:
            return self._finish_worker(
                job,
                provider,
                JobState.BLOCKED,
                "job.provider_finished",
                prefix_artifacts,
            )
        assert provider.bundle is not None
        normalize = self._execute(
            job,
            WorkerKind.IMAGE_PREPARE,
            ("processed_image_raw",),
            {"mode": "normalize_existing", "source_name": "processed_image_raw.png", "normalization": {}},
        )
        prior = prefix_artifacts + provider.bundle.artifacts
        if normalize.outcome.status is not WorkerStatus.SUCCEEDED:
            return self._finish_worker(job, normalize, JobState.BLOCKED, "job.image_prepared", prior)
        assert normalize.bundle is not None
        blocker = JobBlocker("REVIEW_REQUIRED", ErrorCategory.VALIDATION, "processed image review is required", {})
        return self._transition(
            job,
            JobState.BLOCKED,
            "job.review_required",
            None,
            _SERVICE,
            prior + normalize.bundle.artifacts,
            blocker=blocker,
        )

    def _finish_worker(
        self,
        job: JobRecord,
        result: _WorkerResult,
        success_state: JobState,
        event_type: str,
        extra_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> JobRecord:
        outcome = result.outcome
        artifacts = extra_artifacts + (() if result.bundle is None else result.bundle.artifacts)
        if outcome.status is WorkerStatus.SUCCEEDED:
            return self._transition(job, success_state, event_type, None, _SERVICE, artifacts)
        if outcome.status is WorkerStatus.BLOCKED:
            assert outcome.error is not None
            blocker = JobBlocker(
                outcome.error.code,
                outcome.error.category,
                outcome.error.message,
                cast(JsonObject, outcome.error.details),
            )
            return self._transition(job, JobState.BLOCKED, event_type, None, _SERVICE, artifacts, blocker=blocker)
        if outcome.status is WorkerStatus.CANCELLED:
            request_id, requester = self._cancellation_identity(job.job_id)
            return self._transition(job, JobState.CANCELLED, "job.cancelled", request_id, requester)
        assert outcome.error is not None
        request_id = f"worker-failure-{uuid4()}"
        error = ErrorPayload(
            outcome.error.code,
            outcome.error.category,
            outcome.error.message,
            False,
            outcome.error.details,
            request_id=request_id,
            job_id=job.job_id,
        )
        return self._transition(job, JobState.FAILED, "job.worker_failed", request_id, _SERVICE, error=error)

    def _fail(
        self,
        job: JobRecord,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        projection_required: bool = True,
    ) -> JobRecord:
        selected_request_id = request_id or f"orchestration-failure-{uuid4()}"
        error = ErrorPayload(
            code,
            ErrorCategory.INTERNAL,
            message,
            False,
            {},
            request_id=selected_request_id,
            job_id=job.job_id,
        )
        return self._transition(
            job,
            JobState.FAILED,
            "job.orchestration_failed",
            selected_request_id,
            _SERVICE,
            error=error,
            projection_required=projection_required,
        )

    def _execute(
        self,
        job: JobRecord,
        kind: WorkerKind,
        input_roles: tuple[str, ...],
        payload: JsonObject,
    ) -> _WorkerResult:
        for retry in range(2):
            attempt_id = f"attempt-{uuid4()}"
            task_id = f"task-{uuid4()}"
            staging = self._artifacts.create_staging(job.job_id, attempt_id)
            try:
                task = WorkerTaskV1(
                    1,
                    task_id,
                    job.job_id,
                    job.revision,
                    attempt_id,
                    kind,
                    tuple(self._worker_input(job.job_id, role) for role in input_roles),
                    self._config.digests,
                    str(staging.resolve()),
                    payload,
                )
                handle = self._workers.submit(task)
                outcome = self._workers.wait(
                    handle,
                    timeout_seconds=300.0,
                    cancel_requested=lambda: self._cancel_requested(job.job_id),
                )
                if not self._matches(task, outcome):
                    request_id = f"stale-worker-{uuid4()}"
                    stale = WorkerOutcomeV1(
                        1,
                        task.task_id,
                        task.job_id,
                        task.job_revision,
                        task.attempt_id,
                        WorkerStatus.FAILED,
                        (),
                        {},
                        ErrorPayload(
                            "STALE_WORKER_OUTCOME",
                            ErrorCategory.INTERNAL,
                            "worker outcome identity did not match its immutable task",
                            False,
                            {"request_id": request_id},
                            job_id=job.job_id,
                        ),
                    )
                    return _WorkerResult(stale, None)
                if (
                    outcome.status is WorkerStatus.FAILED
                    and outcome.error is not None
                    and outcome.error.code == _WORKER_CRASH_CODE
                    and kind is not WorkerKind.PROVIDER_LOCAL_COMFYUI
                    and retry == 0
                ):
                    continue
                required_roles = self._required_roles(task, outcome.status)
                actual_roles = frozenset(artifact.role for artifact in outcome.artifacts)
                if actual_roles != required_roles:
                    return _WorkerResult(self._artifact_contract_failure(task), None)
                try:
                    self._validate_worker_json_artifacts(task, outcome)
                except DrawingMachineError as validation_error:
                    return _WorkerResult(self._artifact_content_failure(task, validation_error), None)
                bundle = self._promote(task, outcome, required_roles)
                return _WorkerResult(outcome, bundle)
            finally:
                self._artifacts.discard_staging(job.job_id, attempt_id)
        raise AssertionError("worker retry loop is exhaustive")

    def _promote(
        self,
        task: WorkerTaskV1,
        outcome: WorkerOutcomeV1,
        required_roles: frozenset[str],
    ) -> PromotedBundle | None:
        if not required_roles:
            return None
        specs = _ARTIFACT_SPECS[task.kind]
        expected = tuple(
            ExpectedArtifact(
                role,
                specs[role][0],
                specs[role][1],
                specs[role][1] == "application/json",
                (("schema", "input_route_decision_v1"),) if role == "route_decision" else (),
            )
            for role in sorted(required_roles)
        )
        bundle = self._artifacts.validate_and_promote(
            task.job_id,
            task.attempt_id,
            outcome.artifacts,
            expected=expected,
        )
        self._remember_bundle(bundle)
        return bundle

    @staticmethod
    def _required_roles(task: WorkerTaskV1, status: WorkerStatus) -> frozenset[str]:
        if status is WorkerStatus.SUCCEEDED:
            mode = cast(str | None, task.payload.get("mode")) if task.kind is WorkerKind.IMAGE_PREPARE else None
            if task.kind is WorkerKind.IMAGE_PREPARE and mode == "direct" and task.payload.get("route_mode") == "auto":
                return frozenset({"route_decision"})
            return _SUCCESS_ROLES[(task.kind, mode)]
        if status is WorkerStatus.BLOCKED:
            return _BLOCKED_ROLES[task.kind]
        return frozenset()

    @staticmethod
    def _artifact_contract_failure(task: WorkerTaskV1) -> WorkerOutcomeV1:
        return WorkerOutcomeV1(
            1,
            task.task_id,
            task.job_id,
            task.job_revision,
            task.attempt_id,
            WorkerStatus.FAILED,
            (),
            {},
            ErrorPayload(
                "WORKER_ARTIFACT_SET_INVALID",
                ErrorCategory.INTERNAL,
                "worker artifact set did not match its exact contract",
                False,
                {},
                job_id=task.job_id,
            ),
        )

    @staticmethod
    def _artifact_content_failure(task: WorkerTaskV1, error: DrawingMachineError) -> WorkerOutcomeV1:
        details: JsonObject = {"cause_code": error.payload.code}
        role = error.payload.details.get("role")
        if isinstance(role, str):
            details["role"] = role
        return WorkerOutcomeV1(
            1,
            task.task_id,
            task.job_id,
            task.job_revision,
            task.attempt_id,
            WorkerStatus.FAILED,
            (),
            {},
            ErrorPayload(
                "WORKER_ARTIFACT_CONTENT_INVALID",
                ErrorCategory.VALIDATION,
                "worker JSON artifact failed its production content contract",
                False,
                details,
                job_id=task.job_id,
            ),
        )

    def _validate_worker_json_artifacts(self, task: WorkerTaskV1, outcome: WorkerOutcomeV1) -> None:
        if not outcome.artifacts:
            return
        input_artifact = task.input_artifacts[0] if len(task.input_artifacts) == 1 else None
        context = ArtifactContractContext(
            job_id=task.job_id,
            input_role=None if input_artifact is None else input_artifact.role,
            input_name=(
                cast(str | None, task.payload.get("source_name")) if task.kind is WorkerKind.IMAGE_PREPARE else None
            ),
            input_sha256=None if input_artifact is None else input_artifact.sha256,
            input_size_bytes=None if input_artifact is None else input_artifact.size_bytes,
            output_sha256_by_role={artifact.role: artifact.sha256 for artifact in outcome.artifacts},
        )
        documents: dict[str, JsonObject] = {}
        for artifact in outcome.artifacts:
            if artifact.media_type != "application/json":
                continue
            try:
                document = self._read_staged_json(task, artifact)
                documents[artifact.role] = validate_json_artifact(artifact.role, document, context)
            except DrawingMachineError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise DrawingMachineError(
                    ErrorPayload(
                        "WORKER_ARTIFACT_CONTENT_INVALID",
                        ErrorCategory.VALIDATION,
                        "worker JSON artifact could not be safely decoded",
                        False,
                        {"role": artifact.role},
                        job_id=task.job_id,
                    )
                ) from error
        if task.kind is WorkerKind.GCODE_CHECK:
            expected = self._expected_gcode_safety_documents(task)
            validate_gcode_safety_bundle(documents, expected, job_id=task.job_id)
            allow_stream = expected["readiness"].get("allow_stream")
            expected_status = WorkerStatus.SUCCEEDED if allow_stream is True else WorkerStatus.BLOCKED
            if outcome.status is not expected_status:
                raise DrawingMachineError(
                    ErrorPayload(
                        "WORKER_ARTIFACT_CONTENT_INVALID",
                        ErrorCategory.VALIDATION,
                        "worker outcome status does not agree with exact G-code readiness",
                        False,
                        {"role": "readiness"},
                        job_id=task.job_id,
                    )
                )

    def _read_staged_json(
        self,
        task: WorkerTaskV1,
        artifact: WorkerArtifact,
    ) -> object:
        expected_path = _ARTIFACT_SPECS[task.kind][artifact.role][0]
        content = self._artifacts.read_staged_bytes(
            task.job_id,
            task.attempt_id,
            artifact,
            expected_relative_path=expected_path,
            expected_media_type="application/json",
            max_bytes=_MAX_JSON_ARTIFACT_BYTES,
        )
        return json.loads(content.decode("utf-8"))

    def _expected_gcode_safety_documents(self, task: WorkerTaskV1) -> dict[str, JsonObject]:
        gcode_ref = self._artifact(task.job_id, "gcode")
        content = self._artifacts.read_bytes(
            task.job_id,
            gcode_ref,
            expected_media_type="text/x.gcode",
            max_bytes=_MAX_GCODE_VALIDATION_BYTES,
        )
        try:
            gcode = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DrawingMachineError(
                ErrorPayload(
                    "WORKER_ARTIFACT_CONTENT_INVALID",
                    ErrorCategory.VALIDATION,
                    "G-code input is not UTF-8 during safety artifact binding",
                    False,
                    {"role": "gcode"},
                    job_id=task.job_id,
                )
            ) from error
        expected_digest = task.payload.get("expected_gcode_sha256")
        if expected_digest != gcode_ref.sha256:
            raise DrawingMachineError(
                ErrorPayload(
                    "WORKER_ARTIFACT_CONTENT_INVALID",
                    ErrorCategory.VALIDATION,
                    "G-code safety task digest does not bind to the durable input",
                    False,
                    {"role": "gcode"},
                    job_id=task.job_id,
                )
            )
        result = check_candidate(
            gcode,
            profile=parse_machine_build_profile(self._config.machine),
            expected_gcode_sha256=gcode_ref.sha256,
        )
        return {
            "gcode_static": result.static_result.to_json(),
            "send_plan": result.send_plan.to_json(),
            "readiness": result.readiness.to_json(),
        }

    def _transition(
        self,
        job: JobRecord,
        state: JobState,
        event_type: str,
        request_id: str | None,
        requester: RequesterIdentity,
        artifacts: tuple[ArtifactRef, ...] = (),
        *,
        blocker: JobBlocker | None = None,
        error: ErrorPayload | None = None,
        ready_snapshot: ReadyToRunSnapshot | None = None,
        projection_required: bool = True,
        projection_deferred: bool = False,
        commit_allowed: Callable[[], bool] | None = None,
    ) -> JobRecord:
        blocker_code = None if job.blocker is None else job.blocker.code
        if not allowed_transition(job.state, state, blocker_code, job_kind=job.kind):
            raise DrawingMachineError(
                ErrorPayload(
                    "JOB_TRANSITION_NOT_ALLOWED",
                    ErrorCategory.INTERNAL,
                    "offline job transition is not allowed by the domain state authority",
                    False,
                    {
                        "job_kind": job.kind.value,
                        "prior_state": job.state.value,
                        "result_state": state.value,
                    },
                    request_id=request_id,
                    job_id=job.job_id,
                )
            )
        transition = JobTransition(
            job.job_id, job.revision, state, request_id, event_type, blocker, error, ready_snapshot
        )
        event, audit = self._event_pair(
            job,
            job.state,
            state,
            event_type,
            request_id,
            requester,
            blocker=blocker,
            error=error,
        )
        with self._repository.transaction() as transaction:
            updated = transaction.transition_job(transition, artifacts=artifacts, event=event, audit=audit)
            if commit_allowed is not None and not commit_allowed():
                raise _CoordinatorCommitAbandoned
        if projection_deferred:
            return updated
        if projection_required:
            self._artifacts.write_projection(updated, self._repository.list_artifacts(job.job_id))
        else:
            with suppress(Exception):
                self._artifacts.write_projection(updated, self._repository.list_artifacts(job.job_id))
        return updated

    def _event_pair(
        self,
        job: JobRecord,
        prior: JobState | None,
        result: JobState,
        event_type: str,
        request_id: str | None,
        requester: RequesterIdentity,
        *,
        blocker: JobBlocker | None = None,
        error: ErrorPayload | None = None,
    ) -> tuple[JobEvent, AuditRecord]:
        event_id = str(uuid4())
        occurred_at = self._clock.now()
        approval = event_type == "job.review_accepted"
        payload = AuditPayloadV1(
            requester=requester,
            job_id=job.job_id,
            command_name=self._audit_command_name(event_type),
            event_type=event_type,
            job_kind=job.kind,
            prior_state=prior,
            result_state=result,
            approval_identity=requester if approval else None,
            approved_at=occurred_at if approval else None,
            error_code=None if error is None else error.code,
            blocker_code=None if blocker is None else blocker.code,
            service_version=__version__,
        ).to_json()
        event = JobEvent(
            event_id,
            job.job_id,
            job.revision if prior is None else job.revision + 1,
            event_type,
            prior,
            result,
            request_id,
            requester.requester_type,
            payload,
            occurred_at,
        )
        return event, AuditRecord(event_id, event_type, request_id, payload)

    @staticmethod
    def _audit_command_name(event_type: str) -> str:
        if event_type == "job.queued":
            return "workflow.run"
        if event_type == "gcode_check.queued":
            return "gcode.check"
        if event_type == "job.review_accepted":
            return "workflow.run.review"
        if event_type == "job.cancelled":
            return "job.cancel"
        if event_type == "job.service_restarted":
            return "service.reconcile"
        return "service.advance"

    def _remember_bundle(self, bundle: PromotedBundle) -> None:
        roles = self._absolute_artifacts.setdefault(bundle.job_id, {})
        metadata = self._artifact_metadata.setdefault(bundle.job_id, {})
        for artifact in bundle.artifacts:
            relative = PurePosixPath(artifact.relative_path)
            roles[artifact.role] = bundle.root.joinpath(*relative.parts[2:])
            metadata[artifact.role] = artifact

    def _rehydrate_artifacts(self, job: JobRecord) -> None:
        roles = self._absolute_artifacts.setdefault(job.job_id, {})
        metadata = self._artifact_metadata.setdefault(job.job_id, {})
        for artifact in self._repository.list_artifacts(job.job_id):
            roles[artifact.role] = self._artifacts.resolve(job.job_id, artifact)
            metadata[artifact.role] = artifact

    def _worker_input(self, job_id: str, role: str) -> WorkerInputArtifact:
        artifact = self._artifact(job_id, role)
        path = self._absolute_artifacts[job_id][role]
        return WorkerInputArtifact(role, str(path.resolve()), artifact.sha256, artifact.size_bytes, artifact.media_type)

    def _artifact(self, job_id: str, role: str) -> ArtifactRef:
        for artifact in reversed(self._repository.list_artifacts(job_id)):
            if artifact.role == role:
                return artifact
        pending = self._artifact_metadata.get(job_id, {}).get(role)
        if pending is not None:
            return pending
        self._raise("JOB_ARTIFACT_MISSING", f"job artifact is missing: {role}", job_id=job_id)

    def _config_snapshot(self) -> ConfigSnapshot:
        return ConfigSnapshot(
            self._config.application.machine_profile,
            self._config.application.provider_profile,
            self._config.application.schema_version,
            self._config.machine.schema_version,
            self._config.provider.schema_version,
            self._config.digests["application"],
            self._config.digests["machine"],
            self._config.digests["provider"],
        )

    def _machine_profile(self) -> JsonObject:
        return {"schema_version": self._config.machine.schema_version, "profile": self._config.machine.profile}

    def _provider_profile(self) -> JsonObject:
        return {"schema_version": self._config.provider.schema_version, "profile": self._config.provider.profile}

    def _planning_payload(self) -> JsonObject:
        planning = cast(JsonObject, self._config.machine.profile["planning"])
        dedupe_keys = (
            "dedupe_short_path_length_mm",
            "dedupe_distance_mm",
            "dedupe_angle_deg",
            "dedupe_overlap_ratio",
        )
        limits: JsonObject = {
            "soft_component_count": 160,
            "soft_skeleton_pixels": 26000,
            "soft_boundary_pixels": 45000,
            "soft_transitions_total": 68000,
            "hard_component_count": 260,
            "hard_skeleton_pixels": 34000,
            "hard_boundary_pixels": 60000,
            "hard_transitions_total": 100000,
            "threshold": planning["threshold"],
            "invert": planning["invert"],
            "min_component_area_px": planning["min_component_area_px"],
        }
        return {
            "planning_profile": {
                "schema_version": self._config.machine.schema_version,
                "profile": {"name": self._config.machine.profile["name"], "planning": planning},
            },
            "complexity_limits": limits,
            "base_dedupe_profile": {key: planning[key] for key in dedupe_keys},
        }

    @staticmethod
    def _matches(task: WorkerTaskV1, outcome: WorkerOutcomeV1) -> bool:
        return (
            outcome.task_id == task.task_id
            and outcome.job_id == task.job_id
            and outcome.job_revision == task.job_revision
            and outcome.attempt_id == task.attempt_id
        )

    @staticmethod
    def _read_json(bundle: PromotedBundle, role: str) -> JsonObject:
        artifact = next(item for item in bundle.artifacts if item.role == role)
        relative = PurePosixPath(artifact.relative_path)
        value = json.loads(bundle.root.joinpath(*relative.parts[2:]).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{role} artifact must contain a JSON object")
        return cast(JsonObject, value)

    def _validated_route(self, bundle: PromotedBundle, job: JobRecord) -> str:
        try:
            document = self._read_json(bundle, "route_decision")
        except (OSError, ValueError, StopIteration, json.JSONDecodeError) as error:
            raise DrawingMachineError(
                ErrorPayload(
                    "ROUTE_DECISION_INVALID",
                    ErrorCategory.VALIDATION,
                    "promoted route decision could not be read",
                    False,
                    {},
                    job_id=job.job_id,
                )
            ) from error
        if set(document) != _ROUTE_FIELDS:
            self._raise("ROUTE_DECISION_INVALID", "route decision fields are invalid", job_id=job.job_id)
        route = document.get("route")
        expected = {
            "A_DIRECT": (True, "prepare_direct_processed_image"),
            "B_IMAGE_EDIT": (False, "qwen_image_edit"),
        }
        if route not in expected:
            self._raise("ROUTE_DECISION_INVALID", "route decision literal is invalid", job_id=job.job_id)
        allowed, next_stage = expected[cast(str, route)]
        source_name = Path(cast(str, job.request["input_path"])).name
        if (
            document.get("schema") != "input_route_decision_v1"
            or document.get("job_name") != job.job_id
            or document.get("input_image") != source_name
            or document.get("direct_route_allowed") is not allowed
            or document.get("next_stage") != next_stage
            or document.get("hardware_touched") is not False
            or not isinstance(document.get("visual_scores"), Mapping)
            or not isinstance(document.get("stats"), Mapping)
            or not isinstance(document.get("classification"), Mapping)
        ):
            self._raise("ROUTE_DECISION_INVALID", "route decision binding is invalid", job_id=job.job_id)
        return cast(str, route)

    def _cancel_requested(self, job_id: str) -> bool:
        with self._cancellation_lock:
            token = self._cancellation.get(job_id)
        return token is not None and token.is_set()

    def _cancellation_identity(self, job_id: str) -> tuple[str, RequesterIdentity]:
        with self._cancellation_lock:
            metadata = self._cancellation_metadata.get(job_id)
        return metadata if metadata is not None else (f"cancel-{uuid4()}", _SERVICE)

    @staticmethod
    def _raise(code: str, message: str, *, job_id: str | None = None) -> NoReturn:
        raise DrawingMachineError(ErrorPayload(code, ErrorCategory.SERVICE, message, False, {}, job_id=job_id))


__all__ = ["OfflineJobChain"]
