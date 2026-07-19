from __future__ import annotations

import hashlib
import math
import re
from typing import cast

from drawingmachine.domain.gcode.models import (
    CheckSeverity,
    SafetyCheck,
    StaticCheckOptions,
    StaticCheckResult,
    StaticGate,
)
from drawingmachine.json_types import JsonObject

_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?\d*\.?\d+)")
_ALLOWED_WORDS = frozenset({"G", "X", "Y", "Z", "F"})
_Segment = tuple[float, float, float, float]


def analyze_gcode(gcode: str, options: StaticCheckOptions) -> StaticCheckResult:
    raw_lines = gcode.splitlines()
    state = {
        "X": options.start_x,
        "Y": options.start_y,
        "Z": options.start_z if options.start_z is not None else options.pen_up_z,
    }
    active_motion: int | None = None
    draw_segments: list[_Segment] = []
    has_positive_finite_draw_geometry = False
    travel_segments: list[_Segment] = []
    path_lengths: list[float] = []
    current_draw_length = 0.0
    in_draw_path = False
    pen_lifts = 0
    pen_downs = 0
    max_feed_seen = 0.0
    command_count = 0
    checks: list[SafetyCheck] = []
    saw_units = False
    saw_absolute = False
    saw_work_coordinate = False
    saw_initial_pen_up = False
    unsafe_motion_before_initial_pen_up = False
    saw_motion = False
    end_marker_line: int | None = None
    sendable_after_end_marker = False
    last_sendable_line: int | None = None
    last_explicit_pen_up_line: int | None = None

    for line_no, raw_line in enumerate(raw_lines, start=1):
        if raw_line.strip() == "; End":
            end_marker_line = line_no
        line = strip_comments(raw_line).strip()
        if not line:
            continue
        if end_marker_line is not None:
            sendable_after_end_marker = True
        last_sendable_line = line_no
        words, issues = parse_words(line)
        malformed = sorted(issue for issue in issues if issue == "MALFORMED" or issue.startswith("DUPLICATE_"))
        unknown_words = sorted(issue for issue in issues if issue not in malformed)
        if malformed:
            checks.append(
                SafetyCheck(
                    CheckSeverity.BLOCKER, "malformed_line", f"Line {line_no}: malformed G-code words {malformed}."
                )
            )
            continue
        if unknown_words:
            if any(word not in {"M", "S"} for word in unknown_words):
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "malformed_line",
                        f"Line {line_no}: malformed or unsupported words {unknown_words}.",
                    )
                )
            else:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "unknown_word",
                        f"Line {line_no}: unsupported words {unknown_words}.",
                    )
                )
            continue

        if "G" in words:
            g_value_float = words["G"]
            if not g_value_float.is_integer():
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER, "malformed_line", f"Line {line_no}: fractional G code is invalid."
                    )
                )
                continue
            g_value = int(g_value_float)
            if g_value == 20:
                checks.append(
                    SafetyCheck(CheckSeverity.BLOCKER, "unsafe_units", f"Line {line_no}: inch units are forbidden.")
                )
                continue
            if g_value == 21:
                saw_units = not saw_motion
            elif g_value == 90:
                saw_absolute = not saw_motion
            elif g_value == 91:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "unsafe_coordinate_mode",
                        f"Line {line_no}: incremental coordinates are forbidden.",
                    )
                )
                continue
            elif g_value == 54:
                saw_work_coordinate = not saw_motion
            elif g_value in {55, 56, 57, 58, 59}:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "unsafe_work_coordinate",
                        f"Line {line_no}: G{g_value} is not the required G54 work coordinate.",
                    )
                )
                continue
            elif g_value in {0, 1}:
                saw_motion = True
                active_motion = g_value
            else:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "unsupported_g_command",
                        f"Line {line_no}: G{g_value} is not allowed.",
                    )
                )
                continue

        if "F" in words:
            feed = words["F"]
            if not math.isfinite(feed) or feed <= 0.0:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER, "malformed_line", f"Line {line_no}: feed must be finite and positive."
                    )
                )
                continue
            max_feed_seen = max(max_feed_seen, feed)
            if feed > options.max_feed:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "feed_exceeds_limit",
                        f"Line {line_no}: feed {feed} exceeds {options.max_feed}.",
                    )
                )

        if active_motion not in {0, 1}:
            axis_words = tuple(axis for axis in ("X", "Y", "Z") if axis in words)
            if axis_words:
                unsafe_motion_before_initial_pen_up = True
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "axis_without_local_motion",
                        f"Line {line_no}: axis words {list(axis_words)} require a prior file-local G0 or G1.",
                    )
                )
            continue

        start = dict(state)
        invalid_axis = False
        for axis in ("X", "Y", "Z"):
            if axis in words:
                value = words[axis]
                if not math.isfinite(value):
                    checks.append(
                        SafetyCheck(
                            CheckSeverity.BLOCKER,
                            "malformed_line",
                            f"Line {line_no}: {axis} must be finite.",
                        )
                    )
                    invalid_axis = True
                    break
                state[axis] = value
        if invalid_axis:
            continue

        z_changed = "Z" in words
        xy_changed = "X" in words or "Y" in words
        is_pen_up = z_changed and _near(state["Z"], options.pen_up_z, options.z_tolerance)
        if not saw_initial_pen_up and (xy_changed or (z_changed and not is_pen_up)):
            unsafe_motion_before_initial_pen_up = True
        if z_changed:
            if is_pen_up:
                last_explicit_pen_up_line = line_no
                if (
                    not saw_initial_pen_up
                    and not unsafe_motion_before_initial_pen_up
                    and not xy_changed
                    and saw_units
                    and saw_absolute
                    and saw_work_coordinate
                ):
                    saw_initial_pen_up = True
                if in_draw_path:
                    path_lengths.append(current_draw_length)
                    current_draw_length = 0.0
                    in_draw_path = False
                pen_lifts += 1
            elif _near(state["Z"], options.pen_down_z, options.z_tolerance):
                pen_downs += 1
                in_draw_path = True
            else:
                checks.append(
                    SafetyCheck(
                        CheckSeverity.BLOCKER,
                        "unexpected_z",
                        f"Line {line_no}: unexpected Z {state['Z']}.",
                    )
                )

        if xy_changed:
            segment = (start["X"], start["Y"], state["X"], state["Y"])
            length = _segment_length(segment)
            is_down = (
                start["Z"] <= options.pen_down_z + options.z_tolerance
                and state["Z"] <= options.pen_down_z + options.z_tolerance
            )
            if active_motion == 1 and is_down:
                draw_segments.append(segment)
                current_draw_length += length
                in_draw_path = True
                if math.isfinite(length) and length > 0.0:
                    has_positive_finite_draw_geometry = True
            else:
                travel_segments.append(segment)
                if is_down and length > options.max_pen_down_travel_mm:
                    checks.append(
                        SafetyCheck(
                            CheckSeverity.BLOCKER,
                            "pen_down_travel",
                            f"Line {line_no}: pen-down travel segment is {length:.3f} mm.",
                        )
                    )
        command_count += 1

    if in_draw_path:
        path_lengths.append(current_draw_length)
    effective_draw_segments = draw_segments if has_positive_finite_draw_geometry else []
    if pen_downs > 0 and not has_positive_finite_draw_geometry:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "zero_length_draw_geometry",
                "Candidate has pen-down commands but no positive finite XY draw geometry.",
            )
        )
    if not (saw_units and saw_absolute and saw_work_coordinate and saw_initial_pen_up):
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "missing_safe_start",
                "Candidate must begin with G21, G90, G54, and a pen-up move before drawing.",
            )
        )
    terminal_end_marker = (
        end_marker_line is not None
        and last_explicit_pen_up_line is not None
        and last_explicit_pen_up_line < end_marker_line
        and not sendable_after_end_marker
        and (last_sendable_line is None or last_sendable_line < end_marker_line)
    )
    if not terminal_end_marker or not _near(state["Z"], options.pen_up_z, options.z_tolerance):
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "missing_safe_end",
                "Candidate must end with an explicit pen-up state and end marker.",
            )
        )
    all_segments = effective_draw_segments + travel_segments
    checks.extend(_geometry_checks(all_segments, options))
    checks.extend(_summary_checks(path_lengths, pen_lifts, pen_downs, options))
    checks.append(
        SafetyCheck(
            CheckSeverity.INFO,
            "static_only",
            "Static checks only; no controller validation was performed.",
        )
    )
    summary: JsonObject = {
        "input": options.input_name,
        "motion_command_count": command_count,
        "draw_segment_count": len(effective_draw_segments),
        "travel_segment_count": len(travel_segments),
        "draw_length_mm": round(sum(_segment_length(segment) for segment in effective_draw_segments), 3),
        "travel_length_mm": round(sum(_segment_length(segment) for segment in travel_segments), 3),
        "draw_path_count": len(path_lengths),
        "pen_lift_count": pen_lifts,
        "pen_down_count": pen_downs,
        "short_draw_path_count": sum(length < options.warn_short_path_mm for length in path_lengths),
        "max_feed_seen": round(max_feed_seen, 3),
        "bounds": cast(JsonObject, _segment_bounds(all_segments)),
        "gate": gate_from_checks(tuple(checks)).value,
    }
    return StaticCheckResult(
        summary=summary,
        checks=tuple(checks),
        raw_line_count=len(raw_lines),
        actual_sha256=hashlib.sha256(gcode.encode("utf-8")).hexdigest(),
        max_feed_limit=options.max_feed,
    )


