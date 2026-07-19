from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import runpy
import socket
import stat
import struct
import sys
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

import drawingmachine.install.client_endpoint as endpoint_subject
from drawingmachine.config import load_config, resolve_xdg_paths
from drawingmachine.config.openclaw_deployment import (
    PosixGroupV1,
    PosixPrincipalV1,
    parse_openclaw_deployment_policy,
)
from drawingmachine.config.service_access import ServiceAccessConfigV1, parse_service_access_config
from drawingmachine.install.client_endpoint import EndpointAclEntryV1, EndpointFactsV1, rebuild_client_endpoint


def _resource_bytes(relative: str) -> bytes:
    return files("drawingmachine.resources").joinpath(relative).read_bytes()


def test_install_manifest_is_closed_complete_and_digest_bound() -> None:
    raw = _resource_bytes("install/manifest.json")
    assert hashlib.sha256(raw).hexdigest() == endpoint_subject._INSTALL_MANIFEST_SHA256
    document = json.loads(raw)
    assert set(document) == {"schema_version", "resources", "managed_paths", "limits", "render"}
    assert document["schema_version"] == 1
    entries = document["resources"]
    assert isinstance(entries, list)
    expected = {
        "systemd/drawingmachine.service",
        "systemd/drawingmachine-client-endpoint.service",
        "install/service-access.toml.example",
        "install/openclaw-deployment.toml.example",
        "install/config/application.toml.example",
        "install/config/machines/default.toml.example",
        "install/config/providers/local-comfyui.toml.example",
        "install/config/providers/local-comfyui-workflow.json.example",
    }
    assert {entry["resource"] for entry in entries} == expected
    assert len(entries) == len(expected)
    allowed_keys = {"resource", "destination", "creator", "owner", "group", "mode", "sha256", "purpose"}
    for entry in entries:
        assert set(entry) == allowed_keys
        assert entry["creator"] in {"cutover", "service-runtime", "client-runtime-helper"}
        assert entry["mode"] in {"0640", "0644"}
        assert hashlib.sha256(_resource_bytes(entry["resource"])).hexdigest() == entry["sha256"]
        assert entry["destination"].startswith(("%h/", "/home/drawingmachine-service/"))
    by_resource = {entry["resource"]: entry for entry in entries}
    assert (
        by_resource["install/config/application.toml.example"]["destination"]
        == "/home/drawingmachine-service/.config/drawingmachine/config.toml"
    )
    managed = document["managed_paths"]
    managed_keys = {"path", "kind", "creator", "owner", "group", "mode", "access", "default_access", "automatic"}
    assert all(set(entry) == managed_keys | ({"sha256"} if entry["kind"] == "regular" else set()) for entry in managed)
    assert len({entry["path"] for entry in managed}) == len(managed)
    assert all(entry["creator"] in {"cutover", "service-runtime", "client-runtime-helper"} for entry in managed)
    by_path = {entry["path"]: entry for entry in managed}
    assert by_path["@IMAGE_IMPORT_ROOT@/prepare"]["mode"] == "2770"
    assert by_path["@IMAGE_IMPORT_ROOT@/drop"]["access"] == "automation:rwx"
    assert by_path["@IMAGE_IMPORT_ROOT@/admitted"]["mode"] == "0700"
    assert by_path["@AUTOMATION_EXPORT_ROOT@/jobs"]["access"] == "automation:r-x"
    assert by_path["@AUTOMATION_RUNTIME_ENDPOINT@"]["creator"] == "client-runtime-helper"
    assert by_path["@AUTOMATION_IMPORT_ENDPOINT@"]["automatic"] is False
    assert by_path["@OPERATOR_RUNTIME_ENDPOINT@"]["creator"] == "client-runtime-helper"
    assert "@OPERATOR_IMPORT_ENDPOINT@" not in by_path
    assert document["limits"] == {
        "max_entries_per_role_phase": 32,
        "max_export_artifacts_per_job": 16,
        "lease_max_bytes": 16384,
        "image_max_payload_bytes": 33554432,
        "gcode_max_payload_bytes": 16777216,
    }


def test_manifest_freezes_exact_static_and_runtime_data_plane_creators() -> None:
    document = json.loads(_resource_bytes("install/manifest.json"))
    by_path = {entry["path"]: entry for entry in document["managed_paths"]}
    expected_static = {
        "@AUTOMATION_IMPORT_ROOT@": ("2770", "automation:rwx", "automation:rwx"),
        "@AUTOMATION_EXPORT_ROOT@": ("2770", "automation:r-x", "automation:r-x"),
        "@AUTOMATION_EXPORT_ROOT@/jobs": ("2750", "automation:r-x", "automation:r-x"),
    }
    for role in ("IMAGE", "GCODE"):
        expected_static[f"@{role}_IMPORT_ROOT@"] = ("2770", "automation:rwx", "automation:rwx")
        for phase in ("prepare", "drop"):
            expected_static[f"@{role}_IMPORT_ROOT@/{phase}"] = ("2770", "automation:rwx", "automation:rwx")
        for phase in ("admitted", "quarantine", "observations"):
            expected_static[f"@{role}_IMPORT_ROOT@/{phase}"] = ("0700", "service:rwx", "none")
    for path, (mode, access, default_access) in expected_static.items():
        assert by_path[path] == {
            "path": path,
            "kind": "directory",
            "creator": "cutover",
            "owner": "service",
            "group": "connect" if mode != "0700" else "service",
            "mode": mode,
            "access": access,
            "default_access": default_access,
            "automatic": False,
        }
    assert by_path["@AUTOMATION_EXPORT_ROOT@/jobs/<job-id>"]["creator"] == "service-runtime"
    assert by_path["@AUTOMATION_EXPORT_ROOT@/jobs/<job-id>/<relative-path>"]["creator"] == "service-runtime"
    assert "@IMPORT_QUARANTINE_ROOT@" not in by_path
    assert "@IMPORT_OBSERVATION_ROOT@" not in by_path


def test_manifest_names_exact_openclaw_targets_and_binds_d6_digests() -> None:
    install = json.loads(_resource_bytes("install/manifest.json"))
    openclaw = json.loads(_resource_bytes("openclaw/manifest.json"))
    expected = {
        f"@OPENCLAW_MANAGED_ROOT@/{entry['destination']}": entry["sha256"]
        for entry in openclaw["resources"]
        if entry["kind"] == "MANAGED_TARGET"
    }
    managed = {
        entry["path"]: entry["sha256"]
        for entry in install["managed_paths"]
        if entry["path"].startswith("@OPENCLAW_MANAGED_ROOT@/agents/") and entry["kind"] == "regular"
    }
    assert managed == expected
    assert "@OPENCLAW_MANAGED_ROOT@/agents/*/*.md" not in {entry["path"] for entry in install["managed_paths"]}


def _valid_render_replacements() -> tuple[tuple[str, str], ...]:
    return (
        ("SERVICE_UID", "4100"),
        ("SERVICE_GID", "4100"),
        ("AUTOMATION_UID", "4200"),
        ("AUTOMATION_GID", "4200"),
        ("OPERATOR_UID", "4300"),
        ("OPERATOR_GID", "4300"),
        ("CONNECT_GID", "4400"),
        ("AUTOMATION_READ_GID", "4500"),
        ("REGISTRY_OWNER_UID", "4600"),
        ("REGISTRY_OWNER_GID", "4600"),
        ("AUTOMATION_USER", "drawingmachine-automation"),
        ("OPENCLAW_REGISTRY_ROOT", "/srv/openclaw-registry"),
        ("OPENCLAW_MANAGED_ROOT", "/srv/drawingmachine-openclaw"),
        ("SERIAL_DEVICE", "/dev/serial/by-id/example-controller"),
    )


def test_production_renderer_closes_placeholders_and_validates_units_and_policies() -> None:
    renderer = endpoint_subject.render_install_resources
    rendered = renderer(_valid_render_replacements())
    by_resource = {entry.resource: entry for entry in rendered}
    assert len(by_resource) == 8
    assert all(b"@" not in entry.contents for entry in rendered)
    assert all(entry.source_sha256 == hashlib.sha256(_resource_bytes(entry.resource)).hexdigest() for entry in rendered)
    assert all(entry.sha256 == hashlib.sha256(entry.contents).hexdigest() for entry in rendered)
    service_unit = by_resource["systemd/drawingmachine.service"].contents.decode()
    client_unit = by_resource["systemd/drawingmachine-client-endpoint.service"].contents.decode()
    service_policy = by_resource["install/service-access.toml.example"].contents
    openclaw_policy = by_resource["install/openclaw-deployment.toml.example"].contents
    assert "ExecStart=%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run" in service_unit
    assert "ReadOnlyPaths=%h/.config/drawingmachine /srv/openclaw-registry" in service_unit
    assert "ReadWritePaths=" in service_unit and "/srv/drawingmachine-openclaw" in service_unit
    assert hashlib.sha256(service_policy).hexdigest() in client_unit
    assert "--policy /home/drawingmachine-service/.config/drawingmachine/service-access.toml" in client_unit
    parse_service_access_config(service_policy)
    parse_openclaw_deployment_policy(openclaw_policy, source_sha256=hashlib.sha256(openclaw_policy).hexdigest())


