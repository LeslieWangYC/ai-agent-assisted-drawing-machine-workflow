from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.adapters.workers import process_supervisor, resource_limits
from drawingmachine.adapters.workers import tasks as task_io
from drawingmachine.adapters.workers.resource_limits import WorkerResourcePolicy
from drawingmachine.adapters.workers.tasks import TaskOutput, open_bounded_image, read_inputs, write_bundle
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.artifacts import MAX_GCODE_IMPORT_BYTES, MAX_IMAGE_IMPORT_BYTES, WorkerArtifact
from drawingmachine.ports.workers import (
    WorkerInputArtifact,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)


def _policy(*, input_bytes: int = 8, output_bytes: int = 8, bundle_bytes: int = 12) -> WorkerResourcePolicy:
    return WorkerResourcePolicy(
        max_input_file_bytes=input_bytes,
        max_output_file_bytes=output_bytes,
        max_output_bundle_bytes=bundle_bytes,
        max_image_pixels=100,
        address_space_bytes=512 * 1024 * 1024,
        cpu_seconds=60,
        open_files=64,
    )


def _task(tmp_path: Path, *, kind: WorkerKind = WorkerKind.TEST_ECHO, content: bytes = b"input") -> WorkerTaskV1:
    source = tmp_path / f"{kind.value.replace('.', '-')}.bin"
    source.write_bytes(content)
    staging = tmp_path / f"{kind.value.replace('.', '-')}-staging"
    staging.mkdir()
    payload: dict[str, object] = {"mode": "echo", "nested": []}
    if kind is WorkerKind.IMAGE_PREPARE:
        payload = {
            "mode": "direct",
            "source_name": "input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        }
    return WorkerTaskV1(
        1,
        "resource-task",
        "resource-job",
        0,
        "resource-attempt",
        kind,
        (
            WorkerInputArtifact(
                "input_image" if kind is WorkerKind.IMAGE_PREPARE else "input",
                str(source.resolve()),
                hashlib.sha256(content).hexdigest(),
                len(content),
                "image/png" if kind is WorkerKind.IMAGE_PREPARE else "application/octet-stream",
            ),
        ),
        {"application": "a" * 64},
        str(staging.resolve()),
        payload,
    )


def _code(error: pytest.ExceptionInfo[DrawingMachineError]) -> str:
    return error.value.payload.code


def test_worker_resource_policies_are_kind_specific_and_bounded() -> None:
    policies = {kind: resource_limits.policy_for(kind) for kind in WorkerKind}

    assert policies[WorkerKind.IMAGE_PREPARE] != policies[WorkerKind.GCODE_CHECK]
    for policy in policies.values():
        assert 0 < policy.max_input_file_bytes <= 32 * 1024 * 1024
        assert 0 < policy.max_output_file_bytes <= policy.max_output_bundle_bytes
        assert policy.max_image_pixels > 0
        assert policy.address_space_bytes > 0
        assert policy.cpu_seconds > 0
        assert policy.open_files > 0

    with pytest.raises(resource_limits.WorkerResourceLimitError):
        resource_limits.policy_for(cast(WorkerKind, "future.worker"))


def test_worker_input_limits_bind_to_initial_import_limits() -> None:
    policies = {kind: resource_limits.policy_for(kind) for kind in WorkerKind}

    assert policies[WorkerKind.IMAGE_PREPARE].max_input_file_bytes == MAX_IMAGE_IMPORT_BYTES
    assert policies[WorkerKind.PROVIDER_LOCAL_COMFYUI].max_input_file_bytes == MAX_IMAGE_IMPORT_BYTES
    assert policies[WorkerKind.PATHS_PLAN].max_input_file_bytes == MAX_IMAGE_IMPORT_BYTES
    assert policies[WorkerKind.GCODE_BUILD].max_input_file_bytes == MAX_GCODE_IMPORT_BYTES
    assert policies[WorkerKind.GCODE_CHECK].max_input_file_bytes == MAX_GCODE_IMPORT_BYTES
    assert task_io.MAX_WORKER_INPUT_BYTES == MAX_IMAGE_IMPORT_BYTES


def test_input_limit_is_checked_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path, content=b"12345")
    monkeypatch.setattr(resource_limits, "policy_for", lambda _kind: _policy(input_bytes=4))
    monkeypatch.setattr(task_io.os, "read", lambda *_args: pytest.fail("oversized input must not be read"))

    with pytest.raises(DrawingMachineError) as error:
        read_inputs(selected, frozenset({"input"}))

    assert _code(error) == "WORKER_INPUT_INVALID"


@pytest.mark.parametrize(
    "outputs",
    [
        (TaskOutput("one", "one.bin", "application/octet-stream", b"123456789"),),
        (
            TaskOutput("one", "one.bin", "application/octet-stream", b"1234567"),
            TaskOutput("two", "two.bin", "application/octet-stream", b"1234567"),
        ),
    ],
)
def test_output_limits_fail_before_staging_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: tuple[TaskOutput, ...],
) -> None:
    selected = _task(tmp_path)
    monkeypatch.setattr(resource_limits, "policy_for", lambda _kind: _policy())
    monkeypatch.setattr(task_io.os, "open", lambda *_args, **_kwargs: pytest.fail("limit must precede filesystem open"))

    with pytest.raises(DrawingMachineError) as error:
        write_bundle(selected, outputs)

    assert _code(error) == "WORKER_OUTPUT_LIMIT_EXCEEDED"
    assert list(Path(selected.staging_dir).iterdir()) == []


