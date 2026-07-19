import os
from collections.abc import Callable
from pathlib import Path

from drawingmachine import __version__
from drawingmachine.adapters.filesystem import FilesystemArtifactStore, OpenClawFilesystemAdapter, _secure_fs
from drawingmachine.adapters.filesystem.client_data import (
    FilesystemClientInputStaging,
    FilesystemServiceClientData,
    NativeClientDataPermissionOracle,
    SystemStagingClock,
)
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.adapters.providers import LocalComfyUIConfig, LocalComfyUIProvider
from drawingmachine.adapters.providers.local_comfyui import HttpTransport
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.adapters.workers import SpawnProcessSupervisor
from drawingmachine.application.machine import MachineExecutionChain, MachineRuntimeAuthority
from drawingmachine.application.offline_job_chain import OfflineJobChain
from drawingmachine.application.openclaw import PackagedOpenClawDeployment
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.config import (
    ConfigBundle,
    MachineExecutionConfig,
    ProfileEnvelope,
    XdgPaths,
    load_config,
    load_openclaw_deployment_policy,
    load_service_access_config,
    parse_machine_execution_config,
)
from drawingmachine.config.openclaw_deployment import (
    OpenClawDeploymentPolicyV1,
    PosixPrincipalV1,
    ServiceWriterSnapshotV1,
)
from drawingmachine.domain.gcode import parse_machine_build_profile
from drawingmachine.domain.planning import parse_path_plan_config
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.logging_config import configure_logging
from drawingmachine.ports.artifacts import ArtifactReader
from drawingmachine.ports.client_data import (
    ClientInputStagingPort,
    StagingAdmissionRecordV1,
    StagingClock,
    StagingRecoveryEvidenceV1,
)
from drawingmachine.ports.fluidnc import FluidNCSession, FluidNCSessionFactory, OpenSessionRequest
from drawingmachine.ports.providers import ProviderRegistry
from drawingmachine.service import ServiceRuntime
from drawingmachine.service._instance import InstanceLease
from drawingmachine.service.access_policy import (
    AccessFactsInspector,
    LinuxAccessFactsInspector,
    RuntimeAccessPolicy,
    ServiceAccessSnapshotV1,
    build_service_access_snapshot,
)
from drawingmachine.service.job_coordinator import JobCoordinator
from drawingmachine.service.machine_coordinator import MachineCoordinator
from drawingmachine.service.openclaw_protocol import OpenClawProtocol
from drawingmachine.service.server import UnixServiceServer


class _LazySerialFluidNCFactory:
    """Keep pyserial outside service startup and restart reconciliation."""

    __slots__ = ("_config", "_delegate")

    def __init__(self, config: MachineExecutionConfig) -> None:
        self._config = config
        self._delegate: FluidNCSessionFactory | None = None

    def open_session(self, request: OpenSessionRequest) -> FluidNCSession:
        delegate = self._delegate
        if delegate is None:
            from drawingmachine.adapters.hardware.serial_fluidnc import SerialFluidNCFactory

            delegate = SerialFluidNCFactory(self._config)
            self._delegate = delegate
        return delegate.open_session(request)


def build_artifact_reader(paths: XdgPaths) -> ArtifactReader:
    return FilesystemArtifactStore(paths)


def build_client_input_staging(paths: XdgPaths) -> ClientInputStagingPort:
    from drawingmachine.cli.service_access import load_client_access_snapshot
    from drawingmachine.config.service_access import validate_automation_data_endpoints

    snapshot = load_client_access_snapshot(paths)
    access = snapshot.config
    if snapshot.client_principal != access.automation_principal:
        raise DrawingMachineError(
            ErrorPayload(
                "CLIENT_DATA_PERMISSION_DENIED",
                ErrorCategory.PERMISSION,
                "operator principal has no client data authority",
                False,
                {},
            )
        )

    data_inspector = LinuxAccessFactsInspector()
    expected_endpoints = validate_automation_data_endpoints(access, inspector=data_inspector)

    def validate() -> None:
        if validate_automation_data_endpoints(access, inspector=data_inspector) != expected_endpoints:
            raise DrawingMachineError(
                ErrorPayload(
                    "CLIENT_DATA_IDENTITY_DRIFT",
                    ErrorCategory.PERMISSION,
                    "automation data endpoint or canonical root identity changed",
                    False,
                    {},
                )
            )

    validate()
    return FilesystemClientInputStaging(
        access.automation_import_endpoint,
        access.automation_export_endpoint,
        permission_oracle=NativeClientDataPermissionOracle(
            service_uid=access.service_principal.uid,
            automation_uid=access.automation_principal.uid,
            service_gid=access.connect_group.gid,
        ),
        published_import_root=access.automation_import_root,
        published_export_root=access.automation_export_root,
        endpoint_validator=validate,
    )