@pytest.mark.parametrize(
    "replacements",
    [
        _valid_render_replacements()[:-1],
        (*_valid_render_replacements(), ("EXTRA", "value")),
        (*_valid_render_replacements(), ("SERVICE_UID", "4101")),
        tuple(
            (name, "relative" if name == "OPENCLAW_REGISTRY_ROOT" else value)
            for name, value in _valid_render_replacements()
        ),
        tuple(
            (name, '/srv/managed"\nReadWritePaths=/tmp' if name == "OPENCLAW_MANAGED_ROOT" else value)
            for name, value in _valid_render_replacements()
        ),
        tuple(
            (name, '/dev/serial/by-id/controller"\nunsafe = true' if name == "SERIAL_DEVICE" else value)
            for name, value in _valid_render_replacements()
        ),
    ],
)
def test_production_renderer_rejects_missing_extra_duplicate_and_invalid_replacements(
    replacements: tuple[tuple[str, str], ...],
) -> None:
    renderer = endpoint_subject.render_install_resources
    with pytest.raises(ValueError):
        renderer(replacements)


@pytest.mark.parametrize("fault", ["fixed-executable", "unknown-resource", "unknown-managed-path"])
def test_production_renderer_rejects_tampered_unit_and_manifest_namespaces(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_resource = endpoint_subject._packaged_resource
    manifest = json.loads(real_resource("install/manifest.json"))
    overrides: dict[str, bytes] = {}
    if fault == "fixed-executable":
        resource = "systemd/drawingmachine.service"
        overrides[resource] = real_resource(resource).replace(
            b"ExecStart=%h/.local/lib/drawingmachine/venv/bin/drawingmachine service run",
            b"ExecStart=/tmp/drawingmachine service run",
        )
        next(entry for entry in manifest["resources"] if entry["resource"] == resource)["sha256"] = hashlib.sha256(
            overrides[resource]
        ).hexdigest()
    elif fault == "unknown-resource":
        resource = "install/extra.example"
        overrides[resource] = b"fixed\n"
        extra = dict(manifest["resources"][0])
        extra.update(
            resource=resource,
            destination="/home/drawingmachine-service/.config/drawingmachine/extra",
            sha256=hashlib.sha256(overrides[resource]).hexdigest(),
        )
        manifest["resources"].append(extra)
    else:
        extra = dict(manifest["managed_paths"][0])
        extra["path"] = "@UNDECLARED_ROOT@"
        manifest["managed_paths"].append(extra)
    overrides["install/manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()

    def tampered(relative: str) -> bytes:
        return overrides[relative] if relative in overrides else real_resource(relative)

    monkeypatch.setattr(endpoint_subject, "_packaged_resource", tampered)
    with pytest.raises(ValueError):
        endpoint_subject.render_install_resources(_valid_render_replacements())


@pytest.mark.parametrize(
    "fault",
    [
        "unit-user-and-digest",
        "resource-destination",
        "resource-creator",
        "resource-mode",
        "managed-mode",
        "managed-access",
        "managed-default-access",
    ],
)
def test_production_renderer_rejects_self_authorized_manifest_drift(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_resource = endpoint_subject._packaged_resource
    manifest = json.loads(real_resource("install/manifest.json"))
    overrides: dict[str, bytes] = {}
    service_entry = next(
        entry for entry in manifest["resources"] if entry["resource"] == "systemd/drawingmachine.service"
    )
    prepare_entry = next(entry for entry in manifest["managed_paths"] if entry["path"] == "@IMAGE_IMPORT_ROOT@/prepare")
    if fault == "unit-user-and-digest":
        unit = real_resource("systemd/drawingmachine.service").replace(b"Type=simple\n", b"Type=simple\nUser=root\n")
        overrides["systemd/drawingmachine.service"] = unit
        service_entry["sha256"] = hashlib.sha256(unit).hexdigest()
    elif fault == "resource-destination":
        service_entry["destination"] = "/tmp/drawingmachine.service"
    elif fault == "resource-creator":
        service_entry["creator"] = "service-runtime"
    elif fault == "resource-mode":
        service_entry["mode"] = "0600"
    elif fault == "managed-mode":
        prepare_entry["mode"] = "2775"
    elif fault == "managed-access":
        prepare_entry["access"] = "automation:r-x"
    else:
        prepare_entry["default_access"] = "none"
    overrides["install/manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()

    monkeypatch.setattr(
        endpoint_subject,
        "_packaged_resource",
        lambda relative: overrides.get(relative, real_resource(relative)),
    )
    with pytest.raises(ValueError, match=r"manifest.*digest"):
        endpoint_subject.render_install_resources(_valid_render_replacements())


def _authorize_test_manifest(
    monkeypatch: pytest.MonkeyPatch,
    manifest_bytes: bytes,
    overrides: dict[str, bytes] | None = None,
) -> None:
    real_resource = endpoint_subject._packaged_resource
    resources = {"install/manifest.json": manifest_bytes, **(overrides or {})}

    def packaged(relative: str) -> bytes:
        return resources[relative] if relative in resources else real_resource(relative)

    monkeypatch.setattr(endpoint_subject, "_INSTALL_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest())
    monkeypatch.setattr(endpoint_subject, "_packaged_resource", packaged)


@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "oversize",
        "malformed-json",
        "non-object",
        "fields",
        "schema-version",
        "render-version",
        "declarations",
        "limits",
        "resources-type",
        "resources-empty",
        "digest-text",
        "resource-digest",
        "unknown-placeholder",
        "duplicate-resource",
        "unused-placeholder",
        "leftover-placeholder",
        "aggregate-bound",
    ],
)
def test_renderer_defensive_manifest_and_resource_branches(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_resource = endpoint_subject._packaged_resource
    manifest = json.loads(real_resource("install/manifest.json"))
    overrides: dict[str, bytes] = {}
    if fault == "absent":
        raw = b""
    elif fault == "oversize":
        raw = b"x" * (endpoint_subject._MAX_INSTALL_MANIFEST_BYTES + 1)
    elif fault == "malformed-json":
        raw = b"{"
    elif fault == "non-object":
        raw = b"[]"
    else:
        if fault == "fields":
            del manifest["limits"]
        elif fault == "schema-version":
            manifest["schema_version"] = 2
        elif fault == "render-version":
            manifest["render"]["schema_version"] = 2
        elif fault == "declarations":
            manifest["render"]["inputs"].pop()
        elif fault == "limits":
            manifest["limits"]["lease_max_bytes"] += 1
        elif fault == "resources-type":
            manifest["resources"] = {}
        elif fault == "resources-empty":
            manifest["resources"] = []
        elif fault == "digest-text":
            manifest["resources"][0]["sha256"] = "not-a-digest"
        elif fault == "resource-digest":
            overrides[manifest["resources"][0]["resource"]] = b"drifted"
        elif fault == "unknown-placeholder":
            entry = manifest["resources"][0]
            contents = real_resource(entry["resource"]) + b"@UNKNOWN_PLACEHOLDER@"
            overrides[entry["resource"]] = contents
            entry["sha256"] = hashlib.sha256(contents).hexdigest()
        elif fault == "duplicate-resource":
            manifest["resources"].append(dict(manifest["resources"][0]))
        elif fault == "unused-placeholder":
            entry = next(
                item
                for item in manifest["resources"]
                if item["resource"] == "install/config/machines/default.toml.example"
            )
            contents = real_resource(entry["resource"]).replace(b"@SERIAL_DEVICE@", b"/dev/serial/by-id/fixed")
            overrides[entry["resource"]] = contents
            entry["sha256"] = hashlib.sha256(contents).hexdigest()
        elif fault == "aggregate-bound":
            manifest["render"]["max_total_rendered_bytes"] = 1
        raw = json.dumps(manifest, separators=(",", ":")).encode()
    _authorize_test_manifest(monkeypatch, raw, overrides)
    if fault == "leftover-placeholder":
        real_pattern = endpoint_subject._PLACEHOLDER

        class _AlwaysLeftover:
            finditer = real_pattern.finditer

            @staticmethod
            def search(value: bytes) -> object:
                return object()

        monkeypatch.setattr(endpoint_subject, "_PLACEHOLDER", _AlwaysLeftover())
    with pytest.raises((ValueError, UnicodeDecodeError, json.JSONDecodeError)):
        endpoint_subject.render_install_resources(_valid_render_replacements())


@pytest.mark.parametrize(
    "replacements",
    [
        [],
        (("SERVICE_UID",),),
        (["SERVICE_UID", "4100"],),
        tuple((1 if name == "SERVICE_UID" else name, value) for name, value in _valid_render_replacements()),
        tuple((name, 1 if name == "SERVICE_UID" else value) for name, value in _valid_render_replacements()),
    ],
)
def test_renderer_rejects_non_exact_replacement_container_and_scalar_types(replacements: object) -> None:
    with pytest.raises(ValueError):
        endpoint_subject.render_install_resources(replacements)  # type: ignore[arg-type]


@pytest.mark.parametrize("relative", ["", "/absolute", "install/../manifest.json"])
def test_packaged_resource_rejects_noncanonical_names(relative: str) -> None:
    with pytest.raises(ValueError, match="canonical relative"):
        endpoint_subject._packaged_resource(relative)


def test_exact_manifest_scalar_helpers_reject_every_invalid_shape() -> None:
    for value in (None, {"extra": 1}):
        with pytest.raises(ValueError, match="fields"):
            endpoint_subject._exact_object(value, {"field"}, "object")
    for value in (None, "", "x" * 4097):
        with pytest.raises(ValueError, match="bounded exact text"):
            endpoint_subject._exact_text(value, "text")
    for value in (True, 0, 2):
        with pytest.raises(ValueError, match="outside its bound"):
            endpoint_subject._bounded_positive_int(value, 1, "integer")


@pytest.mark.parametrize(
    ("value", "derived"),
    [
        (None, False),
        ([], False),
        ([{"name": "bad-name", "kind": "posix-id"}], False),
        ([{"name": "NAME", "kind": "unknown"}], False),
        ([{"name": "NAME", "kind": "unknown", "resource": "resource"}], True),
        ([{"name": "NAME", "kind": "resource-sha256", "resource": "resource"}] * 2, True),
    ],
)
def test_render_declarations_reject_invalid_shapes(value: object, derived: bool) -> None:
    with pytest.raises(ValueError):
        endpoint_subject._render_declarations(value, derived=derived)


@pytest.mark.parametrize(
    ("kind", "value", "maximum"),
    [
        ("posix-id", "", 16),
        ("posix-id", "12", 1),
        ("posix-id", "1@", 16),
        ("posix-id", "1\x00", 16),
        ("posix-id", "0", 16),
        ("posix-id", "2147483648", 16),
        ("posix-name", "UPPER", 16),
        ("absolute-path", "relative", 16),
        ("absolute-path", "/srv//root", 16),
        ("absolute-path", "/srv/bad name", 32),
        ("serial-device", "/dev/ttyS0", 32),
    ],
)
def test_render_input_rejects_every_invalid_scalar_branch(kind: str, value: str, maximum: int) -> None:
    with pytest.raises(ValueError):
        endpoint_subject._validate_render_input("VALUE", kind, value, maximum)


@pytest.mark.parametrize(
    "fault",
    [
        "not-list",
        "non-object-entry",
        "creator",
        "automatic",
        "target-digest",
        "duplicate-path",
        "openclaw-not-object",
        "openclaw-resources",
        "openclaw-entry",
        "openclaw-target-mismatch",
    ],
)
def test_managed_manifest_rejects_every_invalid_authority_branch(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    manifest = json.loads(_resource_bytes("install/manifest.json"))
    managed = manifest["managed_paths"]
    openclaw: object = json.loads(_resource_bytes("openclaw/manifest.json"))
    if fault == "not-list":
        managed = {}
    elif fault == "non-object-entry":
        managed[0] = None
    elif fault == "creator":
        managed[0]["creator"] = "unknown"
    elif fault == "automatic":
        managed[0]["automatic"] = 1
    elif fault == "target-digest":
        target = next(item for item in managed if item["kind"] == "regular")
        target["sha256"] = "bad"
    elif fault == "duplicate-path":
        managed.append(dict(managed[0]))
    elif fault == "openclaw-not-object":
        openclaw = []
    elif fault == "openclaw-resources":
        openclaw["resources"] = {}  # type: ignore[index]
    elif fault == "openclaw-entry":
        openclaw["resources"][0] = None  # type: ignore[index]
    else:
        target = next(item for item in openclaw["resources"] if item.get("kind") == "MANAGED_TARGET")  # type: ignore[index]
        target["sha256"] = "0" * 64
    real_resource = endpoint_subject._packaged_resource
    monkeypatch.setattr(
        endpoint_subject,
        "_packaged_resource",
        lambda relative: (
            json.dumps(openclaw).encode() if relative == "openclaw/manifest.json" else real_resource(relative)
        ),
    )
    with pytest.raises(ValueError):
        endpoint_subject._validate_managed_manifest(managed)


def _valid_rendered_authority() -> tuple[tuple[endpoint_subject.RenderedInstallResourceV1, ...], dict[str, str]]:
    rendered = endpoint_subject.render_install_resources(_valid_render_replacements())
    by_resource = {item.resource: item for item in rendered}
    derived = {
        "OPENCLAW_MANIFEST_SHA256": hashlib.sha256(_resource_bytes("openclaw/manifest.json")).hexdigest(),
        "OPENCLAW_REGISTRY_POLICY_SHA256": hashlib.sha256(_resource_bytes("openclaw/registry-policy.json")).hexdigest(),
        "SERVICE_ACCESS_SHA256": hashlib.sha256(
            by_resource["install/service-access.toml.example"].contents
        ).hexdigest(),
    }
    return rendered, derived


def _replace_rendered(
    rendered: tuple[endpoint_subject.RenderedInstallResourceV1, ...],
    resource: str,
    contents: bytes,
) -> tuple[endpoint_subject.RenderedInstallResourceV1, ...]:
    return tuple(replace(item, contents=contents) if item.resource == resource else item for item in rendered)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "service-exec",
        "client-exec",
        "mutable-exec",
        "placeholder",
        "sandbox-relative",
        "service-policy-path",
        "service-policy-digest",
        "openclaw-manifest-digest",
        "openclaw-registry-digest",
        "openclaw-registry-relative",
        "openclaw-managed-relative",
        "openclaw-readonly-root",
        "openclaw-writable-root",
        "openclaw-no-writable-roots",
    ],
)
def test_rendered_install_rejects_every_cross_resource_authority_drift(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    rendered, derived = _valid_rendered_authority()
    by_resource = {item.resource: item for item in rendered}
    service_name = "systemd/drawingmachine.service"
    client_name = "systemd/drawingmachine-client-endpoint.service"
    service = by_resource[service_name].contents
    client = by_resource[client_name].contents
    if fault == "duplicate":
        rendered = (rendered[0], rendered[0])
    elif fault == "service-exec":
        rendered = _replace_rendered(rendered, service_name, service.replace(b"ExecStart=", b"ExecStart=/tmp/"))
    elif fault == "client-exec":
        rendered = _replace_rendered(rendered, client_name, client.replace(b"ExecStart=", b"ExecStart=/tmp/"))
    elif fault == "mutable-exec":
        rendered = _replace_rendered(rendered, service_name, service + b"Environment=/usr/bin/env\n")
    elif fault == "placeholder":
        rendered = _replace_rendered(rendered, service_name, service + b"Environment=@LEFTOVER@\n")
    elif fault == "sandbox-relative":
        rendered = _replace_rendered(
            rendered,
            service_name,
            service.replace(b"ReadOnlyPaths=%h/.config/drawingmachine", b"ReadOnlyPaths=relative"),
        )
    elif fault == "service-policy-path":
        name = "install/service-access.toml.example"
        rendered = _replace_rendered(
            rendered,
            name,
            by_resource[name].contents.replace(
                b'openclaw_policy_path = "/home/drawingmachine-service/.config/drawingmachine/openclaw-deployment.toml"',
                b'openclaw_policy_path = "/tmp/openclaw.toml"',
            ),
        )
    elif fault == "service-policy-digest":
        derived["SERVICE_ACCESS_SHA256"] = "0" * 64
        rendered = _replace_rendered(
            rendered,
            client_name,
            client.replace(by_resource["install/service-access.toml.example"].sha256.encode(), b"0" * 64),
        )
    elif fault == "openclaw-manifest-digest":
        derived["OPENCLAW_MANIFEST_SHA256"] = "0" * 64
    elif fault == "openclaw-registry-digest":
        derived["OPENCLAW_REGISTRY_POLICY_SHA256"] = "0" * 64
    elif fault in {"openclaw-registry-relative", "openclaw-managed-relative"}:
        real_parser = endpoint_subject.parse_openclaw_deployment_policy

        def relative_root(contents: bytes, *, source_sha256: str) -> object:
            policy = real_parser(contents, source_sha256=source_sha256)
            field = "registry_root" if fault.endswith("registry-relative") else "managed_root"
            return replace(policy, **{field: Path("relative")})

        monkeypatch.setattr(endpoint_subject, "parse_openclaw_deployment_policy", relative_root)
    elif fault == "openclaw-readonly-root":
        rendered = _replace_rendered(
            rendered,
            service_name,
            service.replace(
                b"ReadOnlyPaths=%h/.config/drawingmachine /srv/openclaw-registry",
                b"ReadOnlyPaths=%h/.config/drawingmachine /srv/other",
            ),
        )
    elif fault == "openclaw-writable-root":
        rendered = _replace_rendered(
            rendered, service_name, service.replace(b"/srv/drawingmachine-openclaw", b"/srv/other")
        )
    else:
        rendered = _replace_rendered(
            rendered,
            service_name,
            b"\n".join(line for line in service.splitlines() if not line.startswith(b"ReadWritePaths=")),
        )
    with pytest.raises((ValueError, KeyError)):
        endpoint_subject._validate_rendered_install(rendered, derived)


def test_templates_freeze_principals_roots_modes_acls_quotas_and_no_live_values() -> None:
    service = _resource_bytes("install/service-access.toml.example").decode()
    openclaw = _resource_bytes("install/openclaw-deployment.toml.example").decode()
    combined = service + openclaw
    for placeholder in (
        "@SERVICE_UID@",
        "@AUTOMATION_UID@",
        "@OPERATOR_UID@",
        "@CONNECT_GID@",
        "@AUTOMATION_READ_GID@",
        "@OPENCLAW_REGISTRY_ROOT@",
        "@OPENCLAW_MANAGED_ROOT@",
    ):
        assert placeholder in combined
    assert 'mode = "2770"' in service
    assert 'bundle_mode = "2770"' in service
    assert 'payload_mode = "0640"' in service
    assert 'private_mode = "0700"' in service
    assert "max_entries_per_role_phase = 32" in service
    assert "image_max_payload_bytes = 33554432" in service
    assert "gcode_max_payload_bytes = 16777216" in service
    assert 'automation_named_user_acl = "rwx"' in service
    assert "managed_dir_mode = 1512 # 0o2750" in openclaw
    assert "target_mode = 416 # 0o0640" in openclaw
    assert "registry_root" in openclaw and "managed_root" in openclaw
    assert "automation-read" in openclaw
    for forbidden in ("/dev/tty", "COM", "secret", "token", "approval", "sudo", "chown", "chgrp", "systemctl"):
        assert forbidden.lower() not in combined.lower()


def test_packaged_operational_examples_match_closed_config_shapes() -> None:
    application = _resource_bytes("install/config/application.toml.example").decode()
    machine = _resource_bytes("install/config/machines/default.toml.example").decode()
    provider = _resource_bytes("install/config/providers/local-comfyui.toml.example").decode()
    workflow = json.loads(_resource_bytes("install/config/providers/local-comfyui-workflow.json.example"))
    assert application == (
        'schema_version = 1\nmachine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "INFO"\n'
    )
    assert "schema_version = 1" in machine and "[profile.hardware]" in machine
    assert 'serial_port = "@SERIAL_DEVICE@"' in machine
    assert "schema_version = 1" in provider and 'workflow_template = "local-comfyui-workflow.json"' in provider
    assert workflow["27"]["inputs"]["prompt"]
    assert not any(key.lower() in {"secret", "token", "uid"} for key in workflow)


def _render(contents: str, replacements: dict[str, str]) -> str:
    for placeholder, value in replacements.items():
        contents = contents.replace(f"@{placeholder}@", value)
    assert "@" not in contents
    return contents


def test_rendered_templates_parse_as_exact_frozen_schemas(tmp_path: Path) -> None:
    ids = {
        "SERVICE_UID": "4100",
        "SERVICE_GID": "4100",
        "AUTOMATION_UID": "4200",
        "AUTOMATION_GID": "4200",
        "OPERATOR_UID": "4300",
        "OPERATOR_GID": "4300",
        "CONNECT_GID": "4400",
        "AUTOMATION_READ_GID": "4500",
        "AUTOMATION_USER": "drawingmachine-automation",
    }
    service_bytes = _render(_resource_bytes("install/service-access.toml.example").decode(), ids).encode()
    service = parse_service_access_config(service_bytes)
    assert service.service_principal == PosixPrincipalV1(4100, 4100)
    assert service.automation_principal == PosixPrincipalV1(4200, 4200)
    assert service.operator_principal == PosixPrincipalV1(4300, 4300)

    openclaw_values = {
        **ids,
        "OPENCLAW_REGISTRY_ROOT": "/srv/openclaw-registry",
        "OPENCLAW_MANAGED_ROOT": "/srv/drawingmachine-openclaw",
        "OPENCLAW_MANIFEST_SHA256": "0" * 64,
        "OPENCLAW_REGISTRY_POLICY_SHA256": "1" * 64,
        "REGISTRY_OWNER_UID": "4600",
        "REGISTRY_OWNER_GID": "4600",
    }
    openclaw_bytes = _render(
        _resource_bytes("install/openclaw-deployment.toml.example").decode(), openclaw_values
    ).encode()
    policy = parse_openclaw_deployment_policy(openclaw_bytes, source_sha256=hashlib.sha256(openclaw_bytes).hexdigest())
    assert policy.managed_dir_mode == 0o2750 and policy.target_mode == 0o640
    assert policy.registry_root != policy.managed_root

    paths = resolve_xdg_paths(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path / "home",
    )
    (paths.config_dir / "machines").mkdir(parents=True)
    (paths.config_dir / "providers").mkdir()
    paths.config_file.write_bytes(_resource_bytes("install/config/application.toml.example"))
    machine = _render(
        _resource_bytes("install/config/machines/default.toml.example").decode(),
        {**ids, "SERIAL_DEVICE": "/dev/serial/by-id/example-controller"},
    )
    (paths.config_dir / "machines/default.toml").write_text(machine, encoding="utf-8")
    (paths.config_dir / "providers/local-comfyui.toml").write_bytes(
        _resource_bytes("install/config/providers/local-comfyui.toml.example")
    )
    (paths.config_dir / "providers/local-comfyui-workflow.json").write_bytes(
        _resource_bytes("install/config/providers/local-comfyui-workflow.json.example")
    )
    bundle = load_config(paths)
    assert bundle.machine.profile["hardware"]["serial_port"] == "/dev/serial/by-id/example-controller"
    assert bundle.provider.profile["workflow_template"] == "local-comfyui-workflow.json"


def _config(root: Path, *, current_uid: int, current_gid: int) -> ServiceAccessConfigV1:
    service = PosixPrincipalV1(current_uid + 100, current_gid + 100)
    automation = PosixPrincipalV1(current_uid, current_gid)
    operator = PosixPrincipalV1(current_uid + 200, current_gid + 200)
    runtime = root / "service-runtime"
    data = root / "service-data"
    return ServiceAccessConfigV1(
        service,
        automation,
        operator,
        PosixGroupV1("drawingmachine-connect", current_gid + 300),
        runtime,
        runtime / "service.sock",
        root / "client-runtime/drawingmachine/service.sock",
        root / "operator-runtime/drawingmachine/service.sock",
        data,
        data / "imports/automation",
        data / "exports/automation",
        root / "client-data/imports",
        root / "client-data/exports",
        root / "openclaw-policy.toml",
    )


class _Facts:
    def __init__(self, values: dict[Path, EndpointFactsV1]) -> None:
        self.values = values

    def inspect(self, path: Path) -> EndpointFactsV1:
        return self.values[path]


def _fact(path: Path, *, kind: str, uid: int, gid: int, mode: int) -> EndpointFactsV1:
    value = path.lstat()
    acl = tuple(
        sorted(
            (
                EndpointAclEntryV1("user_obj", None, (mode >> 6) & 0o7),
                EndpointAclEntryV1("group_obj", None, (mode >> 3) & 0o7),
                EndpointAclEntryV1("other", None, mode & 0o7),
            )
        )
    )
    return EndpointFactsV1(path, kind, value.st_dev, value.st_ino, uid, gid, mode, acl, ())


def test_runtime_helper_is_exact_idempotent_and_rejects_alternate_target(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    try:
        facts = _Facts(
            {
                parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
                config.canonical_runtime_dir: _fact(
                    config.canonical_runtime_dir,
                    kind="directory",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o710,
                ),
                config.canonical_socket: _fact(
                    config.canonical_socket,
                    kind="socket",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o660,
                ),
            }
        )
        first = rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        second = rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        assert first == second
        assert config.automation_runtime_endpoint.is_symlink()
        assert Path(os.readlink(config.automation_runtime_endpoint)) == config.canonical_socket

        config.automation_runtime_endpoint.unlink()
        config.automation_runtime_endpoint.symlink_to(tmp_path / "alternate.sock")
        identity = config.automation_runtime_endpoint.lstat().st_ino
        with pytest.raises(ValueError, match="alternate target"):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        assert config.automation_runtime_endpoint.lstat().st_ino == identity
    finally:
        listener.close()


def test_helper_denies_wrong_principal_operator_data_and_broad_parent(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o755),
            config.canonical_runtime_dir: _fact(
                config.canonical_runtime_dir,
                kind="directory",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o710,
            ),
        }
    )
    with pytest.raises(ValueError, match="private"):
        rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
    assert not config.automation_runtime_endpoint.exists()
    with pytest.raises(ValueError, match="principal"):
        rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid + 1, inspector=facts)
    with pytest.raises(ValueError, match="operator"):
        rebuild_client_endpoint(
            config, role="operator", mode="data", current_uid=config.operator_principal.uid, inspector=facts
        )


def test_helper_rejects_policy_or_endpoint_identity_drift(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    try:
        parent_fact = _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700)
        facts = _Facts(
            {
                parent: replace(parent_fact, inode=parent_fact.inode + 1),
                config.canonical_runtime_dir: _fact(
                    config.canonical_runtime_dir,
                    kind="directory",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o710,
                ),
                config.canonical_socket: _fact(
                    config.canonical_socket,
                    kind="socket",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o660,
                ),
            }
        )
        with pytest.raises(ValueError, match="identity"):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        assert not config.automation_runtime_endpoint.exists()
    finally:
        listener.close()


def _data_fact(path: Path, config: ServiceAccessConfigV1, *, writable: bool) -> EndpointFactsV1:
    value = path.lstat()
    entries = (
        EndpointAclEntryV1("user_obj", None, 0o7),
        EndpointAclEntryV1("user", config.automation_principal.uid, 0o7 if writable else 0o5),
        EndpointAclEntryV1("group_obj", None, 0),
        EndpointAclEntryV1("mask", None, 0o7),
        EndpointAclEntryV1("other", None, 0),
    )
    if writable and config.service_principal.uid != config.automation_principal.uid:
        entries = (*entries, EndpointAclEntryV1("user", config.service_principal.uid, 0o7))
    acl = tuple(sorted(entries))
    return EndpointFactsV1(
        path,
        "directory",
        value.st_dev,
        value.st_ino,
        config.service_principal.uid,
        config.connect_group.gid,
        0o2770,
        acl,
        acl,
    )


def test_explicit_automation_data_mode_creates_only_two_exact_links(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_import_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.automation_import_root.mkdir(parents=True, mode=0o2770)
    config.automation_export_root.mkdir(parents=True, mode=0o2770)
    sentinel = parent / "keep"
    sentinel.write_text("unchanged", encoding="utf-8")
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.automation_import_root: _data_fact(config.automation_import_root, config, writable=True),
            config.automation_export_root: _data_fact(config.automation_export_root, config, writable=False),
        }
    )
    first = rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    second = rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert first == second
    assert Path(os.readlink(config.automation_import_endpoint)) == config.automation_import_root
    assert Path(os.readlink(config.automation_export_endpoint)) == config.automation_export_root
    assert sentinel.read_text(encoding="utf-8") == "unchanged"

    config.automation_import_endpoint.unlink()
    config.automation_export_endpoint.unlink()
    facts.values[config.automation_export_root] = replace(
        facts.values[config.automation_export_root],
        acl=facts.values[config.automation_import_root].acl,
        default_acl=facts.values[config.automation_import_root].default_acl,
    )
    with pytest.raises(ValueError, match="data root"):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert not config.automation_import_endpoint.exists() and not config.automation_export_endpoint.exists()


def _stale_writable_data_fact(path: Path, config: ServiceAccessConfigV1) -> EndpointFactsV1:
    value = path.lstat()
    acl = tuple(
        sorted(
            (
                EndpointAclEntryV1("user_obj", None, 0o7),
                EndpointAclEntryV1("user", config.automation_principal.uid, 0o7),
                EndpointAclEntryV1("group_obj", None, 0),
                EndpointAclEntryV1("mask", None, 0o7),
                EndpointAclEntryV1("other", None, 0),
            )
        )
    )
    return EndpointFactsV1(
        path,
        "directory",
        value.st_dev,
        value.st_ino,
        config.service_principal.uid,
        config.connect_group.gid,
        0o2770,
        acl,
        acl,
    )


def test_helper_rejects_stale_import_acl_and_accepts_named_service_entry(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    assert config.service_principal.uid != config.automation_principal.uid
    parent = config.automation_import_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.automation_import_root.mkdir(parents=True, mode=0o2770)
    config.automation_export_root.mkdir(parents=True, mode=0o2770)
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.automation_import_root: _stale_writable_data_fact(config.automation_import_root, config),
            config.automation_export_root: _data_fact(config.automation_export_root, config, writable=False),
        }
    )
    with pytest.raises(ValueError, match="data root"):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert not config.automation_import_endpoint.exists() and not config.automation_export_endpoint.exists()

    facts.values[config.automation_import_root] = _data_fact(config.automation_import_root, config, writable=True)
    rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert Path(os.readlink(config.automation_import_endpoint)) == config.automation_import_root
    assert Path(os.readlink(config.automation_export_endpoint)) == config.automation_export_root


