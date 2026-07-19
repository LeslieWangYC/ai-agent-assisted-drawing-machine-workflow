from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import NoReturn, cast

from drawingmachine.domain.gcode.models import SendPlan
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.static_check import strip_comments
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

MAX_GCODE_BYTES = 16 * 1024 * 1024
MAX_GCODE_LINES = 1_000_000
MAX_GCODE_LINE_BYTES = 4096
MAX_STREAM_COMMANDS = 500_000
MAX_SERIAL_BYTES = 32 * 1024 * 1024

_SUMMARY_FIELDS = frozenset(
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
)
_SEND_LINE_FIELDS = frozenset({"source_line", "command"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_INIT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class StreamCommand:
    source_line: int
    command: str
    serial_bytes: int

    def __init__(self, token: object, *, source_line: int, command: str, serial_bytes: int) -> None:
        if token is not _INIT_TOKEN:
            raise TypeError("StreamCommand values are created only by stream-program validation")
        object.__setattr__(self, "source_line", source_line)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "serial_bytes", serial_bytes)


@dataclass(frozen=True, slots=True, init=False)
class ValidatedStreamProgram:
    gcode_sha256: str
    source_size_bytes: int
    raw_line_count: int
    serial_bytes: int
    commands: tuple[StreamCommand, ...]
    send_plan: SendPlan

    def __init__(
        self,
        token: object,
        *,
        gcode_sha256: str,
        source_size_bytes: int,
        raw_line_count: int,
        serial_bytes: int,
        commands: tuple[StreamCommand, ...],
        send_plan: SendPlan,
    ) -> None:
        if token is not _INIT_TOKEN:
            raise TypeError("ValidatedStreamProgram values require exact byte validation")
        object.__setattr__(self, "gcode_sha256", gcode_sha256)
        object.__setattr__(self, "source_size_bytes", source_size_bytes)
        object.__setattr__(self, "raw_line_count", raw_line_count)
        object.__setattr__(self, "serial_bytes", serial_bytes)
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "send_plan", send_plan)


@dataclass(frozen=True, slots=True)
class _ScannedProgram:
    raw_line_count: int
    commands: tuple[StreamCommand, ...]
    serial_bytes: int


def build_validated_stream_program(
    exact_gcode_bytes: object,
    frozen_send_plan_summary: object,
    frozen_gcode_sha256: object,
) -> ValidatedStreamProgram:
    if type(exact_gcode_bytes) is not bytes:
        _invalid("G-code input must be exact bytes", "gcode_bytes")
    source = exact_gcode_bytes
    if not source or len(source) > MAX_GCODE_BYTES:
        _invalid("G-code input violates the byte bound", "gcode_bytes")
    expected_digest = _validate_digest(frozen_gcode_sha256)
    actual_digest = hashlib.sha256(source).hexdigest()
    if actual_digest != expected_digest:
        _invalid("G-code bytes do not match the frozen artifact digest", "gcode_sha256")
    scanned = _scan_source(source)
    text = source.decode("utf-8")
    send_plan = build_send_plan(text)
    if (
        scanned.raw_line_count != send_plan.raw_line_count
        or len(scanned.commands) != send_plan.sendable_line_count
        or scanned.serial_bytes != send_plan.estimated_serial_bytes
    ):
        _invalid("G-code command reconstruction disagrees with the send plan", "gcode_bytes")

    expected = _validate_summary(frozen_send_plan_summary)
    if send_plan.to_json() != expected:
        _invalid("recomputed send plan does not exactly match the frozen Package B summary", "send_plan")
    return ValidatedStreamProgram(
        _INIT_TOKEN,
        gcode_sha256=actual_digest,
        source_size_bytes=len(source),
        raw_line_count=scanned.raw_line_count,
        serial_bytes=scanned.serial_bytes,
        commands=scanned.commands,
        send_plan=send_plan,
    )


