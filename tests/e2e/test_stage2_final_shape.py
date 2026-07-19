from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageDraw

from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload["data"]["job"]
    assert isinstance(job, dict)
    return cast(dict[str, Any], job)


def _artifact(payload: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in payload["data"]["artifacts"] if item.get("role") == role]
    assert len(matches) == 1
    return cast(dict[str, Any], matches[0])


def _write_review(path: Path, job: dict[str, Any], processed_image: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "processed_image_review_v1",
                "job_name": job["name"],
                "reviewed_at": "2026-07-15T00:00:00+00:00",
                "reviewer": "test:stage2-final-shape",
                "processed_image": str(processed_image),
                "handoff": None,
                "status": "PASS_TO_BUILD",
                "checks": {
                    "recognizable_subject": True,
                    "background_simplified": True,
                    "black_on_white": True,
                    "no_edge_clipping": True,
                    "line_density_drawable": True,
                    "no_soft_gradients": True,
                    "limited_tiny_isolated_marks": True,
                    "vectorization_complexity_ok": True,
                },
                "issues": [],
                "revised_prompt": None,
                "next_allowed_stage": "build_drawable_job",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _configure_openclaw(service: Any, root: Path) -> tuple[Path, Path]:
    registry_root = root / "registry"
    managed_root = root / "managed"
    registry_root.mkdir(parents=True)
    workspaces = (
        managed_root / "agents/cnc-drawable-translator",
        managed_root / "agents/cnc-drawable-router",
    )
    for workspace in workspaces:
        workspace.mkdir(parents=True)
    for directory in (managed_root, managed_root / "agents", *workspaces):
        directory.chmod(0o2750)
    bundle = load_packaged_openclaw_bundle()
    (registry_root / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {
                            "id": agent.agent_id,
                            "workspace": str(managed_root / agent.workspace_relative_path),
                            "tools": {"alsoAllow": list(agent.required_tools)},
                        }
                        for agent in bundle.registry_policy.agents
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (registry_root / "openclaw.json").chmod(0o440)
    policy = Path(service.environment["XDG_CONFIG_HOME"]) / "drawingmachine/openclaw-deployment.toml"
    policy.write_text(
        f'''schema_version = 1
registry_root = {json.dumps(str(registry_root))}
managed_root = {json.dumps(str(managed_root))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

[operator_principal]
uid = 2200
gid = 2200
[automation_principal]
uid = 1100
gid = 1100
[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}
[managed_owner]
uid = {os.getuid()}
gid = {os.getgid()}
[registry_owner]
uid = {os.getuid()}
gid = {os.getgid()}
''',
        encoding="utf-8",
    )
    policy.chmod(0o400)
    return registry_root, managed_root


_INSTALLED_MACHINE_SCRIPT = r"""
from __future__ import annotations

import hashlib
import gc
import importlib.resources
import itertools
import json
import multiprocessing
import os
import pathlib
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime

import drawingmachine
import sitecustomize
from drawingmachine.adapters.hardware.fake_fluidnc import FakeFluidNCOperation, FakeFluidNCStep, StrictFakeFluidNCFactory
from drawingmachine.bootstrap import build_runtime
from drawingmachine.config import resolve_xdg_paths
from drawingmachine.domain.fluidnc.gates import GateDisposition, SessionObservationPhase, classify_session_observation
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.gcode.stream_program import build_validated_stream_program
from drawingmachine.domain.jobs import ReadyToRunSnapshot
from drawingmachine.domain.machine import MachinePhase
from drawingmachine.ports.fluidnc import (
    CloseOutcome, CloseSessionRequest, HomeAllAxesRequest, HomeOutcome, HomeZAxisRequest,
    HomeZOutcome, InitialPreflightRequest, OpenSessionRequest, PreflightOutcome,
    StreamOutcome, StreamProgramRequest, StreamProgressUpdate, ZCalibrationOutcome,
    ZCalibrationRequest, ZConfirmationOutcome, ZConfirmationRequest,
)
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest
from drawingmachine.service.models import PeerCredentials, RequestContext

root = pathlib.Path(sys.argv[1])
job_id = sys.argv[2]
repository_root = pathlib.Path(sys.argv[3]).resolve()
installed_site = pathlib.Path(sys.argv[4]).resolve()
assert all(name not in os.environ for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"))
before_threads = tuple(thread.name for thread in threading.enumerate())
before_children = tuple(child.pid for child in multiprocessing.active_children())
def fd_targets():
    targets = []
    for path in pathlib.Path("/proc/self/fd").iterdir():
        try:
            targets.append(os.readlink(path))
        except FileNotFoundError:
            pass
    return sorted(targets)

before_fd_targets = fd_targets()
before_fds = len(before_fd_targets)
assert before_threads == ("MainThread",)
assert before_children == ()
assert not pathlib.Path(drawingmachine.__file__).resolve().is_relative_to(repository_root)
assert not pathlib.Path(str(importlib.resources.files("drawingmachine"))).resolve().is_relative_to(repository_root)
assert pathlib.Path(sitecustomize.__file__).resolve().is_relative_to(installed_site)
assert all(not pathlib.Path(item).resolve().is_relative_to(repository_root) for item in sys.path if item)

paths = resolve_xdg_paths(__import__("os").environ, home=pathlib.Path(__import__("os").environ["HOME"]))
connection = sqlite3.connect(paths.database_file)
try:
    row = connection.execute("SELECT state, ready_snapshot_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
finally:
    connection.close()
assert row is not None and row[0] == "READY_TO_RUN" and row[1]
ready = ReadyToRunSnapshot.from_json(json.loads(row[1]))
gcode_path = paths.jobs_dir / job_id / ready.gcode.relative_path
gcode = gcode_path.read_bytes()
assert hashlib.sha256(gcode).hexdigest() == ready.gcode.sha256
program = build_validated_stream_program(gcode, ready.to_json()["send_plan"], ready.gcode.sha256)

def snapshot(mpos, wpos, wco, g54):
    def vector(value): return ",".join(f"{item:.3f}" for item in value)
    return parse_preflight_snapshot(
        status_line=f"<Idle|MPos:{vector(mpos)}|WPos:{vector(wpos)}|WCO:{vector(wco)}>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(f"[G54:{vector(g54)}]",), errors=(), missing_ok_commands=(),
    )

epoch = "00000000-0000-4000-8000-000000000002"
initial = snapshot((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
decision = classify_session_observation(
    ("FluidNC startup reset",), initial.status, SessionObservationPhase.FRESH_STABILIZATION,
)
assert decision.disposition is GateDisposition.AWAIT_HOME_ONLY
preflight = PreflightOutcome(
    epoch, initial, ("FluidNC startup reset",), SessionObservationPhase.FRESH_STABILIZATION, decision,
)
home_request = HomeAllAxesRequest((192.0, 192.0, 192.0), 0.5)
homed = snapshot((192.0, 192.0, 192.0), (96.0, 96.0, 96.0), (96.0, 96.0, 96.0), (96.0, 96.0, 96.0))
zcal_request = ZCalibrationRequest("G54", 96.0, 96.0, 3.5, 0.0, 1200.0, 100.0)
zcal = snapshot((192.0, 192.0, 96.0), (96.0, 96.0, 0.0), (96.0, 96.0, 96.0), (96.0, 96.0, 96.0))
zconfirm_request = ZConfirmationRequest((96.0, 96.0, 3.5), 3.5, 400.0, 0.5)
raised = snapshot((192.0, 192.0, 99.5), (96.0, 96.0, 3.5), (96.0, 96.0, 96.0), (96.0, 96.0, 96.0))
progress = tuple(
    StreamProgressUpdate(epoch, index, command.source_line, hashlib.sha256(command.command.encode("ascii")).hexdigest())
    for index, command in enumerate(program.commands, start=1)
)
steps = (
    FakeFluidNCStep(FakeFluidNCOperation.INITIAL_PREFLIGHT, InitialPreflightRequest(), preflight),
    FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, home_request, HomeOutcome(epoch, home_request, homed)),
    FakeFluidNCStep(FakeFluidNCOperation.Z_CALIBRATION, zcal_request, ZCalibrationOutcome(epoch, zcal_request, 6, zcal)),
    FakeFluidNCStep(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, zconfirm_request, ZConfirmationOutcome(epoch, zconfirm_request, raised)),
    FakeFluidNCStep(FakeFluidNCOperation.STREAM_PROGRAM, StreamProgramRequest(program), StreamOutcome(epoch, len(program.commands), len(program.commands), raised), progress),
    FakeFluidNCStep(FakeFluidNCOperation.HOME_Z_AXIS, HomeZAxisRequest(), HomeZOutcome(epoch, HomeZAxisRequest(), homed)),
    FakeFluidNCStep(FakeFluidNCOperation.CLOSE, CloseSessionRequest(epoch), CloseOutcome(epoch)),
)
factory = StrictFakeFluidNCFactory(OpenSessionRequest(epoch), steps)
identities = itertools.chain(
    ("10000000-0000-4000-8000-000000000001", epoch),
    (f"10000000-0000-4000-8000-{index:012d}" for index in itertools.count(3)),
)
runtime = build_runtime(paths, fluidnc_factory=factory, machine_identity_factory=lambda: next(identities))
runtime.start()
counter = itertools.count(1)

def dispatch(command, arguments, *, operator=False, request_id=None, pid=None):
    value = next(counter)
    peer = PeerCredentials(pid if pid is not None else (41000 + value if operator else 31000 + value), 2200 if operator else 1100, 2200 if operator else 1100)
    response = runtime.dispatcher.dispatch(
        ProtocolRequest(PROTOCOL_VERSION, command, request_id or f"20000000-0000-4000-8000-{value:012d}", arguments),
        RequestContext(peer, datetime.now(UTC)),
    )
    assert response.ok, response.error
    return response.data

def wait_phase(execution_id, expected):
    deadline = time.monotonic() + 10.0
    data = None
    while time.monotonic() < deadline:
        data = dispatch("machine.status", {"schema_version": 1, "execution_id": execution_id})
        if data["execution"]["phase"] == expected.value:
            return data["execution"]
        time.sleep(0.01)
    raise AssertionError((expected, data))

try:
    prepared = dispatch("machine.prepare", {"schema_version": 1, "job_id": job_id, "job_revision": ready.job_revision})
    execution_id = prepared["execution"]["execution_id"]
    wait_phase(execution_id, MachinePhase.AWAITING_HOME_APPROVAL)
    replay = {}
    purposes = {}
    for command, phase in (
        ("machine.home", MachinePhase.AWAITING_ZCAL_APPROVAL),
        ("machine.zcal", MachinePhase.AWAITING_ZCONFIRM_APPROVAL),
        ("machine.zconfirm", MachinePhase.AWAITING_STREAM_APPROVAL),
        ("machine.stream", MachinePhase.AWAITING_HOME_Z_APPROVAL),
        ("machine.home-z", MachinePhase.COMPLETED),
    ):
        request_arguments = {"schema_version": 1, "execution_id": execution_id, "mode": "request", "challenge_id": None}
        request_id = f"30000000-0000-4000-8000-{next(counter):012d}"
        requested = dispatch(command, request_arguments, request_id=request_id, pid=31001)
        purposes[command] = requested["challenge"]["purpose"]
        replayed = dispatch(command, request_arguments, request_id=request_id, pid=31999)
        replay[command] = replayed["deduplicated"] is True
        execute_arguments = {"schema_version": 1, "execution_id": execution_id, "mode": "execute", "challenge_id": requested["challenge"]["challenge_id"]}
        dispatch(command, execute_arguments, operator=True, pid=41999)
        wait_phase(execution_id, phase)
    factory.assert_script_consumed()
    stream_calls = [call for call in factory.calls if call.operation is FakeFluidNCOperation.STREAM_PROGRAM]
    assert len(stream_calls) == 1 and stream_calls[0].request.program == program
    final = dispatch("machine.status", {"schema_version": 1, "execution_id": execution_id})["execution"]
finally:
    runtime.stop()
    del runtime
    gc.collect()

after_threads = tuple(thread.name for thread in threading.enumerate())
after_children = tuple(child.pid for child in multiprocessing.active_children())
after_fd_targets = fd_targets()
after_fds = len(after_fd_targets)
assert after_threads == before_threads
assert after_children == before_children
print(json.dumps({
    "module": str(pathlib.Path(drawingmachine.__file__).resolve()),
    "package_root": str(pathlib.Path(str(importlib.resources.files("drawingmachine"))).resolve()),
    "sitecustomize": str(pathlib.Path(sitecustomize.__file__).resolve()),
    "isolated_environment": all(name not in os.environ for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")),
    "sys_path": [str(pathlib.Path(value).resolve()) for value in sys.path if value],
    "machine_phase": final["phase"],
    "operations": [operation.value for operation in factory.started_operations],
    "program_sha256": program.gcode_sha256,
    "program_size": program.source_size_bytes,
    "program_commands": [[item.source_line, item.command] for item in program.commands],
    "replay": replay,
    "purposes": purposes,
    "socket_exists": paths.socket_file.exists(),
    "threads": list(after_threads),
    "children": list(after_children),
    "fds_before": before_fds,
    "fds_after": after_fds,
    "fd_targets_before": before_fd_targets,
    "fd_targets_after": after_fd_targets,
}, sort_keys=True))
"""


def _run_installed_machine(
    installed_drawingmachine: Path,
    service: Any,
    package_c_audit_guard: Any,
    repository_root: Path,
    child_root: Path,
    job_id: str,
) -> dict[str, Any]:
    installed_site = installed_drawingmachine.parent.parent / "lib"
    installed_site = next(installed_site.glob("python*/site-packages")).resolve()
    dependencies_pth = installed_site / "drawingmachine-local-dependencies.pth"
    dependencies = Path(dependencies_pth.read_text(encoding="utf-8").strip()).resolve()
    installed_dependencies = [installed_site / name for name in ("PIL", "pillow.libs", "serial")]
    audit_source = package_c_audit_guard.root / "package-c-audit-guard/sitecustomize.py"
    audit_text = audit_source.read_text(encoding="utf-8").replace(
        "\nimport coverage\ncoverage.process_startup()\n", "\n"
    )
    sitecustomize = installed_site / "sitecustomize.py"
    for target in installed_dependencies:
        shutil.copytree(dependencies / target.name, target)
    sitecustomize.write_text(audit_text, encoding="utf-8")
    dependencies_pth.unlink()
    environment = package_c_audit_guard.environment.copy()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        environment[name] = service.environment[name]
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    try:
        result = subprocess.run(
            [
                str(installed_drawingmachine.with_name("python")),
                "-c",
                _INSTALLED_MACHINE_SCRIPT,
                str(child_root),
                job_id,
                str(repository_root),
                str(installed_site),
            ],
            cwd=child_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=45.0,
        )
    finally:
        sitecustomize.unlink()
        for target in installed_dependencies:
            shutil.rmtree(target)
        dependencies_pth.write_text(f"{dependencies}\n", encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    return cast(dict[str, Any], json.loads(result.stdout))


def test_installed_wheel_runs_checkout_independent_complete_final_shape(
    installed_drawingmachine: Path,
    package_b_guarded_service: Any,
    package_c_audit_guard: Any,
    service_access_test_harness: Any,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    service = package_b_guarded_service
    private = tmp_path / "automation-private"
    private.mkdir(mode=0o700)
    caller_data = private / "caller-data"
    caller_data.mkdir()
    automation = service_access_test_harness.client_environment(service.environment, "automation")
    automation["XDG_DATA_HOME"] = str(caller_data)
    service.client_environment = automation

    image = private / "source.png"
    canvas = Image.new("L", (64, 64), color=255)
    ImageDraw.Draw(canvas).rectangle((12, 12, 52, 52), outline=0, width=4)
    canvas.save(image)
    image.chmod(0o600)
    submitted = service.run_json(
        "workflow", "run", "--job-name", "stage2-final-shape", "--input-image", str(image), "--route-mode", "direct"
    )
    blocked = service.wait(submitted["data"]["job_id"])
    assert _job(blocked)["state"] == "BLOCKED"
    assert _job(blocked)["blocker"]["code"] == "REVIEW_REQUIRED"
    job = _job(blocked)
    processed = _artifact(blocked, "processed_image")
    exported = (
        service_access_test_harness.root / "automation-data/exports/jobs" / job["job_id"] / processed["relative_path"]
    )
    assert exported.is_file()
    review = private / "review.json"
    _write_review(review, job, exported)
    resumed = service.run_json("workflow", "run", "--resume-job", job["job_id"], "--review-json", str(review))
    assert resumed["data"]["state"] == "IMAGE_READY"
    ready = service.wait(job["job_id"])
    assert _job(ready)["state"] == "READY_TO_RUN"

    caller_gcode = private / "caller.gcode"
    caller_gcode.write_text("G21\nG90\nG54\nG0 Z3.5\nG0 X1 Y1 F1200\n; End\n", encoding="ascii")
    checked_gcode = service.run_json(
        "gcode", "check", str(caller_gcode), "--timeout-seconds", "30", "--poll-interval-seconds", "0.02"
    )
    assert isinstance(checked_gcode["data"]["static_result"], dict)

    safe_root = Path(tempfile.mkdtemp(prefix="dm-d15-openclaw-", dir=Path.home()))
    try:
        registry_root, managed_root = _configure_openclaw(service, safe_root)
        operator = service_access_test_harness.client_environment(service.environment, "operator")
        service.client_environment = operator
        first = service.run_json("openclaw", "check", "--openclaw-root", str(registry_root), expected_returncode=1)
        assert first["error"]["code"] == "OPENCLAW_CHECK_NOT_IN_SYNC"
        installed = service.run_json("openclaw", "install", "--openclaw-root", str(registry_root))
        final = service.run_json("openclaw", "check", "--openclaw-root", str(registry_root))
        assert installed["data"]["installed"] is True
        assert final["data"]["in_sync"] is True
        managed_targets = {path.relative_to(managed_root).as_posix(): path for path in managed_root.rglob("*.md")}
        assert set(managed_targets) == {
            "agents/cnc-drawable-router/AGENTS.md",
            "agents/cnc-drawable-router/TOOLS.md",
            "agents/cnc-drawable-translator/AGENTS.md",
            "agents/cnc-drawable-translator/TOOLS.md",
        }
        assert all(path.stat().st_mode & 0o777 == 0o640 for path in managed_targets.values())
        assert all(path.stat().st_uid == os.getuid() for path in managed_targets.values())
        assert not tuple(managed_root.rglob(".drawingmachine-openclaw-*"))

        service.stop()
        evidence = _run_installed_machine(
            installed_drawingmachine,
            service,
            package_c_audit_guard,
            repository_root,
            tmp_path,
            job["job_id"],
        )
    finally:
        shutil.rmtree(safe_root)

    assert not safe_root.exists()

    assert evidence["machine_phase"] == "COMPLETED"
    assert evidence["operations"] == [
        "INITIAL_PREFLIGHT",
        "HOME_ALL_AXES",
        "Z_CALIBRATION",
        "Z_CONFIRMATION_RAISE",
        "STREAM_PROGRAM",
        "HOME_Z_AXIS",
        "CLOSE",
    ]
    assert all(cast(dict[str, bool], evidence["replay"]).values())
    assert evidence["socket_exists"] is False
    assert evidence["threads"] == ["MainThread"]
    assert evidence["children"] == []
    assert Counter(evidence["fd_targets_after"]) == Counter(evidence["fd_targets_before"]), json.dumps(
        evidence, indent=2
    )
    assert evidence["isolated_environment"] is True
    assert not Path(cast(str, evidence["module"])).is_relative_to(repository_root)
    assert not Path(cast(str, evidence["package_root"])).is_relative_to(repository_root)
    assert Path(cast(str, evidence["sitecustomize"])).is_relative_to(installed_drawingmachine.parent.parent / "lib")
    assert all(
        not Path(value).resolve().is_relative_to(repository_root) for value in cast(list[str], evidence["sys_path"])
    )
    assert service.forbidden_events() == []
    assert package_c_audit_guard.events() == []
    assert tuple(caller_data.rglob("*")) == ()


def test_fd_target_multiset_rejects_an_equal_count_swap() -> None:
    assert Counter(("socket:[1]", "pipe:[2]")) == Counter(("pipe:[2]", "socket:[1]"))
    assert Counter(("socket:[1]", "pipe:[2]")) != Counter(("pipe:[2]", "socket:[3]"))