def test_runtime_helper_rejects_link_chain_and_broad_socket_acl(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    real_socket = tmp_path / "real.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(real_socket))
    config.canonical_socket.symlink_to(real_socket)
    try:
        parent_fact = _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700)
        runtime_fact = _fact(
            config.canonical_runtime_dir,
            kind="directory",
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=0o710,
        )
        forged_socket = _fact(
            config.canonical_socket,
            kind="socket",
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=0o660,
        )
        with pytest.raises(ValueError, match="identity"):
            rebuild_client_endpoint(
                config,
                role="automation",
                mode="runtime",
                current_uid=uid,
                inspector=_Facts(
                    {
                        parent: parent_fact,
                        config.canonical_runtime_dir: runtime_fact,
                        config.canonical_socket: forged_socket,
                    }
                ),
            )
        assert not config.automation_runtime_endpoint.exists()

        config.canonical_socket.unlink()
        listener.close()
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(config.canonical_socket))
        broad = replace(
            _fact(
                config.canonical_socket,
                kind="socket",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o660,
            ),
            acl=(),
        )
        with pytest.raises(ValueError, match="exact"):
            rebuild_client_endpoint(
                config,
                role="automation",
                mode="runtime",
                current_uid=uid,
                inspector=_Facts(
                    {
                        parent: parent_fact,
                        config.canonical_runtime_dir: runtime_fact,
                        config.canonical_socket: broad,
                    }
                ),
            )
    finally:
        listener.close()


