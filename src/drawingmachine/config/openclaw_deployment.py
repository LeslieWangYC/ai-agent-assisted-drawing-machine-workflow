from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

_FIELDS = frozenset(
    {
        "schema_version",
        "operator_principal",
        "automation_principal",
        "automation_read_group",
        "registry_root",
        "managed_root",
        "managed_owner",
        "registry_owner",
        "managed_dir_mode",
        "target_mode",
        "manifest_sha256",
        "registry_policy_sha256",
    }
)
_PRINCIPAL_FIELDS = frozenset({"uid", "gid"})
_GROUP_FIELDS = frozenset({"name", "gid"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,63}\Z")
_MAX_POLICY_BYTES = 65_536
_MAX_ID = (2**32) - 2


@dataclass(frozen=True, slots=True)
class PosixPrincipalV1:
    uid: int
    gid: int

    def __post_init__(self) -> None:
        _id(self.uid, "principal.uid")
        _id(self.gid, "principal.gid")


@dataclass(frozen=True, slots=True)
class PosixGroupV1:
    name: str
    gid: int

    def __post_init__(self) -> None:
        if type(self.name) is not str or _GROUP_NAME.fullmatch(self.name) is None:
            raise TypeError("group name must be bounded canonical POSIX text")
        _id(self.gid, "group.gid")


@dataclass(frozen=True, slots=True)
class ServiceWriterSnapshotV1:
    uid: int
    gid: int

    def __post_init__(self) -> None:
        _id(self.uid, "writer.uid")
        _id(self.gid, "writer.gid")


@dataclass(frozen=True, slots=True)
class OpenClawDeploymentPolicyV1:
    source_sha256: str
    operator_principal: PosixPrincipalV1
    automation_principal: PosixPrincipalV1
    automation_read_group: PosixGroupV1
    registry_root: Path
    managed_root: Path
    managed_owner: PosixPrincipalV1
    registry_owner: PosixPrincipalV1
    managed_dir_mode: int
    target_mode: int
    manifest_sha256: str
    registry_policy_sha256: str


def parse_openclaw_deployment_policy(
    contents: bytes,
    *,
    source_sha256: str,
) -> OpenClawDeploymentPolicyV1:
    if type(contents) is not bytes or not contents or len(contents) > _MAX_POLICY_BYTES:
        _invalid("deployment policy must be non-empty exact bytes within 65536 bytes")
    _digest(source_sha256, "source_sha256")
    if hashlib.sha256(contents).hexdigest() != source_sha256:
        _invalid("deployment policy source digest does not match exact bytes")
    try:
        raw = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("deployment policy must be valid UTF-8 TOML") from error
    data = _table(raw, _FIELDS, "policy")
    if _integer(data["schema_version"], "schema_version") != 1:
        _invalid("schema_version must be 1")
    operator = _principal(data["operator_principal"], "operator_principal")
    automation = _principal(data["automation_principal"], "automation_principal")
    managed_owner = _principal(data["managed_owner"], "managed_owner")
    registry_owner = _principal(data["registry_owner"], "registry_owner")
    group_data = _table(data["automation_read_group"], _GROUP_FIELDS, "automation_read_group")
    group = PosixGroupV1(
        _string(group_data["name"], "automation_read_group.name"),
        _integer(group_data["gid"], "automation_read_group.gid"),
    )
    identities = (operator, automation, managed_owner)
    if len({item.uid for item in identities}) != 3 or len({item.gid for item in identities}) != 3:
        _invalid("operator, automation, and service writer principals must have disjoint UID/GID values")
    if registry_owner.uid in {operator.uid, automation.uid} or registry_owner.gid in {operator.gid, automation.gid}:
        _invalid("registry_owner must be disjoint from operator and automation principals")
    registry_root = _absolute_path(data["registry_root"], "registry_root")
    managed_root = _absolute_path(data["managed_root"], "managed_root")
    if registry_root == managed_root or registry_root in managed_root.parents or managed_root in registry_root.parents:
        _invalid("registry_root and managed_root must be distinct non-nested trees")
    managed_dir_mode = _integer(data["managed_dir_mode"], "managed_dir_mode")
    target_mode = _integer(data["target_mode"], "target_mode")
    if managed_dir_mode != 0o2750:
        _invalid("managed_dir_mode must be 2750")
    if target_mode != 0o640:
        _invalid("target_mode must be 0640")
    manifest_sha256 = _string(data["manifest_sha256"], "manifest_sha256")
    registry_policy_sha256 = _string(data["registry_policy_sha256"], "registry_policy_sha256")
    _digest(manifest_sha256, "manifest_sha256")
    _digest(registry_policy_sha256, "registry_policy_sha256")
    return OpenClawDeploymentPolicyV1(
        source_sha256,
        operator,
        automation,
        group,
        registry_root,
        managed_root,
        managed_owner,
        registry_owner,
        managed_dir_mode,
        target_mode,
        manifest_sha256,
        registry_policy_sha256,
    )


def load_openclaw_deployment_policy(
    path: Path,
    *,
    service_writer_snapshot: ServiceWriterSnapshotV1,
) -> OpenClawDeploymentPolicyV1:
    if type(path) is not type(Path()) or not path.is_absolute():
        _invalid("deployment policy path must be an absolute pathlib Path")
    if type(service_writer_snapshot) is not ServiceWriterSnapshotV1:
        _invalid("service writer snapshot must be exact")
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ValueError("deployment policy cannot be opened no-follow") from error
    try:
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 1
            or opened.st_uid != service_writer_snapshot.uid
            or stat.S_IMODE(opened.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
            or opened.st_size > _MAX_POLICY_BYTES
        ):
            _invalid("deployment policy ownership, type, mode, link, or size is unsafe")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(65_536, _MAX_POLICY_BYTES + 1 - size)):
            size += len(chunk)
            if size > _MAX_POLICY_BYTES:
                _invalid("deployment policy exceeds 65536 bytes")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        stable = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if stable != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (
            path_after.st_dev,
            path_after.st_ino,
        ) != (after.st_dev, after.st_ino):
            _invalid("deployment policy changed while reading")
    finally:
        os.close(descriptor)
    contents = b"".join(chunks)
    policy = parse_openclaw_deployment_policy(contents, source_sha256=hashlib.sha256(contents).hexdigest())
    if policy.managed_owner != PosixPrincipalV1(service_writer_snapshot.uid, service_writer_snapshot.gid):
        _invalid("deployment policy managed owner differs from trusted service writer")
    return policy


