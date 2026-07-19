from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload["data"]["job"]
    assert isinstance(job, dict)
    return job


def _empty_staging_roots(service: Any) -> None:
    import_root = Path(service.environment["XDG_DATA_HOME"]) / "drawingmachine/imports/automation"
    for role in ("image", "gcode"):
        for phase in ("prepare", "drop", "admitted", "observations"):
            assert tuple((import_root / role / phase).iterdir()) == ()


def _run_client(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
    expected_returncode: int = 0,
) -> dict[str, Any]:
    result = subprocess.run(
        [str(executable), "--output", "json", *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=45.0,
    )
    assert result.returncode == expected_returncode, result.stdout + result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_installed_cli_moves_private_sources_through_schema_v4_and_exports(
    package_b_guarded_service: Any,
    service_access_test_harness: Any,
    tmp_path: Path,
) -> None:
    service = package_b_guarded_service
    service.client_environment = service_access_test_harness.client_environment(service.environment, "automation")
    private = tmp_path / "automation-private"
    private.mkdir(mode=0o700)
    caller_data = private / "caller-xdg-data"
    caller_data.mkdir()
    service.client_environment["XDG_DATA_HOME"] = str(caller_data)
    image = private / "source.png"
    canvas = Image.new("L", (64, 64), color=255)
    ImageDraw.Draw(canvas).rectangle((12, 12, 52, 52), outline=0, width=4)
    canvas.save(image)
    image.chmod(0o600)
    symlink = private / "stable-link.png"
    symlink.symlink_to(image)
    hardlink = private / "stable-hardlink.png"
    os.link(image, hardlink)
    hardlink.chmod(0o400)
    assert stat.S_IMODE(hardlink.stat().st_mode) == 0o400

    blocked: list[dict[str, Any]] = []
    for index, source in enumerate((image, symlink, hardlink), start=1):
        submitted = service.run_json(
            "workflow",
            "run",
            "--job-name",
            f"installed-source-{index}",
            "--input-image",
            str(source),
            "--route-mode",
            "direct",
        )
        blocked.append(service.wait(submitted["data"]["job_id"]))
        assert _job(blocked[-1])["state"] == "BLOCKED"
        assert _job(blocked[-1])["blocker"]["code"] == "REVIEW_REQUIRED"
        _empty_staging_roots(service)

    with sqlite3.connect(service.database_path) as connection:
        admissions = connection.execute(
            "SELECT request_id, role, state, job_id FROM staging_admissions ORDER BY request_id"
        ).fetchall()
        query_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT request_id FROM staging_admissions WHERE request_id = ?",
            (admissions[0][0],),
        ).fetchall()
    assert len(admissions) == 3
    assert all(role == "image" and state == "CONSUMED" and job_id for _, role, state, job_id in admissions)
    assert any("INDEX" in str(row).upper() for row in query_plan)
    decoded_images = service_access_test_harness.decoded_requests()
    assert len(decoded_images) == 3
    assert {request_id for request_id, role, _path in decoded_images if role == "image"} == {
        request_id for request_id, role, _state, _job_id in admissions if role == "image"
    }
    for request_id, role, payload_path in decoded_images:
        assert role == "image"
        assert payload_path.name == "payload"
        assert payload_path.parent.parent.name == "drop"
        assert request_id in payload_path.parent.name

    selected = blocked[0]
    job = _job(selected)
    processed = service.artifact(selected, "processed_image")
    export_root = service_access_test_harness.root / "automation-data/exports"
    exported_image = export_root / "jobs" / job["job_id"] / processed["relative_path"]
    assert exported_image.is_file()
    assert not exported_image.is_relative_to(service.jobs_dir)
    review = private / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema": "processed_image_review_v1",
                "job_name": job["name"],
                "reviewed_at": "2026-07-14T00:00:00+00:00",
                "reviewer": "test:installed-d9",
                "processed_image": str(exported_image),
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
    resumed = service.run_json(
        "workflow",
        "run",
        "--resume-job",
        job["job_id"],
        "--review-json",
        str(review),
    )
    assert resumed["data"]["state"] == "IMAGE_READY"
    assert _job(service.wait(job["job_id"]))["state"] == "READY_TO_RUN"

    gcode = private / "candidate.gcode"
    gcode.write_text("G21\nG90\nG54\nG0 Z3.5\nG0 X1 Y1 F1200\n; End\n", encoding="ascii")
    gcode_link = private / "stable-link.gcode"
    gcode_link.symlink_to(gcode)
    gcode_hardlink = private / "stable-hardlink.gcode"
    os.link(gcode, gcode_hardlink)
    gcode_hardlink.chmod(0o400)
    assert stat.S_IMODE(gcode_hardlink.stat().st_mode) == 0o400
    decoy = caller_data / "drawingmachine/jobs/hostile/artifacts/attempt/gcode_static.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text('{"ok":false,"source":"caller-xdg-decoy"}', encoding="utf-8")
    for source in (gcode, gcode_link, gcode_hardlink):
        checked = service.run_json(
            "gcode",
            "check",
            str(source),
            "--timeout-seconds",
            "30",
            "--poll-interval-seconds",
            "0.02",
        )
        assert checked["data"]["state"] in {"BLOCKED", "COMPLETED"}
        assert isinstance(checked["data"]["static_result"], dict)
        assert checked["data"]["static_result"].get("source") != "caller-xdg-decoy"
        _empty_staging_roots(service)
    assert json.loads(decoy.read_text(encoding="utf-8"))["source"] == "caller-xdg-decoy"
    with sqlite3.connect(service.database_path) as connection:
        gcode_admissions = connection.execute(
            "SELECT request_id, role, state, job_id FROM staging_admissions WHERE role = 'gcode' ORDER BY request_id"
        ).fetchall()
    assert len(gcode_admissions) == 3
    assert all(state == "CONSUMED" and job_id for _, _, state, job_id in gcode_admissions)
    decoded_gcode = tuple(item for item in service_access_test_harness.decoded_requests() if item[1] == "gcode")
    assert {request_id for request_id, _role, _path in decoded_gcode} == {
        request_id for request_id, _role, _state, _job_id in gcode_admissions
    }
    for request_id, _role, payload_path in decoded_gcode:
        assert payload_path.name == "payload"
        assert payload_path.parent.parent.name == "drop"
        assert request_id in payload_path.parent.name
    assert service.forbidden_events() == []


