from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

import drawingmachine.cli.config as cli_config_module
from drawingmachine.cli.client import ServiceClient, ServiceDispatchError
from drawingmachine.cli.main import main
from drawingmachine.config import XdgPaths, resolve_xdg_paths
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.client_data import DispatchWriteOutcome
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def _ok_status_response() -> ProtocolResponse:
    return ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=True,
        command="service.status",
        request_id="req-status",
        data={"status": "RUNNING"},
        error=None,
    )


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        returncode = main(argv)
    return returncode, stdout.getvalue(), stderr.getvalue()


def _use_direct_test_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ServiceClient.__init__

    def initialize(
        client: ServiceClient,
        socket_path: Path,
        *,
        timeout_s: float = 5.0,
        endpoint_validator: object = None,
    ) -> None:
        original(client, socket_path, timeout_s=timeout_s, endpoint_validator=endpoint_validator)

    monkeypatch.setattr(ServiceClient, "__init__", initialize)

    class DirectClientData:
        def stage_image(self, path: Path, request_id: str, clock: object) -> SimpleNamespace:
            del request_id, clock
            resolved = path.resolve()
            return SimpleNamespace(payload_path=resolved)

        stage_gcode = stage_image

        def transfer_before_write(self, staged: SimpleNamespace) -> SimpleNamespace:
            return staged

        def discard_definitely_unsent(self, staged: SimpleNamespace, outcome: object) -> None:
            del staged, outcome

        def read_exported_artifact(self, job_id: str, artifact: object) -> bytes:
            relative = Path(artifact.relative_path)
            canonical = resolve_xdg_paths().jobs_dir / job_id / relative
            contents = (canonical if canonical.exists() else relative).read_bytes()
            if len(contents) != artifact.size_bytes or hashlib.sha256(contents).hexdigest() != artifact.sha256:
                raise DrawingMachineError(
                    ErrorPayload(
                        "CLIENT_EXPORT_INVALID",
                        ErrorCategory.INPUT,
                        "test export differs from artifact authority",
                        False,
                        {},
                    )
                )
            return contents

    monkeypatch.setattr("drawingmachine.cli.service.client_input_staging", lambda paths: DirectClientData())
    monkeypatch.setattr("drawingmachine.cli.service.staging_clock", object)


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if existing_pythonpath is None else os.pathsep.join((source_root, existing_pythonpath))
    )
    return environment


def test_parser_builds_foundation_subcommands_and_defaults_to_text() -> None:
    from drawingmachine.cli.parser import build_parser

    parser = build_parser()

    status_args = parser.parse_args(["service", "status"])
    assert status_args.output == "text"
    assert status_args.command_name == "service.status"
    assert callable(status_args.handler)

    config_args = parser.parse_args(["--output", "json", "config", "check"])
    assert config_args.output == "json"
    assert config_args.command_name == "config.check"
    assert callable(config_args.handler)

    run_args = parser.parse_args(["service", "run"])
    assert run_args.command_name == "service.run"
    assert callable(run_args.handler)


def test_parser_builds_offline_job_commands() -> None:
    from drawingmachine.cli.parser import build_parser

    parser = build_parser()

    workflow = parser.parse_args(
        [
            "workflow",
            "run",
            "--job-name",
            "drawing",
            "--input-image",
            "input.png",
            "--route-mode",
            "direct",
        ]
    )
    assert workflow.command_name == "workflow.run"
    assert callable(workflow.handler)
    assert parser.parse_args(["job", "status", "job-1"]).command_name == "job.status"
    assert parser.parse_args(["job", "wait", "job-1"]).command_name == "job.wait"
    assert parser.parse_args(["job", "cancel", "job-1"]).command_name == "job.cancel"
    assert parser.parse_args(["gcode", "check", "candidate.gcode"]).command_name == "gcode.check"


def test_prompt_and_prompt_file_are_mutually_exclusive() -> None:
    returncode, stdout, stderr = _invoke(
        [
            "--output",
            "json",
            "workflow",
            "run",
            "--job-name",
            "drawing",
            "--input-image",
            "input.png",
            "--route-mode",
            "direct",
            "--prompt",
            "one",
            "--prompt-file",
            "prompt.txt",
        ]
    )

    assert returncode == 1
    assert json.loads(stdout)["error"]["code"] == "CLI_USAGE_ERROR"
    assert stderr == ""


