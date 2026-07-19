from __future__ import annotations

import copy
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.providers import (
    MAX_PROVIDER_IDENTIFIER_BYTES,
    MAX_PROVIDER_PROMPT_BYTES,
    PreparedProviderRequest,
    ProviderPollResult,
    ProviderPollState,
    ProviderRecord,
    ProviderRegistry,
    ProviderRequestV1,
    ProviderResultV1,
    ProviderStatus,
    ProviderSubmission,
)


class FakeProvider:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def validate_config(self) -> None:
        return None

    def create_request(self, request: ProviderRequestV1) -> PreparedProviderRequest:
        raise NotImplementedError

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        raise NotImplementedError

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        raise NotImplementedError

    def retrieve(self, submission: ProviderSubmission) -> ProviderResultV1:
        raise NotImplementedError


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
        raise AssertionError("RAW-SECRET hostile string encode reached")

    def __eq__(self, other: object) -> bool:
        del other
        self.calls.append("eq")
        raise AssertionError("RAW-SECRET hostile string equality reached")

    def __hash__(self) -> int:
        self.calls.append("hash")
        raise AssertionError("RAW-SECRET hostile string hash reached")


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


def assert_fresh_sanitized_string_rejection(error: DrawingMachineError, value: LyingText) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert "RAW-SECRET" not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None
    assert value.calls == []


def error_payload() -> ErrorPayload:
    return ErrorPayload(
        code="PROVIDER_FAILED",
        category=ErrorCategory.PROVIDER,
        message="provider operation failed",
        retryable=False,
        details={"phase": "poll"},
    )


def test_registry_is_explicit_and_rejects_duplicate_provider_name() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider(name="local-comfyui")
    registry.register(provider)

    assert registry.get("local-comfyui") is provider
    assert registry.names == ("local-comfyui",)
    with pytest.raises(DrawingMachineError, match="already registered"):
        registry.register(FakeProvider(name="local-comfyui"))
    with pytest.raises(DrawingMachineError, match="not registered"):
        registry.get("missing")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema"),
        ({"request_id": "../escape"}, "request_id"),
        ({"input_path": "relative.png"}, "absolute path"),
        ({"input_path": "/tmp/../escape.png"}, "canonical absolute path"),
        ({"input_sha256": "A" * 64}, "lowercase SHA-256"),
        ({"prompt": ""}, "prompt"),
        ({"output_prefix": "../bad"}, "output_prefix"),
    ],
)
def test_provider_request_validates_schema_identity_path_digest_and_prompt(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": None,
        "output_prefix": "job-1",
    }
    values.update(changes)
    with pytest.raises(DrawingMachineError, match=message):
        ProviderRequestV1(**values)  # type: ignore[arg-type]


def test_provider_request_json_round_trip_rejects_unknown_and_missing_fields() -> None:
    request = ProviderRequestV1(
        schema_version=1,
        request_id="request-1",
        input_path="/tmp/input.png",
        input_sha256="a" * 64,
        prompt="draw clearly",
        output_prefix="job-1",
    )
    payload = request.to_json()
    assert ProviderRequestV1.from_json(payload) == request

    payload["surprise"] = True
    with pytest.raises(DrawingMachineError, match="unknown keys"):
        ProviderRequestV1.from_json(payload)
    payload = request.to_json()
    del payload["prompt"]
    with pytest.raises(DrawingMachineError, match="missing keys"):
        ProviderRequestV1.from_json(payload)


@pytest.mark.parametrize("from_json", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("a" * MAX_PROVIDER_PROMPT_BYTES, id="ascii-exact"),
        pytest.param(
            "é" * (MAX_PROVIDER_PROMPT_BYTES // len("é".encode())),
            id="multibyte-exact",
        ),
    ],
)
def test_provider_request_accepts_prompt_at_exact_utf8_byte_limit(prompt: str, from_json: bool) -> None:
    values: JsonObject = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": prompt,
        "output_prefix": "job-1",
    }
    request = ProviderRequestV1.from_json(values) if from_json else ProviderRequestV1(**values)  # type: ignore[arg-type]
    assert request.prompt == prompt
    assert len(prompt.encode("utf-8")) == MAX_PROVIDER_PROMPT_BYTES


