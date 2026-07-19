from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import drawingmachine.application.staging_reconciliation as reconciliation_module
from drawingmachine.adapters.filesystem import _secure_fs
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.application.staging_reconciliation import StagingReconciler
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.client_data import (
    ClientDataRole,
    PayloadIdentityV1,
    PrepareObservationStatus,
    PrepareObservationV1,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingLeaseV1,
    StagingOwnershipState,
)


class _Clock:
    def __init__(self) -> None:
        self.boot = "boot-a"
        self.monotonic = 100

    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)

    def boot_id(self) -> str:
        return self.boot

    def monotonic_ns(self) -> int:
        return self.monotonic


def _layout(root: Path) -> None:
    for role in ClientDataRole:
        for phase in ("prepare", "drop"):
            path = root / role.value / phase
            path.mkdir(parents=True)
            path.chmod(0o2770)
        for phase in ("admitted", "quarantine", "observations"):
            path = root / role.value / phase
            path.mkdir(mode=0o700)
            path.chmod(0o700)


def _prepare(root: Path) -> Path:
    request_id = "12345678-1234-4234-8234-123456789abc"
    path = root / "image/prepare" / f"image--{request_id}--{'a' * 32}.prepare"
    path.mkdir()
    return path


def _reconciler(tmp_path: Path, clock: _Clock) -> tuple[StagingReconciler, Path]:
    root = tmp_path / "imports"
    _layout(root)
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    return StagingReconciler(root, repository, _secure_fs), root


def test_service_won_prepare_lock_is_quarantined_and_role_clears(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)

    result = reconciler.reconcile("startup", clock)

    assert result.blocked_roles == frozenset()
    assert len(result.evidence) == 1
    assert result.evidence[0].reason_code == "STALE_PREPARE"
    assert not prepare.exists()
    assert list((root / "image/observations").iterdir()) == []


