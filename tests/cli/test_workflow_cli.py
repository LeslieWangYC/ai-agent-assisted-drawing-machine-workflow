from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from drawingmachine.cli import workflow
from drawingmachine.config import XdgPaths
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def test_workflow_cli_freezes_request_id_before_staging_and_transfers_before_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    events: list[tuple[str, object]] = []
    staged_path = tmp_path / "imports/image/drop/bundle.stage/payload"
    staged = SimpleNamespace(payload_path=staged_path)

    class Staging:
        def stage_image(self, path: Path, request_id: str, clock: object):
            events.append(("stage", (path, request_id, clock)))
            return staged

        def transfer_before_write(self, value: object):
            events.append(("transfer", value))
            return value

        def discard_definitely_unsent(self, value: object, outcome: object) -> None:
            events.append(("discard", (value, outcome)))

    class Service:
        def __init__(self, path: Path) -> None:
            events.append(("service", path))

        def call(self, command: str, arguments: object, *, request_id: str, before_write) -> ProtocolResponse:
            events.append(("call", (command, arguments, request_id)))
            before_write()
            return ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                command,
                request_id,
                {"schema_version": 1, "accepted": True, "job_id": "job", "revision": 0, "state": "QUEUED"},
                None,
            )

    paths = XdgPaths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "run")
    monkeypatch.setattr(workflow, "resolve_xdg_paths", lambda: paths)
    monkeypatch.setattr(workflow, "ServiceClient", Service)
    monkeypatch.setattr("drawingmachine.cli.service.client_input_staging", lambda _paths: Staging())
    clock = object()
    monkeypatch.setattr("drawingmachine.cli.service.staging_clock", lambda: clock)
    args = argparse.Namespace(
        resume_job=None,
        review_json=None,
        job_name="drawing",
        input_image=str(source),
        route_mode="direct",
        semantic_assessment_json=None,
        prompt=None,
        prompt_file=None,
    )

    response = workflow.run_workflow(args)

    assert response.ok is True
    stage_request_id = events[1][1][1]
    command, arguments, sent_request_id = events[2][1]
    assert command == "workflow.run"
    assert sent_request_id == stage_request_id
    assert arguments["input_path"] == str(staged_path)
    assert [event[0] for event in events] == ["service", "stage", "call", "transfer"]


def _arguments(**changes: object) -> argparse.Namespace:
    values = {
        "resume_job": None,
        "review_json": None,
        "job_name": "drawing",
        "input_image": "/private/input.png",
        "route_mode": "direct",
        "semantic_assessment_json": None,
        "prompt": None,
        "prompt_file": None,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    "args",
    [
        _arguments(resume_job="job-1", review_json="review.json", job_name="conflict"),
        _arguments(
            resume_job="job-1",
            review_json=None,
            job_name=None,
            input_image=None,
            route_mode=None,
        ),
        _arguments(review_json="review.json"),
        _arguments(job_name=None),
    ],
)
def test_workflow_cli_rejects_conflicting_or_incomplete_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    args: argparse.Namespace,
) -> None:
    monkeypatch.setattr(workflow, "resolve_xdg_paths", lambda: XdgPaths(tmp_path, tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(workflow, "ServiceClient", lambda _path: object())

    with pytest.raises(DrawingMachineError) as caught:
        workflow.run_workflow(args)

    assert caught.value.payload.code == "WORKFLOW_ARGUMENT_INVALID"


def test_resume_returns_failed_status_before_export_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text("{}", encoding="utf-8")
    failed = ProtocolResponse(PROTOCOL_VERSION, SCHEMA_VERSION, False, "job.status", "request", None, None)

    class Service:
        def __init__(self, _path: Path) -> None:
            return

        def call(self, _command: str, _arguments: object) -> ProtocolResponse:
            return failed

    monkeypatch.setattr(workflow, "resolve_xdg_paths", lambda: XdgPaths(tmp_path, tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(workflow, "ServiceClient", Service)
    monkeypatch.setattr("drawingmachine.cli.service.client_input_staging", lambda _paths: object())

    assert (
        workflow.run_workflow(
            _arguments(
                resume_job="job-1",
                review_json=str(review_path),
                job_name=None,
                input_image=None,
                route_mode=None,
            )
        )
        is failed
    )


def test_workflow_json_reader_requires_an_object(tmp_path: Path) -> None:
    document = tmp_path / "review.json"
    document.write_text("[]", encoding="utf-8")

    with pytest.raises(DrawingMachineError) as caught:
        workflow._read_json_object(document)

    assert caught.value.payload.code == "JSON_FILE_INVALID"


def test_reviewed_export_binding_rejects_missing_invalid_or_ambiguous_authority(tmp_path: Path) -> None:
    review_path = tmp_path / "review.json"
    with pytest.raises(DrawingMachineError, match="non-empty path"):
        workflow._reviewed_export_sha256({}, review_path, "job-1", {}, object())  # type: ignore[arg-type]

    review = {"processed_image": "processed.png"}
    for status_data in ({"artifacts": None}, {"artifacts": []}):
        with pytest.raises(DrawingMachineError) as caught:
            workflow._reviewed_export_sha256(review, review_path, "job-1", status_data, object())  # type: ignore[arg-type]
        assert caught.value.payload.code == "REVIEW_IMAGE_INVALID"


def test_reviewed_export_binding_rejects_byte_drift_and_maps_reader_errors(tmp_path: Path) -> None:
    image = tmp_path / "processed.png"
    image.write_bytes(b"reviewed")
    exported = b"controlled"
    artifact = ArtifactRef(
        "processed_image",
        "artifacts/attempt/processed.png",
        hashlib.sha256(exported).hexdigest(),
        len(exported),
        "image/png",
    )
    status = {"artifacts": [artifact.to_json()]}
    review = {"processed_image": str(image)}
    staging = SimpleNamespace(read_exported_artifact=lambda _job_id, _artifact: exported)

    with pytest.raises(DrawingMachineError) as mismatch:
        workflow._reviewed_export_sha256(review, image, "job-1", status, staging)
    assert mismatch.value.payload.code == "WORKFLOW_CONTRACT_INVALID"

    error = DrawingMachineError(ErrorPayload("ARTIFACT_PATH_INVALID", ErrorCategory.VALIDATION, "bad", False, {}))
    failing = SimpleNamespace(read_exported_artifact=lambda _job_id, _artifact: (_ for _ in ()).throw(error))
    with pytest.raises(DrawingMachineError) as mapped:
        workflow._reviewed_export_sha256(review, image, "job-1", status, failing)
    assert mapped.value.payload.code == "REVIEW_IMAGE_INVALID"
