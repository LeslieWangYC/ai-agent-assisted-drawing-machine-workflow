from __future__ import annotations

import asyncio
import os
import socket
import struct
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import drawingmachine.cli.service_access as client_access_module
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.cli.client import ServiceClient
from drawingmachine.cli.service_access import (
    ClientAccessSnapshotV1,
    LinuxClientAccessInspector,
    ValidatedClientEndpointV1,
    load_client_access_snapshot,
    revalidate_client_endpoint,
    select_client_endpoint,
    select_client_endpoint_from,
    validate_client_endpoint,
)
from drawingmachine.config import XdgPaths
from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.config.service_access import LoadedServiceAccessV1, parse_service_access_config
from drawingmachine.errors import DrawingMachineError
from drawingmachine.protocol import ProtocolResponse, encode_response
from drawingmachine.service.access_policy import (
    PathAccessFactsV1,
    PosixAclEntryV1,
)
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.models import ServiceStatus
from drawingmachine.service.server import UnixServiceServer


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
    def __init__(self, facts: dict[Path, PathAccessFactsV1], current: PosixPrincipalV1) -> None:
        self.facts = facts
        self.current = current

    def current_principal(self) -> PosixPrincipalV1:
        return self.current

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]:
        return frozenset({principal.gid, 4})

    def inspect(self, path: Path) -> PathAccessFactsV1:
        return self.facts[path]


def _snapshot(tmp_path: Path) -> tuple[ClientAccessSnapshotV1, FakeInspector, Path, PosixPrincipalV1]:
    service = PosixPrincipalV1(3300, 3300)
    automation = PosixPrincipalV1(os.getuid(), os.getgid())
    endpoint = tmp_path / "automation/drawingmachine/service.sock"
    canonical = tmp_path / "service/drawingmachine/service.sock"
    config = parse_service_access_config(
        f'''\
schema_version = 1
canonical_runtime_dir = "{canonical.parent}"
canonical_socket = "{canonical}"
automation_runtime_endpoint = "{endpoint}"
operator_runtime_endpoint = "{tmp_path}/operator/drawingmachine/service.sock"
canonical_data_dir = "{tmp_path}/data"
automation_import_root = "{tmp_path}/data/imports/automation"
automation_export_root = "{tmp_path}/data/exports/automation"
automation_import_endpoint = "{tmp_path}/automation-data/imports"
automation_export_endpoint = "{tmp_path}/automation-data/exports"
openclaw_policy_path = "{tmp_path}/config/openclaw.toml"
[service_principal]
uid = {service.uid}
gid = {service.gid}
[automation_principal]
uid = {automation.uid}
gid = {automation.gid}
[operator_principal]
uid = 2200
gid = 2200
[connect_group]
name = "drawingmachine-connect"
gid = 4
'''.encode()
    )
    endpoint.parent.mkdir(parents=True, mode=0o700)
    canonical.parent.mkdir(parents=True)
    endpoint.symlink_to(canonical)
    source = tmp_path / "service-access.toml"
    source.write_text("policy")
    facts = {
        endpoint.parent: _facts(endpoint.parent, "directory", automation.uid, automation.gid, 0o700, 10),
        endpoint: _facts(endpoint, "symlink", automation.uid, automation.gid, 0o777, 11),
        canonical: _facts(canonical, "socket", service.uid, 4, 0o660, 12),
    }
    inspector = FakeInspector(facts, automation)
    snapshot = ClientAccessSnapshotV1(
        config,
        source,
        6,
        "a" * 64,
        (1, 14),
        "b" * 64,
        endpoint,
        automation,
    )
    return snapshot, inspector, endpoint, automation


def test_client_accepts_one_private_owned_link_to_exact_canonical_socket(tmp_path: Path) -> None:
    snapshot, inspector, endpoint, principal = _snapshot(tmp_path)
    validated = validate_client_endpoint(snapshot, endpoint, principal, inspector=inspector)

    assert validated.endpoint_identity == (1, 11)
    assert validated.canonical_identity == (1, 12)
    assert validated.canonical_socket == snapshot.config.canonical_socket
    assert select_client_endpoint(snapshot) == (endpoint, principal)
    revalidate_client_endpoint(validated, snapshot, principal, inspector=inspector)


