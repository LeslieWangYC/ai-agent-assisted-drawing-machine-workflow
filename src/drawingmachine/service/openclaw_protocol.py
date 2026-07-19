from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from drawingmachine.application.openclaw import PackagedOpenClawDeployment
from drawingmachine.config.openclaw_deployment import (
    OpenClawDeploymentPolicyV1,
    ServiceWriterSnapshotV1,
)
from drawingmachine.domain.openclaw import (
    OpenClawCheckResultV1,
    OpenClawDeploymentError,
    OpenClawInstallResultV1,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.protocol import ProtocolRequest
from drawingmachine.service.access_policy import ServiceAccessSnapshotV1
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.models import PeerCredentials, RequestContext

PolicyLoader = Callable[[Path, ServiceWriterSnapshotV1], OpenClawDeploymentPolicyV1]
DeploymentFactory = Callable[[OpenClawDeploymentPolicyV1], PackagedOpenClawDeployment]
_ARGUMENT_FIELDS = frozenset({"schema_version", "openclaw_root"})


class OpenClawProtocol:
    """Direct-operator protocol mapping to the service-owned D3 deployment use case."""

    def __init__(
        self,
        access_snapshot: ServiceAccessSnapshotV1,
        *,
        policy_loader: PolicyLoader,
        deployment_factory: DeploymentFactory,
    ) -> None:
        if type(access_snapshot) is not ServiceAccessSnapshotV1:
            raise TypeError("access_snapshot must be an exact ServiceAccessSnapshotV1")
        if not callable(policy_loader) or not callable(deployment_factory):
            raise TypeError("OpenClaw policy loader and deployment factory must be callable")
        self._access = access_snapshot
        self._policy_loader = policy_loader
        self._deployment_factory = deployment_factory
        self._writer = ServiceWriterSnapshotV1(
            access_snapshot.config.service_principal.uid,
            access_snapshot.config.service_principal.gid,
        )
        self._cached: tuple[OpenClawDeploymentPolicyV1, PackagedOpenClawDeployment] | None = None
        self._lock = threading.Lock()

    def register(self, dispatcher: CommandDispatcher) -> None:
        if type(dispatcher) is not CommandDispatcher:
            raise TypeError("dispatcher must be an exact CommandDispatcher")
        dispatcher.register("openclaw.check", self._check)
        dispatcher.register("openclaw.install", self._install)

    def _check(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._authorize(context)
        root = self._arguments(request.arguments)
        deployment = self._deployment(root)
        try:
            result = deployment.check(root, self._writer)
        except OpenClawDeploymentError as error:
            _deployment_error(error)
        if type(result) is not OpenClawCheckResultV1:
            _internal("OpenClaw check returned an invalid result")
        if not result.in_sync:
            _negative_result(
                "OPENCLAW_CHECK_NOT_IN_SYNC",
                ErrorCategory.VALIDATION,
                "OpenClaw resources are not in sync",
                result.to_json(),
            )
        return result.to_json()

    def _install(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        self._authorize(context)
        root = self._arguments(request.arguments)
        deployment = self._deployment(root)
        try:
            result = deployment.install(root, request.request_id, self._writer)
        except OpenClawDeploymentError as error:
            _deployment_error(error)
        if type(result) is not OpenClawInstallResultV1:
            _internal("OpenClaw install returned an invalid result")
        if not result.installed:
            _negative_result(
                "OPENCLAW_INSTALL_NOT_COMPLETED",
                ErrorCategory.SERVICE,
                "OpenClaw installation did not complete",
                result.to_json(),
            )
        return result.to_json()

    def _authorize(self, context: RequestContext) -> None:
        operator = self._access.config.operator_principal
        if (
            type(context) is not RequestContext
            or type(context.peer) is not PeerCredentials
            or (context.peer.uid, context.peer.gid) != (operator.uid, operator.gid)
        ):
            raise DrawingMachineError(
                ErrorPayload(
                    "OPENCLAW_OPERATOR_DENIED",
                    ErrorCategory.PERMISSION,
                    "only the configured direct operator peer may deploy OpenClaw resources",
                    False,
                    {},
                )
            )

    def _arguments(self, arguments: JsonObject) -> Path:
        if type(arguments) is not dict or set(arguments) != _ARGUMENT_FIELDS:
            _arguments_invalid("command arguments must contain exactly the documented fields")
        version = arguments["schema_version"]
        if type(version) is not int or version != 1:
            _arguments_invalid("schema_version must be the integer 1")
        root = _canonical_root(arguments["openclaw_root"])
        return root

    def _deployment(self, root: Path) -> PackagedOpenClawDeployment:
        try:
            policy = self._policy_loader(self._access.config.openclaw_policy_path, self._writer)
        except (OSError, TypeError, ValueError) as error:
            raise _policy_invalid("service-owned OpenClaw deployment policy is invalid") from error
        if type(policy) is not OpenClawDeploymentPolicyV1:
            raise _policy_invalid("service-owned OpenClaw deployment policy is invalid")
        config = self._access.config
        if (
            policy.operator_principal != config.operator_principal
            or policy.automation_principal != config.automation_principal
            or policy.managed_owner != config.service_principal
        ):
            raise _policy_invalid("OpenClaw deployment principals differ from service access authority")
        if root != policy.registry_root:
            _deployment_error(OpenClawDeploymentError("ROOT_MISMATCH", "explicit root differs from policy"))
        with self._lock:
            cached = self._cached
            if cached is None:
                try:
                    deployment = self._deployment_factory(policy)
                except (OSError, TypeError, ValueError) as error:
                    raise _policy_invalid("OpenClaw deployment authority could not be assembled") from error
                if type(deployment) is not PackagedOpenClawDeployment:
                    raise _policy_invalid("OpenClaw deployment authority could not be assembled")
                self._cached = (policy, deployment)
                return deployment
            if cached[0] != policy:
                _policy_changed()
            return cached[1]


def _canonical_root(value: JsonValue) -> Path:
    if type(value) is not str:
        _arguments_invalid("openclaw_root must be a string")
    if (
        not value.isascii()
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _arguments_invalid("openclaw_root must be bounded control-free canonical ASCII")
    path = Path(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or str(path) != value
        or any(part in {".", ".."} for part in path.parts)
    ):
        _arguments_invalid("openclaw_root must be a canonical absolute path")
    return path


def _arguments_invalid(message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload("OPENCLAW_COMMAND_ARGUMENTS_INVALID", ErrorCategory.INPUT, message, False, {})
    )


def _policy_invalid(message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload("OPENCLAW_POLICY_INVALID", ErrorCategory.PERMISSION, message, False, {}))


def _policy_changed() -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            "OPENCLAW_POLICY_CHANGED",
            ErrorCategory.PERMISSION,
            "OpenClaw deployment policy changed during this service lifetime",
            False,
            {},
        )
    )


def _deployment_error(error: OpenClawDeploymentError) -> NoReturn:
    if error.recovery_required:
        category = ErrorCategory.SERVICE
        message = "OpenClaw deployment requires explicit recovery"
    elif error.code in {"ROOT_MISMATCH", "WRITER_MISMATCH", "PROCESS_WRITER_MISMATCH", "POLICY_DRIFT"}:
        category = ErrorCategory.PERMISSION
        message = "OpenClaw deployment authority rejected the request"
    else:
        category = ErrorCategory.VALIDATION
        message = "OpenClaw deployment validation rejected the request"
    raise DrawingMachineError(ErrorPayload(error.code, category, message, False, {}))


def _internal(message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("OPENCLAW_PROTOCOL_INVALID", ErrorCategory.INTERNAL, message, False, {}))


def _negative_result(code: str, category: ErrorCategory, message: str, result: JsonObject) -> NoReturn:
    raise DrawingMachineError(ErrorPayload(code, category, message, False, {"result": result}))


__all__ = ["DeploymentFactory", "OpenClawProtocol", "PolicyLoader"]
