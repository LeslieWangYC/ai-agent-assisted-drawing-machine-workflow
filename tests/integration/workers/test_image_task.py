from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from drawingmachine.adapters.providers.local_comfyui import HttpTransport
from drawingmachine.adapters.workers import SpawnProcessSupervisor
from drawingmachine.adapters.workers.worker_entrypoint import execute_task
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.workers import WorkerInputArtifact, WorkerKind, WorkerOutcomeV1, WorkerStatus, WorkerTaskV1

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures/package_b/golden"


def artifact(path: Path, role: str = "input_image") -> WorkerInputArtifact:
    content = path.read_bytes()
    return WorkerInputArtifact(
        role, str(path.resolve()), hashlib.sha256(content).hexdigest(), len(content), "image/png"
    )


def task(
    tmp_path: Path, kind: WorkerKind, payload: JsonObject, source: Path, *, role: str = "input_image"
) -> WorkerTaskV1:
    staging = tmp_path / f"staging-{kind.value.replace('.', '-')}"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        f"task-{kind.value.replace('.', '-')}",
        "job-1",
        2,
        f"attempt-{kind.value.replace('.', '-')}",
        kind,
        (artifact(source, role),),
        {"application": "a" * 64},
        str(staging.resolve()),
        payload,
    )


def assert_bundle(selected: WorkerTaskV1, outcome: WorkerOutcomeV1, roles: set[str]) -> None:
    assert outcome.status is WorkerStatus.SUCCEEDED
    assert {item.role for item in outcome.artifacts} == roles
    staging = Path(selected.staging_dir)
    assert {path.name for path in staging.iterdir()} == {item.relative_path for item in outcome.artifacts}
    for item in outcome.artifacts:
        output = staging / item.relative_path
        assert output.is_file() and not output.is_symlink()
        content = output.read_bytes()
        assert item.size_bytes == len(content)
        assert item.sha256 == hashlib.sha256(content).hexdigest()


def test_direct_image_emits_declared_fsynced_bundle(tmp_path: Path) -> None:
    selected = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {
            "mode": "direct",
            "source_name": "outline.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        },
        GOLDEN / "outline.png",
    )
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert_bundle(
        selected,
        outcome,
        {"route_decision", "processed_image_raw", "direct_report", "processed_image", "normalization_report"},
    )


def test_auto_image_prepare_is_route_only(tmp_path: Path) -> None:
    selected = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {
            "mode": "direct",
            "source_name": "outline.png",
            "route_mode": "auto",
            "semantic_assessment": None,
            "normalization": {},
        },
        GOLDEN / "outline.png",
    )
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert_bundle(selected, outcome, {"route_decision"})


def test_direct_image_fsyncs_every_artifact_and_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.adapters.workers.tasks as task_support

    selected = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {
            "mode": "direct",
            "source_name": "outline.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        },
        GOLDEN / "outline.png",
    )
    real_fsync = task_support.os.fsync
    synced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(task_support.os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(task_support.os, "fsync", record_fsync)
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert sum(stat.S_ISREG(mode) for mode in synced_modes) == 5
    assert sum(stat.S_ISDIR(mode) for mode in synced_modes) == 1


def test_direct_image_executes_through_spawn_supervisor(tmp_path: Path) -> None:
    selected = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {
            "mode": "direct",
            "source_name": "outline.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        },
        GOLDEN / "outline.png",
    )
    supervisor = SpawnProcessSupervisor(
        entrypoint_module="drawingmachine.adapters.workers.worker_entrypoint",
        entrypoint_name="execute_task",
    )
    try:
        handle = supervisor.submit(selected)
        outcome = supervisor.wait(handle, timeout_seconds=5.0, cancel_requested=lambda: False)
    finally:
        supervisor.close()

    assert_bundle(
        selected,
        outcome,
        {"route_decision", "processed_image_raw", "direct_report", "processed_image", "normalization_report"},
    )


class FakeTransport(HttpTransport):
    def __init__(self, image: bytes, history: JsonObject) -> None:
        self.image = image
        self.history = history
        self.calls: list[str] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        del payload, timeout_seconds
        self.calls.append(f"{method} {url}")
        if url.endswith("/system_stats"):
            return {"devices": []}
        if url.endswith("/prompt"):
            return {"prompt_id": "job-1"}
        return {"job-1": self.history}

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
        self.calls.append(f"UPLOAD {url}")
        return {"name": "uploaded.png"}

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.calls.append(f"GET {url}")
        return self.image


def write_provider_files(tmp_path: Path) -> tuple[Path, JsonObject]:
    workflow = tmp_path / "workflow.json"
    workflow.write_text(
        json.dumps(
            {
                "25": {"inputs": {"image": "old.png"}},
                "27": {"inputs": {"prompt": "Template prompt."}},
                "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
                "18": {"inputs": {"filename_prefix": "old"}},
                "221": {"inputs": {"scale_to_length": 576}},
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "provider.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    profile: JsonObject = {
        "schema_version": 1,
        "profile": {
            "name": "local-comfyui",
            "endpoint": "https://fake.invalid",
            "workflow_template": workflow.name,
            "model_family": "qwen-image-edit-2511",
            "scale_to_length": 576,
            "timeout_seconds": 240.0,
            "poll_interval_seconds": 2.0,
            "free_after_run": False,
            "workflow_nodes": {"load_image": "25", "prompt": "27", "sampler": "28", "save_image": "18", "scale": "221"},
            "sampler_defaults": {"steps": None, "cfg": None, "denoise": None},
            "live_execution_requires_execute_flag": True,
        },
    }
    return config_path, profile


def test_provider_image_uses_injected_fake_transport_then_normalizes(tmp_path: Path) -> None:
    config_path, profile = write_provider_files(tmp_path)
    source = GOLDEN / "outline.png"
    history = json.loads((GOLDEN / "comfy_history_multi_output.json").read_text(encoding="utf-8"))
    fake = FakeTransport(source.read_bytes(), history)
    selected = task(
        tmp_path,
        WorkerKind.PROVIDER_LOCAL_COMFYUI,
        {"prompt": None, "provider_profile": profile, "provider_config_path": str(config_path.resolve())},
        source,
    )
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json(), provider_transport_factory=lambda: fake))
    assert_bundle(
        selected,
        outcome,
        {"provider_request", "provider_response", "provider_handoff", "processed_image_raw"},
    )
    assert fake.calls

    raw = Path(selected.staging_dir) / next(
        a.relative_path for a in outcome.artifacts if a.role == "processed_image_raw"
    )
    normalize = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {"mode": "normalize_existing", "source_name": "provider.png", "normalization": {}},
        raw,
        role="processed_image_raw",
    )
    normalized = WorkerOutcomeV1.from_json(execute_task(normalize.to_json()))
    assert_bundle(normalize, normalized, {"processed_image", "normalization_report"})


def test_worker_environment_has_no_database_socket_or_hardware_fields(tmp_path: Path) -> None:
    selected = task(
        tmp_path,
        WorkerKind.IMAGE_PREPARE,
        {"mode": "normalize_existing", "source_name": "provider.png", "normalization": {}},
        GOLDEN / "outline.png",
        role="processed_image_raw",
    )
    forbidden = {"database", "socket", "serial", "bridge", "openclaw", "controller", "hardware"}

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return {str(key).lower() for key in value} | {item for child in value.values() for item in keys(child)}
        if isinstance(value, list):
            return {item for child in value for item in keys(child)}
        return set()

    assert forbidden.isdisjoint(keys(selected.to_json()))
