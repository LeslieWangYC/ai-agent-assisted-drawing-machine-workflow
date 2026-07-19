import copy
import json
import operator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

import drawingmachine.domain.jobs.models as job_models
from drawingmachine.domain.jobs import (
    ArtifactRef,
    ConfigSnapshot,
    JobBlocker,
    JobKind,
    JobRecord,
    JobState,
    JobTransition,
    ReadyToRunSnapshot,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload


def artifact_json(role: str = "gcode", digest: str = "a" * 64) -> dict[str, Any]:
    return {
        "role": role,
        "relative_path": f"artifacts/{role}.json",
        "sha256": digest,
        "size_bytes": 17,
        "media_type": "application/json",
    }


def config_snapshot_json() -> dict[str, Any]:
    return {
        "machine_profile": "default",
        "provider_profile": "local-comfyui",
        "application_schema_version": 1,
        "machine_schema_version": 1,
        "provider_schema_version": 1,
        "application_digest": "b" * 64,
        "machine_digest": "c" * 64,
        "provider_digest": "d" * 64,
    }


def ready_snapshot_json() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "job_revision": 7,
        "gcode": artifact_json("gcode", "a" * 64),
        "artifacts": [artifact_json("preview", "e" * 64)],
        "static_result": {"gate": "PASS"},
        "send_plan": {"line_count": 10},
        "readiness": {"status": "READY"},
        "config": config_snapshot_json(),
        "planning_parameters": {"stroke_mode": "stroke"},
        "application_version": "0.2.0.dev0",
    }


def job_record_json() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "kind": "OFFLINE_WORKFLOW",
        "name": "fixture job",
        "state": "QUEUED",
        "revision": 0,
        "request": {"route_mode": "direct"},
        "config": config_snapshot_json(),
        "blocker": None,
        "error": None,
        "ready_snapshot": None,
        "created_at": "2026-07-10T12:00:00+00:00",
        "updated_at": "2026-07-10T12:00:01+00:00",
    }


def transition_json() -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "expected_revision": 0,
        "result_state": "PREPARING_IMAGE",
        "request_id": "req-1",
        "event_type": "job.preparing_image",
        "blocker": None,
        "error": None,
        "ready_snapshot": None,
    }


def error_json(
    *,
    job_id: str | None = "job-1",
    request_id: str | None = "req-1",
) -> dict[str, Any]:
    return {
        "code": "FAILED",
        "category": "service",
        "message": "failed",
        "retryable": False,
        "details": {"stage": "validation"},
        "request_id": request_id,
        "job_id": job_id,
    }


def job_payload_for_state(kind: JobKind, state: JobState) -> dict[str, Any]:
    payload = job_record_json()
    payload.update({"kind": kind.value, "state": state.value})
    if state is JobState.BLOCKED:
        payload["blocker"] = {
            "code": "STATIC_CHECK_FAILED",
            "category": "validation",
            "message": "blocked",
            "details": {},
        }
    if state is JobState.FAILED:
        payload["error"] = error_json()
    if state is JobState.READY_TO_RUN:
        payload["revision"] = 7
        payload["ready_snapshot"] = ready_snapshot_json()
    if kind is JobKind.OFFLINE_WORKFLOW and state in {JobState.COMPLETED, JobState.RECOVERY_REQUIRED}:
        payload["revision"] = 8
        payload["ready_snapshot"] = ready_snapshot_json()
    return payload


def test_artifact_config_blocker_and_requester_round_trip() -> None:
    artifact = ArtifactRef.from_json(artifact_json())
    config = ConfigSnapshot.from_json(config_snapshot_json())
    blocker = JobBlocker.from_json(
        {
            "code": "REVIEW_REQUIRED",
            "category": "validation",
            "message": "review required",
            "details": {"review_artifact": "artifacts/review.json"},
        }
    )
    requester = RequesterIdentity.from_json({"requester_type": "LOCAL_PEER", "pid": 101, "uid": 1000, "gid": 1000})

    assert ArtifactRef.from_json(artifact.to_json()) == artifact
    assert ConfigSnapshot.from_json(config.to_json()) == config
    assert JobBlocker.from_json(blocker.to_json()) == blocker
    assert RequesterIdentity.from_json(requester.to_json()) == requester
    assert blocker.category is ErrorCategory.VALIDATION


