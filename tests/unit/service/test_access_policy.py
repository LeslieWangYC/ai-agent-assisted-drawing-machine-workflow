from __future__ import annotations

import errno
import hashlib
import os
import socket
import stat
import struct
from pathlib import Path

import pytest

from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.config.service_access import LoadedServiceAccessV1, parse_service_access_config
from drawingmachine.errors import DrawingMachineError
from drawingmachine.service.access_policy import (
    CANONICAL_RUNTIME_MODE,
    CANONICAL_SOCKET_MODE,
    AccessPolicyApplier,
    LinuxAccessFactsInspector,
    LinuxAccessPolicyApplier,
    PathAccessFactsV1,
    PosixAclEntryV1,
    RuntimeAccessPolicy,
    ServiceAccessSnapshotV1,
    build_service_access_snapshot,
)


def _policy(root: Path) -> bytes:
    return f'''\
schema_version = 1
canonical_runtime_dir = "{root}/outer/drawingmachine"
canonical_socket = "{root}/outer/drawingmachine/service.sock"
automation_runtime_endpoint = "{root}/a/drawingmachine/service.sock"
operator_runtime_endpoint = "{root}/o/drawingmachine/service.sock"
canonical_data_dir = "{root}/data"
automation_import_root = "{root}/data/imports/automation"
automation_export_root = "{root}/data/exports/automation"
automation_import_endpoint = "{root}/a-data/imports"
automation_export_endpoint = "{root}/a-data/exports"
openclaw_policy_path = "{root}/config/openclaw.toml"
[service_principal]
uid = {os.getuid()}
gid = {os.getgid()}
[automation_principal]
uid = 1100
gid = 1100
[operator_principal]
uid = 2200
gid = 2200
[connect_group]
name = "drawingmachine-connect"
gid = 4
'''.encode()


def _acl(mode: int) -> tuple[PosixAclEntryV1, ...]:
    return tuple(
        sorted(
            (
                PosixAclEntryV1("user_obj", None, mode >> 6 & 7),
                PosixAclEntryV1("group_obj", None, mode >> 3 & 7),
                PosixAclEntryV1("other", None, mode & 7),
            )
        )
    )


def _facts(path: Path, kind: str, uid: int, gid: int, mode: int, inode: int) -> PathAccessFactsV1:
    return PathAccessFactsV1(path, kind, 1, inode, uid, gid, mode, _acl(mode))  # type: ignore[arg-type]


class FakeInspector:
    def __init__(self, root: Path, loaded: LoadedServiceAccessV1) -> None:
        config = loaded.config
        self.current = config.service_principal
        self.groups = {
            config.service_principal.uid: frozenset({config.service_principal.gid, 4}),
            config.automation_principal.uid: frozenset({config.automation_principal.gid, 4}),
            config.operator_principal.uid: frozenset({config.operator_principal.gid, 4}),
        }
        self.group_members = frozenset(self.groups)
        outer_acl = tuple(
            sorted(
                (
                    PosixAclEntryV1("user_obj", None, 7),
                    PosixAclEntryV1("group_obj", None, 0),
                    PosixAclEntryV1("group", 4, 1),
                    PosixAclEntryV1("mask", None, 1),
                    PosixAclEntryV1("other", None, 0),
                )
            )
        )
        self.facts = {
            loaded.source_path: _facts(loaded.source_path, "regular", os.getuid(), 4, 0o640, 10),
            config.canonical_runtime_dir.parent: PathAccessFactsV1(
                config.canonical_runtime_dir.parent,
                "directory",
                1,
                20,
                os.getuid(),
                os.getgid(),
                0o710,
                outer_acl,
            ),
            config.canonical_runtime_dir: _facts(
                config.canonical_runtime_dir, "directory", os.getuid(), 4, CANONICAL_RUNTIME_MODE, 30
            ),
            config.canonical_socket: _facts(
                config.canonical_socket, "socket", os.getuid(), 4, CANONICAL_SOCKET_MODE, 40
            ),
        }
        del root

    def current_principal(self) -> PosixPrincipalV1:
        return self.current

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]:
        return self.groups[principal.uid]

    def group_member_uids(self, gid: int) -> frozenset[int]:
        assert gid == 4
        return self.group_members

    def inspect(self, path: Path) -> PathAccessFactsV1:
        return self.facts[path]


class FakeApplier(AccessPolicyApplier):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, int, int, int]] = []

    def ensure_directory(self, path: Path, *, uid: int, gid: int, mode: int) -> None:
        self.calls.append(("directory", path, uid, gid, mode))

    def set_socket_access(self, path: Path, *, uid: int, gid: int, mode: int) -> None:
        self.calls.append(("socket", path, uid, gid, mode))


