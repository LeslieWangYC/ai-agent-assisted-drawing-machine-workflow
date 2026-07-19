from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import struct
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol, cast

from drawingmachine.config.openclaw_deployment import parse_openclaw_deployment_policy
from drawingmachine.config.service_access import (
    MAX_SERVICE_ACCESS_BYTES,
    SERVICE_ACCESS_FILE_MODE,
    ServiceAccessConfigV1,
    parse_service_access_config,
)
from drawingmachine.errors import DrawingMachineError

Role = Literal["automation", "operator"]
Mode = Literal["runtime", "data"]
AclTag = Literal["user", "user_obj", "group", "group_obj", "mask", "other"]
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PLACEHOLDER = re.compile(rb"@([A-Z][A-Z0-9_]*)@")
_POSIX_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_MAX_INSTALL_MANIFEST_BYTES = 262_144
_MAX_CLIENT_PARENT_ENTRIES = 64
_INSTALL_MANIFEST_SHA256 = "0cdcd236230eee19559e779ee39760c67c2cff72d740729fd7ba79f3f52e988f"
_RENAME_NOREPLACE = 1
_RenameAt2 = Callable[[int, bytes, int, bytes, int], int]
_LIBC = ctypes.CDLL(None, use_errno=True)
_raw_renameat2 = _LIBC.renameat2
_raw_renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]
_raw_renameat2.restype = ctypes.c_int
_renameat2 = cast(_RenameAt2, _raw_renameat2)
_EXPECTED_INSTALL_RESOURCES = frozenset(
    {
        "systemd/drawingmachine.service",
        "systemd/drawingmachine-client-endpoint.service",
        "install/service-access.toml.example",
        "install/openclaw-deployment.toml.example",
        "install/config/application.toml.example",
        "install/config/machines/default.toml.example",
        "install/config/providers/local-comfyui.toml.example",
        "install/config/providers/local-comfyui-workflow.json.example",
    }
)
_EXPECTED_RENDER_INPUTS = {
    "SERVICE_UID": "posix-id",
    "SERVICE_GID": "posix-id",
    "AUTOMATION_UID": "posix-id",
    "AUTOMATION_GID": "posix-id",
    "OPERATOR_UID": "posix-id",
    "OPERATOR_GID": "posix-id",
    "CONNECT_GID": "posix-id",
    "AUTOMATION_READ_GID": "posix-id",
    "REGISTRY_OWNER_UID": "posix-id",
    "REGISTRY_OWNER_GID": "posix-id",
    "AUTOMATION_USER": "posix-name",
    "OPENCLAW_REGISTRY_ROOT": "absolute-path",
    "OPENCLAW_MANAGED_ROOT": "absolute-path",
    "SERIAL_DEVICE": "serial-device",
}
_EXPECTED_DERIVED = {
    "OPENCLAW_MANIFEST_SHA256": ("resource-sha256", "openclaw/manifest.json"),
    "OPENCLAW_REGISTRY_POLICY_SHA256": ("resource-sha256", "openclaw/registry-policy.json"),
    "SERVICE_ACCESS_SHA256": ("rendered-resource-sha256", "install/service-access.toml.example"),
}
_EXPECTED_MANAGED_PATHS = frozenset(
    {
        "@SERVICE_HOME@",
        "@SERVICE_CONFIG_ROOT@",
        "@SERVICE_STATE_ROOT@",
        "@SERVICE_DATA_ROOT@",
        "@SERVICE_RUNTIME_ROOT@",
        "@CANONICAL_SOCKET@",
        "@AUTOMATION_IMPORT_ROOT@",
        "@AUTOMATION_EXPORT_ROOT@",
        "@IMAGE_IMPORT_ROOT@",
        "@IMAGE_IMPORT_ROOT@/prepare",
        "@IMAGE_IMPORT_ROOT@/drop",
        "@IMAGE_IMPORT_ROOT@/admitted",
        "@IMAGE_IMPORT_ROOT@/quarantine",
        "@IMAGE_IMPORT_ROOT@/observations",
        "@GCODE_IMPORT_ROOT@",
        "@GCODE_IMPORT_ROOT@/prepare",
        "@GCODE_IMPORT_ROOT@/drop",
        "@GCODE_IMPORT_ROOT@/admitted",
        "@GCODE_IMPORT_ROOT@/quarantine",
        "@GCODE_IMPORT_ROOT@/observations",
        "@AUTOMATION_EXPORT_ROOT@/jobs",
        "@AUTOMATION_EXPORT_ROOT@/jobs/<job-id>",
        "@AUTOMATION_EXPORT_ROOT@/jobs/<job-id>/<relative-path>",
        "@AUTOMATION_RUNTIME_ENDPOINT@",
        "@OPERATOR_RUNTIME_ENDPOINT@",
        "@AUTOMATION_IMPORT_ENDPOINT@",
        "@AUTOMATION_EXPORT_ENDPOINT@",
        "@OPENCLAW_REGISTRY_ROOT@",
        "@OPENCLAW_MANAGED_ROOT@",
        "@OPENCLAW_MANAGED_ROOT@/agents",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-translator",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-router",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-translator/AGENTS.md",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-translator/TOOLS.md",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-router/AGENTS.md",
        "@OPENCLAW_MANAGED_ROOT@/agents/cnc-drawable-router/TOOLS.md",
    }
)


