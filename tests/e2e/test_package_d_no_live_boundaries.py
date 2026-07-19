from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest


@pytest.mark.parametrize(
    ("probe", "expected_event"),
    [
        ("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)", "inet4"),
        ("import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM)", "inet6"),
        ("open('/dev/ttyS0', 'rb')", "serial-device"),
        ("import serial", "serial-import"),
        ("import subprocess; subprocess.run(['openclaw', 'status'])", "openclaw-cli"),
        ("import subprocess; subprocess.run(['systemctl', '--version'])", "systemd"),
        (
            "import socket; sock=socket.socket(socket.AF_UNIX); sock.connect('/tmp/d15-outside.sock')",
            "outside-xdg-unix",
        ),
    ],
)
def test_installed_child_audit_rejects_every_live_boundary(
    installed_drawingmachine: Path,
    package_c_audit_guard: Any,
    tmp_path: Path,
    probe: str,
    expected_event: str,
) -> None:
    before = package_c_audit_guard.events()
    result = subprocess.run(
        [str(installed_drawingmachine.with_name("python")), "-c", probe],
        cwd=tmp_path,
        env=package_c_audit_guard.environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert result.returncode != 0
    assert "Package C live boundary denied" in result.stderr
    assert package_c_audit_guard.events() == [*before, expected_event]


def test_installed_wheel_has_no_checkout_or_legacy_runtime_surface(
    installed_drawingmachine: Path,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    script = r"""
import importlib.resources, importlib.util, json, pathlib, sys
import drawingmachine

legacy = ("cnc", "drawable_path", "pipelines")
resources = importlib.resources.files("drawingmachine")
print(json.dumps({
    "module": str(pathlib.Path(drawingmachine.__file__).resolve()),
    "resources": str(pathlib.Path(str(resources)).resolve()),
    "sys_path": [str(pathlib.Path(value).resolve()) for value in sys.path if value],
    "legacy_specs": {name: importlib.util.find_spec(name) is not None for name in legacy},
    "legacy_resources": sorted(
        str(path.relative_to(pathlib.Path(str(resources))))
        for path in pathlib.Path(str(resources)).rglob("*")
        if any(part in legacy for part in path.parts)
    ),
}))
"""
    result = subprocess.run(
        [str(installed_drawingmachine.with_name("python")), "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = cast(dict[str, Any], json.loads(result.stdout))
    assert evidence["legacy_specs"] == {"cnc": False, "drawable_path": False, "pipelines": False}
    assert evidence["legacy_resources"] == []
    assert not Path(cast(str, evidence["module"])).is_relative_to(repository_root)
    assert not Path(cast(str, evidence["resources"])).is_relative_to(repository_root)
    assert repository_root.resolve() not in {Path(value).resolve() for value in cast(list[str], evidence["sys_path"])}
