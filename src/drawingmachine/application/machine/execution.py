from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import NoReturn, cast
from uuid import UUID, uuid4

from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    HomeZMachineCommand,
    MachineActionCommand,
    MachineCommandMode,
    MachineRuntimeAuthority,
    PrepareMachineCommand,
    RecoverMachineCommand,
    StreamMachineCommand,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.recovery import validate_recovery_disposition
from drawingmachine.config.machine import MachineExecutionConfig
from drawingmachine.domain.fluidnc.gates import GateDisposition, evaluate_calibrated_pose, evaluate_preflight
from drawingmachine.domain.fluidnc.status import (
    ControllerStatus,
    PreflightSnapshot,
    Vector3,
    WorkOffset,
    parse_preflight_snapshot,
)
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.gcode.stream_program import (
    MAX_GCODE_BYTES,
    ValidatedStreamProgram,
    build_validated_stream_program,
)
from drawingmachine.domain.jobs import (
    AuditPayloadV1,
    AuditRecord,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    ReadyToRunSnapshot,
    RequesterIdentity,
)
from drawingmachine.domain.jobs.models import MachineJobOutcomeTransition
from drawingmachine.domain.machine import (
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalBinding,
    MachineApprovalChallenge,
    MachineApprovalDecision,
    MachineApprovalEvidenceV1,
    MachineAuditPayloadV2,
    MachineEvent,
    MachineEventPayloadV2,
    MachineExecution,
    MachineFailureEvidenceV1,
    MachineMotionEvidenceKind,
    MachineMotionRecoveryEvidenceV1,
    MachineMotionStep,
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
    consume_approval,
    expire_approval,
    issue_approval,
    supersede_approval,
)
from drawingmachine.domain.machine.models import recovery_intent_for_milestone
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import ArtifactReader
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCSession,
    FluidNCSessionFactory,
    HomeAllAxesRequest,
    HomeOutcome,
    HomeZAxisRequest,
    HomeZOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    PreflightOutcome,
    SingleCommandOutcomeStage,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressUpdate,
    ZCalibrationOutcome,
    ZCalibrationOutcomeStage,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)
from drawingmachine.ports.machine_repository import (
    MachineAdmissionCapability,
    MachineRepository,
    MachineRepositoryTransaction,
    MachineRequestKey,
    MachineRequestRecord,
)

_COORDINATOR_FATAL_PERSISTENCE = "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
_CLEANED_FATAL_TOKEN = object()


class _CleanedCoordinatorFatal(Exception):
    __slots__ = ("__token",)

    def __init__(self, token: object) -> None:
        if token is not _CLEANED_FATAL_TOKEN:
            raise TypeError("cleaned coordinator fatal construction is application-private")
        super().__init__("cleaned coordinator fatal")
        self.__token = token


def _new_cleaned_coordinator_fatal() -> _CleanedCoordinatorFatal:
    return _CleanedCoordinatorFatal(_CLEANED_FATAL_TOKEN)


def _is_cleaned_coordinator_fatal(value: BaseException) -> bool:
    return type(value) is _CleanedCoordinatorFatal and getattr(value, "_CleanedCoordinatorFatal__token", None) is (
        _CLEANED_FATAL_TOKEN
    )


@dataclass(frozen=True, slots=True)
class MachineCommandResult:
    response: JsonObject
    deduplicated: bool

    def __post_init__(self) -> None:
        if type(self.response) is not dict or type(self.deduplicated) is not bool:
            _fail("MACHINE_RESULT_INVALID", "machine result must be a plain immutable-copyable response")
        object.__setattr__(self, "response", _plain_json(self.response))


