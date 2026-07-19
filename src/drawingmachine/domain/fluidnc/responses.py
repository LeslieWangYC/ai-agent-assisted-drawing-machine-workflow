from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from drawingmachine.domain.fluidnc.status import (
    FluidNCState,
    _has_forbidden_line_character,
    parse_controller_status,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

_MAX_RESPONSE_LINES = 256
_MAX_RESPONSE_LINE_BYTES = 4096
_MAX_RESPONSE_BYTES = 64 * 1024


class ResponseKind(StrEnum):
    ACK = "ack"
    ERROR = "error"
    ALARM = "alarm"
    TIMEOUT = "timeout"
    STATUS = "status"
    INCOMPLETE = "incomplete"


class WaitIdleKind(StrEnum):
    IDLE = "idle"
    WAIT = "wait"
    ALARM = "alarm"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class CommandResponseDecision:
    kind: ResponseKind
    acknowledged: bool
    stop_before_next_command: bool
    response_line_count: int


@dataclass(frozen=True, slots=True)
class WaitIdleDecision:
    kind: WaitIdleKind
    last_state: FluidNCState | None


def classify_command_response(
    response_lines: object,
    *,
    expect_ok: object,
    timed_out: object,
) -> CommandResponseDecision:
    lines = _lines(response_lines)
    expected = _flag(expect_ok, "expect_ok")
    timeout = _flag(timed_out, "timed_out")
    lowered = tuple(line.casefold() for line in lines)
    if any(line.startswith("alarm") for line in lowered):
        return _command_decision(ResponseKind.ALARM, len(lines))
    if any(line.startswith("error") for line in lowered):
        return _command_decision(ResponseKind.ERROR, len(lines))
    if any(line == "ok" for line in lowered):
        return CommandResponseDecision(ResponseKind.ACK, True, False, len(lines))
    if not expected and any(line.startswith("<") and line.endswith(">") for line in lines):
        for line in lines:
            if line.startswith("<") and line.endswith(">"):
                parse_controller_status(line)
        return CommandResponseDecision(ResponseKind.STATUS, True, False, len(lines))
    if timeout:
        return _command_decision(ResponseKind.TIMEOUT, len(lines))
    return _command_decision(ResponseKind.INCOMPLETE, len(lines))


def classify_wait_for_idle(response_lines: object, *, timed_out: object) -> WaitIdleDecision:
    lines = _lines(response_lines)
    timeout = _flag(timed_out, "timed_out")
    lowered = tuple(line.casefold() for line in lines)
    if any(line.startswith("alarm") for line in lowered):
        return WaitIdleDecision(WaitIdleKind.ALARM, FluidNCState.ALARM)
    if any(line.startswith("error") for line in lowered):
        return WaitIdleDecision(WaitIdleKind.ERROR, None)
    statuses = tuple(parse_controller_status(line) for line in lines if line.startswith("<") and line.endswith(">"))
    last_state = statuses[-1].state if statuses else None
    if last_state is FluidNCState.ALARM:
        return WaitIdleDecision(WaitIdleKind.ALARM, last_state)
    if last_state is FluidNCState.IDLE:
        return WaitIdleDecision(WaitIdleKind.IDLE, last_state)
    if timeout:
        return WaitIdleDecision(WaitIdleKind.TIMEOUT, last_state)
    return WaitIdleDecision(WaitIdleKind.WAIT, last_state)


def _command_decision(kind: ResponseKind, line_count: int) -> CommandResponseDecision:
    return CommandResponseDecision(kind, False, True, line_count)


def _flag(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(f"{field} must be a boolean", field)
    return value


def _lines(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_RESPONSE_LINES:
        _invalid("controller responses must be a bounded exact tuple", "response_lines")
    accepted: list[str] = []
    total = 0
    for item in value:
        if type(item) is not str:
            _invalid("controller responses must contain exact strings", "response_lines")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError:
            _invalid("controller responses contain invalid Unicode", "response_lines")
        total += len(encoded)
        if (
            not item
            or len(encoded) > _MAX_RESPONSE_LINE_BYTES
            or total > _MAX_RESPONSE_BYTES
            or _has_forbidden_line_character(item)
        ):
            _invalid("controller responses contain an invalid line", "response_lines")
        accepted.append(item)
    return tuple(accepted)


def _invalid(message: str, field: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="FLUIDNC_RESPONSE_INVALID",
            category=ErrorCategory.HARDWARE,
            message=message,
            retryable=False,
            details={"field": field},
        )
    )


__all__ = [
    "CommandResponseDecision",
    "ResponseKind",
    "WaitIdleDecision",
    "WaitIdleKind",
    "classify_command_response",
    "classify_wait_for_idle",
]
