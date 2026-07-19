from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.adapters.workers.task_registry import TaskRegistry, build_task_registry
from drawingmachine.adapters.workers.worker_entrypoint import execute_task
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.workers import (
    WorkerInputArtifact,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)

MAX_WORKER_INPUT_BYTES = 32 * 1024 * 1024

PLANNING_VALUES: dict[str, Any] = {
    "canvas_width_mm": 120.0,
    "canvas_height_mm": 120.0,
    "pen_width_mm": 0.5,
    "min_gap_mm": 0.8,
    "threshold": None,
    "invert": None,
    "min_component_area_px": 8,
    "simplify_tolerance_mm": 0.12,
    "min_path_length_mm": 0.6,
    "drop_short_stroke_mm": 0.35,
    "merge_endpoint_distance_mm": 0.45,
    "merge_angle_deg": 35.0,
    "dedupe_short_path_length_mm": 2.0,
    "dedupe_distance_mm": 0.3,
    "dedupe_angle_deg": 25.0,
    "dedupe_overlap_ratio": 0.65,
    "hatch_spacing_mm": 0.8,
    "hatch_min_run_mm": 0.8,
    "fill_min_thickness_mm": 0.85,
}
GCODE_VALUES: dict[str, Any] = {
    "hardware_canvas_width_mm": 144.0,
    "hardware_canvas_height_mm": 144.0,
    "machine_width_mm": 192.0,
    "machine_height_mm": 192.0,
    "paper_center_x": 96.0,
    "paper_center_y": 96.0,
    "pen_up_z": 3.5,
    "pen_down_z": 0.0,
    "feed_travel": 1200.0,
    "feed_draw": 900.0,
    "feed_pen_down": 100.0,
    "feed_pen_up": 400.0,
    "max_feed": 1200.0,
    "work_coordinate": "G54",
    "align_mode": "center",
    "mirror_y": True,
    "safe_start": True,
    "path_mode": "stroke",
}
COMPLEXITY_LIMITS: dict[str, Any] = {
    "soft_component_count": 160,
    "soft_skeleton_pixels": 26000,
    "soft_boundary_pixels": 45000,
    "soft_transitions_total": 68000,
    "hard_component_count": 260,
    "hard_skeleton_pixels": 34000,
    "hard_boundary_pixels": 60000,
    "hard_transitions_total": 100000,
    "threshold": None,
    "invert": None,
    "min_component_area_px": 8,
}
DEDUPE_PROFILE: dict[str, Any] = {
    "dedupe_short_path_length_mm": 2.0,
    "dedupe_distance_mm": 0.3,
    "dedupe_angle_deg": 25.0,
    "dedupe_overlap_ratio": 0.65,
}


def machine_profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile": {
            "name": "stage1",
            "planning": copy.deepcopy(PLANNING_VALUES),
            "gcode": copy.deepcopy(GCODE_VALUES),
        },
    }


def planning_payload() -> dict[str, Any]:
    return {
        "planning_profile": {
            "schema_version": 1,
            "profile": {"name": "stage1", "planning": copy.deepcopy(PLANNING_VALUES)},
        },
        "complexity_limits": copy.deepcopy(COMPLEXITY_LIMITS),
        "base_dedupe_profile": copy.deepcopy(DEDUPE_PROFILE),
    }


