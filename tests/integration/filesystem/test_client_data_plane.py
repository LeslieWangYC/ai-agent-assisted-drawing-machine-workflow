from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import drawingmachine.adapters.filesystem.client_data as client_data_module
import drawingmachine.config.service_access as service_access_module
from drawingmachine.adapters.filesystem.client_data import (
    FilesystemClientInputStaging,
    FilesystemServiceClientData,
)
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.config.openclaw_deployment import PosixGroupV1, PosixPrincipalV1
from drawingmachine.config.service_access import ServiceAccessConfigV1, validate_automation_data_endpoints
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.client_data import (
    ClientDataRole,
    DispatchWriteOutcome,
    StagingAdmissionState,
    StagingRecoveryReasonCode,
)
from drawingmachine.service.access_policy import LinuxAccessFactsInspector


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 14, tzinfo=UTC)

    def boot_id(self) -> str:
        return "boot-a"

    def monotonic_ns(self) -> int:
        return 100


class _Reader:
    def read_bytes(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("not used")


class _StaticReader:
    def __init__(self, contents: bytes) -> None:
        self.contents = contents

    def read_bytes(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        return self.contents


def test_automation_data_endpoints_are_exact_absolute_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PosixPrincipalV1(os.getuid(), os.getgid())
    automation = PosixPrincipalV1(service.uid + 10_001, service.gid + 10_001)
    operator = PosixPrincipalV1(service.uid + 10_002, service.gid + 10_002)
    config = ServiceAccessConfigV1(
        service,
        automation,
        operator,
        PosixGroupV1("drawingmachine-connect", service.gid + 10_003),
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
    config.automation_import_root.mkdir(parents=True)
    config.automation_export_root.mkdir(parents=True)
    config.automation_import_endpoint.parent.mkdir(parents=True)
    config.automation_import_endpoint.symlink_to(config.automation_import_root)
    config.automation_export_endpoint.symlink_to(config.automation_export_root)
    real_lstat = os.lstat

    def modeled_lstat(path: os.PathLike[str] | str) -> os.stat_result:
        result = real_lstat(path)
        if Path(path) in {config.automation_import_endpoint, config.automation_export_endpoint}:
            values = list(result)
            values[4] = config.automation_principal.uid
            return os.stat_result(values)
        if Path(path) in {config.automation_import_root, config.automation_export_root}:
            values = list(result)
            values[0] = stat.S_IFDIR | 0o2770
            values[4] = config.service_principal.uid
            values[5] = config.connect_group.gid
            return os.stat_result(values)
        return result

    monkeypatch.setattr(service_access_module.os, "lstat", modeled_lstat)
    inspector = LinuxAccessFactsInspector()
    validated = validate_automation_data_endpoints(config, inspector=inspector)
    assert validated.import_endpoint_identity == (
        real_lstat(config.automation_import_endpoint).st_dev,
        real_lstat(config.automation_import_endpoint).st_ino,
    )
    assert validated.export_endpoint_identity == (
        real_lstat(config.automation_export_endpoint).st_dev,
        real_lstat(config.automation_export_endpoint).st_ino,
    )
    assert validated.import_root_identity == (
        real_lstat(config.automation_import_root).st_dev,
        real_lstat(config.automation_import_root).st_ino,
    )
    real_getxattr = os.getxattr
    default_acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, 0xFFFFFFFF) for tag, permissions in ((1, 7), (4, 7), (32, 0))
    )
    monkeypatch.setattr(
        os,
        "getxattr",
        lambda path, attribute, **kwargs: (
            default_acl if attribute == "system.posix_acl_default" else real_getxattr(path, attribute, **kwargs)
        ),
    )
    assert validate_automation_data_endpoints(config, inspector=inspector) != validated
    monkeypatch.setattr(os, "getxattr", real_getxattr)
    with pytest.raises(DrawingMachineError):
        validate_automation_data_endpoints(object(), inspector=inspector)  # type: ignore[arg-type]
    monkeypatch.setattr(service_access_module.os, "readlink", lambda _path: str(tmp_path / "wrong-target"))
    with pytest.raises(DrawingMachineError, match="identity"):
        validate_automation_data_endpoints(config, inspector=inspector)
    monkeypatch.setattr(service_access_module.os, "readlink", lambda _path: "relative")
    with pytest.raises(DrawingMachineError, match="identity"):
        validate_automation_data_endpoints(config, inspector=inspector)
    monkeypatch.setattr(service_access_module.os, "lstat", lambda _path: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DrawingMachineError, match="inspected"):
        validate_automation_data_endpoints(config, inspector=inspector)


def test_client_production_construction_rejects_default_acl_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.bootstrap as bootstrap_module
    import drawingmachine.cli.service_access as client_access_module

    principal = PosixPrincipalV1(os.getuid(), os.getgid())
    config = ServiceAccessConfigV1(
        principal,
        principal,
        PosixPrincipalV1(principal.uid + 1, principal.gid + 1),
        PosixGroupV1("drawingmachine-connect", principal.gid),
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
    for root in (config.automation_import_root, config.automation_export_root):
        root.mkdir(parents=True)
        root.chmod(0o2770)
    config.automation_import_endpoint.parent.mkdir(parents=True)
    config.automation_import_endpoint.symlink_to(config.automation_import_root)
    config.automation_export_endpoint.symlink_to(config.automation_export_root)

    class DriftingInspector:
        def __init__(self) -> None:
            self.calls = 0

        def inspect(self, path: Path):
            self.calls += 1
            value = path.lstat()
            return SimpleNamespace(
                path=path,
                kind="directory",
                device=value.st_dev,
                inode=value.st_ino,
                uid=value.st_uid,
                gid=value.st_gid,
                mode=stat.S_IMODE(value.st_mode),
                acl=(),
                default_acl=() if self.calls <= 2 else (("group_obj", None, 0o5),),
            )

    inspector = DriftingInspector()
    monkeypatch.setattr(bootstrap_module, "LinuxAccessFactsInspector", lambda: inspector)
    monkeypatch.setattr(
        client_access_module,
        "load_client_access_snapshot",
        lambda _paths: SimpleNamespace(config=config, client_principal=principal),
    )

    with pytest.raises(DrawingMachineError) as error:
        bootstrap_module.build_client_input_staging(SimpleNamespace())  # type: ignore[arg-type]

    assert error.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"


@pytest.mark.parametrize("drift", ["acl", "default_acl"])
def test_service_construction_freezes_acl_facts_before_first_use(tmp_path: Path, drift: str) -> None:
    principal = PosixPrincipalV1(os.getuid(), os.getgid())
    config = ServiceAccessConfigV1(
        principal,
        principal,
        PosixPrincipalV1(principal.uid + 1, principal.gid + 1),
        PosixGroupV1("drawingmachine-connect", principal.gid),
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
    for root in (config.automation_import_root, config.automation_export_root):
        root.mkdir(parents=True)
        root.chmod(0o2770)
    config.automation_import_endpoint.parent.mkdir(parents=True)
    config.automation_import_endpoint.symlink_to(config.automation_import_root)
    config.automation_export_endpoint.symlink_to(config.automation_export_root)
    facts = {"acl": (), "default_acl": ()}

    class Inspector:
        def __init__(self) -> None:
            self.calls = 0

        def inspect(self, path: Path):
            self.calls += 1
            value = path.lstat()
            return SimpleNamespace(
                path=path,
                kind="directory",
                device=value.st_dev,
                inode=value.st_ino,
                uid=value.st_uid,
                gid=value.st_gid,
                mode=stat.S_IMODE(value.st_mode),
                acl=facts["acl"],
                default_acl=facts["default_acl"],
            )

    inspector = Inspector()
    expected = validate_automation_data_endpoints(config, inspector=inspector)

    def validate() -> None:
        if validate_automation_data_endpoints(config, inspector=inspector) != expected:
            raise DrawingMachineError(
                ErrorPayload(
                    "CLIENT_DATA_IDENTITY_DRIFT",
                    ErrorCategory.PERMISSION,
                    "automation data endpoint or canonical root identity changed",
                    False,
                    {},
                )
            )

    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    permissions = SimpleNamespace(
        validate_phase_directory=lambda *_args: None,
        validate_private_directory=lambda *_args: None,
        validate_client_bundle=lambda *_args: None,
        validate_client_file=lambda *_args: None,
        validate_export_file=lambda *_args: None,
        validate_export_directory=lambda *_args: None,
    )
    service = FilesystemServiceClientData(
        config.automation_import_root,
        config.automation_export_root,
        repository=repository,
        artifact_reader=_Reader(),
        permission_oracle=permissions,
        utc_now=_Clock().now,
        endpoint_validator=validate,
    )
    assert inspector.calls == 4
    facts[drift] = (("group_obj", None, 0o5),)

    with pytest.raises(DrawingMachineError) as error:
        service.bind_request_envelope(
            "12345678-1234-4234-8234-123456789abc",
            ClientDataRole.GCODE,
            {"schema_version": 1},
        )

    assert error.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"


def test_service_registers_and_claims_exact_dispatch_uncertain_bundle(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports, exports)
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))

    decoded = service.register_decoded(request_id, staged.role, staged.payload_path)
    claimed = service.claim_gcode(request_id, staged.payload_path)

    assert decoded.state is StagingAdmissionState.REQUEST_DECODED
    assert claimed.payload_path.read_bytes() == b"G21\n"
    assert repository.get_staging_admission(request_id).state is StagingAdmissionState.CLAIMED  # type: ignore[union-attr]
    assert not staged.bundle_path.exists()


def test_request_envelope_binding_is_append_only_exact_and_does_not_store_paths(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    repository = SQLiteRepository(database, clock=_Clock())
    repository.initialize()
    service = FilesystemServiceClientData.for_test(
        tmp_path / "imports",
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    request_id = "12345678-1234-4234-8234-123456789abc"
    envelope = {"schema_version": 1, "path": "/private/automation/input.gcode"}

    service.bind_request_envelope(request_id, ClientDataRole.GCODE, envelope)
    service.bind_request_envelope(request_id, ClientDataRole.GCODE, dict(envelope))
    with pytest.raises(DrawingMachineError, match="not exact"):
        service.bind_request_envelope(request_id, "gcode", envelope)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="canonical"):
        service.bind_request_envelope(request_id, ClientDataRole.GCODE, {"bad": object()})  # type: ignore[dict-item]
    with pytest.raises(DrawingMachineError) as mismatch:
        service.bind_request_envelope(
            request_id,
            ClientDataRole.GCODE,
            {"schema_version": 1, "path": "/private/automation/other.gcode"},
        )

    assert mismatch.value.payload.code == "STAGING_REPLAY_MISMATCH"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT event_type, request_id, payload_json FROM audit_events WHERE event_type = ?",
            ("staging.request-envelope.v1",),
        ).fetchone()
    assert row is not None
    assert row[:2] == ("staging.request-envelope.v1", request_id)
    assert "/private/" not in row[2]
    assert set(json.loads(row[2])) == {"schema_version", "role", "envelope_sha256"}


@pytest.mark.parametrize("digests", [("a" * 64, "a" * 64), ("a" * 64, "b" * 64)])
def test_request_envelope_binding_has_one_concurrent_primary_key_winner(
    tmp_path: Path,
    digests: tuple[str, str],
) -> None:
    database = tmp_path / "state.db"
    seed = SQLiteRepository(database, clock=_Clock())
    seed.initialize()
    seed.close()
    event_id = "fixed-request-envelope-event"
    request_id = "12345678-1234-4234-8234-123456789abc"

    def bind(digest: str) -> str:
        repository = SQLiteRepository(database, clock=_Clock())
        try:
            return repository.bind_staging_request_envelope(event_id, request_id, "gcode", digest)
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(bind, digests))

    assert results[0] == results[1]
    assert results[0] in digests


