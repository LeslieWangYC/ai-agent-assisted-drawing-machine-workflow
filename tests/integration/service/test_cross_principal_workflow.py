from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.adapters.filesystem.client_data import FilesystemClientInputStaging, FilesystemServiceClientData
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.jobs import RouteMode, WorkflowSubmissionV1
from drawingmachine.application.offline_job_chain import OfflineJobChain
from drawingmachine.config import ServiceAccessConfigV1, XdgPaths, load_config
from drawingmachine.config.openclaw_deployment import PosixGroupV1, PosixPrincipalV1
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.client_data import ClientDataRole, ServiceClientDataPort, StagingAdmissionState
from drawingmachine.ports.workers import WorkerSupervisor
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest, decode_response, encode_request
from drawingmachine.service import ServiceRuntime
from drawingmachine.service.access_policy import PathAccessFactsV1, ServiceAccessSnapshotV1
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.job_coordinator import CoordinatorCommand, JobProjection
from drawingmachine.service.models import PeerCredentials, RequestContext
from drawingmachine.service.runtime import _staging_job_id
from drawingmachine.service.server import UnixServiceServer


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)

    def boot_id(self) -> str:
        return "boot-a"

    def monotonic_ns(self) -> int:
        return 100


class _Reader:
    def read_bytes(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("not used")


def _snapshot(tmp_path: Path) -> ServiceAccessSnapshotV1:
    service = PosixPrincipalV1(os.getuid(), os.getgid())
    automation = PosixPrincipalV1(service.uid + 10_001, service.gid + 10_001)
    operator = PosixPrincipalV1(service.uid + 10_002, service.gid + 10_002)
    config = ServiceAccessConfigV1(
        service,
        automation,
        operator,
        PosixGroupV1("drawingmachine-connect", service.gid + 10_003),
        tmp_path / "run/service",
        tmp_path / "run/service/service.sock",
        tmp_path / "run/automation/service.sock",
        tmp_path / "run/operator/service.sock",
        tmp_path / "data",
        tmp_path / "data/imports/automation",
        tmp_path / "data/exports/automation",
        tmp_path / "automation/imports",
        tmp_path / "automation/exports",
        tmp_path / "config/openclaw-deployment.toml",
    )
    source = PathAccessFactsV1(
        tmp_path / "config/service-access.toml",
        "regular",
        1,
        1,
        service.uid,
        config.connect_group.gid,
        0o640,
        (),
    )
    outer = PathAccessFactsV1(tmp_path / "run", "directory", 1, 2, service.uid, service.gid, 0o710, ())
    return ServiceAccessSnapshotV1(
        config,
        source.path,
        1,
        "a" * 64,
        source.identity,
        service,
        0o640,
        (),
        source,
        "b" * 64,
        outer,
    )


def test_automation_stages_for_service_claim_while_operator_job_access_is_denied(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    client = FilesystemClientInputStaging.for_test(imports, exports)
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))

    service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)
    claimed = service.claim_image(request_id, staged.payload_path)

    assert claimed.payload_path.read_bytes() == b"image"
    assert not staged.bundle_path.exists()

    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
    )
    operator = snapshot.config.operator_principal
    with pytest.raises(DrawingMachineError) as error:
        runtime._job_status(
            ProtocolRequest(
                PROTOCOL_VERSION,
                "job.status",
                "operator-denied",
                {"schema_version": 1, "job_id": "x"},
            ),
            RequestContext(PeerCredentials(1, operator.uid, operator.gid), clock.now()),
        )
    assert error.value.payload.code == "CLIENT_DATA_PERMISSION_DENIED"


