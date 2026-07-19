from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from drawingmachine.domain.workflow import ProcessedImageReview
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_SHA256 = re.compile(r"[0-9a-f]{64}")


class RouteMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    IMAGE_EDIT = "image-edit"


def _invalid(message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload("WORKFLOW_SUBMISSION_INVALID", ErrorCategory.INPUT, message, False, {}))


def _freeze_json(value: object, *, path: str, active: set[int]) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        try:
            str(value)
        except ValueError as error:
            raise _invalid(f"semantic_assessment contains an unserializable integer at {path}") from error
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _invalid(f"semantic_assessment contains invalid Unicode at {path}") from error
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(f"semantic_assessment contains a non-finite number at {path}")
        return value
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise _invalid(f"semantic_assessment contains a recursive object at {path}")
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _invalid(f"semantic_assessment contains a non-string key at {path}")
                frozen[key] = _freeze_json(item, path=f"{path}.{key}", active=active)
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise _invalid(f"semantic_assessment contains a recursive array at {path}")
        active.add(identity)
        try:
            return tuple(_freeze_json(item, path=f"{path}[{index}]", active=active) for index, item in enumerate(value))
        finally:
            active.remove(identity)
    raise _invalid(f"semantic_assessment contains a non-JSON value at {path}")


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def semantic_assessment_to_json(value: Mapping[str, object] | None) -> JsonObject | None:
    """Copy the immutable command DTO tree into a durable mutable JSON value."""
    if value is None:
        return None
    return cast(JsonObject, _thaw_json(value))


@dataclass(frozen=True, slots=True)
class WorkflowSubmissionV1:
    schema_version: int
    job_name: str
    input_path: Path
    route_mode: RouteMode
    semantic_assessment: Mapping[str, object] | None
    prompt: str | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _invalid("workflow submission schema_version must be 1")
        if not isinstance(self.job_name, str) or not self.job_name:
            raise _invalid("workflow submission job_name must not be empty")
        if not isinstance(self.input_path, Path) or not self.input_path.is_absolute():
            raise _invalid("workflow submission input_path must be absolute")
        if not isinstance(self.route_mode, RouteMode):
            raise _invalid("workflow submission route_mode is invalid")
        if self.semantic_assessment is not None:
            if not isinstance(self.semantic_assessment, dict):
                raise _invalid("workflow submission semantic_assessment must be an object or null")
            object.__setattr__(
                self,
                "semantic_assessment",
                cast(Mapping[str, object], _freeze_json(self.semantic_assessment, path="$", active=set())),
            )
        if self.prompt is not None and not isinstance(self.prompt, str):
            raise _invalid("workflow submission prompt must be a string or null")


@dataclass(frozen=True, slots=True)
class ReviewContinuationV1:
    schema_version: int
    job_id: str
    review: ProcessedImageReview
    reviewed_image_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _invalid("review continuation schema_version must be 1")
        if not isinstance(self.job_id, str) or not self.job_id:
            raise _invalid("review continuation job_id must not be empty")
        if not isinstance(self.review, ProcessedImageReview):
            raise _invalid("review continuation review is invalid")
        if not isinstance(self.reviewed_image_sha256, str) or _SHA256.fullmatch(self.reviewed_image_sha256) is None:
            raise _invalid("review continuation digest must be a lowercase SHA-256")


__all__ = ["ReviewContinuationV1", "RouteMode", "WorkflowSubmissionV1", "semantic_assessment_to_json"]
