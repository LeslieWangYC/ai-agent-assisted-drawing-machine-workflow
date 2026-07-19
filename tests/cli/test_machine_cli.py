from __future__ import annotations

import argparse
import ast
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest

from drawingmachine.cli.client import ServiceClient
from drawingmachine.cli.machine import (
    machine_action,
    machine_prepare,
    machine_recover,
    machine_status,
)
from drawingmachine.cli.main import main
from drawingmachine.cli.parser import build_parser
from drawingmachine.cli.render import render_text
from drawingmachine.domain.machine import ApprovalPurpose
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse

EXECUTION_ID = str(uuid4())
JOB_ID = str(uuid4())
CHALLENGE_ID = str(uuid4())


def _challenge_machine_data(command: str, purpose: ApprovalPurpose) -> dict[str, object]:
    action, phase = {
        "machine.home": ("HOME", "AWAITING_HOME_APPROVAL"),
        "machine.zcal": ("ZCAL", "AWAITING_ZCAL_APPROVAL"),
        "machine.zconfirm": ("ZCONFIRM", "AWAITING_ZCONFIRM_APPROVAL"),
        "machine.stream": ("STREAM", "AWAITING_STREAM_APPROVAL"),
        "machine.home-z": ("HOME_Z", "AWAITING_HOME_Z_APPROVAL"),
        "machine.recover": ("RECOVER", "RECOVERY_REQUIRED"),
    }[command]
    service_epoch = str(uuid4())
    session_epoch = str(uuid4())
    milestone = "STREAM_CONFIRMED" if action == "HOME_Z" else "NOT_STARTED"
    session = None
    if action != "RECOVER":
        session = {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "machine_session_epoch": session_epoch,
            "controller_state": "Idle",
            "mpos": [192.0, 192.0, 192.0],
            "wpos": [96.0, 96.0, 3.5],
            "wco": [96.0, 96.0, 188.5],
            "g54": [96.0, 96.0, 188.5],
            "stabilized": True,
            "observed_at": "2026-07-13T09:59:59+00:00",
        }
    challenge = {
        "schema_version": 1,
        "challenge_id": CHALLENGE_ID,
        "binding": {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "execution_revision": 8,
            "action": action,
            "recovery_disposition": "RELEASE" if action == "RECOVER" else None,
            "required_prior_phase": phase,
            "service_epoch": service_epoch,
            "machine_session_epoch": session_epoch,
            "job_id": JOB_ID,
            "ready_revision": 7,
            "application_digest": "a" * 64,
            "machine_digest": "b" * 64,
            "provider_digest": "c" * 64,
            "gcode_sha256": "d" * 64,
        },
        "requester": {"uid": 1100, "gid": 1100, "pid": 31001},
        "operator_principal": {"uid": 2200, "gid": 2200},
        "purpose": purpose.value,
        "evidence": {
            "schema_version": 1,
            "action": action,
            "required_prior_phase": phase,
            "session": session,
        },
        "issued_at": "2026-07-13T10:00:00+00:00",
        "issued_monotonic": 1000.0,
        "monotonic_deadline": 1060.0,
        "expires_at": "2026-07-13T10:01:00+00:00",
        "status": "PENDING",
        "status_changed_at": None,
        "consumer": None,
    }
    return {
        "schema_version": 1,
        "deduplicated": False,
        "service_epoch": service_epoch,
        "accepting": True,
        "execution": {
            "schema_version": 1,
            "execution_id": EXECUTION_ID,
            "job_id": JOB_ID,
            "ready_revision": 7,
            "revision": 8,
            "phase": phase,
            "stream_milestone": milestone,
            "recovery_intent": "PRE_STREAM_RESTART" if action == "RECOVER" else None,
            "retired": False,
        },
        "progress": None,
        "last_outcome": None,
        "challenge": challenge,
    }


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


def _response(command: str, data: dict[str, object]) -> ProtocolResponse:
    return ProtocolResponse(PROTOCOL_VERSION, SCHEMA_VERSION, True, command, str(uuid4()), data, None)


