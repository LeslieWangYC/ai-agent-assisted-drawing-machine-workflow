from __future__ import annotations

import builtins
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

import drawingmachine.bootstrap as bootstrap_module
from drawingmachine.adapters.providers.local_comfyui import HttpTransport
from drawingmachine.bootstrap import build_runtime
from drawingmachine.config import XdgPaths, load_config
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonValue

_PLANNING = """\
canvas_width_mm = 120.0
canvas_height_mm = 120.0
pen_width_mm = 0.5
min_gap_mm = 0.8
threshold = 128
invert = false
min_component_area_px = 8
simplify_tolerance_mm = 0.12
min_path_length_mm = 0.6
drop_short_stroke_mm = 0.35
merge_endpoint_distance_mm = 0.45
merge_angle_deg = 35.0
dedupe_short_path_length_mm = 2.0
dedupe_distance_mm = 0.3
dedupe_angle_deg = 25.0
dedupe_overlap_ratio = 0.65
hatch_spacing_mm = 0.8
hatch_min_run_mm = 0.8
fill_min_thickness_mm = 0.85
"""

_GCODE = """\
hardware_canvas_width_mm = 144.0
hardware_canvas_height_mm = 144.0
machine_width_mm = 192.0
machine_height_mm = 192.0
paper_center_x = 96.0
paper_center_y = 96.0
pen_up_z = 3.5
pen_down_z = 0.0
feed_travel = 1200.0
feed_draw = 900.0
feed_pen_down = 100.0
feed_pen_up = 400.0
max_feed = 1200.0
work_coordinate = "G54"
align_mode = "center"
mirror_y = true
safe_start = true
path_mode = "stroke"
"""

_HARDWARE = """\
firmware = "FluidNC"
transport = "serial"
serial_port = "/dev/ttyUSB0"
baud = 115200
line_ending = "\\n"
open_timeout_seconds = 2.0
settle_timeout_seconds = 15.0
read_timeout_seconds = 1.0
command_timeout_seconds = 10.0
home_timeout_seconds = 300.0
stream_idle_timeout_seconds = 30.0
approval_ttl_seconds = 60
require_g54_offset = true
expected_homed_mpos = [192.0, 192.0, 192.0]
position_tolerance_mm = 0.5
post_run_home_z = true

[profile.hardware.automation_principal]
uid = 1100
gid = 1100

[profile.hardware.operator_principal]
uid = 2200
gid = 2200
"""


class DenyTransport(HttpTransport):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue:
        del method, url, payload, timeout_seconds
        self.calls.append("request_json")
        raise AssertionError("bootstrap must not use provider transport")

    def upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
    ) -> JsonValue:
        del url, filename, content, media_type, timeout_seconds
        self.calls.append("upload_image")
        raise AssertionError("bootstrap must not use provider transport")

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes:
        del url, timeout_seconds
        self.calls.append("request_bytes")
        raise AssertionError("bootstrap must not use provider transport")


def _write_operational_config(paths: XdgPaths) -> tuple[Path, Path]:
    machine_path = paths.config_dir / "machines/default.toml"
    machine_path.write_text(
        f'schema_version = 1\n[profile]\nname = "default"\n[profile.planning]\n{_PLANNING}'
        f"[profile.gcode]\n{_GCODE}[profile.hardware]\n{_HARDWARE}",
        encoding="utf-8",
    )
    workflow_path = paths.config_dir / "providers/workflow.json"
    workflow_path.write_text(
        json.dumps(
            {
                "25": {"inputs": {"image": "old.png"}},
                "27": {"inputs": {"prompt": "Template prompt."}},
                "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
                "18": {"inputs": {"filename_prefix": "old"}},
                "221": {"inputs": {"scale_to_length": 576}},
            }
        ),
        encoding="utf-8",
    )
    provider_path = paths.config_dir / "providers/local-comfyui.toml"
    provider_path.write_text(
        """\
schema_version = 1
[profile]
name = "local-comfyui"
endpoint = "http://127.0.0.1:8188"
workflow_template = "workflow.json"
model_family = "qwen-image-edit-2511"
scale_to_length = 576
timeout_seconds = 240.0
poll_interval_seconds = 2.0
free_after_run = false
live_execution_requires_execute_flag = true

[profile.workflow_nodes]
load_image = "25"
prompt = "27"
sampler = "28"
save_image = "18"
scale = "221"

[profile.sampler_defaults]
steps = 20
cfg = 4.0
denoise = 0.7
""",
        encoding="utf-8",
    )
    return machine_path, workflow_path


