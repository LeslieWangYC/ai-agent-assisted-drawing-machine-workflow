from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

if TYPE_CHECKING:
    from drawingmachine.domain.gcode import MachineBuildProfile

_PROFILE_FIELDS = frozenset({"name", "planning", "gcode", "hardware"})
_HARDWARE_FIELDS = frozenset(
    {
        "firmware",
        "transport",
        "serial_port",
        "baud",
        "line_ending",
        "open_timeout_seconds",
        "settle_timeout_seconds",
        "read_timeout_seconds",
        "command_timeout_seconds",
        "home_timeout_seconds",
        "stream_idle_timeout_seconds",
        "approval_ttl_seconds",
        "require_g54_offset",
        "expected_homed_mpos",
        "position_tolerance_mm",
        "post_run_home_z",
        "automation_principal",
        "operator_principal",
    }
)
_PRINCIPAL_FIELDS = frozenset({"uid", "gid"})
_POSIX_PORT_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_WINDOWS_PORT = re.compile(r"COM(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6])\Z")
_MAX_SERIAL_PORT_LENGTH = 255
_MAX_PROFILE_PATH_LENGTH = 4096
_MAX_FIELD_NAME_LENGTH = 128
_MAX_POSITIVE_VALUE = 3600.0
_MAX_POSITION_TOLERANCE_MM = 10.0
_MAX_PRINCIPAL_ID = (2**32) - 2
_MAX_PROFILE_NODES = 256
_MAX_PROFILE_DEPTH = 8
_MAX_PROFILE_STRING_BYTES = 4096


@dataclass(slots=True)
class _SnapshotBudget:
    remaining: int = _MAX_PROFILE_NODES

    def take(self, field: str) -> None:
        if self.remaining <= 0:
            _invalid("machine profile exceeds the bounded plain JSON node limit", field)
        self.remaining -= 1


@dataclass(frozen=True, slots=True)
class MachinePrincipalConfig:
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class MachineExecutionConfig:
    profile_path: Path
    firmware: str
    transport: str
    serial_port: str
    baud: int
    line_ending: str
    open_timeout_seconds: float
    settle_timeout_seconds: float
    read_timeout_seconds: float
    command_timeout_seconds: float
    home_timeout_seconds: float
    stream_idle_timeout_seconds: float
    approval_ttl_seconds: int
    require_g54_offset: bool
    expected_homed_mpos: tuple[float, float, float]
    position_tolerance_mm: float
    post_run_home_z: bool
    automation_principal: MachinePrincipalConfig
    operator_principal: MachinePrincipalConfig
    gcode: MachineBuildProfile


