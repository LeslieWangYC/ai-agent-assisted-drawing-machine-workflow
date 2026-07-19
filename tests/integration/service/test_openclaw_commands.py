from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import drawingmachine.service.openclaw_protocol as subject
from drawingmachine.adapters.filesystem.openclaw_install import OpenClawFilesystemAdapter
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.application.openclaw import PackagedOpenClawDeployment
from drawingmachine.cli.client import ServiceClient
from drawingmachine.config.openclaw_deployment import (
    OpenClawDeploymentPolicyV1,
    PosixGroupV1,
    PosixPrincipalV1,
    ServiceWriterSnapshotV1,
    load_openclaw_deployment_policy,
)
from drawingmachine.config.service_access import ServiceAccessConfigV1
from drawingmachine.domain.openclaw import (
    OpenClawCheckFileV1,
    OpenClawCheckResultV1,
    OpenClawCheckStatus,
    OpenClawInitialState,
    OpenClawInstallFileStatus,
    OpenClawInstallFileV1,
    OpenClawInstallResultV1,
    OpenClawRegistryCheckV1,
    OpenClawRegistryStatus,
)
from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle
from drawingmachine.domain.openclaw.models import OpenClawDeploymentError
from drawingmachine.protocol import ProtocolRequest
from drawingmachine.service.access_policy import PathAccessFactsV1, ServiceAccessSnapshotV1
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.models import PeerCredentials, RequestContext
from drawingmachine.service.openclaw_protocol import OpenClawProtocol
from drawingmachine.service.server import UnixServiceServer


def _principal(offset: int) -> PosixPrincipalV1:
    return PosixPrincipalV1(os.getuid() + offset, os.getgid() + offset)


def _access_snapshot(tmp_path: Path) -> ServiceAccessSnapshotV1:
    service = _principal(0)
    automation = _principal(10_001)
    operator = _principal(10_002)
    config = ServiceAccessConfigV1(
        service,
        automation,
        operator,
        PosixGroupV1("drawingmachine-connect", os.getgid() + 10_003),
        tmp_path / "run/service",
        tmp_path / "run/service/service.sock",
        tmp_path / "run/automation/service.sock",
        tmp_path / "run/operator/service.sock",
        tmp_path / "data",
        tmp_path / "data/imports/automation",
        tmp_path / "data/exports/automation",
        tmp_path / "automation/imports",
        tmp_path / "automation/exports",
        tmp_path / "config/openclaw-deployment.toml",
    )
    source = PathAccessFactsV1(
        tmp_path / "config/service-access.toml", "regular", 1, 1, service.uid, config.connect_group.gid, 0o640, ()
    )
    outer = PathAccessFactsV1(tmp_path / "run", "directory", 1, 2, service.uid, service.gid, 0o710, ())
    return ServiceAccessSnapshotV1(
        config,
        source.path,
        1,
        "a" * 64,
        source.identity,
        service,
        0o640,
        (),
        source,
        "b" * 64,
        outer,
    )


def _write_deployment_tree(snapshot: ServiceAccessSnapshotV1) -> None:
    config = snapshot.config
    registry_root = config.canonical_data_dir.parent / "openclaw-registry"
    managed_root = config.canonical_data_dir.parent / "openclaw-managed"
    registry_root.mkdir(parents=True)
    managed_root.mkdir()
    (managed_root / "agents").mkdir()
    for agent_id in ("cnc-drawable-translator", "cnc-drawable-router"):
        (managed_root / "agents" / agent_id).mkdir()
    for directory in (managed_root, managed_root / "agents", *(managed_root / "agents").iterdir()):
        directory.chmod(0o2750)
    bundle = load_packaged_openclaw_bundle()
    registry = {
        "agents": {
            "list": [
                {
                    "id": agent.agent_id,
                    "workspace": str(managed_root / agent.workspace_relative_path),
                    "tools": {"alsoAllow": list(agent.required_tools)},
                }
                for agent in bundle.registry_policy.agents
            ]
        }
    }
    registry_path = registry_root / "openclaw.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    registry_path.chmod(0o440)
    policy = f'''schema_version = 1
registry_root = {json.dumps(str(registry_root))}
managed_root = {json.dumps(str(managed_root))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

[operator_principal]
uid = {config.operator_principal.uid}
gid = {config.operator_principal.gid}
[automation_principal]
uid = {config.automation_principal.uid}
gid = {config.automation_principal.gid}
[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}
[managed_owner]
uid = {config.service_principal.uid}
gid = {config.service_principal.gid}

[registry_owner]
uid = {config.service_principal.uid}
gid = {config.service_principal.gid}
'''.encode()
    config.openclaw_policy_path.parent.mkdir(parents=True)
    config.openclaw_policy_path.write_bytes(policy)
    config.openclaw_policy_path.chmod(0o400)