def test_definitely_unsent_cleanup_identity_uncertainty_preserves_detached_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    staged = client.stage_gcode(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    identities = iter((True, False))
    monkeypatch.setattr(client_data_module, "entry_matches_descriptor", lambda *args, **kwargs: next(identities))

    with pytest.raises(DrawingMachineError) as error:
        client.discard_definitely_unsent(staged, DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES)

    assert error.value.payload.code == "CLIENT_DATA_CLEANUP_FAILED"
    detached = tuple(staged.bundle_path.parent.glob(".cleanup-*"))
    assert len(detached) == 1
    assert (detached[0] / "payload").read_bytes() == b"G21\n"


def test_claim_rejects_private_ancestor_symlink_without_touching_external_directory(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    admitted = imports / "gcode/admitted"
    admitted.rmdir()
    admitted.symlink_to(external, target_is_directory=True)

    with pytest.raises(DrawingMachineError):
        service.claim_gcode(request_id, staged.payload_path)

    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert list(external.iterdir()) == []
    assert staged.bundle_path.exists()


def test_exact_replay_resumes_the_existing_private_claim(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    first = service.claim_gcode(request_id, staged.payload_path)

    replayed = service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    second = service.claim_gcode(request_id, staged.payload_path)

    assert replayed.state is StagingAdmissionState.CLAIMED
    assert second == first
    with pytest.raises(DrawingMachineError) as mismatch:
        service.register_decoded(request_id, ClientDataRole.IMAGE, staged.payload_path)
    assert mismatch.value.payload.code == "STAGING_REPLAY_MISMATCH"


def test_service_rejects_raw_path_outside_role_dropbox(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    for role in ClientDataRole:
        for phase in ("prepare", "drop"):
            directory = imports / role.value / phase
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o2770)
    exports.mkdir()
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()

    def deny_direct_read(*_args: object) -> None:
        raise DrawingMachineError(
            ErrorPayload(
                "CLIENT_DATA_PERMISSION_DENIED",
                ErrorCategory.PERMISSION,
                "service cannot directly read ordinary automation-private data",
                False,
                {},
            )
        )

    permissions = SimpleNamespace(
        validate_phase_directory=deny_direct_read,
        validate_private_directory=deny_direct_read,
        validate_client_bundle=deny_direct_read,
        validate_client_file=deny_direct_read,
        validate_export_file=deny_direct_read,
        validate_export_directory=deny_direct_read,
    )
    service = FilesystemServiceClientData(
        imports,
        exports,
        repository=repository,
        artifact_reader=_Reader(),
        permission_oracle=permissions,
        utc_now=_Clock().now,
        endpoint_validator=lambda: None,
    )
    outside = tmp_path / "ordinary-private.png"
    outside.write_bytes(b"image")

    with pytest.raises(DrawingMachineError) as error:
        service.register_decoded(
            "12345678-1234-4234-8234-123456789abc",
            ClientDataRole.IMAGE,
            outside,
        )

    assert error.value.payload.code == "STAGING_PATH_INVALID"


def test_automation_production_adapter_denies_service_private_processed_artifact(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    service_private = tmp_path / "service-private"
    for root in (imports, service_private):
        root.mkdir()
    contents = b"processed"
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )
    private_artifact = service_private / "jobs/job/processed.png"
    private_artifact.parent.mkdir(parents=True)
    private_artifact.write_bytes(contents)

    def deny_private(path: Path) -> None:
        assert path == private_artifact
        raise DrawingMachineError(
            ErrorPayload(
                "CLIENT_DATA_PERMISSION_DENIED",
                ErrorCategory.PERMISSION,
                "automation cannot read service-private job data",
                False,
                {},
            )
        )

    permissions = SimpleNamespace(
        validate_phase_directory=lambda *_args: None,
        validate_private_directory=lambda *_args: None,
        validate_client_bundle=lambda *_args: None,
        validate_client_file=lambda *_args: None,
        validate_export_file=deny_private,
        validate_export_directory=deny_private,
    )
    client = FilesystemClientInputStaging(
        imports,
        service_private,
        permission_oracle=permissions,
    )

    with pytest.raises(DrawingMachineError) as error:
        client.read_exported_artifact("job", artifact)

    assert error.value.payload.code == "CLIENT_DATA_PERMISSION_DENIED"


def test_service_publishes_only_authorized_immutable_export_and_client_revalidates_it(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    reader = _StaticReader(contents)
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=reader,
        utc_now=_Clock().now,
    )
    client = FilesystemClientInputStaging.for_test(imports, exports)
    artifact = ArtifactRef(
        "processed_image",
        "artifacts/attempt/processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )

    published = service.publish_artifacts("job-1", (artifact,))
    reader.contents = b"tampered canonical"
    assert service.publish_artifacts("job-1", (artifact,))[0].export_path == published[0].export_path

    assert len(published) == 1
    assert client.read_exported_artifact("job-1", artifact) == contents
    published[0].export_path.write_bytes(b"changed")
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("job-1", artifact)
    with pytest.raises(DrawingMachineError) as collision:
        service.publish_artifacts("job-1", (artifact,))
    assert collision.value.payload.code == "CLIENT_EXPORT_COLLISION"
    private = ArtifactRef("provider_response", "provider.json", "a" * 64, 1, "application/json")
    with pytest.raises(DrawingMachineError) as error:
        service.publish_artifacts("job-1", (private,))
    assert error.value.payload.code == "CLIENT_EXPORT_DENIED"


def test_export_rejects_jobs_ancestor_symlink_without_external_side_effects(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    (exports / "jobs").rmdir()
    (exports / "jobs").symlink_to(external, target_is_directory=True)
    artifact = ArtifactRef(
        "processed_image",
        "artifacts/attempt/processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )

    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job-symlink", (artifact,))

    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert list(external.iterdir()) == []


def test_export_rejects_frozen_root_ancestor_replacement(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    imports = trusted / "imports"
    exports = trusted / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    client = FilesystemClientInputStaging.for_test(imports, exports)
    displaced = tmp_path / "displaced"
    trusted.rename(displaced)
    trusted.mkdir()
    (trusted / "imports").mkdir()
    (trusted / "exports").mkdir()
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )

    with pytest.raises(DrawingMachineError) as error:
        service.publish_artifacts("job", (artifact,))

    assert error.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"
    assert list((trusted / "exports").iterdir()) == []
    with pytest.raises(DrawingMachineError) as client_error:
        client.read_exported_artifact("job", artifact)
    assert client_error.value.payload.code == "CLIENT_DATA_IDENTITY_DRIFT"


def test_client_export_descriptor_open_failure_closes_only_walked_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    imports.mkdir()
    (exports / "jobs/job").mkdir(parents=True)
    permissions = SimpleNamespace(
        validate_phase_directory=lambda *_args: None,
        validate_private_directory=lambda *_args: None,
        validate_client_bundle=lambda *_args: None,
        validate_client_file=lambda *_args: None,
        validate_export_file=lambda *_args: None,
        validate_export_directory=lambda *_args: None,
    )
    client = FilesystemClientInputStaging(imports, exports, permission_oracle=permissions)
    artifact = ArtifactRef("processed_image", "processed.png", "a" * 64, 1, "image/png")
    monkeypatch.setattr(
        client_data_module,
        "open_regular_at",
        lambda *_args: (_ for _ in ()).throw(client_data_module.SecureFilesystemError("forced open")),
    )

    with pytest.raises(client_data_module.SecureFilesystemError, match="forced open"):
        client.read_exported_artifact("job", artifact)


@pytest.mark.parametrize("fault", ["write", "fsync", "link"])
def test_export_failure_removes_only_its_identity_proven_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )
    if fault == "write":
        monkeypatch.setattr(
            client_data_module,
            "_write_all",
            lambda *_args: (_ for _ in ()).throw(OSError("forced export write")),
        )
    elif fault == "fsync":
        real_fsync = os.fsync

        def fail_export_fsync(descriptor: int) -> None:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("forced export fsync")
            real_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_export_fsync)
    else:
        monkeypatch.setattr(os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("forced link")))

    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (artifact,))

    assert list(exports.rglob(".export-*")) == []


def test_export_failure_preserves_attacker_replacement_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )

    def replace_temporary(descriptor: int, _contents: bytes) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        path.unlink()
        path.write_bytes(b"attacker")
        raise OSError("forced post-replacement failure")

    monkeypatch.setattr(client_data_module, "_write_all", replace_temporary)

    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (artifact,))

    replacements = list(exports.rglob(".export-*"))
    assert len(replacements) == 1
    assert replacements[0].read_bytes() == b"attacker"


