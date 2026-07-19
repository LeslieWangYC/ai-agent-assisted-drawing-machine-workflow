from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

_RECOVERY_SCRIPT = r"""
from __future__ import annotations

import importlib.resources
import json
import pathlib
import sys
from datetime import UTC, datetime

import drawingmachine
from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.adapters.filesystem.client_data import FilesystemClientInputStaging, FilesystemServiceClientData
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.ports.client_data import ClientDataRole

root = pathlib.Path(sys.argv[1])
repository_root = pathlib.Path(sys.argv[2]).resolve()
root.mkdir()
assert not pathlib.Path(drawingmachine.__file__).resolve().is_relative_to(repository_root)
assert not pathlib.Path(str(importlib.resources.files("drawingmachine"))).resolve().is_relative_to(repository_root)
assert repository_root not in {pathlib.Path(item).resolve() for item in sys.path if item}

class Clock:
    monotonic = 100
    boot = "boot-a"
    def now(self): return datetime(2026, 7, 15, tzinfo=UTC)
    def boot_id(self): return self.boot
    def monotonic_ns(self): return self.monotonic

class Reader:
    def read_bytes(self, *args, **kwargs): raise AssertionError("canonical artifacts are not read")

clock = Clock()
repository = SQLiteRepository(root / "state.db", clock=clock)
repository.initialize()
imports, exports = root / "imports", root / "exports"
client = FilesystemClientInputStaging.for_test(imports, exports)
service = FilesystemServiceClientData.for_test(
    imports, exports, repository=repository, artifact_reader=Reader(), utc_now=clock.now,
)
reconciler = StagingReconciler(imports.resolve(), repository, _secure_fs)

partial_source = root / "partial.gcode"
partial_source.write_bytes(b"G21\n")
partial_request = "12345678-1234-4234-8234-123456789abc"
partial = client.transfer_before_write(client.stage_gcode(partial_source, partial_request, clock))
active = reconciler.reconcile("startup", clock)
clock.monotonic = partial.lease.deadline_monotonic_ns
expired = reconciler.reconcile("periodic", clock)

decoded_source = root / "decoded.png"
decoded_source.write_bytes(b"image")
decoded_request = "22345678-1234-4234-8234-123456789abc"
decoded = client.transfer_before_write(client.stage_image(decoded_source, decoded_request, clock))
service.register_decoded(decoded_request, ClientDataRole.IMAGE, decoded.payload_path)
clock.boot = "boot-b"
clock.monotonic = 1
restarted = reconciler.reconcile("startup", clock)
admission = repository.get_staging_admission(decoded_request)

replayed = reconciler.reconcile("startup", clock)
query_plan = repository._get_connection().execute(
    "EXPLAIN QUERY PLAN SELECT request_id FROM staging_admissions WHERE request_id = ?", (decoded_request,),
).fetchall()
observations = {
    role: [path.name for path in (imports / role / "observations").iterdir()]
    for role in ("image", "gcode")
}
bundles = {
    role: {
        phase: [path.name for path in (imports / role / phase).iterdir()]
        for phase in ("prepare", "drop", "admitted")
    }
    for role in ("image", "gcode")
}
repository.close()
print(json.dumps({
    "active_evidence": len(active.evidence),
    "expired_status": [item.status.value for item in expired.evidence],
    "expired_drop_exists": partial.bundle_path.exists(),
    "restart_status": [item.status.value for item in restarted.evidence],
    "replay_evidence": len(replayed.evidence),
    "restart_state": None if admission is None else admission.state.value,
    "observations": observations,
    "bundles": bundles,
    "indexed_lookup": any("INDEX" in str(row).upper() for row in query_plan),
    "database_sidecars": sorted(path.name for path in root.glob("state.db-*") if path.exists()),
}, sort_keys=True))
"""


def test_installed_reconciler_converges_expiry_restart_replay_and_boot_mismatch(
    installed_drawingmachine: Path,
    package_c_audit_guard: Any,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    environment = package_c_audit_guard.environment.copy()
    for name in ("PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    result = subprocess.run(
        [
            str(installed_drawingmachine.with_name("python")),
            "-c",
            _RECOVERY_SCRIPT,
            str(tmp_path / "installed-recovery"),
            str(repository_root),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = cast(dict[str, Any], json.loads(result.stdout))
    assert evidence == {
        "active_evidence": 0,
        "bundles": {
            "gcode": {"admitted": [], "drop": [], "prepare": []},
            "image": {"admitted": [], "drop": [], "prepare": []},
        },
        "database_sidecars": [],
        "expired_drop_exists": False,
        "expired_status": ["COMPLETE"],
        "indexed_lookup": True,
        "observations": {"gcode": [], "image": []},
        "replay_evidence": 0,
        "restart_state": "QUARANTINED",
        "restart_status": ["COMPLETE"],
    }
    assert package_c_audit_guard.events() == []
