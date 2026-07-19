from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.domain.jobs.artifact_contracts import (
    ArtifactContractContext,
    validate_gcode_safety_bundle,
    validate_json_artifact,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject

GOLDEN = Path("tests/fixtures/package_b/golden/expected")
SHA = "a" * 64


def context() -> ArtifactContractContext:
    return ArtifactContractContext(
        job_id="package-b-golden",
        input_role="input_image",
        input_name="<FIXTURE_ROOT>/outline.png",
        input_sha256=SHA,
        input_size_bytes=3,
        output_sha256_by_role={"processed_image_raw": "b" * 64},
    )


def golden(name: str) -> JsonObject:
    value = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def set_path(document: JsonObject, path: tuple[str, ...], value: object) -> JsonObject:
    selected = copy.deepcopy(document)
    current: dict[str, Any] = selected
    for component in path[:-1]:
        current = cast(dict[str, Any], current[component])
    current[path[-1]] = value
    return selected


def provider_request() -> JsonObject:
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
            "value": "/tmp/input.png",
            "copied_input": "/tmp/input.png",
            "name": "input.png",
            "sha256": SHA,
            "size_bytes": 3,
            "mime_type": "image/png",
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
        "output_prefix": "package-b-golden",
        "dry_run": False,
        "execute": True,
        "timeout_s": 1.0,
    }


def provider_response() -> JsonObject:
    return {
        "schema": "local_comfyui_qwen_image_edit_response_record_v1",
        "created_at": "2026-07-11T00:00:00+00:00",
        "status": "PROCESSED_IMAGE_CREATED",
        "system_stats": {},
        "upload": {"name": "uploaded.png"},
        "prompt_id": "prompt-1",
        "history": {},
        "outputs": [{"filename": "processed.png", "subfolder": "", "type": "output"}],
        "processed_image": "processed.png",
        "free_requested": False,
    }


def provider_handoff() -> JsonObject:
    request = provider_request()
    return {
        "schema": "cnc_drawable_workflow_handoff_v1",
        "status": "PROCESSED_IMAGE_CREATED",
        "created_at": "2026-07-11T00:00:00+00:00",
        "job_name": "package-b-golden",
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
            "processed_image_sha256": "b" * 64,
        },
        "dry_run": False,
        "execute": True,
        "next_allowed_stage": "review_processed_image",
        "review_gate": {"required": True, "status": "pending"},
    }


def direct_report() -> JsonObject:
    return {
        "schema": "direct_processed_image_report_v1",
        "created_at": "2026-07-11T00:00:00+00:00",
        "input": "input.png",
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
        "foreground_ratio": 0.017,
        "content_bbox_px": [16, 16, 143, 143],
        "hardware_touched": False,
    }


def assert_invalid(role: str, document: object) -> None:
    with pytest.raises(DrawingMachineError) as captured:
        validate_json_artifact(role, document, context())
    assert captured.value.payload.code == "WORKER_ARTIFACT_CONTENT_INVALID"


@pytest.mark.parametrize("document", [[], {1: "invalid"}])
def test_artifact_document_requires_string_keyed_object(document: object) -> None:
    assert_invalid("route_decision", document)


def test_unknown_json_role_fails_closed() -> None:
    assert_invalid("unknown_role", {})


def test_gcode_bundle_requires_exact_roles_and_exact_documents() -> None:
    documents = {
        "gcode_static": golden("gcode_static.json"),
        "send_plan": golden("send_plan.json"),
        "readiness": golden("readiness.json"),
    }
    with pytest.raises(DrawingMachineError):
        validate_gcode_safety_bundle({"gcode_static": documents["gcode_static"]}, documents, job_id="job-1")
    changed = copy.deepcopy(documents)
    changed["readiness"]["allow_stream"] = False
    with pytest.raises(DrawingMachineError):
        validate_gcode_safety_bundle(changed, documents, job_id="job-1")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("job_name",), "wrong-job"),
        (("direct_route_allowed",), False),
        (("hardware_touched",), True),
        (("rationale",), "not-an-array"),
        (("rationale",), [1]),
        (("rationale",), [""]),
        (("route",), "NOT_A_ROUTE"),
    ],
)
def test_route_contract_rejects_identity_authority_and_collection_mutations(
    path: tuple[str, ...], value: object
) -> None:
    assert_invalid("route_decision", set_path(golden("route.json"), path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema",), "wrong"),
        (("size_px",), [160]),
        (("size_px",), [True, 160]),
        (("size_px",), ["160", 160]),
        (("size_px",), [float("inf"), 160]),
    ],
)
def test_direct_report_rejects_discriminator_and_numeric_shape_mutations(path: tuple[str, ...], value: object) -> None:
    assert_invalid("direct_report", set_path(direct_report(), path, value))


