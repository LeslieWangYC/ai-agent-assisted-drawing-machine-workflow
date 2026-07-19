from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHECK_FIELDS = frozenset({"severity", "code", "message"})
_BOUNDS_FIELDS = frozenset({"min_x", "min_y", "max_x", "max_y"})


@dataclass(frozen=True, slots=True)
class ArtifactContractContext:
    job_id: str
    input_role: str | None
    input_name: str | None
    input_sha256: str | None
    input_size_bytes: int | None
    output_sha256_by_role: Mapping[str, str]


def validate_json_artifact(role: str, document: object, context: ArtifactContractContext) -> JsonObject:
    value = _object(document, role)
    validators = {
        "route_decision": _route,
        "direct_report": _direct_report,
        "normalization_report": _normalization_report,
        "provider_request": _provider_request,
        "provider_response": _provider_response,
        "provider_handoff": _provider_handoff,
        "complexity_report": _complexity,
        "path_plan": _path_plan,
        "selected_path_plan": _selected_path_plan,
        "gcode_preview_report": _gcode_preview,
        "gcode_static": _gcode_static,
        "send_plan": _send_plan,
        "readiness": _readiness,
    }
    validator = validators.get(role)
    if validator is None:
        _invalid(role, "JSON artifact role has no production validator", context.job_id)
    validator(value, context)
    return cast(JsonObject, value)


def validate_gcode_safety_bundle(
    documents: Mapping[str, JsonObject],
    expected: Mapping[str, JsonObject],
    *,
    job_id: str,
) -> None:
    roles = frozenset({"gcode_static", "send_plan", "readiness"})
    if set(documents) != roles or set(expected) != roles:
        _invalid("gcode_safety", "G-code safety bundle roles are incomplete", job_id)
    for role in sorted(roles):
        if documents[role] != expected[role]:
            _invalid(role, "G-code safety artifact does not bind to the exact candidate bytes", job_id)


def _exact(value: Mapping[str, object], fields: frozenset[str], role: str, context: str) -> None:
    actual = set(value)
    if actual != fields:
        missing: list[JsonValue] = list(sorted(fields - actual))
        unknown: list[JsonValue] = list(sorted(actual - fields))
        _invalid(role, f"{context} fields are invalid", None, {"missing": missing, "unknown": unknown})


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _invalid(context, f"{context} must be a JSON object")
    return dict(cast(Mapping[str, object], value))


def _array(value: object, role: str, context: str) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        _invalid(role, f"{context} must be an array")
    return list(cast(Sequence[object], value))


def _string(value: object, role: str, context: str, *, literals: frozenset[str] | None = None) -> str:
    if not isinstance(value, str) or not value or (literals is not None and value not in literals):
        _invalid(role, f"{context} must be a permitted non-empty string")
    return value


def _boolean(value: object, role: str, context: str) -> bool:
    if not isinstance(value, bool):
        _invalid(role, f"{context} must be a boolean")
    return value


