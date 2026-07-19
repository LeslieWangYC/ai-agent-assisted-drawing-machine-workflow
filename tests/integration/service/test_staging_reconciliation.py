from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.adapters.filesystem.client_data import FilesystemClientInputStaging, FilesystemServiceClientData
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.ports.client_data import ClientDataRole, StagingAdmissionState, StagingRecoveryStatus


class _Clock:
    def __init__(self) -> None:
        self.monotonic = 100
        self.boot = "boot-a"

    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)

    def boot_id(self) -> str:
        return self.boot

    def monotonic_ns(self) -> int:
        return self.monotonic


class _Reader:
    def read_bytes(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("not used")


def _plane(tmp_path: Path, clock: _Clock):
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports, exports)
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=clock.now,
    )
    return imports, repository, client, service, StagingReconciler(imports.resolve(), repository, _secure_fs)


def test_partial_dispatch_without_decode_is_preserved_until_ttl_then_quarantined(tmp_path: Path) -> None:
    clock = _Clock()
    imports, repository, client, _service, reconciler = _plane(tmp_path, clock)
    source = tmp_path / "candidate.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, clock))

    before = reconciler.reconcile("startup", clock)
    assert before.evidence == ()
    assert staged.bundle_path.exists()
    assert repository.get_staging_admission(request_id) is None

    clock.monotonic = staged.lease.deadline_monotonic_ns
    after = reconciler.reconcile("periodic", clock)
    assert len(after.evidence) == 1
    assert after.evidence[0].status is StagingRecoveryStatus.COMPLETE
    assert after.evidence[0].payload_identity == staged.lease.payload_identity
    assert not staged.bundle_path.exists()
    assert any(path.name.endswith(".quarantine") for path in (imports / "gcode/quarantine").iterdir())


def test_decoded_request_that_crashes_before_claim_is_quarantined_by_revision_cas(tmp_path: Path) -> None:
    clock = _Clock()
    _imports, repository, client, service, reconciler = _plane(tmp_path, clock)
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_image(source, request_id, clock))
    service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)

    active = reconciler.reconcile("before-command", clock)
    assert active.evidence == ()
    assert staged.bundle_path.exists()

    clock.monotonic = staged.lease.deadline_monotonic_ns
    result = reconciler.reconcile("shutdown", clock)

    assert len(result.evidence) == 1
    admission = repository.get_staging_admission(request_id)
    assert admission is not None
    assert admission.state is StagingAdmissionState.QUARANTINED
    assert admission.revision == 1


@pytest.mark.parametrize("state", ["undecoded", "decoded", "claimed"])
def test_boot_change_never_compares_prior_boot_monotonic_deadline(tmp_path: Path, state: str) -> None:
    clock = _Clock()
    _imports, repository, client, service, reconciler = _plane(tmp_path, clock)
    source = tmp_path / "candidate.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, clock))
    if state != "undecoded":
        service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    if state == "claimed":
        service.claim_gcode(request_id, staged.payload_path)
    clock.boot = "boot-b"
    clock.monotonic = 1

    result = reconciler.reconcile("startup", clock)

    assert len(result.evidence) == 1
    assert result.evidence[0].reason_code.value == ("STALE_ADMITTED" if state == "claimed" else "STALE_DROP")
    admission = repository.get_staging_admission(request_id)
    if admission is not None:
        assert admission.state is StagingAdmissionState.QUARANTINED