@pytest.mark.parametrize("from_json", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("a" * (MAX_PROVIDER_PROMPT_BYTES + 1), id="ascii-over"),
        pytest.param(
            "é" * (MAX_PROVIDER_PROMPT_BYTES // len("é".encode())) + "a",
            id="multibyte-over",
        ),
    ],
)
def test_provider_request_rejects_prompt_over_utf8_byte_limit(prompt: str, from_json: bool) -> None:
    values: JsonObject = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": prompt,
        "output_prefix": "job-1",
    }
    with pytest.raises(DrawingMachineError, match="UTF-8 bytes"):
        if from_json:
            ProviderRequestV1.from_json(values)
        else:
            ProviderRequestV1(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("from_json", [False, True])
def test_provider_request_identifier_exact_limit_passes_and_one_over_fails(from_json: bool) -> None:
    boundary = "a" * MAX_PROVIDER_IDENTIFIER_BYTES
    values: JsonObject = {
        "schema_version": 1,
        "request_id": boundary,
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": None,
        "output_prefix": boundary,
    }
    request = ProviderRequestV1.from_json(values) if from_json else ProviderRequestV1(**values)  # type: ignore[arg-type]
    assert request.request_id == boundary
    assert request.output_prefix == boundary

    for field in ("request_id", "output_prefix"):
        invalid = dict(values)
        invalid[field] = boundary + "a"
        with pytest.raises(DrawingMachineError, match="255 ASCII bytes"):
            if from_json:
                ProviderRequestV1.from_json(invalid)
            else:
                ProviderRequestV1(**invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["request_id", "prompt", "output_prefix"])
def test_provider_request_invalid_unicode_has_fresh_sanitized_exception_graph(field: str) -> None:
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": "draw",
        "output_prefix": "job-1",
    }
    values[field] = "\ud800RAW-SECRET"

    with pytest.raises(DrawingMachineError) as caught:
        ProviderRequestV1(**values)  # type: ignore[arg-type]
    rendered = "".join(traceback.format_exception(caught.value))
    assert "RAW-SECRET" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("from_json", [False, True])
