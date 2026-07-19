from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest

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
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineAction,
    MachineApprovalBinding,
    MachineApprovalEvidenceV1,
    MachineFailureEvidenceV1,
    MachinePhase,
    MachineRecoveryEvidence,
    MachineSessionSnapshot,
    RecoveryDisposition,
    RecoveryIntent,
    StreamMilestone,
    issue_approval,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, ProtocolRequest, decode_request
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.machine_protocol import MachineProtocol
from drawingmachine.service.models import PeerCredentials, RequestContext, ServiceStatus

AUTOMATION = MachinePrincipalConfig(uid=1100, gid=1100)
OPERATOR = MachinePrincipalConfig(uid=2200, gid=2200)
JOB_ID = str(uuid4())
EXECUTION_ID = str(uuid4())
CHALLENGE_ID = str(uuid4())
NOW = datetime(2026, 7, 13, 10, tzinfo=UTC)


def _machine_action(command: MachineActionCommand) -> MachineAction:
    return {
        HomeMachineCommand: MachineAction.HOME,
        ZCalMachineCommand: MachineAction.ZCAL,
        ZConfirmMachineCommand: MachineAction.ZCONFIRM,
        StreamMachineCommand: MachineAction.STREAM,
        HomeZMachineCommand: MachineAction.HOME_Z,
        RecoverMachineCommand: MachineAction.RECOVER,
    }[type(command)]


def _challenge_json(
    command: HomeMachineCommand
    | ZCalMachineCommand
    | ZConfirmMachineCommand
    | StreamMachineCommand
    | HomeZMachineCommand
    | RecoverMachineCommand,
    service_epoch: str,
) -> JsonObject:
    action = _machine_action(command)
    phase = {
        MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
        MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
        MachineAction.RECOVER: MachinePhase.RECOVERY_REQUIRED,
    }[action]
    session_epoch = str(uuid4())
    binding = MachineApprovalBinding(
        command.execution_id,
        7,
        action,
        command.disposition if type(command) is RecoverMachineCommand else None,
        phase,
        service_epoch,
        session_epoch,
        JOB_ID,
        7,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
    )
    session = None
    if action is not MachineAction.RECOVER:
        session = MachineSessionSnapshot(
            1,
            command.execution_id,
            session_epoch,
            "Idle",
            (192.0, 192.0, 192.0),
            (96.0, 96.0, 3.5),
            (96.0, 96.0, 188.5),
            (96.0, 96.0, 188.5),
            True,
            NOW,
        )
    challenge = issue_approval(
        challenge_id=CHALLENGE_ID,
        binding=binding,
        requester=command.requester,
        automation_principal=KernelMachinePrincipal(AUTOMATION.uid, AUTOMATION.gid),
        operator_principal=KernelMachinePrincipal(OPERATOR.uid, OPERATOR.gid),
        evidence=MachineApprovalEvidenceV1(1, action, phase, session),
        issued_at=NOW + timedelta(seconds=1),
        monotonic_now=1000.0,
        ttl_seconds=60,
    )
    return challenge.to_json()


def _valid_raw_response(command: MachineActionCommand, service_epoch: str) -> JsonObject:
    if type(command) is PrepareMachineCommand:
        return {
            "schema_version": 1,
            "deduplicated": False,
            "admitted": True,
            "execution_id": EXECUTION_ID,
            "job_id": command.job_id,
            "job_revision": command.job_revision,
            "execution_revision": 0,
            "phase": MachinePhase.PREPARING_SESSION.value,
            "action": MachineAction.PREPARE_SESSION.value,
        }
    if command.mode is MachineCommandMode.REQUEST:
        action = _machine_action(command)
        phase = {
            MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
            MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
            MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
            MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
            MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
            MachineAction.RECOVER: MachinePhase.RECOVERY_REQUIRED,
        }[action]
        return {
            "schema_version": 1,
            "deduplicated": False,
            "executed": False,
            "execution_id": command.execution_id,
            "phase": phase.value,
            "challenge": _challenge_json(command, service_epoch),
        }
    if type(command) is RecoverMachineCommand and command.disposition is RecoveryDisposition.RELEASE:
        return {
            "schema_version": 1,
            "deduplicated": False,
            "executed": True,
            "execution_id": command.execution_id,
            "phase": MachinePhase.RECOVERY_REQUIRED.value,
            "released": True,
        }
    action = _machine_action(command)
    phase = {
        MachineAction.HOME: MachinePhase.HOMING,
        MachineAction.ZCAL: MachinePhase.Z_CALIBRATING,
        MachineAction.ZCONFIRM: MachinePhase.Z_CONFIRMING,
        MachineAction.STREAM: MachinePhase.STREAMING,
        MachineAction.HOME_Z: MachinePhase.HOMING_Z,
        MachineAction.RECOVER: MachinePhase.PREPARING_SESSION,
    }[action]
    return {
        "schema_version": 1,
        "deduplicated": False,
        "admitted": True,
        "execution_id": str(uuid4()) if action is MachineAction.RECOVER else command.execution_id,
        "job_id": JOB_ID,
        "job_revision": 7,
        "execution_revision": 8,
        "phase": phase.value,
        "action": action.value,
        "approval_challenge_id": command.challenge_id,
        "challenge_issued_at": NOW.isoformat(),
        "approved_at": (NOW + timedelta(seconds=1)).isoformat(),
        "issuer_pid": 31001,
        "consumer_pid": command.requester.pid,
    }