class _StreamProgressLifetime:
    __slots__ = ("_active", "_lock")

    def __init__(self) -> None:
        self._active = True
        self._lock = Lock()

    def apply(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._active:
                _fail("STREAM_PROGRESS_LIFETIME_EXPIRED", "STREAM progress callback lifetime expired")
            callback()

    def expire(self) -> None:
        with self._lock:
            self._active = False


@dataclass(frozen=True, slots=True)
class _AdmittedMachineAction:
    capability: MachineAdmissionCapability
    program: ValidatedStreamProgram
    ready_snapshot: ReadyToRunSnapshot
    response: JsonObject
    approval: MachineApprovalChallenge | None = None

    def __post_init__(self) -> None:
        if type(self.capability) is not MachineAdmissionCapability:
            _fail("MACHINE_ADMISSION_INVALID", "admission capability must be exact")
        if type(self.program) is not ValidatedStreamProgram:
            _fail("MACHINE_ADMISSION_INVALID", "admission requires a complete validated stream program")
        if type(self.ready_snapshot) is not ReadyToRunSnapshot:
            _fail("MACHINE_ADMISSION_INVALID", "admission requires the exact frozen ready snapshot")
        if type(self.response) is not dict:
            _fail("MACHINE_ADMISSION_INVALID", "admission response must be a plain object")
        approval_required = self.capability.action in {
            MachineAction.HOME,
            MachineAction.ZCAL,
            MachineAction.ZCONFIRM,
            MachineAction.STREAM,
            MachineAction.HOME_Z,
            MachineAction.RECOVER,
        }
        if approval_required != (self.approval is not None):
            _fail("MACHINE_ADMISSION_INVALID", "approved admission must retain exactly one consumed challenge")
        if self.approval is not None:
            if (
                type(self.approval) is not MachineApprovalChallenge
                or self.approval.status is not ApprovalStatus.CONSUMED
            ):
                _fail("MACHINE_ADMISSION_INVALID", "approved admission requires one consumed challenge")
            binding = self.approval.binding
            expected_execution_id = (
                self.capability.predecessor_execution_id
                if self.capability.action is MachineAction.RECOVER
                else self.capability.execution_id
            )
            if binding.action is not self.capability.action or binding.execution_id != expected_execution_id:
                _fail("MACHINE_ADMISSION_INVALID", "consumed challenge does not match admitted action authority")
        object.__setattr__(self, "response", _plain_json(self.response))

    @property
    def execution_id(self) -> str:
        return self.capability.execution_id


MachineAdmissionResult = _AdmittedMachineAction | MachineCommandResult


@dataclass(frozen=True, slots=True)
class _ValidatedReadyAuthority:
    job: JobRecord
    snapshot: ReadyToRunSnapshot
    program: ValidatedStreamProgram


class MachineExecutionChain:
    """Durably admit first, then claim exactly once before typed adapter I/O."""

    def __init__(
        self,
        repository: MachineRepository,
        artifact_reader: ArtifactReader,
        fluidnc_factory: FluidNCSessionFactory,
        machine_config: MachineExecutionConfig,
        runtime_authority: MachineRuntimeAuthority,
        *,
        identity_factory: Callable[[], str] | None = None,
        wall_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        if type(machine_config) is not MachineExecutionConfig:
            _fail("MACHINE_CHAIN_INVALID", "machine config must be an exact validated MachineExecutionConfig")
        if type(runtime_authority) is not MachineRuntimeAuthority:
            _fail("MACHINE_CHAIN_INVALID", "runtime authority must be exact trusted MachineRuntimeAuthority")
        self._repository = repository
        self._artifact_reader = artifact_reader
        self._fluidnc_factory = fluidnc_factory
        self._config = machine_config
        self._authority = runtime_authority
        self._identity_factory: Callable[[], str] = (
            (lambda: str(uuid4())) if identity_factory is None else identity_factory
        )
        self._wall_now: Callable[[], datetime] = (lambda: datetime.now(UTC)) if wall_now is None else wall_now
        self._monotonic_now: Callable[[], float] = time.monotonic if monotonic_now is None else monotonic_now
        self._sessions: dict[str, FluidNCSession] = {}
        self._session_evidence: dict[str, MachineSessionSnapshot] = {}
        self._retained_sessions: dict[str, FluidNCSession] = {}
        self._progress: dict[str, MachineProgress] = {}
        self._pending_progress: dict[str, MachineProgress] = {}
        self._last_progress_checkpoint: dict[str, tuple[int, float]] = {}
        self._projection_publisher: Callable[[JsonObject], object] | None = None

    def install_projection_publisher(self, publisher: Callable[[JsonObject], object]) -> None:
        """Install the sole coordinator-owned cache publisher before initialization."""
        if not callable(publisher) or self._projection_publisher is not None:
            _fail("MACHINE_CHAIN_INVALID", "projection publisher must be installed exactly once")
        self._projection_publisher = publisher

    def admit_action(
        self,
        command: MachineActionCommand,
        *,
        still_requested: Callable[[], bool] | None = None,
    ) -> MachineAdmissionResult:
        if type(command) is PrepareMachineCommand:
            return self._admit_prepare(command, still_requested=still_requested)
        if type(command) is RecoverMachineCommand:
            return self._admit_recover(command, still_requested=still_requested)
        if type(command) is HomeMachineCommand:
            return self._admit_motion(command, still_requested=still_requested)
        if type(command) is ZCalMachineCommand:
            return self._admit_motion(command, still_requested=still_requested)
        if type(command) is ZConfirmMachineCommand:
            return self._admit_motion(command, still_requested=still_requested)
        if type(command) is StreamMachineCommand:
            return self._admit_motion(command, still_requested=still_requested)
        if type(command) is HomeZMachineCommand:
            return self._admit_motion(command, still_requested=still_requested)
        _fail("MACHINE_COMMAND_INVALID", "unsupported machine command type")

    def drive_admitted_action(self, admission: object) -> MachineExecution:
        if type(admission) is not _AdmittedMachineAction:
            _fail("MACHINE_ADMISSION_INVALID", "drive requires the internal admitted capability")
        admitted = admission
        claimed_at = self._now()
        with self._repository.transaction() as transaction:
            transaction.claim_request(admitted.capability, claimed_at=claimed_at)
        return self._drive_claimed_action(admitted)

    def _drive_claimed_action(self, admission: _AdmittedMachineAction) -> MachineExecution:
        fatal_signal: _CleanedCoordinatorFatal | None = None
        try:
            return self._dispatch_claimed_action(admission)
        except Exception as error:
            if not _is_cleaned_coordinator_fatal(error):
                raise
            fatal_signal = cast(_CleanedCoordinatorFatal, error)
        assert fatal_signal is not None
        public_error = _error_value(
            _COORDINATOR_FATAL_PERSISTENCE,
            "machine persistence failed after possible hardware side effects; coordinator termination required",
        )
        raise public_error

    def _dispatch_claimed_action(self, admitted: _AdmittedMachineAction) -> MachineExecution:
        if admitted.capability.action in {MachineAction.HOME, MachineAction.ZCAL, MachineAction.ZCONFIRM}:
            return self._drive_motion(admitted)
        if admitted.capability.action is MachineAction.STREAM:
            return self._drive_stream(admitted)
        if admitted.capability.action is MachineAction.HOME_Z:
            return self._drive_home_z(admitted)
        if self._retained_sessions:
            return self._commit_preflight_exception(
                admitted,
                _error_value(
                    "SESSION_OWNERSHIP_UNCERTAIN",
                    "a prior FluidNC session remains owned until coordinator shutdown",
                ),
                session=None,
            )
        session: FluidNCSession | None = None
        try:
            session = self._fluidnc_factory.open_session(OpenSessionRequest(admitted.capability.machine_session_epoch))
            outcome = self._require_preflight_outcome(
                session.read_initial_preflight(InitialPreflightRequest()),
                admitted.capability.machine_session_epoch,
            )
            result = self._commit_preflight(admitted, outcome, owned_session=session)
            if result.phase is MachinePhase.AWAITING_HOME_APPROVAL:
                self._sessions[result.execution_id] = session
                snapshot = self._repository.get_session(result.execution_id)
                assert snapshot is not None
                self._session_evidence[result.execution_id] = snapshot
            else:
                self._close_failed_session(session, result, admitted)
            return result
        except Exception as error:
            if _is_cleaned_coordinator_fatal(error):
                raise
            current = self._repository.get_execution(admitted.execution_id)
            if current is None:
                raise
            if current.phase is MachinePhase.PREPARING_SESSION:
                result = self._commit_preflight_exception(admitted, error, session=session)
                if session is not None:
                    self._close_failed_session(session, result, admitted)
                return result
            raise

    def status_projection(self, execution_id: str | None = None) -> JsonObject:
        execution = (
            self._repository.get_active_execution()
            if execution_id is None
            else self._repository.get_execution(execution_id)
        )
        progress = None if execution is None else self.progress_projection(execution.execution_id)
        outcome = None if execution is None else self._repository.get_outcome(execution.execution_id)
        return {
            "schema_version": 1,
            "execution": None if execution is None else _execution_projection(execution),
            "progress": progress,
            "last_outcome": None if outcome is None else _outcome_projection(outcome),
        }

    def progress_projection(self, execution_id: str) -> JsonObject | None:
        execution = self._repository.get_execution(execution_id)
        if execution is None:
            return None
        progress = self._progress.get(execution.execution_id)
        if progress is None:
            persisted = self._repository.list_progress(execution.execution_id)
            progress = None if not persisted else persisted[-1]
        return None if progress is None else _progress_projection(progress)

    def retained_session_epochs(self) -> tuple[str, ...]:
        return tuple(sorted(self._retained_sessions))

    def release_retained_sessions_for_shutdown(self) -> tuple[str, ...]:
        epochs = self.retained_session_epochs()
        self._retained_sessions.clear()
        return epochs

    def initialize_and_reconcile(self) -> JsonObject:
        """Initialize persistence and freeze an old active owner without adapter I/O."""
        now = self._now()
        self._repository.initialize(applied_at=now.isoformat())
        self._reconcile_lifecycle("SERVICE_RESTARTED", "reconcile prior service machine ownership")
        return self.status_projection()

    def shutdown_for_recovery(self) -> JsonObject:
        """Durably freeze authority before closing the sole owned session once."""
        persistence_failed = False
        try:
            recovered = self._reconcile_lifecycle(
                "SERVICE_SHUTDOWN",
                "freeze active machine ownership before service shutdown",
            )
        except Exception:
            recovered = self._repository.get_active_execution()
            persistence_failed = True
        lifecycle_failed = self._close_owned_sessions(recovered)
        self._clear_runtime_observations()
        if persistence_failed or lifecycle_failed:
            raise _error_value(
                _COORDINATOR_FATAL_PERSISTENCE,
                "machine persistence failed during coordinator shutdown; startup reconciliation required",
            ) from None
        return self.status_projection()

    def close(self) -> None:
        self._clear_runtime_observations()
        self._retained_sessions.clear()
        self._repository.close()

    def _reconcile_lifecycle(
        self,
        reason_code: str,
        command_summary: str,
    ) -> MachineExecution | None:
        current = self._repository.get_active_execution()
        if current is None:
            return None
        now = self._now()
        if now <= current.updated_at:
            _fail("MACHINE_LIFECYCLE_CLOCK_INVALID", "lifecycle recovery time must advance durable authority")
        request_id = f"machine-lifecycle-{self._authority.service_epoch}-{reason_code.lower()}"
        if current.phase is MachinePhase.RECOVERY_REQUIRED:
            event, audit = self._lifecycle_machine_event_audit(
                current,
                prior_phase=current.phase,
                request_id=request_id,
                reason_code=reason_code,
                command_summary=command_summary,
                occurred_at=now,
            )
            with self._repository.transaction() as transaction:
                transaction.reconcile_restart(
                    current.revision,
                    current,
                    event=event,
                    audit=audit,
                )
            return current
        intent = recovery_intent_for_milestone(current.stream_milestone)
        failure = MachineFailureEvidenceV1(
            1,
            reason_code,
            "service lifecycle ended before the machine execution became terminal",
            None,
            True,
        )
        evidence = MachineRecoveryEvidence(
            1,
            current.execution_id,
            intent,
            current.stream_milestone,
            reason_code,
            failure,
            now,
        )
        recovered = replace(
            current,
            revision=current.revision + 1,
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=intent,
            recovery_evidence=evidence,
            updated_at=now,
        )
        job = self._repository_job(current.job_id)
        successor = current.predecessor_execution_id is not None
        expected_job_state = JobState.RECOVERY_REQUIRED if successor else JobState.READY_TO_RUN
        if job.state is not expected_job_state or job.ready_snapshot is None:
            _fail(
                "MACHINE_LIFECYCLE_JOB_INVALID",
                "active machine lifecycle recovery requires its retained job snapshot authority",
            )
        ready_snapshot = job.ready_snapshot
        if (
            ready_snapshot.job_id != current.job_id
            or ready_snapshot.job_revision != current.ready_revision
            or ready_snapshot.gcode.sha256 != current.gcode_sha256
            or ready_snapshot.application_version != current.application_version
            or ready_snapshot.config.application_digest != current.application_digest
            or ready_snapshot.config.machine_digest != current.machine_digest
            or ready_snapshot.config.provider_digest != current.provider_digest
            or job.config != ready_snapshot.config
        ):
            _fail("MACHINE_LIFECYCLE_JOB_INVALID", "lifecycle job snapshot changed from frozen authority")
        event, audit = self._lifecycle_machine_event_audit(
            recovered,
            prior_phase=current.phase,
            request_id=request_id,
            reason_code=reason_code,
            command_summary=command_summary,
            occurred_at=now,
        )
        if successor:
            with self._repository.transaction() as transaction:
                transaction.reconcile_restart(
                    current.revision,
                    recovered,
                    event=event,
                    audit=audit,
                )
            return recovered
        outcome = MachineOutcome(
            1,
            recovered.execution_id,
            recovered.job_id,
            recovered.revision,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            ready_snapshot,
            evidence,
            now,
        )
        transition = MachineJobOutcomeTransition(
            job.job_id,
            job.revision,
            JobState.RECOVERY_REQUIRED,
            "machine.lifecycle_recovery_required",
            ready_snapshot,
        )
        job_event, job_audit = self._lifecycle_job_event_audit(
            job,
            recovered,
            transition,
            request_id=request_id,
            reason_code=reason_code,
            occurred_at=now,
        )
        with self._repository.transaction() as transaction:
            transaction.reconcile_restart(
                current.revision,
                recovered,
                event=event,
                audit=audit,
                outcome=outcome,
                job_transition=transition,
                job_event=job_event,
                job_audit=job_audit,
            )
        return recovered

    def _lifecycle_machine_event_audit(
        self,
        execution: MachineExecution,
        *,
        prior_phase: MachinePhase,
        request_id: str,
        reason_code: str,
        command_summary: str,
        occurred_at: datetime,
    ) -> tuple[MachineEvent, MachineAuditPayloadV2]:
        event_id = self._id()
        event = MachineEvent(
            1,
            event_id,
            execution.execution_id,
            execution.job_id,
            execution.revision,
            "machine.lifecycle",
            execution.phase,
            MachineAction.PREPARE_SESSION,
            MachineEventPayloadV2(request_id, prior_phase, execution.phase, reason_code),
            occurred_at,
        )
        audit = MachineAuditPayloadV2(
            operation="machine.lifecycle",
            decision="RECOVERY_REQUIRED",
            execution_id=execution.execution_id,
            job_id=execution.job_id,
            execution_revision=execution.revision,
            frozen_ready_revision=execution.ready_revision,
            request_id=request_id,
            event_id=event_id,
            requester_uid=self._config.automation_principal.uid,
            requester_gid=self._config.automation_principal.gid,
            requester_pid=os.getpid(),
            issuer_pid=None,
            consumer_pid=None,
            prior_phase=prior_phase,
            result_phase=execution.phase,
            action=MachineAction.PREPARE_SESSION,
            recovery_disposition=None,
            approval_challenge_id=None,
            challenge_issued_at=None,
            approved_at=None,
            application_digest=execution.application_digest,
            machine_digest=execution.machine_digest,
            provider_digest=execution.provider_digest,
            gcode_sha256=execution.gcode_sha256,
            service_epoch=execution.service_epoch,
            machine_session_epoch=execution.machine_session_epoch,
            application_version=execution.application_version,
            protocol_version=1,
            command_summary=command_summary,
            error_code=reason_code,
        )
        return event, audit

    def _lifecycle_job_event_audit(
        self,
        job: JobRecord,
        execution: MachineExecution,
        transition: MachineJobOutcomeTransition,
        *,
        request_id: str,
        reason_code: str,
        occurred_at: datetime,
    ) -> tuple[JobEvent, AuditRecord]:
        requester = RequesterIdentity("SERVICE", None, None, None)
        event_id = self._id()
        event = JobEvent(
            event_id,
            job.job_id,
            job.revision + 1,
            transition.event_type,
            job.state,
            transition.result_state,
            request_id,
            requester.requester_type,
            {"requester": requester.to_json()},
            occurred_at,
        )
        audit = AuditRecord(
            event_id,
            transition.event_type,
            request_id,
            AuditPayloadV1(
                requester,
                job.job_id,
                "machine.lifecycle",
                transition.event_type,
                JobKind.OFFLINE_WORKFLOW,
                job.state,
                transition.result_state,
                None,
                None,
                reason_code,
                None,
                execution.application_version,
            ).to_json(),
        )
        return event, audit

    def _close_owned_sessions(self, recovered: MachineExecution | None) -> bool:
        lifecycle_failed = False
        for execution_id, session in tuple(self._sessions.items()):
            self._sessions.pop(execution_id, None)
            epoch = recovered.machine_session_epoch if recovered is not None else execution_id
            close_error = self._close_session_once(session, epoch)
            if close_error is None:
                continue
            self._retained_sessions[epoch] = session
            if recovered is None:
                lifecycle_failed = True
                continue
            now = self._now()
            event, audit = self._lifecycle_machine_event_audit(
                recovered,
                prior_phase=recovered.phase,
                request_id=f"machine-lifecycle-{self._authority.service_epoch}-close-uncertain",
                reason_code=close_error,
                command_summary="retain uncertain session ownership after one shutdown close",
                occurred_at=now,
            )
            try:
                with self._repository.transaction() as transaction:
                    transaction.record_lifecycle_event(recovered, event=event, audit=audit)
            except Exception:
                lifecycle_failed = True
        return lifecycle_failed

    def _clear_runtime_observations(self) -> None:
        self._sessions.clear()
        self._session_evidence.clear()
        self._progress.clear()
        self._pending_progress.clear()
        self._last_progress_checkpoint.clear()

    def _admit_prepare(
        self,
        command: PrepareMachineCommand,
        *,
        still_requested: Callable[[], bool] | None,
    ) -> MachineAdmissionResult:
        self._require_configured_requester(command.requester)
        self._require_requested(still_requested)
        key = _request_key(command)
        digest = _argument_digest(command)
        existing = self._repository.get_request(key)
        if existing is not None:
            return self._replay(existing, command, digest)
        validated = self._validate_initial_ready(command)
        self._require_requested(still_requested)
        now = self._now()
        execution = MachineExecution(
            schema_version=1,
            execution_id=self._id(),
            job_id=command.job_id,
            ready_revision=command.job_revision,
            revision=0,
            phase=MachinePhase.PREPARING_SESSION,
            service_epoch=self._authority.service_epoch,
            machine_session_epoch=self._id(),
            gcode_sha256=validated.snapshot.gcode.sha256,
            application_version=self._authority.application_version,
            application_digest=self._authority.application_digest,
            machine_digest=self._authority.machine_digest,
            provider_digest=self._authority.provider_digest,
            stream_milestone=StreamMilestone.NOT_STARTED,
            recovery_intent=None,
            predecessor_execution_id=None,
            recovery_evidence=None,
            retirement=None,
            created_at=now,
            updated_at=now,
        )
        capability = self._capability(key, execution, MachineAction.PREPARE_SESSION)
        response = _admission_response(execution, MachineAction.PREPARE_SESSION)
        record = _request_record(command, digest, now, capability)
        event, audit = self._machine_event_audit(
            execution,
            command,
            event_id=self._id(),
            prior_phase=None,
            decision="ADMITTED",
            command_summary="reserve READY_TO_RUN job and prepare one FluidNC session",
        )
        replay: MachineRequestRecord | None = None
        with self._repository.transaction() as transaction:
            current = transaction.get_job(command.job_id)
            self._validate_job_identity(current, command, require_ready=True)
            replay = transaction.get_request(key)
            if replay is None:
                transaction.create_execution(execution, event=event, audit=audit)
                reservation = transaction.reserve_request(record)
                assert reservation.created
                transaction.complete_request(key, response=response, completed_at=now)
        if replay is not None:
            return self._replay(replay, command, digest)
        return _AdmittedMachineAction(capability, validated.program, validated.snapshot, response)

    def _admit_recover(
        self,
        command: RecoverMachineCommand,
        *,
        still_requested: Callable[[], bool] | None,
    ) -> MachineAdmissionResult:
        self._require_configured_requester(command.requester)
        self._require_requested(still_requested)
        key = _request_key(command)
        digest = _argument_digest(command)
        existing = self._repository.get_request(key)
        if existing is not None:
            return self._replay(existing, command, digest)
        current = self._require_recovery_owner(command.execution_id)
        validate_recovery_disposition(cast(RecoveryIntent, current.recovery_intent), command.disposition)
        if command.mode is MachineCommandMode.REQUEST:
            return self._request_recovery_challenge(command, current, key, digest)
        return self._execute_recovery(command, current, key, digest, still_requested)

    def _admit_motion(
        self,
        command: HomeMachineCommand
        | ZCalMachineCommand
        | ZConfirmMachineCommand
        | StreamMachineCommand
        | HomeZMachineCommand,
        *,
        still_requested: Callable[[], bool] | None,
    ) -> MachineAdmissionResult:
        self._require_configured_requester(command.requester)
        self._require_requested(still_requested)
        key = _request_key(command)
        digest = _argument_digest(command)
        existing = self._repository.get_request(key)
        if existing is not None:
            return self._replay(existing, command, digest)
        action = _motion_action(command)
        current = self._require_motion_owner(command.execution_id, action)
        validated = self._validate_motion_ready(current)
        evidence = self._require_session_evidence(current)
        if action is MachineAction.STREAM:
            expected_wpos = (
                self._config.gcode.paper_center_x,
                self._config.gcode.paper_center_y,
                self._config.gcode.pen_up_z,
            )
            snapshot = _session_preflight_snapshot(evidence)
            if (
                evaluate_calibrated_pose(
                    snapshot,
                    expected_wpos=expected_wpos,
                    tolerance_mm=self._config.position_tolerance_mm,
                ).disposition
                is not GateDisposition.READY
            ):
                _fail("STREAM_CALIBRATED_POSE_REQUIRED", "STREAM requires the exact same-session calibrated pose")
        if command.mode is MachineCommandMode.REQUEST:
            return self._request_motion_challenge(command, current, action, evidence, key, digest)
        assert command.challenge_id is not None
        challenge = self._repository.get_challenge(command.challenge_id)
        if challenge is None:
            _fail("MACHINE_APPROVAL_NOT_FOUND", "motion approval challenge does not exist")
        consume_approval(
            challenge,
            current_binding=_motion_binding(current, action),
            consumer=command.requester,
            operator_principal=self._operator_principal,
            wall_now=self._now(),
            monotonic_now=self._monotonic_now(),
        )
        self._require_requested(still_requested)
        return self._execute_motion(command, current, action, key, digest, validated)

    def _request_motion_challenge(
        self,
        command: HomeMachineCommand
        | ZCalMachineCommand
        | ZConfirmMachineCommand
        | StreamMachineCommand
        | HomeZMachineCommand,
        execution: MachineExecution,
        action: MachineAction,
        evidence: MachineSessionSnapshot,
        key: MachineRequestKey,
        digest: str,
    ) -> MachineCommandResult:
        binding = _motion_binding(execution, action)
        now = self._now()
        monotonic = self._monotonic_now()
        challenge = issue_approval(
            challenge_id=self._id(),
            binding=binding,
            requester=command.requester,
            automation_principal=self._automation_principal,
            operator_principal=self._operator_principal,
            evidence=MachineApprovalEvidenceV1(1, action, execution.phase, evidence),
            issued_at=now,
            monotonic_now=monotonic,
            ttl_seconds=self._config.approval_ttl_seconds,
        )
        response: JsonObject = {
            "schema_version": 1,
            "deduplicated": False,
            "executed": False,
            "execution_id": execution.execution_id,
            "phase": execution.phase.value,
            "challenge": challenge.to_json(),
        }
        record = _request_record(command, digest, now, None, execution=execution, action=action)
        event, audit = self._machine_event_audit(
            execution,
            command,
            event_id=self._id(),
            prior_phase=execution.phase,
            decision="CHALLENGE_ISSUED",
            command_summary=f"issue one {action.value} challenge without adapter I/O",
            challenge=challenge,
            issuer_pid=command.requester.pid,
            occurred_at=now,
        )
        replay: MachineRequestRecord | None = None
        with self._repository.transaction() as transaction:
            self._validate_motion_job(transaction.get_job(execution.job_id), execution)
            reservation = transaction.reserve_request(record)
            if reservation.created:
                pending = transaction.get_pending_challenge(execution.execution_id, action)
                replacement = None
                if pending is not None:
                    try:
                        replacement = supersede_approval(pending, wall_now=now, monotonic_now=monotonic)
                    except DrawingMachineError as error:
                        if error.payload.code != "MACHINE_APPROVAL_EXPIRED":
                            raise
                        replacement = expire_approval(pending, wall_now=now, monotonic_now=monotonic)
                transaction.issue_challenge(challenge, replacement=replacement, event=event, audit=audit)
                transaction.complete_request(key, response=response, completed_at=now)
            else:
                replay = reservation.record
        if replay is not None:
            return cast(MachineCommandResult, self._replay(replay, command, digest))
        return MachineCommandResult(response, False)

    def _execute_motion(
        self,
        command: HomeMachineCommand
        | ZCalMachineCommand
        | ZConfirmMachineCommand
        | StreamMachineCommand
        | HomeZMachineCommand,
        execution: MachineExecution,
        action: MachineAction,
        key: MachineRequestKey,
        digest: str,
        validated: _ValidatedReadyAuthority,
    ) -> MachineAdmissionResult:
        in_progress_phase = _motion_in_progress_phase(action)
        now = self._now()
        admitted_execution = replace(
            execution,
            revision=execution.revision + 1,
            phase=in_progress_phase,
            updated_at=now,
        )
        capability = self._capability(key, admitted_execution, action)
        record = _request_record(command, digest, now, capability)
        replay: MachineRequestRecord | None = None
        response: JsonObject | None = None
        consumed: MachineApprovalChallenge | None = None
        with self._repository.transaction() as transaction:
            self._validate_motion_job(transaction.get_job(execution.job_id), execution)
            reservation = transaction.reserve_request(record)
            if reservation.created:
                challenge = transaction.get_challenge(cast(str, command.challenge_id))
                if challenge is None:
                    _fail("MACHINE_APPROVAL_NOT_FOUND", "motion approval challenge does not exist")
                decision = consume_approval(
                    challenge,
                    current_binding=_motion_binding(execution, action),
                    consumer=command.requester,
                    operator_principal=self._operator_principal,
                    wall_now=self._now(),
                    monotonic_now=self._monotonic_now(),
                )
                consumed = decision.result
                response = _admission_response(admitted_execution, action, consumed)
                event, audit = self._machine_event_audit(
                    admitted_execution,
                    command,
                    event_id=self._id(),
                    prior_phase=execution.phase,
                    decision="ADMITTED",
                    command_summary=f"consume one {action.value} challenge and commit before adapter I/O",
                    challenge=consumed,
                    issuer_pid=consumed.requester.pid,
                    consumer_pid=cast(AuthenticatedMachinePrincipal, consumed.consumer).pid,
                )
                transaction.decide_challenge(decision)
                transaction.transition_execution(execution.revision, admitted_execution, event=event, audit=audit)
                transaction.complete_request(key, response=response, completed_at=now)
            else:
                replay = reservation.record
        if replay is not None:
            return self._replay(replay, command, digest)
        assert response is not None and consumed is not None
        return _AdmittedMachineAction(capability, validated.program, validated.snapshot, response, consumed)

    def _request_recovery_challenge(
        self,
        command: RecoverMachineCommand,
        execution: MachineExecution,
        key: MachineRequestKey,
        digest: str,
    ) -> MachineCommandResult:
        binding = _recovery_binding(execution, command.disposition, service_epoch=self._authority.service_epoch)
        now = self._now()
        monotonic = self._monotonic_now()
        challenge = issue_approval(
            challenge_id=self._id(),
            binding=binding,
            requester=command.requester,
            automation_principal=self._automation_principal,
            operator_principal=self._operator_principal,
            evidence=MachineApprovalEvidenceV1(
                1,
                MachineAction.RECOVER,
                MachinePhase.RECOVERY_REQUIRED,
                None,
            ),
            issued_at=now,
            monotonic_now=monotonic,
            ttl_seconds=self._config.approval_ttl_seconds,
        )
        response: JsonObject = {
            "schema_version": 1,
            "deduplicated": False,
            "executed": False,
            "execution_id": execution.execution_id,
            "phase": execution.phase.value,
            "challenge": challenge.to_json(),
        }
        record = _request_record(command, digest, now, None, execution=execution)
        event, audit = self._machine_event_audit(
            execution,
            command,
            event_id=self._id(),
            prior_phase=execution.phase,
            decision="CHALLENGE_ISSUED",
            command_summary="issue one RECOVER challenge without adapter I/O",
            challenge=challenge,
            issuer_pid=command.requester.pid,
            occurred_at=now,
        )
        replay: MachineRequestRecord | None = None
        with self._repository.transaction() as transaction:
            current = transaction.get_job(execution.job_id)
            self._validate_recovery_job(current, execution, artifact_required=False)
            reservation = transaction.reserve_request(record)
            if reservation.created:
                pending = transaction.get_pending_challenge(execution.execution_id, MachineAction.RECOVER)
                replacement = None
                if pending is not None:
                    try:
                        replacement = supersede_approval(pending, wall_now=now, monotonic_now=monotonic)
                    except DrawingMachineError as error:
                        if error.payload.code != "MACHINE_APPROVAL_EXPIRED":
                            raise
                        replacement = expire_approval(pending, wall_now=now, monotonic_now=monotonic)
                transaction.issue_challenge(challenge, replacement=replacement, event=event, audit=audit)
                transaction.complete_request(key, response=response, completed_at=now)
            else:
                replay = reservation.record
        if replay is not None:
            return cast(MachineCommandResult, self._replay(replay, command, digest))
        return MachineCommandResult(response, False)

    def _execute_recovery(
        self,
        command: RecoverMachineCommand,
        execution: MachineExecution,
        key: MachineRequestKey,
        digest: str,
        still_requested: Callable[[], bool] | None,
    ) -> MachineAdmissionResult:
        assert command.challenge_id is not None
        challenge = self._repository.get_challenge(command.challenge_id)
        if challenge is None:
            _fail("MACHINE_APPROVAL_NOT_FOUND", "recovery approval challenge does not exist")
        consume_approval(
            challenge,
            current_binding=_recovery_binding(
                execution, command.disposition, service_epoch=self._authority.service_epoch
            ),
            consumer=command.requester,
            operator_principal=self._operator_principal,
            wall_now=self._now(),
            monotonic_now=self._monotonic_now(),
        )
        if command.disposition is RecoveryDisposition.RELEASE:
            return self._release_recovery(command, execution, key, digest)
        if self._retained_sessions:
            raise _error_value(
                "SESSION_OWNERSHIP_UNCERTAIN",
                "a recovery successor is forbidden while prior session ownership is retained",
            )
        self._require_requested(still_requested)
        validated = self._validate_recovery_ready(execution)
        self._require_requested(still_requested)
        return self._transfer_recovery(command, execution, key, digest, validated)

    def _release_recovery(
        self,
        command: RecoverMachineCommand,
        execution: MachineExecution,
        key: MachineRequestKey,
        digest: str,
    ) -> MachineCommandResult:
        now = self._now()
        released = replace(
            execution,
            revision=execution.revision + 1,
            retirement=OwnerRetirementEvidence(
                execution.execution_id,
                RetirementDisposition.RELEASED,
                now,
            ),
            updated_at=now,
        )
        response: JsonObject = {
            "schema_version": 1,
            "deduplicated": False,
            "executed": True,
            "execution_id": released.execution_id,
            "phase": released.phase.value,
            "released": True,
        }
        record = _request_record(command, digest, now, None, execution=execution)
        replay: MachineRequestRecord | None = None
        with self._repository.transaction() as transaction:
            current_job = transaction.get_job(execution.job_id)
            self._validate_recovery_job(current_job, execution, artifact_required=False)
            reservation = transaction.reserve_request(record)
            if reservation.created:
                decision = self._consume_live_recovery_challenge(
                    transaction,
                    command,
                    execution,
                )
                consumed = decision.result
                event, audit = self._machine_event_audit(
                    released,
                    command,
                    event_id=self._id(),
                    prior_phase=execution.phase,
                    decision="RELEASED",
                    command_summary="retire recovery latch without artifact read or adapter I/O",
                    challenge=consumed,
                    issuer_pid=consumed.requester.pid,
                    consumer_pid=cast(AuthenticatedMachinePrincipal, consumed.consumer).pid,
                )
                transaction.decide_challenge(decision)
                transaction.release_owner(execution.revision, released, event=event, audit=audit)
                transaction.complete_request(key, response=response, completed_at=now)
            else:
                replay = reservation.record
        if replay is not None:
            return cast(MachineCommandResult, self._replay(replay, command, digest))
        return MachineCommandResult(response, False)

    def _transfer_recovery(
        self,
        command: RecoverMachineCommand,
        predecessor: MachineExecution,
        key: MachineRequestKey,
        digest: str,
        validated: _ValidatedReadyAuthority,
    ) -> MachineAdmissionResult:
        now = self._now()
        successor = MachineExecution(
            schema_version=predecessor.schema_version,
            execution_id=self._id(),
            job_id=predecessor.job_id,
            ready_revision=predecessor.ready_revision,
            revision=0,
            phase=MachinePhase.PREPARING_SESSION,
            service_epoch=self._authority.service_epoch,
            machine_session_epoch=self._id(),
            gcode_sha256=predecessor.gcode_sha256,
            application_version=predecessor.application_version,
            application_digest=predecessor.application_digest,
            machine_digest=predecessor.machine_digest,
            provider_digest=predecessor.provider_digest,
            stream_milestone=predecessor.stream_milestone,
            recovery_intent=predecessor.recovery_intent,
            predecessor_execution_id=predecessor.execution_id,
            recovery_evidence=None,
            retirement=None,
            created_at=now,
            updated_at=now,
        )
        retired = replace(
            predecessor,
            revision=predecessor.revision + 1,
            retirement=OwnerRetirementEvidence(
                predecessor.execution_id,
                RetirementDisposition.TRANSFERRED,
                now,
                successor.execution_id,
            ),
            updated_at=now,
        )
        capability = self._capability(
            key,
            successor,
            MachineAction.RECOVER,
            disposition=command.disposition,
            predecessor=predecessor,
        )
        record = _request_record(command, digest, now, capability)
        replay: MachineRequestRecord | None = None
        response: JsonObject | None = None
        consumed: MachineApprovalChallenge | None = None
        with self._repository.transaction() as transaction:
            current_job = transaction.get_job(predecessor.job_id)
            self._validate_recovery_job(current_job, predecessor, artifact_required=True)
            replay = transaction.get_request(key)
            if replay is None:
                decision = self._consume_live_recovery_challenge(
                    transaction,
                    command,
                    predecessor,
                )
                consumed = decision.result
                response = _admission_response(successor, MachineAction.RECOVER, consumed)
                predecessor_event, predecessor_audit = self._machine_event_audit(
                    retired,
                    command,
                    event_id=self._id(),
                    prior_phase=predecessor.phase,
                    decision="TRANSFERRED",
                    command_summary="atomically transfer the recovery safety latch to one successor",
                    challenge=consumed,
                    issuer_pid=consumed.requester.pid,
                    consumer_pid=cast(AuthenticatedMachinePrincipal, consumed.consumer).pid,
                )
                successor_event, successor_audit = self._machine_event_audit(
                    successor,
                    command,
                    event_id=self._id(),
                    prior_phase=None,
                    decision="ADMITTED",
                    command_summary="admit intent-specific recovery successor before session open",
                    challenge=consumed,
                    issuer_pid=consumed.requester.pid,
                    consumer_pid=cast(AuthenticatedMachinePrincipal, consumed.consumer).pid,
                )
                transaction.decide_challenge(decision)
                transaction.transfer_owner(
                    predecessor.revision,
                    retired,
                    successor,
                    predecessor_event=predecessor_event,
                    successor_event=successor_event,
                    predecessor_audit=predecessor_audit,
                    successor_audit=successor_audit,
                )
                reservation = transaction.reserve_request(record)
                assert reservation.created
                transaction.complete_request(key, response=response, completed_at=now)
        if replay is not None:
            return self._replay(replay, command, digest)
        assert response is not None and consumed is not None
        return _AdmittedMachineAction(capability, validated.program, validated.snapshot, response, consumed)

    def _commit_preflight(
        self,
        admission: _AdmittedMachineAction,
        outcome: PreflightOutcome,
        *,
        owned_session: FluidNCSession,
    ) -> MachineExecution:
        current = self._repository.get_execution(admission.execution_id)
        if current is None or current.revision != admission.capability.execution_revision:
            _fail("MACHINE_ADMISSION_STALE", "preflight execution authority changed")
        snapshot = _machine_session_snapshot(current, outcome, self._now())
        gate = evaluate_preflight(outcome.snapshot, require_g54=self._config.require_g54_offset)
        safe = (
            outcome.failure is None
            and outcome.decision.disposition in {GateDisposition.READY, GateDisposition.AWAIT_HOME_ONLY}
            and gate.disposition is GateDisposition.READY
            and snapshot is not None
        )
        if not safe:
            return self._commit_recovery_or_fatal(
                current,
                session=owned_session,
                commit=lambda: self._commit_recovery(current, admission, outcome=outcome, session=snapshot),
            )
        now = self._now()
        result = replace(
            current,
            revision=current.revision + 1,
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            updated_at=now,
        )
        event, audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="PREFLIGHT_CONFIRMED_UNHOMED",
            command_summary="record fresh same-session preflight and await separate HOME approval",
        )
        assert snapshot is not None
        with self._repository.transaction() as transaction:
            transaction.record_opened_session(
                current.revision,
                result,
                session=snapshot,
                event=event,
                audit=audit,
            )
        return result

    def _drive_motion(self, admission: _AdmittedMachineAction) -> MachineExecution:
        current = self._repository.get_execution(admission.execution_id)
        if current is None or current.revision != admission.capability.execution_revision:
            _fail("MACHINE_ADMISSION_STALE", "motion execution authority changed before dispatch")
        session = self._sessions.get(current.execution_id)
        if session is None or current.machine_session_epoch in self._retained_sessions:
            result = self._commit_motion_recovery(
                current,
                admission,
                session=None,
                exception=_error_value(
                    "SESSION_OWNERSHIP_UNCERTAIN",
                    "the admitted same-session FluidNC handle is unavailable",
                ),
            )
            self._sessions.pop(result.execution_id, None)
            self._session_evidence.pop(result.execution_id, None)
            return result
        evidence = self._session_evidence.get(current.execution_id)
        if evidence is None or evidence.controller_state != "Idle":
            result = self._commit_motion_recovery(
                current,
                admission,
                session=session,
                exception=_error_value(
                    "MOTION_PRECONDITION_UNSAFE",
                    "motion dispatch requires exact same-session Idle evidence",
                ),
            )
            self._sessions.pop(result.execution_id, None)
            self._session_evidence.pop(result.execution_id, None)
            self._close_failed_session(session, result, admission)
            return result
        action = admission.capability.action
        request: HomeAllAxesRequest | ZCalibrationRequest | ZConfirmationRequest
        outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome
        accepted_outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome | None = None
        session_closed = False
        session_retained = False
        try:
            if action is MachineAction.HOME:
                request = HomeAllAxesRequest(
                    self._config.expected_homed_mpos,
                    self._config.position_tolerance_mm,
                )
                outcome = session.home_all_axes(request)
                if type(outcome) is not HomeOutcome or outcome.request != request:
                    raise _error_value("HOME_OUTCOME_INVALID", "HOME returned an invalid typed outcome")
            elif action is MachineAction.ZCAL:
                gcode = self._config.gcode
                request = ZCalibrationRequest(
                    gcode.work_coordinate,
                    gcode.paper_center_x,
                    gcode.paper_center_y,
                    gcode.pen_up_z,
                    gcode.pen_down_z,
                    gcode.feed_travel,
                    gcode.feed_pen_down,
                )
                outcome = session.run_z_calibration(request)
                if type(outcome) is not ZCalibrationOutcome or outcome.request != request:
                    raise _error_value("ZCAL_OUTCOME_INVALID", "ZCAL returned an invalid typed outcome")
            else:
                assert action is MachineAction.ZCONFIRM
                gcode = self._config.gcode
                request = ZConfirmationRequest(
                    (gcode.paper_center_x, gcode.paper_center_y, gcode.pen_up_z),
                    gcode.pen_up_z,
                    gcode.feed_pen_up,
                    self._config.position_tolerance_mm,
                )
                outcome = session.raise_after_z_confirmation(request)
                if type(outcome) is not ZConfirmationOutcome or outcome.request != request:
                    raise _error_value("ZCONFIRM_OUTCOME_INVALID", "ZCONFIRM returned an invalid typed outcome")
            if not _reserved_motion_proof_authority_is_valid(action, outcome):
                raise _error_value(
                    f"{action.value}_OUTCOME_INVALID",
                    f"{action.value} returned cross-action or wrong-stage reserved proof evidence",
                )
            accepted_outcome = outcome
            if outcome.machine_session_epoch != current.machine_session_epoch:
                raise _error_value(
                    "MOTION_OUTCOME_EPOCH_MISMATCH",
                    "motion outcome did not bind the admitted same-session epoch",
                )
            if outcome.failure is not None:
                result = self._commit_motion_recovery(current, admission, session=session, outcome=outcome)
            else:
                snapshot = _motion_session_snapshot(current, outcome, self._now())
                if snapshot is None or snapshot.controller_state != "Idle":
                    raise _error_value(
                        "MOTION_POSTCONDITION_INVALID",
                        "motion success requires complete same-session Idle position evidence",
                    )
                post_stream_home = (
                    action is MachineAction.HOME
                    and current.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
                    and current.predecessor_execution_id is not None
                )
                if post_stream_home:
                    close_error = self._close_session_once(session, current.machine_session_epoch)
                    self._sessions.pop(current.execution_id, None)
                    self._session_evidence.pop(current.execution_id, None)
                    if close_error is not None:
                        self._retained_sessions[current.machine_session_epoch] = session
                        session_retained = True
                        raise _error_value(close_error, "post-stream HOME session close was uncertain")
                    session_closed = True
                result = self._commit_motion_success(current, admission, snapshot)
        except Exception as error:
            latest = self._repository.get_execution(current.execution_id)
            if latest is None:
                raise
            if latest.phase is MachinePhase.RECOVERY_REQUIRED:
                return latest
            if latest.revision != current.revision or latest.phase is not current.phase:
                raise
            result = self._commit_motion_recovery(
                current,
                admission,
                session=session,
                outcome=accepted_outcome,
                exception=error,
            )
        if result.phase is MachinePhase.RECOVERY_REQUIRED:
            self._sessions.pop(result.execution_id, None)
            self._session_evidence.pop(result.execution_id, None)
            if not session_closed and not session_retained:
                self._close_failed_session(session, result, admission)
        return result

    def _drive_stream(self, admission: _AdmittedMachineAction) -> MachineExecution:
        current = self._repository.get_execution(admission.execution_id)
        if (
            current is None
            or current.revision != admission.capability.execution_revision
            or current.phase is not MachinePhase.STREAMING
            or current.stream_milestone is not StreamMilestone.NOT_STARTED
        ):
            _fail("MACHINE_ADMISSION_STALE", "STREAM execution authority changed before dispatch")
        session = self._sessions.get(current.execution_id)
        evidence = self._session_evidence.get(current.execution_id)
        accepted_evidence = _validated_drive_session_evidence(current, evidence)
        if session is None or accepted_evidence is None or current.machine_session_epoch in self._retained_sessions:
            result = self._commit_stream_recovery_or_fatal(
                current,
                admission,
                exception=_error_value(
                    "SESSION_OWNERSHIP_UNCERTAIN",
                    "the admitted same-session STREAM handle is unavailable",
                ),
            )
            self._sessions.pop(current.execution_id, None)
            self._session_evidence.pop(current.execution_id, None)
            if session is not None and current.machine_session_epoch not in self._retained_sessions:
                self._close_failed_session(session, result, admission)
            return result
        evidence = accepted_evidence
        snapshot = _session_preflight_snapshot(evidence)
        expected_wpos = (
            self._config.gcode.paper_center_x,
            self._config.gcode.paper_center_y,
            self._config.gcode.pen_up_z,
        )
        if (
            evidence.controller_state != "Idle"
            or evaluate_calibrated_pose(
                snapshot,
                expected_wpos=expected_wpos,
                tolerance_mm=self._config.position_tolerance_mm,
            ).disposition
            is not GateDisposition.READY
        ):
            result = self._commit_stream_recovery_or_fatal(
                current,
                admission,
                exception=_error_value(
                    "STREAM_PRECONDITION_UNSAFE",
                    "STREAM dispatch requires exact same-session Idle calibrated-pose evidence",
                ),
            )
            self._sessions.pop(current.execution_id, None)
            self._session_evidence.pop(current.execution_id, None)
            self._close_failed_session(session, result, admission)
            return result

        write_possible = replace(
            current,
            revision=current.revision + 1,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            updated_at=self._now(),
        )
        event, audit = self._machine_event_audit_from_capability(
            write_possible,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="STREAM_WRITE_POSSIBLE",
            command_summary="durably mark the irreversible STREAM write boundary before typed adapter dispatch",
        )
        with self._repository.transaction() as transaction:
            transaction.transition_execution(current.revision, write_possible, event=event, audit=audit)
        current = write_possible
        total = len(admission.program.commands)
        self._last_progress_checkpoint[current.execution_id] = (0, self._monotonic_now())
        progress_lifetime = _StreamProgressLifetime()

        def record_progress(update: StreamProgressUpdate) -> None:
            if (
                type(update) is not StreamProgressUpdate
                or update.machine_session_epoch != current.machine_session_epoch
            ):
                _fail("STREAM_PROGRESS_INVALID", "STREAM progress must bind the exact session epoch")
            previous = self._pending_progress.get(current.execution_id)
            expected_ack = 1 if previous is None else previous.acknowledged_lines + 1
            if update.acknowledged_commands != expected_ack or expected_ack > total:
                _fail("STREAM_PROGRESS_INVALID", "STREAM progress must advance one acknowledged command at a time")
            command = admission.program.commands[expected_ack - 1]
            if (
                update.source_line != command.source_line
                or update.command_digest != hashlib.sha256(command.command.encode("ascii")).hexdigest()
            ):
                _fail("STREAM_PROGRESS_INVALID", "STREAM progress does not match the immutable program prefix")
            now = self._now()
            value = MachineProgress(
                1,
                current.execution_id,
                current.revision,
                total,
                expected_ack,
                expected_ack,
                0,
                expected_ack * 100.0 / total,
                command.source_line,
                command.command,
                now,
            )
            checkpoint_ack, checkpoint_at = self._last_progress_checkpoint[current.execution_id]
            monotonic = self._monotonic_now()
            self._pending_progress[current.execution_id] = value
            if expected_ack == total or expected_ack - checkpoint_ack >= 100 or monotonic - checkpoint_at >= 2.0:
                with self._repository.transaction() as transaction:
                    transaction.append_progress(self._id(), value)
                self._last_progress_checkpoint[current.execution_id] = (expected_ack, monotonic)
                self._progress[current.execution_id] = value
                self._publish_observation(current.execution_id)

        def progress(update: StreamProgressUpdate) -> None:
            progress_lifetime.apply(lambda: record_progress(update))

        stream_outcome: StreamOutcome | None = None
        session_closed = False
        session_retained = False
        try:
            try:
                raw_stream_outcome = session.stream_program(StreamProgramRequest(admission.program), progress)
            finally:
                progress_lifetime.expire()
            stream_outcome = _validated_stream_outcome(
                raw_stream_outcome,
                expected_epoch=current.machine_session_epoch,
                expected_total=total,
            )
            observed_ack = 0
            if current.execution_id in self._pending_progress:
                observed_ack = self._pending_progress[current.execution_id].acknowledged_lines
            if stream_outcome.acknowledged_commands != observed_ack:
                raise _error_value(
                    "STREAM_PROGRESS_OUTCOME_MISMATCH",
                    "STREAM outcome does not match the exact acknowledged progress prefix",
                )
            if stream_outcome.failure is not None:
                result = self._commit_stream_recovery_or_fatal(
                    current,
                    admission,
                    outcome=stream_outcome,
                )
            else:
                assert stream_outcome.final_snapshot is not None
                terminal_session = _machine_session_snapshot_from_preflight(
                    current,
                    stream_outcome.final_snapshot,
                    self._now(),
                )
                if self._config.post_run_home_z:
                    result = self._commit_stream_confirmed(
                        current,
                        admission,
                        MachinePhase.AWAITING_HOME_Z_APPROVAL,
                    )
                    self._session_evidence[result.execution_id] = terminal_session
                else:
                    close_error = self._close_session_once(session, current.machine_session_epoch)
                    self._sessions.pop(current.execution_id, None)
                    self._session_evidence.pop(current.execution_id, None)
                    if close_error is not None:
                        self._retained_sessions[current.machine_session_epoch] = session
                        session_retained = True
                        raise _error_value(close_error, "confirmed STREAM session close was uncertain")
                    session_closed = True
                    result = self._commit_stream_confirmed(
                        current,
                        admission,
                        MachinePhase.COMPLETED,
                    )
        except Exception as error:
            if _is_cleaned_coordinator_fatal(error):
                raise
            latest = self._repository.get_execution(current.execution_id)
            if latest is None:
                raise
            if latest.phase is MachinePhase.RECOVERY_REQUIRED:
                result = latest
            elif latest.phase in {MachinePhase.COMPLETED, MachinePhase.AWAITING_HOME_Z_APPROVAL}:
                raise
            else:
                confirmed = stream_outcome is not None and stream_outcome.failure is None
                result = self._commit_stream_recovery_or_fatal(
                    latest,
                    admission,
                    outcome=stream_outcome,
                    exception=error,
                    confirmed=confirmed,
                )
        if result.phase is MachinePhase.RECOVERY_REQUIRED:
            self._sessions.pop(result.execution_id, None)
            self._session_evidence.pop(result.execution_id, None)
            if not session_closed and not session_retained:
                self._close_failed_session(session, result, admission)
        self._pending_progress.pop(current.execution_id, None)
        self._last_progress_checkpoint.pop(current.execution_id, None)
        return result

    def _drive_home_z(self, admission: _AdmittedMachineAction) -> MachineExecution:
        current = self._repository.get_execution(admission.execution_id)
        if (
            current is None
            or current.revision != admission.capability.execution_revision
            or current.phase is not MachinePhase.HOMING_Z
            or current.stream_milestone is not StreamMilestone.STREAM_CONFIRMED
        ):
            _fail("MACHINE_ADMISSION_STALE", "HOME_Z execution authority changed before dispatch")
        session = self._sessions.get(current.execution_id)
        evidence = self._session_evidence.get(current.execution_id)
        accepted_evidence = _validated_drive_session_evidence(current, evidence)
        if session is None or accepted_evidence is None:
            result = self._commit_stream_recovery_or_fatal(
                current,
                admission,
                exception=_error_value(
                    "HOME_Z_PRECONDITION_UNSAFE", "HOME_Z requires retained same-session Idle proof"
                ),
                confirmed=True,
            )
            self._sessions.pop(current.execution_id, None)
            self._session_evidence.pop(current.execution_id, None)
            if session is not None and current.machine_session_epoch not in self._retained_sessions:
                self._close_failed_session(session, result, admission)
            return result
        evidence = accepted_evidence
        outcome: HomeZOutcome | None = None
        session_closed = False
        session_retained = False
        try:
            request = HomeZAxisRequest()
            raw_outcome = session.home_z_axis(request)
            outcome = _validated_home_z_outcome(
                raw_outcome,
                expected_epoch=current.machine_session_epoch,
                expected_request=request,
            )
            if outcome.failure is not None:
                result = self._commit_stream_recovery_or_fatal(
                    current,
                    admission,
                    home_z_outcome=outcome,
                    confirmed=True,
                )
            else:
                close_error = self._close_session_once(session, current.machine_session_epoch)
                self._sessions.pop(current.execution_id, None)
                self._session_evidence.pop(current.execution_id, None)
                if close_error is not None:
                    self._retained_sessions[current.machine_session_epoch] = session
                    session_retained = True
                    raise _error_value(close_error, "HOME_Z session close was uncertain")
                session_closed = True
                result = self._commit_stream_confirmed(
                    current,
                    admission,
                    MachinePhase.COMPLETED,
                )
        except Exception as error:
            if _is_cleaned_coordinator_fatal(error):
                raise
            latest = self._repository.get_execution(current.execution_id)
            if latest is None:
                raise
            if latest.phase is MachinePhase.RECOVERY_REQUIRED:
                result = latest
            elif latest.phase is MachinePhase.COMPLETED:
                raise
            else:
                result = self._commit_stream_recovery_or_fatal(
                    latest,
                    admission,
                    home_z_outcome=outcome,
                    exception=error,
                    confirmed=True,
                )
        if result.phase is MachinePhase.RECOVERY_REQUIRED:
            self._sessions.pop(result.execution_id, None)
            self._session_evidence.pop(result.execution_id, None)
            if not session_closed and not session_retained and session is not None:
                self._close_failed_session(session, result, admission)
        return result

    def _commit_stream_confirmed(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        result_phase: MachinePhase,
    ) -> MachineExecution:
        now = self._now()
        completed = result_phase is MachinePhase.COMPLETED
        result = replace(
            current,
            revision=current.revision + 1,
            phase=result_phase,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            retirement=(
                OwnerRetirementEvidence(current.execution_id, RetirementDisposition.COMPLETED, now)
                if completed
                else None
            ),
            updated_at=now,
        )
        if completed:
            try:
                return self._commit_completed_outcome(current, result, admission)
            except Exception:
                code = (
                    "STREAM_COMPLETION_COMMIT_UNCERTAIN"
                    if admission.capability.action is MachineAction.STREAM
                    else "HOME_Z_COMPLETION_COMMIT_UNCERTAIN"
                )
                return self._commit_stream_recovery_or_fatal(
                    current,
                    admission,
                    exception=_error_value(code, "confirmed post-stream completion could not be committed"),
                    confirmed=True,
                )
        event, audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="STREAM_CONFIRMED",
            command_summary="commit every STREAM ACK and final Idle proof before awaiting separate HOME_Z approval",
        )
        try:
            with self._repository.transaction() as transaction:
                transaction.transition_execution(current.revision, result, event=event, audit=audit)
        except Exception:
            return self._commit_stream_recovery_or_fatal(
                current,
                admission,
                exception=_error_value(
                    "STREAM_CONFIRMATION_COMMIT_UNCERTAIN",
                    "confirmed STREAM successor phase could not be committed",
                ),
                confirmed=True,
            )
        return result

    def _commit_stream_recovery_or_fatal(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        *,
        outcome: StreamOutcome | None = None,
        home_z_outcome: HomeZOutcome | None = None,
        exception: BaseException | None = None,
        confirmed: bool = False,
    ) -> MachineExecution:
        fatal_signal: _CleanedCoordinatorFatal | None = None
        try:
            return self._commit_stream_recovery(
                current,
                admission,
                outcome=outcome,
                home_z_outcome=home_z_outcome,
                exception=exception,
                confirmed=confirmed,
            )
        except Exception:
            owned_session = self._sessions.pop(current.execution_id, None)
            self._session_evidence.pop(current.execution_id, None)
            self._progress.pop(current.execution_id, None)
            self._pending_progress.pop(current.execution_id, None)
            self._last_progress_checkpoint.pop(current.execution_id, None)
            if owned_session is not None and current.machine_session_epoch not in self._retained_sessions:
                close_error = self._close_session_once(owned_session, current.machine_session_epoch)
                if close_error is not None:
                    self._retained_sessions[current.machine_session_epoch] = owned_session
            fatal_signal = _new_cleaned_coordinator_fatal()
        assert fatal_signal is not None
        raise fatal_signal

    def _commit_stream_recovery(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        *,
        outcome: StreamOutcome | None = None,
        home_z_outcome: HomeZOutcome | None = None,
        exception: BaseException | None = None,
        confirmed: bool = False,
    ) -> MachineExecution:
        now = self._now()
        milestone = StreamMilestone.STREAM_CONFIRMED if confirmed else current.stream_milestone
        failure = _stream_failure_evidence(outcome, home_z_outcome, exception)
        if (
            not confirmed
            and current.phase is MachinePhase.STREAMING
            and current.stream_milestone is StreamMilestone.FIRST_WRITE_POSSIBLE
        ):
            self._persist_stream_failure_progress(current, admission, failure, outcome, now)
        recovery_intent = recovery_intent_for_milestone(milestone)
        recovery = MachineRecoveryEvidence(
            1,
            current.execution_id,
            recovery_intent,
            milestone,
            failure.code,
            failure,
            now,
        )
        result = replace(
            current,
            revision=current.revision + 1,
            phase=MachinePhase.RECOVERY_REQUIRED,
            stream_milestone=milestone,
            recovery_intent=recovery_intent,
            recovery_evidence=recovery,
            updated_at=now,
        )
        machine_outcome = MachineOutcome(
            1,
            result.execution_id,
            result.job_id,
            result.revision,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            admission.ready_snapshot,
            recovery,
            now,
        )
        job = self._repository_job(result.job_id)
        transition = MachineJobOutcomeTransition(
            result.job_id,
            job.revision,
            JobState.RECOVERY_REQUIRED,
            "machine.recovery_required",
            admission.ready_snapshot,
        )
        machine_event, machine_audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="RECOVERY_REQUIRED",
            command_summary="stop without retry or resume after admitted STREAM or HOME_Z uncertainty",
            error_code=failure.code,
        )
        job_event, job_audit = self._job_outcome_event_audit(
            job,
            result,
            admission,
            transition,
            failure.code,
            now,
        )
        with self._repository.transaction() as transaction:
            transaction.commit_machine_outcome(
                current.revision,
                result,
                outcome=machine_outcome,
                job_transition=transition,
                machine_event=machine_event,
                machine_audit=machine_audit,
                job_event=job_event,
                job_audit=job_audit,
            )
        return result

    def _persist_stream_failure_progress(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        failure: MachineFailureEvidenceV1,
        outcome: StreamOutcome | None,
        occurred_at: datetime,
    ) -> None:
        existing = self._progress.get(current.execution_id)
        pending = self._pending_progress.get(current.execution_id)
        acknowledged = (
            outcome.acknowledged_commands
            if outcome is not None
            else (
                pending.acknowledged_lines
                if pending is not None
                else (0 if existing is None else existing.acknowledged_lines)
            )
        )
        total = len(admission.program.commands)
        if acknowledged < 0 or acknowledged > total:
            _fail("STREAM_PROGRESS_INVALID", "failed STREAM acknowledged prefix is outside the immutable program")
        if (
            pending is not None
            and pending.acknowledged_lines == acknowledged
            and failure.code in {"FAKE_FLUIDNC_PROGRESS_CALLBACK_FAILED", "PROGRESS_CALLBACK_FAILED"}
        ):
            assert pending.last_source_line is not None and pending.last_command is not None
            source_line = pending.last_source_line
            command_text = pending.last_command
        else:
            failed_index = min(acknowledged, total - 1)
            source_line = admission.program.commands[failed_index].source_line
            command_text = admission.program.commands[failed_index].command
        value = MachineProgress(
            1,
            current.execution_id,
            current.revision,
            total,
            acknowledged,
            acknowledged,
            1,
            acknowledged * 100.0 / total,
            source_line,
            command_text,
            occurred_at,
            failure,
        )
        if existing == value:
            return
        with self._repository.transaction() as transaction:
            transaction.append_progress(self._id(), value)
        self._progress[current.execution_id] = value
        self._publish_observation(current.execution_id)
        self._pending_progress.pop(current.execution_id, None)

    def _commit_motion_success(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        snapshot: MachineSessionSnapshot,
    ) -> MachineExecution:
        action = admission.capability.action
        now = self._now()
        if (
            action is MachineAction.HOME
            and current.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME
            and current.predecessor_execution_id is not None
        ):
            result = replace(
                current,
                revision=current.revision + 1,
                phase=MachinePhase.COMPLETED,
                retirement=OwnerRetirementEvidence(
                    current.execution_id,
                    RetirementDisposition.COMPLETED,
                    now,
                ),
                updated_at=now,
            )
            return self._commit_completed_outcome(current, result, admission)
        next_phase = {
            MachineAction.HOME: MachinePhase.AWAITING_ZCAL_APPROVAL,
            MachineAction.ZCAL: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
            MachineAction.ZCONFIRM: MachinePhase.AWAITING_STREAM_APPROVAL,
        }[action]
        result = replace(
            current,
            revision=current.revision + 1,
            phase=next_phase,
            updated_at=now,
        )
        event, audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="CONFIRMED",
            command_summary=f"commit exact {action.value} adapter outcome and postcondition proof",
        )
        with self._repository.transaction() as transaction:
            transaction.transition_execution(current.revision, result, event=event, audit=audit)
        self._session_evidence[result.execution_id] = snapshot
        return result

    def _commit_motion_recovery(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        *,
        session: FluidNCSession | None,
        outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome | None = None,
        exception: BaseException | None = None,
    ) -> MachineExecution:
        return self._commit_recovery_or_fatal(
            current,
            session=session,
            commit=lambda: self._commit_motion_recovery_transaction(
                current,
                admission,
                outcome=outcome,
                exception=exception,
            ),
        )

    def _commit_motion_recovery_transaction(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        *,
        outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome | None = None,
        exception: BaseException | None = None,
    ) -> MachineExecution:
        now = self._now()
        failure, motion = _motion_recovery_details(
            current,
            admission.capability.action,
            outcome,
            exception,
            now,
        )
        recovery_intent = recovery_intent_for_milestone(current.stream_milestone)
        recovery = MachineRecoveryEvidence(
            1,
            current.execution_id,
            recovery_intent,
            current.stream_milestone,
            failure.code,
            failure,
            now,
            motion,
        )
        result = replace(
            current,
            revision=current.revision + 1,
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=recovery_intent,
            recovery_evidence=recovery,
            updated_at=now,
        )
        machine_outcome = MachineOutcome(
            1,
            result.execution_id,
            result.job_id,
            result.revision,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            admission.ready_snapshot,
            recovery,
            now,
        )
        job = self._repository_job(result.job_id)
        transition = MachineJobOutcomeTransition(
            result.job_id,
            job.revision,
            JobState.RECOVERY_REQUIRED,
            "machine.recovery_required",
            admission.ready_snapshot,
        )
        machine_event, machine_audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="RECOVERY_REQUIRED",
            command_summary=f"fail closed after one admitted {admission.capability.action.value} dispatch",
            error_code=failure.code,
        )
        job_event, job_audit = self._job_outcome_event_audit(
            job,
            result,
            admission,
            transition,
            failure.code,
            now,
        )
        with self._repository.transaction() as transaction:
            transaction.commit_machine_outcome(
                current.revision,
                result,
                outcome=machine_outcome,
                job_transition=transition,
                machine_event=machine_event,
                machine_audit=machine_audit,
                job_event=job_event,
                job_audit=job_audit,
            )
        return result

    def _commit_completed_outcome(
        self,
        current: MachineExecution,
        result: MachineExecution,
        admission: _AdmittedMachineAction,
    ) -> MachineExecution:
        now = result.updated_at
        machine_outcome = MachineOutcome(
            1,
            result.execution_id,
            result.job_id,
            result.revision,
            MachineOutcomeKind.COMPLETED,
            admission.ready_snapshot,
            None,
            now,
        )
        job = self._repository_job(result.job_id)
        transition = MachineJobOutcomeTransition(
            result.job_id,
            job.revision,
            JobState.COMPLETED,
            "machine.completed",
            admission.ready_snapshot,
        )
        event, audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="COMPLETED",
            command_summary="commit post-stream recovery HOME proof directly to COMPLETED",
        )
        job_event, job_audit = self._job_outcome_event_audit(job, result, admission, transition, None, now)
        with self._repository.transaction() as transaction:
            transaction.commit_machine_outcome(
                current.revision,
                result,
                outcome=machine_outcome,
                job_transition=transition,
                machine_event=event,
                machine_audit=audit,
                job_event=job_event,
                job_audit=job_audit,
            )
        return result

    def _commit_preflight_exception(
        self,
        admission: _AdmittedMachineAction,
        error: BaseException,
        *,
        session: FluidNCSession | None,
    ) -> MachineExecution:
        current = self._repository.get_execution(admission.execution_id)
        assert current is not None
        return self._commit_recovery_or_fatal(
            current,
            session=session,
            commit=lambda: self._commit_recovery(current, admission, exception=error),
        )

    def _commit_recovery_or_fatal(
        self,
        current: MachineExecution,
        *,
        session: FluidNCSession | None,
        commit: Callable[[], MachineExecution],
    ) -> MachineExecution:
        fatal_signal: _CleanedCoordinatorFatal | None = None
        try:
            return commit()
        except Exception:
            mapped_session = self._sessions.pop(current.execution_id, None)
            self._session_evidence.pop(current.execution_id, None)
            self._progress.pop(current.execution_id, None)
            self._pending_progress.pop(current.execution_id, None)
            self._last_progress_checkpoint.pop(current.execution_id, None)
            owned_session = session if session is not None else mapped_session
            if owned_session is not None and current.machine_session_epoch not in self._retained_sessions:
                close_error = self._close_session_once(owned_session, current.machine_session_epoch)
                if close_error is not None:
                    self._retained_sessions[current.machine_session_epoch] = owned_session
            fatal_signal = _new_cleaned_coordinator_fatal()
        assert fatal_signal is not None
        raise fatal_signal

    def _commit_recovery(
        self,
        current: MachineExecution,
        admission: _AdmittedMachineAction,
        *,
        outcome: PreflightOutcome | None = None,
        session: MachineSessionSnapshot | None = None,
        exception: BaseException | None = None,
    ) -> MachineExecution:
        now = self._now()
        failure = _failure_evidence(outcome, exception)
        recovery_intent = recovery_intent_for_milestone(current.stream_milestone)
        recovery = MachineRecoveryEvidence(
            1,
            current.execution_id,
            recovery_intent,
            current.stream_milestone,
            failure.code,
            failure,
            now,
        )
        result = replace(
            current,
            revision=current.revision + 1,
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=recovery_intent,
            recovery_evidence=recovery,
            updated_at=now,
        )
        machine_outcome = MachineOutcome(
            1,
            result.execution_id,
            result.job_id,
            result.revision,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            admission.ready_snapshot,
            recovery,
            now,
        )
        job = self._repository_job(result.job_id)
        transition = MachineJobOutcomeTransition(
            result.job_id,
            job.revision,
            JobState.RECOVERY_REQUIRED,
            "machine.recovery_required",
            admission.ready_snapshot,
        )
        machine_event, machine_audit = self._machine_event_audit_from_capability(
            result,
            admission,
            event_id=self._id(),
            prior_phase=current.phase,
            decision="RECOVERY_REQUIRED",
            command_summary="fail closed after ambiguous or rejected initial session preflight",
            error_code=failure.code,
        )
        request_record = self._repository.get_request(admission.capability.key)
        assert request_record is not None
        requester = RequesterIdentity(
            "LOCAL_PEER",
            request_record.requester_pid,
            admission.capability.key.principal_uid,
            admission.capability.key.principal_gid,
        )
        job_event_id = self._id()
        job_event = JobEvent(
            job_event_id,
            job.job_id,
            job.revision + 1,
            transition.event_type,
            job.state,
            JobState.RECOVERY_REQUIRED,
            admission.capability.key.request_id,
            "LOCAL_PEER",
            {"requester": requester.to_json()},
            now,
        )
        job_audit = AuditRecord(
            job_event_id,
            transition.event_type,
            admission.capability.key.request_id,
            AuditPayloadV1(
                requester,
                job.job_id,
                "machine.prepare",
                transition.event_type,
                JobKind.OFFLINE_WORKFLOW,
                job.state,
                JobState.RECOVERY_REQUIRED,
                None,
                None,
                failure.code,
                None,
                result.application_version,
            ).to_json(),
        )
        with self._repository.transaction() as transaction:
            transaction.commit_machine_outcome(
                current.revision,
                result,
                outcome=machine_outcome,
                job_transition=transition,
                machine_event=machine_event,
                machine_audit=machine_audit,
                job_event=job_event,
                job_audit=job_audit,
                session=session,
            )
        return result

    def _validate_initial_ready(self, command: PrepareMachineCommand) -> _ValidatedReadyAuthority:
        job = self._repository_job(command.job_id)
        self._validate_job_identity(job, command, require_ready=True)
        assert job.ready_snapshot is not None
        return self._revalidate_snapshot(job, job.ready_snapshot, self._authority)

    def _validate_motion_ready(self, execution: MachineExecution) -> _ValidatedReadyAuthority:
        job = self._repository_job(execution.job_id)
        self._validate_motion_job(job, execution)
        assert job.ready_snapshot is not None
        return self._revalidate_snapshot(job, job.ready_snapshot, self._authority)

    def _validate_motion_job(self, job: JobRecord | None, execution: MachineExecution) -> None:
        expected_state = (
            JobState.RECOVERY_REQUIRED if execution.predecessor_execution_id is not None else JobState.READY_TO_RUN
        )
        if (
            job is None
            or type(job) is not JobRecord
            or job.kind is not JobKind.OFFLINE_WORKFLOW
            or job.job_id != execution.job_id
            or job.state is not expected_state
            or job.ready_snapshot is None
            or job.ready_snapshot.job_id != execution.job_id
            or job.ready_snapshot.job_revision != execution.ready_revision
            or job.ready_snapshot.gcode.sha256 != execution.gcode_sha256
            or job.config != job.ready_snapshot.config
            or job.ready_snapshot.application_version != execution.application_version
            or job.ready_snapshot.config.application_digest != execution.application_digest
            or job.ready_snapshot.config.machine_digest != execution.machine_digest
            or job.ready_snapshot.config.provider_digest != execution.provider_digest
            or execution.service_epoch != self._authority.service_epoch
        ):
            _fail("MACHINE_ACTION_AUTHORITY_MISMATCH", "motion action no longer matches exact job/runtime authority")

    def _require_motion_owner(self, execution_id: str, action: MachineAction) -> MachineExecution:
        current = self._repository.get_active_execution()
        required = _motion_prior_phase(action)
        if (
            current is None
            or current.execution_id != execution_id
            or current.phase is not required
            or current.retirement is not None
            or current.service_epoch != self._authority.service_epoch
        ):
            _fail("MACHINE_ACTION_ORDER_INVALID", f"{action.value} requires the current {required.value} owner")
        return current

    def _require_session_evidence(self, execution: MachineExecution) -> MachineSessionSnapshot:
        session = self._sessions.get(execution.execution_id)
        evidence = self._session_evidence.get(execution.execution_id)
        if session is None or evidence is None:
            _fail("MACHINE_SESSION_REQUIRED", "motion approval requires the retained same-session connection")
        if (
            evidence.execution_id != execution.execution_id
            or evidence.machine_session_epoch != execution.machine_session_epoch
            or not evidence.stabilized
            or evidence.controller_state != "Idle"
        ):
            _fail("MACHINE_SESSION_AUTHORITY_MISMATCH", "same-session approval evidence is stale or misbound")
        return evidence

    def _job_outcome_event_audit(
        self,
        job: JobRecord,
        execution: MachineExecution,
        admission: _AdmittedMachineAction,
        transition: MachineJobOutcomeTransition,
        error_code: str | None,
        occurred_at: datetime,
    ) -> tuple[JobEvent, AuditRecord]:
        request_record = self._repository.get_request(admission.capability.key)
        assert request_record is not None
        requester = RequesterIdentity(
            "LOCAL_PEER",
            request_record.requester_pid,
            admission.capability.key.principal_uid,
            admission.capability.key.principal_gid,
        )
        event_id = self._id()
        event = JobEvent(
            event_id,
            job.job_id,
            job.revision + 1,
            transition.event_type,
            job.state,
            transition.result_state,
            admission.capability.key.request_id,
            "LOCAL_PEER",
            {"requester": requester.to_json()},
            occurred_at,
        )
        audit = AuditRecord(
            event_id,
            transition.event_type,
            admission.capability.key.request_id,
            AuditPayloadV1(
                requester,
                job.job_id,
                request_record.command,
                transition.event_type,
                JobKind.OFFLINE_WORKFLOW,
                job.state,
                transition.result_state,
                None,
                None,
                error_code,
                None,
                execution.application_version,
            ).to_json(),
        )
        return event, audit

    def _validate_recovery_ready(
        self,
        execution: MachineExecution,
    ) -> _ValidatedReadyAuthority:
        job = self._repository_job(execution.job_id)
        self._validate_recovery_job(job, execution, artifact_required=True)
        assert job.ready_snapshot is not None
        return self._revalidate_snapshot(job, job.ready_snapshot, self._authority)

    def _revalidate_snapshot(
        self,
        job: JobRecord,
        snapshot: ReadyToRunSnapshot,
        authority: MachineRuntimeAuthority,
    ) -> _ValidatedReadyAuthority:
        _validate_snapshot_authority(snapshot, authority)
        source = self._artifact_reader.read_bytes(
            job.job_id,
            snapshot.gcode,
            expected_media_type="text/x.gcode",
            max_bytes=MAX_GCODE_BYTES,
        )
        if type(source) is not bytes or len(source) != snapshot.gcode.size_bytes:
            _fail("MACHINE_GCODE_IDENTITY_MISMATCH", "descriptor read size changed from the frozen artifact")
        if hashlib.sha256(source).hexdigest() != snapshot.gcode.sha256:
            _fail("MACHINE_GCODE_IDENTITY_MISMATCH", "descriptor read digest changed from the frozen artifact")
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _fail("MACHINE_GCODE_INVALID", "frozen G-code is not strict UTF-8")
        checked = check_candidate(
            text,
            profile=self._config.gcode,
            expected_gcode_sha256=snapshot.gcode.sha256,
        )
        frozen = snapshot.to_json()
        if checked.static_result.to_json() != frozen["static_result"]:
            _fail("MACHINE_STATIC_RESULT_MISMATCH", "recomputed static result differs from the frozen result")
        if checked.send_plan.to_json() != frozen["send_plan"]:
            _fail("MACHINE_SEND_PLAN_MISMATCH", "recomputed send plan differs from the frozen plan")
        if checked.readiness.to_json() != frozen["readiness"] or not checked.readiness.allow_stream:
            _fail("MACHINE_READINESS_MISMATCH", "recomputed readiness differs or blocks streaming")
        program = build_validated_stream_program(
            source,
            cast(dict[str, object], frozen["send_plan"]),
            snapshot.gcode.sha256,
        )
        return _ValidatedReadyAuthority(job, snapshot, program)

    def _validate_job_identity(
        self,
        job: JobRecord | None,
        command: PrepareMachineCommand,
        *,
        require_ready: bool,
    ) -> None:
        if job is None:
            _fail("MACHINE_JOB_NOT_FOUND", "machine admission job does not exist")
        if (
            type(job) is not JobRecord
            or job.kind is not JobKind.OFFLINE_WORKFLOW
            or job.job_id != command.job_id
            or job.revision != command.job_revision
            or (require_ready and job.state is not JobState.READY_TO_RUN)
            or job.ready_snapshot is None
            or job.ready_snapshot.job_id != command.job_id
            or job.ready_snapshot.job_revision != command.job_revision
            or job.config != job.ready_snapshot.config
        ):
            _fail("MACHINE_JOB_AUTHORITY_MISMATCH", "job is not the exact requested READY_TO_RUN authority")

    def _validate_recovery_job(
        self,
        job: JobRecord | None,
        execution: MachineExecution,
        *,
        artifact_required: bool,
    ) -> None:
        del artifact_required
        if (
            job is None
            or type(job) is not JobRecord
            or job.kind is not JobKind.OFFLINE_WORKFLOW
            or job.state is not JobState.RECOVERY_REQUIRED
            or job.ready_snapshot is None
            or job.ready_snapshot.job_id != execution.job_id
            or job.ready_snapshot.job_revision != execution.ready_revision
            or job.ready_snapshot.gcode.sha256 != execution.gcode_sha256
        ):
            _fail("MACHINE_RECOVERY_AUTHORITY_MISMATCH", "recovery job no longer matches frozen execution authority")

    def _require_recovery_owner(self, execution_id: str) -> MachineExecution:
        current = self._repository.get_active_execution()
        if (
            current is None
            or current.execution_id != execution_id
            or current.phase is not MachinePhase.RECOVERY_REQUIRED
            or current.recovery_intent is None
            or current.retirement is not None
        ):
            _fail("MACHINE_RECOVERY_OWNER_REQUIRED", "RECOVER requires the current active recovery latch owner")
        return current

    def _replay(
        self,
        record: MachineRequestRecord,
        command: MachineActionCommand,
        digest: str,
    ) -> MachineAdmissionResult:
        if record.command != _command_name(command) or record.argument_digest != digest:
            _fail("REQUEST_ID_CONFLICT", "request ID was already used with different arguments")
        if record.response is None:
            _fail("MACHINE_REQUEST_INCOMPLETE", "durable request admission has no completed response")
        response = _plain_json(record.response)
        response["deduplicated"] = True
        if record.admission_id is None or record.claimed_at is not None:
            return MachineCommandResult(response, True)
        execution = self._repository.get_execution(cast(str, record.execution_id))
        if execution is None:
            _fail("MACHINE_ADMISSION_STALE", "deduplicated admission execution is missing")
        job = self._repository_job(execution.job_id)
        assert job.ready_snapshot is not None
        validated = self._revalidate_snapshot(job, job.ready_snapshot, self._authority)
        capability = MachineAdmissionCapability(
            record.key,
            record.admission_id,
            cast(str, record.dispatch_nonce),
            execution.execution_id,
            MachineAction(cast(str, record.action)),
            cast(int, record.execution_revision),
            cast(str, record.service_epoch),
            cast(str, record.machine_session_epoch),
            record.recovery_disposition,
            record.recovery_intent,
            record.predecessor_execution_id,
        )
        approval: MachineApprovalChallenge | None = None
        if capability.action is not MachineAction.PREPARE_SESSION:
            challenge_id = response.get("approval_challenge_id")
            if type(challenge_id) is not str:
                _fail("MACHINE_APPROVAL_EVIDENCE_MISSING", "admission replay lacks consumed challenge authority")
            with self._repository.transaction() as transaction:
                approval = transaction.get_challenge(challenge_id)
            if approval is None or approval.status is not ApprovalStatus.CONSUMED:
                _fail("MACHINE_APPROVAL_EVIDENCE_MISSING", "admission replay consumed challenge is unavailable")
        return _AdmittedMachineAction(capability, validated.program, validated.snapshot, response, approval)

    def _consume_live_recovery_challenge(
        self,
        transaction: MachineRepositoryTransaction,
        command: RecoverMachineCommand,
        execution: MachineExecution,
    ) -> MachineApprovalDecision:
        challenge = transaction.get_challenge(cast(str, command.challenge_id))
        if challenge is None:
            _fail("MACHINE_APPROVAL_NOT_FOUND", "recovery approval challenge does not exist")
        return consume_approval(
            challenge,
            current_binding=_recovery_binding(
                execution, command.disposition, service_epoch=self._authority.service_epoch
            ),
            consumer=command.requester,
            operator_principal=self._operator_principal,
            wall_now=self._now(),
            monotonic_now=self._monotonic_now(),
        )

    def _machine_event_audit(
        self,
        execution: MachineExecution,
        command: MachineActionCommand,
        *,
        event_id: str,
        prior_phase: MachinePhase | None,
        decision: str,
        command_summary: str,
        challenge: MachineApprovalChallenge | None = None,
        issuer_pid: int | None = None,
        consumer_pid: int | None = None,
        error_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[MachineEvent, MachineAuditPayloadV2]:
        if type(command) is PrepareMachineCommand:
            action = MachineAction.PREPARE_SESSION
        elif type(command) is RecoverMachineCommand:
            action = MachineAction.RECOVER
        else:
            action = _motion_action(cast(HomeMachineCommand | ZCalMachineCommand | ZConfirmMachineCommand, command))
        disposition = command.disposition if type(command) is RecoverMachineCommand else None
        event = MachineEvent(
            1,
            event_id,
            execution.execution_id,
            execution.job_id,
            execution.revision,
            "machine.authority",
            execution.phase,
            action,
            MachineEventPayloadV2(command.request_id, prior_phase, execution.phase, error_code),
            execution.updated_at if occurred_at is None else occurred_at,
        )
        audit = MachineAuditPayloadV2(
            operation=_command_name(command),
            decision=decision,
            execution_id=execution.execution_id,
            job_id=execution.job_id,
            execution_revision=execution.revision,
            frozen_ready_revision=execution.ready_revision,
            request_id=command.request_id,
            event_id=event_id,
            requester_uid=command.requester.uid,
            requester_gid=command.requester.gid,
            requester_pid=command.requester.pid,
            issuer_pid=issuer_pid,
            consumer_pid=consumer_pid,
            prior_phase=prior_phase,
            result_phase=execution.phase,
            action=action,
            recovery_disposition=disposition,
            approval_challenge_id=None if challenge is None else challenge.challenge_id,
            challenge_issued_at=None if challenge is None else challenge.issued_at,
            approved_at=None if challenge is None else challenge.status_changed_at,
            application_digest=execution.application_digest,
            machine_digest=execution.machine_digest,
            provider_digest=execution.provider_digest,
            gcode_sha256=execution.gcode_sha256,
            service_epoch=execution.service_epoch,
            machine_session_epoch=execution.machine_session_epoch,
            application_version=execution.application_version,
            protocol_version=1,
            command_summary=command_summary,
            error_code=error_code,
        )
        return event, audit

    def _machine_event_audit_from_capability(
        self,
        execution: MachineExecution,
        admission: _AdmittedMachineAction,
        **kwargs: object,
    ) -> tuple[MachineEvent, MachineAuditPayloadV2]:
        record = self._repository.get_request(admission.capability.key)
        assert record is not None
        event_id = cast(str, kwargs["event_id"])
        prior_phase = cast(MachinePhase | None, kwargs["prior_phase"])
        decision = cast(str, kwargs["decision"])
        command_summary = cast(str, kwargs["command_summary"])
        error_code = cast(str | None, kwargs.get("error_code"))
        approval = admission.approval
        approval_consumer = None if approval is None else approval.consumer
        event = MachineEvent(
            1,
            event_id,
            execution.execution_id,
            execution.job_id,
            execution.revision,
            "machine.authority",
            execution.phase,
            admission.capability.action,
            MachineEventPayloadV2(record.key.request_id, prior_phase, execution.phase, error_code),
            execution.updated_at,
        )
        audit = MachineAuditPayloadV2(
            operation=record.command,
            decision=decision,
            execution_id=execution.execution_id,
            job_id=execution.job_id,
            execution_revision=execution.revision,
            frozen_ready_revision=execution.ready_revision,
            request_id=record.key.request_id,
            event_id=event_id,
            requester_uid=record.key.principal_uid,
            requester_gid=record.key.principal_gid,
            requester_pid=record.requester_pid,
            issuer_pid=None if approval is None else approval.requester.pid,
            consumer_pid=None if approval_consumer is None else approval_consumer.pid,
            prior_phase=prior_phase,
            result_phase=execution.phase,
            action=admission.capability.action,
            recovery_disposition=admission.capability.recovery_disposition,
            approval_challenge_id=None if approval is None else approval.challenge_id,
            challenge_issued_at=None if approval is None else approval.issued_at,
            approved_at=None if approval is None else approval.status_changed_at,
            application_digest=execution.application_digest,
            machine_digest=execution.machine_digest,
            provider_digest=execution.provider_digest,
            gcode_sha256=execution.gcode_sha256,
            service_epoch=execution.service_epoch,
            machine_session_epoch=execution.machine_session_epoch,
            application_version=execution.application_version,
            protocol_version=1,
            command_summary=command_summary,
            error_code=error_code,
        )
        return event, audit

    def _capability(
        self,
        key: MachineRequestKey,
        execution: MachineExecution,
        action: MachineAction,
        *,
        disposition: RecoveryDisposition | None = None,
        predecessor: MachineExecution | None = None,
    ) -> MachineAdmissionCapability:
        return MachineAdmissionCapability(
            key,
            self._id(),
            self._id(),
            execution.execution_id,
            action,
            execution.revision,
            execution.service_epoch,
            execution.machine_session_epoch,
            disposition,
            execution.recovery_intent if action is MachineAction.RECOVER else None,
            None if predecessor is None else predecessor.execution_id,
        )

    @property
    def _automation_principal(self) -> KernelMachinePrincipal:
        return KernelMachinePrincipal(
            self._config.automation_principal.uid,
            self._config.automation_principal.gid,
        )

    @property
    def _operator_principal(self) -> KernelMachinePrincipal:
        return KernelMachinePrincipal(
            self._config.operator_principal.uid,
            self._config.operator_principal.gid,
        )

    def _publish_observation(self, execution_id: str) -> None:
        publisher = self._projection_publisher
        if publisher is not None:
            publisher(self.status_projection(execution_id))

    def _require_configured_requester(self, requester: AuthenticatedMachinePrincipal) -> None:
        if requester.stable_principal not in {self._automation_principal, self._operator_principal}:
            _fail("MACHINE_REQUESTER_DENIED", "requester is not a configured machine principal")

    def _repository_job(self, job_id: str) -> JobRecord:
        with self._repository.transaction() as transaction:
            job = transaction.get_job(job_id)
        if job is None:
            _fail("MACHINE_JOB_NOT_FOUND", "machine job does not exist")
        return job

    def _require_requested(self, callback: Callable[[], bool] | None) -> None:
        if callback is not None and callback() is not True:
            _fail("MACHINE_ADMISSION_WITHDRAWN", "machine command was withdrawn before durable admission")

    def _require_preflight_outcome(self, value: object, epoch: str) -> PreflightOutcome:
        if type(value) is not PreflightOutcome:
            raise _error_value(
                "SESSION_PREFLIGHT_OUTCOME_INVALID",
                "FluidNC preflight returned an invalid typed outcome",
            )
        if value.machine_session_epoch != epoch:
            raise _error_value(
                "SESSION_PREFLIGHT_EPOCH_MISMATCH",
                "FluidNC preflight outcome did not bind the admitted session epoch",
            )
        return value

    def _close_failed_session(
        self,
        session: FluidNCSession,
        execution: MachineExecution,
        admission: _AdmittedMachineAction,
    ) -> None:
        error_code = self._close_session_once(session, execution.machine_session_epoch)
        if error_code is None:
            return
        self._retained_sessions[execution.machine_session_epoch] = session
        event, audit = self._machine_event_audit_from_capability(
            execution,
            admission,
            event_id=self._id(),
            prior_phase=execution.phase,
            decision="SESSION_OWNERSHIP_RETAINED",
            command_summary="retain uncertain FluidNC session ownership until coordinator shutdown",
            error_code=error_code,
        )
        fatal_signal: _CleanedCoordinatorFatal | None = None
        try:
            with self._repository.transaction() as transaction:
                transaction.record_lifecycle_event(execution, event=event, audit=audit)
        except Exception:
            fatal_signal = _new_cleaned_coordinator_fatal()
        if fatal_signal is not None:
            raise fatal_signal

    def _close_session_once(self, session: FluidNCSession, machine_session_epoch: str) -> str | None:
        try:
            outcome = session.close(CloseSessionRequest(machine_session_epoch))
        except Exception:
            return "SESSION_CLOSE_EXCEPTION"
        if type(outcome) is not CloseOutcome:
            return "SESSION_CLOSE_OUTCOME_INVALID"
        if outcome.machine_session_epoch != machine_session_epoch:
            return "SESSION_CLOSE_EPOCH_MISMATCH"
        if outcome.failure is not None:
            return outcome.failure.code
        return None

    def _now(self) -> datetime:
        value = self._wall_now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            _fail("MACHINE_CLOCK_INVALID", "machine wall clock must return an aware datetime")
        return value

    def _id(self) -> str:
        value = self._identity_factory()
        if type(value) is not str or not value:
            _fail("MACHINE_IDENTITY_INVALID", "machine identity source returned an invalid value")
        try:
            parsed = UUID(value)
        except ValueError:
            _fail("MACHINE_IDENTITY_INVALID", "machine identity source must return canonical UUID4 values")
        if parsed.version != 4 or str(parsed) != value:
            _fail("MACHINE_IDENTITY_INVALID", "machine identity source must return canonical UUID4 values")
        return value


def _request_key(command: MachineActionCommand) -> MachineRequestKey:
    return MachineRequestKey(command.requester.uid, command.requester.gid, command.request_id)


def _command_name(command: MachineActionCommand) -> str:
    return {
        PrepareMachineCommand: "machine.prepare",
        HomeMachineCommand: "machine.home",
        ZCalMachineCommand: "machine.zcal",
        ZConfirmMachineCommand: "machine.zconfirm",
        StreamMachineCommand: "machine.stream",
        HomeZMachineCommand: "machine.home-z",
        RecoverMachineCommand: "machine.recover",
    }[type(command)]


def _argument_digest(command: MachineActionCommand) -> str:
    if type(command) is PrepareMachineCommand:
        arguments: JsonObject = {
            "schema_version": 1,
            "job_id": command.job_id,
            "job_revision": command.job_revision,
        }
    elif type(command) is RecoverMachineCommand:
        recover = command
        arguments = {
            "schema_version": 1,
            "execution_id": recover.execution_id,
            "disposition": recover.disposition.value,
            "mode": recover.mode.value,
            "challenge_id": recover.challenge_id,
        }
    else:
        motion = cast(
            HomeMachineCommand
            | ZCalMachineCommand
            | ZConfirmMachineCommand
            | StreamMachineCommand
            | HomeZMachineCommand,
            command,
        )
        arguments = {
            "schema_version": 1,
            "execution_id": motion.execution_id,
            "mode": motion.mode.value,
            "challenge_id": motion.challenge_id,
        }
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _request_record(
    command: MachineActionCommand,
    digest: str,
    now: datetime,
    capability: MachineAdmissionCapability | None,
    *,
    execution: MachineExecution | None = None,
    action: MachineAction | None = None,
) -> MachineRequestRecord:
    bound = execution
    if capability is not None:
        return MachineRequestRecord(
            key=capability.key,
            requester_pid=command.requester.pid,
            command=_command_name(command),
            argument_digest=digest,
            execution_id=capability.execution_id,
            action=capability.action.value,
            admission_id=capability.admission_id,
            dispatch_nonce=capability.dispatch_nonce,
            response=None,
            reserved_at=now,
            execution_revision=capability.execution_revision,
            service_epoch=capability.service_epoch,
            machine_session_epoch=capability.machine_session_epoch,
            recovery_disposition=capability.recovery_disposition,
            recovery_intent=capability.recovery_intent,
            predecessor_execution_id=capability.predecessor_execution_id,
        )
    return MachineRequestRecord(
        key=_request_key(command),
        requester_pid=command.requester.pid,
        command=_command_name(command),
        argument_digest=digest,
        execution_id=None if bound is None else bound.execution_id,
        action=None if bound is None else (MachineAction.RECOVER if action is None else action).value,
        admission_id=None,
        dispatch_nonce=None,
        response=None,
        reserved_at=now,
        execution_revision=None if bound is None else bound.revision,
        service_epoch=None if bound is None else bound.service_epoch,
        machine_session_epoch=None if bound is None else bound.machine_session_epoch,
        recovery_disposition=(
            command.disposition if bound is not None and type(command) is RecoverMachineCommand else None
        ),
        recovery_intent=(
            bound.recovery_intent if bound is not None and type(command) is RecoverMachineCommand else None
        ),
    )


def _admission_response(
    execution: MachineExecution,
    action: MachineAction,
    approval: MachineApprovalChallenge | None = None,
) -> JsonObject:
    response: JsonObject = {
        "schema_version": 1,
        "deduplicated": False,
        "admitted": True,
        "execution_id": execution.execution_id,
        "job_id": execution.job_id,
        "job_revision": execution.ready_revision,
        "execution_revision": execution.revision,
        "phase": execution.phase.value,
        "action": action.value,
    }
    if approval is not None:
        consumer = approval.consumer
        assert consumer is not None
        response.update(
            {
                "approval_challenge_id": approval.challenge_id,
                "challenge_issued_at": approval.issued_at.isoformat(),
                "approved_at": cast(datetime, approval.status_changed_at).isoformat(),
                "issuer_pid": approval.requester.pid,
                "consumer_pid": consumer.pid,
            }
        )
    return response


def _recovery_binding(
    execution: MachineExecution, disposition: RecoveryDisposition, *, service_epoch: str
) -> MachineApprovalBinding:
    return MachineApprovalBinding(
        execution.execution_id,
        execution.revision,
        MachineAction.RECOVER,
        disposition,
        MachinePhase.RECOVERY_REQUIRED,
        service_epoch,
        execution.machine_session_epoch,
        execution.job_id,
        execution.ready_revision,
        execution.application_digest,
        execution.machine_digest,
        execution.provider_digest,
        execution.gcode_sha256,
    )


def _motion_action(
    command: HomeMachineCommand
    | ZCalMachineCommand
    | ZConfirmMachineCommand
    | StreamMachineCommand
    | HomeZMachineCommand,
) -> MachineAction:
    return {
        HomeMachineCommand: MachineAction.HOME,
        ZCalMachineCommand: MachineAction.ZCAL,
        ZConfirmMachineCommand: MachineAction.ZCONFIRM,
        StreamMachineCommand: MachineAction.STREAM,
        HomeZMachineCommand: MachineAction.HOME_Z,
    }[type(command)]


def _motion_prior_phase(action: MachineAction) -> MachinePhase:
    return {
        MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
        MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
    }[action]


def _motion_in_progress_phase(action: MachineAction) -> MachinePhase:
    return {
        MachineAction.HOME: MachinePhase.HOMING,
        MachineAction.ZCAL: MachinePhase.Z_CALIBRATING,
        MachineAction.ZCONFIRM: MachinePhase.Z_CONFIRMING,
        MachineAction.STREAM: MachinePhase.STREAMING,
        MachineAction.HOME_Z: MachinePhase.HOMING_Z,
    }[action]


def _motion_binding(execution: MachineExecution, action: MachineAction) -> MachineApprovalBinding:
    return MachineApprovalBinding(
        execution.execution_id,
        execution.revision,
        action,
        None,
        _motion_prior_phase(action),
        execution.service_epoch,
        execution.machine_session_epoch,
        execution.job_id,
        execution.ready_revision,
        execution.application_digest,
        execution.machine_digest,
        execution.provider_digest,
        execution.gcode_sha256,
    )


def _validate_snapshot_authority(snapshot: ReadyToRunSnapshot, authority: MachineRuntimeAuthority) -> None:
    config = snapshot.config
    if (
        snapshot.application_version != authority.application_version
        or config.application_schema_version != authority.application_schema_version
        or config.machine_schema_version != authority.machine_schema_version
        or config.provider_schema_version != authority.provider_schema_version
        or config.application_digest != authority.application_digest
        or config.machine_digest != authority.machine_digest
        or config.provider_digest != authority.provider_digest
        or config.machine_profile != authority.machine_profile
        or config.provider_profile != authority.provider_profile
    ):
        _fail("MACHINE_CONFIG_AUTHORITY_MISMATCH", "current runtime authority differs from the frozen snapshot")


def _completed_positions(
    snapshot: PreflightSnapshot,
) -> tuple[Vector3 | None, Vector3 | None, Vector3 | None]:
    # Grbl/FluidNC status reports carry MPos XOR WPos (mutually exclusive, selected by the
    # status-report mask), and WCO only periodically. Values actually present in the report
    # always win; derivation only fills absent fields. WCO falls back to the observed G54 offset
    # (this product never issues G92/G10/TLO, so WCO is identical to the active G54 offset).
    status = snapshot.status
    mpos, wpos, wco = status.mpos, status.wpos, status.wco
    if wco is None:
        wco = snapshot.g54
    if wco is not None:
        if wpos is None and mpos is not None:
            wpos = tuple(round(m - o, 3) for m, o in zip(mpos, wco, strict=True))  # type: ignore[assignment]
        elif mpos is None and wpos is not None:
            mpos = tuple(round(w + o, 3) for w, o in zip(wpos, wco, strict=True))  # type: ignore[assignment]
    return mpos, wpos, wco


def _machine_session_snapshot(
    execution: MachineExecution,
    outcome: PreflightOutcome,
    observed_at: datetime,
) -> MachineSessionSnapshot | None:
    status = outcome.snapshot.status
    mpos, wpos, wco = _completed_positions(outcome.snapshot)
    if mpos is None or wpos is None or wco is None or outcome.snapshot.g54 is None:
        return None
    return MachineSessionSnapshot(
        1,
        execution.execution_id,
        outcome.machine_session_epoch,
        status.state_text,
        mpos,
        wpos,
        wco,
        outcome.snapshot.g54,
        True,
        observed_at,
    )


def _machine_session_snapshot_from_preflight(
    execution: MachineExecution,
    snapshot: PreflightSnapshot,
    observed_at: datetime,
) -> MachineSessionSnapshot:
    status = snapshot.status
    mpos, wpos, wco = _completed_positions(snapshot)
    if mpos is None or wpos is None or wco is None or snapshot.g54 is None:
        _fail("STREAM_POSTCONDITION_INVALID", "STREAM final Idle proof lacks complete position and G54 evidence")
    return MachineSessionSnapshot(
        1,
        execution.execution_id,
        execution.machine_session_epoch,
        status.state_text,
        mpos,
        wpos,
        wco,
        snapshot.g54,
        True,
        observed_at,
    )


def _session_preflight_snapshot(session: MachineSessionSnapshot) -> PreflightSnapshot:
    def vector(value: tuple[float, float, float]) -> str:
        return ",".join(format(item, ".3f") for item in value)

    return parse_preflight_snapshot(
        status_line=(
            f"<{session.controller_state}|MPos:{vector(session.mpos)}|"
            f"WPos:{vector(session.wpos)}|WCO:{vector(session.wco)}>"
        ),
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(f"[G54:{vector(session.g54)}]",),
        errors=(),
        missing_ok_commands=(),
    )


def _validated_drive_session_evidence(
    execution: MachineExecution,
    value: MachineSessionSnapshot | None,
) -> MachineSessionSnapshot | None:
    if type(value) is not MachineSessionSnapshot:
        return None
    try:
        accepted = MachineSessionSnapshot(
            value.schema_version,
            value.execution_id,
            value.machine_session_epoch,
            value.controller_state,
            value.mpos,
            value.wpos,
            value.wco,
            value.g54,
            value.stabilized,
            value.observed_at,
        )
        if (
            accepted != value
            or accepted.execution_id != execution.execution_id
            or accepted.machine_session_epoch != execution.machine_session_epoch
            or not accepted.stabilized
            or accepted.controller_state != "Idle"
        ):
            return None
    except Exception:
        return None
    return accepted


def _canonical_preflight_snapshot(value: object) -> PreflightSnapshot:
    if type(value) is not PreflightSnapshot:
        raise TypeError("controller snapshot must be exact")
    snapshot = value
    if (
        type(snapshot.status) is not ControllerStatus
        or type(snapshot.parser_state) is not tuple
        or type(snapshot.offset_lines) is not tuple
        or type(snapshot.offsets) is not tuple
        or type(snapshot.errors) is not tuple
        or type(snapshot.missing_ok_commands) is not tuple
        or any(type(item) is not WorkOffset for item in snapshot.offsets)
    ):
        raise TypeError("controller snapshot nested values must be exact")
    rebuilt = parse_preflight_snapshot(
        status_line=snapshot.status.status_line,
        parser_state=snapshot.parser_state,
        offset_lines=snapshot.offset_lines,
        errors=snapshot.errors,
        missing_ok_commands=snapshot.missing_ok_commands,
    )
    if rebuilt != snapshot:
        raise TypeError("controller snapshot contradicts its raw evidence")
    return rebuilt


def _canonical_fluidnc_failure(value: object) -> FluidNCFailure | None:
    if value is None:
        return None
    if type(value) is not FluidNCFailure:
        raise TypeError("FluidNC failure must be exact")
    failure = value
    rebuilt = FluidNCFailure(
        failure.kind,
        failure.code,
        failure.message,
        failure.side_effect_may_have_occurred,
    )
    return rebuilt


def _validated_stream_outcome(
    value: object,
    *,
    expected_epoch: str,
    expected_total: int,
) -> StreamOutcome:
    try:
        if type(value) is not StreamOutcome:
            raise TypeError("STREAM outcome must be exact")
        outcome = value
        snapshot = None if outcome.final_snapshot is None else _canonical_preflight_snapshot(outcome.final_snapshot)
        failure = _canonical_fluidnc_failure(outcome.failure)
        accepted = StreamOutcome(
            outcome.machine_session_epoch,
            outcome.total_commands,
            outcome.acknowledged_commands,
            snapshot,
            failure,
            outcome.evidence,
            outcome.observation_phase,
            outcome.decision,
        )
        if (
            accepted != outcome
            or accepted.machine_session_epoch != expected_epoch
            or accepted.total_commands != expected_total
        ):
            raise TypeError("STREAM outcome does not match admitted authority")
        return accepted
    except Exception:
        raise _error_value("STREAM_OUTCOME_INVALID", "STREAM returned an invalid closed typed outcome") from None


def _validated_home_z_outcome(
    value: object,
    *,
    expected_epoch: str,
    expected_request: HomeZAxisRequest,
) -> HomeZOutcome:
    try:
        if type(value) is not HomeZOutcome:
            raise TypeError("HOME_Z outcome must be exact")
        outcome = value
        snapshot = None if outcome.snapshot is None else _canonical_preflight_snapshot(outcome.snapshot)
        failure = _canonical_fluidnc_failure(outcome.failure)
        accepted = HomeZOutcome(
            outcome.machine_session_epoch,
            outcome.request,
            snapshot,
            failure,
            outcome.evidence,
            outcome.observation_phase,
            outcome.decision,
        )
        if (
            accepted != outcome
            or accepted.request != expected_request
            or accepted.machine_session_epoch != expected_epoch
        ):
            raise TypeError("HOME_Z outcome does not match admitted authority")
        return accepted
    except Exception:
        raise _error_value("HOME_Z_OUTCOME_INVALID", "HOME_Z returned an invalid closed typed outcome") from None


def _failure_evidence(
    outcome: PreflightOutcome | None,
    exception: BaseException | None,
) -> MachineFailureEvidenceV1:
    if outcome is not None and outcome.failure is not None:
        failure = outcome.failure
        return MachineFailureEvidenceV1(
            1,
            failure.code,
            failure.message,
            outcome.snapshot.status.state_text,
            failure.side_effect_may_have_occurred,
        )
    if outcome is not None:
        code = "PREFLIGHT_REJECTED"
        message = "; ".join(outcome.decision.blockers) or "initial preflight did not establish safe HOME-only authority"
        return MachineFailureEvidenceV1(1, code, message, outcome.snapshot.status.state_text, False)
    if isinstance(exception, DrawingMachineError) and exception.payload.code in {
        "SESSION_OWNERSHIP_UNCERTAIN",
        "SESSION_PREFLIGHT_EPOCH_MISMATCH",
        "SESSION_PREFLIGHT_OUTCOME_INVALID",
    }:
        return MachineFailureEvidenceV1(
            1,
            exception.payload.code,
            exception.payload.message,
            None,
            True,
        )
    return MachineFailureEvidenceV1(
        1,
        "SESSION_OPEN_AMBIGUOUS",
        "session open or initial preflight failed with sanitized ambiguous evidence",
        None,
        True,
    )


def _stream_failure_evidence(
    outcome: StreamOutcome | None,
    home_z_outcome: HomeZOutcome | None,
    exception: BaseException | None,
) -> MachineFailureEvidenceV1:
    typed = outcome if outcome is not None else home_z_outcome
    if typed is not None and typed.failure is not None:
        snapshot = outcome.final_snapshot if outcome is not None else cast(HomeZOutcome, typed).snapshot
        return MachineFailureEvidenceV1(
            1,
            typed.failure.code,
            typed.failure.message,
            None if snapshot is None else snapshot.status.state_text,
            typed.failure.side_effect_may_have_occurred,
        )
    if isinstance(exception, DrawingMachineError):
        if exception.payload.code == _COORDINATOR_FATAL_PERSISTENCE:
            return MachineFailureEvidenceV1(
                1,
                "STREAM_OR_HOME_Z_UNCERTAIN",
                "STREAM or HOME_Z failed with sanitized ambiguous evidence",
                None,
                True,
            )
        return MachineFailureEvidenceV1(
            1,
            exception.payload.code,
            exception.payload.message,
            None,
            True,
        )
    return MachineFailureEvidenceV1(
        1,
        "STREAM_OR_HOME_Z_UNCERTAIN",
        "STREAM or HOME_Z failed with sanitized ambiguous evidence",
        None,
        True,
    )


def _motion_session_snapshot(
    execution: MachineExecution,
    outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome,
    observed_at: datetime,
) -> MachineSessionSnapshot | None:
    source = outcome.snapshot
    if source is None:
        return None
    status = source.status
    mpos, wpos, wco = _completed_positions(source)
    if mpos is None or wpos is None or wco is None or source.g54 is None:
        return None
    return MachineSessionSnapshot(
        1,
        execution.execution_id,
        outcome.machine_session_epoch,
        status.state_text,
        mpos,
        wpos,
        wco,
        source.g54,
        True,
        observed_at,
    )


def _motion_recovery_details(
    execution: MachineExecution,
    action: MachineAction,
    outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome | None,
    exception: BaseException | None,
    observed_at: datetime,
) -> tuple[MachineFailureEvidenceV1, MachineMotionRecoveryEvidenceV1]:
    snapshot = None if outcome is None else _motion_session_snapshot(execution, outcome, observed_at)
    maximum = {MachineAction.HOME: 1, MachineAction.ZCAL: 6, MachineAction.ZCONFIRM: 1}[action]
    observed_epoch = None if outcome is None else outcome.machine_session_epoch
    acknowledged: int | None
    if outcome is not None and outcome.failure is not None:
        failure = outcome.failure
        controller_state = None if outcome.snapshot is None else outcome.snapshot.status.state_text
        acknowledged, step = _typed_motion_progress(action, outcome)
        postcondition_failure = snapshot is not None and _is_exact_motion_proof_failure(action, outcome)
        return (
            MachineFailureEvidenceV1(
                1,
                failure.code,
                failure.message,
                controller_state,
                failure.side_effect_may_have_occurred,
            ),
            MachineMotionRecoveryEvidenceV1(
                1,
                execution.execution_id,
                action,
                (
                    MachineMotionEvidenceKind.POSTCONDITION_REJECTED
                    if postcondition_failure
                    else MachineMotionEvidenceKind.TYPED_FAILURE
                ),
                (_motion_postcondition_step(action) if postcondition_failure else step),
                acknowledged,
                execution.machine_session_epoch,
                observed_epoch,
                snapshot,
                False,
            ),
        )
    code = exception.payload.code if isinstance(exception, DrawingMachineError) else None
    if code in {"SESSION_OWNERSHIP_UNCERTAIN", "MOTION_PRECONDITION_UNSAFE"}:
        kind = MachineMotionEvidenceKind.PRECONDITION_REJECTED
        step = MachineMotionStep.ACTION_ADMITTED
        acknowledged = 0
        observed_epoch = None
        snapshot = None
        proven = False
    elif code is not None and (
        code.endswith("_OUTCOME_INVALID") or code in {"MOTION_OUTCOME_EPOCH_MISMATCH", "MOTION_POSTCONDITION_INVALID"}
    ):
        kind = MachineMotionEvidenceKind.INVALID_OUTCOME
        step = MachineMotionStep.UNKNOWN
        acknowledged = None
        snapshot = None
        proven = False
    elif code is not None and ("CLOSE" in code or code == "SAFE_HOME_CLOSE_UNCERTAIN"):
        kind = MachineMotionEvidenceKind.CLOSE_UNCERTAIN
        step = MachineMotionStep.SESSION_CLOSE
        acknowledged = maximum
        proven = snapshot is not None
    elif outcome is not None:
        kind = MachineMotionEvidenceKind.COMMIT_UNCERTAIN
        step = MachineMotionStep.COMMIT_AFTER_SIDE_EFFECT
        acknowledged = maximum
        proven = snapshot is not None and snapshot.controller_state == "Idle"
    else:
        kind = MachineMotionEvidenceKind.ADAPTER_UNCERTAIN
        step = MachineMotionStep.UNKNOWN
        acknowledged = None
        proven = False
    if isinstance(exception, DrawingMachineError):
        failure_evidence = MachineFailureEvidenceV1(1, exception.payload.code, exception.payload.message, None, True)
    else:
        failure_evidence = MachineFailureEvidenceV1(
            1,
            f"{action.value}_AMBIGUOUS",
            f"{action.value} adapter dispatch or durable outcome commit failed with sanitized ambiguous evidence",
            None,
            True,
        )
    return (
        failure_evidence,
        MachineMotionRecoveryEvidenceV1(
            1,
            execution.execution_id,
            action,
            kind,
            step,
            acknowledged,
            execution.machine_session_epoch,
            observed_epoch,
            snapshot,
            proven,
        ),
    )


def _typed_motion_progress(
    action: MachineAction,
    outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome,
) -> tuple[int, MachineMotionStep]:
    if type(outcome) is ZCalibrationOutcome:
        acknowledged = outcome.acknowledged_steps
        zcal_steps = {
            ZCalibrationOutcomeStage.METRIC_ATTEMPTED: MachineMotionStep.ZCAL_METRIC,
            ZCalibrationOutcomeStage.METRIC_ACKNOWLEDGED: MachineMotionStep.ZCAL_METRIC_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.ABSOLUTE_ATTEMPTED: MachineMotionStep.ZCAL_ABSOLUTE,
            ZCalibrationOutcomeStage.ABSOLUTE_ACKNOWLEDGED: MachineMotionStep.ZCAL_ABSOLUTE_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.WORK_COORDINATE_ATTEMPTED: MachineMotionStep.ZCAL_WORK_COORDINATE,
            ZCalibrationOutcomeStage.WORK_COORDINATE_ACKNOWLEDGED: MachineMotionStep.ZCAL_WORK_COORDINATE_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.PEN_UP_ATTEMPTED: MachineMotionStep.ZCAL_PEN_UP,
            ZCalibrationOutcomeStage.PEN_UP_ACKNOWLEDGED: MachineMotionStep.ZCAL_PEN_UP_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.CENTER_ATTEMPTED: MachineMotionStep.ZCAL_CENTER,
            ZCalibrationOutcomeStage.CENTER_ACKNOWLEDGED: MachineMotionStep.ZCAL_CENTER_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.CONTACT_ATTEMPTED: MachineMotionStep.ZCAL_CONTACT,
            ZCalibrationOutcomeStage.CONTACT_ACKNOWLEDGED: MachineMotionStep.ZCAL_CONTACT_ACKNOWLEDGED,
            ZCalibrationOutcomeStage.POSTCONDITION: MachineMotionStep.ZCAL_POSTCONDITION,
        }
        return acknowledged, zcal_steps[outcome.stage]
    acknowledged = outcome.acknowledged_steps
    if type(outcome) is HomeOutcome:
        assert action is MachineAction.HOME
        home_steps = {
            SingleCommandOutcomeStage.DISPATCH_ATTEMPTED: MachineMotionStep.HOME_DISPATCH,
            SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED: MachineMotionStep.HOME_ACKNOWLEDGED,
            SingleCommandOutcomeStage.IDLE_OBSERVED: MachineMotionStep.HOME_IDLE,
            SingleCommandOutcomeStage.POSTCONDITION_PROVEN: MachineMotionStep.HOME_POSTCONDITION,
        }
        return acknowledged, home_steps[outcome.stage]
    assert type(outcome) is ZConfirmationOutcome and action is MachineAction.ZCONFIRM
    zconfirm_steps = {
        SingleCommandOutcomeStage.DISPATCH_ATTEMPTED: MachineMotionStep.ZCONFIRM_RAISE,
        SingleCommandOutcomeStage.COMMAND_ACKNOWLEDGED: MachineMotionStep.ZCONFIRM_ACKNOWLEDGED,
        SingleCommandOutcomeStage.IDLE_OBSERVED: MachineMotionStep.ZCONFIRM_IDLE,
        SingleCommandOutcomeStage.POSTCONDITION_PROVEN: MachineMotionStep.ZCONFIRM_POSTCONDITION,
    }
    return acknowledged, zconfirm_steps[outcome.stage]


_RESERVED_MOTION_PROOF_FAILURE_CODES = frozenset(
    {
        "POST_HOME_PROOF_FAILED",
        "POST_ZCAL_PROOF_FAILED",
        "POST_ZCONFIRM_PROOF_FAILED",
    }
)


def _is_exact_motion_proof_failure(
    action: MachineAction,
    outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome,
) -> bool:
    failure = outcome.failure
    assert failure is not None
    if action is MachineAction.HOME:
        return (
            type(outcome) is HomeOutcome
            and failure.code == "POST_HOME_PROOF_FAILED"
            and outcome.stage is SingleCommandOutcomeStage.IDLE_OBSERVED
        )
    if action is MachineAction.ZCAL:
        return (
            type(outcome) is ZCalibrationOutcome
            and failure.code == "POST_ZCAL_PROOF_FAILED"
            and outcome.stage is ZCalibrationOutcomeStage.POSTCONDITION
        )
    return (
        type(outcome) is ZConfirmationOutcome
        and failure.code == "POST_ZCONFIRM_PROOF_FAILED"
        and outcome.stage is SingleCommandOutcomeStage.IDLE_OBSERVED
    )


def _reserved_motion_proof_authority_is_valid(
    action: MachineAction,
    outcome: HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome,
) -> bool:
    failure = outcome.failure
    return (
        failure is None
        or failure.code not in _RESERVED_MOTION_PROOF_FAILURE_CODES
        or _is_exact_motion_proof_failure(action, outcome)
    )


def _motion_postcondition_step(action: MachineAction) -> MachineMotionStep:
    return {
        MachineAction.HOME: MachineMotionStep.HOME_POSTCONDITION,
        MachineAction.ZCAL: MachineMotionStep.ZCAL_POSTCONDITION,
        MachineAction.ZCONFIRM: MachineMotionStep.ZCONFIRM_POSTCONDITION,
    }[action]


def _execution_projection(execution: MachineExecution) -> JsonObject:
    return {
        "schema_version": execution.schema_version,
        "execution_id": execution.execution_id,
        "job_id": execution.job_id,
        "ready_revision": execution.ready_revision,
        "revision": execution.revision,
        "phase": execution.phase.value,
        "stream_milestone": execution.stream_milestone.value,
        "recovery_intent": None if execution.recovery_intent is None else execution.recovery_intent.value,
        "retired": execution.retirement is not None,
    }


def _progress_projection(progress: MachineProgress) -> JsonObject:
    return {
        "schema_version": progress.schema_version,
        "execution_id": progress.execution_id,
        "execution_revision": progress.execution_revision,
        "total": progress.total_lines,
        "acknowledged": progress.acknowledged_lines,
        "ok_count": progress.ok_count,
        "error_count": progress.error_count,
        "percent": progress.percent,
        "last_source_line": progress.last_source_line,
        "last_command": progress.last_command,
        "last_event_at": progress.last_event_at.isoformat(),
        "failure": None if progress.failure is None else progress.failure.to_json(),
    }


def _outcome_projection(outcome: MachineOutcome) -> JsonObject:
    return {
        "schema_version": outcome.schema_version,
        "execution_id": outcome.execution_id,
        "job_id": outcome.job_id,
        "execution_revision": outcome.execution_revision,
        "kind": outcome.kind.value,
        "recovery_evidence": (None if outcome.recovery_evidence is None else outcome.recovery_evidence.to_json()),
        "occurred_at": outcome.occurred_at.isoformat(),
    }


def _plain_json(value: JsonObject) -> JsonObject:
    return cast(JsonObject, json.loads(json.dumps(value, allow_nan=False, sort_keys=True)))


def _fail(code: str, message: str) -> NoReturn:
    category = ErrorCategory.PERMISSION if code.endswith("DENIED") or "AUTHORITY" in code else ErrorCategory.HARDWARE
    raise DrawingMachineError(
        ErrorPayload(
            code=code,
            category=category,
            message=message,
            retryable=False,
            details={},
        )
    )


def _error_value(code: str, message: str) -> DrawingMachineError:
    category = ErrorCategory.PERMISSION if code.endswith("DENIED") or "AUTHORITY" in code else ErrorCategory.HARDWARE
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=category,
            message=message,
            retryable=False,
            details={},
        )
    )


__all__ = ["MachineCommandResult", "MachineExecutionChain"]
