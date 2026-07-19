from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from drawingmachine.config import (
    ServiceAccessConfigV1,
    XdgPaths,
    load_config,
    load_service_access_config,
    parse_machine_execution_config,
)
from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

CANONICAL_SOCKET_MODE = 0o660
ClientPathKind = Literal["directory", "regular", "socket", "symlink", "special"]


@dataclass(frozen=True, slots=True)
class ClientPathFactsV1:
    path: Path
    kind: ClientPathKind
    device: int
    inode: int
    uid: int
    gid: int
    mode: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True, slots=True)
class ClientAccessSnapshotV1:
    config: ServiceAccessConfigV1
    source_path: Path
    source_size_bytes: int
    source_sha256: str
    source_identity: tuple[int, int]
    machine_profile_sha256: str
    client_endpoint: Path
    client_principal: PosixPrincipalV1


class ClientAccessInspector(Protocol):
    def current_principal(self) -> PosixPrincipalV1: ...

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]: ...

    def inspect(self, path: Path) -> ClientPathFactsV1: ...


class LinuxClientAccessInspector:
    def current_principal(self) -> PosixPrincipalV1:
        return PosixPrincipalV1(os.getuid(), os.getgid())

    def supplemental_gids(self, principal: PosixPrincipalV1) -> frozenset[int]:
        import pwd

        try:
            username = pwd.getpwuid(principal.uid).pw_name
            return frozenset(os.getgrouplist(username, principal.gid))
        except (KeyError, OSError) as error:
            raise _error("direct client group membership cannot be inspected") from error

    def inspect(self, path: Path) -> ClientPathFactsV1:
        try:
            value = os.lstat(path)
        except OSError as error:
            raise _error("client endpoint path cannot be inspected") from error
        if stat.S_ISDIR(value.st_mode):
            kind: ClientPathKind = "directory"
        elif stat.S_ISREG(value.st_mode):
            kind = "regular"
        elif stat.S_ISSOCK(value.st_mode):
            kind = "socket"
        elif stat.S_ISLNK(value.st_mode):
            kind = "symlink"
        else:
            kind = "special"
        return ClientPathFactsV1(
            path,
            kind,
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            stat.S_IMODE(value.st_mode),
        )


@dataclass(frozen=True, slots=True)
class ValidatedClientEndpointV1:
    endpoint: Path
    endpoint_identity: tuple[int, int]
    canonical_socket: Path
    canonical_identity: tuple[int, int]
    policy_sha256: str


def select_client_endpoint(snapshot: ClientAccessSnapshotV1) -> tuple[Path, PosixPrincipalV1]:
    if type(snapshot) is not ClientAccessSnapshotV1:
        raise _error("service access client snapshot must be exact")
    return snapshot.client_endpoint, snapshot.client_principal


def load_client_access_snapshot(
    paths: XdgPaths,
    *,
    inspector: ClientAccessInspector | None = None,
) -> ClientAccessSnapshotV1:
    facts_inspector = LinuxClientAccessInspector() if inspector is None else inspector
    bundle = load_config(paths)
    machine = parse_machine_execution_config(bundle.machine, profile_path=bundle.machine_path)
    loaded = load_service_access_config(
        paths.service_access_file,
        service_principal=None,
        machine_config=machine,
        machine_sha256=bundle.digests["machine"],
    )
    config = loaded.config
    endpoint, principal = select_client_endpoint_from(config, facts_inspector.current_principal())
    if paths.socket_file != endpoint or endpoint == config.canonical_socket:
        raise _error("client XDG socket is not the selected private service endpoint")
    if config.connect_group.gid not in facts_inspector.supplemental_gids(principal):
        raise _error("direct client is not an exact supplemental connect-group member")
    source = facts_inspector.inspect(loaded.source_path)
    if source.identity != loaded.source_identity:
        raise _error("service access policy identity changed after load")
    return ClientAccessSnapshotV1(
        config,
        loaded.source_path,
        loaded.source_size_bytes,
        loaded.source_sha256,
        loaded.source_identity,
        bundle.digests["machine"],
        endpoint,
        principal,
    )