def _execution_from_raw(response: JsonObject, command: MachineActionCommand) -> JsonObject:
    phase = cast(str, response["phase"])
    recovery = type(command) is RecoverMachineCommand
    post_stream = phase in {"AWAITING_HOME_Z_APPROVAL", "HOMING_Z", "COMPLETED"}
    safe_home = recovery and command.disposition is RecoveryDisposition.SAFE_HOME
    challenge = response.get("challenge")
    binding = challenge.get("binding") if type(challenge) is dict else None
    return {
        "schema_version": 1,
        "execution_id": response["execution_id"],
        "job_id": binding.get("job_id", JOB_ID) if type(binding) is dict else response.get("job_id", JOB_ID),
        "ready_revision": (
            binding.get("ready_revision", 7) if type(binding) is dict else response.get("job_revision", 7)
        ),
        "revision": (
            binding.get("execution_revision", 8) if type(binding) is dict else response.get("execution_revision", 8)
        ),
        "phase": phase,
        "stream_milestone": "STREAM_CONFIRMED" if post_stream or safe_home else "NOT_STARTED",
        "recovery_intent": ("POST_STREAM_SAFE_HOME" if safe_home else ("PRE_STREAM_RESTART" if recovery else None)),
        "retired": response.get("released", False),
    }


def _fallback_execution(command: MachineActionCommand) -> JsonObject:
    if type(command) is PrepareMachineCommand:
        execution_id = EXECUTION_ID
        job_id = command.job_id
        ready_revision = command.job_revision
    else:
        execution_id = command.execution_id
        job_id = JOB_ID
        ready_revision = 7
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "job_id": job_id,
        "ready_revision": ready_revision,
        "revision": 0,
        "phase": "PREPARING_SESSION",
        "stream_milestone": "NOT_STARTED",
        "recovery_intent": None,
        "retired": False,
    }


@dataclass
class FakeCoordinator:
    response: JsonObject

    def __post_init__(self) -> None:
        self.service_epoch = str(uuid4())
        self.commands: list[MachineActionCommand] = []
        self.status_ids: list[str | None] = []
        self.observed_execution: JsonObject | None = None

    def submit(self, command: MachineActionCommand) -> MachineCommandResult:
        self.commands.append(command)
        response = _valid_raw_response(command, self.service_epoch) if not self.response else self.response
        return MachineCommandResult(response, False)

    def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
        result = self.submit(command)
        if type(result) is not MachineCommandResult:
            return result, self.status()  # type: ignore[return-value]
        try:
            self.observed_execution = _execution_from_raw(result.response, command)
        except (KeyError, TypeError):
            self.observed_execution = _fallback_execution(command)
        return result, self.status(cast(str, result.response.get("execution_id")))

    def status(self, execution_id: str | None = None) -> JsonObject:
        self.status_ids.append(execution_id)
        return {
            "schema_version": 1,
            "service_epoch": self.service_epoch,
            "accepting": True,
            "execution": self.observed_execution,
            "progress": None,
            "last_outcome": None,
        }


def _service_status() -> ServiceStatus:
    return ServiceStatus("2.0.0", 1, 3, str(uuid4()), datetime.now(UTC), 1)


def _context(principal: MachinePrincipalConfig, *, pid: int = 1234) -> RequestContext:
    return RequestContext(PeerCredentials(pid=pid, uid=principal.uid, gid=principal.gid), datetime.now(UTC))


def _request(command: str, arguments: JsonObject, *, request_id: str | None = None) -> ProtocolRequest:
    return ProtocolRequest(PROTOCOL_VERSION, command, str(uuid4()) if request_id is None else request_id, arguments)


def _dispatcher(
    coordinator: FakeCoordinator,
    *,
    automation: MachinePrincipalConfig = AUTOMATION,
    operator: MachinePrincipalConfig = OPERATOR,
) -> CommandDispatcher:
    dispatcher = CommandDispatcher(_service_status)
    MachineProtocol(
        dispatcher=dispatcher,
        coordinator=lambda: coordinator,
        automation_principal=automation,
        operator_principal=operator,
    ).register()
    return dispatcher


def _dispatch(
    command: str,
    arguments: JsonObject,
    *,
    principal: MachinePrincipalConfig = AUTOMATION,
    pid: int = 1234,
    coordinator: FakeCoordinator | None = None,
) -> tuple[FakeCoordinator, object]:
    selected = FakeCoordinator({}) if coordinator is None else coordinator
    response = _dispatcher(selected).dispatch(_request(command, arguments), _context(principal, pid=pid))
    return selected, response


def test_status_null_is_preserved_and_never_selects_a_terminal_execution() -> None:
    coordinator, response = _dispatch("machine.status", {"schema_version": 1, "execution_id": None})

    assert response.ok is True
    assert response.data["execution"] is None
    assert coordinator.status_ids == [None]


def test_status_explicit_selector_is_preserved() -> None:
    coordinator, response = _dispatch(
        "machine.status", {"schema_version": 1, "execution_id": EXECUTION_ID}, principal=OPERATOR
    )

    assert response.ok is True
    assert coordinator.status_ids == [EXECUTION_ID]


@pytest.mark.parametrize(
    ("arguments", "expected_field"),
    [
        ({"schema_version": 1}, None),
        ({"schema_version": 1, "execution_id": None, "uid": 1100}, None),
        ({"schema_version": True, "execution_id": None}, "schema_version"),
        ({"schema_version": 1, "execution_id": True}, "execution_id"),
        ({"schema_version": 1, "execution_id": "x" * 4097}, "execution_id"),
        ({"schema_version": 1, "execution_id": "not-a-uuid"}, "execution_id"),
    ],
)
def test_status_rejects_non_exact_or_invalid_fields(arguments: JsonObject, expected_field: str | None) -> None:
    coordinator, response = _dispatch("machine.status", arguments)

    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_COMMAND_ARGUMENTS_INVALID"
    assert response.error.details == ({} if expected_field is None else {"field": expected_field})
    assert coordinator.status_ids == []


