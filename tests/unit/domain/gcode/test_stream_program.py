from __future__ import annotations

import hashlib
import inspect

import pytest

from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.stream_program import (
    MAX_GCODE_BYTES,
    MAX_GCODE_LINE_BYTES,
    MAX_STREAM_COMMANDS,
    StreamCommand,
    ValidatedStreamProgram,
    build_validated_stream_program,
)
from drawingmachine.errors import DrawingMachineError

GCODE = b"; Path mode: stroke\nG21\nG90 ; absolute\n\n; comment\nG54\nG0 Z3.500 F400\n"


def _summary(data: bytes = GCODE) -> dict[str, object]:
    return build_send_plan(data.decode("utf-8")).to_json()


def _digest(data: bytes = GCODE) -> str:
    return hashlib.sha256(data).hexdigest()


def test_complete_stream_program_preserves_every_command_and_source_line() -> None:
    result = build_validated_stream_program(GCODE, _summary(), _digest())

    assert isinstance(result, ValidatedStreamProgram)
    assert result.gcode_sha256 == hashlib.sha256(GCODE).hexdigest()
    assert result.source_size_bytes == len(GCODE)
    assert result.raw_line_count == 7
    assert [(item.source_line, item.command) for item in result.commands] == [
        (2, "G21"),
        (3, "G90"),
        (6, "G54"),
        (7, "G0 Z3.500 F400"),
    ]
    assert result.serial_bytes == sum(len(item.command.encode("ascii")) + 1 for item in result.commands)
    with pytest.raises((AttributeError, TypeError)):
        result.commands += ()  # type: ignore[misc]


def test_summary_samples_cannot_be_used_as_the_complete_program() -> None:
    many_lines = b"; Path mode: stroke\n" + b"\n".join(f"G1 X{index}".encode() for index in range(20)) + b"\n"
    result = build_validated_stream_program(many_lines, _summary(many_lines), _digest(many_lines))

    assert len(result.commands) == 20
    assert len(_summary(many_lines)["first_sendable_lines"]) == 8  # type: ignore[arg-type]
    assert len(_summary(many_lines)["last_sendable_lines"]) == 8  # type: ignore[arg-type]


def test_exact_frozen_send_plan_summary_must_match() -> None:
    mismatches = (
        {**_summary(), "sendable_line_count": 3},
        {**_summary(), "estimated_serial_bytes": True},
        {**_summary(), "first_sendable_lines": []},
        {**_summary(), "unknown": 1},
    )
    for summary in mismatches:
        with pytest.raises(DrawingMachineError) as captured:
            build_validated_stream_program(GCODE, summary, _digest())
        assert captured.value.payload.code == "STREAM_PROGRAM_INVALID"


@pytest.mark.parametrize(
    "data",
    (
        b"\xff",
        b"; Path mode: stroke\nG1 X\x00\n",
        b"; Path mode: stroke\nG1 X\xe2\x98\x83\n",
        b"; Path mode: stroke\n\n",
    ),
)
def test_malformed_decode_control_non_ascii_and_empty_program_fail_typed(data: bytes) -> None:
    with pytest.raises(DrawingMachineError) as captured:
        build_validated_stream_program(data, {}, _digest(data))
    assert captured.value.payload.code == "STREAM_PROGRAM_INVALID"
    assert "\\x" not in captured.value.payload.message


@pytest.mark.parametrize("data", (b"", b"\xef\xbb\xbfG21\n", b"G21\x01\n"))
def test_empty_bom_and_control_inputs_fail_typed(data: bytes) -> None:
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(data, {}, _digest(data))


def test_gcode_total_line_command_and_serial_bounds_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(b"x" * (MAX_GCODE_BYTES + 1), {}, "0" * 64)
    long_line = b"; Path mode: stroke\n" + (b"G1 X" + b"1" * MAX_GCODE_LINE_BYTES) + b"\n"
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(long_line, {}, _digest(long_line))

    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_STREAM_COMMANDS", 2)
    three = b"; Path mode: stroke\nG21\nG90\nG54\n"
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(three, _summary(three), _digest(three))

    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_STREAM_COMMANDS", MAX_STREAM_COMMANDS)
    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_SERIAL_BYTES", 3)
    with pytest.raises(DrawingMachineError):
        one = b"; Path mode: stroke\nG21\n"
        build_validated_stream_program(one, _summary(one), _digest(one))

    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_SERIAL_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_GCODE_LINES", 1)
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(three, _summary(three), _digest(three))


