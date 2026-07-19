from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import traceback
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

import drawingmachine.adapters.providers.local_comfyui as local_comfyui
from drawingmachine.adapters.providers.local_comfyui import (
    HttpTransport,
    LocalComfyUIConfig,
    LocalComfyUIProvider,
    UrllibHttpTransport,
    collect_outputs,
    comfyui_view_url,
)
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.providers import (
    MAX_PROVIDER_IDENTIFIER_BYTES,
    MAX_PROVIDER_PROMPT_BYTES,
    ProviderPollState,
    ProviderRequestV1,
    ProviderStatus,
)

WORKFLOW: JsonObject = {
    "25": {"inputs": {"image": "old.png"}},
    "27": {"inputs": {"prompt": "snapshot prompt"}},
    "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
    "18": {"inputs": {"filename_prefix": "old"}},
    "221": {"inputs": {"scale_to_length": 576}},
}
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ENDPOINT_BYTES_LIMIT = 4_096
MODEL_FAMILY_BYTES_LIMIT = 255
WORKFLOW_NODE_ID_BYTES_LIMIT = 255


class LyingText(str):
    def __new__(cls, value: str) -> LyingText:
        instance = cast(LyingText, super().__new__(cls, value))
        instance.calls = []
        return instance

    def __len__(self) -> int:
        self.calls.append("len")
        return 1

    def encode(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        self.calls.append("encode")
        raise AssertionError("RAW-SECRET hostile config string encode reached")

    def __eq__(self, other: object) -> bool:
        del other
        self.calls.append("eq")
        raise AssertionError("RAW-SECRET hostile config string equality reached")

    def __hash__(self) -> int:
        self.calls.append("hash")
        raise AssertionError("RAW-SECRET hostile config string hash reached")


class HostileItemsMapping(Mapping[object, object]):
    def __init__(self, items: tuple[tuple[object, object], ...]) -> None:
        self._supplied_items = items

    def __getitem__(self, key: object) -> object:
        del key
        raise KeyError

    def __iter__(self) -> Iterator[object]:
        return iter(())

    def __len__(self) -> int:
        return len(self._supplied_items)

    def items(self) -> tuple[tuple[object, object], ...]:
        return self._supplied_items


class HostileIterMapping(Mapping[object, object]):
    def __init__(self, items: tuple[tuple[object, object], ...]) -> None:
        self._supplied_items = items

    def __getitem__(self, key: object) -> object:
        for candidate, value in self._supplied_items:
            if type(candidate) is str and candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        return (key for key, _value in self._supplied_items)

    def __len__(self) -> int:
        return len(self._supplied_items)


def envelope(endpoint: str = "https://comfy.example.test") -> ProfileEnvelope:
    return ProfileEnvelope(
        schema_version=1,
        profile={
            "name": "local-comfyui",
            "endpoint": endpoint,
            "workflow_template": "workflow.json",
            "model_family": "qwen-image-edit-2511",
            "scale_to_length": 576,
            "timeout_seconds": 4.0,
            "poll_interval_seconds": 1.0,
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


def provenance(tmp_path: Path) -> tuple[Path, Path]:
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(WORKFLOW), encoding="utf-8")
    return profile_path, workflow_path


def direct_config(
    tmp_path: Path,
    *,
    profile_path: Path | None = None,
    free_after_run: bool = False,
) -> LocalComfyUIConfig:
    selected_profile, _workflow_path = provenance(tmp_path)
    return LocalComfyUIConfig(
        name="local-comfyui",
        endpoint="https://comfy.example.test",
        workflow_template=Path("workflow.json"),
        model_family="qwen-image-edit-2511",
        scale_to_length=576,
        timeout_seconds=4.0,
        poll_interval_seconds=1.0,
        free_after_run=free_after_run,
        workflow_nodes={
            "load_image": "25",
            "prompt": "27",
            "sampler": "28",
            "save_image": "18",
            "scale": "221",
        },
        sampler_defaults={"steps": None, "cfg": None, "denoise": None},
        live_execution_requires_execute_flag=True,
        profile_path=selected_profile if profile_path is None else profile_path,
    )


def utf8_exact(prefix: str, *, maximum: int, character: str) -> str:
    remaining = maximum - len(prefix.encode())
    encoded_character = character.encode()
    return prefix + character * (remaining // len(encoded_character)) + "a" * (remaining % len(encoded_character))


def bounded_config(
    tmp_path: Path,
    *,
    from_profile: bool,
    name: str = "local-comfyui",
    endpoint: str = "https://comfy.example.test",
    model_family: str = "qwen-image-edit-2511",
    workflow_nodes: Mapping[str, str] | None = None,
    template_prompt: str = "snapshot prompt",
) -> LocalComfyUIConfig:
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    workflow = json.loads(json.dumps(WORKFLOW))
    cast(dict[str, object], cast(dict[str, object], workflow["27"])["inputs"])["prompt"] = template_prompt
    (tmp_path / "workflow.json").write_text(json.dumps(workflow), encoding="utf-8")
    nodes = dict(envelope().profile["workflow_nodes"]) if workflow_nodes is None else dict(workflow_nodes)  # type: ignore[arg-type]
    selected = envelope(endpoint)
    cast(dict[str, JsonValue], selected.profile)["name"] = name
    cast(dict[str, JsonValue], selected.profile)["model_family"] = model_family
    cast(dict[str, JsonValue], selected.profile)["workflow_nodes"] = nodes
    if from_profile:
        return LocalComfyUIConfig.from_profile(selected, profile_path=profile_path)
    return LocalComfyUIConfig(
        name=name,
        endpoint=endpoint,
        workflow_template=Path("workflow.json"),
        model_family=model_family,
        scale_to_length=576,
        timeout_seconds=4.0,
        poll_interval_seconds=1.0,
        free_after_run=False,
        workflow_nodes=nodes,
        sampler_defaults={"steps": None, "cfg": None, "denoise": None},
        live_execution_requires_execute_flag=True,
        profile_path=profile_path,
    )


def provider_request(image: Path) -> ProviderRequestV1:
    content = image.read_bytes()
    return ProviderRequestV1(
        1,
        "request-1",
        str(image.resolve()),
        hashlib.sha256(content).hexdigest(),
        None,
        "job-name",
    )


def provider_request_with_id(image: Path, request_id: str) -> ProviderRequestV1:
    content = image.read_bytes()
    return ProviderRequestV1(
        1,
        request_id,
        str(image.resolve()),
        hashlib.sha256(content).hexdigest(),
        None,
        f"job-{request_id}",
    )


class SecretTransport(HttpTransport):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        del method, url, payload, timeout_seconds
        raise RuntimeError("RAW-SECRET-URL-BODY-PROMPT-BYTES")

    def upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
    ) -> JsonValue:
        del url, filename, content, media_type, timeout_seconds
        raise RuntimeError("RAW-SECRET-URL-BODY-PROMPT-BYTES")

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del url, timeout_seconds
        raise RuntimeError("RAW-SECRET-URL-BODY-PROMPT-BYTES")


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedTransport(HttpTransport):
    def __init__(self, script: list[JsonValue | bytes], clock: MutableClock) -> None:
        self.script = list(script)
        self.clock = clock
        self.calls: list[tuple[str, float]] = []

    def _next(self, kind: str, timeout_seconds: float) -> JsonValue | bytes:
        self.calls.append((kind, timeout_seconds))
        return self.script.pop(0)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        del url, payload
        value = self._next(f"JSON {method}", timeout_seconds)
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
        del url, filename, content, media_type
        value = self._next("UPLOAD", timeout_seconds)
        assert not isinstance(value, bytes)
        return value

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del url
        value = self._next("BYTES", timeout_seconds)
        assert isinstance(value, bytes)
        return value


def assert_sanitized(error: DrawingMachineError) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert "RAW-SECRET" not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def assert_hostile_string_not_touched(error: DrawingMachineError, value: LyingText) -> None:
    assert_sanitized(error)
    assert value.calls == []


def test_default_transport_uses_redirect_denying_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[object, ...]] = []

    class FakeOpener:
        pass

    def fake_build_opener(*handlers: object) -> FakeOpener:
        captured.append(handlers)
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    UrllibHttpTransport()

    assert len(captured) == 1
    handler = next(handler for handler in captured[0] if type(handler).__name__ == "_DenyRedirectHandler")
    assert (
        handler.redirect_request(  # type: ignore[attr-defined]
            urllib.request.Request("https://example.test/start"),
            None,
            302,
            "Found",
            {},
            "https://attacker.test/RAW-SECRET",
        )
        is None
    )


def test_default_transport_reads_chunked_json_with_a_bounded_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    class ChunkedResponse:
        def __init__(self) -> None:
            self.chunks = [b'{"ok":', b"true}", b""]

        def __enter__(self) -> ChunkedResponse:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, maximum: int) -> bytes:
            assert 0 < maximum <= 4 * 1024 * 1024 + 1
            return self.chunks.pop(0)

        def settimeout(self, timeout: float) -> None:
            assert timeout > 0

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> ChunkedResponse:
            del request, timeout
            return ChunkedResponse()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    assert UrllibHttpTransport().request_json(
        "GET", "https://example.test/status", payload=None, timeout_seconds=1.0
    ) == {"ok": True}