def strip_comments(line: str) -> str:
    return re.sub(r"\([^)]*\)", "", line).split(";", 1)[0]


def parse_words(line: str) -> tuple[dict[str, float], frozenset[str]]:
    words: dict[str, float] = {}
    issues: set[str] = set()
    consumed = [False] * len(line)
    for match in _WORD_RE.finditer(line):
        for index in range(match.start(), match.end()):
            consumed[index] = True
        letter = match.group(1).upper()
        if letter in words:
            issues.add(f"DUPLICATE_{letter}")
            continue
        if letter not in _ALLOWED_WORDS:
            issues.add(letter)
            continue
        words[letter] = float(match.group(2))
    if any(not used and not character.isspace() for used, character in zip(consumed, line, strict=True)):
        issues.add("MALFORMED")
    return words, frozenset(issues)


def gate_from_checks(checks: tuple[SafetyCheck, ...]) -> StaticGate:
    if any(check.severity is CheckSeverity.BLOCKER for check in checks):
        return StaticGate.BLOCKED
    if any(check.severity is CheckSeverity.WARNING for check in checks):
        return StaticGate.REVIEW_WARNINGS
    return StaticGate.PASS_STATIC


def _geometry_checks(segments: list[_Segment], options: StaticCheckOptions) -> tuple[SafetyCheck, ...]:
    out_canvas = 0
    out_machine = 0
    for segment in segments:
        for x, y in ((segment[0], segment[1]), (segment[2], segment[3])):
            if x < -0.001 or y < -0.001 or x > options.canvas_width_mm + 0.001 or y > options.canvas_height_mm + 0.001:
                out_canvas += 1
            if (
                x < -0.001
                or y < -0.001
                or x > options.machine_width_mm + 0.001
                or y > options.machine_height_mm + 0.001
            ):
                out_machine += 1
    bounds = _segment_bounds(segments)
    span_x = bounds["max_x"] - bounds["min_x"]
    span_y = bounds["max_y"] - bounds["min_y"]
    checks: list[SafetyCheck] = []
    if out_machine:
        checks.append(
            SafetyCheck(
                CheckSeverity.BLOCKER,
                "machine_bounds",
                f"{out_machine} segment endpoints exceed machine area.",
            )
        )
    if options.canvas_bounds_mode == "origin" and out_canvas:
        checks.append(
            SafetyCheck(
                CheckSeverity.WARNING,
                "canvas_bounds",
                f"{out_canvas} segment endpoints exceed configured canvas area.",
            )
        )
    if options.canvas_bounds_mode == "span" and (
        span_x > options.canvas_width_mm + 0.001 or span_y > options.canvas_height_mm + 0.001
    ):
        checks.append(
            SafetyCheck(
                CheckSeverity.WARNING,
                "canvas_span",
                f"Drawing span {span_x:.3f} x {span_y:.3f} mm exceeds configured canvas {options.canvas_width_mm:.3f} x {options.canvas_height_mm:.3f} mm.",
            )
        )
    return tuple(checks)


