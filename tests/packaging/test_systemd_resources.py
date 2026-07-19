from __future__ import annotations

from importlib.resources import files


def _unit(name: str) -> str:
    return files("drawingmachine.resources.systemd").joinpath(name).read_text(encoding="utf-8")


def test_service_is_a_fixed_hardened_user_unit() -> None:
    unit = _unit("drawingmachine.service")
    assert "User=" not in unit and "Group=" not in unit
    assert "ExecStart=%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run" in unit
    assert "UMask=0077" in unit
    assert "Restart=on-failure" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "PrivateTmp=true" in unit
    assert (
        "ReadWritePaths=%h/.local/state/drawingmachine %h/.local/share/drawingmachine "
        "%t/drawingmachine @OPENCLAW_MANAGED_ROOT@" in unit
    )
    assert "ReadOnlyPaths=%h/.config/drawingmachine @OPENCLAW_REGISTRY_ROOT@" in unit
    for forbidden in ("/usr/bin/env", "PATH=", "WorkingDirectory=", "scripts/", "systemctl", "sudo", "chown", "chgrp"):
        assert forbidden not in unit


def test_client_unit_only_rebuilds_its_volatile_endpoint() -> None:
    unit = _unit("drawingmachine-client-endpoint.service")
    assert "User=" not in unit and "Group=" not in unit
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert (
        "ExecStart=%h/.local/lib/drawingmachine/venv/bin/python "
        "-m drawingmachine.install.client_endpoint runtime" in unit
    )
    assert "--policy /home/drawingmachine-service/.config/drawingmachine/service-access.toml" in unit
    assert "--expected-policy-sha256 @SERVICE_ACCESS_SHA256@" in unit
    assert "drawingmachine.service" not in unit
    for forbidden in (
        "/usr/bin/env",
        "PATH=",
        "WorkingDirectory=",
        "systemctl",
        "enable",
        "start",
        "reload",
        "sudo",
        "chown",
        "chgrp",
        " data",
    ):
        assert forbidden not in unit