def test_default_transport_rejects_chunk_drip_past_absolute_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MutableClock()

    class DripResponse:
        def __enter__(self) -> DripResponse:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 1.0

        def read(self, maximum: int) -> bytes:
            del maximum
            clock.now += 0.6
            return b"{" if clock.now < 1.0 else b"}"

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> DripResponse:
            del request
            assert timeout == pytest.approx(1.0)
            return DripResponse()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    transport = UrllibHttpTransport(monotonic=clock)

    with pytest.raises(TimeoutError):
        transport.request_json("GET", "https://example.test/status", payload=None, timeout_seconds=1.0)


def test_direct_config_requires_provenance_and_relative_workflow(tmp_path: Path) -> None:
    selected = direct_config(tmp_path)
    assert selected.workflow_template == (tmp_path / "workflow.json").resolve()

    values = {
        "name": selected.name,
        "endpoint": selected.endpoint,
        "workflow_template": selected.workflow_template,
        "model_family": selected.model_family,
        "scale_to_length": selected.scale_to_length,
        "timeout_seconds": selected.timeout_seconds,
        "poll_interval_seconds": selected.poll_interval_seconds,
        "free_after_run": selected.free_after_run,
        "workflow_nodes": selected.workflow_nodes,
        "sampler_defaults": selected.sampler_defaults,
        "live_execution_requires_execute_flag": selected.live_execution_requires_execute_flag,
    }
    with pytest.raises(TypeError, match="profile_path"):
        LocalComfyUIConfig(**values)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="relative"):
        LocalComfyUIConfig(**values, profile_path=tmp_path / "provider.toml")  # type: ignore[arg-type]


def test_config_snapshot_survives_parent_and_final_symlink_replacement(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    profile_path, workflow_path = provenance(profile_dir)
    selected = LocalComfyUIConfig.from_profile(envelope(), profile_path=profile_path)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflow.json").write_text(json.dumps({**WORKFLOW, "27": {"inputs": {"prompt": "secret"}}}))
    original = tmp_path / "original"
    profile_dir.rename(original)
    profile_dir.symlink_to(outside, target_is_directory=True)
    workflow_path = original / workflow_path.name
    workflow_path.unlink()
    workflow_path.symlink_to(outside / "workflow.json")
    image = tmp_path / "input.png"
    image.write_bytes(b"source")

    prepared = LocalComfyUIProvider(selected, transport=SecretTransport()).create_request(provider_request(image))
    prompt_inputs = cast(Mapping[str, object], cast(Mapping[str, object], prepared.workflow["27"])["inputs"])
    assert prompt_inputs["prompt"] == "snapshot prompt"


@pytest.mark.parametrize("from_profile", [False, True])
@pytest.mark.parametrize(
    ("field", "exact"),
    [
        ("endpoint", "https://" + "a" * (ENDPOINT_BYTES_LIMIT - len("https://"))),
        (
            "endpoint",
            utf8_exact("https://example.test/", maximum=ENDPOINT_BYTES_LIMIT, character="é"),
        ),
        ("model_family", "a" * MODEL_FAMILY_BYTES_LIMIT),
        ("model_family", utf8_exact("", maximum=MODEL_FAMILY_BYTES_LIMIT, character="é")),
        ("workflow_node", "a" * WORKFLOW_NODE_ID_BYTES_LIMIT),
    ],
    ids=["endpoint-ascii", "endpoint-multibyte", "model-ascii", "model-multibyte", "node-ascii"],
)
def test_config_bounded_strings_accept_exact_and_reject_one_byte_over(
    tmp_path: Path, from_profile: bool, field: str, exact: str
) -> None:
    values: dict[str, object] = {}
    if field == "workflow_node":
        nodes = dict(envelope().profile["workflow_nodes"])  # type: ignore[arg-type]
        nodes["load_image"] = exact
        values["workflow_nodes"] = nodes
    else:
        values[field] = exact
    selected = bounded_config(tmp_path, from_profile=from_profile, **values)  # type: ignore[arg-type]
    retained = selected.workflow_nodes["load_image"] if field == "workflow_node" else getattr(selected, field)
    maximum = {
        "endpoint": ENDPOINT_BYTES_LIMIT,
        "model_family": MODEL_FAMILY_BYTES_LIMIT,
        "workflow_node": WORKFLOW_NODE_ID_BYTES_LIMIT,
    }[field]
    assert len(retained.encode()) == maximum

    if field == "workflow_node":
        over_nodes = dict(envelope().profile["workflow_nodes"])  # type: ignore[arg-type]
        over_nodes["load_image"] = exact + "a"
        over_values: dict[str, object] = {"workflow_nodes": over_nodes}
    else:
        over_values = {field: exact + "a"}
    with pytest.raises(DrawingMachineError, match="bytes"):
        bounded_config(tmp_path, from_profile=from_profile, **over_values)  # type: ignore[arg-type]


@pytest.mark.parametrize("from_profile", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("a" * MAX_PROVIDER_PROMPT_BYTES, id="ascii"),
        pytest.param(utf8_exact("", maximum=MAX_PROVIDER_PROMPT_BYTES, character="é"), id="multibyte"),
    ],
)
def test_workflow_template_prompt_accepts_exact_utf8_limit_and_rejects_one_over(
    tmp_path: Path, from_profile: bool, prompt: str
) -> None:
    selected = bounded_config(tmp_path, from_profile=from_profile, template_prompt=prompt)
    prompt_inputs = cast(
        Mapping[str, object],
        cast(Mapping[str, object], selected._workflow_document["27"])["inputs"],
    )
    assert prompt_inputs["prompt"] == prompt
    assert len(prompt.encode()) == MAX_PROVIDER_PROMPT_BYTES
    with pytest.raises(DrawingMachineError, match="UTF-8 bytes"):
        bounded_config(tmp_path, from_profile=from_profile, template_prompt=prompt + "a")