def test_workflow_run_resolves_input_and_reads_strict_json_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_direct_test_clients(monkeypatch)
    source = tmp_path / "input.png"
    source.write_bytes(b"image")
    assessment = tmp_path / "assessment.json"
    assessment.write_text('{"drawable":true}', encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("clean outline", encoding="utf-8")
    calls: list[tuple[str, object]] = []

    def call(
        client: ServiceClient,
        command: str,
        arguments: object,
        *,
        request_id: str | None = None,
        before_write: object = None,
    ) -> ProtocolResponse:
        del client
        del request_id
        if callable(before_write):
            before_write()
        calls.append((command, arguments))
        return ProtocolResponse(
            PROTOCOL_VERSION,
            SCHEMA_VERSION,
            True,
            command,
            "req-workflow",
            {"schema_version": 1, "accepted": True, "job_id": "job-1", "revision": 0, "state": "QUEUED"},
            None,
        )

    monkeypatch.setattr(ServiceClient, "call", call)
    monkeypatch.chdir(tmp_path)

    returncode, stdout, stderr = _invoke(
        [
            "--output",
            "json",
            "workflow",
            "run",
            "--job-name",
            "drawing",
            "--input-image",
            "input.png",
            "--route-mode",
            "auto",
            "--semantic-assessment-json",
            "assessment.json",
            "--prompt-file",
            "prompt.txt",
        ]
    )

    assert returncode == 0
    assert set(json.loads(stdout)) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert calls == [
        (
            "workflow.run",
            {
                "schema_version": 1,
                "mode": "submit",
                "job_name": "drawing",
                "input_path": str(source.resolve()),
                "route_mode": "auto",
                "semantic_assessment": {"drawable": True},
                "prompt": "clean outline",
            },
        )
    ]
    assert stderr == ""


def test_workflow_auto_requires_semantic_assessment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: pytest.fail("must not call service"))

    returncode, stdout, stderr = _invoke(
        [
            "--output",
            "json",
            "workflow",
            "run",
            "--job-name",
            "drawing",
            "--input-image",
            "input.png",
            "--route-mode",
            "auto",
        ]
    )

    assert returncode == 1
    assert json.loads(stdout)["error"]["code"] == "WORKFLOW_ARGUMENT_INVALID"
    assert stderr == ""


def test_semantic_assessment_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    assessment = tmp_path / "assessment.json"
    assessment.write_text('{"drawable":true,"drawable":false}', encoding="utf-8")

    returncode, stdout, stderr = _invoke(
        [
            "--output",
            "json",
            "workflow",
            "run",
            "--job-name",
            "drawing",
            "--input-image",
            str(tmp_path / "input.png"),
            "--route-mode",
            "auto",
            "--semantic-assessment-json",
            str(assessment),
        ]
    )

    assert returncode == 1
    assert json.loads(stdout)["error"]["code"] == "JSON_FILE_INVALID"
    assert stderr == ""


def test_workflow_resume_reads_review_json_and_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_direct_test_clients(monkeypatch)
    processed = tmp_path / "processed.png"
    processed.write_bytes(b"reviewed-image")
    review = tmp_path / "review.json"
    review.write_text(
        '{"schema":"processed_image_review_v1","processed_image":"processed.png"}',
        encoding="utf-8",
    )
    calls: list[tuple[str, object]] = []

    def call(client: ServiceClient, command: str, arguments: object) -> ProtocolResponse:
        del client
        calls.append((command, arguments))
        if command == "job.status":
            contents = processed.read_bytes()
            return ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                command,
                "req-status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "job-1", "state": "BLOCKED"},
                    "artifacts": [
                        {
                            "role": "processed_image",
                            "relative_path": "processed.png",
                            "sha256": hashlib.sha256(contents).hexdigest(),
                            "size_bytes": len(contents),
                            "media_type": "image/png",
                        }
                    ],
                    "cancellation_requested": False,
                },
                None,
            )
        return ProtocolResponse(
            PROTOCOL_VERSION,
            SCHEMA_VERSION,
            True,
            command,
            "req-review",
            {"schema_version": 1, "accepted": True, "job_id": "job-1", "revision": 2, "state": "IMAGE_READY"},
            None,
        )

    monkeypatch.setattr(ServiceClient, "call", call)
    monkeypatch.chdir(tmp_path)

    returncode, stdout, stderr = _invoke(
        [
            "--output",
            "json",
            "workflow",
            "run",
            "--resume-job",
            "job-1",
            "--review-json",
            str(review),
        ]
    )

    assert returncode == 0
    assert calls == [
        ("job.status", {"schema_version": 1, "job_id": "job-1"}),
        (
            "workflow.run",
            {
                "schema_version": 1,
                "mode": "resume_review",
                "job_id": "job-1",
                "review": {"schema": "processed_image_review_v1", "processed_image": "processed.png"},
                "reviewed_image_sha256": "28a48c3c451e524a214b8d4d061cba2ba00ad94fac4361c71b2b2de55d457556",
            },
        ),
    ]
    assert json.loads(stdout)["data"]["schema_version"] == 1
    assert stderr == ""


@pytest.mark.parametrize(
    "state,static_result",
    [("COMPLETED", {"ok": True, "violations": []}), ("BLOCKED", {"ok": False, "violations": ["unsafe"]})],
)
def test_gcode_check_enqueues_then_polls_status_and_reads_static_result_locally(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    static_result: dict[str, object],
) -> None:
    _use_direct_test_clients(monkeypatch)
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G0 X0\n", encoding="utf-8")
    artifact = valid_xdg_paths.jobs_dir / "gcode-job" / "artifacts/attempt-1/gcode_static.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(static_result), encoding="utf-8")
    responses = iter(
        [
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "gcode.check",
                "req-submit",
                {"schema_version": 1, "job_id": "gcode-job", "state": "QUEUED", "static_result": None},
                None,
            ),
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "job.status",
                "req-status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "gcode-job", "state": state},
                    "artifacts": [
                        {
                            "role": "gcode_static",
                            "relative_path": "artifacts/attempt-1/gcode_static.json",
                            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                            "size_bytes": artifact.stat().st_size,
                            "media_type": "application/json",
                        }
                    ],
                    "cancellation_requested": False,
                },
                None,
            ),
        ]
    )
    calls: list[tuple[str, object]] = []

    def call(
        client: ServiceClient,
        command: str,
        arguments: object,
        *,
        timeout_s: float | None = None,
        request_id: str | None = None,
        before_write: object = None,
    ) -> ProtocolResponse:
        del client
        del request_id
        if callable(before_write):
            before_write()
        assert timeout_s is not None and timeout_s > 0
        calls.append((command, arguments))
        return next(responses)

    monkeypatch.setattr(ServiceClient, "call", call)

    returncode, stdout, stderr = _invoke(
        ["--output", "json", "gcode", "check", str(candidate), "--poll-interval-seconds", "0.01"]
    )

    payload = json.loads(stdout)
    assert returncode == 0
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload["command"] == "gcode.check"
    assert payload["data"] == {
        "schema_version": 1,
        "job_id": "gcode-job",
        "state": state,
        "static_result": static_result,
    }
    assert [call[0] for call in calls] == ["gcode.check", "job.status"]
    assert calls[0][1] == {"schema_version": 1, "path": str(candidate.resolve())}
    assert calls[1][1] == {"schema_version": 1, "job_id": "gcode-job"}
    assert stderr == ""


