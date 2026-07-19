from __future__ import annotations

import errno
import os
import stat
import struct
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import drawingmachine.adapters.filesystem.client_data as client_data_module
from drawingmachine.adapters.filesystem.client_data import (
    ClientDataPermissionOracle,
    FilesystemClientInputStaging,
    NativeClientDataPermissionOracle,
)
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.client_data import ClientDataRole, DispatchWriteOutcome


@dataclass
class _Clock:
    value: int = 100

    def boot_id(self) -> str:
        return "boot-a"

    def monotonic_ns(self) -> int:
        return self.value


class _Permissions(ClientDataPermissionOracle):
    def validate_phase_directory(self, path: Path, role: ClientDataRole, phase: str) -> None:
        del path, role, phase

    def validate_client_bundle(self, path: Path, role: ClientDataRole) -> None:
        del path, role

    def validate_private_directory(self, path: Path) -> None:
        del path

    def validate_client_file(self, path: Path, role: ClientDataRole) -> None:
        del path, role

    def validate_export_file(self, path: Path) -> None:
        del path

    def validate_export_directory(self, path: Path) -> None:
        del path


def test_client_stages_direct_symlink_and_hardlink_sources(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    adapter = FilesystemClientInputStaging.for_test(import_root)
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    symlink = tmp_path / "source-link.png"
    symlink.symlink_to(source)
    hardlink = tmp_path / "source-hard.png"
    hardlink.hardlink_to(source)

    staged = [
        adapter.stage_image(path, f"12345678-1234-4234-8234-123456789ab{index}", _Clock())
        for index, path in enumerate((source, symlink, hardlink))
    ]

    assert all(item.role is ClientDataRole.IMAGE for item in staged)
    assert all(item.payload_path.read_bytes() == b"png" for item in staged)


def test_definitely_unsent_cleanup_is_identity_safe(tmp_path: Path) -> None:
    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    staged = adapter.stage_gcode(
        source,
        "12345678-1234-4234-8234-123456789abc",
        _Clock(),
    )

    adapter.discard_definitely_unsent(staged, DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES)

    assert not staged.bundle_path.exists()


@pytest.mark.parametrize("kind", ["broken", "loop", "fifo", "empty"])
def test_stage_rejects_unstable_special_or_empty_sources(tmp_path: Path, kind: str) -> None:
    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "source"
    if kind == "broken":
        source.symlink_to(tmp_path / "missing")
    elif kind == "loop":
        source.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.write_bytes(b"")

    with pytest.raises(DrawingMachineError):
        adapter.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())