def _integer(value: object, role: str, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _invalid(role, f"{context} must be an integer at least {minimum}")
    return value


def _number(value: object, role: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        _invalid(role, f"{context} must be a finite number")
    return float(value)


def _digest(value: object, role: str, context: str) -> str:
    digest = _string(value, role, context)
    if _SHA256.fullmatch(digest) is None:
        _invalid(role, f"{context} must be a lowercase SHA-256 digest")
    return digest


def _string_array(value: object, role: str, context: str) -> None:
    for index, item in enumerate(_array(value, role, context)):
        _string(item, role, f"{context}[{index}]")


def _check_array(value: object, role: str, context: str) -> None:
    for index, item in enumerate(_array(value, role, context)):
        check = _object(item, f"{context}[{index}]")
        _exact(check, _CHECK_FIELDS, role, f"{context}[{index}]")
        _string(
            check["severity"], role, f"{context}[{index}].severity", literals=frozenset({"INFO", "WARNING", "BLOCKER"})
        )
        _string(check["code"], role, f"{context}[{index}].code")
        _string(check["message"], role, f"{context}[{index}].message")


def _size(value: object, role: str, context: str, *, length: int) -> None:
    items = _array(value, role, context)
    if len(items) != length:
        _invalid(role, f"{context} must contain {length} entries")
    for index, item in enumerate(items):
        _number(item, role, f"{context}[{index}]")


def _route(value: dict[str, object], context: ArtifactContractContext) -> None:
    role = "route_decision"
    fields = frozenset(
        {
            "schema",
            "job_name",
            "input_image",
            "decided_at",
            "decider",
            "route",
            "confidence",
            "image_category",
            "rationale",
            "risk_flags",
            "blocking_risks",
            "direct_route_allowed",
            "next_stage",
            "hardware_touched",
            "semantic_style_score",
            "semantic_score_source",
            "semantic_rationale",
            "semantic_blockers",
            "visual_direct_score",
            "visual_score_source",
            "direct_score",
            "route_override",
            "subject_mask_ratio",
            "binary_transition_density",
            "background_simplicity_score",
            "subject_background_separation_score",
            "visual_scores",
            "stats",
            "classification",
        }
    )
    _exact(value, fields, role, role)
    if (
        value["schema"] != "input_route_decision_v1"
        or value["job_name"] != context.job_id
        or (context.input_name is not None and value["input_image"] != context.input_name)
    ):
        _invalid(role, "route decision identity binding is invalid", context.job_id)
    route = _string(value["route"], role, "route", literals=frozenset({"A_DIRECT", "B_IMAGE_EDIT"}))
    expected = (route == "A_DIRECT", "prepare_direct_processed_image" if route == "A_DIRECT" else "qwen_image_edit")
    if value["direct_route_allowed"] is not expected[0] or value["next_stage"] != expected[1]:
        _invalid(role, "route decision authority binding is invalid", context.job_id)
    if value["hardware_touched"] is not False:
        _invalid(role, "route decision must remain pre-hardware", context.job_id)
    for field in ("rationale", "risk_flags", "blocking_risks", "semantic_rationale", "semantic_blockers"):
        _string_array(value[field], role, field)
    visual = _object(value["visual_scores"], "visual_scores")
    _exact(
        visual,
        frozenset(
            {
                "subject_background_separation_score",
                "background_simplicity_score",
                "binary_transition_density_score",
                "subject_mask_ratio_score",
                "component_noise_score",
                "color_gradient_shadow_complexity_score",
                "line_subject_clarity_score",
            }
        ),
        role,
        "visual_scores",
    )
    stats = _object(value["stats"], "stats")
    _exact(
        stats,
        frozenset(
            {
                "width",
                "height",
                "aspect_ratio",
                "quantized_color_count",
                "foreground_ratio",
                "edge_ratio",
                "contrast",
                "white_background_likelihood",
                "component_count",
                "small_component_count",
                "estimated_colorfulness",
                "foreground_background_delta",
                "background_color_variance",
                "border_color_variance",
                "border_dominant_color_ratio",
                "background_edge_density",
                "alpha_background_ratio",
                "alpha_channel_presence",
                "threshold_margin",
                "subject_mask_ratio",
                "binary_transition_density",
                "binarization_threshold",
                "auto_inverted",
                "threshold_variant_subject_ratios",
                "dominant_color_count",
                "dominant_quantized_color_count",
                "local_color_variance",
                "gradient_density",
                "shadow_likelihood",
                "largest_component_ratio",
                "skeleton_density",
                "component_bbox_fill_ratio",
                "post_binarization_readability_proxy",
                "subject_background_separation_score",
                "background_simplicity_score",
            }
        ),
        role,
        "stats",
    )
    classification = _object(value["classification"], "classification")
    _exact(
        classification,
        frozenset({"style_family", "confidence", "rationale", "uncertainty_note", "stats"}),
        role,
        "classification",
    )
    classification_stats = _object(classification["stats"], "classification.stats")
    _exact(
        classification_stats,
        frozenset(
            {
                "width",
                "height",
                "aspect_ratio",
                "quantized_color_count",
                "foreground_ratio",
                "edge_ratio",
                "contrast",
                "white_background_likelihood",
            }
        ),
        role,
        "classification.stats",
    )


def _direct_report(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "direct_report"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "created_at",
                "input",
                "output",
                "size_px",
                "binarization",
                "components_before",
                "components_after",
                "small_component_pixels_removed",
                "foreground_pixels",
                "foreground_ratio",
                "content_bbox_px",
                "hardware_touched",
            }
        ),
        role,
        role,
    )
    if value["schema"] != "direct_processed_image_report_v1" or value["hardware_touched"] is not False:
        _invalid(role, "direct image report discriminator is invalid")
    _size(value["size_px"], role, "size_px", length=2)
    if value["content_bbox_px"] is not None:
        _size(value["content_bbox_px"], role, "content_bbox_px", length=4)
    binarization = _object(value["binarization"], "binarization")
    _exact(
        binarization,
        frozenset({"method", "threshold", "inverted", "min_component_area_px", "median_filter_size"}),
        role,
        "binarization",
    )


