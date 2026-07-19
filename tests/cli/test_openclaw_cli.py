from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import drawingmachine.cli.openclaw as subject
from drawingmachine.cli.main import main
from drawingmachine.domain.openclaw import (
    OpenClawCheckFileV1,
    OpenClawCheckResultV1,
    OpenClawCheckStatus,
    OpenClawRegistryCheckV1,
    OpenClawRegistryStatus,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        returncode = main(argv)
    return returncode, stdout.getvalue(), stderr.getvalue()


def _response(command: str, data: dict[str, object]) -> ProtocolResponse:
    return ProtocolResponse(PROTOCOL_VERSION, SCHEMA_VERSION, True, command, "request-1", data, None)


@pytest.mark.parametrize("command", ["check", "install"])
def test_openclaw_cli_sends_only_the_closed_absolute_root_request(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    calls: list[tuple[str, object]] = []

    class Client:
        def __init__(self, socket_path: Path) -> None:
            assert socket_path.is_absolute()

        def call(self, command_name: str, arguments: object) -> ProtocolResponse:
            calls.append((command_name, arguments))
            return _response(command_name, {"schema_version": 1, "installed": True})

    monkeypatch.setattr(subject, "ServiceClient", Client)
    returncode, stdout, stderr = _invoke(["--output", "json", "openclaw", command, "--openclaw-root", str(root)])

    assert returncode == 0
    assert calls == [(f"openclaw.{command}", {"schema_version": 1, "openclaw_root": str(root)})]
    assert json.loads(stdout) == {
        "schema_version": 1,
        "ok": True,
        "command": f"openclaw.{command}",
        "request_id": "request-1",
        "data": {"schema_version": 1, "installed": True},
        "error": None,
    }
    assert len(stdout.splitlines()) == 1
    assert stderr == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["openclaw", "check"],
        ["openclaw", "install", "--openclaw-root", "relative"],
        ["openclaw", "check", "--openclaw-root", "/tmp/one", "--openclaw-root", "/tmp/two"],
        ["openclaw", "check", "--openclaw-root", "/tmp/root", "--unknown"],
        ["openclaw", "check", "--openclaw-root", "/tmp/control\nroot"],
        ["openclaw", "check", "--openclaw-root", "/tmp/root", "--check-registry"],
        ["openclaw", "install", "--openclaw-root", "/tmp/root", "--uid", "1000"],
        ["openclaw", "install", "--openclaw-root", "/tmp/root", "--policy", "/tmp/policy"],
        ["sync-openclaw-agent-configs", "--openclaw-root", "/tmp/root"],
    ],
)
def test_openclaw_cli_rejects_missing_relative_repeated_or_undeclared_authority(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subject.ServiceClient, "call", lambda *args, **kwargs: pytest.fail("service not allowed"))

    returncode, stdout, stderr = _invoke(["--output", "json", *argv])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] in {"CLI_USAGE_ERROR", "OPENCLAW_ARGUMENT_INVALID"}
    assert len(stdout.splitlines()) == 1
    assert stderr == ""


def test_openclaw_cli_hides_raw_service_exceptions_in_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"

    def fail(*args: object, **kwargs: object) -> ProtocolResponse:
        del args, kwargs
        raise RuntimeError("RAW_SECRET_MUST_NOT_ESCAPE")

    monkeypatch.setattr(subject.ServiceClient, "call", fail)
    returncode, stdout, stderr = _invoke(["--output", "json", "openclaw", "check", "--openclaw-root", str(root)])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["command"] == "openclaw.check"
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert "RAW_SECRET" not in stdout
    assert stderr == ""


def test_openclaw_cli_preserves_typed_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(*args: object, **kwargs: object) -> ProtocolResponse:
        del args, kwargs
        raise DrawingMachineError(
            ErrorPayload("SERVICE_PERMISSION_DENIED", ErrorCategory.PERMISSION, "permission denied", False, {})
        )

    monkeypatch.setattr(subject.ServiceClient, "call", fail)
    returncode, stdout, stderr = _invoke(["--output", "json", "openclaw", "install", "--openclaw-root", str(tmp_path)])

    assert returncode == 1
    assert json.loads(stdout)["error"]["code"] == "SERVICE_PERMISSION_DENIED"
    assert stderr == ""


def test_negative_check_result_exits_one_in_json_and_text_without_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = OpenClawCheckResultV1(
        False,
        True,
        OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.UNSAFE),
        tuple(
            OpenClawCheckFileV1(destination, "b" * 64, None, OpenClawCheckStatus.UNSAFE)
            for destination in (
                "agents/cnc-drawable-translator/AGENTS.md",
                "agents/cnc-drawable-translator/TOOLS.md",
                "agents/cnc-drawable-router/AGENTS.md",
                "agents/cnc-drawable-router/TOOLS.md",
            )
        ),
    )
    response = ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        False,
        "openclaw.check",
        "request-negative",
        {},
        ErrorPayload(
            "OPENCLAW_CHECK_NOT_IN_SYNC",
            ErrorCategory.VALIDATION,
            "OpenClaw resources are not in sync",
            False,
            {"result": result.to_json()},
            request_id="request-negative",
        ),
    )
    monkeypatch.setattr(subject.ServiceClient, "call", lambda *args, **kwargs: response)

    json_code, json_stdout, json_stderr = _invoke(
        ["--output", "json", "openclaw", "check", "--openclaw-root", str(tmp_path)]
    )
    text_code, text_stdout, text_stderr = _invoke(["openclaw", "check", "--openclaw-root", str(tmp_path)])

    payload = json.loads(json_stdout)
    assert json_code == 1
    assert payload["ok"] is False
    assert payload["data"] == {}
    assert payload["error"]["details"] == {"result": result.to_json()}
    assert json_stderr == ""
    assert text_code == 1
    assert text_stdout == ("openclaw.check: OPENCLAW_CHECK_NOT_IN_SYNC: OpenClaw resources are not in sync\n")
    assert "OK" not in text_stdout
    assert text_stderr == ""