def test_held_prepare_expires_then_converges_after_lock_release(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        active = reconciler.reconcile("startup", clock)
        assert active.blocked_roles == frozenset({ClientDataRole.IMAGE})
        observation_path = next((root / "image/observations").iterdir())
        assert '"status":"ACTIVE"' in observation_path.read_text(encoding="utf-8")

        clock.monotonic += 300_000_000_000
        expired = reconciler.reconcile("periodic", clock)
        assert expired.blocked_roles == frozenset({ClientDataRole.IMAGE})
        assert f'"status":"{PrepareObservationStatus.HELD_EXPIRED.value}"' in observation_path.read_text(
            encoding="utf-8"
        )
    finally:
        os.close(descriptor)

    converged = reconciler.reconcile("periodic", clock)
    assert converged.blocked_roles == frozenset()
    assert not prepare.exists()
    assert list((root / "image/observations").iterdir()) == []


def test_published_handoff_and_proven_abort_clear_observations(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    payload = prepare / "payload"
    payload.write_bytes(b"image")
    value = payload.stat()
    digest = hashlib.sha256(b"image").hexdigest()
    request_id = "12345678-1234-4234-8234-123456789abc"
    lease = StagingLeaseV1(
        request_id,
        ClientDataRole.IMAGE,
        StagingOwnershipState.PUBLISHED_UNSENT,
        digest,
        5,
        PayloadIdentityV1(value.st_dev, value.st_ino, value.st_size, digest),
        clock.boot_id(),
        clock.monotonic_ns(),
        clock.monotonic_ns() + 300_000_000_000,
    )
    (prepare / "lease.json").write_text(json.dumps(lease.to_json()), encoding="utf-8")
    drop = root / "image/drop" / (prepare.name.removesuffix(".prepare") + ".stage")
    prepare.rename(drop)
    os.close(descriptor)

    handoff = reconciler.reconcile("before-command", clock)
    assert handoff.blocked_roles == frozenset()
    assert drop.exists()
    assert list((root / "image/observations").iterdir()) == []

    aborted = _prepare(root)
    descriptor = os.open(aborted, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("before-command", clock)
    aborted.rmdir()
    os.close(descriptor)

    result = reconciler.reconcile("after-command", clock)
    assert result.blocked_roles == frozenset()
    assert list((root / "image/observations").iterdir()) == []


def test_invalid_prepare_and_special_drop_are_quarantined_without_false_block(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    invalid = root / "image/prepare/not-authorized"
    invalid.mkdir()
    special = root / "gcode/drop/not-a-bundle"
    special.write_bytes(b"special")

    result = reconciler.reconcile("startup", clock)

    assert result.blocked_roles == frozenset()
    assert {item.reason_code.value for item in result.evidence} == {
        "INVALID_PREPARE_NAME",
        "SPECIAL_STAGING_ENTRY",
    }
    assert not invalid.exists()
    assert not special.exists()


def test_corrupt_observation_and_identity_replacement_block_only_affected_role(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    observations = root / "gcode/observations"
    observations.chmod(0o700)
    (observations / "corrupt.json").write_text('{"bad":NaN}', encoding="utf-8")
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    prepare.rename(prepare.with_name("old-prepare"))
    prepare.mkdir()

    result = reconciler.reconcile("periodic", clock)

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE, ClientDataRole.GCODE})
    assert {"IDENTITY_DRIFT", "OBSERVATION_CORRUPT"} <= {item.reason_code.value for item in result.evidence}
    with pytest.raises(DrawingMachineError):
        reconciler.require_role_ready(ClientDataRole.IMAGE)


def test_private_reconciliation_symlink_is_rejected_without_chmod_or_external_write(tmp_path: Path) -> None:
    clock = _Clock()
    root = tmp_path / "imports"
    _layout(root)
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    (root / "image/observations").rmdir()
    (root / "image/observations").symlink_to(external, target_is_directory=True)

    with pytest.raises(DrawingMachineError) as error:
        StagingReconciler(root.resolve(), repository, _secure_fs).reconcile("startup", clock)

    assert error.value.payload.code == "STAGING_PRIVATE_ROOT_INVALID"
    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("fault", ["missing", "mode", "acl"])
def test_static_private_reconciliation_roots_are_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    clock = _Clock()
    root = tmp_path / "imports"
    _layout(root)
    repository = SQLiteRepository(tmp_path / "state.db", clock=clock)
    repository.initialize()
    private = root / "image/observations"
    if fault == "missing":
        private.rmdir()
    elif fault == "mode":
        private.chmod(0o755)
    else:
        real_getxattr = os.getxattr

        def drift(path: os.PathLike[str] | str, attribute: str, **kwargs: object) -> bytes:
            if Path(path) == private and attribute == "system.posix_acl_access":
                return b"extended"
            return real_getxattr(path, attribute, **kwargs)

        monkeypatch.setattr(reconciliation_module.os, "getxattr", drift)

    with pytest.raises(DrawingMachineError) as error:
        StagingReconciler(root.resolve(), repository, _secure_fs).reconcile("startup", clock)

    assert error.value.payload.code == "STAGING_PRIVATE_ROOT_INVALID"
    if fault == "missing":
        assert not private.exists()
    elif fault == "mode":
        assert stat.S_IMODE(private.stat().st_mode) == 0o755


def test_boot_change_with_live_prepare_lock_records_recovery_and_invalid_trigger_is_rejected(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        reconciler.reconcile("startup", clock)
        clock.boot = "boot-b"
        result = reconciler.reconcile("periodic", clock)
    finally:
        os.close(descriptor)

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].reason_code.value == "LOCK_RACE"
    with pytest.raises(ValueError):
        reconciler.reconcile("unknown", clock)
    with pytest.raises(TypeError, match="active"):
        reconciler.reconcile("periodic", clock, {"request"})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="durable"):
        reconciler.reconcile("periodic", clock, frozenset(), (object(),))  # type: ignore[arg-type]


def test_active_request_preserves_observation_and_drop_authority(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    request_id = "12345678-1234-4234-8234-123456789abc"

    observed = reconciler.reconcile("periodic", clock, frozenset({request_id}))

    assert observed.blocked_roles == frozenset()
    assert prepare.exists()
    assert next((root / "image/observations").iterdir()).exists()

    second_root = tmp_path / "second-imports"
    _layout(second_root)
    repository = SQLiteRepository(tmp_path / "second.db", clock=clock)
    repository.initialize()
    second = StagingReconciler(second_root.resolve(), repository, _secure_fs)
    drop = second_root / "image/drop" / f"image--{request_id}--{'a' * 32}.stage"
    _write_bundle(drop, ClientDataRole.IMAGE, request_id, b"image", clock)

    active = second.reconcile("before-command", clock, frozenset({request_id}))

    assert active.blocked_roles == frozenset()
    assert drop.exists()


def test_quarantine_rename_failure_writes_recovery_evidence_and_blocks_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    invalid = root / "image/prepare/invalid"
    invalid.mkdir()
    real_replace = _secure_fs.rename_noreplace

    def replace(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        if source_name == invalid.name:
            raise OSError("forced quarantine rename failure")
        real_replace(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(_secure_fs, "rename_noreplace", replace)

    result = reconciler.reconcile("startup", clock)

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].status.value == "RECOVERY_REQUIRED"
    assert result.evidence[0].reason_code.value == "QUARANTINE_FAILED"
    assert any(path.name.startswith("recovery-") for path in (root / "image/quarantine").iterdir())


def test_observation_delete_failure_persists_terminal_fact_and_separate_recovery_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    prepare.rmdir()
    os.close(descriptor)
    observation = next((root / "image/observations").iterdir())
    real_unlink = Path.unlink

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == observation:
            raise OSError("forced observation delete failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(DrawingMachineError) as error:
        reconciler.reconcile("periodic", clock)

    assert error.value.payload.code == "OBSERVATION_DELETE_FAILED"
    assert '"status":"CLIENT_ABORTED"' in observation.read_text(encoding="utf-8")
    assert any(path.name.startswith("recovery-") for path in (root / "image/quarantine").iterdir())
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retried = reconciler.reconcile("periodic", clock)
    assert observation.exists()
    assert retried.blocked_roles == frozenset({ClientDataRole.IMAGE})


def test_closed_reconciler_edges_and_observation_cas_fail_closed(tmp_path: Path) -> None:
    clock = _Clock()
    with pytest.raises(ValueError):
        StagingReconciler(Path("relative"), object(), _secure_fs)  # type: ignore[arg-type]
    reconciler, root = _reconciler(tmp_path, clock)
    reconciler.require_role_ready(ClientDataRole.IMAGE)
    wrong_role = root / "image/prepare" / f"gcode--12345678-1234-4234-8234-123456789abc--{'a' * 32}.prepare"
    wrong_role.mkdir()
    result = reconciler.reconcile("startup", clock)
    assert result.evidence[0].reason_code.value == "INVALID_PREPARE_NAME"

    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    observation_path = next((root / "image/observations").iterdir())
    current = reconciler._read_observation(observation_path)
    invalid_schema = observation_path.with_name("schema-invalid.json")
    schema_value = json.loads(observation_path.read_text(encoding="utf-8"))
    schema_value["schema_version"] = 2
    invalid_schema.write_text(json.dumps(schema_value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        reconciler._read_observation(invalid_schema)
    invalid_schema.unlink()
    invalid_identity = observation_path.with_name("identity-invalid.json")
    identity_value = json.loads(observation_path.read_text(encoding="utf-8"))
    identity_value["bundle_identity"] = []
    invalid_identity.write_text(json.dumps(identity_value), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        reconciler._read_observation(invalid_identity)
    invalid_identity.unlink()
    for field, invalid in (
        ("role", 1),
        ("revision", "0"),
        ("recovery_code", 1),
    ):
        invalid_field = observation_path.with_name(f"{field}-invalid.json")
        field_value = json.loads(observation_path.read_text(encoding="utf-8"))
        field_value[field] = invalid
        invalid_field.write_text(json.dumps(field_value), encoding="utf-8")
        with pytest.raises(ValueError):
            reconciler._read_observation(invalid_field)
        invalid_field.unlink()
    with pytest.raises(DrawingMachineError) as stale:
        reconciler._update_observation(observation_path, replace_observation(current, revision=1), current.status)
    assert stale.value.payload.code == "OBSERVATION_UPDATE_FAILED"
    with pytest.raises(DrawingMachineError) as illegal:
        reconciler._update_observation(observation_path, current, PrepareObservationStatus.ACTIVE)
    assert illegal.value.payload.code == "OBSERVATION_UPDATE_FAILED"

    value = json.loads(observation_path.read_text(encoding="utf-8"))
    value["status"] = "RECOVERY_REQUIRED"
    value["recovery_code"] = "LOCK_RACE"
    observation_path.write_text(json.dumps(value), encoding="utf-8")
    blocked = reconciler.reconcile("periodic", clock)
    assert blocked.blocked_roles == frozenset({ClientDataRole.IMAGE})


def test_malformed_observation_identity_becomes_durable_corruption_evidence(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    observations = root / "image/observations"
    (observations / "malformed.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "image",
                "prepare_bundle_name": "image--12345678-1234-4234-8234-123456789abc--" + "a" * 32 + ".prepare",
                "published_bundle_name": "image--12345678-1234-4234-8234-123456789abc--" + "a" * 32 + ".stage",
                "bundle_identity": [],
                "request_id": "12345678-1234-4234-8234-123456789abc",
                "first_seen_boot_id": "boot-a",
                "first_seen_monotonic_ns": 1,
                "deadline_monotonic_ns": 2,
                "status": "ACTIVE",
                "revision": 0,
                "recovery_code": None,
            }
        ),
        encoding="utf-8",
    )

    result = reconciler.reconcile("periodic", clock)

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert any(item.reason_code.value == "OBSERVATION_CORRUPT" for item in result.evidence)
    assert any(path.name.startswith("recovery-") for path in (root / "image/quarantine").iterdir())


def test_strict_json_bounded_read_and_bundle_lease_hostile_edges(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        reconciliation_module._strict_json(b'{"a":1,"a":2}')
    with pytest.raises(ValueError, match="object"):
        reconciliation_module._strict_json(b"[]")

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"ab")
    descriptor = os.open(oversized, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="exceeded"):
            reconciliation_module._read_at_most(descriptor, 1)
    finally:
        os.close(descriptor)

    request_id = "12345678-1234-4234-8234-123456789abc"
    wrong = tmp_path / f"gcode--{request_id}--{'a' * 32}.stage"
    with pytest.raises(ValueError, match="role"):
        reconciliation_module._read_bundle_lease(wrong, ClientDataRole.IMAGE)

    bundle = tmp_path / f"image--{request_id}--{'a' * 32}.stage"
    bundle.mkdir()
    (bundle / "extra").write_bytes(b"x")
    with pytest.raises(ValueError, match="children"):
        reconciliation_module._read_bundle_lease(bundle, ClientDataRole.IMAGE)
    (bundle / "extra").unlink()
    (bundle / "payload").write_bytes(b"image")
    (bundle / "lease.json").write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(ValueError, match="lease facts"):
        reconciliation_module._read_bundle_lease(bundle, ClientDataRole.IMAGE)

    (bundle / "payload").unlink()
    (bundle / "lease.json").unlink()
    lease = _write_bundle_contents(bundle, ClientDataRole.IMAGE, request_id, b"image", _Clock())
    mismatched = lease.to_json()
    mismatched["request_id"] = "22345678-1234-4234-8234-123456789abc"
    (bundle / "lease.json").write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="authority"):
        reconciliation_module._read_bundle_lease(bundle, ClientDataRole.IMAGE)
    (bundle / "lease.json").write_text(json.dumps(lease.to_json()), encoding="utf-8")
    (bundle / "payload").write_bytes(b"other")
    with pytest.raises(ValueError, match="identity"):
        reconciliation_module._read_bundle_lease(bundle, ClientDataRole.IMAGE)


def test_atomic_evidence_write_rejects_short_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    destination = tmp_path / "evidence.json"
    clock = _Clock()
    reconciler, _ = _reconciler(tmp_path, clock)
    monkeypatch.setattr(reconciliation_module.os, "write", lambda descriptor, contents: 0)
    with pytest.raises(OSError, match="short evidence write"):
        reconciler._atomic_json(destination, {"schema_version": 1})


@pytest.mark.parametrize(
    ("state", "clears"),
    [
        (StagingAdmissionState.REQUEST_DECODED, False),
        (StagingAdmissionState.CLAIMED, True),
    ],
)
def test_missing_prepare_converges_against_indexed_admission(
    tmp_path: Path,
    state: StagingAdmissionState,
    clears: bool,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    prepare.rmdir()
    request_id = "12345678-1234-4234-8234-123456789abc"
    published = prepare.name.removesuffix(".prepare") + ".stage"
    record = StagingAdmissionRecordV1(
        request_id,
        ClientDataRole.IMAGE,
        published,
        PayloadIdentityV1(1, 2, 5, "a" * 64),
        "a" * 64,
        "b" * 64,
        clock.boot_id(),
        100,
        300_000_000_100,
        StagingAdmissionState.REQUEST_DECODED,
        None,
        0,
        "created",
        "updated",
        None,
    )
    repository = reconciler._repository
    repository.register_decoded_staging(record)
    if state is StagingAdmissionState.CLAIMED:
        repository.mark_staging_claimed(request_id, expected_revision=0)

    result = reconciler.reconcile("periodic", clock)

    assert (ClientDataRole.IMAGE not in result.blocked_roles) is clears


def test_drop_admission_digest_mismatch_persists_recovery_marker(tmp_path: Path) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    request_id = "12345678-1234-4234-8234-123456789abc"
    bundle = root / "gcode/drop" / f"gcode--{request_id}--{'a' * 32}.stage"
    bundle.mkdir()
    payload = bundle / "payload"
    payload.write_bytes(b"G21\n")
    status = payload.stat()
    digest = hashlib.sha256(b"G21\n").hexdigest()
    lease = StagingLeaseV1(
        request_id,
        ClientDataRole.GCODE,
        StagingOwnershipState.DISPATCH_UNCERTAIN,
        digest,
        status.st_size,
        PayloadIdentityV1(status.st_dev, status.st_ino, status.st_size, digest),
        clock.boot_id(),
        100,
        300_000_000_100,
    )
    (bundle / "lease.json").write_text(json.dumps(lease.to_json()), encoding="utf-8")
    record = StagingAdmissionRecordV1(
        request_id,
        ClientDataRole.GCODE,
        bundle.name,
        replace(lease.payload_identity, sha256="c" * 64),
        "c" * 64,
        "b" * 64,
        clock.boot_id(),
        100,
        300_000_000_100,
        StagingAdmissionState.REQUEST_DECODED,
        None,
        0,
        "created",
        "updated",
        None,
    )
    reconciler._repository.register_decoded_staging(record)

    result = reconciler.reconcile("periodic", clock)

    assert result.blocked_roles == frozenset({ClientDataRole.GCODE})
    assert result.evidence[0].reason_code.value == "PUBLISH_MISMATCH"


def test_observed_prepare_quarantine_failure_remains_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    real_replace = _secure_fs.rename_noreplace

    def fail_prepare(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        if source_name == prepare.name:
            raise OSError("forced observed quarantine failure")
        real_replace(source_parent_fd, source_name, destination_parent_fd, destination_name)

    monkeypatch.setattr(_secure_fs, "rename_noreplace", fail_prepare)

    result = reconciler.reconcile("periodic", clock)

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].reason_code.value == "QUARANTINE_FAILED"


def test_durable_and_consumed_admitted_bundles_are_idempotently_cleaned(tmp_path: Path) -> None:
    clock = _Clock()
    root = tmp_path / "imports"
    _layout(root)
    request_id = "12345678-1234-4234-8234-123456789abc"
    bundle_name = f"image--{request_id}--{'a' * 32}.stage"
    admitted = root / "image/admitted" / bundle_name
    admitted.parent.chmod(0o700)
    lease = _write_bundle(admitted, ClientDataRole.IMAGE, request_id, b"image", clock)
    durable = StagingAdmissionRecordV1(
        request_id,
        ClientDataRole.IMAGE,
        bundle_name,
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
        clock.boot_id(),
        100,
        300_000_000_100,
        StagingAdmissionState.DURABLY_ADMITTED,
        "job",
        2,
        "created",
        "updated",
        None,
    )

    class Repository:
        def __init__(self, admission: StagingAdmissionRecordV1) -> None:
            self.admission = admission
            self.consumed = False

        def list_staging_admissions(self) -> tuple[StagingAdmissionRecordV1, ...]:
            return (self.admission,)

        def mark_staging_consumed(self, request: str, *, expected_revision: int) -> StagingAdmissionRecordV1:
            assert (request, expected_revision) == (request_id, self.admission.revision)
            self.consumed = True
            self.admission = replace(
                self.admission,
                state=StagingAdmissionState.CONSUMED,
                revision=expected_revision + 1,
            )
            return self.admission

    repository = Repository(durable)
    StagingReconciler(root.resolve(), repository, _secure_fs).reconcile("startup", clock)  # type: ignore[arg-type]
    assert repository.consumed is True
    assert not admitted.exists()

    replayed_lease = _write_bundle(admitted, ClientDataRole.IMAGE, request_id, b"image", clock)
    repository.admission = replace(
        repository.admission,
        payload_identity=replayed_lease.payload_identity,
        source_sha256=replayed_lease.source_sha256,
    )
    StagingReconciler(root.resolve(), repository, _secure_fs).reconcile("periodic", clock)  # type: ignore[arg-type]
    assert not admitted.exists()


@pytest.mark.parametrize("state", [StagingAdmissionState.DURABLY_ADMITTED, StagingAdmissionState.CONSUMED])
def test_durable_cleanup_identity_failure_is_recovery_required(
    state: StagingAdmissionState,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    root = tmp_path / "imports"
    _layout(root)
    request_id = "12345678-1234-4234-8234-123456789abc"
    bundle_name = f"image--{request_id}--{'a' * 32}.stage"
    admitted = root / "image/admitted" / bundle_name
    admitted.parent.chmod(0o700)
    lease = _write_bundle(admitted, ClientDataRole.IMAGE, request_id, b"image", clock)
    admission = StagingAdmissionRecordV1(
        request_id,
        ClientDataRole.IMAGE,
        bundle_name,
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
        clock.boot_id(),
        100,
        300_000_000_100,
        state,
        "job",
        2,
        "created",
        "updated",
        None,
    )

    class Repository:
        def list_staging_admissions(self) -> tuple[StagingAdmissionRecordV1, ...]:
            return (admission,)

        def mark_staging_consumed(self, request: str, *, expected_revision: int) -> StagingAdmissionRecordV1:
            raise AssertionError((request, expected_revision))

    monkeypatch.setattr(
        reconciliation_module,
        "_detach_and_remove_bundle",
        lambda *args: (_ for _ in ()).throw(reconciliation_module.SecureFilesystemError("forced")),
    )

    result = StagingReconciler(root.resolve(), Repository(), _secure_fs).reconcile(  # type: ignore[arg-type]
        "startup", clock
    )

    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].reason_code.value == "QUARANTINE_FAILED"
    assert admitted.exists()


def test_reconciliation_descriptor_helpers_and_quarantine_swap_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(reconciliation_module.SecureFilesystemError, match="cannot be opened"):
        reconciliation_module._open_verified_directory(tmp_path / "absent")
    managed = tmp_path / "managed"
    managed.mkdir()
    real_fstat = reconciliation_module.os.fstat

    def changed_identity(descriptor: int):
        value = real_fstat(descriptor)
        values = list(value)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(reconciliation_module.os, "fstat", changed_identity)
    with pytest.raises(RuntimeError, match="identity"):
        reconciliation_module._open_verified_directory(managed)
    monkeypatch.setattr(reconciliation_module.os, "fstat", real_fstat)

    parent = tmp_path / "parent"
    parent.mkdir()
    bundle = parent / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(_secure_fs, "entry_matches_descriptor", lambda *args, **kwargs: False)
    with pytest.raises(reconciliation_module.SecureFilesystemError, match="identity"):
        reconciliation_module._detach_and_remove_bundle(parent, bundle.name, _secure_fs)
    assert next(parent.glob(".cleanup-*")).exists()

    role = tmp_path / "role"
    role.mkdir()
    role_fd = os.open(role, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileNotFoundError):
            reconciliation_module._open_private_directory(role_fd, role / "private", _secure_fs)
        assert not (role / "private").exists()
    finally:
        os.close(role_fd)

    clock = _Clock()
    reconciler, root = _reconciler(tmp_path / "quarantine", clock)
    invalid = root / "image/prepare/invalid"
    invalid.mkdir()
    real_open_verified = reconciliation_module._open_verified_directory

    def fail_source_open(path: Path) -> int:
        if path == invalid.parent:
            raise reconciliation_module.SecureFilesystemError("forced open")
        return real_open_verified(path)

    monkeypatch.setattr(
        reconciliation_module,
        "_open_verified_directory",
        fail_source_open,
    )
    unopened = reconciler._quarantine_path(
        ClientDataRole.IMAGE,
        invalid,
        None,
        "INVALID_PREPARE_NAME",
        clock,
    )
    assert unopened.reason_code.value == "QUARANTINE_FAILED"
    monkeypatch.setattr(reconciliation_module, "_open_verified_directory", real_open_verified)
    real_stat_at = _secure_fs.stat_at

    def changed_quarantine(parent_fd: int, name: str):
        value = real_stat_at(parent_fd, name)
        if name.endswith(".quarantine"):
            values = list(value)
            values[1] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(_secure_fs, "stat_at", changed_quarantine)
    result = reconciler.reconcile("startup", clock)
    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].reason_code.value == "QUARANTINE_FAILED"


def test_private_directory_post_open_identity_and_acl_errors_close_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    role = tmp_path / "role"
    private = role / "private"
    private.mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    role_fd = os.open(role, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = reconciliation_module.os.fstat
    opened: list[int] = []

    class OpenOnly:
        @staticmethod
        def open_directory_at(parent_fd: int, name: str) -> int:
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
            opened.append(descriptor)
            return descriptor

    def changed_fstat(descriptor: int) -> os.stat_result:
        value = real_fstat(descriptor)
        fields = list(value)
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(reconciliation_module.os, "fstat", changed_fstat)
    try:
        with pytest.raises(reconciliation_module.SecureFilesystemError, match="identity"):
            reconciliation_module._open_private_directory(role_fd, private, OpenOnly())  # type: ignore[arg-type]
    finally:
        os.close(role_fd)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])

    with monkeypatch.context() as scoped:
        scoped.setattr(reconciliation_module.errno, "ENOATTR", 999, raising=False)

        def absent(*_args: object, **_kwargs: object) -> bytes:
            raise OSError(999, "absent")

        scoped.setattr(reconciliation_module.os, "getxattr", absent)
        assert reconciliation_module._private_acl_is_exact(private) is True

    def unreadable(*_args: object, **_kwargs: object) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(reconciliation_module.os, "getxattr", unreadable)
    with pytest.raises(reconciliation_module.SecureFilesystemError, match="cannot be read"):
        reconciliation_module._private_acl_is_exact(private)


def _write_bundle(
    bundle: Path,
    role: ClientDataRole,
    request_id: str,
    contents: bytes,
    clock: _Clock,
) -> StagingLeaseV1:
    bundle.mkdir()
    return _write_bundle_contents(bundle, role, request_id, contents, clock)


def _write_bundle_contents(
    bundle: Path,
    role: ClientDataRole,
    request_id: str,
    contents: bytes,
    clock: _Clock,
) -> StagingLeaseV1:
    payload = bundle / "payload"
    payload.write_bytes(contents)
    value = payload.stat()
    digest = hashlib.sha256(contents).hexdigest()
    lease = StagingLeaseV1(
        request_id,
        role,
        StagingOwnershipState.DISPATCH_UNCERTAIN,
        digest,
        len(contents),
        PayloadIdentityV1(value.st_dev, value.st_ino, value.st_size, digest),
        clock.boot_id(),
        100,
        300_000_000_100,
    )
    (bundle / "lease.json").write_text(json.dumps(lease.to_json()), encoding="utf-8")
    return lease


def test_remaining_reconciler_exception_statements_are_durable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    assert reconciler.blocked_roles == frozenset()

    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    observation = next((root / "image/observations").iterdir())
    value = json.loads(observation.read_text(encoding="utf-8"))
    value["status"] = "CLIENT_ABORTED"
    observation.write_text(json.dumps(value), encoding="utf-8")
    real_unlink = Path.unlink

    def fail_terminal_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == observation:
            raise OSError("forced terminal cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_terminal_unlink)
    result = reconciler.reconcile("periodic", clock)
    assert result.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert result.evidence[0].reason_code.value == "OBSERVATION_DELETE_FAILED"


def test_prepare_open_failure_abort_invalid_bundle_and_parent_fsync_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    reconciler, root = _reconciler(tmp_path, clock)
    prepare = _prepare(root)
    descriptor = os.open(prepare, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reconciler.reconcile("startup", clock)
    os.close(descriptor)
    real_open = reconciliation_module.os.open

    def fail_prepare_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path) == prepare:
            raise OSError("forced prepare open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(reconciliation_module.os, "open", fail_prepare_open)
    opened = reconciler.reconcile("periodic", clock)
    assert opened.blocked_roles == frozenset({ClientDataRole.IMAGE})
    assert opened.evidence[0].reason_code.value == "LOCK_RACE"
    monkeypatch.setattr(reconciliation_module.os, "open", real_open)

    second_root = tmp_path / "second-imports"
    _layout(second_root)
    second_repository = SQLiteRepository(tmp_path / "second.db", clock=clock)
    second_repository.initialize()
    second = StagingReconciler(second_root.resolve(), second_repository, _secure_fs)
    aborted = _prepare(second_root)
    descriptor = os.open(aborted, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    second.reconcile("startup", clock)
    aborted.rmdir()
    os.close(descriptor)
    assert second.reconcile("periodic", clock).blocked_roles == frozenset()

    invalid = second_root / "gcode/drop" / f"gcode--22345678-1234-4234-8234-123456789abc--{'b' * 32}.stage"
    invalid.mkdir()
    (invalid / "extra").write_bytes(b"x")
    invalid_result = second.reconcile("periodic", clock)
    assert invalid_result.evidence[0].reason_code.value == "INVALID_STAGING_BUNDLE"

    third_root = tmp_path / "third-imports"
    _layout(third_root)
    third_repository = SQLiteRepository(tmp_path / "third.db", clock=clock)
    third_repository.initialize()
    third = StagingReconciler(third_root.resolve(), third_repository, _secure_fs)
    parent_fault = _prepare(third_root)
    descriptor = os.open(parent_fault, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    third.reconcile("startup", clock)
    parent_fault.rmdir()
    os.close(descriptor)
    real_fsync = reconciliation_module._fsync
    observation_parent = third_root / "image/observations"
    observation_syncs = 0

    def fail_second_observation_sync(path: Path) -> None:
        nonlocal observation_syncs
        if path == observation_parent:
            observation_syncs += 1
            if observation_syncs == 1:
                raise OSError("forced observation parent fsync failure")
        real_fsync(path)

    monkeypatch.setattr(reconciliation_module, "_fsync", fail_second_observation_sync)
    with pytest.raises(DrawingMachineError) as fsync_error:
        third.reconcile("periodic", clock)
    assert fsync_error.value.payload.code == "PARENT_FSYNC_FAILED"
    monkeypatch.setattr(reconciliation_module, "_fsync", real_fsync)

    for marker_failure in ("write", "sync"):
        fault_root = tmp_path / f"{marker_failure}-imports"
        _layout(fault_root)
        repository = SQLiteRepository(tmp_path / f"{marker_failure}.db", clock=clock)
        repository.initialize()
        faulted = StagingReconciler(fault_root.resolve(), repository, _secure_fs)
        fault_prepare = _prepare(fault_root)
        descriptor = os.open(fault_prepare, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        faulted.reconcile("startup", clock)
        fault_prepare.rmdir()
        os.close(descriptor)
        observation = next((fault_root / "image/observations").iterdir())
        observation_parent = observation.parent
        quarantine = fault_root / "image/quarantine"
        real_atomic_json = faulted._atomic_json

        def fail_observation_parent_sync(path: Path, parent: Path = observation_parent) -> None:
            if path == parent:
                raise OSError("forced observation parent fsync failure")
            real_fsync(path)

        def fail_parent_sync_marker(
            path: Path,
            value: object,
            quarantine_root: Path = quarantine,
            failure: str = marker_failure,
            atomic_json: Callable[[Path, object], None] = real_atomic_json,
        ) -> None:
            if (
                path.parent == quarantine_root
                and isinstance(value, dict)
                and value.get("reason_code") == "PARENT_FSYNC_FAILED"
            ):
                if failure == "sync":
                    atomic_json(path, value)
                raise OSError(f"forced recovery marker {failure} failure")
            atomic_json(path, value)

        monkeypatch.setattr(reconciliation_module, "_fsync", fail_observation_parent_sync)
        monkeypatch.setattr(faulted, "_atomic_json", fail_parent_sync_marker)
        with pytest.raises(DrawingMachineError) as double_fault:
            faulted.reconcile("periodic", clock)
        assert double_fault.value.payload.code == "PARENT_FSYNC_FAILED"

        durable_facts = tuple((fault_root / "image/observations").iterdir()) + tuple(quarantine.glob("recovery-*.json"))
        monkeypatch.setattr(reconciliation_module, "_fsync", real_fsync)
        restarted = StagingReconciler(fault_root.resolve(), repository, _secure_fs)
        restarted_result = restarted.reconcile("startup", clock)
        repeated_result = restarted.reconcile("periodic", clock)

        assert durable_facts
        assert restarted_result.blocked_roles == frozenset({ClientDataRole.IMAGE})
        assert repeated_result.blocked_roles == frozenset({ClientDataRole.IMAGE})


def replace_observation(value: PrepareObservationV1, *, revision: int) -> PrepareObservationV1:
    return PrepareObservationV1(
        value.role,
        value.prepare_bundle_name,
        value.published_bundle_name,
        value.bundle_identity,
        value.request_id,
        value.first_seen_boot_id,
        value.first_seen_monotonic_ns,
        value.deadline_monotonic_ns,
        value.status,
        revision,
        value.recovery_code,
    )
