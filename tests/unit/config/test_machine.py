from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

import drawingmachine.config.machine as machine_module
from drawingmachine.config.machine import MachineExecutionConfig, parse_machine_execution_config
from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError


class _ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        del key
        raise AssertionError("hostile mapping must not be indexed")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("hostile mapping must not be iterated")

    def __len__(self) -> int:
        raise AssertionError("hostile mapping must not be sized")


class _ExplodingDict(dict[str, object]):
    def __iter__(self) -> Iterator[str]:
        raise AssertionError("dict subclass must not be iterated")

    def items(self) -> object:
        raise AssertionError("dict subclass items must not be called")

    def __contains__(self, key: object) -> bool:
        del key
        raise AssertionError("dict subclass membership must not be called")


class _ChangingMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.calls = 0

    def __getitem__(self, key: str) -> object:
        del key
        self.calls += 1
        return "changed"

    def __iter__(self) -> Iterator[str]:
        self.calls += 1
        return iter(("name",) if self.calls == 1 else ("raw_write",))

    def __len__(self) -> int:
        self.calls += 1
        return 1


class _HostileString(str):
    def isascii(self) -> bool:
        raise AssertionError("hostile string isascii must not be called")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("hostile string encode must not be called")

    def __eq__(self, other: object) -> bool:
        del other
        raise AssertionError("hostile string equality must not be called")

    def __hash__(self) -> int:
        raise AssertionError("hostile string hash must not be called")

    def __str__(self) -> str:
        raise AssertionError("hostile string conversion must not be called")


class _HostileFloat(float):
    def __float__(self) -> float:
        raise AssertionError("hostile number conversion must not be called")


class _HostileInt(int):
    def __int__(self) -> int:
        raise AssertionError("hostile integer conversion must not be called")

    def __index__(self) -> int:
        raise AssertionError("hostile integer index must not be called")


class _HostileList(list[object]):
    def __iter__(self) -> Iterator[object]:
        raise AssertionError("list subclass must not be iterated")

    def __len__(self) -> int:
        raise AssertionError("list subclass must not be sized")


def _profile(**hardware_changes: Any) -> ProfileEnvelope:
    hardware: dict[str, Any] = {
        "firmware": "FluidNC",
        "transport": "serial",
        "serial_port": "/dev/serial/by-id/fluidnc-controller",
        "baud": 115200,
        "line_ending": "\n",
        "open_timeout_seconds": 2.0,
        "settle_timeout_seconds": 15.0,
        "read_timeout_seconds": 1.0,
        "command_timeout_seconds": 10.0,
        "home_timeout_seconds": 300.0,
        "stream_idle_timeout_seconds": 30.0,
        "approval_ttl_seconds": 60,
        "require_g54_offset": True,
        "expected_homed_mpos": [192.0, 192.0, 192.0],
        "position_tolerance_mm": 0.5,
        "post_run_home_z": True,
        "automation_principal": {"uid": 1100, "gid": 1100},
        "operator_principal": {"uid": 2200, "gid": 2200},
    }
    hardware.update(hardware_changes)
    return ProfileEnvelope(
        schema_version=1,
        profile={
            "name": "default",
            "planning": {
                "canvas_width_mm": 120.0,
                "canvas_height_mm": 120.0,
                "pen_width_mm": 0.5,
                "min_gap_mm": 0.8,
                "threshold": 128,
                "invert": False,
                "min_component_area_px": 8,
                "simplify_tolerance_mm": 0.12,
                "min_path_length_mm": 0.6,
                "drop_short_stroke_mm": 0.35,
                "merge_endpoint_distance_mm": 0.45,
                "merge_angle_deg": 35.0,
                "dedupe_short_path_length_mm": 2.0,
                "dedupe_distance_mm": 0.3,
                "dedupe_angle_deg": 25.0,
                "dedupe_overlap_ratio": 0.65,
                "hatch_spacing_mm": 0.8,
                "hatch_min_run_mm": 0.8,
                "fill_min_thickness_mm": 0.85,
            },
            "gcode": {
                "hardware_canvas_width_mm": 144.0,
                "hardware_canvas_height_mm": 144.0,
                "machine_width_mm": 192.0,
                "machine_height_mm": 192.0,
                "paper_center_x": 96.0,
                "paper_center_y": 96.0,
                "pen_up_z": 3.5,
                "pen_down_z": 0.0,
                "feed_travel": 1200.0,
                "feed_draw": 900.0,
                "feed_pen_down": 100.0,
                "feed_pen_up": 400.0,
                "max_feed": 1200.0,
                "work_coordinate": "G54",
                "align_mode": "center",
                "mirror_y": True,
                "safe_start": True,
                "path_mode": "stroke",
            },
            "hardware": hardware,
        },
    )


