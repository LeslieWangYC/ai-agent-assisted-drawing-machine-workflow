from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

import drawingmachine.application.offline_job_chain as offline_module
from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.jobs import ReviewContinuationV1, RouteMode, WorkflowSubmissionV1
from drawingmachine.application.offline_job_chain import OfflineJobChain
from drawingmachine.config import ApplicationConfig, ConfigBundle, LogLevel, ProfileEnvelope, XdgPaths
from drawingmachine.domain.gcode import build_gcode, parse_machine_build_profile, render_preview
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.jobs import (
    ArtifactRef,
    AuditRecord,
    JobEvent,
    JobRecord,
    JobState,
    JobTransition,
    RequesterIdentity,
    allowed_transition,
)
from drawingmachine.domain.workflow import ProcessedImageReview
from drawingmachine.domain.workflow.routing import RouteContext, decide_route
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import ExpectedArtifact, PromotedBundle, WorkerArtifact
from drawingmachine.ports.repository import RepositoryHealth
from drawingmachine.ports.workers import (
    WorkerHandle,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)

_SHA = "a" * 64
_PLANNING: JsonObject = {
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
_GCODE: JsonObject = {
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
_PROVIDER: JsonObject = {
    "name": "local-comfyui",
    "endpoint": "https://fake.invalid",
    "workflow_template": "workflow.json",
    "model_family": "qwen-image-edit-2511",
    "scale_to_length": 576,
    "timeout_seconds": 1.0,
    "poll_interval_seconds": 0.01,
    "free_after_run": False,
    "workflow_nodes": {"load_image": "25", "prompt": "27", "sampler": "28", "save_image": "18", "scale": "221"},
    "sampler_defaults": {"steps": None, "cfg": None, "denoise": None},
    "live_execution_requires_execute_flag": True,
}


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


class MemoryTransaction:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def create_job(
        self,
        job: JobRecord,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> None:
        self.repository.jobs[job.job_id] = job
        self.repository.artifacts[job.job_id] = list(artifacts)
        self.repository.events[job.job_id] = [event]
        self.repository.trace.append("repository-write")

    def transition_job(
        self,
        transition: JobTransition,
        *,
        artifacts: tuple[ArtifactRef, ...],
        event: JobEvent,
        audit: AuditRecord,
    ) -> JobRecord:
        current = self.repository.jobs[transition.job_id]
        updated = replace(
            current,
            state=transition.result_state,
            revision=current.revision + 1,
            blocker=transition.blocker,
            error=transition.error,
            ready_snapshot=transition.ready_snapshot,
            updated_at=self.repository.clock.now(),
        )
        self.repository.jobs[current.job_id] = updated
        self.repository.artifacts[current.job_id].extend(artifacts)
        self.repository.events[current.job_id].append(event)
        self.repository.trace.append("repository-write")
        return updated

    def append_audit(self, event_id: str, event_type: str, request_id: str | None, payload: JsonObject) -> None:
        del event_id, event_type, request_id, payload


class MemoryRepository:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.jobs: dict[str, JobRecord] = {}
        self.artifacts: dict[str, list[ArtifactRef]] = {}
        self.events: dict[str, list[JobEvent]] = {}
        self.trace: list[str] = []
        self.closed = False

    def initialize(self) -> int:
        return 2

    def health(self) -> RepositoryHealth:
        return RepositoryHealth(2, "wal", True)

    @contextmanager
    def transaction(self) -> Iterator[MemoryTransaction]:
        transaction = MemoryTransaction(self)
        yield transaction
        self.trace.append("commit")

    def append_audit(self, event_id: str, event_type: str, request_id: str | None, payload: JsonObject) -> None:
        del event_id, event_type, request_id, payload

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def list_jobs_in_states(self, states: frozenset[JobState]) -> tuple[JobRecord, ...]:
        return tuple(job for job in self.jobs.values() if job.state in states)

    def list_artifacts(self, job_id: str) -> tuple[ArtifactRef, ...]:
        return tuple(self.artifacts.get(job_id, ()))

    def list_job_events(self, job_id: str) -> tuple[JobEvent, ...]:
        return tuple(self.events.get(job_id, ()))

    def close(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(
        self,
        root: Path,
        trace: list[str],
        *,
        enforce_order: bool = True,
        route: str = "A_DIRECT",
    ) -> None:
        self.root = root
        self.trace = trace
        self.enforce_order = enforce_order
        self.route = route
        self.projections: list[JobState] = []
        self.discarded: list[tuple[str, str]] = []
        self.promoted: list[PromotedBundle] = []

    def import_file(
        self,
        job_id: str,
        attempt_id: str,
        source: Path,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> PromotedBundle:
        content = source.read_bytes()
        assert len(content) <= max_bytes
        destination = self.root / job_id / "artifacts" / attempt_id
        output = destination / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        artifact = ArtifactRef(
            role,
            f"artifacts/{attempt_id}/{relative_path}",
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type,
        )
        self.trace.append("promotion")
        bundle = PromotedBundle(job_id, attempt_id, destination, (artifact,))
        self.promoted.append(bundle)
        return bundle

    def create_staging(self, job_id: str, attempt_id: str) -> Path:
        staging = self.root / ".staging" / job_id / attempt_id
        staging.mkdir(parents=True)
        return staging

    def read_staged_bytes(
        self,
        job_id: str,
        attempt_id: str,
        artifact: WorkerArtifact,
        *,
        expected_relative_path: str,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        assert artifact.relative_path == expected_relative_path
        assert artifact.media_type == expected_media_type
        content = (self.root / ".staging" / job_id / attempt_id / expected_relative_path).read_bytes()
        assert len(content) <= max_bytes
        assert len(content) == artifact.size_bytes
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
        return content

    def validate_and_promote(
        self,
        job_id: str,
        attempt_id: str,
        artifacts: tuple[WorkerArtifact, ...],
        *,
        expected: tuple[ExpectedArtifact, ...],
    ) -> PromotedBundle:
        assert {(item.role, item.relative_path, item.media_type) for item in artifacts} == {
            (item.role, item.relative_path, item.media_type) for item in expected
        }
        destination = self.root / job_id / "artifacts" / attempt_id
        destination.mkdir(parents=True)
        refs = []
        for artifact in artifacts:
            output = destination / artifact.relative_path
            source = self.root / ".staging" / job_id / attempt_id / artifact.relative_path
            content = source.read_bytes()
            if artifact.role == "route_decision":
                document = json.loads(content)
                document.update(
                    {
                        "route": self.route,
                        "direct_route_allowed": self.route == "A_DIRECT",
                        "next_stage": (
                            "prepare_direct_processed_image" if self.route == "A_DIRECT" else "qwen_image_edit"
                        ),
                    }
                )
                content = _json_bytes(document)
            output.write_bytes(content)
            refs.append(
                ArtifactRef(
                    artifact.role,
                    f"artifacts/{attempt_id}/{artifact.relative_path}",
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    artifact.media_type,
                )
            )
        self.trace.append("promotion")
        bundle = PromotedBundle(job_id, attempt_id, destination, tuple(refs))
        self.promoted.append(bundle)
        return bundle

    def resolve(self, job_id: str, artifact: ArtifactRef) -> Path:
        assert job_id
        return self.root / job_id / artifact.relative_path

    def read_bytes(
        self,
        job_id: str,
        artifact: ArtifactRef,
        *,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        assert artifact.media_type == expected_media_type
        content = (self.root / job_id / artifact.relative_path).read_bytes()
        assert len(content) <= max_bytes
        assert len(content) == artifact.size_bytes
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
        return content

    def discard_staging(self, job_id: str, attempt_id: str) -> None:
        self.discarded.append((job_id, attempt_id))
        staging = self.root / ".staging" / job_id / attempt_id
        if staging.exists():
            import shutil

            shutil.rmtree(staging)

    def write_projection(self, job: JobRecord, artifacts: tuple[ArtifactRef, ...]) -> None:
        del artifacts
        if self.enforce_order:
            assert self.trace[-1] == "commit"
        self.projections.append(job.state)
        self.trace.append("projection")


class FakeHandle:
    def __init__(self, task_id: str, attempt_id: str) -> None:
        self._task_id = task_id
        self._attempt_id = attempt_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    def is_alive(self) -> bool:
        return False


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")


def _fixture_document(name: str) -> JsonObject:
    value = json.loads((Path("tests/fixtures/package_b/golden/expected") / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _task_machine_profile(task: WorkerTaskV1) -> object:
    raw = cast(Mapping[str, object], task.payload["machine_build_profile"])
    envelope = ProfileEnvelope(cast(int, raw["schema_version"]), cast(JsonObject, raw["profile"]))
    return parse_machine_build_profile(envelope)


def _provider_request_document(task: WorkerTaskV1) -> JsonObject:
    source = task.input_artifacts[0]
    return {
        "schema": "local_comfyui_qwen_image_edit_request_record_v1",
        "created_at": "2026-07-11T00:00:00+00:00",
        "provider": "local-comfyui",
        "provider_config": "local_comfyui_provider_config.json",
        "endpoint": "https://fake.invalid",
        "workflow_template": "workflow.json",
        "workflow_nodes": {"load_image": "25", "prompt": "27", "sampler": "28", "save_image": "18", "scale": "221"},
        "source_image": {
            "kind": "local_file",
            "value": source.absolute_path,
            "copied_input": source.absolute_path,
            "name": Path(source.absolute_path).name,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "mime_type": source.media_type,
            "size_px": None,
            "mode": None,
        },
        "prompt": {
            "path": "qwen_image_edit_prompt.txt",
            "sha256": "0" * 64,
            "source": "workflow_template_default",
            "workflow_node": "27",
        },
        "scale_to_side": "longest",
        "scale_to_length": 576,
        "sampler": {"seed": 1, "steps": None, "cfg": None, "denoise": None},
        "output_prefix": task.job_id,
        "dry_run": False,
        "execute": True,
        "timeout_s": 1.0,
    }


def _fake_artifact_content(task: WorkerTaskV1, role: str, *, route: str = "A_DIRECT") -> bytes:
    if role in {"processed_image_raw", "processed_image"}:
        return Path("tests/fixtures/package_b/golden/outline.png").read_bytes()
    if role == "route_decision":
        document = _fixture_document("route.json")
        document.update(
            {
                "job_name": task.job_id,
                "input_image": cast(str, task.payload["source_name"]),
                "route": route,
                "direct_route_allowed": route == "A_DIRECT",
                "next_stage": "prepare_direct_processed_image" if route == "A_DIRECT" else "qwen_image_edit",
            }
        )
        return _json_bytes(document)
    if role == "direct_report":
        return _json_bytes(
            {
                "schema": "direct_processed_image_report_v1",
                "created_at": "2026-07-11T00:00:00+00:00",
                "input": cast(str, task.payload["source_name"]),
                "output": "processed_image_raw.png",
                "size_px": [160, 160],
                "binarization": {
                    "method": "otsu",
                    "threshold": 1,
                    "inverted": False,
                    "min_component_area_px": 8,
                    "median_filter_size": 3,
                },
                "components_before": 1,
                "components_after": 1,
                "small_component_pixels_removed": 0,
                "foreground_pixels": 438,
                "foreground_ratio": 0.01710938,
                "content_bbox_px": [16, 16, 143, 143],
                "hardware_touched": False,
            }
        )
    if role == "normalization_report":
        document = _fixture_document("normalize_report.json")
        document.update(
            {
                "input": cast(str, task.payload["source_name"]),
                "output": "processed_image.png",
                "created_at": "2026-07-11T00:00:00+00:00",
            }
        )
        return _json_bytes(document)
    if role == "provider_request":
        return _json_bytes(_provider_request_document(task))
    if role == "provider_response":
        return _json_bytes(
            {
                "schema": "local_comfyui_qwen_image_edit_response_record_v1",
                "created_at": "2026-07-11T00:00:00+00:00",
                "status": "PROCESSED_IMAGE_CREATED",
                "system_stats": {},
                "upload": {"name": "input.png", "subfolder": "", "type": "output"},
                "prompt_id": "prompt-1",
                "history": {},
                "outputs": [{"filename": "processed.png", "subfolder": "", "type": "output"}],
                "processed_image": "processed.png",
                "free_requested": False,
            }
        )
    if role == "provider_handoff":
        request = _provider_request_document(task)
        return _json_bytes(
            {
                "schema": "cnc_drawable_workflow_handoff_v1",
                "status": "PROCESSED_IMAGE_CREATED",
                "created_at": "2026-07-11T00:00:00+00:00",
                "job_name": task.job_id,
                "stage": "qwen_image_edit_processed_image",
                "provider": {
                    "name": "local-comfyui",
                    "endpoint": "https://fake.invalid",
                    "workflow_template": "workflow.json",
                    "workflow_nodes": request["workflow_nodes"],
                    "prompt_id": "prompt-1",
                    "scale_to_length": 576,
                    "free_requested": False,
                },
                "source_image": request["source_image"],
                "prompt": request["prompt"],
                "artifacts": {
                    "out_dir": ".",
                    "request_record": "qwen_image_edit_request.json",
                    "response_record": "qwen_image_edit_response.json",
                    "processed_image": "processed.png",
                    "processed_image_sha256": hashlib.sha256(
                        Path("tests/fixtures/package_b/golden/outline.png").read_bytes()
                    ).hexdigest(),
                },
                "dry_run": False,
                "execute": True,
                "next_allowed_stage": "review_processed_image",
                "review_gate": {"required": True, "status": "pending"},
            }
        )
    if role == "complexity_report":
        return _json_bytes(_fixture_document("complexity.json"))
    if role == "path_plan":
        return _json_bytes(_fixture_document("outline.path_plan.json"))
    if role in {"preview_stroke_svg", "preview_final_svg", "gcode_preview_svg"}:
        return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n'
    if role in {"planning_report", "gcode_build_report"}:
        return b"# deterministic fake report\n"
    if task.kind is WorkerKind.GCODE_BUILD:
        path_plan = json.loads(Path(task.input_artifacts[0].absolute_path).read_text(encoding="utf-8"))
        result = build_gcode(cast(JsonObject, path_plan), cast(Any, _task_machine_profile(task)))
        if role == "selected_path_plan":
            return _json_bytes(result.selected_plan)
        if role == "gcode":
            return result.gcode.encode("utf-8")
        if role == "gcode_preview_report":
            return _json_bytes(render_preview(result.gcode, cast(Any, _task_machine_profile(task))).report)
    if task.kind is WorkerKind.GCODE_CHECK:
        gcode = Path(task.input_artifacts[0].absolute_path).read_text(encoding="utf-8")
        result = check_candidate(
            gcode,
            profile=cast(Any, _task_machine_profile(task)),
            expected_gcode_sha256=cast(str, task.payload["expected_gcode_sha256"]),
        )
        if role == "gcode_static":
            return _json_bytes(result.static_result.to_json())
        if role == "send_plan":
            return _json_bytes(result.send_plan.to_json())
        if role == "readiness":
            return _json_bytes(result.readiness.to_json())
    raise AssertionError(f"unsupported fake artifact role: {role}")


class FakeWorkers:
    def __init__(
        self,
        scripted: list[tuple[WorkerStatus, str | None]] | None = None,
        *,
        artifact_overrides: dict[str, bytes] | None = None,
    ) -> None:
        self.scripted = [] if scripted is None else list(scripted)
        self.artifact_overrides = {} if artifact_overrides is None else dict(artifact_overrides)
        self.tasks: list[WorkerTaskV1] = []
        self.closed = False

    def submit(self, task: WorkerTaskV1) -> WorkerHandle:
        self.tasks.append(task)
        return FakeHandle(task.task_id, task.attempt_id)

    def wait(self, handle: WorkerHandle, *, timeout_seconds: float, cancel_requested: object) -> WorkerOutcomeV1:
        del timeout_seconds, cancel_requested
        task = next(item for item in self.tasks if item.task_id == handle.task_id)
        status, code = self.scripted.pop(0) if self.scripted else (WorkerStatus.SUCCEEDED, None)
        image_roles = (
            (
                ("processed_image", "processed_image.png", "image/png"),
                ("normalization_report", "normalization_report.json", "application/json"),
            )
            if task.kind is WorkerKind.IMAGE_PREPARE and cast(str, task.payload["mode"]) == "normalize_existing"
            else (("route_decision", "route_decision.json", "application/json"),)
            if task.kind is WorkerKind.IMAGE_PREPARE and task.payload.get("route_mode") == "auto"
            else (
                ("route_decision", "route_decision.json", "application/json"),
                ("processed_image_raw", "processed_image_raw.png", "image/png"),
                ("direct_report", "direct_report.json", "application/json"),
                ("processed_image", "processed_image.png", "image/png"),
                ("normalization_report", "normalization_report.json", "application/json"),
            )
        )
        roles: dict[WorkerKind, tuple[tuple[str, str, str], ...]] = {
            WorkerKind.IMAGE_PREPARE: image_roles,
            WorkerKind.PROVIDER_LOCAL_COMFYUI: (
                ("provider_request", "provider_request.json", "application/json"),
                ("provider_response", "provider_response.json", "application/json"),
                ("provider_handoff", "provider_handoff.json", "application/json"),
                ("processed_image_raw", "processed_image_raw.png", "image/png"),
            ),
            WorkerKind.PATHS_PLAN: (
                ("complexity_report", "complexity_report.json", "application/json"),
                ("path_plan", "path_plan.json", "application/json"),
                ("preview_stroke_svg", "preview_stroke.svg", "image/svg+xml"),
                ("preview_final_svg", "preview_final.svg", "image/svg+xml"),
                ("planning_report", "planning_report.md", "text/markdown; charset=utf-8"),
            ),
            WorkerKind.GCODE_BUILD: (
                ("selected_path_plan", "selected_path_plan.json", "application/json"),
                ("gcode", "drawing.gcode", "text/x.gcode"),
                ("gcode_build_report", "gcode_build_report.md", "text/markdown; charset=utf-8"),
                ("gcode_preview_svg", "gcode_preview.svg", "image/svg+xml"),
                ("gcode_preview_report", "gcode_preview_report.json", "application/json"),
            ),
            WorkerKind.GCODE_CHECK: (
                ("gcode_static", "gcode_static.json", "application/json"),
                ("send_plan", "send_plan.json", "application/json"),
                ("readiness", "readiness.json", "application/json"),
            ),
        }
        selected_roles = roles[task.kind]
        if status is WorkerStatus.BLOCKED:
            if task.kind is WorkerKind.PATHS_PLAN:
                selected_roles = selected_roles[:1]
            elif task.kind not in {WorkerKind.GCODE_CHECK}:
                selected_roles = ()
        artifact_values: list[WorkerArtifact] = []
        if status in {WorkerStatus.SUCCEEDED, WorkerStatus.BLOCKED}:
            for role, path, media in selected_roles:
                content = self.artifact_overrides.get(
                    role,
                    _fake_artifact_content(task, role, route=getattr(self, "route", "A_DIRECT")),
                )
                output = Path(task.staging_dir) / path
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                artifact_values.append(
                    WorkerArtifact(role, path, hashlib.sha256(content).hexdigest(), len(content), media)
                )
        artifacts = tuple(artifact_values)
        error = (
            None
            if status is WorkerStatus.SUCCEEDED
            else ErrorPayload(
                code or "SEMANTIC_FAILURE", ErrorCategory.INTERNAL, "failed", False, {}, job_id=task.job_id
            )
        )
        return WorkerOutcomeV1(
            1, task.task_id, task.job_id, task.job_revision, task.attempt_id, status, artifacts, {}, error
        )

    def cancel(self, handle: WorkerHandle, *, grace_seconds: float = 2.0) -> WorkerOutcomeV1:
        del grace_seconds
        task = next(item for item in self.tasks if item.task_id == handle.task_id)
        return WorkerOutcomeV1(
            1,
            task.task_id,
            task.job_id,
            task.job_revision,
            task.attempt_id,
            WorkerStatus.CANCELLED,
            (),
            {},
            ErrorPayload("WORKER_CANCELLED", ErrorCategory.SERVICE, "cancelled", False, {}, job_id=task.job_id),
        )

    def close(self) -> None:
        self.closed = True


class RoutingWorkers(FakeWorkers):
    def __init__(self, store: FakeStore) -> None:
        super().__init__()
        self.store = store
        self.route = store.route

    def wait(self, handle: WorkerHandle, *, timeout_seconds: float, cancel_requested: object) -> WorkerOutcomeV1:
        task = next(item for item in self.tasks if item.task_id == handle.task_id)
        if task.kind is WorkerKind.IMAGE_PREPARE and task.payload["mode"] == "direct":
            from PIL import Image

            from drawingmachine.adapters.workers.tasks._payload_validation import validate_image_payload

            with Image.open(task.input_artifacts[0].absolute_path) as opened:
                opened.load()
                assessment = validate_image_payload(task).semantic_assessment
                assert assessment is not None
                result = decide_route(
                    opened,
                    assessment,
                    context=RouteContext(
                        job_name=task.job_id,
                        input_image=cast(str, task.payload["source_name"]),
                    ),
                )
            self.store.route = result.route
            self.route = result.route
        return super().wait(handle, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested)


def config(tmp_path: Path) -> ConfigBundle:
    machine = {"name": "stage1", "planning": dict(_PLANNING), "gcode": dict(_GCODE)}
    return ConfigBundle(
        ApplicationConfig(1, "stage1", "local-comfyui", LogLevel.INFO),
        ProfileEnvelope(1, machine),
        ProfileEnvelope(1, dict(_PROVIDER)),
        tmp_path / "machine.toml",
        tmp_path / "provider.toml",
        MappingProxyType({"application": "1" * 64, "machine": "2" * 64, "provider": "3" * 64}),
    )


def requester() -> RequesterIdentity:
    return RequesterIdentity("LOCAL_PEER", 1, 2, 3)


def submission(path: Path, *, route: RouteMode = RouteMode.DIRECT) -> WorkflowSubmissionV1:
    return WorkflowSubmissionV1(1, "job", path, route, None, "simplify this" if route is RouteMode.IMAGE_EDIT else None)


def review(job_name: str = "job", *, status: str = "PASS_TO_BUILD") -> ProcessedImageReview:
    passed = status == "PASS_TO_BUILD"
    return ProcessedImageReview.from_json(
        {
            "schema": "processed_image_review_v1",
            "job_name": job_name,
            "reviewed_at": "2026-07-11T00:00:00+00:00",
            "reviewer": "test",
            "processed_image": "processed_image.png",
            "handoff": None,
            "status": status,
            "checks": {
                name: passed
                for name in (
                    "recognizable_subject",
                    "background_simplified",
                    "black_on_white",
                    "no_edge_clipping",
                    "line_density_drawable",
                    "no_soft_gradients",
                    "limited_tiny_isolated_marks",
                    "vectorization_complexity_ok",
                )
            },
            "issues": [] if passed else ["still complex"],
            "revised_prompt": None,
            "next_allowed_stage": "build_drawable_job" if passed else "stop",
        }
    )


def build_chain(
    tmp_path: Path,
    scripted: list[tuple[WorkerStatus, str | None]] | None = None,
    *,
    artifact_overrides: dict[str, bytes] | None = None,
) -> tuple[OfflineJobChain, MemoryRepository, FakeStore, FakeWorkers]:
    clock = FakeClock()
    repository = MemoryRepository(clock)
    store = FakeStore(tmp_path / "store", repository.trace)
    workers = FakeWorkers(scripted, artifact_overrides=artifact_overrides)
    chain = OfflineJobChain(
        repository=repository, artifacts=store, workers=workers, config=config(tmp_path), clock=clock
    )
    return chain, repository, store, workers


@pytest.mark.parametrize(
    ("job_kind", "maximum"),
    [("image", 32 * 1024 * 1024), ("gcode", 16 * 1024 * 1024)],
)
def test_initial_sparse_input_over_admission_limit_leaves_no_staged_or_promoted_bytes(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    job_kind: str,
    maximum: int,
) -> None:
    source = tmp_path / ("oversized.png" if job_kind == "image" else "oversized.gcode")
    source.touch()
    os.truncate(source, maximum + 1)
    clock = FakeClock()
    repository = MemoryRepository(clock)
    chain = OfflineJobChain(
        repository=repository,
        artifacts=FilesystemArtifactStore(valid_xdg_paths),
        workers=FakeWorkers(),
        config=config(tmp_path),
        clock=clock,
    )
    if job_kind == "image":
        chain.submit(submission(source), request_id="submit", job_id="job-limit", requester=requester())
    else:
        chain.submit_gcode_check(source, request_id="submit", job_id="job-limit", requester=requester())

    with pytest.raises(DrawingMachineError, match=r"maximum|limit"):
        chain.advance("job-limit")

    staging_job = valid_xdg_paths.jobs_dir / ".staging" / "job-limit"
    artifacts = valid_xdg_paths.jobs_dir / "job-limit" / "artifacts"
    assert not staging_job.exists() or not any(staging_job.iterdir())
    assert not artifacts.exists() or not any(artifacts.iterdir())


def submit_to_review(chain: OfflineJobChain, source: Path, *, route: RouteMode = RouteMode.DIRECT) -> JobRecord:
    chain.submit(submission(source, route=route), request_id="submit-1", job_id="job-1", requester=requester())
    return chain.run_to_intervention_or_terminal("job-1")


def test_direct_job_reaches_ready_to_run_after_review_in_order(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, store, workers = build_chain(tmp_path)
    blocked = submit_to_review(chain, source)
    assert blocked.state is JobState.BLOCKED
    assert blocked.blocker is not None and blocked.blocker.code == "REVIEW_REQUIRED"
    processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
    chain.continue_review(
        ReviewContinuationV1(1, "job-1", review(), processed.sha256), request_id="review-1", requester=requester()
    )
    final = chain.run_to_intervention_or_terminal("job-1")
    assert [event.result_state for event in chain.events("job-1")] == [
        JobState.QUEUED,
        JobState.PREPARING_IMAGE,
        JobState.BLOCKED,
        JobState.IMAGE_READY,
        JobState.PLANNING_PATHS,
        JobState.PATH_PLAN_READY,
        JobState.BUILDING_GCODE,
        JobState.VALIDATING_GCODE,
        JobState.READY_TO_RUN,
    ]
    assert final.ready_snapshot is not None
    assert repository.trace.index("promotion") < repository.trace.index("repository-write", 2)
    assert store.projections[-1] is JobState.READY_TO_RUN
    assert [task.kind for task in workers.tasks] == [
        WorkerKind.IMAGE_PREPARE,
        WorkerKind.PATHS_PLAN,
        WorkerKind.GCODE_BUILD,
        WorkerKind.GCODE_CHECK,
    ]


def test_application_transition_rejects_illegal_edge_before_repository_event_or_audit_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, store, _workers = build_chain(tmp_path)
    queued_job = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    before_job = repository.jobs["job-1"]
    before_events = tuple(repository.events["job-1"])
    before_trace = tuple(repository.trace)
    before_projections = tuple(store.projections)

    with pytest.raises(DrawingMachineError) as captured:
        chain._transition(  # type: ignore[attr-defined]
            queued_job,
            JobState.PATH_PLAN_READY,
            "job.illegal_skip",
            "illegal",
            requester(),
        )

    assert captured.value.payload.code == "JOB_TRANSITION_NOT_ALLOWED"
    assert repository.jobs["job-1"] == before_job
    assert tuple(repository.events["job-1"]) == before_events
    assert tuple(repository.trace) == before_trace
    assert tuple(store.projections) == before_projections


def test_image_edit_runs_provider_then_normalization_and_binds_review(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, workers = build_chain(tmp_path)
    blocked = submit_to_review(chain, source, route=RouteMode.IMAGE_EDIT)
    assert blocked.state is JobState.BLOCKED
    assert [task.kind for task in workers.tasks] == [WorkerKind.PROVIDER_LOCAL_COMFYUI, WorkerKind.IMAGE_PREPARE]
    processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
    with pytest.raises(DrawingMachineError):
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(), "f" * 64), request_id="bad", requester=requester()
        )
    assert chain.get("job-1").state is JobState.BLOCKED
    continued = chain.continue_review(
        ReviewContinuationV1(1, "job-1", review(), processed.sha256), request_id="ok", requester=requester()
    )
    assert continued.state is JobState.IMAGE_READY
    assert next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image") == processed


def test_invalid_review_status_stays_blocked(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    submit_to_review(chain, source)
    processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
    with pytest.raises(DrawingMachineError):
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(status="REJECT_INPUT"), processed.sha256),
            request_id="bad",
            requester=requester(),
        )
    assert chain.get("job-1").state is JobState.BLOCKED


@pytest.mark.parametrize("code", ["PROVIDER_UNAVAILABLE", "WORKER_COMPLEXITY_BLOCKED", "WORKER_GCODE_BLOCKED"])
def test_provider_complexity_and_static_failures_are_blockers(tmp_path: Path, code: str) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    if code == "PROVIDER_UNAVAILABLE":
        script = [(WorkerStatus.BLOCKED, code)]
        route = RouteMode.IMAGE_EDIT
    elif code == "WORKER_COMPLEXITY_BLOCKED":
        script = [(WorkerStatus.SUCCEEDED, None), (WorkerStatus.BLOCKED, code)]
        route = RouteMode.DIRECT
    else:
        script = [
            (WorkerStatus.SUCCEEDED, None),
            (WorkerStatus.SUCCEEDED, None),
            (WorkerStatus.SUCCEEDED, None),
            (WorkerStatus.BLOCKED, code),
        ]
        route = RouteMode.DIRECT
    artifact_overrides = {"gcode": b"G21\nG90\nG54\nG2 X1 Y1\n"} if code == "WORKER_GCODE_BLOCKED" else None
    chain, repository, _store, _workers = build_chain(
        tmp_path,
        script,
        artifact_overrides=artifact_overrides,
    )
    blocked = submit_to_review(chain, source, route=route)
    if code != "PROVIDER_UNAVAILABLE":
        processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(), processed.sha256), request_id="review", requester=requester()
        )
        blocked = chain.run_to_intervention_or_terminal("job-1")
    assert blocked.state is JobState.BLOCKED
    assert blocked.blocker is not None and blocked.blocker.code == code


def test_worker_crash_retries_identical_task_once_with_new_attempt(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, workers = build_chain(
        tmp_path, [(WorkerStatus.FAILED, "WORKER_PROCESS_CRASHED"), (WorkerStatus.SUCCEEDED, None)]
    )
    assert submit_to_review(chain, source).state is JobState.BLOCKED
    first, second = workers.tasks
    assert first.attempt_id != second.attempt_id
    first_json = first.to_json()
    second_json = second.to_json()
    for key in ("attempt_id", "task_id", "staging_dir"):
        first_json.pop(key)
        second_json.pop(key)
    assert first_json == second_json


@pytest.mark.parametrize("code", ["WORKER_DID_NOT_EXIT", "WORKER_OUTCOME_TOO_LARGE", "WORKER_TIMEOUT"])
@pytest.mark.parametrize("route", [RouteMode.DIRECT, RouteMode.IMAGE_EDIT])
def test_non_crash_worker_failures_never_retry_by_code_or_provider_kind(
    tmp_path: Path,
    code: str,
    route: RouteMode,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, workers = build_chain(tmp_path, [(WorkerStatus.FAILED, code)])

    final = submit_to_review(chain, source, route=route)

    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == code
    assert len(workers.tasks) == 1


def test_provider_process_crash_is_not_retried_because_submission_is_not_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, workers = build_chain(
        tmp_path,
        [(WorkerStatus.FAILED, "WORKER_PROCESS_CRASHED")],
    )

    final = submit_to_review(chain, source, route=RouteMode.IMAGE_EDIT)

    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "WORKER_PROCESS_CRASHED"
    assert [task.kind for task in workers.tasks] == [WorkerKind.PROVIDER_LOCAL_COMFYUI]


def test_semantic_worker_failure_is_not_retried(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, workers = build_chain(tmp_path, [(WorkerStatus.FAILED, "WORKER_INPUT_INVALID")])
    final = submit_to_review(chain, source)
    assert final.state is JobState.FAILED
    assert len(workers.tasks) == 1


@pytest.mark.parametrize(
    ("role", "hostile_document"),
    [
        ("gcode_static", {"summary": {}, "checks": []}),
        (
            "send_plan",
            {
                **_fixture_document("send_plan.json"),
                "raw_line_count": 999999,
            },
        ),
        (
            "readiness",
            {
                "allow_stream": False,
                "blockers": [],
                "warnings": [],
                "pen_motion_assessment": {
                    "pen_lift_count": 0,
                    "pen_down_count": 0,
                    "draw_path_count": 0,
                    "lift_down_delta": 0,
                    "hard_gate": False,
                },
            },
        ),
    ],
)
def test_hostile_gcode_safety_json_never_promotes_or_reaches_completed(
    tmp_path: Path,
    role: str,
    hostile_document: JsonObject,
) -> None:
    source = tmp_path / "candidate.gcode"
    source.write_bytes(Path("tests/fixtures/package_b/golden/expected/drawing.gcode").read_bytes())
    clock = FakeClock()
    repository = MemoryRepository(clock)
    store = FakeStore(tmp_path / "store", repository.trace)
    workers = FakeWorkers(artifact_overrides={role: _json_bytes(hostile_document)})
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=workers,
        config=config(tmp_path),
        clock=clock,
    )
    chain.submit_gcode_check(source, request_id="check", job_id="check-job", requester=requester())

    final = chain.run_to_intervention_or_terminal("check-job")

    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "WORKER_ARTIFACT_CONTENT_INVALID"
    assert [bundle.attempt_id.startswith("input-") for bundle in store.promoted] == [True]


def test_audit_history_is_complete_non_secret_and_immutable_after_later_transitions(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    clock = FakeClock()
    database = tmp_path / "jobs.db"
    repository = SQLiteRepository(database, clock=clock)
    repository.initialize()
    store = FakeStore(tmp_path / "store", [], enforce_order=False)
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=FakeWorkers(),
        config=config(tmp_path),
        clock=clock,
    )
    try:
        chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
        queued_payload_before = (
            sqlite3.connect(database).execute("SELECT payload_json FROM audit_events ORDER BY rowid LIMIT 1").fetchone()
        )
        blocked = chain.run_to_intervention_or_terminal("job-1")
        assert blocked.blocker is not None
        processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(), processed.sha256),
            request_id="review",
            requester=requester(),
        )
        chain.cancel("job-1", request_id="cancel", requester=requester())

        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT event_type, request_id, payload_json FROM audit_events ORDER BY rowid"
            ).fetchall()
        assert queued_payload_before is not None
        assert json.loads(rows[0][2]) == json.loads(queued_payload_before[0])
        expected_fields = {
            "schema_version",
            "requester",
            "job_id",
            "command_summary",
            "prior_state",
            "result_state",
            "approval",
            "error_code",
            "blocker_code",
            "service_version",
            "protocol_version",
        }
        payloads = {event_type: json.loads(payload) for event_type, _request_id, payload in rows}
        assert all(set(json.loads(payload)) == expected_fields for _event, _request, payload in rows)
        assert payloads["job.queued"]["command_summary"] == {
            "name": "workflow.run",
            "event_type": "job.queued",
            "job_kind": "OFFLINE_WORKFLOW",
        }
        assert payloads["job.queued"]["prior_state"] is None
        assert payloads["job.review_required"]["blocker_code"] == "REVIEW_REQUIRED"
        assert payloads["job.review_accepted"]["approval"] == {
            "identity": requester().to_json(),
            "approved_at": "2026-07-11T00:00:00+00:00",
        }
        assert payloads["job.cancelled"]["result_state"] == "CANCELLED"
        serialized = "".join(payload for _event, _request, payload in rows)
        assert str(source) not in serialized
        assert "simplify this" not in serialized
    finally:
        chain.close()


@pytest.mark.parametrize(
    ("mode", "expected_code", "expected_event"),
    [
        ("failure", "WORKER_INPUT_INVALID", "job.worker_failed"),
        ("restart", "SERVICE_RESTARTED", "job.service_restarted"),
    ],
)
def test_audit_failure_and_restart_rows_capture_error_code(
    tmp_path: Path,
    mode: str,
    expected_code: str,
    expected_event: str,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    database = tmp_path / "jobs.db"
    repository = SQLiteRepository(database, clock=FakeClock())
    repository.initialize()
    store = FakeStore(tmp_path / "store", [], enforce_order=False)
    workers = FakeWorkers([(WorkerStatus.FAILED, "WORKER_INPUT_INVALID")])
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=workers,
        config=config(tmp_path),
        clock=FakeClock(),
    )
    try:
        chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
        if mode == "failure":
            chain.run_to_intervention_or_terminal("job-1")
        else:
            chain.reconcile_inflight_jobs()
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT payload_json FROM audit_events WHERE event_type = ?",
                (expected_event,),
            ).fetchone()
        assert row is not None
        assert json.loads(row[0])["error_code"] == expected_code
    finally:
        chain.close()


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.PREPARING_IMAGE, JobState.BLOCKED, JobState.READY_TO_RUN])
def test_cancel_queued_running_blocked_and_ready_jobs(tmp_path: Path, state: JobState) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    job = repository.jobs["job-1"]
    blocker = None
    ready = None
    if state is JobState.BLOCKED:
        from drawingmachine.domain.jobs import JobBlocker

        blocker = JobBlocker("REVIEW_REQUIRED", ErrorCategory.VALIDATION, "review", {})
    if state is JobState.READY_TO_RUN:
        state = JobState.QUEUED  # reach READY through the normal chain below
        chain.run_to_intervention_or_terminal("job-1")
        processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(), processed.sha256), request_id="review", requester=requester()
        )
        chain.run_to_intervention_or_terminal("job-1")
    elif state is not JobState.QUEUED:
        repository.jobs["job-1"] = replace(job, state=state, revision=1, blocker=blocker, ready_snapshot=ready)
    cancelled = chain.cancel("job-1", request_id="cancel", requester=requester())
    assert cancelled.state is JobState.CANCELLED