@pytest.mark.parametrize(
    "contents,metadata_digest,include_artifact",
    [
        (b'{"ok":true}\n', "0" * 64, True),
        (b'{"ok":NaN}\n', None, True),
        (b'{"ok":true,"ok":false}\n', None, True),
        (b"[]\n", None, True),
        (b"\xff", None, True),
        (b'{"ok":true}\n', None, False),
    ],
)
def test_gcode_check_terminal_result_rejects_missing_mismatched_or_invalid_artifact(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    metadata_digest: str | None,
    include_artifact: bool,
) -> None:
    _use_direct_test_clients(monkeypatch)
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G0 X0\n", encoding="utf-8")
    artifact_path = valid_xdg_paths.jobs_dir / "gcode-job/artifacts/attempt-1/gcode_static.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(contents)
    artifact = {
        "role": "gcode_static",
        "relative_path": "artifacts/attempt-1/gcode_static.json",
        "sha256": hashlib.sha256(contents).hexdigest() if metadata_digest is None else metadata_digest,
        "size_bytes": len(contents),
        "media_type": "application/json",
    }
    responses = iter(
        [
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "gcode.check",
                "req-submit",
                {"schema_version": 1, "job_id": "gcode-job", "state": "QUEUED", "static_result": None},
                None,
            ),
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "job.status",
                "req-status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "gcode-job", "state": "COMPLETED"},
                    "artifacts": [artifact] if include_artifact else [],
                    "cancellation_requested": False,
                },
                None,
            ),
        ]
    )

    def call(
        client: ServiceClient,
        command: str,
        arguments: object,
        *,
        timeout_s: float | None = None,
        request_id: str | None = None,
        before_write: object = None,
    ) -> ProtocolResponse:
        del client, command, arguments, request_id
        if callable(before_write):
            before_write()
        assert timeout_s is not None and timeout_s > 0
        return next(responses)

    monkeypatch.setattr(ServiceClient, "call", call)

    returncode, stdout, stderr = _invoke(["--output", "json", "gcode", "check", str(candidate)])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["command"] == "gcode.check"
    assert payload["error"]["code"] == "GCODE_STATIC_RESULT_INVALID"
    assert stderr == ""


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_gcode_check_failed_or_cancelled_job_allows_null_static_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    _use_direct_test_clients(monkeypatch)
    responses = iter(
        [
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "gcode.check",
                "req-submit",
                {"schema_version": 1, "job_id": "gcode-job", "state": "QUEUED", "static_result": None},
                None,
            ),
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "job.status",
                "req-status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "gcode-job", "state": state},
                    "artifacts": [],
                    "cancellation_requested": state == "CANCELLED",
                },
                None,
            ),
        ]
    )

    def call(
        client: ServiceClient,
        command: str,
        arguments: object,
        *,
        timeout_s: float | None = None,
        request_id: str | None = None,
        before_write: object = None,
    ) -> ProtocolResponse:
        del client, command, arguments, request_id
        if callable(before_write):
            before_write()
        assert timeout_s is not None and timeout_s > 0
        return next(responses)

    monkeypatch.setattr(ServiceClient, "call", call)
    candidate = tmp_path / "candidate.gcode"
    candidate.write_text("G0 X0\n", encoding="utf-8")

    returncode, stdout, stderr = _invoke(["--output", "json", "gcode", "check", str(candidate)])

    payload = json.loads(stdout)
    assert returncode == 0
    assert payload["data"]["state"] == state
    assert payload["data"]["static_result"] is None
    assert stderr == ""


def test_gcode_check_rejects_invalid_wait_timing_before_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: pytest.fail("must not enqueue"))

    returncode, stdout, stderr = _invoke(
        ["--output", "json", "gcode", "check", "candidate.gcode", "--timeout-seconds", "0"]
    )

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["command"] == "gcode.check"
    assert payload["error"]["code"] == "JOB_WAIT_ARGUMENT_INVALID"
    assert stderr == ""


def test_cli_modules_do_not_import_concrete_adapters() -> None:
    cli_root = Path(__file__).resolve().parents[2] / "src/drawingmachine/cli"
    violations: list[str] = []
    for path in cli_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = () if node.module is None else (node.module,)
            elif isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            else:
                continue
            violations.extend(f"{path.name}:{name}" for name in imported if name.startswith("drawingmachine.adapters"))

    assert violations == []


