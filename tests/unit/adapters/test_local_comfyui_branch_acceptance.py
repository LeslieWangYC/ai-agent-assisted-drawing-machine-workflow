from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from PIL import Image

from drawingmachine.adapters.providers import local_comfyui as comfy
from drawingmachine.errors import DrawingMachineError


class BrokenMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 1

    def items(self) -> Any:
        raise RuntimeError("broken")


def test_config_helper_rejection_branches() -> None:
    for value in ([], BrokenMapping(), {1: True}):
        with pytest.raises(DrawingMachineError):
            comfy._mapping(value, "field")
    for value, maximum in (("", None), ("\ud800", None), ("abcd", 3), ("éé", 3)):
        with pytest.raises(DrawingMachineError):
            comfy._string(value, "field", maximum_utf8_bytes=maximum)
    with pytest.raises(DrawingMachineError):
        comfy._boolean(1, "field")
    for value in (True, 0, 3):
        with pytest.raises(DrawingMachineError):
            comfy._integer(value, "field", minimum=1, maximum=2)
    for value in (True, float("nan"), -1.0, 3.0, 10**5000):
        with pytest.raises(DrawingMachineError):
            comfy._number(value, "field", minimum=0.0, maximum=2.0)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://[bad",
        "ftp://host",
        "http:///missing",
        "http://user@host",
        "http://host?q=1",
        "http://host#f",
        "http://host\n",
    ],
)
def test_endpoint_rejects_each_shape(endpoint: str) -> None:
    with pytest.raises(DrawingMachineError):
        comfy._endpoint(endpoint)


@pytest.mark.parametrize("path", ["/absolute", "../up", "a\\b", "a/./b", "a\n"])
def test_workflow_path_rejects_each_shape(path: str) -> None:
    with pytest.raises(DrawingMachineError):
        comfy._workflow_relative_path(path)


def test_directory_and_workflow_snapshot_invalid_branches(tmp_path: Path) -> None:
    assert comfy._open_directory_chain(Path("relative")) is None
    assert comfy._open_directory_chain(tmp_path / "missing") is None
    with pytest.raises(DrawingMachineError):
        comfy._read_workflow_snapshot(PurePosixPath("workflow.json"), profile_path=Path("relative"))
    profile = tmp_path / "provider.toml"
    profile.write_text("schema_version=1\n", encoding="utf-8")
    with pytest.raises(DrawingMachineError):
        comfy._read_workflow_snapshot(PurePosixPath("missing.json"), profile_path=profile)
    workflow = tmp_path / "workflow.json"
    workflow.write_text("[]", encoding="utf-8")
    with pytest.raises(DrawingMachineError):
        comfy._read_workflow_snapshot(PurePosixPath("workflow.json"), profile_path=profile)
    workflow.write_text("{bad", encoding="utf-8")
    with pytest.raises(DrawingMachineError):
        comfy._read_workflow_snapshot(PurePosixPath("workflow.json"), profile_path=profile)


def test_freeze_nodes_sampler_and_workflow_validation_branches() -> None:
    assert comfy._freeze_document({"x": [1]})["x"] == (1,)
    assert comfy._freeze_document(1) == 1
    nodes = {"load_image": "25", "prompt": "27", "sampler": "28", "save_image": "18", "scale": "221"}
    assert comfy._workflow_nodes(nodes)["prompt"] == "27"
    with pytest.raises(DrawingMachineError):
        comfy._workflow_nodes({**nodes, "prompt": "bad node"})
    assert comfy._sampler_defaults({"steps": 2, "cfg": 1.0, "denoise": 0.5}) == {
        "steps": 2,
        "cfg": 1.0,
        "denoise": 0.5,
    }
    with pytest.raises(DrawingMachineError):
        comfy._validate_workflow_document({}, prompt_node="27")
    workflow = {node: {"inputs": {}} for node in {"25", "27", "28", "18", "221"}}
    with pytest.raises(DrawingMachineError):
        comfy._validate_workflow_document(workflow, prompt_node="27")


class Response:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = iter(chunks)
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def read(self, maximum: int) -> object:
        del maximum
        return next(self.chunks, b"")

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class Opener:
    def __init__(self, response: Response) -> None:
        self.response = response

    def open(self, request: object, timeout: float) -> Response:
        del request, timeout
        return self.response


