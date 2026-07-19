from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from drawingmachine.domain.gcode.models import StaticCheckOptions, StaticGate
from drawingmachine.domain.gcode.static_check import analyze_gcode, gate_from_checks, parse_words, strip_comments


def options(**overrides: object) -> StaticCheckOptions:
    values: dict[str, object] = {
        "input_name": "sample.gcode",
        "canvas_width_mm": 144.0,
        "canvas_height_mm": 144.0,
        "machine_width_mm": 192.0,
        "machine_height_mm": 192.0,
        "canvas_bounds_mode": "span",
        "pen_up_z": 3.5,
        "pen_down_z": 0.0,
        "start_x": 96.0,
        "start_y": 96.0,
        "start_z": 3.5,
        "z_tolerance": 0.01,
        "max_feed": 1200.0,
        "max_pen_down_travel_mm": 0.05,
        "warn_pen_lifts": 500,
        "warn_short_path_mm": 0.5,
    }
    values.update(overrides)
    return StaticCheckOptions(**values)  # type: ignore[arg-type]


def safe_gcode(*middle: str) -> str:
    return "\n".join(
        [
            "; Path mode: stroke (fill_paths + stroke_paths + fill_boundary_paths)",
            "G21",
            "G90",
            "G54",
            "G1 Z3.500 F400.0",
            *middle,
            "G1 Z3.500 F400.0",
            "; End",
            "",
        ]
    )


def codes(gcode: str, **overrides: object) -> set[str]:
    return {check.code for check in analyze_gcode(gcode, options(**overrides)).checks}


def test_options_and_result_are_frozen_and_slotted() -> None:
    result = analyze_gcode(safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "G1 X97 Y97 F900"), options())
    assert StaticCheckOptions.__slots__
    assert result.__slots__
    with pytest.raises(FrozenInstanceError):
        result.raw_line_count = 0


def test_parser_strips_comments_and_rejects_unconsumed_or_duplicate_words() -> None:
    assert strip_comments("G1 X1 (travel) ; ignored") == "G1 X1  "
    assert parse_words("G1 X1.5 Y-2") == ({"G": 1.0, "X": 1.5, "Y": -2.0}, frozenset())
    assert parse_words("G1 X1 M3 S100")[1] == frozenset({"M", "S"})
    assert parse_words("G1 X1 ???")[1] == frozenset({"MALFORMED"})
    assert parse_words("G1 X1 X2")[1] == frozenset({"DUPLICATE_X"})


@pytest.mark.parametrize(
    ("gcode", "expected_code"),
    [
        (safe_gcode("G2 X100 Y100"), "unsupported_g_command"),
        (safe_gcode("M3"), "unknown_word"),
        (safe_gcode("G1 X96 ???"), "malformed_line"),
        (safe_gcode("G1 X96 X97"), "malformed_line"),
        (safe_gcode("G1.5 X96"), "malformed_line"),
        (safe_gcode("G1 X96 F-1"), "malformed_line"),
        (safe_gcode(f"G1 X{'9' * 400} Y96"), "malformed_line"),
        (safe_gcode("G91", "G1 X1 Y1"), "unsafe_coordinate_mode"),
        (safe_gcode("G55"), "unsafe_work_coordinate"),
        (safe_gcode("G20"), "unsafe_units"),
        (safe_gcode("G0 X250 Y96"), "machine_bounds"),
        (safe_gcode("G0 X96 Y96 F1201"), "feed_exceeds_limit"),
        (safe_gcode("G1 Z2"), "unexpected_z"),
        (safe_gcode("G1 Z0 F100", "G0 X97 Y97"), "pen_down_travel"),
        ("G90\nG54\nG1 Z3.5\n; End\n", "missing_safe_start"),
        ("G21\nG54\nG1 Z3.5\n; End\n", "missing_safe_start"),
        ("G21\nG90\nG1 Z3.5\n; End\n", "missing_safe_start"),
        ("G21\nG90\nG54\nG0 X96 Y96\n; End\n", "missing_safe_start"),
        ("G21\nG90\nG54\nG1 Z3.5\nG1 Z0\nG1 X97 Y97\n; End\n", "missing_safe_end"),
        (safe_gcode("G1 X1e309 Y1"), "malformed_line"),
    ],
)
def test_analyzer_blocks_every_named_safety_failure(gcode: str, expected_code: str) -> None:
    result = analyze_gcode(gcode, options())

    assert result.gate is StaticGate.BLOCKED
    assert expected_code in {check.code for check in result.checks}


