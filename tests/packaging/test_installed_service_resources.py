from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_MIGRATION_SHA256 = {
    "0001_foundation.sql": "65449ddd1aeb5c1fa3d4b85500ddd9de5f6444be33f1b12101949959232ccac6",
    "0002_offline_job_chain.sql": "49de58e965a88da77d6ff10b22a3f69fa8abdc886a9163063b75f56d81f4f495",
    "0003_hardware_execution.sql": "d25849bc7feb0e57daba961ba4fbdd96b74f93bb4e22dc704da42681adc231f5",
    "0004_staging_admissions.sql": "fd9f3c79503bc8271ebeded60379f0f08414d25ab557a2b2a19b2232650167d6",
    "0005_recover_binding_current_epoch.sql": "147985e9249cfd42cc8ca941cb658870ac2f8c97dec7fbcf37a357bdc9eafc53",
}

_INSTALLED_HOSTILE_TESTS = (
    "tests/unit/ports/test_client_data.py",
    "tests/unit/adapters/test_client_data.py",
    "tests/unit/application/test_staging_reconciliation.py",
    "tests/integration/filesystem/test_client_data_plane.py",
    "tests/integration/persistence/test_staging_admissions.py",
    "tests/integration/service/test_cross_principal_workflow.py",
    "tests/integration/service/test_runtime.py",
    "tests/integration/service/test_staging_reconciliation.py",
    "tests/integration/service/test_multi_principal_topology.py",
    "tests/cli/test_workflow_cli.py",
    "tests/cli/test_gcode_cli.py",
    "tests/packaging/test_install_resources.py",
    "tests/integration/service/test_openclaw_commands.py",
    "tests/integration/service/test_service_process.py",
    "tests/e2e/test_machine_safety_failures.py",
    "tests/integration/filesystem/test_openclaw_install.py",
)
_REQUIRED_INSTALLED_HOSTILE_TESTS = frozenset(
    {
        "tests/unit/adapters/test_client_data.py",
        "tests/integration/persistence/test_staging_admissions.py",
        "tests/integration/service/test_cross_principal_workflow.py",
        "tests/integration/service/test_runtime.py",
        "tests/cli/test_workflow_cli.py",
        "tests/cli/test_gcode_cli.py",
        "tests/packaging/test_install_resources.py",
        "tests/integration/service/test_openclaw_commands.py",
        "tests/integration/service/test_service_process.py",
        "tests/e2e/test_machine_safety_failures.py",
        "tests/integration/filesystem/test_openclaw_install.py",
    }
)