def test_image_dimension_limit_is_checked_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _task(tmp_path, kind=WorkerKind.IMAGE_PREPARE)
    monkeypatch.setattr(resource_limits, "policy_for", lambda _kind: _policy())

    class FakeImage:
        size = (11, 10)

        def __enter__(self) -> FakeImage:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def load(self) -> None:
            pytest.fail("oversized image must be rejected before decompression")

        def copy(self) -> Any:
            pytest.fail("oversized image must not be copied")

    monkeypatch.setattr(task_io.Image, "open", lambda *_args, **_kwargs: FakeImage())

    with pytest.raises(DrawingMachineError) as error:
        open_bounded_image(selected, b"image")

    assert _code(error) == "WORKER_IMAGE_LIMIT_EXCEEDED"


def test_outcome_artifact_limits_reject_declared_oversize() -> None:
    task = WorkerTaskV1(
        1,
        "task",
        "job",
        0,
        "attempt",
        WorkerKind.GCODE_CHECK,
        (),
        {"application": "a" * 64},
        "/tmp/staging",
        {
            "machine_build_profile": {},
            "expected_gcode_sha256": "a" * 64,
        },
    )
    outcome = WorkerOutcomeV1(
        1,
        task.task_id,
        task.job_id,
        task.job_revision,
        task.attempt_id,
        WorkerStatus.SUCCEEDED,
        (WorkerArtifact("readiness", "readiness.json", "b" * 64, 9, "application/json"),),
        {},
        None,
    )

    with pytest.raises(resource_limits.WorkerResourceLimitError):
        resource_limits.validate_outcome(task, outcome, policy=_policy())


def test_outcome_artifact_limits_reject_aggregate_oversize() -> None:
    task = WorkerTaskV1(
        1,
        "task",
        "job",
        0,
        "attempt",
        WorkerKind.GCODE_CHECK,
        (),
        {"application": "a" * 64},
        "/tmp/staging",
        {
            "machine_build_profile": {},
            "expected_gcode_sha256": "a" * 64,
        },
    )
    artifacts = tuple(
        WorkerArtifact(
            f"readiness-{index}",
            f"readiness-{index}.json",
            str(index) * 64,
            7,
            "application/json",
        )
        for index in (1, 2)
    )
    outcome = WorkerOutcomeV1(
        1,
        task.task_id,
        task.job_id,
        task.job_revision,
        task.attempt_id,
        WorkerStatus.SUCCEEDED,
        artifacts,
        {},
        None,
    )

    with pytest.raises(resource_limits.WorkerResourceLimitError, match="bundle"):
        resource_limits.validate_outcome(task, outcome, policy=_policy())


def test_process_limits_apply_cpu_memory_file_and_descriptor_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    class FakeResource:
        RLIM_INFINITY = -1
        RLIMIT_AS = 1
        RLIMIT_CPU = 2
        RLIMIT_FSIZE = 3
        RLIMIT_NOFILE = 4

        @staticmethod
        def getrlimit(_name: int) -> tuple[int, int]:
            return (-1, -1)

        @staticmethod
        def setrlimit(name: int, limits: tuple[int, int]) -> None:
            calls.append((name, limits))

    monkeypatch.setattr(resource_limits, "_resource", FakeResource)
    resource_limits.apply_process_limits(_policy())

    assert {name for name, _limits in calls} == {1, 2, 3, 4}
    assert all(soft == hard and soft > 0 for _name, (soft, hard) in calls)


def test_process_limits_fail_closed_when_kernel_hard_limit_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class ZeroHardLimit:
        RLIM_INFINITY = -1
        RLIMIT_AS = 1
        RLIMIT_CPU = 2
        RLIMIT_FSIZE = 3
        RLIMIT_NOFILE = 4

        @staticmethod
        def getrlimit(_name: int) -> tuple[int, int]:
            return (0, 0)

        @staticmethod
        def setrlimit(_name: int, _limits: tuple[int, int]) -> None:
            pytest.fail("a zero kernel hard limit must fail before setrlimit")

    monkeypatch.setattr(resource_limits, "_resource", ZeroHardLimit)
    with pytest.raises(resource_limits.WorkerResourceLimitError):
        resource_limits.apply_process_limits(_policy())


def test_supervisor_preflight_uses_kind_input_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _task(tmp_path, content=b"12345")
    monkeypatch.setattr(resource_limits, "policy_for", lambda _kind: _policy(input_bytes=4))
    monkeypatch.setattr(
        process_supervisor.os,
        "read",
        lambda *_args: pytest.fail("supervisor must reject an oversized input before hashing"),
    )

    with pytest.raises(process_supervisor._InputVerificationError, match="resource limit"):
        process_supervisor._verify_input_artifacts(selected)
