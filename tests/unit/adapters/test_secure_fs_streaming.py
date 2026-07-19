from __future__ import annotations

import errno
import hashlib
import importlib
import os
import stat
from pathlib import Path

import pytest

from drawingmachine.adapters.filesystem import _secure_fs


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def test_artifact_copy_hashes_without_accumulating_destination_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    contents = b"bounded-stream" * 4096
    (source / "artifact.bin").write_bytes(contents)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        _secure_fs,
        "inspect_descriptor",
        lambda _descriptor: pytest.fail("artifact copy must not accumulate copied bytes in memory"),
    )
    try:
        copied = _secure_fs.copy_regular_file(source_fd, "artifact.bin", destination_fd, "artifact.bin")
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert copied.size_bytes == len(contents)
    assert copied.sha256 == hashlib.sha256(contents).hexdigest()
    assert (destination / "artifact.bin").read_bytes() == contents


def test_module_import_and_platform_guard_handle_missing_renameat2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_cdll = _secure_fs.ctypes.CDLL
    try:
        monkeypatch.setattr(_secure_fs.ctypes, "CDLL", lambda *args, **kwargs: object())
        importlib.reload(_secure_fs)
        assert _secure_fs._renameat2 is None
        with pytest.raises(_secure_fs.SecurePlatformUnavailable):
            _secure_fs.require_secure_platform()
    finally:
        monkeypatch.setattr(_secure_fs.ctypes, "CDLL", real_cdll)
        importlib.reload(_secure_fs)


def test_identity_checks_reject_missing_or_replaced_managed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    descriptor = _open_directory(managed)
    try:
        with pytest.raises(_secure_fs.SecureFilesystemError, match="managed directory path was replaced"):
            _secure_fs.verify_path_identity(tmp_path / "missing", descriptor)

        replacement = tmp_path / "replacement"
        replacement.mkdir()
        with pytest.raises(_secure_fs.SecureFilesystemError, match="managed directory path was replaced"):
            _secure_fs.verify_path_identity(replacement, descriptor)

        monkeypatch.setattr(_secure_fs, "identity", lambda _status: object())
        with pytest.raises(_secure_fs.SecureFilesystemError, match="trusted directory was replaced"):
            _secure_fs.open_trusted_directory(managed)
    finally:
        os.close(descriptor)


def test_open_trusted_directory_enforces_private_mode_by_default(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    managed.chmod(0o755)
    descriptor = _secure_fs.open_trusted_directory(managed)
    try:
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o700
    finally:
        os.close(descriptor)


def test_managed_root_closes_data_descriptor_when_jobs_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[int] = []
    real_open = _secure_fs.open_trusted_directory

    def track_open(path: Path, *, enforce_mode: bool = True) -> int:
        descriptor = real_open(path, enforce_mode=enforce_mode)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(_secure_fs, "open_trusted_directory", track_open)
    monkeypatch.setattr(
        _secure_fs,
        "ensure_directory_at",
        lambda parent_fd, name: (_ for _ in ()).throw(RuntimeError("jobs unavailable")),
    )
    with (
        pytest.raises(RuntimeError, match="jobs unavailable"),
        _secure_fs.managed_jobs_root(tmp_path / "data"),
    ):
        pytest.fail("root must not be yielded")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_managed_jobs_root_leaves_preexisting_data_root_mode_unchanged(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir(mode=0o750)
    data_path.chmod(0o750)

    with _secure_fs.managed_jobs_root(data_path) as root:
        assert stat.S_IMODE(os.fstat(root.jobs_fd).st_mode) == 0o700

    assert stat.S_IMODE(data_path.stat().st_mode) == 0o750


@pytest.mark.parametrize("nonblocking", [False, True])
def test_regular_open_rejects_descriptor_identity_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nonblocking: bool,
) -> None:
    (tmp_path / "artifact").write_bytes(b"data")
    parent_fd = _open_directory(tmp_path)
    monkeypatch.setattr(_secure_fs, "identity", lambda _status: object())
    opener = _secure_fs.open_regular_nonblocking_at if nonblocking else _secure_fs.open_regular_at
    try:
        with pytest.raises(_secure_fs.SecureFilesystemError, match="replaced while opening"):
            opener(parent_fd, "artifact")
    finally:
        os.close(parent_fd)


def test_created_and_external_files_reject_descriptor_identity_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = _open_directory(tmp_path)
    monkeypatch.setattr(_secure_fs, "identity", lambda _status: object())
    try:
        with pytest.raises(_secure_fs.SecureFilesystemError, match="created regular file identity mismatch"):
            _secure_fs.create_regular_at(parent_fd, "created")
        source = tmp_path / "source"
        source.write_bytes(b"data")
        with pytest.raises(_secure_fs.SecureFilesystemError, match="source path was replaced"):
            _secure_fs.open_external_regular(source)
    finally:
        os.close(parent_fd)


def test_write_all_rejects_zero_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_secure_fs.os, "write", lambda descriptor, contents: 0)
    with pytest.raises(OSError, match="made no progress"):
        _secure_fs.write_all(1, b"data")