def _root(snapshot: ServiceAccessSnapshotV1) -> Path:
    return snapshot.config.canonical_data_dir.parent / "openclaw-registry"


def _managed(snapshot: ServiceAccessSnapshotV1) -> Path:
    return snapshot.config.canonical_data_dir.parent / "openclaw-managed"


def _operator(snapshot: ServiceAccessSnapshotV1) -> RequestContext:
    principal = snapshot.config.operator_principal
    return RequestContext(PeerCredentials(1234, principal.uid, principal.gid), datetime.now(UTC))


def _request(command: str, root: Path, request_id: str | None = None) -> ProtocolRequest:
    return ProtocolRequest(
        1,
        command,
        str(uuid4()) if request_id is None else request_id,
        {
            "schema_version": 1,
            "openclaw_root": str(root),
        },
    )


def _protocol(
    snapshot: ServiceAccessSnapshotV1,
    *,
    loader_calls: list[Path] | None = None,
    factory_calls: list[OpenClawDeploymentPolicyV1] | None = None,
) -> tuple[CommandDispatcher, OpenClawProtocol]:
    dispatcher = CommandDispatcher(lambda: pytest.fail("status/provider/job/machine path entered"))

    def load(path: Path, writer: ServiceWriterSnapshotV1) -> OpenClawDeploymentPolicyV1:
        if loader_calls is not None:
            loader_calls.append(path)
        return load_openclaw_deployment_policy(path, service_writer_snapshot=writer)

    def build(policy: OpenClawDeploymentPolicyV1) -> PackagedOpenClawDeployment:
        if factory_calls is not None:
            factory_calls.append(policy)
        adapter = OpenClawFilesystemAdapter(policy, ancestor_authority=lambda _path, _status: True)
        return PackagedOpenClawDeployment(policy, adapter)

    protocol = OpenClawProtocol(snapshot, policy_loader=load, deployment_factory=build)
    protocol.register(dispatcher)
    return dispatcher, protocol


def _unsafe_check_result() -> OpenClawCheckResultV1:
    bundle = load_packaged_openclaw_bundle()
    return OpenClawCheckResultV1(
        False,
        True,
        OpenClawRegistryCheckV1(bundle.registry_policy_sha256, OpenClawRegistryStatus.UNSAFE),
        tuple(
            OpenClawCheckFileV1(
                cast(str, resource.destination),
                resource.sha256,
                None,
                OpenClawCheckStatus.UNSAFE,
            )
            for resource in bundle.managed_resources
        ),
    )


def _rolled_back_install_result() -> OpenClawInstallResultV1:
    return OpenClawInstallResultV1(
        False,
        True,
        False,
        True,
        True,
        True,
        True,
        tuple(
            OpenClawInstallFileV1(
                destination,
                OpenClawInitialState.ABSENT,
                None,
                OpenClawInstallFileStatus.RESTORED_ABSENT,
                False,
                True,
                True,
                "INSTALL_FAILED",
            )
            for destination in (
                "agents/cnc-drawable-translator/AGENTS.md",
                "agents/cnc-drawable-translator/TOOLS.md",
                "agents/cnc-drawable-router/AGENTS.md",
                "agents/cnc-drawable-router/TOOLS.md",
            )
        ),
    )


@pytest.mark.parametrize(
    "command,result,code,category,message",
    [
        (
            "openclaw.check",
            _unsafe_check_result(),
            "OPENCLAW_CHECK_NOT_IN_SYNC",
            "validation",
            "OpenClaw resources are not in sync",
        ),
        (
            "openclaw.install",
            _rolled_back_install_result(),
            "OPENCLAW_INSTALL_NOT_COMPLETED",
            "service",
            "OpenClaw installation did not complete",
        ),
    ],
)
def test_negative_closed_d3_results_are_failed_envelopes_with_preserved_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    result: OpenClawCheckResultV1 | OpenClawInstallResultV1,
    code: str,
    category: str,
    message: str,
) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    dispatcher, _ = _protocol(snapshot)
    monkeypatch.setattr(
        subject.PackagedOpenClawDeployment,
        "check" if command == "openclaw.check" else "install",
        lambda *args, **kwargs: result,
    )

    response = dispatcher.dispatch(_request(command, _root(snapshot)), _operator(snapshot))

    assert response.ok is False
    assert response.data == {}
    assert response.error is not None
    assert response.error.code == code
    assert response.error.category.value == category
    assert response.error.message == message
    assert response.error.retryable is False
    assert response.error.details == {"result": result.to_json()}