def test_ambiguous_dispatch_never_allows_client_cleanup(tmp_path: Path) -> None:
    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    staged = adapter.stage_gcode(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    transferred = adapter.transfer_before_write(staged)

    adapter.discard_definitely_unsent(transferred, DispatchWriteOutcome.WRITE_POSSIBLE)

    assert transferred.bundle_path.exists()
    with pytest.raises(DrawingMachineError):
        adapter.transfer_before_write(transferred)


def test_client_endpoint_is_revalidated_and_request_uses_canonical_root(tmp_path: Path) -> None:
    canonical_import = tmp_path / "canonical/imports"
    canonical_export = tmp_path / "canonical/exports"
    for role in ClientDataRole:
        for phase in ("prepare", "drop"):
            (canonical_import / role.value / phase).mkdir(parents=True)
    canonical_export.mkdir(parents=True)
    endpoint_import = tmp_path / "client/imports"
    endpoint_export = tmp_path / "client/exports"
    endpoint_import.parent.mkdir(parents=True)
    endpoint_import.symlink_to(canonical_import, target_is_directory=True)
    endpoint_export.symlink_to(canonical_export, target_is_directory=True)
    validations: list[str] = []
    adapter = FilesystemClientInputStaging(
        endpoint_import,
        endpoint_export,
        permission_oracle=_Permissions(),
        published_import_root=canonical_import,
        published_export_root=canonical_export,
        endpoint_validator=lambda: validations.append("validated"),
        nonce_factory=lambda: "a" * 32,
    )
    source = tmp_path / "image.png"
    source.write_bytes(b"image")

    staged = adapter.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    transferred = adapter.transfer_before_write(staged)

    assert staged.payload_path == canonical_import / "image/drop" / staged.bundle_name / "payload"
    assert (endpoint_import / "image/drop" / staged.bundle_name / "payload").read_bytes() == b"image"
    assert transferred.lease.state.value == "DISPATCH_UNCERTAIN"
    assert validations == ["validated", "validated"]


def test_native_permission_oracle_requires_exact_publication_and_private_facts(tmp_path: Path) -> None:
    uid = os.getuid()
    gid = os.getgid()
    oracle = NativeClientDataPermissionOracle(service_uid=uid, automation_uid=uid, service_gid=gid)
    phase = tmp_path / "drop"
    phase.mkdir(mode=0o2770)
    phase.chmod(0o2770)
    bundle = phase / "bundle"
    bundle.mkdir(mode=0o2770)
    bundle.chmod(0o2770)
    private = tmp_path / "admitted"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    payload = bundle / "payload"
    payload.write_bytes(b"image")
    payload.chmod(0o640)
    exported = tmp_path / "exported"
    exported.write_bytes(b"image")
    exported.chmod(0o640)

    oracle.validate_phase_directory(phase, ClientDataRole.IMAGE, "drop")
    oracle.validate_client_bundle(bundle, ClientDataRole.IMAGE)
    oracle.validate_private_directory(private)
    NativeClientDataPermissionOracle(
        service_uid=uid,
        automation_uid=uid,
        service_gid=gid + 1,
    ).validate_private_directory(private)
    oracle.validate_client_file(payload, ClientDataRole.IMAGE)
    oracle.validate_export_file(exported)

    with pytest.raises(DrawingMachineError):
        oracle.validate_phase_directory(phase, ClientDataRole.IMAGE, "admitted")
    for path, validate in (
        (phase, lambda: oracle.validate_phase_directory(phase, ClientDataRole.IMAGE, "drop")),
        (bundle, lambda: oracle.validate_client_bundle(bundle, ClientDataRole.IMAGE)),
        (private, lambda: oracle.validate_private_directory(private)),
        (payload, lambda: oracle.validate_client_file(payload, ClientDataRole.IMAGE)),
        (exported, lambda: oracle.validate_export_file(exported)),
    ):
        path.chmod(0o777)
        with pytest.raises(DrawingMachineError):
            validate()


def test_native_export_directory_requires_exact_named_access_and_default_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "jobs"
    path.mkdir(mode=0o2750)
    path.chmod(0o2750)
    real_lstat = os.lstat
    modeled_mode = {"value": 0o2750}

    def modeled_lstat(target: os.PathLike[str] | str) -> os.stat_result:
        value = real_lstat(target)
        values = list(value)
        values[0] = stat.S_IFDIR | modeled_mode["value"]
        values[4] = 4100
        values[5] = 4400
        return os.stat_result(values)

    permissions = {"named": 0o5}

    def acl(*_args: object, **_kwargs: object) -> bytes:
        return b"".join(
            (
                struct.pack("<I", 2),
                struct.pack("<HHI", 1, 0o7, 0),
                struct.pack("<HHI", 2, permissions["named"], 4200),
                struct.pack("<HHI", 4, 0, 0),
                struct.pack("<HHI", 16, 0o5, 0),
                struct.pack("<HHI", 32, 0, 0),
            )
        )

    monkeypatch.setattr(client_data_module.os, "lstat", modeled_lstat)
    monkeypatch.setattr(client_data_module.os, "getxattr", acl)
    oracle = NativeClientDataPermissionOracle(service_uid=4100, automation_uid=4200, service_gid=4400)
    oracle.validate_export_directory(path)
    permissions["named"] = 0o7
    with pytest.raises(DrawingMachineError, match="ACL"):
        oracle.validate_export_directory(path)
    permissions["named"] = 0o5
    modeled_mode["value"] = 0o700
    with pytest.raises(DrawingMachineError, match="ownership"):
        oracle.validate_export_directory(path)


def test_native_export_file_accepts_inherited_named_automation_raw_five_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "export"
    path.write_bytes(b"x")
    real_lstat = os.lstat

    def modeled_lstat(target: os.PathLike[str] | str) -> os.stat_result:
        value = real_lstat(target)
        values = list(value)
        values[0] = stat.S_IFREG | 0o640
        values[3] = 1
        values[4] = 4100
        values[5] = 4400
        return os.stat_result(values)

    permissions = {"named": 0o5}

    def acl(path: object, attribute: str, *args: object, **kwargs: object) -> bytes:
        if attribute == "system.posix_acl_default":
            raise OSError(errno.ENODATA, "no default acl")
        return b"".join(
            (
                struct.pack("<I", 2),
                struct.pack("<HHI", 1, 0o6, 0),
                struct.pack("<HHI", 2, permissions["named"], 4200),
                struct.pack("<HHI", 4, 0, 0),
                struct.pack("<HHI", 16, 0o4, 0),
                struct.pack("<HHI", 32, 0, 0),
            )
        )

    monkeypatch.setattr(client_data_module.os, "lstat", modeled_lstat)
    monkeypatch.setattr(client_data_module.os, "getxattr", acl)
    oracle = NativeClientDataPermissionOracle(service_uid=4100, automation_uid=4200, service_gid=4400)

    oracle.validate_export_file(path)

    permissions["named"] = 0o4
    with pytest.raises(DrawingMachineError, match="ACL"):
        oracle.validate_export_file(path)


def test_native_phase_directory_requires_named_automation_and_service_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "drop"
    path.mkdir(mode=0o2770)
    path.chmod(0o2770)
    real_lstat = os.lstat

    def modeled_lstat(target: os.PathLike[str] | str) -> os.stat_result:
        value = real_lstat(target)
        values = list(value)
        values[0] = stat.S_IFDIR | 0o2770
        values[4] = 4100
        values[5] = 4400
        return os.stat_result(values)

    include_service = {"value": False}

    def acl(*_args: object, **_kwargs: object) -> bytes:
        entries = [
            struct.pack("<HHI", 1, 0o7, 0),
            struct.pack("<HHI", 2, 0o7, 4200),
            struct.pack("<HHI", 4, 0, 0),
            struct.pack("<HHI", 16, 0o7, 0),
            struct.pack("<HHI", 32, 0, 0),
        ]
        if include_service["value"]:
            entries.append(struct.pack("<HHI", 2, 0o7, 4100))
        return struct.pack("<I", 2) + b"".join(entries)

    monkeypatch.setattr(client_data_module.os, "lstat", modeled_lstat)
    monkeypatch.setattr(client_data_module.os, "getxattr", acl)
    oracle = NativeClientDataPermissionOracle(service_uid=4100, automation_uid=4200, service_gid=4400)

    with pytest.raises(DrawingMachineError, match="ACL"):
        oracle.validate_phase_directory(path, ClientDataRole.IMAGE, "drop")

    include_service["value"] = True
    oracle.validate_phase_directory(path, ClientDataRole.IMAGE, "drop")


def test_native_acl_parser_and_default_acl_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "export"
    path.write_bytes(b"x")
    real_lstat = os.lstat

    def modeled_lstat(target: os.PathLike[str] | str) -> os.stat_result:
        value = real_lstat(target)
        values = list(value)
        values[0] = stat.S_IFREG | 0o640
        values[3] = 1
        values[4] = 4100
        values[5] = 4400
        return os.stat_result(values)

    exact = b"".join(
        (
            struct.pack("<I", 2),
            struct.pack("<HHI", 1, 0o6, 0),
            struct.pack("<HHI", 2, 0o5, 4200),
            struct.pack("<HHI", 4, 0, 0),
            struct.pack("<HHI", 16, 0o4, 0),
            struct.pack("<HHI", 32, 0, 0),
        )
    )
    monkeypatch.setattr(client_data_module.os, "lstat", modeled_lstat)
    monkeypatch.setattr(client_data_module.os, "getxattr", lambda *_args, **_kwargs: exact)
    oracle = NativeClientDataPermissionOracle(service_uid=4100, automation_uid=4200, service_gid=4400)
    with pytest.raises(DrawingMachineError, match="default ACL"):
        oracle.validate_export_file(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(client_data_module.errno, "ENOATTR", 999, raising=False)

        def missing(*_args: object, **_kwargs: object) -> bytes:
            raise OSError(999, "absent")

        scoped.setattr(client_data_module.os, "getxattr", missing)
        assert client_data_module._read_posix_acl(path, "system.posix_acl_default", 0o640) == ()
    for raw, message in ((b"bad", "malformed"), (struct.pack("<IHHI", 2, 99, 0o7, 0), "unknown tag")):
        monkeypatch.setattr(client_data_module.os, "getxattr", lambda *_args, value=raw, **_kwargs: value)
        with pytest.raises(DrawingMachineError, match=message):
            client_data_module._read_posix_acl(path, "system.posix_acl_access", 0o640)

    def unreadable(*_args: object, **_kwargs: object) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(client_data_module.os, "getxattr", unreadable)
    with pytest.raises(DrawingMachineError, match="cannot be read"):
        client_data_module._read_posix_acl(path, "system.posix_acl_access", 0o640)
    monkeypatch.setattr(client_data_module.os, "getxattr", lambda *_args, **_kwargs: exact)
    with pytest.raises(DrawingMachineError, match="private client data ACL"):
        client_data_module._validate_mode_only_acl(path, 0o700)


def test_system_clock_and_test_permission_oracle_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = client_data_module.SystemStagingClock()
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "boot-a\n")
    assert clock.boot_id() == "boot-a"
    assert isinstance(clock.monotonic_ns(), int)

    def unreadable(*args: object, **kwargs: object) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(DrawingMachineError) as error:
        clock.boot_id()
    assert error.value.payload.code == "CLIENT_DATA_CLOCK_INVALID"

    oracle = client_data_module._TestPermissionOracle()
    ordinary = tmp_path / "ordinary"
    ordinary.write_bytes(b"x")
    directory = tmp_path / "directory"
    directory.mkdir()
    directory.chmod(0o755)
    for validate in (
        lambda: oracle.validate_phase_directory(ordinary, ClientDataRole.IMAGE, "drop"),
        lambda: oracle.validate_client_bundle(ordinary, ClientDataRole.IMAGE),
        lambda: oracle.validate_private_directory(directory),
        lambda: oracle.validate_client_file(ordinary, ClientDataRole.IMAGE),
        lambda: oracle.validate_export_file(ordinary),
    ):
        with pytest.raises(DrawingMachineError):
            validate()


def test_client_constructor_nonce_and_staged_authority_edges_fail_closed(tmp_path: Path) -> None:
    permissions = _Permissions()
    with pytest.raises(ValueError):
        FilesystemClientInputStaging(Path("relative"), tmp_path / "exports", permission_oracle=permissions)
    with pytest.raises(ValueError):
        FilesystemClientInputStaging(
            tmp_path / "imports",
            tmp_path / "exports",
            permission_oracle=permissions,
            published_import_root=Path("relative"),
        )

    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    invalid_nonce = FilesystemClientInputStaging(
        adapter._import_root,
        adapter._export_root,
        permission_oracle=client_data_module._TestPermissionOracle(),
        nonce_factory=lambda: "z" * 32,
    )
    with pytest.raises(DrawingMachineError) as nonce:
        invalid_nonce.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    assert nonce.value.payload.code == "CLIENT_DATA_INTERNAL"

    staged = adapter.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    changed_lease = replace(staged.lease, publisher_boot_id="boot-b")
    changed = replace(staged, lease=changed_lease)
    with pytest.raises(DrawingMachineError) as drift:
        adapter.transfer_before_write(changed)
    assert drift.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    with pytest.raises(TypeError):
        adapter.discard_definitely_unsent(staged, "zero")  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError):
        adapter.discard_definitely_unsent(changed, DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES)
    with pytest.raises(TypeError):
        adapter._validate_staged(object())  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError):
        adapter._validate_phase(tmp_path, ClientDataRole.IMAGE, "drop")
    with pytest.raises(DrawingMachineError):
        outside = tmp_path / staged.bundle_name
        adapter._validate_staged(replace(staged, bundle_path=outside, payload_path=outside / "payload"))


