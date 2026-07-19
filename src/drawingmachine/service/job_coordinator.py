from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, cast
from uuid import uuid4

from drawingmachine.application.jobs import ReviewContinuationV1, WorkflowSubmissionV1
from drawingmachine.application.offline_job_chain import OfflineJobChain
from drawingmachine.domain.jobs import ArtifactRef, JobRecord, JobState, RequesterIdentity, allowed_transition
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload


class CoordinatorCommandKind(StrEnum):
    SUBMIT = "SUBMIT"
    CONTINUE_REVIEW = "CONTINUE_REVIEW"
    GCODE_CHECK = "GCODE_CHECK"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class CoordinatorCommand:
    kind: CoordinatorCommandKind
    request_id: str
    job_id: str
    requester: RequesterIdentity
    payload: WorkflowSubmissionV1 | ReviewContinuationV1 | Path | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CoordinatorCommandKind):
            _command_invalid("coordinator command kind is invalid")
        if not isinstance(self.request_id, str) or not self.request_id:
            _command_invalid("coordinator command request_id must not be empty")
        if not isinstance(self.job_id, str) or not self.job_id:
            _command_invalid("coordinator command job_id must not be empty")
        if not isinstance(self.requester, RequesterIdentity):
            _command_invalid("coordinator command requester is invalid")
        valid_payload = (
            (self.kind is CoordinatorCommandKind.SUBMIT and isinstance(self.payload, WorkflowSubmissionV1))
            or (
                self.kind is CoordinatorCommandKind.CONTINUE_REVIEW
                and isinstance(self.payload, ReviewContinuationV1)
                and self.payload.job_id == self.job_id
            )
            or (
                self.kind is CoordinatorCommandKind.GCODE_CHECK
                and isinstance(self.payload, Path)
                and self.payload.is_absolute()
            )
            or (self.kind is CoordinatorCommandKind.STOP and self.payload is None)
        )
        if not valid_payload:
            _command_invalid("coordinator command payload does not match its kind")


@dataclass(frozen=True, slots=True)
class JobProjection:
    job: JobRecord
    artifacts: tuple[ArtifactRef, ...]
    cancellation_requested: bool


class _EnvelopeState(StrEnum):
    QUEUED = "QUEUED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ABANDONED = "ABANDONED"


@dataclass(slots=True)
class _Envelope:
    command: CoordinatorCommand
    acknowledged: threading.Event
    deadline: float | None = None
    result: JobProjection | None = None
    error: BaseException | None = None
    _state: _EnvelopeState = dataclass_field(default=_EnvelopeState.QUEUED, init=False)
    _state_lock: threading.Lock = dataclass_field(default_factory=threading.Lock, init=False, repr=False)

    def commit_allowed(self) -> bool:
        with self._state_lock:
            if self._state is not _EnvelopeState.QUEUED:
                return False
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self._state = _EnvelopeState.ABANDONED
                return False
            return True

    def is_abandoned(self) -> bool:
        with self._state_lock:
            return self._state is _EnvelopeState.ABANDONED

    def abandon_unacknowledged(self) -> bool:
        with self._state_lock:
            if self._state is _EnvelopeState.ACKNOWLEDGED:
                return False
            self._state = _EnvelopeState.ABANDONED
            return True

    def acknowledge(self) -> bool:
        with self._state_lock:
            if self._state is _EnvelopeState.ABANDONED:
                return False
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self._state = _EnvelopeState.ABANDONED
                return False
            self._state = _EnvelopeState.ACKNOWLEDGED
            self.acknowledged.set()
            return True

    def is_acknowledged(self) -> bool:
        with self._state_lock:
            return self._state is _EnvelopeState.ACKNOWLEDGED


@dataclass(frozen=True, slots=True)
class _CancelWake:
    job_id: str
    request_id: str
    requester: RequesterIdentity


@dataclass(frozen=True, slots=True)
class _ExternalPublication:
    job_id: str


_MailboxItem = _Envelope | _CancelWake | _ExternalPublication


def _command_invalid(message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("COORDINATOR_COMMAND_INVALID", ErrorCategory.INPUT, message, False, {}))


