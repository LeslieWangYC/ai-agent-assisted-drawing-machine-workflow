from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TypeAlias, cast

from drawingmachine.adapters.providers.local_comfyui import (
    HttpTransport,
    LocalComfyUIConfig,
    LocalComfyUIProvider,
)
from drawingmachine.domain.workflow import (
    RouteContext,
    decide_route,
    normalize_image,
    prepare_direct_image,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.providers import ProviderPollState, ProviderRequestV1, ProviderStatus
from drawingmachine.ports.workers import WorkerOutcomeV1, WorkerTaskV1

from . import TaskOutput, open_bounded_image, read_inputs, succeeded, validate_bounded_image, write_bundle
from ._payload_validation import validate_image_payload, validate_provider_payload

ProviderTransportFactory: TypeAlias = Callable[[], HttpTransport]


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def run_image_prepare(task: WorkerTaskV1) -> WorkerOutcomeV1:
    payload = validate_image_payload(task)
    normalization_config = replace(
        payload.normalization_config,
        input_path=payload.source_name,
        output_path="processed_image.png",
    )
    if payload.mode == "normalize_existing":
        content = read_inputs(task, frozenset({"processed_image_raw"}))["processed_image_raw"]
        normalized = normalize_image(open_bounded_image(task, content), normalization_config)
        artifacts = write_bundle(
            task,
            (
                TaskOutput("processed_image", "processed_image.png", "image/png", normalized.png_bytes),
                TaskOutput(
                    "normalization_report",
                    "normalization_report.json",
                    "application/json",
                    _json_bytes(normalized.report),
                ),
            ),
        )
        return succeeded(task, artifacts)

    content = read_inputs(task, frozenset({"input_image"}))["input_image"]
    image = open_bounded_image(task, content)
    assessment = payload.semantic_assessment
    if assessment is None:
        assessment = {
            "semantic_style_score": 0.0,
            "semantic_score_source": "router_multimodal_model",
            "semantic_rationale": [],
            "semantic_blockers": [],
        }
    prompt = "preserve original and do not simplify" if payload.route_mode == "direct" else ""
    route = decide_route(
        image,
        assessment,
        context=RouteContext(job_name=task.job_id, input_image=payload.source_name, prompt=prompt),
    )
    if payload.route_mode == "auto":
        artifacts = write_bundle(
            task,
            (TaskOutput("route_decision", "route_decision.json", "application/json", _json_bytes(route.to_json())),),
        )
        return succeeded(task, artifacts)
    if payload.direct_config is None:
        raise RuntimeError("validated direct image payload has no direct config")
    direct_config = replace(
        payload.direct_config,
        input_path=payload.source_name,
        output_path="processed_image_raw.png",
    )
    prepared = prepare_direct_image(image, direct_config)
    normalized = normalize_image(
        open_bounded_image(task, prepared.png_bytes),
        replace(normalization_config, input_path="processed_image_raw.png"),
    )
    artifacts = write_bundle(
        task,
        (
            TaskOutput("route_decision", "route_decision.json", "application/json", _json_bytes(route.to_json())),
            TaskOutput("processed_image_raw", "processed_image_raw.png", "image/png", prepared.png_bytes),
            TaskOutput("direct_report", "direct_report.json", "application/json", _json_bytes(prepared.report)),
            TaskOutput("processed_image", "processed_image.png", "image/png", normalized.png_bytes),
            TaskOutput(
                "normalization_report",
                "normalization_report.json",
                "application/json",
                _json_bytes(normalized.report),
            ),
        ),
    )
    return succeeded(task, artifacts)


def run_local_comfyui(
    task: WorkerTaskV1,
    *,
    transport_factory: ProviderTransportFactory | None = None,
) -> WorkerOutcomeV1:
    payload = validate_provider_payload(task)
    input_artifact = task.input_artifacts[0] if len(task.input_artifacts) == 1 else None
    read_inputs(task, frozenset({"input_image"}))
    if input_artifact is None:
        raise RuntimeError("provider task requires one input")
    config = LocalComfyUIConfig.from_profile(
        payload.profile,
        profile_path=payload.config_path,
    )
    transport = None if transport_factory is None else transport_factory()
    provider = LocalComfyUIProvider(config, transport=transport)
    provider.validate_config()
    request = ProviderRequestV1(
        1,
        task.attempt_id,
        input_artifact.absolute_path,
        input_artifact.sha256,
        payload.prompt,
        task.job_id,
    )
    prepared = provider.create_request(request)
    submission = provider.submit(prepared)
    poll = provider.poll(submission)
    while poll.state is ProviderPollState.PENDING:
        time.sleep(cast(float, poll.retry_after_seconds))
        poll = provider.poll(submission)
    result = provider.retrieve(submission)
    if result.status is not ProviderStatus.SUCCEEDED or result.processed_image is None:
        if result.error is not None:
            raise DrawingMachineError(result.error)
        raise RuntimeError("provider completed without an image")
    validate_bounded_image(task, result.processed_image)
    records = {record.role: record for record in result.records}
    outputs = (
        TaskOutput(
            "provider_request", "provider_request.json", "application/json", records["provider.request"].content
        ),
        TaskOutput(
            "provider_response", "provider_response.json", "application/json", records["provider.response"].content
        ),
        TaskOutput(
            "provider_handoff", "provider_handoff.json", "application/json", records["provider.handoff"].content
        ),
        TaskOutput("processed_image_raw", "processed_image_raw.png", "image/png", result.processed_image),
    )
    return succeeded(task, write_bundle(task, outputs), {"provider_record_count": len(result.records)})


__all__ = ["ProviderTransportFactory", "run_image_prepare", "run_local_comfyui"]
