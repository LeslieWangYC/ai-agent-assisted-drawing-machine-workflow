from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import drawingmachine.adapters.filesystem._secure_fs as secure_fs
from drawingmachine.adapters.filesystem.artifact_store import FilesystemArtifactStore
from drawingmachine.adapters.filesystem.hashing import sha256_file
from drawingmachine.config import XdgPaths
from drawingmachine.domain.jobs import ArtifactRef, ConfigSnapshot, JobKind, JobRecord, JobState
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.artifacts import ExpectedArtifact, WorkerArtifact


@pytest.fixture
def store(valid_xdg_paths: XdgPaths) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(valid_xdg_paths)


def _write_worker_artifact(
    staging: Path,
    relative_path: str = "result.json",
    *,
    role: str = "result",
    media_type: str = "application/json",
    contents: bytes = b'{"schema_version": 1}\n',
) -> WorkerArtifact:
    path = staging.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(contents)
    return WorkerArtifact(
        role=role,
        relative_path=relative_path,
        sha256=sha256_file(path),
        size_bytes=len(contents),
        media_type=media_type,
    )


def _expected(
    relative_path: str = "result.json",
    *,
    role: str = "result",
    media_type: str = "application/json",
    json_object_required: bool = True,
    required_json_values: tuple[tuple[str, str | int], ...] = (("schema_version", 1),),
) -> ExpectedArtifact:
    return ExpectedArtifact(
        role=role,
        relative_path=relative_path,
        media_type=media_type,
        json_object_required=json_object_required,
        required_json_values=required_json_values,
    )


def _promoted_result(
    store: FilesystemArtifactStore,
    *,
    contents: bytes = b'{"schema_version": 1}\n',
) -> tuple[ArtifactRef, Path]:
    staging = store.create_staging("job-1", "attempt-1")
    worker = _write_worker_artifact(staging, contents=contents)
    bundle = store.validate_and_promote(
        "job-1",
        "attempt-1",
        (worker,),
        expected=(_expected(),),
    )
    artifact = bundle.artifacts[0]
    return artifact, store.jobs_dir / "job-1" / artifact.relative_path