def _normalization_report(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "normalization_report"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "created_at",
                "input",
                "output",
                "size_px",
                "threshold",
                "color_delta_threshold",
                "input_colored_pixels",
                "input_colored_ratio",
                "output_black_pixels",
                "output_black_ratio",
                "small_components_removed",
                "small_component_pixels_removed",
                "content_bbox_px",
                "hardware_touched",
            }
        ),
        role,
        role,
    )
    if value["schema"] != "processed_image_normalize_report_v1" or value["hardware_touched"] is not False:
        _invalid(role, "normalization report discriminator is invalid")
    _size(value["size_px"], role, "size_px", length=2)
    if value["content_bbox_px"] is not None:
        _size(value["content_bbox_px"], role, "content_bbox_px", length=4)


def _provider_request(value: dict[str, object], context: ArtifactContractContext) -> None:
    role = "provider_request"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "created_at",
                "provider",
                "provider_config",
                "endpoint",
                "workflow_template",
                "workflow_nodes",
                "source_image",
                "prompt",
                "scale_to_side",
                "scale_to_length",
                "sampler",
                "output_prefix",
                "dry_run",
                "execute",
                "timeout_s",
            }
        ),
        role,
        role,
    )
    if (
        value["schema"] != "local_comfyui_qwen_image_edit_request_record_v1"
        or value["provider"] != "local-comfyui"
        or value["output_prefix"] != context.job_id
        or value["dry_run"] is not False
        or value["execute"] is not True
    ):
        _invalid(role, "provider request binding is invalid", context.job_id)
    nodes = _object(value["workflow_nodes"], "workflow_nodes")
    _exact(nodes, frozenset({"load_image", "prompt", "sampler", "save_image", "scale"}), role, "workflow_nodes")
    source = _object(value["source_image"], "source_image")
    _exact(
        source,
        frozenset({"kind", "value", "copied_input", "name", "sha256", "size_bytes", "mime_type", "size_px", "mode"}),
        role,
        "source_image",
    )
    if context.input_sha256 is not None and (
        _digest(source["sha256"], role, "source_image.sha256") != context.input_sha256
        or source["size_bytes"] != context.input_size_bytes
    ):
        _invalid(role, "provider request source image does not bind to the worker input", context.job_id)
    prompt = _object(value["prompt"], "prompt")
    _exact(prompt, frozenset({"path", "sha256", "source", "workflow_node"}), role, "prompt")
    _digest(prompt["sha256"], role, "prompt.sha256")
    sampler = _object(value["sampler"], "sampler")
    _exact(sampler, frozenset({"seed", "steps", "cfg", "denoise"}), role, "sampler")