@dataclass(frozen=True, slots=True, order=True)
class EndpointAclEntryV1:
    tag: AclTag
    qualifier: int | None
    permissions: int


@dataclass(frozen=True, slots=True)
class EndpointFactsV1:
    path: Path
    kind: str
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    acl: tuple[EndpointAclEntryV1, ...]
    default_acl: tuple[EndpointAclEntryV1, ...]

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True, slots=True)
class RenderedInstallResourceV1:
    resource: str
    destination: str
    mode: str
    contents: bytes
    source_sha256: str
    sha256: str


class EndpointFactsInspector(Protocol):
    def inspect(self, path: Path) -> EndpointFactsV1: ...


class LinuxEndpointFactsInspector:
    def inspect(self, path: Path) -> EndpointFactsV1:
        value = os.lstat(path)
        return EndpointFactsV1(
            path,
            _kind(value.st_mode),
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            stat.S_IMODE(value.st_mode),
            _read_acl(path, value.st_mode, "system.posix_acl_access"),
            _read_acl(path, value.st_mode, "system.posix_acl_default"),
        )


def render_install_resources(
    replacements: tuple[tuple[str, str], ...],
) -> tuple[RenderedInstallResourceV1, ...]:
    """Purely render and validate packaged cutover resources without writing them."""
    manifest_bytes = _packaged_resource("install/manifest.json")
    if not manifest_bytes or len(manifest_bytes) > _MAX_INSTALL_MANIFEST_BYTES:
        raise ValueError("install manifest is absent or exceeds its bound")
    if hashlib.sha256(manifest_bytes).hexdigest() != _INSTALL_MANIFEST_SHA256:
        raise ValueError("install manifest digest drifted from the frozen production authority")
    try:
        raw = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("install manifest is not canonical JSON") from error
    if type(raw) is not dict:
        raise ValueError("install manifest must be an exact object")
    manifest = cast(dict[str, object], raw)
    if set(manifest) != {"schema_version", "resources", "render", "managed_paths", "limits"}:
        raise ValueError("install manifest fields are not exact")
    if manifest["schema_version"] != 1:
        raise ValueError("install manifest schema_version must be 1")
    render = _exact_object(
        manifest["render"],
        {"schema_version", "max_input_value_bytes", "max_total_rendered_bytes", "inputs", "derived"},
        "render schema",
    )
    if render["schema_version"] != 1:
        raise ValueError("render schema_version must be 1")
    max_value_bytes = _bounded_positive_int(render["max_input_value_bytes"], 16_384, "max input value bytes")
    max_total_bytes = _bounded_positive_int(render["max_total_rendered_bytes"], 2_097_152, "max rendered bytes")
    inputs = _render_declarations(render["inputs"], derived=False)
    derived = _render_declarations(render["derived"], derived=True)
    if {name: kind for name, kind, _ in inputs} != _EXPECTED_RENDER_INPUTS or {
        name: (kind, resource) for name, kind, resource in derived
    } != _EXPECTED_DERIVED:
        raise ValueError("render input or derived schema is not the frozen version")
    _validate_managed_manifest(manifest["managed_paths"])
    if manifest["limits"] != {
        "max_entries_per_role_phase": 32,
        "max_export_artifacts_per_job": 16,
        "lease_max_bytes": 16_384,
        "image_max_payload_bytes": 33_554_432,
        "gcode_max_payload_bytes": 16_777_216,
    }:
        raise ValueError("install limits are not the frozen D7 values")
    if type(replacements) is not tuple or any(type(item) is not tuple or len(item) != 2 for item in replacements):
        raise ValueError("render replacements must be an exact tuple of pairs")
    replacement_names = [item[0] for item in replacements]
    if any(type(name) is not str or type(value) is not str for name, value in replacements):
        raise ValueError("render replacement names and values must be exact strings")
    if len(replacement_names) != len(set(replacement_names)):
        raise ValueError("render replacement names must be unique")
    expected_names = [name for name, _, _ in inputs]
    if set(replacement_names) != set(expected_names) or len(replacement_names) != len(expected_names):
        raise ValueError("render replacements do not match the exact input schema")
    values = dict(replacements)
    for name, kind, _ in inputs:
        _validate_render_input(name, kind, values[name], max_value_bytes)

    resource_entries = manifest["resources"]
    if type(resource_entries) is not list or not resource_entries:
        raise ValueError("install resources must be a non-empty exact list")
    source_records: list[tuple[str, str, str, str, bytes]] = []
    partially_rendered: dict[str, bytes] = {}
    allowed_names = set(expected_names) | {name for name, _, _ in derived}
    seen_placeholders: set[str] = set()
    for item in cast(list[object], resource_entries):
        entry = _exact_object(
            item,
            {"resource", "destination", "creator", "owner", "group", "mode", "sha256", "purpose"},
            "install resource",
        )
        resource = _exact_text(entry["resource"], "resource")
        destination = _exact_text(entry["destination"], "destination")
        mode = _exact_text(entry["mode"], "mode")
        expected_sha256 = _exact_text(entry["sha256"], "resource sha256")
        if _DIGEST.fullmatch(expected_sha256) is None:
            raise ValueError("install resource digest is not canonical")
        contents = _packaged_resource(resource)
        if hashlib.sha256(contents).hexdigest() != expected_sha256:
            raise ValueError("packaged install resource digest drifted")
        names = {match.group(1).decode("ascii") for match in _PLACEHOLDER.finditer(contents)}
        if not names <= allowed_names:
            raise ValueError("packaged install resource contains an undeclared placeholder")
        seen_placeholders.update(names)
        rendered = contents
        for name in expected_names:
            rendered = rendered.replace(f"@{name}@".encode(), values[name].encode())
        partially_rendered[resource] = rendered
        source_records.append((resource, destination, mode, expected_sha256, contents))
    resource_names = [item[0] for item in source_records]
    if len(resource_names) != len(set(resource_names)) or set(resource_names) != _EXPECTED_INSTALL_RESOURCES:
        raise ValueError("install resource namespace is not exact")
    if seen_placeholders != allowed_names:
        raise ValueError("render schema contains an unused or missing placeholder")

    derived_values: dict[str, str] = {}
    for name, kind, resource in derived:
        contents = partially_rendered[resource] if kind == "rendered-resource-sha256" else _packaged_resource(resource)
        derived_values[name] = hashlib.sha256(contents).hexdigest()
    rendered_records: list[RenderedInstallResourceV1] = []
    total = 0
    for resource, destination, mode, source_sha256, _ in source_records:
        contents = partially_rendered[resource]
        for name, value in derived_values.items():
            contents = contents.replace(f"@{name}@".encode(), value.encode())
        if _PLACEHOLDER.search(contents) is not None:
            raise ValueError("rendered install resource retains a placeholder")
        total += len(contents)
        if total > max_total_bytes:
            raise ValueError("rendered install resources exceed their aggregate bound")
        rendered_records.append(
            RenderedInstallResourceV1(
                resource,
                destination,
                mode,
                contents,
                source_sha256,
                hashlib.sha256(contents).hexdigest(),
            )
        )
    result = tuple(rendered_records)
    _validate_rendered_install(result, derived_values)
    return result