@pytest.mark.parametrize(
    "arguments",
    [
        ["machine", "--help"],
        ["machine", "status", "--help"],
        ["machine", "prepare", "--help"],
        ["machine", "home", "--help"],
        ["machine", "zcal", "--help"],
        ["machine", "zconfirm", "--help"],
        ["machine", "stream", "--help"],
        ["machine", "home-z", "--help"],
        ["machine", "recover", "--help"],
    ],
)
def test_all_machine_help_shapes_exit_cleanly(arguments: list[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as captured:
        parser.parse_args(arguments)
    assert captured.value.code == 0


def test_parser_freezes_machine_cli_shapes() -> None:
    status = build_parser().parse_args(["machine", "status"])
    assert (status.command_name, status.execution_id, status.handler) == ("machine.status", None, machine_status)

    prepare = build_parser().parse_args(["machine", "prepare", JOB_ID, "--job-revision", "7"])
    assert (prepare.command_name, prepare.job_id, prepare.job_revision, prepare.handler) == (
        "machine.prepare",
        JOB_ID,
        7,
        machine_prepare,
    )

    for name in ("home", "zcal", "zconfirm", "stream", "home-z"):
        request = build_parser().parse_args(["machine", name, EXECUTION_ID])
        assert (request.execution_id, request.approve, request.handler) == (EXECUTION_ID, None, machine_action)
        execute = build_parser().parse_args(["machine", name, EXECUTION_ID, "--approve", CHALLENGE_ID])
        assert execute.approve == CHALLENGE_ID

    recover = build_parser().parse_args(
        ["machine", "recover", EXECUTION_ID, "--disposition", "safe-home", "--approve", CHALLENGE_ID]
    )
    assert (recover.command_name, recover.disposition, recover.approve, recover.handler) == (
        "machine.recover",
        "safe-home",
        CHALLENGE_ID,
        machine_recover,
    )


def test_cli_builds_exact_status_and_prepare_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(_self: ServiceClient, command: str, arguments: dict[str, object], **_kwargs: object) -> ProtocolResponse:
        calls.append((command, arguments))
        return _response(command, {"schema_version": 1})

    monkeypatch.setattr(ServiceClient, "call", call)
    machine_status(argparse.Namespace(execution_id=None))
    machine_status(argparse.Namespace(execution_id=EXECUTION_ID))
    machine_prepare(argparse.Namespace(job_id=JOB_ID, job_revision=7))
    assert calls == [
        ("machine.status", {"schema_version": 1, "execution_id": None}),
        ("machine.status", {"schema_version": 1, "execution_id": EXECUTION_ID}),
        ("machine.prepare", {"schema_version": 1, "job_id": JOB_ID, "job_revision": 7}),
    ]


def test_cli_approve_presence_selects_request_or_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(_self: ServiceClient, command: str, arguments: dict[str, object], **_kwargs: object) -> ProtocolResponse:
        calls.append((command, arguments))
        return _response(command, {"schema_version": 1})

    monkeypatch.setattr(ServiceClient, "call", call)
    machine_action(argparse.Namespace(command_name="machine.home", execution_id=EXECUTION_ID, approve=None))
    machine_action(argparse.Namespace(command_name="machine.home", execution_id=EXECUTION_ID, approve=CHALLENGE_ID))
    machine_recover(argparse.Namespace(execution_id=EXECUTION_ID, disposition="release", approve=None))
    machine_recover(argparse.Namespace(execution_id=EXECUTION_ID, disposition="release", approve=CHALLENGE_ID))
    assert calls == [
        (
            "machine.home",
            {"schema_version": 1, "execution_id": EXECUTION_ID, "mode": "request", "challenge_id": None},
        ),
        (
            "machine.home",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "execute",
                "challenge_id": CHALLENGE_ID,
            },
        ),
        (
            "machine.recover",
            {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "mode": "request",
                "challenge_id": None,
                "disposition": "release",
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
        ),
    ]


def test_human_challenge_output_says_creation_is_not_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ServiceClient,
        "call",
        lambda _self, command, _arguments, **_kwargs: _response(
            command,
            _challenge_machine_data("machine.home", ApprovalPurpose.HOME),
        ),
    )
    code, stdout, stderr = _invoke(["machine", "home", EXECUTION_ID])
    assert code == 0
    assert "challenge" in stdout.lower()
    assert "not execution" in stdout.lower()
    assert stderr == ""


@pytest.mark.parametrize(
    ("command", "purpose"),
    [
        ("machine.home", ApprovalPurpose.HOME),
        ("machine.zcal", ApprovalPurpose.ZCAL),
        ("machine.zconfirm", ApprovalPurpose.ZCONFIRM),
        ("machine.stream", ApprovalPurpose.STREAM),
        ("machine.home-z", ApprovalPurpose.HOME_Z),
        ("machine.recover", ApprovalPurpose.RECOVER),
    ],
)
def test_human_challenge_render_accepts_only_each_fixed_purpose(
    command: str,
    purpose: ApprovalPurpose,
) -> None:
    data = _challenge_machine_data(command, purpose)
    rendered = render_text(_response(command, data))
    assert purpose.value in rendered
    assert "challenge creation is not execution" in rendered

    data["challenge"] = {"challenge_id": CHALLENGE_ID, "purpose": "caller supplied text"}
    assert render_text(_response(command, data)) == f"{command}: OK"


def test_json_machine_output_is_exactly_one_final_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ServiceClient,
        "call",
        lambda _self, command, _arguments, **_kwargs: _response(command, {"schema_version": 1, "execution": None}),
    )
    code, stdout, stderr = _invoke(["--output", "json", "machine", "status"])
    assert code == 0
    assert len(stdout.splitlines()) == 1
    payload = json.loads(stdout)
    assert payload["command"] == "machine.status"
    assert payload["data"] == {"schema_version": 1, "execution": None}
    assert stderr == ""