def test_transport_deadline_timeout_and_bounded_read_branches() -> None:
    with pytest.raises(TimeoutError):
        comfy._transport_remaining(1.0, lambda: 1.0)
    with pytest.raises(ValueError):
        comfy._set_response_timeout(object(), 1.0)
    response = Response([b"a", b""])
    assert comfy._bounded_response_read(response, maximum=2, expires_at=10.0, monotonic=lambda: 0.0) == b"a"
    with pytest.raises(ValueError):
        comfy._bounded_response_read(Response(["bad"]), maximum=2, expires_at=10.0, monotonic=lambda: 0.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        comfy._bounded_response_read(Response([b""]), maximum=2, expires_at=10.0, monotonic=lambda: 0.0)
    with pytest.raises(ValueError):
        comfy._bounded_response_read(Response([b"abc"]), maximum=2, expires_at=10.0, monotonic=lambda: 0.0)
    transport = comfy.UrllibHttpTransport(monotonic=lambda: 1.0)
    with pytest.raises(TimeoutError):
        transport._deadline(0.0)
    assert transport._deadline(2.0) == 3.0
    assert transport._deadline(2.0, 2.0) == 2.0


def test_transport_invalid_json_response_branches() -> None:
    transport = comfy.UrllibHttpTransport(monotonic=lambda: 0.0)
    transport._opener = Opener(Response([b"bad", b""]))  # type: ignore[assignment]
    with pytest.raises(comfy._ResponseProtocolError):
        transport.request_json("GET", "http://invalid.test", payload=None, timeout_seconds=2.0)
    transport._opener = Opener(Response([b"bad", b""]))  # type: ignore[assignment]
    with pytest.raises(comfy._ResponseProtocolError):
        transport.upload_image(
            "http://invalid.test", filename="x.png", content=b"x", media_type="image/png", timeout_seconds=2.0
        )


def test_plain_json_copy_object_and_endpoint_url_branches() -> None:
    class StringSubclass(str):
        pass

    for value in (StringSubclass("bad"), float("nan"), {1: True}, object()):
        with pytest.raises((TypeError, ValueError)):
            comfy._plain_json(value)
    assert comfy._plain_json({"a": [1, True]}) == {"a": [1, True]}
    with pytest.raises(DrawingMachineError):
        comfy._json_copy(object(), phase="test")
    with pytest.raises(DrawingMachineError):
        comfy._json_object([], phase="test")
    with pytest.raises(DrawingMachineError):
        comfy._endpoint_url("http://[bad", "view")
    assert comfy._endpoint_url("http://host/base", "a b") == "http://host/base/a%20b"


@pytest.mark.parametrize(
    "image",
    [
        [],
        {"filename": "x"},
        {"filename": 1, "subfolder": "", "type": "output"},
        {"filename": "../x", "subfolder": "", "type": "output"},
        {"filename": "x.png", "subfolder": "../bad", "type": "output"},
        {"filename": "x.png", "subfolder": "", "type": "bad"},
        {"filename": "\ud800", "subfolder": "", "type": "output"},
    ],
)
def test_image_record_rejects_every_shape(image: object) -> None:
    with pytest.raises(DrawingMachineError):
        comfy._validated_image_record(image, phase="test")


def test_status_and_history_validation_branches() -> None:
    for payload in ([], float("nan"), {1: True}, {"x": object()}, {"x": "\ud800"}):
        with pytest.raises(DrawingMachineError):
            comfy._validate_status_payload(payload)
    for messages in ("bad", [["unknown", {}]], [["execution_start"]], [1]):
        with pytest.raises(DrawingMachineError):
            comfy._validate_status_messages(messages)
    pending = {"status": {"status_str": "running", "completed": False}, "outputs": {}}
    failure = {"status": {"status_str": "error", "completed": False}, "outputs": {}}
    success = {
        "status": {"status_str": "success", "completed": True, "messages": [["execution_start", {}]]},
        "outputs": {"18": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}},
    }
    assert comfy._parse_history_item(pending).kind is comfy._HistoryKind.PENDING
    assert comfy._parse_history_item(failure).kind is comfy._HistoryKind.FAILURE
    assert comfy.collect_outputs(success)[0]["filename"] == "x.png"
    invalid_items = [
        {"status": [], "outputs": {}},
        {"status": {"status_str": "success", "completed": False}, "outputs": {}},
        {"status": {"status_str": "running", "completed": False}, "outputs": {"18": {}}},
        {"status": {"status_str": "success", "completed": True}, "outputs": []},
        {"status": {"status_str": "success", "completed": True}, "outputs": {"bad node": {}}},
        {"status": {"status_str": "success", "completed": True}, "outputs": {"18": []}},
        {"status": {"status_str": "success", "completed": True}, "outputs": {"18": {"bad": []}}},
        {"status": {"status_str": "success", "completed": True}, "outputs": {"18": {"images": "bad"}}},
    ]
    for value in invalid_items:
        with pytest.raises(DrawingMachineError):
            comfy._parse_history_item(value)


def test_status_size_depth_subclass_and_collect_unexpected_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(DrawingMachineError):
        comfy._validate_status_payload({"value": StringSubclass("bad")})
    nested: dict[str, object] = {}
    current = nested
    for _ in range(comfy._MAX_STATUS_JSON_DEPTH + 2):
        child: dict[str, object] = {}
        current["x"] = child
        current = child
    with pytest.raises(DrawingMachineError):
        comfy._validate_status_payload(nested)
    with pytest.raises(DrawingMachineError):
        comfy._validate_status_messages([["execution_start", {}]] * (comfy._MAX_STATUS_MESSAGES + 1))
    monkeypatch.setattr(comfy, "_parse_history_item", lambda value: (_ for _ in ()).throw(RuntimeError("bad")))
    with pytest.raises(DrawingMachineError):
        comfy.collect_outputs({})


def test_image_validation_type_empty_invalid_and_valid_png() -> None:
    assert comfy._image_is_valid(None) is False
    assert comfy._image_is_valid(b"") is False
    assert comfy._image_is_valid(b"not-image") is False
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    assert comfy._image_is_valid(output.getvalue()) is True
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="BMP")
    assert comfy._image_is_valid(output.getvalue()) is False


def test_nested_workflow_snapshot_and_noncanonical_profile_path(tmp_path: Path) -> None:
    with pytest.raises(DrawingMachineError):
        comfy._read_workflow_snapshot(
            PurePosixPath("workflow.json"), profile_path=Path(f"{tmp_path}/a/../provider.toml")
        )
    profile = tmp_path / "provider.toml"
    profile.write_text("schema_version=1\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    workflow = nested / "workflow.json"
    workflow.write_text('{"node":{"inputs":{}}}', encoding="utf-8")
    resolved, document = comfy._read_workflow_snapshot(PurePosixPath("nested/workflow.json"), profile_path=profile)
    assert resolved == workflow and "node" in document
