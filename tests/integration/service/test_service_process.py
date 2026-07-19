from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest


def _temporary_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    roots = {
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_STATE_HOME": tmp_path / "state",
        "XDG_DATA_HOME": tmp_path / "data",
        "XDG_RUNTIME_DIR": tmp_path / "run",
    }
    config_dir = roots["XDG_CONFIG_HOME"] / "drawingmachine"
    (config_dir / "machines").mkdir(parents=True)
    (config_dir / "providers").mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'schema_version = 1\nmachine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )
    (config_dir / "machines/default.toml").write_text(
        """\
schema_version = 1
[profile]
name = "default"
[profile.planning]
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
[profile.gcode]
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
[profile.hardware]
firmware = "FluidNC"
transport = "serial"
serial_port = "/dev/serial/by-id/fluidnc-controller"
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
""",
        encoding="utf-8",
    )
    (config_dir / "providers/workflow.json").write_text(
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
    (config_dir / "providers/local-comfyui.toml").write_text(
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

    environment = os.environ.copy()
    environment.update({name: str(path) for name, path in roots.items()})
    environment["HOME"] = str(tmp_path / "home")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)

    runtime_dir = roots["XDG_RUNTIME_DIR"] / "drawingmachine"
    database = roots["XDG_STATE_HOME"] / "drawingmachine/drawingmachine.db"
    return environment, runtime_dir / "service.sock", runtime_dir / "service.pid", database


def _wait_for_status(
    process: subprocess.Popen[str],
    executable: Path,
    environment: dict[str, str],
    socket_path: Path,
    cwd: Path,
) -> dict[str, object]:
    deadline = time.monotonic() + 15.0
    last_result: subprocess.CompletedProcess[str] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"installed service exited before readiness: stdout={stdout!r} stderr={stderr!r}")
        if socket_path.exists():
            last_result = subprocess.run(
                [str(executable), "--output", "json", "service", "status"],
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if last_result.returncode == 0:
                payload = json.loads(last_result.stdout)
                assert isinstance(payload, dict)
                return payload
        time.sleep(0.02)
    pytest.fail(f"installed service did not become ready; last status result: {last_result}")


def test_installed_wheel_runs_real_foreground_service_lifecycle(
    installed_drawingmachine: Path,
    tmp_path: Path,
    service_access_test_harness,
) -> None:
    environment, socket_path, pid_path, database = _temporary_environment(tmp_path)
    client_environment = service_access_test_harness.install(environment)
    process = subprocess.Popen(
        [str(installed_drawingmachine), "--output", "json", "service", "run"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        status = _wait_for_status(process, installed_drawingmachine, client_environment, socket_path, tmp_path)
        assert status["ok"] is True
        assert status["command"] == "service.status"
        data = status["data"]
        assert isinstance(data, dict)
        assert data["status"] == "RUNNING"
        assert data["pid"] == process.pid
        assert pid_path.read_text(encoding="ascii") == f"{process.pid}\n"

        missing_job = subprocess.run(
            [str(installed_drawingmachine), "--output", "json", "job", "status", "missing-job"],
            cwd=tmp_path,
            env=client_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        missing_payload = json.loads(missing_job.stdout)
        assert missing_job.returncode == 1
        assert set(missing_payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
        assert missing_payload["command"] == "job.status"
        assert missing_payload["error"]["code"] == "JOB_NOT_FOUND"
        assert missing_job.stderr == ""

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0
    assert stderr == ""
    stopped = json.loads(stdout)
    assert stopped["ok"] is True
    assert stopped["command"] == "service.run"
    assert stopped["data"] == {"status": "STOPPED"}
    assert not socket_path.exists()
    assert not pid_path.exists()

    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT event_type, payload_json FROM audit_events ORDER BY rowid").fetchall()
    assert [row[0] for row in rows] == ["service.started", "service.stopped"]
    audit_payloads = [json.loads(row[1]) for row in rows]
    assert all(payload["pid"] == process.pid for payload in audit_payloads)
    assert audit_payloads[0]["service_epoch"] == audit_payloads[1]["service_epoch"]
    assert service_access_test_harness.events() == {
        "client_snapshot",
        "policy_load",
        "post_bind_revalidation",
        "pre_lease_snapshot",
    }


def test_installed_fixed_executable_recovers_after_process_kill_and_restarts_cleanly(
    installed_drawingmachine: Path,
    tmp_path: Path,
    service_access_test_harness,
) -> None:
    assert installed_drawingmachine.parts[-6:] == (
        ".local",
        "lib",
        "drawingmachine",
        "venv",
        "bin",
        "drawingmachine",
    )
    environment, socket_path, pid_path, database = _temporary_environment(tmp_path)
    client_environment = service_access_test_harness.install(environment)

    first = subprocess.Popen(
        [str(installed_drawingmachine), "--output", "json", "service", "run"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        first_status = _wait_for_status(first, installed_drawingmachine, client_environment, socket_path, tmp_path)
        first_epoch = first_status["data"]["service_epoch"]
        first.kill()
        first.communicate(timeout=10.0)
        assert first.returncode == -signal.SIGKILL

        second = subprocess.Popen(
            [str(installed_drawingmachine), "--output", "json", "service", "run"],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second_status = _wait_for_status(second, installed_drawingmachine, client_environment, socket_path, tmp_path)
        assert second_status["data"]["status"] == "RUNNING"
        assert second_status["data"]["pid"] == second.pid
        assert second_status["data"]["service_epoch"] != first_epoch
        assert pid_path.read_text(encoding="ascii") == f"{second.pid}\n"
        second.send_signal(signal.SIGTERM)
        stdout, stderr = second.communicate(timeout=10.0)
        assert second.returncode == 0
        assert stderr == ""
        assert json.loads(stdout)["data"] == {"status": "STOPPED"}
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert not socket_path.exists()
    assert not pid_path.exists()
    with sqlite3.connect(database) as connection:
        service_events = connection.execute(
            "SELECT event_type FROM audit_events WHERE event_type LIKE 'service.%' ORDER BY rowid"
        ).fetchall()
    assert service_events == [("service.started",), ("service.started",), ("service.stopped",)]
    import_root = Path(environment["XDG_DATA_HOME"]) / "drawingmachine/imports/automation"
    assert all(not any((import_root / role / "observations").iterdir()) for role in ("image", "gcode"))


def test_installed_losing_service_does_not_touch_sqlite_or_audit(
    installed_drawingmachine: Path,
    tmp_path: Path,
    service_access_test_harness,
) -> None:
    environment, socket_path, pid_path, database = _temporary_environment(tmp_path)
    service_access_test_harness.install(environment)
    data_root = Path(environment["XDG_DATA_HOME"]) / "drawingmachine"
    import_root = data_root / "imports/automation"
    export_root = data_root / "exports/automation"
    provisioned_roots = [
        data_root,
        data_root / "imports",
        import_root,
        data_root / "exports",
        export_root,
        export_root / "jobs",
    ]
    empty_static_leaves = [export_root / "jobs"]
    for role in ("image", "gcode"):
        role_root = import_root / role
        provisioned_roots.append(role_root)
        for phase in ("prepare", "drop", "admitted", "quarantine", "observations"):
            leaf = role_root / phase
            provisioned_roots.append(leaf)
            empty_static_leaves.append(leaf)

    def provisioned_snapshot() -> dict[Path, tuple[int, int, int, int, int, tuple[str, ...]]]:
        snapshot = {}
        for path in provisioned_roots:
            facts = path.stat()
            snapshot[path.relative_to(data_root)] = (
                facts.st_dev,
                facts.st_ino,
                facts.st_mode,
                facts.st_uid,
                facts.st_gid,
                tuple(sorted(child.name for child in path.iterdir())),
            )
        return snapshot

    expected_data = provisioned_snapshot()
    socket_path.parent.mkdir(parents=True)
    lock_path = socket_path.with_suffix(".lock")
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            [str(installed_drawingmachine), "--output", "json", "service", "run"],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["error"]["code"] == "SERVICE_ALREADY_RUNNING"
    assert result.stderr == ""
    assert not database.exists()
    assert not database.parent.exists()
    assert not Path(environment["XDG_STATE_HOME"]).exists()
    assert provisioned_snapshot() == expected_data
    assert all(not any(path.iterdir()) for path in empty_static_leaves)
    assert not socket_path.exists()
    assert not pid_path.exists()


@pytest.mark.parametrize("tamper", ["policy-bytes", "outer-mode"])
def test_installed_service_access_tamper_fails_closed_before_runtime_state(
    installed_drawingmachine: Path,
    tmp_path: Path,
    service_access_test_harness,
    tamper: str,
) -> None:
    environment, socket_path, pid_path, database = _temporary_environment(tmp_path)
    service_access_test_harness.install(environment)
    if tamper == "policy-bytes":
        policy = Path(environment["XDG_CONFIG_HOME"]) / "drawingmachine/service-access.toml"
        policy.write_text("schema_version = 2\n", encoding="utf-8")
    else:
        Path(environment["XDG_RUNTIME_DIR"]).chmod(0o770)

    result = subprocess.run(
        [str(installed_drawingmachine), "--output", "json", "service", "run"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["error"]["code"] == "SERVICE_ACCESS_INVALID"
    assert result.stderr == ""
    assert not database.exists()
    assert not socket_path.exists()
    assert not pid_path.exists()