@pytest.mark.parametrize("from_profile", [False, True])
@pytest.mark.parametrize("field", ["endpoint", "model_family", "workflow_node"])
def test_non_builtin_config_strings_reject_before_encoding_or_workflow_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_profile: bool,
    field: str,
) -> None:
    class EncodeMustNotRun(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("oversize config value reached UTF-8 allocation")

    maximum = {
        "endpoint": ENDPOINT_BYTES_LIMIT,
        "model_family": MODEL_FAMILY_BYTES_LIMIT,
        "workflow_node": WORKFLOW_NODE_ID_BYTES_LIMIT,
    }[field]
    oversized = EncodeMustNotRun("a" * (maximum + 1))
    if field == "endpoint":
        values: dict[str, object] = {"endpoint": oversized}
    elif field == "model_family":
        values = {"model_family": oversized}
    else:
        nodes = dict(envelope().profile["workflow_nodes"])  # type: ignore[arg-type]
        nodes["load_image"] = oversized
        values = {"workflow_nodes": nodes}
    snapshot_calls: list[object] = []

    def denied_snapshot(*args: object, **kwargs: object) -> object:
        snapshot_calls.append((args, kwargs))
        raise AssertionError("invalid config reached workflow snapshot")

    monkeypatch.setattr(local_comfyui, "_read_workflow_snapshot", denied_snapshot)
    with pytest.raises(DrawingMachineError, match="string"):
        bounded_config(tmp_path, from_profile=from_profile, **values)  # type: ignore[arg-type]
    assert snapshot_calls == []


def test_exact_profile_name_rejects_oversize_value_without_encoding_or_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EncodeMustNotRun(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("nonmatching exact profile name reached UTF-8 allocation")

    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    selected = envelope()
    cast(dict[str, JsonValue], selected.profile)["name"] = EncodeMustNotRun("x" * 1_000_000)
    snapshot_calls: list[object] = []

    def denied_snapshot(*args: object, **kwargs: object) -> object:
        snapshot_calls.append((args, kwargs))
        raise AssertionError("invalid profile name reached workflow snapshot")

    monkeypatch.setattr(local_comfyui, "_read_workflow_snapshot", denied_snapshot)
    with pytest.raises(DrawingMachineError, match="exactly local-comfyui"):
        LocalComfyUIConfig.from_profile(selected, profile_path=profile_path)
    assert snapshot_calls == []


@pytest.mark.parametrize("from_profile", [False, True])
@pytest.mark.parametrize("field", ["endpoint", "model_family", "workflow_node"])
def test_bounded_config_invalid_unicode_has_fresh_sanitized_exception_graph(
    tmp_path: Path, from_profile: bool, field: str
) -> None:
    invalid = "\ud800RAW-SECRET"
    if field == "endpoint":
        values: dict[str, object] = {"endpoint": invalid}
    elif field == "model_family":
        values = {"model_family": invalid}
    else:
        nodes = dict(envelope().profile["workflow_nodes"])  # type: ignore[arg-type]
        nodes["load_image"] = invalid
        values = {"workflow_nodes": nodes}

    with pytest.raises(DrawingMachineError) as caught:
        bounded_config(tmp_path, from_profile=from_profile, **values)  # type: ignore[arg-type]
    assert_sanitized(caught.value)


@pytest.mark.parametrize("from_profile", [False, True])
@pytest.mark.parametrize("field", ["name", "endpoint", "model_family", "workflow_node"])
def test_config_rejects_string_subclasses_before_hostile_methods_or_workflow_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_profile: bool,
    field: str,
) -> None:
    underlying = {
        "name": "local-comfyui",
        "endpoint": "a" * (ENDPOINT_BYTES_LIMIT + 1),
        "model_family": "a" * (MODEL_FAMILY_BYTES_LIMIT + 1),
        "workflow_node": "a" * (WORKFLOW_NODE_ID_BYTES_LIMIT + 1),
    }[field]
    hostile = LyingText(underlying)
    if field == "workflow_node":
        nodes = dict(envelope().profile["workflow_nodes"])  # type: ignore[arg-type]
        nodes["load_image"] = hostile
        values: dict[str, object] = {"workflow_nodes": nodes}
    else:
        values = {field: hostile}
    snapshot_calls: list[object] = []

    def denied_snapshot(*args: object, **kwargs: object) -> object:
        snapshot_calls.append((args, kwargs))
        raise AssertionError("invalid hostile config reached workflow snapshot")

    monkeypatch.setattr(local_comfyui, "_read_workflow_snapshot", denied_snapshot)
    with pytest.raises(DrawingMachineError) as caught:
        bounded_config(tmp_path, from_profile=from_profile, **values)  # type: ignore[arg-type]
    assert_hostile_string_not_touched(caught.value, hostile)
    assert snapshot_calls == []


@pytest.mark.parametrize("from_profile", [False, True])
def test_template_prompt_rejects_string_subclass_before_hostile_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_profile: bool
) -> None:
    hostile = LyingText("a" * (MAX_PROVIDER_PROMPT_BYTES + 1))
    workflow = json.loads(json.dumps(WORKFLOW))
    cast(dict[str, object], cast(dict[str, object], workflow["27"])["inputs"])["prompt"] = hostile
    snapshot_calls: list[Path] = []

    def hostile_snapshot(relative_workflow: Path, *, profile_path: Path) -> tuple[Path, JsonObject]:
        snapshot_calls.append(profile_path)
        return profile_path.parent / relative_workflow, workflow

    monkeypatch.setattr(local_comfyui, "_read_workflow_snapshot", hostile_snapshot)
    with pytest.raises(DrawingMachineError) as caught:
        bounded_config(tmp_path, from_profile=from_profile)
    assert_hostile_string_not_touched(caught.value, hostile)
    assert len(snapshot_calls) == 1


