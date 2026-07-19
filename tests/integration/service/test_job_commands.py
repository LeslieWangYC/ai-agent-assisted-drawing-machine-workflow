from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drawingmachine.cli.client import ServiceClient
from drawingmachine.cli.jobs import wait_for_job
from drawingmachine.config import XdgPaths, load_config
from drawingmachine.domain.jobs import JobKind, JobState, RequesterIdentity
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.repository import RepositoryHealth
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest
from drawingmachine.service import ServiceRuntime
from drawingmachine.service.job_coordinator import CoordinatorCommandKind, JobProjection
from drawingmachine.service.models import PeerCredentials, RequestContext
from drawingmachine.service.server import UnixServiceServer


@dataclass(slots=True)
class FakeJob:
    job_id: str
    state: JobState = JobState.QUEUED
    revision: int = 0
    kind: JobKind = JobKind.OFFLINE_WORKFLOW

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "revision": self.revision,
        }


class FakeRepository:
    def initialize(self) -> int:
        return 1

    def health(self) -> RepositoryHealth:
        return RepositoryHealth(1, "wal", True)

    def append_audit(self, *args: object) -> None:
        return None

    def close(self) -> None:
        return None


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


class FakeCoordinator:
    def __init__(self) -> None:
        self.commands: list[object] = []
        self.started = False
        self.stopped = False
        self.projections: dict[str, JobProjection] = {}

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def submit(self, command: object) -> JobProjection:
        self.commands.append(command)
        job_id = command.job_id
        kind = command.kind
        projection = JobProjection(
            FakeJob(
                job_id,
                kind=JobKind.GCODE_CHECK if kind is CoordinatorCommandKind.GCODE_CHECK else JobKind.OFFLINE_WORKFLOW,
            ),  # type: ignore[arg-type]
            (),
            False,
        )
        self.projections[job_id] = projection
        return projection

    def status(self, job_id: str) -> JobProjection:
        try:
            return self.projections[job_id]
        except KeyError as error:
            raise DrawingMachineError(
                ErrorPayload("JOB_NOT_FOUND", ErrorCategory.INPUT, "job not found", False, {}, job_id=job_id)
            ) from error

    def request_cancel(self, job_id: str, request_id: str, requester: RequesterIdentity) -> JobProjection:
        del request_id, requester
        prior = self.status(job_id)
        projection = JobProjection(prior.job, prior.artifacts, True)
        self.projections[job_id] = projection
        return projection


def _runtime(paths: XdgPaths, coordinator: FakeCoordinator) -> ServiceRuntime:
    runtime = ServiceRuntime(
        paths=paths,
        config=load_config(paths),
        repository=FakeRepository(),  # type: ignore[arg-type]
        clock=FakeClock(),
    )
    runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]
    runtime.start()
    return runtime


def _dispatch(runtime: ServiceRuntime, command: str, arguments: JsonObject):
    return runtime.dispatcher.dispatch(
        ProtocolRequest(PROTOCOL_VERSION, command, f"req-{command}", arguments),
        RequestContext(PeerCredentials(123, 1000, 1000), datetime(2026, 7, 11, tzinfo=UTC)),
    )


def test_workflow_submit_status_and_cancel_exact_data(valid_xdg_paths: XdgPaths, tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"not-sent-on-socket")
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        submitted = _dispatch(
            runtime,
            "workflow.run",
            {
                "schema_version": 1,
                "mode": "submit",
                "job_name": "drawing",
                "input_path": str(source.resolve()),
                "route_mode": "auto",
                "semantic_assessment": {"drawable": True},
                "prompt": None,
            },
        )
        assert submitted.ok is True
        assert set(submitted.data) == {"schema_version", "accepted", "job_id", "revision", "state"}
        assert submitted.data["schema_version"] == 1
        job_id = submitted.data["job_id"]
        assert isinstance(job_id, str)

        status = _dispatch(runtime, "job.status", {"schema_version": 1, "job_id": job_id})
        assert status.ok is True
        assert set(status.data) == {"schema_version", "job", "artifacts", "cancellation_requested"}

        cancelled = _dispatch(runtime, "job.cancel", {"schema_version": 1, "job_id": job_id})
        assert cancelled.data == {
            "schema_version": 1,
            "accepted": True,
            "job_id": job_id,
            "cancellation_requested": True,
        }
        command = coordinator.commands[0]
        assert command.payload.input_path == source.resolve()
        assert command.requester == RequesterIdentity("LOCAL_PEER", 123, 1000, 1000)
    finally:
        runtime.stop()
    assert coordinator.stopped is True