def test_exact_operator_check_install_check_and_replay_use_closed_results(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    factories: list[OpenClawDeploymentPolicyV1] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads, factory_calls=factories)
    context = _operator(snapshot)

    before_tree = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    checked = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)
    after_check = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert checked.ok is False and checked.data == {} and checked.error is not None
    assert checked.error.code == "OPENCLAW_CHECK_NOT_IN_SYNC"
    checked_result = checked.error.details["result"]
    assert isinstance(checked_result, dict) and checked_result["in_sync"] is False
    assert before_tree == after_check

    request_id = str(uuid4())
    installed = dispatcher.dispatch(_request("openclaw.install", _root(snapshot), request_id), context)
    replayed = dispatcher.dispatch(_request("openclaw.install", _root(snapshot), request_id), context)
    final = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)
    assert installed.ok is True and installed.data["installed"] is True
    assert installed.data["globally_atomic"] is False
    assert replayed.ok is True and replayed.data["deduplicated"] is True
    assert final.ok is True and final.data["in_sync"] is True
    assert loads == [snapshot.config.openclaw_policy_path] * 4
    assert len(factories) == 1


@pytest.mark.parametrize("role", ["automation", "service", "wrong-uid", "wrong-gid"])
def test_non_operator_peer_is_rejected_before_policy_or_target_access(tmp_path: Path, role: str) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    factories: list[OpenClawDeploymentPolicyV1] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads, factory_calls=factories)
    selected = {
        "automation": snapshot.config.automation_principal,
        "service": snapshot.config.service_principal,
        "wrong-uid": replace(snapshot.config.operator_principal, uid=snapshot.config.operator_principal.uid + 1),
        "wrong-gid": replace(snapshot.config.operator_principal, gid=snapshot.config.operator_principal.gid + 1),
    }[role]
    context = RequestContext(PeerCredentials(9, selected.uid, selected.gid), datetime.now(UTC))

    response = dispatcher.dispatch(_request("openclaw.install", _root(snapshot)), context)

    assert response.ok is False
    assert response.error is not None and response.error.code == "OPENCLAW_OPERATOR_DENIED"
    assert loads == []
    assert factories == []
    assert list(_managed(snapshot).rglob("*.md")) == []


def test_missing_peer_and_unknown_fields_fail_before_policy_load(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads)
    request = _request("openclaw.check", _root(snapshot))

    missing = dispatcher.dispatch(request, cast(RequestContext, object()))
    unknown = dispatcher.dispatch(
        replace(request, arguments={**request.arguments, "operator_uid": snapshot.config.operator_principal.uid}),
        _operator(snapshot),
    )

    assert missing.ok is False and missing.error is not None
    assert missing.error.code == "OPENCLAW_OPERATOR_DENIED"
    assert unknown.ok is False and unknown.error is not None
    assert unknown.error.code == "OPENCLAW_COMMAND_ARGUMENTS_INVALID"
    assert loads == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"schema_version": 1},
        {"schema_version": True, "openclaw_root": "/tmp/root"},
        {"schema_version": 2, "openclaw_root": "/tmp/root"},
        {"schema_version": 1, "openclaw_root": "relative"},
        {"schema_version": 1, "openclaw_root": "/tmp/control\nroot"},
        {"schema_version": 1, "openclaw_root": "/tmp/control\x7froot"},
        {"schema_version": 1, "openclaw_root": "/tmp/é"},
        {"schema_version": 1, "openclaw_root": "/" + "a" * 4097},
        {"schema_version": 1, "openclaw_root": "//tmp/root"},
        {"schema_version": 1, "openclaw_root": "/tmp//root"},
        {"schema_version": 1, "openclaw_root": "/tmp/../tmp/root"},
        {"schema_version": 1, "openclaw_root": 7},
    ],
)
def test_closed_request_schema_rejects_hostile_values_before_policy(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads)
    request = ProtocolRequest(1, "openclaw.check", str(uuid4()), arguments)  # type: ignore[arg-type]

    response = dispatcher.dispatch(request, _operator(snapshot))

    assert response.ok is False
    assert response.error is not None and response.error.code == "OPENCLAW_COMMAND_ARGUMENTS_INVALID"
    assert loads == []


