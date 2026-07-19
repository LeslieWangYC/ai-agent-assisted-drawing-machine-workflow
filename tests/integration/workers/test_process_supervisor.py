from __future__ import annotations

import hashlib
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from drawingmachine.adapters.workers import SpawnProcessHandle, SpawnProcessSupervisor
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.workers import (
    WorkerInputArtifact,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)

ENTRYPOINT_MODULE = "tests.fixtures.package_b.workers.echo_worker"
ENTRYPOINT_NAME = "run"


def make_task(tmp_path: Path, *, mode: str = "echo", task_id: str = "task-1") -> WorkerTaskV1:
    source = tmp_path / f"{task_id}.bin"
    source.write_bytes(b"verified input")
    staging = tmp_path / f"{task_id}-staging"
    staging.mkdir(exist_ok=True)
    return WorkerTaskV1(
        schema_version=1,
        task_id=task_id,
        job_id="job-1",
        job_revision=4,
        attempt_id=f"{task_id}-attempt",
        kind=WorkerKind.TEST_HANG if mode == "hang" else WorkerKind.TEST_ECHO,
        input_artifacts=(
            WorkerInputArtifact(
                role="input",
                absolute_path=str(source.resolve()),
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                size_bytes=source.stat().st_size,
                media_type="application/octet-stream",
            ),
        ),
        config_digests={"application": "a" * 64},
        staging_dir=str(staging.resolve()),
        payload={"mode": mode},
    )


@pytest.fixture
def supervisor() -> SpawnProcessSupervisor:
    value = SpawnProcessSupervisor(entrypoint_module=ENTRYPOINT_MODULE, entrypoint_name=ENTRYPOINT_NAME)
    try:
        yield value
    finally:
        value.close()


def assert_reaped(handle: SpawnProcessHandle) -> None:
    assert not handle.is_alive()
    assert handle.joined
    assert handle.exitcode is not None
    assert handle.pid not in {child.pid for child in multiprocessing.active_children()}
    assert not any(thread.name == f"drawingmachine-outcome-{handle.task_id}" for thread in threading.enumerate())


def wait(supervisor: SpawnProcessSupervisor, handle: SpawnProcessHandle, timeout: float = 3.0):
    return supervisor.wait(handle, timeout_seconds=timeout, cancel_requested=lambda: False)


def with_input(task: WorkerTaskV1, path: Path, *, sha256: str, size_bytes: int) -> WorkerTaskV1:
    return WorkerTaskV1(
        schema_version=task.schema_version,
        task_id=task.task_id,
        job_id=task.job_id,
        job_revision=task.job_revision,
        attempt_id=task.attempt_id,
        kind=task.kind,
        input_artifacts=(
            WorkerInputArtifact(
                role="input",
                absolute_path=str(path.resolve()),
                sha256=sha256,
                size_bytes=size_bytes,
                media_type="application/octet-stream",
            ),
        ),
        config_digests=task.config_digests,
        staging_dir=task.staging_dir,
        payload=task.payload,
    )


class _FalseOnceAfterExitReceiver:
    def __init__(self, receiver: object, handle: SpawnProcessHandle) -> None:
        self._receiver = receiver
        self._handle = handle
        self._first_poll = True

    def poll(self, timeout: float = 0.0) -> bool:
        if self._first_poll:
            self._first_poll = False
            deadline = time.monotonic() + 2.0
            while self._handle.is_alive() and time.monotonic() < deadline:
                time.sleep(0.005)
            assert not self._handle.is_alive()
            assert self._receiver.poll(1.0)  # type: ignore[attr-defined]
            return False
        return self._receiver.poll(timeout)  # type: ignore[attr-defined,no-any-return]

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        return self._receiver.recv_bytes(maxlength)  # type: ignore[attr-defined,no-any-return]

    def close(self) -> None:
        self._receiver.close()  # type: ignore[attr-defined]