def test_prepare_derives_automation_principal_only_from_peer_context() -> None:
    coordinator, response = _dispatch(
        "machine.prepare",
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7},
        principal=AUTOMATION,
        pid=4567,
    )

    assert response.ok is True
    command = cast(PrepareMachineCommand, coordinator.commands[0])
    assert type(command) is PrepareMachineCommand
    assert (command.requester.uid, command.requester.gid, command.requester.pid) == (1100, 1100, 4567)
    assert command.job_id == JOB_ID
    assert command.job_revision == 7


@pytest.mark.parametrize(
    "arguments",
    [
        {"schema_version": 1, "job_id": JOB_ID},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "pid": 1},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "uid": 1},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "gid": 1},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "service_epoch": str(uuid4())},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "machine_session_epoch": str(uuid4())},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "digest": "0" * 64},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "phase": "PREPARING_SESSION"},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7, "action": "HOME"},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": True},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 1.0},
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 0},
        {"schema_version": 1, "job_id": "bad", "job_revision": 1},
        {"schema_version": 1, "job_id": "x" * 4097, "job_revision": 1},
    ],
)
def test_prepare_rejects_missing_unknown_spoofed_and_invalid_fields(arguments: JsonObject) -> None:
    coordinator, response = _dispatch("machine.prepare", arguments)
    assert response.ok is False
    assert coordinator.commands == []


@pytest.mark.parametrize(
    ("command_name", "command_type"),
    [
        ("machine.home", HomeMachineCommand),
        ("machine.zcal", ZCalMachineCommand),
        ("machine.zconfirm", ZConfirmMachineCommand),
        ("machine.stream", StreamMachineCommand),
        ("machine.home-z", HomeZMachineCommand),
    ],
)
@pytest.mark.parametrize(
    ("principal", "pid", "mode", "challenge", "expected_mode"),
    [
        (AUTOMATION, 31001, "request", None, MachineCommandMode.REQUEST),
        (OPERATOR, 41001, "request", None, MachineCommandMode.REQUEST),
        (OPERATOR, 41002, "execute", CHALLENGE_ID, MachineCommandMode.EXECUTE),
    ],
)
def test_motion_commands_accept_authorized_request_and_changed_pid_operator_consume(
    command_name: str,
    command_type: type[MachineActionCommand],
    principal: MachinePrincipalConfig,
    pid: int,
    mode: str,
    challenge: str | None,
    expected_mode: MachineCommandMode,
) -> None:
    coordinator, response = _dispatch(
        command_name,
        {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "mode": mode,
            "challenge_id": challenge,
        },
        principal=principal,
        pid=pid,
    )

    assert response.ok is True
    actual = coordinator.commands[0]
    assert type(actual) is command_type
    assert actual.mode is expected_mode  # type: ignore[union-attr]
    assert actual.challenge_id == challenge  # type: ignore[union-attr]
    assert actual.requester.pid == pid


@pytest.mark.parametrize(
    "arguments",
    [
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request"},
        {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "mode": "request",
            "challenge_id": None,
            "operator_uid": 2200,
        },
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": True, "challenge_id": None},
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "REQUEST", "challenge_id": None},
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": CHALLENGE_ID},
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "execute", "challenge_id": None},
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "execute", "challenge_id": "bad"},
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "execute", "challenge_id": "x" * 4097},
    ],
)
def test_motion_rejects_non_exact_mode_challenge_and_spoof_fields(arguments: JsonObject) -> None:
    coordinator, response = _dispatch("machine.home", arguments)
    assert response.ok is False
    assert coordinator.commands == []


def test_automation_and_wrong_principal_cannot_consume() -> None:
    arguments: JsonObject = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "mode": "execute",
        "challenge_id": CHALLENGE_ID,
    }
    for principal in (AUTOMATION, MachinePrincipalConfig(3300, 3300)):
        coordinator, response = _dispatch("machine.home", arguments, principal=principal)
        assert response.ok is False
        assert response.error is not None and response.error.code == "MACHINE_PRINCIPAL_DENIED"
        assert coordinator.commands == []


def test_automation_request_then_operator_consume_and_operator_pid_change_are_preserved() -> None:
    arguments_request: JsonObject = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "mode": "request",
        "challenge_id": None,
    }
    arguments_execute: JsonObject = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "mode": "execute",
        "challenge_id": CHALLENGE_ID,
    }
    for issuer, issuer_pid in ((AUTOMATION, 31001), (OPERATOR, 41001)):
        coordinator = FakeCoordinator({})
        dispatcher = _dispatcher(coordinator)
        requested = dispatcher.dispatch(
            _request("machine.home", arguments_request),
            _context(issuer, pid=issuer_pid),
        )
        consumed = dispatcher.dispatch(
            _request("machine.home", arguments_execute),
            _context(OPERATOR, pid=41002),
        )
        assert requested.ok is True
        assert consumed.ok is True
        assert [command.requester.pid for command in coordinator.commands] == [issuer_pid, 41002]