@pytest.mark.parametrize(
    ("field", "underlying"),
    [
        pytest.param("request_id", "a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1), id="request-id"),
        pytest.param("input_path", "/tmp/input.png", id="input-path"),
        pytest.param("input_sha256", "a" * 64, id="digest"),
        pytest.param("prompt", "a" * (MAX_PROVIDER_PROMPT_BYTES + 1), id="prompt"),
        pytest.param("output_prefix", "a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1), id="output-prefix"),
    ],
)
def test_provider_request_rejects_string_subclasses_before_hostile_methods(
    field: str, underlying: str, from_json: bool
) -> None:
    hostile = LyingText(underlying)
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": "draw",
        "output_prefix": "job-1",
    }
    values[field] = hostile

    with pytest.raises(DrawingMachineError) as caught:
        if from_json:
            ProviderRequestV1.from_json(values)
        else:
            ProviderRequestV1(**values)  # type: ignore[arg-type]
    assert_fresh_sanitized_string_rejection(caught.value, hostile)


def test_provider_json_object_rejects_string_subclass_key_before_hostile_methods() -> None:
    hostile = LyingText("RAW-SECRET-key")
    payload = HostileItemsMapping(((hostile, 1),))

    with pytest.raises(DrawingMachineError) as caught:
        ProviderRequestV1.from_json(payload)
    assert_fresh_sanitized_string_rejection(caught.value, hostile)


@pytest.mark.parametrize("as_key", [False, True])
def test_prepared_workflow_rejects_string_subclasses_before_freezing(as_key: bool) -> None:
    hostile = LyingText("a" * (MAX_PROVIDER_PROMPT_BYTES + 1))
    workflow: object = HostileItemsMapping(((hostile, 1),)) if as_key else {"prompt": hostile}

    with pytest.raises(DrawingMachineError) as caught:
        PreparedProviderRequest(
            1,
            "request-1",
            workflow,  # type: ignore[arg-type]
            "input.png",
            ProviderRecord("provider.request", "request.json", "application/json", b"{}"),
        )
    assert_fresh_sanitized_string_rejection(caught.value, hostile)


def test_provider_dtos_retain_only_exact_builtin_strings() -> None:
    request = ProviderRequestV1(1, "request-1", "/tmp/input.png", "a" * 64, "draw", "job-1")
    record = ProviderRecord("provider.request", "request.json", "application/json", b"{}")
    prepared = PreparedProviderRequest(1, "request-1", {"prompt": "draw"}, "input.png", record)
    submission = ProviderSubmission(1, "request-1", "external-job")
    failed = ProviderPollResult(1, ProviderPollState.FAILED, None, error_payload())
    assert failed.error is not None

    assert all(
        type(value) is str
        for value in (
            request.request_id,
            request.input_path,
            request.input_sha256,
            request.prompt,
            request.output_prefix,
            record.role,
            record.relative_path,
            record.media_type,
            prepared.request_id,
            prepared.upload_name,
            prepared.workflow["prompt"],
            submission.request_id,
            submission.provider_job_id,
            failed.error.code,
            failed.error.message,
            failed.error.details["phase"],
        )
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda value: ProviderRecord(value, "request.json", "application/json", b"{}"),
        lambda value: ProviderRecord("provider.request", value, "application/json", b"{}"),
        lambda value: ProviderRecord("provider.request", "request.json", value, b"{}"),
        lambda value: PreparedProviderRequest(
            1,
            value,
            {},
            "input.png",
            ProviderRecord("provider.request", "request.json", "application/json", b"{}"),
        ),
        lambda value: PreparedProviderRequest(
            1,
            "request-1",
            {},
            value,
            ProviderRecord("provider.request", "request.json", "application/json", b"{}"),
        ),
        lambda value: ProviderSubmission(1, value, "external-job"),
        lambda value: ProviderSubmission(1, "request-1", value),
        lambda value: ProviderPollResult(
            1,
            ProviderPollState.FAILED,
            None,
            ErrorPayload(value, ErrorCategory.PROVIDER, "failed", False, {}),
        ),
        lambda value: ProviderPollResult(
            1,
            ProviderPollState.FAILED,
            None,
            ErrorPayload("PROVIDER_FAILED", ErrorCategory.PROVIDER, value, False, {}),
        ),
        lambda value: ProviderResultV1(
            1,
            ProviderStatus.FAILED,
            None,
            (),
            ErrorPayload("PROVIDER_FAILED", ErrorCategory.PROVIDER, "failed", False, {}, request_id=value),
        ),
        lambda value: ProviderRegistry().register(FakeProvider(value)),
    ],
)
def test_public_provider_dtos_reject_string_subclasses_before_hostile_methods(
    build: Callable[[object], object],
) -> None:
    hostile = LyingText("a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1))

    with pytest.raises(DrawingMachineError) as caught:
        build(hostile)
    assert_fresh_sanitized_string_rejection(caught.value, hostile)


def test_all_provider_identifier_consumers_share_the_finite_identifier_limit() -> None:
    oversized = "a" * (MAX_PROVIDER_IDENTIFIER_BYTES + 1)
    record = ProviderRecord("provider.request", "request.json", "application/json", b"{}")
    with pytest.raises(DrawingMachineError, match="255 ASCII bytes"):
        PreparedProviderRequest(1, oversized, {}, "input.png", record)
    with pytest.raises(DrawingMachineError, match="255 ASCII bytes"):
        ProviderSubmission(1, oversized, "external-job")
    with pytest.raises(DrawingMachineError, match="255 ASCII bytes"):
        ProviderRegistry().register(FakeProvider(oversized))

    for field in ("code", "request_id", "job_id"):
        values: dict[str, object] = {
            "code": "PROVIDER_FAILED",
            "category": ErrorCategory.PROVIDER,
            "message": "provider operation failed",
            "retryable": False,
            "details": {},
            "request_id": None,
            "job_id": None,
        }
        values[field] = oversized
        error = ErrorPayload(**values)  # type: ignore[arg-type]
        with pytest.raises(DrawingMachineError, match="255 ASCII bytes"):
            ProviderPollResult(1, ProviderPollState.FAILED, None, error)


@pytest.mark.parametrize("field", ["request_id", "prompt", "output_prefix"])
def test_oversize_provider_request_fields_reject_before_utf8_allocation(field: str) -> None:
    class EncodeMustNotRun(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("oversize value reached UTF-8 allocation")

    maximum = MAX_PROVIDER_PROMPT_BYTES if field == "prompt" else MAX_PROVIDER_IDENTIFIER_BYTES
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-1",
        "input_path": "/tmp/input.png",
        "input_sha256": "a" * 64,
        "prompt": "draw",
        "output_prefix": "job-1",
    }
    values[field] = EncodeMustNotRun("a" * (maximum + 1))

    with pytest.raises(DrawingMachineError):
        ProviderRequestV1(**values)  # type: ignore[arg-type]


def test_prepared_workflow_is_deeply_immutable_and_has_no_input_alias() -> None:
    workflow: JsonObject = {"27": {"inputs": {"prompt": "template"}}, "items": [1, {"x": True}]}
    prepared = PreparedProviderRequest(
        schema_version=1,
        request_id="request-1",
        workflow=workflow,
        upload_name="input image.png",
        record=ProviderRecord(
            role="provider.request",
            relative_path="qwen_image_edit_request.json",
            media_type="application/json",
            content=b"{}",
        ),
    )
    cast(dict[str, object], workflow["27"])["inputs"] = {"prompt": "changed"}
    cast(list[object], workflow["items"])[1] = {"x": False}

    assert cast(dict[str, object], prepared.workflow["27"])["inputs"] == {"prompt": "template"}
    assert prepared.workflow["items"] == [1, {"x": True}]
    with pytest.raises(TypeError):
        cast(dict[str, object], prepared.workflow)["new"] = 1
    assert copy.deepcopy(prepared.workflow) is prepared.workflow


def test_prepared_workflow_rejects_1500_level_json_without_raw_recursion() -> None:
    nested: object = 0
    for _ in range(1500):
        nested = [nested]

    with pytest.raises(DrawingMachineError) as caught:
        PreparedProviderRequest(
            1,
            "request-1",
            {"nested": nested},
            "input.png",
            ProviderRecord("provider.request", "request.json", "application/json", b"{}"),
        )
    assert caught.value.payload.code == "PROVIDER_CONTRACT_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "record",
    [
        ProviderRecord("provider.request", "request.json", "application/json", b"{}"),
        ProviderRecord("provider.prompt", "prompt.txt", "text/plain; charset=utf-8", b"prompt"),
    ],
)
def test_provider_record_is_frozen_and_content_is_bytes(record: ProviderRecord) -> None:
    with pytest.raises(FrozenInstanceError):
        record.role = "changed"  # type: ignore[misc]
    with pytest.raises(DrawingMachineError, match="content must be bytes"):
        ProviderRecord(record.role, record.relative_path, record.media_type, bytearray(record.content))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("bad role", "a.json", "application/json", b"{}"), "role"),
        (("provider.request", "../a.json", "application/json", b"{}"), "relative_path"),
        (("provider.request", "a.json", "bad media", b"{}"), "media_type"),
    ],
)
def test_provider_record_validates_metadata(values: tuple[object, ...], message: str) -> None:
    with pytest.raises(DrawingMachineError, match=message):
        ProviderRecord(*values)  # type: ignore[arg-type]


