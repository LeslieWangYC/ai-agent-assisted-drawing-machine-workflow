from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import drawingmachine.adapters.workers.tasks as task_io
from drawingmachine.adapters.workers.tasks import TaskOutput, read_inputs, write_bundle
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.workers import WorkerInputArtifact, WorkerKind, WorkerTaskV1


def _task(
    tmp_path: Path,
    *,
    source: Path | None = None,
    declared_size: int | None = None,
    declared_digest: str | None = None,
    staging: Path | None = None,
) -> WorkerTaskV1:
    source = source or tmp_path / "input.png"
    if not source.exists():
        source.write_bytes(b"input")
    content = source.read_bytes() if source.is_file() else b""
    staging = staging or tmp_path / "staging"
    if not staging.exists():
        staging.mkdir()
    return WorkerTaskV1(
        1,
        "task-io",
        "job-io",
        1,
        "attempt-io",
        WorkerKind.IMAGE_PREPARE,
        (
            WorkerInputArtifact(
                "input_image",
                str(source.resolve()),
                declared_digest or hashlib.sha256(content).hexdigest(),
                len(content) if declared_size is None else declared_size,
                "image/png",
            ),
        ),
        {"application": "a" * 64},
        str(staging.resolve()),
        {
            "mode": "direct",
            "source_name": "input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        },
    )


def _assert_code(error: pytest.ExceptionInfo[DrawingMachineError], code: str) -> None:
    assert error.value.payload.code == code


def test_read_inputs_rejects_role_set_before_opening(tmp_path: Path) -> None:
    selected = _task(tmp_path)

    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"processed_image"}))

    _assert_code(error, "WORKER_INPUT_INVALID")


def test_read_inputs_rejects_missing_and_nonregular_sources(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    selected = _task(tmp_path, source=missing)
    missing.unlink()
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")

    directory = tmp_path / "directory-input"
    directory.mkdir()
    selected = _task(tmp_path, source=directory)
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")


def test_read_inputs_rejects_size_digest_and_open_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path, declared_size=99)
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")

    digest_selected = _task(tmp_path, declared_digest="b" * 64)
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(digest_selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")

    monkeypatch.setattr(task_io.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(_task(tmp_path), frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")


def test_read_inputs_rejects_descriptor_identity_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path)
    real_fstat = task_io.os.fstat

    def changed_identity(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        values = list(result)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(task_io.os, "fstat", changed_identity)
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")


def test_read_inputs_rejects_identity_change_after_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path)
    real_fstat = task_io.os.fstat
    calls = 0

    def change_after_read(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(task_io.os, "fstat", change_after_read)
    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input_image"}))
    _assert_code(error, "WORKER_INPUT_INVALID")


@pytest.mark.parametrize(
    "outputs",
    [
        (
            TaskOutput("same", "first.bin", "application/octet-stream", b"a"),
            TaskOutput("same", "second.bin", "application/octet-stream", b"b"),
        ),
        (
            TaskOutput("first", "same.bin", "application/octet-stream", b"a"),
            TaskOutput("second", "same.bin", "application/octet-stream", b"b"),
        ),
    ],
)
def test_write_bundle_rejects_duplicate_roles_or_paths(tmp_path: Path, outputs: tuple[TaskOutput, ...]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(_task(tmp_path), outputs)

    _assert_code(error, "WORKER_OUTPUT_INVALID")


@pytest.mark.parametrize("relative_path", ["/absolute.bin", "nested/file.bin", ".", ".."])
def test_write_bundle_rejects_noncanonical_output_paths(tmp_path: Path, relative_path: str) -> None:
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(
            _task(tmp_path),
            (TaskOutput("output", relative_path, "application/octet-stream", b"value"),),
        )

    _assert_code(error, "WORKER_OUTPUT_INVALID")


def test_write_bundle_rejects_non_directory_and_nonempty_staging(tmp_path: Path) -> None:
    staging_file = tmp_path / "staging-file"
    staging_file.write_bytes(b"not a directory")
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(
            _task(tmp_path, staging=staging_file),
            (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),),
        )
    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")

    selected = _task(tmp_path)
    (Path(selected.staging_dir) / "existing.bin").write_bytes(b"existing")
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))
    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")


def test_write_bundle_cleans_partial_output_when_write_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _task(tmp_path)
    monkeypatch.setattr(task_io.os, "write", lambda descriptor, content: 0)

    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))

    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")
    assert list(Path(selected.staging_dir).iterdir()) == []


def test_write_bundle_rejects_unstable_created_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path)
    real_stat = task_io.os.stat

    def wrong_size(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == "output.bin" and kwargs.get("dir_fd") is not None:
            values = list(result)
            values[6] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(task_io.os, "stat", wrong_size)
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))

    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")
    assert list(Path(selected.staging_dir).iterdir()) == []


def test_write_bundle_rejects_opened_staging_identity_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _task(tmp_path)
    real_fstat = task_io.os.fstat
    calls = 0

    def changed_staging(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 1:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(task_io.os, "fstat", changed_staging)
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))
    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")


def test_write_bundle_rejects_nonregular_created_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _task(tmp_path)
    real_fstat = task_io.os.fstat
    calls = 0

    def nonregular_output(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            values = list(result)
            values[0] = stat.S_IFDIR | 0o700
            return os.stat_result(values)
        return result

    monkeypatch.setattr(task_io.os, "fstat", nonregular_output)
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))
    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")
    assert list(Path(selected.staging_dir).iterdir()) == []


def test_write_bundle_rejects_staging_identity_change_before_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _task(tmp_path)
    real_lstat = task_io.os.lstat
    staging_calls = 0

    def changed_staging(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal staging_calls
        result = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == selected.staging_dir:
            staging_calls += 1
            if staging_calls == 2:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(task_io.os, "lstat", changed_staging)
    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, (TaskOutput("output", "output.bin", "application/octet-stream", b"value"),))
    _assert_code(error, "WORKER_OUTPUT_WRITE_FAILED")
    assert list(Path(selected.staging_dir).iterdir()) == []
