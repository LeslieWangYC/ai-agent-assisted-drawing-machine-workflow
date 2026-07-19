from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID

import pytest

from drawingmachine.ports.client_data import (
    ClientDataRole,
    DispatchWriteOutcome,
    ImportedClientFileV1,
    PayloadIdentityV1,
    PrepareObservationStatus,
    PrepareObservationV1,
    PrepareRecoveryCode,
    PublishedClientArtifactV1,
    StagedClientInputV1,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingLeaseV1,
    StagingOwnershipState,
    StagingRecoveryAction,
    StagingRecoveryEvidenceV1,
    StagingRecoveryReasonCode,
    StagingRecoveryStatus,
    canonical_request_id,
    parse_bundle_name,
    staging_job_id,
)

REQUEST = "12345678-1234-4234-8234-123456789abc"


def test_staging_job_id_rejects_non_exact_role() -> None:
    with pytest.raises(TypeError, match="role"):
        staging_job_id(REQUEST, "image")  # type: ignore[arg-type]


def test_staging_job_id_is_deterministic_canonical_uuid4_and_role_request_separated() -> None:
    image = staging_job_id(REQUEST, ClientDataRole.IMAGE)
    same_image = staging_job_id(REQUEST, ClientDataRole.IMAGE)
    gcode = staging_job_id(REQUEST, ClientDataRole.GCODE)
    other_request = staging_job_id("22345678-1234-4234-8234-123456789abc", ClientDataRole.IMAGE)

    parsed = UUID(image)
    assert image == same_image == str(parsed)
    assert parsed.version == 4
    assert parsed.variant == "specified in RFC 4122"
    assert len({image, gcode, other_request}) == 3


def _lease() -> StagingLeaseV1:
    return StagingLeaseV1(
        REQUEST,
        ClientDataRole.IMAGE,
        StagingOwnershipState.PUBLISHED_UNSENT,
        "a" * 64,
        3,
        PayloadIdentityV1(10, 20, 3, "a" * 64),
        "boot-a",
        100,
        200,
    )


