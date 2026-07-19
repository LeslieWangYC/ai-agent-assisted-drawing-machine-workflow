from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

Vector3 = tuple[float, float, float]

_MAX_CONTROLLER_LINE_BYTES = 4096
_MAX_RESPONSE_LINES = 256
_MAX_RESPONSE_BYTES = 64 * 1024
_OFFSET_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9]{0,15}):([^\]]+)\]$")
_OTHER_STATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}(?::[0-9]{1,3})?\Z")


class FluidNCState(StrEnum):
    IDLE = "Idle"
    RUN = "Run"
    HOLD = "Hold"
    DOOR = "Door"
    ALARM = "Alarm"
    HOME = "Home"
    CHECK = "Check"
    JOG = "Jog"
    SLEEP = "Sleep"
    OTHER = "Other"


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    state: FluidNCState
    state_text: str
    mpos: Vector3 | None
    wpos: Vector3 | None
    wco: Vector3 | None
    status_line: str

    def positions_json(self) -> JsonObject:
        result: JsonObject = {}
        if self.mpos is not None:
            result["MPos"] = list(self.mpos)
        if self.wpos is not None:
            result["WPos"] = list(self.wpos)
        if self.wco is not None:
            result["WCO"] = list(self.wco)
        return result


@dataclass(frozen=True, slots=True)
class WorkOffset:
    name: str
    position: Vector3


@dataclass(frozen=True, slots=True)
class PreflightSnapshot:
    status: ControllerStatus
    parser_state: tuple[str, ...]
    offset_lines: tuple[str, ...]
    offsets: tuple[WorkOffset, ...]
    errors: tuple[str, ...]
    missing_ok_commands: tuple[str, ...]

    @property
    def g54(self) -> Vector3 | None:
        return next((item.position for item in self.offsets if item.name == "G54"), None)

    @property
    def computed_wpos_from_mpos_g54(self) -> Vector3 | None:
        if self.status.mpos is None or self.g54 is None:
            return None
        return tuple(round(left - right, 3) for left, right in zip(self.status.mpos, self.g54, strict=True))  # type: ignore[return-value]

    def to_json(self) -> JsonObject:
        parsed_offsets: JsonObject = {item.name: list(item.position) for item in self.offsets}
        return {
            "status_line": self.status.status_line,
            "state": self.status.state_text,
            "status_positions": self.status.positions_json(),
            "parser_state": list(self.parser_state),
            "offsets": list(self.offset_lines),
            "parsed_offsets": parsed_offsets,
            "has_g54_offset": self.g54 is not None,
            "computed_wpos_from_mpos_g54": (
                None if self.computed_wpos_from_mpos_g54 is None else list(self.computed_wpos_from_mpos_g54)
            ),
            "errors": list(self.errors),
            "missing_ok_commands": list(self.missing_ok_commands),
        }


def parse_controller_status(raw_line: object) -> ControllerStatus:
    line = _line(raw_line, "controller status")
    if not line.startswith("<") or not line.endswith(">") or line.count("<") != 1 or line.count(">") != 1:
        _invalid("controller status is invalid", "status_line")
    parts = line[1:-1].split("|")
    if not parts or not parts[0]:
        _invalid("controller status is invalid", "status_line")
    state_text = parts[0]
    state = _state(state_text)
    positions: dict[str, Vector3] = {}
    for part in parts[1:]:
        if ":" not in part:
            _invalid("controller status is invalid", "status_line")
        name, raw_value = part.split(":", 1)
        if name not in {"MPos", "WPos", "WCO"}:
            continue
        if name in positions:
            _invalid("controller status contains a duplicate position", "status_line")
        positions[name] = _vector(raw_value, "controller status position")
    return ControllerStatus(
        state=state,
        state_text=state_text,
        mpos=positions.get("MPos"),
        wpos=positions.get("WPos"),
        wco=positions.get("WCO"),
        status_line=line,
    )


def parse_offsets(raw_lines: object) -> tuple[WorkOffset, ...]:
    lines = _lines(raw_lines, "offset responses")
    offsets: list[WorkOffset] = []
    names: set[str] = set()
    for line in lines:
        match = _OFFSET_RE.fullmatch(line.strip())
        if match is None:
            continue
        name = match.group(1).upper()
        if name in names:
            _invalid("offset responses contain a duplicate offset", "offset_lines")
        names.add(name)
        offsets.append(WorkOffset(name=name, position=_vector(match.group(2), "work offset")))
    return tuple(offsets)