def test_config_mapping_rejects_string_subclass_key_before_hostile_methods() -> None:
    hostile = LyingText("RAW-SECRET-key")

    with pytest.raises(DrawingMachineError) as caught:
        local_comfyui._mapping(HostileItemsMapping(((hostile, 1),)), "profile")
    assert_hostile_string_not_touched(caught.value, hostile)


@pytest.mark.parametrize("as_key", [False, True])
def test_external_json_rejects_string_subclasses_before_hostile_methods(as_key: bool) -> None:
    hostile = LyingText("a" * (MAX_PROVIDER_PROMPT_BYTES + 1))
    value: object = HostileItemsMapping(((hostile, 1),)) if as_key else hostile

    with pytest.raises(TypeError):
        local_comfyui._plain_json(value)
    assert hostile.calls == []


@pytest.mark.parametrize("field", ["status", "event", "filename"])
def test_history_rejects_string_subclasses_before_hostile_methods(field: str) -> None:
    hostile = LyingText("a" * (MAX_PROVIDER_PROMPT_BYTES + 1))
    status: dict[str, object] = {"status_str": "success", "completed": True}
    image: dict[str, object] = {"filename": "result.png", "subfolder": "", "type": "output"}
    outputs: dict[str, object] = {"18": {"images": [image]}}
    if field == "status":
        status["status_str"] = hostile
    elif field == "event":
        status["messages"] = [[hostile, {}]]
    else:
        image["filename"] = hostile

    with pytest.raises(DrawingMachineError) as caught:
        collect_outputs({"status": status, "outputs": outputs})
    assert_hostile_string_not_touched(caught.value, hostile)


def test_history_rejects_string_subclass_status_key_before_hostile_hash() -> None:
    hostile = LyingText("RAW-SECRET-key")
    status = HostileIterMapping((("status_str", "success"), ("completed", True), (hostile, None)))

    with pytest.raises(DrawingMachineError) as caught:
        collect_outputs({"status": status, "outputs": {}})
    assert_hostile_string_not_touched(caught.value, hostile)


@pytest.mark.parametrize("from_profile", [False, True])
def test_exact_bounded_config_strings_remain_finite_in_config_and_prompt_records(
    tmp_path: Path, from_profile: bool
) -> None:
    endpoint = utf8_exact("https://example.test/", maximum=ENDPOINT_BYTES_LIMIT, character="é")
    model_family = utf8_exact("", maximum=MODEL_FAMILY_BYTES_LIMIT, character="é")
    selected = bounded_config(
        tmp_path,
        from_profile=from_profile,
        endpoint=endpoint,
        model_family=model_family,
        template_prompt="a" * MAX_PROVIDER_PROMPT_BYTES,
    )
    transport = TimedTransport([], MutableClock())
    provider = LocalComfyUIProvider(selected, transport=transport)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    prepared = provider.create_request(provider_request(image))
    state = provider._prepared[prepared.request_id]
    config_record = json.loads(state.config_record.content)

    assert type(selected.name) is str
    assert type(selected.endpoint) is str
    assert type(selected.model_family) is str
    assert all(type(key) is str and type(value) is str for key, value in selected.workflow_nodes.items())
    prompt_node = cast(Mapping[str, object], selected._workflow_document["27"])
    prompt_inputs = cast(Mapping[str, object], prompt_node["inputs"])
    assert type(prompt_inputs["prompt"]) is str
    assert len(config_record["endpoint"].encode()) == ENDPOINT_BYTES_LIMIT
    assert len(config_record["model_family"].encode()) == MODEL_FAMILY_BYTES_LIMIT
    assert len(state.prompt_record.content) == MAX_PROVIDER_PROMPT_BYTES
    assert not hasattr(state, "prompt_text")
    assert transport.calls == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_profile_fifo_open_is_nonblocking_and_fails_as_nonregular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = tmp_path / "provider.toml"
    os.mkfifo(profile_path)
    (tmp_path / "workflow.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    original_open = os.open

    def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == profile_path.name and not flags & os.O_NONBLOCK:
            raise AssertionError("FIFO open would block")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(DrawingMachineError, match="regular file"):
        LocalComfyUIConfig.from_profile(envelope(), profile_path=profile_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_workflow_fifo_open_is_nonblocking_and_fails_as_nonregular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    workflow_path = tmp_path / "workflow.json"
    os.mkfifo(workflow_path)
    original_open = os.open

    def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == workflow_path.name and not flags & os.O_NONBLOCK:
            raise AssertionError("FIFO open would block")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(DrawingMachineError, match="regular file"):
        LocalComfyUIConfig.from_profile(envelope(), profile_path=profile_path)


@pytest.mark.parametrize(
    "record",
    [
        {"filename": "../escape.png", "subfolder": "", "type": "output"},
        {"filename": "nested/file.png", "subfolder": "", "type": "output"},
        {"filename": "file.png", "subfolder": "../escape", "type": "output"},
        {"filename": "file.png", "subfolder": "nested\\escape", "type": "output"},
        {"filename": "file.png", "subfolder": "", "type": "input"},
        {"filename": "file.png", "subfolder": "", "type": "output", "extra": "secret"},
        {"filename": "file.png", "subfolder": ""},
    ],
)
def test_history_image_records_are_exact_and_path_safe(record: JsonObject) -> None:
    with pytest.raises(DrawingMachineError):
        collect_outputs(
            {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"18": {"images": [record]}},
            }
        )


def test_malformed_first_output_fails_instead_of_selecting_later_valid_output() -> None:
    history = {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "18": {
                "images": [
                    {"filename": "../bad.png", "subfolder": "", "type": "output"},
                    {"filename": "good.png", "subfolder": "", "type": "output"},
                ]
            }
        },
    }
    with pytest.raises(DrawingMachineError):
        collect_outputs(history)


@pytest.mark.parametrize(
    "history",
    [
        {"outputs": {}},
        {"status": {"status_str": "success", "completed": True}},
        {"status": {"status_str": "success", "completed": True, "extra": "secret"}, "outputs": {}},
        {"status": {"status_str": "running", "completed": True}, "outputs": {}},
        {
            "status": {"status_str": "error", "completed": False},
            "outputs": {"18": {"images": [{"filename": "valid.png", "subfolder": "", "type": "output"}]}},
        },
        {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"18": {"images": [], "extra": "secret"}},
        },
    ],
)
def test_history_item_status_outputs_and_nodes_have_exact_schema(history: JsonObject) -> None:
    with pytest.raises(DrawingMachineError) as caught:
        collect_outputs(history)
    assert caught.value.payload.code == "PROVIDER_PROTOCOL_INVALID"