def _parse(profile: ProfileEnvelope | None = None) -> MachineExecutionConfig:
    return parse_machine_execution_config(profile or _profile(), profile_path=Path("/config/machines/default.toml"))


def test_exact_valid_hardware_profile_is_parsed_to_immutable_typed_config() -> None:
    config = _parse()
    assert config.profile_path == Path("/config/machines/default.toml")
    assert config.firmware == "FluidNC"
    assert config.transport == "serial"
    assert config.serial_port == "/dev/serial/by-id/fluidnc-controller"
    assert config.baud == 115200
    assert config.line_ending == "\n"
    assert config.open_timeout_seconds == 2.0
    assert config.settle_timeout_seconds == 15.0
    assert config.read_timeout_seconds == 1.0
    assert config.command_timeout_seconds == 10.0
    assert config.home_timeout_seconds == 300.0
    assert config.stream_idle_timeout_seconds == 30.0
    assert config.approval_ttl_seconds == 60
    assert config.require_g54_offset is True
    assert config.expected_homed_mpos == (192.0, 192.0, 192.0)
    assert config.position_tolerance_mm == 0.5
    assert config.post_run_home_z is True
    assert (config.automation_principal.uid, config.automation_principal.gid) == (1100, 1100)
    assert (config.operator_principal.uid, config.operator_principal.gid) == (2200, 2200)
    assert config.gcode.machine_width_mm == 192.0
    with pytest.raises(FrozenInstanceError):
        config.serial_port = "/dev/ttyS0"  # type: ignore[misc]


@pytest.mark.parametrize("missing", sorted(_profile().profile["hardware"]))
def test_hardware_requires_every_exact_field(missing: str) -> None:
    profile = _profile()
    hardware = profile.profile["hardware"]
    assert isinstance(hardware, dict)
    del hardware[missing]
    with pytest.raises(DrawingMachineError, match="missing fields"):
        _parse(profile)


@pytest.mark.parametrize(
    "field",
    ["command", "commands", "arm", "confirmation_phrase", "unlock", "jog", "offset", "resume_line", "pen_up_z"],
)
def test_unknown_or_indirect_motion_authority_is_rejected(field: str) -> None:
    with pytest.raises(DrawingMachineError, match="unknown fields"):
        _parse(_profile(**{field: "G0 X1"}))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("firmware", "fluidnc", "must be exactly FluidNC"),
        ("firmware", 7, "must be exactly FluidNC"),
        ("transport", "tcp", "must be exactly serial"),
        ("baud", True, "must be exactly 115200"),
        ("baud", 9600, "must be exactly 115200"),
        ("line_ending", "\\n", "line ending"),
        ("line_ending", "\r\n", "line ending"),
        ("require_g54_offset", 1, "must be true"),
        ("require_g54_offset", False, "must be true"),
        ("post_run_home_z", 1, "must be a boolean"),
    ],
)
def test_fixed_and_boolean_fields_are_exact(field: str, value: object, message: str) -> None:
    with pytest.raises(DrawingMachineError, match=message):
        _parse(_profile(**{field: value}))


