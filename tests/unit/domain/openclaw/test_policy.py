from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import drawingmachine.config.openclaw_deployment as subject
from drawingmachine.config.openclaw_deployment import (
    PosixGroupV1,
    PosixPrincipalV1,
    ServiceWriterSnapshotV1,
    load_openclaw_deployment_policy,
    parse_openclaw_deployment_policy,
)


def _policy(tmp_path: Path) -> bytes:
    return f'''schema_version = 1
registry_root = "{tmp_path / "registry"}"
managed_root = "{tmp_path / "managed"}"
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{"a" * 64}"
registry_policy_sha256 = "{"b" * 64}"
[operator_principal]
uid = {os.getuid() + 10001}
gid = {os.getgid() + 10001}
[automation_principal]
uid = {os.getuid() + 10002}
gid = {os.getgid() + 10002}
[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}
[managed_owner]
uid = {os.getuid()}
gid = {os.getgid()}
[registry_owner]
uid = {os.getuid()}
gid = {os.getgid()}
'''.encode()


def _parse(contents: bytes) -> object:
    return parse_openclaw_deployment_policy(contents, source_sha256=hashlib.sha256(contents).hexdigest())


def test_policy_parser_and_secure_loader_freeze_exact_bytes(tmp_path: Path) -> None:
    contents = _policy(tmp_path)
    parsed = _parse(contents)
    path = tmp_path / "service-policy.toml"
    path.write_bytes(contents)
    os.chmod(path, 0o600)

    loaded = load_openclaw_deployment_policy(
        path,
        service_writer_snapshot=ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert loaded == parsed
    assert loaded.managed_dir_mode == 0o2750
    assert loaded.target_mode == 0o640


@pytest.mark.parametrize(
    ("transform", "match"),
    [
        (lambda value: b"", "non-empty"),
        (lambda value: value + b"\nunknown = 1\n", "exactly"),
        (lambda value: value.replace(b"schema_version = 1", b"schema_version = 2"), "schema_version"),
        (lambda value: value.replace(b"managed_dir_mode = 1512", b"managed_dir_mode = 448"), "2750"),
        (lambda value: value.replace(b"target_mode = 416", b"target_mode = 384"), "0640"),
        (lambda value: value.replace(b'manifest_sha256 = "' + b"a" * 64, b'manifest_sha256 = "bad'), "SHA-256"),
        (
            lambda value: value.replace(
                f"uid = {os.getuid() + 10001}".encode(),
                f'uid = "{os.getuid() + 10001}"'.encode(),
                1,
            ),
            "integer",
        ),
        (lambda value: value.replace(b"drawingmachine-openclaw-read", b"Bad Group"), "group name"),
        (lambda value: value.replace(b"[managed_owner]\nuid", b"[managed_owner]\nextra = 1\nuid"), "exactly"),
    ],
)
def test_policy_parser_rejects_closed_schema_drift(
    tmp_path: Path,
    transform: object,
    match: str,
) -> None:
    contents = transform(_policy(tmp_path))  # type: ignore[operator]
    with pytest.raises((TypeError, ValueError), match=match):
        _parse(contents)


def test_policy_parser_rejects_digest_encoding_size_principal_and_path_drift(tmp_path: Path) -> None:
    contents = _policy(tmp_path)
    with pytest.raises(ValueError, match="source digest"):
        parse_openclaw_deployment_policy(contents, source_sha256="0" * 64)
    with pytest.raises(ValueError, match="UTF-8"):
        _parse(b"\xff")
    with pytest.raises(ValueError, match="65536"):
        _parse(b"x" * 65_537)
    with pytest.raises(ValueError, match="disjoint"):
        _parse(contents.replace(f"uid = {os.getuid() + 10002}".encode(), f"uid = {os.getuid() + 10001}".encode()))
    with pytest.raises(ValueError, match="non-nested"):
        _parse(contents.replace(str(tmp_path / "managed").encode(), str(tmp_path / "registry" / "managed").encode()))
    with pytest.raises(ValueError, match="absolute"):
        _parse(contents.replace(str(tmp_path / "registry").encode(), b"relative"))


def test_policy_value_objects_reject_hostile_exact_types() -> None:
    with pytest.raises(TypeError, match="POSIX ID"):
        PosixPrincipalV1(True, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="group name"):
        PosixGroupV1("Bad Group", 1)
    with pytest.raises(TypeError, match="POSIX ID"):
        ServiceWriterSnapshotV1(-1, 1)


def test_secure_policy_loader_rejects_alias_mode_link_and_wrong_writer(tmp_path: Path) -> None:
    contents = _policy(tmp_path)
    path = tmp_path / "policy.toml"
    path.write_bytes(contents)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    with pytest.raises(ValueError, match="absolute"):
        load_openclaw_deployment_policy(Path("relative"), service_writer_snapshot=writer)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="no-follow"):
        load_openclaw_deployment_policy(alias, service_writer_snapshot=writer)
    os.chmod(path, 0o620)
    with pytest.raises(ValueError, match="unsafe"):
        load_openclaw_deployment_policy(path, service_writer_snapshot=writer)
    os.chmod(path, 0o600)
    hardlink = tmp_path / "hardlink.toml"
    os.link(path, hardlink)
    with pytest.raises(ValueError, match="unsafe"):
        load_openclaw_deployment_policy(path, service_writer_snapshot=writer)


