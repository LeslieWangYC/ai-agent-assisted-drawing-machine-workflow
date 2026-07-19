import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol, TypedDict
from uuid import uuid4

from drawingmachine import __version__
from drawingmachine.application.jobs import ReviewContinuationV1, RouteMode, WorkflowSubmissionV1
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.config import ConfigBundle, XdgPaths, parse_machine_execution_config
from drawingmachine.domain.jobs import JobKind, RequesterIdentity
from drawingmachine.domain.workflow import ProcessedImageReview
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.client_data import (
    EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1,
    ClientDataRole,
    ServiceClientDataPort,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingClock,
    staging_job_id,
)
from drawingmachine.ports.clock import Clock
from drawingmachine.ports.providers import ProviderRegistry
from drawingmachine.ports.repository import Repository
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest
from drawingmachine.service.access_policy import RuntimeAccessPolicy, ServiceAccessSnapshotV1
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.job_coordinator import (
    CoordinatorCommand,
    CoordinatorCommandKind,
    JobCoordinator,
    JobProjection,
)
from drawingmachine.service.machine_protocol import MachineCoordinatorPort, MachineProtocol
from drawingmachine.service.models import RequestContext, ServiceStatus
from drawingmachine.service.openclaw_protocol import OpenClawProtocol


class WorkflowSubmitArguments(TypedDict):
    schema_version: Literal[1]
    mode: Literal["submit"]
    job_name: str
    input_path: str
    route_mode: Literal["auto", "direct", "image-edit"]
    semantic_assessment: JsonObject | None
    prompt: str | None


class WorkflowReviewArguments(TypedDict):
    schema_version: Literal[1]
    mode: Literal["resume_review"]
    job_id: str
    review: JsonObject
    reviewed_image_sha256: str


class JobArguments(TypedDict):
    schema_version: Literal[1]
    job_id: str


class GcodeCheckArguments(TypedDict):
    schema_version: Literal[1]
    path: str


class WorkflowRunData(TypedDict):
    schema_version: Literal[1]
    accepted: bool
    job_id: str
    revision: int
    state: str


class JobStatusData(TypedDict):
    schema_version: Literal[1]
    job: JsonObject
    artifacts: list[JsonObject]
    cancellation_requested: bool


class JobCancelData(TypedDict):
    schema_version: Literal[1]
    accepted: bool
    job_id: str
    cancellation_requested: bool


class GcodeCheckData(TypedDict):
    schema_version: Literal[1]
    job_id: str
    state: str
    static_result: JsonObject | None


_JOB_ARGUMENTS = frozenset({"schema_version", "job_id"})
_GCODE_ARGUMENTS = frozenset({"schema_version", "path"})
_WORKFLOW_SUBMIT_ARGUMENTS = frozenset(
    {"schema_version", "mode", "job_name", "input_path", "route_mode", "semantic_assessment", "prompt"}
)
_WORKFLOW_REVIEW_ARGUMENTS = frozenset({"schema_version", "mode", "job_id", "review", "reviewed_image_sha256"})