def test_policy_root_registry_and_policy_authority_drift_are_zero_write(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    dispatcher, _ = _protocol(snapshot)
    context = _operator(snapshot)

    mismatch = dispatcher.dispatch(_request("openclaw.install", tmp_path / "elsewhere"), context)
    assert mismatch.ok is False and mismatch.error is not None
    assert mismatch.error.code == "ROOT_MISMATCH"
    assert list(_managed(snapshot).rglob("*.md")) == []

    registry = _root(snapshot) / "openclaw.json"
    registry.chmod(0o600)
    registry.write_text("{}", encoding="utf-8")
    registry.chmod(0o440)
    drift = dispatcher.dispatch(_request("openclaw.install", _root(snapshot)), context)
    assert drift.ok is False and drift.error is not None
    assert drift.error.code == "REGISTRY_DRIFT"
    assert list(_managed(snapshot).rglob("*.md")) == []

    snapshot.config.openclaw_policy_path.chmod(0o620)
    unsafe_policy = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)
    assert unsafe_policy.ok is False and unsafe_policy.error is not None
    assert unsafe_policy.error.code == "OPENCLAW_POLICY_INVALID"
    assert list(_managed(snapshot).rglob("*.md")) == []


@pytest.mark.parametrize("kind", ["symlink", "regular"])
def test_symlink_or_non_directory_policy_root_fails_closed_without_target_write(tmp_path: Path, kind: str) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    root = _root(snapshot)
    moved = root.with_name("registry-real")
    root.rename(moved)
    if kind == "symlink":
        root.symlink_to(moved, target_is_directory=True)
    else:
        root.write_text("not a directory", encoding="utf-8")
    dispatcher, _ = _protocol(snapshot)

    checked = dispatcher.dispatch(_request("openclaw.check", root), _operator(snapshot))
    installed = dispatcher.dispatch(_request("openclaw.install", root), _operator(snapshot))

    assert checked.ok is False and checked.error is not None
    assert checked.error.code == "OPENCLAW_CHECK_NOT_IN_SYNC"
    checked_result = checked.error.details["result"]
    assert isinstance(checked_result, dict) and checked_result["unsafe"] is True
    assert installed.ok is False and installed.error is not None
    assert list(_managed(snapshot).rglob("*.md")) == []


def test_policy_identity_mismatch_and_runtime_policy_change_fail_closed(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads)
    context = _operator(snapshot)

    first = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)
    assert first.ok is False and first.error is not None
    assert first.error.code == "OPENCLAW_CHECK_NOT_IN_SYNC"
    path = snapshot.config.openclaw_policy_path
    contents = path.read_text(encoding="utf-8")
    path.chmod(0o600)
    path.write_text(contents + "\n", encoding="utf-8")
    path.chmod(0o400)

    changed = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)

    assert changed.ok is False and changed.error is not None
    assert changed.error.code == "OPENCLAW_POLICY_CHANGED"
    assert len(loads) == 2


def test_new_service_instance_reports_transaction_residue_as_recovery(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    residue = (
        _managed(snapshot)
        / "agents/cnc-drawable-router/.drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-tmp-0123456789abcdef"
    )
    residue.write_text("recovery evidence", encoding="utf-8")
    residue.chmod(0o640)
    dispatcher, _ = _protocol(snapshot)

    response = dispatcher.dispatch(_request("openclaw.install", _root(snapshot)), _operator(snapshot))

    assert response.ok is False and response.error is not None
    assert response.error.code == "PARTIAL_INSTALL"
    assert residue.read_text(encoding="utf-8") == "recovery evidence"


def test_protocol_constructor_and_registration_are_closed(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    dispatcher, protocol = _protocol(snapshot)
    with pytest.raises(TypeError):
        OpenClawProtocol(
            cast(ServiceAccessSnapshotV1, object()), policy_loader=lambda *_: None, deployment_factory=lambda _: None
        )  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError):
        OpenClawProtocol(snapshot, policy_loader=cast(object, None), deployment_factory=lambda _: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError):
        OpenClawProtocol(snapshot, policy_loader=lambda *_: None, deployment_factory=cast(object, None))  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError):
        protocol.register(cast(CommandDispatcher, object()))
    with pytest.raises(Exception, match="already registered"):
        protocol.register(dispatcher)