def test_low_level_json_quota_and_io_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        client_data_module._unique_object([("a", 1), ("a", 2)])
    with pytest.raises(ValueError, match="NaN"):
        client_data_module._reject_constant("NaN")

    directory = tmp_path / "directory"
    directory.mkdir()
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(DrawingMachineError) as oversized:
            client_data_module._write_json_at(descriptor, "large", "x" * (17 * 1024))
        assert oversized.value.payload.code == "CLIENT_DATA_INTERNAL"
    finally:
        os.close(descriptor)

    monkeypatch.setattr(client_data_module.os, "write", lambda descriptor, contents: 0)
    with pytest.raises(DrawingMachineError) as short:
        client_data_module._write_all(1, b"x")
    assert short.value.payload.code == "CLIENT_DATA_IO_FAILED"

    contents = tmp_path / "contents"
    contents.write_bytes(b"ab")
    descriptor = os.open(contents, os.O_RDONLY)
    try:
        with pytest.raises(DrawingMachineError) as overread:
            client_data_module._read_bounded(descriptor, 1)
        assert overread.value.payload.code == "CLIENT_DATA_QUOTA_EXCEEDED"
    finally:
        os.close(descriptor)

    phase = tmp_path / "phase"
    phase.mkdir()
    for index in range(32):
        (phase / f"image--12345678-1234-4234-8234-123456789abc--{index:032x}.stage").mkdir()
    with pytest.raises(DrawingMachineError) as quota:
        client_data_module._require_phase_quota(phase, ClientDataRole.IMAGE, 1)
    assert quota.value.payload.code == "CLIENT_DATA_QUOTA_EXCEEDED"
    single = tmp_path / "single-phase"
    single.mkdir()
    (single / f"image--12345678-1234-4234-8234-123456789abc--{'f' * 32}.stage").mkdir()
    client_data_module._require_phase_quota(single, ClientDataRole.IMAGE, 1)


