from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, cast

from drawingmachine.config.machine import MachineExecutionConfig
from drawingmachine.config.openclaw_deployment import PosixGroupV1, PosixPrincipalV1
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

MAX_SERVICE_ACCESS_BYTES = 65_536
SERVICE_ACCESS_FILE_MODE = 0o640
_FIELDS = frozenset(
    {
        "schema_version",
        "service_principal",
        "automation_principal",
        "operator_principal",
        "connect_group",
        "canonical_runtime_dir",
        "canonical_socket",
        "automation_runtime_endpoint",
        "operator_runtime_endpoint",
        "canonical_data_dir",
        "automation_import_root",
        "automation_export_root",
        "automation_import_endpoint",
        "automation_export_endpoint",
        "openclaw_policy_path",
    }
)
_PRINCIPAL_FIELDS = frozenset({"uid", "gid"})
_GROUP_FIELDS = frozenset({"name", "gid"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ServiceAccessConfigV1:
    service_principal: PosixPrincipalV1
    automation_principal: PosixPrincipalV1
    operator_principal: PosixPrincipalV1
    connect_group: PosixGroupV1
    canonical_runtime_dir: Path
    canonical_socket: Path
    automation_runtime_endpoint: Path
    operator_runtime_endpoint: Path
    canonical_data_dir: Path
    automation_import_root: Path
    automation_export_root: Path
    automation_import_endpoint: Path
    automation_export_endpoint: Path
    openclaw_policy_path: Path
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class LoadedServiceAccessV1:
    config: ServiceAccessConfigV1
    source_path: Path
    source_size_bytes: int
    source_sha256: str
    source_identity: tuple[int, int]
    source_mode: int


@dataclass(frozen=True, slots=True)
class ValidatedAutomationDataEndpointsV1:
    import_endpoint_identity: tuple[int, int]
    export_endpoint_identity: tuple[int, int]
    import_root_identity: tuple[int, int]
    export_root_identity: tuple[int, int]
    import_root_acl: tuple[object, ...]
    export_root_acl: tuple[object, ...]
    import_root_default_acl: tuple[object, ...]
    export_root_default_acl: tuple[object, ...]


class AutomationDataFacts(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def kind(self) -> str: ...

    @property
    def device(self) -> int: ...

    @property
    def inode(self) -> int: ...

    @property
    def uid(self) -> int: ...

    @property
    def gid(self) -> int: ...

    @property
    def mode(self) -> int: ...

    @property
    def acl(self) -> tuple[object, ...]: ...

    @property
    def default_acl(self) -> tuple[object, ...]: ...


class AutomationDataFactsInspector(Protocol):
    def inspect(self, path: Path) -> AutomationDataFacts: ...


def validate_automation_data_endpoints(
    config: ServiceAccessConfigV1,
    *,
    inspector: AutomationDataFactsInspector,
) -> ValidatedAutomationDataEndpointsV1:
    if type(config) is not ServiceAccessConfigV1:
        _invalid("automation data endpoint policy must be exact")
    endpoint_identities: list[tuple[int, int]] = []
    root_identities: list[tuple[int, int]] = []
    root_acls: list[tuple[object, ...]] = []
    root_default_acls: list[tuple[object, ...]] = []
    for endpoint, target in (
        (config.automation_import_endpoint, config.automation_import_root),
        (config.automation_export_endpoint, config.automation_export_root),
    ):
        try:
            link = os.lstat(endpoint)
            link_target = Path(os.readlink(endpoint))
            canonical = inspector.inspect(target)
        except OSError as error:
            raise _error("automation data endpoint cannot be inspected") from error
        if (
            not stat.S_ISLNK(link.st_mode)
            or link.st_uid != config.automation_principal.uid
            or not link_target.is_absolute()
            or link_target != target
            or canonical.path != target
            or canonical.kind != "directory"
            or canonical.uid != config.service_principal.uid
            or canonical.gid != config.connect_group.gid
            or canonical.mode != 0o2770
            or type(canonical.acl) is not tuple
            or type(canonical.default_acl) is not tuple
        ):
            _invalid("automation data endpoint/root identity, owner, group, mode, type, or target is not exact")
        endpoint_identities.append((link.st_dev, link.st_ino))
        root_identities.append((canonical.device, canonical.inode))
        root_acls.append(canonical.acl)
        root_default_acls.append(canonical.default_acl)
    return ValidatedAutomationDataEndpointsV1(
        endpoint_identities[0],
        endpoint_identities[1],
        root_identities[0],
        root_identities[1],
        root_acls[0],
        root_acls[1],
        root_default_acls[0],
        root_default_acls[1],
    )


def parse_service_access_config(contents: bytes) -> ServiceAccessConfigV1:
    if type(contents) is not bytes or not contents or len(contents) > MAX_SERVICE_ACCESS_BYTES:
        _invalid("service access policy must be non-empty exact bytes within 65536 bytes")
    try:
        raw = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _error("service access policy must be valid UTF-8 TOML") from error
    data = _table(raw, _FIELDS, "service access policy")
    if _integer(data["schema_version"], "schema_version") != 1:
        _invalid("schema_version must be 1")
    service = _principal(data["service_principal"], "service_principal")
    automation = _principal(data["automation_principal"], "automation_principal")
    operator = _principal(data["operator_principal"], "operator_principal")
    principals = (service, automation, operator)
    if len({item.uid for item in principals}) != 3 or len({item.gid for item in principals}) != 3:
        _invalid("service, automation, and operator principals must have disjoint UID/GID values")
    group_data = _table(data["connect_group"], _GROUP_FIELDS, "connect_group")
    try:
        connect_group = PosixGroupV1(
            _string(group_data["name"], "connect_group.name"),
            _integer(group_data["gid"], "connect_group.gid"),
        )
    except TypeError as error:
        raise _error("connect_group contains an invalid POSIX identity") from error
    if connect_group.gid in {item.gid for item in principals}:
        _invalid("connect_group must be supplemental and not a configured primary GID")

    paths = {
        field: _absolute_path(data[field], field)
        for field in _FIELDS
        if field.endswith(("_dir", "_socket", "_endpoint", "_root", "_path"))
    }
    runtime_dir = paths["canonical_runtime_dir"]
    canonical_socket = paths["canonical_socket"]
    if canonical_socket != runtime_dir / "service.sock":
        _invalid("canonical_socket must be canonical_runtime_dir/service.sock")
    automation_endpoint = paths["automation_runtime_endpoint"]
    operator_endpoint = paths["operator_runtime_endpoint"]
    if canonical_socket in {automation_endpoint, operator_endpoint}:
        _invalid("client runtime endpoints must be distinct from the canonical service socket")
    if automation_endpoint == operator_endpoint or any(
        path.name != "service.sock" for path in (automation_endpoint, operator_endpoint)
    ):
        _invalid("client runtime endpoints must be distinct drawingmachine service.sock paths")
    if any(path.parent.name != "drawingmachine" for path in (automation_endpoint, operator_endpoint)):
        _invalid("client runtime endpoints must be contained in private drawingmachine runtime directories")
    data_dir = paths["canonical_data_dir"]
    import_root = paths["automation_import_root"]
    export_root = paths["automation_export_root"]
    if import_root == export_root:
        _invalid("automation import/export roots must be distinct")
    if data_dir not in import_root.parents:
        _invalid("automation_import_root must be beneath canonical_data_dir")
    if data_dir not in export_root.parents:
        _invalid("automation_export_root must be beneath canonical_data_dir")
    if paths["automation_import_endpoint"] == paths["automation_export_endpoint"]:
        _invalid("automation import/export endpoints must be distinct")
    return ServiceAccessConfigV1(
        service,
        automation,
        operator,
        connect_group,
        runtime_dir,
        canonical_socket,
        automation_endpoint,
        operator_endpoint,
        data_dir,
        import_root,
        export_root,
        paths["automation_import_endpoint"],
        paths["automation_export_endpoint"],
        paths["openclaw_policy_path"],
    )


def load_service_access_config(
    path: Path,
    *,
    service_principal: PosixPrincipalV1 | None,
    machine_config: MachineExecutionConfig,
    machine_sha256: str,
) -> LoadedServiceAccessV1:
    if type(path) is not type(Path()) or not path.is_absolute():
        _invalid("service access path must be an absolute pathlib Path")
    if service_principal is not None and type(service_principal) is not PosixPrincipalV1:
        _invalid("service principal snapshot must be exact")
    if type(machine_config) is not MachineExecutionConfig:
        _invalid("machine configuration must be exact")
    if type(machine_sha256) is not str or _DIGEST.fullmatch(machine_sha256) is None:
        _invalid("machine profile digest must be canonical SHA-256")
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise _error("service access policy cannot be opened no-follow") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != SERVICE_ACCESS_FILE_MODE
            or opened.st_size > MAX_SERVICE_ACCESS_BYTES
        ):
            _invalid("service access policy ownership, type, mode, link, or size is unsafe")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65_536, MAX_SERVICE_ACCESS_BYTES + 1 - total)):
            total += len(chunk)
            if total > MAX_SERVICE_ACCESS_BYTES:
                _invalid("service access policy exceeds 65536 bytes")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        stable = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if stable != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (
            path_after.st_dev,
            path_after.st_ino,
        ) != (after.st_dev, after.st_ino):
            _invalid("service access policy changed while reading")
    finally:
        os.close(descriptor)
    contents = b"".join(chunks)
    config = parse_service_access_config(contents)
    if opened.st_uid != config.service_principal.uid:
        _invalid("service access policy must be owned by the configured service UID")
    if service_principal is not None and config.service_principal != service_principal:
        _invalid("configured service principal differs from the running service")
    if opened.st_gid != config.connect_group.gid:
        _invalid("service access policy must be owned by the exact connect group")
    machine_automation = PosixPrincipalV1(
        machine_config.automation_principal.uid, machine_config.automation_principal.gid
    )
    machine_operator = PosixPrincipalV1(machine_config.operator_principal.uid, machine_config.operator_principal.gid)
    if config.automation_principal != machine_automation or config.operator_principal != machine_operator:
        _invalid("service access principals differ from the exact machine profile")
    return LoadedServiceAccessV1(
        config,
        path,
        len(contents),
        hashlib.sha256(contents).hexdigest(),
        (opened.st_dev, opened.st_ino),
        stat.S_IMODE(opened.st_mode),
    )