def test_hostile_bytes_and_summary_containers_are_rejected() -> None:
    class HostileBytes(bytes):
        pass

    class HostileDict(dict[str, object]):
        pass

    for data, summary in ((HostileBytes(GCODE), _summary()), (GCODE, HostileDict(_summary()))):
        with pytest.raises(DrawingMachineError):
            build_validated_stream_program(data, summary, _digest(data))  # type: ignore[arg-type]


def test_stream_value_constructors_are_factory_only() -> None:
    with pytest.raises(TypeError):
        StreamCommand(object(), source_line=1, command="G21", serial_bytes=4)
    with pytest.raises(TypeError):
        ValidatedStreamProgram(
            object(),
            gcode_sha256="0" * 64,
            source_size_bytes=4,
            raw_line_count=1,
            serial_bytes=4,
            commands=(),
            send_plan=build_send_plan("G21"),
        )


def test_reconstruction_disagreement_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = build_send_plan(GCODE.decode())
    monkeypatch.setattr(
        "drawingmachine.domain.gcode.stream_program.build_send_plan",
        lambda text: type(real)(
            raw_line_count=real.raw_line_count,
            blank_line_count=real.blank_line_count,
            comment_only_line_count=real.comment_only_line_count,
            sendable_line_count=real.sendable_line_count,
            estimated_serial_bytes=real.estimated_serial_bytes + 1,
            max_send_line_length=real.max_send_line_length,
            first_sendable_lines=real.first_sendable_lines,
            last_sendable_lines=real.last_sendable_lines,
            streaming_mode=real.streaming_mode,
            actual_sha256=real.actual_sha256,
            candidate_source=real.candidate_source,
        ),
    )
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(GCODE, _summary(), _digest())


def test_frozen_summary_validation_rejects_every_hostile_field_shape() -> None:
    valid = _summary()
    sample = {"source_line": 2, "command": "G21"}
    invalid = (
        {1: 1},
        {**valid, "raw_line_count": -1},
        {**valid, "streaming_mode": 1},
        {**valid, "streaming_mode": "raw"},
        {**valid, "first_sendable_lines": [sample] * 9},
        {**valid, "first_sendable_lines": ["bad"]},
        {**valid, "first_sendable_lines": [{"source_line": 2}]},
        {**valid, "first_sendable_lines": [{1: 2, "command": "G21"}]},
        {**valid, "first_sendable_lines": [{"source_line": 0, "command": "G21"}]},
        {**valid, "first_sendable_lines": [{"source_line": True, "command": "G21"}]},
        {**valid, "first_sendable_lines": [{"source_line": 2, "command": 1}]},
        {**valid, "first_sendable_lines": [{"source_line": 2, "command": ""}]},
        {**valid, "first_sendable_lines": [{"source_line": 2, "command": "G1 \u2603"}]},
        {**valid, "first_sendable_lines": [{"source_line": 2, "command": "G" * (MAX_GCODE_LINE_BYTES + 1)}]},
    )
    for summary in invalid:
        with pytest.raises(DrawingMachineError):
            build_validated_stream_program(GCODE, summary, _digest())


def test_exact_artifact_digest_is_a_separate_required_identity() -> None:
    result = build_validated_stream_program(GCODE, _summary(), _digest())
    assert result.gcode_sha256 == _digest()
    for digest in ("0" * 64, "A" * 64, "short", True, type("Hostile", (str,), {})(_digest())):
        with pytest.raises(DrawingMachineError):
            build_validated_stream_program(GCODE, _summary(), digest)