def _provider_response(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "provider_response"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "created_at",
                "status",
                "system_stats",
                "upload",
                "prompt_id",
                "history",
                "outputs",
                "processed_image",
                "free_requested",
            }
        ),
        role,
        role,
    )
    if (
        value["schema"] != "local_comfyui_qwen_image_edit_response_record_v1"
        or value["status"] != "PROCESSED_IMAGE_CREATED"
    ):
        _invalid(role, "provider response discriminator is invalid")
    _object(value["system_stats"], "system_stats")
    _object(value["history"], "history")
    upload = _object(value["upload"], "upload")
    if set(upload) not in ({"name"}, {"name", "subfolder", "type"}):
        _invalid(role, "upload fields are invalid")
    _string(upload["name"], role, "upload.name")
    if "subfolder" in upload:
        if not isinstance(upload["subfolder"], str):
            _invalid(role, "upload.subfolder must be a string")
        _string(upload["type"], role, "upload.type", literals=frozenset({"input", "output", "temp"}))
    for index, item in enumerate(_array(value["outputs"], role, "outputs")):
        _image_record(item, role, f"outputs[{index}]")


def _image_record(value: object, role: str, context: str) -> None:
    image = _object(value, context)
    _exact(image, frozenset({"filename", "subfolder", "type"}), role, context)
    _string(image["filename"], role, f"{context}.filename")
    if not isinstance(image["subfolder"], str):
        _invalid(role, f"{context}.subfolder must be a string")
    _string(image["type"], role, f"{context}.type", literals=frozenset({"input", "output", "temp"}))


def _provider_handoff(value: dict[str, object], context: ArtifactContractContext) -> None:
    role = "provider_handoff"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "status",
                "created_at",
                "job_name",
                "stage",
                "provider",
                "source_image",
                "prompt",
                "artifacts",
                "dry_run",
                "execute",
                "next_allowed_stage",
                "review_gate",
            }
        ),
        role,
        role,
    )
    if (
        value["schema"] != "cnc_drawable_workflow_handoff_v1"
        or value["status"] != "PROCESSED_IMAGE_CREATED"
        or value["job_name"] != context.job_id
        or value["stage"] != "qwen_image_edit_processed_image"
        or value["dry_run"] is not False
        or value["execute"] is not True
        or value["next_allowed_stage"] != "review_processed_image"
    ):
        _invalid(role, "provider handoff binding is invalid", context.job_id)
    provider = _object(value["provider"], "provider")
    _exact(
        provider,
        frozenset(
            {
                "name",
                "endpoint",
                "workflow_template",
                "workflow_nodes",
                "prompt_id",
                "scale_to_length",
                "free_requested",
            }
        ),
        role,
        "provider",
    )
    nodes = _object(provider["workflow_nodes"], "provider.workflow_nodes")
    _exact(
        nodes, frozenset({"load_image", "prompt", "sampler", "save_image", "scale"}), role, "provider.workflow_nodes"
    )
    source = _object(value["source_image"], "source_image")
    _exact(
        source,
        frozenset({"kind", "value", "copied_input", "name", "sha256", "size_bytes", "mime_type", "size_px", "mode"}),
        role,
        "source_image",
    )
    if context.input_sha256 is not None and (
        _digest(source["sha256"], role, "source_image.sha256") != context.input_sha256
        or source["size_bytes"] != context.input_size_bytes
    ):
        _invalid(role, "provider handoff source image does not bind to the worker input", context.job_id)
    prompt = _object(value["prompt"], "prompt")
    _exact(prompt, frozenset({"path", "sha256", "source", "workflow_node"}), role, "prompt")
    artifacts = _object(value["artifacts"], "artifacts")
    _exact(
        artifacts,
        frozenset({"out_dir", "request_record", "response_record", "processed_image", "processed_image_sha256"}),
        role,
        "artifacts",
    )
    expected_image = context.output_sha256_by_role.get("processed_image_raw")
    if (
        expected_image is not None
        and _digest(artifacts["processed_image_sha256"], role, "artifacts.processed_image_sha256") != expected_image
    ):
        _invalid(role, "provider handoff processed image digest is invalid", context.job_id)
    gate = _object(value["review_gate"], "review_gate")
    _exact(gate, frozenset({"required", "status"}), role, "review_gate")
    if gate != {"required": True, "status": "pending"}:
        _invalid(role, "provider handoff review gate is invalid", context.job_id)


