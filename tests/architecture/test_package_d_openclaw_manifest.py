from __future__ import annotations

from pathlib import Path


def test_every_d3_openclaw_authority_module_is_in_the_safety_manifest() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    expected = {
        "src/drawingmachine/config/openclaw_deployment.py",
        "src/drawingmachine/domain/openclaw/models.py",
        "src/drawingmachine/domain/openclaw/manifest.py",
        "src/drawingmachine/ports/openclaw.py",
        "src/drawingmachine/application/openclaw.py",
        "src/drawingmachine/adapters/filesystem/openclaw_install.py",
    }
    assert expected <= manifest


def test_openclaw_adapter_has_no_chown_chgrp_sudo_proxy_or_live_root_default() -> None:
    sources = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/drawingmachine/config/openclaw_deployment.py",
            "src/drawingmachine/application/openclaw.py",
            "src/drawingmachine/adapters/filesystem/openclaw_install.py",
        )
    )
    for forbidden in ("os.chown", "os.chgrp", "sudo", "proxy", "Path.home", '"/.openclaw"', '"~/.openclaw"'):
        assert forbidden not in sources
