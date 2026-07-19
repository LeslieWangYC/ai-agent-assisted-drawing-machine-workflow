import hashlib
import json
import socket
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from queue import Queue

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_built_wheel_exposes_cli(installed_drawingmachine: Path) -> None:
    result = subprocess.run(
        [str(installed_drawingmachine), "--help"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "drawingmachine" in result.stdout


@pytest.mark.parametrize("command", ["check", "install"])
def test_installed_wheel_exposes_closed_openclaw_help(
    installed_drawingmachine: Path,
    command: str,
) -> None:
    result = subprocess.run(
        [str(installed_drawingmachine), "openclaw", command, "--help"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--openclaw-root" in result.stdout
    assert "--check-registry" not in result.stdout
    assert result.stderr == ""


def test_package_c_wheel_contains_runtime_resources_machine_chain_and_all_worker_modules(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")

    required = {
        "drawingmachine/py.typed",
        "drawingmachine/adapters/persistence/migrations/0001_foundation.sql",
        "drawingmachine/adapters/persistence/migrations/0002_offline_job_chain.sql",
        "drawingmachine/adapters/persistence/migrations/0003_hardware_execution.sql",
        "drawingmachine/adapters/persistence/migrations/0004_staging_admissions.sql",
        "drawingmachine/adapters/persistence/migrations/0005_recover_binding_current_epoch.sql",
        "drawingmachine/adapters/hardware/fake_fluidnc.py",
        "drawingmachine/adapters/hardware/serial_fluidnc.py",
        "drawingmachine/application/machine/execution.py",
        "drawingmachine/bootstrap.py",
        "drawingmachine/service/machine_coordinator.py",
        "drawingmachine/service/machine_protocol.py",
        "drawingmachine/adapters/workers/process_supervisor.py",
        "drawingmachine/adapters/workers/task_registry.py",
        "drawingmachine/adapters/workers/worker_entrypoint.py",
        "drawingmachine/adapters/workers/tasks/__init__.py",
        "drawingmachine/adapters/workers/tasks/_payload_validation.py",
        "drawingmachine/adapters/workers/tasks/gcode.py",
        "drawingmachine/adapters/workers/tasks/image.py",
        "drawingmachine/adapters/workers/tasks/planning.py",
    }
    assert required <= names
    assert "Requires-Dist: Pillow<13,>=10" in metadata
    assert "Requires-Dist: pyserial<4,>=3.5" in metadata


def test_package_b_wheel_has_no_runtime_test_fixture_dependency(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
        python_sources = {
            name: archive.read(name).decode("utf-8")
            for name in names
            if name.startswith("drawingmachine/") and name.endswith(".py")
        }

    assert not any(name.startswith(("tests/", "test/")) or "/fixtures/" in name for name in names)
    forbidden = ("tests.fixtures", "tests/fixtures", "fixtures/package_b")
    assert {name: token for name, source in python_sources.items() for token in forbidden if token in source} == {}


def test_package_d_wheel_contains_only_declared_openclaw_resources_from_package_sources(built_wheel: Path) -> None:
    package_root = _REPOSITORY_ROOT / "src/drawingmachine/resources/openclaw"
    expected = {
        "drawingmachine/resources/openclaw/__init__.py",
        "drawingmachine/resources/openclaw/manifest.json",
        "drawingmachine/resources/openclaw/registry-policy.json",
        "drawingmachine/resources/openclaw/agents/cnc-drawable-translator/AGENTS.md",
        "drawingmachine/resources/openclaw/agents/cnc-drawable-translator/TOOLS.md",
        "drawingmachine/resources/openclaw/agents/cnc-drawable-router/AGENTS.md",
        "drawingmachine/resources/openclaw/agents/cnc-drawable-router/TOOLS.md",
    }
    with zipfile.ZipFile(built_wheel) as archive:
        actual = {name for name in archive.namelist() if name.startswith("drawingmachine/resources/openclaw/")}
        assert actual == expected
        for name in expected:
            info = archive.getinfo(name)
            assert stat.S_IFMT(info.external_attr >> 16) != stat.S_IFLNK
            relative = name.removeprefix("drawingmachine/resources/openclaw/")
            assert archive.read(name) == (package_root / relative).read_bytes()

        names = archive.namelist()
        forbidden_parts = {"openclaw_agents", ".openclaw", "workspace", "memory", "identity", "prompt", "session"}
        assert not any(forbidden_parts & {part.lower() for part in Path(name).parts} for name in names)
        assert not any(name.endswith(("openclaw.json", "secrets.json", ".db", ".sock", ".pid")) for name in names)


def test_package_d_wheel_contains_exact_digested_install_resources_and_helper(built_wheel: Path) -> None:
    package_root = _REPOSITORY_ROOT / "src/drawingmachine/resources"
    manifest = json.loads((package_root / "install/manifest.json").read_bytes())
    expected_resources = {f"drawingmachine/resources/{entry['resource']}" for entry in manifest["resources"]}
    expected = expected_resources | {
        "drawingmachine/install/__init__.py",
        "drawingmachine/install/client_endpoint.py",
        "drawingmachine/resources/systemd/__init__.py",
        "drawingmachine/resources/install/__init__.py",
        "drawingmachine/resources/install/manifest.json",
    }
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        actual = {
            name
            for name in names
            if name.startswith(
                (
                    "drawingmachine/install/",
                    "drawingmachine/resources/install/",
                    "drawingmachine/resources/systemd/",
                )
            )
        }
        assert actual == expected
        for entry in manifest["resources"]:
            wheel_name = f"drawingmachine/resources/{entry['resource']}"
            contents = archive.read(wheel_name)
            assert contents == (package_root / entry["resource"]).read_bytes()
            assert hashlib.sha256(contents).hexdigest() == entry["sha256"]
        assert (
            archive.read("drawingmachine/resources/install/manifest.json")
            == (package_root / "install/manifest.json").read_bytes()
        )
        assert b"/usr/bin/env" not in archive.read("drawingmachine/resources/systemd/drawingmachine.service")


def test_systemd_user_unit_uses_packaged_authority() -> None:
    packaged = _REPOSITORY_ROOT / "src/drawingmachine/resources/systemd/drawingmachine.service"
    assert packaged.is_file()
    assert b"/usr/bin/env" not in packaged.read_bytes()


def _hold_subprocess_from_worker_thread(
    ready: Queue[tuple[subprocess.Popen[bytes], int]],
    release: threading.Event,
) -> None:
    script = """
import signal
import subprocess
import sys
import time

grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print(grandchild.pid, flush=True)

def stop(_signum, _frame):
    grandchild.terminate()
    grandchild.wait(timeout=5.0)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    assert child.stdout is not None
    grandchild_pid = int(child.stdout.readline())
    ready.put((child, grandchild_pid))
    release.wait(timeout=30.0)
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=5.0)


def test_package_c_residue_detector_fails_closed_on_ignored_products_and_thread_descendant() -> None:
    from tests.conftest import (
        assert_package_c_repository_residue_unchanged,
        package_c_descendant_pids,
        package_c_repository_residue_snapshot,
        restore_package_c_repository_residue_snapshot,
    )

    before = package_c_repository_residue_snapshot(_REPOSITORY_ROOT)
    build_marker = _REPOSITORY_ROOT / "build/c14-hostile.marker"
    egg_info = _REPOSITORY_ROOT / "src/c14-hostile.egg-info"
    assert not build_marker.exists() and not egg_info.exists()
    ready: Queue[tuple[subprocess.Popen[bytes], int]] = Queue()
    release = threading.Event()
    worker = threading.Thread(target=_hold_subprocess_from_worker_thread, args=(ready, release))
    worker.start()
    child, grandchild_pid = ready.get(timeout=5.0)
    try:
        build_marker.parent.mkdir(exist_ok=True)
        build_marker.write_text("hostile ignored build product", encoding="utf-8")
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("hostile ignored egg-info", encoding="utf-8")
        assert {child.pid, grandchild_pid} <= set(package_c_descendant_pids())
        with pytest.raises(AssertionError, match="repository residue detected") as denied:
            assert_package_c_repository_residue_unchanged(_REPOSITORY_ROOT, before)
        message = str(denied.value)
        assert "build/c14-hostile.marker" in message
        assert "src/c14-hostile.egg-info" in message
        assert str(child.pid) in message
        assert str(grandchild_pid) in message
    finally:
        release.set()
        worker.join(timeout=10.0)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5.0)
        restore_package_c_repository_residue_snapshot(_REPOSITORY_ROOT, before)
    assert not worker.is_alive()
    assert {child.pid, grandchild_pid}.isdisjoint(package_c_descendant_pids())
    assert_package_c_repository_residue_unchanged(_REPOSITORY_ROOT, before, check_descendants=False)


def test_package_c_residue_descendant_baseline_requires_exact_unchanged_set(tmp_path: Path) -> None:
    from tests.conftest import (
        assert_package_c_repository_residue_unchanged,
        package_c_descendant_pids,
        package_c_repository_residue_snapshot,
    )

    before = package_c_repository_residue_snapshot(tmp_path)
    baseline_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        baseline = package_c_descendant_pids()
        assert baseline_child.pid in baseline
        assert_package_c_repository_residue_unchanged(tmp_path, before, expected_descendants=baseline)
        added_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with pytest.raises(AssertionError, match="descendants_added") as added:
                assert_package_c_repository_residue_unchanged(tmp_path, before, expected_descendants=baseline)
            assert str(added_child.pid) in str(added.value)
        finally:
            added_child.terminate()
            added_child.wait(timeout=5.0)
        assert_package_c_repository_residue_unchanged(tmp_path, before, expected_descendants=baseline)
    finally:
        baseline_child.terminate()
        baseline_child.wait(timeout=5.0)
    with pytest.raises(AssertionError, match="descendants_missing") as missing:
        assert_package_c_repository_residue_unchanged(tmp_path, before, expected_descendants=baseline)
    assert str(baseline_child.pid) in str(missing.value)


def test_package_c_residue_snapshot_restores_preexisting_content_mode_and_symlinks(tmp_path: Path) -> None:
    from tests.conftest import (
        assert_package_c_repository_residue_unchanged,
        package_c_repository_residue_snapshot,
        restore_package_c_repository_residue_snapshot,
    )

    build_file = tmp_path / "build/preexisting.txt"
    dist_target = tmp_path / "dist/target.txt"
    dist_link = tmp_path / "dist/preexisting-link"
    egg_info = tmp_path / "src/preexisting.egg-info/PKG-INFO"
    coverage_file = tmp_path / ".coverage.preexisting"
    runtime_files = tuple(
        tmp_path / "runtime" / name
        for name in ("drawingmachine.db", "drawingmachine.db-wal", "drawingmachine.db-shm", "service.pid")
    )
    socket_path = tmp_path / "runtime/preexisting.sock"
    for path in (build_file, dist_target, egg_info, coverage_file, *runtime_files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"before:{path.name}", encoding="utf-8")
        path.chmod(0o640)
    dist_link.symlink_to("target.txt")
    preexisting_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    new_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    preexisting_socket.bind(str(socket_path))
    new_socket_path = tmp_path / "runtime/new.sock"
    try:
        before = package_c_repository_residue_snapshot(tmp_path)
        before_entries = {entry.relative_path: entry for entry in before.entries}
        assert {
            "build/preexisting.txt",
            "dist/preexisting-link",
            "src/preexisting.egg-info/PKG-INFO",
            ".coverage.preexisting",
            "runtime/drawingmachine.db",
            "runtime/drawingmachine.db-wal",
            "runtime/drawingmachine.db-shm",
            "runtime/service.pid",
            "runtime/preexisting.sock",
        } <= set(before_entries)

        for path in (build_file, dist_target, coverage_file, *runtime_files):
            path.write_text(f"changed:{path.name}", encoding="utf-8")
            path.chmod(0o600)
        dist_link.unlink()
        dist_link.symlink_to("changed-target.txt")
        egg_info.unlink()
        new_paths = (
            tmp_path / "dist/new.txt",
            tmp_path / "coverage.json",
            tmp_path / "runtime/new.pid",
        )
        for path in new_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new", encoding="utf-8")
        new_socket.bind(str(new_socket_path))
        with pytest.raises(AssertionError, match="repository residue detected"):
            assert_package_c_repository_residue_unchanged(tmp_path, before, check_descendants=False)

        restore_package_c_repository_residue_snapshot(tmp_path, before)
        assert package_c_repository_residue_snapshot(tmp_path) == before
        assert all(
            path.read_text(encoding="utf-8") == f"before:{path.name}"
            for path in (build_file, dist_target, egg_info, coverage_file, *runtime_files)
        )
        assert all(
            path.stat().st_mode & 0o7777 == 0o640
            for path in (build_file, dist_target, egg_info, coverage_file, *runtime_files)
        )
        assert dist_link.readlink() == Path("target.txt")
        assert all(not path.exists() for path in (*new_paths, new_socket_path))
    finally:
        preexisting_socket.close()
        new_socket.close()