def test_export_temporary_create_collision_preserves_existing_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    parent = exports / "jobs/job"
    parent.mkdir(parents=True)
    (exports / "jobs").chmod(0o2750)
    parent.chmod(0o2750)
    collision = parent / ".export-fixed"
    collision.write_bytes(b"existing")
    monkeypatch.setattr(client_data_module, "hidden_name", lambda _prefix: collision.name)
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )

    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (artifact,))

    assert collision.read_bytes() == b"existing"


def test_identical_concurrent_export_winner_is_revalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = b"processed"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    service = FilesystemServiceClientData.for_test(
        tmp_path / "imports",
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    artifact = ArtifactRef(
        "processed_image",
        "artifacts/attempt/processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )
    link = os.link

    def concurrent_link(source: Path | str, destination: Path | str, **kwargs: object) -> None:
        link(source, destination, **kwargs)
        if isinstance(source, Path):
            source.unlink()
        else:
            os.unlink(source, dir_fd=kwargs.get("src_dir_fd"))  # type: ignore[arg-type]
        raise FileExistsError

    monkeypatch.setattr(os, "link", concurrent_link)

    published = service.publish_artifacts("job-race", (artifact,))

    assert published[0].export_path.read_bytes() == contents


def test_export_and_reconcile_authority_reject_open_inputs(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    client = FilesystemClientInputStaging.for_test(imports, exports)
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )
    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("", (artifact,))
    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (artifact, artifact))
    wrong = ArtifactRef("processed_image", "wrong.png", "a" * 64, len(contents), "image/png")
    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (wrong,))
    unsafe = SimpleNamespace(
        role="processed_image",
        relative_path="../escape",
        sha256=hashlib.sha256(contents).hexdigest(),
        size_bytes=len(contents),
        media_type="image/png",
    )
    with pytest.raises(DrawingMachineError):
        service.publish_artifacts("job", (unsafe,))  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("", artifact)
    private = SimpleNamespace(
        role="provider_response",
        relative_path="provider.json",
        sha256="a" * 64,
        size_bytes=1,
        media_type="application/json",
    )
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("job", private)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError) as unavailable:
        service.reconcile("startup", _Clock(), frozenset(), ())
    assert unavailable.value.payload.code == "STAGING_RECONCILER_UNAVAILABLE"


