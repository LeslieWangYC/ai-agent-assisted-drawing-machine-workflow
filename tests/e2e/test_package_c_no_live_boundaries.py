from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from drawingmachine.bootstrap import build_runtime


def test_package_c_audit_guard_rejects_each_forbidden_live_boundary(package_c_audit_guard: Any) -> None:
    guard = package_c_audit_guard
    for probe, expected_event in (
        ("inet4", "inet4"),
        ("inet6", "inet6"),
        ("serial-import", "serial-import"),
        ("serial-device", "serial-device"),
        ("serial-open", "serial-open"),
        ("openclaw-cli", "openclaw-cli"),
        ("openclaw-path", "openclaw-cli"),
        ("openclaw-shell", "openclaw-cli"),
        ("systemd", "systemd"),
        ("systemd-path", "systemd"),
        ("systemd-shell", "systemd"),
        ("outside-xdg-unix", "outside-xdg-unix"),
    ):
        result = guard.run_probe(probe)
        assert result.returncode != 0, result.stdout + result.stderr
        assert guard.events()[-1] == expected_event


def test_fake_injection_is_object_only_and_has_no_profile_or_environment_selector(repository_root: Path) -> None:
    parameters = inspect.signature(build_runtime).parameters
    assert {"fluidnc_factory", "machine_identity_factory"} <= parameters.keys()
    source = (repository_root / "src/drawingmachine/bootstrap.py").read_text(encoding="utf-8")
    assert "StrictFakeFluidNCFactory" not in source
    assert "DRAWINGMACHINE_FAKE" not in source
    assert "fake_fluidnc" not in source