def _rendered_service_policy() -> bytes:
    return _render(
        _resource_bytes("install/service-access.toml.example").decode(),
        {
            "SERVICE_UID": "4100",
            "SERVICE_GID": "4100",
            "AUTOMATION_UID": "4200",
            "AUTOMATION_GID": "4200",
            "OPERATOR_UID": "4300",
            "OPERATOR_GID": "4300",
            "CONNECT_GID": "4400",
            "AUTOMATION_USER": "drawingmachine-automation",
        },
    ).encode()


def test_policy_loader_requires_exact_bytes_digest_owner_mode_and_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "service-access.toml"
    contents = _rendered_service_policy()
    policy_path.write_bytes(contents)
    policy_path.chmod(0o640)
    value = policy_path.lstat()
    facts = EndpointFactsV1(
        policy_path,
        "regular",
        value.st_dev,
        value.st_ino,
        4100,
        4400,
        0o640,
        tuple(
            sorted(
                (
                    EndpointAclEntryV1("user_obj", None, 0o6),
                    EndpointAclEntryV1("group_obj", None, 0o4),
                    EndpointAclEntryV1("other", None, 0),
                )
            )
        ),
        (),
    )
    monkeypatch.setattr(endpoint_subject.LinuxEndpointFactsInspector, "inspect", lambda self, path: facts)
    digest = hashlib.sha256(contents).hexdigest()
    loaded = endpoint_subject._load_policy(policy_path, digest)
    assert loaded.service_principal == PosixPrincipalV1(4100, 4100)

    with pytest.raises(ValueError, match="digest"):
        endpoint_subject._load_policy(policy_path, "0" * 64)
    with pytest.raises(ValueError, match="absolute policy"):
        endpoint_subject._load_policy(Path("relative.toml"), digest)
    policy_path.chmod(0o600)
    monkeypatch.setattr(
        endpoint_subject.LinuxEndpointFactsInspector,
        "inspect",
        lambda self, path: replace(facts, mode=0o600, acl=()),
    )
    with pytest.raises(ValueError, match="exact"):
        endpoint_subject._load_policy(policy_path, digest)