def test_poll_result_enforces_state_retry_and_error_relationships() -> None:
    pending = ProviderPollResult(1, ProviderPollState.PENDING, 0.25, None)
    assert pending.retry_after_seconds == 0.25

    invalid = [
        (ProviderPollState.PENDING, None, None),
        (ProviderPollState.PENDING, float("inf"), None),
        (ProviderPollState.PENDING, 10_001.0, None),
        (ProviderPollState.SUCCEEDED, 1.0, None),
        (ProviderPollState.SUCCEEDED, None, error_payload()),
        (ProviderPollState.FAILED, None, None),
        (ProviderPollState.FAILED, 1.0, error_payload()),
    ]
    for state, retry_after, error in invalid:
        with pytest.raises(DrawingMachineError):
            ProviderPollResult(1, state, retry_after, error)


def test_result_invariants_fail_closed_and_copy_records_exactly() -> None:
    record = ProviderRecord("provider.response", "response.json", "application/json", b"{}")
    records = [record]
    result = ProviderResultV1(1, ProviderStatus.SUCCEEDED, b"png", cast(tuple[ProviderRecord, ...], records), None)
    records.clear()
    assert result.records == (record,)

    invalid = [
        (ProviderStatus.SUCCEEDED, None, (record,), None),
        (ProviderStatus.SUCCEEDED, b"png", (record,), error_payload()),
        (ProviderStatus.FAILED, b"png", (record,), error_payload()),
        (ProviderStatus.FAILED, None, (record,), None),
        (ProviderStatus.BLOCKED, b"png", (record,), error_payload()),
        (ProviderStatus.BLOCKED, None, (record,), None),
    ]
    for status, image, result_records, error in invalid:
        with pytest.raises(DrawingMachineError):
            ProviderResultV1(1, status, image, result_records, error)


def test_submission_validates_request_and_provider_job_identity() -> None:
    with pytest.raises(DrawingMachineError, match="request_id"):
        ProviderSubmission(1, "../request", "job-1")
    with pytest.raises(DrawingMachineError, match="provider_job_id"):
        ProviderSubmission(1, "request-1", "")