def test_policy_private_exact_guards_and_loader_drift_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        load_openclaw_deployment_policy(tmp_path / "x", service_writer_snapshot=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="table"):
        subject._table(1, frozenset(), "x")
    with pytest.raises(ValueError, match="non-empty"):
        subject._string("", "x")
    with pytest.raises(ValueError, match="control-free"):
        subject._absolute_path("/tmp/bad\n", "x")

    contents = _policy(tmp_path).replace(
        f"uid = {os.getuid()}\ngid = {os.getgid()}".encode(), b"uid = 99999\ngid = 99998"
    )
    path = tmp_path / "wrong-owner-policy.toml"
    path.write_bytes(contents)
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="managed owner"):
        load_openclaw_deployment_policy(
            path,
            service_writer_snapshot=ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )

    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"x" * 65_537)
    os.chmod(oversized, 0o600)
    real_fstat = subject.os.fstat

    def hidden_size(descriptor: int) -> os.stat_result:
        values = list(real_fstat(descriptor))[:10]
        values[6] = 0
        return os.stat_result(values)

    monkeypatch.setattr(subject.os, "fstat", hidden_size)
    with pytest.raises(ValueError, match="exceeds"):
        load_openclaw_deployment_policy(
            oversized,
            service_writer_snapshot=ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )


def test_secure_policy_loader_rejects_path_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "policy.toml"
    path.write_bytes(_policy(tmp_path))
    os.chmod(path, 0o600)
    real = Path.lstat
    calls = 0

    def drifting(candidate: Path) -> os.stat_result:
        nonlocal calls
        current = real(candidate)
        if candidate != path:
            return current
        calls += 1
        if calls == 1:
            return current
        values = list(current)[:10]
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", drifting)
    with pytest.raises(ValueError, match="changed"):
        load_openclaw_deployment_policy(
            path,
            service_writer_snapshot=ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )


@pytest.mark.parametrize("hostile", ["//tmp/registry", "/tmp/r\N{LATIN SMALL LETTER E WITH ACUTE}gistry"])
def test_i4_policy_rejects_noncanonical_or_unicode_roots(tmp_path: Path, hostile: str) -> None:
    contents = _policy(tmp_path).replace(str(tmp_path / "registry").encode(), hostile.encode())
    with pytest.raises(ValueError, match=r"canonical|ASCII"):
        _parse(contents)


def _policy_without_registry_owner(tmp_path: Path) -> bytes:
    contents = _policy(tmp_path)
    marker = b"[registry_owner]\n"
    return contents[: contents.index(marker)]


def test_registry_owner_field_parses_and_is_exposed(tmp_path: Path) -> None:
    admin_uid = os.getuid() + 20001
    admin_gid = os.getgid() + 20001
    contents = (
        _policy_without_registry_owner(tmp_path)
        + f"""[registry_owner]
uid = {admin_uid}
gid = {admin_gid}
""".encode()
    )

    parsed = _parse(contents)

    assert parsed.registry_owner == PosixPrincipalV1(admin_uid, admin_gid)  # type: ignore[attr-defined]


def test_registry_owner_must_be_disjoint_from_operator_and_automation_but_may_equal_managed_owner(
    tmp_path: Path,
) -> None:
    base = _policy_without_registry_owner(tmp_path)
    operator_uid = os.getuid() + 10001
    automation_gid = os.getgid() + 10002

    colliding_uid = (
        base
        + f"""[registry_owner]
uid = {operator_uid}
gid = {os.getgid() + 30001}
""".encode()
    )
    with pytest.raises(ValueError, match="disjoint"):
        _parse(colliding_uid)

    colliding_gid = (
        base
        + f"""[registry_owner]
uid = {os.getuid() + 30002}
gid = {automation_gid}
""".encode()
    )
    with pytest.raises(ValueError, match="disjoint"):
        _parse(colliding_gid)

    degenerate = (
        base
        + f"""[registry_owner]
uid = {os.getuid()}
gid = {os.getgid()}
""".encode()
    )
    parsed = _parse(degenerate)
    assert parsed.registry_owner == parsed.managed_owner  # type: ignore[attr-defined]