def test_status_publishes_only_closed_export_roles_but_preserves_snapshot(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    job = _queued_job("job-export", tmp_path / "private/payload", clock.now())
    allowed = ArtifactRef("processed_image", "artifacts/a/processed.png", "a" * 64, 1, "image/png")
    private = ArtifactRef("provider_response", "artifacts/a/provider.json", "b" * 64, 1, "application/json")

    class Coordinator:
        def status(self, job_id: str) -> JobProjection:
            assert job_id == job.job_id
            return JobProjection(job, (allowed, private), False)

    class ClientData:
        def __init__(self) -> None:
            self.published: tuple[ArtifactRef, ...] | None = None

        def publish_artifacts(self, job_id: str, artifacts: tuple[ArtifactRef, ...]) -> tuple[object, ...]:
            assert job_id == job.job_id
            self.published = artifacts
            return ()

    client_data = ClientData()
    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
        client_data=cast(ServiceClientDataPort, client_data),
    )
    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]
    automation = snapshot.config.automation_principal
    response = runtime._job_status(
        ProtocolRequest(PROTOCOL_VERSION, "job.status", "request", {"schema_version": 1, "job_id": job.job_id}),
        RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now()),
    )

    assert client_data.published == (allowed,)
    assert response["artifacts"] == [allowed.to_json(), private.to_json()]


def test_status_maps_canonical_artifact_tamper_to_frozen_public_contract(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    canonical = tmp_path / "canonical.png"
    canonical.write_bytes(b"tampered")
    job = _queued_job("job-1", tmp_path / "private/payload", clock.now())
    artifact = ArtifactRef("processed_image", "artifacts/a/processed.png", "a" * 64, 1, "image/png")

    class Coordinator:
        def status(self, job_id: str) -> JobProjection:
            assert job_id == job.job_id
            return JobProjection(job, (artifact,), False)

    class ClientData:
        def publish_artifacts(self, job_id: str, artifacts: tuple[ArtifactRef, ...]) -> tuple[object, ...]:
            assert job_id == job.job_id
            assert artifacts == (artifact,)
            assert canonical.read_bytes() == b"tampered"
            raise DrawingMachineError(
                ErrorPayload(
                    "ARTIFACT_VALIDATION_FAILED",
                    ErrorCategory.VALIDATION,
                    "internal canonical artifact digest mismatch",
                    False,
                    {},
                    job_id=job_id,
                )
            )

    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
        client_data=cast(ServiceClientDataPort, ClientData()),
    )
    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]
    automation = snapshot.config.automation_principal
    request = ProtocolRequest(
        PROTOCOL_VERSION, "job.status", "status-request", {"schema_version": 1, "job_id": job.job_id}
    )

    with pytest.raises(DrawingMachineError) as public:
        runtime._job_status(
            request,
            RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now()),
        )

    assert public.value.payload.code == "ARTIFACT_PATH_INVALID"
    assert public.value.payload.details == {}
    assert isinstance(public.value.__cause__, DrawingMachineError)
    assert public.value.__cause__.payload.code == "ARTIFACT_VALIDATION_FAILED"
    assert canonical.read_bytes() == b"tampered"