@pytest.mark.parametrize(
    "port",
    [
        "",
        "ttyUSB0",
        "/tmp/ttyUSB0",
        "/dev/../tmp/ttyUSB0",
        "/dev//ttyUSB0",
        "/dev/tty USB0",
        "/dev/ttyUSB0;HOME",
        "/dev/ttyUSB0\n",
        "/dev/ttyµ0",
        "com1",
        "COM0",
        "COM257",
        "COM1:evil",
        "/dev/" + "x" * 251,
    ],
)
def test_serial_port_rejects_unsafe_unbounded_control_and_unicode_names(port: str) -> None:
    with pytest.raises(DrawingMachineError, match="serial_port"):
        _parse(_profile(serial_port=port))


@pytest.mark.parametrize(
    "port",
    ["/dev/ttyUSB0", "/dev/ttyACM99", "/dev/serial/by-id/a_b-c.1", "/dev/" + "x" * 250, "COM1", "COM256"],
)
def test_serial_port_accepts_bounded_platform_names(port: str) -> None:
    assert _parse(_profile(serial_port=port)).serial_port == port


@pytest.mark.parametrize(
    "field",
    [
        "open_timeout_seconds",
        "settle_timeout_seconds",
        "read_timeout_seconds",
        "command_timeout_seconds",
        "home_timeout_seconds",
        "stream_idle_timeout_seconds",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        True,
        0,
        -0.1,
        float("inf"),
        float("-inf"),
        float("nan"),
        3600.0001,
        pytest.param(10**10000, id="oversized-integer"),
    ],
)
def test_positive_bounded_finite_fields_reject_invalid_numbers(field: str, value: object) -> None:
    with pytest.raises(DrawingMachineError, match="positive finite number"):
        _parse(_profile(**{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "open_timeout_seconds",
        "settle_timeout_seconds",
        "read_timeout_seconds",
        "command_timeout_seconds",
        "home_timeout_seconds",
        "stream_idle_timeout_seconds",
    ],
)
def test_positive_bounded_timeout_fields_accept_exact_upper_boundary(field: str) -> None:
    assert getattr(_parse(_profile(**{field: 3600})), field) == 3600.0


@pytest.mark.parametrize("value", [5, 900])
def test_approval_ttl_accepts_exact_boundaries(value: int) -> None:
    assert _parse(_profile(approval_ttl_seconds=value)).approval_ttl_seconds == value


@pytest.mark.parametrize("value", [True, 4, 901, 5.0, -1, 2**63])
def test_approval_ttl_rejects_boolean_noninteger_and_out_of_range(value: object) -> None:
    with pytest.raises(DrawingMachineError, match="integer from 5 to 900"):
        _parse(_profile(approval_ttl_seconds=value))


@pytest.mark.parametrize("value", [0.0001, 10.0])
def test_position_tolerance_accepts_safe_boundaries(value: float) -> None:
    assert _parse(_profile(position_tolerance_mm=value)).position_tolerance_mm == value


@pytest.mark.parametrize("value", [True, 0, -0.1, float("inf"), float("nan"), 10.0001])
def test_position_tolerance_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(DrawingMachineError, match="position_tolerance_mm"):
        _parse(_profile(position_tolerance_mm=value))


@pytest.mark.parametrize("role", ["automation_principal", "operator_principal"])
@pytest.mark.parametrize(
    ("key", "value"),
    [("uid", True), ("gid", False), ("uid", -1), ("gid", -1), ("uid", 2**32), ("gid", 2**32)],
)
def test_principals_reject_boolean_negative_and_oversized_ids(role: str, key: str, value: object) -> None:
    principal = {"uid": 1100, "gid": 1100}
    principal[key] = value
    with pytest.raises(DrawingMachineError, match="non-negative platform principal"):
        _parse(_profile(**{role: principal}))


def test_principal_ids_accept_highest_platform_value() -> None:
    config = _parse(_profile(automation_principal={"uid": (2**32) - 2, "gid": (2**32) - 2}))
    assert config.automation_principal.uid == (2**32) - 2


def test_expected_homed_z_accepts_exact_bounded_upper_value() -> None:
    assert _parse(_profile(expected_homed_mpos=[192.0, 192.0, 3600])).expected_homed_mpos[2] == 3600.0


@pytest.mark.parametrize(
    ("role", "value", "message"),
    [
        ("automation_principal", {"uid": 1100}, "missing fields"),
        ("operator_principal", {"uid": 2200, "gid": 2200, "pid": 9}, "unknown fields"),
        ("operator_principal", "2200:2200", "must be a table"),
    ],
)
def test_principal_tables_are_closed(role: str, value: object, message: str) -> None:
    with pytest.raises(DrawingMachineError, match=message):
        _parse(_profile(**{role: value}))


@pytest.mark.parametrize(
    "operator",
    [{"uid": 1100, "gid": 2200}, {"uid": 2200, "gid": 1100}, {"uid": 1100, "gid": 1100}],
)
def test_automation_and_operator_uid_and_gid_must_each_be_distinct(operator: dict[str, int]) -> None:
    with pytest.raises(DrawingMachineError, match="disjoint"):
        _parse(_profile(operator_principal=operator))


@pytest.mark.parametrize(
    "position",
    [
        [192.0, 192.0],
        [192.0, 192.0, 192.0, 1.0],
        [True, 192.0, 192.0],
        [192.0, float("nan"), 192.0],
        [192.0, 192.0, 0.0],
        [192.0, 192.0, 3600.0001],
    ],
)
def test_expected_homed_mpos_is_exact_positive_bounded_xyz(position: list[object]) -> None:
    with pytest.raises(DrawingMachineError, match="expected_homed_mpos"):
        _parse(_profile(expected_homed_mpos=position))


@pytest.mark.parametrize(("index", "value"), [(0, 191.0), (1, 191.0)])
def test_expected_home_xy_must_match_validated_machine_bounds(index: int, value: float) -> None:
    position = [192.0, 192.0, 192.0]
    position[index] = value
    with pytest.raises(DrawingMachineError, match="machine bounds"):
        _parse(_profile(expected_homed_mpos=position))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work_coordinate", "G55"),
        ("paper_center_x", 193.0),
        ("paper_center_y", -1.0),
        ("pen_up_z", 0.0),
        ("machine_width_mm", 0.0),
        ("feed_draw", 1201.0),
        ("max_feed", float("inf")),
    ],
)
def test_existing_gcode_authority_is_cross_validated(field: str, value: object) -> None:
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode[field] = value
    with pytest.raises(DrawingMachineError):
        _parse(profile)