def test_stale_worker_outcome_fails_closed_without_promotion(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, workers = build_chain(tmp_path)
    original_wait = workers.wait

    def stale_wait(handle: WorkerHandle, *, timeout_seconds: float, cancel_requested: object) -> WorkerOutcomeV1:
        outcome = original_wait(handle, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested)
        return replace(outcome, job_revision=outcome.job_revision + 1)

    workers.wait = stale_wait  # type: ignore[method-assign]
    final = submit_to_review(chain, source)
    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "STALE_WORKER_OUTCOME"
    assert repository.trace.count("promotion") == 1  # input import only


def test_gcode_check_uses_separate_queued_validating_completed_progression(tmp_path: Path) -> None:
    source = tmp_path / "candidate.gcode"
    source.write_bytes(Path("tests/fixtures/package_b/golden/expected/drawing.gcode").read_bytes())
    chain, _repository, _store, workers = build_chain(tmp_path)
    queued_job = chain.submit_gcode_check(
        source,
        request_id="check-1",
        job_id="check-job",
        requester=requester(),
    )
    assert queued_job.state is JobState.QUEUED
    final = chain.run_to_intervention_or_terminal("check-job")
    assert final.state is JobState.COMPLETED
    assert [event.result_state for event in chain.events("check-job")] == [
        JobState.QUEUED,
        JobState.VALIDATING_GCODE,
        JobState.COMPLETED,
    ]
    assert [task.kind for task in workers.tasks] == [WorkerKind.GCODE_CHECK]


def test_succeeded_worker_status_cannot_override_exact_blocked_readiness(tmp_path: Path) -> None:
    source = tmp_path / "candidate.gcode"
    source.write_text("G21\n", encoding="utf-8")
    chain, _repository, store, _workers = build_chain(tmp_path)
    chain.submit_gcode_check(source, request_id="check", job_id="check-job", requester=requester())

    final = chain.run_to_intervention_or_terminal("check-job")

    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "WORKER_ARTIFACT_CONTENT_INVALID"
    assert [bundle.attempt_id.startswith("input-") for bundle in store.promoted] == [True]


def test_worker_cancellation_uses_socket_request_identity_in_final_event(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, _workers = build_chain(tmp_path, [(WorkerStatus.CANCELLED, "WORKER_CANCELLED")])
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    chain.advance("job-1")
    token = __import__("threading").Event()
    chain.register_cancellation_request("job-1", token, "socket-cancel", requester())
    final = chain.advance("job-1")
    assert final.state is JobState.CANCELLED
    event = chain.events("job-1")[-1]
    assert event.request_id == "socket-cancel"
    assert event.payload["requester"] == requester().to_json()


def test_restart_reconciliation_fails_inflight_without_worker_submission(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, workers = build_chain(tmp_path)
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    queued_job = repository.jobs["job-1"]
    repository.jobs["job-1"] = replace(queued_job, state=JobState.PLANNING_PATHS, revision=3)
    reconciled = chain.reconcile_inflight_jobs()
    assert len(reconciled) == 1
    assert reconciled[0].state is JobState.FAILED
    assert reconciled[0].error is not None and reconciled[0].error.code == "SERVICE_RESTARTED"
    assert workers.tasks == []


def test_restart_reconciliation_also_fails_durable_queued_without_worker(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, workers = build_chain(tmp_path)
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    reconciled = chain.reconcile_inflight_jobs()
    assert len(reconciled) == 1
    assert reconciled[0].state is JobState.FAILED
    assert reconciled[0].error is not None and reconciled[0].error.code == "SERVICE_RESTARTED"
    assert workers.tasks == []


def test_ready_job_cancellation_preserves_immutable_snapshot_in_sqlite(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    clock = FakeClock()
    repository = SQLiteRepository(tmp_path / "jobs.db", clock=clock)
    repository.initialize()
    store = FakeStore(tmp_path / "store", [], enforce_order=False)
    workers = FakeWorkers()
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=workers,
        config=config(tmp_path),
        clock=clock,
    )
    try:
        submit_to_review(chain, source)
        processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
        chain.continue_review(
            ReviewContinuationV1(1, "job-1", review(), processed.sha256),
            request_id="review",
            requester=requester(),
        )
        ready_job = chain.run_to_intervention_or_terminal("job-1")
        assert ready_job.ready_snapshot is not None
        cancelled = chain.cancel("job-1", request_id="cancel", requester=requester())
        assert cancelled.state is JobState.CANCELLED
        assert cancelled.ready_snapshot == ready_job.ready_snapshot
        assert repository.get_job("job-1") == cancelled
    finally:
        chain.close()


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.BLOCKED, JobState.READY_TO_RUN])
def test_offline_state_rules_allow_requested_cancellation_states(state: JobState) -> None:
    blocker_code = "REVIEW_REQUIRED" if state is JobState.BLOCKED else None
    assert allowed_transition(state, JobState.CANCELLED, blocker_code)


def test_auto_route_uses_promoted_route_decision_and_runs_provider_for_image_edit(tmp_path: Path) -> None:
    from PIL import Image

    source = tmp_path / "input.png"
    uncertain = Image.new("L", (100, 100), 255)
    uncertain.paste(0, (0, 0, 50, 100))
    uncertain.save(source)
    assessment = {
        "semantic_style_score": 0.3,
        "semantic_score_source": "router_multimodal_model",
        "semantic_rationale": [],
        "semantic_blockers": [],
    }
    clock = FakeClock()
    repository = MemoryRepository(clock)
    store = FakeStore(tmp_path / "store", repository.trace)
    workers = RoutingWorkers(store)
    chain = OfflineJobChain(
        repository=repository, artifacts=store, workers=workers, config=config(tmp_path), clock=clock
    )
    selected = WorkflowSubmissionV1(1, "job", source, RouteMode.AUTO, assessment, "simplify")
    chain.submit(selected, request_id="submit", job_id="job-1", requester=requester())

    blocked = chain.run_to_intervention_or_terminal("job-1")

    assert blocked.state is JobState.BLOCKED
    assert blocked.blocker is not None and blocked.blocker.code == "REVIEW_REQUIRED"
    assert [task.kind for task in workers.tasks] == [
        WorkerKind.IMAGE_PREPARE,
        WorkerKind.PROVIDER_LOCAL_COMFYUI,
        WorkerKind.IMAGE_PREPARE,
    ]
    roles = {artifact.role for artifact in repository.list_artifacts("job-1")}
    assert "route_decision" in roles
    assert "provider_request" in roles
    assert "processed_image" in roles
    assert "direct_report" not in roles
    persisted_paths = {artifact.relative_path for artifact in repository.list_artifacts("job-1")}
    assert {artifact.relative_path for bundle in store.promoted for artifact in bundle.artifacts} == persisted_paths


def test_auto_direct_route_runs_route_only_then_explicit_direct_without_orphan_bundle(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    clock = FakeClock()
    repository = MemoryRepository(clock)
    store = FakeStore(tmp_path / "store", repository.trace, route="A_DIRECT")
    workers = FakeWorkers()
    chain = OfflineJobChain(
        repository=repository, artifacts=store, workers=workers, config=config(tmp_path), clock=clock
    )
    chain.submit(
        WorkflowSubmissionV1(1, "job", source, RouteMode.AUTO, None, None),
        request_id="submit",
        job_id="job-1",
        requester=requester(),
    )

    blocked = chain.run_to_intervention_or_terminal("job-1")

    assert blocked.state is JobState.BLOCKED
    assert [(task.payload["mode"], task.payload.get("route_mode")) for task in workers.tasks] == [
        ("direct", "auto"),
        ("direct", "direct"),
    ]
    assert len(workers.tasks[0].to_json()["input_artifacts"]) == 1
    persisted_paths = {artifact.relative_path for artifact in repository.list_artifacts("job-1")}
    assert {artifact.relative_path for bundle in store.promoted for artifact in bundle.artifacts} == persisted_paths


def test_auto_route_invalid_promoted_literal_fails_closed_before_provider(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    clock = FakeClock()
    repository = MemoryRepository(clock)
    store = FakeStore(tmp_path / "store", repository.trace, route="NOT_A_ROUTE")
    workers = FakeWorkers()
    chain = OfflineJobChain(
        repository=repository, artifacts=store, workers=workers, config=config(tmp_path), clock=clock
    )
    chain.submit(
        WorkflowSubmissionV1(1, "job", source, RouteMode.AUTO, None, None),
        request_id="submit",
        job_id="job-1",
        requester=requester(),
    )
    final = chain.run_to_intervention_or_terminal("job-1")
    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "ROUTE_DECISION_INVALID"
    assert [task.kind for task in workers.tasks] == [WorkerKind.IMAGE_PREPARE]


@pytest.mark.parametrize("variant", ["missing", "empty", "extra"])
def test_success_worker_artifact_roles_must_match_exact_contract(tmp_path: Path, variant: str) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, store, workers = build_chain(tmp_path)
    original_wait = workers.wait

    def malformed(handle: WorkerHandle, *, timeout_seconds: float, cancel_requested: object) -> WorkerOutcomeV1:
        outcome = original_wait(handle, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested)
        artifacts = list(outcome.artifacts)
        if variant == "missing":
            artifacts.pop()
        elif variant == "empty":
            artifacts.clear()
        else:
            artifacts.append(WorkerArtifact("unexpected", "unexpected.json", _SHA, 2, "application/json"))
        return replace(outcome, artifacts=tuple(artifacts))

    workers.wait = malformed  # type: ignore[method-assign]
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    chain.advance("job-1")
    final = chain.advance("job-1")
    assert final.state is JobState.FAILED
    assert final.error is not None and final.error.code == "WORKER_ARTIFACT_SET_INVALID"
    assert len(store.discarded) == 1


@pytest.mark.parametrize(
    ("script", "stale"),
    [
        ([(WorkerStatus.FAILED, "WORKER_INPUT_INVALID")], False),
        ([(WorkerStatus.CANCELLED, "WORKER_CANCELLED")], False),
        ([(WorkerStatus.FAILED, "WORKER_PROCESS_CRASHED"), (WorkerStatus.SUCCEEDED, None)], False),
        ([(WorkerStatus.FAILED, "WORKER_TIMEOUT"), (WorkerStatus.FAILED, "WORKER_TIMEOUT")], False),
        (None, True),
    ],
)
def test_every_worker_attempt_discards_staging(
    tmp_path: Path,
    script: list[tuple[WorkerStatus, str | None]] | None,
    stale: bool,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, store, workers = build_chain(tmp_path, script)
    if stale:
        original_wait = workers.wait

        def stale_wait(handle: WorkerHandle, *, timeout_seconds: float, cancel_requested: object) -> WorkerOutcomeV1:
            return replace(
                original_wait(handle, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested),
                job_revision=999,
            )

        workers.wait = stale_wait  # type: ignore[method-assign]
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    chain.advance("job-1")
    chain.advance("job-1")
    assert len(store.discarded) == len(workers.tasks)
    assert not list((store.root / ".staging").glob("**/attempt-*"))


def test_promotion_exception_still_discards_attempt_staging(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, store, _workers = build_chain(tmp_path)

    def fail_promotion(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise OSError("promotion failed")

    store.validate_and_promote = fail_promotion  # type: ignore[method-assign]
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    chain.advance("job-1")
    with pytest.raises(OSError, match="promotion failed"):
        chain.advance("job-1")
    assert len(store.discarded) == 1
    assert not list((store.root / ".staging").glob("**/attempt-*"))


def test_submit_rejects_duplicate_and_precommit_withdrawal_leaves_no_job(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    with pytest.raises(DrawingMachineError) as caught:
        chain.submit(submission(source), request_id="duplicate", job_id="job-1", requester=requester())
    assert caught.value.payload.code == "JOB_ALREADY_EXISTS"
    assert repository.jobs["job-1"].revision == 0

    withdrawn = chain._coordinator_submit(  # type: ignore[attr-defined]
        submission(source),
        request_id="withdrawn",
        job_id="job-2",
        requester=requester(),
        commit_allowed=lambda: False,
        committed=lambda job: pytest.fail(f"withdrawn job committed: {job.job_id}"),
    )
    assert withdrawn is None
    assert "job-2" not in repository.jobs


def test_review_rejection_and_withdrawal_preserve_blocked_authority(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    blocked = submit_to_review(chain, source)
    processed = next(item for item in repository.list_artifacts("job-1") if item.role == "processed_image")
    rejected = ReviewContinuationV1(1, "job-1", review(status="REJECT_INPUT"), processed.sha256)
    with pytest.raises(DrawingMachineError) as caught:
        chain.continue_review(rejected, request_id="reject", requester=requester())
    assert caught.value.payload.code == "REVIEW_NOT_APPROVED"

    approved = ReviewContinuationV1(1, "job-1", review(), processed.sha256)
    withdrawn = chain._coordinator_continue_review(  # type: ignore[attr-defined]
        approved,
        request_id="withdrawn",
        requester=requester(),
        commit_allowed=lambda: False,
        committed=lambda job: pytest.fail(f"withdrawn review committed: {job.job_id}"),
    )
    assert withdrawn is None
    assert repository.jobs["job-1"] == blocked


def test_gcode_submission_rejects_relative_duplicate_and_withdrawn_authority(tmp_path: Path) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_text("G21\n", encoding="ascii")
    chain, repository, _store, _workers = build_chain(tmp_path)
    with pytest.raises(DrawingMachineError) as relative:
        chain.submit_gcode_check(Path("relative.gcode"), request_id="relative", job_id="gcode", requester=requester())
    assert relative.value.payload.code == "GCODE_CHECK_INPUT_INVALID"

    chain.submit_gcode_check(source, request_id="submit", job_id="gcode", requester=requester())
    with pytest.raises(DrawingMachineError) as duplicate:
        chain.submit_gcode_check(source, request_id="duplicate", job_id="gcode", requester=requester())
    assert duplicate.value.payload.code == "JOB_ALREADY_EXISTS"

    withdrawn = chain._coordinator_submit_gcode_check(  # type: ignore[attr-defined]
        source,
        request_id="withdrawn",
        job_id="gcode-2",
        requester=requester(),
        commit_allowed=lambda: False,
        committed=lambda job: pytest.fail(f"withdrawn G-code committed: {job.job_id}"),
    )
    assert withdrawn is None
    assert "gcode-2" not in repository.jobs


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.CANCELLED])
def test_terminal_job_cancel_and_unexpected_failure_are_idempotent(tmp_path: Path, state: JobState) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    original = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    error = (
        ErrorPayload("FAILED", ErrorCategory.INTERNAL, "failed", False, {}, job_id=original.job_id)
        if state is JobState.FAILED
        else None
    )
    terminal = replace(original, state=state, error=error)
    repository.jobs[terminal.job_id] = terminal
    assert chain.cancel(terminal.job_id, request_id="cancel", requester=requester()) == terminal
    assert chain.fail_unexpected(terminal.job_id, request_id="failure", error=OSError("ignored")) == terminal


def test_non_review_blocked_initialize_skips_artifact_rehydration(tmp_path: Path) -> None:
    chain, repository, store, _workers = build_chain(tmp_path)
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    job = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    repository.jobs[job.job_id] = replace(
        job,
        state=JobState.BLOCKED,
        blocker=offline_module.JobBlocker("COMPLEXITY_LIMIT", ErrorCategory.VALIDATION, "blocked", {}),
    )
    store.resolve = lambda *args, **kwargs: pytest.fail("non-review block must not rehydrate")  # type: ignore[method-assign]
    assert chain.initialize() == 2


def test_pending_artifact_is_visible_but_missing_role_fails_closed(tmp_path: Path) -> None:
    chain, _repository, _store, _workers = build_chain(tmp_path)
    pending = ArtifactRef("pending", "artifacts/attempt/pending.json", "1" * 64, 1, "application/json")
    chain._artifact_metadata["job-1"] = {"pending": pending}  # type: ignore[attr-defined]
    assert chain._artifact("job-1", "pending") == pending  # type: ignore[attr-defined]
    with pytest.raises(DrawingMachineError) as caught:
        chain._artifact("job-1", "missing")  # type: ignore[attr-defined]
    assert caught.value.payload.code == "JOB_ARTIFACT_MISSING"


def test_json_and_route_authority_fail_closed_on_nonobject_or_wrong_binding(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    artifact = ArtifactRef("route_decision", "artifacts/attempt/route.json", "1" * 64, 2, "application/json")
    bundle = PromotedBundle("job-1", "attempt", root, (artifact,))
    (root / "route.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        OfflineJobChain._read_json(bundle, "route_decision")

    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, store, _workers = build_chain(tmp_path)
    job = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    invalid_root = store.root / "invalid"
    invalid_root.mkdir(parents=True)
    document = _fixture_document("route.json")
    document.update(
        {
            "job_name": "wrong-job",
            "input_image": source.name,
            "route": "A_DIRECT",
            "direct_route_allowed": True,
            "next_stage": "prepare_direct_processed_image",
        }
    )
    route_bytes = _json_bytes(document)
    (invalid_root / "route.json").write_bytes(route_bytes)
    route_ref = replace(artifact, sha256=hashlib.sha256(route_bytes).hexdigest(), size_bytes=len(route_bytes))
    invalid = PromotedBundle(job.job_id, "attempt", invalid_root, (route_ref,))
    with pytest.raises(DrawingMachineError) as caught:
        chain._validated_route(invalid, repository.jobs[job.job_id])  # type: ignore[attr-defined]
    assert caught.value.payload.code == "ROUTE_DECISION_INVALID"

    del document["schema"]
    (invalid_root / "route.json").write_bytes(_json_bytes(document))
    with pytest.raises(DrawingMachineError) as fields:
        chain._validated_route(invalid, repository.jobs[job.job_id])  # type: ignore[attr-defined]
    assert fields.value.payload.code == "ROUTE_DECISION_INVALID"


@pytest.mark.parametrize(("kind", "role"), [("workflow", "gcode"), ("gcode", "image")])
def test_submission_rejects_claimed_staging_authority_for_wrong_role(
    tmp_path: Path,
    kind: str,
    role: str,
) -> None:
    source = tmp_path / ("input.png" if kind == "workflow" else "drawing.gcode")
    source.write_bytes(b"data")
    chain, repository, _store, _workers = build_chain(tmp_path)
    admission = SimpleNamespace(
        state=offline_module.StagingAdmissionState.CLAIMED,
        role=SimpleNamespace(value=role),
    )
    repository.get_staging_admission = lambda request_id: admission  # type: ignore[attr-defined]
    with pytest.raises(DrawingMachineError) as caught:
        if kind == "workflow":
            chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
        else:
            chain.submit_gcode_check(source, request_id="submit", job_id="job-1", requester=requester())
    assert caught.value.payload.code == "STAGING_ADMISSION_INVALID"
    assert repository.jobs == {}


@pytest.mark.parametrize("with_bundle", [False, True])
def test_second_commit_check_withdraws_staged_work_and_discards_only_promoted_bundle(
    tmp_path: Path,
    with_bundle: bool,
) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, repository, store, _workers = build_chain(tmp_path)
    admission = SimpleNamespace(
        state=offline_module.StagingAdmissionState.CLAIMED,
        role=SimpleNamespace(value="image"),
        revision=0,
    )
    repository.get_staging_admission = lambda request_id: admission  # type: ignore[attr-defined]
    artifact = ArtifactRef("input_image", "artifacts/input/inputs/input.png", "1" * 64, 3, "image/png")
    bundle = PromotedBundle("job-1", "input", store.root, (artifact,)) if with_bundle else None
    chain._import_or_recover_staged = lambda *args, **kwargs: ((artifact,), bundle)  # type: ignore[method-assign]
    discarded: list[PromotedBundle] = []
    chain._discard_uncommitted_bundle = discarded.append  # type: ignore[method-assign]
    decisions = iter((True, False))
    result = chain._coordinator_submit(  # type: ignore[attr-defined]
        submission(source),
        request_id="submit",
        job_id="job-1",
        requester=requester(),
        commit_allowed=lambda: next(decisions),
        committed=lambda job: pytest.fail(f"withdrawn job committed: {job.job_id}"),
    )
    assert result is None
    assert discarded == ([] if bundle is None else [bundle])
    assert repository.jobs == {}


def test_transaction_commit_withdrawal_rolls_back_job_and_gcode(tmp_path: Path) -> None:
    for kind in ("workflow", "gcode"):
        database = tmp_path / f"{kind}.db"
        repository = SQLiteRepository(database, clock=FakeClock())
        repository.initialize()
        store = FakeStore(tmp_path / f"{kind}-store", [], enforce_order=False)
        chain = OfflineJobChain(
            repository=repository,
            artifacts=store,
            workers=FakeWorkers(),
            config=config(tmp_path),
            clock=FakeClock(),
        )
        source = tmp_path / ("input.png" if kind == "workflow" else "drawing.gcode")
        source.write_bytes(b"data")
        decisions = iter((True, True, False))
        if kind == "workflow":
            result = chain._coordinator_submit(  # type: ignore[attr-defined]
                submission(source),
                request_id="submit",
                job_id="job-1",
                requester=requester(),
                commit_allowed=lambda decisions=decisions: next(decisions),
                committed=lambda job: pytest.fail(f"withdrawn job committed: {job.job_id}"),
            )
        else:
            result = chain._coordinator_submit_gcode_check(  # type: ignore[attr-defined]
                source,
                request_id="submit",
                job_id="job-1",
                requester=requester(),
                commit_allowed=lambda decisions=decisions: next(decisions),
                committed=lambda job: pytest.fail(f"withdrawn job committed: {job.job_id}"),
            )
        assert result is None
        assert repository.get_job("job-1") is None
        repository.close()


def test_recovery_rejects_claim_with_durable_job_and_handles_missing_or_corrupt_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain, repository, store, _workers = build_chain(tmp_path)
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    job = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    admission = SimpleNamespace(
        state=offline_module.StagingAdmissionState.CLAIMED,
        role=SimpleNamespace(value="image"),
        request_id="submit",
        source_sha256="1" * 64,
        payload_identity=SimpleNamespace(size_bytes=3),
    )
    repository.list_staging_admissions = lambda: (admission,)  # type: ignore[attr-defined]
    monkeypatch.setattr(offline_module, "staging_job_id", lambda request_id, role: job.job_id)
    with pytest.raises(DrawingMachineError) as caught:
        chain._recover_uncommitted_staging_promotions()  # type: ignore[attr-defined]
    assert caught.value.payload.code == "STAGING_ADMISSION_INVALID"

    repository.jobs.clear()
    store.read_bytes = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        DrawingMachineError(ErrorPayload("ARTIFACT_PATH_INVALID", ErrorCategory.INPUT, "missing", False, {}))
    )
    chain._recover_uncommitted_staging_promotions()  # type: ignore[attr-defined]

    store.read_bytes = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        DrawingMachineError(ErrorPayload("ARTIFACT_DIGEST_MISMATCH", ErrorCategory.VALIDATION, "corrupt", False, {}))
    )
    with pytest.raises(DrawingMachineError) as corrupt:
        chain._recover_uncommitted_staging_promotions()  # type: ignore[attr-defined]
    assert corrupt.value.payload.code == "ARTIFACT_DIGEST_MISMATCH"


def test_staged_reuse_propagates_nonabsence_integrity_failure(tmp_path: Path) -> None:
    chain, _repository, store, _workers = build_chain(tmp_path)
    store.read_bytes = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        DrawingMachineError(ErrorPayload("ARTIFACT_DIGEST_MISMATCH", ErrorCategory.VALIDATION, "corrupt", False, {}))
    )
    admission = SimpleNamespace(source_sha256="1" * 64, payload_identity=SimpleNamespace(size_bytes=3))
    with pytest.raises(DrawingMachineError) as caught:
        chain._import_or_recover_staged(  # type: ignore[attr-defined]
            "job-1",
            "request",
            tmp_path / "input.png",
            admission,
            role="input_image",
            relative_path="inputs/input.png",
            media_type="image/png",
            max_bytes=10,
        )
    assert caught.value.payload.code == "ARTIFACT_DIGEST_MISMATCH"


def test_review_requires_the_exact_review_blocked_state(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    chain, _repository, _store, _workers = build_chain(tmp_path)
    chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    continuation = ReviewContinuationV1(1, "job-1", review(), "0" * 64)
    with pytest.raises(DrawingMachineError) as caught:
        chain.continue_review(continuation, request_id="review", requester=requester())
    assert caught.value.payload.code == "REVIEW_NOT_ALLOWED"


@pytest.mark.parametrize("with_bundle", [False, True])
def test_gcode_second_commit_check_discards_only_promoted_staging(
    tmp_path: Path,
    with_bundle: bool,
) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_text("G21\n", encoding="ascii")
    chain, repository, store, _workers = build_chain(tmp_path)
    admission = SimpleNamespace(
        state=offline_module.StagingAdmissionState.CLAIMED,
        role=SimpleNamespace(value="gcode"),
        revision=0,
    )
    repository.get_staging_admission = lambda request_id: admission  # type: ignore[attr-defined]
    artifact = ArtifactRef("gcode", "artifacts/input/inputs/drawing.gcode", "1" * 64, 4, "text/x.gcode")
    bundle = PromotedBundle("job-1", "input", store.root, (artifact,)) if with_bundle else None
    chain._import_or_recover_staged = lambda *args, **kwargs: ((artifact,), bundle)  # type: ignore[method-assign]
    discarded: list[PromotedBundle] = []
    chain._discard_uncommitted_bundle = discarded.append  # type: ignore[method-assign]
    decisions = iter((True, False))
    result = chain._coordinator_submit_gcode_check(  # type: ignore[attr-defined]
        source,
        request_id="submit",
        job_id="job-1",
        requester=requester(),
        commit_allowed=lambda: next(decisions),
        committed=lambda job: pytest.fail(f"withdrawn G-code committed: {job.job_id}"),
    )
    assert result is None
    assert discarded == ([] if bundle is None else [bundle])
    assert repository.jobs == {}


def test_gcode_staging_is_discarded_on_commit_abandonment_or_repository_failure(tmp_path: Path) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_text("G21\n", encoding="ascii")
    for failure in (offline_module._CoordinatorCommitAbandoned(), OSError("repository failed")):
        chain, repository, store, _workers = build_chain(tmp_path)
        admission = SimpleNamespace(
            state=offline_module.StagingAdmissionState.CLAIMED,
            role=SimpleNamespace(value="gcode"),
            revision=0,
        )
        repository.get_staging_admission = lambda request_id, admission=admission: admission  # type: ignore[attr-defined]
        artifact = ArtifactRef("gcode", "artifacts/input/inputs/drawing.gcode", "1" * 64, 4, "text/x.gcode")
        bundle = PromotedBundle("job-1", "input", store.root, (artifact,))
        chain._import_or_recover_staged = lambda *args, artifact=artifact, bundle=bundle, **kwargs: (  # type: ignore[method-assign]
            (artifact,),
            bundle,
        )
        discarded: list[PromotedBundle] = []
        chain._discard_uncommitted_bundle = discarded.append  # type: ignore[method-assign]

        @contextmanager
        def failing_transaction(
            failure: BaseException = failure,
            repository: MemoryRepository = repository,
        ) -> Iterator[MemoryTransaction]:
            raise failure
            yield MemoryTransaction(repository)

        repository.transaction = failing_transaction  # type: ignore[method-assign]
        if isinstance(failure, offline_module._CoordinatorCommitAbandoned):
            assert (
                chain._coordinator_submit_gcode_check(  # type: ignore[attr-defined]
                    source,
                    request_id="submit",
                    job_id="job-1",
                    requester=requester(),
                    commit_allowed=lambda: True,
                    committed=lambda job: pytest.fail(f"abandoned G-code committed: {job.job_id}"),
                )
                is None
            )
        else:
            with pytest.raises(OSError, match="repository failed"):
                chain._coordinator_submit_gcode_check(  # type: ignore[attr-defined]
                    source,
                    request_id="submit",
                    job_id="job-1",
                    requester=requester(),
                    commit_allowed=lambda: True,
                    committed=lambda job: pytest.fail(f"failed G-code committed: {job.job_id}"),
                )
        assert discarded == [bundle]


def test_advance_is_idempotent_for_unhandled_terminal_workflow_and_gcode_states(tmp_path: Path) -> None:
    workflow_source = tmp_path / "input.png"
    workflow_source.write_bytes(b"png")
    chain, repository, _store, _workers = build_chain(tmp_path)
    workflow = chain.submit(
        submission(workflow_source), request_id="workflow", job_id="workflow", requester=requester()
    )
    workflow = replace(workflow, state=JobState.CANCELLED)
    repository.jobs[workflow.job_id] = workflow
    assert chain.advance(workflow.job_id) == workflow

    gcode_source = tmp_path / "drawing.gcode"
    gcode_source.write_text("G21\n", encoding="ascii")
    gcode = chain.submit_gcode_check(gcode_source, request_id="gcode", job_id="gcode", requester=requester())
    gcode = replace(gcode, state=JobState.CANCELLED)
    repository.jobs[gcode.job_id] = gcode
    assert chain.advance(gcode.job_id) == gcode


def test_auto_direct_and_image_edit_normalization_failures_stop_before_review(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    for route, script in (
        (RouteMode.AUTO, [(WorkerStatus.SUCCEEDED, None), (WorkerStatus.FAILED, "WORKER_INPUT_INVALID")]),
        (RouteMode.IMAGE_EDIT, [(WorkerStatus.SUCCEEDED, None), (WorkerStatus.FAILED, "WORKER_INPUT_INVALID")]),
    ):
        chain, _repository, _store, workers = build_chain(tmp_path / route.value, script)
        chain.submit(submission(source, route=route), request_id="submit", job_id="job-1", requester=requester())
        final = chain.run_to_intervention_or_terminal("job-1")
        assert final.state is JobState.FAILED
        assert final.error is not None and final.error.code == "WORKER_INPUT_INVALID"
        assert len(workers.tasks) == 2


def test_worker_retry_exhaustion_guard_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, _repository, _store, _workers = build_chain(tmp_path)
    monkeypatch.setattr(offline_module, "range", lambda count: (), raising=False)
    with pytest.raises(AssertionError, match="retry loop is exhaustive"):
        chain._execute(  # type: ignore[attr-defined]
            SimpleNamespace(job_id="job-1", revision=0),  # type: ignore[arg-type]
            WorkerKind.IMAGE_PREPARE,
            (),
            {},
        )


def test_artifact_content_failure_preserves_role_detail(tmp_path: Path) -> None:
    chain, _repository, _store, _workers = build_chain(tmp_path)
    task = WorkerTaskV1(
        1,
        "task",
        "job-1",
        0,
        "attempt",
        WorkerKind.TEST_ECHO,
        (),
        chain._config.digests,  # type: ignore[attr-defined]
        str(tmp_path.resolve()),
        {"mode": "echo"},
    )
    cause = DrawingMachineError(
        ErrorPayload("INVALID", ErrorCategory.VALIDATION, "invalid", False, {"role": "route_decision"})
    )
    outcome = OfflineJobChain._artifact_content_failure(task, cause)
    assert outcome.error is not None
    assert outcome.error.details == {"cause_code": "INVALID", "role": "route_decision"}

    cause_without_role = DrawingMachineError(
        ErrorPayload("INVALID", ErrorCategory.VALIDATION, "invalid", False, {"role": 1})
    )
    outcome_without_role = OfflineJobChain._artifact_content_failure(task, cause_without_role)
    assert outcome_without_role.error is not None
    assert outcome_without_role.error.details == {"cause_code": "INVALID"}


def test_gcode_safety_binding_rejects_task_digest_drift(tmp_path: Path) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_text("G21\n", encoding="ascii")
    chain, repository, _store, _workers = build_chain(tmp_path)
    job = chain.submit_gcode_check(source, request_id="submit", job_id="job-1", requester=requester())
    validating = chain.advance(job.job_id)
    gcode = next(item for item in repository.list_artifacts(job.job_id) if item.role == "gcode")
    task = WorkerTaskV1(
        1,
        "task",
        job.job_id,
        validating.revision,
        "attempt",
        WorkerKind.GCODE_CHECK,
        (chain._worker_input(job.job_id, "gcode"),),  # type: ignore[attr-defined]
        chain._config.digests,  # type: ignore[attr-defined]
        str(tmp_path.resolve()),
        {"machine_build_profile": chain._machine_profile(), "expected_gcode_sha256": "0" * 64},  # type: ignore[attr-defined]
    )
    assert gcode.sha256 != "0" * 64
    with pytest.raises(DrawingMachineError) as caught:
        chain._expected_gcode_safety_documents(task)  # type: ignore[attr-defined]
    assert caught.value.payload.code == "WORKER_ARTIFACT_CONTENT_INVALID"
    assert caught.value.payload.details == {"role": "gcode"}


def test_transition_commit_withdrawal_rolls_back_state(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "transition.db", clock=FakeClock())
    repository.initialize()
    store = FakeStore(tmp_path / "transition-store", [], enforce_order=False)
    chain = OfflineJobChain(
        repository=repository,
        artifacts=store,
        workers=FakeWorkers(),
        config=config(tmp_path),
        clock=FakeClock(),
    )
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    job = chain.submit(submission(source), request_id="submit", job_id="job-1", requester=requester())
    with pytest.raises(offline_module._CoordinatorCommitAbandoned):
        chain._transition(  # type: ignore[attr-defined]
            job,
            JobState.PREPARING_IMAGE,
            "job.preparing",
            "transition",
            requester(),
            commit_allowed=lambda: False,
            projection_deferred=True,
        )
    assert repository.get_job(job.job_id) == job
    repository.close()