def test_policy_loader_value_error_is_a_bounded_typed_failure(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    dispatcher = CommandDispatcher(lambda: pytest.fail("status path entered"))

    def broken_loader(path: Path, writer: ServiceWriterSnapshotV1) -> OpenClawDeploymentPolicyV1:
        del path, writer
        raise ValueError("RAW_POLICY_DETAIL")

    protocol = OpenClawProtocol(snapshot, policy_loader=broken_loader, deployment_factory=lambda _: pytest.fail())
    protocol.register(dispatcher)
    response = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), _operator(snapshot))

    assert response.ok is False and response.error is not None
    assert response.error.code == "OPENCLAW_POLICY_INVALID"
    assert "RAW_POLICY_DETAIL" not in response.error.message


def test_policy_exact_bytes_are_loaded_for_every_command(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    expected = hashlib.sha256(snapshot.config.openclaw_policy_path.read_bytes()).hexdigest()
    dispatcher, _ = _protocol(snapshot)

    first = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), _operator(snapshot))
    second = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), _operator(snapshot))

    assert first.ok is False and first.error is not None
    assert second.ok is False and second.error is not None
    assert first.error.code == second.error.code == "OPENCLAW_CHECK_NOT_IN_SYNC"
    loaded = load_openclaw_deployment_policy(
        snapshot.config.openclaw_policy_path,
        service_writer_snapshot=ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert loaded.source_sha256 == expected


def test_peer_object_and_non_object_arguments_are_rejected_before_policy(tmp_path: Path) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    loads: list[Path] = []
    dispatcher, _ = _protocol(snapshot, loader_calls=loads)
    context = RequestContext(cast(PeerCredentials, object()), datetime.now(UTC))
    peer = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), context)
    arguments = dispatcher.dispatch(
        ProtocolRequest(1, "openclaw.check", str(uuid4()), cast(dict[str, object], object())),
        _operator(snapshot),
    )
    assert peer.ok is False and peer.error is not None and peer.error.code == "OPENCLAW_OPERATOR_DENIED"
    assert arguments.ok is False and arguments.error is not None
    assert arguments.error.code == "OPENCLAW_COMMAND_ARGUMENTS_INVALID"
    assert loads == []


@pytest.mark.parametrize("field", ["operator", "automation", "service"])
def test_each_policy_principal_must_match_service_access(tmp_path: Path, field: str) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    loaded = load_openclaw_deployment_policy(snapshot.config.openclaw_policy_path, service_writer_snapshot=writer)
    changed = {
        "operator": replace(loaded, operator_principal=_principal(77)),
        "automation": replace(loaded, automation_principal=_principal(78)),
        "service": replace(loaded, managed_owner=_principal(79)),
    }[field]
    dispatcher = CommandDispatcher(lambda: pytest.fail("status path entered"))
    protocol = OpenClawProtocol(snapshot, policy_loader=lambda *_: changed, deployment_factory=lambda _: pytest.fail())
    protocol.register(dispatcher)

    response = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), _operator(snapshot))

    assert response.ok is False and response.error is not None
    assert response.error.code == "OPENCLAW_POLICY_INVALID"


@pytest.mark.parametrize("failure", ["loader-type", "factory-error", "factory-type"])
def test_loader_and_factory_defensive_failures_are_typed(tmp_path: Path, failure: str) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    loaded = load_openclaw_deployment_policy(snapshot.config.openclaw_policy_path, service_writer_snapshot=writer)

    def load(path: Path, service_writer: ServiceWriterSnapshotV1) -> OpenClawDeploymentPolicyV1:
        del path, service_writer
        if failure == "loader-type":
            return cast(OpenClawDeploymentPolicyV1, object())
        return loaded

    def build(policy: OpenClawDeploymentPolicyV1) -> PackagedOpenClawDeployment:
        del policy
        if failure == "factory-error":
            raise ValueError("raw factory detail")
        return cast(PackagedOpenClawDeployment, object())

    dispatcher = CommandDispatcher(lambda: pytest.fail("status path entered"))
    protocol = OpenClawProtocol(snapshot, policy_loader=load, deployment_factory=build)
    protocol.register(dispatcher)
    response = dispatcher.dispatch(_request("openclaw.check", _root(snapshot)), _operator(snapshot))

    assert response.ok is False and response.error is not None
    assert response.error.code == "OPENCLAW_POLICY_INVALID"
    assert "raw" not in response.error.message