def test_policy_loader_rejects_symlink_oversize_and_changed_digest(tmp_path: Path) -> None:
    target = tmp_path / "policy"
    target.write_bytes(_rendered_service_policy())
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        endpoint_subject._load_policy(link, hashlib.sha256(target.read_bytes()).hexdigest())

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * 65_537)
    with pytest.raises(ValueError, match="size"):
        endpoint_subject._load_policy(oversized, hashlib.sha256(oversized.read_bytes()).hexdigest())

    with pytest.raises(ValueError, match="canonical absolute"):
        endpoint_subject._walk_directory(Path("relative"))
    real_parent = tmp_path / "real-parent"
    (real_parent / "child").mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(OSError):
        endpoint_subject._walk_directory(linked_parent / "child")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC permission checks")
def test_lstat_absolute_traverses_execute_only_ancestor(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    link = outer / "link"
    link.symlink_to(tmp_path / "target")
    os.chmod(outer, 0o111)
    try:
        result = endpoint_subject._lstat_absolute(link)
    finally:
        os.chmod(outer, 0o700)
    assert stat.S_ISLNK(result.st_mode)


@pytest.mark.parametrize("fault", ["stream-oversize", "changed", "identity"])
def test_policy_loader_rejects_stream_growth_change_and_post_validation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    path = tmp_path / "service-access.toml"
    contents = _rendered_service_policy()
    path.write_bytes(contents if fault != "stream-oversize" else b"x" * 65_537)
    path.chmod(0o640)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        value = real_fstat(descriptor)
        calls += 1
        raw = list(value)
        if fault == "stream-oversize":
            raw[6] = 0
        elif fault == "changed" and calls > 1:
            raw[6] += 1
        return os.stat_result(raw)

    if fault in {"stream-oversize", "changed"}:
        monkeypatch.setattr(os, "fstat", changed_fstat)
    if fault == "identity":
        facts = _fact(path, kind="regular", uid=4100, gid=4400, mode=0o640)
        monkeypatch.setattr(
            endpoint_subject.LinuxEndpointFactsInspector,
            "inspect",
            lambda self, target: replace(facts, inode=facts.inode + 1),
        )
    message = "changed while reading" if fault == "changed" else None
    with pytest.raises(ValueError, match=message):
        endpoint_subject._load_policy(path, digest)


def test_module_main_guard_executes_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["client_endpoint", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(endpoint_subject.__file__, run_name="__main__")
    assert caught.value.code == 0


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"bad", "malformed"),
        (struct.pack("<IHHI", 2, 99, 0o7, 0), "unknown tag"),
    ],
)
def test_linux_acl_reader_rejects_malformed_and_unknown_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, message: str
) -> None:
    path = tmp_path / "entry"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: raw)
    with pytest.raises(ValueError, match=message):
        endpoint_subject.LinuxEndpointFactsInspector().inspect(path)


def test_linux_acl_reader_fails_closed_when_evidence_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry"
    path.write_text("x", encoding="utf-8")

    def denied(*args: object, **kwargs: object) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "getxattr", denied)
    with pytest.raises(ValueError, match="cannot be read"):
        endpoint_subject.LinuxEndpointFactsInspector().inspect(path)