@pytest.mark.parametrize("invalid_part", ["planning", "machine", "hardware", "provider", "workflow"])
def test_bootstrap_rejects_invalid_operational_config_before_readiness_with_zero_transport(
    valid_xdg_paths: XdgPaths,
    invalid_part: str,
) -> None:
    machine_path, workflow_path = _write_operational_config(valid_xdg_paths)
    provider_path = valid_xdg_paths.config_dir / "providers/local-comfyui.toml"
    if invalid_part == "planning":
        machine_path.write_text(
            machine_path.read_text(encoding="utf-8").replace("canvas_width_mm = 120.0", "canvas_width_mm = 0.0"),
            encoding="utf-8",
        )
    elif invalid_part == "machine":
        machine_path.write_text(
            machine_path.read_text(encoding="utf-8").replace("safe_start = true", "safe_start = false"),
            encoding="utf-8",
        )
    elif invalid_part == "hardware":
        machine_path.write_text(
            machine_path.read_text(encoding="utf-8").replace(
                'serial_port = "/dev/ttyUSB0"', 'serial_port = "/tmp/live"'
            ),
            encoding="utf-8",
        )
    elif invalid_part == "provider":
        provider_path.write_text(
            provider_path.read_text(encoding="utf-8").replace(
                'endpoint = "http://127.0.0.1:8188"', 'endpoint = "ftp://127.0.0.1"'
            ),
            encoding="utf-8",
        )
    else:
        workflow_path.write_text('{"27":{"inputs":{"prompt":"still incomplete"}}}', encoding="utf-8")

    transport = DenyTransport()
    with pytest.raises(DrawingMachineError) as error:
        build_runtime(valid_xdg_paths, provider_transport=transport)

    assert (
        error.value.payload.code
        == {
            "planning": "PATH_PLAN_CONFIG_INVALID",
            "machine": "MACHINE_BUILD_PROFILE_INVALID",
            "hardware": "MACHINE_EXECUTION_CONFIG_INVALID",
            "provider": "PROVIDER_CONFIG_INVALID",
            "workflow": "PROVIDER_CONFIG_INVALID",
        }[invalid_part]
    )
    assert transport.calls == []
    assert not valid_xdg_paths.socket_file.exists()
    assert not valid_xdg_paths.state_dir.exists()
    assert not valid_xdg_paths.data_dir.exists()
    assert not valid_xdg_paths.runtime_dir.exists()


def test_bootstrap_explicitly_registers_builtin_local_provider_without_transport_activity(
    valid_xdg_paths: XdgPaths,
) -> None:
    _write_operational_config(valid_xdg_paths)
    transport = DenyTransport()

    runtime = build_runtime(valid_xdg_paths, provider_transport=transport)

    assert runtime.provider_registry.names == ("local-comfyui",)
    assert runtime.provider_registry.get("local-comfyui").name == "local-comfyui"
    assert transport.calls == []
    assert not valid_xdg_paths.socket_file.exists()


def test_bootstrap_machine_validation_never_imports_serial_or_probes_the_configured_device(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_operational_config(valid_xdg_paths)
    transport = DenyTransport()
    real_import = builtins.__import__
    real_stat = Path.stat
    real_parser = bootstrap_module.parse_machine_execution_config
    parse_calls = 0

    def guarded_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "serial" or name.startswith("serial."):
            raise AssertionError("machine configuration validation must not import serial")
        return real_import(name, globals, locals, fromlist, level)

    def guarded_stat(path: Path, *, follow_symlinks: bool = True) -> object:
        if str(path).startswith("/dev/"):
            raise AssertionError("machine configuration validation must not probe the serial device")
        return real_stat(path, follow_symlinks=follow_symlinks)

    def counted_parser(*args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return real_parser(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(bootstrap_module, "parse_machine_execution_config", counted_parser)

    runtime = build_runtime(valid_xdg_paths, provider_transport=transport)

    assert runtime.provider_registry.names == ("local-comfyui",)
    assert parse_calls == 1
    assert transport.calls == []
    assert not valid_xdg_paths.socket_file.exists()


def test_bootstrap_keeps_package_b_worker_profile_projection_frozen(
    valid_xdg_paths: XdgPaths,
) -> None:
    _write_operational_config(valid_xdg_paths)
    config = load_config(valid_xdg_paths)

    package_b = bootstrap_module._package_b_config_view(config)

    assert set(config.machine.profile) == {"name", "planning", "gcode", "hardware"}
    assert set(package_b.machine.profile) == {"name", "planning", "gcode"}
    assert package_b.machine.profile["planning"] is config.machine.profile["planning"]
    assert package_b.machine.profile["gcode"] is config.machine.profile["gcode"]
    assert package_b.digests == config.digests
