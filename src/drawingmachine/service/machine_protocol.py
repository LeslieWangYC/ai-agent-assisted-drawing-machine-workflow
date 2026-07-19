from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import NoReturn, Protocol, TypeVar, cast
from uuid import UUID

from drawingmachine.application.machine.commands import (
    HomeMachineCommand,
    HomeZMachineCommand,
    MachineActionCommand,
    MachineCommandMode,
    PrepareMachineCommand,
    RecoverMachineCommand,
    StreamMachineCommand,
    ZCalMachineCommand,
    ZConfirmMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.config.machine import MachinePrincipalConfig
from drawingmachine.domain.machine import (
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalChallenge,
    MachineFailureEvidenceV1,
    MachinePhase,
    MachineRecoveryEvidence,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
)
from drawingmachine.domain.machine.transitions import allowed_phase_transition, allowed_recovery_disposition
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.protocol import ProtocolRequest
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.models import RequestContext

_STATUS_FIELDS = frozenset({"schema_version", "execution_id"})
_PREPARE_FIELDS = frozenset({"schema_version", "job_id", "job_revision"})
_MOTION_FIELDS = frozenset({"schema_version", "execution_id", "mode", "challenge_id"})
_RECOVER_FIELDS = _MOTION_FIELDS | {"disposition"}
_STATUS_RESULT_FIELDS = frozenset(
    {"schema_version", "service_epoch", "accepting", "execution", "progress", "last_outcome"}
)
_EXECUTION_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "job_id",
        "ready_revision",
        "revision",
        "phase",
        "stream_milestone",
        "recovery_intent",
        "retired",
    }
)
_PROGRESS_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "execution_revision",
        "total",
        "acknowledged",
        "ok_count",
        "error_count",
        "percent",
        "last_source_line",
        "last_command",
        "last_event_at",
        "failure",
    }
)
_OUTCOME_FIELDS = frozenset(
    {"schema_version", "execution_id", "job_id", "execution_revision", "kind", "recovery_evidence", "occurred_at"}
)
_CHALLENGE_RESPONSE_FIELDS = frozenset(
    {"schema_version", "deduplicated", "executed", "execution_id", "phase", "challenge"}
)
_ADMISSION_FIELDS = frozenset(
    {
        "schema_version",
        "deduplicated",
        "admitted",
        "execution_id",
        "job_id",
        "job_revision",
        "execution_revision",
        "phase",
        "action",
    }
)
_APPROVED_ADMISSION_FIELDS = _ADMISSION_FIELDS | {
    "approval_challenge_id",
    "challenge_issued_at",
    "approved_at",
    "issuer_pid",
    "consumer_pid",
}
_RELEASE_FIELDS = frozenset({"schema_version", "deduplicated", "executed", "execution_id", "phase", "released"})
_MAX_ID_LENGTH = 4096
MotionCommandFactory = Callable[
    [str, AuthenticatedMachinePrincipal, str, MachineCommandMode, str | None],
    MachineActionCommand,
]
_MOTION_COMMANDS: dict[str, MotionCommandFactory] = {
    "machine.home": HomeMachineCommand,
    "machine.zcal": ZCalMachineCommand,
    "machine.zconfirm": ZConfirmMachineCommand,
    "machine.stream": StreamMachineCommand,
    "machine.home-z": HomeZMachineCommand,
}
_DISPOSITIONS = {
    "release": RecoveryDisposition.RELEASE,
    "restart-sequence": RecoveryDisposition.RESTART_SEQUENCE,
    "safe-home": RecoveryDisposition.SAFE_HOME,
}
_OutputEnum = TypeVar("_OutputEnum", MachinePhase, StreamMilestone, RecoveryIntent)


class MachineCoordinatorPort(Protocol):
    @property
    def service_epoch(self) -> str: ...

    def submit(self, command: MachineActionCommand) -> MachineCommandResult: ...

    def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]: ...

    def status(self, execution_id: str | None = None) -> JsonObject: ...