def test_lost_response_exact_replay_returns_the_existing_job_and_finishes_cleanup(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    contents = b"image"
    source.write_bytes(contents)
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))
    service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)
    claimed_file = service.claim_image(request_id, staged.payload_path)
    claimed = repository.get_staging_admission(request_id)
    assert claimed is not None

    job = _queued_job("job-existing", claimed_file.payload_path, clock.now())
    event, audit = _initial_authority(job, request_id, clock.now())
    artifact = ArtifactRef(
        "input_image",
        "inputs/payload",
        claimed.source_sha256,
        claimed.payload_identity.size_bytes,
        "image/png",
    )
    with repository.transaction() as transaction:
        durable = transaction.create_job_with_staging(
            job,
            artifacts=(artifact,),
            event=event,
            audit=audit,
            request_id=request_id,
            expected_staging_revision=claimed.revision,
        )

    class Coordinator:
        def status(self, job_id: str) -> JobProjection:
            assert job_id == job.job_id
            return JobProjection(job, (artifact,), False)

    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
        client_data=service,
    )
    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]
    automation = snapshot.config.automation_principal
    response = runtime._workflow_run_inner(
        ProtocolRequest(
            PROTOCOL_VERSION,
            "workflow.run",
            request_id,
            {
                "schema_version": 1,
                "mode": "submit",
                "job_name": job.name,
                "input_path": str(staged.payload_path),
                "route_mode": "direct",
                "semantic_assessment": None,
                "prompt": None,
            },
        ),
        RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now()),
    )

    assert response["job_id"] == job.job_id
    assert response["state"] == JobState.QUEUED.value
    consumed = repository.get_staging_admission(request_id)
    assert consumed is not None and consumed.state is StagingAdmissionState.CONSUMED
    assert not claimed_file.payload_path.parent.exists()
    assert durable.job_id == job.job_id

    with pytest.raises(DrawingMachineError) as mismatch:
        runtime._workflow_run_inner(
            ProtocolRequest(
                PROTOCOL_VERSION,
                "workflow.run",
                request_id,
                {
                    "schema_version": 1,
                    "mode": "submit",
                    "job_name": job.name,
                    "input_path": str(staged.payload_path),
                    "route_mode": "image-edit",
                    "semantic_assessment": None,
                    "prompt": None,
                },
            ),
            RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now()),
        )
    assert mismatch.value.payload.code == "STAGING_REPLAY_MISMATCH"


def test_claimed_retry_reuses_stable_job_identity(valid_xdg_paths: XdgPaths, tmp_path: Path) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))

    class FailingCoordinator:
        def __init__(self) -> None:
            self.job_ids: list[str] = []

        def submit(self, command: CoordinatorCommand) -> JobProjection:
            self.job_ids.append(command.job_id)
            raise RuntimeError("forced pre-admission failure")

    coordinator = FailingCoordinator()
    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
        client_data=service,
    )
    runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]
    automation = snapshot.config.automation_principal
    request = ProtocolRequest(
        PROTOCOL_VERSION,
        "workflow.run",
        request_id,
        {
            "schema_version": 1,
            "mode": "submit",
            "job_name": "drawing",
            "input_path": str(staged.payload_path),
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
        },
    )
    context = RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now())

    with pytest.raises(RuntimeError, match="forced pre-admission failure"):
        runtime._workflow_run_inner(request, context)
    with pytest.raises(RuntimeError, match="forced pre-admission failure"):
        runtime._workflow_run_inner(request, context)
    changed = dict(request.arguments)
    changed["route_mode"] = "image-edit"
    with pytest.raises(DrawingMachineError) as mismatch:
        runtime._workflow_run_inner(
            ProtocolRequest(PROTOCOL_VERSION, "workflow.run", request_id, changed),
            context,
        )

    assert len(coordinator.job_ids) == 2
    assert mismatch.value.payload.code == "STAGING_REPLAY_MISMATCH"
    assert coordinator.job_ids[0] == coordinator.job_ids[1]
    assert _staging_job_id(request_id, ClientDataRole.GCODE) == _staging_job_id(request_id, ClientDataRole.GCODE)
    assert _staging_job_id(request_id, ClientDataRole.GCODE) != coordinator.job_ids[0]


def test_invalid_workflow_envelope_fails_before_bind_decode_or_claim(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))
    snapshot = _snapshot(tmp_path)
    runtime = ServiceRuntime(
        paths=valid_xdg_paths,
        config=load_config(valid_xdg_paths),
        repository=repository,
        clock=clock,
        access_snapshot=snapshot,
        client_data=service,
    )
    automation = snapshot.config.automation_principal

    with pytest.raises(DrawingMachineError):
        runtime._workflow_run_inner(
            ProtocolRequest(
                PROTOCOL_VERSION,
                "workflow.run",
                request_id,
                {
                    "schema_version": 1,
                    "mode": "submit",
                    "job_name": "drawing",
                    "input_path": str(staged.payload_path),
                    "route_mode": "invalid",
                    "semantic_assessment": None,
                    "prompt": None,
                },
            ),
            RequestContext(PeerCredentials(2, automation.uid, automation.gid), clock.now()),
        )

    assert repository.get_staging_admission(request_id) is None
    assert staged.bundle_path.exists()
    connection = repository._get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'staging.request-envelope.v1'"
    ).fetchone() == (0,)


