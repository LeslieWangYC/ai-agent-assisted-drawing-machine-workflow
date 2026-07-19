from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

import drawingmachine.service.job_coordinator as coordinator_module
from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.adapters.workers.worker_entrypoint import execute_task
from drawingmachine.application.jobs import ReviewContinuationV1, RouteMode, WorkflowSubmissionV1
from drawingmachine.application.offline_job_chain import OfflineJobChain
from drawingmachine.config import XdgPaths, load_config
from drawingmachine.domain.jobs import ConfigSnapshot, JobBlocker, JobKind, JobRecord, JobState, RequesterIdentity
from drawingmachine.domain.workflow import ProcessedImageReview
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.artifacts import PromotedBundle
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerStatus, WorkerTaskV1
from drawingmachine.service.job_coordinator import (
    CoordinatorCommand,
    CoordinatorCommandKind,
    JobCoordinator,
    JobProjection,
)


class RestartStore:
    def import_file(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("restart must not import")

    def create_staging(self, job_id: str, attempt_id: str) -> NoReturn:
        del job_id, attempt_id
        raise AssertionError("restart must not stage")

    def validate_and_promote(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("restart must not promote")

    def discard_staging(self, job_id: str, attempt_id: str) -> None:
        del job_id, attempt_id

    def resolve(self, job_id: str, artifact: object) -> NoReturn:
        del job_id, artifact
        raise AssertionError("restart fixture has no continuable persisted artifacts")

    def write_projection(self, job: JobRecord, artifacts: tuple[object, ...]) -> None:
        del job, artifacts


class RestartWorkers:
    def __init__(self) -> None:
        self.submit_count = 0

    def submit(self, task: object) -> NoReturn:
        del task
        self.submit_count += 1
        raise AssertionError("restart must not submit worker")

    def wait(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("restart must not wait")

    def cancel(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("restart must not cancel worker")

    def close(self) -> None:
        return


class CountingFilesystemArtifactStore(FilesystemArtifactStore):
    def __init__(self, paths: XdgPaths) -> None:
        super().__init__(paths)
        self.import_count = 0

    def import_file(
        self,
        job_id: str,
        attempt_id: str,
        source: Path,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> PromotedBundle:
        self.import_count += 1
        return super().import_file(
            job_id,
            attempt_id,
            source,
            role=role,
            relative_path=relative_path,
            media_type=media_type,
            max_bytes=max_bytes,
        )


class DelayedFilesystemArtifactStore(CountingFilesystemArtifactStore):
    def __init__(self, paths: XdgPaths, started: threading.Event, release: threading.Event) -> None:
        super().__init__(paths)
        self.started = started
        self.release = release

    def import_file(
        self,
        job_id: str,
        attempt_id: str,
        source: Path,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> PromotedBundle:
        self.started.set()
        self.release.wait(timeout=2.0)
        return super().import_file(
            job_id,
            attempt_id,
            source,
            role=role,
            relative_path=relative_path,
            media_type=media_type,
            max_bytes=max_bytes,
        )


class DelayedMissingSourceStore(RestartStore):
    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def import_file(self, job_id: str, attempt_id: str, source: Path, **kwargs: object) -> NoReturn:
        del job_id, attempt_id, kwargs
        self.release.wait(timeout=2.0)
        source.read_bytes()
        raise AssertionError("deleted source unexpectedly remained readable")


class InlineWorkers:
    def submit(self, task: WorkerTaskV1) -> WorkerTaskV1:
        return task

    def wait(
        self,
        handle: WorkerTaskV1,
        *,
        timeout_seconds: float,
        cancel_requested: object,
    ) -> WorkerOutcomeV1:
        del timeout_seconds, cancel_requested
        return WorkerOutcomeV1.from_json(execute_task(handle.to_json()))

    def cancel(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("inline worker is never cancelled")

    def close(self) -> None:
        return


class CapturePlanningWorkers(InlineWorkers):
    def __init__(self) -> None:
        self.submitted: list[WorkerTaskV1] = []
        self.called = threading.Event()

    def submit(self, task: WorkerTaskV1) -> NoReturn:
        self.submitted.append(task)
        self.called.set()
        raise RuntimeError("stop after observing rehydrated planning input")


class RunningCancelWorkers(InlineWorkers):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.tasks: list[WorkerTaskV1] = []
        self.closed = False

    def submit(self, task: WorkerTaskV1) -> WorkerTaskV1:
        self.tasks.append(task)
        self.started.set()
        return task

    def wait(
        self,
        handle: WorkerTaskV1,
        *,
        timeout_seconds: float,
        cancel_requested: Callable[[], bool],
    ) -> WorkerOutcomeV1:
        deadline = time.monotonic() + timeout_seconds
        while not cancel_requested() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert cancel_requested()
        return WorkerOutcomeV1(
            1,
            handle.task_id,
            handle.job_id,
            handle.job_revision,
            handle.attempt_id,
            WorkerStatus.CANCELLED,
            (),
            {},
            ErrorPayload(
                "WORKER_CANCELLED",
                ErrorCategory.SERVICE,
                "worker cancelled",
                False,
                {},
                job_id=handle.job_id,
            ),
        )

    def close(self) -> None:
        self.closed = True


def requester() -> RequesterIdentity:
    return RequesterIdentity("LOCAL_PEER", 1, 2, 3)


def queued(job_id: str = "job-1") -> JobRecord:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    return JobRecord(
        1,
        job_id,
        JobKind.OFFLINE_WORKFLOW,
        "job",
        JobState.QUEUED,
        0,
        {"route_mode": "direct"},
        ConfigSnapshot("machine", "provider", 1, 1, 1, "1" * 64, "2" * 64, "3" * 64),
        None,
        None,
        None,
        now,
        now,
    )


class FakeChain:
    def __init__(
        self,
        *,
        inflight: bool = False,
        hold: threading.Event | None = None,
        drive_error: BaseException | None = None,
        projection_failure_call: int | None = None,
        cancel_error: BaseException | None = None,
        review_error: BaseException | None = None,
    ) -> None:
        self.records: dict[str, JobRecord] = {}
        self.tokens: dict[str, threading.Event] = {}
        self.hold = hold
        self.created_thread = threading.get_ident()
        self.closed_thread: int | None = None
        self.worker_submits = 0
        self.trace: list[str] = []
        self.drive_error = drive_error
        self.projection_failure_call = projection_failure_call
        self.artifact_reads = 0
        self.cancel_error = cancel_error
        self.review_error = review_error
        if inflight:
            self.records["job-1"] = replace(queued(), state=JobState.PLANNING_PATHS, revision=3)

    def reconcile_inflight_jobs(self) -> tuple[JobRecord, ...]:
        reconciled = []
        for job_id, job in tuple(self.records.items()):
            if job.state in {
                JobState.PREPARING_IMAGE,
                JobState.PLANNING_PATHS,
                JobState.BUILDING_GCODE,
                JobState.VALIDATING_GCODE,
                JobState.IMAGE_READY,
                JobState.PATH_PLAN_READY,
            }:
                from drawingmachine.errors import ErrorCategory, ErrorPayload

                failed = replace(
                    job,
                    state=JobState.FAILED,
                    revision=job.revision + 1,
                    error=ErrorPayload(
                        "SERVICE_RESTARTED",
                        ErrorCategory.SERVICE,
                        "service restarted",
                        False,
                        {},
                        request_id="service-restart",
                        job_id=job_id,
                    ),
                )
                self.records[job_id] = failed
                reconciled.append(failed)
        return tuple(reconciled)

    def register_cancellation(self, job_id: str, token: threading.Event) -> None:
        self.tokens[job_id] = token

    def submit(
        self, submission: WorkflowSubmissionV1, *, request_id: str, job_id: str, requester: RequesterIdentity
    ) -> JobRecord:
        del submission, request_id, requester
        if job_id in self.records:
            raise DrawingMachineError(
                ErrorPayload("JOB_ALREADY_EXISTS", ErrorCategory.INPUT, "job already exists", False, {}, job_id=job_id)
            )
        self.records[job_id] = queued(job_id)
        self.trace.append("commit")
        return self.records[job_id]

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
        if not commit_allowed():
            return None
        job = self.submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
        )
        committed(job)
        return job

    def continue_review(self, continuation: object, *, request_id: str, requester: RequesterIdentity) -> JobRecord:
        del continuation, request_id, requester
        if self.review_error is not None:
            raise self.review_error
        current = self.records["job-1"]
        if current.state is not JobState.BLOCKED:
            raise DrawingMachineError(
                ErrorPayload("REVIEW_NOT_ALLOWED", ErrorCategory.INPUT, "review not allowed", False, {}, job_id="job-1")
            )
        self.records["job-1"] = replace(
            current, state=JobState.IMAGE_READY, revision=current.revision + 1, blocker=None
        )
        return self.records["job-1"]

    def _coordinator_continue_review(
        self,
        continuation: ReviewContinuationV1,
        *,
        request_id: str,
        requester: RequesterIdentity,
        commit_allowed: Callable[[], bool],
        committed: Callable[[JobRecord], None],
    ) -> JobRecord | None:
        if not commit_allowed():
            return None
        job = self.continue_review(
            continuation,
            request_id=request_id,
            requester=requester,
        )
        committed(job)
        return job

    def run_to_intervention_or_terminal(self, job_id: str) -> JobRecord:
        if self.tokens[job_id].is_set():
            self.records[job_id] = replace(self.records[job_id], state=JobState.CANCELLED, revision=1)
            self.trace.append("cancel-commit")
            return self.records[job_id]
        self.worker_submits += 1
        if self.hold is not None:
            while not self.hold.wait(0.01):
                token = self.tokens[job_id]
                if token.is_set():
                    self.records[job_id] = replace(self.records[job_id], state=JobState.CANCELLED, revision=1)
                    self.trace.append("cancel-commit")
                    return self.records[job_id]
        if self.drive_error is not None:
            raise self.drive_error
        self.records[job_id] = replace(
            self.records[job_id],
            state=JobState.BLOCKED,
            revision=1,
            blocker=JobBlocker("REVIEW_REQUIRED", ErrorCategory.VALIDATION, "review", {}),
        )
        self.trace.append("run-commit")
        return self.records[job_id]

    def cancel(self, job_id: str, *, request_id: str, requester: RequesterIdentity) -> JobRecord:
        del request_id, requester
        if self.cancel_error is not None:
            raise self.cancel_error
        self.records[job_id] = replace(
            self.records[job_id], state=JobState.CANCELLED, revision=self.records[job_id].revision + 1
        )
        self.trace.append("cancel-commit")
        return self.records[job_id]

    def get(self, job_id: str) -> JobRecord:
        return self.records[job_id]

    def artifact_refs(self, job_id: str) -> tuple[object, ...]:
        del job_id
        self.artifact_reads += 1
        if self.artifact_reads == self.projection_failure_call:
            raise OSError("projection exploded")
        return ()

    def jobs(self) -> tuple[JobRecord, ...]:
        return tuple(self.records.values())

    def fail_unexpected(self, job_id: str, *, request_id: str, error: BaseException) -> JobRecord:
        from drawingmachine.errors import ErrorCategory, ErrorPayload

        del error
        current = self.records[job_id]
        failed = replace(
            current,
            state=JobState.FAILED,
            revision=current.revision + 1,
            blocker=None,
            error=ErrorPayload(
                "ORCHESTRATION_FAILED",
                ErrorCategory.INTERNAL,
                "offline job orchestration failed",
                False,
                {},
                request_id=request_id,
                job_id=job_id,
            ),
        )
        self.records[job_id] = failed
        self.trace.append("failure-commit")
        return failed

    def close(self) -> None:
        self.closed_thread = threading.get_ident()


class CommitReturnHeldChain(FakeChain):
    def __init__(self, committed: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.committed = committed
        self.release = release

    def submit(
        self, submission: WorkflowSubmissionV1, *, request_id: str, job_id: str, requester: RequesterIdentity
    ) -> JobRecord:
        job = super().submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
        )
        self.committed.set()
        assert self.release.wait(timeout=2.0)
        return job


class CommitBoundaryHeldChain(FakeChain):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def submit(
        self, submission: WorkflowSubmissionV1, *, request_id: str, job_id: str, requester: RequesterIdentity
    ) -> JobRecord:
        self.started.set()
        assert self.release.wait(timeout=2.0)
        return super().submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
        )

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
        if not commit_allowed():
            return None
        self.started.set()
        assert self.release.wait(timeout=2.0)
        if not commit_allowed():
            return None
        job = super().submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
        )
        committed(job)
        return job


class ProjectionHeldChain(FakeChain):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def artifact_refs(self, job_id: str) -> tuple[object, ...]:
        self.started.set()
        assert self.release.wait(timeout=2.0)
        return super().artifact_refs(job_id)


class CommittedCallbackHeldChain(CommitReturnHeldChain):
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
        if not commit_allowed():
            return None
        job = FakeChain.submit(
            self,
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
        )
        committed(job)
        self.committed.set()
        assert self.release.wait(timeout=2.0)
        return job


def submit_command(source: Path) -> CoordinatorCommand:
    return CoordinatorCommand(
        CoordinatorCommandKind.SUBMIT,
        "submit-1",
        "job-1",
        requester(),
        WorkflowSubmissionV1(1, "job", source, RouteMode.DIRECT, None, None),
    )


def approved_review(job_id: str = "job-1") -> ReviewContinuationV1:
    review = ProcessedImageReview.from_json(
        {
            "schema": "processed_image_review_v1",
            "job_name": "job",
            "reviewed_at": "2026-07-11T00:00:00+00:00",
            "reviewer": "test",
            "processed_image": "processed_image.png",
            "handoff": None,
            "status": "PASS_TO_BUILD",
            "checks": {
                "recognizable_subject": True,
                "background_simplified": True,
                "black_on_white": True,
                "no_edge_clipping": True,
                "line_density_drawable": True,
                "no_soft_gradients": True,
                "limited_tiny_isolated_marks": True,
                "vectorization_complexity_ok": True,
            },
            "issues": [],
            "revised_prompt": None,
            "next_allowed_stage": "build_drawable_job",
        }
    )
    return ReviewContinuationV1(1, job_id, review, "0" * 64)


@pytest.mark.parametrize(
    "build",
    [
        lambda: CoordinatorCommand("SUBMIT", "request", "job", requester(), None),  # type: ignore[arg-type]
        lambda: CoordinatorCommand(CoordinatorCommandKind.SUBMIT, "", "job", requester(), None),
        lambda: CoordinatorCommand(CoordinatorCommandKind.SUBMIT, "request", "", requester(), None),
        lambda: CoordinatorCommand(CoordinatorCommandKind.SUBMIT, "request", "job", object(), None),  # type: ignore[arg-type]
        lambda: CoordinatorCommand(CoordinatorCommandKind.STOP, "request", "job", requester(), object()),
    ],
)
def test_coordinator_command_defensive_contract_branches(build: Callable[[], object]) -> None:
    with pytest.raises(DrawingMachineError):
        build()


def test_coordinator_lifecycle_and_request_defensive_branches(tmp_path: Path) -> None:
    coordinator = JobCoordinator(chain_factory=FakeChain)
    coordinator.stop()
    with pytest.raises(DrawingMachineError, match="not started"):
        coordinator.status("missing")
    with pytest.raises(DrawingMachineError, match="cannot be submitted"):
        coordinator.submit(CoordinatorCommand(CoordinatorCommandKind.STOP, "request", "job", requester(), None))
    coordinator.start()
    try:
        with pytest.raises(DrawingMachineError, match="already started"):
            coordinator.start()
        with pytest.raises(DrawingMachineError, match="does not exist"):
            coordinator.status("missing")
        with pytest.raises(DrawingMachineError, match="request_id"):
            coordinator.request_cancel("missing", "", requester())
        with pytest.raises(DrawingMachineError, match="requester"):
            coordinator.request_cancel("missing", "request", object())  # type: ignore[arg-type]
        with pytest.raises(DrawingMachineError, match="does not exist"):
            coordinator.request_cancel("missing", "request", requester())
        source = tmp_path / "input.png"
        source.write_bytes(b"x")
        projection = coordinator.submit(submit_command(source))
        assert projection.job.job_id == "job-1"
    finally:
        coordinator.stop()


def test_coordinator_rejects_cancellation_for_failed_job() -> None:
    chain = FakeChain()
    failed = replace(
        queued(),
        state=JobState.FAILED,
        error=ErrorPayload("FAILED", ErrorCategory.INTERNAL, "failed", False, {}, job_id="job-1"),
    )
    chain.records["job-1"] = failed
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    try:
        with pytest.raises(DrawingMachineError, match="not cancellable"):
            coordinator.request_cancel("job-1", "request", requester())
    finally:
        coordinator.stop()


def review_command(job_id: str = "job-1") -> CoordinatorCommand:
    return CoordinatorCommand(
        CoordinatorCommandKind.CONTINUE_REVIEW,
        "review-1",
        job_id,
        requester(),
        approved_review(job_id),
    )


def gcode_command(source: Path, *, job_id: str = "gcode-job", request_id: str = "gcode-submit") -> CoordinatorCommand:
    return CoordinatorCommand(
        CoordinatorCommandKind.GCODE_CHECK,
        request_id,
        job_id,
        requester(),
        source,
    )


def test_submission_ack_is_durable_queued_and_bounded_before_slow_work(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    hold = threading.Event()
    chain = FakeChain(hold=hold)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    started = time.monotonic()
    projection = coordinator.submit(submit_command(source))
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert projection.job.state is JobState.QUEUED
    assert chain.trace[0] == "commit"
    hold.set()
    coordinator.stop()


def test_ack_timeout_withdraws_prequeued_command_before_persistence_or_execution(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    hold = threading.Event()
    chain = FakeChain(hold=hold)
    coordinator = JobCoordinator(
        chain_factory=lambda: chain,
        acknowledgement_timeout_seconds=0.05,
    )
    coordinator.start()
    coordinator.submit(submit_command(source))
    second = replace(submit_command(source), request_id="submit-2", job_id="job-2")

    with pytest.raises(DrawingMachineError) as caught:
        coordinator.submit(second)
    assert caught.value.payload.code == "COORDINATOR_ACK_TIMEOUT"
    with pytest.raises(DrawingMachineError) as cancel_error:
        coordinator.request_cancel("job-2", "cancel-timed-out", requester())
    assert cancel_error.value.payload.code == "JOB_NOT_FOUND"

    stop_thread = threading.Thread(target=coordinator.stop)
    stop_thread.start()
    hold.set()
    stop_thread.join(timeout=2.0)
    assert not stop_thread.is_alive()
    assert "job-2" not in chain.records
    assert chain.worker_submits == 1


def test_durable_commit_winning_ack_deadline_race_returns_projection(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    committed = threading.Event()
    release = threading.Event()
    chain = CommittedCallbackHeldChain(committed, release)
    coordinator = JobCoordinator(
        chain_factory=lambda: chain,
        acknowledgement_timeout_seconds=0.05,
    )
    coordinator.start()
    result: list[JobProjection | BaseException] = []

    def submit() -> None:
        try:
            result.append(coordinator.submit(submit_command(source)))
        except BaseException as error:
            result.append(error)

    submit_thread = threading.Thread(target=submit)
    submit_thread.start()
    committed_before_deadline = False
    returned_before_release = False
    try:
        committed_before_deadline = committed.wait(timeout=1.0)
        submit_thread.join(timeout=0.2)
        returned_before_release = not submit_thread.is_alive()
    finally:
        release.set()
        submit_thread.join(timeout=2.0)
        coordinator.stop()
    assert committed_before_deadline
    assert returned_before_release
    assert len(result) == 1
    assert isinstance(result[0], JobProjection)
    assert result[0].job.state is JobState.QUEUED


def test_stalled_commit_boundary_times_out_bounded_and_never_persists(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    started = threading.Event()
    release = threading.Event()
    chain = CommitBoundaryHeldChain(started, release)
    coordinator = JobCoordinator(
        chain_factory=lambda: chain,
        acknowledgement_timeout_seconds=0.05,
    )
    coordinator.start()
    result: list[JobProjection | BaseException] = []

    def submit() -> None:
        try:
            result.append(coordinator.submit(submit_command(source)))
        except BaseException as error:
            result.append(error)

    submit_thread = threading.Thread(target=submit)
    submit_thread.start()
    boundary_reached = False
    returned_before_release = False
    try:
        boundary_reached = started.wait(timeout=1.0)
        submit_thread.join(timeout=0.2)
        returned_before_release = not submit_thread.is_alive()
    finally:
        release.set()
        submit_thread.join(timeout=2.0)
        coordinator.stop()

    assert boundary_reached
    assert returned_before_release
    assert len(result) == 1
    assert isinstance(result[0], DrawingMachineError)
    assert result[0].payload.code == "COORDINATOR_ACK_TIMEOUT"
    assert "job-1" not in chain.records
    assert chain.worker_submits == 0


def test_stalled_projection_is_post_ack_and_cannot_extend_ack_deadline(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    started = threading.Event()
    release = threading.Event()
    chain = ProjectionHeldChain(started, release)
    coordinator = JobCoordinator(
        chain_factory=lambda: chain,
        acknowledgement_timeout_seconds=0.05,
    )
    coordinator.start()
    result: list[JobProjection | BaseException] = []

    def submit() -> None:
        try:
            result.append(coordinator.submit(submit_command(source)))
        except BaseException as error:
            result.append(error)

    submit_thread = threading.Thread(target=submit)
    submit_thread.start()
    projection_started = False
    returned_before_release = False
    try:
        projection_started = started.wait(timeout=1.0)
        submit_thread.join(timeout=0.2)
        returned_before_release = not submit_thread.is_alive()
    finally:
        release.set()
        submit_thread.join(timeout=2.0)
        coordinator.stop()

    assert projection_started
    assert returned_before_release
    assert len(result) == 1
    projection = result[0]
    assert isinstance(projection, JobProjection)
    assert projection.job.state is JobState.QUEUED
    assert projection.artifacts == ()


def test_restart_fails_inflight_without_submitting_worker() -> None:
    chain = FakeChain(inflight=True)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    projection = coordinator.status("job-1")
    assert projection.job.state is JobState.FAILED
    assert projection.job.error is not None and projection.job.error.code == "SERVICE_RESTARTED"
    assert chain.worker_submits == 0
    coordinator.stop()


@pytest.mark.parametrize("state", [JobState.IMAGE_READY, JobState.PATH_PLAN_READY])
def test_restart_safely_recovers_stable_resumable_states(state: JobState) -> None:
    chain = FakeChain()
    chain.records["job-1"] = replace(queued(), state=state, revision=2)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is state and time.monotonic() < deadline:
        time.sleep(0.01)
    recovered = coordinator.status("job-1").job
    assert recovered.state is JobState.FAILED
    assert recovered.error is not None and recovered.error.code == "SERVICE_RESTARTED"
    coordinator.stop()


def test_real_repository_restart_fails_durable_queued_without_worker(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    selected_config = load_config(valid_xdg_paths)
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    initial_workers = RestartWorkers()
    initial = OfflineJobChain(
        repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
        artifacts=RestartStore(),
        workers=initial_workers,
        config=selected_config,
        clock=clock,
    )
    initial.initialize()
    initial.submit(
        WorkflowSubmissionV1(1, "job", source, RouteMode.DIRECT, None, None),
        request_id="submit",
        job_id="job-queued",
        requester=requester(),
    )
    initial.close()
    restarted_workers: list[RestartWorkers] = []

    def factory() -> OfflineJobChain:
        workers = RestartWorkers()
        restarted_workers.append(workers)
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=RestartStore(),
            workers=workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    projection = coordinator.status("job-queued")
    assert projection.job.state is JobState.FAILED
    assert projection.job.error is not None and projection.job.error.code == "SERVICE_RESTARTED"
    assert restarted_workers[0].submit_count == 0
    coordinator.stop()


def test_real_durable_queued_gcode_check_restart_fails_without_import_or_worker(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G21\n", encoding="utf-8")
    selected_config = load_config(valid_xdg_paths)
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    initial_store = CountingFilesystemArtifactStore(valid_xdg_paths)
    initial = OfflineJobChain(
        repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
        artifacts=initial_store,
        workers=RestartWorkers(),
        config=selected_config,
        clock=clock,
    )
    initial.initialize()
    initial.submit_gcode_check(
        candidate,
        request_id="gcode-submit",
        job_id="gcode-restart",
        requester=requester(),
    )
    assert initial_store.import_count == 0
    initial.close()

    restarted_store = CountingFilesystemArtifactStore(valid_xdg_paths)
    restarted_workers = RestartWorkers()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=restarted_store,
            workers=restarted_workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    failed = coordinator.status("gcode-restart").job
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "SERVICE_RESTARTED"
    assert restarted_store.import_count == 0
    assert restarted_workers.submit_count == 0
    coordinator.stop()

    connection = sqlite3.connect(valid_xdg_paths.database_file)
    try:
        event = connection.execute(
            "SELECT event_id, job_revision, event_type, prior_state, result_state, request_id "
            "FROM job_events WHERE job_id = ? ORDER BY job_revision DESC LIMIT 1",
            ("gcode-restart",),
        ).fetchone()
        assert event is not None
        audit = connection.execute(
            "SELECT event_id, event_type, request_id FROM audit_events WHERE event_id = ?",
            (event[0],),
        ).fetchone()
    finally:
        connection.close()
    assert event[1] == failed.revision
    assert event[2:5] == ("job.service_restarted", JobState.QUEUED.value, JobState.FAILED.value)
    assert isinstance(event[5], str) and event[5].startswith("service-restart-")
    assert audit == (event[0], event[2], event[5])


def test_real_gcode_check_post_ack_deleted_candidate_fails_without_sticking_queued(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G21\n", encoding="utf-8")
    selected_config = load_config(valid_xdg_paths)
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    import_started = threading.Event()
    release_import = threading.Event()
    store = DelayedFilesystemArtifactStore(valid_xdg_paths, import_started, release_import)
    workers = RestartWorkers()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=store,
            workers=workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    acknowledged = coordinator.submit(gcode_command(candidate))
    assert acknowledged.job.state is JobState.QUEUED
    assert import_started.wait(timeout=2.0)
    candidate.unlink()
    release_import.set()
    deadline = time.monotonic() + 2.0
    while coordinator.status("gcode-job").job.state is JobState.QUEUED and time.monotonic() < deadline:
        time.sleep(0.01)
    failed = coordinator.status("gcode-job").job
    coordinator.stop()
    assert acknowledged.job.state is JobState.QUEUED
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "ORCHESTRATION_FAILED"
    assert store.import_count == 1
    assert workers.submit_count == 0
    connection = sqlite3.connect(valid_xdg_paths.database_file)
    try:
        event = connection.execute(
            "SELECT event_id, job_revision, event_type, prior_state, result_state, request_id "
            "FROM job_events WHERE job_id = ? ORDER BY job_revision DESC LIMIT 1",
            ("gcode-job",),
        ).fetchone()
        assert event is not None
        audit = connection.execute(
            "SELECT event_id, event_type, request_id FROM audit_events WHERE event_id = ?",
            (event[0],),
        ).fetchone()
    finally:
        connection.close()
    assert event[1] == failed.revision
    assert event[2:5] == ("job.orchestration_failed", JobState.QUEUED.value, JobState.FAILED.value)
    assert isinstance(event[5], str) and event[5].startswith("orchestration-failure-")
    assert audit == (event[0], event[2], event[5])


def test_real_blocked_restart_rehydrates_artifacts_and_review_reaches_next_worker(
    valid_xdg_paths: XdgPaths,
) -> None:
    source = Path("tests/fixtures/package_b/golden/outline.png").resolve()
    selected_config = load_config(valid_xdg_paths)
    selected_config = replace(
        selected_config,
        machine=replace(
            selected_config.machine,
            profile={
                "name": "restart-test",
                "planning": {
                    "threshold": None,
                    "invert": None,
                    "min_component_area_px": 8,
                    "dedupe_short_path_length_mm": 2.0,
                    "dedupe_distance_mm": 0.3,
                    "dedupe_angle_deg": 25.0,
                    "dedupe_overlap_ratio": 0.65,
                },
            },
        ),
    )
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    initial = OfflineJobChain(
        repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
        artifacts=FilesystemArtifactStore(valid_xdg_paths),
        workers=InlineWorkers(),
        config=selected_config,
        clock=clock,
    )
    initial.initialize()
    initial.submit(
        WorkflowSubmissionV1(1, "job", source, RouteMode.DIRECT, None, None),
        request_id="submit",
        job_id="job-restart",
        requester=requester(),
    )
    blocked = initial.run_to_intervention_or_terminal("job-restart")
    assert blocked.state is JobState.BLOCKED
    processed = next(item for item in initial.artifact_refs("job-restart") if item.role == "processed_image")
    initial.close()

    workers = CapturePlanningWorkers()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=FilesystemArtifactStore(valid_xdg_paths),
            workers=workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    continuation = replace(approved_review("job-restart"), reviewed_image_sha256=processed.sha256)
    command = CoordinatorCommand(
        CoordinatorCommandKind.CONTINUE_REVIEW,
        "review-restart",
        "job-restart",
        requester(),
        continuation,
    )
    assert coordinator.submit(command).job.state is JobState.IMAGE_READY
    observed = workers.called.wait(timeout=2.0)
    current = coordinator.status("job-restart").job
    if not observed:
        coordinator.stop()
    assert observed, (current.state, None if current.error is None else current.error.code)
    planning = workers.submitted[0]
    assert planning.kind.value == "paths.plan"
    assert len(planning.input_artifacts) == 1
    rehydrated = Path(planning.input_artifacts[0].absolute_path)
    assert rehydrated.is_file()
    assert rehydrated.is_relative_to(valid_xdg_paths.jobs_dir / "job-restart")
    coordinator.stop()


def test_damaged_persisted_review_artifact_restart_fails_closed_without_worker(
    valid_xdg_paths: XdgPaths,
) -> None:
    source = Path("tests/fixtures/package_b/golden/outline.png").resolve()
    selected_config = load_config(valid_xdg_paths)
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    initial = OfflineJobChain(
        repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
        artifacts=FilesystemArtifactStore(valid_xdg_paths),
        workers=InlineWorkers(),
        config=selected_config,
        clock=clock,
    )
    initial.initialize()
    initial.submit(
        WorkflowSubmissionV1(1, "job", source, RouteMode.DIRECT, None, None),
        request_id="submit-damaged",
        job_id="job-damaged",
        requester=requester(),
    )
    blocked = initial.run_to_intervention_or_terminal("job-damaged")
    assert blocked.state is JobState.BLOCKED
    processed = next(item for item in initial.artifact_refs("job-damaged") if item.role == "processed_image")
    damaged = valid_xdg_paths.jobs_dir / "job-damaged" / processed.relative_path
    damaged.write_bytes(b"damaged")
    initial.close()

    workers = RestartWorkers()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=FilesystemArtifactStore(valid_xdg_paths),
            workers=workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    failed = coordinator.status("job-damaged").job
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "ARTIFACT_REHYDRATION_FAILED"
    assert workers.submit_count == 0
    coordinator.stop()

    connection = sqlite3.connect(valid_xdg_paths.database_file)
    try:
        event = connection.execute(
            "SELECT event_id, job_revision, event_type, prior_state, result_state, request_id "
            "FROM job_events WHERE job_id = ? ORDER BY job_revision DESC LIMIT 1",
            ("job-damaged",),
        ).fetchone()
        assert event is not None
        audit = connection.execute(
            "SELECT event_id, event_type, request_id FROM audit_events WHERE event_id = ?",
            (event[0],),
        ).fetchone()
    finally:
        connection.close()
    assert event[1:] == (
        failed.revision,
        "job.orchestration_failed",
        JobState.BLOCKED.value,
        JobState.FAILED.value,
        event[5],
    )
    assert audit == (event[0], event[2], event[5])


def test_real_running_worker_cancel_clears_terminal_projection_flag_and_preserves_identity(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    selected_config = load_config(valid_xdg_paths)
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    workers = RunningCancelWorkers()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=FilesystemArtifactStore(valid_xdg_paths),
            workers=workers,
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    coordinator.submit(submit_command(source))
    assert workers.started.wait(timeout=2.0)
    immediate = coordinator.request_cancel("job-1", "cancel-running", requester())
    assert immediate.cancellation_requested is True
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.CANCELLED and time.monotonic() < deadline:
        time.sleep(0.01)
    final = coordinator.status("job-1")
    assert final.job.state is JobState.CANCELLED
    assert final.cancellation_requested is False
    coordinator.stop()

    repository = SQLiteRepository(valid_xdg_paths.database_file, clock=clock)
    repository.initialize()
    try:
        event = repository.list_job_events("job-1")[-1]
    finally:
        repository.close()
    assert event.request_id == "cancel-running"
    assert event.payload["requester"] == requester().to_json()
    assert workers.closed is True
    assert not list((valid_xdg_paths.jobs_dir / ".staging/job-1").glob("attempt-*"))
    assert not any(thread.name.startswith("drawingmachine-job-coordinator") for thread in threading.enumerate())


def test_cancel_token_is_set_immediately_and_coordinator_commits_cancel(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    hold = threading.Event()
    chain = FakeChain(hold=hold)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    coordinator.submit(submit_command(source))
    projection = coordinator.request_cancel("job-1", "cancel-1", requester())
    assert projection.cancellation_requested is True
    assert chain.tokens["job-1"].is_set()
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.CANCELLED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.status("job-1").job.state is JobState.CANCELLED
    assert coordinator.status("job-1").cancellation_requested is False
    coordinator.stop()


def test_factory_and_close_run_on_same_coordinator_thread_and_stop_leaves_no_thread() -> None:
    chain_holder: list[FakeChain] = []

    def factory() -> FakeChain:
        chain = FakeChain()
        chain_holder.append(chain)
        return chain

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    coordinator.stop()
    chain = chain_holder[0]
    assert chain.closed_thread == chain.created_thread
    assert not any(thread.name.startswith("drawingmachine-job-coordinator") for thread in threading.enumerate())


def test_projection_is_published_only_after_chain_commit(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    hold = threading.Event()
    chain = FakeChain(hold=hold)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    projection = coordinator.submit(submit_command(source))
    assert projection.job.state is JobState.QUEUED
    assert chain.trace == ["commit"]
    hold.set()
    coordinator.stop()


def test_coordinator_command_rejects_payload_kind_mismatch() -> None:
    with pytest.raises(DrawingMachineError, match="payload"):
        CoordinatorCommand(
            CoordinatorCommandKind.SUBMIT,
            "request-1",
            "job-1",
            requester(),
            None,
        )


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.IMAGE_READY, JobState.PATH_PLAN_READY])
def test_duplicate_submit_returns_original_error_without_mutating_existing_job(
    tmp_path: Path,
    state: JobState,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = FakeChain()
    original = replace(queued(), state=state, revision=2)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    chain.records["job-1"] = original
    with pytest.raises(DrawingMachineError) as captured:
        coordinator.submit(submit_command(source))
    assert captured.value.payload.code == "JOB_ALREADY_EXISTS"
    assert chain.records["job-1"] == original
    coordinator.stop()


def test_repeat_review_returns_original_error_without_mutating_job() -> None:
    chain = FakeChain()
    original = replace(queued(), state=JobState.IMAGE_READY, revision=3)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    chain.records["job-1"] = original
    with pytest.raises(DrawingMachineError) as captured:
        coordinator.submit(review_command())
    assert captured.value.payload.code == "REVIEW_NOT_ALLOWED"
    assert chain.records["job-1"] == original
    coordinator.stop()


def test_invalid_review_returns_original_error_without_mutating_blocked_job() -> None:
    original_error = DrawingMachineError(
        ErrorPayload("REVIEW_NOT_APPROVED", ErrorCategory.INPUT, "review rejected", False, {}, job_id="job-1")
    )
    chain = FakeChain(review_error=original_error)
    original = replace(
        queued(),
        state=JobState.BLOCKED,
        revision=3,
        blocker=JobBlocker("REVIEW_REQUIRED", ErrorCategory.VALIDATION, "review", {}),
    )
    chain.records["job-1"] = original
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    with pytest.raises(DrawingMachineError) as captured:
        coordinator.submit(review_command())
    assert captured.value is original_error
    assert chain.records["job-1"] == original
    coordinator.stop()


def test_post_ack_projection_failure_is_sanitized_transactionally(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = FakeChain(projection_failure_call=1)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    result = coordinator.submit(submit_command(source))
    assert result.job.state is JobState.QUEUED
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)
    failed = coordinator.status("job-1").job
    assert failed.error is not None and failed.error.code == "ORCHESTRATION_FAILED"
    assert "projection exploded" not in failed.error.message
    coordinator.stop()


@pytest.mark.parametrize("failure", [FileNotFoundError("source vanished"), OSError("promotion failed")])
def test_post_ack_drive_failure_keeps_queued_result_and_publishes_sanitized_failed(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    release = threading.Event()
    chain = FakeChain(hold=release, drive_error=failure)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    acknowledged = coordinator.submit(submit_command(source))
    assert acknowledged.job.state is JobState.QUEUED
    release.set()
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)
    failed = coordinator.status("job-1").job
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "ORCHESTRATION_FAILED"
    assert "vanished" not in failed.error.message and "promotion" not in failed.error.message
    coordinator.stop()


def test_real_chain_source_can_disappear_after_durable_ack_and_fails_transactionally(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    source = tmp_path / "vanishing.png"
    source.write_bytes(b"png")
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    selected_config = load_config(valid_xdg_paths)
    clock = SystemClock()
    release = threading.Event()

    def factory() -> OfflineJobChain:
        return OfflineJobChain(
            repository=SQLiteRepository(valid_xdg_paths.database_file, clock=clock),
            artifacts=DelayedMissingSourceStore(release),
            workers=RestartWorkers(),
            config=selected_config,
            clock=clock,
        )

    coordinator = JobCoordinator(chain_factory=factory)
    coordinator.start()
    acknowledged = coordinator.submit(submit_command(source))
    assert acknowledged.job.state is JobState.QUEUED
    source.unlink()
    release.set()
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)
    failed = coordinator.status("job-1").job
    assert failed.state is JobState.FAILED
    assert failed.error is not None and failed.error.code == "ORCHESTRATION_FAILED"
    assert "vanishing" not in failed.error.message
    coordinator.stop()


def test_post_ack_projection_failure_rebuilds_authoritative_failed_projection(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = FakeChain(projection_failure_call=2)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    acknowledged = coordinator.submit(submit_command(source))
    assert acknowledged.job.state is JobState.QUEUED
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.status("job-1").job.state is JobState.FAILED
    coordinator.stop()


def test_stop_atomically_cancels_active_and_prequeued_work_without_worker_start(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    hold = threading.Event()
    chain = FakeChain(hold=hold)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    coordinator.submit(submit_command(source))
    second = replace(submit_command(source), request_id="submit-2", job_id="job-2")
    second_result: list[object] = []

    def submit_second() -> None:
        try:
            second_result.append(coordinator.submit(second))
        except BaseException as error:
            second_result.append(error)

    submit_thread = threading.Thread(target=submit_second)
    submit_thread.start()
    deadline = time.monotonic() + 1.0
    while coordinator._mailbox.qsize() < 1 and time.monotonic() < deadline:  # type: ignore[attr-defined]
        time.sleep(0.005)
    stop_thread = threading.Thread(target=coordinator.stop)
    stop_thread.start()
    time.sleep(0.05)
    hold.set()
    stop_thread.join(timeout=2.0)
    submit_thread.join(timeout=2.0)
    assert not stop_thread.is_alive() and not submit_thread.is_alive()
    assert chain.worker_submits == 1
    assert coordinator._mailbox.empty()  # type: ignore[attr-defined]
    assert not any(thread.name.startswith("drawingmachine-job-coordinator") for thread in threading.enumerate())


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.COMPLETED, JobState.CANCELLED])
def test_cancel_rejects_non_cancellable_terminal_projection(state: JobState) -> None:
    from drawingmachine.errors import ErrorCategory, ErrorPayload

    chain = FakeChain()
    job = queued()
    error = (
        ErrorPayload("FAILED", ErrorCategory.INTERNAL, "failed", False, {}, request_id="r", job_id=job.job_id)
        if state is JobState.FAILED
        else None
    )
    if state is JobState.COMPLETED:
        job = replace(job, kind=JobKind.GCODE_CHECK)
    chain.records[job.job_id] = replace(job, state=state, revision=1, error=error)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    with pytest.raises(DrawingMachineError, match="cancell"):
        coordinator.request_cancel(job.job_id, "cancel", requester())
    assert coordinator.status(job.job_id).cancellation_requested is False
    coordinator.stop()


def test_cancel_rejects_empty_request_id() -> None:
    chain = FakeChain()
    chain.records["job-1"] = queued()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    with pytest.raises(DrawingMachineError, match="request_id"):
        coordinator.request_cancel("job-1", "", requester())
    coordinator.stop()


def test_cancel_precommit_failure_preserves_job_clears_flag_and_thread_survives(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = FakeChain(cancel_error=OSError("cancel precommit exploded"))
    chain.records["job-1"] = queued()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    coordinator.request_cancel("job-1", "cancel", requester())
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").cancellation_requested and time.monotonic() < deadline:
        time.sleep(0.01)
    projection = coordinator.status("job-1")
    assert projection.job.state is JobState.QUEUED
    assert projection.cancellation_requested is False
    assert not chain.tokens["job-1"].is_set()
    second = replace(submit_command(source), job_id="job-2", request_id="submit-2")
    assert coordinator.submit(second).job.state is JobState.QUEUED
    coordinator.stop()


def test_cancel_postcommit_projection_failure_rebuilds_cancelled_and_thread_survives(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = FakeChain(projection_failure_call=2)
    chain.records["job-1"] = queued()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    coordinator.request_cancel("job-1", "cancel", requester())
    deadline = time.monotonic() + 2.0
    while coordinator.status("job-1").job.state is not JobState.CANCELLED and time.monotonic() < deadline:
        time.sleep(0.01)
    projection = coordinator.status("job-1")
    assert projection.job.state is JobState.CANCELLED
    assert projection.cancellation_requested is False
    second = replace(submit_command(source), job_id="job-2", request_id="submit-2")
    assert coordinator.submit(second).job.state is JobState.QUEUED
    coordinator.stop()


def test_external_machine_outcome_publication_refreshes_on_job_thread_and_never_regresses() -> None:
    chain = FakeChain()
    current = replace(queued(), revision=7)
    chain.records[current.job_id] = current
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    try:
        recovered = replace(
            current,
            state=JobState.FAILED,
            revision=8,
            error=ErrorPayload(
                "MACHINE_RECOVERY_REQUIRED",
                ErrorCategory.HARDWARE,
                "machine outcome was committed",
                False,
                {},
                job_id=current.job_id,
            ),
        )
        chain.records[current.job_id] = recovered

        coordinator.publish_external(current.job_id)
        deadline = time.monotonic() + 1.0
        while coordinator.status(current.job_id).job.revision != 8 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert coordinator.status(current.job_id).job == recovered

        chain.records[current.job_id] = current
        coordinator.publish_external(current.job_id)
        time.sleep(0.03)
        assert coordinator.status(current.job_id).job == recovered
        assert chain.closed_thread is None
    finally:
        coordinator.stop()


def test_external_machine_outcome_publication_rejects_when_job_coordinator_is_stopped() -> None:
    coordinator = JobCoordinator(chain_factory=FakeChain)

    with pytest.raises(DrawingMachineError) as error:
        coordinator.publish_external("job-1")

    assert error.value.payload.code == "COORDINATOR_NOT_STARTED"


def test_envelope_deadlines_withdraw_authority_before_or_after_ack() -> None:
    command = CoordinatorCommand(CoordinatorCommandKind.STOP, "stop", "coordinator", requester(), None)
    expired = coordinator_module._Envelope(command, threading.Event(), deadline=0.0)
    assert expired.commit_allowed() is False
    assert expired.is_abandoned() is True

    acknowledged = coordinator_module._Envelope(command, threading.Event())
    assert acknowledged.acknowledge() is True
    assert acknowledged.abandon_unacknowledged() is False
    assert acknowledged.is_acknowledged() is True

    abandoned = coordinator_module._Envelope(command, threading.Event())
    assert abandoned.abandon_unacknowledged() is True
    assert abandoned.acknowledge() is False

    late_ack = coordinator_module._Envelope(command, threading.Event(), deadline=0.0)
    assert late_ack.acknowledge() is False
    assert late_ack.is_abandoned() is True


@pytest.mark.parametrize("timeout", [0.0, 1.01])
def test_coordinator_rejects_ack_timeout_outside_closed_bound(timeout: float) -> None:
    with pytest.raises(ValueError, match="must be in"):
        JobCoordinator(chain_factory=FakeChain, acknowledgement_timeout_seconds=timeout)


def test_startup_failure_closes_constructed_chain_and_rejects_admission() -> None:
    chain = FakeChain()
    chain.initialize = lambda: (_ for _ in ()).throw(OSError("initialize failed"))  # type: ignore[attr-defined]
    coordinator = JobCoordinator(chain_factory=lambda: chain)

    with pytest.raises(OSError, match="initialize failed"):
        coordinator.start()

    assert chain.closed_thread is not None
    with pytest.raises(DrawingMachineError) as caught:
        coordinator.publish_external("job-1")
    assert caught.value.payload.code == "COORDINATOR_NOT_STARTED"


@pytest.mark.parametrize("job_id", ["", 1])
def test_external_publication_rejects_noncanonical_job_identity(job_id: object) -> None:
    coordinator = JobCoordinator(chain_factory=FakeChain)
    with pytest.raises(DrawingMachineError) as caught:
        coordinator.publish_external(job_id)  # type: ignore[arg-type]
    assert caught.value.payload.code == "EXTERNAL_PUBLICATION_INVALID"


def test_cancel_recovery_removes_missing_authority_and_clears_stale_request() -> None:
    chain = FakeChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator._projections["missing"] = JobProjection(queued("missing"), (), True)  # type: ignore[attr-defined]
    coordinator._recover_cancel_failure(chain, "missing")  # type: ignore[attr-defined]
    assert coordinator._projection_or_none("missing") is None  # type: ignore[attr-defined]

    chain.records["job-1"] = queued()
    token = coordinator._token_for("job-1")  # type: ignore[attr-defined]
    token.set()
    coordinator._projections["job-1"] = JobProjection(chain.records["job-1"], (), True)  # type: ignore[attr-defined]
    coordinator._recover_cancel_failure(chain, "job-1")  # type: ignore[attr-defined]
    recovered = coordinator._projection_or_none("job-1")  # type: ignore[attr-defined]
    assert recovered is not None and recovered.cancellation_requested is False
    assert token.is_set() is False


def test_cancel_wake_is_idempotent_for_missing_and_already_cancelled_projection() -> None:
    chain = FakeChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    wake = coordinator_module._CancelWake("job-1", "cancel", requester())
    coordinator._process_cancel(chain, wake)  # type: ignore[attr-defined]
    assert chain.trace == []

    cancelled = replace(queued(), state=JobState.CANCELLED, revision=1)
    coordinator._projections["job-1"] = JobProjection(cancelled, (), True)  # type: ignore[attr-defined]
    coordinator._process_cancel(chain, wake)  # type: ignore[attr-defined]
    projection = coordinator._projection_or_none("job-1")  # type: ignore[attr-defined]
    assert projection == JobProjection(cancelled, (), False)


def test_projection_rebuild_preserves_last_artifacts_when_authoritative_read_fails() -> None:
    chain = FakeChain(projection_failure_call=1)
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    previous = JobProjection(queued(), (), True)
    coordinator._projections["job-1"] = previous  # type: ignore[attr-defined]

    rebuilt = coordinator._rebuild_projection(  # type: ignore[attr-defined]
        chain,
        replace(previous.job, revision=1),
        cancellation_requested=False,
    )

    assert rebuilt.artifacts == previous.artifacts
    assert rebuilt.cancellation_requested is False


def test_authoritative_lookup_distinguishes_absence_from_repository_failure() -> None:
    class Chain:
        def get(self, job_id: str) -> JobRecord:
            if job_id == "missing":
                raise DrawingMachineError(ErrorPayload("JOB_NOT_FOUND", ErrorCategory.INPUT, "missing", False, {}))
            raise DrawingMachineError(ErrorPayload("DATABASE_FAILED", ErrorCategory.INTERNAL, "failed", False, {}))

    assert JobCoordinator._authoritative_or_none(Chain(), "missing") is None  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError) as caught:
        JobCoordinator._authoritative_or_none(Chain(), "failed")  # type: ignore[arg-type]
    assert caught.value.payload.code == "DATABASE_FAILED"
    assert JobCoordinator._same_authoritative_revision(None, None) is True
    assert JobCoordinator._same_authoritative_revision(None, queued()) is False


def test_startup_handles_factory_failure_and_chain_without_existing_job_enumerator() -> None:
    failed = JobCoordinator(chain_factory=lambda: (_ for _ in ()).throw(OSError("factory failed")))
    with pytest.raises(OSError, match="factory failed"):
        failed.start()

    chain = FakeChain()
    chain.jobs = None  # type: ignore[method-assign]
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    assert coordinator._projections == {}  # type: ignore[attr-defined]
    coordinator.stop()


def test_stop_timeout_is_reported_without_claiming_shutdown() -> None:
    class StuckThread:
        def join(self, timeout: float) -> None:
            assert timeout == 10.0

        def is_alive(self) -> bool:
            return True

    coordinator = JobCoordinator(chain_factory=FakeChain)
    coordinator._thread = StuckThread()  # type: ignore[assignment,attr-defined]
    with pytest.raises(DrawingMachineError) as caught:
        coordinator.stop()
    assert caught.value.payload.code == "COORDINATOR_STOP_TIMEOUT"


def test_unavailable_gcode_orchestration_fails_without_creating_job(tmp_path: Path) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_text("G21\n", encoding="ascii")
    chain = FakeChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    try:
        with pytest.raises(DrawingMachineError) as caught:
            coordinator.submit(gcode_command(source))
        assert caught.value.payload.code == "GCODE_CHECK_UNAVAILABLE"
        assert "gcode-job" not in chain.records
    finally:
        coordinator.stop()


def test_post_ack_failure_and_late_abandonment_both_commit_sanitized_failure(tmp_path: Path) -> None:
    class PostAckFailureChain(FakeChain):
        def _coordinator_submit(self, *args: object, **kwargs: object) -> JobRecord:
            job = super()._coordinator_submit(*args, **kwargs)  # type: ignore[arg-type]
            assert job is not None
            raise OSError("after durable ack")

    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain = PostAckFailureChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    coordinator.start()
    acknowledged = coordinator.submit(submit_command(source))
    assert acknowledged.job.state is JobState.QUEUED
    deadline = time.monotonic() + 1.0
    while coordinator.status("job-1").job.state is not JobState.FAILED and time.monotonic() < deadline:
        time.sleep(0.005)
    assert coordinator.status("job-1").job.error is not None
    coordinator.stop()

    class LateChain(FakeChain):
        def _coordinator_submit(self, *args: object, **kwargs: object) -> JobRecord:
            del args, kwargs
            self.records["job-1"] = queued()
            return self.records["job-1"]

    late_chain = LateChain()
    late = JobCoordinator(chain_factory=lambda: late_chain)
    envelope = coordinator_module._Envelope(submit_command(source), threading.Event())
    assert envelope.abandon_unacknowledged()
    late._process_command(late_chain, envelope)  # type: ignore[attr-defined]
    assert late_chain.records["job-1"].state is JobState.FAILED


def test_abandoned_ack_and_changed_revision_failure_never_publish_stale_projection(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    coordinator = JobCoordinator(chain_factory=FakeChain)
    envelope = coordinator_module._Envelope(submit_command(source), threading.Event())
    assert envelope.abandon_unacknowledged()
    coordinator._acknowledge_durable(envelope, queued())  # type: ignore[attr-defined]
    assert coordinator._projection_or_none("job-1") is None  # type: ignore[attr-defined]

    chain = FakeChain()
    chain.records["job-1"] = replace(queued(), revision=1)
    fresh = coordinator_module._Envelope(submit_command(source), threading.Event())
    coordinator._complete_pre_ack_failure(chain, fresh, OSError("failed"), queued())  # type: ignore[attr-defined]
    assert fresh.result is not None and fresh.result.job.state is JobState.FAILED
    assert fresh.acknowledged.is_set()


def test_pre_ack_failure_falls_back_to_authoritative_or_sanitized_error(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")

    class FailingChain(FakeChain):
        def fail_unexpected(self, *args: object, **kwargs: object) -> JobRecord:
            del args, kwargs
            raise OSError("failure write failed")

    coordinator = JobCoordinator(chain_factory=FakeChain)
    chain = FailingChain()
    chain.records["job-1"] = replace(queued(), revision=1)
    envelope = coordinator_module._Envelope(submit_command(source), threading.Event())
    coordinator._complete_pre_ack_failure(chain, envelope, OSError("original"), queued())  # type: ignore[attr-defined]
    assert envelope.result is not None and envelope.error is None

    class UnreadableChain(FailingChain):
        def get(self, job_id: str) -> JobRecord:
            del job_id
            raise OSError("repository unavailable")

    unreadable = UnreadableChain()
    unreadable.records["job-1"] = replace(queued(), revision=1)
    failed = coordinator_module._Envelope(submit_command(source), threading.Event())
    coordinator._complete_pre_ack_failure(unreadable, failed, OSError("original"), queued())  # type: ignore[attr-defined]
    assert failed.result is None
    assert isinstance(failed.error, DrawingMachineError)
    assert failed.error.payload.code == "ORCHESTRATION_FAILED"


def test_cancel_recovery_does_not_clear_token_during_shutdown() -> None:
    chain = FakeChain()
    chain.records["job-1"] = queued()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    token = coordinator._token_for("job-1")  # type: ignore[attr-defined]
    token.set()
    coordinator._stopping = True  # type: ignore[attr-defined]
    coordinator._recover_cancel_failure(chain, "job-1")  # type: ignore[attr-defined]
    assert token.is_set()


@pytest.mark.parametrize("with_state", [False, True])
def test_double_cancel_recovery_failure_clears_only_existing_live_state(with_state: bool) -> None:
    chain = FakeChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    reached_recovery = threading.Event()

    def fail_cancel(chain: FakeChain, wake: object) -> NoReturn:
        del chain, wake
        raise OSError("cancel failed")

    def fail_recovery(chain: FakeChain, job_id: str) -> NoReturn:
        del chain, job_id
        reached_recovery.set()
        raise OSError("recovery failed")

    coordinator._process_cancel = fail_cancel  # type: ignore[method-assign]
    coordinator._recover_cancel_failure = fail_recovery  # type: ignore[method-assign]
    coordinator.start()
    try:
        if with_state:
            token = coordinator._token_for("job-1")  # type: ignore[attr-defined]
            token.set()
            coordinator._projections["job-1"] = JobProjection(queued(), (), True)  # type: ignore[attr-defined]
        coordinator._mailbox.put(coordinator_module._CancelWake("job-1", "cancel", requester()))  # type: ignore[attr-defined]
        assert reached_recovery.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while (
            with_state
            and coordinator._projection_or_none("job-1").cancellation_requested
            and time.monotonic() < deadline
        ):  # type: ignore[union-attr]
            time.sleep(0.005)
        if with_state:
            projection = coordinator._projection_or_none("job-1")  # type: ignore[attr-defined]
            assert projection is not None and projection.cancellation_requested is False
            assert coordinator._tokens["job-1"].is_set() is False  # type: ignore[attr-defined]
        else:
            assert coordinator._projection_or_none("job-1") is None  # type: ignore[attr-defined]
    finally:
        coordinator.stop()


def test_pre_ack_failure_with_lost_authority_returns_sanitized_error(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")

    class LostAfterFailureChain(FakeChain):
        def __init__(self) -> None:
            super().__init__()
            self.records["job-1"] = replace(queued(), revision=1)
            self.reads = 0

        def get(self, job_id: str) -> JobRecord:
            self.reads += 1
            if self.reads == 1:
                return self.records[job_id]
            raise KeyError(job_id)

        def fail_unexpected(self, *args: object, **kwargs: object) -> JobRecord:
            del args, kwargs
            raise OSError("failure write failed")

    chain = LostAfterFailureChain()
    coordinator = JobCoordinator(chain_factory=lambda: chain)
    envelope = coordinator_module._Envelope(submit_command(source), threading.Event())
    coordinator._complete_pre_ack_failure(chain, envelope, OSError("original"), queued())  # type: ignore[attr-defined]
    assert envelope.result is None
    assert isinstance(envelope.error, DrawingMachineError)
    assert envelope.error.payload.code == "ORCHESTRATION_FAILED"
    assert envelope.acknowledged.is_set()
