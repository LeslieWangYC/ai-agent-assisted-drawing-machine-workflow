from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.providers.local_comfyui import (
    HttpTransport,
    LocalComfyUIConfig,
    LocalComfyUIProvider,
    collect_outputs,
    comfyui_view_url,
)
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.providers import ProviderRequestV1

WORKFLOW_NODES = {
    "load_image": "25",
    "prompt": "27",
    "sampler": "28",
    "save_image": "18",
    "scale": "221",
}


class DenyTransport(HttpTransport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        self.calls.append((method, url))
        raise AssertionError("network transport called")

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
        self.calls.append(("UPLOAD", url))
        raise AssertionError("network transport called")

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.calls.append(("GET_BYTES", url))
        raise AssertionError("network transport called")


def write_workflow(path: Path, *, prompt_node: str = "27") -> Path:
    workflow: JsonObject = {
        "25": {"inputs": {"image": "old.png"}},
        prompt_node: {"inputs": {"prompt": "Template prompt."}},
        "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
        "18": {"inputs": {"filename_prefix": "old"}},
        "221": {"inputs": {"scale_to_length": 576}},
    }
    path.write_text(json.dumps(workflow), encoding="utf-8")
    return path


def profile(workflow_name: str = "workflow.json", **changes: object) -> ProfileEnvelope:
    values: JsonObject = {
        "name": "local-comfyui",
        "endpoint": "https://comfy.example.test/api base",
        "workflow_template": workflow_name,
        "model_family": "qwen-image-edit-2511",
        "scale_to_length": 576,
        "timeout_seconds": 240.0,
        "poll_interval_seconds": 2.0,
        "free_after_run": False,
        "workflow_nodes": dict(WORKFLOW_NODES),
        "sampler_defaults": {"steps": None, "cfg": None, "denoise": None},
        "live_execution_requires_execute_flag": True,
    }
    values.update(cast(dict[str, JsonValue], changes))
    return ProfileEnvelope(schema_version=1, profile=values)


def config(tmp_path: Path, **changes: object) -> LocalComfyUIConfig:
    workflow = write_workflow(tmp_path / "workflow.json")
    provider_path = tmp_path / "local-comfyui.toml"
    provider_path.write_text("schema_version = 1\n", encoding="utf-8")
    envelope = profile(workflow.name, **changes)
    return LocalComfyUIConfig.from_profile(envelope, profile_path=provider_path)


def request(image_path: Path, *, prompt: str | None = None) -> ProviderRequestV1:
    content = image_path.read_bytes()
    return ProviderRequestV1(
        schema_version=1,
        request_id="request-1",
        input_path=str(image_path.resolve()),
        input_sha256=hashlib.sha256(content).hexdigest(),
        prompt=prompt,
        output_prefix="job-name",
    )


def test_config_requires_exact_operational_keys_and_name(tmp_path: Path) -> None:
    write_workflow(tmp_path / "workflow.json")
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")

    with pytest.raises(DrawingMachineError, match=r"profile\.name"):
        LocalComfyUIConfig.from_profile(profile(name="other"), profile_path=config_path)
    with pytest.raises(DrawingMachineError, match="unknown keys"):
        LocalComfyUIConfig.from_profile(profile(extra=True), profile_path=config_path)
    missing = profile()
    del missing.profile["endpoint"]
    with pytest.raises(DrawingMachineError, match="missing keys"):
        LocalComfyUIConfig.from_profile(missing, profile_path=config_path)


def test_config_copies_nested_profile_mappings_without_mutable_aliases(tmp_path: Path) -> None:
    write_workflow(tmp_path / "workflow.json")
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")
    nodes = dict(WORKFLOW_NODES)
    sampler: JsonObject = {"steps": 20, "cfg": 4.0, "denoise": 0.7}
    selected = LocalComfyUIConfig.from_profile(
        profile(workflow_nodes=nodes, sampler_defaults=sampler),
        profile_path=config_path,
    )

    nodes["prompt"] = "99"
    sampler["steps"] = 99
    assert selected.workflow_nodes["prompt"] == "27"
    assert selected.sampler_defaults["steps"] == 20
    with pytest.raises(TypeError):
        selected.workflow_nodes["prompt"] = "99"  # type: ignore[index]
    with pytest.raises(TypeError):
        selected.sampler_defaults["steps"] = 99


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"endpoint": 1}, "endpoint must be a string"),
        ({"endpoint": "ftp://example.test"}, "http or https"),
        ({"endpoint": "https://user:secret@example.test"}, "credentials"),
        ({"scale_to_length": True}, "scale_to_length must be an integer"),
        ({"scale_to_length": 0}, "scale_to_length"),
        ({"scale_to_length": 1 << 80}, "scale_to_length"),
        ({"timeout_seconds": float("nan")}, "finite"),
        ({"timeout_seconds": float("inf")}, "finite"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": 999_999}, "timeout_seconds"),
        ({"poll_interval_seconds": True}, "poll_interval_seconds must be a number"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"poll_interval_seconds": 300, "timeout_seconds": 200}, "poll_interval_seconds"),
        ({"free_after_run": 1}, "free_after_run must be a boolean"),
        ({"workflow_nodes": {"prompt": "27"}}, "workflow_nodes"),
        ({"sampler_defaults": {"steps": 0, "cfg": None, "denoise": None}}, "steps"),
        ({"sampler_defaults": {"steps": None, "cfg": float("inf"), "denoise": None}}, "finite"),
        ({"sampler_defaults": {"steps": None, "cfg": None, "denoise": 1.1}}, "denoise"),
        ({"live_execution_requires_execute_flag": False}, "execute flag"),
    ],
)
def test_config_rejects_type_range_nonfinite_and_overflow(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    write_workflow(tmp_path / "workflow.json")
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")
    with pytest.raises(DrawingMachineError, match=message):
        LocalComfyUIConfig.from_profile(profile(**changes), profile_path=config_path)


@pytest.mark.parametrize("workflow_name", ["/tmp/workflow.json", "../workflow.json", "nested/../workflow.json"])
def test_workflow_template_rejects_absolute_and_traversal(tmp_path: Path, workflow_name: str) -> None:
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")
    with pytest.raises(DrawingMachineError, match="workflow_template"):
        LocalComfyUIConfig.from_profile(profile(workflow_name), profile_path=config_path)


def test_workflow_template_rejects_symlink_directory_and_escape(tmp_path: Path) -> None:
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(outside)
    directory = tmp_path / "directory"
    directory.mkdir()
    try:
        with pytest.raises(DrawingMachineError, match="symlink"):
            LocalComfyUIConfig.from_profile(profile(symlink.name), profile_path=config_path)
        with pytest.raises(DrawingMachineError, match="regular file"):
            LocalComfyUIConfig.from_profile(profile(directory.name), profile_path=config_path)
    finally:
        outside.unlink()


def test_workflow_template_has_no_current_directory_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_directory = tmp_path / "profile"
    profile_directory.mkdir()
    config_path = profile_directory / "provider.toml"
    config_path.write_text("schema_version=1\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    write_workflow(cwd / "workflow.json")
    monkeypatch.chdir(cwd)

    with pytest.raises(DrawingMachineError, match="existing regular file"):
        LocalComfyUIConfig.from_profile(profile(), profile_path=config_path)


def test_construction_validation_and_create_request_make_zero_transport_calls(tmp_path: Path) -> None:
    transport = DenyTransport()
    selected_config = config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"image bytes")

    provider = LocalComfyUIProvider(selected_config, transport=transport)
    provider.validate_config()
    prepared = provider.create_request(request(image))

    assert prepared.request_id == "request-1"
    assert transport.calls == []


def test_default_urllib_transport_is_not_called_by_non_transport_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def denied_urlopen(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("urllib I/O attempted")

    monkeypatch.setattr(urllib.request, "urlopen", denied_urlopen)
    selected_config = config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"image bytes")
    provider = LocalComfyUIProvider(selected_config)

    provider.validate_config()
    provider.create_request(request(image))
    assert calls == []


def test_create_request_preserves_template_prompt_and_hard_coded_override_nodes(tmp_path: Path) -> None:
    selected_config = config(
        tmp_path,
        workflow_nodes={
            "load_image": "999",
            "prompt": "27",
            "sampler": "998",
            "save_image": "997",
            "scale": "996",
        },
        sampler_defaults={"steps": 30, "cfg": 4.5, "denoise": 0.8},
    )
    image = tmp_path / "input image.png"
    image.write_bytes(b"image bytes")
    prepared = LocalComfyUIProvider(selected_config, transport=DenyTransport()).create_request(request(image))
    workflow = prepared.workflow

    assert (
        cast(Mapping[str, object], cast(Mapping[str, object], workflow["25"])["inputs"])["image"] == "input image.png"
    )
    assert (
        cast(Mapping[str, object], cast(Mapping[str, object], workflow["27"])["inputs"])["prompt"] == "Template prompt."
    )
    sampler = cast(Mapping[str, object], cast(Mapping[str, object], workflow["28"])["inputs"])
    assert sampler["steps"] == 30
    assert sampler["cfg"] == 4.5
    assert sampler["denoise"] == 0.8
    assert (
        cast(Mapping[str, object], cast(Mapping[str, object], workflow["18"])["inputs"])["filename_prefix"]
        == "job-name"
    )
    assert cast(Mapping[str, object], cast(Mapping[str, object], workflow["221"])["inputs"])["scale_to_length"] == 576


def test_create_request_only_changes_template_prompt_when_explicit(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"image bytes")
    provider = LocalComfyUIProvider(selected_config, transport=DenyTransport())

    implicit = provider.create_request(request(image))
    explicit = provider.create_request(
        ProviderRequestV1(
            1,
            "request-2",
            str(image.resolve()),
            hashlib.sha256(image.read_bytes()).hexdigest(),
            "Explicit prompt.",
            "job-2",
        )
    )

    implicit_prompt = cast(Mapping[str, object], cast(Mapping[str, object], implicit.workflow["27"])["inputs"])
    explicit_prompt = cast(Mapping[str, object], cast(Mapping[str, object], explicit.workflow["27"])["inputs"])
    assert implicit_prompt["prompt"] == "Template prompt."
    assert explicit_prompt["prompt"] == "Explicit prompt."


def test_collect_outputs_preserves_history_insertion_order() -> None:
    history: JsonObject = {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "99": {"images": [{"filename": "third.png", "subfolder": "", "type": "temp"}]},
            "18": {
                "images": [
                    {"filename": "first.png", "subfolder": "package-b", "type": "output"},
                    {"filename": "second.png", "subfolder": "package-b", "type": "output"},
                ]
            },
        },
    }
    assert collect_outputs(history) == (
        {"filename": "third.png", "subfolder": "", "type": "temp"},
        {"filename": "first.png", "subfolder": "package-b", "type": "output"},
        {"filename": "second.png", "subfolder": "package-b", "type": "output"},
    )