@pytest.mark.parametrize("failed_statement", ["jobs", "job_artifacts", "job_events", "audit_events", "admission"])
def test_atomic_admission_failure_reuses_exact_canonical_import_on_retry(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    failed_statement: str,
) -> None:
    clock = _Clock()
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(valid_xdg_paths.state_dir / "state.db", clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))
    service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)
    claimed_file = service.claim_image(request_id, staged.payload_path)
    chain = OfflineJobChain(
        repository=repository,
        artifacts=FilesystemArtifactStore(valid_xdg_paths),
        workers=cast(WorkerSupervisor, None),  # unused before durable admission
        config=load_config(valid_xdg_paths),
        clock=clock,
    )
    submission = WorkflowSubmissionV1(1, "drawing", claimed_file.payload_path, RouteMode.DIRECT, None, None)
    requester = RequesterIdentity("LOCAL_PEER", 2, 3, 4)
    job_id = "stable-staging-job"
    connection = repository._get_connection()
    if failed_statement == "admission":
        connection.execute(
            "CREATE TRIGGER fail_once BEFORE UPDATE ON staging_admissions "
            "BEGIN SELECT RAISE(ABORT, 'forced admission failure'); END"
        )
    else:
        connection.execute(
            f"CREATE TRIGGER fail_once BEFORE INSERT ON {failed_statement} "
            "BEGIN SELECT RAISE(ABORT, 'forced admission failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced admission failure"):
        chain._coordinator_submit(
            submission,
            request_id=request_id,
            job_id=job_id,
            requester=requester,
            commit_allowed=lambda: True,
            committed=lambda _job: None,
        )
    claimed = repository.get_staging_admission(request_id)
    assert claimed is not None and claimed.state is StagingAdmissionState.CLAIMED
    assert claimed_file.payload_path.exists()
    assert repository.get_job(job_id) is None
    assert repository.list_artifacts(job_id) == ()
    assert repository.list_job_events(job_id) == ()
    assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
    canonical_files = [path for path in (valid_xdg_paths.jobs_dir / job_id).rglob("*") if path.is_file()]
    assert canonical_files == []
    connection.execute("DROP TRIGGER fail_once")

    job = chain._coordinator_submit(
        submission,
        request_id=request_id,
        job_id=job_id,
        requester=requester,
        commit_allowed=lambda: True,
        committed=lambda _job: None,
    )

    assert job is not None
    artifacts = repository.list_artifacts(job_id)
    assert len(artifacts) == 1
    assert len([path for path in (valid_xdg_paths.jobs_dir / job_id).rglob("*") if path.is_file()]) == 1
    durable = repository.get_staging_admission(request_id)
    assert durable is not None and durable.state is StagingAdmissionState.DURABLY_ADMITTED
    service.consume(request_id, expected_revision=durable.revision)
    assert not claimed_file.payload_path.parent.exists()