def provider_payload() -> dict[str, Any]:
    return {
        "prompt": None,
        "provider_profile": {
            "schema_version": 1,
            "profile": {
                "name": "local-comfyui",
                "endpoint": "https://fake.invalid",
                "workflow_template": "workflow.json",
                "model_family": "qwen-image-edit-2511",
                "scale_to_length": 576,
                "timeout_seconds": 240.0,
                "poll_interval_seconds": 2.0,
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
        },
        "provider_config_path": "/missing/provider.toml",
    }


def invalid_payload_case(case: str) -> tuple[WorkerKind, str, dict[str, Any]]:
    if case == "image-direct-literal":
        return (
            WorkerKind.IMAGE_PREPARE,
            "input_image",
            {
                "mode": "direct",
                "source_name": "input.png",
                "route_mode": "not-a-route",
                "semantic_assessment": None,
                "normalization": {},
            },
        )
    if case == "image-direct-semantic-nested":
        return (
            WorkerKind.IMAGE_PREPARE,
            "input_image",
            {
                "mode": "direct",
                "source_name": "input.png",
                "route_mode": "direct",
                "semantic_assessment": [],
                "normalization": {},
            },
        )
    if case == "image-normalize-type":
        return (
            WorkerKind.IMAGE_PREPARE,
            "processed_image_raw",
            {
                "mode": "normalize_existing",
                "source_name": "input.png",
                "normalization": {"threshold": True},
            },
        )
    if case == "provider-prompt-type":
        payload = provider_payload()
        payload["prompt"] = 7
        return WorkerKind.PROVIDER_LOCAL_COMFYUI, "input_image", payload
    if case == "provider-profile-numeric":
        payload = provider_payload()
        cast(dict[str, Any], cast(dict[str, Any], payload["provider_profile"])["profile"])["scale_to_length"] = True
        return WorkerKind.PROVIDER_LOCAL_COMFYUI, "input_image", payload
    if case == "planning-profile-numeric":
        payload = planning_payload()
        cast(
            dict[str, Any],
            cast(dict[str, Any], cast(dict[str, Any], payload["planning_profile"])["profile"])["planning"],
        )["canvas_width_mm"] = True
        return WorkerKind.PATHS_PLAN, "processed_image", payload
    if case == "planning-limits-type":
        payload = planning_payload()
        cast(dict[str, Any], payload["complexity_limits"])["soft_component_count"] = True
        return WorkerKind.PATHS_PLAN, "processed_image", payload
    if case == "planning-dedupe-range":
        payload = planning_payload()
        cast(dict[str, Any], payload["base_dedupe_profile"])["dedupe_overlap_ratio"] = 1.5
        return WorkerKind.PATHS_PLAN, "processed_image", payload
    if case == "gcode-build-literal":
        profile = machine_profile()
        cast(dict[str, Any], cast(dict[str, Any], profile["profile"])["gcode"])["path_mode"] = "preview"
        return WorkerKind.GCODE_BUILD, "path_plan", {"machine_build_profile": profile}
    if case == "gcode-build-numeric":
        profile = machine_profile()
        cast(dict[str, Any], cast(dict[str, Any], profile["profile"])["gcode"])["feed_draw"] = True
        return WorkerKind.GCODE_BUILD, "path_plan", {"machine_build_profile": profile}
    if case == "gcode-check-sha":
        return (
            WorkerKind.GCODE_CHECK,
            "gcode",
            {
                "machine_build_profile": machine_profile(),
                "expected_gcode_sha256": "not-a-sha256",
            },
        )
    if case == "gcode-check-nested-literal":
        profile = machine_profile()
        cast(dict[str, Any], cast(dict[str, Any], profile["profile"])["gcode"])["safe_start"] = False
        return (
            WorkerKind.GCODE_CHECK,
            "gcode",
            {
                "machine_build_profile": profile,
                "expected_gcode_sha256": "a" * 64,
            },
        )
    raise AssertionError(f"unknown case: {case}")


def boundary_task(
    tmp_path: Path,
    *,
    kind: WorkerKind,
    role: str,
    payload: dict[str, Any],
    declared_size: int | None = None,
    source_size: int | None = None,
) -> WorkerTaskV1:
    source = tmp_path / "boundary-input.bin"
    if source_size is None:
        source.write_bytes(b"input")
    else:
        with source.open("wb") as stream:
            stream.truncate(source_size)
    actual_size = source.stat().st_size
    staging = tmp_path / "boundary-staging"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        "boundary-task",
        "job-1",
        1,
        "boundary-attempt",
        kind,
        (
            WorkerInputArtifact(
                role,
                str(source.resolve()),
                hashlib.sha256(source.read_bytes()).hexdigest() if actual_size <= 1024 else "a" * 64,
                actual_size if declared_size is None else declared_size,
                "application/octet-stream",
            ),
        ),
        {"application": "a" * 64},
        str(staging.resolve()),
        cast(JsonObject, payload),
    )


