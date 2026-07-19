from __future__ import annotations

import json
from pathlib import Path

import pytest

from drawingmachine.domain.fluidnc.responses import (
    ResponseKind,
    WaitIdleKind,
    classify_command_response,
    classify_wait_for_idle,
)
from drawingmachine.errors import DrawingMachineError

GOLDEN = Path("tests/fixtures/package_c/golden/stream.json")


def test_command_classification_matches_c1_ack_error_alarm_timeout_transcripts() -> None:
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = payload["line_ack_transcript"] + payload["alarm_stop_transcript"] + payload["timeout_stop_transcript"]
    expected = {
        "ack": ResponseKind.ACK,
        "error": ResponseKind.ERROR,
        "alarm": ResponseKind.ALARM,
        "timeout": ResponseKind.TIMEOUT,
    }

    for case in cases:
        decision = classify_command_response(
            tuple(case["response"]), expect_ok=True, timed_out=case["classification"] == "timeout"
        )
        assert decision.kind is expected[case["classification"]]
        assert decision.acknowledged is case["acknowledged"]
        assert decision.stop_before_next_command is (not case["acknowledged"])


def test_status_response_and_wait_idle_decisions_are_fail_closed() -> None:
    assert (
        classify_command_response(("<Idle|MPos:1,2,3>",), expect_ok=False, timed_out=False).kind is ResponseKind.STATUS
    )
    assert classify_command_response(("noise",), expect_ok=True, timed_out=False).kind is ResponseKind.INCOMPLETE
    assert classify_wait_for_idle(("<Run|MPos:1,2,3>",), timed_out=False).kind is WaitIdleKind.WAIT
    assert classify_wait_for_idle(("<Run|MPos:1,2,3>", "<Idle|MPos:1,2,3>"), timed_out=False).kind is WaitIdleKind.IDLE
    assert classify_wait_for_idle(("<Alarm|MPos:1,2,3>",), timed_out=False).kind is WaitIdleKind.ALARM
    assert classify_wait_for_idle(("<Run|MPos:1,2,3>",), timed_out=True).kind is WaitIdleKind.TIMEOUT
    assert classify_wait_for_idle(("ALARM:1",), timed_out=False).kind is WaitIdleKind.ALARM
    assert classify_wait_for_idle(("error:2",), timed_out=False).kind is WaitIdleKind.ERROR
    assert (
        classify_command_response(("noise", "<Idle|MPos:1,2,3>"), expect_ok=False, timed_out=False).kind
        is ResponseKind.STATUS
    )


def test_response_parser_prioritizes_alarm_and_error_over_ok() -> None:
    assert classify_command_response(("ok", "ALARM:1"), expect_ok=True, timed_out=False).kind is ResponseKind.ALARM
    assert classify_command_response(("ok", "error:2"), expect_ok=True, timed_out=False).kind is ResponseKind.ERROR


def test_response_inputs_are_bounded_exact_and_do_not_leak_raw_content() -> None:
    class HostileTuple(tuple[str, ...]):
        pass

    values: tuple[object, ...] = (
        ["ok"],
        HostileTuple(("ok",)),
        tuple("noise" for _ in range(257)),
        ("x" * 5000,),
        ("secret-controller-payload\x00",),
    )
    for value in values:
        with pytest.raises(DrawingMachineError) as captured:
            classify_command_response(value, expect_ok=True, timed_out=False)  # type: ignore[arg-type]
        assert captured.value.payload.code == "FLUIDNC_RESPONSE_INVALID"
        assert "secret-controller-payload" not in captured.value.payload.message


def test_response_flags_reject_booleans_subclasses() -> None:
    class HostileBool(int):
        pass

    with pytest.raises(DrawingMachineError):
        classify_command_response(("ok",), expect_ok=HostileBool(1), timed_out=False)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError):
        classify_wait_for_idle(("<Idle|MPos:1,2,3>",), timed_out=1)


def test_response_line_validation_covers_each_bounded_failure() -> None:
    invalid: tuple[object, ...] = (
        (1,),
        ("bad\ud800",),
        ("",),
        tuple("x" * 4096 for _ in range(17)),
        ("bad\nline",),
    )
    for lines in invalid:
        with pytest.raises(DrawingMachineError):
            classify_command_response(lines, expect_ok=True, timed_out=False)


@pytest.mark.parametrize("control", ("\u0085", "\u007f", "\u009f", "\u200b", "\ufeff", "\u2028", "\u2029", "\r", "\n"))
def test_response_lines_reject_every_control_separator(control: str) -> None:
    with pytest.raises(DrawingMachineError):
        classify_command_response((f"ok{control}",), expect_ok=True, timed_out=False)
