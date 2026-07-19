from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import Any, NoReturn, cast

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.domain.planning.models import parse_path_plan_config
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_SUPPORTED_SCHEMA_VERSION = 1
_GCODE_FIELDS = frozenset(
    {
        "hardware_canvas_width_mm",
        "hardware_canvas_height_mm",
        "machine_width_mm",
        "machine_height_mm",
        "paper_center_x",
        "paper_center_y",
        "pen_up_z",
        "pen_down_z",
        "feed_travel",
        "feed_draw",
        "feed_pen_down",
        "feed_pen_up",
        "max_feed",
        "work_coordinate",
        "align_mode",
        "mirror_y",
        "safe_start",
        "path_mode",
    }
)


class CheckSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"


class StaticGate(StrEnum):
    PASS_STATIC = "PASS_STATIC"
    REVIEW_WARNINGS = "REVIEW_WARNINGS"
    BLOCKED = "BLOCKED"


class ReadinessStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SafetyCheck:
    severity: CheckSeverity
    code: str
    message: str

    def to_json(self) -> JsonObject:
        return {"severity": self.severity.value, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class MachineBuildProfile:
    hardware_canvas_width_mm: float
    hardware_canvas_height_mm: float
    machine_width_mm: float
    machine_height_mm: float
    paper_center_x: float
    paper_center_y: float
    pen_up_z: float
    pen_down_z: float
    feed_travel: float
    feed_draw: float
    feed_pen_down: float
    feed_pen_up: float
    max_feed: float
    work_coordinate: str
    align_mode: str
    mirror_y: bool
    safe_start: bool
    path_mode: str

    @classmethod
    def stage1_defaults(cls) -> MachineBuildProfile:
        return cls(
            hardware_canvas_width_mm=144.0,
            hardware_canvas_height_mm=144.0,
            machine_width_mm=192.0,
            machine_height_mm=192.0,
            paper_center_x=96.0,
            paper_center_y=96.0,
            pen_up_z=3.5,
            pen_down_z=0.0,
            feed_travel=1200.0,
            feed_draw=900.0,
            feed_pen_down=100.0,
            feed_pen_up=400.0,
            max_feed=1200.0,
            work_coordinate="G54",
            align_mode="center",
            mirror_y=True,
            safe_start=True,
            path_mode="stroke",
        )


@dataclass(frozen=True, slots=True, init=False)
class GcodeBuildResult:
    gcode: str
    report: str
    checks: tuple[SafetyCheck, ...]
    _selected_plan_document: str = dataclass_field(init=False, repr=False)
    _metrics_document: str = dataclass_field(init=False, repr=False)

    def __init__(
        self,
        gcode: str,
        selected_plan: JsonObject,
        report: str,
        metrics: JsonObject,
        checks: tuple[SafetyCheck, ...],
    ) -> None:
        object.__setattr__(self, "gcode", gcode)
        object.__setattr__(self, "report", report)
        object.__setattr__(self, "checks", tuple(checks))
        object.__setattr__(
            self,
            "_selected_plan_document",
            _safety_json_document(selected_plan, "selected plan"),
        )
        object.__setattr__(self, "_metrics_document", _safety_json_document(metrics, "build metrics"))

    @property
    def selected_plan(self) -> JsonObject:
        return _load_safety_json_document(self._selected_plan_document)

    @property
    def metrics(self) -> JsonObject:
        return _load_safety_json_document(self._metrics_document)


@dataclass(frozen=True, slots=True, init=False)
class PreviewResult:
    svg: str
    _report_document: str = dataclass_field(init=False, repr=False)

    def __init__(self, svg: str, report: JsonObject) -> None:
        object.__setattr__(self, "svg", svg)
        object.__setattr__(self, "_report_document", _safety_json_document(report, "preview report"))

    @property
    def report(self) -> JsonObject:
        return _load_safety_json_document(self._report_document)


@dataclass(frozen=True, slots=True)
class StaticCheckOptions:
    input_name: str
    canvas_width_mm: float
    canvas_height_mm: float
    machine_width_mm: float
    machine_height_mm: float
    canvas_bounds_mode: str
    pen_up_z: float
    pen_down_z: float
    start_x: float
    start_y: float
    start_z: float | None
    z_tolerance: float
    max_feed: float
    max_pen_down_travel_mm: float
    warn_pen_lifts: int
    warn_short_path_mm: float

    @classmethod
    def stage1_defaults(cls, *, input_name: str = "drawing.gcode") -> StaticCheckOptions:
        return cls(
            input_name=input_name,
            canvas_width_mm=144.0,
            canvas_height_mm=144.0,
            machine_width_mm=192.0,
            machine_height_mm=192.0,
            canvas_bounds_mode="span",
            pen_up_z=3.5,
            pen_down_z=0.0,
            start_x=96.0,
            start_y=96.0,
            start_z=3.5,
            z_tolerance=0.01,
            max_feed=1200.0,
            max_pen_down_travel_mm=0.05,
            warn_pen_lifts=500,
            warn_short_path_mm=0.5,
        )


@dataclass(frozen=True, slots=True, init=False)
class StaticCheckResult:
    checks: tuple[SafetyCheck, ...]
    raw_line_count: int
    actual_sha256: str
    max_feed_limit: float
    _summary_document: str = dataclass_field(init=False, repr=False)

    def __init__(
        self,
        summary: JsonObject,
        checks: tuple[SafetyCheck, ...],
        raw_line_count: int,
        actual_sha256: str,
        max_feed_limit: float,
    ) -> None:
        immutable_checks = tuple(checks)
        normalized_summary = dict(summary)
        normalized_summary["gate"] = _gate_from_safety_checks(immutable_checks).value
        object.__setattr__(self, "checks", immutable_checks)
        object.__setattr__(self, "raw_line_count", raw_line_count)
        object.__setattr__(self, "actual_sha256", actual_sha256)
        object.__setattr__(self, "max_feed_limit", max_feed_limit)
        object.__setattr__(self, "_summary_document", _safety_json_document(normalized_summary, "static summary"))

    @property
    def summary(self) -> JsonObject:
        return _load_safety_json_document(self._summary_document)

    @property
    def gate(self) -> StaticGate:
        return _gate_from_safety_checks(self.checks)

    def to_json(self) -> JsonObject:
        return {"summary": self.summary, "checks": [check.to_json() for check in self.checks]}


@dataclass(frozen=True, slots=True)
class SendLine:
    source_line: int
    command: str

    def to_json(self) -> JsonObject:
        return {"source_line": self.source_line, "command": self.command}


@dataclass(frozen=True, slots=True)
class SendPlan:
    raw_line_count: int
    blank_line_count: int
    comment_only_line_count: int
    sendable_line_count: int
    estimated_serial_bytes: int
    max_send_line_length: int
    first_sendable_lines: tuple[SendLine, ...]
    last_sendable_lines: tuple[SendLine, ...]
    streaming_mode: str
    actual_sha256: str
    candidate_source: str

    def to_json(self) -> JsonObject:
        return {
            "raw_line_count": self.raw_line_count,
            "blank_line_count": self.blank_line_count,
            "comment_only_line_count": self.comment_only_line_count,
            "sendable_line_count": self.sendable_line_count,
            "estimated_serial_bytes": self.estimated_serial_bytes,
            "max_send_line_length": self.max_send_line_length,
            "first_sendable_lines": [line.to_json() for line in self.first_sendable_lines],
            "last_sendable_lines": [line.to_json() for line in self.last_sendable_lines],
            "streaming_mode": self.streaming_mode,
        }


@dataclass(frozen=True, slots=True, init=False)
class ReadinessResult:
    status: ReadinessStatus
    checks: tuple[SafetyCheck, ...]
    _pen_motion_assessment_document: str = dataclass_field(init=False, repr=False)

    def __init__(
        self,
        status: ReadinessStatus,
        checks: tuple[SafetyCheck, ...],
        pen_motion_assessment: JsonObject,
    ) -> None:
        immutable_checks = tuple(checks)
        object.__setattr__(self, "status", _readiness_status_from_safety_checks(immutable_checks))
        object.__setattr__(self, "checks", immutable_checks)
        object.__setattr__(
            self,
            "_pen_motion_assessment_document",
            _safety_json_document(pen_motion_assessment, "pen motion assessment"),
        )

    @property
    def pen_motion_assessment(self) -> JsonObject:
        return _load_safety_json_document(self._pen_motion_assessment_document)

    @property
    def allow_stream(self) -> bool:
        return self.status is ReadinessStatus.READY

    def to_json(self) -> JsonObject:
        blockers = [check.message for check in self.checks if check.severity is CheckSeverity.BLOCKER]
        warnings = [check.message for check in self.checks if check.severity is CheckSeverity.WARNING]
        return {
            "allow_stream": self.allow_stream,
            "blockers": cast(list[JsonValue], blockers),
            "warnings": cast(list[JsonValue], warnings),
            "pen_motion_assessment": self.pen_motion_assessment,
        }


def _gate_from_safety_checks(checks: tuple[SafetyCheck, ...]) -> StaticGate:
    if any(check.severity is CheckSeverity.BLOCKER for check in checks):
        return StaticGate.BLOCKED
    if any(check.severity is CheckSeverity.WARNING for check in checks):
        return StaticGate.REVIEW_WARNINGS
    return StaticGate.PASS_STATIC


def _readiness_status_from_safety_checks(checks: tuple[SafetyCheck, ...]) -> ReadinessStatus:
    if any(check.severity is CheckSeverity.BLOCKER for check in checks):
        return ReadinessStatus.BLOCKED
    return ReadinessStatus.READY


def _safety_json_document(value: Mapping[str, object], context: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise DrawingMachineError(
            ErrorPayload(
                code="GCODE_SAFETY_RESULT_INVALID",
                category=ErrorCategory.VALIDATION,
                message=f"{context} must contain finite JSON values",
                retryable=False,
                details={"field": context},
            )
        ) from error


def _load_safety_json_document(value: str) -> JsonObject:
    loaded: Any = json.loads(value)
    if not isinstance(loaded, dict):
        raise RuntimeError("stored G-code safety document is not an object")
    return cast(JsonObject, loaded)


def parse_machine_build_profile(profile: ProfileEnvelope) -> MachineBuildProfile:
    if (
        isinstance(profile.schema_version, bool)
        or not isinstance(profile.schema_version, int)
        or profile.schema_version != _SUPPORTED_SCHEMA_VERSION
    ):
        _invalid_profile("profile.schema_version must be 1", "profile.schema_version")
    profile_values = _object(profile.profile, "profile")
    for required in ("name", "planning", "gcode"):
        if required not in profile_values:
            _invalid_profile(f"profile has missing fields: {required}", "profile")
    _nonempty_string(profile_values["name"], "profile.name")
    try:
        parse_path_plan_config(profile)
    except DrawingMachineError as error:
        _invalid_profile(error.payload.message, cast(str, error.payload.details.get("field", "profile.planning")))
    gcode = _object(profile_values["gcode"], "profile.gcode")
    _require_exact_fields(gcode, _GCODE_FIELDS, "profile.gcode")

    result = MachineBuildProfile(
        hardware_canvas_width_mm=_positive(gcode["hardware_canvas_width_mm"], "profile.gcode.hardware_canvas_width_mm"),
        hardware_canvas_height_mm=_positive(
            gcode["hardware_canvas_height_mm"], "profile.gcode.hardware_canvas_height_mm"
        ),
        machine_width_mm=_positive(gcode["machine_width_mm"], "profile.gcode.machine_width_mm"),
        machine_height_mm=_positive(gcode["machine_height_mm"], "profile.gcode.machine_height_mm"),
        paper_center_x=_number(gcode["paper_center_x"], "profile.gcode.paper_center_x"),
        paper_center_y=_number(gcode["paper_center_y"], "profile.gcode.paper_center_y"),
        pen_up_z=_number(gcode["pen_up_z"], "profile.gcode.pen_up_z"),
        pen_down_z=_number(gcode["pen_down_z"], "profile.gcode.pen_down_z"),
        feed_travel=_positive(gcode["feed_travel"], "profile.gcode.feed_travel"),
        feed_draw=_positive(gcode["feed_draw"], "profile.gcode.feed_draw"),
        feed_pen_down=_positive(gcode["feed_pen_down"], "profile.gcode.feed_pen_down"),
        feed_pen_up=_positive(gcode["feed_pen_up"], "profile.gcode.feed_pen_up"),
        max_feed=_positive(gcode["max_feed"], "profile.gcode.max_feed"),
        work_coordinate=_fixed_string(gcode["work_coordinate"], "profile.gcode.work_coordinate", "G54"),
        align_mode=_fixed_string(gcode["align_mode"], "profile.gcode.align_mode", "center"),
        mirror_y=_fixed_true(gcode["mirror_y"], "profile.gcode.mirror_y"),
        safe_start=_fixed_true(gcode["safe_start"], "profile.gcode.safe_start"),
        path_mode=_fixed_string(gcode["path_mode"], "profile.gcode.path_mode", "stroke"),
    )
    return validate_machine_build_profile(result)


def validate_machine_build_profile(profile: MachineBuildProfile) -> MachineBuildProfile:
    for field in (
        "hardware_canvas_width_mm",
        "hardware_canvas_height_mm",
        "machine_width_mm",
        "machine_height_mm",
        "feed_travel",
        "feed_draw",
        "feed_pen_down",
        "feed_pen_up",
        "max_feed",
    ):
        _positive(getattr(profile, field), f"profile.gcode.{field}")
    for field in ("paper_center_x", "paper_center_y", "pen_up_z", "pen_down_z"):
        _number(getattr(profile, field), f"profile.gcode.{field}")
    _fixed_string(profile.work_coordinate, "profile.gcode.work_coordinate", "G54")
    _fixed_string(profile.align_mode, "profile.gcode.align_mode", "center")
    _fixed_true(profile.mirror_y, "profile.gcode.mirror_y")
    _fixed_true(profile.safe_start, "profile.gcode.safe_start")
    _fixed_string(profile.path_mode, "profile.gcode.path_mode", "stroke")
    for field, feed in (
        ("feed_travel", profile.feed_travel),
        ("feed_draw", profile.feed_draw),
        ("feed_pen_down", profile.feed_pen_down),
        ("feed_pen_up", profile.feed_pen_up),
    ):
        if feed > profile.max_feed:
            _invalid_profile(f"profile.gcode.{field} must not exceed max_feed", f"profile.gcode.{field}")
    if profile.pen_up_z <= profile.pen_down_z:
        _invalid_profile("profile.gcode.pen_up_z must be greater than pen_down_z", "profile.gcode.pen_up_z")
    if profile.hardware_canvas_width_mm > profile.machine_width_mm:
        _invalid_profile(
            "profile.gcode.hardware_canvas_width_mm must not exceed machine_width_mm",
            "profile.gcode.hardware_canvas_width_mm",
        )
    if profile.hardware_canvas_height_mm > profile.machine_height_mm:
        _invalid_profile(
            "profile.gcode.hardware_canvas_height_mm must not exceed machine_height_mm",
            "profile.gcode.hardware_canvas_height_mm",
        )
    if not 0.0 <= profile.paper_center_x <= profile.machine_width_mm:
        _invalid_profile(
            "profile.gcode.paper_center_x must be within machine X bounds",
            "profile.gcode.paper_center_x",
        )
    if not 0.0 <= profile.paper_center_y <= profile.machine_height_mm:
        _invalid_profile(
            "profile.gcode.paper_center_y must be within machine Y bounds",
            "profile.gcode.paper_center_y",
        )
    return profile


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid_profile(f"{field} must be an object", field)
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            _invalid_profile(f"{field} contains a non-string key", field)
        result[key] = item
    return result


def _require_exact_fields(values: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    unknown = sorted(set(values) - expected)
    if unknown:
        _invalid_profile(
            f"{field} has unknown fields: {', '.join(unknown)}",
            field,
            {"unknown_fields": cast(list[JsonValue], unknown)},
        )
    missing = sorted(expected - set(values))
    if missing:
        _invalid_profile(
            f"{field} has missing fields: {', '.join(missing)}",
            field,
            {"missing_fields": cast(list[JsonValue], missing)},
        )


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _invalid_profile(f"{field} must be a non-empty string", field)
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _invalid_profile(f"{field} must be a number", field)
    try:
        result = float(value)
    except OverflowError:
        _invalid_profile(f"{field} must be finite", field)
    if not math.isfinite(result):
        _invalid_profile(f"{field} must be finite", field)
    return result


def _positive(value: object, field: str) -> float:
    result = _number(value, field)
    if result <= 0.0:
        _invalid_profile(f"{field} must be greater than zero", field)
    return result


def _fixed_string(value: object, field: str, expected: str) -> str:
    if not isinstance(value, str) or value != expected:
        _invalid_profile(f"{field} must be {expected!r}", field)
    return value


def _fixed_true(value: object, field: str) -> bool:
    if not isinstance(value, bool) or value is not True:
        _invalid_profile(f"{field} must be true", field)
    return value


def _invalid_profile(
    message: str,
    field: str,
    extra: Mapping[str, JsonValue] | None = None,
) -> NoReturn:
    details: dict[str, JsonValue] = {"field": field}
    details.update(extra or {})
    raise DrawingMachineError(
        ErrorPayload(
            code="MACHINE_BUILD_PROFILE_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            retryable=False,
            details=details,
        )
    )


__all__ = [
    "CheckSeverity",
    "GcodeBuildResult",
    "MachineBuildProfile",
    "PreviewResult",
    "ReadinessResult",
    "ReadinessStatus",
    "SafetyCheck",
    "SendLine",
    "SendPlan",
    "StaticCheckOptions",
    "StaticCheckResult",
    "StaticGate",
    "parse_machine_build_profile",
    "validate_machine_build_profile",
]