def _packaged_resource(relative: str) -> bytes:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError("packaged resource path is not canonical relative text")
    return files("drawingmachine.resources").joinpath(relative).read_bytes()


def _exact_object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != fields:
        raise ValueError(f"{context} fields are not exact")
    return cast(dict[str, object], value)


def _exact_text(value: object, context: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{context} must be bounded exact text")
    return value


def _bounded_positive_int(value: object, maximum: int, context: str) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{context} is outside its bound")
    return value


def _render_declarations(value: object, *, derived: bool) -> tuple[tuple[str, str, str], ...]:
    if type(value) is not list or not value:
        raise ValueError("render declarations must be a non-empty exact list")
    result: list[tuple[str, str, str]] = []
    fields = {"name", "kind", "resource"} if derived else {"name", "kind"}
    for item in cast(list[object], value):
        entry = _exact_object(item, fields, "render declaration")
        name = _exact_text(entry["name"], "placeholder name")
        kind = _exact_text(entry["kind"], "placeholder kind")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None:
            raise ValueError("placeholder name is not canonical")
        resource = _exact_text(entry["resource"], "derived resource") if derived else ""
        if derived and kind not in {"resource-sha256", "rendered-resource-sha256"}:
            raise ValueError("derived placeholder kind is not supported")
        if not derived and kind not in {"posix-id", "posix-name", "absolute-path", "serial-device"}:
            raise ValueError("input placeholder kind is not supported")
        result.append((name, kind, resource))
    names = [item[0] for item in result]
    if len(names) != len(set(names)):
        raise ValueError("render declaration names must be unique")
    return tuple(result)


def _validate_render_input(name: str, kind: str, value: str, maximum_bytes: int) -> None:
    if not value or len(value.encode("utf-8")) > maximum_bytes or "@" in value or "\x00" in value:
        raise ValueError(f"render input {name} is not bounded safe text")
    if kind == "posix-id":
        if re.fullmatch(r"[1-9][0-9]{0,9}", value) is None or int(value) > 2_147_483_647:
            raise ValueError(f"render input {name} is not a canonical POSIX id")
        return
    if kind == "posix-name":
        if _POSIX_NAME.fullmatch(value) is None:
            raise ValueError(f"render input {name} is not a canonical POSIX name")
        return
    path = Path(value)
    if (
        not path.is_absolute()
        or value != str(path)
        or any(part in {".", ".."} or _PATH_COMPONENT.fullmatch(part) is None for part in path.parts[1:])
    ):
        raise ValueError(f"render input {name} is not a canonical absolute path")
    if kind == "serial-device" and (path.parent != Path("/dev/serial/by-id") or path.name in {"", ".", ".."}):
        raise ValueError("serial device must be one fixed /dev/serial/by-id entry")


def _validate_managed_manifest(value: object) -> None:
    if type(value) is not list:
        raise ValueError("managed path manifest must be an exact list")
    entries = cast(list[object], value)
    paths: list[str] = []
    openclaw_targets: dict[str, str] = {}
    base_fields = {"path", "kind", "creator", "owner", "group", "mode", "access", "default_access", "automatic"}
    for item in entries:
        if type(item) is not dict:
            raise ValueError("managed path entry must be an exact object")
        raw = cast(dict[str, object], item)
        kind = _exact_text(raw.get("kind"), "managed path kind")
        expected_fields = base_fields | ({"sha256"} if kind == "regular" else set())
        entry = _exact_object(raw, expected_fields, "managed path")
        path = _exact_text(entry["path"], "managed path")
        if entry["creator"] not in {"cutover", "service-runtime", "client-runtime-helper"}:
            raise ValueError("managed path creator is not closed")
        if type(entry["automatic"]) is not bool:
            raise ValueError("managed path automatic fact is not exact")
        paths.append(path)
        if kind == "regular" and path.startswith("@OPENCLAW_MANAGED_ROOT@/agents/"):
            digest = _exact_text(entry["sha256"], "managed target sha256")
            if _DIGEST.fullmatch(digest) is None:
                raise ValueError("managed target digest is not canonical")
            openclaw_targets[path] = digest
    if len(paths) != len(set(paths)) or set(paths) != _EXPECTED_MANAGED_PATHS:
        raise ValueError("managed path namespace is not the frozen exact tree")
    raw_openclaw = json.loads(_packaged_resource("openclaw/manifest.json"))
    if type(raw_openclaw) is not dict:
        raise ValueError("packaged OpenClaw manifest must be an exact object")
    resources = cast(dict[str, object], raw_openclaw).get("resources")
    if type(resources) is not list:
        raise ValueError("packaged OpenClaw resources must be an exact list")
    expected_targets: dict[str, str] = {}
    for item in cast(list[object], resources):
        if type(item) is not dict:
            raise ValueError("packaged OpenClaw resource must be an exact object")
        resource = cast(dict[str, object], item)
        if resource.get("kind") == "MANAGED_TARGET":
            destination = _exact_text(resource.get("destination"), "OpenClaw destination")
            digest = _exact_text(resource.get("sha256"), "OpenClaw digest")
            expected_targets[f"@OPENCLAW_MANAGED_ROOT@/{destination}"] = digest
    if openclaw_targets != expected_targets:
        raise ValueError("managed OpenClaw targets do not match the packaged D6 manifest")


def _validate_rendered_install(
    resources: tuple[RenderedInstallResourceV1, ...],
    derived: dict[str, str],
) -> None:
    by_resource = {item.resource: item for item in resources}
    if len(by_resource) != len(resources):
        raise ValueError("rendered install resources are not unique")
    service = by_resource["systemd/drawingmachine.service"].contents.decode("utf-8")
    client = by_resource["systemd/drawingmachine-client-endpoint.service"].contents.decode("utf-8")
    if "ExecStart=%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run" not in service:
        raise ValueError("rendered service unit lacks the fixed executable")
    expected_client = (
        "ExecStart=%h/.local/lib/drawingmachine/venv/bin/python "
        "-m drawingmachine.install.client_endpoint runtime "
        "--policy /home/drawingmachine-service/.config/drawingmachine/service-access.toml "
        f"--expected-policy-sha256 {derived['SERVICE_ACCESS_SHA256']}"
    )
    if expected_client not in client:
        raise ValueError("rendered client unit lacks the fixed helper, policy, or digest")
    for unit in (service, client):
        if "/usr/bin/env" in unit or "@" in unit:
            raise ValueError("rendered unit retains mutable executable or placeholder authority")
        for line in unit.splitlines():
            if line.startswith(("ReadOnlyPaths=", "ReadWritePaths=")):
                for path in line.split("=", 1)[1].split():
                    if not (path.startswith(("%h/", "%t/")) or Path(path).is_absolute()):
                        raise ValueError("rendered unit sandbox path is not fixed absolute authority")
    service_access = by_resource["install/service-access.toml.example"].contents
    access = parse_service_access_config(service_access)
    if access.openclaw_policy_path != Path(
        "/home/drawingmachine-service/.config/drawingmachine/openclaw-deployment.toml"
    ):
        raise ValueError("rendered service access policy does not bind the fixed OpenClaw policy")
    if hashlib.sha256(service_access).hexdigest() != derived["SERVICE_ACCESS_SHA256"]:
        raise ValueError("rendered service access digest is not bound to the client unit")
    openclaw_contents = by_resource["install/openclaw-deployment.toml.example"].contents
    openclaw = parse_openclaw_deployment_policy(
        openclaw_contents,
        source_sha256=hashlib.sha256(openclaw_contents).hexdigest(),
    )
    writable_roots = next(
        (line.split("=", 1)[1].split() for line in service.splitlines() if line.startswith("ReadWritePaths=")),
        [],
    )
    if (
        openclaw.manifest_sha256 != derived["OPENCLAW_MANIFEST_SHA256"]
        or openclaw.registry_policy_sha256 != derived["OPENCLAW_REGISTRY_POLICY_SHA256"]
        or not openclaw.registry_root.is_absolute()
        or not openclaw.managed_root.is_absolute()
        or f"ReadOnlyPaths=%h/.config/drawingmachine {openclaw.registry_root}" not in service
        or str(openclaw.managed_root) not in writable_roots
    ):
        raise ValueError("rendered OpenClaw policy roots or packaged digests are not exact")


def rebuild_client_endpoint(
    config: ServiceAccessConfigV1,
    *,
    role: Role,
    mode: Mode,
    current_uid: int | None = None,
    inspector: EndpointFactsInspector | None = None,
) -> tuple[tuple[int, int], ...]:
    """Create only the exact policy endpoint links after validating both sides."""
    if type(config) is not ServiceAccessConfigV1 or role not in {"automation", "operator"}:
        raise ValueError("client endpoint policy and role must be exact")
    if mode not in {"runtime", "data"}:
        raise ValueError("client endpoint mode must be exact")
    principal = config.automation_principal if role == "automation" else config.operator_principal
    uid = os.getuid() if current_uid is None else current_uid
    if type(uid) is not int or uid != principal.uid:
        raise ValueError("current principal does not match the selected endpoint role")
    if mode == "data" and role == "operator":
        raise ValueError("operator data endpoints are forbidden")
    facts = LinuxEndpointFactsInspector() if inspector is None else inspector
    if mode == "runtime":
        endpoint = config.automation_runtime_endpoint if role == "automation" else config.operator_runtime_endpoint
        return _rebuild_links(
            ((endpoint, config.canonical_socket),),
            principal_uid=principal.uid,
            principal_gid=principal.gid,
            config=config,
            inspector=facts,
            runtime=True,
        )
    return _rebuild_links(
        (
            (config.automation_import_endpoint, config.automation_import_root),
            (config.automation_export_endpoint, config.automation_export_root),
        ),
        principal_uid=principal.uid,
        principal_gid=principal.gid,
        config=config,
        inspector=facts,
        runtime=False,
    )


def _rebuild_links(
    links: tuple[tuple[Path, Path], ...],
    *,
    principal_uid: int,
    principal_gid: int,
    config: ServiceAccessConfigV1,
    inspector: EndpointFactsInspector,
    runtime: bool,
) -> tuple[tuple[int, int], ...]:
    parents = {endpoint.parent for endpoint, _ in links}
    if len(parents) != 1:
        raise ValueError("client endpoint links must share one exact private parent")
    parent = next(iter(parents))
    parent_facts = inspector.inspect(parent)
    _require_path(parent_facts, kind="directory", uid=principal_uid, gid=principal_gid, mode=0o700)
    parent_fd = _open_exact_directory(parent, parent_facts.identity)
    try:
        targets: list[tuple[Path, tuple[int, int], str]] = []
        runtime_identity: tuple[int, int] | None = None
        for _, target in links:
            target_facts = inspector.inspect(target)
            if runtime:
                if target != config.canonical_socket:
                    raise ValueError("runtime link target is not the canonical socket")
                runtime_facts = inspector.inspect(config.canonical_runtime_dir)
                _require_runtime_target(runtime_facts, target_facts, config)
                runtime_identity = runtime_facts.identity
            else:
                _require_data_root(target_facts, config, writable=target == config.automation_import_root)
            actual = _lstat_absolute(target)
            expected_kind = "socket" if runtime else "directory"
            if (actual.st_dev, actual.st_ino) != target_facts.identity or _kind(actual.st_mode) != expected_kind:
                raise ValueError("canonical endpoint target identity changed")
            targets.append((target, target_facts.identity, expected_kind))
        existing = [_inspect_link(parent_fd, endpoint.name, target, principal_uid) for endpoint, target in links]
        for (endpoint, _), identity in zip(links, existing, strict=True):
            if identity is None:
                _require_no_endpoint_quarantine(parent_fd, endpoint.name)
        created: list[tuple[str, tuple[int, int], int]] = []
        try:
            for index, ((endpoint, target), identity) in enumerate(zip(links, existing, strict=True)):
                if identity is None:
                    os.symlink(str(target), endpoint.name, dir_fd=parent_fd)
                    try:
                        rollback_descriptor, rollback_identity = _bind_created_link(
                            parent_fd,
                            endpoint.name,
                            target,
                            principal_uid,
                        )
                    except BaseException:
                        _quarantine_unbound_link(parent_fd, endpoint.name, target, principal_uid)
                        raise
                    created.append((endpoint.name, rollback_identity, rollback_descriptor))
                    created_identity = _inspect_link(parent_fd, endpoint.name, target, principal_uid)
                    if created_identity is None or created_identity != rollback_identity:
                        raise ValueError("client endpoint creation did not produce an exact endpoint")
                    existing[index] = created_identity
            os.fsync(parent_fd)
            for target, identity, expected_kind in targets:
                current_facts = inspector.inspect(target)
                if runtime:
                    runtime_after = inspector.inspect(config.canonical_runtime_dir)
                    _require_runtime_target(runtime_after, current_facts, config)
                    if runtime_identity is None or runtime_after.identity != runtime_identity:
                        raise ValueError("canonical runtime directory identity changed during endpoint rebuild")
                else:
                    _require_data_root(current_facts, config, writable=target == config.automation_import_root)
                current = _lstat_absolute(target)
                if (
                    current_facts.identity != identity
                    or (current.st_dev, current.st_ino) != identity
                    or _kind(current.st_mode) != expected_kind
                ):
                    raise ValueError("canonical endpoint target identity changed during endpoint rebuild")
            parent_after = inspector.inspect(parent)
            _require_path(parent_after, kind="directory", uid=principal_uid, gid=principal_gid, mode=0o700)
            if parent_after.identity != parent_facts.identity:
                raise ValueError("client private parent identity changed during endpoint rebuild")
            path_fd = _open_exact_directory(parent, parent_facts.identity)
            os.close(path_fd)
            final = [_inspect_link(parent_fd, endpoint.name, target, principal_uid) for endpoint, target in links]
            if any(identity is None for identity in final):
                raise ValueError("client endpoint disappeared after creation")
            return tuple(identity for identity in final if identity is not None)
        except BaseException:
            cleanup_error: OSError | None = None
            for name, _, _ in reversed(created):
                try:
                    _move_to_quarantine(parent_fd, name)
                except OSError as error:
                    cleanup_error = error
            if created:
                try:
                    os.fsync(parent_fd)
                except OSError as error:
                    cleanup_error = error
            if cleanup_error is not None:
                raise ValueError("client endpoint rollback could not be verified") from cleanup_error
            raise
        finally:
            for _, _, descriptor in created:
                with suppress(OSError):
                    os.close(descriptor)
    finally:
        os.close(parent_fd)


def _open_exact_directory(path: Path, identity: tuple[int, int]) -> int:
    descriptor = _walk_directory(path)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != identity:
        os.close(descriptor)
        raise ValueError("client private parent identity changed")
    return descriptor


def _walk_directory(path: Path, *, open_flags: int = os.O_RDONLY) -> int:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("endpoint directory must be a canonical absolute path")
    flags = open_flags | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(Path("/"), flags)
    try:
        for component in path.parts[1:]:
            opened = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = opened
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lstat_absolute(path: Path) -> os.stat_result:
    parent_fd = _walk_directory(path.parent, open_flags=os.O_PATH)
    try:
        return os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)


