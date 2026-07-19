from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import TypeAlias

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.service.access_policy import RuntimeAccessPolicy

FileIdentity: TypeAlias = tuple[int, int]


class InstanceLease:
    def __init__(self, socket_path: Path, access_policy: RuntimeAccessPolicy | None = None) -> None:
        self._socket_path = socket_path
        self._lock_path = socket_path.with_suffix(".lock")
        self._descriptor: int | None = None
        self._access_policy = access_policy

    @property
    def descriptor(self) -> int | None:
        return self._descriptor

    def acquire(self) -> None:
        if self._descriptor is not None:
            raise RuntimeError("drawingmachine instance lease is already acquired")

        if self._access_policy is None:
            self._socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._socket_path.parent.chmod(0o700)
        else:
            if self._socket_path != self._access_policy.snapshot.config.canonical_socket:
                raise RuntimeError("instance lease socket differs from service access policy")
            self._access_policy.prepare_runtime_directory()
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise socket_in_use(self._socket_path, "service lease path is not a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise socket_in_use(self._socket_path, "drawingmachine service lease is already held") from error
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def require_held_for(self, socket_path: Path) -> None:
        if self._descriptor is None or socket_path != self._socket_path:
            raise RuntimeError("drawingmachine instance lease handoff is not held for this socket")
        if self._access_policy is not None:
            self._access_policy.validate_runtime_directory()

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def identity(path_stat: os.stat_result) -> FileIdentity:
    return path_stat.st_dev, path_stat.st_ino


def open_path_descriptor(path: Path) -> int:
    return os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)


def pid_target_matches(path: Path, expected_identity: FileIdentity, payload: bytes) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        target_stat = os.fstat(descriptor)
        content = os.read(descriptor, len(payload) + 1)
    finally:
        os.close(descriptor)
    current = lstat(path)
    return (
        stat.S_ISREG(target_stat.st_mode)
        and identity(target_stat) == expected_identity
        and content == payload
        and current is not None
        and stat.S_ISREG(current.st_mode)
        and identity(current) == expected_identity
    )


def descriptor_has_exact_content(
    descriptor: int,
    expected_identity: FileIdentity,
    expected: bytes,
) -> bool:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or identity(before) != expected_identity or before.st_size != len(expected):
            return False
        content = os.pread(descriptor, len(expected) + 1, 0)
        after = os.fstat(descriptor)
    except OSError:
        return False
    return (
        content == expected
        and stat.S_ISREG(after.st_mode)
        and identity(after) == expected_identity
        and after.st_size == len(expected)
        and after.st_mtime_ns == before.st_mtime_ns
        and after.st_ctime_ns == before.st_ctime_ns
    )


def unlink_if_owned(path: Path, expected_identity: FileIdentity, *, require_socket: bool) -> bool:
    path_stat = lstat(path)
    if path_stat is None or identity(path_stat) != expected_identity:
        return False
    if require_socket and not stat.S_ISSOCK(path_stat.st_mode):
        return False
    if not require_socket and not stat.S_ISREG(path_stat.st_mode):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def socket_in_use(path: Path, message: str) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code="SERVICE_ALREADY_RUNNING",
            category=ErrorCategory.SERVICE,
            message=message,
            retryable=False,
            details={"socket": os.fspath(path)},
        )
    )