def make_task(tmp_path: Path, *, digest: str | None = None) -> WorkerTaskV1:
    source = tmp_path / "input.png"
    source.write_bytes(b"not-a-png")
    staging = tmp_path / "staging"
    staging.mkdir()
    return WorkerTaskV1(
        schema_version=1,
        task_id="task-1",
        job_id="job-1",
        job_revision=7,
        attempt_id="attempt-1",
        kind=WorkerKind.IMAGE_PREPARE,
        input_artifacts=(
            WorkerInputArtifact(
                role="input_image",
                absolute_path=str(source.resolve()),
                sha256=digest or hashlib.sha256(source.read_bytes()).hexdigest(),
                size_bytes=source.stat().st_size,
                media_type="image/png",
            ),
        ),
        config_digests={"application": "a" * 64},
        staging_dir=str(staging.resolve()),
        payload={
            "mode": "direct",
            "source_name": "input.png",
            "route_mode": "direct",
            "semantic_assessment": None,
            "normalization": {},
        },
    )


def test_registry_is_closed_and_does_not_register_graph_clean() -> None:
    registry = build_task_registry()

    assert registry.kinds == (
        WorkerKind.IMAGE_PREPARE,
        WorkerKind.PROVIDER_LOCAL_COMFYUI,
        WorkerKind.PATHS_PLAN,
        WorkerKind.GCODE_BUILD,
        WorkerKind.GCODE_CHECK,
    )
    assert all("graph" not in kind.value for kind in registry.kinds)


def test_registry_rejects_duplicate_registration_and_stale_outcome_identity(tmp_path: Path) -> None:
    registry = TaskRegistry()

    def stale(task: WorkerTaskV1) -> WorkerOutcomeV1:
        return WorkerOutcomeV1(
            1,
            task.task_id,
            task.job_id,
            task.job_revision + 1,
            task.attempt_id,
            WorkerStatus.SUCCEEDED,
            (),
            {},
            None,
        )

    registry.register(WorkerKind.IMAGE_PREPARE, stale)
    with pytest.raises(DrawingMachineError, match="already registered"):
        registry.register(WorkerKind.IMAGE_PREPARE, stale)
    with pytest.raises(DrawingMachineError, match="identity"):
        registry.execute(make_task(tmp_path))


def test_execute_task_rejects_input_digest_before_image_decode(tmp_path: Path) -> None:
    outcome = WorkerOutcomeV1.from_json(execute_task(make_task(tmp_path, digest="b" * 64).to_json()))

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "WORKER_INPUT_INVALID"


def test_execute_task_accepts_supervisor_typed_task(tmp_path: Path) -> None:
    selected = make_task(tmp_path)
    source = Path(selected.input_artifacts[0].absolute_path)
    source.write_bytes((Path(__file__).resolve().parents[3] / "fixtures/package_b/golden/outline.png").read_bytes())
    content = source.read_bytes()
    selected = WorkerTaskV1(
        selected.schema_version,
        selected.task_id,
        selected.job_id,
        selected.job_revision,
        selected.attempt_id,
        selected.kind,
        (
            WorkerInputArtifact(
                "input_image",
                str(source.resolve()),
                hashlib.sha256(content).hexdigest(),
                len(content),
                "image/png",
            ),
        ),
        selected.config_digests,
        selected.staging_dir,
        selected.payload,
    )

    outcome = WorkerOutcomeV1.from_json(execute_task(selected))

    assert outcome.status is WorkerStatus.SUCCEEDED


def test_write_failure_is_fail_closed_and_preserves_existing_file(tmp_path: Path) -> None:
    selected = make_task(tmp_path)
    source = Path(selected.input_artifacts[0].absolute_path)
    content = (Path(__file__).resolve().parents[3] / "fixtures/package_b/golden/outline.png").read_bytes()
    source.write_bytes(content)
    selected = WorkerTaskV1(
        selected.schema_version,
        selected.task_id,
        selected.job_id,
        selected.job_revision,
        selected.attempt_id,
        selected.kind,
        (
            WorkerInputArtifact(
                "input_image",
                str(source.resolve()),
                hashlib.sha256(content).hexdigest(),
                len(content),
                "image/png",
            ),
        ),
        selected.config_digests,
        selected.staging_dir,
        selected.payload,
    )
    existing = Path(selected.staging_dir) / "route_decision.json"
    existing.write_bytes(b"preexisting")

    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "WORKER_OUTPUT_WRITE_FAILED"
    assert existing.read_bytes() == b"preexisting"


def test_execute_task_redacts_unexpected_exception_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from drawingmachine.adapters.workers.tasks import image

    secret = "transport-secret-and-local-path"

    def explode(_task: WorkerTaskV1) -> WorkerOutcomeV1:
        raise RuntimeError(secret)

    monkeypatch.setattr(image, "run_image_prepare", explode)
    rendered: dict[str, Any] = execute_task(make_task(tmp_path).to_json())

    assert rendered["status"] == "FAILED"
    assert secret not in str(rendered)