def test_c15_durable_replay_preserves_historical_pid_for_same_stable_principal() -> None:
    class DurableReplay(FakeCoordinator):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.stored: JsonObject | None = None

        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            if self.stored is None:
                self.stored = _valid_raw_response(command, self.service_epoch)
                return MachineCommandResult(self.stored, False)
            replay = dict(self.stored)
            replay["deduplicated"] = True
            return MachineCommandResult(replay, True)

    request_arguments: JsonObject = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "mode": "request",
        "challenge_id": None,
    }
    request_coordinator = DurableReplay({})
    request_dispatcher = _dispatcher(request_coordinator)
    request_id = str(uuid4())
    first_request = request_dispatcher.dispatch(
        _request("machine.home", request_arguments, request_id=request_id),
        _context(AUTOMATION, pid=31001),
    )
    replayed_request = request_dispatcher.dispatch(
        _request("machine.home", request_arguments, request_id=request_id),
        _context(AUTOMATION, pid=31999),
    )
    assert first_request.ok is True
    assert replayed_request.ok is True
    assert replayed_request.data["deduplicated"] is True
    challenge = cast(dict[str, object], replayed_request.data["challenge"])
    requester = cast(dict[str, object], challenge["requester"])
    assert requester == {"uid": AUTOMATION.uid, "gid": AUTOMATION.gid, "pid": 31001}

    execute_arguments: JsonObject = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "mode": "execute",
        "challenge_id": CHALLENGE_ID,
    }
    execute_coordinator = DurableReplay({})
    execute_dispatcher = _dispatcher(execute_coordinator)
    execute_id = str(uuid4())
    first_execute = execute_dispatcher.dispatch(
        _request("machine.home", execute_arguments, request_id=execute_id),
        _context(OPERATOR, pid=41002),
    )
    replayed_execute = execute_dispatcher.dispatch(
        _request("machine.home", execute_arguments, request_id=execute_id),
        _context(OPERATOR, pid=41999),
    )
    assert first_execute.ok is True
    assert replayed_execute.ok is True
    assert replayed_execute.data["deduplicated"] is True


def test_c15_durable_challenge_replay_never_crosses_stable_principals() -> None:
    class CrossPrincipalReplay(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            historical = HomeMachineCommand(
                command.request_id,
                AuthenticatedMachinePrincipal(AUTOMATION.uid, AUTOMATION.gid, 31001),
                EXECUTION_ID,
                MachineCommandMode.REQUEST,
                None,
            )
            response = _valid_raw_response(historical, self.service_epoch)
            response["deduplicated"] = True
            return MachineCommandResult(response, True)

    response = _dispatcher(CrossPrincipalReplay({})).dispatch(
        _request(
            "machine.home",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "request",
                "challenge_id": None,
            },
        ),
        _context(OPERATOR, pid=41001),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_wrong_principal_cannot_request_or_query() -> None:
    wrong = MachinePrincipalConfig(3300, 3300)
    for command, arguments in (
        ("machine.status", {"schema_version": 1, "execution_id": None}),
        (
            "machine.home",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
    ):
        coordinator, response = _dispatch(command, arguments, principal=wrong)
        assert response.ok is False
        assert coordinator.commands == []
        assert coordinator.status_ids == []


def test_equal_roles_are_rejected_before_handler_registration() -> None:
    dispatcher = CommandDispatcher(_service_status)
    with pytest.raises(DrawingMachineError) as captured:
        MachineProtocol(
            dispatcher=dispatcher,
            coordinator=lambda: FakeCoordinator({}),
            automation_principal=AUTOMATION,
            operator_principal=MachinePrincipalConfig(AUTOMATION.uid, 2200),
        )
    assert captured.value.payload.code == "MACHINE_APPROVAL_ROLES_NOT_DISJOINT"


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("release", RecoveryDisposition.RELEASE),
        ("restart-sequence", RecoveryDisposition.RESTART_SEQUENCE),
        ("safe-home", RecoveryDisposition.SAFE_HOME),
    ],
)
def test_recover_binds_exact_disposition_in_request_and_execute(literal: str, expected: RecoveryDisposition) -> None:
    for principal, pid, mode, challenge in (
        (AUTOMATION, 31001, "request", None),
        (OPERATOR, 41002, "execute", CHALLENGE_ID),
    ):
        coordinator, response = _dispatch(
            "machine.recover",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": mode,
                "challenge_id": challenge,
                "disposition": literal,
            },
            principal=principal,
            pid=pid,
        )
        assert response.ok is True
        command = cast(RecoverMachineCommand, coordinator.commands[0])
        assert type(command) is RecoverMachineCommand
        assert command.disposition is expected
        assert command.requester.pid == pid


@pytest.mark.parametrize("disposition", [None, True, "RELEASE", "restart_sequence", "unknown"])
def test_recover_rejects_invalid_dispositions(disposition: object) -> None:
    coordinator, response = _dispatch(
        "machine.recover",
        {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "mode": "request",
            "challenge_id": None,
            "disposition": disposition,
        },
    )
    assert response.ok is False
    assert coordinator.commands == []


@pytest.mark.parametrize(
    "frame",
    [
        (
            b'{"protocol_version":1,"command":"machine.status","request_id":"a",'
            b'"arguments":{"schema_version":1,"execution_id":null,"execution_id":"spoof"}}\n'
        ),
        (
            b'{"protocol_version":1,"command":"machine.prepare","request_id":"a",'
            b'"arguments":{"schema_version":1,"job_id":"x","job_revision":NaN}}\n'
        ),
        b"{" + b" " * MAX_MESSAGE_BYTES + b"}\n",
    ],
)
def test_machine_frames_reject_duplicate_nonfinite_and_oversize(frame: bytes) -> None:
    with pytest.raises(DrawingMachineError) as captured:
        decode_request(frame)
    assert captured.value.payload.code in {"PROTOCOL_INVALID", "PROTOCOL_MESSAGE_TOO_LARGE"}


