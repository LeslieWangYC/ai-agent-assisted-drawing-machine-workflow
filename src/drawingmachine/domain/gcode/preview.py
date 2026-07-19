from __future__ import annotations

import math
import re
from typing import NoReturn, cast

from drawingmachine.domain.gcode.models import MachineBuildProfile, PreviewResult
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject

_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?\d*\.?\d+)")
_Segment = tuple[float, float, float, float]


def render_preview(gcode: str, profile: MachineBuildProfile) -> PreviewResult:
    parsed = _parse_gcode(gcode, profile)
    svg = _render_svg(parsed)
    report = _build_report(parsed)
    return PreviewResult(svg=svg, report=report)


def _parse_gcode(gcode: str, profile: MachineBuildProfile) -> dict[str, object]:
    position = {"X": profile.paper_center_x, "Y": profile.paper_center_y, "Z": profile.pen_up_z}
    draw_segments: list[_Segment] = []
    travel_segments: list[_Segment] = []
    command_count = 0
    for raw_line in gcode.splitlines():
        line = re.sub(r"\([^)]*\)", "", raw_line).split(";", 1)[0].strip()
        if not line:
            continue
        words = _parse_words(line)
        if "G" not in words or int(words["G"]) not in {0, 1}:
            continue
        command = int(words["G"])
        command_count += 1
        start = dict(position)
        for axis in ("X", "Y", "Z"):
            if axis in words:
                position[axis] = words[axis]
        if "X" not in words and "Y" not in words:
            continue
        segment = (start["X"], start["Y"], position["X"], position["Y"])
        if start["Z"] <= profile.pen_down_z and position["Z"] <= profile.pen_down_z and command == 1:
            draw_segments.append(segment)
        else:
            travel_segments.append(segment)
    return {
        "draw_segments": draw_segments,
        "travel_segments": travel_segments,
        "bounds": _segment_bounds(draw_segments + travel_segments),
        "command_count": command_count,
    }


def _segment_bounds(segments: list[_Segment]) -> dict[str, float]:
    if not segments:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 1.0, "max_y": 1.0}
    xs = [value for segment in segments for value in (segment[0], segment[2])]
    ys = [value for segment in segments for value in (segment[1], segment[3])]
    return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}


def _render_svg(parsed: dict[str, object]) -> str:
    bounds = cast(dict[str, float], parsed["bounds"])
    margin = 5.0
    min_x = bounds["min_x"] - margin
    min_y = bounds["min_y"] - margin
    width = max(1.0, bounds["max_x"] - bounds["min_x"] + margin * 2)
    height = max(1.0, bounds["max_y"] - bounds["min_y"] + margin * 2)
    if not all(math.isfinite(value) for value in (min_x, min_y, width, height)):
        _invalid_preview("Preview geometry exceeds finite numeric range.")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" viewBox="{min_x:.3f} {min_y:.3f} {width:.3f} {height:.3f}">',
        '<rect x="-10000" y="-10000" width="20000" height="20000" fill="white"/>',
        '<g id="travel" fill="none" stroke="#c7c7c7" stroke-width="0.150" stroke-linecap="round" stroke-linejoin="round" opacity="0.65">',
    ]
    lines.extend(_segment_svg(segment) for segment in cast(list[_Segment], parsed["travel_segments"]))
    lines.extend(
        [
            "</g>",
            '<g id="draw" fill="none" stroke="#000000" stroke-width="0.350" stroke-linecap="round" stroke-linejoin="round">',
        ]
    )
    lines.extend(_segment_svg(segment) for segment in cast(list[_Segment], parsed["draw_segments"]))
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


def _segment_svg(segment: _Segment) -> str:
    x1, y1, x2, y2 = segment
    return f'<path d="M{x1:.4f},{y1:.4f} L{x2:.4f},{y2:.4f}" />'


def _build_report(parsed: dict[str, object]) -> JsonObject:
    draw_segments = cast(list[_Segment], parsed["draw_segments"])
    travel_segments = cast(list[_Segment], parsed["travel_segments"])
    return {
        "input": "drawing.gcode",
        "svg": "gcode_preview.svg",
        "metrics": {
            "draw_segment_count": len(draw_segments),
            "travel_segment_count": len(travel_segments),
            "draw_length_mm": round(_total_length(draw_segments), 3),
            "travel_length_mm": round(_total_length(travel_segments), 3),
            "bounds": cast(JsonObject, parsed["bounds"]),
            "motion_command_count": cast(int, parsed["command_count"]),
        },
    }


def _total_length(segments: list[_Segment]) -> float:
    total = sum(math.dist((x1, y1), (x2, y2)) for x1, y1, x2, y2 in segments)
    if not math.isfinite(total):
        _invalid_preview("Preview geometry exceeds finite numeric range.")
    return total


def _parse_words(line: str) -> dict[str, float]:
    words: dict[str, float] = {}
    for letter, raw_value in _WORD_RE.findall(line):
        try:
            value = float(raw_value)
        except (OverflowError, ValueError):
            _invalid_preview("G-code preview contains an invalid numeric word.")
        if not math.isfinite(value):
            _invalid_preview("G-code preview contains a non-finite numeric word.")
        words[letter.upper()] = value
    return words


def _invalid_preview(message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="GCODE_PREVIEW_INVALID",
            category=ErrorCategory.VALIDATION,
            message=message,
            retryable=False,
            details={"field": "gcode"},
        )
    )


__all__ = ["render_preview"]
