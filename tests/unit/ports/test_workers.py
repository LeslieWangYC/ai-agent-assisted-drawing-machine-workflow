import copy
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

import drawingmachine.ports.workers as workers_module
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.artifacts import WorkerArtifact
from drawingmachine.ports.workers import (
    WorkerInputArtifact,
    WorkerKind,
    WorkerOutcomeV1,
    WorkerStatus,
    WorkerTaskV1,
)


def input_artifact(path: Path) -> WorkerInputArtifact:
    return WorkerInputArtifact(
        role="input",
        absolute_path=str(path.resolve()),
        sha256="a" * 64,
        size_bytes=3,
        media_type="image/png",
    )


def task_json(tmp_path: Path) -> dict[str, Any]:
    source = tmp_path / "input.png"
    source.write_bytes(b"png")
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return {
        "schema_version": 1,
        "task_id": "task-1",
        "job_id": "job-1",
        "job_revision": 3,
        "attempt_id": "attempt-1",
        "kind": "test.echo",
        "input_artifacts": [
            {
                "role": "input",
                "absolute_path": str(source.resolve()),
                "sha256": "a" * 64,
                "size_bytes": 3,
                "media_type": "image/png",
            }
        ],
        "config_digests": {"application": "b" * 64},
        "staging_dir": str(staging.resolve()),
        "payload": {"mode": "echo", "nested": [{"enabled": True}]},
    }


def error_payload(*, job_id: str = "job-1") -> ErrorPayload:
    return ErrorPayload(
        code="WORKER_FAILED",
        category=ErrorCategory.INTERNAL,
        message="worker failed",
        retryable=False,
        details={"stage": {"name": "fixture"}},
        job_id=job_id,
    )


def outcome_json(status: str = "SUCCEEDED") -> dict[str, Any]:
    error = None
    if status != "SUCCEEDED":
        error = error_payload().to_json()
    return {
        "schema_version": 1,
        "task_id": "task-1",
        "job_id": "job-1",
        "job_revision": 3,
        "attempt_id": "attempt-1",
        "status": status,
        "artifacts": [],
        "metrics": {"elapsed_ms": 12, "nested": [1, {"ok": True}]},
        "error": error,
    }


def test_worker_task_round_trip_rejects_unknown_fields(tmp_path: Path) -> None:
    task = WorkerTaskV1.from_json(task_json(tmp_path))

    assert WorkerTaskV1.from_json(task.to_json()) == task
    invalid = task.to_json()
    invalid["database_path"] = "/forbidden.db"
    with pytest.raises(DrawingMachineError, match="unknown"):
        WorkerTaskV1.from_json(invalid)