def test_normalization_report_rejects_discriminator_mutation() -> None:
    assert_invalid(
        "normalization_report",
        set_path(golden("normalize_report.json"), ("hardware_touched",), True),
    )


@pytest.mark.parametrize(
    ("document_factory", "path", "value"),
    [
        (provider_request, ("execute",), False),
        (provider_request, ("source_image", "sha256"), "c" * 64),
        (provider_request, ("prompt", "sha256"), "not-a-digest"),
        (provider_response, ("status",), "FAILED"),
        (provider_response, ("upload",), {"unexpected": True}),
        (provider_response, ("upload",), {"name": "x", "subfolder": 1, "type": "output"}),
        (provider_response, ("outputs",), [{"filename": "x", "subfolder": 1, "type": "output"}]),
        (provider_handoff, ("job_name",), "wrong-job"),
        (provider_handoff, ("source_image", "size_bytes"), 4),
        (provider_handoff, ("artifacts", "processed_image_sha256"), "c" * 64),
        (provider_handoff, ("review_gate", "status"), "approved"),
    ],
)
def test_provider_contracts_reject_cross_binding_and_nested_schema_mutations(
    document_factory: Callable[[], JsonObject],
    path: tuple[str, ...],
    value: object,
) -> None:
    document = set_path(document_factory(), path, value)
    role = {
        provider_request: "provider_request",
        provider_response: "provider_response",
        provider_handoff: "provider_handoff",
    }[document_factory]
    assert_invalid(role, document)


@pytest.mark.parametrize(
    ("role", "document", "path", "value"),
    [
        ("complexity_report", golden("complexity.json"), ("hardware_touched",), True),
        ("selected_path_plan", golden("selected_path_plan.json"), ("path_mode",), "preview"),
        ("selected_path_plan", golden("selected_path_plan.json"), ("execution", "allow_run"), True),
        ("send_plan", golden("send_plan.json"), ("raw_line_count",), True),
        ("send_plan", golden("send_plan.json"), ("raw_line_count",), "467"),
        ("send_plan", golden("send_plan.json"), ("raw_line_count",), -1),
        ("send_plan", golden("send_plan.json"), ("streaming_mode",), "fast"),
        ("readiness", golden("readiness.json"), ("allow_stream",), 1),
    ],
)
def test_planning_and_gcode_contracts_reject_safety_mutations(
    role: str,
    document: JsonObject,
    path: tuple[str, ...],
    value: object,
) -> None:
    assert_invalid(role, set_path(document, path, value))


def test_path_plan_rejects_too_short_geometry() -> None:
    document = golden("outline.path_plan.json")
    document["stroke_paths"][0]["points_mm"] = [[1.0, 2.0]]  # type: ignore[index]
    assert_invalid("path_plan", document)


def test_complexity_contract_accepts_populated_hits_and_dedupe_profile() -> None:
    document = golden("complexity.json")
    decision = cast(dict[str, object], document["decision"])
    decision["soft_limit_hits"] = [{"metric": "component_count", "value": 10, "limit": 5}]
    decision["hard_limit_hits"] = [{"metric": "boundary_pixels", "value": 20, "limit": 15}]
    decision["dedupe_profile"] = {
        "dedupe_short_path_length_mm": 1.0,
        "dedupe_distance_mm": 0.2,
        "dedupe_angle_deg": 5.0,
        "dedupe_overlap_ratio": 0.8,
    }

    assert validate_json_artifact("complexity_report", document, context()) == document


def test_path_plan_rejects_invalid_stage_discriminator() -> None:
    document = golden("outline.path_plan.json")
    document["stage"] = "unexpected"
    assert_invalid("path_plan", document)
