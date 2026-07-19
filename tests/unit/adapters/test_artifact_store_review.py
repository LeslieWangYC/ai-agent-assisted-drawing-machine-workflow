from __future__ import annotations

import hashlib
import inspect
import os
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest

import drawingmachine.adapters.filesystem._secure_fs as secure_fs
import drawingmachine.adapters.filesystem.artifact_store as artifact_store_module
from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.config import XdgPaths
from drawingmachine.domain.jobs import ArtifactRef, ConfigSnapshot, JobKind, JobRecord, JobState
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.artifacts import ArtifactStore, ExpectedArtifact, WorkerArtifact


class SimulatedCrash(BaseException):
    pass


@pytest.fixture
def store(valid_xdg_paths: XdgPaths) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(valid_xdg_paths)


def _stage(
    store: FilesystemArtifactStore,
    contents: bytes = b'{"schema_version": 1}\n',
    *,
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
    relative_path: str = "result.json",
) -> tuple[Path, WorkerArtifact]:
    staging = store.create_staging(job_id, attempt_id)
    path = staging.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return staging, WorkerArtifact(
        role="result",
        relative_path=relative_path,
        sha256=hashlib.sha256(contents).hexdigest(),
        size_bytes=len(contents),
        media_type="application/json",
    )


def _expected(relative_path: str = "result.json") -> tuple[ExpectedArtifact, ...]:
    return (
        ExpectedArtifact(
            role="result",
            relative_path=relative_path,
            media_type="application/json",
            json_object_required=True,
            required_json_values=(("schema_version", 1),),
        ),
    )