def test_json_status_has_one_public_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_direct_test_clients(monkeypatch)
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: _ok_status_response())

    returncode, stdout, stderr = _invoke(["--output", "json", "service", "status"])

    payload = json.loads(stdout)
    assert returncode == 0
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "command": "service.status",
        "request_id": "req-status",
        "data": {"status": "RUNNING"},
        "error": None,
    }
    assert len(stdout.splitlines()) == 1
    assert stderr == ""


def test_equals_form_selects_json_output_before_argument_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_direct_test_clients(monkeypatch)
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: _ok_status_response())

    returncode, stdout, stderr = _invoke(["--output=json", "service", "status"])

    assert returncode == 0
    assert json.loads(stdout)["ok"] is True
    assert stderr == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--output", "json"],
        ["--output", "json", "service", "unknown"],
    ],
)
def test_json_usage_errors_have_one_public_envelope(argv: list[str]) -> None:
    returncode, stdout, stderr = _invoke(argv)

    payload = json.loads(stdout)
    assert returncode == 1
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload["ok"] is False
    assert payload["command"] == "cli"
    assert payload["data"] == {}
    assert payload["error"]["code"] == "CLI_USAGE_ERROR"
    assert payload["error"]["category"] == "input"
    assert payload["error"]["retryable"] is False
    assert len(stdout.splitlines()) == 1
    assert "usage:" not in stdout
    assert stderr == ""


def test_unexpected_handler_error_is_masked_in_one_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_direct_test_clients(monkeypatch)
    secret = "secret handler failure"

    def fail_unexpected(*args: object, **kwargs: object) -> ProtocolResponse:
        raise RuntimeError(secret)

    monkeypatch.setattr(ServiceClient, "call", fail_unexpected)

    returncode, stdout, stderr = _invoke(["--output", "json", "service", "status"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload["ok"] is False
    assert payload["command"] == "service.status"
    assert payload["data"] == {}
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["category"] == "internal"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["details"] == {}
    assert secret not in stdout
    assert secret not in stderr
    assert "Traceback" not in stderr
    assert len(stdout.splitlines()) == 1


@pytest.mark.parametrize(
    "invalid_value",
    [object(), float("nan")],
    ids=["non-serializable", "non-finite"],
)
def test_json_serialization_failure_emits_one_safe_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    invalid_value: object,
) -> None:
    _use_direct_test_clients(monkeypatch)
    response = ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=True,
        command="service.status",
        request_id="req-unsafe",
        data={"unsafe": invalid_value},  # type: ignore[dict-item]
        error=None,
    )
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: response)

    returncode, stdout, stderr = _invoke(["--output", "json", "service", "status"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload["ok"] is False
    assert payload["command"] == "service.status"
    assert payload["data"] == {}
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["category"] == "internal"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["details"] == {}
    assert len(stdout.splitlines()) == 1
    assert "Traceback" not in stdout
    assert "serializ" not in stdout.lower()
    assert stderr == ""


def test_service_client_maps_permission_error_to_non_retryable_permission_failure(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.cli.client as client_module

    class PermissionDeniedSocket:
        def __enter__(self) -> PermissionDeniedSocket:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, timeout_s: float) -> None:
            del timeout_s

        def connect(self, path: str) -> None:
            del path
            raise PermissionError("secret permission detail")

    monkeypatch.setattr(client_module.socket, "socket", lambda *args: PermissionDeniedSocket())

    with pytest.raises(DrawingMachineError) as error:
        ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call(
            "service.status", {}, request_id="req-permission"
        )

    assert error.value.payload.code == "SERVICE_PERMISSION_DENIED"
    assert error.value.payload.category is ErrorCategory.PERMISSION
    assert error.value.payload.retryable is False
    assert error.value.payload.request_id == "req-permission"
    assert "secret permission detail" not in error.value.payload.message


def test_default_service_client_requires_installed_endpoint_policy(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.cli.service_access as client_access_module
    import drawingmachine.config as config_module

    client = ServiceClient(valid_xdg_paths.socket_file)
    with pytest.raises(DrawingMachineError, match="policy"):
        client.call("service.status", {})
    ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None)
    with pytest.raises(TypeError, match="endpoint_validator"):
        ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=object())

    monkeypatch.setattr(config_module, "resolve_xdg_paths", lambda: valid_xdg_paths)
    with pytest.raises(DrawingMachineError, match="current XDG"):
        ServiceClient(valid_xdg_paths.socket_file.with_name("alternate.sock")).call("service.status", {})

    def unreadable_policy(_paths: XdgPaths) -> object:
        raise OSError("private detail")

    monkeypatch.setattr(client_access_module, "load_client_access_snapshot", unreadable_policy)
    with pytest.raises(DrawingMachineError, match="cannot be inspected") as unreadable:
        ServiceClient(valid_xdg_paths.socket_file).call("service.status", {})
    assert unreadable.value.payload.details == {"reason": "OSError"}

    snapshot = object()
    principal = object()
    monkeypatch.setattr(client_access_module, "load_client_access_snapshot", lambda _paths: snapshot)
    monkeypatch.setattr(
        client_access_module,
        "select_client_endpoint",
        lambda _snapshot: (valid_xdg_paths.socket_file.with_name("wrong.sock"), principal),
    )
    with pytest.raises(DrawingMachineError, match="selected private"):
        ServiceClient(valid_xdg_paths.socket_file).call("service.status", {})

    validated = object()
    monkeypatch.setattr(
        client_access_module,
        "select_client_endpoint",
        lambda _snapshot: (valid_xdg_paths.socket_file, principal),
    )
    monkeypatch.setattr(
        client_access_module,
        "validate_client_endpoint",
        lambda loaded, endpoint, selected: (
            validated
            if (loaded, endpoint, selected) == (snapshot, valid_xdg_paths.socket_file, principal)
            else pytest.fail("installed validator inputs changed")
        ),
    )
    client = ServiceClient(valid_xdg_paths.socket_file)
    assert client._endpoint_validator is not None
    assert client._endpoint_validator() is validated


def test_service_client_per_call_timeout_caps_existing_default(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.cli.client as client_module

    observed: list[float] = []

    class UnavailableSocket:
        def __enter__(self) -> UnavailableSocket:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, timeout_s: float) -> None:
            observed.append(timeout_s)

        def connect(self, path: str) -> None:
            del path
            raise PermissionError

    monkeypatch.setattr(client_module.socket, "socket", lambda *args: UnavailableSocket())
    client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None)

    for timeout_s in (None, 0.025, 30.0):
        with pytest.raises(DrawingMachineError):
            client.call("service.status", {}, timeout_s=timeout_s)

    assert observed == [5.0, 0.025, 5.0]