def test_staging_cleanup_and_lock_faults_are_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    real_copy = client_data_module._copy_payload

    def rejected_copy(*args: object, **kwargs: object) -> tuple[str, int]:
        raise client_data_module._error("forced copy rejection", "CLIENT_DATA_IDENTITY_DRIFT")

    monkeypatch.setattr(client_data_module, "_copy_payload", rejected_copy)
    with pytest.raises(DrawingMachineError) as rejected:
        adapter.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    assert rejected.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    assert list((tmp_path / "imports/image/prepare").iterdir()) == []
    monkeypatch.setattr(client_data_module, "_copy_payload", real_copy)

    real_mkdir = client_data_module.os.mkdir

    def failed_mkdir(path: object, mode: int = 0o777, *args: object, **kwargs: object) -> None:
        if str(path).endswith(".prepare"):
            raise OSError("forced mkdir failure")
        real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(client_data_module.os, "mkdir", failed_mkdir)
    with pytest.raises(DrawingMachineError) as io_error:
        adapter.stage_image(source, "22345678-1234-4234-8234-123456789abc", _Clock())
    assert io_error.value.payload.code == "CLIENT_DATA_IO_FAILED"
    monkeypatch.setattr(client_data_module.os, "mkdir", real_mkdir)

    staged = adapter.stage_image(source, "32345678-1234-4234-8234-123456789abc", _Clock())
    real_flock = client_data_module.fcntl.flock
    monkeypatch.setattr(
        client_data_module.fcntl,
        "flock",
        lambda descriptor, operation: (_ for _ in ()).throw(BlockingIOError("locked")),
    )
    with pytest.raises(DrawingMachineError) as locked:
        adapter.transfer_before_write(staged)
    assert locked.value.payload.code == "CLIENT_DATA_LOCKED"
    monkeypatch.setattr(client_data_module.fcntl, "flock", real_flock)


