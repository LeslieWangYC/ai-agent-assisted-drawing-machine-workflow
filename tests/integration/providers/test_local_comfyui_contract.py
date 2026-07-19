from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from drawingmachine.adapters.providers.local_comfyui import HttpTransport, LocalComfyUIConfig, LocalComfyUIProvider
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.providers import ProviderPollState, ProviderRequestV1, ProviderStatus

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ScriptedTransport(HttpTransport):
    def __init__(self, script: list[tuple[str, JsonValue | bytes | Exception]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def _next(self, kind: str, url: str) -> JsonValue | bytes:
        self.calls.append((kind, url))
        expected, value = self.script.pop(0)
        assert expected == kind
        if isinstance(value, Exception):
            raise value
        return value

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        del payload, timeout_seconds
        value = self._next(f"JSON {method}", url)
        assert not isinstance(value, bytes)
        return value

    def upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
    ) -> JsonValue:
        del filename, content, media_type, timeout_seconds
        value = self._next("UPLOAD", url)
        assert not isinstance(value, bytes)
        return value

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        value = self._next("BYTES GET", url)
        assert isinstance(value, bytes)
        return value


def make_provider(
    tmp_path: Path,
    transport: HttpTransport,
    *,
    timeout: float = 4,
    poll: float = 1,
    monotonic: Callable[[], float] | None = None,
) -> LocalComfyUIProvider:
    workflow: JsonObject = {
        "25": {"inputs": {"image": "old.png"}},
        "27": {"inputs": {"prompt": "Template prompt."}},
        "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
        "18": {"inputs": {"filename_prefix": "old"}},
        "221": {"inputs": {"scale_to_length": 576}},
    }
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    envelope = ProfileEnvelope(
        schema_version=1,
        profile={
            "name": "local-comfyui",
            "endpoint": "https://comfy.example.test/root path",
            "workflow_template": "workflow.json",
            "model_family": "qwen-image-edit-2511",
            "scale_to_length": 576,
            "timeout_seconds": timeout,
            "poll_interval_seconds": poll,
            "free_after_run": False,
            "workflow_nodes": {
                "load_image": "25",
                "prompt": "27",
                "sampler": "28",
                "save_image": "18",
                "scale": "221",
            },
            "sampler_defaults": {"steps": None, "cfg": None, "denoise": None},
            "live_execution_requires_execute_flag": True,
        },
    )
    config = LocalComfyUIConfig.from_profile(envelope, profile_path=profile_path)
    if monotonic is None:
        return LocalComfyUIProvider(config, transport=transport)
    return LocalComfyUIProvider(config, transport=transport, monotonic=monotonic)


def prepare(provider: LocalComfyUIProvider, tmp_path: Path):  # type: ignore[no-untyped-def]
    image = tmp_path / "input image.png"
    image.write_bytes(b"source-image")
    return provider.create_request(
        ProviderRequestV1(
            1,
            "request-1",
            str(image.resolve()),
            hashlib.sha256(image.read_bytes()).hexdigest(),
            None,
            "job-name",
        )
    )


def test_fake_transport_full_contract_selects_first_b1_history_output(
    tmp_path: Path,
) -> None:
    golden_root = Path(__file__).resolve().parents[2] / "fixtures/package_b/golden"
    history = json.loads((golden_root / "comfy_history_multi_output.json").read_text(encoding="utf-8"))
    transport = ScriptedTransport(
        [
            ("JSON GET", {"devices": []}),
            ("UPLOAD", {"name": "server uploaded.png"}),
            ("JSON POST", {"prompt_id": "prompt id/one"}),
            ("JSON GET", {"prompt id/one": history}),
            ("BYTES GET", VALID_PNG),
        ]
    )
    provider = make_provider(tmp_path, transport)
    prepared = prepare(provider, tmp_path)

    submission = provider.submit(prepared)
    poll = provider.poll(submission)
    result = provider.retrieve(submission)

    assert poll.state is ProviderPollState.SUCCEEDED
    assert result.status is ProviderStatus.SUCCEEDED
    assert result.processed_image == VALID_PNG
    assert [record.role for record in result.records] == [
        "provider.prompt",
        "provider.config",
        "provider.request",
        "provider.response",
        "provider.handoff",
    ]
    records = {record.role: record for record in result.records}
    assert json.loads(records["provider.response"].content)["schema"] == (
        "local_comfyui_qwen_image_edit_response_record_v1"
    )
    assert json.loads(records["provider.handoff"].content)["schema"] == "cnc_drawable_workflow_handoff_v1"
    assert json.loads(records["provider.config"].content)["schema"] == "cnc_image_edit_provider_config_v2"
    assert transport.calls == [
        ("JSON GET", "https://comfy.example.test/root%20path/system_stats"),
        ("UPLOAD", "https://comfy.example.test/root%20path/upload/image"),
        ("JSON POST", "https://comfy.example.test/root%20path/prompt"),
        ("JSON GET", "https://comfy.example.test/root%20path/history/prompt%20id%2Fone"),
        (
            "BYTES GET",
            "https://comfy.example.test/root%20path/view?filename=package_b_third.png&subfolder=alternate&type=temp",
        ),
    ]
    assert transport.script == []


def test_poll_is_single_step_bounded_and_times_out_deterministically(tmp_path: Path) -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    transport = ScriptedTransport(
        [
            ("JSON GET", {}),
            ("UPLOAD", {"name": "input.png"}),
            ("JSON POST", {"prompt_id": "job-1"}),
            ("JSON GET", {}),
            ("JSON GET", {}),
            ("JSON GET", {}),
        ]
    )
    provider = make_provider(tmp_path, transport, timeout=4, poll=1, monotonic=clock)
    submission = provider.submit(prepare(provider, tmp_path))

    clock.now = 1
    assert provider.poll(submission).state is ProviderPollState.PENDING
    clock.now = 2
    assert provider.poll(submission).retry_after_seconds == 1
    clock.now = 3
    assert provider.poll(submission).state is ProviderPollState.PENDING
    clock.now = 4
    failed = provider.poll(submission)
    assert failed.state is ProviderPollState.FAILED
    assert failed.error is not None
    assert failed.error.code == "PROVIDER_TIMEOUT"
    calls = list(transport.calls)
    assert provider.poll(submission).state is ProviderPollState.FAILED
    assert transport.calls == calls


def test_completed_history_without_image_fails_closed(tmp_path: Path) -> None:
    transport = ScriptedTransport(
        [
            ("JSON GET", {}),
            ("UPLOAD", {"name": "input.png"}),
            ("JSON POST", {"prompt_id": "job-1"}),
            (
                "JSON GET",
                {"job-1": {"status": {"status_str": "success", "completed": True}, "outputs": {}}},
            ),
        ]
    )
    provider = make_provider(tmp_path, transport)
    submission = provider.submit(prepare(provider, tmp_path))

    failed = provider.poll(submission)
    assert failed.state is ProviderPollState.FAILED
    assert failed.error is not None
    assert failed.error.code == "PROVIDER_OUTPUT_MISSING"
    assert provider.retrieve(submission).status is ProviderStatus.FAILED


def test_duplicate_submit_is_rejected_without_additional_transport_call(tmp_path: Path) -> None:
    transport = ScriptedTransport(
        [
            ("JSON GET", {}),
            ("UPLOAD", {"name": "input.png"}),
            ("JSON POST", {"prompt_id": "job-1"}),
        ]
    )
    provider = make_provider(tmp_path, transport)
    prepared = prepare(provider, tmp_path)
    provider.submit(prepared)
    calls = list(transport.calls)

    with pytest.raises(DrawingMachineError, match="already been submitted"):
        provider.submit(prepared)
    assert transport.calls == calls


def test_transport_failure_is_sanitized_and_does_not_include_secret_material(tmp_path: Path) -> None:
    secret = "secret-token prompt-text raw-response-body"
    transport = ScriptedTransport([("JSON GET", RuntimeError(secret))])
    provider = make_provider(tmp_path, transport)
    prepared = prepare(provider, tmp_path)

    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(prepared)

    rendered = str(caught.value.payload.to_json())
    assert caught.value.payload.code == "PROVIDER_TRANSPORT_FAILED"
    assert secret not in rendered
    assert "comfy.example.test" not in rendered


def test_protocol_failure_does_not_echo_transport_response_body(tmp_path: Path) -> None:
    secret_body = "secret response body with prompt text"
    transport = ScriptedTransport([("JSON GET", secret_body)])
    provider = make_provider(tmp_path, transport)

    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(prepare(provider, tmp_path))

    assert caught.value.payload.code == "PROVIDER_PROTOCOL_INVALID"
    assert secret_body not in str(caught.value.payload.to_json())


def test_submission_and_poll_identity_mismatch_fail_without_transport_call(tmp_path: Path) -> None:
    transport = ScriptedTransport([])
    provider = make_provider(tmp_path, transport)
    prepared = prepare(provider, tmp_path)
    replacement = type(prepared)(
        prepared.schema_version, prepared.request_id, prepared.workflow, prepared.upload_name, prepared.record
    )
    with pytest.raises(DrawingMachineError, match="prepared request identity"):
        provider.submit(replacement)
    assert transport.calls == []
