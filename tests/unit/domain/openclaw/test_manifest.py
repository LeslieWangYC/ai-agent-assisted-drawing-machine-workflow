from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

import drawingmachine.domain.openclaw.manifest as subject
from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle


def test_packaged_bundle_freezes_exact_five_sources_and_four_targets() -> None:
    bundle = load_packaged_openclaw_bundle()

    assert len(bundle.resources) == 5
    assert len(bundle.managed_resources) == 4
    assert tuple(item.destination for item in bundle.managed_resources) == tuple(
        item.source for item in bundle.managed_resources
    )
    assert bundle.registry_policy_resource.source == "registry-policy.json"
    assert len(bundle.manifest_sha256) == len(bundle.registry_policy_sha256) == 64


class _Resource:
    def __init__(self, contents: bytes | BaseException) -> None:
        self.contents = contents

    def joinpath(self, *_parts: str) -> _Resource:
        return self

    def open(self, _mode: str) -> io.BytesIO:
        if isinstance(self.contents, BaseException):
            raise self.contents
        return io.BytesIO(self.contents)


def test_bundle_loader_rejects_second_read_drift_and_resource_io(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = SimpleNamespace(
        resources=(SimpleNamespace(source="x", destination="x", size_bytes=1, sha256="0" * 64, target_mode=0o640),)
    )
    monkeypatch.setattr(subject, "load_openclaw_resource_manifest", lambda: manifest)
    monkeypatch.setattr(subject, "load_openclaw_registry_policy", lambda: SimpleNamespace(agents=()))
    monkeypatch.setattr(subject.importlib.resources, "files", lambda _name: _Resource(b"x"))
    with pytest.raises(ValueError, match="changed"):
        load_packaged_openclaw_bundle()

    with pytest.raises(ValueError, match="unavailable"):
        subject._read(_Resource(OSError("no")), "x")
    with pytest.raises(ValueError, match="oversized"):
        subject._read(_Resource(b"x" * 65_537), "x")