def test_machine_usage_errors_use_normal_cli_exit_and_json_envelope() -> None:
    code, stdout, stderr = _invoke(["--output", "json", "machine", "recover", EXECUTION_ID])
    assert code == 1
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout)["error"]["code"] == "CLI_USAGE_ERROR"
    assert stderr == ""


def test_machine_cli_has_no_repository_adapter_serial_bridge_or_hardware_import() -> None:
    source = Path(__file__).parents[2] / "src/drawingmachine/cli/machine.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    forbidden = (
        "drawingmachine.ports",
        "drawingmachine.adapters",
        "drawingmachine.service",
        "serial",
        "pyserial",
        "openclaw_operator_bridge",
        "fluidnc_operator_session",
        "stream_gcode_fluidnc",
        "drawingmachine.hardware",
    )
    assert [name for name in imported if name.startswith(forbidden)] == []


def test_f3_machine_status_human_render_is_exact_for_active_or_null() -> None:
    service_epoch = str(uuid4())
    null = _response(
        "machine.status",
        {
            "schema_version": 1,
            "deduplicated": False,
            "service_epoch": service_epoch,
            "accepting": True,
            "execution": None,
            "progress": None,
            "last_outcome": None,
            "challenge": None,
        },
    )
    active = _response(
        "machine.status",
        {
            "schema_version": 1,
            "deduplicated": False,
            "service_epoch": service_epoch,
            "accepting": False,
            "execution": {
                "schema_version": 1,
                "execution_id": EXECUTION_ID,
                "job_id": JOB_ID,
                "ready_revision": 7,
                "revision": 8,
                "phase": "STREAMING",
                "stream_milestone": "FIRST_WRITE_POSSIBLE",
                "recovery_intent": None,
                "retired": False,
            },
            "progress": None,
            "last_outcome": None,
            "challenge": None,
        },
    )
    assert render_text(null) == (f"machine.status: service_epoch={service_epoch} accepting=true active_execution=null")
    assert render_text(active) == (
        f"machine.status: service_epoch={service_epoch} accepting=false "
        f"execution_id={EXECUTION_ID} job_id={JOB_ID} phase=STREAMING revision=8 "
        "stream_milestone=FIRST_WRITE_POSSIBLE recovery_intent=null retired=false"
    )


@pytest.mark.parametrize(
    "challenge",
    [
        {
            "challenge_id": CHALLENGE_ID,
            "purpose": ApprovalPurpose.HOME.value,
        },
        {
            "challenge_id": CHALLENGE_ID,
            "purpose": ApprovalPurpose.HOME.value,
            "binding": {},
            "unexpected": "caller text",
        },
    ],
)
def test_f4_malformed_nested_challenge_never_renders_actionable_text(challenge: dict[str, object]) -> None:
    data = _challenge_machine_data("machine.home", ApprovalPurpose.HOME)
    data["challenge"] = challenge
    response = _response("machine.home", data)
    rendered = render_text(response)
    assert rendered == "machine.home: OK"
    assert "challenge creation is not execution" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_epoch", str(uuid4())),
        ("job_id", str(uuid4())),
        ("ready_revision", 8),
        ("execution_revision", 9),
    ],
)
def test_f4_individually_valid_challenge_must_match_public_observation(field: str, value: object) -> None:
    data = _challenge_machine_data("machine.home", ApprovalPurpose.HOME)
    challenge = data["challenge"]
    assert isinstance(challenge, dict)
    binding = challenge["binding"]
    assert isinstance(binding, dict)
    binding[field] = value
    rendered = render_text(_response("machine.home", data))
    assert rendered == "machine.home: OK"
    assert "challenge creation is not execution" not in rendered