def parse_machine_execution_config(
    envelope: ProfileEnvelope,
    *,
    profile_path: Path,
) -> MachineExecutionConfig:
    """Parse the accepted machine envelope without reading or probing any path."""
    from drawingmachine.domain.gcode import parse_machine_build_profile

    accepted = _snapshot_envelope(envelope)
    if accepted.schema_version != 1:
        _invalid("profile.schema_version must be 1", "profile.schema_version")
    path = _profile_path(profile_path)
    profile = cast(dict[str, object], accepted.profile)
    _profile_name(profile["name"])

    # This is the sole motion-authority source. Hardware only cross-references its
    # validated machine bounds; it never accepts duplicated G-code values.
    gcode = parse_machine_build_profile(accepted)
    hardware = cast(dict[str, object], profile["hardware"])

    firmware = _fixed_string(hardware["firmware"], "profile.hardware.firmware", "FluidNC")
    transport = _fixed_string(hardware["transport"], "profile.hardware.transport", "serial")
    serial_port = _serial_port(hardware["serial_port"])
    baud = _fixed_integer(hardware["baud"], "profile.hardware.baud", 115200)
    line_ending = _fixed_string(hardware["line_ending"], "profile.hardware.line_ending", "\n")
    open_timeout = _positive_bounded(hardware["open_timeout_seconds"], "profile.hardware.open_timeout_seconds")
    settle_timeout = _positive_bounded(hardware["settle_timeout_seconds"], "profile.hardware.settle_timeout_seconds")
    read_timeout = _positive_bounded(hardware["read_timeout_seconds"], "profile.hardware.read_timeout_seconds")
    command_timeout = _positive_bounded(hardware["command_timeout_seconds"], "profile.hardware.command_timeout_seconds")
    home_timeout = _positive_bounded(hardware["home_timeout_seconds"], "profile.hardware.home_timeout_seconds")
    stream_idle_timeout = _positive_bounded(
        hardware["stream_idle_timeout_seconds"], "profile.hardware.stream_idle_timeout_seconds"
    )
    approval_ttl = _approval_ttl(hardware["approval_ttl_seconds"])
    require_g54 = _fixed_true(hardware["require_g54_offset"], "profile.hardware.require_g54_offset")
    expected_homed_mpos = _position(hardware["expected_homed_mpos"])
    tolerance = _position_tolerance(hardware["position_tolerance_mm"])
    post_run_home_z = _boolean(hardware["post_run_home_z"], "profile.hardware.post_run_home_z")
    automation = _principal(hardware["automation_principal"], "profile.hardware.automation_principal")
    operator = _principal(hardware["operator_principal"], "profile.hardware.operator_principal")

    if automation.uid == operator.uid or automation.gid == operator.gid:
        _invalid(
            "automation and operator principals must have disjoint UID and GID values",
            "profile.hardware.operator_principal",
        )
    if expected_homed_mpos[0] != gcode.machine_width_mm or expected_homed_mpos[1] != gcode.machine_height_mm:
        _invalid(
            "profile.hardware.expected_homed_mpos X/Y must exactly match validated machine bounds",
            "profile.hardware.expected_homed_mpos",
        )
    _cross_validate_gcode(gcode, expected_homed_mpos)

    return MachineExecutionConfig(
        profile_path=path,
        firmware=firmware,
        transport=transport,
        serial_port=serial_port,
        baud=baud,
        line_ending=line_ending,
        open_timeout_seconds=open_timeout,
        settle_timeout_seconds=settle_timeout,
        read_timeout_seconds=read_timeout,
        command_timeout_seconds=command_timeout,
        home_timeout_seconds=home_timeout,
        stream_idle_timeout_seconds=stream_idle_timeout,
        approval_ttl_seconds=approval_ttl,
        require_g54_offset=require_g54,
        expected_homed_mpos=expected_homed_mpos,
        position_tolerance_mm=tolerance,
        post_run_home_z=post_run_home_z,
        automation_principal=automation,
        operator_principal=operator,
        gcode=gcode,
    )


def _profile_path(value: Path) -> Path:
    if type(value) is not type(Path()):
        _invalid("machine profile path must be a pathlib Path", "machine_profile_path")
    text = str(value)
    if not text or len(text) > _MAX_PROFILE_PATH_LENGTH or not value.is_absolute():
        _invalid("machine profile path must be a bounded absolute path", "machine_profile_path")
    return value


def _snapshot_envelope(envelope: ProfileEnvelope) -> ProfileEnvelope:
    if type(envelope) is not ProfileEnvelope:
        _invalid("machine profile envelope must be an exact accepted ProfileEnvelope", "profile")
    if type(envelope.schema_version) is not int:
        _invalid("profile.schema_version must be 1", "profile.schema_version")
    budget = _SnapshotBudget()
    profile = _plain_exact_table(envelope.profile, _PROFILE_FIELDS, "profile", budget)
    hardware = _plain_exact_table(profile["hardware"], _HARDWARE_FIELDS, "profile.hardware", budget)
    automation = _plain_exact_table(
        hardware["automation_principal"],
        _PRINCIPAL_FIELDS,
        "profile.hardware.automation_principal",
        budget,
    )
    operator = _plain_exact_table(
        hardware["operator_principal"],
        _PRINCIPAL_FIELDS,
        "profile.hardware.operator_principal",
        budget,
    )
    hardware["automation_principal"] = automation
    hardware["operator_principal"] = operator
    profile["hardware"] = _snapshot_table_values(hardware, "profile.hardware", budget, depth=1)
    profile["planning"] = _snapshot_json(profile["planning"], "profile.planning", budget, depth=1)
    profile["gcode"] = _snapshot_json(profile["gcode"], "profile.gcode", budget, depth=1)
    profile["name"] = _snapshot_json(profile["name"], "profile.name", budget, depth=1)
    return ProfileEnvelope(envelope.schema_version, cast(JsonObject, profile))