def _table(value: object, fields: frozenset[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{context} must be an exact table")
    result = cast(dict[str, object], value)
    if set(result) != fields or any(type(key) is not str for key in result):
        _invalid(f"{context} must contain exactly: {', '.join(sorted(fields))}")
    return result


def _principal(value: object, context: str) -> PosixPrincipalV1:
    data = _table(value, _PRINCIPAL_FIELDS, context)
    return PosixPrincipalV1(_integer(data["uid"], f"{context}.uid"), _integer(data["gid"], f"{context}.gid"))


def _absolute_path(value: object, field: str) -> Path:
    text = _string(value, field)
    if not text.isascii():
        _invalid(f"{field} must be canonical ASCII")
    if len(text.encode("utf-8")) > 4096 or any(ord(character) < 32 or ord(character) == 127 for character in text):
        _invalid(f"{field} must be bounded control-free canonical ASCII")
    path = Path(text)
    if (
        not path.is_absolute()
        or text.startswith("//")
        or str(path) != text
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a canonical normalized absolute path")
    return path


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        _invalid(f"{field} must be an exact integer")
    return value


def _id(value: int, field: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_ID:
        raise TypeError(f"{field} must be a bounded exact POSIX ID")


def _string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        _invalid(f"{field} must be a non-empty exact string")
    return value


def _digest(value: str, field: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _invalid(f"{field} must be canonical SHA-256")


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


__all__ = [
    "OpenClawDeploymentPolicyV1",
    "PosixGroupV1",
    "PosixPrincipalV1",
    "ServiceWriterSnapshotV1",
    "load_openclaw_deployment_policy",
    "parse_openclaw_deployment_policy",
]
