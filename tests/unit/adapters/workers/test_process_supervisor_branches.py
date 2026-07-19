from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.adapters.workers import process_supervisor as supervisor
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.workers import WorkerKind, WorkerTaskV1


class FakeProcess:
    pid: int | None = None
    exitcode: int | None = None

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        del timeout


class FakeEvent:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def is_set(self) -> bool:
        return self.value

    def set(self) -> None:
        self.value = True


class FakeReceiver:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False

    def recv_bytes(self, maximum: int) -> bytes:
        del maximum
        if self.error is not None:
            raise self.error
        return b"{}"

    def poll(self, timeout: float) -> bool:
        del timeout
        return False

    def close(self) -> None:
        self.closed = True


def _task(tmp_path: Path) -> WorkerTaskV1:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return WorkerTaskV1(
        1,
        "task",
        "job",
        0,
        "attempt",
        WorkerKind.TEST_ECHO,
        (),
        {"application": "a" * 64},
        str(staging),
        {"mode": "echo", "nested": []},
    )


def test_wire_strict_json_and_cancellation_branches() -> None:
    with pytest.raises(ValueError):
        supervisor._strict_loads(b'{"a":1,"a":2}')
    with pytest.raises(ValueError):
        supervisor._strict_loads(b'{"a":NaN}')
    original = supervisor._worker_cancel_event
    try:
        supervisor._worker_cancel_event = None
        assert supervisor.worker_cancellation_requested() is False
        supervisor._worker_cancel_event = FakeEvent(True)  # type: ignore[assignment]
        assert supervisor.worker_cancellation_requested() is True
    finally:
        supervisor._worker_cancel_event = original


def test_staging_and_input_verification_missing_branches(tmp_path: Path) -> None:
    with pytest.raises(supervisor._InputVerificationError):
        supervisor._verify_staging_dir(str(tmp_path / "missing"))
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    with pytest.raises(supervisor._InputVerificationError):
        supervisor._verify_staging_dir(str(regular))
    task = _task(tmp_path)
    assert supervisor._verify_input_artifacts(task) is None


def test_handle_supervisor_argument_and_closed_branches(tmp_path: Path) -> None:
    handle = supervisor.SpawnProcessHandle("task", "attempt", FakeProcess())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        _ = handle.pid
    with pytest.raises(ValueError):
        supervisor.SpawnProcessSupervisor(entrypoint_module="", entrypoint_name="name")
    selected = supervisor.SpawnProcessSupervisor(entrypoint_module="builtins", entrypoint_name="len")
    selected.close()
    selected.close()
    with pytest.raises(DrawingMachineError):
        selected.submit(_task(tmp_path))
    with pytest.raises(DrawingMachineError):
        selected._record_for(cast_handle := cast(Any, object()))
    assert cast_handle is not None


@pytest.mark.parametrize("error", [OSError("bad message length"), OSError("other"), EOFError()])
def test_outcome_reader_error_classification(tmp_path: Path, error: BaseException) -> None:
    task = _task(tmp_path)
    handle = supervisor.SpawnProcessHandle("task", "attempt", FakeProcess())  # type: ignore[arg-type]
    receiver = FakeReceiver(error)
    record = supervisor._WorkerRecord(task, handle, receiver, FakeEvent())  # type: ignore[arg-type]
    supervisor.SpawnProcessSupervisor._read_outcome(record)
    expected = "WORKER_OUTCOME_TOO_LARGE" if str(error) == "bad message length" else "WORKER_PROCESS_CRASHED"
    assert record.read_error == expected and record.read_complete.is_set()


def test_reap_assertion_mark_joined_and_invalid_timeout_branches(tmp_path: Path) -> None:
    task = _task(tmp_path)
    handle = supervisor.SpawnProcessHandle("task", "attempt", FakeProcess())  # type: ignore[arg-type]
    record = supervisor._WorkerRecord(task, handle, FakeReceiver(), FakeEvent())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        supervisor.SpawnProcessSupervisor._assert_reaped(record)
    handle._joined = True
    supervisor.SpawnProcessSupervisor._assert_reaped(record)
    selected = supervisor.SpawnProcessSupervisor(entrypoint_module="builtins", entrypoint_name="len")
    selected._records[task.task_id] = record
    with pytest.raises(ValueError):
        selected.wait(handle, timeout_seconds=0.0, cancel_requested=lambda: False)
    with pytest.raises(ValueError):
        selected.cancel(handle, grace_seconds=3.0)
    selected._close_receiver(record)
    assert cast(Any, record.receiver).closed is True