def _plain_exact_table(
    value: object,
    expected: frozenset[str],
    field: str,
    budget: _SnapshotBudget,
) -> dict[str, object]:
    budget.take(field)
    if type(value) is not dict:
        _invalid(f"{field} must be a table; exact plain table required", field)
    result: dict[str, object] = {}
    for raw_key, item in cast(dict[object, object], value).items():
        key = _plain_key(raw_key, field)
        if key not in expected:
            _invalid(f"{field} has unknown fields: {key}", field, {"unknown_field": key})
        result[key] = item
    for key in expected:
        if key not in result:
            _invalid(f"{field} has missing fields: {key}", field, {"missing_field": key})
    return result


def _snapshot_table_values(
    values: dict[str, object],
    field: str,
    budget: _SnapshotBudget,
    *,
    depth: int,
) -> JsonObject:
    return {key: _snapshot_json(item, f"{field}.{key}", budget, depth=depth + 1) for key, item in values.items()}


def _snapshot_json(value: object, field: str, budget: _SnapshotBudget, *, depth: int) -> JsonValue:
    budget.take(field)
    if value is None or type(value) in {bool, int, float}:
        return cast(JsonValue, value)
    if type(value) is str:
        if len(value) > _MAX_PROFILE_STRING_BYTES:
            _invalid(f"{field} exceeds the bounded plain JSON string limit", field)
        try:
            encoded_size = _utf8_size(value)
        except UnicodeEncodeError:
            _invalid(f"{field} must be valid UTF-8 plain JSON text", field)
        if encoded_size > _MAX_PROFILE_STRING_BYTES:
            _invalid(f"{field} exceeds the bounded plain JSON string limit", field)
        return value
    if depth >= _MAX_PROFILE_DEPTH:
        _invalid(f"{field} exceeds the bounded plain JSON depth limit", field)
    if type(value) is dict:
        result: JsonObject = {}
        for raw_key, item in cast(dict[object, object], value).items():
            key = _plain_key(raw_key, field)
            result[key] = _snapshot_json(item, f"{field}.{key}", budget, depth=depth + 1)
        return result
    if type(value) is list:
        return [_snapshot_json(item, f"{field}[]", budget, depth=depth + 1) for item in cast(list[object], value)]
    _invalid(f"{field} must contain only exact plain JSON values", field)


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _plain_key(value: object, field: str) -> str:
    if type(value) is not str:
        _invalid(f"{field} contains a non-string field", field)
    if not value or len(value) > _MAX_FIELD_NAME_LENGTH or not value.isascii() or not value.isprintable():
        _invalid(f"{field} contains an invalid field name", field)
    return value


def _cross_validate_gcode(
    gcode: MachineBuildProfile,
    expected_homed_mpos: tuple[float, float, float],
) -> None:
    if gcode.paper_center_x * 2.0 != gcode.machine_width_mm or gcode.paper_center_y * 2.0 != gcode.machine_height_mm:
        _invalid("profile.gcode paper center must exactly bisect the machine bounds", "profile.gcode.paper_center_x")
    if not 0.0 <= gcode.pen_down_z < gcode.pen_up_z <= expected_homed_mpos[2]:
        _invalid(
            "profile.gcode pen heights must be non-negative and within expected homed Z",
            "profile.gcode.pen_up_z",
        )
    if gcode.max_feed > _MAX_POSITIVE_VALUE:
        _invalid("profile.gcode.max_feed exceeds the hardware-safe limit", "profile.gcode.max_feed")


def _profile_name(value: object) -> str:
    field = "profile.name"
    if type(value) is not str or not value or len(value) > 128 or not value.isascii():
        _invalid(f"{field} must be a non-empty bounded ASCII string", field)
    if any(character.isspace() or ord(character) < 0x21 or ord(character) == 0x7F for character in value):
        _invalid(f"{field} contains whitespace or control characters", field)
    return value