def test_copy_regular_rejects_source_change_and_closes_source_if_create_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "artifact").write_bytes(b"data")
    source_fd = _open_directory(source)
    destination_fd = _open_directory(destination)
    try:
        monkeypatch.setattr(_secure_fs, "stable_file_identity", lambda _status: object())
        with pytest.raises(_secure_fs.SecureFilesystemError, match="changed during copy"):
            _secure_fs.copy_regular_file(source_fd, "artifact", destination_fd, "copied")

        monkeypatch.undo()
        opened: list[int] = []
        real_open = _secure_fs.open_regular_at

        def track_open(parent_fd: int, name: str) -> int:
            descriptor = real_open(parent_fd, name)
            opened.append(descriptor)
            return descriptor

        monkeypatch.setattr(_secure_fs, "open_regular_at", track_open)
        monkeypatch.setattr(
            _secure_fs,
            "create_regular_at",
            lambda parent_fd, name: (_ for _ in ()).throw(OSError("create failed")),
        )
        with pytest.raises(OSError, match="create failed"):
            _secure_fs.copy_regular_file(source_fd, "artifact", destination_fd, "never-created")
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def test_external_copy_rejects_invalid_limit_type_and_size(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    destination_fd = _open_directory(destination)
    source = tmp_path / "source"
    source.write_bytes(b"data")
    source_fd = os.open(source, os.O_RDONLY)
    directory_fd = _open_directory(tmp_path)
    try:
        with pytest.raises(_secure_fs.SecureFilesystemError, match="positive integer"):
            _secure_fs.copy_external_file(source_fd, destination_fd, "invalid", max_bytes=True)
        with pytest.raises(_secure_fs.SecureFilesystemError, match="must be a regular file"):
            _secure_fs.copy_external_file(directory_fd, destination_fd, "directory", max_bytes=10)
        with pytest.raises(_secure_fs.SecureFilesystemError, match="exceeds"):
            _secure_fs.copy_external_file(source_fd, destination_fd, "too-large", max_bytes=3)
    finally:
        os.close(directory_fd)
        os.close(source_fd)
        os.close(destination_fd)


def test_stable_reads_reject_changed_identity_invalid_limit_and_dynamic_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "artifact").write_bytes(b"x")
    parent_fd = _open_directory(tmp_path)
    try:
        monkeypatch.setattr(_secure_fs, "stable_file_identity", lambda _status: object())
        with pytest.raises(_secure_fs.SecureFilesystemError, match="changed while reading"):
            _secure_fs.read_stable_regular(parent_fd, "artifact")

        monkeypatch.undo()
        with pytest.raises(_secure_fs.SecureFilesystemError, match="positive integer"):
            _secure_fs.read_stable_regular_bounded(parent_fd, "artifact", max_bytes=False)

        monkeypatch.setattr(_secure_fs.os, "read", lambda descriptor, size: b"xx")
        with pytest.raises(_secure_fs.SecureFilesystemError, match="exceeds"):
            _secure_fs.read_stable_regular_bounded(parent_fd, "artifact", max_bytes=1)
    finally:
        os.close(parent_fd)


def test_fsync_rejects_wrong_descriptor_types(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_bytes(b"data")
    file_fd = os.open(file_path, os.O_RDONLY)
    directory_fd = _open_directory(tmp_path)
    try:
        with pytest.raises(_secure_fs.SecureFilesystemError, match="file fsync requires"):
            _secure_fs.fsync_file(directory_fd)
        with pytest.raises(_secure_fs.SecureFilesystemError, match="directory fsync requires"):
            _secure_fs.fsync_directory(file_fd)
    finally:
        os.close(directory_fd)
        os.close(file_fd)


def test_rename_noreplace_propagates_non_collision_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_secure_fs, "_renameat2", lambda *args: -1)
    monkeypatch.setattr(_secure_fs.ctypes, "get_errno", lambda: errno.EPERM)
    with pytest.raises(OSError) as caught:
        _secure_fs.rename_noreplace(1, "source", 2, "destination")
    assert caught.value.errno == errno.EPERM
