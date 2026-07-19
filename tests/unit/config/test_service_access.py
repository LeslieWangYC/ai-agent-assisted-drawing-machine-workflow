from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import drawingmachine.config.service_access as service_access_module
from drawingmachine.config.machine import MachineExecutionConfig, MachinePrincipalConfig
from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.config.service_access import (
    MAX_SERVICE_ACCESS_BYTES,
    ServiceAccessConfigV1,
    load_service_access_config,
    parse_service_access_config,
)
from drawingmachine.errors import DrawingMachineError


def _policy(root: Path, *, service_uid: int = 1000, service_gid: int = 1000, connect_gid: int = 4) -> bytes:
    data = root / "data"
    return f'''\
schema_version = 1
canonical_runtime_dir = "{root}/service-runtime/drawingmachine"
canonical_socket = "{root}/service-runtime/drawingmachine/service.sock"
automation_runtime_endpoint = "{root}/automation-runtime/drawingmachine/service.sock"
operator_runtime_endpoint = "{root}/operator-runtime/drawingmachine/service.sock"
canonical_data_dir = "{data}"
automation_import_root = "{data}/imports/automation"
automation_export_root = "{data}/exports/automation"
automation_import_endpoint = "{root}/automation-data/imports"
automation_export_endpoint = "{root}/automation-data/exports"
openclaw_policy_path = "{root}/config/openclaw-deployment.toml"

[service_principal]
uid = {service_uid}
gid = {service_gid}
[automation_principal]
uid = 1100
gid = 1100
[operator_principal]
uid = 2200
gid = 2200
[connect_group]
name = "drawingmachine-connect"
gid = {connect_gid}
'''.encode()


def _machine() -> MachineExecutionConfig:
    machine = object.__new__(MachineExecutionConfig)
    object.__setattr__(machine, "automation_principal", MachinePrincipalConfig(1100, 1100))
    object.__setattr__(machine, "operator_principal", MachinePrincipalConfig(2200, 2200))
    return machine


def test_service_access_config_is_a_separate_closed_contract(tmp_path: Path) -> None:
    config = parse_service_access_config(_policy(tmp_path))

    assert type(config) is ServiceAccessConfigV1
    assert config.schema_version == 1
    assert config.canonical_socket == config.canonical_runtime_dir / "service.sock"
    assert config.automation_import_root.is_relative_to(config.canonical_data_dir)
    assert config.automation_export_root.is_relative_to(config.canonical_data_dir)
    with pytest.raises(AttributeError):
        config.schema_version = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace(b"schema_version = 1", b"schema_version = 2"),
        lambda value: value.replace(b"schema_version = 1", b"schema_version = true"),
        lambda value: value.replace(b"schema_version = 1\n", b""),
        lambda value: value.replace(b"schema_version = 1", b"schema_version = 1\nunknown = 1"),
        lambda value: value + b"\nschema_version = 1\n",
        lambda value: value.replace(b"uid = 2200", b"uid = 1100"),
        lambda value: value.replace(b"gid = 2200", b"gid = 1100"),
        lambda value: value.replace(b"gid = 4", b"gid = 1000"),
        lambda value: value.replace(b'/service.sock"\nautomation_runtime', b'/wrong.sock"\nautomation_runtime', 1),
        lambda value: value.replace(b"automation-runtime", b"operator-runtime"),
        lambda value: value.replace(b"automation-runtime", b"service-runtime"),
        lambda value: value.replace(b"/drawingmachine/service.sock", b"/other/service.sock", 1),
        lambda value: value.replace(b"/imports/automation", b"/../outside"),
        lambda value: value.replace(b"/exports/automation", b"/imports/automation"),
    ],
)
def test_parser_rejects_hostile_or_non_closed_policy(tmp_path: Path, mutate: Callable[[bytes], bytes]) -> None:
    with pytest.raises(DrawingMachineError) as error:
        parse_service_access_config(mutate(_policy(tmp_path)))
    assert error.value.payload.code == "SERVICE_ACCESS_INVALID"


@pytest.mark.parametrize("contents", [b"", b"\xff", b"x" * (MAX_SERVICE_ACCESS_BYTES + 1)])
def test_parser_rejects_empty_invalid_utf8_or_oversize(contents: bytes) -> None:
    with pytest.raises(DrawingMachineError):
        parse_service_access_config(contents)


def test_no_follow_loader_freezes_exact_bytes_digest_and_machine_principals(tmp_path: Path) -> None:
    contents = _policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid())
    path = tmp_path / "service-access.toml"
    path.write_bytes(contents)
    os.chown(path, os.getuid(), 4)
    path.chmod(0o640)

    loaded = load_service_access_config(
        path,
        service_principal=PosixPrincipalV1(os.getuid(), os.getgid()),
        machine_config=_machine(),
        machine_sha256="a" * 64,
    )

    assert loaded.source_sha256 == hashlib.sha256(contents).hexdigest()
    assert loaded.source_size_bytes == len(contents)
    assert loaded.source_identity == (path.stat().st_dev, path.stat().st_ino)
    assert loaded.config.automation_principal == PosixPrincipalV1(1100, 1100)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o660])
def test_loader_rejects_non_exact_policy_mode(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "service-access.toml"
    path.write_bytes(_policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid()))
    os.chown(path, os.getuid(), 4)
    path.chmod(mode)
    with pytest.raises(DrawingMachineError):
        load_service_access_config(
            path,
            service_principal=PosixPrincipalV1(os.getuid(), os.getgid()),
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )


def test_loader_rejects_symlink_and_machine_principal_drift(tmp_path: Path) -> None:
    target = tmp_path / "target.toml"
    target.write_bytes(_policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid()))
    target.chmod(0o640)
    link = tmp_path / "service-access.toml"
    link.symlink_to(target)
    with pytest.raises(DrawingMachineError):
        load_service_access_config(
            link,
            service_principal=None,
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )
    drifted = _machine()
    object.__setattr__(drifted, "operator_principal", MachinePrincipalConfig(3300, 3300))
    os.chown(target, os.getuid(), 4)
    with pytest.raises(DrawingMachineError, match="machine profile"):
        load_service_access_config(
            target,
            service_principal=PosixPrincipalV1(os.getuid(), os.getgid()),
            machine_config=drifted,
            machine_sha256="a" * 64,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": Path("relative")},
        {"service_principal": object()},
        {"machine_config": object()},
        {"machine_sha256": "bad"},
    ],
)
def test_loader_rejects_non_exact_inputs(tmp_path: Path, kwargs: dict[str, object]) -> None:
    path = tmp_path / "service-access.toml"
    path.write_bytes(_policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid()))
    os.chown(path, os.getuid(), 4)
    path.chmod(0o640)
    arguments: dict[str, object] = {
        "path": path,
        "service_principal": PosixPrincipalV1(os.getuid(), os.getgid()),
        "machine_config": _machine(),
        "machine_sha256": "a" * 64,
    }
    arguments.update(kwargs)
    with pytest.raises(DrawingMachineError):
        load_service_access_config(**arguments)  # type: ignore[arg-type]


def test_loader_rejects_source_owner_group_and_running_principal_drift(tmp_path: Path) -> None:
    path = tmp_path / "service-access.toml"
    path.write_bytes(_policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid()))
    path.chmod(0o640)
    os.chown(path, os.getuid(), 4)

    with pytest.raises(DrawingMachineError, match="running service"):
        load_service_access_config(
            path,
            service_principal=PosixPrincipalV1(3300, 3300),
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )
    os.chown(path, os.getuid(), os.getgid())
    with pytest.raises(DrawingMachineError, match="connect group"):
        load_service_access_config(
            path,
            service_principal=None,
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )


def test_loader_rejects_configured_owner_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "service-access.toml"
    path.write_bytes(_policy(tmp_path, service_uid=3300, service_gid=3300))
    os.chown(path, os.getuid(), 4)
    path.chmod(0o640)
    with pytest.raises(DrawingMachineError, match="configured service UID"):
        load_service_access_config(
            path,
            service_principal=None,
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )


def test_loader_rejects_growth_and_stat_drift_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "service-access.toml"
    path.write_bytes(_policy(tmp_path, service_uid=os.getuid(), service_gid=os.getgid()))
    os.chown(path, os.getuid(), 4)
    path.chmod(0o640)
    real_read = os.read
    monkeypatch.setattr(service_access_module.os, "read", lambda _fd, _size: b"x" * 65_537)
    with pytest.raises(DrawingMachineError, match="exceeds"):
        load_service_access_config(
            path,
            service_principal=None,
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )
    monkeypatch.setattr(service_access_module.os, "read", real_read)

    real_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        value = real_fstat(descriptor)
        if calls == 2:
            values = list(value)
            values[8] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(service_access_module.os, "fstat", changed_fstat)
    with pytest.raises(DrawingMachineError, match="changed while reading"):
        load_service_access_config(
            path,
            service_principal=None,
            machine_config=_machine(),
            machine_sha256="a" * 64,
        )


def test_parser_rejects_scalar_principal_table(tmp_path: Path) -> None:
    contents = _policy(tmp_path).decode()
    start = contents.index("[service_principal]")
    end = contents.index("[automation_principal]")
    hostile = (contents[:start] + "service_principal = 1\n" + contents[end:]).encode()
    with pytest.raises(DrawingMachineError, match="exact table"):
        parse_service_access_config(hostile)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace(b'canonical_data_dir = "', b'canonical_data_dir = "relative/'),
        lambda value: value.replace(b'canonical_data_dir = "', b'canonical_data_dir = "//'),
        lambda value: value.replace(b"/data/imports/automation", b"/outside-imports"),
        lambda value: value.replace(b"/data/exports/automation", b"/outside-exports"),
        lambda value: value.replace(b"/automation-data/exports", b"/automation-data/imports"),
        lambda value: value.replace(b"operator-runtime/drawingmachine", b"operator-runtime/not-drawingmachine"),
        lambda value: value.replace(b"uid = 1000", b"uid = true", 1),
        lambda value: value.replace(b'name = "drawingmachine-connect"', b'name = ""'),
        lambda value: value.replace(b'name = "drawingmachine-connect"', b'name = "INVALID GROUP"'),
        lambda value: value.replace(b"uid = 1000", b"uid = -1", 1),
    ],
)
def test_parser_rejects_additional_path_and_scalar_hostility(
    tmp_path: Path,
    mutate: Callable[[bytes], bytes],
) -> None:
    with pytest.raises(DrawingMachineError):
        parse_service_access_config(mutate(_policy(tmp_path)))