def test_linux_acl_reader_handles_platform_alias_and_both_absent_acl_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry"
    path.write_text("x", encoding="utf-8")
    missing = 61_234
    monkeypatch.setattr(errno, "ENOATTR", missing, raising=False)

    def absent(*args: object, **kwargs: object) -> bytes:
        raise OSError(missing, "absent")

    monkeypatch.setattr(os, "getxattr", absent)
    assert endpoint_subject._read_acl(path, 0o640, "system.posix_acl_default") == ()
    assert endpoint_subject._read_acl(path, 0o640, "system.posix_acl_access") == endpoint_subject._mode_acl(0o640)


def test_linux_acl_reader_accepts_exact_named_acl_and_classifies_special(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry"
    path.write_text("x", encoding="utf-8")
    raw = b"".join(
        (
            struct.pack("<I", 2),
            struct.pack("<HHI", 1, 0o7, 0),
            struct.pack("<HHI", 2, 0o5, 4200),
            struct.pack("<HHI", 4, 0, 0),
            struct.pack("<HHI", 16, 0o7, 0),
            struct.pack("<HHI", 32, 0, 0),
        )
    )
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: raw)
    facts = endpoint_subject.LinuxEndpointFactsInspector().inspect(path)
    assert EndpointAclEntryV1("user", 4200, 0o5) in facts.acl
    assert endpoint_subject._kind(stat.S_IFIFO | 0o600) == "special"


def test_data_pair_creation_rolls_back_only_its_first_owned_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_import_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.automation_import_root.mkdir(parents=True, mode=0o2770)
    config.automation_export_root.mkdir(parents=True, mode=0o2770)
    sentinel = parent / "keep"
    sentinel.write_text("keep", encoding="utf-8")
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.automation_import_root: _data_fact(config.automation_import_root, config, writable=True),
            config.automation_export_root: _data_fact(config.automation_export_root, config, writable=False),
        }
    )
    real_symlink = os.symlink

    def fail_second(source: str, destination: str, *, dir_fd: int) -> None:
        if destination == "exports":
            raise OSError("injected second-link failure")
        real_symlink(source, destination, dir_fd=dir_fd)

    monkeypatch.setattr(os, "symlink", fail_second)
    with pytest.raises(OSError, match="second-link"):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert not config.automation_import_endpoint.exists()
    assert not config.automation_export_endpoint.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_runtime_creation_rolls_back_when_target_policy_drifts_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.canonical_runtime_dir: _fact(
                config.canonical_runtime_dir,
                kind="directory",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o710,
            ),
            config.canonical_socket: _fact(
                config.canonical_socket,
                kind="socket",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o660,
            ),
        }
    )
    real_symlink = os.symlink

    def create_then_broaden(source: str, destination: str, *, dir_fd: int) -> None:
        real_symlink(source, destination, dir_fd=dir_fd)
        facts.values[config.canonical_socket] = replace(
            facts.values[config.canonical_socket],
            mode=0o666,
            acl=endpoint_subject._mode_acl(0o666),
        )

    monkeypatch.setattr(os, "symlink", create_then_broaden)
    try:
        with pytest.raises(ValueError, match="exact"):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        assert not config.automation_runtime_endpoint.exists()
    finally:
        listener.close()


@pytest.mark.parametrize("fault", ["owner", "readlink", "missing", "replacement"])
def test_runtime_creation_quarantines_post_create_inspection_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.canonical_runtime_dir: _fact(
                config.canonical_runtime_dir,
                kind="directory",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o710,
            ),
            config.canonical_socket: _fact(
                config.canonical_socket,
                kind="socket",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o660,
            ),
        }
    )
    real_inspect = endpoint_subject._inspect_link

    def fail_after_create(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, int] | None:
        identity = real_inspect(parent_fd, name, target, owner_uid)
        if identity is None:
            return None
        if fault == "missing":
            os.unlink(name, dir_fd=parent_fd)
            return None
        if fault == "replacement":
            os.unlink(name, dir_fd=parent_fd)
            os.symlink(str(tmp_path / "attacker.sock"), name, dir_fd=parent_fd)
            raise OSError("injected replacement race")
        raise ValueError("injected owner drift") if fault == "owner" else OSError("injected readlink failure")

    monkeypatch.setattr(endpoint_subject, "_inspect_link", fail_after_create)
    try:
        with pytest.raises((OSError, ValueError)):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        quarantined = [
            entry
            for entry in parent.iterdir()
            if entry.name.startswith(
                endpoint_subject._endpoint_quarantine_prefix(config.automation_runtime_endpoint.name)
            )
        ]
        if fault == "replacement":
            assert not config.automation_runtime_endpoint.exists()
            assert len(quarantined) == 1 and Path(os.readlink(quarantined[0])) == tmp_path / "attacker.sock"
        elif fault == "missing":
            assert not config.automation_runtime_endpoint.exists() and quarantined == []
        else:
            assert not config.automation_runtime_endpoint.exists()
            assert len(quarantined) == 1 and Path(os.readlink(quarantined[0])) == config.canonical_socket
    finally:
        listener.close()