def test_same_summary_and_length_but_changed_unsampled_middle_is_rejected_by_digest() -> None:
    original = b"; Path mode: stroke\n" + b"\n".join(f"G1 X{index:02d}".encode() for index in range(20)) + b"\n"
    changed = original.replace(b"G1 X10", b"G1 X99")
    assert len(original) == len(changed)
    assert _summary(original) == _summary(changed)
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(changed, _summary(original), _digest(original))


def test_bounds_are_scanned_incrementally_before_send_plan_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    source = inspect.getsource(build_validated_stream_program)
    assert ".splitlines(" not in source

    two_commands = b"; Path mode: stroke\nG21\nG90\n"
    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_STREAM_COMMANDS", 1)
    monkeypatch.setattr(
        "drawingmachine.domain.gcode.stream_program.build_send_plan",
        lambda text: pytest.fail("send plan must not be built before command bounds pass"),
    )
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(two_commands, _summary(two_commands), _digest(two_commands))


@pytest.mark.parametrize(
    "data",
    (
        b"; Path mode: stroke\r\nG21\rG90\r\n",
        "; Path mode: stroke\u0085G21\n".encode(),
        "; printable comment \u2028 still a forbidden separator\nG21\n".encode(),
        b"; DEL \x7f\nG21\n",
        "; C1 \u009f\nG21\n".encode(),
        "; format \u200b\nG21\n".encode(),
        "; embedded bom \ufeff\nG21\n".encode(),
        b"; tab\tcontrol\nG21\n",
    ),
)
def test_stream_source_rejects_lone_cr_unicode_separators_and_controls(data: bytes) -> None:
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(data, _summary(data), _digest(data))


def test_crlf_blank_comments_and_source_mapping_are_preserved() -> None:
    data = b"; Path mode: stroke\r\n\r\n; comment\r\nG21\r\nG90\r\n"
    result = build_validated_stream_program(data, _summary(data), _digest(data))
    assert result.raw_line_count == 5
    assert [(item.source_line, item.command) for item in result.commands] == [(4, "G21"), (5, "G90")]


def test_valid_final_line_without_newline_is_bounded_and_mapped() -> None:
    data = b"; Path mode: stroke\nG21"
    result = build_validated_stream_program(data, _summary(data), _digest(data))
    assert [(item.source_line, item.command) for item in result.commands] == [(2, "G21")]


def test_each_structural_bound_fails_before_send_plan_or_extra_command_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_command = StreamCommand

    def assert_before_send_plan(text: str) -> None:
        del text
        pytest.fail("send plan construction must occur only after bounded scanning")

    cases = (
        ("MAX_GCODE_LINE_BYTES", 2, b"G21\n", 0),
        ("MAX_GCODE_LINES", 1, b"G21\nG90\n", 1),
        ("MAX_STREAM_COMMANDS", 1, b"G21\nG90\n", 1),
        ("MAX_SERIAL_BYTES", 3, b"G21\n", 0),
    )
    for constant, limit, data, expected_constructions in cases:
        constructions = 0

        def observed_command(*args: object, **kwargs: object) -> StreamCommand:
            nonlocal constructions
            constructions += 1
            return original_command(*args, **kwargs)  # type: ignore[arg-type]

        with monkeypatch.context() as context:
            context.setattr(f"drawingmachine.domain.gcode.stream_program.{constant}", limit)
            context.setattr("drawingmachine.domain.gcode.stream_program.StreamCommand", observed_command)
            context.setattr("drawingmachine.domain.gcode.stream_program.build_send_plan", assert_before_send_plan)
            with pytest.raises(DrawingMachineError):
                build_validated_stream_program(data, _summary(data), _digest(data))
        assert constructions == expected_constructions


def test_total_byte_bound_precedes_digest_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("drawingmachine.domain.gcode.stream_program.MAX_GCODE_BYTES", 2)
    monkeypatch.setattr(
        "drawingmachine.domain.gcode.stream_program._validate_digest",
        lambda value: pytest.fail("digest must not be inspected before total byte bound"),
    )
    with pytest.raises(DrawingMachineError):
        build_validated_stream_program(b"G21", {}, "not-a-digest")