def test_supervisor_uses_spawn_and_reaps_success_after_producer_exit(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    assert supervisor.start_method == "spawn"
    handle = supervisor.submit(make_task(tmp_path, mode="linger"))

    started = time.monotonic()
    outcome = wait(supervisor, handle)

    assert time.monotonic() - started >= 0.15
    assert outcome.status is WorkerStatus.SUCCEEDED
    assert outcome.metrics["input_count"] == 1
    assert_reaped(handle)


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("blocker", WorkerStatus.BLOCKED, "REVIEW_REQUIRED"),
        ("failure", WorkerStatus.FAILED, "FIXTURE_FAILURE"),
        ("raise", WorkerStatus.FAILED, "WORKER_UNCAUGHT_EXCEPTION"),
        ("malformed", WorkerStatus.FAILED, "WORKER_OUTCOME_INVALID"),
        ("oversized", WorkerStatus.FAILED, "WORKER_OUTCOME_TOO_LARGE"),
        ("crash", WorkerStatus.FAILED, "WORKER_PROCESS_CRASHED"),
        ("nonzero_after_send", WorkerStatus.FAILED, "WORKER_PROCESS_CRASHED"),
        ("identity_mismatch", WorkerStatus.FAILED, "WORKER_IDENTITY_MISMATCH"),
    ],
)
def test_wait_returns_typed_reaped_outcomes(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
    mode: str,
    status: WorkerStatus,
    code: str,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode=mode, task_id=f"task-{mode}"))

    outcome = wait(supervisor, handle, timeout=5.0)

    assert outcome.status is status
    assert outcome.error is not None
    assert outcome.error.code == code
    assert_reaped(handle)
    if mode == "raise":
        assert "fixture-secret" not in outcome.error.message
        assert "fixture-secret" not in str(dict(outcome.error.details))


def test_wait_times_out_and_reaps_worker(tmp_path: Path, supervisor: SpawnProcessSupervisor) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="hang"))

    started = time.monotonic()
    outcome = wait(supervisor, handle, timeout=0.15)

    assert time.monotonic() - started < 1.5
    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "WORKER_TIMEOUT"
    assert_reaped(handle)


def test_wait_observes_cancel_callback_and_reaps_worker(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="hang"))

    outcome = supervisor.wait(handle, timeout_seconds=5.0, cancel_requested=lambda: True)

    assert outcome.status is WorkerStatus.CANCELLED
    assert_reaped(handle)


def test_wait_reaps_worker_when_cancel_callback_raises(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="hang"))

    def broken_callback() -> bool:
        raise RuntimeError("cancel state unavailable")

    with pytest.raises(RuntimeError, match="cancel state unavailable"):
        supervisor.wait(handle, timeout_seconds=5.0, cancel_requested=broken_callback)

    assert_reaped(handle)


def test_cancel_terminates_worker_after_two_second_grace(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="hang"))
    started = time.monotonic()

    outcome = supervisor.cancel(handle, grace_seconds=0.1)

    assert outcome.status is WorkerStatus.CANCELLED
    assert time.monotonic() - started < 1.5
    assert_reaped(handle)


def test_cancel_requests_cooperative_shutdown_before_termination(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="cooperative_cancel"))

    outcome = supervisor.cancel(handle, grace_seconds=1.0)

    assert outcome.status is WorkerStatus.CANCELLED
    assert outcome.error is not None
    assert outcome.error.code == "FIXTURE_CANCELLED_COOPERATIVELY"
    assert_reaped(handle)


def test_cancel_rejects_grace_period_above_two_seconds(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path))

    with pytest.raises(ValueError, match="at most two seconds"):
        supervisor.cancel(handle, grace_seconds=2.001)

    wait(supervisor, handle)
    assert_reaped(handle)


def test_cancel_after_completion_is_idempotent(tmp_path: Path, supervisor: SpawnProcessSupervisor) -> None:
    handle = supervisor.submit(make_task(tmp_path))
    completed = wait(supervisor, handle)

    assert supervisor.cancel(handle) is completed
    assert supervisor.wait(handle, timeout_seconds=0.1, cancel_requested=lambda: False) is completed
    assert_reaped(handle)


def test_cancel_preserves_outcome_already_sent_by_a_naturally_completed_worker(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="linger"))

    outcome = supervisor.cancel(handle, grace_seconds=1.0)

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert_reaped(handle)


def test_cancel_drains_large_bounded_outcome_during_grace_period(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="large_after_cancel"))

    outcome = supervisor.cancel(handle, grace_seconds=1.0)

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert len(outcome.metrics["blob"]) == 900_000
    assert_reaped(handle)