def _fixed_string(value: object, field: str, expected: str) -> str:
    if type(value) is not str or value != expected:
        if field.endswith("line_ending"):
            _invalid(f"{field} line ending must be exactly newline", field)
        _invalid(f"{field} must be exactly {expected}", field)
    return value


def _fixed_integer(value: object, field: str, expected: int) -> int:
    if type(value) is not int or value != expected:
        _invalid(f"{field} must be exactly {expected}", field)
    return value


def _fixed_true(value: object, field: str) -> bool:
    if type(value) is not bool or value is not True:
        _invalid(f"{field} must be true", field)
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(f"{field} must be a boolean", field)
    return value


def _positive_bounded(value: object, field: str) -> float:
    if type(value) not in (int, float):
        _invalid(f"{field} must be a positive finite number no greater than {_MAX_POSITIVE_VALUE:g}", field)
    try:
        result = float(cast(int | float, value))
    except OverflowError:
        _invalid(f"{field} must be a positive finite number no greater than {_MAX_POSITIVE_VALUE:g}", field)
    if not math.isfinite(result) or not 0.0 < result <= _MAX_POSITIVE_VALUE:
        _invalid(f"{field} must be a positive finite number no greater than {_MAX_POSITIVE_VALUE:g}", field)
    return result


def _approval_ttl(value: object) -> int:
    field = "profile.hardware.approval_ttl_seconds"
    if type(value) is not int or not 5 <= value <= 900:
        _invalid(f"{field} must be an integer from 5 to 900", field)
    return value


def _position_tolerance(value: object) -> float:
    field = "profile.hardware.position_tolerance_mm"
    result = _positive_bounded(value, field)
    if result > _MAX_POSITION_TOLERANCE_MM:
        _invalid(f"{field} must not exceed {_MAX_POSITION_TOLERANCE_MM:g}", field)
    return result


def _principal(value: object, field: str) -> MachinePrincipalConfig:
    values = cast(dict[str, object], value)
    return MachinePrincipalConfig(
        uid=_principal_id(values["uid"], f"{field}.uid"),
        gid=_principal_id(values["gid"], f"{field}.gid"),
    )


def _principal_id(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_PRINCIPAL_ID:
        _invalid(f"{field} must be a non-negative platform principal ID", field)
    return value


def _position(value: object) -> tuple[float, float, float]:
    field = "profile.hardware.expected_homed_mpos"
    if type(value) is not list or len(value) != 3:
        _invalid(f"{field} must be an exact XYZ array", field)
    values = cast(list[object], value)
    return (
        _positive_bounded(values[0], f"{field}.x"),
        _positive_bounded(values[1], f"{field}.y"),
        _positive_bounded(values[2], f"{field}.z"),
    )


def _serial_port(value: object) -> str:
    field = "profile.hardware.serial_port"
    if type(value) is not str or not value or len(value) > _MAX_SERIAL_PORT_LENGTH or not value.isascii():
        _invalid(f"{field} must be a bounded ASCII /dev path or COM name", field)
    if _WINDOWS_PORT.fullmatch(value):
        return value
    if not value.startswith("/dev/"):
        _invalid(f"{field} must be a platform-valid /dev path or COM name", field)
    suffix = value[5:]
    components = suffix.split("/")
    if not suffix or any(not component or component in {".", ".."} for component in components):
        _invalid(f"{field} must be a canonical /dev path", field)
    if any(_POSIX_PORT_COMPONENT.fullmatch(component) is None for component in components):
        _invalid(f"{field} contains unsafe path characters", field)
    return value


def _invalid(
    message: str,
    field: str,
    details: Mapping[str, JsonValue] | None = None,
) -> NoReturn:
    payload_details: dict[str, JsonValue] = {"field": field}
    if details is not None:
        payload_details.update(details)
    raise DrawingMachineError(
        ErrorPayload(
            code="MACHINE_EXECUTION_CONFIG_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            retryable=False,
            details=payload_details,
        )
    )
