import asyncio
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import drawingmachine.service.runtime as runtime_module
from drawingmachine import __version__
from drawingmachine.bootstrap import build_runtime
from drawingmachine.config import ConfigBundle, XdgPaths, load_config, parse_machine_execution_config
from drawingmachine.domain.jobs import ArtifactRef, JobKind, JobState
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import ClientDataRole, StagingAdmissionState
from drawingmachine.ports.repository import RepositoryHealth
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest
from drawingmachine.service import ServiceRuntime
from drawingmachine.service.models import PeerCredentials, RequestContext
from drawingmachine.service.openclaw_protocol import OpenClawProtocol


@dataclass(slots=True)
class FixedClock:
    value: datetime = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)
    calls: list[str] = field(default_factory=list)

    def now(self) -> datetime:
        self.calls.append("clock.now")
        return self.value


class RecordingRepository:
    def __init__(
        self,
        paths: XdgPaths,
        events: list[str],
        *,
        fail_started_audit: bool = False,
        fail_stopped_audit: bool = False,
    ) -> None:
        self.paths = paths
        self.events = events
        self.fail_started_audit = fail_started_audit
        self.fail_stopped_audit = fail_stopped_audit
        self.audits: list[tuple[str, str, str | None, JsonObject]] = []
        self.closed = False

    def initialize(self) -> int:
        for path in (self.paths.state_dir, self.paths.data_dir, self.paths.runtime_dir):
            assert path.is_dir()
        for path in (self.paths.state_dir, self.paths.runtime_dir):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        self.events.append("repository.initialize")
        return 1

    def health(self) -> RepositoryHealth:
        self.events.append("repository.health")
        return RepositoryHealth(schema_version=1, journal_mode="wal", foreign_keys_enabled=True)

    def append_audit(
        self,
        event_id: str,
        event_type: str,
        request_id: str | None,
        payload: JsonObject,
    ) -> None:
        self.events.append(f"repository.append_audit:{event_type}")
        self.audits.append((event_id, event_type, request_id, payload))
        if self.fail_started_audit and event_type == "service.started":
            raise RuntimeError("forced audit failure")
        if self.fail_stopped_audit and event_type == "service.stopped":
            raise RuntimeError("forced stopped audit failure")

    def close(self) -> None:
        self.events.append("repository.close")
        self.closed = True


def _runtime(
    paths: XdgPaths,
    config: ConfigBundle,
    repository: RecordingRepository,
    clock: FixedClock,
) -> ServiceRuntime:
    return ServiceRuntime(paths=paths, config=config, repository=repository, clock=clock)


def test_status_contains_epoch_and_database_version(valid_xdg_paths: XdgPaths) -> None:
    runtime = build_runtime(valid_xdg_paths)
    runtime.start()
    try:
        status = runtime.status()
        assert status.version == __version__
        assert status.protocol_version == PROTOCOL_VERSION
        assert status.database_schema_version == 5
        assert UUID(status.service_epoch).version == 4
        assert status.started_at.tzinfo is not None
        assert status.pid == os.getpid()
    finally:
        runtime.stop()


def test_bootstrap_builds_server_for_started_runtime(valid_xdg_paths: XdgPaths) -> None:
    from drawingmachine.bootstrap import build_server
    from drawingmachine.service.server import UnixServiceServer

    runtime = build_runtime(valid_xdg_paths)
    runtime.start()
    try:
        server = build_server(valid_xdg_paths, runtime)
        assert isinstance(server, UnixServiceServer)
    finally:
        runtime.stop()


def test_server_surfaces_pre_ready_and_periodic_maintenance_failures(tmp_path: Path) -> None:
    from drawingmachine.service.server import UnixServiceServer

    async def exercise_pre_ready() -> None:
        error = RuntimeError("startup reconciliation blocked")
        server = UnixServiceServer(
            tmp_path / "pre-ready.sock",
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            pre_ready_maintenance=lambda: (_ for _ in ()).throw(error),
        )
        task = asyncio.create_task(server.serve())
        with pytest.raises(RuntimeError, match="startup reconciliation blocked"):
            await server.wait_started()
        with pytest.raises(RuntimeError, match="startup reconciliation blocked"):
            await task

    async def exercise_periodic() -> None:
        error = RuntimeError("periodic reconciliation failed")
        server = UnixServiceServer(
            tmp_path / "periodic.sock",
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            periodic_maintenance=lambda: (_ for _ in ()).throw(error),
            maintenance_interval_s=0.001,
        )
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        with pytest.raises(RuntimeError, match="periodic reconciliation failed"):
            await task

    asyncio.run(exercise_pre_ready())
    asyncio.run(exercise_periodic())