def test_service_client_closes_protocol_and_unavailable_branches(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.cli.client as client_module
    from drawingmachine.cli.service_access import ValidatedClientEndpointV1

    class StubSocket:
        def __enter__(self) -> StubSocket:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, timeout_s: float) -> None:
            del timeout_s

        def connect(self, path: str) -> None:
            assert path == str(valid_xdg_paths.socket_file)

        def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
            del args, kwargs
            return io.BytesIO()

    response = ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        True,
        "service.status",
        "req-direct",
        {"status": "RUNNING"},
        None,
    )
    validated = ValidatedClientEndpointV1(
        valid_xdg_paths.socket_file,
        (1, 2),
        valid_xdg_paths.socket_file,
        (1, 3),
        "a" * 64,
    )
    monkeypatch.setattr(client_module.socket, "socket", lambda *args: StubSocket())
    monkeypatch.setattr(client_module, "write_frame", lambda *args: None)
    monkeypatch.setattr(client_module, "read_frame", lambda *args: b"response")
    monkeypatch.setattr(client_module, "decode_response", lambda _contents: response)
    client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=lambda: validated)

    assert client.call("service.status", {}, request_id="req-direct") == response
    with pytest.raises(ValueError, match="greater than zero"):
        client.call("service.status", {}, timeout_s=0)

    response = ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        True,
        "job.status",
        "wrong-request",
        {},
        None,
    )
    with pytest.raises(DrawingMachineError) as mismatch:
        client.call("service.status", {}, request_id="req-direct")
    assert mismatch.value.payload.code == "PROTOCOL_INVALID"

    class UnavailableSocket(StubSocket):
        def connect(self, path: str) -> None:
            del path
            raise OSError("private detail")

    monkeypatch.setattr(client_module.socket, "socket", lambda *args: UnavailableSocket())
    with pytest.raises(DrawingMachineError) as unavailable:
        client.call("service.status", {}, request_id="req-direct")
    assert unavailable.value.payload.code == "SERVICE_UNAVAILABLE"
    assert unavailable.value.payload.details == {
        "socket": str(valid_xdg_paths.socket_file),
        "reason": "OSError",
    }


def test_service_client_reports_exact_dispatch_write_outcome(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.cli.client as client_module
    from drawingmachine.cli.service_access import ValidatedClientEndpointV1

    class StubSocket:
        def __enter__(self) -> StubSocket:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, timeout_s: float) -> None:
            del timeout_s

        def connect(self, path: str) -> None:
            assert path == str(valid_xdg_paths.socket_file)

        def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
            del args, kwargs
            return io.BytesIO()

    validated = ValidatedClientEndpointV1(
        valid_xdg_paths.socket_file,
        (1, 2),
        valid_xdg_paths.socket_file,
        (1, 3),
        "a" * 64,
    )
    changed = ValidatedClientEndpointV1(
        valid_xdg_paths.socket_file,
        (1, 4),
        valid_xdg_paths.socket_file,
        (1, 3),
        "a" * 64,
    )
    values = iter((validated, changed))
    monkeypatch.setattr(client_module.socket, "socket", lambda *args: StubSocket())
    client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=lambda: next(values))
    with pytest.raises(ServiceDispatchError) as endpoint_changed:
        client.call("service.status", {}, request_id="req", before_write=lambda: None)
    assert endpoint_changed.value.send_outcome is DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES

    response = ProtocolResponse(PROTOCOL_VERSION, SCHEMA_VERSION, True, "wrong", "req", {}, None)
    monkeypatch.setattr(client_module, "write_frame", lambda *args: None)
    monkeypatch.setattr(client_module, "read_frame", lambda *args: b"response")
    monkeypatch.setattr(client_module, "decode_response", lambda _contents: response)
    client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=lambda: validated)
    with pytest.raises(ServiceDispatchError) as protocol_mismatch:
        client.call("service.status", {}, request_id="req", before_write=lambda: None)
    assert protocol_mismatch.value.send_outcome is DispatchWriteOutcome.WRITE_POSSIBLE

    class PermissionSocket(StubSocket):
        def connect(self, path: str) -> None:
            del path
            raise PermissionError

    monkeypatch.setattr(client_module.socket, "socket", lambda *args: PermissionSocket())
    with pytest.raises(ServiceDispatchError) as permission:
        client.call("service.status", {}, request_id="req", before_write=lambda: None)
    assert permission.value.send_outcome is DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES

    monkeypatch.setattr(client_module.socket, "socket", lambda *args: StubSocket())

    def failed_write(*args: object) -> None:
        del args
        raise OSError

    monkeypatch.setattr(client_module, "write_frame", failed_write)
    with pytest.raises(ServiceDispatchError) as write_failed:
        client.call("service.status", {}, request_id="req", before_write=lambda: None)
    assert write_failed.value.send_outcome is DispatchWriteOutcome.WRITE_POSSIBLE