def _scan_source(source: bytes) -> _ScannedProgram:
    commands: list[StreamCommand] = []
    serial_bytes = 0
    line_start = 0
    source_line = 0
    while line_start < len(source):
        newline = source.find(b"\n", line_start)
        line_end = len(source) if newline < 0 else newline
        has_crlf = newline >= 0 and line_end > line_start and source[line_end - 1] == 13
        content_end = line_end - 1 if has_crlf else line_end
        if source.find(b"\r", line_start, content_end) >= 0 or (
            newline < 0 and line_end > line_start and source[line_end - 1] == 13
        ):
            _invalid("G-code input contains a lone carriage return", "gcode_bytes")
        source_line += 1
        if source_line > MAX_GCODE_LINES:
            _invalid("G-code input violates the line-count bound", "gcode_bytes")
        if content_end - line_start > MAX_GCODE_LINE_BYTES:
            _invalid("G-code input violates the per-line byte bound", "gcode_bytes")
        raw_line_bytes = source[line_start:content_end]
        try:
            raw_line = raw_line_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _invalid("G-code input is not strict UTF-8", "gcode_bytes")
        if raw_line.startswith("\ufeff") or _has_forbidden_source_character(raw_line):
            _invalid("G-code input contains a forbidden control or separator", "gcode_bytes")
        command = strip_comments(raw_line).strip()
        if command:
            try:
                encoded = command.encode("ascii", errors="strict")
            except UnicodeEncodeError:
                _invalid("sendable G-code commands must be ASCII", "gcode_bytes")
            if len(commands) >= MAX_STREAM_COMMANDS:
                _invalid("G-code input violates the command-count bound", "gcode_bytes")
            command_serial_bytes = len(encoded) + 1
            if serial_bytes + command_serial_bytes > MAX_SERIAL_BYTES:
                _invalid("G-code input violates the serial-byte bound", "gcode_bytes")
            commands.append(
                StreamCommand(
                    _INIT_TOKEN,
                    source_line=source_line,
                    command=command,
                    serial_bytes=command_serial_bytes,
                )
            )
            serial_bytes += command_serial_bytes
        if newline < 0:
            break
        line_start = newline + 1
    if not commands:
        _invalid("G-code input contains no sendable commands", "gcode_bytes")
    return _ScannedProgram(source_line, tuple(commands), serial_bytes)


def _has_forbidden_source_character(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _validate_digest(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _invalid("frozen G-code digest must be a canonical SHA-256", "gcode_sha256")
    return value


def _validate_summary(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _invalid("frozen send plan must be an exact plain object", "send_plan")
    accepted = cast(dict[object, object], value)
    if any(type(key) is not str for key in accepted) or set(accepted) != _SUMMARY_FIELDS:
        _invalid("frozen send plan fields are invalid", "send_plan")
    for field in (
        "raw_line_count",
        "blank_line_count",
        "comment_only_line_count",
        "sendable_line_count",
        "estimated_serial_bytes",
        "max_send_line_length",
    ):
        item = accepted[field]
        if type(item) is not int or item < 0:
            _invalid("frozen send plan counters are invalid", "send_plan")
    if type(accepted["streaming_mode"]) is not str or accepted["streaming_mode"] != "conservative_send_line_wait_ok":
        _invalid("frozen send plan streaming mode is invalid", "send_plan")
    _validate_samples(accepted["first_sendable_lines"])
    _validate_samples(accepted["last_sendable_lines"])
    return cast(dict[str, object], accepted)


def _validate_samples(value: object) -> None:
    if type(value) is not list or len(value) > 8:
        _invalid("frozen send plan samples are invalid", "send_plan")
    for item in cast(list[object], value):
        if type(item) is not dict:
            _invalid("frozen send plan sample is invalid", "send_plan")
        sample = cast(dict[object, object], item)
        if any(type(key) is not str for key in sample) or set(sample) != _SEND_LINE_FIELDS:
            _invalid("frozen send plan sample fields are invalid", "send_plan")
        if type(sample["source_line"]) is not int or sample["source_line"] < 1:
            _invalid("frozen send plan source line is invalid", "send_plan")
        command = sample["command"]
        if type(command) is not str or not command:
            _invalid("frozen send plan command is invalid", "send_plan")
        try:
            encoded = command.encode("ascii")
        except UnicodeEncodeError:
            _invalid("frozen send plan command is invalid", "send_plan")
        if len(encoded) > MAX_GCODE_LINE_BYTES:
            _invalid("frozen send plan command is invalid", "send_plan")


def _invalid(message: str, field: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="STREAM_PROGRAM_INVALID",
            category=ErrorCategory.VALIDATION,
            message=message,
            retryable=False,
            details={"field": field},
        )
    )


__all__ = [
    "MAX_GCODE_BYTES",
    "MAX_GCODE_LINES",
    "MAX_GCODE_LINE_BYTES",
    "MAX_SERIAL_BYTES",
    "MAX_STREAM_COMMANDS",
    "StreamCommand",
    "ValidatedStreamProgram",
    "build_validated_stream_program",
]