def test_low_level_identity_lease_and_phase_drift_guards(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"ab")
    descriptor = os.open(source, os.O_RDONLY)
    expected = os.fstat(descriptor)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"cd")
    replacement.replace(source)
    try:
        with pytest.raises(DrawingMachineError) as drift:
            client_data_module._verify_source(descriptor, expected, source)
        assert drift.value.payload.code == "CLIENT_INPUT_CHANGED"
    finally:
        os.close(descriptor)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "lease.json").write_text('{"bad":NaN}', encoding="utf-8")
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(DrawingMachineError) as invalid:
            client_data_module._read_lease_at(bundle_fd)
        assert invalid.value.payload.code == "CLIENT_DATA_LEASE_INVALID"
    finally:
        os.close(bundle_fd)
    (bundle / "lease.json").write_bytes(b"x" * (16 * 1024 + 1))
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(DrawingMachineError) as oversized:
            client_data_module._read_lease_at(bundle_fd)
        assert oversized.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    finally:
        os.close(bundle_fd)

    phase = tmp_path / "phase"
    phase.mkdir()
    unauthorized = phase / f"gcode--12345678-1234-4234-8234-123456789abc--{'a' * 32}.stage"
    unauthorized.mkdir()
    with pytest.raises(DrawingMachineError):
        client_data_module._require_phase_quota(phase, ClientDataRole.IMAGE, 1)
    unauthorized.rmdir()
    authorized = phase / f"image--12345678-1234-4234-8234-123456789abc--{'a' * 32}.stage"
    authorized.mkdir()
    (authorized / "payload").mkdir()
    with pytest.raises(DrawingMachineError):
        client_data_module._require_phase_quota(phase, ClientDataRole.IMAGE, 1)
    (authorized / "payload").rmdir()
    (authorized / "payload").write_bytes(b"abc")
    with pytest.raises(DrawingMachineError):
        client_data_module._require_phase_quota(phase, ClientDataRole.IMAGE, 0)