@pytest.mark.parametrize("status_str", ["error", "failed"])
def test_history_failure_variants_are_exact_and_have_no_outputs(status_str: str) -> None:
    assert collect_outputs({"status": {"status_str": status_str, "completed": True}, "outputs": {}}) == ()


def test_history_status_types_are_validated_before_membership_and_poll_is_not_poisoned(tmp_path: Path) -> None:
    invalid_history: JsonObject = {"job-1": {"status": {"status_str": ["success"], "completed": True}, "outputs": {}}}
    clock = MutableClock()
    transport = TimedTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}, invalid_history], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    failed = provider.poll(submission)
    assert failed.state is ProviderPollState.FAILED
    assert failed.error is not None and failed.error.code == "PROVIDER_PROTOCOL_INVALID"
    calls = list(transport.calls)
    assert provider.poll(submission) == failed
    assert transport.calls == calls
    assert provider.retrieve(submission).status is ProviderStatus.FAILED


def test_history_job_key_with_null_item_is_protocol_failure_not_pending(tmp_path: Path) -> None:
    clock = MutableClock()
    transport = TimedTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}, {"job-1": None}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    failed = provider.poll(submission)
    assert failed.state is ProviderPollState.FAILED
    assert failed.error is not None and failed.error.code == "PROVIDER_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    ("status_str", "completed", "expected"),
    [
        ("success", True, ProviderPollState.SUCCEEDED),
        ("error", True, ProviderPollState.FAILED),
        ("failed", False, ProviderPollState.FAILED),
        ("running", False, ProviderPollState.PENDING),
        ("pending", False, ProviderPollState.PENDING),
    ],
)
def test_realistic_history_status_union_with_bounded_messages(
    tmp_path: Path,
    status_str: str,
    completed: bool,
    expected: ProviderPollState,
) -> None:
    outputs: JsonObject = (
        {"18": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}}
        if expected is ProviderPollState.SUCCEEDED
        else {}
    )
    history: JsonObject = {
        "job-1": {
            "status": {
                "status_str": status_str,
                "completed": completed,
                "messages": [
                    ["execution_start", {"prompt_id": "job-1", "timestamp": 1}],
                    ["executing", {"node": "18", "prompt_id": "job-1"}],
                ],
            },
            "outputs": outputs,
        }
    }
    clock = MutableClock()
    transport = TimedTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}, history], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    result = provider.poll(submission)
    assert result.state is expected
    if expected is ProviderPollState.FAILED:
        assert result.error is not None and result.error.code == "PROVIDER_EXECUTION_FAILED"
    elif expected is ProviderPollState.PENDING:
        assert result.retry_after_seconds == pytest.approx(1.0)


@pytest.mark.parametrize(
    "status",
    [
        {"status_str": "success", "completed": 1},
        {"status_str": "success", "completed": True, "messages": "not-a-list"},
        {"status_str": "success", "completed": True, "messages": [["unknown_event", {}]]},
        {"status_str": "success", "completed": True, "messages": [["execution_start", {}, "extra"]]},
    ],
)
def test_history_rejects_invalid_status_types_and_message_records(status: JsonObject) -> None:
    with pytest.raises(DrawingMachineError) as caught:
        collect_outputs({"status": status, "outputs": {}})
    assert caught.value.payload.code == "PROVIDER_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.test:bad",
        "https://[::1",
        "https://example.test\uff0fRAW-SECRET",
    ],
)
def test_malformed_endpoint_is_typed_and_sanitized(tmp_path: Path, endpoint: str) -> None:
    profile_path, _workflow = provenance(tmp_path)
    with pytest.raises(DrawingMachineError) as caught:
        LocalComfyUIConfig.from_profile(envelope(endpoint), profile_path=profile_path)
    assert_sanitized(caught.value)


def test_submit_transport_exception_has_no_secret_exception_graph(tmp_path: Path) -> None:
    selected = direct_config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    provider = LocalComfyUIProvider(selected, transport=SecretTransport())
    prepared = provider.create_request(provider_request(image))

    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(prepared)

    assert_sanitized(caught.value)


def test_json_canonicalization_exception_has_no_secret_exception_graph(tmp_path: Path) -> None:
    class ExplodingMapping(Mapping[str, JsonValue]):
        def __getitem__(self, key: str) -> JsonValue:
            raise KeyError(key)

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("RAW-SECRET-WORKFLOW-BODY")

        def __len__(self) -> int:
            return 1

    class ReturnedSecretTransport(SecretTransport):
        def request_json(
            self,
            method: str,
            url: str,
            *,
            payload: Mapping[str, JsonValue] | None,
            timeout_seconds: float,
        ) -> JsonValue:
            del method, url, payload, timeout_seconds
            return cast(JsonValue, ExplodingMapping())

    selected = direct_config(tmp_path)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    provider = LocalComfyUIProvider(selected, transport=ReturnedSecretTransport())
    prepared = provider.create_request(provider_request(image))
    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(prepared)
    assert_sanitized(caught.value)


def test_unexpected_image_plugin_exception_is_fresh_typed_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock()
    transport = TimedTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG],
        clock,
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    def explode(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("RAW-SECRET-IMAGE-PATH-BYTES")

    monkeypatch.setattr("drawingmachine.adapters.providers.local_comfyui.Image.open", explode)
    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.FAILED
    assert result.error is not None and result.error.code == "PROVIDER_PROTOCOL_INVALID"
    assert "RAW-SECRET" not in str(result.error.to_json())


def test_view_url_rejects_non_exact_image_records() -> None:
    with pytest.raises(DrawingMachineError):
        comfyui_view_url(
            "https://example.test",
            {"filename": "ok.png", "subfolder": "", "type": "output", "extra": "raw"},
        )


def make_timed_provider(
    tmp_path: Path,
    transport: HttpTransport,
    clock: MutableClock,
    *,
    free_after_run: bool = False,
) -> tuple[LocalComfyUIProvider, ProviderRequestV1]:
    selected = direct_config(tmp_path, free_after_run=free_after_run)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    return LocalComfyUIProvider(selected, transport=transport, monotonic=clock), provider_request(image)


def successful_history(job_id: str = "job-1") -> JsonObject:
    return {
        job_id: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"18": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}},
        }
    }


