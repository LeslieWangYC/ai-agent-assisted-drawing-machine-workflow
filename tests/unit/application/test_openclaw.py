from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from drawingmachine.application.openclaw import PackagedOpenClawDeployment
from drawingmachine.config.openclaw_deployment import ServiceWriterSnapshotV1, parse_openclaw_deployment_policy
from drawingmachine.domain.openclaw import (
    OpenClawCheckFileV1,
    OpenClawCheckResultV1,
    OpenClawCheckStatus,
    OpenClawInstallResultV1,
    OpenClawRegistryCheckV1,
    OpenClawRegistryStatus,
)
from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle


class _Port:
    def __init__(self, result: OpenClawCheckResultV1) -> None:
        self.result = result
        self.calls = 0

    def check(self, explicit_root: Path, service_writer_snapshot: ServiceWriterSnapshotV1) -> OpenClawCheckResultV1:
        self.calls += 1
        return self.result

    def install(
        self,
        explicit_root: Path,
        request_id: str,
        service_writer_snapshot: ServiceWriterSnapshotV1,
    ) -> OpenClawInstallResultV1:
        raise AssertionError("install not expected")


def _check_result() -> OpenClawCheckResultV1:
    destinations = (
        "agents/cnc-drawable-translator/AGENTS.md",
        "agents/cnc-drawable-translator/TOOLS.md",
        "agents/cnc-drawable-router/AGENTS.md",
        "agents/cnc-drawable-router/TOOLS.md",
    )
    return OpenClawCheckResultV1(
        True,
        False,
        OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.MATCHES),
        tuple(
            OpenClawCheckFileV1(destination, "b" * 64, "b" * 64, OpenClawCheckStatus.MATCHES)
            for destination in destinations
        ),
    )


@pytest.fixture
def valid_policy(tmp_path: Path) -> object:
    bundle = load_packaged_openclaw_bundle()
    encoded = f'''schema_version = 1
registry_root = {json.dumps(str(tmp_path / "registry"))}
managed_root = {json.dumps(str(tmp_path / "managed"))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

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
    return parse_openclaw_deployment_policy(encoded, source_sha256=hashlib.sha256(encoded).hexdigest())


def test_use_case_binds_packaged_digests_before_port_access(valid_policy: object) -> None:
    result = _check_result()
    port = _Port(result)
    use_case = PackagedOpenClawDeployment(valid_policy, port)  # type: ignore[arg-type]
    writer = ServiceWriterSnapshotV1(
        uid=valid_policy.managed_owner.uid,  # type: ignore[attr-defined]
        gid=valid_policy.managed_owner.gid,  # type: ignore[attr-defined]
    )

    assert use_case.check(valid_policy.registry_root, writer) is result  # type: ignore[attr-defined]
    assert port.calls == 1

    drifted = replace(valid_policy, manifest_sha256="f" * 64)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="manifest digest"):
        PackagedOpenClawDeployment(drifted, port)
    assert port.calls == 1

    registry_drift = replace(valid_policy, registry_policy_sha256="f" * 64)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="registry digest"):
        PackagedOpenClawDeployment(registry_drift, port)


def test_use_case_rejects_non_snapshot_and_delegates_install(valid_policy: object) -> None:
    with pytest.raises(TypeError, match="exact"):
        PackagedOpenClawDeployment(object(), object())  # type: ignore[arg-type]
    port = _Port(_check_result())
    use_case = PackagedOpenClawDeployment(valid_policy, port)  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="not expected"):
        use_case.install(
            valid_policy.registry_root,  # type: ignore[attr-defined]
            "00000000-0000-4000-8000-000000000000",
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