def _loaded(tmp_path: Path) -> tuple[LoadedServiceAccessV1, FakeInspector]:
    contents = _policy(tmp_path)
    path = tmp_path / "service-access.toml"
    path.write_bytes(contents)
    identity = (path.stat().st_dev, path.stat().st_ino)
    loaded = LoadedServiceAccessV1(
        parse_service_access_config(contents),
        path,
        len(contents),
        hashlib.sha256(contents).hexdigest(),
        identity,
        0o640,
    )
    inspector = FakeInspector(tmp_path, loaded)
    inspector.facts[path] = PathAccessFactsV1(
        path, "regular", identity[0], identity[1], os.getuid(), 4, 0o640, _acl(0o640)
    )
    return loaded, inspector


def test_service_access_snapshot_is_immutable_and_exact(tmp_path: Path) -> None:
    loaded, inspector = _loaded(tmp_path)
    snapshot = build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)

    assert type(snapshot) is ServiceAccessSnapshotV1
    assert snapshot.source_sha256 == hashlib.sha256(_policy(tmp_path)).hexdigest()
    assert snapshot.machine_profile_sha256 == "a" * 64
    with pytest.raises(AttributeError):
        snapshot.source_sha256 = "b" * 64  # type: ignore[misc]


@pytest.mark.parametrize("failure", ["current", "service-group", "automation-group", "operator-group", "extra-member"])
def test_snapshot_rejects_wrong_principal_or_connect_membership(tmp_path: Path, failure: str) -> None:
    loaded, inspector = _loaded(tmp_path)
    if failure == "current":
        inspector.current = PosixPrincipalV1(9999, 9999)
    elif failure != "extra-member":
        uid = {
            "service-group": loaded.config.service_principal.uid,
            "automation-group": loaded.config.automation_principal.uid,
            "operator-group": loaded.config.operator_principal.uid,
        }[failure]
        inspector.groups[uid] = frozenset({9999})
    else:
        inspector.group_members = inspector.group_members | {9999}
    with pytest.raises(DrawingMachineError):
        build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)


@pytest.mark.parametrize(
    "fault", ["source-kind", "source-owner", "source-group", "source-mode", "source-acl", "source-id", "outer"]
)
def test_snapshot_rejects_unsafe_policy_or_outer_traversal(tmp_path: Path, fault: str) -> None:
    loaded, inspector = _loaded(tmp_path)
    source = inspector.facts[loaded.source_path]
    if fault == "source-kind":
        source = _facts(source.path, "special", source.uid, source.gid, source.mode, source.inode)
    elif fault == "source-owner":
        source = _facts(source.path, source.kind, 9999, source.gid, source.mode, source.inode)
    elif fault == "source-group":
        source = _facts(source.path, source.kind, source.uid, 9999, source.mode, source.inode)
    elif fault == "source-mode":
        source = _facts(source.path, source.kind, source.uid, source.gid, 0o660, source.inode)
    elif fault == "source-acl":
        source = PathAccessFactsV1(source.path, source.kind, 1, source.inode, source.uid, source.gid, source.mode, ())
    elif fault == "source-id":
        source = _facts(source.path, source.kind, source.uid, source.gid, source.mode, source.inode + 1)
    else:
        outer = inspector.facts[loaded.config.canonical_runtime_dir.parent]
        inspector.facts[outer.path] = _facts(outer.path, "directory", outer.uid, outer.gid, 0o770, outer.inode)
    inspector.facts[loaded.source_path] = source
    with pytest.raises(DrawingMachineError):
        build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)


def test_runtime_policy_preserves_0710_and_applies_0660_then_revalidates(tmp_path: Path) -> None:
    loaded, inspector = _loaded(tmp_path)
    snapshot = build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)
    applier = FakeApplier()
    policy = RuntimeAccessPolicy(snapshot, inspector=inspector, applier=applier)

    policy.prepare_runtime_directory()
    socket_facts = policy.apply_and_validate_bound_socket((1, 40))

    assert socket_facts.identity == (1, 40)
    assert applier.calls == [
        ("directory", loaded.config.canonical_runtime_dir, os.getuid(), 4, 0o710),
        ("socket", loaded.config.canonical_socket, os.getuid(), 4, 0o660),
    ]


def test_runtime_policy_rejects_digest_or_socket_identity_change(tmp_path: Path) -> None:
    loaded, inspector = _loaded(tmp_path)
    snapshot = build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)
    policy = RuntimeAccessPolicy(snapshot, inspector=inspector, applier=FakeApplier())
    loaded.source_path.write_bytes(b"changed")
    with pytest.raises(DrawingMachineError, match="digest"):
        policy.prepare_runtime_directory()

    loaded.source_path.write_bytes(_policy(tmp_path))
    with pytest.raises(DrawingMachineError, match="identity"):
        policy.validate_bound_socket((1, 999))