def test_linux_client_inspector_reads_groups_identity_and_closed_file_types(tmp_path: Path) -> None:
    inspector = LinuxClientAccessInspector()
    principal = PosixPrincipalV1(os.getuid(), os.getgid())
    assert inspector.current_principal() == principal
    assert principal.gid in inspector.supplemental_gids(principal)

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
        facts = inspector.inspect(directory)
        assert facts.kind == "directory"
        assert facts.identity == (directory.stat().st_dev, directory.stat().st_ino)
        assert inspector.inspect(regular).kind == "regular"
        assert inspector.inspect(link).kind == "symlink"
        assert inspector.inspect(fifo).kind == "special"
        assert inspector.inspect(endpoint).kind == "socket"
        with pytest.raises(DrawingMachineError, match="cannot be inspected"):
            inspector.inspect(tmp_path / "missing")
        with pytest.raises(DrawingMachineError, match="group membership"):
            inspector.supplemental_gids(PosixPrincipalV1(2**32 - 2, 2**32 - 2))
    finally:
        listener.close()


def test_client_role_selection_is_closed_for_operator_unknown_and_non_exact_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _inspector, _endpoint, _principal = _snapshot(tmp_path)
    monkeypatch.setattr(client_access_module.os, "getuid", lambda: snapshot.config.operator_principal.uid)
    monkeypatch.setattr(client_access_module.os, "getgid", lambda: snapshot.config.operator_principal.gid)
    assert select_client_endpoint_from(snapshot.config) == (
        snapshot.config.operator_runtime_endpoint,
        snapshot.config.operator_principal,
    )
    monkeypatch.setattr(client_access_module.os, "getuid", lambda: 9999)
    monkeypatch.setattr(client_access_module.os, "getgid", lambda: 9999)
    with pytest.raises(DrawingMachineError, match="not an authorized"):
        select_client_endpoint_from(snapshot.config)
    with pytest.raises(DrawingMachineError, match="must be exact"):
        select_client_endpoint_from(object())  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="principal must be exact"):
        select_client_endpoint_from(snapshot.config, object())  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="snapshot must be exact"):
        select_client_endpoint(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["automation", "operator"])