def test_remove_owned_prepare_covers_absent_foreign_and_owned_paths(tmp_path: Path) -> None:
    client_data_module._remove_owned_prepare(tmp_path / "absent", -1)
    client_data_module._remove_owned_prepare(tmp_path / "absent", 0)

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    descriptor = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
    try:
        client_data_module._remove_owned_prepare(foreign, descriptor)
    finally:
        os.close(descriptor)
    assert foreign.exists()

    owned = tmp_path / "owned"
    owned.mkdir()
    descriptor = os.open(owned, os.O_RDONLY | os.O_DIRECTORY)
    client_data_module._remove_owned_prepare(owned, descriptor)
    os.close(descriptor)
    assert not owned.exists()


def test_descriptor_relative_directory_helpers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(client_data_module.SecureFilesystemError, match="must be absolute"):
        client_data_module._open_verified_directory(Path("relative"))
    with pytest.raises(client_data_module.SecureFilesystemError, match="cannot be opened"):
        client_data_module._open_verified_directory(tmp_path / "absent")

    managed = tmp_path / "managed"
    managed.mkdir()
    real_fstat = client_data_module.os.fstat

    def changed_identity(descriptor: int):
        value = real_fstat(descriptor)
        values = list(value)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(client_data_module.os, "fstat", changed_identity)
    with pytest.raises(client_data_module.SecureFilesystemError, match="identity"):
        client_data_module._open_verified_directory(managed)
    monkeypatch.setattr(client_data_module.os, "fstat", real_fstat)

    role = tmp_path / "role"
    role.mkdir()
    role_fd = os.open(role, os.O_RDONLY | os.O_DIRECTORY)
    try:
        private = role / "private"
        with pytest.raises(FileNotFoundError):
            client_data_module._open_private_directory(
                role_fd,
                private,
                client_data_module._TestPermissionOracle(),
            )
        assert not private.exists()
        private.mkdir(mode=0o700)
        private.chmod(0o700)
        descriptor = client_data_module._open_private_directory(
            role_fd,
            private,
            client_data_module._TestPermissionOracle(),
        )
        os.close(descriptor)
        descriptor = client_data_module._open_private_directory(
            role_fd,
            private,
            client_data_module._TestPermissionOracle(),
        )
        os.close(descriptor)
        private.chmod(0o755)
        with pytest.raises(DrawingMachineError, match="unsafe"):
            client_data_module._open_private_directory(
                role_fd,
                private,
                client_data_module._TestPermissionOracle(),
            )
        private.chmod(0o700)
        monkeypatch.setattr(client_data_module.os, "fstat", changed_identity)
        with pytest.raises(client_data_module.SecureFilesystemError, match=r"identity|replaced"):
            client_data_module._open_private_directory(
                role_fd,
                private,
                client_data_module._TestPermissionOracle(),
            )
        monkeypatch.setattr(client_data_module.os, "fstat", real_fstat)

        with pytest.raises(client_data_module.SecureFilesystemError, match="unsafe"):
            client_data_module._ensure_export_directory(role_fd, "../unsafe")
        unsafe = role / "unsafe-mode"
        unsafe.mkdir(mode=0o700)
        unsafe.chmod(0o700)
        with pytest.raises(client_data_module.SecureFilesystemError, match="mode"):
            client_data_module._ensure_export_directory(role_fd, unsafe.name)
        static_jobs = role / "jobs"
        with pytest.raises(FileNotFoundError):
            client_data_module._open_static_export_directory(
                role_fd,
                static_jobs,
                client_data_module._TestPermissionOracle(),
            )
        assert not static_jobs.exists()
        external = tmp_path / "external-jobs"
        external.mkdir(mode=0o755)
        static_jobs.symlink_to(external, target_is_directory=True)
        with pytest.raises(DrawingMachineError, match="unsafe"):
            client_data_module._open_static_export_directory(
                role_fd,
                static_jobs,
                client_data_module._TestPermissionOracle(),
            )
        assert list(external.iterdir()) == []
        static_jobs.unlink()
        static_jobs.mkdir(mode=0o2750)
        static_jobs.chmod(0o2750)
        monkeypatch.setattr(client_data_module.os, "fstat", changed_identity)
        with pytest.raises(client_data_module.SecureFilesystemError, match=r"identity|replaced"):
            client_data_module._open_static_export_directory(
                role_fd,
                static_jobs,
                client_data_module._TestPermissionOracle(),
            )
        monkeypatch.setattr(client_data_module.os, "fstat", real_fstat)
        real_fsync = client_data_module.fsync_directory
        monkeypatch.setattr(
            client_data_module,
            "fsync_directory",
            lambda _descriptor: (_ for _ in ()).throw(OSError("forced export fsync failure")),
        )
        with pytest.raises(OSError, match="export fsync"):
            client_data_module._ensure_export_directory(role_fd, "export-failure")
        monkeypatch.setattr(client_data_module, "fsync_directory", real_fsync)

        authority = role / "authority"
        authority.mkdir()
        with pytest.raises(client_data_module.SecureFilesystemError, match="identity"):
            client_data_module._detach_and_remove_tree(role_fd, authority.name, (0, 0))
        assert authority.exists()
    finally:
        os.close(role_fd)