@pytest.mark.parametrize(
    "case",
    [
        "image-direct-literal",
        "image-direct-semantic-nested",
        "image-normalize-type",
        "provider-prompt-type",
        "provider-profile-numeric",
        "planning-profile-numeric",
        "planning-limits-type",
        "planning-dedupe-range",
        "gcode-build-literal",
        "gcode-build-numeric",
        "gcode-check-sha",
        "gcode-check-nested-literal",
    ],
)
def test_invalid_production_payloads_fail_before_input_or_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    import drawingmachine.adapters.workers.tasks as task_support
    from drawingmachine.adapters.workers.tasks import image

    kind, role, payload = invalid_payload_case(case)
    selected = boundary_task(tmp_path, kind=kind, role=role, payload=payload)
    reads: list[int] = []
    factories: list[str] = []
    snapshots: list[str] = []

    def deny_read(_descriptor: int, size: int) -> bytes:
        reads.append(size)
        raise AssertionError("input read occurred before payload rejection")

    def deny_factory() -> object:
        factories.append("factory")
        raise AssertionError("provider factory occurred before payload rejection")

    def deny_snapshot(*_args: object, **_kwargs: object) -> object:
        snapshots.append("snapshot")
        raise AssertionError("workflow snapshot occurred before payload rejection")

    monkeypatch.setattr(task_support.os, "read", deny_read)
    monkeypatch.setattr(image.LocalComfyUIConfig, "from_profile", deny_snapshot)
    rendered = execute_task(selected.to_json(), provider_transport_factory=deny_factory)  # type: ignore[arg-type]
    outcome = WorkerOutcomeV1.from_json(rendered)

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "WORKER_INPUT_INVALID"
    assert reads == []
    assert factories == []
    assert snapshots == []
    assert "AssertionError" not in str(rendered)


@pytest.mark.parametrize("mode", ["declared-mismatch", "descriptor-over-limit"])
def test_invalid_descriptor_size_is_rejected_before_first_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    import drawingmachine.adapters.workers.tasks as task_support

    payload = {
        "mode": "direct",
        "source_name": "input.png",
        "route_mode": "direct",
        "semantic_assessment": None,
        "normalization": {},
    }
    if mode == "declared-mismatch":
        selected = boundary_task(
            tmp_path,
            kind=WorkerKind.IMAGE_PREPARE,
            role="input_image",
            payload=payload,
            declared_size=6,
        )
    else:
        selected = boundary_task(
            tmp_path,
            kind=WorkerKind.IMAGE_PREPARE,
            role="input_image",
            payload=payload,
            source_size=MAX_WORKER_INPUT_BYTES + 1,
        )
    reads: list[int] = []

    def deny_read(_descriptor: int, size: int) -> bytes:
        reads.append(size)
        raise AssertionError("descriptor was read before size admission")

    monkeypatch.setattr(task_support.os, "read", deny_read)
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "WORKER_INPUT_INVALID"
    assert reads == []


def test_exact_declared_input_below_cap_is_streamed_normally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.adapters.workers.tasks as task_support

    selected = make_task(tmp_path)
    source = Path(selected.input_artifacts[0].absolute_path)
    content = (Path(__file__).resolve().parents[3] / "fixtures/package_b/golden/outline.png").read_bytes()
    source.write_bytes(content)
    selected = WorkerTaskV1(
        selected.schema_version,
        selected.task_id,
        selected.job_id,
        selected.job_revision,
        selected.attempt_id,
        selected.kind,
        (
            WorkerInputArtifact(
                "input_image", str(source.resolve()), hashlib.sha256(content).hexdigest(), len(content), "image/png"
            ),
        ),
        selected.config_digests,
        selected.staging_dir,
        selected.payload,
    )
    real_read = task_support.os.read
    reads: list[int] = []

    def record_read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(task_support.os, "read", record_read)
    outcome = WorkerOutcomeV1.from_json(execute_task(selected.to_json()))

    assert outcome.status is WorkerStatus.SUCCEEDED
    assert reads
    assert len(content) < MAX_WORKER_INPUT_BYTES
    assert getattr(task_support, "MAX_WORKER_INPUT_BYTES", None) == MAX_WORKER_INPUT_BYTES
