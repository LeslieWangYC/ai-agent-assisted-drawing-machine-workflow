from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

import drawingmachine

ROOT = Path(__file__).resolve().parents[2]


def test_package_version_and_module_help() -> None:
    assert drawingmachine.__version__ == "0.2.0.dev0"
    result = subprocess.run(
        [sys.executable, "-m", "drawingmachine", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "drawingmachine" in result.stdout


def test_packaged_migrations_are_readable_resources() -> None:
    migrations = files("drawingmachine.adapters.persistence.migrations")
    names = sorted(item.name for item in migrations.iterdir() if item.name.endswith(".sql"))

    assert names == [
        "0001_foundation.sql",
        "0002_offline_job_chain.sql",
        "0003_hardware_execution.sql",
        "0004_staging_admissions.sql",
        "0005_recover_binding_current_epoch.sql",
    ]
    assert "CREATE TABLE audit_events" in migrations.joinpath("0001_foundation.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE jobs" in migrations.joinpath("0002_offline_job_chain.sql").read_text(encoding="utf-8")


def test_runtime_dependency_policy_replaces_stage1_requirements_files() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == ["Pillow>=10,<13", "pyserial>=3.5,<4"]


def test_dev_dependency_and_toolchain_policy_is_centralized_in_pyproject() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["optional-dependencies"]["dev"] == [
        "build>=1,<2",
        "import-linter>=2,<3",
        "mypy>=1.11,<2",
        "pytest>=8,<9",
        "pytest-cov>=5,<7",
        "ruff>=0.5,<1",
    ]
    assert document["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert document["tool"]["mypy"]["strict"] is True
    assert document["tool"]["ruff"]["target-version"] == "py311"