def _run_installed(python: Path, cwd: Path, script: str, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [str(python), "-c", script, *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_fresh_wheel_uses_the_fixed_service_executable_layout(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    assert installed_drawingmachine.parts[-6:] == (
        ".local",
        "lib",
        "drawingmachine",
        "venv",
        "bin",
        "drawingmachine",
    )
    python = installed_drawingmachine.with_name("python")
    result = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.resources, json, pathlib, drawingmachine; "
                "root=importlib.resources.files('drawingmachine.resources'); "
                "print(json.dumps({'module': drawingmachine.__file__, "
                "'manifest': str(root.joinpath('install/manifest.json'))}))"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert "site-packages" in Path(evidence["module"]).parts
    assert "site-packages" in Path(evidence["manifest"]).parts


def test_installed_resources_migrations_and_schema_v4_are_digest_bound(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    evidence = _run_installed(
        installed_drawingmachine.with_name("python"),
        tmp_path,
        """
import hashlib, importlib.resources, json, pathlib, sqlite3, sys
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.install import client_endpoint

resources = importlib.resources.files("drawingmachine.resources")
migrations = importlib.resources.files("drawingmachine.adapters.persistence.migrations")
manifest = resources.joinpath("install/manifest.json").read_bytes()
document = json.loads(manifest)
resource_digests = {
    entry["resource"]: hashlib.sha256(resources.joinpath(entry["resource"]).read_bytes()).hexdigest()
    for entry in document["resources"]
}
migration_digests = {
    name: hashlib.sha256(migrations.joinpath(name).read_bytes()).hexdigest()
    for name in (
        "0001_foundation.sql", "0002_offline_job_chain.sql",
        "0003_hardware_execution.sql", "0004_staging_admissions.sql",
        "0005_recover_binding_current_epoch.sql",
    )
}
database = pathlib.Path(sys.argv[1])
repository = SQLiteRepository(database, clock=SystemClock())
repository.initialize()
with sqlite3.connect(database) as connection:
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    columns = [row[1] for row in connection.execute("PRAGMA table_info(staging_admissions)")]
    indices = sorted(row[1] for row in connection.execute("PRAGMA index_list(staging_admissions)"))
print(json.dumps({
    "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    "frozen_manifest_sha256": client_endpoint._INSTALL_MANIFEST_SHA256,
    "resource_count": len(document["resources"]),
    "resources_match": all(
        resource_digests[entry["resource"]] == entry["sha256"] for entry in document["resources"]
    ),
    "migration_digests": migration_digests,
    "version": version,
    "columns": columns,
    "indices": indices,
}))
""",
        str(tmp_path / "installed-schema.db"),
    )
    assert evidence["manifest_sha256"] == evidence["frozen_manifest_sha256"]
    assert evidence["resource_count"] == 8
    assert evidence["resources_match"] is True
    assert evidence["migration_digests"] == _MIGRATION_SHA256
    assert evidence["version"] == 5
    assert evidence["columns"] == [
        "schema_version",
        "request_id",
        "role",
        "bundle_name",
        "payload_device",
        "payload_inode",
        "payload_size_bytes",
        "source_sha256",
        "lease_sha256",
        "publisher_boot_id",
        "published_monotonic_ns",
        "deadline_monotonic_ns",
        "state",
        "job_id",
        "revision",
        "created_at_utc",
        "updated_at_utc",
        "recovery_code",
    ]
    assert evidence["indices"] == [
        "idx_staging_admissions_state_deadline",
        "sqlite_autoindex_staging_admissions_1",
        "sqlite_autoindex_staging_admissions_2",
        "sqlite_autoindex_staging_admissions_3",
    ]


def test_installed_units_and_openclaw_cli_have_fixed_socket_only_authority(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    evidence = _run_installed(
        installed_drawingmachine.with_name("python"),
        tmp_path,
        """
import importlib.resources, json

resources = importlib.resources.files("drawingmachine.resources")
service = resources.joinpath("systemd/drawingmachine.service").read_text()
client = resources.joinpath("systemd/drawingmachine-client-endpoint.service").read_text()
cli = importlib.resources.files("drawingmachine.cli").joinpath("openclaw.py").read_text()
print(json.dumps({
    "service_fixed": "%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run" in service,
    "client_fixed": "%h/.local/lib/drawingmachine/venv/bin/python -m drawingmachine.install.client_endpoint" in client,
    "no_env": "/usr/bin/env" not in service + client,
    "no_systemctl": "systemctl" not in service + client,
    "socket_client": "ServiceClient" in cli and ".call(" in cli,
    "no_local_writer": all(token not in cli for token in ("write_text(", "write_bytes(", "shutil", "subprocess", "os.replace")),
}))
""",
    )
    assert evidence == {
        "service_fixed": True,
        "client_fixed": True,
        "no_env": True,
        "no_systemctl": True,
        "socket_client": True,
        "no_local_writer": True,
    }


def test_installed_client_helper_fails_closed_before_mutation_on_missing_policy(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            str(installed_drawingmachine.with_name("python")),
            "-m",
            "drawingmachine.install.client_endpoint",
            "runtime",
            "--policy",
            str(tmp_path / "missing.toml"),
            "--expected-policy-sha256",
            "0" * 64,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "client endpoint setup failed" in result.stderr
    assert tuple(tmp_path.iterdir()) == ()


def test_installed_wheel_passes_frozen_hostile_control_and_data_matrices(
    installed_drawingmachine: Path,
    repository_root: Path,
) -> None:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith(("COV_CORE_", "COVERAGE_")):
            environment.pop(name)
    python = installed_drawingmachine.with_name("python")
    provenance = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import pathlib, drawingmachine; "
                "path=pathlib.Path(drawingmachine.__file__).resolve(); "
                "assert 'site-packages' in path.parts; print(path)"
            ),
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert provenance.returncode == 0, provenance.stdout + provenance.stderr
    assert not Path(provenance.stdout.strip()).is_relative_to(repository_root / "src")
    hostile = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            *_INSTALLED_HOSTILE_TESTS,
            "-q",
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=360.0,
    )
    assert hostile.returncode == 0, hostile.stdout + hostile.stderr
    assert "failed" not in hostile.stdout
    assert "492 passed" in hostile.stdout


def test_installed_selector_contains_every_frozen_d9_matrix() -> None:
    assert frozenset(_INSTALLED_HOSTILE_TESTS) >= _REQUIRED_INSTALLED_HOSTILE_TESTS