def _complexity(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "complexity_report"
    _exact(
        value,
        frozenset(
            {
                "schema",
                "created_at",
                "input",
                "size_px",
                "binarization",
                "metrics",
                "limits",
                "decision",
                "hardware_touched",
            }
        ),
        role,
        role,
    )
    if value["schema"] != "processed_image_complexity_report_v1" or value["hardware_touched"] is not False:
        _invalid(role, "complexity report discriminator is invalid")
    binarization = _object(value["binarization"], "binarization")
    _exact(binarization, frozenset({"method", "threshold", "inverted", "min_component_area_px"}), role, "binarization")
    metrics = _object(value["metrics"], "metrics")
    _exact(
        metrics,
        frozenset(
            {
                "foreground_pixels",
                "foreground_ratio",
                "component_count",
                "largest_component_pixels",
                "top5_component_pixels",
                "skeleton_pixels",
                "skeleton_ratio",
                "boundary_pixels",
                "boundary_ratio",
                "horizontal_transitions",
                "vertical_transitions",
                "transitions_total",
                "transitions_per_megapixel",
                "content_bbox_px",
                "content_bbox_fill_ratio",
            }
        ),
        role,
        "metrics",
    )
    limits = _object(value["limits"], "limits")
    _exact(limits, frozenset({"soft", "hard"}), role, "limits")
    for key in ("soft", "hard"):
        nested = _object(limits[key], f"limits.{key}")
        _exact(
            nested,
            frozenset({"component_count", "skeleton_pixels", "boundary_pixels", "transitions_total"}),
            role,
            f"limits.{key}",
        )
    decision = _object(value["decision"], "decision")
    _exact(
        decision,
        frozenset({"status", "reason", "soft_limit_hits", "hard_limit_hits", "next_allowed_stage", "dedupe_profile"}),
        role,
        "decision",
    )
    for key in ("soft_limit_hits", "hard_limit_hits"):
        for index, item in enumerate(_array(decision[key], role, f"decision.{key}")):
            hit = _object(item, f"decision.{key}[{index}]")
            _exact(hit, frozenset({"metric", "value", "limit"}), role, f"decision.{key}[{index}]")
    if decision["dedupe_profile"] is not None:
        profile = _object(decision["dedupe_profile"], "decision.dedupe_profile")
        _exact(
            profile,
            frozenset(
                {"dedupe_short_path_length_mm", "dedupe_distance_mm", "dedupe_angle_deg", "dedupe_overlap_ratio"}
            ),
            role,
            "decision.dedupe_profile",
        )


def _path_plan(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "path_plan"
    _exact(
        value,
        frozenset(
            {
                "stage",
                "input",
                "method",
                "canvas",
                "stroke_paths",
                "preview_centerline_paths",
                "centerline_base_paths",
                "fill_paths",
                "fill_boundary_paths",
                "fill_regions",
                "regions",
                "metrics",
                "gcode_policy",
                "checks",
                "artifacts",
            }
        ),
        role,
        role,
    )
    if value["stage"] != "drawable_path_plan_mvp":
        _invalid(role, "path plan stage is invalid")
    method = _object(value["method"], "method")
    _exact(
        method,
        frozenset({"binarization", "threshold", "inverted", "centerline", "stroke_postprocess", "fill", "routing"}),
        role,
        "method",
    )
    post = _object(method["stroke_postprocess"], "method.stroke_postprocess")
    _exact(
        post,
        frozenset(
            {
                "drop_short_stroke_mm",
                "merge_endpoint_distance_mm",
                "merge_angle_deg",
                "dedupe_short_path_length_mm",
                "dedupe_distance_mm",
                "dedupe_angle_deg",
                "dedupe_overlap_ratio",
            }
        ),
        role,
        "method.stroke_postprocess",
    )
    _canvas(value["canvas"], role, "canvas")
    for field in ("stroke_paths", "preview_centerline_paths", "centerline_base_paths"):
        _path_records(value[field], role, field, frozenset({"id", "source", "points_mm", "length_mm"}))
    _path_records(
        value["fill_paths"], role, "fill_paths", frozenset({"id", "source", "points_mm", "length_mm", "spacing_mm"})
    )
    _path_records(
        value["fill_boundary_paths"],
        role,
        "fill_boundary_paths",
        frozenset({"id", "source", "points_mm", "length_mm", "region_id"}),
    )
    for field, fields in (
        (
            "fill_regions",
            frozenset(
                {
                    "id",
                    "area_px",
                    "area_mm2",
                    "bbox_px",
                    "bbox_mm",
                    "estimated_avg_width_mm",
                    "fill_method",
                    "hatch_path_ids",
                    "boundary_path_ids",
                }
            ),
        ),
        (
            "regions",
            frozenset(
                {
                    "id",
                    "area_px",
                    "area_mm2",
                    "bbox_px",
                    "bbox_mm",
                    "skeleton_pixels",
                    "estimated_avg_width_mm",
                    "classification",
                }
            ),
        ),
    ):
        for index, item in enumerate(_array(value[field], role, field)):
            _exact(_object(item, f"{field}[{index}]"), fields, role, f"{field}[{index}]")
    metrics = _object(value["metrics"], "metrics")
    _exact(
        metrics,
        frozenset(
            {
                "source_size_px",
                "foreground_pixels",
                "skeleton_pixels",
                "stroke_path_count",
                "preview_centerline_path_count",
                "centerline_base_path_count",
                "raw_stroke_path_count",
                "dropped_short_stroke_count",
                "merged_stroke_pair_count",
                "deduped_short_stroke_count",
                "deduped_short_stroke_length_mm",
                "fill_path_count",
                "fill_boundary_path_count",
                "fill_region_count",
                "preview_centerline_length_mm",
                "centerline_base_length_mm",
                "stroke_length_mm",
                "fill_length_mm",
                "fill_boundary_length_mm",
                "preview_draw_length_mm",
                "draw_length_mm",
                "estimated_pen_up_travel_mm",
            }
        ),
        role,
        "metrics",
    )
    policy = _object(value["gcode_policy"], "gcode_policy")
    _exact(
        policy,
        frozenset({"status", "pen_lift", "default_pen_up_z", "default_pen_down_z", "source_for_gcode"}),
        role,
        "gcode_policy",
    )
    artifacts = _object(value["artifacts"], "artifacts")
    _exact(
        artifacts,
        frozenset(
            {
                "path_plan",
                "binary_png",
                "skeleton_debug_png",
                "preview_stroke_only_svg",
                "preview_centerline_only_svg",
                "preview_layers_svg",
                "preview_final_svg",
                "report_md",
            }
        ),
        role,
        "artifacts",
    )
    _check_array(value["checks"], role, "checks")


def _canvas(value: object, role: str, context: str) -> None:
    canvas = _object(value, context)
    _exact(
        canvas,
        frozenset(
            {
                "width_mm",
                "height_mm",
                "draw_width_mm",
                "draw_height_mm",
                "offset_x_mm",
                "offset_y_mm",
                "mm_per_px",
                "pen_width_mm",
                "min_gap_mm",
            }
        ),
        role,
        context,
    )


def _path_records(value: object, role: str, context: str, fields: frozenset[str]) -> None:
    for index, item in enumerate(_array(value, role, context)):
        record = _object(item, f"{context}[{index}]")
        _exact(record, fields, role, f"{context}[{index}]")
        points = _array(record["points_mm"], role, f"{context}[{index}].points_mm")
        if len(points) < 2:
            _invalid(role, f"{context}[{index}].points_mm must contain at least two points")
        for point_index, point in enumerate(points):
            _size(point, role, f"{context}[{index}].points_mm[{point_index}]", length=2)


def _selected_path_plan(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "selected_path_plan"
    _exact(
        value,
        frozenset(
            {
                "stage",
                "source_path_plan",
                "path_mode",
                "path_mode_definition",
                "canvas",
                "selected_paths_before_ordering",
                "ordered_paths",
                "execution",
                "metrics",
                "checks",
            }
        ),
        role,
        role,
    )
    if (
        value["stage"] != "gcode_selected_path_plan_v1"
        or value["path_mode"] != "stroke"
        or value["path_mode_definition"] != "fill_paths + stroke_paths + fill_boundary_paths"
    ):
        _invalid(role, "selected path plan production mode is invalid")
    _canvas(value["canvas"], role, "canvas")
    fields = frozenset({"id", "group", "source", "points_mm"})
    for field in ("selected_paths_before_ordering", "ordered_paths"):
        _path_records(value[field], role, field, fields)
    execution = _object(value["execution"], "execution")
    _exact(execution, frozenset({"allow_run", "reason"}), role, "execution")
    if execution["allow_run"] is not False:
        _invalid(role, "Package B selected plan cannot grant hardware run authority")
    metrics = _object(value["metrics"], "metrics")
    _exact(
        metrics,
        frozenset(
            {
                "path_count",
                "selected_path_count_before_ordering",
                "point_count",
                "draw_length_mm",
                "pen_up_travel_mm",
                "estimated_draw_time_s",
                "estimated_travel_time_s",
                "rough_estimated_total_time_s",
                "pen_lift_count",
                "canvas_width_mm",
                "canvas_height_mm",
                "bounds",
                "work_coordinate",
                "xy_airrun",
                "coordinate_transform",
            }
        ),
        role,
        "metrics",
    )
    bounds = _object(metrics["bounds"], "metrics.bounds")
    _exact(bounds, _BOUNDS_FIELDS | frozenset({"span_x", "span_y"}), role, "metrics.bounds")
    transform = _object(metrics["coordinate_transform"], "metrics.coordinate_transform")
    _exact(
        transform,
        frozenset({"align_mode", "mirror_x", "mirror_y", "anchor_x", "anchor_y", "paper_center_x", "paper_center_y"}),
        role,
        "metrics.coordinate_transform",
    )
    _check_array(value["checks"], role, "checks")


def _gcode_preview(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "gcode_preview_report"
    _exact(value, frozenset({"input", "svg", "metrics"}), role, role)
    metrics = _object(value["metrics"], "metrics")
    _exact(
        metrics,
        frozenset(
            {
                "draw_segment_count",
                "travel_segment_count",
                "draw_length_mm",
                "travel_length_mm",
                "bounds",
                "motion_command_count",
            }
        ),
        role,
        "metrics",
    )
    bounds = _object(metrics["bounds"], "metrics.bounds")
    _exact(bounds, _BOUNDS_FIELDS, role, "metrics.bounds")


def _gcode_static(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "gcode_static"
    _exact(value, frozenset({"summary", "checks"}), role, role)
    summary = _object(value["summary"], "summary")
    _exact(
        summary,
        frozenset(
            {
                "input",
                "motion_command_count",
                "draw_segment_count",
                "travel_segment_count",
                "draw_length_mm",
                "travel_length_mm",
                "draw_path_count",
                "pen_lift_count",
                "pen_down_count",
                "short_draw_path_count",
                "max_feed_seen",
                "bounds",
                "gate",
            }
        ),
        role,
        "summary",
    )
    bounds = _object(summary["bounds"], "summary.bounds")
    _exact(bounds, _BOUNDS_FIELDS, role, "summary.bounds")
    _string(summary["gate"], role, "summary.gate", literals=frozenset({"PASS_STATIC", "REVIEW_WARNINGS", "BLOCKED"}))
    _check_array(value["checks"], role, "checks")


def _send_plan(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "send_plan"
    _exact(
        value,
        frozenset(
            {
                "raw_line_count",
                "blank_line_count",
                "comment_only_line_count",
                "sendable_line_count",
                "estimated_serial_bytes",
                "max_send_line_length",
                "first_sendable_lines",
                "last_sendable_lines",
                "streaming_mode",
            }
        ),
        role,
        role,
    )
    counts = {
        field: _integer(value[field], role, field)
        for field in (
            "raw_line_count",
            "blank_line_count",
            "comment_only_line_count",
            "sendable_line_count",
            "estimated_serial_bytes",
            "max_send_line_length",
        )
    }
    if counts["raw_line_count"] != (
        counts["blank_line_count"] + counts["comment_only_line_count"] + counts["sendable_line_count"]
    ):
        _invalid(role, "send plan line counts are inconsistent")
    for field in ("first_sendable_lines", "last_sendable_lines"):
        for index, item in enumerate(_array(value[field], role, field)):
            line = _object(item, f"{field}[{index}]")
            _exact(line, frozenset({"source_line", "command"}), role, f"{field}[{index}]")
            _integer(line["source_line"], role, f"{field}[{index}].source_line", minimum=1)
            _string(line["command"], role, f"{field}[{index}].command")
    if value["streaming_mode"] != "conservative_send_line_wait_ok":
        _invalid(role, "send plan streaming mode is invalid")


def _readiness(value: dict[str, object], context: ArtifactContractContext) -> None:
    del context
    role = "readiness"
    _exact(value, frozenset({"allow_stream", "blockers", "warnings", "pen_motion_assessment"}), role, role)
    allow = _boolean(value["allow_stream"], role, "allow_stream")
    blockers = _array(value["blockers"], role, "blockers")
    warnings = _array(value["warnings"], role, "warnings")
    for field, items in (("blockers", blockers), ("warnings", warnings)):
        for index, item in enumerate(items):
            _string(item, role, f"{field}[{index}]")
    if allow == bool(blockers):
        _invalid(role, "readiness allow_stream does not agree with blockers")
    assessment = _object(value["pen_motion_assessment"], "pen_motion_assessment")
    _exact(
        assessment,
        frozenset({"pen_lift_count", "pen_down_count", "draw_path_count", "lift_down_delta", "hard_gate"}),
        role,
        "pen_motion_assessment",
    )
    for field in ("pen_lift_count", "pen_down_count", "draw_path_count"):
        _integer(assessment[field], role, f"pen_motion_assessment.{field}")
    _integer(assessment["lift_down_delta"], role, "pen_motion_assessment.lift_down_delta", minimum=-1)
    _boolean(assessment["hard_gate"], role, "pen_motion_assessment.hard_gate")


def _invalid(
    role: str,
    message: str,
    job_id: str | None = None,
    details: Mapping[str, JsonValue] | None = None,
) -> NoReturn:
    payload_details: JsonObject = {"role": role}
    if details is not None:
        payload_details.update(details)
    raise DrawingMachineError(
        ErrorPayload(
            "WORKER_ARTIFACT_CONTENT_INVALID",
            ErrorCategory.VALIDATION,
            message,
            False,
            payload_details,
            job_id=job_id,
        )
    )


__all__ = ["ArtifactContractContext", "validate_gcode_safety_bundle", "validate_json_artifact"]