@pytest.mark.parametrize(
    "command,outcome",
    [
        ("openclaw.check", "error"),
        ("openclaw.check", "wrong-result"),
        ("openclaw.install", "wrong-result"),
    ],
)
def test_deployment_errors_and_wrong_result_types_never_escape_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    outcome: str,
) -> None:
    snapshot = _access_snapshot(tmp_path)
    _write_deployment_tree(snapshot)
    dispatcher, _ = _protocol(snapshot)

    def result(*args: object, **kwargs: object) -> object:
        del args, kwargs
        if outcome == "error":
            raise OpenClawDeploymentError("WRITER_MISMATCH", "raw writer detail")
        return object()

    monkeypatch.setattr(
        subject.PackagedOpenClawDeployment,
        "check" if command == "openclaw.check" else "install",
        result,
    )
    response = dispatcher.dispatch(_request(command, _root(snapshot)), _operator(snapshot))

    assert response.ok is False and response.error is not None
    expected = "WRITER_MISMATCH" if outcome == "error" else "OPENCLAW_PROTOCOL_INVALID"
    assert response.error.code == expected
    assert "raw" not in response.error.message


def test_real_temporary_socket_uses_direct_kernel_operator_peer(tmp_path: Path) -> None:
    base = _access_snapshot(tmp_path)
    config = replace(
        base.config,
        service_principal=_principal(90),
        automation_principal=_principal(91),
        operator_principal=_principal(0),
    )
    snapshot = replace(base, config=config)
    bundle = load_packaged_openclaw_bundle()
    policy = OpenClawDeploymentPolicyV1(
        "a" * 64,
        config.operator_principal,
        config.automation_principal,
        PosixGroupV1("drawingmachine-openclaw-read", os.getgid()),
        _root(snapshot),
        _managed(snapshot),
        config.service_principal,
        config.service_principal,
        0o2750,
        0o640,
        bundle.manifest_sha256,
        bundle.registry_policy_sha256,
    )
    check_result = OpenClawCheckResultV1(
        True,
        False,
        OpenClawRegistryCheckV1(bundle.registry_policy_sha256, OpenClawRegistryStatus.MATCHES),
        tuple(
            OpenClawCheckFileV1(
                cast(str, resource.destination),
                resource.sha256,
                resource.sha256,
                OpenClawCheckStatus.MATCHES,
            )
            for resource in bundle.managed_resources
        ),
    )

    class Port:
        def check(self, explicit_root: Path, writer: ServiceWriterSnapshotV1) -> OpenClawCheckResultV1:
            assert explicit_root == policy.registry_root
            assert writer == ServiceWriterSnapshotV1(config.service_principal.uid, config.service_principal.gid)
            return check_result

        def install(
            self,
            explicit_root: Path,
            request_id: str,
            writer: ServiceWriterSnapshotV1,
        ) -> OpenClawInstallResultV1:
            del explicit_root, request_id, writer
            raise AssertionError("install not expected")

    dispatcher = CommandDispatcher(lambda: pytest.fail("status path entered"))
    protocol = OpenClawProtocol(
        snapshot,
        policy_loader=lambda *_: policy,
        deployment_factory=lambda loaded: PackagedOpenClawDeployment(loaded, Port()),
    )
    protocol.register(dispatcher)
    socket_path = tmp_path / "explicit/service.sock"
    socket_path.parent.mkdir()

    async def scenario() -> None:
        server = UnixServiceServer(socket_path, dispatcher, SystemClock())
        task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            response = await asyncio.to_thread(
                ServiceClient(socket_path, endpoint_validator=None).call,
                "openclaw.check",
                {"schema_version": 1, "openclaw_root": str(policy.registry_root)},
                request_id=str(uuid4()),
            )
            assert response.ok is True
            assert response.data == check_result.to_json()
        finally:
            await server.close()
            await task

    asyncio.run(scenario())
    assert not socket_path.exists()