def test_directory_component_fstat_failure_never_leaks_open_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "managed/target"
    target.mkdir(parents=True)
    real_fstat = client_data_module.os.fstat

    def fail_target_fstat(descriptor: int):
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")) == target:
            raise OSError("forced component fstat")
        return real_fstat(descriptor)

    monkeypatch.setattr(client_data_module.os, "fstat", fail_target_fstat)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        with pytest.raises(client_data_module.SecureFilesystemError, match="cannot be opened"):
            client_data_module._open_verified_directory(target)
    after = len(os.listdir("/proc/self/fd"))

    assert after == before


@pytest.mark.parametrize("drift", ["before", "during", "after"])
def test_validated_static_directory_rejects_every_identity_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: str,
) -> None:
    parent = tmp_path / "parent"
    path = parent / "private"
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_lstat = client_data_module.os.lstat
    real_stat = client_data_module.os.stat
    calls = 0

    def changed(value: os.stat_result) -> os.stat_result:
        fields = list(value)
        fields[1] += 1
        return os.stat_result(fields)

    def lstat(target: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        value = real_lstat(target, *args, **kwargs)
        return changed(value) if drift == "before" and Path(target) == path else value

    def relative_stat(target: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        value = real_stat(target, *args, **kwargs)
        if target == path.name and kwargs.get("dir_fd") == parent_fd:
            calls += 1
            if (drift == "during" and calls == 3) or (drift == "after" and calls == 4):
                return changed(value)
        return value

    monkeypatch.setattr(client_data_module.os, "lstat", lstat)
    monkeypatch.setattr(client_data_module.os, "stat", relative_stat)
    before_fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(client_data_module.SecureFilesystemError, match="identity"):
            client_data_module._open_private_directory(parent_fd, path, _Permissions())
    finally:
        os.close(parent_fd)
    assert len(os.listdir("/proc/self/fd")) == before_fds - 1


def test_runtime_export_directory_identity_failure_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    path = parent / "job"
    path.mkdir(parents=True, mode=0o2750)
    path.chmod(0o2750)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = client_data_module.os.fstat
    opened: list[int] = []

    def open_existing(_parent_fd: int, name: str) -> int:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
        opened.append(descriptor)
        return descriptor

    def changed_fstat(descriptor: int) -> os.stat_result:
        value = real_fstat(descriptor)
        fields = list(value)
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(client_data_module, "_ensure_export_directory", open_existing)
    monkeypatch.setattr(client_data_module.os, "fstat", changed_fstat)
    try:
        with pytest.raises(client_data_module.SecureFilesystemError, match="identity"):
            client_data_module._ensure_runtime_export_directory(parent_fd, path, _Permissions())
    finally:
        os.close(parent_fd)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])


