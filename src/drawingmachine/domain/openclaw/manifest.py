from __future__ import annotations

import hashlib
import importlib.resources
from dataclasses import dataclass
from importlib.resources.abc import Traversable

from drawingmachine.resources.openclaw import (
    MAX_RESOURCE_BYTES,
    OpenClawRegistryPolicyV1,
    load_openclaw_registry_policy,
    load_openclaw_resource_manifest,
)


@dataclass(frozen=True, slots=True)
class PackagedOpenClawResourceV1:
    source: str
    destination: str | None
    contents: bytes
    sha256: str
    target_mode: int | None


@dataclass(frozen=True, slots=True)
class PackagedOpenClawBundleV1:
    manifest_sha256: str
    registry_policy_sha256: str
    registry_policy: OpenClawRegistryPolicyV1
    resources: tuple[PackagedOpenClawResourceV1, ...]

    @property
    def managed_resources(self) -> tuple[PackagedOpenClawResourceV1, ...]:
        return tuple(item for item in self.resources if item.destination is not None)

    @property
    def registry_policy_resource(self) -> PackagedOpenClawResourceV1:
        return next(item for item in self.resources if item.destination is None)


def load_packaged_openclaw_bundle() -> PackagedOpenClawBundleV1:
    manifest = load_openclaw_resource_manifest()
    policy = load_openclaw_registry_policy()
    root = importlib.resources.files("drawingmachine.resources.openclaw")
    manifest_bytes = _read(root.joinpath("manifest.json"), "manifest.json")
    resources: list[PackagedOpenClawResourceV1] = []
    for item in manifest.resources:
        contents = _read(root.joinpath(*item.source.split("/")), item.source)
        digest = hashlib.sha256(contents).hexdigest()
        if len(contents) != item.size_bytes or digest != item.sha256:
            raise ValueError(f"packaged OpenClaw resource changed after manifest validation: {item.source}")
        resources.append(PackagedOpenClawResourceV1(item.source, item.destination, contents, digest, item.target_mode))
    registry_resource = next(item for item in resources if item.destination is None)
    return PackagedOpenClawBundleV1(
        hashlib.sha256(manifest_bytes).hexdigest(),
        registry_resource.sha256,
        policy,
        tuple(resources),
    )


def _read(resource: Traversable, name: str) -> bytes:
    try:
        with resource.open("rb") as stream:
            contents = stream.read(MAX_RESOURCE_BYTES + 1)
    except (FileNotFoundError, OSError) as error:
        raise ValueError(f"packaged OpenClaw resource is unavailable: {name}") from error
    if len(contents) > MAX_RESOURCE_BYTES:
        raise ValueError(f"packaged OpenClaw resource is oversized: {name}")
    return contents


__all__ = ["PackagedOpenClawBundleV1", "PackagedOpenClawResourceV1", "load_packaged_openclaw_bundle"]