def test_models_are_frozen_and_slotted() -> None:
    artifact = ArtifactRef.from_json(artifact_json())
    with pytest.raises(FrozenInstanceError):
        artifact.role = "changed"  # type: ignore[misc]
    assert not hasattr(artifact, "__dict__")


@pytest.mark.parametrize(
    "payload",
    [
        artifact_json(digest="short"),
        artifact_json(digest="A" * 64),
        {**artifact_json(), "relative_path": "/absolute/gcode.json"},
        {**artifact_json(), "relative_path": "artifacts/../gcode.json"},
        {**artifact_json(), "relative_path": "artifacts\\gcode.json"},
        {**artifact_json(), "size_bytes": -1},
        {**artifact_json(), "size_bytes": True},
    ],
)
def test_artifact_rejects_invalid_digest_path_or_size(payload: dict[str, Any]) -> None:
    with pytest.raises(DrawingMachineError):
        ArtifactRef.from_json(payload)


@pytest.mark.parametrize(
    "field",
    ["application_digest", "machine_digest", "provider_digest"],
)
def test_config_snapshot_requires_sha256_digests(field: str) -> None:
    payload = config_snapshot_json()
    payload[field] = "not-a-digest"

    with pytest.raises(DrawingMachineError, match="SHA-256"):
        ConfigSnapshot.from_json(payload)


def test_ready_snapshot_round_trip() -> None:
    snapshot = ReadyToRunSnapshot.from_json(ready_snapshot_json())

    assert ReadyToRunSnapshot.from_json(snapshot.to_json()) == snapshot
    assert snapshot.artifacts[0].role == "preview"


def test_ready_snapshot_rejects_hardware_authority_fields() -> None:
    payload = ready_snapshot_json()
    payload["bridge_command"] = "forbidden"

    with pytest.raises(DrawingMachineError, match="hardware authority"):
        ReadyToRunSnapshot.from_json(payload)


def test_ready_snapshot_rejects_revision_zero() -> None:
    payload = ready_snapshot_json()
    payload["job_revision"] = 0

    with pytest.raises(DrawingMachineError, match="job_revision"):
        ReadyToRunSnapshot.from_json(payload)


def test_ready_snapshot_rejects_duplicate_artifact_roles() -> None:
    payload = ready_snapshot_json()
    payload["artifacts"] = [artifact_json("preview", "e" * 64), artifact_json("preview", "f" * 64)]

    with pytest.raises(DrawingMachineError, match="duplicate artifact role"):
        ReadyToRunSnapshot.from_json(payload)


def test_ready_snapshot_rejects_gcode_role_in_artifacts() -> None:
    payload = ready_snapshot_json()
    payload["artifacts"] = [artifact_json("gcode", "f" * 64)]

    with pytest.raises(DrawingMachineError, match="gcode"):
        ReadyToRunSnapshot.from_json(payload)


def test_direct_ready_snapshot_rejects_gcode_role_in_artifacts() -> None:
    payload = ready_snapshot_json()

    with pytest.raises(DrawingMachineError, match="gcode"):
        ReadyToRunSnapshot(
            schema_version=1,
            job_id="job-1",
            job_revision=7,
            gcode=ArtifactRef.from_json(payload["gcode"]),
            artifacts=(ArtifactRef.from_json(artifact_json("gcode", "f" * 64)),),
            static_result={"gate": "PASS"},
            send_plan={"line_count": 10},
            readiness={"status": "READY"},
            config=ConfigSnapshot.from_json(config_snapshot_json()),
            planning_parameters={},
            application_version="0.2.0.dev0",
        )