@pytest.mark.parametrize(
    "target",
    [
        "source-owner",
        "source-group",
        "source-mode",
        "source-acl",
        "outer-owner",
        "outer-group",
        "outer-mode",
        "outer-acl",
    ],
)
@pytest.mark.parametrize("checkpoint", ["pre-lease", "post-bind"])
def test_runtime_policy_revalidates_frozen_source_and_outer_facts(
    tmp_path: Path,
    target: str,
    checkpoint: str,
) -> None:
    loaded, inspector = _loaded(tmp_path)
    snapshot = build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)
    policy = RuntimeAccessPolicy(snapshot, inspector=inspector, applier=FakeApplier())
    path = loaded.source_path if target.startswith("source") else loaded.config.canonical_runtime_dir.parent
    facts = inspector.facts[path]
    if target.endswith("owner"):
        inspector.facts[path] = _facts(path, facts.kind, facts.uid + 1, facts.gid, facts.mode, facts.inode)
    elif target.endswith("group"):
        inspector.facts[path] = _facts(path, facts.kind, facts.uid, facts.gid + 1, facts.mode, facts.inode)
    elif target.endswith("mode"):
        inspector.facts[path] = _facts(path, facts.kind, facts.uid, facts.gid, facts.mode | 0o020, facts.inode)
    else:
        inspector.facts[path] = PathAccessFactsV1(
            path,
            facts.kind,
            facts.device,
            facts.inode,
            facts.uid,
            facts.gid,
            facts.mode,
            (),
        )
    with pytest.raises(DrawingMachineError, match="changed"):
        if checkpoint == "pre-lease":
            policy.prepare_runtime_directory()
        else:
            policy.validate_bound_socket((1, 40))


@pytest.mark.parametrize(
    ("tag", "qualifier", "permissions"),
    [("group", None, 1), ("other", 1, 0), ("user_obj", None, 8)],
)
def test_acl_evidence_rejects_invalid_closed_entries(tag: str, qualifier: int | None, permissions: int) -> None:
    with pytest.raises(TypeError):
        PosixAclEntryV1(tag, qualifier, permissions)  # type: ignore[arg-type]


def test_linux_inspector_reads_current_identity_groups_and_native_file_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = LinuxAccessFactsInspector()
    principal = inspector.current_principal()
    assert principal == PosixPrincipalV1(os.getuid(), os.getgid())
    assert principal.gid in inspector.supplemental_gids(principal)
    assert os.getuid() in inspector.group_member_uids(4)

    directory = tmp_path / "directory"
    regular = tmp_path / "regular"
    link = tmp_path / "link"
    fifo = tmp_path / "fifo"
    endpoint = tmp_path / "socket"
    directory.mkdir()
    regular.write_text("x")
    link.symlink_to(regular)
    os.mkfifo(fifo)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    try:
        assert inspector.inspect(directory).kind == "directory"
        assert inspector.inspect(regular).kind == "regular"
        with pytest.raises(DrawingMachineError, match="cannot be read"):
            inspector.inspect(link)
        monkeypatch.setattr(
            os,
            "getxattr",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.ENODATA, "none")),
        )
        assert inspector.inspect(link).kind == "symlink"
        assert inspector.inspect(fifo).kind == "special"
        assert inspector.inspect(endpoint).kind == "socket"
        with pytest.raises(DrawingMachineError):
            inspector.inspect(tmp_path / "missing")
        with pytest.raises(DrawingMachineError):
            inspector.supplemental_gids(PosixPrincipalV1(2**32 - 2, 2**32 - 2))
        with pytest.raises(DrawingMachineError, match="exactly"):
            inspector.group_member_uids(2**32 - 2)
    finally:
        listener.close()


def test_linux_acl_parser_accepts_all_closed_tags_and_rejects_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "entry"
    path.write_text("x")
    inspector = LinuxAccessFactsInspector()
    entries = b"".join(
        struct.pack("<HHI", tag, permission, qualifier)
        for tag, permission, qualifier in (
            (1, 7, 0xFFFFFFFF),
            (2, 4, 123),
            (4, 0, 0xFFFFFFFF),
            (8, 1, 456),
            (16, 5, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
    )
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: struct.pack("<I", 2) + entries)
    assert {entry.tag for entry in inspector.inspect(path).acl} == {
        "user_obj",
        "user",
        "group_obj",
        "group",
        "mask",
        "other",
    }

    for raw in (b"", struct.pack("<I", 3), struct.pack("<IHHI", 2, 99, 1, 0)):
        monkeypatch.setattr(os, "getxattr", lambda *args, _raw=raw, **kwargs: _raw)
        with pytest.raises(DrawingMachineError):
            inspector.inspect(path)

    for denied_errno in (errno.EACCES, errno.EIO):
        monkeypatch.setattr(
            os,
            "getxattr",
            lambda *args, _errno=denied_errno, **kwargs: (_ for _ in ()).throw(OSError(_errno, "denied")),
        )
        with pytest.raises(DrawingMachineError, match="cannot be read"):
            inspector.inspect(path)

    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.ENODATA, "none")))
    assert inspector.inspect(path).acl == _acl(stat.S_IMODE(path.stat().st_mode))
    monkeypatch.setattr(errno, "ENOATTR", 9999, raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(9999, "none")))
    assert inspector.inspect(path).acl == _acl(stat.S_IMODE(path.stat().st_mode))


