from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, cast

MAX_RESOURCE_BYTES = 65_536
_SCHEMA_VERSION = 1
_TARGET_MODE = 0o640
_AGENT_IDS = ("cnc-drawable-translator", "cnc-drawable-router")
_MANAGED_SOURCES = tuple(f"agents/{agent_id}/{name}" for agent_id in _AGENT_IDS for name in ("AGENTS.md", "TOOLS.md"))
_RESOURCE_SOURCES = (*_MANAGED_SOURCES, "registry-policy.json")
_REQUIRED_TOOLS = {
    "cnc-drawable-translator": ("image", "read", "write", "exec", "sessions_spawn", "subagents"),
    "cnc-drawable-router": ("image", "read", "write", "exec"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


class OpenClawResourceError(ValueError):
    """A packaged OpenClaw resource contract is invalid."""


@dataclass(frozen=True, slots=True)
class OpenClawResourceV1:
    kind: Literal["MANAGED_TARGET", "CHECK_ONLY_REGISTRY_POLICY"]
    source: str
    destination: str | None
    media_type: str
    size_bytes: int
    sha256: str
    target_mode: int | None


@dataclass(frozen=True, slots=True)
class OpenClawResourceManifestV1:
    schema_version: int
    agent_ids: tuple[str, str]
    resources: tuple[OpenClawResourceV1, ...]


@dataclass(frozen=True, slots=True)
class OpenClawRegistryAgentPolicyV1:
    agent_id: str
    workspace_relative_path: str
    required_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenClawRegistryPolicyV1:
    schema_version: int
    agents: tuple[OpenClawRegistryAgentPolicyV1, ...]


def load_openclaw_resource_manifest() -> OpenClawResourceManifestV1:
    return _load_openclaw_package()[0]


def load_openclaw_registry_policy() -> OpenClawRegistryPolicyV1:
    return _load_openclaw_package()[1]


def _load_openclaw_package() -> tuple[OpenClawResourceManifestV1, OpenClawRegistryPolicyV1]:
    root = importlib.resources.files("drawingmachine.resources.openclaw")
    manifest_bytes = _read_bounded(root.joinpath("manifest.json"), "manifest.json")
    packaged = _collect_packaged_resources(root)
    return _validate_openclaw_package(manifest_bytes, packaged)


def _collect_packaged_resources(root: Traversable) -> dict[str, bytes]:
    result: dict[str, bytes] = {}

    def visit(directory: Traversable, prefix: str) -> None:
        for item in directory.iterdir():
            relative = f"{prefix}/{item.name}" if prefix else item.name
            if item.name == "__pycache__":
                continue
            if isinstance(item, Path) and item.is_symlink():
                _invalid(f"packaged resource must not be a symlink: {relative}")
            if item.is_dir():
                visit(item, relative)
            elif item.is_file():
                if relative in {"__init__.py", "manifest.json"} or relative.endswith((".pyc", ".pyo")):
                    continue
                result[relative] = _read_bounded(item, relative)
            else:
                _invalid(f"packaged resource must be a regular file: {relative}")

    visit(root, "")
    return result


def _read_bounded(resource: Traversable, name: str) -> bytes:
    try:
        with resource.open("rb") as stream:
            contents = stream.read(MAX_RESOURCE_BYTES + 1)
    except (FileNotFoundError, OSError) as error:
        raise OpenClawResourceError(f"could not read packaged resource: {name}") from error
    if len(contents) > MAX_RESOURCE_BYTES:
        _invalid(f"packaged resource exceeds {MAX_RESOURCE_BYTES} bytes: {name}")
    return contents


def _validate_openclaw_package(
    manifest_bytes: bytes,
    packaged_resources: Mapping[str, bytes],
) -> tuple[OpenClawResourceManifestV1, OpenClawRegistryPolicyV1]:
    manifest_data = _object(_decode_json(manifest_bytes, "manifest.json"), "manifest")
    _exact_fields(manifest_data, frozenset({"schema_version", "agent_ids", "resources"}), "manifest")
    _schema(manifest_data["schema_version"], "manifest.schema_version")

    agent_ids = _string_list(manifest_data["agent_ids"], "manifest.agent_ids")
    if agent_ids != _AGENT_IDS:
        _invalid("manifest.agent_ids must contain the two closed agent IDs in canonical order")

    resource_values = manifest_data["resources"]
    if not isinstance(resource_values, list):
        _invalid("manifest.resources must be an array")
    if len(resource_values) != len(_RESOURCE_SOURCES):
        _invalid("manifest.resources must contain exactly five resources")

    resources = tuple(_resource(item, index) for index, item in enumerate(cast(list[object], resource_values)))
    sources = tuple(item.source for item in resources)
    if len(set(sources)) != len(sources):
        _invalid("manifest resource sources must be unique")
    if sources != _RESOURCE_SOURCES:
        _invalid("manifest resources must name the exact five canonical package sources")

    destinations = tuple(item.destination for item in resources if item.destination is not None)
    if len(set(destinations)) != len(destinations):
        _invalid("managed resource destinations must be unique")
    if destinations != _MANAGED_SOURCES:
        _invalid("manifest must name the exact four managed destinations")

    packaged_names = tuple(packaged_resources)
    for name in packaged_names:
        _relative_path(name, "packaged resource name")
    if set(packaged_names) != set(_RESOURCE_SOURCES):
        _invalid("packaged resource set does not exactly match the manifest")

    for resource in resources:
        contents = packaged_resources[resource.source]
        if not isinstance(contents, bytes):
            _invalid(f"packaged resource must contain bytes: {resource.source}")
        if len(contents) > MAX_RESOURCE_BYTES:
            _invalid(f"packaged resource exceeds {MAX_RESOURCE_BYTES} bytes: {resource.source}")
        if len(contents) != resource.size_bytes:
            _invalid(f"packaged resource size does not match manifest: {resource.source}")
        if hashlib.sha256(contents).hexdigest() != resource.sha256:
            _invalid(f"packaged resource digest does not match manifest: {resource.source}")
        try:
            contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise OpenClawResourceError(f"packaged resource is not UTF-8: {resource.source}") from error

    policy = _registry_policy(packaged_resources["registry-policy.json"])
    return OpenClawResourceManifestV1(_SCHEMA_VERSION, _AGENT_IDS, resources), policy


def _resource(value: object, index: int) -> OpenClawResourceV1:
    context = f"manifest.resources[{index}]"
    data = _object(value, context)
    kind = _string(data.get("kind"), f"{context}.kind")
    common = frozenset({"kind", "source", "media_type", "size_bytes", "sha256"})
    if kind == "MANAGED_TARGET":
        _exact_fields(data, common | {"destination", "target_mode"}, context)
        destination = _relative_path(data["destination"], f"{context}.destination")
        target_mode = _integer(data["target_mode"], f"{context}.target_mode", minimum=1, maximum=0o7777)
        if target_mode != _TARGET_MODE:
            _invalid(f"{context}.target_mode must be 0640")
        expected_media_type = "text/markdown; charset=utf-8"
    elif kind == "CHECK_ONLY_REGISTRY_POLICY":
        _exact_fields(data, common, context)
        destination = None
        target_mode = None
        expected_media_type = "application/json; charset=utf-8"
    else:
        _invalid(f"{context}.kind is unsupported")

    source = _relative_path(data["source"], f"{context}.source")
    media_type = _string(data["media_type"], f"{context}.media_type")
    if media_type != expected_media_type:
        _invalid(f"{context}.media_type is invalid for {kind}")
    size_bytes = _integer(data["size_bytes"], f"{context}.size_bytes", minimum=1, maximum=MAX_RESOURCE_BYTES)
    sha256 = _digest(data["sha256"], f"{context}.sha256")
    return OpenClawResourceV1(
        cast(Literal["MANAGED_TARGET", "CHECK_ONLY_REGISTRY_POLICY"], kind),
        source,
        destination,
        media_type,
        size_bytes,
        sha256,
        target_mode,
    )


def _registry_policy(contents: bytes) -> OpenClawRegistryPolicyV1:
    data = _object(_decode_json(contents, "registry-policy.json"), "registry policy")
    _exact_fields(data, frozenset({"schema_version", "agents"}), "registry policy")
    _schema(data["schema_version"], "registry policy.schema_version")
    values = data["agents"]
    if not isinstance(values, list) or len(values) != len(_AGENT_IDS):
        _invalid("registry policy agents must contain exactly two entries")

    agents: list[OpenClawRegistryAgentPolicyV1] = []
    for index, value in enumerate(cast(list[object], values)):
        context = f"registry policy.agents[{index}]"
        agent = _object(value, context)
        _exact_fields(agent, frozenset({"agent_id", "workspace_relative_path", "required_tools"}), context)
        agent_id = _string(agent["agent_id"], f"{context}.agent_id")
        workspace = _relative_path(agent["workspace_relative_path"], f"{context}.workspace_relative_path")
        tools = _string_list(agent["required_tools"], f"{context}.required_tools")
        if agent_id not in _REQUIRED_TOOLS:
            _invalid(f"{context}.agent_id is not a closed agent ID")
        if workspace != f"agents/{agent_id}":
            _invalid(f"{context}.workspace_relative_path must be policy-derived from its agent ID")
        if tools != _REQUIRED_TOOLS[agent_id]:
            _invalid(f"{context}.required_tools does not match the closed tool set")
        agents.append(OpenClawRegistryAgentPolicyV1(agent_id, workspace, tools))
    if tuple(agent.agent_id for agent in agents) != _AGENT_IDS:
        _invalid("registry policy agents must use canonical order without duplicates")
    return OpenClawRegistryPolicyV1(_SCHEMA_VERSION, tuple(agents))


def _decode_json(contents: bytes, context: str) -> object:
    if len(contents) > MAX_RESOURCE_BYTES:
        _invalid(f"{context} exceeds {MAX_RESOURCE_BYTES} bytes")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OpenClawResourceError(f"{context} must be UTF-8") from error

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _invalid(f"{context} contains duplicate field: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: _invalid(f"{context} contains non-finite number: {value}"),
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise OpenClawResourceError(f"{context} is not valid bounded JSON") from error


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _invalid(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _exact_fields(data: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    actual = set(data)
    if actual != expected:
        _invalid(f"{context} fields must be exactly {sorted(expected)}")


def _schema(value: object, field: str) -> int:
    version = _integer(value, field, minimum=1, maximum=1)
    if version != _SCHEMA_VERSION:
        _invalid(f"{field} must be {_SCHEMA_VERSION}")
    return version


def _integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _invalid(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        _invalid(f"{field} must be a non-empty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid(f"{field} must not contain control characters")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _invalid(f"{field} must be a non-empty array")
    result = tuple(_string(item, f"{field}[]") for item in cast(list[object], value))
    if len(set(result)) != len(result):
        _invalid(f"{field} must contain unique values")
    return result


def _relative_path(value: object, field: str) -> str:
    text = _string(value, field)
    path = PurePosixPath(text)
    if (
        "\\" in text
        or path.is_absolute()
        or len(path.parts) < 1
        or path.as_posix() != text
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid(f"{field} must be a canonical safe relative path")
    return text


def _digest(value: object, field: str) -> str:
    digest = _string(value, field)
    if _SHA256.fullmatch(digest) is None:
        _invalid(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _invalid(message: str) -> NoReturn:
    raise OpenClawResourceError(message)