def _inspect_link(parent_fd: int, name: str, target: Path, uid: int) -> tuple[int, int] | None:
    try:
        value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(value.st_mode) or value.st_uid != uid:
        raise ValueError("existing client endpoint is not the exact owned symlink")
    try:
        linked = Path(os.readlink(name, dir_fd=parent_fd))
    except OSError as error:
        raise ValueError("existing client endpoint cannot be read exactly") from error
    if not linked.is_absolute() or linked != target:
        raise ValueError("existing client endpoint has an alternate target")
    return value.st_dev, value.st_ino


def _bind_created_link(parent_fd: int, name: str, target: Path, uid: int) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISLNK(value.st_mode) or value.st_uid != uid:
            raise ValueError("created client endpoint is not the exact owned symlink")
        linked = Path(os.readlink(name, dir_fd=parent_fd))
        if not linked.is_absolute() or linked != target:
            raise ValueError("created client endpoint has an alternate target")
        return descriptor, (value.st_dev, value.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _quarantine_unbound_link(parent_fd: int, name: str, target: Path, uid: int) -> None:
    quarantine = _move_to_quarantine(parent_fd, name)
    if quarantine is None:
        _fsync_rollback(parent_fd)
        return
    try:
        try:
            descriptor, _ = _bind_created_link(parent_fd, quarantine, target, uid)
        except BaseException as bind_error:
            classification, classified_identity = _classify_quarantined_link(parent_fd, quarantine, target, uid)
            if classification == "foreign":
                _restore_quarantine(parent_fd, quarantine, name)
                _fsync_rollback(parent_fd)
                return
            if classification == "unknown" or classified_identity is None:
                raise ValueError("client endpoint quarantine identity could not be classified") from bind_error
            raise ValueError("client endpoint quarantine could not be identity-safely deleted") from bind_error
        os.close(descriptor)
        raise ValueError("client endpoint rollback remains safely quarantined")
    except BaseException as error:
        try:
            _fsync_rollback(parent_fd)
        except ValueError as fsync_error:
            raise fsync_error from error
        raise


def _move_to_quarantine(parent_fd: int, name: str) -> str | None:
    quarantine = f"{_endpoint_quarantine_prefix(name)}{secrets.token_hex(16)}"
    try:
        _rename_noreplace(parent_fd, name, parent_fd, quarantine)
    except FileNotFoundError:
        return None
    return quarantine


def _endpoint_quarantine_prefix(name: str) -> str:
    return f".drawingmachine-endpoint-rollback-{name}-"


def _require_no_endpoint_quarantine(parent_fd: int, name: str) -> None:
    prefix = _endpoint_quarantine_prefix(name)
    with os.scandir(parent_fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > _MAX_CLIENT_PARENT_ENTRIES:
                raise ValueError("client endpoint private parent exceeds its bounded entry quota")
            if entry.name.startswith(prefix):
                raise ValueError("client endpoint has an unresolved quarantine")


def _classify_quarantined_link(
    parent_fd: int,
    quarantine: str,
    target: Path,
    uid: int,
) -> tuple[Literal["created", "foreign", "unknown"], tuple[int, int] | None]:
    try:
        before = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISLNK(before.st_mode) or before.st_uid != uid:
            return "foreign", None
        linked = Path(os.readlink(quarantine, dir_fd=parent_fd))
        after = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return "unknown", None
    identity = before.st_dev, before.st_ino
    if identity != (after.st_dev, after.st_ino):
        return "unknown", None
    if not linked.is_absolute() or linked != target:
        return "foreign", None
    return "created", identity


def _restore_quarantine(parent_fd: int, quarantine: str, name: str) -> None:
    try:
        _rename_noreplace(parent_fd, quarantine, parent_fd, name)
    except BaseException as error:
        raise ValueError("client endpoint quarantine could not be restored") from error


def _fsync_rollback(parent_fd: int) -> None:
    try:
        os.fsync(parent_fd)
    except OSError as error:
        raise ValueError("client endpoint rollback could not be made durable") from error


def _rename_noreplace(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    ctypes.set_errno(0)
    result = _renameat2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _require_path(facts: EndpointFactsV1, *, kind: str, uid: int, gid: int, mode: int) -> None:
    if (
        type(facts) is not EndpointFactsV1
        or facts.kind != kind
        or (facts.uid, facts.gid, facts.mode) != (uid, gid, mode)
        or facts.acl != _mode_acl(mode)
        or facts.default_acl
    ):
        qualifier = "private" if mode == 0o700 else "exact"
        raise ValueError(f"endpoint path does not have the {qualifier} identity, mode, and ACL")


def _require_runtime_target(
    runtime_facts: EndpointFactsV1,
    socket_facts: EndpointFactsV1,
    config: ServiceAccessConfigV1,
) -> None:
    _require_path(
        runtime_facts,
        kind="directory",
        uid=config.service_principal.uid,
        gid=config.connect_group.gid,
        mode=0o710,
    )
    _require_path(
        socket_facts,
        kind="socket",
        uid=config.service_principal.uid,
        gid=config.connect_group.gid,
        mode=0o660,
    )


def _require_data_root(facts: EndpointFactsV1, config: ServiceAccessConfigV1, *, writable: bool) -> None:
    entries: tuple[EndpointAclEntryV1, ...] = (
        EndpointAclEntryV1("user_obj", None, 0o7),
        EndpointAclEntryV1("user", config.automation_principal.uid, 0o7 if writable else 0o5),
        EndpointAclEntryV1("group_obj", None, 0),
        EndpointAclEntryV1("mask", None, 0o7),
        EndpointAclEntryV1("other", None, 0),
    )
    if writable and config.service_principal.uid != config.automation_principal.uid:
        entries = (*entries, EndpointAclEntryV1("user", config.service_principal.uid, 0o7))
    acl = tuple(sorted(entries))
    if (
        type(facts) is not EndpointFactsV1
        or facts.kind != "directory"
        or (facts.uid, facts.gid, facts.mode) != (config.service_principal.uid, config.connect_group.gid, 0o2770)
        or facts.acl != acl
        or facts.default_acl != acl
    ):
        raise ValueError("automation data root identity, mode, access ACL, or default ACL is not exact")


def _mode_acl(mode: int) -> tuple[EndpointAclEntryV1, ...]:
    return tuple(
        sorted(
            (
                EndpointAclEntryV1("user_obj", None, (mode >> 6) & 0o7),
                EndpointAclEntryV1("group_obj", None, (mode >> 3) & 0o7),
                EndpointAclEntryV1("other", None, mode & 0o7),
            )
        )
    )


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _read_acl(path: Path, mode: int, attribute: str) -> tuple[EndpointAclEntryV1, ...]:
    try:
        raw = os.getxattr(path, attribute, follow_symlinks=False)
    except OSError as error:
        absent = {errno.ENODATA}
        if hasattr(errno, "ENOATTR"):
            absent.add(errno.ENOATTR)
        if error.errno not in absent:
            raise ValueError("endpoint ACL evidence cannot be read") from error
        return () if attribute.endswith("default") else _mode_acl(stat.S_IMODE(mode))
    if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != 2 or (len(raw) - 4) % 8:
        raise ValueError("endpoint ACL evidence is malformed")
    names: dict[int, AclTag] = {1: "user_obj", 2: "user", 4: "group_obj", 8: "group", 16: "mask", 32: "other"}
    entries: list[EndpointAclEntryV1] = []
    for offset in range(4, len(raw), 8):
        tag, permissions, qualifier = struct.unpack_from("<HHI", raw, offset)
        if tag not in names:
            raise ValueError("endpoint ACL evidence has an unknown tag")
        name = names[tag]
        entries.append(EndpointAclEntryV1(name, qualifier if name in {"user", "group"} else None, permissions))
    return tuple(sorted(entries))


def _load_policy(path: Path, expected_sha256: str) -> ServiceAccessConfigV1:
    if (
        not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or _DIGEST.fullmatch(expected_sha256) is None
    ):
        raise ValueError("absolute policy path and expected policy digest are required")
    parent_fd = _walk_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > MAX_SERVICE_ACCESS_BYTES
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ValueError("service access policy type, link count, size, or identity is unsafe")
            contents = b""
            while chunk := os.read(descriptor, MAX_SERVICE_ACCESS_BYTES + 1 - len(contents)):
                contents += chunk
                if len(contents) > MAX_SERVICE_ACCESS_BYTES:
                    raise ValueError("service access policy exceeds its size limit")
            after = os.fstat(descriptor)
            path_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("service access policy changed while reading")
    if hashlib.sha256(contents).hexdigest() != expected_sha256:
        raise ValueError("service access policy digest does not match the expected digest")
    config = parse_service_access_config(contents)
    facts = LinuxEndpointFactsInspector().inspect(path)
    if facts.identity != (after.st_dev, after.st_ino):
        raise ValueError("service access policy identity changed after exact-byte validation")
    _require_path(
        facts,
        kind="regular",
        uid=config.service_principal.uid,
        gid=config.connect_group.gid,
        mode=SERVICE_ACCESS_FILE_MODE,
    )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m drawingmachine.install.client_endpoint")
    parser.add_argument("mode", choices=("runtime", "data"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        config = _load_policy(arguments.policy, arguments.expected_policy_sha256)
        uid = os.getuid()
        if uid == config.automation_principal.uid:
            role: Role = "automation"
        elif uid == config.operator_principal.uid:
            role = "operator"
        else:
            raise ValueError("current principal is not an authorized client")
        rebuild_client_endpoint(config, role=role, mode=arguments.mode, current_uid=uid)
    except (DrawingMachineError, OSError, ValueError) as error:
        print(f"client endpoint setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EndpointAclEntryV1",
    "EndpointFactsInspector",
    "EndpointFactsV1",
    "RenderedInstallResourceV1",
    "main",
    "rebuild_client_endpoint",
    "render_install_resources",
]