@pytest.mark.parametrize(
    "unsafe_start",
    [
        ("G0 X97 Y96 F1200", "G1 Z3.5 F400"),
        ("G1 X97 Y96 Z3.5 F400",),
    ],
    ids=["xy-before-explicit-lift", "same-line-xy-and-lift"],
)
def test_analyzer_requires_explicit_z_up_only_before_xy_or_pen_down(unsafe_start: tuple[str, ...]) -> None:
    gcode = "\n".join(
        [
            "G21",
            "G90",
            "G54",
            *unsafe_start,
            "G1 Z0 F100",
            "G1 X98 Y97 F900",
            "G1 Z3.5 F400",
            "; End",
            "",
        ]
    )

    result = analyze_gcode(gcode, options())

    assert result.gate is StaticGate.BLOCKED
    assert "missing_safe_start" in {check.code for check in result.checks}


def test_analyzer_rejects_sendable_command_after_early_end_marker() -> None:
    gcode = "; End\n" + safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "G1 X97 Y97 F900")

    result = analyze_gcode(gcode, options())

    assert result.gate is StaticGate.BLOCKED
    assert "missing_safe_end" in {check.code for check in result.checks}


def test_analyzer_allows_comments_after_terminal_end_marker() -> None:
    gcode = safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "G1 X97 Y97 F900") + "; audit comment\n"

    result = analyze_gcode(gcode, options())

    assert result.gate is StaticGate.PASS_STATIC


def test_analyzer_preserves_canvas_origin_and_span_warnings() -> None:
    origin = analyze_gcode(safe_gcode("G0 X170 Y96"), options(canvas_bounds_mode="origin"))
    span = analyze_gcode(
        safe_gcode("G0 X20 Y96", "G0 X170 Y96"),
        options(canvas_width_mm=100.0, canvas_bounds_mode="span"),
    )

    assert origin.gate is StaticGate.REVIEW_WARNINGS
    assert "canvas_bounds" in {check.code for check in origin.checks}
    assert span.gate is StaticGate.REVIEW_WARNINGS
    assert "canvas_span" in {check.code for check in span.checks}


def test_analyzer_preserves_advisory_path_structure_checks() -> None:
    result = analyze_gcode(
        safe_gcode(
            "G1 Z0 F100",
            "G1 X96.1 Y96 F900",
            "G1 Z3.5 F400",
            "G1 Z3.5 F400",
            "G1 Z3.5 F400",
        ),
        options(warn_pen_lifts=1, warn_short_path_mm=0.5),
    )
    result_codes = {check.code for check in result.checks}

    assert {"pen_lift_down_mismatch", "many_pen_lifts", "short_draw_paths"} <= result_codes


def test_analyzer_handles_empty_segments_and_start_z_none() -> None:
    result = analyze_gcode("G21\nG90\nG54\nG1 Z3.5\n; End\n", options(start_z=None))

    assert result.summary["bounds"] == {"min_x": 0, "min_y": 0, "max_x": 0, "max_y": 0}


def test_gate_mapping_covers_pass_warning_and_blocker() -> None:
    assert gate_from_checks(()) is StaticGate.PASS_STATIC
    warning = analyze_gcode(safe_gcode("G0 X170 Y96"), options(canvas_bounds_mode="origin")).checks
    blocker = analyze_gcode(safe_gcode("G2 X1"), options()).checks
    assert gate_from_checks(warning) is StaticGate.REVIEW_WARNINGS
    assert gate_from_checks(blocker) is StaticGate.BLOCKED


def test_valid_pen_contact_candidate_passes_static_gate() -> None:
    result = analyze_gcode(
        safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "G1 X97 Y97 F900"),
        options(),
    )

    assert result.gate is StaticGate.PASS_STATIC
    assert result.summary["draw_segment_count"] == 1
    assert result.summary["pen_down_count"] == 1


def test_zero_displacement_pen_down_xy_is_not_a_draw_segment_and_blocks_static_gate() -> None:
    result = analyze_gcode(
        safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "G1 X96 Y96 F900"),
        options(),
    )

    assert result.gate is StaticGate.BLOCKED
    assert result.summary["draw_segment_count"] == 0
    assert result.summary["draw_length_mm"] == 0.0
    assert "zero_length_draw_geometry" in {check.code for check in result.checks}


def test_modal_motion_line_without_explicit_g_is_analyzed() -> None:
    result = analyze_gcode(
        safe_gcode("G0 X96 Y96 F1200", "G1 Z0 F100", "X97 Y97 F900"),
        options(),
    )

    assert result.gate is StaticGate.PASS_STATIC
    assert result.summary["draw_segment_count"] == 1


def test_modal_axis_line_after_explicit_local_g0_z_up_is_analyzed() -> None:
    result = analyze_gcode(
        "\n".join(
            [
                "G21",
                "G90",
                "G54",
                "G0 Z3.5 F400",
                "X96 Y96 F1200",
                "G1 Z0 F100",
                "X97 Y97 F900",
                "G1 Z3.5 F400",
                "; End",
                "",
            ]
        ),
        options(),
    )

    assert result.gate is StaticGate.PASS_STATIC
    assert result.summary["draw_segment_count"] == 1