def test_comfyui_view_url_encodes_path_and_query_components() -> None:
    assert comfyui_view_url(
        "https://example.test/api base",
        {"filename": "result image.png", "subfolder": "nested/a&b", "type": "temp"},
    ) == ("https://example.test/api%20base/view?filename=result+image.png&subfolder=nested%2Fa%26b&type=temp")


def test_request_record_preserves_legacy_schema_and_prompt_role(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"image bytes")
    prepared = LocalComfyUIProvider(selected_config, transport=DenyTransport()).create_request(request(image))
    record = json.loads(prepared.record.content)

    assert prepared.record.role == "provider.request"
    assert record["schema"] == "local_comfyui_qwen_image_edit_request_record_v1"
    assert record["provider"] == "local-comfyui"
    assert record["prompt"]["source"] == "workflow_template_default"
    assert record["workflow_nodes"] == WORKFLOW_NODES


def test_input_digest_is_rechecked_before_request_is_prepared(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"actual")
    bad = ProviderRequestV1(1, "request-1", str(image.resolve()), "0" * 64, None, "job")
    with pytest.raises(DrawingMachineError, match="digest"):
        LocalComfyUIProvider(selected_config, transport=DenyTransport()).create_request(bad)


def test_validate_config_uses_construction_snapshot_after_template_replacement(tmp_path: Path) -> None:
    selected_config = config(tmp_path)
    selected_config.workflow_template.write_text("[]", encoding="utf-8")
    LocalComfyUIProvider(selected_config, transport=DenyTransport()).validate_config()


def test_config_rejects_integer_too_large_to_convert_to_float(tmp_path: Path) -> None:
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    write_workflow(tmp_path / "workflow.json")
    values = profile().profile
    values["timeout_seconds"] = 10**10000

    with pytest.raises(DrawingMachineError) as caught:
        LocalComfyUIConfig.from_profile(ProfileEnvelope(schema_version=1, profile=values), profile_path=profile_path)

    assert caught.value.payload.code == "PROVIDER_CONFIG_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
