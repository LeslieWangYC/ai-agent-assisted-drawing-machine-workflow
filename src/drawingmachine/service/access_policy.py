from __future__ import annotations

import errno
import grp
import hashlib
import os
import pwd
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.config.service_access import (
    SERVICE_ACCESS_FILE_MODE,
    LoadedServiceAccessV1,
    ServiceAccessConfigV1,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

CANONICAL_RUNTIME_MODE = 0o710
CANONICAL_SOCKET_MODE = 0o660

AclTag = Literal["user", "user_obj", "group", "group_obj", "mask", "other"]
PathKind = Literal["directory", "regular", "socket", "symlink", "special"]


@dataclass(frozen=True, slots=True, order=True)
class PosixAclEntryV1:
    tag: AclTag
    qualifier: int | None
    permissions: int

    def __post_init__(self) -> None:
        if self.tag in {"user", "group"}:
            if type(self.qualifier) is not int or self.qualifier < 0:
                raise TypeError("named ACL entries require a non-negative qualifier")
        elif self.qualifier is not None:
            raise TypeError("object ACL entries cannot have a qualifier")
        if type(self.permissions) is not int or not 0 <= self.permissions <= 0o7:
            raise TypeError("ACL permissions must be an exact rwx bit set")


@dataclass(frozen=True, slots=True)
class PathAccessFactsV1:
    path: Path
    kind: PathKind
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    acl: tuple[PosixAclEntryV1, ...]
    default_acl: tuple[PosixAclEntryV1, ...] = ()

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True, slots=True)
class ServiceAccessSnapshotV1:
    config: ServiceAccessConfigV1
    source_path: Path
    source_size_bytes: int
    source_sha256: str
    source_identity: tuple[int, int]
    source_owner: PosixPrincipalV1
    source_mode: int
    source_acl: tuple[PosixAclEntryV1, ...]
    source_facts: PathAccessFactsV1
    machine_profile_sha256: str
    outer_runtime_facts: PathAccessFactsV1


class AccessFactsInspector(Protocol):
    def current_principal(self) -> PosixPrincipalV1: ...

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]: ...

    def group_member_uids(self, gid: int) -> frozenset[int]: ...

    def inspect(self, path: Path) -> PathAccessFactsV1: ...


class AccessPolicyApplier(Protocol):
    def ensure_directory(self, path: Path, *, uid: int, gid: int, mode: int) -> None: ...

    def set_socket_access(self, path: Path, *, uid: int, gid: int, mode: int) -> None: ...


class LinuxAccessFactsInspector:
    def current_principal(self) -> PosixPrincipalV1:
        return PosixPrincipalV1(os.getuid(), os.getgid())

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]:
        try:
            username = pwd.getpwuid(principal.uid).pw_name
            gids = os.getgrouplist(username, principal.gid)
        except (KeyError, OSError) as error:
            raise _error("configured principal group membership cannot be inspected") from error
        return frozenset(gids)

    def group_member_uids(self, gid: int) -> frozenset[int]:
        try:
            names = grp.getgrgid(gid).gr_mem
            return frozenset(pwd.getpwnam(name).pw_uid for name in names)
        except (KeyError, OSError) as error:
            raise _error("connect group membership cannot be inspected exactly") from error

    def inspect(self, path: Path) -> PathAccessFactsV1:
        try:
            value = os.lstat(path)
        except OSError as error:
            raise _error("required service access path cannot be inspected") from error
        return PathAccessFactsV1(
            path,
            _kind(value.st_mode),
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            stat.S_IMODE(value.st_mode),
            _read_acl(path, value.st_mode, attribute="system.posix_acl_access"),
            _read_acl(path, value.st_mode, attribute="system.posix_acl_default"),
        )


class LinuxAccessPolicyApplier:
    def ensure_directory(self, path: Path, *, uid: int, gid: int, mode: int) -> None:
        try:
            path.mkdir(mode=mode, exist_ok=True)
        except OSError as error:
            raise _error("canonical runtime directory cannot be created") from error
        before = os.lstat(path)
        if not stat.S_ISDIR(before.st_mode):
            raise _error("canonical runtime path is not a directory")
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)

    def set_socket_access(self, path: Path, *, uid: int, gid: int, mode: int) -> None:
        before = os.lstat(path)
        if not stat.S_ISSOCK(before.st_mode):
            raise _error("canonical service endpoint is not a socket")
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)