def test_ready_snapshot_rejects_non_json_values() -> None:
    payload = ready_snapshot_json()
    payload["planning_parameters"] = {"invalid": object()}

    with pytest.raises(DrawingMachineError, match="JSON-compatible"):
        ReadyToRunSnapshot.from_json(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_ready_snapshot_rejects_non_finite_json_values(value: float) -> None:
    payload = ready_snapshot_json()
    payload["static_result"] = {"nested": [value]}

    with pytest.raises(DrawingMachineError, match="finite"):
        ReadyToRunSnapshot.from_json(payload)


def test_ready_snapshot_rejects_recursive_json_values() -> None:
    recursive_mapping: dict[str, Any] = {}
    recursive_mapping["self"] = recursive_mapping
    mapping_payload = ready_snapshot_json()
    mapping_payload["send_plan"] = recursive_mapping
    with pytest.raises(DrawingMachineError, match="recursive"):
        ReadyToRunSnapshot.from_json(mapping_payload)

    recursive_list: list[Any] = []
    recursive_list.append(recursive_list)
    list_payload = ready_snapshot_json()
    list_payload["readiness"] = {"values": recursive_list}
    with pytest.raises(DrawingMachineError, match="recursive"):
        ReadyToRunSnapshot.from_json(list_payload)


@pytest.mark.parametrize(
    "authority_key",
    [
        "approval",
        "arm_phrase",
        "authorization",
        "bridge_command",
        "bridge_commands",
        "hardware_authority",
        "serial_device",
        "serial_path",
    ],
)
@pytest.mark.parametrize("field", ["static_result", "send_plan", "readiness", "planning_parameters"])
def test_ready_snapshot_rejects_every_nested_hardware_authority_key(
    field: str,
    authority_key: str,
) -> None:
    payload = ready_snapshot_json()
    payload[field] = {"outer": [{authority_key: "forbidden"}]}

    with pytest.raises(DrawingMachineError, match="hardware authority"):
        ReadyToRunSnapshot.from_json(payload)


def test_direct_ready_snapshot_rejects_nested_hardware_authority() -> None:
    payload = ready_snapshot_json()

    with pytest.raises(DrawingMachineError, match="hardware authority"):
        ReadyToRunSnapshot(
            schema_version=1,
            job_id="job-1",
            job_revision=7,
            gcode=ArtifactRef.from_json(payload["gcode"]),
            artifacts=(),
            static_result={"gate": "PASS"},
            send_plan={"line_count": 10},
            readiness={"status": "READY"},
            config=ConfigSnapshot.from_json(config_snapshot_json()),
            planning_parameters={"nested": [{"serial_path": "/dev/ttyUSB0"}]},
            application_version="0.2.0.dev0",
        )


def test_json_fields_are_deeply_immutable_for_direct_and_decoded_models() -> None:
    source_details = {"nested": {"values": [1]}}
    blocker = JobBlocker(
        code="REVIEW_REQUIRED",
        category=ErrorCategory.VALIDATION,
        message="review required",
        details=source_details,
    )
    snapshot_payload = ready_snapshot_json()
    snapshot_payload["static_result"] = {"nested": {"values": [1]}}
    snapshot = ReadyToRunSnapshot.from_json(snapshot_payload)

    source_details["nested"]["values"].append(2)
    with pytest.raises((AttributeError, TypeError)):
        blocker.details["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    with pytest.raises((AttributeError, TypeError)):
        snapshot.static_result["nested"]["new"] = True  # type: ignore[index]

    assert blocker.details["nested"]["values"] == [1]  # type: ignore[index]
    assert snapshot.static_result["nested"]["values"] == [1]  # type: ignore[index]


def test_to_json_returns_detached_plain_mutable_values() -> None:
    blocker = JobBlocker.from_json(
        {
            "code": "REVIEW_REQUIRED",
            "category": "validation",
            "message": "review required",
            "details": {"nested": {"values": [1]}},
        }
    )
    encoded = blocker.to_json()

    assert type(encoded) is dict
    assert type(encoded["details"]) is dict
    assert type(encoded["details"]["nested"]["values"]) is list  # type: ignore[index]
    encoded["details"]["nested"]["values"].append(2)  # type: ignore[index,union-attr]

    assert blocker.details["nested"]["values"] == [1]  # type: ignore[index]


def test_detached_json_output_remains_stdlib_json_serializable() -> None:
    payload = ready_snapshot_json()
    payload["planning_parameters"] = {"nested": {"values": [1, 2.5, None]}}
    snapshot = ReadyToRunSnapshot.from_json(payload)

    encoded = snapshot.to_json()

    assert json.loads(json.dumps(encoded)) == encoded


def _mapping_init_attack(mapping: Any, array: Any) -> None:
    del array
    mapping.__init__({"bridge_command": "forbidden"})


def _dict_setitem_attack(mapping: Any, array: Any) -> None:
    del array
    dict.__setitem__(mapping, "bridge_command", "forbidden")


def _guarded_mapping_reinit_attack(mapping: Any, array: Any) -> None:
    del array
    type(mapping).__init__(
        mapping,
        job_models._FROZEN_INIT_TOKEN,
        (("bridge_command", "forbidden"),),
    )


def _ordinary_mapping_attack(mapping: Any, array: Any) -> None:
    del array
    mapping.update({"bridge_command": "forbidden"})


def _mapping_in_place_attack(mapping: Any, array: Any) -> None:
    del array
    operator.ior(mapping, {"bridge_command": "forbidden"})


def _mapping_item_attack(mapping: Any, array: Any) -> None:
    del array
    operator.setitem(mapping, "bridge_command", "forbidden")


def _array_init_attack(mapping: Any, array: Any) -> None:
    del mapping
    array.__init__([{"serial_path": "/dev/ttyUSB0"}])


def _list_append_attack(mapping: Any, array: Any) -> None:
    del mapping
    list.append(array, {"serial_path": "/dev/ttyUSB0"})


def _guarded_array_reinit_attack(mapping: Any, array: Any) -> None:
    del mapping
    type(array).__init__(
        array,
        job_models._FROZEN_INIT_TOKEN,
        ({"serial_path": "/dev/ttyUSB0"},),
    )


def _list_slice_attack(mapping: Any, array: Any) -> None:
    del mapping
    list.__setitem__(array, slice(0, 0), [{"serial_path": "/dev/ttyUSB0"}])


def _ordinary_array_attack(mapping: Any, array: Any) -> None:
    del mapping
    array.extend([{"serial_path": "/dev/ttyUSB0"}])


def _array_in_place_attack(mapping: Any, array: Any) -> None:
    del mapping
    operator.iadd(array, [{"serial_path": "/dev/ttyUSB0"}])


def _array_item_attack(mapping: Any, array: Any) -> None:
    del mapping
    operator.setitem(array, 0, {"serial_path": "/dev/ttyUSB0"})


@pytest.mark.parametrize(
    ("case", "attack"),
    [
        ("mapping-init", _mapping_init_attack),
        ("mapping-guarded-reinit", _guarded_mapping_reinit_attack),
        ("dict-setitem", _dict_setitem_attack),
        ("mapping-update", _ordinary_mapping_attack),
        ("mapping-in-place", _mapping_in_place_attack),
        ("mapping-item", _mapping_item_attack),
        ("array-init", _array_init_attack),
        ("array-guarded-reinit", _guarded_array_reinit_attack),
        ("list-append", _list_append_attack),
        ("list-slice", _list_slice_attack),
        ("array-extend", _ordinary_array_attack),
        ("array-in-place", _array_in_place_attack),
        ("array-item", _array_item_attack),
    ],
)
def test_internal_json_rejects_mutable_builtin_and_ordinary_mutation_bypasses(
    case: str,
    attack: Any,
) -> None:
    payload = ready_snapshot_json()
    payload["planning_parameters"] = {"nested": {"values": [{"safe": True}]}}
    snapshot = ReadyToRunSnapshot.from_json(payload)
    mapping = snapshot.planning_parameters
    array = mapping["nested"]["values"]  # type: ignore[index]
    blocked = False

    try:
        attack(mapping, array)
    except (AttributeError, TypeError):
        blocked = True

    try:
        round_tripped = ReadyToRunSnapshot.from_json(snapshot.to_json())
    except DrawingMachineError as error:
        pytest.fail(f"{case} injected nested hardware authority: {error}")

    assert round_tripped == snapshot
    assert blocked, f"{case} mutation unexpectedly succeeded"


def test_internal_json_does_not_inherit_mutable_builtins() -> None:
    payload = ready_snapshot_json()
    payload["planning_parameters"] = {"nested": {"values": [1]}}
    snapshot = ReadyToRunSnapshot.from_json(payload)
    mapping = snapshot.planning_parameters
    array = mapping["nested"]["values"]  # type: ignore[index]

    assert not isinstance(mapping, dict)
    assert not isinstance(array, list)


def test_internal_json_copy_and_deepcopy_are_safe() -> None:
    payload = ready_snapshot_json()
    payload["planning_parameters"] = {"nested": {"values": [1]}}
    snapshot = ReadyToRunSnapshot.from_json(payload)
    mapping = snapshot.planning_parameters
    array = mapping["nested"]["values"]  # type: ignore[index]

    assert copy.copy(mapping) is mapping
    assert copy.deepcopy(mapping) is mapping
    assert copy.copy(array) is array
    assert copy.deepcopy(array) is array


def test_job_request_and_snapshot_json_are_deeply_immutable_and_detached() -> None:
    request = {"nested": {"values": [1]}}
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    job = JobRecord(
        schema_version=1,
        job_id="job-1",
        kind=JobKind.OFFLINE_WORKFLOW,
        name="fixture job",
        state=JobState.QUEUED,
        revision=0,
        request=request,
        config=ConfigSnapshot.from_json(config_snapshot_json()),
        blocker=None,
        error=None,
        ready_snapshot=None,
        created_at=now,
        updated_at=now,
    )
    snapshot = ReadyToRunSnapshot.from_json(ready_snapshot_json())

    request["nested"]["values"].append(2)
    with pytest.raises((AttributeError, TypeError)):
        job.request["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    with pytest.raises((AttributeError, TypeError)):
        snapshot.planning_parameters["stroke_mode"] = "fill"

    job_json = job.to_json()
    snapshot_json = snapshot.to_json()
    job_json["request"]["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    snapshot_json["planning_parameters"]["stroke_mode"] = "fill"  # type: ignore[index]
    assert job.request["nested"]["values"] == [1]  # type: ignore[index]
    assert snapshot.planning_parameters["stroke_mode"] == "stroke"


def test_job_record_and_transition_round_trip() -> None:
    job = JobRecord.from_json(job_record_json())
    transition = JobTransition.from_json(transition_json())

    assert JobRecord.from_json(job.to_json()) == job
    assert JobTransition.from_json(transition.to_json()) == transition
    assert job.kind is JobKind.OFFLINE_WORKFLOW
    assert job.state is JobState.QUEUED
    assert transition.result_state is JobState.PREPARING_IMAGE
    assert job.created_at == datetime(2026, 7, 10, 12, tzinfo=UTC)


def test_job_record_round_trip_preserves_typed_error() -> None:
    payload = job_record_json()
    payload.update(
        {
            "state": "FAILED",
            "error": ErrorPayload(
                code="SERVICE_RESTARTED",
                category=ErrorCategory.SERVICE,
                message="service restarted",
                retryable=False,
                details={"prior_state": "PREPARING_IMAGE"},
                request_id="req-1",
                job_id="job-1",
            ).to_json(),
        }
    )

    job = JobRecord.from_json(payload)

    assert job.error is not None
    assert job.error.category is ErrorCategory.SERVICE
    assert JobRecord.from_json(job.to_json()) == job


def test_offline_machine_outcome_requires_retained_ready_snapshot() -> None:
    payload = job_record_json()
    payload["state"] = "COMPLETED"

    with pytest.raises(DrawingMachineError, match="retain exactly one"):
        JobRecord.from_json(payload)

    assert (
        JobRecord.from_json(job_payload_for_state(JobKind.OFFLINE_WORKFLOW, JobState.COMPLETED)).state
        is JobState.COMPLETED
    )


def test_machine_outcome_transition_binds_job_and_earlier_ready_revision() -> None:
    snapshot = ReadyToRunSnapshot.from_json(ready_snapshot_json())
    with pytest.raises(DrawingMachineError, match="match job ID"):
        job_models.MachineJobOutcomeTransition("other-job", 8, JobState.COMPLETED, "job.completed", snapshot)
    with pytest.raises(DrawingMachineError, match="earlier immutable ready revision"):
        job_models.MachineJobOutcomeTransition("job-1", 6, JobState.COMPLETED, "job.completed", snapshot)


def test_gcode_check_job_record_cannot_use_offline_success_state() -> None:
    payload = job_record_json()
    payload.update(
        {
            "kind": "GCODE_CHECK",
            "state": "READY_TO_RUN",
            "revision": 7,
            "ready_snapshot": ready_snapshot_json(),
        }
    )

    with pytest.raises(DrawingMachineError, match="GCODE_CHECK"):
        JobRecord.from_json(payload)


@pytest.mark.parametrize("kind", list(JobKind))
@pytest.mark.parametrize("state", list(JobState))
def test_job_record_states_are_exhaustively_isolated_by_kind(kind: JobKind, state: JobState) -> None:
    offline_states = set(JobState)
    gcode_states = {
        JobState.QUEUED,
        JobState.VALIDATING_GCODE,
        JobState.COMPLETED,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
    expected_states = offline_states if kind is JobKind.OFFLINE_WORKFLOW else gcode_states

    if state in expected_states:
        assert JobRecord.from_json(job_payload_for_state(kind, state)).state is state
    else:
        with pytest.raises(DrawingMachineError, match=kind.value):
            JobRecord.from_json(job_payload_for_state(kind, state))


def test_job_transition_rejects_recovery_required_directly_and_from_json() -> None:
    with pytest.raises(DrawingMachineError, match="RECOVERY_REQUIRED"):
        JobTransition(
            job_id="job-1",
            expected_revision=2,
            result_state=JobState.RECOVERY_REQUIRED,
            request_id="req-1",
            event_type="job.recovery_required",
        )

    payload = transition_json()
    payload["result_state"] = "RECOVERY_REQUIRED"
    with pytest.raises(DrawingMachineError, match="RECOVERY_REQUIRED"):
        JobTransition.from_json(payload)


@pytest.mark.parametrize("constructor", ["direct", "from_json"])
def test_ready_transition_requires_next_snapshot_revision(constructor: str) -> None:
    snapshot_payload = ready_snapshot_json()
    snapshot = ReadyToRunSnapshot.from_json(snapshot_payload)

    if constructor == "direct":
        with pytest.raises(DrawingMachineError, match="expected_revision"):
            JobTransition(
                job_id="job-1",
                expected_revision=7,
                result_state=JobState.READY_TO_RUN,
                request_id="req-1",
                event_type="job.ready_to_run",
                ready_snapshot=snapshot,
            )
    else:
        payload = transition_json()
        payload.update(
            {
                "expected_revision": 7,
                "result_state": "READY_TO_RUN",
                "event_type": "job.ready_to_run",
                "ready_snapshot": snapshot_payload,
            }
        )
        with pytest.raises(DrawingMachineError, match="expected_revision"):
            JobTransition.from_json(payload)


def test_ready_transition_accepts_snapshot_for_next_revision() -> None:
    payload = transition_json()
    payload.update(
        {
            "expected_revision": 6,
            "result_state": "READY_TO_RUN",
            "event_type": "job.ready_to_run",
            "ready_snapshot": ready_snapshot_json(),
        }
    )

    transition = JobTransition.from_json(payload)

    assert transition.ready_snapshot is not None
    assert transition.ready_snapshot.job_revision == 7
    assert JobTransition.from_json(transition.to_json()) == transition


@pytest.mark.parametrize(
    ("field", "state"),
    [
        ("blocker", JobState.BLOCKED),
        ("error", JobState.FAILED),
        ("ready_snapshot", JobState.READY_TO_RUN),
    ],
)
@pytest.mark.parametrize("model", ["record", "transition"])
def test_direct_state_payloads_require_exact_runtime_types(field: str, state: JobState, model: str) -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    state_payloads: dict[str, Any] = {"blocker": None, "error": None, "ready_snapshot": None}
    state_payloads[field] = object()

    with pytest.raises(DrawingMachineError, match=field):
        if model == "record":
            JobRecord(
                schema_version=1,
                job_id="job-1",
                kind=JobKind.OFFLINE_WORKFLOW,
                name="fixture job",
                state=state,
                revision=7 if state is JobState.READY_TO_RUN else 1,
                request={},
                config=ConfigSnapshot.from_json(config_snapshot_json()),
                blocker=state_payloads["blocker"],
                error=state_payloads["error"],
                ready_snapshot=state_payloads["ready_snapshot"],
                created_at=now,
                updated_at=now,
            )
        else:
            JobTransition(
                job_id="job-1",
                expected_revision=6,
                result_state=state,
                request_id="req-1",
                event_type="job.transitioned",
                blocker=state_payloads["blocker"],
                error=state_payloads["error"],
                ready_snapshot=state_payloads["ready_snapshot"],
            )


def test_job_record_clones_validates_and_freezes_error_payload() -> None:
    details = {"nested": {"values": [1]}}
    source_error = ErrorPayload(
        code="FAILED",
        category=ErrorCategory.SERVICE,
        message="failed",
        retryable=False,
        details=details,
        request_id="req-1",
        job_id="job-1",
    )
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    job = JobRecord(
        schema_version=1,
        job_id="job-1",
        kind=JobKind.OFFLINE_WORKFLOW,
        name="fixture job",
        state=JobState.FAILED,
        revision=1,
        request={},
        config=ConfigSnapshot.from_json(config_snapshot_json()),
        blocker=None,
        error=source_error,
        ready_snapshot=None,
        created_at=now,
        updated_at=now,
    )

    details["nested"]["values"].append(2)
    assert job.error is not source_error
    assert job.error is not None
    with pytest.raises((AttributeError, TypeError)):
        job.error.details["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    assert job.error.details["nested"]["values"] == [1]  # type: ignore[index]

    encoded = job.to_json()
    encoded["error"]["details"]["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    assert job.error.details["nested"]["values"] == [1]  # type: ignore[index]


@pytest.mark.parametrize(
    "error",
    [
        ErrorPayload(1, ErrorCategory.SERVICE, "failed", False, {}, "req-1", "job-1"),  # type: ignore[arg-type]
        ErrorPayload("FAILED", "service", "failed", False, {}, "req-1", "job-1"),  # type: ignore[arg-type]
        ErrorPayload("FAILED", ErrorCategory.SERVICE, "failed", 1, {}, "req-1", "job-1"),  # type: ignore[arg-type]
        ErrorPayload("FAILED", ErrorCategory.SERVICE, "failed", False, object(), "req-1", "job-1"),  # type: ignore[arg-type]
    ],
)
def test_direct_job_record_validates_error_payload_fields(error: ErrorPayload) -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    with pytest.raises(DrawingMachineError, match="error"):
        JobRecord(
            schema_version=1,
            job_id="job-1",
            kind=JobKind.OFFLINE_WORKFLOW,
            name="fixture job",
            state=JobState.FAILED,
            revision=1,
            request={},
            config=ConfigSnapshot.from_json(config_snapshot_json()),
            blocker=None,
            error=error,
            ready_snapshot=None,
            created_at=now,
            updated_at=now,
        )


@pytest.mark.parametrize("job_id", [None, "other-job"])
@pytest.mark.parametrize("constructor", ["direct", "from_json"])
def test_failed_job_record_requires_explicit_matching_error_job_id(
    job_id: str | None,
    constructor: str,
) -> None:
    if constructor == "from_json":
        payload = job_payload_for_state(JobKind.OFFLINE_WORKFLOW, JobState.FAILED)
        payload["error"] = error_json(job_id=job_id)
        with pytest.raises(DrawingMachineError, match="job_id"):
            JobRecord.from_json(payload)
        return

    error = ErrorPayload(
        code="FAILED",
        category=ErrorCategory.SERVICE,
        message="failed",
        retryable=False,
        details={},
        request_id="req-1",
        job_id=job_id,
    )
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    with pytest.raises(DrawingMachineError, match="job_id"):
        JobRecord(
            1,
            "job-1",
            JobKind.OFFLINE_WORKFLOW,
            "fixture job",
            JobState.FAILED,
            1,
            {},
            ConfigSnapshot.from_json(config_snapshot_json()),
            None,
            error,
            None,
            now,
            now,
        )


@pytest.mark.parametrize(
    ("error_job_id", "transition_request_id", "error_request_id"),
    [
        (None, "req-1", "req-1"),
        ("other-job", "req-1", "req-1"),
        ("job-1", None, None),
        ("job-1", "req-1", None),
        ("job-1", "req-1", "other-request"),
    ],
)
@pytest.mark.parametrize("constructor", ["direct", "from_json"])
def test_failed_transition_requires_explicit_matching_error_bindings(
    error_job_id: str | None,
    transition_request_id: str | None,
    error_request_id: str | None,
    constructor: str,
) -> None:
    if constructor == "from_json":
        payload = transition_json()
        payload.update(
            {
                "result_state": "FAILED",
                "request_id": transition_request_id,
                "error": error_json(job_id=error_job_id, request_id=error_request_id),
            }
        )
        with pytest.raises(DrawingMachineError, match="error"):
            JobTransition.from_json(payload)
        return

    error = ErrorPayload(
        code="FAILED",
        category=ErrorCategory.SERVICE,
        message="failed",
        retryable=False,
        details={},
        request_id=error_request_id,
        job_id=error_job_id,
    )
    with pytest.raises(DrawingMachineError, match="error"):
        JobTransition(
            job_id="job-1",
            expected_revision=0,
            result_state=JobState.FAILED,
            request_id=transition_request_id,
            event_type="job.failed",
            error=error,
        )


def test_failed_transition_accepts_explicit_matching_error_bindings() -> None:
    payload = transition_json()
    payload.update({"result_state": "FAILED", "error": error_json()})

    transition = JobTransition.from_json(payload)

    assert transition.error is not None
    assert transition.error.job_id == transition.job_id
    assert transition.error.request_id == transition.request_id
    assert JobTransition.from_json(transition.to_json()) == transition


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (ArtifactRef.from_json, artifact_json()),
        (ConfigSnapshot.from_json, config_snapshot_json()),
        (
            JobBlocker.from_json,
            {"code": "BLOCKED", "category": "validation", "message": "blocked", "details": {}},
        ),
        (
            RequesterIdentity.from_json,
            {"requester_type": "LOCAL_PEER", "pid": 101, "uid": 1000, "gid": 1000},
        ),
        (ReadyToRunSnapshot.from_json, ready_snapshot_json()),
        (JobRecord.from_json, job_record_json()),
        (JobTransition.from_json, transition_json()),
    ],
)
def test_every_model_from_json_rejects_unknown_keys(factory: Any, payload: dict[str, Any]) -> None:
    payload["unexpected"] = True

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        factory(payload)


@pytest.mark.parametrize(
    ("factory", "payload", "missing"),
    [
        (ArtifactRef.from_json, artifact_json(), "role"),
        (ConfigSnapshot.from_json, config_snapshot_json(), "machine_profile"),
        (
            JobBlocker.from_json,
            {"code": "BLOCKED", "category": "validation", "message": "blocked", "details": {}},
            "code",
        ),
        (
            RequesterIdentity.from_json,
            {"requester_type": "LOCAL_PEER", "pid": 101, "uid": 1000, "gid": 1000},
            "pid",
        ),
        (ReadyToRunSnapshot.from_json, ready_snapshot_json(), "schema_version"),
        (JobRecord.from_json, job_record_json(), "job_id"),
        (JobTransition.from_json, transition_json(), "job_id"),
    ],
)
def test_every_model_from_json_rejects_missing_keys(
    factory: Any,
    payload: dict[str, Any],
    missing: str,
) -> None:
    del payload[missing]

    with pytest.raises(DrawingMachineError, match="missing keys"):
        factory(payload)


def test_nested_error_payload_rejects_unknown_keys() -> None:
    payload = job_record_json()
    payload["state"] = "FAILED"
    payload["error"] = {
        "code": "FAILED",
        "category": "service",
        "message": "failed",
        "retryable": False,
        "details": {},
        "request_id": None,
        "job_id": "job-1",
        "unexpected": True,
    }

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        JobRecord.from_json(payload)


def test_nested_error_payload_rejects_missing_keys() -> None:
    payload = job_record_json()
    payload["state"] = "FAILED"
    nested_error = error_json()
    del nested_error["retryable"]
    payload["error"] = nested_error

    with pytest.raises(DrawingMachineError, match="missing keys"):
        JobRecord.from_json(payload)