def test_real_monotonic_deadline_bounds_calls_retry_and_stops_after_expiry(tmp_path: Path) -> None:
    clock = MutableClock()
    transport = TimedTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}, {}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    clock.now = 3.75
    pending = provider.poll(submission)
    assert pending.state is ProviderPollState.PENDING
    assert pending.retry_after_seconds == pytest.approx(0.25)
    assert transport.calls[-1] == ("JSON GET", pytest.approx(0.25))

    clock.now = 4.0
    calls = list(transport.calls)
    expired = provider.poll(submission)
    assert expired.state is ProviderPollState.FAILED
    assert expired.error is not None and expired.error.code == "PROVIDER_TIMEOUT"
    assert transport.calls == calls


def test_submit_call_timeouts_consume_real_elapsed_time(tmp_path: Path) -> None:
    class AdvancingTransport(TimedTransport):
        def _next(self, kind: str, timeout_seconds: float) -> JsonValue | bytes:
            value = super()._next(kind, timeout_seconds)
            self.clock.now += 0.5
            return value

    clock = MutableClock()
    transport = AdvancingTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    provider.submit(provider.create_request(request_value))

    assert transport.calls == [
        ("JSON GET", pytest.approx(4.0)),
        ("UPLOAD", pytest.approx(3.5)),
        ("JSON POST", pytest.approx(3.0)),
    ]


@pytest.mark.parametrize("late_kind", ["submit", "poll", "retrieve"])
def test_success_returned_after_deadline_is_rejected(tmp_path: Path, late_kind: str) -> None:
    class LateTransport(TimedTransport):
        def _next(self, kind: str, timeout_seconds: float) -> JsonValue | bytes:
            value = super()._next(kind, timeout_seconds)
            if (
                (late_kind == "submit" and kind == "JSON POST")
                or (late_kind == "poll" and kind == "JSON GET" and len(self.calls) == 4)
                or (late_kind == "retrieve" and kind == "BYTES")
            ):
                self.clock.now = 4.0
            return value

    clock = MutableClock()
    transport = LateTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG], clock
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    prepared = provider.create_request(request_value)
    if late_kind == "submit":
        with pytest.raises(DrawingMachineError) as caught:
            provider.submit(prepared)
        assert caught.value.payload.code == "PROVIDER_TIMEOUT"
        return
    submission = provider.submit(prepared)
    polled = provider.poll(submission)
    if late_kind == "poll":
        assert polled.state is ProviderPollState.FAILED
        assert polled.error is not None and polled.error.code == "PROVIDER_TIMEOUT"
        return
    assert polled.state is ProviderPollState.SUCCEEDED
    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.FAILED
    assert result.error is not None and result.error.code == "PROVIDER_TIMEOUT"


def test_transport_deadline_exception_after_expiry_is_typed_timeout(tmp_path: Path) -> None:
    class ExpiringTransport(TimedTransport):
        def request_json(
            self,
            method: str,
            url: str,
            *,
            payload: Mapping[str, JsonValue] | None,
            timeout_seconds: float,
        ) -> JsonValue:
            if len(self.calls) == 3:
                self.calls.append((f"JSON {method}", timeout_seconds))
                self.clock.now = 4.0
                raise TimeoutError("raw transport timeout")
            return super().request_json(method, url, payload=payload, timeout_seconds=timeout_seconds)

    clock = MutableClock()
    transport = ExpiringTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    result = provider.poll(submission)
    assert result.state is ProviderPollState.FAILED
    assert result.error is not None and result.error.code == "PROVIDER_TIMEOUT"
    assert "raw transport timeout" not in str(result.error.to_json())


def test_retryable_poll_failure_restores_pollable_state_for_same_submission(tmp_path: Path) -> None:
    class RetryPollTransport(TimedTransport):
        failed = False

        def request_json(
            self,
            method: str,
            url: str,
            *,
            payload: Mapping[str, JsonValue] | None,
            timeout_seconds: float,
        ) -> JsonValue:
            if len(self.calls) == 3 and not self.failed:
                self.failed = True
                self.calls.append((f"JSON {method}", timeout_seconds))
                raise RuntimeError("retryable poll failure")
            return super().request_json(method, url, payload=payload, timeout_seconds=timeout_seconds)

    clock = MutableClock()
    transport = RetryPollTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history()], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))

    failed = provider.poll(submission)
    assert failed.state is ProviderPollState.FAILED
    assert failed.error is not None and failed.error.retryable is True
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED


def test_retryable_submit_failure_rolls_back_reservation_for_same_prepared_request(tmp_path: Path) -> None:
    class RetryTransport(TimedTransport):
        failed = False

        def request_json(
            self,
            method: str,
            url: str,
            *,
            payload: Mapping[str, JsonValue] | None,
            timeout_seconds: float,
        ) -> JsonValue:
            if not self.failed:
                self.failed = True
                self.calls.append((f"JSON {method}", timeout_seconds))
                raise RuntimeError("retryable")
            return super().request_json(method, url, payload=payload, timeout_seconds=timeout_seconds)

    clock = MutableClock()
    transport = RetryTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    prepared = provider.create_request(request_value)
    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(prepared)
    assert caught.value.payload.retryable is True
    submission = provider.submit(prepared)
    assert submission.provider_job_id == "job-1"


def test_concurrent_duplicate_submit_is_reserved_before_transport(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTransport(TimedTransport):
        def request_json(
            self,
            method: str,
            url: str,
            *,
            payload: Mapping[str, JsonValue] | None,
            timeout_seconds: float,
        ) -> JsonValue:
            if not self.calls:
                entered.set()
                assert release.wait(2)
            return super().request_json(method, url, payload=payload, timeout_seconds=timeout_seconds)

    clock = MutableClock()
    transport = BlockingTransport([{}, {"name": "input.png"}, {"prompt_id": "job-1"}], clock)
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    prepared = provider.create_request(request_value)
    errors: list[BaseException] = []

    def run_submit() -> None:
        try:
            provider.submit(prepared)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_submit)
    thread.start()
    assert entered.wait(2)
    with pytest.raises(DrawingMachineError, match="submitted"):
        provider.submit(prepared)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    assert [kind for kind, _timeout in transport.calls].count("JSON GET") == 1


@pytest.mark.parametrize("operation", ["poll", "retrieve"])
def test_concurrent_duplicate_poll_and_retrieve_issue_one_live_operation(tmp_path: Path, operation: str) -> None:
    entered = threading.Event()
    release = threading.Event()
    block_at = 4 if operation == "poll" else 5

    class BlockingTransport(TimedTransport):
        def _next(self, kind: str, timeout_seconds: float) -> JsonValue | bytes:
            if len(self.calls) + 1 == block_at:
                entered.set()
                assert release.wait(2)
            return super()._next(kind, timeout_seconds)

    clock = MutableClock()
    transport = BlockingTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG], clock
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    if operation == "retrieve":
        assert provider.poll(submission).state is ProviderPollState.SUCCEEDED
    errors: list[BaseException] = []

    def run_operation() -> None:
        try:
            getattr(provider, operation)(submission)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_operation)
    thread.start()
    assert entered.wait(2)
    with pytest.raises(DrawingMachineError, match="progress"):
        getattr(provider, operation)(submission)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    expected_kind = "JSON GET" if operation == "poll" else "BYTES"
    assert [kind for kind, _timeout in transport.calls].count(expected_kind) == (2 if operation == "poll" else 1)