def _job() -> JobRecord:
    timestamp = datetime(2026, 7, 10, 12, tzinfo=UTC)
    return JobRecord(
        schema_version=1,
        job_id="job-1",
        kind=JobKind.OFFLINE_WORKFLOW,
        name="review fixture",
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


def _promote(
    store: FilesystemArtifactStore,
    artifact: WorkerArtifact,
    *,
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
) -> Path:
    return store.validate_and_promote(
        job_id,
        attempt_id,
        (artifact,),
        expected=_expected(artifact.relative_path),
    ).root


def _write_marker(directory_fd: int, name: str, contents: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    try:
        os.write(descriptor, contents)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def test_artifact_store_rejects_invalid_revocation_and_resolution_authority(
    store: FilesystemArtifactStore,
) -> None:
    with pytest.raises(DrawingMachineError, match="exact artifact references"):
        store.discard_promoted_bundle("job-1", "attempt-1", ())

    outside_attempt = ArtifactRef(
        "result",
        "artifacts/other-attempt/result.json",
        "a" * 64,
        1,
        "application/json",
    )
    with pytest.raises(DrawingMachineError, match="outside the exact attempt"):
        store.discard_promoted_bundle("job-1", "attempt-1", (outside_attempt,))

    outside_artifacts = ArtifactRef("result", "result.json", "a" * 64, 1, "application/json")
    with pytest.raises(DrawingMachineError, match="under artifacts"):
        store.resolve("job-1", outside_artifacts)


def test_artifact_store_preserves_domain_errors_during_translation() -> None:
    error = artifact_store_module.artifact_error("already translated")

    assert artifact_store_module._translated(error) is error


def test_staged_nested_read_revalidates_declared_metadata_and_final_identity(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = b'{"schema_version": 1}\n'
    _staging, artifact = _stage(store, contents, relative_path="nested/result.json")

    assert (
        store.read_staged_bytes(
            "job-1",
            "attempt-1",
            artifact,
            expected_relative_path="nested/result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )
        == contents
    )

    real_read = secure_fs.read_stable_regular_bounded

    def wrong_digest(*args: object, **kwargs: object) -> tuple[secure_fs.StableRead, bytes]:
        metadata, read_contents = real_read(*args, **kwargs)  # type: ignore[arg-type]
        return secure_fs.StableRead("0" * 64, metadata.size_bytes, metadata.identity), read_contents

    monkeypatch.setattr(secure_fs, "read_stable_regular_bounded", wrong_digest)
    with pytest.raises(DrawingMachineError, match="declared size and digest"):
        store.read_staged_bytes(
            "job-1",
            "attempt-1",
            artifact,
            expected_relative_path="nested/result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )

    def wrong_identity(*args: object, **kwargs: object) -> tuple[secure_fs.StableRead, bytes]:
        metadata, read_contents = real_read(*args, **kwargs)  # type: ignore[arg-type]
        identity = (*metadata.identity[:-1], metadata.identity[-1] + 1)
        return secure_fs.StableRead(metadata.sha256, metadata.size_bytes, identity), read_contents

    monkeypatch.setattr(secure_fs, "read_stable_regular_bounded", wrong_identity)
    with pytest.raises(DrawingMachineError, match="identity changed"):
        store.read_staged_bytes(
            "job-1",
            "attempt-1",
            artifact,
            expected_relative_path="nested/result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )


def test_prepared_directory_collision_budget_is_bounded(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        secure_fs,
        "create_directory_at",
        lambda *_args: (_ for _ in ()).throw(FileExistsError),
    )

    with ExitStack() as stack, pytest.raises(DrawingMachineError, match="unique prepared"):
        store._create_prepared(stack, 1, "attempt-1")


@pytest.mark.parametrize("capability", ["O_NOFOLLOW", "O_DIRECTORY", "_renameat2"])
def test_store_fails_closed_when_secure_platform_capability_is_unavailable(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    monkeypatch.setattr(secure_fs, capability, None)

    with pytest.raises(DrawingMachineError, match=r"secure filesystem primitives.*unavailable"):
        FilesystemArtifactStore(valid_xdg_paths)


def test_post_hash_staging_mutation_cannot_change_promoted_bytes(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b'{"schema_version": 1, "value": "validated"}\n'
    mutated = b'{"schema_version": 1, "value": "mutated-after-copy"}\n'
    staging, artifact = _stage(store, original)
    real_rename = secure_fs.rename_noreplace

    def mutate_then_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        (staging / "result.json").write_bytes(mutated)
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "rename_noreplace", mutate_then_publish)

    promoted = store.validate_and_promote("job-1", "attempt-1", (artifact,), expected=_expected())
    final = promoted.root / "result.json"

    assert final.read_bytes() == original
    assert promoted.artifacts[0].sha256 == hashlib.sha256(final.read_bytes()).hexdigest()
    assert promoted.artifacts[0].size_bytes == len(final.read_bytes())


def test_rename_destination_race_never_overwrites(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, artifact = _stage(store)
    real_rename = secure_fs.rename_noreplace

    def race_destination(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        os.mkdir(destination_name, mode=0o700, dir_fd=destination_parent_fd)
        raced_fd = secure_fs.open_directory_at(destination_parent_fd, destination_name)
        try:
            _write_marker(raced_fd, "marker.txt", b"raced destination")
        finally:
            os.close(raced_fd)
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "rename_noreplace", race_destination)

    with pytest.raises(DrawingMachineError, match=r"promoted attempt.*exists"):
        _promote(store, artifact)

    destination = store.jobs_dir / "job-1/artifacts/attempt-1"
    assert (destination / "marker.txt").read_bytes() == b"raced destination"
    assert staging.is_dir()


def test_fault_after_prepared_fsync_leaves_hidden_orphan_and_retry_succeeds(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging, artifact = _stage(store)
    real_rename = secure_fs.rename_noreplace

    def crash_before_publish(*args: object) -> None:
        del args
        raise SimulatedCrash

    monkeypatch.setattr(secure_fs, "rename_noreplace", crash_before_publish)

    with pytest.raises(SimulatedCrash):
        _promote(store, artifact)

    artifacts = store.jobs_dir / "job-1/artifacts"
    assert not (artifacts / "attempt-1").exists()
    orphans = tuple(artifacts.glob(".promoting-attempt-1-*"))
    assert len(orphans) == 1

    monkeypatch.setattr(secure_fs, "rename_noreplace", real_rename)
    promoted = _promote(store, artifact)

    assert (promoted / "result.json").is_file()
    assert orphans[0].is_dir()


def test_destination_ancestor_replacement_during_publish_fails_closed(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging, artifact = _stage(store)
    real_rename = secure_fs.rename_noreplace
    artifacts = store.jobs_dir / "job-1/artifacts"
    held_artifacts = store.jobs_dir / "job-1/artifacts-held"

    def replace_destination_ancestor(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        artifacts.rename(held_artifacts)
        artifacts.mkdir(mode=0o700)
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "rename_noreplace", replace_destination_ancestor)

    persisted_artifacts: list[ArtifactRef] = []
    with pytest.raises(DrawingMachineError, match=r"managed directory.*replaced"):
        promoted = store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=_expected(),
        )
        persisted_artifacts.extend(promoted.artifacts)

    assert not (artifacts / "attempt-1").exists()
    assert (held_artifacts / "attempt-1/result.json").is_file()
    assert persisted_artifacts == []


def test_source_ancestor_replacement_during_publish_fails_closed(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging, artifact = _stage(store)
    real_rename = secure_fs.rename_noreplace
    staging_root = store.jobs_dir / ".staging"
    held_staging = store.jobs_dir / ".staging-held"

    def replace_source_ancestor(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        staging_root.rename(held_staging)
        staging_root.mkdir(mode=0o700)
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "rename_noreplace", replace_source_ancestor)

    persisted_artifacts: list[ArtifactRef] = []
    with pytest.raises(DrawingMachineError, match=r"managed directory.*replaced"):
        promoted = store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=_expected(),
        )
        persisted_artifacts.extend(promoted.artifacts)

    assert (store.jobs_dir / "job-1/artifacts/attempt-1/result.json").is_file()
    assert (held_staging / "job-1/attempt-1/result.json").is_file()
    assert persisted_artifacts == []


def test_projection_job_ancestor_replacement_never_writes_replacement_directory(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = ArtifactRef("result", "artifacts/attempt-1/result.json", "e" * 64, 5, "application/json")
    store.write_projection(_job(), (artifact,))
    job_root = store.jobs_dir / "job-1"
    held_job_root = store.jobs_dir / "job-1-held"
    real_replace = secure_fs.replace_file

    def replace_job_ancestor(parent_fd: int, temporary_name: str, destination_name: str) -> None:
        job_root.rename(held_job_root)
        job_root.mkdir(mode=0o700)
        (job_root / "job.json").write_bytes(b"replacement-directory-sentinel")
        real_replace(parent_fd, temporary_name, destination_name)

    monkeypatch.setattr(secure_fs, "replace_file", replace_job_ancestor)

    with pytest.raises(DrawingMachineError, match=r"managed directory.*replaced"):
        store.write_projection(_job(), (artifact,))

    assert (job_root / "job.json").read_bytes() == b"replacement-directory-sentinel"


@pytest.mark.parametrize(
    "contents",
    [
        b'{"schema_version": 1, "value": NaN}',
        b'{"schema_version": 1, "value": Infinity}',
        b'{"schema_version": 1, "value": -Infinity}',
        b'{"schema_version": 1, "schema_version": 1}',
        b'{"schema_version": 1, "nested": {"key": 1, "key": 2}}',
        b'{"schema_version": 1, "value": "\xff"}',
    ],
)
def test_strict_json_rejects_nonfinite_duplicate_and_malformed_values(
    store: FilesystemArtifactStore,
    contents: bytes,
) -> None:
    staging, artifact = _stage(store, contents)

    with pytest.raises(DrawingMachineError, match="strict JSON"):
        _promote(store, artifact)

    assert not staging.exists()
    artifacts = store.jobs_dir / "job-1/artifacts"
    assert not artifacts.exists() or not tuple(artifacts.glob(".promoting-*"))


def test_validation_failure_cleans_partial_trees_and_same_attempt_can_retry(
    store: FilesystemArtifactStore,
) -> None:
    staging, artifact = _stage(store, b'{"schema_version": 2}\n')

    with pytest.raises(DrawingMachineError, match="schema_version"):
        _promote(store, artifact)

    assert not staging.exists()
    retry_staging, retry_artifact = _stage(store)
    promoted = _promote(store, retry_artifact)

    assert not retry_staging.exists()
    assert (promoted / "result.json").is_file()


def test_partial_import_failure_cleans_staging_and_same_attempt_can_retry(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"original")
    real_copy = secure_fs.copy_external_file

    def replace_source_after_copy(
        source_fd: int,
        destination_parent_fd: int,
        destination_name: str,
        *,
        max_bytes: int,
    ) -> secure_fs.CopiedFile:
        copied = real_copy(source_fd, destination_parent_fd, destination_name, max_bytes=max_bytes)
        replacement = tmp_path / "replacement.png"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, source)
        return copied

    monkeypatch.setattr(secure_fs, "copy_external_file", replace_source_after_copy)

    with pytest.raises(DrawingMachineError, match=r"source path.*replaced"):
        store.import_file(
            "job-1",
            "attempt-1",
            source,
            role="input",
            relative_path="inputs/source.png",
            media_type="image/png",
            max_bytes=1024,
        )

    assert not (store.jobs_dir / ".staging/job-1/attempt-1").exists()
    monkeypatch.setattr(secure_fs, "copy_external_file", real_copy)
    promoted = store.import_file(
        "job-1",
        "attempt-1",
        source,
        role="input",
        relative_path="inputs/source.png",
        media_type="image/png",
        max_bytes=1024,
    )

    assert (promoted.root / "inputs/source.png").read_bytes() == b"replacement"


def test_import_parent_creation_failure_cleans_staging_for_retry(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    real_ensure = secure_fs.ensure_directory_at

    def fail_second_parent(parent_fd: int, name: str) -> int:
        if name == "second":
            raise OSError("forced nested parent failure")
        return real_ensure(parent_fd, name)

    monkeypatch.setattr(secure_fs, "ensure_directory_at", fail_second_parent)

    with pytest.raises(DrawingMachineError, match="forced nested parent failure"):
        store.import_file(
            "job-1",
            "attempt-1",
            source,
            role="input",
            relative_path="first/second/source.png",
            media_type="image/png",
            max_bytes=1024,
        )

    assert not (store.jobs_dir / ".staging/job-1/attempt-1").exists()
    monkeypatch.setattr(secure_fs, "ensure_directory_at", real_ensure)
    promoted = store.import_file(
        "job-1",
        "attempt-1",
        source,
        role="input",
        relative_path="first/second/source.png",
        media_type="image/png",
        max_bytes=1024,
    )

    assert (promoted.root / "first/second/source.png").read_bytes() == b"source"


def test_prepared_tree_rejects_injected_undeclared_file_and_cleans_for_retry(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, artifact = _stage(store)
    real_copy = secure_fs.copy_regular_file

    def inject_extra(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> secure_fs.CopiedFile:
        copied = real_copy(source_parent_fd, source_name, destination_parent_fd, destination_name)
        _write_marker(destination_parent_fd, "undeclared.txt", b"extra")
        return copied

    monkeypatch.setattr(secure_fs, "copy_regular_file", inject_extra)

    with pytest.raises(DrawingMachineError, match="undeclared"):
        _promote(store, artifact)

    assert not staging.exists()
    assert not tuple((store.jobs_dir / "job-1/artifacts").glob(".promoting-*"))
    store.create_staging("job-1", "attempt-1")


def test_prepared_metadata_matches_final_bytes(store: FilesystemArtifactStore) -> None:
    contents = b'{"schema_version": 1, "payload": "prepared"}\n'
    _staging, artifact = _stage(store, contents, relative_path="nested/result.json")

    promoted = store.validate_and_promote(
        "job-1",
        "attempt-1",
        (artifact,),
        expected=_expected("nested/result.json"),
    )
    final = promoted.root / "nested/result.json"

    assert promoted.artifacts[0].sha256 == hashlib.sha256(final.read_bytes()).hexdigest()
    assert promoted.artifacts[0].size_bytes == final.stat().st_size
    assert final.read_bytes() == contents


def test_prepared_fsync_order_and_rename_noreplace_invocation(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging, artifact = _stage(store, relative_path="nested/result.json")
    events: list[tuple[str, int]] = []
    real_fsync_file = secure_fs.fsync_file
    real_fsync_directory = secure_fs.fsync_directory
    real_rename = secure_fs.rename_noreplace

    def track_file(descriptor: int) -> None:
        events.append(("file", os.fstat(descriptor).st_ino))
        real_fsync_file(descriptor)

    def track_directory(descriptor: int) -> None:
        events.append(("directory", os.fstat(descriptor).st_ino))
        real_fsync_directory(descriptor)

    def track_rename(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        with pytest.raises(FileNotFoundError):
            os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
        events.append(("rename", os.fstat(destination_parent_fd).st_ino))
        assert source_name.startswith(".promoting-attempt-1-")
        assert destination_name == "attempt-1"
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "fsync_file", track_file)
    monkeypatch.setattr(secure_fs, "fsync_directory", track_directory)
    monkeypatch.setattr(secure_fs, "rename_noreplace", track_rename)

    promoted = _promote(store, artifact)
    final_file_inode = (promoted / "nested/result.json").stat().st_ino
    final_root_inode = promoted.stat().st_ino
    artifacts_inode = promoted.parent.stat().st_ino
    rename_index = events.index(("rename", artifacts_inode))
    file_index = events.index(("file", final_file_inode))
    prepared_root_index = events.index(("directory", final_root_inode))
    parent_before = max(
        index for index, event in enumerate(events[:rename_index]) if event == ("directory", artifacts_inode)
    )
    parent_after = min(
        index
        for index, event in enumerate(events[rename_index + 1 :], start=rename_index + 1)
        if event == ("directory", artifacts_inode)
    )

    assert file_index < prepared_root_index < parent_before < rename_index < parent_after


def test_rename_noreplace_passes_linux_noreplace_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_parent = tmp_path / "source"
    destination_parent = tmp_path / "destination"
    source_parent.mkdir()
    destination_parent.mkdir()
    source_fd = os.open(source_parent, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(destination_parent, os.O_RDONLY | os.O_DIRECTORY)
    calls: list[tuple[int, bytes, int, bytes, int]] = []

    def fake_renameat2(
        old_directory_fd: int,
        old_name: bytes,
        new_directory_fd: int,
        new_name: bytes,
        flags: int,
    ) -> int:
        calls.append((old_directory_fd, old_name, new_directory_fd, new_name, flags))
        return 0

    monkeypatch.setattr(secure_fs, "_renameat2", fake_renameat2)
    try:
        secure_fs.rename_noreplace(source_fd, "prepared", destination_fd, "attempt-1")
    finally:
        os.close(source_fd)
        os.close(destination_fd)

    assert calls == [(source_fd, b"prepared", destination_fd, b"attempt-1", secure_fs.RENAME_NOREPLACE)]


@pytest.mark.parametrize("inject_extra", [False, True])
def test_postpublish_validation_failure_leaves_unreferenced_public_orphan(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    inject_extra: bool,
) -> None:
    _staging, artifact = _stage(store)
    real_rename = secure_fs.rename_noreplace

    def mutate_prepared_then_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        prepared_fd = secure_fs.open_directory_at(source_parent_fd, source_name)
        try:
            if inject_extra:
                _write_marker(prepared_fd, "late-extra.txt", b"undeclared")
            else:
                assert secure_fs.O_NOFOLLOW is not None
                descriptor = os.open(
                    "result.json",
                    os.O_WRONLY | os.O_TRUNC | secure_fs.O_NOFOLLOW,
                    dir_fd=prepared_fd,
                )
                try:
                    os.write(descriptor, b'{"schema_version": 1, "value": "changed"}\n')
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            os.close(prepared_fd)
        real_rename(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(secure_fs, "rename_noreplace", mutate_prepared_then_publish)

    persisted_artifacts: list[ArtifactRef] = []
    with pytest.raises(DrawingMachineError, match="post-publication"):
        promoted = store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=_expected(),
        )
        persisted_artifacts.extend(promoted.artifacts)

    final = store.jobs_dir / "job-1/artifacts/attempt-1"
    assert final.is_dir()
    if inject_extra:
        assert (final / "late-extra.txt").read_bytes() == b"undeclared"
    else:
        assert (final / "result.json").read_bytes() == b'{"schema_version": 1, "value": "changed"}\n'
    assert persisted_artifacts == []


def test_postpublish_failure_never_deletes_unrelated_raced_destination(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging, artifact = _stage(store)
    real_validate = artifact_store_module.validate_prepared_tree
    calls = 0
    destination = store.jobs_dir / "job-1/artifacts/attempt-1"
    displaced = store.jobs_dir / "job-1/artifacts/attempt-1-displaced"

    def race_before_final_validation(*args: object, **kwargs: object) -> tuple[ArtifactRef, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            destination.rename(displaced)
            destination.mkdir(mode=0o700)
            (destination / "marker.txt").write_bytes(b"unrelated-raced-directory")
        return real_validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(artifact_store_module, "validate_prepared_tree", race_before_final_validation)

    persisted_artifacts: list[ArtifactRef] = []
    with pytest.raises(DrawingMachineError, match="replaced"):
        promoted = store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=_expected(),
        )
        persisted_artifacts.extend(promoted.artifacts)

    assert (destination / "marker.txt").read_bytes() == b"unrelated-raced-directory"
    assert (displaced / "result.json").is_file()
    assert persisted_artifacts == []


def test_artifact_store_documents_quiescent_single_writer_trust_boundary() -> None:
    protocol_contract = inspect.getdoc(ArtifactStore) or ""
    implementation_contract = inspect.getdoc(FilesystemArtifactStore) or ""
    protocol_promotion_contract = inspect.getdoc(ArtifactStore.validate_and_promote) or ""
    promotion_contract = inspect.getdoc(FilesystemArtifactStore.validate_and_promote) or ""
    projection_contract = " ".join((inspect.getdoc(FilesystemArtifactStore.write_projection) or "").split())

    for contract in (protocol_contract, implementation_contract):
        normalized = contract.lower()
        assert "producer process has exited" in normalized
        assert "sole artifact and projection writer" in normalized
        assert "arbitrary malicious same-uid processes" in normalized
    for contract in (protocol_promotion_contract, promotion_contract):
        assert "producer process has exited" in contract
        assert "B5 supervisor must enforce this precondition" in contract
    assert "serialized single-writer" in projection_contract
    assert "concurrent same-UID mutation is outside this contract" in projection_contract


def test_projection_post_replace_identity_race_preserves_unknown_public_entry(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_projection(_job(), ())
    real_replace = secure_fs.replace_file

    def replace_then_race(parent_fd: int, temporary_name: str, destination_name: str) -> None:
        real_replace(parent_fd, temporary_name, destination_name)
        os.rename(
            destination_name,
            "job.json-displaced",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _write_marker(parent_fd, destination_name, b"unknown-public-projection")

    monkeypatch.setattr(secure_fs, "replace_file", replace_then_race)

    with pytest.raises(DrawingMachineError, match=r"projection.*identity"):
        store.write_projection(_job(), ())

    assert (store.jobs_dir / "job-1/job.json").read_bytes() == b"unknown-public-projection"


def test_projection_post_replace_content_race_is_detected_without_deleting_public_entry(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_projection(_job(), ())
    real_open = secure_fs.open_regular_at
    replacement = b"mutated-public-projection"

    def mutate_when_opening_public(parent_fd: int, name: str) -> int:
        descriptor = real_open(parent_fd, name)
        if name == "job.json":
            assert secure_fs.O_NOFOLLOW is not None
            writer = os.open(
                name,
                os.O_WRONLY | os.O_TRUNC | secure_fs.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.write(writer, replacement)
                os.fsync(writer)
            finally:
                os.close(writer)
        return descriptor

    monkeypatch.setattr(secure_fs, "open_regular_at", mutate_when_opening_public)

    with pytest.raises(DrawingMachineError, match=r"projection.*contents"):
        store.write_projection(_job(), ())

    assert (store.jobs_dir / "job-1/job.json").read_bytes() == replacement


@pytest.mark.parametrize(
    "contents",
    [
        b"1e999",
        b'{"schema_version": 1, "nested": {"value": -1e999}}',
    ],
)
def test_strict_json_rejects_nonfinite_float_overflow(
    store: FilesystemArtifactStore,
    contents: bytes,
) -> None:
    _staging, artifact = _stage(store, contents)

    with pytest.raises(DrawingMachineError, match="strict JSON"):
        _promote(store, artifact)


def test_source_tree_late_extra_after_declared_copy_fails_validation(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, artifact = _stage(store)
    real_copy = secure_fs.copy_regular_file

    def add_source_extra_after_copy(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> secure_fs.CopiedFile:
        copied = real_copy(source_parent_fd, source_name, destination_parent_fd, destination_name)
        _write_marker(source_parent_fd, "late-source-extra.txt", b"late")
        return copied

    monkeypatch.setattr(secure_fs, "copy_regular_file", add_source_extra_after_copy)

    with pytest.raises(DrawingMachineError, match="staging tree changed"):
        _promote(store, artifact)

    assert not staging.exists()


def test_real_renameat2_has_explicit_abi_and_preserves_existing_destination(tmp_path: Path) -> None:
    assert secure_fs._renameat2 is not None
    assert secure_fs._renameat2.argtypes == [
        secure_fs.ctypes.c_int,
        secure_fs.ctypes.c_char_p,
        secure_fs.ctypes.c_int,
        secure_fs.ctypes.c_char_p,
        secure_fs.ctypes.c_uint,
    ]
    assert secure_fs._renameat2.restype is secure_fs.ctypes.c_int
    parent = tmp_path / "artifacts"
    parent.mkdir()
    (parent / "prepared").mkdir()
    (parent / "attempt-1").mkdir()
    (parent / "attempt-1/marker.txt").write_bytes(b"existing")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            secure_fs.rename_noreplace(parent_fd, "prepared", parent_fd, "attempt-1")
    finally:
        os.close(parent_fd)

    assert (parent / "prepared").is_dir()
    assert (parent / "attempt-1/marker.txt").read_bytes() == b"existing"


def test_projection_removes_only_stale_regular_and_symlink_temps(store: FilesystemArtifactStore) -> None:
    store.write_projection(_job(), ())
    job_root = store.jobs_dir / "job-1"
    stale_regular = job_root / f".job.json.tmp-{'a' * 32}"
    stale_symlink = job_root / f".job.json.tmp-{'b' * 32}"
    unrelated_namespace = job_root / ".job.json.tmp-not-owned"
    unrelated_file = job_root / "notes.txt"
    stale_regular.write_bytes(b"stale")
    stale_symlink.symlink_to(unrelated_file.name)
    unrelated_namespace.write_bytes(b"not exact namespace")
    unrelated_file.write_bytes(b"keep")

    store.write_projection(_job(), ())

    assert not stale_regular.exists()
    assert not stale_symlink.exists()
    assert unrelated_namespace.read_bytes() == b"not exact namespace"
    assert unrelated_file.read_bytes() == b"keep"


def test_projection_rejects_stale_temp_directory_without_traversing(store: FilesystemArtifactStore) -> None:
    store.write_projection(_job(), ())
    stale_directory = store.jobs_dir / "job-1" / f".job.json.tmp-{'c' * 32}"
    stale_directory.mkdir()
    marker = stale_directory / "marker.txt"
    marker.write_bytes(b"do-not-traverse")

    with pytest.raises(DrawingMachineError, match="stale projection temporary"):
        store.write_projection(_job(), ())

    assert marker.read_bytes() == b"do-not-traverse"