@pytest.mark.parametrize(
    ("phase", "milestone", "intent"),
    [
        ("AWAITING_HOME_APPROVAL", "NOT_A_MILESTONE", None),
        ("AWAITING_HOME_APPROVAL", "FIRST_WRITE_POSSIBLE", None),
        ("RECOVERY_REQUIRED", "NOT_STARTED", "POST_STREAM_SAFE_HOME"),
        ("RECOVERY_REQUIRED", "STREAM_CONFIRMED", "PRE_STREAM_RESTART"),
    ],
)
def test_f4_actionable_render_requires_consistent_execution_enums(
    phase: str,
    milestone: str,
    intent: str | None,
) -> None:
    command = "machine.recover" if phase == "RECOVERY_REQUIRED" else "machine.home"
    purpose = ApprovalPurpose.RECOVER if command == "machine.recover" else ApprovalPurpose.HOME
    data = _challenge_machine_data(command, purpose)
    execution = data["execution"]
    assert isinstance(execution, dict)
    execution["phase"] = phase
    execution["stream_milestone"] = milestone
    execution["recovery_intent"] = intent
    rendered = render_text(_response(command, data))
    assert rendered == f"{command}: OK"
    assert "challenge creation is not execution" not in rendered


def test_f4_recover_challenge_disposition_must_match_public_recovery_intent() -> None:
    data = _challenge_machine_data("machine.recover", ApprovalPurpose.RECOVER)
    challenge = data["challenge"]
    assert isinstance(challenge, dict)
    binding = challenge["binding"]
    assert isinstance(binding, dict)
    binding["recovery_disposition"] = "SAFE_HOME"
    rendered = render_text(_response("machine.recover", data))
    assert rendered == "machine.recover: OK"
    assert "challenge creation is not execution" not in rendered


@pytest.mark.parametrize(
    ("disposition", "intent", "milestone", "actionable"),
    [
        ("RELEASE", "PRE_STREAM_RESTART", "NOT_STARTED", True),
        ("RESTART_SEQUENCE", "PRE_STREAM_RESTART", "NOT_STARTED", True),
        ("SAFE_HOME", "PRE_STREAM_RESTART", "NOT_STARTED", False),
        ("RELEASE", "STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", True),
        ("RESTART_SEQUENCE", "STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", False),
        ("SAFE_HOME", "STREAM_AMBIGUOUS_RELEASE_ONLY", "FIRST_WRITE_POSSIBLE", False),
        ("RELEASE", "POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", True),
        ("RESTART_SEQUENCE", "POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", False),
        ("SAFE_HOME", "POST_STREAM_SAFE_HOME", "STREAM_CONFIRMED", True),
    ],
)
def test_f4_recovery_intent_disposition_matrix_is_fail_closed(
    disposition: str,
    intent: str,
    milestone: str,
    actionable: bool,
) -> None:
    data = _challenge_machine_data("machine.recover", ApprovalPurpose.RECOVER)
    challenge = data["challenge"]
    execution = data["execution"]
    assert isinstance(challenge, dict) and isinstance(execution, dict)
    binding = challenge["binding"]
    assert isinstance(binding, dict)
    binding["recovery_disposition"] = disposition
    execution["recovery_intent"] = intent
    execution["stream_milestone"] = milestone
    rendered = render_text(_response("machine.recover", data))
    assert ("challenge creation is not execution" in rendered) is actionable


def test_n3_post_stream_safe_home_home_z_challenge_is_never_actionable() -> None:
    data = _challenge_machine_data("machine.home-z", ApprovalPurpose.HOME_Z)
    execution = data["execution"]
    assert isinstance(execution, dict)
    execution["recovery_intent"] = "POST_STREAM_SAFE_HOME"
    rendered = render_text(_response("machine.home-z", data))
    assert rendered == "machine.home-z: OK"
    assert "challenge creation is not execution" not in rendered


def test_n3_post_stream_safe_home_home_challenge_remains_actionable() -> None:
    data = _challenge_machine_data("machine.home", ApprovalPurpose.HOME)
    execution = data["execution"]
    assert isinstance(execution, dict)
    execution["stream_milestone"] = "STREAM_CONFIRMED"
    execution["recovery_intent"] = "POST_STREAM_SAFE_HOME"
    assert "challenge creation is not execution" in render_text(_response("machine.home", data))

    pre_stream_home_z = _challenge_machine_data("machine.home-z", ApprovalPurpose.HOME_Z)
    pre_stream_execution = pre_stream_home_z["execution"]
    assert isinstance(pre_stream_execution, dict)
    pre_stream_execution["recovery_intent"] = "PRE_STREAM_RESTART"
    assert "challenge creation is not execution" in render_text(_response("machine.home-z", pre_stream_home_z))