@pytest.mark.parametrize(
    ("changes", "hardware_changes", "message"),
    [
        ({"paper_center_x": 95.0}, {}, "paper center"),
        ({"paper_center_y": 95.0}, {}, "paper center"),
        ({"pen_down_z": -0.1}, {}, "pen heights"),
        ({"pen_up_z": 193.0}, {}, "pen heights"),
        ({"max_feed": 3600.1}, {}, "max_feed"),
    ],
)
def test_cross_table_values_remain_within_hardware_safe_relationships(
    changes: dict[str, object], hardware_changes: dict[str, object], message: str
) -> None:
    profile = _profile(**hardware_changes)
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode.update(changes)
    with pytest.raises(DrawingMachineError, match=message):
        _parse(profile)


def test_profile_schema_and_top_level_tables_are_closed() -> None:
    profile = _profile()
    profile.profile["hardware_alias"] = {"path": "/tmp/indirect.toml"}
    with pytest.raises(DrawingMachineError, match="unknown fields"):
        _parse(profile)
    with pytest.raises(DrawingMachineError, match="schema_version"):
        _parse(ProfileEnvelope(schema_version=2, profile=_profile().profile))


@pytest.mark.parametrize(
    "profile_path",
    [Path("relative.toml"), Path("/" + "x" * 4097)],
)
def test_profile_path_must_be_bounded_and_absolute(profile_path: Path) -> None:
    with pytest.raises(DrawingMachineError, match="bounded absolute path"):
        parse_machine_execution_config(_profile(), profile_path=profile_path)


