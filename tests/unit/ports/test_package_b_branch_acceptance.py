from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports import providers, workers
from drawingmachine.ports.artifacts import WorkerArtifact
from drawingmachine.ports.providers import (
    PreparedProviderRequest,
    ProviderPollResult,
    ProviderPollState,
    ProviderRecord,
    ProviderResultV1,
    ProviderStatus,
    ProviderSubmission,
)
from drawingmachine.ports.workers import WorkerInputArtifact, WorkerKind, WorkerOutcomeV1, WorkerStatus, WorkerTaskV1


@pytest.mark.parametrize(
    ("function", "value"),
    [
        (workers._object, []),
        (workers._string, 1),
        (workers._boolean, 1),
        (workers._freeze_object, []),
        (workers._config_digests, []),
    ],
)
def test_worker_private_scalar_rejections(function: Any, value: object) -> None:
    with pytest.raises(DrawingMachineError):
        function(value, "field") if function not in {workers._config_digests} else function(value)


def test_worker_private_string_paths_and_objects_cover_defensive_branches() -> None:
    with pytest.raises(DrawingMachineError):
        workers._object({1: True}, "object")
    with pytest.raises(DrawingMachineError):
        workers._string("\ud800", "text")
    for value in ("", "/absolute", "../up", "a\\b", "a/./b", "a\x00b"):
        with pytest.raises(DrawingMachineError):
            workers._relative_path(value, "path")
    with pytest.raises(TypeError):
        workers._FrozenJsonObject(object(), ())
    frozen = workers._FrozenJsonObject._create((("a", 1),))
    with pytest.raises(TypeError):
        del frozen.a
    with pytest.raises(KeyError):
        frozen["missing"]
    array = workers._FrozenJsonArray._create((1, 2))
    with pytest.raises(TypeError):
        workers._FrozenJsonArray(object(), ())
    assert array == [1, 2]
    assert array != "12"
    with pytest.raises(TypeError):
        del array.value
    assert copy.copy(array) is array and copy.deepcopy(array) is array
    with pytest.raises(TypeError):
        workers._FrozenStringMap(object(), ())
    string_map = workers._FrozenStringMap._create((("a", "b"),))
    with pytest.raises(TypeError):
        del string_map.value
    with pytest.raises(KeyError):
        string_map["missing"]
    assert copy.copy(string_map) is string_map and copy.deepcopy(string_map) is string_map


def test_worker_freezing_recursion_keys_values_and_empty_config() -> None:
    recursive_list: list[object] = []
    recursive_list.append(recursive_list)
    recursive_dict: dict[str, object] = {}
    recursive_dict["self"] = recursive_dict
    for value in (recursive_list, recursive_dict, {1: "bad"}, {"bad": object()}):
        with pytest.raises(DrawingMachineError):
            workers._freeze_json(value, "value", set())
    with pytest.raises(DrawingMachineError):
        workers._config_digests({})
    assert workers._plain_json(workers._FrozenJsonArray._create((1,))) == [1]
    assert workers._plain_json(workers._FrozenJsonObject._create((("x", 1),))) == {"x": 1}
    assert workers._reject_objects_in_payload_leaf({}, "payload") is None
    assert workers._reject_objects_in_payload_leaf([{}, []], "payload") is None
    with pytest.raises(DrawingMachineError):
        workers._reject_objects_in_payload_leaf({"unknown": True}, "payload")
    with pytest.raises(DrawingMachineError):
        workers._require_payload_fields({"a": 1}, expected=frozenset({"b"}), context="payload")


def test_worker_payload_dispatch_and_error_category_cover_fail_closed_branches() -> None:
    invalid_category = ErrorPayload("FAILED", cast(Any, "internal"), "failed", False, {})
    with pytest.raises(DrawingMachineError):
        workers._error_payload(invalid_category, "error")
    with pytest.raises(DrawingMachineError):
        workers._validate_worker_payload(WorkerKind.IMAGE_PREPARE, {"mode": "unsupported"})
    with pytest.raises(DrawingMachineError):
        workers._validate_worker_payload(WorkerKind.PATHS_PLAN, {})
    with pytest.raises(DrawingMachineError):
        workers._validate_worker_payload(WorkerKind.TEST_ECHO, {"mode": "echo", "unknown": 1})
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", cast(Any, "SUCCEEDED"), (), {}, None)


def _worker_task(tmp_path: Any) -> WorkerTaskV1:
    source = tmp_path / "input"
    source.write_bytes(b"x")
    staging = tmp_path / "staging"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        "task",
        "job",
        0,
        "attempt",
        WorkerKind.TEST_ECHO,
        (WorkerInputArtifact("input", str(source), "a" * 64, 1, "application/octet-stream"),),
        {"application": "b" * 64},
        str(staging),
        {"mode": "echo", "nested": []},
    )