def test_service_replay_rejects_recovery_missing_and_recreated_authority(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    decoded = service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)

    recovery = decoded.__class__(
        decoded.request_id,
        decoded.role,
        decoded.bundle_name,
        decoded.payload_identity,
        decoded.source_sha256,
        decoded.lease_sha256,
        decoded.publisher_boot_id,
        decoded.published_monotonic_ns,
        decoded.deadline_monotonic_ns,
        StagingAdmissionState.RECOVERY_REQUIRED,
        None,
        decoded.revision,
        decoded.created_at_utc,
        decoded.updated_at_utc,
        StagingRecoveryReasonCode.LOCK_RACE,
    )
    with pytest.raises(DrawingMachineError):
        service._validate_existing_replay(recovery, ClientDataRole.GCODE, staged.payload_path)

    staged.bundle_path.rename(tmp_path / "removed")
    with pytest.raises(DrawingMachineError):
        service._validate_existing_replay(decoded, ClientDataRole.GCODE, staged.payload_path)

    consumed = decoded.__class__(
        decoded.request_id,
        decoded.role,
        decoded.bundle_name,
        decoded.payload_identity,
        decoded.source_sha256,
        decoded.lease_sha256,
        decoded.publisher_boot_id,
        decoded.published_monotonic_ns,
        decoded.deadline_monotonic_ns,
        StagingAdmissionState.CONSUMED,
        "job",
        decoded.revision,
        decoded.created_at_utc,
        decoded.updated_at_utc,
        None,
    )
    service._validate_existing_replay(consumed, ClientDataRole.GCODE, staged.payload_path)
    staged.bundle_path.mkdir()
    with pytest.raises(DrawingMachineError):
        service._validate_existing_replay(consumed, ClientDataRole.GCODE, staged.payload_path)