def test_machine_response_remains_plain_json() -> None:
    coordinator, response = _dispatch(
        "machine.home",
        {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
    )
    assert response.ok is True
    assert set(response.data) == {
        "schema_version",
        "deduplicated",
        "service_epoch",
        "accepting",
        "execution",
        "progress",
        "last_outcome",
        "challenge",
    }
    assert json.loads(json.dumps(response.data, allow_nan=False)) == response.data
    assert response.data["challenge"] is not None
    assert coordinator.commands


def test_protocol_constructor_rejects_invalid_dependencies_and_principals() -> None:
    dispatcher = CommandDispatcher(_service_status)
    invalid_cases = (
        (object(), lambda: FakeCoordinator({}), AUTOMATION, TypeError),
        (dispatcher, object(), AUTOMATION, TypeError),
        (dispatcher, lambda: FakeCoordinator({}), object(), TypeError),
        (dispatcher, lambda: FakeCoordinator({}), MachinePrincipalConfig(True, 1100), DrawingMachineError),
    )
    for invalid_dispatcher, invalid_coordinator, invalid_automation, expected_error in invalid_cases:
        with pytest.raises(expected_error):
            MachineProtocol(
                dispatcher=invalid_dispatcher,  # type: ignore[arg-type]
                coordinator=invalid_coordinator,  # type: ignore[arg-type]
                automation_principal=invalid_automation,  # type: ignore[arg-type]
                operator_principal=OPERATOR,
            )


def test_prepare_rejects_operator_before_coordinator_submission() -> None:
    coordinator, response = _dispatch(
        "machine.prepare",
        {"schema_version": 1, "job_id": JOB_ID, "job_revision": 1},
        principal=OPERATOR,
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PRINCIPAL_DENIED"
    assert coordinator.commands == []


def test_invalid_request_id_and_non_uuid4_identifier_are_rejected() -> None:
    coordinator = FakeCoordinator({})
    dispatcher = _dispatcher(coordinator)
    empty = dispatcher.dispatch(
        _request("machine.status", {"schema_version": 1, "execution_id": None}, request_id=""),
        _context(AUTOMATION),
    )
    non_v4 = dispatcher.dispatch(
        _request(
            "machine.status",
            {"schema_version": 1, "execution_id": "00000000-0000-1000-8000-000000000000"},
        ),
        _context(AUTOMATION),
    )
    assert empty.ok is False
    assert non_v4.ok is False
    assert coordinator.status_ids == []


def test_invalid_context_and_kernel_peer_values_are_denied() -> None:
    coordinator = FakeCoordinator({})
    dispatcher = _dispatcher(coordinator)
    request = _request("machine.status", {"schema_version": 1, "execution_id": None})
    wrong_context = dispatcher.dispatch(request, cast(RequestContext, object()))
    invalid_peer = dispatcher.dispatch(
        request,
        RequestContext(PeerCredentials(pid=0, uid=AUTOMATION.uid, gid=AUTOMATION.gid), datetime.now(UTC)),
    )
    assert wrong_context.ok is False
    assert invalid_peer.ok is False
    assert coordinator.status_ids == []


def test_invalid_coordinator_status_and_result_are_closed_internal_failures() -> None:
    class InvalidStatusCoordinator(FakeCoordinator):
        def status(self, execution_id: str | None = None) -> JsonObject:
            del execution_id
            return cast(JsonObject, [])

    class InvalidResultCoordinator(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            del command
            return cast(MachineCommandResult, object())

    status_response = _dispatcher(InvalidStatusCoordinator({})).dispatch(
        _request("machine.status", {"schema_version": 1, "execution_id": None}),
        _context(AUTOMATION),
    )
    result_response = _dispatcher(InvalidResultCoordinator({})).dispatch(
        _request("machine.prepare", {"schema_version": 1, "job_id": JOB_ID, "job_revision": 1}),
        _context(AUTOMATION),
    )
    assert status_response.ok is False
    assert status_response.error is not None and status_response.error.code == "MACHINE_PROTOCOL_INVALID"
    assert result_response.ok is False
    assert result_response.error is not None and result_response.error.code == "MACHINE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    "malformed",
    [
        {"schema_version": 1, "service_epoch": str(uuid4()), "execution": None},
        {
            "schema_version": 1,
            "service_epoch": str(uuid4()),
            "accepting": True,
            "execution": None,
            "unexpected": 1,
        },
        {"schema_version": True, "service_epoch": str(uuid4()), "accepting": True, "execution": None},
        {"schema_version": 1, "service_epoch": "not-a-uuid", "accepting": True, "execution": None},
        {"schema_version": 1, "service_epoch": str(uuid4()), "accepting": 1, "execution": None},
        {
            "schema_version": 1,
            "service_epoch": str(uuid4()),
            "accepting": True,
            "execution": {
                "schema_version": 1,
                "execution_id": "not-a-uuid",
                "job_id": JOB_ID,
                "ready_revision": 7,
                "revision": True,
                "phase": "STREAMING",
                "stream_milestone": "NOT_STARTED",
                "recovery_intent": None,
                "retired": False,
            },
        },
        {
            "schema_version": 1,
            "service_epoch": str(uuid4()),
            "accepting": True,
            "execution": {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "job_id": JOB_ID,
                "ready_revision": 7,
                "revision": 8,
                "phase": "STREAMING",
                "stream_milestone": "NOT_STARTED",
                "recovery_intent": None,
                "retired": False,
                "percent": float("nan"),
            },
        },
    ],
)
def test_f1_status_rejects_every_nonclosed_coordinator_projection(malformed: JsonObject) -> None:
    coordinator = FakeCoordinator({})
    coordinator.status = lambda _execution_id=None: malformed  # type: ignore[method-assign]
    response = _dispatcher(coordinator).dispatch(
        _request("machine.status", {"schema_version": 1, "execution_id": None}),
        _context(AUTOMATION),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    ("command_name", "arguments", "malformed"),
    [
        (
            "machine.prepare",
            {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7},
            {"schema_version": 1, "deduplicated": False},
        ),
        (
            "machine.prepare",
            {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7},
            {
                "schema_version": 1,
                "deduplicated": False,
                "admitted": True,
                "execution_id": EXECUTION_ID,
                "job_id": JOB_ID,
                "job_revision": True,
                "execution_revision": 0,
                "phase": "PREPARING_SESSION",
                "action": "PREPARE_SESSION",
            },
        ),
        (
            "machine.home",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
            {
                "schema_version": 1,
                "deduplicated": False,
                "executed": False,
                "execution_id": EXECUTION_ID,
                "phase": "AWAITING_HOME_APPROVAL",
                "challenge": {"challenge_id": CHALLENGE_ID},
            },
        ),
        (
            "machine.home",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "execute",
                "challenge_id": CHALLENGE_ID,
            },
            {
                "schema_version": 1,
                "deduplicated": False,
                "admitted": True,
                "execution_id": EXECUTION_ID,
                "job_id": JOB_ID,
                "job_revision": 7,
                "execution_revision": 8,
                "phase": "HOMING",
                "action": "HOME",
                "approval_challenge_id": CHALLENGE_ID,
                "challenge_issued_at": "not-a-time",
                "approved_at": "not-a-time",
                "issuer_pid": 31001,
                "consumer_pid": 41002,
            },
        ),
        (
            "machine.recover",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "execute",
                "challenge_id": CHALLENGE_ID,
                "disposition": "release",
            },
            {
                "schema_version": 1,
                "deduplicated": False,
                "executed": True,
                "execution_id": EXECUTION_ID,
                "phase": "RECOVERY_REQUIRED",
                "released": 1,
            },
        ),
    ],
)
def test_f1_command_results_reject_every_nonclosed_variant(
    command_name: str,
    arguments: JsonObject,
    malformed: JsonObject,
) -> None:
    response = _dispatcher(FakeCoordinator(malformed)).dispatch(
        _request(command_name, arguments),
        _context(OPERATOR if arguments.get("mode") == "execute" else AUTOMATION),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_f1_typed_deduplication_and_observation_authority_are_cross_bound() -> None:
    class DeduplicationMismatch(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            return MachineCommandResult(_valid_raw_response(command, self.service_epoch), True)

    class ObservationMismatch(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result, observation = super().submit_observed(command)
            execution = cast(JsonObject, observation["execution"])
            execution["phase"] = "HOMING"
            return result, observation

    arguments: JsonObject = {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7}
    for coordinator in (DeduplicationMismatch({}), ObservationMismatch({})):
        response = _dispatcher(coordinator).dispatch(
            _request("machine.prepare", arguments),
            _context(AUTOMATION),
        )
        assert response.ok is False
        assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_f1_challenge_authority_is_cross_bound_to_observed_service_and_execution() -> None:
    class ChallengeBindingMismatch(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            response = _valid_raw_response(command, self.service_epoch)
            challenge = cast(JsonObject, response["challenge"])
            binding = cast(JsonObject, challenge["binding"])
            binding["service_epoch"] = str(uuid4())
            return MachineCommandResult(response, False)

    response = _dispatcher(ChallengeBindingMismatch({})).dispatch(
        _request(
            "machine.home",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
        _context(AUTOMATION),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_f1_status_accepts_closed_progress_and_terminal_outcome_variants() -> None:
    class ProjectionCoordinator(FakeCoordinator):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.progress: JsonObject | None = None
            self.outcome: JsonObject | None = None

        def status(self, execution_id: str | None = None) -> JsonObject:
            result = super().status(execution_id)
            result["progress"] = self.progress
            result["last_outcome"] = self.outcome
            return result

    coordinator = ProjectionCoordinator({})
    execution = _fallback_execution(
        PrepareMachineCommand("request", AuthenticatedMachinePrincipal(1100, 1100, 1234), JOB_ID, 7)
    )
    execution["revision"] = 8
    coordinator.observed_execution = execution
    coordinator.progress = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "execution_revision": 8,
        "total": 0,
        "acknowledged": 0,
        "ok_count": 0,
        "error_count": 0,
        "percent": 100.0,
        "last_source_line": None,
        "last_command": None,
        "last_event_at": NOW.isoformat(),
        "failure": None,
    }
    coordinator.outcome = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "job_id": JOB_ID,
        "execution_revision": 8,
        "kind": "COMPLETED",
        "recovery_evidence": None,
        "occurred_at": NOW.isoformat(),
    }
    _, completed = _dispatch(
        "machine.status",
        {"schema_version": 1, "execution_id": None},
        coordinator=coordinator,
    )
    assert completed.ok is True
    assert completed.data["progress"] == coordinator.progress
    assert completed.data["last_outcome"] == coordinator.outcome

    execution["phase"] = MachinePhase.STREAMING.value
    execution["stream_milestone"] = "FIRST_WRITE_POSSIBLE"
    coordinator.progress = {
        **coordinator.progress,
        "total": 2,
        "acknowledged": 1,
        "ok_count": 1,
        "percent": 50.0,
        "last_source_line": 1,
        "last_command": "G1 X1",
    }
    coordinator.outcome = None
    _, streaming = _dispatch(
        "machine.status",
        {"schema_version": 1, "execution_id": EXECUTION_ID},
        coordinator=coordinator,
    )
    assert streaming.ok is True

    failure = MachineFailureEvidenceV1(1, "CONTROLLER_ERROR", "controller error", "Alarm", True)
    recovery = MachineRecoveryEvidence(
        1,
        EXECUTION_ID,
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "CONTROLLER_ERROR",
        failure,
        NOW,
    )
    execution["phase"] = MachinePhase.RECOVERY_REQUIRED.value
    execution["stream_milestone"] = "NOT_STARTED"
    execution["recovery_intent"] = RecoveryIntent.PRE_STREAM_RESTART.value
    coordinator.progress = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "execution_revision": 8,
        "total": 2,
        "acknowledged": 0,
        "ok_count": 0,
        "error_count": 1,
        "percent": 0.0,
        "last_source_line": 1,
        "last_command": "G1 X1",
        "last_event_at": NOW.isoformat(),
        "failure": failure.to_json(),
    }
    coordinator.outcome = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "job_id": JOB_ID,
        "execution_revision": 8,
        "kind": "RECOVERY_REQUIRED",
        "recovery_evidence": recovery.to_json(),
        "occurred_at": NOW.isoformat(),
    }
    _, recovered = _dispatch(
        "machine.status",
        {"schema_version": 1, "execution_id": EXECUTION_ID},
        coordinator=coordinator,
    )
    assert recovered.ok is True
    assert recovered.data["progress"] == coordinator.progress
    assert recovered.data["last_outcome"] == coordinator.outcome


def test_f1_action_rejects_invalid_or_missing_observed_projection() -> None:
    class InvalidObservation(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            return self.submit(command), []  # type: ignore[return-value]

    class MissingExecution(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            return result, self.status()

    arguments: JsonObject = {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7}
    for coordinator in (InvalidObservation({}), MissingExecution({})):
        response = _dispatcher(coordinator).dispatch(_request("machine.prepare", arguments), _context(AUTOMATION))
        assert response.ok is False
        assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    ("command_name", "arguments", "principal", "next_phase", "milestone", "intent", "retired"),
    [
        (
            "machine.prepare",
            {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7},
            AUTOMATION,
            "AWAITING_HOME_APPROVAL",
            "NOT_STARTED",
            None,
            False,
        ),
        *[
            (
                name,
                {
                    "schema_version": 1,
                    "execution_id": EXECUTION_ID,
                    "mode": mode,
                    "challenge_id": None if mode == "request" else CHALLENGE_ID,
                },
                AUTOMATION if mode == "request" else OPERATOR,
                next_phase,
                milestone,
                None,
                False,
            )
            for name, mode, next_phase, milestone in (
                ("machine.home", "request", "HOMING", "NOT_STARTED"),
                ("machine.home", "execute", "AWAITING_ZCAL_APPROVAL", "NOT_STARTED"),
                ("machine.zcal", "request", "Z_CALIBRATING", "NOT_STARTED"),
                ("machine.zcal", "execute", "AWAITING_ZCONFIRM_APPROVAL", "NOT_STARTED"),
                ("machine.zconfirm", "request", "Z_CONFIRMING", "NOT_STARTED"),
                ("machine.zconfirm", "execute", "AWAITING_STREAM_APPROVAL", "NOT_STARTED"),
                ("machine.stream", "request", "STREAMING", "NOT_STARTED"),
                ("machine.stream", "execute", "AWAITING_HOME_Z_APPROVAL", "STREAM_CONFIRMED"),
                ("machine.home-z", "request", "HOMING_Z", "STREAM_CONFIRMED"),
                ("machine.home-z", "execute", "COMPLETED", "STREAM_CONFIRMED"),
            )
        ],
        *[
            (
                "machine.recover",
                {
                    "schema_version": 1,
                    "execution_id": EXECUTION_ID,
                    "mode": mode,
                    "challenge_id": None if mode == "request" else CHALLENGE_ID,
                    "disposition": disposition,
                },
                AUTOMATION if mode == "request" else OPERATOR,
                next_phase,
                milestone,
                intent,
                retired,
            )
            for mode, disposition, next_phase, milestone, intent, retired in (
                ("request", "release", "RECOVERY_REQUIRED", "NOT_STARTED", "PRE_STREAM_RESTART", False),
                ("request", "safe-home", "RECOVERY_REQUIRED", "STREAM_CONFIRMED", "POST_STREAM_SAFE_HOME", False),
                ("execute", "release", "RECOVERY_REQUIRED", "NOT_STARTED", "PRE_STREAM_RESTART", True),
                ("execute", "restart-sequence", "AWAITING_HOME_APPROVAL", "NOT_STARTED", "PRE_STREAM_RESTART", False),
                ("execute", "safe-home", "AWAITING_HOME_APPROVAL", "STREAM_CONFIRMED", "POST_STREAM_SAFE_HOME", False),
            )
        ],
    ],
)
def test_n1_stable_replay_normalizes_to_forward_current_observation_without_stale_challenge(
    command_name: str,
    arguments: JsonObject,
    principal: MachinePrincipalConfig,
    next_phase: str,
    milestone: str,
    intent: str | None,
    retired: bool,
) -> None:
    class StableReplay(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            response = _valid_raw_response(command, self.service_epoch)
            response["deduplicated"] = True
            return MachineCommandResult(response, True)

        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["revision"] = cast(int, execution["revision"]) + 1
            execution["phase"] = next_phase
            execution["stream_milestone"] = milestone
            execution["recovery_intent"] = intent
            execution["retired"] = retired
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    response = _dispatcher(StableReplay({})).dispatch(
        _request(command_name, arguments),
        _context(principal),
    )
    assert response.ok is True
    assert response.data["deduplicated"] is True
    execution = response.data["execution"]
    assert isinstance(execution, dict) and execution["phase"] == next_phase
    assert response.data["challenge"] is None


def test_n2_recover_disposition_must_match_observed_recovery_intent() -> None:
    class HostileRecoveryIntent(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["stream_milestone"] = "NOT_STARTED"
            execution["recovery_intent"] = "PRE_STREAM_RESTART"
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    response = _dispatcher(HostileRecoveryIntent({})).dispatch(
        _request(
            "machine.recover",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "request",
                "challenge_id": None,
                "disposition": "safe-home",
            },
        ),
        _context(AUTOMATION),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    ("revision", "phase"),
    [
        (7, "HOMING"),
        (8, "AWAITING_HOME_APPROVAL"),
        (9, "HOMING"),
        (9, "AWAITING_HOME_APPROVAL"),
    ],
)
def test_n1_replay_rejects_revision_regression_equal_conflict_and_backward_phase(
    revision: int,
    phase: str,
) -> None:
    class RegressiveReplay(FakeCoordinator):
        def submit(self, command: MachineActionCommand) -> MachineCommandResult:
            self.commands.append(command)
            response = _valid_raw_response(command, self.service_epoch)
            response["deduplicated"] = True
            return MachineCommandResult(response, True)

        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["revision"] = revision
            execution["phase"] = phase
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    response = _dispatcher(RegressiveReplay({})).dispatch(
        _request(
            "machine.home",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "execute",
                "challenge_id": CHALLENGE_ID,
            },
        ),
        _context(OPERATOR),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_n3_post_stream_safe_home_never_accepts_home_z_authority() -> None:
    class PostStreamHomeZ(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["stream_milestone"] = "STREAM_CONFIRMED"
            execution["recovery_intent"] = "POST_STREAM_SAFE_HOME"
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    response = _dispatcher(PostStreamHomeZ({})).dispatch(
        _request(
            "machine.home-z",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
        _context(AUTOMATION),
    )
    assert response.ok is False
    assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"


def test_n3_post_stream_safe_home_accepts_home_challenge_and_completed_status() -> None:
    class PostStreamHome(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["stream_milestone"] = "STREAM_CONFIRMED"
            execution["recovery_intent"] = "POST_STREAM_SAFE_HOME"
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    coordinator = PostStreamHome({})
    challenge = _dispatcher(coordinator).dispatch(
        _request(
            "machine.home",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
        _context(AUTOMATION),
    )
    assert challenge.ok is True and challenge.data["challenge"] is not None

    execution = cast(JsonObject, coordinator.observed_execution)
    execution["revision"] = cast(int, execution["revision"]) + 1
    execution["phase"] = "COMPLETED"
    status = _dispatcher(coordinator).dispatch(
        _request("machine.status", {"schema_version": 1, "execution_id": EXECUTION_ID}),
        _context(OPERATOR),
    )
    assert status.ok is True

    class PreStreamHomeZ(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            home_z_execution = _execution_from_raw(result.response, command)
            home_z_execution["recovery_intent"] = "PRE_STREAM_RESTART"
            self.observed_execution = home_z_execution
            return result, self.status(cast(str, home_z_execution["execution_id"]))

    home_z = _dispatcher(PreStreamHomeZ({})).dispatch(
        _request(
            "machine.home-z",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
        _context(AUTOMATION),
    )
    assert home_z.ok is True and home_z.data["challenge"] is not None


@pytest.mark.parametrize(
    ("intent", "milestone", "disposition", "allowed"),
    [
        ("PRE_STREAM_RESTART", "NOT_STARTED", "release", True),
        ("PRE_STREAM_RESTART", "NOT_STARTED", "restart-sequence", True),
        ("PRE_STREAM_RESTART", "NOT_STARTED", "safe-home", False),
        ("STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", "release", True),
        ("STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", "restart-sequence", False),
        ("STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", "safe-home", False),
        ("POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", "release", True),
        ("POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", "restart-sequence", False),
        ("POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", "safe-home", True),
    ],
)
def test_n2_recovery_intent_disposition_matrix_remains_closed(
    intent: str,
    milestone: str,
    disposition: str,
    allowed: bool,
) -> None:
    class RecoveryOwner(FakeCoordinator):
        def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
            result = self.submit(command)
            execution = _execution_from_raw(result.response, command)
            execution["stream_milestone"] = milestone
            execution["recovery_intent"] = intent
            self.observed_execution = execution
            return result, self.status(cast(str, execution["execution_id"]))

    response = _dispatcher(RecoveryOwner({})).dispatch(
        _request(
            "machine.recover",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "request",
                "challenge_id": None,
                "disposition": disposition,
            },
        ),
        _context(AUTOMATION),
    )
    assert response.ok is allowed
    if not allowed:
        assert response.error is not None and response.error.code == "MACHINE_PROTOCOL_INVALID"