class RuntimeAccessPolicy:
    def __init__(
        self,
        snapshot: ServiceAccessSnapshotV1,
        *,
        inspector: AccessFactsInspector | None = None,
        applier: AccessPolicyApplier | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._inspector = LinuxAccessFactsInspector() if inspector is None else inspector
        self._applier = LinuxAccessPolicyApplier() if applier is None else applier

    def prepare_runtime_directory(self) -> None:
        self.validate_source()
        config = self.snapshot.config
        self._applier.ensure_directory(
            config.canonical_runtime_dir,
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=CANONICAL_RUNTIME_MODE,
        )
        self.validate_runtime_directory()

    def validate_runtime_directory(self) -> PathAccessFactsV1:
        facts = self._inspector.inspect(self.snapshot.config.canonical_runtime_dir)
        _require_exact_path(
            facts,
            kind="directory",
            uid=self.snapshot.config.service_principal.uid,
            gid=self.snapshot.config.connect_group.gid,
            mode=CANONICAL_RUNTIME_MODE,
        )
        return facts

    def validate_source(self) -> None:
        expected = self.snapshot
        try:
            descriptor = os.open(expected.source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            raise _error("service access policy cannot be reopened no-follow") from error
        try:
            before = os.fstat(descriptor)
            contents = b""
            while chunk := os.read(descriptor, 65_537 - len(contents)):
                contents += chunk
                if len(contents) > 65_536:
                    raise _error("service access policy changed size")
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = self._inspector.inspect(expected.source_path)
        if (
            current.identity != expected.source_identity
            or (before.st_dev, before.st_ino) != expected.source_identity
            or (after.st_dev, after.st_ino) != expected.source_identity
            or len(contents) != expected.source_size_bytes
            or hashlib.sha256(contents).hexdigest() != expected.source_sha256
        ):
            raise _error("service access policy bytes, digest, or identity changed")
        outer = self._inspector.inspect(expected.config.canonical_runtime_dir.parent)
        if current != expected.source_facts or outer != expected.outer_runtime_facts:
            raise _error("frozen service access source or outer traversal facts changed")

    def apply_and_validate_bound_socket(self, expected_identity: tuple[int, int]) -> PathAccessFactsV1:
        config = self.snapshot.config
        self._applier.set_socket_access(
            config.canonical_socket,
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=CANONICAL_SOCKET_MODE,
        )
        facts = self.validate_bound_socket(expected_identity)
        return facts

    def validate_bound_socket(self, expected_identity: tuple[int, int]) -> PathAccessFactsV1:
        self.validate_source()
        facts = self._inspector.inspect(self.snapshot.config.canonical_socket)
        _require_exact_path(
            facts,
            kind="socket",
            uid=self.snapshot.config.service_principal.uid,
            gid=self.snapshot.config.connect_group.gid,
            mode=CANONICAL_SOCKET_MODE,
        )
        if facts.identity != expected_identity:
            raise _error("canonical service socket identity changed")
        return facts


def build_service_access_snapshot(
    loaded: LoadedServiceAccessV1,
    *,
    machine_profile_sha256: str,
    inspector: AccessFactsInspector | None = None,
) -> ServiceAccessSnapshotV1:
    if type(loaded) is not LoadedServiceAccessV1:
        raise _error("loaded service access policy must be exact")
    facts_inspector = LinuxAccessFactsInspector() if inspector is None else inspector
    config = loaded.config
    if facts_inspector.current_principal() != config.service_principal:
        raise _error("running service principal differs from service access policy")
    expected_members = {
        config.service_principal.uid,
        config.automation_principal.uid,
        config.operator_principal.uid,
    }
    actual_members = {
        principal.uid
        for principal in (config.service_principal, config.automation_principal, config.operator_principal)
        if config.connect_group.gid in facts_inspector.supplemental_gids(principal)
    }
    if actual_members != expected_members:
        raise _error("connect group must contain exactly all three configured principals")
    if facts_inspector.group_member_uids(config.connect_group.gid) != expected_members:
        raise _error("connect group has missing or extra supplemental members")
    source = facts_inspector.inspect(loaded.source_path)
    _require_exact_path(
        source,
        kind="regular",
        uid=config.service_principal.uid,
        gid=config.connect_group.gid,
        mode=SERVICE_ACCESS_FILE_MODE,
    )
    if source.identity != loaded.source_identity:
        raise _error("service access policy identity changed after exact-byte load")
    outer = facts_inspector.inspect(config.canonical_runtime_dir.parent)
    _require_outer_traverse(outer, config.service_principal, config.connect_group.gid)
    return ServiceAccessSnapshotV1(
        config,
        loaded.source_path,
        loaded.source_size_bytes,
        loaded.source_sha256,
        loaded.source_identity,
        PosixPrincipalV1(source.uid, source.gid),
        source.mode,
        source.acl,
        source,
        machine_profile_sha256,
        outer,
    )


def _require_exact_path(
    facts: PathAccessFactsV1,
    *,
    kind: PathKind,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    if facts.kind != kind or (facts.uid, facts.gid, facts.mode) != (uid, gid, mode):
        raise _error("service access path type, owner, group, or mode is not exact")
    if facts.acl != _mode_acl(mode):
        raise _error("service access path ACL is not exact")


def _require_outer_traverse(facts: PathAccessFactsV1, service: PosixPrincipalV1, connect_gid: int) -> None:
    expected = tuple(
        sorted(
            (
                PosixAclEntryV1("user_obj", None, 0o7),
                PosixAclEntryV1("group_obj", None, 0),
                PosixAclEntryV1("group", connect_gid, 0o1),
                PosixAclEntryV1("mask", None, 0o1),
                PosixAclEntryV1("other", None, 0),
            )
        )
    )
    if (
        facts.kind != "directory"
        or (facts.uid, facts.gid) != (service.uid, service.gid)
        or facts.mode != 0o710
        or facts.acl != expected
    ):
        raise _error("outer runtime traversal must grant only exact connect-group search authority")


def _read_acl(path: Path, mode: int, *, attribute: str) -> tuple[PosixAclEntryV1, ...]:
    try:
        raw = os.getxattr(path, attribute, follow_symlinks=False)
    except OSError as error:
        absent_errnos = {errno.ENODATA}
        if hasattr(errno, "ENOATTR"):
            absent_errnos.add(errno.ENOATTR)
        if error.errno in absent_errnos:
            if attribute == "system.posix_acl_default":
                return ()
            return _mode_acl(stat.S_IMODE(mode))
        raise _error("POSIX ACL evidence cannot be read") from error
    if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != 2 or (len(raw) - 4) % 8:
        raise _error("POSIX ACL evidence is malformed")
    tag_names: dict[int, AclTag] = {1: "user_obj", 2: "user", 4: "group_obj", 8: "group", 16: "mask", 32: "other"}
    entries: list[PosixAclEntryV1] = []
    for offset in range(4, len(raw), 8):
        tag, permissions, qualifier = struct.unpack_from("<HHI", raw, offset)
        try:
            name = tag_names[tag]
        except KeyError as error:
            raise _error("POSIX ACL evidence has an unknown tag") from error
        entries.append(PosixAclEntryV1(name, qualifier if name in {"user", "group"} else None, permissions))
    return tuple(sorted(entries))


def _mode_acl(mode: int) -> tuple[PosixAclEntryV1, ...]:
    return tuple(
        sorted(
            (
                PosixAclEntryV1("user_obj", None, (mode >> 6) & 0o7),
                PosixAclEntryV1("group_obj", None, (mode >> 3) & 0o7),
                PosixAclEntryV1("other", None, mode & 0o7),
            )
        )
    )


def _kind(mode: int) -> PathKind:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _error(message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload("SERVICE_ACCESS_INVALID", ErrorCategory.PERMISSION, message, False, {}))


__all__ = [
    "CANONICAL_RUNTIME_MODE",
    "CANONICAL_SOCKET_MODE",
    "AccessFactsInspector",
    "AccessPolicyApplier",
    "LinuxAccessFactsInspector",
    "LinuxAccessPolicyApplier",
    "PathAccessFactsV1",
    "PosixAclEntryV1",
    "RuntimeAccessPolicy",
    "ServiceAccessSnapshotV1",
    "build_service_access_snapshot",
]