class MachineProtocol:
    """Closed protocol-to-application mapping for the sole machine coordinator."""

    def __init__(
        self,
        *,
        dispatcher: CommandDispatcher,
        coordinator: Callable[[], MachineCoordinatorPort],
        automation_principal: MachinePrincipalConfig,
        operator_principal: MachinePrincipalConfig,
    ) -> None:
        if type(dispatcher) is not CommandDispatcher:
            raise TypeError("dispatcher must be an exact CommandDispatcher")
        if not callable(coordinator):
            raise TypeError("coordinator must be callable")
        self._automation = _configured_principal(automation_principal, "automation")
        self._operator = _configured_principal(operator_principal, "operator")
        if self._automation.uid == self._operator.uid or self._automation.gid == self._operator.gid:
            _denied(
                "MACHINE_APPROVAL_ROLES_NOT_DISJOINT",
                "configured automation and operator UID/GID roles must be disjoint",
            )
        self._dispatcher = dispatcher
        self._coordinator = coordinator

    def register(self) -> None:
        self._dispatcher.register("machine.status", self._status)
        self._dispatcher.register("machine.prepare", self._prepare)
        for command in _MOTION_COMMANDS:
            self._dispatcher.register(command, self._motion)
        self._dispatcher.register("machine.recover", self._recover)

    def _status(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        _request_id(request.request_id)
        _require_exact(request.arguments, _STATUS_FIELDS)
        _require_schema(request.arguments)
        self._known_principal(context)
        selector = request.arguments["execution_id"]
        execution_id = None if selector is None else _uuid4(selector, "execution_id")
        coordinator = self._coordinator()
        result = coordinator.status(execution_id)
        if type(result) is not dict:
            _internal("machine coordinator returned an invalid status projection")
        _validate_status_result(result, coordinator.service_epoch, execution_id)
        return _machine_data(result, deduplicated=False, challenge=None)

    def _prepare(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        _request_id(request.request_id)
        _require_exact(request.arguments, _PREPARE_FIELDS)
        _require_schema(request.arguments)
        requester = self._principal(context)
        if requester.stable_principal != self._automation:
            _denied("MACHINE_PRINCIPAL_DENIED", "only the configured automation principal may prepare")
        command = PrepareMachineCommand(
            request.request_id,
            requester,
            _uuid4(request.arguments["job_id"], "job_id"),
            _positive_integer(request.arguments["job_revision"], "job_revision"),
        )
        return self._submit(command)

    def _motion(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        _request_id(request.request_id)
        _require_exact(request.arguments, _MOTION_FIELDS)
        _require_schema(request.arguments)
        requester = self._authorized_for_mode(context, request.arguments["mode"])
        mode, challenge_id = _mode_and_challenge(request.arguments["mode"], request.arguments["challenge_id"])
        command_type = _MOTION_COMMANDS[request.command]
        command = command_type(
            request.request_id,
            requester,
            _uuid4(request.arguments["execution_id"], "execution_id"),
            mode,
            challenge_id,
        )
        return self._submit(command)

    def _recover(self, request: ProtocolRequest, context: RequestContext) -> JsonObject:
        _request_id(request.request_id)
        _require_exact(request.arguments, _RECOVER_FIELDS)
        _require_schema(request.arguments)
        requester = self._authorized_for_mode(context, request.arguments["mode"])
        mode, challenge_id = _mode_and_challenge(request.arguments["mode"], request.arguments["challenge_id"])
        disposition_value = request.arguments["disposition"]
        if type(disposition_value) is not str or disposition_value not in _DISPOSITIONS:
            _invalid("disposition has an invalid literal value", field="disposition")
        command = RecoverMachineCommand(
            request.request_id,
            requester,
            _uuid4(request.arguments["execution_id"], "execution_id"),
            _DISPOSITIONS[disposition_value],
            mode,
            challenge_id,
        )
        return self._submit(command)

    def _submit(self, command: MachineActionCommand) -> JsonObject:
        coordinator = self._coordinator()
        result, observation = coordinator.submit_observed(command)
        if type(result) is not MachineCommandResult:
            _internal("machine coordinator returned an invalid command result")
        _validate_command_result(
            result.response,
            command,
            self._automation,
            self._operator,
            deduplicated=result.deduplicated,
        )
        execution_id = result.response.get("execution_id")
        if type(observation) is not dict or type(execution_id) is not str:
            _internal("machine coordinator returned an invalid observed command result")
        _validate_status_result(observation, coordinator.service_epoch, execution_id)
        if observation["execution"] is None:
            _internal("machine coordinator omitted the observed command execution")
        _validate_result_observation(result.response, observation, command)
        challenge = (
            result.response.get("challenge")
            if _is_challenge_request(command) and _challenge_matches_observation(result.response, observation)
            else None
        )
        return _machine_data(
            observation,
            deduplicated=result.deduplicated,
            challenge=challenge,
        )

    def _authorized_for_mode(self, context: RequestContext, value: JsonValue) -> AuthenticatedMachinePrincipal:
        requester = self._principal(context)
        if type(value) is not str or value not in {"request", "execute"}:
            _invalid("mode must be request or execute", field="mode")
        if value == "execute" and requester.stable_principal != self._operator:
            _denied("MACHINE_PRINCIPAL_DENIED", "only the configured operator principal may execute")
        if requester.stable_principal not in {self._automation, self._operator}:
            _denied("MACHINE_PRINCIPAL_DENIED", "peer is not a configured machine principal")
        return requester

    def _known_principal(self, context: RequestContext) -> AuthenticatedMachinePrincipal:
        requester = self._principal(context)
        if requester.stable_principal not in {self._automation, self._operator}:
            _denied("MACHINE_PRINCIPAL_DENIED", "peer is not a configured machine principal")
        return requester

    @staticmethod
    def _principal(context: RequestContext) -> AuthenticatedMachinePrincipal:
        if type(context) is not RequestContext:
            _denied("MACHINE_PRINCIPAL_DENIED", "machine commands require kernel peer context")
        peer = context.peer
        if (
            type(peer.pid) is not int
            or peer.pid < 1
            or type(peer.uid) is not int
            or peer.uid < 0
            or type(peer.gid) is not int
            or peer.gid < 0
        ):
            _denied("MACHINE_PRINCIPAL_DENIED", "kernel peer credentials are invalid")
        return AuthenticatedMachinePrincipal(peer.uid, peer.gid, peer.pid)


def _configured_principal(value: object, name: str) -> KernelMachinePrincipal:
    if type(value) is not MachinePrincipalConfig:
        raise TypeError(f"{name}_principal must be exact MachinePrincipalConfig")
    principal = value
    if type(principal.uid) is not int or principal.uid < 0 or type(principal.gid) is not int or principal.gid < 0:
        _denied("MACHINE_PRINCIPAL_DENIED", f"configured {name} principal is invalid")
    return KernelMachinePrincipal(principal.uid, principal.gid)


def _mode_and_challenge(mode_value: JsonValue, challenge_value: JsonValue) -> tuple[MachineCommandMode, str | None]:
    if mode_value == "request":
        if challenge_value is not None:
            _invalid("request mode requires a null challenge_id", field="challenge_id")
        return MachineCommandMode.REQUEST, None
    return MachineCommandMode.EXECUTE, _uuid4(challenge_value, "challenge_id")


def _require_exact(arguments: JsonObject, expected: frozenset[str]) -> None:
    if set(arguments) != expected:
        _invalid("command arguments must contain exactly the documented fields")


def _require_schema(arguments: JsonObject) -> None:
    value = arguments["schema_version"]
    if type(value) is not int or value != 1:
        _invalid("schema_version must be the integer 1", field="schema_version")


def _positive_integer(value: JsonValue, field: str) -> int:
    if type(value) is not int or value < 1:
        _invalid(f"{field} must be a positive integer", field=field)
    return value


def _request_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_ID_LENGTH:
        _invalid("request_id must be a bounded non-empty string", field="request_id")
    return value


def _uuid4(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_ID_LENGTH:
        _invalid(f"{field} must be a bounded canonical UUID4", field=field)
    try:
        parsed = UUID(value)
    except ValueError:
        _invalid(f"{field} must be a canonical UUID4", field=field)
    if parsed.version != 4 or str(parsed) != value:
        _invalid(f"{field} must be a canonical UUID4", field=field)
    return value


def _validate_status_result(result: JsonObject, service_epoch: object, selector: str | None) -> None:
    try:
        _output_fields(result, _STATUS_RESULT_FIELDS)
        _output_schema(result["schema_version"])
        _output_uuid(result["service_epoch"])
        _output(service_epoch == result["service_epoch"])
        _output(type(result["accepting"]) is bool)
        execution = result["execution"]
        progress = result["progress"]
        outcome = result["last_outcome"]
        if execution is None:
            _output(progress is None and outcome is None)
            return
        _output(type(execution) is dict)
        execution_value = cast(JsonObject, execution)
        _validate_execution(execution_value)
        execution_id = execution_value["execution_id"]
        if selector is not None:
            _output(execution_id == selector)
        if progress is not None:
            _output(type(progress) is dict)
            _validate_progress(cast(JsonObject, progress), execution_value)
        if outcome is not None:
            _output(type(outcome) is dict)
            _validate_outcome(cast(JsonObject, outcome), execution_value)
    except Exception:
        _internal("machine coordinator returned an invalid status projection")


def _machine_data(observation: JsonObject, *, deduplicated: bool, challenge: JsonValue) -> JsonObject:
    return {
        "schema_version": 1,
        "deduplicated": deduplicated,
        "service_epoch": observation["service_epoch"],
        "accepting": observation["accepting"],
        "execution": observation["execution"],
        "progress": observation["progress"],
        "last_outcome": observation["last_outcome"],
        "challenge": challenge,
    }


def _is_challenge_request(command: MachineActionCommand) -> bool:
    if type(command) is PrepareMachineCommand:
        return False
    motion = cast(
        HomeMachineCommand
        | ZCalMachineCommand
        | ZConfirmMachineCommand
        | StreamMachineCommand
        | HomeZMachineCommand
        | RecoverMachineCommand,
        command,
    )
    return motion.mode is MachineCommandMode.REQUEST


def _validate_execution(value: JsonObject) -> None:
    _output_fields(value, _EXECUTION_FIELDS)
    _output_schema(value["schema_version"])
    _output_uuid(value["execution_id"])
    _output_uuid(value["job_id"])
    _output_positive_int(value["ready_revision"])
    _output_nonnegative_int(value["revision"])
    phase = _output_enum(value["phase"], MachinePhase)
    milestone = _output_enum(value["stream_milestone"], StreamMilestone)
    intent_value = value["recovery_intent"]
    intent = None if intent_value is None else _output_enum(intent_value, RecoveryIntent)
    _output(type(value["retired"]) is bool)
    if phase is MachinePhase.RECOVERY_REQUIRED:
        _output(intent is not None)
    if intent is not None:
        expected = {
            StreamMilestone.NOT_STARTED: RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.FIRST_WRITE_POSSIBLE: RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
            StreamMilestone.STREAM_CONFIRMED: RecoveryIntent.POST_STREAM_SAFE_HOME,
        }[milestone]
        if phase is MachinePhase.RECOVERY_REQUIRED:
            _output(intent is expected)
    _validate_phase_milestone_intent(phase, milestone, intent)


def _validate_phase_milestone_intent(
    phase: MachinePhase,
    milestone: StreamMilestone,
    intent: RecoveryIntent | None,
) -> None:
    if phase is MachinePhase.RECOVERY_REQUIRED:
        expected = {
            StreamMilestone.NOT_STARTED: RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.FIRST_WRITE_POSSIBLE: RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
            StreamMilestone.STREAM_CONFIRMED: RecoveryIntent.POST_STREAM_SAFE_HOME,
        }[milestone]
        _output(intent is expected)
        return
    _output(intent is not RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY)
    if milestone is StreamMilestone.FIRST_WRITE_POSSIBLE:
        _output(phase is MachinePhase.STREAMING)
    if intent is RecoveryIntent.POST_STREAM_SAFE_HOME:
        _output(milestone is StreamMilestone.STREAM_CONFIRMED)
        _output(
            phase
            in {
                MachinePhase.PREPARING_SESSION,
                MachinePhase.AWAITING_HOME_APPROVAL,
                MachinePhase.HOMING,
                MachinePhase.COMPLETED,
            }
        )
        return
    if milestone is StreamMilestone.STREAM_CONFIRMED:
        _output(
            phase
            in {
                MachinePhase.AWAITING_HOME_Z_APPROVAL,
                MachinePhase.HOMING_Z,
                MachinePhase.COMPLETED,
            }
        )
    else:
        _output(phase not in {MachinePhase.AWAITING_HOME_Z_APPROVAL, MachinePhase.HOMING_Z, MachinePhase.COMPLETED})


def _validate_progress(value: JsonObject, execution: JsonObject) -> None:
    _output_fields(value, _PROGRESS_FIELDS)
    _output_schema(value["schema_version"])
    _output_uuid(value["execution_id"])
    _output(value["execution_id"] == execution["execution_id"])
    revision = _output_nonnegative_int(value["execution_revision"])
    execution_revision = _output_nonnegative_int(execution["revision"])
    _output(revision <= execution_revision)
    total = _output_nonnegative_int(value["total"])
    acknowledged = _output_nonnegative_int(value["acknowledged"])
    ok_count = _output_nonnegative_int(value["ok_count"])
    error_count = _output_nonnegative_int(value["error_count"])
    _output(acknowledged == ok_count and acknowledged <= total and error_count in {0, 1})
    percent = value["percent"]
    _output(type(percent) in {int, float})
    numeric_percent = cast(int | float, percent)
    _output(math.isfinite(float(numeric_percent)))
    expected_percent = 100.0 if total == 0 else acknowledged * 100.0 / total
    _output(math.isclose(float(numeric_percent), expected_percent, rel_tol=0.0, abs_tol=1e-9))
    last_source_line = value["last_source_line"]
    last_command = value["last_command"]
    if acknowledged + error_count == 0:
        _output(last_source_line is None and last_command is None)
    else:
        _output_positive_int(last_source_line)
        _output_text(last_command)
    _output_timestamp(value["last_event_at"])
    failure = value["failure"]
    _output((failure is None) == (error_count == 0))
    if failure is not None:
        MachineFailureEvidenceV1.from_json(failure)


def _validate_outcome(value: JsonObject, execution: JsonObject) -> None:
    _output_fields(value, _OUTCOME_FIELDS)
    _output_schema(value["schema_version"])
    _output_uuid(value["execution_id"])
    _output_uuid(value["job_id"])
    _output(value["execution_id"] == execution["execution_id"] and value["job_id"] == execution["job_id"])
    revision = _output_positive_int(value["execution_revision"])
    _output(revision <= _output_nonnegative_int(execution["revision"]))
    kind = value["kind"]
    _output(type(kind) is str and kind in {"COMPLETED", "RECOVERY_REQUIRED"})
    recovery = value["recovery_evidence"]
    _output((kind == "RECOVERY_REQUIRED") == (recovery is not None))
    if recovery is not None:
        parsed = MachineRecoveryEvidence.from_json(recovery)
        _output(parsed.execution_id == execution["execution_id"])
    _output_timestamp(value["occurred_at"])


def _validate_command_result(
    response: JsonObject,
    command: MachineActionCommand,
    automation: KernelMachinePrincipal,
    operator: KernelMachinePrincipal,
    *,
    deduplicated: bool,
) -> None:
    try:
        _output(response.get("deduplicated") is deduplicated)
        if type(command) is PrepareMachineCommand:
            _validate_admission(response, command)
            return
        if type(command) is RecoverMachineCommand:
            if command.mode is MachineCommandMode.REQUEST:
                _validate_challenge_response(response, command, automation, operator)
            elif command.disposition is RecoveryDisposition.RELEASE:
                _validate_release(response, command)
            else:
                _validate_admission(response, command)
            return
        motion = cast(
            HomeMachineCommand
            | ZCalMachineCommand
            | ZConfirmMachineCommand
            | StreamMachineCommand
            | HomeZMachineCommand,
            command,
        )
        if motion.mode is MachineCommandMode.REQUEST:
            _validate_challenge_response(response, motion, automation, operator)
        else:
            _validate_admission(response, motion)
    except Exception:
        _internal("machine coordinator returned an invalid command result")


def _validate_result_observation(
    response: JsonObject,
    observation: JsonObject,
    command: MachineActionCommand,
) -> None:
    try:
        execution = observation["execution"]
        _output(type(execution) is dict)
        execution_value = cast(JsonObject, execution)
        _output(response["execution_id"] == execution_value["execution_id"])
        raw_phase = _output_enum(response["phase"], MachinePhase)
        current_phase = _output_enum(execution_value["phase"], MachinePhase)
        current_revision = _output_nonnegative_int(execution_value["revision"])
        raw_revision: int | None = None
        if "job_id" in response:
            _output(response["job_id"] == execution_value["job_id"])
        if "job_revision" in response:
            _output(response["job_revision"] == execution_value["ready_revision"])
        if "execution_revision" in response:
            raw_revision = _output_nonnegative_int(response["execution_revision"])
        if response.get("released") is True:
            _output(execution_value["retired"] is True)
        if _is_challenge_request(command):
            challenge = cast(JsonObject, response["challenge"])
            binding = cast(JsonObject, challenge["binding"])
            _output(binding["service_epoch"] == observation["service_epoch"])
            _output(binding["execution_id"] == execution_value["execution_id"])
            _output(binding["job_id"] == execution_value["job_id"])
            _output(binding["ready_revision"] == execution_value["ready_revision"])
            raw_revision = _output_nonnegative_int(binding["execution_revision"])
            _output(binding["required_prior_phase"] == raw_phase.value)
            _output(binding["action"] == _command_action(command).value)
            expected_disposition = command.disposition.value if type(command) is RecoverMachineCommand else None
            _output(binding["recovery_disposition"] == expected_disposition)
        if raw_revision is None:
            _output(raw_phase is current_phase)
        else:
            _output(current_revision >= raw_revision)
            if current_revision == raw_revision:
                _output(current_phase is raw_phase)
            else:
                intent_value = execution_value["recovery_intent"]
                intent = None if intent_value is None else _output_enum(intent_value, RecoveryIntent)
                _output(
                    current_phase is MachinePhase.RECOVERY_REQUIRED or _forward_phase(raw_phase, current_phase, intent)
                )
        if type(command) is RecoverMachineCommand:
            intent_value = execution_value["recovery_intent"]
            _output(intent_value is not None)
            intent = _output_enum(intent_value, RecoveryIntent)
            _output(allowed_recovery_disposition(intent, command.disposition))
    except Exception:
        _internal("machine coordinator returned mismatched command authority")


def _forward_phase(
    prior: MachinePhase,
    result: MachinePhase,
    recovery_intent: RecoveryIntent | None,
) -> bool:
    if prior is result:
        return False
    visited = {prior}
    frontier = {prior}
    while frontier:
        following: set[MachinePhase] = set()
        for phase in frontier:
            for action in MachineAction:
                for candidate in MachinePhase:
                    if candidate in visited:
                        continue
                    if allowed_phase_transition(
                        phase,
                        action,
                        candidate,
                        recovery_intent=recovery_intent,
                    ):
                        if candidate is result:
                            return True
                        visited.add(candidate)
                        following.add(candidate)
        frontier = following
    return False


def _challenge_matches_observation(response: JsonObject, observation: JsonObject) -> bool:
    challenge = cast(JsonObject, response["challenge"])
    execution = cast(JsonObject, observation["execution"])
    binding = cast(JsonObject, challenge["binding"])
    return (
        binding.get("service_epoch") == observation.get("service_epoch")
        and binding.get("execution_id") == execution.get("execution_id")
        and binding.get("job_id") == execution.get("job_id")
        and binding.get("ready_revision") == execution.get("ready_revision")
        and binding.get("execution_revision") == execution.get("revision")
        and binding.get("required_prior_phase") == execution.get("phase")
    )


def _validate_challenge_response(
    response: JsonObject,
    command: HomeMachineCommand
    | ZCalMachineCommand
    | ZConfirmMachineCommand
    | StreamMachineCommand
    | HomeZMachineCommand
    | RecoverMachineCommand,
    automation: KernelMachinePrincipal,
    operator: KernelMachinePrincipal,
) -> None:
    _output_fields(response, _CHALLENGE_RESPONSE_FIELDS)
    _output_schema(response["schema_version"])
    _output(type(response["deduplicated"]) is bool and response["executed"] is False)
    _output_uuid(response["execution_id"])
    _output(response["execution_id"] == command.execution_id)
    action, prior_phase = _command_action_phase(command)
    _output(response["phase"] == prior_phase.value)
    challenge_json = response["challenge"]
    _output(type(challenge_json) is dict)
    trusted_requester = command.requester
    if response["deduplicated"] is True:
        requester_json = cast(JsonObject, challenge_json).get("requester")
        _output(type(requester_json) is dict and set(requester_json) == {"uid", "gid", "pid"})
        requester_value = cast(JsonObject, requester_json)
        _output(requester_value["uid"] == command.requester.uid and requester_value["gid"] == command.requester.gid)
        trusted_requester = AuthenticatedMachinePrincipal(
            command.requester.uid,
            command.requester.gid,
            _output_positive_int(requester_value["pid"]),
        )
    challenge = MachineApprovalChallenge.from_json(
        challenge_json,
        automation_principal=automation,
        operator_principal=operator,
        trusted_requester=trusted_requester,
        trusted_consumer=None,
    )
    _output(challenge.status is ApprovalStatus.PENDING)
    _output(challenge.binding.action is action and challenge.binding.required_prior_phase is prior_phase)
    _output(challenge.binding.execution_id == command.execution_id)
    expected_disposition = command.disposition if type(command) is RecoverMachineCommand else None
    _output(challenge.binding.recovery_disposition is expected_disposition)
    for identifier in (
        challenge.challenge_id,
        challenge.binding.execution_id,
        challenge.binding.job_id,
        challenge.binding.service_epoch,
        challenge.binding.machine_session_epoch,
    ):
        _output_uuid(identifier)


def _validate_admission(response: JsonObject, command: MachineActionCommand) -> None:
    action = _command_action(command)
    approved = action is not MachineAction.PREPARE_SESSION
    _output_fields(response, _APPROVED_ADMISSION_FIELDS if approved else _ADMISSION_FIELDS)
    _output_schema(response["schema_version"])
    _output(type(response["deduplicated"]) is bool and response["admitted"] is True)
    _output_uuid(response["execution_id"])
    _output_uuid(response["job_id"])
    _output_positive_int(response["job_revision"])
    _output_nonnegative_int(response["execution_revision"])
    _output(response["action"] == action.value)
    expected_phase = {
        MachineAction.PREPARE_SESSION: MachinePhase.PREPARING_SESSION,
        MachineAction.HOME: MachinePhase.HOMING,
        MachineAction.ZCAL: MachinePhase.Z_CALIBRATING,
        MachineAction.ZCONFIRM: MachinePhase.Z_CONFIRMING,
        MachineAction.STREAM: MachinePhase.STREAMING,
        MachineAction.HOME_Z: MachinePhase.HOMING_Z,
        MachineAction.RECOVER: MachinePhase.PREPARING_SESSION,
    }[action]
    _output(response["phase"] == expected_phase.value)
    if type(command) is PrepareMachineCommand:
        _output(response["job_id"] == command.job_id and response["job_revision"] == command.job_revision)
    elif type(command) is not RecoverMachineCommand:
        motion_command = cast(
            HomeMachineCommand
            | ZCalMachineCommand
            | ZConfirmMachineCommand
            | StreamMachineCommand
            | HomeZMachineCommand,
            command,
        )
        _output(response["execution_id"] == motion_command.execution_id)
    if approved:
        approved_command = cast(
            HomeMachineCommand
            | ZCalMachineCommand
            | ZConfirmMachineCommand
            | StreamMachineCommand
            | HomeZMachineCommand
            | RecoverMachineCommand,
            command,
        )
        challenge_id = response["approval_challenge_id"]
        _output_uuid(challenge_id)
        _output(challenge_id == approved_command.challenge_id)
        issued = _output_timestamp(response["challenge_issued_at"])
        approved_at = _output_timestamp(response["approved_at"])
        _output(approved_at >= issued)
        _output_positive_int(response["issuer_pid"])
        consumer_pid = _output_positive_int(response["consumer_pid"])
        if response["deduplicated"] is False:
            _output(consumer_pid == approved_command.requester.pid)


def _validate_release(response: JsonObject, command: RecoverMachineCommand) -> None:
    _output_fields(response, _RELEASE_FIELDS)
    _output_schema(response["schema_version"])
    _output(type(response["deduplicated"]) is bool and response["executed"] is True)
    _output_uuid(response["execution_id"])
    _output(response["execution_id"] == command.execution_id)
    _output(response["phase"] == MachinePhase.RECOVERY_REQUIRED.value)
    _output(response["released"] is True)


def _command_action(command: MachineActionCommand) -> MachineAction:
    return {
        PrepareMachineCommand: MachineAction.PREPARE_SESSION,
        HomeMachineCommand: MachineAction.HOME,
        ZCalMachineCommand: MachineAction.ZCAL,
        ZConfirmMachineCommand: MachineAction.ZCONFIRM,
        StreamMachineCommand: MachineAction.STREAM,
        HomeZMachineCommand: MachineAction.HOME_Z,
        RecoverMachineCommand: MachineAction.RECOVER,
    }[type(command)]


def _command_action_phase(
    command: HomeMachineCommand
    | ZCalMachineCommand
    | ZConfirmMachineCommand
    | StreamMachineCommand
    | HomeZMachineCommand
    | RecoverMachineCommand,
) -> tuple[MachineAction, MachinePhase]:
    action = _command_action(command)
    phase = {
        MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
        MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
        MachineAction.RECOVER: MachinePhase.RECOVERY_REQUIRED,
    }[action]
    return action, phase


def _output_fields(value: object, expected: frozenset[str]) -> None:
    _output(type(value) is dict and set(value) == expected)


def _output_schema(value: object) -> None:
    _output(type(value) is int and value == 1)


def _output_uuid(value: object) -> str:
    _output(type(value) is str and 0 < len(value) <= _MAX_ID_LENGTH)
    assert isinstance(value, str)
    parsed = UUID(value)
    _output(parsed.version == 4 and str(parsed) == value)
    return value


def _output_text(value: object) -> str:
    _output(type(value) is str and 0 < len(value) <= _MAX_ID_LENGTH)
    assert isinstance(value, str)
    _output(not any(ord(character) < 32 or ord(character) == 127 for character in value))
    return value


def _output_nonnegative_int(value: object) -> int:
    _output(type(value) is int and value >= 0)
    assert isinstance(value, int)
    return value


def _output_positive_int(value: object) -> int:
    _output(type(value) is int and value >= 1)
    assert isinstance(value, int)
    return value


def _output_timestamp(value: object) -> datetime:
    text = _output_text(value)
    parsed = datetime.fromisoformat(text)
    _output(parsed.tzinfo is not None and parsed.utcoffset() is not None)
    return parsed


def _output_enum(value: object, enum_type: type[_OutputEnum]) -> _OutputEnum:
    _output(type(value) is str)
    return enum_type(cast(str, value))


def _output(condition: object) -> None:
    if condition is not True:
        raise ValueError("invalid closed machine output")


def _invalid(message: str, *, field: str | None = None) -> NoReturn:
    details: JsonObject = {} if field is None else {"field": field}
    raise DrawingMachineError(
        ErrorPayload("MACHINE_COMMAND_ARGUMENTS_INVALID", ErrorCategory.INPUT, message, False, details)
    )


def _denied(code: str, message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload(code, ErrorCategory.PERMISSION, message, False, {}))


def _internal(message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("MACHINE_PROTOCOL_INVALID", ErrorCategory.INTERNAL, message, False, {}))


__all__ = ["MachineCoordinatorPort", "MachineProtocol"]