def test_invalid_config_fails_before_runtime_directories_are_created(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text("schema_version = 2\n", encoding="utf-8")

    with pytest.raises(DrawingMachineError) as error:
        build_runtime(valid_xdg_paths)

    assert error.value.payload.code == "CONFIG_INVALID"
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()


def test_start_orders_directories_database_epoch_and_started_audit(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    with pytest.raises(DrawingMachineError, match="not started"):
        _ = runtime.dispatcher

    runtime.start()

    assert events == [
        "repository.initialize",
        "clock.now",
        "repository.append_audit:service.started",
    ]
    started = repository.audits[0]
    assert UUID(started[0]).version == 4
    assert started[1] == "service.started"
    assert started[2] is None
    assert started[3] == {
        "service_epoch": runtime.status().service_epoch,
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "pid": os.getpid(),
    }
    assert runtime.dispatcher is not None

    runtime.stop()
    assert events[-2:] == ["repository.append_audit:service.stopped", "repository.close"]
    assert repository.closed is True
    with pytest.raises(DrawingMachineError, match="not started"):
        runtime.status()


def test_start_rejects_an_already_started_runtime(valid_xdg_paths: XdgPaths) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)
    runtime.start()
    try:
        with pytest.raises(DrawingMachineError) as error:
            runtime.start()
        assert error.value.payload.code == "SERVICE_ALREADY_STARTED"
    finally:
        runtime.stop()


def test_start_failure_never_exposes_dispatcher_and_closes_repository(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events, fail_started_audit=True)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    with pytest.raises(RuntimeError, match="forced audit failure"):
        runtime.start()

    assert repository.closed is True
    with pytest.raises(DrawingMachineError, match="not started"):
        _ = runtime.dispatcher


def test_coordinator_starts_after_persistence_and_stops_before_repository(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    class Coordinator:
        def start(self) -> None:
            events.append("coordinator.start")

        def stop(self) -> None:
            events.append("coordinator.stop")

    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]
    runtime.start()
    runtime.stop()

    assert events.index("repository.initialize") < events.index("coordinator.start")
    assert events.index("coordinator.start") < events.index("repository.append_audit:service.started")
    assert events.index("coordinator.stop") < events.index("repository.close")


def test_candidate_epoch_starts_machine_reconciliation_before_job_coordinator_and_handlers(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    class JobCoordinator:
        def start(self) -> None:
            events.append("job.start")

        def stop(self) -> None:
            events.append("job.stop")

        def publish_external(self, job_id: str) -> None:
            events.append(f"job.publish:{job_id}")

    class MachineCoordinator:
        def __init__(self, epoch: str) -> None:
            assert UUID(epoch).version == 4
            self.epoch = epoch

        def start(self) -> None:
            events.append(f"machine.start:{self.epoch}")

        def stop(self) -> None:
            events.append("machine.stop")

    job = JobCoordinator()
    runtime.install_job_coordinator(job)  # type: ignore[arg-type]
    runtime.install_machine_coordinator_factory(
        lambda epoch, publisher: (
            events.append(f"machine.factory:{getattr(publisher, '__self__', None) is job}") or MachineCoordinator(epoch)
        )
    )

    runtime.start()
    epoch = runtime.status().service_epoch
    runtime.stop()

    assert events.index("repository.initialize") < events.index("machine.factory:True")
    assert events.index(f"machine.start:{epoch}") < events.index("job.start")
    assert events.index("job.start") < events.index("repository.append_audit:service.started")
    assert events.index("machine.stop") < events.index("job.stop")
    assert events.index("job.stop") < events.index("repository.close")


def test_machine_start_failure_never_starts_job_and_closes_repository(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    class JobCoordinator:
        def start(self) -> None:
            events.append("job.start")

        def stop(self) -> None:
            events.append("job.stop")

        def publish_external(self, job_id: str) -> None:
            del job_id

    class MachineCoordinator:
        def start(self) -> None:
            events.append("machine.start")
            raise RuntimeError("machine reconciliation failed")

        def stop(self) -> None:
            events.append("machine.stop")

    runtime.install_job_coordinator(JobCoordinator())  # type: ignore[arg-type]
    runtime.install_machine_coordinator_factory(lambda _epoch, _publisher: MachineCoordinator())

    with pytest.raises(RuntimeError, match="machine reconciliation failed"):
        runtime.start()

    assert "job.start" not in events
    assert events[-2:] == ["machine.stop", "repository.close"]


def test_service_restart_generates_a_fresh_candidate_machine_epoch(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)
    epochs: list[str] = []

    class MachineCoordinator:
        def start(self) -> None:
            return

        def stop(self) -> None:
            return

    def factory(epoch: str, publisher: object) -> MachineCoordinator:
        del publisher
        epochs.append(epoch)
        return MachineCoordinator()

    runtime.install_machine_coordinator_factory(factory)
    runtime.start()
    runtime.stop()
    runtime.start()
    runtime.stop()

    assert len(epochs) == 2
    assert epochs[0] != epochs[1]


def test_machine_protocol_registration_tracks_the_fresh_coordinator_across_restart(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    config = load_config(valid_xdg_paths)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, config, repository, clock)

    class MachineCoordinator:
        def __init__(self, epoch: str) -> None:
            self.epoch = epoch

        @property
        def service_epoch(self) -> str:
            return self.epoch

        def start(self) -> None:
            return

        def stop(self) -> None:
            return

        def status(self, execution_id: str | None = None) -> JsonObject:
            assert execution_id is None
            return {
                "schema_version": 1,
                "service_epoch": self.epoch,
                "accepting": True,
                "execution": None,
                "progress": None,
                "last_outcome": None,
            }

        def submit(self, command: object) -> object:
            raise AssertionError(f"unexpected command: {command}")

    runtime.install_machine_coordinator_factory(
        lambda epoch, _publisher: MachineCoordinator(epoch)  # type: ignore[return-value]
    )
    machine_config = parse_machine_execution_config(config.machine, profile_path=config.machine_path)
    peer = machine_config.automation_principal
    context = RequestContext(PeerCredentials(1234, peer.uid, peer.gid), clock.value)
    epochs: list[str] = []
    for _ in range(2):
        runtime.start()
        response = runtime.dispatcher.dispatch(
            ProtocolRequest(
                PROTOCOL_VERSION,
                "machine.status",
                "runtime-status",
                {"schema_version": 1, "execution_id": None},
            ),
            context,
        )
        assert response.ok is True
        epochs.append(response.data["service_epoch"])  # type: ignore[arg-type]
        runtime.stop()

    assert len(set(epochs)) == 2


def test_coordinator_start_failure_closes_repository_and_leaves_runtime_stopped(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    class Coordinator:
        def start(self) -> None:
            events.append("coordinator.start")
            raise RuntimeError("coordinator startup failed")

        def stop(self) -> None:
            events.append("coordinator.stop")

    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="coordinator startup failed"):
        runtime.start()

    assert events[-2:] == ["coordinator.stop", "repository.close"]
    with pytest.raises(DrawingMachineError, match="not started"):
        _ = runtime.dispatcher


def test_started_audit_failure_stops_coordinator_before_closing_repository(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events, fail_started_audit=True)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    class Coordinator:
        def start(self) -> None:
            events.append("coordinator.start")

        def stop(self) -> None:
            events.append("coordinator.stop")

    runtime.install_job_coordinator(Coordinator())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="forced audit failure"):
        runtime.start()

    assert events[-2:] == ["coordinator.stop", "repository.close"]
    with pytest.raises(DrawingMachineError, match="not started"):
        _ = runtime.dispatcher


def test_throwing_stopped_audit_closes_repository_and_clears_started_state(
    valid_xdg_paths: XdgPaths,
) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events, fail_stopped_audit=True)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)
    runtime.start()

    with pytest.raises(RuntimeError, match="forced stopped audit failure"):
        runtime.stop()

    assert repository.closed is True
    assert runtime._service_epoch is None
    assert runtime._started_at is None
    assert runtime._pid is None
    with pytest.raises(DrawingMachineError, match="not started"):
        _ = runtime.dispatcher
    runtime.stop()
    assert events.count("repository.append_audit:service.stopped") == 1


