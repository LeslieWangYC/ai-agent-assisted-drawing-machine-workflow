from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import drawingmachine.resources.openclaw as openclaw_resources
from drawingmachine.cli.parser import CliUsageError, build_parser
from drawingmachine.resources.openclaw import (
    MAX_RESOURCE_BYTES,
    OpenClawResourceError,
    _validate_openclaw_package,
    load_openclaw_registry_policy,
    load_openclaw_resource_manifest,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPOSITORY_ROOT / "src/drawingmachine/resources/openclaw"
_AGENT_IDS = ("cnc-drawable-translator", "cnc-drawable-router")
_MANAGED_SOURCES = tuple(f"agents/{agent_id}/{name}" for agent_id in _AGENT_IDS for name in ("AGENTS.md", "TOOLS.md"))
_FORMER_REPOSITORY_DUPLICATES = (
    *tuple(f"openclaw_agents/{source.removeprefix('agents/')}" for source in _MANAGED_SOURCES),
    "openclaw_agents/registry/cnc_drawable_agents.json",
)


def _package_fixture() -> tuple[dict[str, object], dict[str, bytes]]:
    manifest = cast(dict[str, object], json.loads((_PACKAGE_ROOT / "manifest.json").read_bytes()))
    sources = cast(list[dict[str, object]], manifest["resources"])
    resources = {
        cast(str, item["source"]): (_PACKAGE_ROOT / cast(str, item["source"])).read_bytes() for item in sources
    }
    return manifest, resources


def _validate(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    _validate_openclaw_package(json.dumps(manifest).encode(), resources)


def _assert_no_repository_duplicates(root: Path) -> None:
    assert [path for path in _FORMER_REPOSITORY_DUPLICATES if (root / path).exists()] == []


def test_loader_returns_closed_exact_manifest_and_registry_policy() -> None:
    manifest = load_openclaw_resource_manifest()
    policy = load_openclaw_registry_policy()

    assert manifest.schema_version == 1
    assert manifest.agent_ids == _AGENT_IDS
    assert tuple(resource.source for resource in manifest.resources) == (*_MANAGED_SOURCES, "registry-policy.json")
    assert tuple(resource.kind for resource in manifest.resources) == (
        "MANAGED_TARGET",
        "MANAGED_TARGET",
        "MANAGED_TARGET",
        "MANAGED_TARGET",
        "CHECK_ONLY_REGISTRY_POLICY",
    )
    assert tuple(resource.destination for resource in manifest.resources[:-1]) == _MANAGED_SOURCES
    assert all(resource.target_mode == 0o640 for resource in manifest.resources[:-1])
    assert manifest.resources[-1].destination is None
    assert manifest.resources[-1].target_mode is None

    assert policy.schema_version == 1
    assert tuple(agent.agent_id for agent in policy.agents) == _AGENT_IDS
    assert tuple(agent.workspace_relative_path for agent in policy.agents) == tuple(
        f"agents/{agent_id}" for agent_id in _AGENT_IDS
    )
    assert policy.agents[0].required_tools == ("image", "read", "write", "exec", "sessions_spawn", "subagents")
    assert policy.agents[1].required_tools == ("image", "read", "write", "exec")


def test_manifest_declares_exact_canonical_d6_bytes() -> None:
    manifest = load_openclaw_resource_manifest()
    by_source = {resource.source: resource for resource in manifest.resources}
    for source, resource in by_source.items():
        contents = (_PACKAGE_ROOT / source).read_bytes()
        assert resource.size_bytes == len(contents) <= MAX_RESOURCE_BYTES
        assert resource.sha256 == hashlib.sha256(contents).hexdigest()
        contents.decode("utf-8")
    _assert_no_repository_duplicates(_REPOSITORY_ROOT)


def test_duplicate_resource_absence_check_rejects_registry_copy(tmp_path: Path) -> None:
    registry = tmp_path / "openclaw_agents/registry/cnc_drawable_agents.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_no_repository_duplicates(tmp_path)


def _unknown_root(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    manifest["unexpected"] = True


def _unknown_resource(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["unexpected"] = True


def _duplicate_agent(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    manifest["agent_ids"] = [_AGENT_IDS[0], _AGENT_IDS[0]]


def _duplicate_source(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    items = cast(list[dict[str, object]], manifest["resources"])
    items[1]["source"] = items[0]["source"]


def _duplicate_destination(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    items = cast(list[dict[str, object]], manifest["resources"])
    items[1]["destination"] = items[0]["destination"]


def _check_only_destination(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[-1]["destination"] = "agents/policy.json"


def _managed_without_destination(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    del cast(list[dict[str, object]], manifest["resources"])[0]["destination"]


def _absolute_source(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["source"] = "/tmp/AGENTS.md"


def _escaping_destination(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["destination"] = "agents/../AGENTS.md"


def _control_path(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["source"] = "agents/bad\nname/AGENTS.md"


def _wrong_digest(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["sha256"] = "0" * 64


def _oversize_bytes(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    source = cast(str, cast(list[dict[str, object]], manifest["resources"])[0]["source"])
    resources[source] = b"x" * (MAX_RESOURCE_BYTES + 1)


def _alias_source(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    cast(list[dict[str, object]], manifest["resources"])[0]["source"] = "agents//cnc-drawable-translator/AGENTS.md"


def _unlisted_resource(manifest: dict[str, object], resources: dict[str, bytes]) -> None:
    resources["agents/cnc-drawable-router/SECRET.md"] = b"not declared"


@pytest.mark.parametrize(
    "mutate",
    (
        _unknown_root,
        _unknown_resource,
        _duplicate_agent,
        _duplicate_source,
        _duplicate_destination,
        _check_only_destination,
        _managed_without_destination,
        _absolute_source,
        _escaping_destination,
        _control_path,
        _wrong_digest,
        _oversize_bytes,
        _alias_source,
        _unlisted_resource,
    ),
)
def test_hostile_manifest_and_resource_fixtures_fail_closed(
    mutate: Callable[[dict[str, object], dict[str, bytes]], None],
) -> None:
    manifest, resources = _package_fixture()
    mutate(manifest, resources)
    with pytest.raises(OpenClawResourceError):
        _validate(manifest, resources)


def test_hostile_registry_policy_fixtures_fail_closed() -> None:
    manifest, resources = _package_fixture()
    policy = cast(dict[str, object], json.loads(resources["registry-policy.json"]))
    agents = cast(list[dict[str, object]], policy["agents"])
    hostile = (
        {**policy, "managed_root": "/tmp/forbidden"},
        {**policy, "unexpected": True},
        {**policy, "agents": [agents[0], agents[0]]},
        {**policy, "agents": [{**agents[0], "workspace_relative_path": "/tmp/agent"}, agents[1]]},
        {**policy, "agents": [{**agents[0], "workspace_relative_path": "agents/../agent"}, agents[1]]},
        {**policy, "agents": [{**agents[0], "required_tools": ["read", "read"]}, agents[1]]},
    )
    for invalid in hostile:
        changed_resources = copy.copy(resources)
        changed_resources["registry-policy.json"] = json.dumps(invalid).encode()
        registry_item = cast(list[dict[str, object]], manifest["resources"])[-1]
        registry_item["size_bytes"] = len(changed_resources["registry-policy.json"])
        registry_item["sha256"] = hashlib.sha256(changed_resources["registry-policy.json"]).hexdigest()
        with pytest.raises(OpenClawResourceError):
            _validate(manifest, changed_resources)


def test_loader_rejects_symlink_like_packaged_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "openclaw"
    root.mkdir()
    (root / "manifest.json").write_bytes((_PACKAGE_ROOT / "manifest.json").read_bytes())
    (root / "registry-policy.json").symlink_to(_PACKAGE_ROOT / "registry-policy.json")
    monkeypatch.setattr(openclaw_resources.importlib.resources, "files", lambda package: root)

    with pytest.raises(OpenClawResourceError, match="symlink"):
        load_openclaw_resource_manifest()


def test_resource_collection_rejects_nonfiles_and_bounded_reader_rejects_oversize(
    tmp_path: Path,
) -> None:
    class NonFile:
        name = "device"

        def is_dir(self) -> bool:
            return False

        def is_file(self) -> bool:
            return False

    class Root:
        def iterdir(self) -> tuple[NonFile, ...]:
            return (NonFile(),)

    with pytest.raises(OpenClawResourceError, match="regular file"):
        openclaw_resources._collect_packaged_resources(Root())  # type: ignore[arg-type]

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
    with pytest.raises(OpenClawResourceError, match="exceeds"):
        openclaw_resources._read_bounded(oversized, "oversized")


def test_manifest_rejects_every_closed_shape_mismatch() -> None:
    def rejects(mutate: Callable[[dict[str, object], dict[str, object]], None]) -> None:
        manifest, byte_resources = _package_fixture()
        resources = cast(dict[str, object], byte_resources)
        mutate(manifest, resources)
        with pytest.raises(OpenClawResourceError):
            _validate_openclaw_package(json.dumps(manifest).encode(), resources)  # type: ignore[arg-type]

    rejects(lambda manifest, _resources: manifest.__setitem__("agent_ids", list(reversed(_AGENT_IDS))))
    rejects(lambda manifest, _resources: manifest.__setitem__("resources", {}))
    rejects(lambda manifest, _resources: cast(list[object], manifest["resources"]).pop())

    def swap_sources(manifest: dict[str, object], _resources: dict[str, object]) -> None:
        items = cast(list[dict[str, object]], manifest["resources"])
        items[0]["source"], items[1]["source"] = items[1]["source"], items[0]["source"]

    def swap_destinations(manifest: dict[str, object], _resources: dict[str, object]) -> None:
        items = cast(list[dict[str, object]], manifest["resources"])
        items[0]["destination"], items[1]["destination"] = items[1]["destination"], items[0]["destination"]

    rejects(swap_sources)
    rejects(swap_destinations)
    rejects(
        lambda manifest, resources: resources.__setitem__(
            cast(str, cast(list[dict[str, object]], manifest["resources"])[0]["source"]), "not bytes"
        )
    )
    rejects(
        lambda manifest, _resources: cast(list[dict[str, object]], manifest["resources"])[0].__setitem__(
            "size_bytes", 1
        )
    )
    rejects(
        lambda manifest, _resources: cast(list[dict[str, object]], manifest["resources"])[0].__setitem__(
            "target_mode", 0o600
        )
    )
    rejects(
        lambda manifest, _resources: cast(list[dict[str, object]], manifest["resources"])[0].__setitem__(
            "kind", "UNKNOWN"
        )
    )
    rejects(
        lambda manifest, _resources: cast(list[dict[str, object]], manifest["resources"])[0].__setitem__(
            "media_type", "application/json"
        )
    )


def test_registry_policy_rejects_each_derived_authority_mismatch() -> None:
    def rejects(mutate: Callable[[dict[str, object]], None]) -> None:
        manifest, resources = _package_fixture()
        policy = cast(dict[str, object], json.loads(resources["registry-policy.json"]))
        mutate(policy)
        contents = json.dumps(policy).encode()
        resources["registry-policy.json"] = contents
        item = cast(list[dict[str, object]], manifest["resources"])[-1]
        item["size_bytes"] = len(contents)
        item["sha256"] = hashlib.sha256(contents).hexdigest()
        with pytest.raises(OpenClawResourceError):
            _validate(manifest, resources)

    rejects(lambda policy: policy.__setitem__("agents", {}))
    rejects(lambda policy: cast(list[dict[str, object]], policy["agents"])[0].__setitem__("agent_id", "unknown"))
    rejects(
        lambda policy: cast(list[dict[str, object]], policy["agents"])[0].__setitem__(
            "workspace_relative_path", "agents/cnc-drawable-router"
        )
    )
    rejects(lambda policy: cast(list[dict[str, object]], policy["agents"])[0].__setitem__("required_tools", ["read"]))


def test_json_and_scalar_contract_helpers_reject_all_invalid_shapes() -> None:
    with pytest.raises(OpenClawResourceError, match="exceeds"):
        openclaw_resources._decode_json(b" " * (MAX_RESOURCE_BYTES + 1), "oversized")
    with pytest.raises(OpenClawResourceError, match="duplicate"):
        openclaw_resources._decode_json(b'{"same": 1, "same": 2}', "duplicate")
    with pytest.raises(OpenClawResourceError, match="JSON object"):
        openclaw_resources._object([], "object")

    class UnequalOne(int):
        def __eq__(self, other: object) -> bool:
            return False

        def __ne__(self, other: object) -> bool:
            return True

    with pytest.raises(OpenClawResourceError, match="must be 1"):
        openclaw_resources._schema(UnequalOne(1), "schema")
    with pytest.raises(OpenClawResourceError, match="integer"):
        openclaw_resources._integer(True, "integer", minimum=1, maximum=1)
    with pytest.raises(OpenClawResourceError, match="bounded string"):
        openclaw_resources._string("", "string")
    with pytest.raises(OpenClawResourceError, match="non-empty array"):
        openclaw_resources._string_list([], "strings")
    with pytest.raises(OpenClawResourceError, match="SHA-256"):
        openclaw_resources._digest("A" * 64, "digest")


def test_d5_exposes_only_closed_openclaw_check_and_install_commands() -> None:
    parser = build_parser()
    for command in ("check", "install"):
        arguments = parser.parse_args(["openclaw", command, "--openclaw-root", "/tmp/openclaw"])
        assert arguments.command_name == f"openclaw.{command}"
        assert arguments.openclaw_root == ["/tmp/openclaw"]
    for arguments in (
        ["openclaw", "install"],
        ["openclaw", "reload", "--openclaw-root", "/tmp/openclaw"],
        ["openclaw", "check", "--openclaw-root", "/tmp/openclaw", "--check-registry"],
    ):
        with pytest.raises(CliUsageError):
            parser.parse_args(arguments)


def test_installed_loader_uses_wheel_provenance_and_ignores_ambient_openclaw_state(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    canary = "D2_AMBIENT_SECRET_MUST_NOT_BE_READ"
    for name in ("openclaw.json", "secrets.json", "MEMORY.md", "IDENTITY.md", "PROMPT.md", "session.json"):
        (ambient / name).write_text(canary, encoding="utf-8")
    environment = os.environ.copy()
    environment.update({"HOME": str(ambient), "XDG_CONFIG_HOME": str(ambient), "XDG_STATE_HOME": str(ambient)})
    result = subprocess.run(
        [
            str(installed_drawingmachine.parent / "python"),
            "-I",
            "-c",
            (
                "import json; from drawingmachine.resources.openclaw import load_openclaw_resource_manifest as load; "
                "manifest=load(); print(json.dumps({'agents': manifest.agent_ids, 'sources': "
                "[item.source for item in manifest.resources], 'module': __import__('drawingmachine.resources.openclaw', "
                "fromlist=['x']).__file__}))"
            ),
        ],
        cwd=ambient,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = cast(dict[str, object], json.loads(result.stdout))
    assert tuple(cast(list[str], output["agents"])) == _AGENT_IDS
    assert tuple(cast(list[str], output["sources"])) == (*_MANAGED_SOURCES, "registry-policy.json")
    module = Path(cast(str, output["module"])).resolve()
    assert "site-packages" in module.parts and not module.is_relative_to(_REPOSITORY_ROOT)
    assert canary not in result.stdout + result.stderr