@pytest.mark.parametrize("endpoint_name", ["imports", "exports"])
@pytest.mark.parametrize("fault", ["open", "fstat", "owner", "readlink"])
def test_data_creation_cleans_the_unbound_first_or_second_link_without_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_name: str,
    fault: str,
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_import_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.automation_import_root.mkdir(parents=True, mode=0o2770)
    config.automation_export_root.mkdir(parents=True, mode=0o2770)
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.automation_import_root: _data_fact(config.automation_import_root, config, writable=True),
            config.automation_export_root: _data_fact(config.automation_export_root, config, writable=False),
        }
    )
    real_symlink = os.symlink
    real_open = os.open
    real_fstat = os.fstat
    real_readlink = os.readlink
    armed = False
    fsyncs = 0

    def arm(source: str, destination: str, *, dir_fd: int) -> None:
        nonlocal armed
        real_symlink(source, destination, dir_fd=dir_fd)
        if destination == endpoint_name:
            armed = True

    def fail_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal armed
        if fault == "open" and armed and path == endpoint_name and flags & os.O_PATH:
            armed = False
            raise OSError("injected O_PATH failure")
        return real_open(path, flags, *args, **kwargs)

    def fail_fstat(descriptor: int) -> os.stat_result:
        nonlocal armed
        value = real_fstat(descriptor)
        if fault in {"fstat", "owner"} and armed and stat.S_ISLNK(value.st_mode):
            armed = False
            if fault == "fstat":
                raise OSError("injected fstat failure")
            raw = list(value)
            raw[4] = uid + 1
            return os.stat_result(raw)
        return value

    def fail_readlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object
    ) -> str:
        nonlocal armed
        if fault == "readlink" and armed and path == endpoint_name:
            armed = False
            raise OSError("injected readlink failure")
        return real_readlink(path, *args, **kwargs)

    real_fsync = os.fsync

    def count_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "symlink", arm)
    monkeypatch.setattr(os, "open", fail_open)
    monkeypatch.setattr(os, "fstat", fail_fstat)
    monkeypatch.setattr(os, "readlink", fail_readlink)
    monkeypatch.setattr(os, "fsync", count_fsync)
    baseline = len(os.listdir("/proc/self/fd"))
    with pytest.raises((OSError, ValueError)):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert not config.automation_import_endpoint.exists()
    assert not config.automation_export_endpoint.exists()
    quarantined = [
        entry.name for entry in parent.iterdir() if entry.name.startswith(".drawingmachine-endpoint-rollback-")
    ]
    assert len(quarantined) == (1 if endpoint_name == "imports" else 2)
    assert (
        sum(name.startswith(endpoint_subject._endpoint_quarantine_prefix(endpoint_name)) for name in quarantined) == 1
    )
    assert fsyncs >= 1
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("endpoint_name", ["imports", "exports"])
@pytest.mark.parametrize("fault", ["open", "fstat", "owner", "readlink"])
def test_persistent_bind_failure_never_restores_the_created_data_link_publicly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_name: str,
    fault: str,
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_import_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.automation_import_root.mkdir(parents=True, mode=0o2770)
    config.automation_export_root.mkdir(parents=True, mode=0o2770)
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.automation_import_root: _data_fact(config.automation_import_root, config, writable=True),
            config.automation_export_root: _data_fact(config.automation_export_root, config, writable=False),
        }
    )
    quarantine_prefix = f".drawingmachine-endpoint-rollback-{endpoint_name}-"
    real_open = os.open
    real_fstat = os.fstat
    real_readlink = os.readlink
    fault_descriptors: set[int] = set()

    def persistent_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, *args: object, **kwargs: object
    ) -> int:
        targets_endpoint = (path == endpoint_name or str(path).startswith(quarantine_prefix)) and not (
            flags & os.O_DIRECTORY
        )
        if fault == "open" and flags & os.O_PATH and targets_endpoint:
            raise OSError("persistent O_PATH failure")
        descriptor = real_open(path, flags, *args, **kwargs)
        if flags & os.O_PATH and targets_endpoint:
            fault_descriptors.add(descriptor)
        return descriptor

    def persistent_fstat(descriptor: int) -> os.stat_result:
        value = real_fstat(descriptor)
        if fault in {"fstat", "owner"} and descriptor in fault_descriptors and stat.S_ISLNK(value.st_mode):
            if fault == "fstat":
                raise OSError("persistent fstat failure")
            raw = list(value)
            raw[4] = uid + 1
            return os.stat_result(raw)
        return value

    def persistent_readlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object
    ) -> str:
        if fault == "readlink" and (path == endpoint_name or str(path).startswith(quarantine_prefix)):
            raise OSError("persistent readlink failure")
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", persistent_open)
    monkeypatch.setattr(os, "fstat", persistent_fstat)
    monkeypatch.setattr(os, "readlink", persistent_readlink)
    baseline = len(os.listdir("/proc/self/fd"))
    with pytest.raises((OSError, ValueError)):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert not config.automation_import_endpoint.exists()
    assert not config.automation_export_endpoint.exists()
    quarantined = [entry for entry in parent.iterdir() if entry.name.startswith(quarantine_prefix)]
    assert len(quarantined) == 1
    with pytest.raises(ValueError, match="unresolved quarantine"):
        rebuild_client_endpoint(config, role="automation", mode="data", current_uid=uid, inspector=facts)
    assert len([entry for entry in parent.iterdir() if entry.name.startswith(quarantine_prefix)]) == 1
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("fault", ["foreign-race", "no-unlink", "restore", "fsync"])
def test_unbound_link_quarantine_preserves_foreign_names_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    facts = _Facts(
        {
            parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
            config.canonical_runtime_dir: _fact(
                config.canonical_runtime_dir,
                kind="directory",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o710,
            ),
            config.canonical_socket: _fact(
                config.canonical_socket,
                kind="socket",
                uid=config.service_principal.uid,
                gid=config.connect_group.gid,
                mode=0o660,
            ),
        }
    )
    endpoint = config.automation_runtime_endpoint
    attacker = tmp_path / "attacker.sock"
    real_bind = endpoint_subject._bind_created_link
    first = True

    def fail_first(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, tuple[int, int]]:
        nonlocal first
        if first:
            first = False
            if fault in {"foreign-race", "restore"}:
                os.unlink(name, dir_fd=parent_fd)
                os.symlink(str(attacker), name, dir_fd=parent_fd)
            raise OSError("injected initial bind failure")
        return real_bind(parent_fd, name, target, owner_uid)

    real_unlink = os.unlink
    hidden_unlinks = 0

    def fail_cleanup(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object
    ) -> None:
        nonlocal hidden_unlinks
        if str(path).startswith(".drawingmachine-endpoint-rollback-"):
            hidden_unlinks += 1
        real_unlink(path, *args, **kwargs)

    real_rename = endpoint_subject._rename_noreplace

    def fail_restore(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        if fault == "restore" and source.startswith(".drawingmachine-endpoint-rollback-"):
            raise OSError("injected quarantine restore failure")
        real_rename(source_fd, source, destination_fd, destination)

    real_fsync = os.fsync

    def fail_fsync(descriptor: int) -> None:
        if fault == "fsync":
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(endpoint_subject, "_bind_created_link", fail_first)
    monkeypatch.setattr(os, "unlink", fail_cleanup)
    monkeypatch.setattr(endpoint_subject, "_rename_noreplace", fail_restore)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    baseline = len(os.listdir("/proc/self/fd"))
    try:
        expected = "quarantine could not be restored" if fault == "restore" else None
        with pytest.raises((OSError, ValueError), match=expected):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        quarantined = [
            entry for entry in parent.iterdir() if entry.name.startswith(".drawingmachine-endpoint-rollback-")
        ]
        if fault == "foreign-race":
            assert endpoint.is_symlink() and Path(os.readlink(endpoint)) == attacker
            assert quarantined == []
        elif fault in {"no-unlink", "restore", "fsync"}:
            assert not endpoint.exists() and len(quarantined) == 1
        assert hidden_unlinks == 0
        assert len(os.listdir("/proc/self/fd")) == baseline
    finally:
        listener.close()


def test_unbound_link_quarantine_handles_missing_and_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        endpoint_subject._quarantine_unbound_link(parent_fd, "missing", Path("/target"), os.getuid())

        endpoint = parent / "endpoint"
        endpoint.symlink_to("/target")
        real_stat = os.stat
        stats = 0

        def fail_bind(*args: object, **kwargs: object) -> tuple[int, tuple[int, int]]:
            raise OSError("injected persistent bind failure")

        def drift_identity(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            nonlocal stats
            value = real_stat(path, *args, **kwargs)
            if str(path).startswith(".drawingmachine-endpoint-rollback-"):
                stats += 1
                if stats > 1:
                    raw = list(value)
                    raw[1] += 1
                    return os.stat_result(raw)
            return value

        monkeypatch.setattr(endpoint_subject, "_bind_created_link", fail_bind)
        monkeypatch.setattr(os, "stat", drift_identity)
        with pytest.raises(ValueError, match="identity could not be classified"):
            endpoint_subject._quarantine_unbound_link(parent_fd, endpoint.name, Path("/target"), os.getuid())
        assert not endpoint.exists()
        quarantined = [
            entry for entry in parent.iterdir() if entry.name.startswith(".drawingmachine-endpoint-rollback-")
        ]
        assert len(quarantined) == 1 and os.readlink(quarantined[0]) == "/target"
    finally:
        os.close(parent_fd)


def test_quarantine_preflight_is_bounded_and_classification_rejects_foreign_or_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index in range(endpoint_subject._MAX_CLIENT_PARENT_ENTRIES + 1):
            (parent / f"entry-{index}").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="entry quota"):
            endpoint_subject._require_no_endpoint_quarantine(parent_fd, "endpoint")
        for entry in parent.iterdir():
            entry.unlink()

        quarantine = f"{endpoint_subject._endpoint_quarantine_prefix('endpoint')}fixed"
        (parent / quarantine).write_text("foreign", encoding="utf-8")
        assert endpoint_subject._classify_quarantined_link(parent_fd, quarantine, Path("/target"), os.getuid()) == (
            "foreign",
            None,
        )
        (parent / quarantine).unlink()
        (parent / quarantine).symlink_to("/target")
        real_stat = os.stat
        calls = 0

        def drift(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            nonlocal calls
            value = real_stat(path, *args, **kwargs)
            if path == quarantine:
                calls += 1
                if calls > 1:
                    raw = list(value)
                    raw[1] += 1
                    return os.stat_result(raw)
            return value

        monkeypatch.setattr(os, "stat", drift)
        assert endpoint_subject._classify_quarantined_link(parent_fd, quarantine, Path("/target"), os.getuid()) == (
            "unknown",
            None,
        )
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize(
    ("error_number", "error_type"),
    [(errno.EEXIST, FileExistsError), (errno.ENOTEMPTY, FileExistsError), (errno.EPERM, OSError)],
)
def test_endpoint_rename_noreplace_maps_linux_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    error_type: type[OSError],
) -> None:
    def fail(*args: object) -> int:
        ctypes.set_errno(error_number)
        return -1

    monkeypatch.setattr(endpoint_subject, "_renameat2", fail)
    with pytest.raises(error_type):
        endpoint_subject._rename_noreplace(1, "source", 2, "destination")


def _runtime_rebuild_case(
    tmp_path: Path,
) -> tuple[int, ServiceAccessConfigV1, Path, socket.socket, dict[Path, EndpointFactsV1]]:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    values = {
        parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
        config.canonical_runtime_dir: _fact(
            config.canonical_runtime_dir,
            kind="directory",
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=0o710,
        ),
        config.canonical_socket: _fact(
            config.canonical_socket,
            kind="socket",
            uid=config.service_principal.uid,
            gid=config.connect_group.gid,
            mode=0o660,
        ),
    }
    return uid, config, parent, listener, values


def test_outer_created_rollback_quarantines_a_foreign_swap_without_pathname_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid, config, parent, listener, values = _runtime_rebuild_case(tmp_path)
    endpoint = config.automation_runtime_endpoint
    attacker = tmp_path / "outer-attacker.sock"
    real_inspect = endpoint_subject._inspect_link
    inspections = 0

    def fail_final(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, int] | None:
        nonlocal inspections
        inspections += 1
        value = real_inspect(parent_fd, name, target, owner_uid)
        if inspections == 3:
            raise ValueError("injected final validation failure")
        return value

    real_move = endpoint_subject._move_to_quarantine
    real_unlink = os.unlink
    real_symlink = os.symlink
    swapped = False

    def swap_then_isolate(parent_fd: int, name: str) -> str | None:
        nonlocal swapped
        if name == endpoint.name:
            real_unlink(name, dir_fd=parent_fd)
            real_symlink(str(attacker), name, dir_fd=parent_fd)
            swapped = True
        return real_move(parent_fd, name)

    hidden_unlinks = 0

    def count_hidden_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object
    ) -> None:
        nonlocal hidden_unlinks
        if str(path).startswith(endpoint_subject._endpoint_quarantine_prefix(endpoint.name)):
            hidden_unlinks += 1
        real_unlink(path, *args, **kwargs)

    real_fsync = os.fsync
    fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(endpoint_subject, "_inspect_link", fail_final)
    monkeypatch.setattr(endpoint_subject, "_move_to_quarantine", swap_then_isolate)
    monkeypatch.setattr(os, "unlink", count_hidden_unlink)
    monkeypatch.setattr(os, "fsync", count_fsync)
    baseline = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(ValueError, match="final validation failure"):
            rebuild_client_endpoint(
                config, role="automation", mode="runtime", current_uid=uid, inspector=_Facts(values)
            )
        quarantined = [
            entry
            for entry in parent.iterdir()
            if entry.name.startswith(endpoint_subject._endpoint_quarantine_prefix(endpoint.name))
        ]
        assert swapped and not endpoint.exists()
        assert len(quarantined) == 1 and Path(os.readlink(quarantined[0])) == attacker
        assert hidden_unlinks == 0 and fsyncs >= 2
        assert len(os.listdir("/proc/self/fd")) == baseline
    finally:
        listener.close()