@pytest.mark.parametrize("directory_name", ["state_dir", "runtime_dir"])
def test_start_tightens_preexisting_runtime_directory_permissions(
    valid_xdg_paths: XdgPaths,
    directory_name: str,
) -> None:
    path = getattr(valid_xdg_paths, directory_name)
    path.mkdir(parents=True)
    path.chmod(0o777)
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    runtime.start()
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    finally:
        runtime.stop()


def test_start_leaves_preexisting_data_dir_mode_unchanged(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.data_dir.mkdir(parents=True)
    valid_xdg_paths.data_dir.chmod(0o750)
    valid_xdg_paths.state_dir.mkdir(parents=True)
    valid_xdg_paths.state_dir.chmod(0o777)
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    runtime.start()
    try:
        assert stat.S_IMODE(valid_xdg_paths.data_dir.stat().st_mode) == 0o750
        assert stat.S_IMODE(valid_xdg_paths.state_dir.stat().st_mode) == 0o700
    finally:
        runtime.stop()


def test_start_creates_missing_data_dir_as_private(valid_xdg_paths: XdgPaths) -> None:
    events: list[str] = []
    clock = FixedClock(calls=events)
    repository = RecordingRepository(valid_xdg_paths, events)
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, clock)

    assert not valid_xdg_paths.data_dir.exists()
    runtime.start()
    try:
        assert stat.S_IMODE(valid_xdg_paths.data_dir.stat().st_mode) == 0o700
    finally:
        runtime.stop()