def test_render_json_omits_protocol_only_fields() -> None:
    from drawingmachine.cli.render import render_json

    payload = json.loads(render_json(_ok_status_response()))

    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert "protocol_version" not in payload


def test_default_text_output_is_concise(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_direct_test_clients(monkeypatch)
    monkeypatch.setattr(ServiceClient, "call", lambda *args, **kwargs: _ok_status_response())

    returncode, stdout, stderr = _invoke(["service", "status"])

    assert returncode == 0
    assert stdout == "service.status: RUNNING\n"
    assert stderr == ""


def test_failed_text_output_is_concise() -> None:
    from drawingmachine.cli.render import render_text

    response = ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=False,
        command="service.status",
        request_id="req-failed",
        data={},
        error=ErrorPayload(
            code="SERVICE_UNAVAILABLE",
            category=ErrorCategory.SERVICE,
            message="drawingmachine service is unavailable",
            retryable=True,
            details={},
            request_id="req-failed",
        ),
    )

    assert render_text(response) == "service.status: SERVICE_UNAVAILABLE: drawingmachine service is unavailable"


def test_failed_text_output_without_error_payload_is_defensive() -> None:
    from drawingmachine.cli.render import render_text

    response = ProtocolResponse(1, 1, False, "service.status", "req-missing-error", {}, None)

    assert render_text(response) == "service.status: FAILED"