class JobCoordinator:
    def __init__(
        self,
        *,
        chain_factory: Callable[[], OfflineJobChain],
        acknowledgement_timeout_seconds: float = 1.0,
    ) -> None:
        if acknowledgement_timeout_seconds <= 0 or acknowledgement_timeout_seconds > 1.0:
            raise ValueError("acknowledgement_timeout_seconds must be in (0, 1]")
        self._chain_factory = chain_factory
        self._acknowledgement_timeout_seconds = acknowledgement_timeout_seconds
        self._mailbox: queue.Queue[_MailboxItem] = queue.Queue()
        self._lock = threading.Lock()
        self._projections: dict[str, JobProjection] = {}
        self._tokens: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._chain: OfflineJobChain | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                self._raise("COORDINATOR_ALREADY_STARTED", "job coordinator is already started")
            self._stopping = False
            self._ready.clear()
            self._startup_error = None
            self._tokens.clear()
            self._projections.clear()
            thread = threading.Thread(
                target=self._run,
                name="drawingmachine-job-coordinator",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        self._ready.wait()
        error = cast(BaseException | None, self._startup_error)
        if error is not None:
            with self._lock:
                self._thread = None
            raise error

    def submit(self, command: CoordinatorCommand) -> JobProjection:
        if command.kind not in {
            CoordinatorCommandKind.SUBMIT,
            CoordinatorCommandKind.CONTINUE_REVIEW,
            CoordinatorCommandKind.GCODE_CHECK,
        }:
            self._raise("COORDINATOR_COMMAND_INVALID", "command cannot be submitted to the coordinator")
        deadline = time.monotonic() + self._acknowledgement_timeout_seconds
        envelope = _Envelope(command, threading.Event(), deadline)
        with self._lock:
            self._require_admission_locked()
            self._tokens.setdefault(command.job_id, threading.Event())
            self._mailbox.put(envelope)
        remaining = max(0.0, deadline - time.monotonic())
        if not envelope.acknowledged.wait(remaining) and envelope.abandon_unacknowledged():
            self._raise(
                "COORDINATOR_ACK_TIMEOUT",
                "job submission was not durably acknowledged within one second",
                job_id=command.job_id,
            )
        if envelope.error is not None:
            raise envelope.error
        assert envelope.result is not None
        return envelope.result

    def status(self, job_id: str) -> JobProjection:
        self._require_started()
        with self._lock:
            projection = self._projections.get(job_id)
        if projection is None:
            self._raise("JOB_NOT_FOUND", "job projection does not exist", job_id=job_id)
        return projection

    def request_cancel(
        self,
        job_id: str,
        request_id: str,
        requester: RequesterIdentity,
    ) -> JobProjection:
        if not isinstance(request_id, str) or not request_id:
            self._raise("CANCELLATION_REQUEST_INVALID", "cancellation request_id must not be empty", job_id=job_id)
        if not isinstance(requester, RequesterIdentity):
            self._raise("CANCELLATION_REQUEST_INVALID", "cancellation requester is invalid", job_id=job_id)
        with self._lock:
            self._require_admission_locked()
            projection = self._projections.get(job_id)
            if projection is None:
                self._raise("JOB_NOT_FOUND", "job projection does not exist", job_id=job_id)
            blocker_code = None if projection.job.blocker is None else projection.job.blocker.code
            if not allowed_transition(
                projection.job.state,
                JobState.CANCELLED,
                blocker_code,
                job_kind=projection.job.kind,
            ):
                self._raise("CANCELLATION_NOT_ALLOWED", "job state is not cancellable", job_id=job_id)
            token = self._tokens.setdefault(job_id, threading.Event())
            chain = self._chain
            register_request = None if chain is None else getattr(chain, "register_cancellation_request", None)
            if callable(register_request):
                register_request(job_id, token, request_id, requester)
            else:
                token.set()
            updated = JobProjection(projection.job, projection.artifacts, True)
            self._projections[job_id] = updated
            self._mailbox.put(_CancelWake(job_id, request_id, requester))
        return updated

    def publish_external(self, job_id: str) -> None:
        """Queue a durable externally-committed job refresh on the owning thread."""
        if type(job_id) is not str or not job_id:
            self._raise("EXTERNAL_PUBLICATION_INVALID", "external publication job_id must not be empty")
        with self._lock:
            self._require_admission_locked()
            self._mailbox.put(_ExternalPublication(job_id))

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stopping = True
            for token in self._tokens.values():
                token.set()
            command = CoordinatorCommand(
                CoordinatorCommandKind.STOP,
                "coordinator-stop",
                "coordinator",
                RequesterIdentity("SERVICE", None, None, None),
                None,
            )
            self._mailbox.put(_Envelope(command, threading.Event()))
        thread.join(timeout=10.0)
        if thread.is_alive():
            self._raise("COORDINATOR_STOP_TIMEOUT", "job coordinator did not stop")
        with self._lock:
            self._thread = None
            self._stopping = False

    def _run(self) -> None:
        chain: OfflineJobChain | None = None
        try:
            chain = self._chain_factory()
            with self._lock:
                self._chain = chain
            initialize = getattr(chain, "initialize", None)
            if callable(initialize):
                initialize()
            reconciled = chain.reconcile_inflight_jobs()
            reconciled_ids = {job.job_id for job in reconciled}
            for job in reconciled:
                self._register_and_publish(chain, job)
            existing_jobs = getattr(chain, "jobs", None)
            if callable(existing_jobs):
                for job in existing_jobs():
                    if job.job_id not in reconciled_ids:
                        self._register_and_publish(chain, job)
        except BaseException as error:
            self._startup_error = error
            self._ready.set()
            if chain is not None:
                chain.close()
            with self._lock:
                self._chain = None
            return
        self._ready.set()
        try:
            while True:
                item = self._mailbox.get()
                if isinstance(item, _ExternalPublication):
                    self._publish_external(chain, item.job_id)
                    continue
                if isinstance(item, _CancelWake):
                    try:
                        self._process_cancel(chain, item)
                    except BaseException:
                        try:
                            self._recover_cancel_failure(chain, item.job_id)
                        except BaseException:
                            with self._lock:
                                token = self._tokens.get(item.job_id)
                                if token is not None and not self._stopping:
                                    token.clear()
                                previous = self._projections.get(item.job_id)
                                if previous is not None:
                                    self._projections[item.job_id] = JobProjection(
                                        previous.job,
                                        previous.artifacts,
                                        False,
                                    )
                    continue
                if item.command.kind is CoordinatorCommandKind.STOP:
                    item.acknowledged.set()
                    break
                if item.is_abandoned():
                    continue
                self._process_command(chain, item)
        finally:
            chain.close()
            with self._lock:
                self._chain = None

    def _process_command(self, chain: OfflineJobChain, envelope: _Envelope) -> None:
        command = envelope.command
        try:
            before = self._authoritative_or_none(chain, command.job_id)
        except BaseException:
            envelope.error = self._orchestration_error(command.request_id, command.job_id)
            envelope.acknowledge()
            return
        try:
            token = self._token_for(command.job_id)
            chain.register_cancellation(command.job_id, token)

            def acknowledge_durable(job: JobRecord) -> None:
                self._acknowledge_durable(envelope, job)

            if command.kind is CoordinatorCommandKind.SUBMIT:
                submission = cast(WorkflowSubmissionV1, command.payload)
                job = chain._coordinator_submit(
                    submission,
                    request_id=command.request_id,
                    job_id=command.job_id,
                    requester=command.requester,
                    commit_allowed=envelope.commit_allowed,
                    committed=acknowledge_durable,
                )
            elif command.kind is CoordinatorCommandKind.CONTINUE_REVIEW:
                continuation = cast(ReviewContinuationV1, command.payload)
                job = chain._coordinator_continue_review(
                    continuation,
                    request_id=command.request_id,
                    requester=command.requester,
                    commit_allowed=envelope.commit_allowed,
                    committed=acknowledge_durable,
                )
            else:
                path = cast(Path, command.payload)
                submit_gcode_check = getattr(chain, "_coordinator_submit_gcode_check", None)
                if not callable(submit_gcode_check):
                    self._raise("GCODE_CHECK_UNAVAILABLE", "G-code check orchestration is unavailable")
                job = submit_gcode_check(
                    path,
                    request_id=command.request_id,
                    job_id=command.job_id,
                    requester=command.requester,
                    commit_allowed=envelope.commit_allowed,
                    committed=acknowledge_durable,
                )
            if job is None:
                return
        except BaseException as error:
            if envelope.is_acknowledged():
                self._complete_post_ack_failure(chain, command.job_id, error)
                return
            self._complete_pre_ack_failure(chain, envelope, error, before)
            return
        if envelope.is_abandoned():
            self._complete_post_ack_failure(
                chain,
                job.job_id,
                self._ack_deadline_error(command.request_id, job.job_id),
            )
            return
        try:
            write_projection = getattr(chain, "_write_durable_projection", None)
            if callable(write_projection):
                write_projection(job)
            self._publish(chain, job)
            self._drive(chain, job)
        except BaseException as error:
            self._complete_post_ack_failure(chain, job.job_id, error)

    def _acknowledge_durable(self, envelope: _Envelope, job: JobRecord) -> None:
        with self._lock:
            previous = self._projections.get(job.job_id)
            artifacts = () if previous is None else previous.artifacts
            requested = False if job.state is JobState.CANCELLED else bool(previous and previous.cancellation_requested)
            projection = JobProjection(job, artifacts, requested)
            envelope.result = projection
            if envelope.acknowledge():
                self._projections[job.job_id] = projection

    @staticmethod
    def _ack_deadline_error(request_id: str, job_id: str) -> DrawingMachineError:
        return DrawingMachineError(
            ErrorPayload(
                "COORDINATOR_ACK_DEADLINE_EXCEEDED",
                ErrorCategory.SERVICE,
                "job commit completed after its acknowledgement deadline",
                False,
                {},
                request_id=request_id,
                job_id=job_id,
            )
        )

    def _complete_pre_ack_failure(
        self,
        chain: OfflineJobChain,
        envelope: _Envelope,
        error: BaseException,
        before: JobRecord | None,
    ) -> None:
        try:
            after = self._authoritative_or_none(chain, envelope.command.job_id)
        except BaseException:
            envelope.error = self._orchestration_error(envelope.command.request_id, envelope.command.job_id)
            envelope.acknowledge()
            return
        if self._same_authoritative_revision(before, after):
            envelope.error = error
            envelope.acknowledge()
            return
        try:
            failed = chain.fail_unexpected(
                envelope.command.job_id,
                request_id=f"orchestration-failure-{uuid4()}",
                error=error,
            )
            envelope.result = self._publish(chain, failed)
        except BaseException:
            try:
                authoritative = self._authoritative_or_none(chain, envelope.command.job_id)
            except BaseException:
                authoritative = None
            if authoritative is not None:
                envelope.result = self._rebuild_projection(chain, authoritative, cancellation_requested=False)
            if envelope.result is None:
                envelope.error = self._orchestration_error(
                    envelope.command.request_id,
                    envelope.command.job_id,
                )
        finally:
            envelope.acknowledge()

    def _complete_post_ack_failure(
        self,
        chain: OfflineJobChain,
        job_id: str,
        error: BaseException,
    ) -> None:
        try:
            failed = chain.fail_unexpected(
                job_id,
                request_id=f"orchestration-failure-{uuid4()}",
                error=error,
            )
            self._publish(chain, failed)
        except BaseException:
            try:
                authoritative = chain.get(job_id)
                artifacts = chain.artifact_refs(job_id)
            except BaseException:
                return
            with self._lock:
                previous = self._projections.get(job_id)
                requested = False if previous is None else previous.cancellation_requested
                self._projections[job_id] = JobProjection(authoritative, artifacts, requested)

    def _process_cancel(self, chain: OfflineJobChain, wake: _CancelWake) -> None:
        projection = self._projection_or_none(wake.job_id)
        if projection is None:
            return
        if projection.job.state is JobState.CANCELLED:
            if projection.cancellation_requested:
                with self._lock:
                    self._projections[wake.job_id] = JobProjection(
                        projection.job,
                        projection.artifacts,
                        False,
                    )
            return
        job = chain.cancel(
            wake.job_id,
            request_id=wake.request_id,
            requester=wake.requester,
        )
        self._publish(chain, job, cancellation_requested=False)

    def _recover_cancel_failure(self, chain: OfflineJobChain, job_id: str) -> None:
        authoritative = self._authoritative_or_none(chain, job_id)
        if authoritative is None:
            with self._lock:
                self._projections.pop(job_id, None)
            return
        if authoritative.state is not JobState.CANCELLED:
            with self._lock:
                token = self._tokens.get(job_id)
                if token is not None and not self._stopping:
                    token.clear()
        self._rebuild_projection(chain, authoritative, cancellation_requested=False)

    def _rebuild_projection(
        self,
        chain: OfflineJobChain,
        job: JobRecord,
        *,
        cancellation_requested: bool,
    ) -> JobProjection:
        try:
            artifacts = chain.artifact_refs(job.job_id)
        except BaseException:
            with self._lock:
                previous = self._projections.get(job.job_id)
                artifacts = () if previous is None else previous.artifacts
        projection = JobProjection(job, artifacts, cancellation_requested)
        with self._lock:
            self._projections[job.job_id] = projection
        return projection

    @staticmethod
    def _authoritative_or_none(chain: OfflineJobChain, job_id: str) -> JobRecord | None:
        try:
            return chain.get(job_id)
        except KeyError:
            return None
        except DrawingMachineError as error:
            if error.payload.code == "JOB_NOT_FOUND":
                return None
            raise

    @staticmethod
    def _same_authoritative_revision(before: JobRecord | None, after: JobRecord | None) -> bool:
        if before is None or after is None:
            return before is after
        return before.job_id == after.job_id and before.revision == after.revision

    @staticmethod
    def _orchestration_error(request_id: str, job_id: str) -> DrawingMachineError:
        return DrawingMachineError(
            ErrorPayload(
                "ORCHESTRATION_FAILED",
                ErrorCategory.INTERNAL,
                "offline job orchestration failed",
                False,
                {},
                request_id=request_id,
                job_id=job_id,
            )
        )

    def _drive(self, chain: OfflineJobChain, job: JobRecord) -> None:
        advance = getattr(chain, "advance", None)
        if not callable(advance):
            self._publish(chain, chain.run_to_intervention_or_terminal(job.job_id))
            return
        current = job
        terminal = {JobState.READY_TO_RUN, JobState.BLOCKED, JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED}
        while current.state not in terminal:
            current = advance(current.job_id)
            self._publish(chain, current)

    def _register_and_publish(self, chain: OfflineJobChain, job: JobRecord) -> JobProjection:
        token = self._token_for(job.job_id)
        chain.register_cancellation(job.job_id, token)
        return self._publish(chain, job)

    def _publish(
        self,
        chain: OfflineJobChain,
        job: JobRecord,
        *,
        cancellation_requested: bool | None = None,
    ) -> JobProjection:
        artifacts = chain.artifact_refs(job.job_id)
        with self._lock:
            previous = self._projections.get(job.job_id)
            if job.state is JobState.CANCELLED:
                requested = False
            else:
                requested = (
                    previous.cancellation_requested
                    if cancellation_requested is None and previous is not None
                    else bool(cancellation_requested)
                )
            projection = JobProjection(job, artifacts, requested)
            self._projections[job.job_id] = projection
        return projection

    def _publish_external(self, chain: OfflineJobChain, job_id: str) -> None:
        try:
            job = chain.get(job_id)
            artifacts = chain.artifact_refs(job_id)
        except BaseException:
            return
        with self._lock:
            previous = self._projections.get(job_id)
            if previous is not None and job.revision <= previous.job.revision:
                return
            self._projections[job_id] = JobProjection(job, artifacts, False)

    def _token_for(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._tokens.setdefault(job_id, threading.Event())

    def _projection_or_none(self, job_id: str) -> JobProjection | None:
        with self._lock:
            return self._projections.get(job_id)

    def _require_started(self) -> None:
        with self._lock:
            started = self._thread is not None and self._thread.is_alive() and not self._stopping
        if not started:
            self._raise("COORDINATOR_NOT_STARTED", "job coordinator is not started")

    def _require_admission_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive() or self._stopping:
            self._raise("COORDINATOR_NOT_STARTED", "job coordinator is not accepting commands")

    @staticmethod
    def _raise(code: str, message: str, *, job_id: str | None = None) -> NoReturn:
        raise DrawingMachineError(ErrorPayload(code, ErrorCategory.SERVICE, message, False, {}, job_id=job_id))


__all__ = [
    "CoordinatorCommand",
    "CoordinatorCommandKind",
    "JobCoordinator",
    "JobProjection",
]