def test_service_claim_and_bundle_faults_fail_before_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))

    with pytest.raises(DrawingMachineError) as unregistered:
        service.claim_gcode(request_id, staged.payload_path)
    assert unregistered.value.payload.code == "STAGING_STATE_INVALID"
    decoded = service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    with pytest.raises(DrawingMachineError):
        service.consume(request_id, expected_revision=decoded.revision)
    with pytest.raises(DrawingMachineError):
        service._validate_service_bundle(request_id, "gcode", staged.payload_path, phase="drop")  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError):
        service._validate_service_bundle(request_id, ClientDataRole.GCODE, staged.bundle_path, phase="drop")
    with pytest.raises(DrawingMachineError):
        service._validate_service_bundle(
            "22345678-1234-4234-8234-123456789abc",
            ClientDataRole.GCODE,
            staged.payload_path,
            phase="drop",
        )
    (staged.bundle_path / "extra").write_bytes(b"x")
    with pytest.raises(DrawingMachineError):
        service._validate_service_bundle(request_id, ClientDataRole.GCODE, staged.payload_path, phase="drop")
    (staged.bundle_path / "extra").unlink()
    with pytest.raises(DrawingMachineError):
        service._validate_existing_replay(
            replace(decoded, source_sha256="c" * 64),
            ClientDataRole.GCODE,
            staged.payload_path,
        )

    real_rename = client_data_module.rename_noreplace
    monkeypatch.setattr(
        client_data_module,
        "rename_noreplace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("forced rename")),
    )
    with pytest.raises(DrawingMachineError) as claim:
        service.claim_gcode(request_id, staged.payload_path)
    assert claim.value.payload.code == "STAGING_CLAIM_FAILED"
    monkeypatch.setattr(client_data_module, "rename_noreplace", real_rename)

    real_open_directory = client_data_module.open_directory_at
    monkeypatch.setattr(
        client_data_module,
        "open_directory_at",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("forced source open")),
    )
    with pytest.raises(DrawingMachineError) as unopened:
        service.claim_gcode(request_id, staged.payload_path)
    assert unopened.value.payload.code == "STAGING_CLAIM_FAILED"
    monkeypatch.setattr(client_data_module, "open_directory_at", real_open_directory)