def _make_job(job_id: str = "job-1") -> JobRecord:
    timestamp = datetime(2026, 7, 10, 12, tzinfo=UTC)
    return JobRecord(
        schema_version=1,
        job_id=job_id,
        kind=JobKind.OFFLINE_WORKFLOW,
        name="artifact fixture",
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


def _staged_descriptor(
    relative_path: str,
    contents: bytes,
    *,
    role: str = "result",
    media_type: str = "application/json",
) -> WorkerArtifact:
    return WorkerArtifact(
        role,
        relative_path,
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        media_type,
    )


def _assert_no_attempt_bytes(store: FilesystemArtifactStore, job_id: str, attempt_id: str) -> None:
    assert not (store.jobs_dir / job_id / "artifacts" / attempt_id).exists()


def test_staging_read_rejects_invalid_descriptor_before_filesystem_access(
    store: FilesystemArtifactStore,
) -> None:
    with pytest.raises(DrawingMachineError, match="descriptor"):
        store.read_staged_bytes(
            "job-invalid",
            "attempt-1",
            object(),  # type: ignore[arg-type]
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )


def test_staging_read_rejects_invalid_maximum_before_filesystem_access(
    store: FilesystemArtifactStore,
) -> None:
    artifact = _staged_descriptor("result.json", b"{}")

    with pytest.raises(DrawingMachineError, match="maximum"):
        store.read_staged_bytes(
            "job-limit",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=0,
        )


def test_staging_read_rejects_declared_oversize_before_filesystem_access(
    store: FilesystemArtifactStore,
) -> None:
    artifact = _staged_descriptor("result.json", b"123456789")

    with pytest.raises(DrawingMachineError, match="maximum"):
        store.read_staged_bytes(
            "job-limit",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=8,
        )


def test_staging_read_rejects_fifo_before_any_blocking_open(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-fifo", "attempt-1")
    os.mkfifo(staging / "result.json")
    artifact = _staged_descriptor("result.json", b"{}")

    with pytest.raises(DrawingMachineError, match="regular file"):
        store.read_staged_bytes(
            "job-fifo",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )

    _assert_no_attempt_bytes(store, "job-fifo", "attempt-1")


@pytest.mark.parametrize("replacement", ["parent_symlink", "final_symlink"])
def test_staging_read_rejects_symlink_escape_without_promotion(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    replacement: str,
) -> None:
    contents = b'{"safe": true}'
    staging = store.create_staging("job-symlink", "attempt-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_bytes(contents)
    if replacement == "parent_symlink":
        staging.rmdir()
        staging.symlink_to(outside, target_is_directory=True)
    else:
        (staging / "result.json").symlink_to(outside / "result.json")
    artifact = _staged_descriptor("result.json", contents)

    with pytest.raises(DrawingMachineError, match="artifact"):
        store.read_staged_bytes(
            "job-symlink",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )

    _assert_no_attempt_bytes(store, "job-symlink", "attempt-1")


def test_staging_read_rejects_final_identity_replacement_without_promotion(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = b'{"safe": true}'
    staging = store.create_staging("job-replaced", "attempt-1")
    result = staging / "result.json"
    result.write_bytes(contents)
    artifact = _staged_descriptor("result.json", contents)
    real_read = secure_fs.os.read
    replaced = False

    def replace_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            replaced = True
            result.rename(staging / "original.json")
            result.write_bytes(contents)
        return chunk

    monkeypatch.setattr(secure_fs.os, "read", replace_after_first_read)

    with pytest.raises(DrawingMachineError, match=r"changed|replaced"):
        store.read_staged_bytes(
            "job-replaced",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )

    _assert_no_attempt_bytes(store, "job-replaced", "attempt-1")


def test_staging_read_rejects_attempt_directory_replacement_without_promotion(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = b'{"safe": true}'
    staging = store.create_staging("job-dir-replaced", "attempt-1")
    result = staging / "result.json"
    result.write_bytes(contents)
    artifact = _staged_descriptor("result.json", contents)
    real_read = secure_fs.os.read
    replaced = False

    def replace_directory_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            replaced = True
            staging.rename(staging.with_name("attempt-original"))
            staging.mkdir()
            (staging / "result.json").write_bytes(contents)
        return chunk

    monkeypatch.setattr(secure_fs.os, "read", replace_directory_after_first_read)

    with pytest.raises(DrawingMachineError, match=r"changed|replaced|identity"):
        store.read_staged_bytes(
            "job-dir-replaced",
            "attempt-1",
            artifact,
            expected_relative_path="result.json",
            expected_media_type="application/json",
            max_bytes=1024,
        )

    _assert_no_attempt_bytes(store, "job-dir-replaced", "attempt-1")


@pytest.mark.parametrize("maximum", [0, -1, True])
def test_import_rejects_nonpositive_maximum_without_staging(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    maximum: object,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image")

    with pytest.raises(DrawingMachineError, match="maximum"):
        store.import_file(
            "job-limit",
            "attempt-1",
            source,
            role="input_image",
            relative_path="inputs/source.png",
            media_type="image/png",
            max_bytes=maximum,  # type: ignore[arg-type]
        )

    assert not (store.jobs_dir / ".staging" / "job-limit" / "attempt-1").exists()
    _assert_no_attempt_bytes(store, "job-limit", "attempt-1")


def test_import_rejects_sparse_oversized_source_before_staging_or_copy(
    store: FilesystemArtifactStore,
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.gcode"
    source.touch()
    os.truncate(source, 1025)

    with pytest.raises(DrawingMachineError, match=r"maximum|limit"):
        store.import_file(
            "job-limit",
            "attempt-1",
            source,
            role="gcode",
            relative_path="inputs/source.gcode",
            media_type="text/x.gcode",
            max_bytes=1024,
        )

    assert not (store.jobs_dir / ".staging" / "job-limit" / "attempt-1").exists()
    _assert_no_attempt_bytes(store, "job-limit", "attempt-1")


def test_import_streaming_limit_cleans_partial_staging_when_source_grows(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "growing.gcode"
    source.write_bytes(b"a" * 1024)
    real_read = secure_fs.os.read
    extended = False

    def extend_after_initial_source_read(descriptor: int, size: int) -> bytes:
        nonlocal extended
        chunk = real_read(descriptor, size)
        if chunk and not extended:
            try:
                descriptor_path = Path(f"/proc/self/fd/{descriptor}").resolve()
            except OSError:
                descriptor_path = Path()
            if descriptor_path == source:
                extended = True
                with source.open("ab") as stream:
                    stream.write(b"b")
        return chunk

    monkeypatch.setattr(secure_fs.os, "read", extend_after_initial_source_read)

    with pytest.raises(DrawingMachineError, match=r"maximum|limit"):
        store.import_file(
            "job-growing",
            "attempt-1",
            source,
            role="gcode",
            relative_path="inputs/source.gcode",
            media_type="text/x.gcode",
            max_bytes=1024,
        )

    assert extended
    assert not (store.jobs_dir / ".staging" / "job-growing" / "attempt-1").exists()
    _assert_no_attempt_bytes(store, "job-growing", "attempt-1")


@pytest.mark.parametrize(
    "job_id",
    ["", ".", "..", "job/other", "job\\other", "/absolute", "job\nother", "job-绘图"],
)
def test_create_staging_rejects_unsafe_job_id(store: FilesystemArtifactStore, job_id: str) -> None:
    with pytest.raises(DrawingMachineError, match="job_id"):
        store.create_staging(job_id, "attempt-1")


@pytest.mark.parametrize(
    "attempt_id",
    ["", ".", "..", "attempt/other", "attempt\\other", "/absolute", "attempt\x00other", "尝试"],
)
def test_create_staging_rejects_unsafe_attempt_id(store: FilesystemArtifactStore, attempt_id: str) -> None:
    with pytest.raises(DrawingMachineError, match="attempt_id"):
        store.create_staging("job-1", attempt_id)


def test_create_staging_is_exclusive_and_private(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")

    with pytest.raises(DrawingMachineError, match=r"staging.*exists"):
        store.create_staging("job-1", "attempt-1")

    private_directories = (
        store.jobs_dir,
        store.jobs_dir / ".staging",
        store.jobs_dir / ".staging/job-1",
        staging,
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in private_directories)


@pytest.mark.parametrize(
    "relative_path",
    ["../other/result.json", "/tmp/result.json", "nested/../result.json", "nested\\result.json", "./result.json"],
)
def test_promote_rejects_noncanonical_or_escaping_declared_path(
    store: FilesystemArtifactStore,
    relative_path: str,
) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    (staging / "result.json").write_text("{}", encoding="utf-8")
    artifact = WorkerArtifact("result", relative_path, "0" * 64, 2, "application/json")

    with pytest.raises(DrawingMachineError, match="relative path"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(relative_path, required_json_values=()),),
        )


def test_promote_rejects_duplicate_worker_roles(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    first = _write_worker_artifact(staging, "first.json")
    second = _write_worker_artifact(staging, "second.json")

    with pytest.raises(DrawingMachineError, match=r"duplicate.*role"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (first, second),
            expected=(_expected("first.json"), _expected("second.json")),
        )


def test_promote_rejects_duplicate_expected_paths(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)

    with pytest.raises(DrawingMachineError, match=r"duplicate.*path"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(role="first"), _expected(role="second")),
        )


@pytest.mark.parametrize("changed_field", ["role", "relative_path", "media_type"])
def test_promote_requires_exact_expected_and_worker_descriptor_sets(
    store: FilesystemArtifactStore,
    changed_field: str,
) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    values = {
        "role": "other" if changed_field == "role" else "result",
        "relative_path": "other.json" if changed_field == "relative_path" else "result.json",
        "media_type": "text/plain" if changed_field == "media_type" else "application/json",
    }

    with pytest.raises(DrawingMachineError, match=r"expected.*worker"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(ExpectedArtifact(json_object_required=True, required_json_values=(), **values),),
        )


def test_promote_rejects_missing_declared_file(store: FilesystemArtifactStore) -> None:
    store.create_staging("job-1", "attempt-1")
    artifact = WorkerArtifact("result", "result.json", hashlib.sha256(b"").hexdigest(), 0, "application/json")

    with pytest.raises(DrawingMachineError, match=r"missing.*result.json"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_promote_rejects_undeclared_regular_file(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    (staging / "extra.txt").write_text("undeclared", encoding="utf-8")

    with pytest.raises(DrawingMachineError, match=r"undeclared.*extra.txt"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_promote_rejects_directory_not_needed_by_declared_paths(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    (staging / "empty").mkdir()

    with pytest.raises(DrawingMachineError, match=r"undeclared.*director.*empty"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_promote_rejects_symlink_even_when_target_is_inside_staging(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    target = staging / "result.json"
    target.write_text("{}", encoding="utf-8")
    (staging / "alias.json").symlink_to(target)
    artifact = WorkerArtifact("result", "alias.json", sha256_file(target), 2, "application/json")

    with pytest.raises(DrawingMachineError, match="symlink"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected("alias.json", required_json_values=()),),
        )


def test_promote_rejects_nested_directory_symlink(store: FilesystemArtifactStore, tmp_path: Path) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text("{}", encoding="utf-8")
    (staging / "nested").symlink_to(outside, target_is_directory=True)
    artifact = WorkerArtifact(
        "result", "nested/result.json", sha256_file(outside / "result.json"), 2, "application/json"
    )

    with pytest.raises(DrawingMachineError, match="symlink"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected("nested/result.json", required_json_values=()),),
        )


def test_promote_rejects_symlink_in_managed_staging_ancestors(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    staging_root = store.jobs_dir / ".staging"
    real_staging_root = store.jobs_dir / ".staging-real"
    staging_root.rename(real_staging_root)
    staging_root.symlink_to(real_staging_root.name, target_is_directory=True)

    with pytest.raises(DrawingMachineError, match="symlink"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_promote_rejects_fifo_without_opening_it(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    os.mkfifo(staging / "result.json", mode=0o600)
    artifact = WorkerArtifact("result", "result.json", "0" * 64, 0, "application/json")

    with pytest.raises(DrawingMachineError, match="regular file"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_import_rejects_character_device_source(store: FilesystemArtifactStore) -> None:
    null_device = Path("/dev/null")
    if not null_device.exists() or not stat.S_ISCHR(null_device.stat().st_mode):
        pytest.skip("platform has no /dev/null character device")

    with pytest.raises(DrawingMachineError, match="regular file"):
        store.import_file(
            "job-1",
            "attempt-1",
            null_device,
            role="input",
            relative_path="inputs/source.bin",
            media_type="application/octet-stream",
            max_bytes=1024,
        )


@pytest.mark.parametrize("field", ["size", "digest"])
def test_promote_rejects_size_or_digest_mismatch(store: FilesystemArtifactStore, field: str) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    if field == "size":
        artifact = WorkerArtifact(
            artifact.role,
            artifact.relative_path,
            artifact.sha256,
            artifact.size_bytes + 1,
            artifact.media_type,
        )
    else:
        artifact = WorkerArtifact(
            artifact.role,
            artifact.relative_path,
            "f" * 64,
            artifact.size_bytes,
            artifact.media_type,
        )

    with pytest.raises(DrawingMachineError, match=field):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"[]", "JSON root"),
        (b'{"schema_version": 2}', "schema_version"),
        (b'{"schema_version": true}', "schema_version"),
        (b"{broken", "valid JSON"),
    ],
)
def test_promote_validates_json_root_and_exact_discriminators(
    store: FilesystemArtifactStore,
    contents: bytes,
    message: str,
) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging, contents=contents)

    with pytest.raises(DrawingMachineError, match=message):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )


def test_promote_rejects_existing_attempt_without_overwriting(store: FilesystemArtifactStore) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    existing = store.jobs_dir / "job-1/artifacts/attempt-1"
    existing.mkdir(parents=True, mode=0o700)
    marker = existing / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(DrawingMachineError, match=r"promoted attempt.*exists"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )

    assert marker.read_text(encoding="utf-8") == "keep"
    assert staging.is_dir()


def test_promote_refuses_cross_filesystem_rename(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = store.create_staging("job-1", "attempt-1")
    artifact = _write_worker_artifact(staging)
    monkeypatch.setattr(secure_fs, "same_filesystem", lambda first_fd, second_fd: False)

    with pytest.raises(DrawingMachineError, match="same filesystem"):
        store.validate_and_promote(
            "job-1",
            "attempt-1",
            (artifact,),
            expected=(_expected(),),
        )

    assert staging.is_dir()


def test_import_rejects_symlink_source(store: FilesystemArtifactStore, tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    alias = tmp_path / "alias.png"
    alias.symlink_to(source)

    with pytest.raises(DrawingMachineError, match="symlink"):
        store.import_file(
            "job-1",
            "attempt-1",
            alias,
            role="input",
            relative_path="inputs/source.png",
            media_type="image/png",
            max_bytes=1024,
        )


def test_import_detects_source_path_replacement(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"original")
    real_copy = secure_fs.copy_external_file

    def replace_after_copy(
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

    monkeypatch.setattr(secure_fs, "copy_external_file", replace_after_copy)

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


def test_read_bytes_validates_and_returns_exact_promoted_contents(store: FilesystemArtifactStore) -> None:
    contents = b'{"schema_version":1,"ok":true}\n'
    artifact, _path = _promoted_result(store, contents=contents)

    result = store.read_bytes(
        "job-1",
        artifact,
        expected_media_type="application/json",
        max_bytes=1024,
    )

    assert result == contents


def test_read_bytes_rejects_invalid_reference_before_filesystem_access(
    store: FilesystemArtifactStore,
) -> None:
    with pytest.raises(DrawingMachineError, match="reference"):
        store.read_bytes(
            "job-1",
            object(),  # type: ignore[arg-type]
            expected_media_type="application/json",
            max_bytes=1024,
        )


def test_read_bytes_rejects_invalid_maximum_before_filesystem_access(
    store: FilesystemArtifactStore,
) -> None:
    artifact = ArtifactRef(
        "result",
        "artifacts/attempt-1/result.json",
        "a" * 64,
        2,
        "application/json",
    )

    with pytest.raises(DrawingMachineError, match="maximum"):
        store.read_bytes(
            "job-1",
            artifact,
            expected_media_type="application/json",
            max_bytes=0,
        )


@pytest.mark.parametrize("job_id", ["../job-1", "/absolute", "job/other", "job\\other"])
def test_read_bytes_rejects_unsafe_job_id(
    store: FilesystemArtifactStore,
    job_id: str,
) -> None:
    artifact, _path = _promoted_result(store)

    with pytest.raises(DrawingMachineError, match="job_id"):
        store.read_bytes(job_id, artifact, expected_media_type="application/json", max_bytes=1024)


@pytest.mark.parametrize("relative_path", ["/tmp/result.json", "artifacts/../result.json", "inputs/result.json"])
def test_read_bytes_rejects_absolute_traversal_or_nonpromoted_reference(
    store: FilesystemArtifactStore,
    relative_path: str,
) -> None:
    artifact, _path = _promoted_result(store)
    object.__setattr__(artifact, "relative_path", relative_path)

    with pytest.raises(DrawingMachineError, match=r"persisted artifact|relative.*path"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=1024)


@pytest.mark.parametrize("target", ["attempt", "file"])
def test_read_bytes_rejects_parent_and_final_symlink(
    store: FilesystemArtifactStore,
    tmp_path: Path,
    target: str,
) -> None:
    artifact, path = _promoted_result(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_bytes(path.read_bytes())
    if target == "attempt":
        attempt = path.parent
        displaced = attempt.with_name("attempt-original")
        attempt.rename(displaced)
        attempt.symlink_to(outside, target_is_directory=True)
    else:
        path.unlink()
        path.symlink_to(outside / "result.json")

    with pytest.raises(DrawingMachineError, match=r"symlink|opened safely|path|regular file"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=1024)


def test_read_bytes_rejects_nonregular_final_entry(store: FilesystemArtifactStore) -> None:
    artifact, path = _promoted_result(store)
    path.unlink()
    path.mkdir()

    with pytest.raises(DrawingMachineError, match=r"regular|safely|path"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=1024)


@pytest.mark.parametrize("field", ["size_bytes", "sha256", "media_type"])
def test_read_bytes_rejects_persisted_metadata_mismatch(
    store: FilesystemArtifactStore,
    field: str,
) -> None:
    artifact, _path = _promoted_result(store)
    if field == "size_bytes":
        object.__setattr__(artifact, field, artifact.size_bytes + 1)
    elif field == "sha256":
        object.__setattr__(artifact, field, "0" * 64)
    else:
        object.__setattr__(artifact, field, "text/plain")

    with pytest.raises(DrawingMachineError, match=r"metadata|media type|digest|size"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=1024)


def test_read_bytes_rejects_oversized_artifact(store: FilesystemArtifactStore) -> None:
    artifact, _path = _promoted_result(
        store,
        contents=b'{"schema_version":1,"value":"' + b"x" * 2048 + b'"}',
    )

    with pytest.raises(DrawingMachineError, match=r"maximum|large|size"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=8)


def test_read_bytes_detects_replacement_during_read(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, _path = _promoted_result(store)
    real_read = secure_fs.read_stable_regular_bounded

    def replace_after_read(parent_fd: int, name: str, *, max_bytes: int) -> tuple[secure_fs.StableRead, bytes]:
        copied, contents = real_read(parent_fd, name, max_bytes=max_bytes)
        os.rename(name, f"{name}.old", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        try:
            os.write(replacement, contents)
        finally:
            os.close(replacement)
        return copied, contents

    monkeypatch.setattr(secure_fs, "read_stable_regular_bounded", replace_after_read)

    with pytest.raises(DrawingMachineError, match=r"replaced|identity|path"):
        store.read_bytes("job-1", artifact, expected_media_type="application/json", max_bytes=1024)


def test_projection_is_deterministic_atomic_private_and_rebuildable(store: FilesystemArtifactStore) -> None:
    job = _make_job()
    zeta = ArtifactRef("zeta", "artifacts/a/zeta.txt", "f" * 64, 4, "text/plain")
    alpha = ArtifactRef("alpha", "artifacts/a/alpha.txt", "e" * 64, 5, "text/plain")

    store.write_projection(job, (zeta, alpha))
    projection = store.jobs_dir / "job-1/job.json"
    first_inode = projection.stat().st_ino
    store.write_projection(job, (alpha, zeta))

    assert projection.stat().st_ino != first_inode
    assert stat.S_IMODE(projection.stat().st_mode) == 0o600
    assert json.loads(projection.read_text(encoding="utf-8")) == {
        **job.to_json(),
        "artifacts": [alpha.to_json(), zeta.to_json()],
    }
    assert not [path for path in projection.parent.iterdir() if path.name.endswith(".tmp")]


def test_projection_rejects_cross_job_artifact_path(store: FilesystemArtifactStore) -> None:
    job = _make_job()
    artifact = ArtifactRef("result", "other/result.json", "e" * 64, 5, "application/json")

    with pytest.raises(DrawingMachineError, match="artifact"):
        store.write_projection(job, (artifact,))


def test_projection_fsyncs_its_job_parent(
    store: FilesystemArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_directories: list[int] = []
    real_fsync_directory = secure_fs.fsync_directory

    def track_directory(descriptor: int) -> None:
        synced_directories.append(os.fstat(descriptor).st_ino)
        real_fsync_directory(descriptor)

    monkeypatch.setattr(secure_fs, "fsync_directory", track_directory)

    store.write_projection(_make_job(), ())

    assert synced_directories[-1] == (store.jobs_dir / "job-1").stat().st_ino