def test_wait_rechecks_pipe_after_observing_process_exit(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    handle = supervisor.submit(make_task(tmp_path))
    record = supervisor._records[handle.task_id]
    record.receiver = _FalseOnceAfterExitReceiver(record.receiver, handle)  # type: ignore[assignment]

    outcome = wait(supervisor, handle)

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert_reaped(handle)


@pytest.mark.parametrize(
    ("operation", "expected_status", "expected_code"),
    [
        ("wait", WorkerStatus.FAILED, "WORKER_TIMEOUT"),
        ("cancel", WorkerStatus.CANCELLED, "WORKER_CANCELLED"),
    ],
)
def test_partial_outcome_frame_cannot_exceed_wait_or_cancel_deadline(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
    operation: str,
    expected_status: WorkerStatus,
    expected_code: str,
) -> None:
    handle = supervisor.submit(make_task(tmp_path, mode="partial_frame_stall"))
    outcomes: list[WorkerOutcomeV1] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            if operation == "wait":
                outcomes.append(supervisor.wait(handle, timeout_seconds=0.15, cancel_requested=lambda: False))
            else:
                outcomes.append(supervisor.cancel(handle, grace_seconds=0.15))
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=invoke, name=f"partial-frame-{operation}")
    caller.start()
    caller.join(1.0)
    exceeded_deadline = caller.is_alive()
    if exceeded_deadline:
        handle._process.terminate()
        handle._process.join(0.5)
        if handle._process.is_alive():
            handle._process.kill()
            handle._process.join(0.5)
    caller.join(2.0)

    assert not caller.is_alive()
    assert not exceeded_deadline, f"{operation} blocked after a partial outcome frame"
    assert errors == []
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is expected_status
    assert outcome.error is not None
    assert outcome.error.code == expected_code
    assert_reaped(handle)


def test_duplicate_task_id_is_rejected(tmp_path: Path, supervisor: SpawnProcessSupervisor) -> None:
    first = supervisor.submit(make_task(tmp_path, mode="hang"))

    with pytest.raises(DrawingMachineError, match="duplicate"):
        supervisor.submit(make_task(tmp_path, task_id="task-1"))

    supervisor.cancel(first, grace_seconds=0.0)
    assert_reaped(first)


def test_invalid_input_digest_fails_before_entrypoint(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
) -> None:
    task = make_task(tmp_path)
    bad = with_input(
        task,
        Path(task.input_artifacts[0].absolute_path),
        sha256="f" * 64,
        size_bytes=task.input_artifacts[0].size_bytes,
    )

    handle = supervisor.submit(bad)
    outcome = wait(supervisor, handle)

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "WORKER_INPUT_INVALID"
    assert_reaped(handle)


@pytest.mark.parametrize("input_kind", ["fifo", "symlink"])
def test_non_regular_or_symlink_input_fails_without_blocking(
    tmp_path: Path,
    supervisor: SpawnProcessSupervisor,
    input_kind: str,
) -> None:
    task = make_task(tmp_path, task_id=f"task-{input_kind}")
    special = tmp_path / input_kind
    if input_kind == "fifo":
        os.mkfifo(special)
    else:
        special.symlink_to(Path(task.input_artifacts[0].absolute_path))
    bad = with_input(task, special, sha256=hashlib.sha256(b"").hexdigest(), size_bytes=0)

    started = time.monotonic()
    outcome = wait(supervisor, supervisor.submit(bad), timeout=1.0)

    assert time.monotonic() - started < 0.75
    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "WORKER_INPUT_INVALID"


def test_close_reaps_every_owned_child(tmp_path: Path) -> None:
    supervisor = SpawnProcessSupervisor(entrypoint_module=ENTRYPOINT_MODULE, entrypoint_name=ENTRYPOINT_NAME)
    handles = [supervisor.submit(make_task(tmp_path, mode="hang", task_id=f"task-{index}")) for index in range(3)]

    supervisor.close()

    assert all(handle.joined and not handle.is_alive() for handle in handles)
    assert not ({handle.pid for handle in handles} & {child.pid for child in multiprocessing.active_children()})