def test_worker_task_and_outcome_direct_constructor_defensive_branches(tmp_path: Any) -> None:
    task = _worker_task(tmp_path)
    values = task.to_json()
    values["input_artifacts"] = {}
    with pytest.raises(DrawingMachineError):
        WorkerTaskV1.from_json(values)
    with pytest.raises(DrawingMachineError):
        WorkerTaskV1(
            1,
            "task2",
            "job",
            0,
            "attempt2",
            cast(Any, "test.echo"),
            (),
            {"a": "b" * 64},
            str(tmp_path),
            {"mode": "echo", "nested": []},
        )
    with pytest.raises(DrawingMachineError):
        WorkerTaskV1(
            1,
            "task2",
            "job",
            0,
            "attempt2",
            WorkerKind.TEST_ECHO,
            cast(Any, (object(),)),
            {"a": "b" * 64},
            str(tmp_path),
            {"mode": "echo", "nested": []},
        )
    artifact = WorkerArtifact("result", "result.json", "c" * 64, 1, "application/json")
    error = ErrorPayload("FAILED", ErrorCategory.INTERNAL, "failed", False, {}, job_id="job")
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.FAILED, (artifact,), {}, error)
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.SUCCEEDED, (), {}, error)
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.FAILED, (), {}, None)
    wrong = ErrorPayload("FAILED", ErrorCategory.INTERNAL, "failed", False, {}, job_id="other")
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.FAILED, (), {}, wrong)
    duplicate_role = WorkerArtifact("result", "other.json", "d" * 64, 1, "application/json")
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.BLOCKED, (artifact, duplicate_role), {}, error)
    duplicate_path = WorkerArtifact("other", "result.json", "d" * 64, 1, "application/json")
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.BLOCKED, (artifact, duplicate_path), {}, error)
    payload = WorkerOutcomeV1(1, "task", "job", 0, "attempt", WorkerStatus.SUCCEEDED, (), {}, None).to_json()
    payload["artifacts"] = {}
    with pytest.raises(DrawingMachineError):
        WorkerOutcomeV1.from_json(payload)


def test_provider_private_frozen_and_json_helpers_cover_defensive_branches() -> None:
    with pytest.raises(TypeError):
        providers._FrozenJsonObject(object(), ())
    frozen = providers._FrozenJsonObject.create((("a", 1),))
    with pytest.raises(TypeError):
        del frozen.a
    with pytest.raises(KeyError):
        frozen["missing"]
    array = providers._FrozenJsonArray.create((1, 2))
    with pytest.raises(TypeError):
        providers._FrozenJsonArray(object(), ())
    assert array == [1, 2]
    assert array != "12"
    with pytest.raises(TypeError):
        del array.a
    assert copy.copy(array) is array and copy.deepcopy(array) is array
    recursive: list[object] = []
    recursive.append(recursive)
    mapping: dict[str, object] = {}
    mapping["self"] = mapping
    for value in (recursive, mapping, {1: True}, object(), float("nan")):
        with pytest.raises(DrawingMachineError):
            providers._freeze_json(value, "value")
    assert providers._thaw_json(frozen) == {"a": 1}
    assert providers._thaw_json(array) == [1, 2]
    assert providers._thaw_json(1) == 1
    assert providers._freeze_error(None, "error") is None
    assert providers._error_to_json(None) is None
    with pytest.raises(DrawingMachineError):
        providers._freeze_error(cast(Any, object()), "error")
    invalid_category = ErrorPayload("E", cast(Any, "provider"), "error", False, {})
    with pytest.raises(DrawingMachineError):
        providers._freeze_error(invalid_category, "error")
    invalid_retry = ErrorPayload("E", ErrorCategory.PROVIDER, "error", cast(Any, 1), {})
    with pytest.raises(DrawingMachineError):
        providers._freeze_error(invalid_retry, "error")
    invalid_details = ErrorPayload("E", ErrorCategory.PROVIDER, "error", False, cast(Any, []))
    with pytest.raises(DrawingMachineError):
        providers._freeze_error(invalid_details, "error")


def test_provider_dto_defensive_branches() -> None:
    record = ProviderRecord("provider.request", "request.json", "application/json", b"{}")
    for values in (
        ("bad role!", "request.json", "application/json", b"{}"),
        ("provider.request", "../request.json", "application/json", b"{}"),
        ("provider.request", "request.json", "bad", b"{}"),
        ("provider.request", "request.json", "application/json", "bad"),
    ):
        with pytest.raises(DrawingMachineError):
            ProviderRecord(*cast(Any, values))
    with pytest.raises(DrawingMachineError):
        PreparedProviderRequest(1, "request", cast(Any, []), "input.png", record)
    with pytest.raises(DrawingMachineError):
        PreparedProviderRequest(1, "request", {}, "../input.png", record)
    with pytest.raises(DrawingMachineError):
        PreparedProviderRequest(1, "request", {}, "input.png", cast(Any, object()))
    with pytest.raises(DrawingMachineError):
        ProviderSubmission(1, "request", "x" * 513)
    with pytest.raises(DrawingMachineError):
        ProviderPollResult(1, cast(Any, "PENDING"), 1.0, None)
    for retry in (None, True, 0.0, float("inf"), 10001.0):
        with pytest.raises(DrawingMachineError):
            ProviderPollResult(1, ProviderPollState.PENDING, cast(Any, retry), None)
    with pytest.raises(DrawingMachineError):
        ProviderPollResult(1, ProviderPollState.PENDING, 1.0, ErrorPayload("E", ErrorCategory.PROVIDER, "e", False, {}))
    with pytest.raises(DrawingMachineError):
        ProviderPollResult(1, ProviderPollState.SUCCEEDED, 1.0, None)
    with pytest.raises(DrawingMachineError):
        ProviderPollResult(1, ProviderPollState.FAILED, None, None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(1, cast(Any, "SUCCEEDED"), None, (), None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(1, ProviderStatus.SUCCEEDED, cast(Any, "bad"), (), None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(1, ProviderStatus.SUCCEEDED, b"x", cast(Any, None), None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(1, ProviderStatus.SUCCEEDED, b"x", cast(Any, (object(),)), None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(1, ProviderStatus.SUCCEEDED, None, (), None)
    with pytest.raises(DrawingMachineError):
        ProviderResultV1(
            1,
            ProviderStatus.FAILED,
            b"x",
            (),
            ErrorPayload("E", ErrorCategory.PROVIDER, "e", False, {}),
        )
    failed = ProviderResultV1(
        1,
        ProviderStatus.FAILED,
        None,
        (),
        ErrorPayload("E", ErrorCategory.PROVIDER, "e", False, {}),
    )
    assert failed.status is ProviderStatus.FAILED
