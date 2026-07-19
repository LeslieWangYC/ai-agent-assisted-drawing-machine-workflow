from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.filesystem._artifact_validation import (
    artifact_error,
    validate_manifests,
    validate_prepared_tree,
    validate_relative_path,
    validate_text,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.artifacts import ExpectedArtifact, WorkerArtifact


def _worker(
    *,
    role: str = "result",
    path: str = "result.json",
    digest: object = "a" * 64,
    size: object = 2,
    media_type: str = "application/json",
) -> WorkerArtifact:
    return WorkerArtifact(role, path, cast(str, digest), cast(int, size), media_type)


def _expected(
    *,
    role: str = "result",
    path: str = "result.json",
    media_type: str = "application/json",
    object_required: object = True,
    values: object = (),
) -> ExpectedArtifact:
    return ExpectedArtifact(
        role,
        path,
        media_type,
        cast(bool, object_required),
        cast(tuple[tuple[str, str | int], ...], values),
    )


def _assert_validation_error(call: object) -> None:
    assert isinstance(call, DrawingMachineError)
    assert call.payload.category.value == "validation"


def test_artifact_error_defaults_and_scalar_validators_reject_invalid_values() -> None:
    assert artifact_error("bad").payload.details == {}
    assert artifact_error("bad", details={"field": "x"}).payload.details == {"field": "x"}
    for value in ("", "line\nfeed"):
        with pytest.raises(DrawingMachineError) as error:
            validate_text(value, "value", job_id="job-1")
        _assert_validation_error(error.value)
    for value in (cast(str, 1), ""):
        with pytest.raises(DrawingMachineError) as error:
            validate_relative_path(value, field="path", job_id="job-1")
        _assert_validation_error(error.value)


@pytest.mark.parametrize(
    "workers",
    [
        (_worker(), _worker(path="other.json")),
        (_worker(), _worker(role="other")),
        (_worker(size=True),),
        (_worker(size=-1),),
        (_worker(digest="A" * 64),),
    ],
)
def test_manifest_rejects_invalid_worker_declarations(workers: tuple[WorkerArtifact, ...]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        validate_manifests(workers, (_expected(),), job_id="job-1")

    _assert_validation_error(error.value)


@pytest.mark.parametrize(
    "expected",
    [
        (_expected(), _expected(path="other.json")),
        (_expected(), _expected(role="other")),
        (_expected(object_required=1),),
        (_expected(values=(("", 1),)),),
        (_expected(values=(("version", 1), ("version", 2))),),
        (_expected(values=(("version", True),)),),
        (_expected(object_required=False, values=(("version", 1),)),),
    ],
)
def test_manifest_rejects_invalid_expected_declarations(expected: tuple[ExpectedArtifact, ...]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        validate_manifests((_worker(),), expected, job_id="job-1")

    _assert_validation_error(error.value)


def test_manifest_rejects_descriptor_set_mismatch() -> None:
    with pytest.raises(DrawingMachineError) as error:
        validate_manifests((_worker(),), (_expected(media_type="text/plain"),), job_id="job-1")

    _assert_validation_error(error.value)


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "fifo", "missing"])
def test_prepared_tree_rejects_each_unsafe_or_missing_entry(tmp_path: Path, entry_kind: str) -> None:
    manifest = validate_manifests((_worker(),), (_expected(values=()),), job_id="job-1")
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    if entry_kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        (prepared / "result.json").symlink_to(target)
    elif entry_kind == "directory":
        (prepared / "undeclared").mkdir()
    elif entry_kind == "fifo":
        os.mkfifo(prepared / "result.json")

    descriptor = os.open(prepared, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(DrawingMachineError) as error:
            validate_prepared_tree(
                descriptor,
                manifest,
                job_id="job-1",
                attempt_id="attempt-1",
            )
    finally:
        os.close(descriptor)

    _assert_validation_error(error.value)