def test_installed_invalid_inputs_and_operator_fail_before_staging_or_request(
    package_b_guarded_service: Any,
    service_access_test_harness: Any,
    tmp_path: Path,
) -> None:
    service = package_b_guarded_service
    automation = service_access_test_harness.client_environment(service.environment, "automation")
    private = tmp_path / "automation-private"
    private.mkdir(mode=0o700)
    fifo = private / "fifo.png"
    os.mkfifo(fifo)
    broken = private / "broken.png"
    broken.symlink_to(private / "missing.png")
    loop = private / "loop.png"
    loop.symlink_to(loop)
    with sqlite3.connect(service.database_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM staging_admissions").fetchone()[0]
    decoded_before = service_access_test_harness.decoded_requests()

    for source in (fifo, broken, loop):
        denied = _run_client(
            service.executable,
            automation,
            tmp_path,
            "workflow",
            "run",
            "--job-name",
            "invalid-source",
            "--input-image",
            str(source),
            "--route-mode",
            "direct",
            expected_returncode=1,
        )
        assert denied["error"]["code"] == "CLIENT_INPUT_INVALID"
        _empty_staging_roots(service)

    valid = private / "valid.png"
    Image.new("L", (8, 8), color=255).save(valid)
    operator = service_access_test_harness.client_environment(service.environment, "operator")
    denied = _run_client(
        service.executable,
        operator,
        tmp_path,
        "workflow",
        "run",
        "--job-name",
        "operator-denied",
        "--input-image",
        str(valid),
        "--route-mode",
        "direct",
        expected_returncode=1,
    )
    assert denied["error"]["code"] == "CLIENT_DATA_PERMISSION_DENIED"
    with sqlite3.connect(service.database_path) as connection:
        after = connection.execute("SELECT COUNT(*) FROM staging_admissions").fetchone()[0]
    assert after == before
    assert service_access_test_harness.decoded_requests() == decoded_before
    _empty_staging_roots(service)
    assert service.forbidden_events() == []


def test_installed_reconciler_closes_partial_frame_and_restart_orphans(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    script = r"""
import json, pathlib, sys
from datetime import UTC, datetime
from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.adapters.filesystem.client_data import FilesystemClientInputStaging, FilesystemServiceClientData
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.ports.client_data import ClientDataRole

class Clock:
    monotonic = 100
    boot = "boot-a"
    def now(self): return datetime(2026, 7, 14, tzinfo=UTC)
    def boot_id(self): return self.boot
    def monotonic_ns(self): return self.monotonic

class Reader:
    def read_bytes(self, *args, **kwargs): raise AssertionError("canonical artifacts are not read")

root = pathlib.Path(sys.argv[1])
root.mkdir()
clock = Clock()
repository = SQLiteRepository(root / "state.db", clock=clock)
repository.initialize()
imports, exports = root / "imports", root / "exports"
client = FilesystemClientInputStaging.for_test(imports, exports)
service = FilesystemServiceClientData.for_test(
    imports, exports, repository=repository, artifact_reader=Reader(), utc_now=clock.now,
)
reconciler = StagingReconciler(imports.resolve(), repository, _secure_fs)
source = root / "partial.gcode"
source.write_bytes(b"G21\n")
request_id = "12345678-1234-4234-8234-123456789abc"
staged = client.transfer_before_write(client.stage_gcode(source, request_id, clock))
before = reconciler.reconcile("startup", clock)
clock.monotonic = staged.lease.deadline_monotonic_ns
expired = reconciler.reconcile("periodic", clock)

source2 = root / "decoded.png"
source2.write_bytes(b"image")
request_id2 = "22345678-1234-4234-8234-123456789abc"
staged2 = client.transfer_before_write(client.stage_image(source2, request_id2, clock))
service.register_decoded(request_id2, ClientDataRole.IMAGE, staged2.payload_path)
clock.boot = "boot-b"
clock.monotonic = 1
restarted = reconciler.reconcile("startup", clock)
admission = repository.get_staging_admission(request_id2)
print(json.dumps({
    "before_evidence": len(before.evidence),
    "expired_status": [item.status.value for item in expired.evidence],
    "expired_drop_exists": staged.bundle_path.exists(),
    "restart_status": [item.status.value for item in restarted.evidence],
    "restart_state": None if admission is None else admission.state.value,
    "observations": {
        role: [path.name for path in (imports / role / "observations").iterdir()]
        for role in ("image", "gcode")
    },
}))
"""
    result = subprocess.run(
        [
            str(installed_drawingmachine.with_name("python")),
            "-c",
            script,
            str(tmp_path / "installed-reconcile"),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence == {
        "before_evidence": 0,
        "expired_status": ["COMPLETE"],
        "expired_drop_exists": False,
        "restart_status": ["COMPLETE"],
        "restart_state": "QUARANTINED",
        "observations": {"image": [], "gcode": []},
    }