@pytest.mark.parametrize(
    "arguments",
    [
        {"schema_version": True, "job_id": "job-1"},
        {"schema_version": 2, "job_id": "job-1"},
        {"schema_version": 1, "job_id": 3},
        {"schema_version": 1, "job_id": "job-1", "extra": False},
    ],
)
def test_job_arguments_reject_wrong_type_version_and_unknown_keys(
    valid_xdg_paths: XdgPaths, arguments: JsonObject
) -> None:
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        response = _dispatch(runtime, "job.status", arguments)
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "COMMAND_ARGUMENTS_INVALID"
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
            "extra": False,
        },
        {
            "schema_version": 1,
            "mode": "start",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": 3,
            "input_path": "/tmp/input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "relative/input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "invalid",
            "semantic_assessment": None,
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "auto",
            "semantic_assessment": None,
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "direct",
            "semantic_assessment": [],
            "prompt": None,
        },
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": "/tmp/input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": 3,
        },
        {
            "schema_version": True,
            "mode": "resume_review",
            "job_id": "job-1",
            "review": {},
            "reviewed_image_sha256": "0" * 64,
        },
        {
            "schema_version": 1,
            "mode": "resume_review",
            "job_id": "job-1",
            "review": {},
            "reviewed_image_sha256": "0" * 64,
            "extra": False,
        },
        {
            "schema_version": 1,
            "mode": "resume_review",
            "job_id": 3,
            "review": {},
            "reviewed_image_sha256": "0" * 64,
        },
        {
            "schema_version": 1,
            "mode": "resume_review",
            "job_id": "job-1",
            "review": [],
            "reviewed_image_sha256": "0" * 64,
        },
    ],
)
def test_workflow_arguments_reject_unknown_keys_wrong_types_and_literals(
    valid_xdg_paths: XdgPaths,
    arguments: JsonObject,
) -> None:
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        response = _dispatch(runtime, "workflow.run", arguments)
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "COMMAND_ARGUMENTS_INVALID"
        assert coordinator.commands == []
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "arguments",
    [
        {"schema_version": True, "path": "/tmp/candidate.gcode"},
        {"schema_version": 2, "path": "/tmp/candidate.gcode"},
        {"schema_version": 1, "path": 3},
        {"schema_version": 1, "path": "relative/candidate.gcode"},
        {"schema_version": 1, "path": "/tmp/candidate.gcode", "extra": False},
    ],
)
def test_gcode_arguments_reject_unknown_keys_wrong_types_and_relative_paths(
    valid_xdg_paths: XdgPaths,
    arguments: JsonObject,
) -> None:
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        response = _dispatch(runtime, "gcode.check", arguments)
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "COMMAND_ARGUMENTS_INVALID"
        assert coordinator.commands == []
    finally:
        runtime.stop()


def test_gcode_check_enqueues_without_reading_bytes_in_dispatcher(valid_xdg_paths: XdgPaths, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G0 X0\n", encoding="utf-8")
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        response = _dispatch(runtime, "gcode.check", {"schema_version": 1, "path": str(candidate.resolve())})
        assert response.ok is True
        assert response.data["schema_version"] == 1
        assert response.data["state"] == "QUEUED"
        assert response.data["static_result"] is None
        assert coordinator.commands[0].kind is CoordinatorCommandKind.GCODE_CHECK
        assert coordinator.commands[0].payload == candidate.resolve()
    finally:
        runtime.stop()


def test_job_wait_is_not_a_service_command(valid_xdg_paths: XdgPaths) -> None:
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)
    try:
        response = _dispatch(runtime, "job.wait", {"schema_version": 1, "job_id": "job-1"})
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "COMMAND_UNKNOWN"
    finally:
        runtime.stop()


def test_tmp_unix_socket_submit_status_cancel_and_client_polled_wait(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    source = tmp_path / "socket-input.png"
    source.write_bytes(b"socket-carries-path-only")
    coordinator = FakeCoordinator()
    runtime = _runtime(valid_xdg_paths, coordinator)

    async def exercise() -> None:
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, FakeClock())
        serve_task = asyncio.create_task(server.serve())
        await server.wait_started()
        client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None)
        try:
            submitted = await asyncio.to_thread(
                client.call,
                "workflow.run",
                {
                    "schema_version": 1,
                    "mode": "submit",
                    "job_name": "socket-job",
                    "input_path": str(source.resolve()),
                    "route_mode": "direct",
                    "semantic_assessment": None,
                    "prompt": None,
                },
            )
            assert submitted.ok is True
            job_id = submitted.data["job_id"]
            assert isinstance(job_id, str)
            coordinator.projections[job_id] = JobProjection(FakeJob(job_id, JobState.READY_TO_RUN), (), False)  # type: ignore[arg-type]
            waited = await asyncio.to_thread(
                wait_for_job,
                argparse.Namespace(job_id=job_id, timeout_seconds=1.0, poll_interval_seconds=0.01),
                client=client,
            )
            assert waited.command == "job.wait"
            assert waited.ok is True
            cancelled = await asyncio.to_thread(
                client.call,
                "job.cancel",
                {"schema_version": 1, "job_id": job_id},
            )
            assert cancelled.data["cancellation_requested"] is True
        finally:
            await server.close()
            await serve_task

    try:
        asyncio.run(exercise())
    finally:
        runtime.stop()

    assert not valid_xdg_paths.socket_file.exists()