def test_image_validation_finishing_at_deadline_fails_before_best_effort_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock()
    transport = TimedTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG], clock
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock, free_after_run=True)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    def validated_then_expired(image: object) -> bool:
        assert image == VALID_PNG
        clock.now = 4.0
        return True

    monkeypatch.setattr("drawingmachine.adapters.providers.local_comfyui._image_is_valid", validated_then_expired)
    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.FAILED
    assert result.processed_image is None
    assert result.error is not None and result.error.code == "PROVIDER_TIMEOUT"
    assert [kind for kind, _timeout in transport.calls] == ["JSON GET", "UPLOAD", "JSON POST", "JSON GET", "BYTES"]


def test_oversize_input_is_rejected_before_state_is_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_comfyui, "MAX_INPUT_IMAGE_BYTES", 8)
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"123456789")
    request_value = provider_request(image)

    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(request_value)
    assert caught.value.payload.code == "PROVIDER_INPUT_INVALID"
    image.write_bytes(b"12345678")
    corrected = provider_request(image)
    assert provider.create_request(corrected).request_id == "request-1"


def test_prepared_capacity_is_bounded_and_release_is_local_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_comfyui, "MAX_PREPARED_ENTRIES", 1)
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    first = provider.create_request(provider_request_with_id(image, "request-1"))

    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(provider_request_with_id(image, "request-2"))
    assert caught.value.payload.code == "PROVIDER_CAPACITY_EXCEEDED"
    assert caught.value.payload.retryable is True
    assert provider.release_local_state(first) is True
    assert provider.release_local_state(first) is False
    assert provider.create_request(provider_request_with_id(image, "request-2")).request_id == "request-2"


@pytest.mark.parametrize(
    ("second_request_id", "expected_code", "expected_retryable"),
    [
        ("request-2", "PROVIDER_CAPACITY_EXCEEDED", True),
        ("request-1", "PROVIDER_REQUEST_DUPLICATE", False),
    ],
)
def test_concurrent_create_reserves_capacity_and_identity_before_any_input_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_request_id: str,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    monkeypatch.setattr(local_comfyui, "MAX_PREPARED_ENTRIES", 1)
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    first_image.write_bytes(b"first")
    second_image.write_bytes(b"second")
    first_request = provider_request_with_id(first_image, "request-1")
    second_request = provider_request_with_id(second_image, second_request_id)

    first_read_entered = threading.Event()
    second_read_entered = threading.Event()
    release_first_read = threading.Event()
    second_done = threading.Event()
    original_open = Path.open

    def gated_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if path == first_image:
            first_read_entered.set()
            assert release_first_read.wait(2)
        elif path == second_image:
            second_read_entered.set()
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", gated_open)
    prepared_ids: list[str] = []
    errors: dict[str, DrawingMachineError] = {}

    def run_create(request_value: ProviderRequestV1, *, done: threading.Event | None = None) -> None:
        try:
            prepared_ids.append(provider.create_request(request_value).request_id)
        except DrawingMachineError as error:
            errors[request_value.request_id] = error
        finally:
            if done is not None:
                done.set()

    first_thread = threading.Thread(target=run_create, args=(first_request,))
    second_thread = threading.Thread(target=run_create, args=(second_request,), kwargs={"done": second_done})
    first_thread.start()
    assert first_read_entered.wait(2)
    second_thread.start()
    assert second_done.wait(2)
    release_first_read.set()
    first_thread.join(2)
    second_thread.join(2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_read_entered.is_set() is False
    assert prepared_ids == ["request-1"]
    assert set(errors) == {second_request_id}
    assert errors[second_request_id].payload.code == expected_code
    assert errors[second_request_id].payload.retryable is expected_retryable


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1)},
        {"output_prefix": "a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1)},
        {"prompt": "é" * (MAX_PROVIDER_PROMPT_BYTES // 2) + "a"},
    ],
)
def test_invalid_provider_request_is_rejected_before_reservation_or_input_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": str(image.resolve()),
        "input_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "prompt": None,
        "output_prefix": "job-name",
    }
    values.update(changes)
    open_calls: list[Path] = []

    def denied_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        open_calls.append(path)
        raise AssertionError("input Path.open reached")

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(DrawingMachineError):
        provider.create_request(ProviderRequestV1(**values))  # type: ignore[arg-type]
    assert open_calls == []
    assert provider._preparing == {}


@pytest.mark.parametrize("field", ["request_id", "prompt", "output_prefix"])
def test_hostile_provider_string_rejects_before_workflow_reservation_input_or_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    hostile = LyingText("a" * ((MAX_PROVIDER_PROMPT_BYTES if field == "prompt" else MAX_PROVIDER_IDENTIFIER_BYTES) + 1))
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": str(image.resolve()),
        "input_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "prompt": "draw",
        "output_prefix": "job-name",
    }
    values[field] = hostile
    open_calls: list[Path] = []
    workflow_calls: list[object] = []

    def denied_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        open_calls.append(path)
        raise AssertionError("hostile request reached input Path.open")

    def denied_workflow(instance: LocalComfyUIProvider) -> JsonObject:
        workflow_calls.append(instance)
        raise AssertionError("hostile request reached workflow load")

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(LocalComfyUIProvider, "_load_workflow", denied_workflow)
    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(ProviderRequestV1(**values))  # type: ignore[arg-type]
    assert_hostile_string_not_touched(caught.value, hostile)
    assert open_calls == []
    assert workflow_calls == []
    assert provider._preparing == {}
    assert provider._prepared == {}


def test_create_unexpected_workflow_failure_is_sanitized_and_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    request_value = provider_request(image)
    original_load = LocalComfyUIProvider._load_workflow
    attempts = 0

    def fail_once(instance: LocalComfyUIProvider) -> JsonObject:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("RAW-SECRET-WORKFLOW")
        return original_load(instance)

    monkeypatch.setattr(LocalComfyUIProvider, "_load_workflow", fail_once)
    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(request_value)
    assert caught.value.payload.code == "PROVIDER_REQUEST_INVALID"
    assert_sanitized(caught.value)

    assert provider.create_request(request_value).request_id == "request-1"