def test_linux_inspector_reads_default_acl_and_fails_closed_on_bad_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "directory"
    path.mkdir()
    inspector = LinuxAccessFactsInspector()
    access = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, 0xFFFFFFFF) for tag, permissions in ((1, 7), (4, 5), (32, 0))
    )
    default = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, 0xFFFFFFFF) for tag, permissions in ((1, 7), (4, 7), (32, 0))
    )

    def getxattr(_path: Path, attribute: str, **_kwargs: object) -> bytes:
        return access if attribute == "system.posix_acl_access" else default

    monkeypatch.setattr(os, "getxattr", getxattr)
    facts = inspector.inspect(path)
    assert facts.acl == _acl(0o750)
    assert facts.default_acl == _acl(0o770)

    def absent_default(_path: Path, attribute: str, **_kwargs: object) -> bytes:
        if attribute == "system.posix_acl_default":
            raise OSError(errno.ENODATA, "none")
        return access

    monkeypatch.setattr(os, "getxattr", absent_default)
    assert inspector.inspect(path).default_acl == ()

    for failure in (b"", struct.pack("<I", 3), struct.pack("<IHHI", 2, 99, 1, 0)):
        monkeypatch.setattr(
            os,
            "getxattr",
            lambda _path, attribute, *, _failure=failure, **_kwargs: (
                access if attribute == "system.posix_acl_access" else _failure
            ),
        )
        with pytest.raises(DrawingMachineError, match=r"malformed|unknown"):
            inspector.inspect(path)

    monkeypatch.setattr(
        os,
        "getxattr",
        lambda _path, attribute, **_kwargs: (
            access if attribute == "system.posix_acl_access" else (_ for _ in ()).throw(OSError(errno.EACCES, "denied"))
        ),
    )
    with pytest.raises(DrawingMachineError, match="cannot be read"):
        inspector.inspect(path)


def test_linux_access_applier_uses_only_native_directory_and_socket_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applier = LinuxAccessPolicyApplier()
    runtime = tmp_path / "runtime"
    applier.ensure_directory(runtime, uid=os.getuid(), gid=os.getgid(), mode=0o710)
    assert runtime.stat().st_mode & 0o777 == 0o710

    denied = tmp_path / "denied"
    real_mkdir = Path.mkdir
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda self, **kwargs: (
            (_ for _ in ()).throw(PermissionError()) if self == denied else real_mkdir(self, **kwargs)
        ),
    )
    with pytest.raises(DrawingMachineError, match="cannot be created"):
        applier.ensure_directory(denied, uid=os.getuid(), gid=os.getgid(), mode=0o710)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)

    regular = tmp_path / "regular"
    regular.write_text("x")
    monkeypatch.setattr(Path, "mkdir", lambda self, **kwargs: None if self == regular else real_mkdir(self, **kwargs))
    with pytest.raises(DrawingMachineError):
        applier.ensure_directory(regular, uid=os.getuid(), gid=os.getgid(), mode=0o710)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    with pytest.raises(DrawingMachineError):
        applier.set_socket_access(regular, uid=os.getuid(), gid=os.getgid(), mode=0o660)

    endpoint = tmp_path / "service.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    try:
        applier.set_socket_access(endpoint, uid=os.getuid(), gid=os.getgid(), mode=0o660)
        assert endpoint.stat().st_mode & 0o777 == 0o660
    finally:
        listener.close()


def test_runtime_source_reopen_and_oversize_fail_closed(tmp_path: Path) -> None:
    loaded, inspector = _loaded(tmp_path)
    snapshot = build_service_access_snapshot(loaded, machine_profile_sha256="a" * 64, inspector=inspector)
    policy = RuntimeAccessPolicy(snapshot, inspector=inspector, applier=FakeApplier())
    loaded.source_path.unlink()
    with pytest.raises(DrawingMachineError, match="reopened"):
        policy.validate_source()

    loaded.source_path.write_bytes(b"x" * 65_537)
    with pytest.raises(DrawingMachineError, match="size"):
        policy.validate_source()


def test_snapshot_rejects_non_exact_loaded_value() -> None:
    with pytest.raises(DrawingMachineError, match="must be exact"):
        build_service_access_snapshot(object(), machine_profile_sha256="a" * 64)  # type: ignore[arg-type]