def test_profile_path_requires_a_path_object() -> None:
    with pytest.raises(DrawingMachineError, match="pathlib Path"):
        parse_machine_execution_config(_profile(), profile_path="/config/default.toml")  # type: ignore[arg-type]


def test_profile_path_subclass_is_rejected_before_method_dispatch() -> None:
    class HostilePath(type(Path())):
        def is_absolute(self) -> bool:
            raise AssertionError("hostile path method must not be called")

    with pytest.raises(DrawingMachineError, match="pathlib Path"):
        parse_machine_execution_config(_profile(), profile_path=HostilePath("/config/default.toml"))


@pytest.mark.parametrize("name", ["", "x" * 129, "défault", "default profile", "default\nprofile"])
def test_profile_name_is_bounded_ascii_without_control_or_whitespace(name: str) -> None:
    profile = _profile()
    profile.profile["name"] = name
    with pytest.raises(DrawingMachineError, match=r"profile\.name"):
        _parse(profile)


def test_non_string_table_key_is_rejected() -> None:
    profile = ProfileEnvelope(schema_version=1, profile={1: "hostile"})  # type: ignore[dict-item]
    with pytest.raises(DrawingMachineError, match="non-string field"):
        _parse(profile)


@pytest.mark.parametrize("key", ["", "µ", "bad\nkey", "x" * 129])
def test_invalid_field_names_are_rejected_before_diagnostics(key: str) -> None:
    profile = _profile()
    profile.profile[key] = "hostile"
    with pytest.raises(DrawingMachineError, match="invalid field name"):
        _parse(profile)


def test_parser_does_not_read_or_probe_profile_or_serial_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("configuration parsing must not access the filesystem")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    assert _parse().serial_port == "/dev/serial/by-id/fluidnc-controller"


@pytest.mark.parametrize("hostile", [_ExplodingMapping(), _ExplodingDict()])
def test_hostile_profile_mapping_is_rejected_without_invoking_overrides(hostile: object) -> None:
    envelope = ProfileEnvelope(schema_version=1, profile=hostile)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="plain table"):
        _parse(envelope)


def test_changing_mapping_is_rejected_without_first_or_second_traversal() -> None:
    hostile = _ChangingMapping()
    envelope = ProfileEnvelope(schema_version=1, profile=hostile)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="plain table"):
        _parse(envelope)
    assert hostile.calls == 0


def test_hidden_raw_write_is_rejected_before_visiting_hostile_value() -> None:
    profile = _profile()
    profile.profile["raw_write"] = _ExplodingMapping()
    with pytest.raises(DrawingMachineError, match="unknown fields: raw_write") as caught:
        _parse(profile)
    assert "hostile mapping" not in caught.value.payload.message


@pytest.mark.parametrize("target_name", ["hardware", "automation_principal", "operator_principal"])
def test_nested_hidden_raw_write_is_rejected_before_visiting_hostile_value(target_name: str) -> None:
    profile = _profile()
    hardware = profile.profile["hardware"]
    assert isinstance(hardware, dict)
    target = hardware if target_name == "hardware" else hardware[target_name]
    assert isinstance(target, dict)
    target["raw_write"] = _ExplodingMapping()
    with pytest.raises(DrawingMachineError, match="unknown fields: raw_write") as caught:
        _parse(profile)
    assert "hostile mapping" not in caught.value.payload.message


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        pytest.param("profile", "name", _HostileString("unsafe-profile"), id="profile-string-subclass"),
        pytest.param("gcode", "work_coordinate", _HostileString("G54"), id="gcode-string-subclass"),
        pytest.param("gcode", "max_feed", _HostileFloat(1200.0), id="gcode-number-subclass"),
        pytest.param("planning", "threshold", _HostileInt(128), id="planning-integer-subclass"),
        pytest.param(
            "hardware",
            "expected_homed_mpos",
            _HostileList([192.0, 192.0, 192.0]),
            id="hardware-list-subclass",
        ),
    ],
)
def test_scalar_and_container_subclasses_fail_closed_without_invoking_overrides(
    section: str,
    field: str,
    value: object,
) -> None:
    profile = _profile()
    target = profile.profile if section == "profile" else profile.profile[section]
    assert isinstance(target, dict)
    target[field] = value
    with pytest.raises(DrawingMachineError, match="plain JSON"):
        _parse(profile)


