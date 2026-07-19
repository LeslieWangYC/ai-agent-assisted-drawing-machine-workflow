from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import drawingmachine.domain.jobs.models as models
from drawingmachine.domain.jobs import (
    ArtifactRef,
    ConfigSnapshot,
    JobBlocker,
    JobKind,
    JobRecord,
    JobState,
    JobTransition,
    ReadyToRunSnapshot,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject


def _artifact(role: str = "gcode") -> ArtifactRef:
    return ArtifactRef(role, f"artifacts/{role}.bin", "a" * 64, 1, "application/octet-stream")


def _config() -> ConfigSnapshot:
    return ConfigSnapshot("default", "local-comfyui", 1, 1, 1, "a" * 64, "b" * 64, "c" * 64)


def _ready(*, job_id: str = "job-1", revision: int = 2) -> ReadyToRunSnapshot:
    return ReadyToRunSnapshot(
        1,
        job_id,
        revision,
        _artifact(),
        (_artifact("preview"),),
        {"gate": "PASS"},
        {"line_count": 1},
        {"status": "READY"},
        _config(),
        {},
        "test",
    )


def _error(*, job_id: str = "job-1", request_id: str = "request-1") -> ErrorPayload:
    return ErrorPayload(
        "FAILED",
        ErrorCategory.SERVICE,
        "failed",
        False,
        {},
        request_id=request_id,
        job_id=job_id,
    )


def _record(**changes: object) -> JobRecord:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    values: dict[str, object] = {
        "schema_version": 1,
        "job_id": "job-1",
        "kind": JobKind.OFFLINE_WORKFLOW,
        "name": "job",
        "state": JobState.QUEUED,
        "revision": 0,
        "request": {},
        "config": _config(),
        "blocker": None,
        "error": None,
        "ready_snapshot": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(changes)
    return JobRecord(**cast(Any, values))


def test_frozen_array_comparison_and_low_level_validators_cover_false_paths() -> None:
    frozen = models._FrozenJsonArray._create((1, 2))
    assert frozen == [1, 2]
    assert frozen != "12"
    with pytest.raises(DrawingMachineError):
        models._object([], "payload")
    with pytest.raises(DrawingMachineError):
        models._object({1: "bad"}, "payload")
    with pytest.raises(DrawingMachineError):
        models._validate_schema_version(2, "version")
    with pytest.raises(DrawingMachineError):
        models._freeze_json_object([], "payload")
    with pytest.raises(DrawingMachineError):
        models._freeze_json_object(cast(JsonObject, {1: "bad"}), "payload")
    with pytest.raises(DrawingMachineError):
        models._validate_datetime("not-a-date", "timestamp")


def test_blocker_rejects_non_enum_category() -> None:
    with pytest.raises(DrawingMachineError):
        JobBlocker("BLOCKED", cast(ErrorCategory, "validation"), "blocked", {})


@pytest.mark.parametrize(
    "changes",
    [
        {"gcode": cast(ArtifactRef, object())},
        {"gcode": _artifact("preview")},
        {"artifacts": cast(tuple[ArtifactRef, ...], [])},
        {"artifacts": (cast(ArtifactRef, object()),)},
        {"config": cast(ConfigSnapshot, object())},
    ],
)
def test_ready_snapshot_direct_constructor_rejects_wrong_typed_fields(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "schema_version": 1,
        "job_id": "job-1",
        "job_revision": 2,
        "gcode": _artifact(),
        "artifacts": (_artifact("preview"),),
        "static_result": {},
        "send_plan": {},
        "readiness": {},
        "config": _config(),
        "planning_parameters": {},
        "application_version": "test",
    }
    values.update(changes)
    with pytest.raises(DrawingMachineError):
        ReadyToRunSnapshot(**cast(Any, values))


def test_ready_snapshot_json_rejects_non_array_artifacts() -> None:
    payload = _ready().to_json()
    payload["artifacts"] = {}

    with pytest.raises(DrawingMachineError):
        ReadyToRunSnapshot.from_json(payload)


def test_error_and_state_payload_helpers_reject_wrong_types_and_combinations() -> None:
    with pytest.raises(DrawingMachineError):
        models._clone_error(object(), "error")
    with pytest.raises(DrawingMachineError):
        models._validated_state_payloads(JobState.QUEUED, object(), None, None, "job")
    with pytest.raises(DrawingMachineError):
        models._validated_state_payloads(JobState.BLOCKED, None, None, None, "job")
    with pytest.raises(DrawingMachineError):
        models._validated_state_payloads(JobState.QUEUED, None, _error(), None, "job")
    with pytest.raises(DrawingMachineError):
        models._validated_state_payloads(JobState.READY_TO_RUN, None, None, None, "job")
    with pytest.raises(DrawingMachineError):
        models._validated_state_payloads(JobState.QUEUED, None, None, _ready(), "job")


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": cast(JobKind, "OFFLINE_WORKFLOW")},
        {"state": cast(JobState, "QUEUED")},
        {"config": cast(ConfigSnapshot, object())},
        {"updated_at": datetime(2026, 7, 9, tzinfo=UTC)},
        {"state": JobState.FAILED, "error": _error(job_id="other")},
        {
            "state": JobState.READY_TO_RUN,
            "revision": 2,
            "ready_snapshot": _ready(job_id="other", revision=2),
        },
    ],
)
def test_job_record_rejects_invalid_direct_fields(changes: dict[str, object]) -> None:
    with pytest.raises(DrawingMachineError):
        _record(**changes)


def test_cancelled_record_accepts_only_an_older_immutable_ready_snapshot() -> None:
    assert (
        _record(
            state=JobState.CANCELLED,
            revision=3,
            ready_snapshot=_ready(revision=2),
            updated_at=datetime(2026, 7, 10, tzinfo=UTC) + timedelta(seconds=1),
        ).ready_snapshot
        is not None
    )
    with pytest.raises(DrawingMachineError):
        _record(state=JobState.CANCELLED, revision=2, ready_snapshot=_ready(revision=2))


def test_transition_rejects_invalid_state_and_ready_snapshot_identity() -> None:
    with pytest.raises(DrawingMachineError):
        JobTransition("job-1", 0, cast(JobState, "QUEUED"), None, "event")
    with pytest.raises(DrawingMachineError):
        JobTransition("job-1", 0, JobState.RECOVERY_REQUIRED, None, "event")
    with pytest.raises(DrawingMachineError):
        JobTransition(
            "job-1",
            1,
            JobState.READY_TO_RUN,
            None,
            "event",
            ready_snapshot=_ready(job_id="other", revision=2),
        )
    with pytest.raises(DrawingMachineError):
        JobTransition(
            "job-1",
            1,
            JobState.READY_TO_RUN,
            None,
            "event",
            ready_snapshot=_ready(revision=3),
        )