def test_worker_task_detaches_and_deeply_freezes_json(tmp_path: Path) -> None:
    raw = task_json(tmp_path)
    task = WorkerTaskV1.from_json(raw)
    raw["payload"]["nested"][0]["enabled"] = False
    raw["config_digests"]["application"] = "f" * 64

    assert task.payload["nested"][0]["enabled"] is True
    assert task.config_digests["application"] == "b" * 64
    with pytest.raises(TypeError):
        task.payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        task.payload["nested"][0]["enabled"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        task.config_digests["application"] = "c" * 64  # type: ignore[index]
    with pytest.raises(TypeError):
        task.config_digests.__init__([("changed", "c" * 64)])  # type: ignore[misc]
    assert copy.copy(task.payload) is task.payload
    assert copy.deepcopy(task.payload) is task.payload

    serialized = task.to_json()
    serialized["payload"]["nested"][0]["enabled"] = False
    assert task.payload["nested"][0]["enabled"] is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema"),
        ("task_id", "../task", "task_id"),
        ("job_id", "", "job_id"),
        ("job_revision", True, "job_revision"),
        ("attempt_id", ".", "attempt_id"),
        ("kind", "unknown", "kind"),
        ("staging_dir", "relative/staging", "absolute"),
    ],
)
def test_worker_task_rejects_invalid_scalar_contracts(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = task_json(tmp_path)
    payload[field] = value

    with pytest.raises(DrawingMachineError, match=message):
        WorkerTaskV1.from_json(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"input_artifacts": [{"role": "input"}]},
        {"config_digests": {"application": "BAD"}},
        {"config_digests": {1: "b" * 64}},
        {"payload": {"nested": {"serial_device": "/dev/ttyUSB0"}}},
        {"payload": {"service_socket": "/run/drawingmachine.sock"}},
        {"payload": {"database_path": "/data/jobs.sqlite3"}},
        {"payload": {"bad": float("nan")}},
    ],
)
def test_worker_task_rejects_invalid_nested_contracts(tmp_path: Path, payload_update: dict[str, Any]) -> None:
    payload = task_json(tmp_path)
    payload.update(payload_update)

    with pytest.raises(DrawingMachineError):
        WorkerTaskV1.from_json(payload)


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "serial_port",
        "SocketPath",
        "DATABASE",
        "Controller",
        "hardware",
        "OpenClaw",
        "bridge_endpoint",
        "AUTHORIZATION",
        "ArmPhrase",
        "DBPath",
        "SQLitePath",
        "AuthToken",
        "TTYDevice",
        "CNCController",
        "DataBasePath",
        "AuthoriZation",
        "FluidNCAddress",
        "PostgresDSN",
        "HardWareAuthority",
        "SerIalPort",
        "SoCketPath",
        "OpenCLawRoot",
        "ConTrollerState",
        "BriDgeCommand",
        "AppRoval",
        "ArMPhrase",
        "MySQLDSN",
        "MongoDBURI",
        "UARTDevice",
        "GRBLAddress",
        "AuthenticationToken",
        "ArmingToken",
        "ApprovedBy",
        "COMPort",
        "NamedPipe",
    ],
)
def test_worker_task_rejects_nested_authority_aliases_and_case_variants(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    payload = task_json(tmp_path)
    payload["payload"] = {"nested": {forbidden_key: "forbidden"}}

    with pytest.raises(DrawingMachineError, match="forbidden worker fields"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_allows_safe_fields_that_only_contain_forbidden_substrings(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["payload"] = {
        "mode": "echo",
        "nested": [
            {
                "enabled": True,
                "pretty_print": True,
                "bounds_normalized": True,
                "redistribute_paths": False,
            }
        ],
    }

    task = WorkerTaskV1.from_json(payload)

    assert task.payload["nested"][0]["pretty_print"] is True


@pytest.mark.parametrize(
    ("kind", "valid_payload"),
    [
        (
            "image.prepare",
            {
                "mode": "direct",
                "source_name": "input.png",
                "route_mode": "auto",
                "semantic_assessment": None,
                "normalization": {},
            },
        ),
        (
            "image.prepare",
            {"mode": "normalize_existing", "source_name": "provider.png", "normalization": {}},
        ),
        (
            "provider.local_comfyui",
            {"prompt": None, "provider_profile": {}, "provider_config_path": "/config/provider.toml"},
        ),
        (
            "paths.plan",
            {"planning_profile": {}, "complexity_limits": {}, "base_dedupe_profile": {}},
        ),
        ("gcode.build", {"machine_build_profile": {}}),
        ("gcode.check", {"machine_build_profile": {}, "expected_gcode_sha256": "a" * 64}),
    ],
)
def test_worker_task_rejects_unknown_top_level_payload_fields_for_each_production_kind(
    tmp_path: Path,
    kind: str,
    valid_payload: dict[str, Any],
) -> None:
    payload = task_json(tmp_path)
    payload["kind"] = kind
    payload["payload"] = valid_payload

    task = WorkerTaskV1.from_json(payload)

    assert set(task.payload) == set(valid_payload)

    payload["payload"] = {**valid_payload, "unplanned_value": True}

    with pytest.raises(DrawingMachineError, match="unknown worker payload fields"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_rejects_unknown_nested_payload_fields(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["payload"] = {"mode": "echo", "nested": [{"enabled": True, "unplanned_value": True}]}

    with pytest.raises(DrawingMachineError, match="unknown worker payload fields"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_rejects_allowlisted_field_in_wrong_payload_context(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["kind"] = "provider.local_comfyui"
    payload["payload"] = {
        "prompt": None,
        "provider_profile": {
            "schema_version": 1,
            "profile": {"sampler_defaults": {"endpoint": "http://127.0.0.1:18188"}},
        },
        "provider_config_path": "/config/provider.toml",
    }

    with pytest.raises(DrawingMachineError, match="unknown worker payload fields"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_allows_planned_numeric_hardware_dimensions_without_granting_authority(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["kind"] = "gcode.build"
    payload["payload"] = {
        "machine_build_profile": {
            "schema_version": 1,
            "profile": {
                "name": "default",
                "gcode": {
                    "hardware_canvas_width_mm": 120.0,
                    "hardware_canvas_height_mm": 120.0,
                },
            },
        }
    }

    task = WorkerTaskV1.from_json(payload)

    assert task.payload["machine_build_profile"]["profile"]["gcode"]["hardware_canvas_width_mm"] == 120.0


def test_worker_task_rejects_integer_that_json_cannot_serialize(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["payload"] = {"too_large": 10**5000}

    with pytest.raises(DrawingMachineError, match="serialized"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_rejects_duplicate_input_roles_and_paths(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    duplicate = dict(payload["input_artifacts"][0])
    duplicate["absolute_path"] = str((tmp_path / "other.png").resolve())
    payload["input_artifacts"].append(duplicate)
    with pytest.raises(DrawingMachineError, match="duplicate input artifact role"):
        WorkerTaskV1.from_json(payload)

    payload = task_json(tmp_path)
    duplicate = dict(payload["input_artifacts"][0])
    duplicate["role"] = "other"
    payload["input_artifacts"].append(duplicate)
    with pytest.raises(DrawingMachineError, match="duplicate input artifact path"):
        WorkerTaskV1.from_json(payload)


def test_worker_task_allows_no_input_artifacts_for_source_workers(tmp_path: Path) -> None:
    payload = task_json(tmp_path)
    payload["input_artifacts"] = []

    task = WorkerTaskV1.from_json(payload)

    assert task.input_artifacts == ()


def test_test_worker_payload_rejects_logically_missing_and_unknown_fields(tmp_path: Path) -> None:
    class MissingMode(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            return "echo" if key == "mode" else super().get(key, default)

    with pytest.raises(DrawingMachineError, match="missing worker payload fields: mode"):
        workers_module._validate_worker_payload(WorkerKind.TEST_ECHO, MissingMode())

    payload = task_json(tmp_path)
    payload["payload"] = {"mode": "echo", "unexpected": True}
    with pytest.raises(DrawingMachineError, match="unknown worker payload fields"):
        WorkerTaskV1.from_json(payload)

    class LateUnknown(Mapping[str, object]):
        def __init__(self) -> None:
            self.iterations = 0

        def __getitem__(self, key: str) -> object:
            return {"mode": "echo", "unexpected": True}[key]

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            return iter(("mode", "unexpected") if self.iterations >= 4 else ("mode",))

        def __len__(self) -> int:
            return 2

    with pytest.raises(DrawingMachineError, match="unknown worker payload fields"):
        workers_module._validate_worker_payload(WorkerKind.TEST_ECHO, LateUnknown())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("absolute_path", "relative.png"),
        ("sha256", "A" * 64),
        ("size_bytes", -1),
        ("size_bytes", True),
        ("role", ""),
        ("media_type", ""),
    ],
)
def test_worker_input_artifact_rejects_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    values: dict[str, object] = {
        "role": "input",
        "absolute_path": str((tmp_path / "input.png").resolve()),
        "sha256": "a" * 64,
        "size_bytes": 3,
        "media_type": "image/png",
    }
    values[field] = value

    with pytest.raises(DrawingMachineError):
        WorkerInputArtifact(**values)  # type: ignore[arg-type]


def test_worker_outcome_round_trip_and_deep_freeze() -> None:
    outcome = WorkerOutcomeV1.from_json(outcome_json())

    assert WorkerOutcomeV1.from_json(outcome.to_json()) == outcome
    assert outcome.status is WorkerStatus.SUCCEEDED
    with pytest.raises(TypeError):
        outcome.metrics["elapsed_ms"] = 0  # type: ignore[index]
    serialized = outcome.to_json()
    serialized["metrics"]["nested"][1]["ok"] = False
    assert outcome.metrics["nested"][1]["ok"] is True


def test_worker_outcome_clones_error_and_artifacts() -> None:
    artifact = WorkerArtifact("result", "result.json", "c" * 64, 17, "application/json")
    source_error = error_payload()
    outcome = WorkerOutcomeV1(
        schema_version=1,
        task_id="task-1",
        job_id="job-1",
        job_revision=3,
        attempt_id="attempt-1",
        status=WorkerStatus.BLOCKED,
        artifacts=(artifact,),
        metrics={},
        error=source_error,
    )

    assert outcome.artifacts == (artifact,)
    assert outcome.error is not source_error
    with pytest.raises(TypeError):
        outcome.error.details["new"] = True  # type: ignore[index,union-attr]
    assert WorkerOutcomeV1.from_json(outcome.to_json()) == outcome


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {"schema_version": 2},
        {"task_id": "../task"},
        {"job_revision": -1},
        {"status": "UNKNOWN"},
        {"status": "FAILED", "error": None},
        {"status": "SUCCEEDED", "error": error_payload().to_json()},
        {"status": "FAILED", "artifacts": [{"role": "bad"}]},
        {"status": "FAILED", "error": error_payload(job_id="other-job").to_json()},
    ],
)
def test_worker_outcome_rejects_invalid_contracts(mutation: dict[str, Any]) -> None:
    payload = outcome_json()
    payload.update(mutation)

    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1.from_json(payload)


def test_worker_models_are_frozen_slotted_and_use_exact_enums(tmp_path: Path) -> None:
    task = WorkerTaskV1.from_json(task_json(tmp_path))
    outcome = WorkerOutcomeV1.from_json(outcome_json())

    with pytest.raises(FrozenInstanceError):
        task.task_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        outcome.status = WorkerStatus.FAILED  # type: ignore[misc]
    assert not hasattr(task, "__dict__")
    assert not hasattr(outcome, "__dict__")
    assert WorkerKind.IMAGE_PREPARE.value == "image.prepare"
    assert WorkerKind.PROVIDER_LOCAL_COMFYUI.value == "provider.local_comfyui"
    assert WorkerKind.PATHS_PLAN.value == "paths.plan"
    assert WorkerKind.GCODE_BUILD.value == "gcode.build"
    assert WorkerKind.GCODE_CHECK.value == "gcode.check"
    assert WorkerKind.TEST_ECHO.value == "test.echo"
    assert WorkerKind.TEST_HANG.value == "test.hang"
