from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REVIEW_FIELDS = frozenset(
    {
        "schema",
        "job_name",
        "reviewed_at",
        "reviewer",
        "processed_image",
        "handoff",
        "status",
        "checks",
        "issues",
        "revised_prompt",
        "next_allowed_stage",
    }
)
REVIEW_CHECK_NAMES = (
    "recognizable_subject",
    "background_simplified",
    "black_on_white",
    "no_edge_clipping",
    "line_density_drawable",
    "no_soft_gradients",
    "limited_tiny_isolated_marks",
    "vectorization_complexity_ok",
)
STATUS_TO_NEXT_STAGE = {
    "PASS_TO_BUILD": "build_drawable_job",
    "REVISE_PROMPT": "repeat_image_edit",
    "REJECT_INPUT": "stop",
}


def workflow_error(message: str, *, field: str | None = None) -> DrawingMachineError:
    details: JsonObject = {} if field is None else {"field": field}
    return DrawingMachineError(
        ErrorPayload(
            code="WORKFLOW_CONTRACT_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details=details,
        )
    )


def _json_document(value: Mapping[str, object], context: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise workflow_error(f"{context} must contain finite JSON values") from error


def _load_document(value: str) -> JsonObject:
    loaded: Any = json.loads(value)
    if not isinstance(loaded, dict):
        raise RuntimeError("stored workflow document is not an object")
    return cast(JsonObject, loaded)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise workflow_error(f"{context} must be a JSON object", field=context)
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            raise workflow_error(f"{context} contains a non-string key", field=context)
        result[key] = item
    return result


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise workflow_error(f"{field} must be a string", field=field)
    if not allow_empty and not value:
        raise workflow_error(f"{field} must not be empty", field=field)
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise workflow_error(f"{field} must be an array of strings", field=field)
    return tuple(cast(list[str], value))


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise workflow_error(f"{field} must be a number", field=field)
    return float(value)


@dataclass(frozen=True, slots=True)
class RouteContext:
    job_name: str = ""
    input_image: str = ""
    prompt: str = ""
    decided_at: str = ""
    decider: str = "agent:cnc-drawable-router"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    direct_score: float
    semantic_style_score: float
    visual_direct_score: float
    risk_flags: tuple[str, ...]
    blocking_risks: tuple[str, ...]
    route_override: str | None
    _document: str

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> Self:
        route = _string(value.get("route"), "route.route")
        direct_score = value.get("direct_score")
        semantic_score = value.get("semantic_style_score")
        visual_score = value.get("visual_direct_score")
        if not all(
            isinstance(item, int | float) and not isinstance(item, bool)
            for item in (direct_score, semantic_score, visual_score)
        ):
            raise workflow_error("route scores must be numbers")
        override = value.get("route_override")
        if override is not None and not isinstance(override, str):
            raise workflow_error("route.route_override must be a string or null")
        return cls(
            route=route,
            direct_score=float(cast(float, direct_score)),
            semantic_style_score=float(cast(float, semantic_score)),
            visual_direct_score=float(cast(float, visual_score)),
            risk_flags=_string_list(value.get("risk_flags"), "route.risk_flags"),
            blocking_risks=_string_list(value.get("blocking_risks"), "route.blocking_risks"),
            route_override=override,
            _document=_json_document(value, "route decision"),
        )

    def to_json(self) -> JsonObject:
        return _load_document(self._document)


@dataclass(frozen=True, slots=True)
class DirectImageConfig:
    threshold: int | None = None
    invert: bool | None = None
    min_component_area_px: int = 8
    median_filter_size: int = 3
    created_at: str = ""
    input_path: str = ""
    output_path: str = ""


@dataclass(frozen=True, slots=True)
class NormalizationConfig:
    threshold: float = 220.0
    color_delta_threshold: int = 10
    remove_small_components_px: int = 8
    created_at: str = ""
    input_path: str = ""
    output_path: str = ""


@dataclass(frozen=True, slots=True)
class PreparedImage:
    png_bytes: bytes
    _report: str

    @property
    def report(self) -> JsonObject:
        return _load_document(self._report)

    def to_json(self) -> JsonObject:
        return self.report

    @classmethod
    def create(cls, png_bytes: bytes, report: Mapping[str, object]) -> Self:
        return cls(bytes(png_bytes), _json_document(report, "direct image report"))


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    png_bytes: bytes
    _report: str

    @property
    def report(self) -> JsonObject:
        return _load_document(self._report)

    def to_json(self) -> JsonObject:
        return self.report

    @classmethod
    def create(cls, png_bytes: bytes, report: Mapping[str, object]) -> Self:
        return cls(bytes(png_bytes), _json_document(report, "normalization report"))


@dataclass(frozen=True, slots=True)
class ProcessedImageReview:
    job_name: str
    status: str
    next_allowed_stage: str
    processed_image: str
    checks: tuple[tuple[str, bool], ...]
    issues: tuple[str, ...]
    _document: str

    @classmethod
    def from_json(cls, value: object) -> Self:
        data = _object(value, "processed image review")
        unknown = sorted(set(data) - _REVIEW_FIELDS)
        missing = sorted(_REVIEW_FIELDS - set(data))
        if unknown:
            raise workflow_error(f"processed image review has unknown keys: {', '.join(unknown)}")
        if missing:
            raise workflow_error(f"processed image review has missing keys: {', '.join(missing)}")
        if data["schema"] != "processed_image_review_v1":
            raise workflow_error("review.schema must be processed_image_review_v1")
        job_name = _string(data["job_name"], "review.job_name")
        _string(data["reviewed_at"], "review.reviewed_at")
        _string(data["reviewer"], "review.reviewer")
        processed_image = _string(data["processed_image"], "review.processed_image")
        if data["handoff"] is not None:
            _string(data["handoff"], "review.handoff")
        status = _string(data["status"], "review.status")
        if status not in STATUS_TO_NEXT_STAGE:
            raise workflow_error("review.status has an invalid value")
        checks_value = _object(data["checks"], "review.checks")
        if set(checks_value) != set(REVIEW_CHECK_NAMES):
            raise workflow_error("review.checks must contain the exact review check set")
        if not all(isinstance(item, bool) for item in checks_value.values()):
            raise workflow_error("review.checks values must be booleans")
        checks = tuple((name, cast(bool, checks_value[name])) for name in REVIEW_CHECK_NAMES)
        issues = _string_list(data["issues"], "review.issues")
        revised_prompt = data["revised_prompt"]
        if revised_prompt is not None:
            _string(revised_prompt, "review.revised_prompt", allow_empty=True)
        next_stage = _string(data["next_allowed_stage"], "review.next_allowed_stage")
        if next_stage != STATUS_TO_NEXT_STAGE[status]:
            raise workflow_error("review.next_allowed_stage does not match status")
        failed = [name for name, passed in checks if not passed]
        if status == "PASS_TO_BUILD" and failed:
            raise workflow_error(f"PASS_TO_BUILD cannot include failed checks: {', '.join(failed)}")
        if status == "PASS_TO_BUILD" and issues:
            raise workflow_error("PASS_TO_BUILD cannot include unresolved issues")
        if status == "REVISE_PROMPT" and not issues and not revised_prompt:
            raise workflow_error("REVISE_PROMPT requires issues or a revised prompt")
        return cls(
            job_name=job_name,
            status=status,
            next_allowed_stage=next_stage,
            processed_image=processed_image,
            checks=checks,
            issues=issues,
            _document=_json_document(data, "processed image review"),
        )

    def to_json(self) -> JsonObject:
        return _load_document(self._document)

    def validate_for(
        self,
        *,
        job_name: str,
        processed_image_sha256: str,
        reviewed_image_sha256: str,
    ) -> None:
        if self.job_name != job_name:
            raise workflow_error("review.job_name does not match the current job_name")
        for field, digest in (
            ("processed_image_sha256", processed_image_sha256),
            ("reviewed_image_sha256", reviewed_image_sha256),
        ):
            if _SHA256_PATTERN.fullmatch(digest) is None:
                raise workflow_error(f"{field} must be a lowercase SHA-256 digest")
        if processed_image_sha256 != reviewed_image_sha256:
            raise workflow_error("reviewed processed-image digest does not match the current image digest")


@dataclass(frozen=True, slots=True)
class ComplexityLimits:
    soft_component_count: int = 160
    soft_skeleton_pixels: int = 26000
    soft_boundary_pixels: int = 45000
    soft_transitions_total: int = 68000
    hard_component_count: int = 260
    hard_skeleton_pixels: int = 34000
    hard_boundary_pixels: int = 60000
    hard_transitions_total: int = 100000
    threshold: int | None = None
    invert: bool | None = None
    min_component_area_px: int = 8
    created_at: str = ""
    input_path: str = ""

    def soft(self) -> dict[str, int]:
        return {
            "component_count": self.soft_component_count,
            "skeleton_pixels": self.soft_skeleton_pixels,
            "boundary_pixels": self.soft_boundary_pixels,
            "transitions_total": self.soft_transitions_total,
        }

    def hard(self) -> dict[str, int]:
        return {
            "component_count": self.hard_component_count,
            "skeleton_pixels": self.hard_skeleton_pixels,
            "boundary_pixels": self.hard_boundary_pixels,
            "transitions_total": self.hard_transitions_total,
        }


@dataclass(frozen=True, slots=True)
class DedupeProfile:
    dedupe_short_path_length_mm: float = 2.0
    dedupe_distance_mm: float = 0.3
    dedupe_angle_deg: float = 25.0
    dedupe_overlap_ratio: float = 0.65


@dataclass(frozen=True, slots=True)
class ComplexityResult:
    status: str
    dedupe_profile: DedupeProfile | None
    _document: str

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> Self:
        decision = _object(value.get("decision"), "complexity.decision")
        status = _string(decision.get("status"), "complexity.decision.status")
        raw_profile = decision.get("dedupe_profile")
        profile = None
        if raw_profile is not None:
            profile_data = _object(raw_profile, "complexity.decision.dedupe_profile")
            profile = DedupeProfile(
                dedupe_short_path_length_mm=_number(
                    profile_data.get("dedupe_short_path_length_mm"),
                    "complexity.dedupe_short_path_length_mm",
                ),
                dedupe_distance_mm=_number(
                    profile_data.get("dedupe_distance_mm"),
                    "complexity.dedupe_distance_mm",
                ),
                dedupe_angle_deg=_number(profile_data.get("dedupe_angle_deg"), "complexity.dedupe_angle_deg"),
                dedupe_overlap_ratio=_number(
                    profile_data.get("dedupe_overlap_ratio"),
                    "complexity.dedupe_overlap_ratio",
                ),
            )
        return cls(status, profile, _json_document(value, "complexity result"))

    def to_json(self) -> JsonObject:
        return _load_document(self._document)


__all__ = [
    "REVIEW_CHECK_NAMES",
    "STATUS_TO_NEXT_STAGE",
    "ComplexityLimits",
    "ComplexityResult",
    "DedupeProfile",
    "DirectImageConfig",
    "NormalizationConfig",
    "NormalizedImage",
    "PreparedImage",
    "ProcessedImageReview",
    "RouteContext",
    "RouteDecision",
    "workflow_error",
]