class _MachineCoordinatorLifecycle(MachineCoordinatorPort, Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


MachineCoordinatorFactory = Callable[
    [str, Callable[[str], None] | None],
    _MachineCoordinatorLifecycle,
]


class ServiceRuntime:
    def __init__(
        self,
        *,
        paths: XdgPaths,
        config: ConfigBundle,
        repository: Repository,
        clock: Clock,
        provider_registry: ProviderRegistry | None = None,
        access_snapshot: ServiceAccessSnapshotV1 | None = None,
        openclaw_protocol: OpenClawProtocol | None = None,
        client_data: ServiceClientDataPort | None = None,
        staging_reconciler: StagingReconciler | None = None,
        staging_clock: StagingClock | None = None,
    ) -> None:
        self._paths = paths
        self._config = config
        self._repository = repository
        self._clock = clock
        self._provider_registry = provider_registry
        self._client_data = client_data
        self._staging_reconciler = staging_reconciler
        self._staging_clock = staging_clock
        if (staging_reconciler is None) != (staging_clock is None):
            raise TypeError("staging reconciler and clock must be installed together")
        self._access_policy = None if access_snapshot is None else RuntimeAccessPolicy(access_snapshot)
        self._service_epoch: str | None = None
        self._started_at: datetime | None = None
        self._pid: int | None = None
        self._started = False
        self._command_dispatcher = CommandDispatcher(self.status)
        if openclaw_protocol is not None:
            if type(openclaw_protocol) is not OpenClawProtocol:
                raise TypeError("openclaw_protocol must be an exact OpenClawProtocol")
            if access_snapshot is None:
                raise TypeError("OpenClaw protocol requires a service access snapshot")
            openclaw_protocol.register(self._command_dispatcher)
        self._job_coordinator: JobCoordinator | None = None
        self._machine_coordinator_factory: MachineCoordinatorFactory | None = None
        self._machine_coordinator: _MachineCoordinatorLifecycle | None = None
        self._job_handlers_registered = False
        self._machine_handlers_registered = False
        self._stopping = False
        self._active_staging_lock = threading.Lock()
        self._active_staging_requests: set[str] = set()

    @property
    def provider_registry(self) -> ProviderRegistry:
        if self._provider_registry is None:
            raise RuntimeError("provider registry is not installed")
        return self._provider_registry

    @property
    def dispatcher(self) -> CommandDispatcher:
        self._require_started()
        return self._command_dispatcher

    @property
    def access_policy(self) -> RuntimeAccessPolicy | None:
        return self._access_policy

    def install_job_coordinator(self, coordinator: JobCoordinator) -> None:
        if self._started:
            self._raise("SERVICE_ALREADY_STARTED", "job coordinator cannot be installed after runtime start")
        if self._job_coordinator is not None:
            self._raise("COORDINATOR_ALREADY_INSTALLED", "job coordinator is already installed")
        self._job_coordinator = coordinator

    def install_machine_coordinator_factory(self, factory: MachineCoordinatorFactory) -> None:
        if self._started:
            self._raise("SERVICE_ALREADY_STARTED", "machine coordinator cannot be installed after runtime start")
        if self._machine_coordinator_factory is not None:
            self._raise("COORDINATOR_ALREADY_INSTALLED", "machine coordinator is already installed")
        self._machine_coordinator_factory = factory

    def start(self) -> None:
        if self._started:
            raise DrawingMachineError(
                ErrorPayload(
                    code="SERVICE_ALREADY_STARTED",
                    category=ErrorCategory.SERVICE,
                    message="service runtime is already started",
                    retryable=False,
                    details={},
                )
            )

        candidate_service_epoch = str(uuid4())
        job_start_attempted = False
        machine_start_attempted = False
        machine_coordinator: _MachineCoordinatorLifecycle | None = None
        try:
            _create_private_directory(self._paths.state_dir)
            self._paths.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._access_policy is None:
                _create_private_directory(self._paths.runtime_dir)
            else:
                self._access_policy.prepare_runtime_directory()
            self._repository.initialize()
            job_coordinator = self._job_coordinator
            machine_factory = self._machine_coordinator_factory
            if machine_factory is not None:
                publisher = None if job_coordinator is None else job_coordinator.publish_external
                machine_coordinator = machine_factory(candidate_service_epoch, publisher)
                self._machine_coordinator = machine_coordinator
                machine_start_attempted = True
                machine_coordinator.start()
                if not self._machine_handlers_registered:
                    machine_config = parse_machine_execution_config(
                        self._config.machine,
                        profile_path=self._config.machine_path,
                    )
                    MachineProtocol(
                        dispatcher=self._command_dispatcher,
                        coordinator=self._require_machine_coordinator,
                        automation_principal=machine_config.automation_principal,
                        operator_principal=machine_config.operator_principal,
                    ).register()
                    self._machine_handlers_registered = True
            if job_coordinator is not None:
                job_start_attempted = True
                job_coordinator.start()
                if not self._job_handlers_registered:
                    self._register_job_handlers()
                    self._job_handlers_registered = True
            started_at = self._clock.now()
            service_epoch = candidate_service_epoch
            pid = os.getpid()
            payload: JsonObject = {
                "service_epoch": service_epoch,
                "version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "pid": pid,
            }
            if self._access_policy is not None:
                payload["service_access_sha256"] = self._access_policy.snapshot.source_sha256
            self._repository.append_audit(
                str(uuid4()),
                "service.started",
                None,
                payload,
            )
        except BaseException:
            try:
                if machine_start_attempted and machine_coordinator is not None:
                    machine_coordinator.stop()
            finally:
                if job_start_attempted and self._job_coordinator is not None:
                    self._job_coordinator.stop()
                self._repository.close()
                self._machine_coordinator = None
            raise

        self._service_epoch = service_epoch
        self._started_at = started_at
        self._pid = pid
        self._stopping = False
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return

        self._stopping = True

        service_epoch = self._service_epoch
        payload: JsonObject = {
            "service_epoch": service_epoch,
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "pid": self._pid,
        }
        try:
            try:
                self.reconcile_staging("shutdown")
                if self._machine_coordinator is not None:
                    self._machine_coordinator.stop()
            finally:
                if self._job_coordinator is not None:
                    self._job_coordinator.stop()
            self._repository.append_audit(str(uuid4()), "service.stopped", None, payload)
        finally:
            self._started = False
            self._stopping = False
            self._service_epoch = None
            self._started_at = None
            self._pid = None
            self._machine_coordinator = None
            self._repository.close()

    def _register_job_handlers(self) -> None:
        self._command_dispatcher.register("workflow.run", self._workflow_run)
        self._command_dispatcher.register("job.status", self._job_status)
        self._command_dispatcher.register("job.cancel", self._job_cancel)
        self._command_dispatcher.register("gcode.check", self._gcode_check)

    def _workflow_run(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._set_staging_request_active(request.request_id, active=True)
        try:
            self._before_staging_command(ClientDataRole.IMAGE)
            return self._workflow_run_inner(request, context)
        finally:
            try:
                self.reconcile_staging("after-command")
            finally:
                self._set_staging_request_active(request.request_id, active=False)

    def _workflow_run_inner(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        coordinator = self._require_coordinator()
        arguments = request.arguments
        mode = arguments.get("mode")
        requester = _requester(context)
        self._require_automation(context)
        if mode == "submit":
            _require_exact(arguments, _WORKFLOW_SUBMIT_ARGUMENTS)
            _require_schema(arguments)
            job_name = _string(arguments["job_name"], "job_name")
            input_path = _absolute_path(arguments["input_path"], "input_path")
            route_text = _literal(arguments["route_mode"], "route_mode", {item.value for item in RouteMode})
            semantic = arguments["semantic_assessment"]
            if semantic is not None and not isinstance(semantic, dict):
                _invalid("semantic_assessment must be an object or null", field="semantic_assessment")
            if route_text == RouteMode.AUTO.value and semantic is None:
                _invalid("auto route mode requires semantic_assessment", field="semantic_assessment")
            prompt = arguments["prompt"]
            if prompt is not None and not isinstance(prompt, str):
                _invalid("prompt must be a string or null", field="prompt")
            client_data = self._client_data
            if client_data is not None:
                client_data.bind_request_envelope(request.request_id, ClientDataRole.IMAGE, arguments)
                admission = client_data.register_decoded(request.request_id, ClientDataRole.IMAGE, input_path)
                if admission.state in {
                    StagingAdmissionState.DURABLY_ADMITTED,
                    StagingAdmissionState.CONSUMED,
                }:
                    projection = self._exact_staging_replay(
                        admission,
                        ClientDataRole.IMAGE,
                        job_name=job_name,
                        request_arguments={
                            "route_mode": arguments["route_mode"],
                            "semantic_assessment": arguments["semantic_assessment"],
                            "prompt": arguments["prompt"],
                        },
                    )
                    if admission.state is StagingAdmissionState.DURABLY_ADMITTED:
                        client_data.consume(request.request_id, expected_revision=admission.revision)
                    return _workflow_run_data(projection)
                imported = client_data.claim_image(request.request_id, input_path)
                input_path = imported.payload_path
            job_id = (
                _staging_job_id(request.request_id, ClientDataRole.IMAGE) if client_data is not None else str(uuid4())
            )
            command_payload: WorkflowSubmissionV1 | ReviewContinuationV1 = WorkflowSubmissionV1(
                1,
                job_name,
                input_path,
                RouteMode(route_text),
                semantic,
                prompt,
            )
            kind = CoordinatorCommandKind.SUBMIT
        elif mode == "resume_review":
            _require_exact(arguments, _WORKFLOW_REVIEW_ARGUMENTS)
            _require_schema(arguments)
            job_id = _string(arguments["job_id"], "job_id")
            review_value = arguments["review"]
            if not isinstance(review_value, dict):
                _invalid("review must be an object", field="review")
            digest = _string(arguments["reviewed_image_sha256"], "reviewed_image_sha256")
            command_payload = ReviewContinuationV1(
                1,
                job_id,
                ProcessedImageReview.from_json(review_value),
                digest,
            )
            kind = CoordinatorCommandKind.CONTINUE_REVIEW
        else:
            _invalid("mode must be submit or resume_review", field="mode")
        projection = coordinator.submit(
            CoordinatorCommand(kind, request.request_id, job_id, requester, command_payload)
        )
        if mode == "submit" and self._client_data is not None:
            durable_admission = self._repository.get_staging_admission(request.request_id)
            if durable_admission is None or durable_admission.state is not StagingAdmissionState.DURABLY_ADMITTED:
                self._raise("STAGING_ADMISSION_INVALID", "job was not durably bound to its staging authority")
            self._client_data.consume(request.request_id, expected_revision=durable_admission.revision)
        return _workflow_run_data(projection)

    def _job_status(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._require_automation(context)
        arguments = request.arguments
        _require_exact(arguments, _JOB_ARGUMENTS)
        _require_schema(arguments)
        projection = self._require_coordinator().status(_string(arguments["job_id"], "job_id"))
        if self._client_data is not None:
            exportable = tuple(
                artifact for artifact in projection.artifacts if artifact.role in EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1
            )
            try:
                self._client_data.publish_artifacts(projection.job.job_id, exportable)
            except DrawingMachineError as error:
                if error.payload.code != "ARTIFACT_VALIDATION_FAILED":
                    raise
                raise DrawingMachineError(
                    ErrorPayload(
                        "ARTIFACT_PATH_INVALID",
                        ErrorCategory.VALIDATION,
                        "published artifact does not match its durable reference",
                        False,
                        {},
                        request_id=request.request_id,
                        job_id=projection.job.job_id,
                    )
                ) from error
        return _status_data(projection, redact_client_path=self._client_data is not None)

    def _job_cancel(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._require_automation(context)
        arguments = request.arguments
        _require_exact(arguments, _JOB_ARGUMENTS)
        _require_schema(arguments)
        job_id = _string(arguments["job_id"], "job_id")
        projection = self._require_coordinator().request_cancel(job_id, request.request_id, _requester(context))
        return {
            "schema_version": 1,
            "accepted": True,
            "job_id": projection.job.job_id,
            "cancellation_requested": projection.cancellation_requested,
        }

    def _gcode_check(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._set_staging_request_active(request.request_id, active=True)
        try:
            self._before_staging_command(ClientDataRole.GCODE)
            return self._gcode_check_inner(request, context)
        finally:
            try:
                self.reconcile_staging("after-command")
            finally:
                self._set_staging_request_active(request.request_id, active=False)

    def _gcode_check_inner(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._require_automation(context)
        arguments = request.arguments
        _require_exact(arguments, _GCODE_ARGUMENTS)
        _require_schema(arguments)
        path = _absolute_path(arguments["path"], "path")
        client_data = self._client_data
        if client_data is not None:
            client_data.bind_request_envelope(request.request_id, ClientDataRole.GCODE, arguments)
            admission = client_data.register_decoded(request.request_id, ClientDataRole.GCODE, path)
            if admission.state in {
                StagingAdmissionState.DURABLY_ADMITTED,
                StagingAdmissionState.CONSUMED,
            }:
                projection = self._exact_staging_replay(admission, ClientDataRole.GCODE)
                if admission.state is StagingAdmissionState.DURABLY_ADMITTED:
                    client_data.consume(request.request_id, expected_revision=admission.revision)
                return _gcode_check_data(projection)
            imported = client_data.claim_gcode(request.request_id, path)
            path = imported.payload_path
        job_id = _staging_job_id(request.request_id, ClientDataRole.GCODE) if client_data is not None else str(uuid4())
        projection = self._require_coordinator().submit(
            CoordinatorCommand(
                CoordinatorCommandKind.GCODE_CHECK,
                request.request_id,
                job_id,
                _requester(context),
                path,
            )
        )
        if client_data is not None:
            durable_admission = self._repository.get_staging_admission(request.request_id)
            if durable_admission is None or durable_admission.state is not StagingAdmissionState.DURABLY_ADMITTED:
                self._raise("STAGING_ADMISSION_INVALID", "G-code job lacks durable staging authority")
            client_data.consume(request.request_id, expected_revision=durable_admission.revision)
        return _gcode_check_data(projection)

    def _exact_staging_replay(
        self,
        admission: StagingAdmissionRecordV1,
        role: ClientDataRole,
        *,
        job_name: str | None = None,
        request_arguments: JsonObject | None = None,
    ) -> JobProjection:
        job_id = admission.job_id
        if job_id is None:
            self._raise("STAGING_REPLAY_MISMATCH", "durable staging replay lacks a job binding")
        job = self._repository.get_job(job_id)
        expected_kind = JobKind.OFFLINE_WORKFLOW if role is ClientDataRole.IMAGE else JobKind.GCODE_CHECK
        expected_artifact_role = "input_image" if role is ClientDataRole.IMAGE else "gcode"
        if job is None or job.kind is not expected_kind or (job_name is not None and job.name != job_name):
            self._raise("STAGING_REPLAY_MISMATCH", "durable staging replay does not match its job")
        persisted_request = job.to_json()["request"]
        if not isinstance(persisted_request, dict):
            self._raise("STAGING_REPLAY_MISMATCH", "durable job request is invalid")
        input_value = persisted_request.get("input_path")
        if not isinstance(input_value, str):
            self._raise("STAGING_REPLAY_MISMATCH", "durable job input binding is invalid")
        admitted_path = Path(input_value)
        if (
            not admitted_path.is_absolute()
            or admitted_path.name != "payload"
            or admitted_path.parent.name != admission.bundle_name
            or admitted_path.parent.parent.name != "admitted"
            or admitted_path.parent.parent.parent.name != role.value
        ):
            self._raise("STAGING_REPLAY_MISMATCH", "durable job input does not match staging authority")
        if request_arguments is not None and any(
            persisted_request.get(key) != value for key, value in request_arguments.items()
        ):
            self._raise("STAGING_REPLAY_MISMATCH", "request_id was replayed with different arguments")
        input_artifacts = tuple(
            artifact for artifact in self._repository.list_artifacts(job_id) if artifact.role == expected_artifact_role
        )
        if (
            len(input_artifacts) != 1
            or input_artifacts[0].sha256 != admission.source_sha256
            or input_artifacts[0].size_bytes != admission.payload_identity.size_bytes
        ):
            self._raise("STAGING_REPLAY_MISMATCH", "durable job artifact does not match staging authority")
        return self._require_coordinator().status(job_id)

    def _require_coordinator(self) -> JobCoordinator:
        coordinator = self._job_coordinator
        if coordinator is None:
            self._raise("JOB_COORDINATOR_UNAVAILABLE", "job coordinator is unavailable")
        return coordinator

    def _require_machine_coordinator(self) -> MachineCoordinatorPort:
        coordinator = self._machine_coordinator
        if coordinator is None:
            self._raise("MACHINE_COORDINATOR_UNAVAILABLE", "machine coordinator is unavailable")
        return coordinator

    def reconcile_staging(self, trigger: str) -> None:
        clock = self._staging_clock
        client_data = self._client_data
        if client_data is not None and clock is not None:
            with self._active_staging_lock:
                active_requests = frozenset(self._active_staging_requests)
            client_data.reconcile(
                trigger,
                clock,
                active_requests,
                self._repository.list_staging_admissions(),
            )

    def _set_staging_request_active(self, request_id: str, *, active: bool) -> None:
        with self._active_staging_lock:
            if active:
                self._active_staging_requests.add(request_id)
            else:
                self._active_staging_requests.discard(request_id)

    def _before_staging_command(self, role: ClientDataRole) -> None:
        self.reconcile_staging("before-command")
        reconciler = self._staging_reconciler
        if reconciler is not None:
            reconciler.require_role_ready(role)

    def _require_automation(self, context: RequestContext) -> None:
        policy = self._access_policy
        if policy is None:
            return
        expected = policy.snapshot.config.automation_principal
        if (context.peer.uid, context.peer.gid) != (expected.uid, expected.gid):
            raise DrawingMachineError(
                ErrorPayload(
                    code="CLIENT_DATA_PERMISSION_DENIED",
                    category=ErrorCategory.PERMISSION,
                    message="only the configured automation principal may access jobs and client data",
                    retryable=False,
                    details={},
                )
            )

    def status(self) -> ServiceStatus:
        self._require_started()
        assert self._service_epoch is not None
        assert self._started_at is not None
        assert self._pid is not None
        health = self._repository.health()
        return ServiceStatus(
            version=__version__,
            protocol_version=PROTOCOL_VERSION,
            database_schema_version=health.schema_version,
            service_epoch=self._service_epoch,
            started_at=self._started_at,
            pid=self._pid,
        )

    def _require_started(self) -> None:
        if not self._started or self._stopping:
            raise DrawingMachineError(
                ErrorPayload(
                    code="SERVICE_NOT_STARTED",
                    category=ErrorCategory.SERVICE,
                    message="service runtime is not started",
                    retryable=True,
                    details={},
                )
            )

    @staticmethod
    def _raise(code: str, message: str) -> NoReturn:
        raise DrawingMachineError(ErrorPayload(code, ErrorCategory.SERVICE, message, False, {}))


def _create_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _invalid(message: str, *, field: str | None = None) -> NoReturn:
    details: JsonObject = {} if field is None else {"field": field}
    raise DrawingMachineError(ErrorPayload("COMMAND_ARGUMENTS_INVALID", ErrorCategory.INPUT, message, False, details))


def _require_exact(arguments: JsonObject, expected: frozenset[str]) -> None:
    actual = set(arguments)
    if actual != expected:
        _invalid(
            "command arguments must contain exactly the documented fields",
        )


def _require_schema(arguments: JsonObject) -> None:
    value = arguments["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        _invalid("schema_version must be the integer 1", field="schema_version")


def _string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str) or not value:
        _invalid(f"{field} must be a non-empty string", field=field)
    return value


def _literal(value: JsonValue, field: str, allowed: set[str]) -> str:
    text = _string(value, field)
    if text not in allowed:
        _invalid(f"{field} has an invalid literal value", field=field)
    return text


def _absolute_path(value: JsonValue, field: str) -> Path:
    path = Path(_string(value, field))
    if not path.is_absolute():
        _invalid(f"{field} must be absolute", field=field)
    return path


def _requester(context: RequestContext) -> RequesterIdentity:
    peer = context.peer
    return RequesterIdentity("LOCAL_PEER", peer.pid, peer.uid, peer.gid)


def _staging_job_id(request_id: str, role: ClientDataRole) -> str:
    return staging_job_id(request_id, role)


def _workflow_run_data(projection: JobProjection) -> JsonObject:
    return {
        "schema_version": 1,
        "accepted": True,
        "job_id": projection.job.job_id,
        "revision": projection.job.revision,
        "state": projection.job.state.value,
    }


def _gcode_check_data(projection: JobProjection) -> JsonObject:
    return {
        "schema_version": 1,
        "job_id": projection.job.job_id,
        "state": projection.job.state.value,
        "static_result": None,
    }


def _status_data(projection: JobProjection, *, redact_client_path: bool = False) -> JsonObject:
    job = projection.job.to_json()
    if redact_client_path:
        request = job.get("request")
        if isinstance(request, dict) and "input_path" in request:
            sanitized_request: JsonObject = dict(request)
            sanitized_request["input_path"] = "<client-data>"
            job = dict(job)
            job["request"] = sanitized_request
    return {
        "schema_version": 1,
        "job": job,
        "artifacts": [artifact.to_json() for artifact in projection.artifacts],
        "cancellation_requested": projection.cancellation_requested,
    }