def select_client_endpoint_from(
    config: ServiceAccessConfigV1,
    current: PosixPrincipalV1 | None = None,
) -> tuple[Path, PosixPrincipalV1]:
    if type(config) is not ServiceAccessConfigV1:
        raise _error("service access client config must be exact")
    if current is None:
        current = PosixPrincipalV1(os.getuid(), os.getgid())
    elif type(current) is not PosixPrincipalV1:
        raise _error("current service client principal must be exact")
    if current == config.automation_principal:
        return config.automation_runtime_endpoint, current
    if current == config.operator_principal:
        return config.operator_runtime_endpoint, current
    raise _error("current principal is not an authorized service client")


def validate_client_endpoint(
    snapshot: ClientAccessSnapshotV1,
    endpoint: Path,
    principal: PosixPrincipalV1,
    *,
    inspector: ClientAccessInspector | None = None,
) -> ValidatedClientEndpointV1:
    if type(snapshot) is not ClientAccessSnapshotV1 or type(principal) is not PosixPrincipalV1:
        raise _error("client endpoint validation inputs must be exact")
    config = snapshot.config
    expected = {
        config.automation_runtime_endpoint: config.automation_principal,
        config.operator_runtime_endpoint: config.operator_principal,
    }
    if expected.get(endpoint) != principal:
        raise _error("client endpoint is not assigned to the direct client principal")
    facts_inspector = LinuxClientAccessInspector() if inspector is None else inspector
    parent = facts_inspector.inspect(endpoint.parent)
    _require_private_parent(parent, principal)
    link = facts_inspector.inspect(endpoint)
    if link.kind != "symlink" or link.uid != principal.uid:
        raise _error("client endpoint must be one client-owned symlink")
    try:
        target = Path(os.readlink(endpoint))
    except OSError as error:
        raise _error("client endpoint symlink cannot be read") from error
    if not target.is_absolute() or target != config.canonical_socket:
        raise _error("client endpoint must target the exact canonical socket")
    canonical = facts_inspector.inspect(config.canonical_socket)
    if (
        canonical.kind != "socket"
        or canonical.uid != config.service_principal.uid
        or canonical.gid != config.connect_group.gid
        or canonical.mode != CANONICAL_SOCKET_MODE
    ):
        raise _error("canonical service socket facts are not exact")
    return ValidatedClientEndpointV1(
        endpoint,
        link.identity,
        config.canonical_socket,
        canonical.identity,
        snapshot.source_sha256,
    )


def revalidate_client_endpoint(
    validated: ValidatedClientEndpointV1,
    snapshot: ClientAccessSnapshotV1,
    principal: PosixPrincipalV1,
    *,
    inspector: ClientAccessInspector | None = None,
) -> None:
    current = validate_client_endpoint(snapshot, validated.endpoint, principal, inspector=inspector)
    if current != validated:
        raise _error("client endpoint or canonical socket changed during connect")


def _require_private_parent(facts: ClientPathFactsV1, principal: PosixPrincipalV1) -> None:
    if facts.kind != "directory" or (facts.uid, facts.gid) != (principal.uid, principal.gid) or facts.mode != 0o700:
        raise _error("client endpoint parent must be the client's exact private runtime directory")


def _error(message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload("SERVICE_ENDPOINT_INVALID", ErrorCategory.PERMISSION, message, False, {}))


__all__ = [
    "ClientAccessSnapshotV1",
    "ClientPathFactsV1",
    "LinuxClientAccessInspector",
    "ValidatedClientEndpointV1",
    "load_client_access_snapshot",
    "revalidate_client_endpoint",
    "select_client_endpoint",
    "validate_client_endpoint",
]
