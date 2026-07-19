from __future__ import annotations

import json
from pathlib import Path

import pytest

from drawingmachine.domain.fluidnc.status import (
    FluidNCState,
    parse_controller_status,
    parse_offsets,
    parse_preflight_snapshot,
    validate_positive_finite,
    validate_vector,
)
from drawingmachine.errors import DrawingMachineError

GOLDEN = Path("tests/fixtures/package_c/golden/responses.json")


def test_status_and_offsets_match_the_c1_differential_golden() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    status = parse_controller_status("<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>")
    offsets = parse_offsets(("[G54:96,96,188.5]", "[G55:1,2,3]", "bad"))

    assert status.state is FluidNCState.IDLE
    assert status.positions_json() == golden["positions"]
    assert {item.name: list(item.position) for item in offsets} == golden["offsets"]


def test_preflight_snapshot_matches_the_c1_differential_golden() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["preflight"]
    result = parse_preflight_snapshot(
        status_line="<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("[G54:96,96,188.5]", "ok"),
        errors=(),
        missing_ok_commands=(),
    )

    assert result.to_json() == golden


@pytest.mark.parametrize(
    "value",
    (
        "bad",
        "<Idle|MPos:1,2>",
        "<Idle|MPos:1,nan,3>",
        "<Idle|MPos:1,inf,3>",
        "<Idle|MPos:1,2,3|MPos:1,2,3>",
        "<Idle|WPos:1,2,3|unknown>",
    ),
)
def test_status_rejects_malformed_or_nonfinite_vectors_without_raw_leakage(value: str) -> None:
    with pytest.raises(DrawingMachineError) as captured:
        parse_controller_status(value)

    assert captured.value.payload.code == "FLUIDNC_STATUS_INVALID"
    assert value not in captured.value.payload.message


def test_status_rejects_hostile_strings_and_oversized_lines() -> None:
    class HostileString(str):
        pass

    for value in (HostileString("<Idle|MPos:1,2,3>"), "<Idle|MPos:" + ("1" * 5000) + ">"):
        with pytest.raises(DrawingMachineError, match="controller status"):
            parse_controller_status(value)


def test_offset_parser_rejects_hostile_and_oversized_collections() -> None:
    class HostileTuple(tuple[str, ...]):
        pass

    for value in (HostileTuple(("[G54:1,2,3]",)), tuple("ok" for _ in range(257))):
        with pytest.raises(DrawingMachineError) as captured:
            parse_offsets(value)
        assert captured.value.payload.code == "FLUIDNC_STATUS_INVALID"


@pytest.mark.parametrize(
    "status_line",
    (
        "<Idle:0|MPos:1,2,3>",
        "<Idle:corrupt|MPos:1,2,3>",
        "<Run:1|MPos:1,2,3>",
        "<Alarm:1|MPos:1,2,3>",
        "<Hold:2|MPos:1,2,3>",
        "<Door:4|MPos:1,2,3>",
    ),
)
def test_known_state_suffixes_follow_only_the_documented_dialect(status_line: str) -> None:
    with pytest.raises(DrawingMachineError):
        parse_controller_status(status_line)


@pytest.mark.parametrize(
    ("status_line", "expected"),
    (
        ("<Hold:0|MPos:1,2,3>", FluidNCState.HOLD),
        ("<Hold:1|MPos:1,2,3>", FluidNCState.HOLD),
        ("<Door:0|MPos:1,2,3>", FluidNCState.DOOR),
        ("<Door:3|MPos:1,2,3>", FluidNCState.DOOR),
    ),
)
def test_documented_hold_and_door_substates_are_typed(status_line: str, expected: FluidNCState) -> None:
    assert parse_controller_status(status_line).state is expected


@pytest.mark.parametrize("control", ("\u0085", "\u007f", "\u009f", "\u200b", "\ufeff", "\u2028", "\u2029", "\r", "\n"))
def test_status_offset_and_preflight_lines_reject_every_control_separator(control: str) -> None:
    with pytest.raises(DrawingMachineError):
        parse_controller_status(f"<Idle|MPos:1,2,3>{control}")
    with pytest.raises(DrawingMachineError):
        parse_offsets((f"[G54:1,2,3]{control}",))


def test_status_parser_covers_empty_positions_unknown_fields_and_other_states() -> None:
    empty = parse_controller_status("<Mystery:7|FS:0,0>")
    assert empty.state is FluidNCState.OTHER
    assert empty.positions_json() == {}
    assert parse_controller_status("<Idle|MPos:1,2,3|FS:0,0>").positions_json() == {"MPos": [1.0, 2.0, 3.0]}
    assert (
        parse_preflight_snapshot(
            status_line="<Idle|MPos:1,2,3>",
            parser_state=("ok",),
            offset_lines=("ok",),
            errors=(),
            missing_ok_commands=(),
        ).computed_wpos_from_mpos_g54
        is None
    )


@pytest.mark.parametrize("status_line", ("<Idle", "<<Idle>", "<Idle>>", "<>", "<bad state|MPos:1,2,3>"))
def test_status_parser_rejects_each_malformed_envelope_and_state_shape(status_line: str) -> None:
    with pytest.raises(DrawingMachineError):
        parse_controller_status(status_line)


def test_status_parser_rejects_duplicate_offsets_bad_numbers_and_total_response_bytes() -> None:
    with pytest.raises(DrawingMachineError):
        parse_offsets(("[G54:1,2,3]", "[g54:1,2,3]"))
    with pytest.raises(DrawingMachineError):
        parse_controller_status("<Idle|MPos:1,wat,3>")
    with pytest.raises(DrawingMachineError):
        parse_offsets(tuple("x" * 4096 for _ in range(17)))


def test_numeric_helpers_reject_overflow_type_range_and_nonfinite_values() -> None:
    huge = 10**10000
    for vector in ((huge, 1, 1), (object(), 1, 1), (1, 2), (1, 2, float("nan"))):
        with pytest.raises(DrawingMachineError):
            validate_vector(vector, "vector")
    for value in (huge, object(), 0, -1, float("inf")):
        with pytest.raises(DrawingMachineError):
            validate_positive_finite(value, "positive")


def test_status_parser_rejects_invalid_unicode_without_echoing_it() -> None:
    with pytest.raises(DrawingMachineError) as captured:
        parse_controller_status("<Idle|MPos:1,2,3>\ud800")
    assert captured.value.payload.code == "FLUIDNC_STATUS_INVALID"