def test_client_export_size_digest_and_read_identity_are_revalidated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    contents = b"processed"
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_StaticReader(contents),
        utc_now=_Clock().now,
    )
    client = FilesystemClientInputStaging.for_test(imports, exports)
    artifact = ArtifactRef(
        "processed_image",
        "processed.png",
        hashlib.sha256(contents).hexdigest(),
        len(contents),
        "image/png",
    )
    service.publish_artifacts("job", (artifact,))
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("job", replace(artifact, size_bytes=len(contents) + 1))
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("job", replace(artifact, sha256="a" * 64))

    real_fstat = os.fstat
    regular_calls = 0

    def changed_fstat(descriptor: int):
        nonlocal regular_calls
        value = real_fstat(descriptor)
        if stat.S_ISREG(value.st_mode):
            regular_calls += 1
        if regular_calls == 3:
            values = list(value)
            values[8] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(os, "fstat", changed_fstat)
    with pytest.raises(DrawingMachineError):
        client.read_exported_artifact("job", artifact)


def test_service_constructor_callback_and_claim_identity_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    with pytest.raises(ValueError):
        FilesystemServiceClientData(
            Path("relative"),
            tmp_path / "exports",
            repository=repository,
            artifact_reader=_Reader(),
            permission_oracle=object(),  # type: ignore[arg-type]
            utc_now=_Clock().now,
            endpoint_validator=lambda: None,
        )
    imports = tmp_path / "imports"
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    service._reconcile_callback = lambda trigger, clock, active, durable: ()
    assert service.reconcile("startup", _Clock(), frozenset(), ()) == ()

    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    decoded = service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    real_get = repository.get_staging_admission
    monkeypatch.setattr(
        repository,
        "get_staging_admission",
        lambda value: replace(decoded, lease_sha256="c" * 64),
    )
    with pytest.raises(DrawingMachineError) as drift:
        service.claim_gcode(request_id, staged.payload_path)
    assert drift.value.payload.code == "STAGING_IDENTITY_DRIFT"
    monkeypatch.setattr(repository, "get_staging_admission", real_get)

    real_stat_at = client_data_module.stat_at

    def changed_claim(parent_fd: int, name: str):
        value = real_stat_at(parent_fd, name)
        if name.endswith(".stage"):
            values = list(value)
            values[1] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(client_data_module, "stat_at", changed_claim)
    with pytest.raises(DrawingMachineError) as changed:
        service.claim_gcode(request_id, staged.payload_path)
    assert changed.value.payload.code == "STAGING_IDENTITY_DRIFT"