def _table(value: object, fields: frozenset[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{context} must be an exact table")
    result = cast(dict[str, object], value)
    if set(result) != fields or any(type(key) is not str for key in result):
        _invalid(f"{context} must contain exactly: {', '.join(sorted(fields))}")
    return result


def _principal(value: object, context: str) -> PosixPrincipalV1:
    data = _table(value, _PRINCIPAL_FIELDS, context)
    try:
        return PosixPrincipalV1(
            _integer(data["uid"], f"{context}.uid"),
            _integer(data["gid"], f"{context}.gid"),
        )
    except TypeError as error:
        raise _error(f"{context} contains an invalid POSIX identity") from error


def _absolute_path(value: object, field: str) -> Path:
    text = _string(value, field)
    path = Path(text)
    if (
        not text.isascii()
        or len(text) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or not path.is_absolute()
        or text.startswith("//")
        or str(path) != text
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a bounded canonical absolute ASCII path")
    return path


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        _invalid(f"{field} must be an exact integer")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        _invalid(f"{field} must be a non-empty exact string")
    return value


def _error(message: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload("SERVICE_ACCESS_INVALID", ErrorCategory.CONFIGURATION, message, False, {}))


def _invalid(message: str) -> NoReturn:
    raise _error(message)


__all__ = [
    "MAX_SERVICE_ACCESS_BYTES",
    "SERVICE_ACCESS_FILE_MODE",
    "LoadedServiceAccessV1",
    "ServiceAccessConfigV1",
    "ValidatedAutomationDataEndpointsV1",
    "load_service_access_config",
    "parse_service_access_config",
    "validate_automation_data_endpoints",
]