def test_stop_persists_audit_and_closes_sqlite(valid_xdg_paths: XdgPaths) -> None:
    runtime = build_runtime(valid_xdg_paths)
    runtime.start()
    epoch = runtime.status().service_epoch
    runtime.stop()

    with sqlite3.connect(valid_xdg_paths.database_file) as connection:
        rows = connection.execute(
            "SELECT event_type, request_id, payload_json FROM audit_events ORDER BY rowid"
        ).fetchall()

    assert [row[0] for row in rows] == ["service.started", "service.stopped"]
    assert all(row[1] is None for row in rows)
    assert all(epoch in str(row[2]) for row in rows)

    valid_xdg_paths.database_file.rename(valid_xdg_paths.database_file.with_suffix(".closed"))


def test_runtime_constructor_and_coordinator_installation_are_closed(
    valid_xdg_paths: XdgPaths,
) -> None:
    config = load_config(valid_xdg_paths)
    repository = RecordingRepository(valid_xdg_paths, [])
    clock = FixedClock()

    with pytest.raises(TypeError, match="installed together"):
        ServiceRuntime(
            paths=valid_xdg_paths,
            config=config,
            repository=repository,  # type: ignore[arg-type]
            clock=clock,
            staging_reconciler=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="exact OpenClawProtocol"):
        ServiceRuntime(
            paths=valid_xdg_paths,
            config=config,
            repository=repository,  # type: ignore[arg-type]
            clock=clock,
            openclaw_protocol=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="requires a service access snapshot"):
        ServiceRuntime(
            paths=valid_xdg_paths,
            config=config,
            repository=repository,  # type: ignore[arg-type]
            clock=clock,
            openclaw_protocol=object.__new__(OpenClawProtocol),
        )

    runtime = _runtime(valid_xdg_paths, config, repository, clock)
    with pytest.raises(RuntimeError, match="provider registry"):
        _ = runtime.provider_registry

    coordinator = SimpleNamespace(start=lambda: None, stop=lambda: None, publish_external=lambda _job_id: None)
    runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="already installed"):
        runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]

    def factory(_epoch: str, _publisher: object) -> SimpleNamespace:
        return coordinator

    runtime.install_machine_coordinator_factory(factory)
    with pytest.raises(DrawingMachineError, match="already installed"):
        runtime.install_machine_coordinator_factory(factory)

    runtime.start()
    try:
        with pytest.raises(DrawingMachineError, match="after runtime start"):
            runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]
        with pytest.raises(DrawingMachineError, match="after runtime start"):
            runtime.install_machine_coordinator_factory(factory)
    finally:
        runtime.stop()


def test_job_handlers_are_registered_once_across_restart(valid_xdg_paths: XdgPaths) -> None:
    events: list[str] = []
    runtime = _runtime(
        valid_xdg_paths,
        load_config(valid_xdg_paths),
        RecordingRepository(valid_xdg_paths, events),
        FixedClock(),
    )
    coordinator = SimpleNamespace(start=lambda: None, stop=lambda: None)
    runtime.install_job_coordinator(coordinator)  # type: ignore[arg-type]

    runtime.start()
    runtime.stop()
    runtime.start()
    runtime.stop()

    assert runtime._job_handlers_registered is True