def test_claim_rejects_source_descriptor_swap_before_private_move(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    replacement_fd = os.open(replacement, os.O_RDONLY | os.O_DIRECTORY)
    real_open = client_data_module.open_directory_at

    def swapped_open(parent_fd: int, name: str) -> int:
        if name == staged.bundle_name:
            return os.dup(replacement_fd)
        return real_open(parent_fd, name)

    monkeypatch.setattr(client_data_module, "open_directory_at", swapped_open)
    try:
        with pytest.raises(DrawingMachineError) as error:
            service.claim_gcode(request_id, staged.payload_path)
    finally:
        os.close(replacement_fd)
    assert error.value.payload.code == "STAGING_IDENTITY_DRIFT"
    assert staged.bundle_path.exists()


def test_service_bundle_phase_state_request_and_payload_faults(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")

    published = client.stage_gcode(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    with pytest.raises(DrawingMachineError) as state:
        service._validate_service_bundle(
            published.request_id,
            published.role,
            published.payload_path,
            phase="drop",
        )
    assert state.value.payload.code == "STAGING_STATE_INVALID"

    transferred = client.transfer_before_write(
        client.stage_gcode(source, "22345678-1234-4234-8234-123456789abc", _Clock())
    )
    invalid_root = imports / "gcode/invalid"
    invalid_root.mkdir()
    invalid_bundle = invalid_root / transferred.bundle_name
    shutil.copytree(transferred.bundle_path, invalid_bundle)
    with pytest.raises(DrawingMachineError):
        service._validate_service_bundle(
            transferred.request_id,
            transferred.role,
            invalid_bundle / "payload",
            phase="invalid",
        )

    lease_path = transferred.bundle_path / "lease.json"
    lease_value = transferred.lease.to_json()
    lease_value["request_id"] = "32345678-1234-4234-8234-123456789abc"
    lease_path.write_text(json.dumps(lease_value), encoding="utf-8")
    with pytest.raises(DrawingMachineError) as request:
        service._validate_service_bundle(
            transferred.request_id,
            transferred.role,
            transferred.payload_path,
            phase="drop",
        )
    assert request.value.payload.code == "STAGING_REPLAY_MISMATCH"
    lease_path.write_text(json.dumps(transferred.lease.to_json()), encoding="utf-8")
    transferred.payload_path.write_bytes(b"G20\n")
    with pytest.raises(DrawingMachineError) as identity:
        service._validate_service_bundle(
            transferred.request_id,
            transferred.role,
            transferred.payload_path,
            phase="drop",
        )
    assert identity.value.payload.code == "STAGING_IDENTITY_DRIFT"


def test_durable_cleanup_rejects_record_digest_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports)
    service = FilesystemServiceClientData.for_test(
        imports,
        tmp_path / "exports",
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    request_id = "12345678-1234-4234-8234-123456789abc"
    staged = client.transfer_before_write(client.stage_gcode(source, request_id, _Clock()))
    service.register_decoded(request_id, ClientDataRole.GCODE, staged.payload_path)
    service.claim_gcode(request_id, staged.payload_path)
    claimed = repository.get_staging_admission(request_id)
    assert claimed is not None
    durable = replace(
        claimed,
        state=StagingAdmissionState.DURABLY_ADMITTED,
        job_id="job",
        source_sha256="c" * 64,
    )
    monkeypatch.setattr(repository, "get_staging_admission", lambda value: durable)
    with pytest.raises(DrawingMachineError) as drift:
        service.consume(request_id, expected_revision=durable.revision)
    assert drift.value.payload.code == "STAGING_IDENTITY_DRIFT"

    exact_durable = replace(claimed, state=StagingAdmissionState.DURABLY_ADMITTED, job_id="job")
    claimed_bundle = imports / "gcode/admitted" / claimed.bundle_name
    monkeypatch.setattr(repository, "get_staging_admission", lambda value: exact_durable)
    real_detach = client_data_module._detach_and_remove_tree
    monkeypatch.setattr(
        client_data_module,
        "_detach_and_remove_tree",
        lambda *args, **kwargs: (_ for _ in ()).throw(client_data_module.SecureFilesystemError("forced")),
    )
    with pytest.raises(DrawingMachineError) as cleanup:
        service.consume(request_id, expected_revision=exact_durable.revision)
    assert cleanup.value.payload.code == "STAGING_CLEANUP_FAILED"
    monkeypatch.setattr(client_data_module, "_detach_and_remove_tree", real_detach)
    shutil.rmtree(claimed_bundle)
    consumed = replace(exact_durable, state=StagingAdmissionState.CONSUMED, revision=exact_durable.revision + 1)
    monkeypatch.setattr(repository, "mark_staging_consumed", lambda *args, **kwargs: consumed)
    assert service.consume(request_id, expected_revision=exact_durable.revision) == consumed


def test_mode_and_hardlink_drift_fail_before_dispatch_or_decode(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    exports = tmp_path / "exports"
    repository = SQLiteRepository(tmp_path / "state.db", clock=_Clock())
    repository.initialize()
    client = FilesystemClientInputStaging.for_test(imports, exports)
    service = FilesystemServiceClientData.for_test(
        imports,
        exports,
        repository=repository,
        artifact_reader=_Reader(),
        utc_now=_Clock().now,
    )
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G21\n")
    staged = client.stage_gcode(source, "12345678-1234-4234-8234-123456789abc", _Clock())
    (staged.bundle_path / "lease.json").chmod(0o666)
    with pytest.raises(DrawingMachineError) as mode_error:
        client.transfer_before_write(staged)
    assert mode_error.value.payload.code == "CLIENT_DATA_POLICY_INVALID"

    (staged.bundle_path / "lease.json").chmod(0o640)
    transferred = client.transfer_before_write(staged)
    alias = tmp_path / "payload-alias"
    alias.hardlink_to(transferred.payload_path)
    with pytest.raises(DrawingMachineError) as link_error:
        service.register_decoded(transferred.request_id, transferred.role, transferred.payload_path)
    assert link_error.value.payload.code == "CLIENT_DATA_POLICY_INVALID"