def test_publish_identity_open_copy_and_lease_swap_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = FilesystemClientInputStaging.for_test(tmp_path / "imports")
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    real_lstat = client_data_module.os.lstat

    def changed_publish(path: object, *args: object, **kwargs: object):
        value = real_lstat(path, *args, **kwargs)
        if str(path).endswith(".stage"):
            values = list(value)
            values[1] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(client_data_module.os, "lstat", changed_publish)
    with pytest.raises(DrawingMachineError) as changed:
        adapter.stage_image(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    assert changed.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    monkeypatch.setattr(client_data_module.os, "lstat", real_lstat)

    real_open = client_data_module.os.open

    def denied_open(path: object, *args: object, **kwargs: object) -> int:
        if path == source.name:
            raise OSError("forced no-follow open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(client_data_module.os, "open", denied_open)
    with pytest.raises(DrawingMachineError) as denied:
        client_data_module._open_canonical_regular(source)
    assert denied.value.payload.code == "CLIENT_INPUT_INVALID"
    monkeypatch.setattr(client_data_module.os, "open", real_open)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    lease = adapter.stage_image(source, "22345678-1234-4234-8234-123456789abc", _Clock())
    lease_path = lease.bundle_path / "lease.json"
    bundle_fd = os.open(lease.bundle_path, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = client_data_module.os.fstat
    calls = 0

    def changed_lease(descriptor: int):
        nonlocal calls
        value = real_fstat(descriptor)
        if descriptor != bundle_fd:
            calls += 1
            if calls == 2:
                values = list(value)
                values[8] += 1
                return os.stat_result(values)
        return value

    monkeypatch.setattr(client_data_module.os, "fstat", changed_lease)
    try:
        with pytest.raises(DrawingMachineError) as lease_drift:
            client_data_module._read_lease_at(bundle_fd)
        assert lease_drift.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    finally:
        os.close(bundle_fd)
    assert lease_path.exists()


def test_copy_payload_growth_short_source_and_output_identity_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"ab")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source, os.O_RDONLY)
    source_status = os.fstat(source_fd)
    real_read = client_data_module.os.read
    reads = iter((b"ab", b"c", b""))
    monkeypatch.setattr(client_data_module.os, "read", lambda descriptor, count: next(reads))
    try:
        with pytest.raises(DrawingMachineError) as grown:
            client_data_module._copy_payload(source_fd, source_status, bundle_fd, 2)
        assert grown.value.payload.code == "CLIENT_INPUT_INVALID"
    finally:
        os.close(source_fd)
        os.close(bundle_fd)
    monkeypatch.setattr(client_data_module.os, "read", real_read)

    (bundle / "payload").unlink()
    source_fd = os.open(source, os.O_RDONLY)
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
    short_values = list(source_status)
    short_values[6] = 3
    short_status = os.stat_result(short_values)
    try:
        with pytest.raises(DrawingMachineError) as short:
            client_data_module._copy_payload(source_fd, short_status, bundle_fd, 3)
        assert short.value.payload.code == "CLIENT_INPUT_CHANGED"
    finally:
        os.close(source_fd)
        os.close(bundle_fd)

    (bundle / "payload").unlink()
    source_fd = os.open(source, os.O_RDONLY)
    bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = client_data_module.os.fstat

    def unsafe_output(descriptor: int):
        value = real_fstat(descriptor)
        if descriptor not in {source_fd, bundle_fd}:
            values = list(value)
            values[3] = 2
            return os.stat_result(values)
        return value

    monkeypatch.setattr(client_data_module.os, "fstat", unsafe_output)
    try:
        with pytest.raises(DrawingMachineError) as unsafe:
            client_data_module._copy_payload(source_fd, source_status, bundle_fd, 3)
        assert unsafe.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    finally:
        os.close(source_fd)
        os.close(bundle_fd)