def _replay_fixture(
    runtime: ServiceRuntime,
    repository: RecordingRepository,
    *,
    role: ClientDataRole = ClientDataRole.GCODE,
) -> tuple[SimpleNamespace, SimpleNamespace, ArtifactRef, SimpleNamespace]:
    request_id = "12345678-1234-4234-8234-123456789abc"
    bundle_name = f"stage-{role.value}-{request_id}"
    path = Path(f"/private/{role.value}/admitted/{bundle_name}/payload")
    kind = JobKind.OFFLINE_WORKFLOW if role is ClientDataRole.IMAGE else JobKind.GCODE_CHECK
    artifact_role = "input_image" if role is ClientDataRole.IMAGE else "gcode"
    job = SimpleNamespace(
        job_id="job-1",
        kind=kind,
        name="replay",
        state=JobState.QUEUED,
        revision=1,
        to_json=lambda: {"request": {"input_path": str(path)}},
    )
    artifact = ArtifactRef(artifact_role, "artifacts/attempt/input", "a" * 64, 7, "application/octet-stream")
    admission = SimpleNamespace(
        request_id=request_id,
        role=role,
        bundle_name=bundle_name,
        payload_identity=SimpleNamespace(size_bytes=7),
        source_sha256="a" * 64,
        state=StagingAdmissionState.CONSUMED,
        job_id="job-1",
        revision=2,
    )
    projection = SimpleNamespace(job=job, artifacts=(artifact,), cancellation_requested=False)
    repository.get_job = lambda _job_id: job  # type: ignore[attr-defined]
    repository.list_artifacts = lambda _job_id: (artifact,)  # type: ignore[attr-defined]
    runtime._job_coordinator = SimpleNamespace(status=lambda _job_id: projection)  # type: ignore[assignment]
    return admission, job, artifact, projection


def test_exact_staging_replay_rejects_every_durable_binding_mismatch(
    valid_xdg_paths: XdgPaths,
) -> None:
    repository = RecordingRepository(valid_xdg_paths, [])
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, FixedClock())
    admission, job, artifact, _projection = _replay_fixture(runtime, repository)

    def rejects() -> None:
        with pytest.raises(DrawingMachineError) as caught:
            runtime._exact_staging_replay(admission, ClientDataRole.GCODE)
        assert caught.value.payload.code == "STAGING_REPLAY_MISMATCH"

    admission.job_id = None
    rejects()
    admission.job_id = "job-1"
    repository.get_job = lambda _job_id: None  # type: ignore[attr-defined]
    rejects()
    repository.get_job = lambda _job_id: job  # type: ignore[attr-defined]

    job.to_json = lambda: {"request": []}
    rejects()
    job.to_json = lambda: {"request": {}}
    rejects()
    job.to_json = lambda: {"request": {"input_path": "/wrong/payload"}}
    rejects()
    job.to_json = lambda: {
        "request": {
            "input_path": f"/private/gcode/admitted/{admission.bundle_name}/payload",
            "route_mode": "direct",
        }
    }
    with pytest.raises(DrawingMachineError, match="different arguments"):
        runtime._exact_staging_replay(
            admission,
            ClientDataRole.GCODE,
            request_arguments={"route_mode": "auto"},
        )

    repository.list_artifacts = lambda _job_id: ()  # type: ignore[attr-defined]
    rejects()
    repository.list_artifacts = lambda _job_id: (artifact,)  # type: ignore[attr-defined]

    with pytest.raises(DrawingMachineError, match="machine coordinator"):
        runtime._require_machine_coordinator()