@pytest.mark.parametrize("stage", ["config", "dto"])
def test_create_config_and_dto_failures_release_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    request_value = provider_request(image)
    attempts = 0

    if stage == "config":
        original_validate = LocalComfyUIProvider.validate_config

        def fail_config_once(instance: LocalComfyUIProvider) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("RAW-SECRET-CONFIG")
            original_validate(instance)

        monkeypatch.setattr(LocalComfyUIProvider, "validate_config", fail_config_once)
    else:
        original_dto = local_comfyui.PreparedProviderRequest

        def fail_dto_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("RAW-SECRET-DTO")
            return original_dto(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(local_comfyui, "PreparedProviderRequest", fail_dto_once)

    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(request_value)
    assert caught.value.payload.code == "PROVIDER_REQUEST_INVALID"
    assert_sanitized(caught.value)
    assert provider.create_request(request_value).request_id == "request-1"


def test_digest_failure_releases_admission_for_corrected_same_identity(tmp_path: Path) -> None:
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport())
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    bad = ProviderRequestV1(1, "request-1", str(image.resolve()), "0" * 64, None, "job-name")

    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(bad)
    assert caught.value.payload.code == "PROVIDER_INPUT_DIGEST_MISMATCH"
    assert provider.create_request(provider_request(image)).request_id == "request-1"


def test_create_clock_failure_releases_admission_for_same_identity(tmp_path: Path) -> None:
    class FailStoredClock:
        calls = 0

        def __call__(self) -> float:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("RAW-SECRET-CLOCK")
            return 0.0

    clock = FailStoredClock()
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport(), monotonic=clock)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    request_value = provider_request(image)

    with pytest.raises(DrawingMachineError) as caught:
        provider.create_request(request_value)
    assert caught.value.payload.code == "PROVIDER_CLOCK_INVALID"
    assert_sanitized(caught.value)
    assert provider.create_request(request_value).request_id == "request-1"


def test_prepared_ttl_purges_stale_identity_without_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_comfyui, "PREPARED_TTL_SECONDS", 2.0)
    clock = MutableClock()
    provider = LocalComfyUIProvider(direct_config(tmp_path), transport=SecretTransport(), monotonic=clock)
    image = tmp_path / "input.png"
    image.write_bytes(b"source")
    stale = provider.create_request(provider_request(image))
    clock.now = 2.0

    replacement = provider.create_request(provider_request(image))
    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(stale)
    assert caught.value.payload.code == "PROVIDER_IDENTITY_MISMATCH"
    assert replacement.request_id == stale.request_id


def test_live_submission_capacity_and_expiry_cleanup_are_lock_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_comfyui, "MAX_LIVE_SUBMISSIONS", 1)
    clock = MutableClock()
    transport = TimedTransport(
        [
            {},
            {"name": "input.png"},
            {"prompt_id": "job-1"},
            {},
            {"name": "input.png"},
            {"prompt_id": "job-2"},
        ],
        clock,
    )
    provider, _request_value = make_timed_provider(tmp_path, transport, clock)
    image = tmp_path / "second.png"
    image.write_bytes(b"source")
    first = provider.create_request(provider_request_with_id(image, "request-1"))
    first_submission = provider.submit(first)
    second = provider.create_request(provider_request_with_id(image, "request-2"))
    calls = list(transport.calls)
    with pytest.raises(DrawingMachineError) as caught:
        provider.submit(second)
    assert caught.value.payload.code == "PROVIDER_CAPACITY_EXCEEDED"
    assert caught.value.payload.retryable is True
    assert transport.calls == calls

    clock.now = 4.0
    second_submission = provider.submit(second)
    assert second_submission.provider_job_id == "job-2"
    with pytest.raises(DrawingMachineError) as stale:
        provider.poll(first_submission)
    assert stale.value.payload.code == "PROVIDER_IDENTITY_MISMATCH"
    assert provider.release_local_state(second_submission) is True
    assert provider.release_local_state(second_submission) is False


def test_deep_workflow_json_recursion_is_fresh_typed_config_error(tmp_path: Path) -> None:
    profile_path = tmp_path / "provider.toml"
    profile_path.write_text("schema_version=1\n", encoding="utf-8")
    shallow = json.dumps(WORKFLOW)[:-1]
    nested = "[" * 1500 + "0" + "]" * 1500
    (tmp_path / "workflow.json").write_text(f'{shallow},"deep":{nested}}}', encoding="utf-8")

    with pytest.raises(DrawingMachineError) as caught:
        LocalComfyUIConfig.from_profile(envelope(), profile_path=profile_path)
    assert caught.value.payload.code == "PROVIDER_CONFIG_INVALID"
    assert_sanitized(caught.value)


def test_retryable_retrieve_transport_failure_preserves_exactly_one_retry(tmp_path: Path) -> None:
    class RetryBytesTransport(TimedTransport):
        attempts = 0

        def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
            self.attempts += 1
            if self.attempts == 1:
                self.calls.append(("BYTES", timeout_seconds))
                raise RuntimeError("retryable")
            return super().request_bytes(url, timeout_seconds=timeout_seconds)

    clock = MutableClock()
    transport = RetryBytesTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG], clock
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    failed = provider.retrieve(submission)
    assert failed.status is ProviderStatus.FAILED
    assert failed.error is not None and failed.error.retryable is True
    succeeded = provider.retrieve(submission)
    assert succeeded.status is ProviderStatus.SUCCEEDED
    assert transport.attempts == 2


def test_retrieve_rechecks_deadline_before_transport(tmp_path: Path) -> None:
    clock = MutableClock()
    transport = TimedTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history()],
        clock,
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    clock.now = 4.0
    calls = list(transport.calls)
    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.FAILED
    assert result.error is not None and result.error.code == "PROVIDER_TIMEOUT"
    assert transport.calls == calls


@pytest.mark.parametrize(
    "image",
    [
        b"",
        b"<html>RAW-SECRET</html>",
        VALID_PNG[:-5],
        b"x" * (32 * 1024 * 1024 + 1),
    ],
)
def test_retrieve_rejects_empty_oversize_arbitrary_and_truncated_images(tmp_path: Path, image: bytes) -> None:
    clock = MutableClock()
    transport = TimedTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), image],
        clock,
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.FAILED
    assert result.processed_image is None
    assert result.error is not None and result.error.code == "PROVIDER_PROTOCOL_INVALID"


def test_retrieve_accepts_real_allowed_image_and_returns_original_bytes(tmp_path: Path) -> None:
    clock = MutableClock()
    transport = TimedTransport(
        [{}, {"name": "input.png"}, {"prompt_id": "job-1"}, successful_history(), VALID_PNG],
        clock,
    )
    provider, request_value = make_timed_provider(tmp_path, transport, clock)
    submission = provider.submit(provider.create_request(request_value))
    assert provider.poll(submission).state is ProviderPollState.SUCCEEDED

    result = provider.retrieve(submission)
    assert result.status is ProviderStatus.SUCCEEDED
    assert result.processed_image == VALID_PNG
    calls = list(transport.calls)
    with pytest.raises(DrawingMachineError, match="identity"):
        provider.poll(submission)
    assert transport.calls == calls