def test_config_check_is_local_strict_and_does_not_disclose_profile_values(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-not-appear-in-cli-output"
    provider_path = valid_xdg_paths.config_dir / "providers/local-comfyui.toml"
    provider_path.write_text(
        f'schema_version = 1\n[profile]\nname = "local-comfyui"\napi_key = "{secret}"\n',
        encoding="utf-8",
    )

    def reject_service_call(*args: object, **kwargs: object) -> ProtocolResponse:
        raise AssertionError("config check must not contact the service")

    real_import = builtins.__import__
    real_stat = Path.stat
    real_os_stat = os.stat
    real_os_open = os.open
    real_parser = cli_config_module.parse_machine_execution_config
    bootstrap_module_before = sys.modules.get("drawingmachine.bootstrap")
    parse_calls = 0

    def guarded_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "serial" or name.startswith("serial."):
            raise AssertionError("config check must not import serial")
        return real_import(name, globals, locals, fromlist, level)

    def guarded_stat(path: Path, *, follow_symlinks: bool = True) -> object:
        if str(path).startswith("/dev/"):
            raise AssertionError("config check must not probe the configured device")
        return real_stat(path, follow_symlinks=follow_symlinks)

    def guarded_os_stat(path: os.PathLike[str] | str | int, *args: object, **kwargs: object) -> object:
        if not isinstance(path, int) and os.fspath(path).startswith("/dev/"):
            raise AssertionError("config check must not stat the configured device")
        return real_os_stat(path, *args, **kwargs)  # type: ignore[call-overload]

    def guarded_os_open(path: os.PathLike[str] | str, flags: int, *args: object, **kwargs: object) -> int:
        if os.fspath(path).startswith("/dev/"):
            raise AssertionError("config check must not open the configured device")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[call-overload]

    def counted_parser(*args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return real_parser(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ServiceClient, "call", reject_service_call)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(os, "stat", guarded_os_stat)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(cli_config_module, "parse_machine_execution_config", counted_parser)

    returncode, stdout, stderr = _invoke(["--output", "json", "config", "check"])

    payload = json.loads(stdout)
    assert returncode == 0
    assert payload["data"]["status"] == "VALID"
    assert payload["data"]["profiles"] == {
        "machine": "default",
        "provider": "local-comfyui",
    }
    assert payload["data"]["schema_versions"] == {
        "application": 1,
        "machine": 1,
        "provider": 1,
    }
    assert set(payload["data"]["digests"]) == {"application", "machine", "provider"}
    assert all(len(value) == 64 for value in payload["data"]["digests"].values())
    assert secret not in stdout
    assert parse_calls == 1
    assert sys.modules.get("drawingmachine.bootstrap") is bootstrap_module_before
    assert stderr == ""
    assert not valid_xdg_paths.socket_file.exists()
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()


@pytest.mark.parametrize("invalid_hardware", ["missing", "unsafe-port"])
def test_config_check_rejects_missing_or_unsafe_machine_hardware_locally(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
    invalid_hardware: str,
) -> None:
    machine_path = valid_xdg_paths.config_dir / "machines/default.toml"
    contents = machine_path.read_text(encoding="utf-8")
    if invalid_hardware == "missing":
        contents = contents.partition("[profile.hardware]")[0]
    else:
        contents = contents.replace(
            'serial_port = "/dev/serial/by-id/fluidnc-controller"',
            'serial_port = "/tmp/unsafe-controller"',
        )
    machine_path.write_text(contents, encoding="utf-8")

    def reject_service_call(*args: object, **kwargs: object) -> ProtocolResponse:
        del args, kwargs
        raise AssertionError("config check must not contact the service")

    monkeypatch.setattr(ServiceClient, "call", reject_service_call)

    returncode, stdout, stderr = _invoke(["--output", "json", "config", "check"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["error"]["code"] == "MACHINE_EXECUTION_CONFIG_INVALID"
    assert stderr == ""
    assert not valid_xdg_paths.socket_file.exists()
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()


def test_invalid_config_returns_failed_public_response_without_service_state(
    valid_xdg_paths: XdgPaths,
) -> None:
    valid_xdg_paths.config_file.write_text("schema_version = 2\n", encoding="utf-8")

    returncode, stdout, stderr = _invoke(["--output", "json", "config", "check"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["ok"] is False
    assert payload["command"] == "config.check"
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert stderr == ""
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()


def test_service_status_fails_closed_before_stale_state_without_policy(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.state_dir.mkdir(parents=True)
    (valid_xdg_paths.state_dir / "service-status.json").write_text(
        '{"status":"RUNNING","service_epoch":"stale"}\n',
        encoding="utf-8",
    )

    returncode, stdout, stderr = _invoke(["--output", "json", "service", "status"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["ok"] is False
    assert payload["command"] == "service.status"
    assert payload["error"]["code"] == "SERVICE_ACCESS_INVALID"
    assert "stale" not in stdout
    assert stderr == ""
    assert not valid_xdg_paths.runtime_dir.exists()


def test_service_run_validates_config_before_acquiring_instance_paths(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text("schema_version = 2\n", encoding="utf-8")

    returncode, stdout, stderr = _invoke(["--output", "json", "service", "run"])

    payload = json.loads(stdout)
    assert returncode == 1
    assert payload["command"] == "service.run"
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert stderr == ""
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()
    lock_file = valid_xdg_paths.socket_file.with_suffix(".lock")
    assert not lock_file.exists()
    assert not valid_xdg_paths.socket_file.exists()
    assert not valid_xdg_paths.pid_file.exists()
    assert not valid_xdg_paths.database_file.exists()


def test_foreground_service_holds_preacquired_lease_through_runtime_stop(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.bootstrap as bootstrap
    from drawingmachine.cli.service import _run_foreground

    events: list[str] = []

    class FakeLease:
        held = False

        def release(self) -> None:
            assert self.held is True
            events.append("lease.release")
            self.held = False

    lease = FakeLease()
    test_access_policy = object()

    class FakeRuntime:
        access_policy = test_access_policy

        @property
        def dispatcher(self) -> object:
            assert lease.held is True
            return object()

        def start(self) -> None:
            assert lease.held is True
            events.append("runtime.start")

        def stop(self) -> None:
            assert lease.held is True
            events.append("runtime.stop")

    class FakeServer:
        async def serve(self) -> None:
            events.append("server.serve")

        async def wait_started(self) -> None:
            events.append("server.wait_started")
            await asyncio.sleep(0)

        async def close(self) -> None:
            events.append("server.close")

    def build_instance_lease(paths: XdgPaths, policy: object) -> FakeLease:
        assert paths is valid_xdg_paths
        assert policy is test_access_policy
        events.append("lease.acquire")
        lease.held = True
        return lease

    def build_runtime(paths: XdgPaths, *, require_service_access: bool) -> FakeRuntime:
        assert paths is valid_xdg_paths
        assert require_service_access is True
        assert lease.held is False
        events.append("build_runtime")
        return FakeRuntime()

    def build_server(
        paths: XdgPaths,
        runtime: FakeRuntime,
        *,
        instance_lease: FakeLease,
    ) -> FakeServer:
        assert paths is valid_xdg_paths
        assert isinstance(runtime, FakeRuntime)
        assert instance_lease is lease
        assert lease.held is True
        events.append("build_server")
        return FakeServer()

    monkeypatch.setattr(bootstrap, "build_instance_lease", build_instance_lease, raising=False)
    monkeypatch.setattr(bootstrap, "build_runtime", build_runtime)
    monkeypatch.setattr(bootstrap, "build_server", build_server)

    asyncio.run(_run_foreground(valid_xdg_paths))

    assert events[:4] == [
        "build_runtime",
        "lease.acquire",
        "runtime.start",
        "build_server",
    ]
    assert set(events[4:6]) == {"server.serve", "server.wait_started"}
    assert events[-3:] == [
        "server.close",
        "runtime.stop",
        "lease.release",
    ]


def test_service_run_handles_sigterm_during_runtime_start(tmp_path: Path) -> None:
    trace_path = tmp_path / "startup-trace.json"
    roots = {
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_STATE_HOME": tmp_path / "state",
        "XDG_DATA_HOME": tmp_path / "data",
        "XDG_RUNTIME_DIR": tmp_path / "run",
    }
    environment = _subprocess_environment()
    environment.update({name: str(path) for name, path in roots.items()})
    environment["STARTUP_TRACE"] = str(trace_path)
    script = r"""
import asyncio
import json
import os
import signal
import threading
import time
from pathlib import Path

import drawingmachine.bootstrap as bootstrap
from drawingmachine.cli.main import main

events = []


class FakeLease:
    def __init__(self):
        self.held = False

    def release(self):
        assert self.held
        events.append("lease.release")
        self.held = False


lease = FakeLease()


class FakeRuntime:
    access_policy = object()

    def start(self):
        assert lease.held
        events.append("runtime.start.begin")

        def terminate():
            events.append("signal.SIGTERM")
            os.kill(os.getpid(), signal.SIGTERM)

        timer = threading.Timer(0.05, terminate)
        timer.start()
        time.sleep(0.15)
        timer.join()
        events.append("runtime.start.end")

    def stop(self):
        assert lease.held
        events.append("runtime.stop")


class FakeServer:
    def __init__(self):
        self.closed = asyncio.Event()

    async def serve(self):
        events.append("server.serve")
        await self.closed.wait()
        events.append("server.serve.done")

    async def wait_started(self):
        events.append("server.wait_started")
        await asyncio.sleep(0)

    async def close(self):
        events.append("server.close")
        self.closed.set()


def build_instance_lease(paths, access_policy):
    del paths
    assert access_policy is FakeRuntime.access_policy
    events.append("lease.acquire")
    lease.held = True
    return lease


def build_runtime(paths, *, require_service_access):
    del paths
    assert require_service_access is True
    assert not lease.held
    events.append("build_runtime")
    return FakeRuntime()


def build_server(paths, runtime, *, instance_lease):
    del paths, runtime
    assert instance_lease is lease
    assert lease.held
    events.append("build_server")
    return FakeServer()


bootstrap.build_instance_lease = build_instance_lease
bootstrap.build_runtime = build_runtime
bootstrap.build_server = build_server
returncode = main(["--output", "json", "service", "run"])
if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
    events.append("signal_handler.removed")
Path(os.environ["STARTUP_TRACE"]).write_text(json.dumps(events), encoding="utf-8")
raise SystemExit(returncode)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["command"] == "service.run"
    assert payload["data"] == {"status": "STOPPED"}

    events = json.loads(trace_path.read_text(encoding="utf-8"))
    assert events.index("build_runtime") < events.index("lease.acquire")
    assert events.index("runtime.start.begin") < events.index("signal.SIGTERM")
    assert events.index("signal.SIGTERM") < events.index("runtime.start.end")
    assert events.index("runtime.start.end") < events.index("build_server")
    assert events.index("server.close") < events.index("runtime.stop")
    assert events.index("runtime.stop") < events.index("lease.release")
    assert events[-1] == "signal_handler.removed"


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_service_run_handles_signal_and_closes_server_then_runtime(
    valid_xdg_paths: XdgPaths,
    shutdown_signal: signal.Signals,
    service_access_test_harness,
) -> None:
    environment = _subprocess_environment()
    service_access_test_harness.install(environment)
    import_root = valid_xdg_paths.data_dir / "imports/automation"
    for role in ("image", "gcode"):
        for phase in ("prepare", "drop"):
            directory = import_root / role / phase
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o2770)
    process = subprocess.Popen(
        [sys.executable, "-m", "drawingmachine", "--output", "json", "service", "run"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        response: ProtocolResponse | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"service exited before readiness: stdout={stdout!r} stderr={stderr!r}")
            if valid_xdg_paths.socket_file.exists():
                try:
                    response = ServiceClient(
                        valid_xdg_paths.socket_file,
                        timeout_s=0.2,
                        endpoint_validator=None,
                    ).call(
                        "service.status",
                        {},
                        request_id="req-ready",
                    )
                except Exception:
                    time.sleep(0.02)
                else:
                    break
            else:
                time.sleep(0.02)

        assert response is not None
        assert response.ok is True

        process.send_signal(shutdown_signal)
        stdout, stderr = process.communicate(timeout=5.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0
    assert len(stdout.splitlines()) == 1
    payload = json.loads(stdout)
    assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
    assert payload["ok"] is True
    assert payload["command"] == "service.run"
    assert payload["data"] == {"status": "STOPPED"}
    assert stderr == ""
    assert not valid_xdg_paths.socket_file.exists()
    assert not valid_xdg_paths.pid_file.exists()

    with sqlite3.connect(valid_xdg_paths.database_file) as connection:
        events = [row[0] for row in connection.execute("SELECT event_type FROM audit_events ORDER BY rowid")]
    assert events == ["service.started", "service.stopped"]


def test_all_help_commands_exit_zero_without_creating_xdg_state(tmp_path: Path) -> None:
    roots = {
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_STATE_HOME": tmp_path / "state",
        "XDG_DATA_HOME": tmp_path / "data",
        "XDG_RUNTIME_DIR": tmp_path / "run",
    }
    environment = _subprocess_environment()
    environment.update({name: str(path) for name, path in roots.items()})
    commands = [
        ["--help"],
        ["config", "check", "--help"],
        ["service", "run", "--help"],
        ["service", "status", "--help"],
        ["workflow", "run", "--help"],
        ["job", "status", "--help"],
        ["job", "wait", "--help"],
        ["job", "cancel", "--help"],
        ["gcode", "check", "--help"],
        ["openclaw", "check", "--help"],
        ["openclaw", "install", "--help"],
    ]

    for arguments in commands:
        result = subprocess.run(
            [sys.executable, "-m", "drawingmachine", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout
        assert result.stderr == ""

    assert all(not path.exists() for path in roots.values())