def _summary_checks(
    path_lengths: list[float],
    pen_lifts: int,
    pen_downs: int,
    options: StaticCheckOptions,
) -> tuple[SafetyCheck, ...]:
    checks: list[SafetyCheck] = []
    if pen_lifts != pen_downs + 1 and pen_lifts != pen_downs:
        checks.append(
            SafetyCheck(
                CheckSeverity.INFO,
                "pen_lift_down_mismatch",
                f"pen lifts={pen_lifts}, pen downs={pen_downs}; review as an advisory path-structure signal.",
            )
        )
    if pen_lifts > options.warn_pen_lifts:
        checks.append(
            SafetyCheck(
                CheckSeverity.INFO,
                "many_pen_lifts",
                f"Pen lift count {pen_lifts} exceeds advisory threshold {options.warn_pen_lifts}.",
            )
        )
    short_count = sum(length < options.warn_short_path_mm for length in path_lengths)
    if short_count:
        checks.append(
            SafetyCheck(
                CheckSeverity.INFO,
                "short_draw_paths",
                f"{short_count} draw paths are shorter than {options.warn_short_path_mm} mm.",
            )
        )
    return tuple(checks)


def _near(value: float, target: float, tolerance: float) -> bool:
    return abs(value - target) <= tolerance


def _segment_length(segment: _Segment) -> float:
    return math.dist((segment[0], segment[1]), (segment[2], segment[3]))


def _segment_bounds(segments: list[_Segment]) -> dict[str, float | int]:
    if not segments:
        return {"min_x": 0, "min_y": 0, "max_x": 0, "max_y": 0}
    xs = [value for segment in segments for value in (segment[0], segment[2])]
    ys = [value for segment in segments for value in (segment[1], segment[3])]
    return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}


__all__ = ["analyze_gcode", "gate_from_checks", "parse_words", "strip_comments"]