def test_restart_without_request_retry_revokes_exact_precommit_promotion(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    valid_xdg_paths.state_dir.mkdir(parents=True, exist_ok=True)
    database = valid_xdg_paths.state_dir / "state.db"
    repository = SQLiteRepository(database, clock=clock)
    repository.initialize()
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    source = tmp_path / "caller-private.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))
    service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)
    claimed_file = service.claim_image(request_id, staged.payload_path)
    claimed = repository.get_staging_admission(request_id)
    assert claimed is not None
    job_id = _staging_job_id(request_id, ClientDataRole.IMAGE)
    store = FilesystemArtifactStore(valid_xdg_paths)
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=cast(WorkerSupervisor, None),
        config=load_config(valid_xdg_paths),
        clock=clock,
    )
    artifacts, promoted = chain._import_or_recover_staged(
        job_id,
        request_id,
        claimed_file.payload_path,
        claimed,
        role="input_image",
        relative_path="inputs/payload",
        media_type="image/png",
        max_bytes=1024,
    )
    assert promoted is not None and promoted.root.exists() and len(artifacts) == 1
    repository.close()

    reopened = SQLiteRepository(database, clock=clock)
    restarted = OfflineJobChain(
        repository=reopened,
        artifacts=FilesystemArtifactStore(valid_xdg_paths),
        workers=cast(WorkerSupervisor, None),
        config=load_config(valid_xdg_paths),
        clock=clock,
    )
    restarted.initialize()

    assert not promoted.root.exists()
    recovered = reopened.get_staging_admission(request_id)
    assert recovered is not None and recovered.state is StagingAdmissionState.CLAIMED
    assert reopened.get_job(job_id) is None


def test_every_partial_frame_disconnect_stays_undecoded_and_full_fragmented_frame_dispatches(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        dispatcher = CommandDispatcher(lambda: (_ for _ in ()).throw(AssertionError("not used")))

        def record(request: ProtocolRequest, context: RequestContext) -> dict[str, object]:
            del context
            calls.append(request.request_id)
            return {"handled": True}

        dispatcher.register("test.record", record)
        server = UnixServiceServer(tmp_path / "partial.sock", dispatcher, _Clock())
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        frame = encode_request(ProtocolRequest(PROTOCOL_VERSION, "test.record", "req-fragmented", {}))
        try:
            for length in range(1, len(frame)):
                _reader, writer = await asyncio.open_unix_connection(tmp_path / "partial.sock")
                writer.write(frame[:length])
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            assert calls == []

            reader, writer = await asyncio.open_unix_connection(tmp_path / "partial.sock")
            for byte in frame:
                writer.write(bytes((byte,)))
                await writer.drain()
            response = decode_response(await reader.readuntil(b"\n"))
            assert response.ok is True
            writer.close()
            await writer.wait_closed()
            assert calls == ["req-fragmented"]
        finally:
            await server.close()
            await task

    asyncio.run(scenario())


def _queued_job(job_id: str, input_path: Path, timestamp: datetime) -> JobRecord:
    return JobRecord(
        1,
        job_id,
        JobKind.OFFLINE_WORKFLOW,
        "drawing",
        JobState.QUEUED,
        0,
        {
            "input_path": str(input_path),
            "route_mode": "direct",
            "semantic_assessment": None,
            "prompt": None,
        },
        ConfigSnapshot("machine", "provider", 1, 1, 1, "a" * 64, "b" * 64, "c" * 64),
        None,
        None,
        None,
        timestamp,
        timestamp,
    )


def _initial_authority(job: JobRecord, request_id: str, timestamp: datetime) -> tuple[JobEvent, AuditRecord]:
    requester = RequesterIdentity("LOCAL_PEER", 2, 3, 4)
    event = JobEvent(
        "event-existing",
        job.job_id,
        0,
        "job.queued",
        None,
        JobState.QUEUED,
        request_id,
        "LOCAL_PEER",
        {"requester": requester.to_json()},
        timestamp,
    )
    audit = AuditRecord(
        event.event_id,
        event.event_type,
        request_id,
        {
            "schema_version": 1,
            "requester": requester.to_json(),
            "job_id": job.job_id,
            "command_summary": {
                "name": "workflow.run",
                "event_type": event.event_type,
                "job_kind": job.kind.value,
            },
            "prior_state": None,
            "result_state": JobState.QUEUED.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "0.2.0",
            "protocol_version": 1,
        },
    )
    return event, audit