def build_staging_clock() -> StagingClock:
    return SystemStagingClock()


def build_provider_registry(
    config: ConfigBundle,
    *,
    transport: HttpTransport | None = None,
) -> ProviderRegistry:
    parse_path_plan_config(config.machine)
    parse_machine_build_profile(config.machine)
    parse_machine_execution_config(config.machine, profile_path=config.machine_path)
    return _build_provider_registry(config, transport=transport)


def _build_provider_registry(
    config: ConfigBundle,
    *,
    transport: HttpTransport | None,
) -> ProviderRegistry:
    provider_config = LocalComfyUIConfig.from_profile(config.provider, profile_path=config.provider_path)
    provider = LocalComfyUIProvider(provider_config, transport=transport)
    provider.validate_config()
    registry = ProviderRegistry()
    registry.register(provider)
    return registry


def _package_b_config_view(config: ConfigBundle) -> ConfigBundle:
    """Keep Package B worker payloads frozen while Package C extends the source profile."""
    machine_profile: JsonObject = {field: config.machine.profile[field] for field in ("name", "planning", "gcode")}
    return ConfigBundle(
        application=config.application,
        machine=ProfileEnvelope(config.machine.schema_version, machine_profile),
        provider=config.provider,
        machine_path=config.machine_path,
        provider_path=config.provider_path,
        digests=config.digests,
    )


def build_runtime(
    paths: XdgPaths,
    *,
    provider_transport: HttpTransport | None = None,
    fluidnc_factory: FluidNCSessionFactory | None = None,
    machine_identity_factory: Callable[[], str] | None = None,
    require_service_access: bool = False,
    access_inspector: AccessFactsInspector | None = None,
) -> ServiceRuntime:
    config = load_config(paths)
    parse_path_plan_config(config.machine)
    parse_machine_build_profile(config.machine)
    machine_config = parse_machine_execution_config(config.machine, profile_path=config.machine_path)
    access_snapshot = (
        _build_service_access_snapshot(paths, config, machine_config, inspector=access_inspector)
        if require_service_access
        else None
    )
    openclaw_protocol = None if access_snapshot is None else _build_openclaw_protocol(access_snapshot)
    provider_registry = _build_provider_registry(config, transport=provider_transport)
    configure_logging(config.application.log_level)
    clock = SystemClock()
    repository = SQLiteRepository(paths.database_file, clock=clock)
    client_data = None
    staging_reconciler = None
    if access_snapshot is not None:
        from drawingmachine.config.service_access import (
            ValidatedAutomationDataEndpointsV1,
            validate_automation_data_endpoints,
        )

        access = access_snapshot.config
        staging_reconciler = StagingReconciler(access.automation_import_root, repository, _secure_fs)
        data_inspector = LinuxAccessFactsInspector() if access_inspector is None else access_inspector
        expected_client_data_endpoints: ValidatedAutomationDataEndpointsV1 = validate_automation_data_endpoints(
            access,
            inspector=data_inspector,
        )

        def validate_client_data_endpoints() -> None:
            current = validate_automation_data_endpoints(access, inspector=data_inspector)
            if current != expected_client_data_endpoints:
                raise DrawingMachineError(
                    ErrorPayload(
                        "CLIENT_DATA_IDENTITY_DRIFT",
                        ErrorCategory.PERMISSION,
                        "automation data endpoint or canonical root identity changed",
                        False,
                        {},
                    )
                )

        def reconcile_client_data(
            trigger: str,
            staging_clock: StagingClock,
            active_requests: frozenset[str],
            durable_admissions: tuple[StagingAdmissionRecordV1, ...],
        ) -> tuple[StagingRecoveryEvidenceV1, ...]:
            return staging_reconciler.reconcile(
                trigger,
                staging_clock,
                active_requests,
                durable_admissions,
            ).evidence

        client_data = FilesystemServiceClientData(
            access.automation_import_root,
            access.automation_export_root,
            repository=repository,
            artifact_reader=FilesystemArtifactStore(paths),
            permission_oracle=NativeClientDataPermissionOracle(
                service_uid=access.service_principal.uid,
                automation_uid=access.automation_principal.uid,
                service_gid=access.connect_group.gid,
            ),
            utc_now=clock.now,
            endpoint_validator=validate_client_data_endpoints,
            reconcile_callback=reconcile_client_data,
        )
    runtime = ServiceRuntime(
        paths=paths,
        config=config,
        repository=repository,
        clock=clock,
        provider_registry=provider_registry,
        access_snapshot=access_snapshot,
        openclaw_protocol=openclaw_protocol,
        client_data=client_data,
        staging_reconciler=staging_reconciler,
        staging_clock=None if client_data is None else SystemStagingClock(),
    )
    package_b_config = _package_b_config_view(config)

    def build_chain() -> OfflineJobChain:
        coordinator_clock = SystemClock()
        return OfflineJobChain(
            repository=SQLiteRepository(paths.database_file, clock=coordinator_clock),
            artifacts=FilesystemArtifactStore(paths),
            workers=SpawnProcessSupervisor(
                entrypoint_module="drawingmachine.adapters.workers.worker_entrypoint",
                entrypoint_name="execute_task",
            ),
            config=package_b_config,
            clock=coordinator_clock,
        )

    job_coordinator = JobCoordinator(chain_factory=build_chain)
    runtime.install_job_coordinator(job_coordinator)

    def build_machine_coordinator(
        service_epoch: str,
        job_publisher: object,
    ) -> MachineCoordinator:
        publisher = job_publisher if callable(job_publisher) else None

        def build_machine_chain(chain_epoch: str) -> MachineExecutionChain:
            if chain_epoch != service_epoch:
                raise RuntimeError("machine coordinator service epoch changed during assembly")

            authority = MachineRuntimeAuthority(
                service_epoch=service_epoch,
                application_version=__version__,
                application_schema_version=config.application.schema_version,
                application_digest=config.digests["application"],
                machine_profile=config.application.machine_profile,
                machine_schema_version=config.machine.schema_version,
                machine_digest=config.digests["machine"],
                provider_profile=config.application.provider_profile,
                provider_schema_version=config.provider.schema_version,
                provider_digest=config.digests["provider"],
            )
            return MachineExecutionChain(
                SQLiteMachineRepository(paths.database_file),
                FilesystemArtifactStore(paths),
                _LazySerialFluidNCFactory(machine_config) if fluidnc_factory is None else fluidnc_factory,
                machine_config,
                authority,
                identity_factory=machine_identity_factory,
            )

        return MachineCoordinator(
            chain_factory=build_machine_chain,
            service_epoch=service_epoch,
            job_publisher=publisher,
        )

    runtime.install_machine_coordinator_factory(build_machine_coordinator)
    return runtime