def test_profile_envelope_and_schema_scalar_subclasses_are_rejected() -> None:
    class EnvelopeSubclass(ProfileEnvelope):
        pass

    with pytest.raises(DrawingMachineError, match="exact accepted ProfileEnvelope"):
        _parse(EnvelopeSubclass(schema_version=1, profile=_profile().profile))
    with pytest.raises(DrawingMachineError, match="schema_version"):
        _parse(ProfileEnvelope(schema_version=True, profile=_profile().profile))  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="schema_version"):
        _parse(ProfileEnvelope(schema_version=_HostileInt(1), profile=_profile().profile))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x" * 4097, id="character-limit"),
        pytest.param("é" * 3000, id="utf8-byte-limit"),
    ],
)
def test_snapshot_rejects_oversized_plain_strings(value: str) -> None:
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode["work_coordinate"] = value
    with pytest.raises(DrawingMachineError, match="string limit"):
        _parse(profile)


def test_snapshot_rejects_codepoint_overflow_before_utf8_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_encode_calls = 0

    def forbidden_encode_size(value: str) -> int:
        nonlocal oversized_encode_calls
        if len(value) > 4096:
            oversized_encode_calls += 1
            raise AssertionError("oversized plain strings must not be encoded")
        return len(value.encode("utf-8"))

    monkeypatch.setattr(machine_module, "_utf8_size", forbidden_encode_size)
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode["work_coordinate"] = "x" * 4097

    with pytest.raises(DrawingMachineError, match="string limit"):
        _parse(profile)
    assert oversized_encode_calls == 0


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x" * 4096, id="exact-codepoint-and-byte-boundary"),
        pytest.param("é" * 2048, id="exact-byte-boundary"),
    ],
)
def test_snapshot_accepts_exact_string_allocation_boundaries_before_semantic_rejection(value: str) -> None:
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode["work_coordinate"] = value
    with pytest.raises(DrawingMachineError, match="must be 'G54'"):
        _parse(profile)


def test_snapshot_rejects_first_multibyte_value_above_utf8_byte_boundary() -> None:
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode["work_coordinate"] = "é" * 2049
    with pytest.raises(DrawingMachineError, match="string limit"):
        _parse(profile)


def test_snapshot_rejects_non_utf8_surrogate_text_with_typed_error() -> None:
    profile = _profile()
    gcode = profile.profile["gcode"]
    assert isinstance(gcode, dict)
    gcode["work_coordinate"] = "\ud800"
    with pytest.raises(DrawingMachineError, match="valid UTF-8"):
        _parse(profile)


def test_snapshot_rejects_excessive_depth_and_node_count() -> None:
    deep: object = 1
    for _ in range(9):
        deep = [deep]
    depth_profile = _profile()
    planning = depth_profile.profile["planning"]
    assert isinstance(planning, dict)
    planning["canvas_width_mm"] = deep
    with pytest.raises(DrawingMachineError, match="depth limit"):
        _parse(depth_profile)

    nodes_profile = _profile()
    node_planning = nodes_profile.profile["planning"]
    assert isinstance(node_planning, dict)
    node_planning["canvas_width_mm"] = [None] * 300
    with pytest.raises(DrawingMachineError, match="node limit"):
        _parse(nodes_profile)