def test_consumed_staging_replays_without_reconsumption_and_invalid_new_admissions_fail(
    valid_xdg_paths: XdgPaths,
) -> None:
    repository = RecordingRepository(valid_xdg_paths, [])
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, FixedClock())
    context = RequestContext(PeerCredentials(10, 20, 30), FixedClock().value)

    for role in (ClientDataRole.GCODE, ClientDataRole.IMAGE):
        admission, _job, _artifact, projection = _replay_fixture(runtime, repository, role=role)
        consumed: list[int] = []
        client = SimpleNamespace(
            bind_request_envelope=lambda *_args: None,
            register_decoded=lambda *_args, admission=admission: admission,
            consume=lambda _request_id, *, expected_revision, consumed=consumed: consumed.append(expected_revision),
        )
        runtime._client_data = client  # type: ignore[assignment]
        if role is ClientDataRole.GCODE:
            request = ProtocolRequest(
                PROTOCOL_VERSION,
                "gcode.check",
                admission.request_id,
                {"schema_version": 1, "path": "/private/input.gcode"},
            )
            assert runtime._gcode_check_inner(request, context)["job_id"] == projection.job.job_id
        else:
            request = ProtocolRequest(
                PROTOCOL_VERSION,
                "workflow.run",
                admission.request_id,
                {
                    "schema_version": 1,
                    "mode": "submit",
                    "job_name": "replay",
                    "input_path": "/private/input.png",
                    "route_mode": "direct",
                    "semantic_assessment": None,
                    "prompt": None,
                },
            )
            job_request = projection.job.to_json()["request"]
            job_request.update({"route_mode": "direct", "semantic_assessment": None, "prompt": None})
            projection.job.to_json = lambda request=job_request: {"request": request}
            assert runtime._workflow_run_inner(request, context)["job_id"] == projection.job.job_id
        assert consumed == []

    admission, _job, _artifact, _projection = _replay_fixture(runtime, repository)
    admission.state = StagingAdmissionState.DURABLY_ADMITTED
    consumed = []
    runtime._client_data = SimpleNamespace(
        bind_request_envelope=lambda *_args: None,
        register_decoded=lambda *_args: admission,
        consume=lambda _request_id, *, expected_revision: consumed.append(expected_revision),
    )  # type: ignore[assignment]
    runtime._gcode_check_inner(
        ProtocolRequest(
            PROTOCOL_VERSION,
            "gcode.check",
            admission.request_id,
            {"schema_version": 1, "path": "/private/input.gcode"},
        ),
        context,
    )
    assert consumed == [admission.revision]

    projection = _replay_fixture(runtime, repository)[3]
    runtime._job_coordinator = SimpleNamespace(submit=lambda _command: projection)  # type: ignore[assignment]
    repository.get_staging_admission = lambda _request_id: None  # type: ignore[attr-defined]
    runtime._client_data = SimpleNamespace(
        bind_request_envelope=lambda *_args: None,
        register_decoded=lambda *_args: SimpleNamespace(state=StagingAdmissionState.REQUEST_DECODED),
        claim_gcode=lambda _request_id, path: SimpleNamespace(payload_path=path),
        claim_image=lambda _request_id, path: SimpleNamespace(payload_path=path),
    )  # type: ignore[assignment]
    for request_id, command, arguments, message in (
        (
            "22345678-1234-4234-8234-123456789abc",
            "gcode.check",
            {"schema_version": 1, "path": "/private/input.gcode"},
            "G-code",
        ),
        (
            "32345678-1234-4234-8234-123456789abc",
            "workflow.run",
            {
                "schema_version": 1,
                "mode": "submit",
                "job_name": "new",
                "input_path": "/private/input.png",
                "route_mode": "direct",
                "semantic_assessment": None,
                "prompt": None,
            },
            "job",
        ),
    ):
        request = ProtocolRequest(PROTOCOL_VERSION, command, request_id, arguments)
        with pytest.raises(DrawingMachineError, match=message):
            if command == "gcode.check":
                runtime._gcode_check_inner(request, context)
            else:
                runtime._workflow_run_inner(request, context)


def test_job_status_preserves_nonvalidation_publish_errors_and_redaction_is_shape_safe(
    valid_xdg_paths: XdgPaths,
) -> None:
    repository = RecordingRepository(valid_xdg_paths, [])
    runtime = _runtime(valid_xdg_paths, load_config(valid_xdg_paths), repository, FixedClock())
    projection = _replay_fixture(runtime, repository)[3]
    runtime._client_data = SimpleNamespace(
        publish_artifacts=lambda *_args: (_ for _ in ()).throw(
            DrawingMachineError(
                runtime_module.ErrorPayload("OTHER", runtime_module.ErrorCategory.SERVICE, "boom", False, {})
            )
        )
    )  # type: ignore[assignment]
    request = ProtocolRequest(
        PROTOCOL_VERSION,
        "job.status",
        "status-request",
        {"schema_version": 1, "job_id": "job-1"},
    )
    with pytest.raises(DrawingMachineError, match="boom"):
        runtime._job_status(request, RequestContext(PeerCredentials(10, 20, 30), FixedClock().value))

    projection.job.to_json = lambda: {"request": {"route_mode": "direct"}}
    assert runtime_module._status_data(projection, redact_client_path=True)["job"]["request"] == {
        "route_mode": "direct"
    }