def test_client_snapshot_loads_exact_policy_and_rejects_path_group_or_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    snapshot, inspector, _endpoint, _principal = _snapshot(tmp_path)
    config = snapshot.config
    principal = config.automation_principal if role == "automation" else config.operator_principal
    endpoint = config.automation_runtime_endpoint if role == "automation" else config.operator_runtime_endpoint
    inspector.current = principal
    loaded = LoadedServiceAccessV1(
        config,
        snapshot.source_path,
        snapshot.source_size_bytes,
        snapshot.source_sha256,
        snapshot.source_identity,
        0o640,
    )
    paths = XdgPaths(
        tmp_path / "config",
        tmp_path / "state",
        config.canonical_data_dir,
        endpoint.parent,
    )
    bundle = SimpleNamespace(
        machine=object(),
        machine_path=tmp_path / "machine.toml",
        digests={"machine": snapshot.machine_profile_sha256},
    )
    inspector.facts[snapshot.source_path] = _facts(
        snapshot.source_path, "regular", config.service_principal.uid, config.connect_group.gid, 0o640, 14
    )
    loaded = LoadedServiceAccessV1(
        config,
        snapshot.source_path,
        snapshot.source_size_bytes,
        snapshot.source_sha256,
        (1, 14),
        0o640,
    )
    monkeypatch.setattr(client_access_module, "load_config", lambda _paths: bundle)
    monkeypatch.setattr(client_access_module, "parse_machine_execution_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(client_access_module, "load_service_access_config", lambda *args, **kwargs: loaded)

    result = load_client_access_snapshot(paths, inspector=inspector)
    assert result.config == config
    assert result.machine_profile_sha256 == snapshot.machine_profile_sha256
    assert select_client_endpoint(result) == (endpoint, principal)

    inspector.supplemental_gids = lambda _principal: frozenset({principal.gid})  # type: ignore[method-assign]
    with pytest.raises(DrawingMachineError, match="supplemental"):
        load_client_access_snapshot(paths, inspector=inspector)
    inspector.supplemental_gids = lambda _principal: frozenset({principal.gid, 4})  # type: ignore[method-assign]
    inspector.facts[snapshot.source_path] = _facts(
        snapshot.source_path, "regular", config.service_principal.uid, config.connect_group.gid, 0o640, 99
    )
    with pytest.raises(DrawingMachineError, match="identity changed"):
        load_client_access_snapshot(paths, inspector=inspector)

    wrong_paths = XdgPaths(paths.config_dir, paths.state_dir, paths.data_dir, config.canonical_runtime_dir)
    with pytest.raises(DrawingMachineError, match="selected private"):
        load_client_access_snapshot(wrong_paths, inspector=inspector)


@pytest.mark.parametrize(
    "fault",
    [
        "unassigned",
        "parent-owner",
        "parent-mode",
        "link-type",
        "link-owner",
        "relative",
        "wrong-target",
        "chain",
        "canonical-type",
        "canonical-owner",
        "canonical-group",
        "canonical-mode",
    ],
)
def test_client_endpoint_hostile_topology_fails_closed(tmp_path: Path, fault: str) -> None:
    snapshot, inspector, endpoint, principal = _snapshot(tmp_path)
    parent = inspector.facts[endpoint.parent]
    link = inspector.facts[endpoint]
    canonical = inspector.facts[snapshot.config.canonical_socket]
    selected = endpoint
    if fault == "unassigned":
        selected = tmp_path / "elsewhere/service.sock"
    elif fault == "parent-owner":
        inspector.facts[parent.path] = _facts(parent.path, parent.kind, 9999, parent.gid, parent.mode, parent.inode)
    elif fault == "parent-mode":
        inspector.facts[parent.path] = _facts(parent.path, parent.kind, parent.uid, parent.gid, 0o750, parent.inode)
    elif fault == "link-type":
        inspector.facts[link.path] = _facts(link.path, "socket", link.uid, link.gid, link.mode, link.inode)
    elif fault == "link-owner":
        inspector.facts[link.path] = _facts(link.path, link.kind, 9999, link.gid, link.mode, link.inode)
    elif fault in {"relative", "wrong-target", "chain"}:
        endpoint.unlink()
        target = Path("relative.sock") if fault == "relative" else tmp_path / "wrong.sock"
        if fault == "chain":
            target.symlink_to(snapshot.config.canonical_socket)
        endpoint.symlink_to(target)
    elif fault == "canonical-type":
        inspector.facts[canonical.path] = _facts(
            canonical.path, "symlink", canonical.uid, canonical.gid, canonical.mode, canonical.inode
        )
    elif fault == "canonical-owner":
        inspector.facts[canonical.path] = _facts(
            canonical.path, canonical.kind, 9999, canonical.gid, canonical.mode, canonical.inode
        )
    elif fault == "canonical-group":
        inspector.facts[canonical.path] = _facts(
            canonical.path, canonical.kind, canonical.uid, 9999, canonical.mode, canonical.inode
        )
    else:
        inspector.facts[canonical.path] = _facts(
            canonical.path, canonical.kind, canonical.uid, canonical.gid, 0o666, canonical.inode
        )

    with pytest.raises(DrawingMachineError):
        validate_client_endpoint(snapshot, selected, principal, inspector=inspector)


def test_client_endpoint_rejects_bad_exact_inputs_readlink_failure_and_revalidation_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, inspector, endpoint, principal = _snapshot(tmp_path)
    with pytest.raises(DrawingMachineError, match="inputs must be exact"):
        validate_client_endpoint(object(), endpoint, principal, inspector=inspector)  # type: ignore[arg-type]
    endpoint.unlink()
    with pytest.raises(DrawingMachineError, match="cannot be read"):
        validate_client_endpoint(snapshot, endpoint, principal, inspector=inspector)
    endpoint.symlink_to(snapshot.config.canonical_socket)
    validated = validate_client_endpoint(snapshot, endpoint, principal, inspector=inspector)
    inspector.facts[endpoint] = _facts(endpoint, "symlink", principal.uid, principal.gid, 0o777, 99)
    with pytest.raises(DrawingMachineError, match="changed during connect"):
        revalidate_client_endpoint(validated, snapshot, principal, inspector=inspector)

    monkeypatch.setattr(client_access_module, "LinuxClientAccessInspector", lambda: inspector)
    assert validate_client_endpoint(snapshot, endpoint, principal).endpoint_identity == (1, 99)


def test_service_client_connects_canonical_and_rejects_validation_swap(tmp_path: Path) -> None:
    async def scenario() -> None:
        canonical = tmp_path / "canonical.sock"
        response = encode_response(
            ProtocolResponse(1, 1, True, "service.status", "request", {"status": "RUNNING"}, None)
        )

        async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\n")
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(reply, path=canonical)
        stable = ValidatedClientEndpointV1(tmp_path / "link.sock", (1, 2), canonical, (1, 3), "a" * 64)
        calls = 0

        def swapped() -> ValidatedClientEndpointV1:
            nonlocal calls
            calls += 1
            return (
                stable
                if calls == 1
                else ValidatedClientEndpointV1(
                    stable.endpoint, (1, 9), canonical, stable.canonical_identity, stable.policy_sha256
                )
            )

        try:
            client = ServiceClient(stable.endpoint, endpoint_validator=swapped)
            with pytest.raises(DrawingMachineError, match="changed during connect"):
                await asyncio.to_thread(client.call, "service.status", {}, request_id="request")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_kernel_peer_credentials_are_unchanged_through_client_symlink(tmp_path: Path) -> None:
    async def scenario() -> None:
        canonical = tmp_path / "canonical.sock"
        endpoint = tmp_path / "client.sock"
        peers: list[tuple[int, int, int]] = []

        async def capture(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            peer_socket = writer.get_extra_info("socket")
            peers.append(struct.unpack("3i", peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)))
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(capture, path=canonical)
        endpoint.symlink_to(canonical)
        try:
            reader, writer = await asyncio.open_unix_connection(endpoint)
            writer.close()
            await writer.wait_closed()
            await reader.read()
            await asyncio.sleep(0)
        finally:
            server.close()
            await server.wait_closed()
        assert peers == [(os.getpid(), os.getuid(), os.getgid())]

    asyncio.run(scenario())


class FakeStartupPolicy:
    def __init__(self, socket_path: Path) -> None:
        config = type("Config", (), {"canonical_socket": socket_path})()
        self.snapshot = type("Snapshot", (), {"config": config})()
        self.events: list[str] = []

    def prepare_runtime_directory(self) -> None:
        self.events.append("runtime")
        self.snapshot.config.canonical_socket.parent.mkdir(parents=True, exist_ok=True)

    def validate_runtime_directory(self) -> None:
        self.events.append("runtime-valid")

    def apply_and_validate_bound_socket(self, identity: tuple[int, int]) -> None:
        assert identity[0] >= 0
        self.events.append("socket-applied")

    def validate_bound_socket(self, identity: tuple[int, int]) -> None:
        assert identity[0] >= 0
        self.events.append("socket-valid")


def test_server_ready_follows_post_bind_revalidation_and_never_waits_for_client_links(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "canonical/drawingmachine/service.sock"
        policy = FakeStartupPolicy(socket_path)
        dispatcher = CommandDispatcher(lambda: ServiceStatus("1", 1, 3, "epoch", datetime.now(UTC), os.getpid()))
        server = UnixServiceServer(
            socket_path,
            dispatcher,
            SystemClock(),
            access_policy=policy,  # type: ignore[arg-type]
        )
        task = asyncio.create_task(server.serve())
        try:
            await asyncio.wait_for(server.wait_started(), timeout=10)
            assert policy.events == ["runtime", "socket-applied", "runtime-valid", "socket-valid"]
            assert not (tmp_path / "automation/drawingmachine/service.sock").exists()
            assert not (tmp_path / "operator/drawingmachine/service.sock").exists()
        finally:
            await server.close()
            with suppress(BaseException):
                await task

    asyncio.run(scenario())
