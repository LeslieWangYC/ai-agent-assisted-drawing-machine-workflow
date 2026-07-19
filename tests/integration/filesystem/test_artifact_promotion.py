from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import drawingmachine.adapters.filesystem.artifact_store as artifact_store_module
from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.adapters.filesystem.hashing import sha256_file
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.config import XdgPaths
from drawingmachine.domain.jobs import (
    AuditRecord,
    ConfigSnapshot,
    JobEvent,
    JobKind,
    JobRecord,
    JobState,
    RequesterIdentity,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.artifacts import ExpectedArtifact, WorkerArtifact


def _make_store(paths: XdgPaths) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(paths)


def _make_job(job_id: str = "job-1") -> JobRecord:
    timestamp = datetime(2026, 7, 10, 12, tzinfo=UTC)
    return JobRecord(
        schema_version=1,
        job_id=job_id,
        kind=JobKind.OFFLINE_WORKFLOW,
        name="promotion fixture",
        state=JobState.QUEUED,
        revision=0,
        request={"route_mode": "direct"},
        config=ConfigSnapshot(
            machine_profile="default",
            provider_profile="local-comfyui",
            application_schema_version=1,
            machine_schema_version=1,
            provider_schema_version=1,
            application_digest="a" * 64,
            machine_digest="b" * 64,
            provider_digest="c" * 64,
        ),
        blocker=None,
        error=None,
        ready_snapshot=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _event_for(job: JobRecord) -> JobEvent:
    requester = RequesterIdentity("LOCAL_PEER", 101, 1000, 1000)
    return JobEvent(
        event_id="event-created",
        job_id=job.job_id,
        job_revision=job.revision,
        event_type="job.created",
        prior_state=None,
        result_state=job.state,
        request_id="request-1",
        requester_type=requester.requester_type,
        payload={"requester": requester.to_json()},
        created_at=datetime(2026, 7, 10, 12, 0, 1, tzinfo=UTC),
    )


def _audit_for(event: JobEvent) -> AuditRecord:
    return AuditRecord(
        event_id=event.event_id,
        event_type=event.event_type,
        request_id=event.request_id,
        payload={
            "schema_version": 1,
            "requester": event.payload["requester"],
            "job_id": event.job_id,
            "command_summary": {
                "name": "service.advance",
                "event_type": event.event_type,
                "job_kind": "OFFLINE_WORKFLOW",
            },
            "prior_state": None,
            "result_state": event.result_state.value,
            "approval": None,
            "error_code": None,
            "blocker_code": None,
            "service_version": "0.2.0",
            "protocol_version": 1,
        },
    )


def test_import_and_promote_create_complete_private_job_relative_bundle(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store = _make_store(valid_xdg_paths)
    source = tmp_path / "input image.png"
    source.write_bytes(b"input-image-bytes")

    promoted = store.import_file(
        "job-1",
        "input-attempt",
        source,
        role="input_image",
        relative_path="inputs/original.png",
        media_type="image/png",
        max_bytes=1024,
    )

    destination = valid_xdg_paths.jobs_dir / "job-1/artifacts/input-attempt"
    copied = destination / "inputs/original.png"
    assert promoted.root == destination
    assert copied.read_bytes() == source.read_bytes()
    assert promoted.artifacts[0].relative_path == "artifacts/input-attempt/inputs/original.png"
    assert promoted.artifacts[0].sha256 == sha256_file(copied)
    assert not (valid_xdg_paths.jobs_dir / ".staging/job-1/input-attempt").exists()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(copied.parent.stat().st_mode) == 0o700


def test_resolve_revalidates_persisted_artifact_identity_size_and_digest(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store = _make_store(valid_xdg_paths)
    source = tmp_path / "input.png"
    source.write_bytes(b"trusted")
    promoted = store.import_file(
        "job-resolve",
        "input-attempt",
        source,
        role="input_image",
        relative_path="inputs/original.png",
        media_type="image/png",
        max_bytes=1024,
    )
    artifact = promoted.artifacts[0]

    assert store.resolve("job-resolve", artifact).read_bytes() == b"trusted"
    (promoted.root / "inputs/original.png").write_bytes(b"changed")
    with pytest.raises(DrawingMachineError):
        store.resolve("job-resolve", artifact)


def test_resolve_rejects_cross_job_and_symlinked_persisted_artifacts(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store = _make_store(valid_xdg_paths)
    source = tmp_path / "input.png"
    source.write_bytes(b"trusted")
    promoted = store.import_file(
        "job-resolve",
        "input-attempt",
        source,
        role="input_image",
        relative_path="inputs/original.png",
        media_type="image/png",
        max_bytes=1024,
    )
    artifact = promoted.artifacts[0]
    with pytest.raises(DrawingMachineError):
        store.resolve("other-job", artifact)
    target = promoted.root / "inputs/original.png"
    target.unlink()
    target.symlink_to(source)
    with pytest.raises(DrawingMachineError):
        store.resolve("job-resolve", artifact)


def test_discard_staging_is_secure_and_idempotent(valid_xdg_paths: XdgPaths) -> None:
    store = _make_store(valid_xdg_paths)
    staging = store.create_staging("job-discard", "attempt-1")
    nested = staging / "nested"
    nested.mkdir()
    (nested / "output.tmp").write_bytes(b"partial")

    store.discard_staging("job-discard", "attempt-1")
    store.discard_staging("job-discard", "attempt-1")

    assert not staging.exists()


def test_discard_promoted_bundle_revokes_only_exact_uncommitted_authority(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store = _make_store(valid_xdg_paths)
    source = tmp_path / "input.png"
    source.write_bytes(b"trusted")
    promoted = store.import_file(
        "job-revoke",
        "input-attempt",
        source,
        role="input_image",
        relative_path="inputs/original.png",
        media_type="image/png",
        max_bytes=1024,
    )

    store.discard_promoted_bundle("job-revoke", "input-attempt", promoted.artifacts)
    store.discard_promoted_bundle("job-revoke", "input-attempt", promoted.artifacts)

    assert not promoted.root.exists()


def test_discard_promoted_bundle_preserves_tampered_or_replaced_authority(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(valid_xdg_paths)
    source = tmp_path / "input.png"
    source.write_bytes(b"trusted")
    promoted = store.import_file(
        "job-revoke",
        "input-attempt",
        source,
        role="input_image",
        relative_path="inputs/original.png",
        media_type="image/png",
        max_bytes=1024,
    )
    target = promoted.root / "inputs/original.png"
    target.write_bytes(b"tampered")

    with pytest.raises(DrawingMachineError, match="revocation"):
        store.discard_promoted_bundle("job-revoke", "input-attempt", promoted.artifacts)
    assert target.read_bytes() == b"tampered"

    target.write_bytes(b"trusted")
    real_rename = artifact_store_module._secure_fs.rename_noreplace

    def replace_after_detach(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)
        if source_name != "input-attempt":
            return
        real_rename(destination_parent_fd, destination_name, destination_parent_fd, ".preserved-authority")
        os.mkdir(destination_name, mode=0o700, dir_fd=destination_parent_fd)
        replacement_fd = os.open(
            destination_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=destination_parent_fd,
        )
        try:
            descriptor = os.open(
                "replacement",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=replacement_fd,
            )
            os.close(descriptor)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(artifact_store_module._secure_fs, "rename_noreplace", replace_after_detach)
    with pytest.raises(DrawingMachineError, match="replaced"):
        store.discard_promoted_bundle("job-revoke", "input-attempt", promoted.artifacts)

    artifacts_root = valid_xdg_paths.jobs_dir / "job-revoke/artifacts"
    assert (artifacts_root / ".preserved-authority/inputs/original.png").read_bytes() == b"trusted"
    replacement = next(path for path in artifacts_root.iterdir() if path.name.startswith(".revoke-"))
    assert (replacement / "replacement").exists()


def test_validated_nested_attempt_fsyncs_files_and_directories_before_and_after_promotion(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(valid_xdg_paths)
    staging = store.create_staging("job-1", "attempt-1")
    nested = staging / "reports"
    nested.mkdir(mode=0o755)
    contents = b'{"schema_version": 1, "status": "PASS"}\n'
    report = nested / "validation.json"
    report.write_bytes(contents)
    artifact = WorkerArtifact(
        "validation",
        "reports/validation.json",
        sha256_file(report),
        len(contents),
        "application/json",
    )
    synced_types: list[int] = []
    real_fsync = artifact_store_module.os.fsync

    def track_fsync(descriptor: int) -> None:
        synced_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(artifact_store_module.os, "fsync", track_fsync)

    promoted = store.validate_and_promote(
        "job-1",
        "attempt-1",
        (artifact,),
        expected=(
            ExpectedArtifact(
                "validation",
                "reports/validation.json",
                "application/json",
                True,
                (("schema_version", 1), ("status", "PASS")),
            ),
        ),
    )

    assert promoted.root == valid_xdg_paths.jobs_dir / "job-1/artifacts/attempt-1"
    assert stat.S_IFREG in synced_types
    assert synced_types.count(stat.S_IFDIR) >= 3
    assert stat.S_IMODE((promoted.root / "reports").stat().st_mode) == 0o700


def test_failed_repository_commit_leaves_only_unreferenced_complete_bundle(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store = _make_store(valid_xdg_paths)
    staging = store.create_staging("job-1", "attempt-1")
    contents = b'{"schema_version": 1}\n'
    result = staging / "result.json"
    result.write_bytes(contents)
    promoted = store.validate_and_promote(
        "job-1",
        "attempt-1",
        (
            WorkerArtifact(
                "result",
                "result.json",
                sha256_file(result),
                len(contents),
                "application/json",
            ),
        ),
        expected=(ExpectedArtifact("result", "result.json", "application/json", True, (("schema_version", 1),)),),
    )
    repository = SQLiteRepository(tmp_path / "drawingmachine.db", clock=SystemClock())
    assert repository.initialize() == 5
    job = _make_job()
    event = _event_for(job)
    audit = _audit_for(event)
    repository.append_audit(event.event_id, "conflict", None, {"seed": True})

    try:
        with pytest.raises(sqlite3.IntegrityError), repository.transaction() as transaction:
            transaction.create_job(job, artifacts=promoted.artifacts, event=event, audit=audit)

        assert promoted.root.is_dir()
        assert json.loads((promoted.root / "result.json").read_text(encoding="utf-8")) == {"schema_version": 1}
        assert repository.get_job(job.job_id) is None
        assert repository.list_artifacts(job.job_id) == ()
    finally:
        repository.close()
