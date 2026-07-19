from __future__ import annotations

import hashlib
import re

from drawingmachine.domain.gcode.models import SendLine, SendPlan
from drawingmachine.domain.gcode.static_check import strip_comments

_PATH_MODE_RE = re.compile(r"^;\s*Path mode:\s*([^\s(]+)", re.MULTILINE)


def build_send_plan(gcode: str) -> SendPlan:
    raw_lines = gcode.splitlines()
    sendable: list[SendLine] = []
    comment_lines = 0
    blank_lines = 0
    max_send_line_length = 0
    for line_no, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            blank_lines += 1
            continue
        command = strip_comments(raw_line).strip()
        if not command:
            comment_lines += 1
            continue
        max_send_line_length = max(max_send_line_length, len(command))
        sendable.append(SendLine(line_no, command))
    match = _PATH_MODE_RE.search(gcode)
    candidate_source = match.group(1) if match is not None else "unknown"
    return SendPlan(
        raw_line_count=len(raw_lines),
        blank_line_count=blank_lines,
        comment_only_line_count=comment_lines,
        sendable_line_count=len(sendable),
        estimated_serial_bytes=sum(len(item.command) + 1 for item in sendable),
        max_send_line_length=max_send_line_length,
        first_sendable_lines=tuple(sendable[:8]),
        last_sendable_lines=tuple(sendable[-8:]),
        streaming_mode="conservative_send_line_wait_ok",
        actual_sha256=hashlib.sha256(gcode.encode("utf-8")).hexdigest(),
        candidate_source=candidate_source,
    )


__all__ = ["build_send_plan"]