def build_instance_lease(paths: XdgPaths, access_policy: RuntimeAccessPolicy | None = None) -> InstanceLease:
    lease = InstanceLease(paths.socket_file, access_policy)
    lease.acquire()
    return lease


def build_server(
    paths: XdgPaths,
    runtime: ServiceRuntime,
    *,
    instance_lease: InstanceLease | None = None,
) -> UnixServiceServer:
    return UnixServiceServer(
        paths.socket_file,
        runtime.dispatcher,
        SystemClock(),
        instance_lease=instance_lease,
        access_policy=runtime.access_policy,
        periodic_maintenance=lambda: runtime.reconcile_staging("periodic"),
        pre_ready_maintenance=lambda: runtime.reconcile_staging("startup"),
    )


def _build_service_access_snapshot(
    paths: XdgPaths,
    config: ConfigBundle,
    machine_config: MachineExecutionConfig,
    *,
    inspector: AccessFactsInspector | None,
) -> ServiceAccessSnapshotV1:
    running = PosixPrincipalV1(os.getuid(), os.getgid())
    loaded = load_service_access_config(
        paths.service_access_file,
        service_principal=running,
        machine_config=machine_config,
        machine_sha256=config.digests["machine"],
    )
    if loaded.config.canonical_runtime_dir != paths.runtime_dir:
        raise ValueError("service access canonical runtime directory differs from service XDG runtime")
    if loaded.config.canonical_socket != paths.socket_file:
        raise ValueError("service access canonical socket differs from service XDG socket")
    if loaded.config.canonical_data_dir != paths.data_dir:
        raise ValueError("service access canonical data directory differs from service XDG data")
    return build_service_access_snapshot(
        loaded,
        machine_profile_sha256=config.digests["machine"],
        inspector=inspector,
    )


def _build_openclaw_protocol(access_snapshot: ServiceAccessSnapshotV1) -> OpenClawProtocol:
    def load_policy(path: Path, writer: ServiceWriterSnapshotV1) -> OpenClawDeploymentPolicyV1:
        return load_openclaw_deployment_policy(path, service_writer_snapshot=writer)

    def build_deployment(policy: OpenClawDeploymentPolicyV1) -> PackagedOpenClawDeployment:
        return PackagedOpenClawDeployment(policy, OpenClawFilesystemAdapter(policy))

    return OpenClawProtocol(
        access_snapshot,
        policy_loader=load_policy,
        deployment_factory=build_deployment,
    )