def parse_preflight_snapshot(
    *,
    status_line: object,
    parser_state: object,
    offset_lines: object,
    errors: object,
    missing_ok_commands: object,
) -> PreflightSnapshot:
    accepted_parser = _lines(parser_state, "parser-state responses")
    accepted_offsets = _lines(offset_lines, "offset responses")
    accepted_errors = _lines(errors, "preflight errors")
    accepted_missing = _lines(missing_ok_commands, "missing-ok commands")
    return PreflightSnapshot(
        status=parse_controller_status(status_line),
        parser_state=accepted_parser,
        offset_lines=accepted_offsets,
        offsets=parse_offsets(accepted_offsets),
        errors=accepted_errors,
        missing_ok_commands=accepted_missing,
    )


def validate_vector(value: object, field: str) -> Vector3:
    if type(value) is not tuple or len(value) != 3:
        _invalid(f"{field} must be an exact three-number tuple", field)
    accepted: list[float] = []
    for item in value:
        if type(item) not in {int, float}:
            _invalid(f"{field} must contain finite numbers", field)
        try:
            number = float(cast(int | float, item))
        except OverflowError:
            _invalid(f"{field} must contain finite numbers", field)
        if not math.isfinite(number):
            _invalid(f"{field} must contain finite numbers", field)
        accepted.append(number)
    return accepted[0], accepted[1], accepted[2]


def validate_positive_finite(value: object, field: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{field} must be a positive finite number", field)
    try:
        result = float(cast(int | float, value))
    except OverflowError:
        _invalid(f"{field} must be a positive finite number", field)
    if not math.isfinite(result) or result <= 0.0:
        _invalid(f"{field} must be a positive finite number", field)
    return result


def _state(value: str) -> FluidNCState:
    if value in {"Hold:0", "Hold:1"}:
        return FluidNCState.HOLD
    if value in {"Door:0", "Door:1", "Door:2", "Door:3"}:
        return FluidNCState.DOOR
    for state in FluidNCState:
        if state is not FluidNCState.OTHER and state.value == value:
            return state
    known_prefix = value.split(":", 1)[0]
    if any(state is not FluidNCState.OTHER and state.value == known_prefix for state in FluidNCState):
        _invalid("controller status state is invalid", "status_line")
    if _OTHER_STATE_RE.fullmatch(value) is None:
        _invalid("controller status state is invalid", "status_line")
    return FluidNCState.OTHER


def _vector(raw_value: str, field: str) -> Vector3:
    parts = raw_value.split(",")
    if len(parts) != 3:
        _invalid(f"{field} must contain exactly three finite values", field)
    values: list[float] = []
    for part in parts:
        try:
            number = float(part.strip())
        except (OverflowError, ValueError):
            _invalid(f"{field} must contain exactly three finite values", field)
        if not math.isfinite(number):
            _invalid(f"{field} must contain exactly three finite values", field)
        values.append(number)
    return values[0], values[1], values[2]


def _lines(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_RESPONSE_LINES:
        _invalid(f"{field} must be a bounded exact tuple", field)
    accepted: list[str] = []
    total_bytes = 0
    for item in value:
        line = _line(item, field)
        total_bytes += len(line.encode("utf-8"))
        if total_bytes > _MAX_RESPONSE_BYTES:
            _invalid(f"{field} exceeds its byte limit", field)
        accepted.append(line)
    return tuple(accepted)


def _line(value: object, field: str) -> str:
    if type(value) is not str:
        _invalid(f"{field} must contain exact strings", field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{field} contains invalid Unicode", field)
    if not value or len(encoded) > _MAX_CONTROLLER_LINE_BYTES or _has_forbidden_line_character(value):
        _invalid(f"{field} contains an invalid line", field)
    return value


def _has_forbidden_line_character(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _invalid(message: str, field: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="FLUIDNC_STATUS_INVALID",
            category=ErrorCategory.HARDWARE,
            message=message,
            retryable=False,
            details={"field": field},
        )
    )


__all__ = [
    "ControllerStatus",
    "FluidNCState",
    "PreflightSnapshot",
    "Vector3",
    "WorkOffset",
    "parse_controller_status",
    "parse_offsets",
    "parse_preflight_snapshot",
]
