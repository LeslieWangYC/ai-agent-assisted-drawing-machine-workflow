from __future__ import annotations

from pathlib import Path

from drawingmachine.config.openclaw_deployment import OpenClawDeploymentPolicyV1, ServiceWriterSnapshotV1
from drawingmachine.domain.openclaw import OpenClawCheckResultV1, OpenClawInstallResultV1
from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle
from drawingmachine.ports.openclaw import OpenClawDeploymentPort


class PackagedOpenClawDeployment:
    def __init__(
        self,
        policy: OpenClawDeploymentPolicyV1,
        port: OpenClawDeploymentPort[OpenClawCheckResultV1, OpenClawInstallResultV1],
    ) -> None:
        if type(policy) is not OpenClawDeploymentPolicyV1:
            raise TypeError("OpenClaw policy must be an exact immutable snapshot")
        bundle = load_packaged_openclaw_bundle()
        if policy.manifest_sha256 != bundle.manifest_sha256:
            raise ValueError("deployment policy manifest digest does not match packaged authority")
        if policy.registry_policy_sha256 != bundle.registry_policy_sha256:
            raise ValueError("deployment policy registry digest does not match packaged authority")
        self._policy = policy
        self._port = port

    def check(self, explicit_root: Path, writer: ServiceWriterSnapshotV1) -> OpenClawCheckResultV1:
        return self._port.check(explicit_root, writer)

    def install(
        self,
        explicit_root: Path,
        request_id: str,
        writer: ServiceWriterSnapshotV1,
    ) -> OpenClawInstallResultV1:
        return self._port.install(explicit_root, request_id, writer)


__all__ = ["PackagedOpenClawDeployment"]
