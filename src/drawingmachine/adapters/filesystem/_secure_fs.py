from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

RENAME_NOREPLACE = 1
O_NOFOLLOW: int | None = getattr(os, "O_NOFOLLOW", None)
O_DIRECTORY: int | None = getattr(os, "O_DIRECTORY", None)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_RenameAt2 = Callable[[int, bytes, int, bytes, int], int]
_LIBC = ctypes.CDLL(None, use_errno=True)
_raw_renameat2 = getattr(_LIBC, "renameat2", None)
if _raw_renameat2 is not None:
    _raw_renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _raw_renameat2.restype = ctypes.c_int
_renameat2 = cast(_RenameAt2 | None, _raw_renameat2)
_PROJECTION_TEMP = re.compile(r"\.job\.json\.tmp-[0-9a-f]{32}\Z")


class SecureFilesystemError(RuntimeError):
    pass


class SecurePlatformUnavailable(SecureFilesystemError):
    pass


@dataclass(frozen=True, slots=True)
class CopiedFile:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StableRead:
    sha256: str
    size_bytes: int
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ManagedJobsRoot:
    data_fd: int
    jobs_fd: int
    data_path: Path

    def verify(self) -> None:
        verify_path_identity(self.data_path, self.data_fd)
        verify_entry_identity(self.data_fd, "jobs", self.jobs_fd, directory=True)


def require_secure_platform() -> None:
    if (
        sys.platform != "linux"
        or O_NOFOLLOW is None
        or O_NOFOLLOW == 0
        or O_DIRECTORY is None
        or O_DIRECTORY == 0
        or _renameat2 is None
    ):
        raise SecurePlatformUnavailable(
            "required Linux secure filesystem primitives are unavailable (O_NOFOLLOW, O_DIRECTORY, renameat2)"
        )


def _directory_flags() -> int:
    require_secure_platform()
    assert O_DIRECTORY is not None
    assert O_NOFOLLOW is not None
    return os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | _O_CLOEXEC


def _regular_read_flags() -> int:
    require_secure_platform()
    assert O_NOFOLLOW is not None
    return os.O_RDONLY | O_NOFOLLOW | _O_CLOEXEC


def _regular_nonblocking_read_flags() -> int:
    return _regular_read_flags() | os.O_NONBLOCK


def _regular_create_flags() -> int:
    require_secure_platform()
    assert O_NOFOLLOW is not None
    return os.O_RDWR | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | _O_CLOEXEC


def identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def stable_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def stat_at(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def verify_path_identity(path: Path, descriptor: int) -> None:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise SecureFilesystemError(f"managed directory path was replaced: {path}") from error
    descriptor_status = os.fstat(descriptor)
    if (
        stat.S_ISLNK(path_status.st_mode)
        or not stat.S_ISDIR(path_status.st_mode)
        or not stat.S_ISDIR(descriptor_status.st_mode)
        or identity(path_status) != identity(descriptor_status)
    ):
        raise SecureFilesystemError(f"managed directory path was replaced: {path}")


def verify_entry_identity(parent_fd: int, name: str, descriptor: int, *, directory: bool) -> None:
    if not entry_matches_descriptor(parent_fd, name, descriptor, directory=directory):
        raise SecureFilesystemError(f"managed directory entry was replaced: {name}")


def entry_matches_descriptor(parent_fd: int, name: str, descriptor: int, *, directory: bool) -> bool:
    try:
        path_status = stat_at(parent_fd, name)
    except OSError:
        return False
    descriptor_status = os.fstat(descriptor)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return not (
        stat.S_ISLNK(path_status.st_mode)
        or not expected_type(path_status.st_mode)
        or not expected_type(descriptor_status.st_mode)
        or identity(path_status) != identity(descriptor_status)
    )


def open_trusted_directory(path: Path, *, enforce_mode: bool = True) -> int:
    try:
        path_status = path.lstat()
        descriptor = os.open(path, _directory_flags())
    except OSError as error:
        raise SecureFilesystemError(f"trusted directory cannot be opened safely: {path}") from error
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise SecureFilesystemError(f"trusted directory was replaced while opening: {path}")
        if enforce_mode:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_directory_at(parent_fd: int, name: str) -> int:
    try:
        path_status = stat_at(parent_fd, name)
        if stat.S_ISLNK(path_status.st_mode):
            raise SecureFilesystemError(f"managed directory symlink is forbidden: {name}")
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except SecureFilesystemError:
        raise
    except OSError as error:
        raise SecureFilesystemError(f"managed directory cannot be opened safely: {name}") from error
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise SecureFilesystemError(f"managed directory was replaced while opening: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ensure_directory_at(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    descriptor = open_directory_at(parent_fd, name)
    try:
        os.fchmod(descriptor, 0o700)
        if created:
            fsync_directory(parent_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_directory_at(parent_fd: int, name: str) -> int:
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    descriptor = open_directory_at(parent_fd, name)
    try:
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def managed_jobs_root(data_path: Path) -> Iterator[ManagedJobsRoot]:
    require_secure_platform()
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileExistsError):
        os.mkdir(data_path, mode=0o700)
    data_fd = open_trusted_directory(data_path, enforce_mode=False)
    jobs_fd: int | None = None
    try:
        jobs_fd = ensure_directory_at(data_fd, "jobs")
        root = ManagedJobsRoot(data_fd=data_fd, jobs_fd=jobs_fd, data_path=data_path)
        root.verify()
        yield root
        root.verify()
    finally:
        if jobs_fd is not None:
            os.close(jobs_fd)
        os.close(data_fd)


def open_regular_at(parent_fd: int, name: str) -> int:
    try:
        path_status = stat_at(parent_fd, name)
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise SecureFilesystemError(f"regular file entry is not a regular file: {name}")
        descriptor = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    except SecureFilesystemError:
        raise
    except OSError as error:
        raise SecureFilesystemError(f"regular file cannot be opened safely: {name}") from error
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISREG(path_status.st_mode)
            or not stat.S_ISREG(descriptor_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise SecureFilesystemError(f"regular file was replaced while opening: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_regular_nonblocking_at(parent_fd: int, name: str) -> int:
    """Reject non-regular entries before a nonblocking descriptor-relative open."""
    try:
        path_status = stat_at(parent_fd, name)
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise SecureFilesystemError(f"regular file entry is not a regular file: {name}")
        descriptor = os.open(name, _regular_nonblocking_read_flags(), dir_fd=parent_fd)
    except SecureFilesystemError:
        raise
    except OSError as error:
        raise SecureFilesystemError(f"regular file cannot be opened safely: {name}") from error
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISREG(path_status.st_mode)
            or not stat.S_ISREG(descriptor_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise SecureFilesystemError(f"regular file was replaced while opening: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_regular_at(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, _regular_create_flags(), 0o600, dir_fd=parent_fd)
    try:
        descriptor_status = os.fstat(descriptor)
        path_status = stat_at(parent_fd, name)
        if not stat.S_ISREG(descriptor_status.st_mode) or identity(path_status) != identity(descriptor_status):
            raise SecureFilesystemError(f"created regular file identity mismatch: {name}")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_external_regular(path: Path) -> tuple[int, os.stat_result]:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise SecureFilesystemError(f"artifact source cannot be inspected: {path}") from error
    if stat.S_ISLNK(path_status.st_mode):
        raise SecureFilesystemError(f"artifact source must not be a symlink: {path}")
    if not stat.S_ISREG(path_status.st_mode):
        raise SecureFilesystemError(f"artifact source must be a regular file: {path}")
    try:
        descriptor = os.open(path, _regular_read_flags())
    except OSError as error:
        raise SecureFilesystemError(f"artifact source cannot be opened safely: {path}") from error
    descriptor_status = os.fstat(descriptor)
    if not stat.S_ISREG(descriptor_status.st_mode) or identity(path_status) != identity(descriptor_status):
        os.close(descriptor)
        raise SecureFilesystemError(f"artifact source path was replaced while opening: {path}")
    return descriptor, descriptor_status


def verify_external_regular(path: Path, descriptor: int, baseline: os.stat_result) -> None:
    descriptor_status = os.fstat(descriptor)
    try:
        path_status = path.lstat()
    except OSError as error:
        raise SecureFilesystemError(f"artifact source path was replaced after import: {path}") from error
    if (
        stable_file_identity(descriptor_status) != stable_file_identity(baseline)
        or identity(path_status) != identity(descriptor_status)
        or not stat.S_ISREG(path_status.st_mode)
    ):
        raise SecureFilesystemError(f"artifact source path was replaced or changed during import: {path}")


def write_all(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("filesystem write made no progress")
        view = view[written:]


def _copy_bytes(source_fd: int, destination_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        write_all(destination_fd, chunk)


def _copy_bytes_bounded(source_fd: int, destination_fd: int, *, max_bytes: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    size_bytes = 0
    while True:
        chunk = os.read(source_fd, min(1024 * 1024, max_bytes + 1 - size_bytes))
        if not chunk:
            return
        size_bytes += len(chunk)
        if size_bytes > max_bytes:
            raise SecureFilesystemError("external artifact source exceeds the maximum import size")
        write_all(destination_fd, chunk)


def inspect_descriptor(descriptor: int) -> tuple[CopiedFile, bytes]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        chunks.append(chunk)
        size_bytes += len(chunk)
    return CopiedFile(digest.hexdigest(), size_bytes), b"".join(chunks)


def inspect_descriptor_metadata(descriptor: int) -> CopiedFile:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        size_bytes += len(chunk)
    return CopiedFile(digest.hexdigest(), size_bytes)


def copy_regular_file(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> CopiedFile:
    source_fd = open_regular_at(source_parent_fd, source_name)
    destination_fd: int | None = None
    try:
        source_before = os.fstat(source_fd)
        destination_fd = create_regular_at(destination_parent_fd, destination_name)
        _copy_bytes(source_fd, destination_fd)
        source_after = os.fstat(source_fd)
        source_path_after = stat_at(source_parent_fd, source_name)
        if stable_file_identity(source_before) != stable_file_identity(source_after) or identity(
            source_after
        ) != identity(source_path_after):
            raise SecureFilesystemError(f"source regular file changed during copy: {source_name}")
        copied = inspect_descriptor_metadata(destination_fd)
        fsync_file(destination_fd)
        return copied
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def copy_external_file(
    source_fd: int,
    destination_parent_fd: int,
    destination_name: str,
    *,
    max_bytes: int,
) -> CopiedFile:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise SecureFilesystemError("artifact import maximum must be a positive integer")
    source_status = os.fstat(source_fd)
    if not stat.S_ISREG(source_status.st_mode):
        raise SecureFilesystemError("external artifact source must be a regular file")
    if source_status.st_size > max_bytes:
        raise SecureFilesystemError("external artifact source exceeds the maximum import size")
    destination_fd = create_regular_at(destination_parent_fd, destination_name)
    try:
        _copy_bytes_bounded(source_fd, destination_fd, max_bytes=max_bytes)
        copied = inspect_descriptor_metadata(destination_fd)
        fsync_file(destination_fd)
        return copied
    finally:
        os.close(destination_fd)


def read_stable_regular(parent_fd: int, name: str) -> tuple[CopiedFile, bytes]:
    descriptor = open_regular_at(parent_fd, name)
    try:
        before = os.fstat(descriptor)
        copied, contents = inspect_descriptor(descriptor)
        after = os.fstat(descriptor)
        path_after = stat_at(parent_fd, name)
        if stable_file_identity(before) != stable_file_identity(after) or identity(after) != identity(path_after):
            raise SecureFilesystemError(f"regular file changed while reading: {name}")
        return copied, contents
    finally:
        os.close(descriptor)


def read_stable_regular_bounded(
    parent_fd: int,
    name: str,
    *,
    max_bytes: int,
    nonblocking: bool = False,
) -> tuple[StableRead, bytes]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise SecureFilesystemError("artifact read maximum must be a positive integer")
    descriptor = open_regular_nonblocking_at(parent_fd, name) if nonblocking else open_regular_at(parent_fd, name)
    try:
        before = os.fstat(descriptor)
        if before.st_size > max_bytes:
            raise SecureFilesystemError(f"regular file exceeds the maximum read size: {name}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size_bytes = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size_bytes))
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise SecureFilesystemError(f"regular file exceeds the maximum read size: {name}")
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        path_after = stat_at(parent_fd, name)
        if stable_file_identity(before) != stable_file_identity(after) or identity(after) != identity(path_after):
            raise SecureFilesystemError(f"regular file changed while reading: {name}")
        return StableRead(digest.hexdigest(), size_bytes, stable_file_identity(after)), b"".join(chunks)
    finally:
        os.close(descriptor)


def fsync_file(descriptor: int) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise SecureFilesystemError("file fsync requires a regular file descriptor")
    os.fsync(descriptor)


def fsync_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise SecureFilesystemError("directory fsync requires a directory descriptor")
    os.fsync(descriptor)


def same_filesystem(first_fd: int, second_fd: int) -> bool:
    return os.fstat(first_fd).st_dev == os.fstat(second_fd).st_dev


def rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    require_secure_platform()
    assert _renameat2 is not None
    ctypes.set_errno(0)
    result = _renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination_name)
    raise OSError(error_number, os.strerror(error_number), destination_name)


def replace_file(parent_fd: int, temporary_name: str, destination_name: str) -> None:
    os.replace(
        temporary_name,
        destination_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )


def hidden_name(prefix: str) -> str:
    return f".{prefix}-{secrets.token_hex(16)}"


def cleanup_stale_projection_temps(job_fd: int) -> None:
    removed = False
    for name in sorted(os.listdir(job_fd)):
        if _PROJECTION_TEMP.fullmatch(name) is None:
            continue
        path_status = stat_at(job_fd, name)
        if stat.S_ISREG(path_status.st_mode) or stat.S_ISLNK(path_status.st_mode):
            os.unlink(name, dir_fd=job_fd)
            removed = True
            continue
        raise SecureFilesystemError(f"stale projection temporary entry has unsafe type: {name}")
    if removed:
        fsync_directory(job_fd)


def remove_regular_if_identity(parent_fd: int, name: str, descriptor: int) -> bool:
    if not entry_matches_descriptor(parent_fd, name, descriptor, directory=False):
        return False
    os.unlink(name, dir_fd=parent_fd)
    fsync_directory(parent_fd)
    return True


def remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        path_status = stat_at(parent_fd, name)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_status.st_mode) and not stat.S_ISLNK(path_status.st_mode):
        directory_fd = open_directory_at(parent_fd, name)
        try:
            for child in sorted(os.listdir(directory_fd)):
                remove_tree_at(directory_fd, child)
            verify_entry_identity(parent_fd, name, directory_fd, directory=True)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)
    fsync_directory(parent_fd)