def test_hidden_cleanup_quarantines_a_foreign_swap_without_pathname_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid, config, parent, listener, values = _runtime_rebuild_case(tmp_path)
    endpoint = config.automation_runtime_endpoint
    attacker = tmp_path / "hidden-attacker.sock"
    real_bind = endpoint_subject._bind_created_link
    real_unlink = os.unlink
    real_symlink = os.symlink
    binds = 0

    def bind_then_swap(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, tuple[int, int]]:
        nonlocal binds
        binds += 1
        if binds == 1:
            raise OSError("injected initial bind failure")
        descriptor, identity = real_bind(parent_fd, name, target, owner_uid)
        real_unlink(name, dir_fd=parent_fd)
        real_symlink(str(attacker), name, dir_fd=parent_fd)
        return descriptor, identity

    hidden_unlinks = 0

    def count_hidden_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object
    ) -> None:
        nonlocal hidden_unlinks
        if str(path).startswith(endpoint_subject._endpoint_quarantine_prefix(endpoint.name)):
            hidden_unlinks += 1
        real_unlink(path, *args, **kwargs)

    real_fsync = os.fsync
    fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(endpoint_subject, "_bind_created_link", bind_then_swap)
    monkeypatch.setattr(os, "unlink", count_hidden_unlink)
    monkeypatch.setattr(os, "fsync", count_fsync)
    baseline = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(ValueError, match="remains safely quarantined"):
            rebuild_client_endpoint(
                config, role="automation", mode="runtime", current_uid=uid, inspector=_Facts(values)
            )
        quarantined = [
            entry
            for entry in parent.iterdir()
            if entry.name.startswith(endpoint_subject._endpoint_quarantine_prefix(endpoint.name))
        ]
        assert not endpoint.exists()
        assert len(quarantined) == 1 and Path(os.readlink(quarantined[0])) == attacker
        assert hidden_unlinks == 0 and fsyncs >= 1
        assert len(os.listdir("/proc/self/fd")) == baseline
    finally:
        listener.close()


def test_rebuild_links_rejects_split_parents_and_noncanonical_runtime_target(tmp_path: Path) -> None:
    uid, config, parent, listener, values = _runtime_rebuild_case(tmp_path)
    try:
        with pytest.raises(ValueError, match="share one exact"):
            endpoint_subject._rebuild_links(
                ((parent / "one", config.canonical_socket), (tmp_path / "other/two", config.canonical_socket)),
                principal_uid=uid,
                principal_gid=os.getgid(),
                config=config,
                inspector=_Facts(values),
                runtime=True,
            )
        with pytest.raises(ValueError, match="canonical socket"):
            endpoint_subject._rebuild_links(
                ((parent / "one", config.canonical_runtime_dir),),
                principal_uid=uid,
                principal_gid=os.getgid(),
                config=config,
                inspector=_Facts(values),
                runtime=True,
            )
    finally:
        listener.close()


@pytest.mark.parametrize("fault", ["runtime-root", "target", "parent", "final-link"])
def test_rebuild_links_rejects_every_post_create_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    uid, config, parent, listener, values = _runtime_rebuild_case(tmp_path)
    counts: dict[Path, int] = {}

    class _DriftingFacts:
        def inspect(self, path: Path) -> EndpointFactsV1:
            counts[path] = counts.get(path, 0) + 1
            value = values[path]
            if fault == "runtime-root" and path == config.canonical_runtime_dir and counts[path] > 1:
                return replace(value, inode=value.inode + 1)
            if fault == "target" and path == config.canonical_socket and counts[path] > 1:
                return replace(value, inode=value.inode + 1)
            if fault == "parent" and path == parent and counts[path] > 1:
                return replace(value, inode=value.inode + 1)
            return value

    real_inspect = endpoint_subject._inspect_link
    link_inspections = 0

    def disappear(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, int] | None:
        nonlocal link_inspections
        link_inspections += 1
        value = real_inspect(parent_fd, name, target, owner_uid)
        return None if fault == "final-link" and link_inspections == 3 else value

    monkeypatch.setattr(endpoint_subject, "_inspect_link", disappear)
    try:
        with pytest.raises(ValueError):
            rebuild_client_endpoint(
                config,
                role="automation",
                mode="runtime",
                current_uid=uid,
                inspector=_DriftingFacts(),
            )
        assert not config.automation_runtime_endpoint.exists()
    finally:
        listener.close()


@pytest.mark.parametrize("fault", ["rename", "fsync"])
def test_created_link_rollback_reports_isolation_and_fsync_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    uid, config, _parent, listener, values = _runtime_rebuild_case(tmp_path)
    real_inspect = endpoint_subject._inspect_link
    inspections = 0
    rollback = False

    def fail_final(parent_fd: int, name: str, target: Path, owner_uid: int) -> tuple[int, int] | None:
        nonlocal inspections, rollback
        inspections += 1
        value = real_inspect(parent_fd, name, target, owner_uid)
        if inspections == 3:
            rollback = True
            raise ValueError("injected final validation failure")
        return value

    real_rename = endpoint_subject._rename_noreplace

    def fail_rename(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        if fault == "rename" and rollback and source == config.automation_runtime_endpoint.name:
            raise OSError("injected rollback isolation failure")
        real_rename(source_fd, source, destination_fd, destination)

    real_fsync = os.fsync
    fsyncs = 0

    def fail_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        if fault == "fsync" and fsyncs > 1:
            raise OSError("injected rollback fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(endpoint_subject, "_inspect_link", fail_final)
    monkeypatch.setattr(endpoint_subject, "_rename_noreplace", fail_rename)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    try:
        with pytest.raises(ValueError, match="rollback could not be verified"):
            rebuild_client_endpoint(
                config, role="automation", mode="runtime", current_uid=uid, inspector=_Facts(values)
            )
        quarantined = [
            entry
            for entry in config.automation_runtime_endpoint.parent.iterdir()
            if entry.name.startswith(
                endpoint_subject._endpoint_quarantine_prefix(config.automation_runtime_endpoint.name)
            )
        ]
        if fault == "rename":
            assert config.automation_runtime_endpoint.is_symlink() and quarantined == []
        else:
            assert not config.automation_runtime_endpoint.exists() and len(quarantined) == 1
    finally:
        listener.close()


def test_inspect_link_wraps_readlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    (parent / "endpoint").symlink_to("/target")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)

    def unreadable(*args: object, **kwargs: object) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(os, "readlink", unreadable)
    try:
        with pytest.raises(ValueError, match="cannot be read exactly"):
            endpoint_subject._inspect_link(parent_fd, "endpoint", Path("/target"), os.getuid())
    finally:
        os.close(parent_fd)


def test_helper_rejects_invalid_policy_shape_mode_and_existing_regular_endpoint(tmp_path: Path) -> None:
    uid, gid = os.getuid(), os.getgid()
    config = _config(tmp_path, current_uid=uid, current_gid=gid)
    with pytest.raises(ValueError, match="policy and role"):
        rebuild_client_endpoint(object(), role="automation", mode="runtime")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mode"):
        rebuild_client_endpoint(config, role="automation", mode="unknown")  # type: ignore[arg-type]

    parent = config.automation_runtime_endpoint.parent
    parent.mkdir(parents=True, mode=0o700)
    config.canonical_runtime_dir.mkdir(mode=0o710)
    config.automation_runtime_endpoint.write_text("not a link", encoding="utf-8")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(config.canonical_socket))
    try:
        facts = _Facts(
            {
                parent: _fact(parent, kind="directory", uid=uid, gid=gid, mode=0o700),
                config.canonical_runtime_dir: _fact(
                    config.canonical_runtime_dir,
                    kind="directory",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o710,
                ),
                config.canonical_socket: _fact(
                    config.canonical_socket,
                    kind="socket",
                    uid=config.service_principal.uid,
                    gid=config.connect_group.gid,
                    mode=0o660,
                ),
            }
        )
        with pytest.raises(ValueError, match="owned symlink"):
            rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=uid, inspector=facts)
        assert config.automation_runtime_endpoint.read_text(encoding="utf-8") == "not a link"
    finally:
        listener.close()


def test_internal_cli_selects_kernel_principal_and_sanitizes_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = _config(tmp_path, current_uid=4200, current_gid=4200)
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(endpoint_subject, "_load_policy", lambda path, digest: config)
    monkeypatch.setattr(os, "getuid", lambda: 4200)
    monkeypatch.setattr(
        endpoint_subject,
        "rebuild_client_endpoint",
        lambda config, *, role, mode, current_uid: calls.append((role, mode, current_uid)) or (),
    )
    arguments = ["runtime", "--policy", str(tmp_path / "policy"), "--expected-policy-sha256", "0" * 64]
    assert endpoint_subject.main(arguments) == 0
    assert calls == [("automation", "runtime", 4200)]

    monkeypatch.setattr(os, "getuid", lambda: config.operator_principal.uid)
    assert endpoint_subject.main(arguments) == 0
    assert calls[-1] == ("operator", "runtime", config.operator_principal.uid)

    monkeypatch.setattr(os, "getuid", lambda: 9999)
    assert endpoint_subject.main(arguments) == 1
    assert "not an authorized client" in capsys.readouterr().err