def test_staging_lease_is_closed_and_immutable() -> None:
    identity = PayloadIdentityV1(10, 20, 3, "a" * 64)
    lease = StagingLeaseV1(
        request_id="12345678-1234-4234-8234-123456789abc",
        role=ClientDataRole.IMAGE,
        state=StagingOwnershipState.PUBLISHED_UNSENT,
        source_sha256="a" * 64,
        source_size_bytes=3,
        payload_identity=identity,
        publisher_boot_id="boot-a",
        published_monotonic_ns=100,
        deadline_monotonic_ns=200,
    )

    assert StagingLeaseV1.from_json(lease.to_json()) == lease
    with pytest.raises(FrozenInstanceError):
        lease.request_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "not-a-uuid"),
        ("source_sha256", "A" * 64),
        ("source_size_bytes", -1),
        ("deadline_monotonic_ns", 99),
    ],
)
def test_staging_lease_rejects_noncanonical_authority(field: str, value: object) -> None:
    values: dict[str, object] = {
        "request_id": "12345678-1234-4234-8234-123456789abc",
        "role": ClientDataRole.GCODE,
        "state": StagingOwnershipState.DISPATCH_UNCERTAIN,
        "source_sha256": "b" * 64,
        "source_size_bytes": 3,
        "payload_identity": PayloadIdentityV1(10, 20, 3, "b" * 64),
        "publisher_boot_id": "boot-a",
        "published_monotonic_ns": 100,
        "deadline_monotonic_ns": 200,
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        StagingLeaseV1(**values)  # type: ignore[arg-type]


def test_dispatch_outcome_is_binary() -> None:
    assert set(DispatchWriteOutcome) == {
        DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES,
        DispatchWriteOutcome.WRITE_POSSIBLE,
    }


@pytest.mark.parametrize(
    "value",
    [None, "not-a-uuid", "12345678-1234-1234-8234-123456789abc", REQUEST.upper()],
)
def test_request_and_bundle_names_are_closed(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_request_id(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_bundle_name(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_bundle_name(f"image--{REQUEST}--{'a' * 32}.stage", phase="prepare")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device", -1),
        ("inode", 0),
        ("size_bytes", -1),
        ("sha256", "A" * 64),
        ("schema_version", 2),
    ],
)
def test_payload_identity_rejects_open_or_noncanonical_values(field: str, value: object) -> None:
    values = {"device": 1, "inode": 2, "size_bytes": 3, "sha256": "a" * 64, "schema_version": 1}
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        PayloadIdentityV1(**values)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PayloadIdentityV1.from_json({"device": 1})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("role", "image"),
        ("state", StagingOwnershipState.CLIENT_PREPARED),
        ("source_size_bytes", 0),
        ("payload_identity", object()),
        ("publisher_boot_id", "bad boot"),
        ("published_monotonic_ns", -1),
        ("deadline_monotonic_ns", 100),
        ("payload_name", "other"),
    ],
)
def test_lease_rejects_every_open_authority_edge(field: str, value: object) -> None:
    values = {
        "request_id": REQUEST,
        "role": ClientDataRole.IMAGE,
        "state": StagingOwnershipState.PUBLISHED_UNSENT,
        "source_sha256": "a" * 64,
        "source_size_bytes": 3,
        "payload_identity": PayloadIdentityV1(1, 2, 3, "a" * 64),
        "publisher_boot_id": "boot-a",
        "published_monotonic_ns": 100,
        "deadline_monotonic_ns": 200,
        "payload_name": "payload",
        "schema_version": 1,
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        StagingLeaseV1(**values)  # type: ignore[arg-type]


def test_lease_json_and_source_identity_mismatch_fail_closed() -> None:
    lease = _lease()
    value = lease.to_json()
    value["schema_version"] = 2
    with pytest.raises(ValueError):
        StagingLeaseV1.from_json(value)
    with pytest.raises(ValueError):
        StagingLeaseV1.from_json({"schema_version": 1})
    with pytest.raises(ValueError):
        replace(lease, source_sha256="b" * 64)
    with pytest.raises(ValueError):
        replace(lease, source_size_bytes=4)


def test_staged_imported_and_published_dtos_bind_exact_paths() -> None:
    lease = _lease()
    prepare = f"image--{REQUEST}--{'a' * 32}.prepare"
    bundle = f"image--{REQUEST}--{'a' * 32}.stage"
    bundle_path = Path("/imports/image/drop") / bundle
    staged = StagedClientInputV1(
        ClientDataRole.IMAGE,
        REQUEST,
        prepare,
        bundle,
        bundle_path,
        bundle_path / "payload",
        lease,
    )
    with pytest.raises((TypeError, ValueError)):
        replace(staged, role=ClientDataRole.GCODE)
    with pytest.raises(ValueError):
        replace(staged, payload_path=Path("/other"))
    with pytest.raises(ValueError):
        replace(staged, bundle_path=Path("relative"), payload_path=Path("relative/payload"))
    imported = ImportedClientFileV1(
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        bundle_path / "payload",
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
    )
    assert imported.request_id == REQUEST
    with pytest.raises(ValueError):
        replace(imported, payload_path=Path("relative/payload"))
    with pytest.raises(ValueError):
        PublishedClientArtifactV1("", object(), Path("relative"))  # type: ignore[arg-type]


def test_admission_observation_and_recovery_dtos_are_closed() -> None:
    lease = _lease()
    bundle = f"image--{REQUEST}--{'a' * 32}.stage"
    admission = StagingAdmissionRecordV1(
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
        "boot-a",
        100,
        200,
        StagingAdmissionState.REQUEST_DECODED,
        None,
        0,
        "created",
        "updated",
        None,
    )
    with pytest.raises(ValueError):
        replace(admission, state=StagingAdmissionState.DURABLY_ADMITTED)
    recovery = replace(
        admission,
        state=StagingAdmissionState.RECOVERY_REQUIRED,
        job_id="job",
        recovery_code=StagingRecoveryReasonCode.LOCK_RACE,
    )
    assert recovery.job_id == "job"
    with pytest.raises(ValueError):
        replace(recovery, recovery_code=None)
    observation = PrepareObservationV1(
        ClientDataRole.IMAGE,
        bundle.removesuffix(".stage") + ".prepare",
        bundle,
        (1, 2),
        REQUEST,
        "boot-a",
        100,
        200,
        PrepareObservationStatus.ACTIVE,
        0,
        None,
    )
    with pytest.raises(ValueError):
        replace(observation, status=PrepareObservationStatus.RECOVERY_REQUIRED)
    recovered_observation = replace(
        observation,
        status=PrepareObservationStatus.RECOVERY_REQUIRED,
        recovery_code=PrepareRecoveryCode.LOCK_RACE,
    )
    assert recovered_observation.recovery_code is PrepareRecoveryCode.LOCK_RACE
    evidence = StagingRecoveryEvidenceV1(
        "12345678-1234-4234-8234-123456789abc",
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        lease.payload_identity,
        lease.source_sha256,
        StagingOwnershipState.DISPATCH_UNCERTAIN.value,
        StagingRecoveryReasonCode.STALE_DROP,
        "boot-a",
        200,
        StagingRecoveryAction.QUARANTINED,
        StagingRecoveryStatus.COMPLETE,
    )
    assert evidence.reason_code is StagingRecoveryReasonCode.STALE_DROP
    with pytest.raises(ValueError):
        replace(evidence, source_sha256=None)
    with pytest.raises(ValueError):
        replace(evidence, event_id="bad")


def test_remaining_authority_dto_edges_fail_closed() -> None:
    lease = _lease()
    bundle = f"image--{REQUEST}--{'a' * 32}.stage"
    prepare = bundle.removesuffix(".stage") + ".prepare"
    drop = Path("/imports/image/drop") / bundle
    staged = StagedClientInputV1(
        ClientDataRole.IMAGE,
        REQUEST,
        prepare,
        bundle,
        drop,
        drop / "payload",
        lease,
    )
    invalid_staged = (
        {"lease": object()},
        {"lease": replace(lease, role=ClientDataRole.GCODE)},
    )
    for changes in invalid_staged:
        with pytest.raises((TypeError, ValueError)):
            replace(staged, **changes)  # type: ignore[arg-type]

    admission = StagingAdmissionRecordV1(
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
        "boot-a",
        100,
        200,
        StagingAdmissionState.REQUEST_DECODED,
        None,
        0,
        "created",
        "updated",
        None,
    )
    invalid_admissions = (
        {"schema_version": 2},
        {"role": "image"},
        {"bundle_name": bundle.replace("image--", "gcode--")},
        {"payload_identity": object()},
        {"deadline_monotonic_ns": 100},
        {"job_id": "job"},
        {"state": StagingAdmissionState.RECOVERY_REQUIRED, "job_id": ""},
        {"state": StagingAdmissionState.RECOVERY_REQUIRED, "recovery_code": "OPEN"},
        {"recovery_code": StagingRecoveryReasonCode.LOCK_RACE},
        {"created_at_utc": ""},
    )
    for changes in invalid_admissions:
        with pytest.raises((TypeError, ValueError)):
            replace(admission, **changes)  # type: ignore[arg-type]
    assert (
        replace(
            admission,
            state=StagingAdmissionState.DURABLY_ADMITTED,
            job_id="job",
        ).job_id
        == "job"
    )

    imported = ImportedClientFileV1(
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        drop / "payload",
        lease.payload_identity,
        lease.source_sha256,
        "b" * 64,
    )
    for changes in (
        {"role": "image"},
        {"bundle_name": bundle.replace("image--", "gcode--")},
        {"source_sha256": "A" * 64},
    ):
        with pytest.raises((TypeError, ValueError)):
            replace(imported, **changes)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PublishedClientArtifactV1(1, object(), Path("/export"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PublishedClientArtifactV1("job", object(), Path("relative"))  # type: ignore[arg-type]
    assert PublishedClientArtifactV1("job", object(), Path("/export")).job_id == "job"  # type: ignore[arg-type]

    observation = PrepareObservationV1(
        ClientDataRole.IMAGE,
        prepare,
        bundle,
        (1, 2),
        REQUEST,
        "boot-a",
        100,
        200,
        PrepareObservationStatus.ACTIVE,
        0,
        None,
    )
    invalid_observations = (
        {"schema_version": 2},
        {"published_bundle_name": bundle.replace("image--", "gcode--")},
        {"bundle_identity": (1, 0)},
        {"deadline_monotonic_ns": 100},
        {"status": "ACTIVE"},
        {"recovery_code": PrepareRecoveryCode.LOCK_RACE},
    )
    for changes in invalid_observations:
        with pytest.raises((TypeError, ValueError)):
            replace(observation, **changes)  # type: ignore[arg-type]

    evidence = StagingRecoveryEvidenceV1(
        REQUEST,
        REQUEST,
        ClientDataRole.IMAGE,
        bundle,
        lease.payload_identity,
        lease.source_sha256,
        "DISPATCH_UNCERTAIN",
        StagingRecoveryReasonCode.STALE_DROP,
        "boot-a",
        200,
        StagingRecoveryAction.QUARANTINED,
        StagingRecoveryStatus.COMPLETE,
    )
    invalid_evidence = (
        {"schema_version": 2},
        {"event_id": "12345678-1234-1234-8234-123456789abc"},
        {"role": "image"},
        {"payload_identity": replace(lease.payload_identity, sha256="b" * 64)},
        {"prior_state": ""},
        {"action": "QUARANTINED"},
    )
    for changes in invalid_evidence:
        with pytest.raises((TypeError, ValueError)):
            replace(evidence, **changes)  # type: ignore[arg-type]
    assert replace(evidence, request_id=None, payload_identity=None, source_sha256=None).request_id is None
