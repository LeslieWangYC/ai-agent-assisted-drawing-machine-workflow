from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from drawingmachine.json_types import JsonObject

MAX_LEASE_BYTES = 16 * 1024
MAX_BUNDLES_PER_PHASE = 32
STAGING_TTL_NS = 300_000_000_000
EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1 = frozenset(
    {
        "route_decision",
        "direct_report",
        "processed_image",
        "normalization_report",
        "complexity_report",
        "path_plan",
        "preview_stroke_svg",
        "preview_final_svg",
        "planning_report",
        "gcode",
        "gcode_build_report",
        "gcode_preview_svg",
        "gcode_preview_report",
        "gcode_static",
        "send_plan",
        "readiness",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BUNDLE = re.compile(
    r"(?P<role>image|gcode)--(?P<request>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"--[0-9a-f]{32}\.(?P<phase>prepare|stage)\Z"
)


class ClientDataRole(StrEnum):
    IMAGE = "image"
    GCODE = "gcode"


class StagingOwnershipState(StrEnum):
    CLIENT_PREPARED = "CLIENT_PREPARED"
    PUBLISHED_UNSENT = "PUBLISHED_UNSENT"
    DISPATCH_UNCERTAIN = "DISPATCH_UNCERTAIN"
    REQUEST_DECODED = "REQUEST_DECODED"
    CLAIMED = "CLAIMED"
    DURABLY_ADMITTED = "DURABLY_ADMITTED"
    CONSUMED = "CONSUMED"
    EXPIRED_QUARANTINED = "EXPIRED_QUARANTINED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class DispatchWriteOutcome(StrEnum):
    DEFINITELY_UNSENT_ZERO_BYTES = "DEFINITELY_UNSENT_ZERO_BYTES"
    WRITE_POSSIBLE = "WRITE_POSSIBLE"


class StagingAdmissionState(StrEnum):
    REQUEST_DECODED = "REQUEST_DECODED"
    CLAIMED = "CLAIMED"
    DURABLY_ADMITTED = "DURABLY_ADMITTED"
    CONSUMED = "CONSUMED"
    QUARANTINED = "QUARANTINED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class PrepareObservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    HELD_EXPIRED = "HELD_EXPIRED"
    PUBLISHED_HANDOFF = "PUBLISHED_HANDOFF"
    CLIENT_ABORTED = "CLIENT_ABORTED"
    QUARANTINED = "QUARANTINED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class PrepareRecoveryCode(StrEnum):
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    LOCK_RACE = "LOCK_RACE"
    PUBLISH_MISMATCH = "PUBLISH_MISMATCH"
    QUARANTINE_FAILED = "QUARANTINE_FAILED"
    OBSERVATION_CORRUPT = "OBSERVATION_CORRUPT"
    OBSERVATION_UPDATE_FAILED = "OBSERVATION_UPDATE_FAILED"
    OBSERVATION_DELETE_FAILED = "OBSERVATION_DELETE_FAILED"
    PARENT_FSYNC_FAILED = "PARENT_FSYNC_FAILED"
    EVIDENCE_WRITE_FAILED = "EVIDENCE_WRITE_FAILED"


class StagingRecoveryAction(StrEnum):
    QUARANTINED = "QUARANTINED"
    CLEANED = "CLEANED"
    PRESERVED = "PRESERVED"


class StagingRecoveryStatus(StrEnum):
    COMPLETE = "COMPLETE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class StagingRecoveryReasonCode(StrEnum):
    EVIDENCE_WRITE_FAILED = "EVIDENCE_WRITE_FAILED"
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    INVALID_PREPARE_NAME = "INVALID_PREPARE_NAME"
    INVALID_STAGING_BUNDLE = "INVALID_STAGING_BUNDLE"
    LOCK_RACE = "LOCK_RACE"
    OBSERVATION_CORRUPT = "OBSERVATION_CORRUPT"
    OBSERVATION_DELETE_FAILED = "OBSERVATION_DELETE_FAILED"
    OBSERVATION_UPDATE_FAILED = "OBSERVATION_UPDATE_FAILED"
    PARENT_FSYNC_FAILED = "PARENT_FSYNC_FAILED"
    PUBLISH_MISMATCH = "PUBLISH_MISMATCH"
    QUARANTINE_FAILED = "QUARANTINE_FAILED"
    SPECIAL_STAGING_ENTRY = "SPECIAL_STAGING_ENTRY"
    STALE_ADMITTED = "STALE_ADMITTED"
    STALE_DROP = "STALE_DROP"
    STALE_PREPARE = "STALE_PREPARE"


class StagingClock(Protocol):
    def boot_id(self) -> str: ...

    def monotonic_ns(self) -> int: ...


class ClientArtifactRefV1(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def relative_path(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def media_type(self) -> str: ...


def canonical_request_id(value: str) -> str:
    if type(value) is not str:
        raise TypeError("request_id must be an exact string")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError("request_id must be a canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("request_id must be a canonical lowercase UUIDv4")
    return value


def staging_job_id(request_id: str, role: ClientDataRole) -> str:
    canonical_request_id(request_id)
    if type(role) is not ClientDataRole:
        raise TypeError("staging job role must be exact")
    derived = uuid5(NAMESPACE_URL, f"drawingmachine:staging:{role.value}:{request_id}")
    return str(UUID(bytes=derived.bytes, version=4))


def parse_bundle_name(value: str, *, phase: str | None = None) -> tuple[ClientDataRole, str]:
    if type(value) is not str or (match := _BUNDLE.fullmatch(value)) is None:
        raise ValueError("staging bundle name is not canonical")
    if phase is not None and match.group("phase") != phase:
        raise ValueError("staging bundle phase is not canonical")
    request_id = canonical_request_id(match.group("request"))
    return ClientDataRole(match.group("role")), request_id


def _digest(value: str, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be canonical SHA-256")
    return value


def _nonnegative(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative exact integer")
    return value


def _positive(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{field} must be a positive exact integer")
    return value


def _boot(value: str) -> str:
    if type(value) is not str or _BOOT_ID.fullmatch(value) is None:
        raise ValueError("publisher_boot_id must be bounded and canonical")
    return value


@dataclass(frozen=True, slots=True)
class PayloadIdentityV1:
    device: int
    inode: int
    size_bytes: int
    sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("payload identity schema_version must be 1")
        _nonnegative(self.device, "device")
        _positive(self.inode, "inode")
        _nonnegative(self.size_bytes, "size_bytes")
        _digest(self.sha256, "sha256")

    def to_json(self) -> JsonObject:
        return {
            "device": self.device,
            "inode": self.inode,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_json(cls, value: object) -> PayloadIdentityV1:
        if type(value) is not dict or set(cast(dict[object, object], value)) != {
            "device",
            "inode",
            "size_bytes",
            "sha256",
        }:
            raise ValueError("payload identity fields are not exact")
        data = cast(dict[str, object], value)
        return cls(
            cast(int, data["device"]),
            cast(int, data["inode"]),
            cast(int, data["size_bytes"]),
            cast(str, data["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class StagingLeaseV1:
    request_id: str
    role: ClientDataRole
    state: StagingOwnershipState
    source_sha256: str
    source_size_bytes: int
    payload_identity: PayloadIdentityV1
    publisher_boot_id: str
    published_monotonic_ns: int
    deadline_monotonic_ns: int
    payload_name: str = "payload"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("staging lease schema_version must be 1")
        canonical_request_id(self.request_id)
        if type(self.role) is not ClientDataRole or type(self.state) is not StagingOwnershipState:
            raise TypeError("staging lease role/state must be exact enums")
        if self.state not in {
            StagingOwnershipState.PUBLISHED_UNSENT,
            StagingOwnershipState.DISPATCH_UNCERTAIN,
        }:
            raise ValueError("published lease state is invalid")
        _digest(self.source_sha256, "source_sha256")
        _positive(self.source_size_bytes, "source_size_bytes")
        if type(self.payload_identity) is not PayloadIdentityV1:
            raise TypeError("payload_identity must be exact")
        if (
            self.payload_identity.sha256 != self.source_sha256
            or self.payload_identity.size_bytes != self.source_size_bytes
        ):
            raise ValueError("payload identity must match source bytes")
        _boot(self.publisher_boot_id)
        _nonnegative(self.published_monotonic_ns, "published_monotonic_ns")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns <= self.published_monotonic_ns:
            raise ValueError("deadline_monotonic_ns must follow publication")
        if self.payload_name != "payload":
            raise ValueError("payload_name must be payload")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "state": self.state.value,
            "request_id": self.request_id,
            "role": self.role.value,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "payload_name": self.payload_name,
            "payload_identity": self.payload_identity.to_json(),
            "publisher_boot_id": self.publisher_boot_id,
            "published_monotonic_ns": self.published_monotonic_ns,
            "deadline_monotonic_ns": self.deadline_monotonic_ns,
        }

    @classmethod
    def from_json(cls, value: object) -> StagingLeaseV1:
        expected = {
            "schema_version",
            "state",
            "request_id",
            "role",
            "source_sha256",
            "source_size_bytes",
            "payload_name",
            "payload_identity",
            "publisher_boot_id",
            "published_monotonic_ns",
            "deadline_monotonic_ns",
        }
        if type(value) is not dict or set(cast(dict[object, object], value)) != expected:
            raise ValueError("staging lease fields are not exact")
        data = cast(dict[str, object], value)
        if data["schema_version"] != 1:
            raise ValueError("staging lease schema_version must be 1")
        return cls(
            cast(str, data["request_id"]),
            ClientDataRole(cast(str, data["role"])),
            StagingOwnershipState(cast(str, data["state"])),
            cast(str, data["source_sha256"]),
            cast(int, data["source_size_bytes"]),
            PayloadIdentityV1.from_json(data["payload_identity"]),
            cast(str, data["publisher_boot_id"]),
            cast(int, data["published_monotonic_ns"]),
            cast(int, data["deadline_monotonic_ns"]),
            cast(str, data["payload_name"]),
        )


@dataclass(frozen=True, slots=True)
class StagedClientInputV1:
    role: ClientDataRole
    request_id: str
    prepare_bundle_name: str
    bundle_name: str
    bundle_path: Path
    payload_path: Path
    lease: StagingLeaseV1

    def __post_init__(self) -> None:
        if type(self.role) is not ClientDataRole or type(self.lease) is not StagingLeaseV1:
            raise TypeError("staged input role/lease must be exact")
        canonical_request_id(self.request_id)
        prepare_role, prepare_request = parse_bundle_name(self.prepare_bundle_name, phase="prepare")
        role, request = parse_bundle_name(self.bundle_name, phase="stage")
        if (prepare_role, role) != (self.role, self.role) or (prepare_request, request) != (
            self.request_id,
            self.request_id,
        ):
            raise ValueError("staged input bundle identity does not match")
        if not self.bundle_path.is_absolute() or self.payload_path != self.bundle_path / "payload":
            raise ValueError("staged input paths must be exact absolute bundle paths")
        if self.lease.request_id != self.request_id or self.lease.role is not self.role:
            raise ValueError("staged input lease binding does not match")


@dataclass(frozen=True, slots=True)
class StagingAdmissionRecordV1:
    request_id: str
    role: ClientDataRole
    bundle_name: str
    payload_identity: PayloadIdentityV1
    source_sha256: str
    lease_sha256: str
    publisher_boot_id: str
    published_monotonic_ns: int
    deadline_monotonic_ns: int
    state: StagingAdmissionState
    job_id: str | None
    revision: int
    created_at_utc: str
    updated_at_utc: str
    recovery_code: StagingRecoveryReasonCode | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("staging admission schema_version must be 1")
        canonical_request_id(self.request_id)
        if type(self.role) is not ClientDataRole or type(self.state) is not StagingAdmissionState:
            raise TypeError("staging admission role/state must be exact")
        role, request_id = parse_bundle_name(self.bundle_name, phase="stage")
        if (role, request_id) != (self.role, self.request_id):
            raise ValueError("staging admission bundle binding does not match")
        if type(self.payload_identity) is not PayloadIdentityV1:
            raise TypeError("staging admission payload identity must be exact")
        _digest(self.source_sha256, "source_sha256")
        _digest(self.lease_sha256, "lease_sha256")
        _boot(self.publisher_boot_id)
        _nonnegative(self.published_monotonic_ns, "published_monotonic_ns")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns <= self.published_monotonic_ns:
            raise ValueError("staging admission deadline must follow publication")
        _nonnegative(self.revision, "revision")
        if self.state in {StagingAdmissionState.DURABLY_ADMITTED, StagingAdmissionState.CONSUMED}:
            if type(self.job_id) is not str or not self.job_id:
                raise ValueError("durable staging admission requires job_id")
        elif self.state is not StagingAdmissionState.RECOVERY_REQUIRED and self.job_id is not None:
            raise ValueError("pre-admission staging record cannot bind a job")
        elif self.job_id is not None and (type(self.job_id) is not str or not self.job_id):
            raise ValueError("recovery staging job binding must be a non-empty string")
        if self.state is StagingAdmissionState.RECOVERY_REQUIRED:
            if type(self.recovery_code) is not StagingRecoveryReasonCode:
                raise ValueError("recovery-required staging admission requires a closed code")
        elif self.recovery_code is not None:
            raise ValueError("recovery code is allowed only for recovery-required admission")
        if type(self.created_at_utc) is not str or not self.created_at_utc or type(self.updated_at_utc) is not str:
            raise TypeError("staging admission timestamps must be exact strings")


@dataclass(frozen=True, slots=True)
class ImportedClientFileV1:
    request_id: str
    role: ClientDataRole
    bundle_name: str
    payload_path: Path
    payload_identity: PayloadIdentityV1
    source_sha256: str
    lease_sha256: str

    def __post_init__(self) -> None:
        canonical_request_id(self.request_id)
        if type(self.role) is not ClientDataRole or type(self.payload_identity) is not PayloadIdentityV1:
            raise TypeError("imported client file authority must be exact")
        role, request_id = parse_bundle_name(self.bundle_name, phase="stage")
        if (role, request_id) != (self.role, self.request_id):
            raise ValueError("imported client file bundle binding does not match")
        if not self.payload_path.is_absolute() or self.payload_path.name != "payload":
            raise ValueError("imported client payload path must be absolute and exact")
        _digest(self.source_sha256, "source_sha256")
        _digest(self.lease_sha256, "lease_sha256")


@dataclass(frozen=True, slots=True)
class PublishedClientArtifactV1:
    job_id: str
    artifact: ClientArtifactRefV1
    export_path: Path

    def __post_init__(self) -> None:
        if type(self.job_id) is not str or not self.job_id or not self.export_path.is_absolute():
            raise ValueError("published client artifact authority is invalid")


@dataclass(frozen=True, slots=True)
class PrepareObservationV1:
    role: ClientDataRole
    prepare_bundle_name: str
    published_bundle_name: str
    bundle_identity: tuple[int, int]
    request_id: str
    first_seen_boot_id: str
    first_seen_monotonic_ns: int
    deadline_monotonic_ns: int
    status: PrepareObservationStatus
    revision: int
    recovery_code: PrepareRecoveryCode | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.role) is not ClientDataRole:
            raise ValueError("prepare observation schema or role is invalid")
        prepare_role, prepare_request = parse_bundle_name(self.prepare_bundle_name, phase="prepare")
        published_role, published_request = parse_bundle_name(self.published_bundle_name, phase="stage")
        canonical_request_id(self.request_id)
        if (prepare_role, published_role) != (self.role, self.role) or (
            prepare_request,
            published_request,
        ) != (self.request_id, self.request_id):
            raise ValueError("prepare observation bundle binding does not match")
        if (
            type(self.bundle_identity) is not tuple
            or len(self.bundle_identity) != 2
            or type(self.bundle_identity[0]) is not int
            or type(self.bundle_identity[1]) is not int
            or self.bundle_identity[0] < 0
            or self.bundle_identity[1] <= 0
        ):
            raise TypeError("prepare observation identity is invalid")
        _boot(self.first_seen_boot_id)
        _nonnegative(self.first_seen_monotonic_ns, "first_seen_monotonic_ns")
        if type(self.deadline_monotonic_ns) is not int or self.deadline_monotonic_ns <= self.first_seen_monotonic_ns:
            raise ValueError("prepare observation deadline must follow first sighting")
        if type(self.status) is not PrepareObservationStatus:
            raise TypeError("prepare observation status must be exact")
        _nonnegative(self.revision, "revision")
        if self.status is PrepareObservationStatus.RECOVERY_REQUIRED:
            if type(self.recovery_code) is not PrepareRecoveryCode:
                raise ValueError("recovery-required observation requires a closed code")
        elif self.recovery_code is not None:
            raise ValueError("prepare observation recovery code is not allowed")


@dataclass(frozen=True, slots=True)
class StagingRecoveryEvidenceV1:
    event_id: str
    request_id: str | None
    role: ClientDataRole
    bundle_name: str
    payload_identity: PayloadIdentityV1 | None
    source_sha256: str | None
    prior_state: str
    reason_code: StagingRecoveryReasonCode
    observed_boot_id: str
    observed_monotonic_ns: int
    action: StagingRecoveryAction
    status: StagingRecoveryStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("staging recovery evidence schema_version must be 1")
        try:
            event = UUID(self.event_id)
        except ValueError as error:
            raise ValueError("staging recovery event_id must be a UUIDv4") from error
        if event.version != 4 or str(event) != self.event_id:
            raise ValueError("staging recovery event_id must be a canonical UUIDv4")
        if self.request_id is not None:
            canonical_request_id(self.request_id)
        if type(self.role) is not ClientDataRole or type(self.bundle_name) is not str or not self.bundle_name:
            raise TypeError("staging recovery role/bundle authority is invalid")
        if (self.payload_identity is None) != (self.source_sha256 is None):
            raise ValueError("staging recovery payload identity and digest must be paired")
        if self.payload_identity is not None:
            _digest(cast(str, self.source_sha256), "source_sha256")
            if self.payload_identity.sha256 != self.source_sha256:
                raise ValueError("staging recovery payload digest differs from identity")
        if (
            type(self.prior_state) is not str
            or not self.prior_state
            or type(self.reason_code) is not StagingRecoveryReasonCode
        ):
            raise TypeError("staging recovery prior state/reason must be exact strings")
        _boot(self.observed_boot_id)
        _nonnegative(self.observed_monotonic_ns, "observed_monotonic_ns")
        if type(self.action) is not StagingRecoveryAction or type(self.status) is not StagingRecoveryStatus:
            raise TypeError("staging recovery action/status must be exact enums")


class ClientInputStagingPort(Protocol):
    def stage_image(
        self, caller_supplied_local_path: Path, request_id: str, clock: StagingClock
    ) -> StagedClientInputV1: ...

    def stage_gcode(
        self, caller_supplied_local_path: Path, request_id: str, clock: StagingClock
    ) -> StagedClientInputV1: ...

    def transfer_before_write(self, staged_input: StagedClientInputV1) -> StagedClientInputV1: ...

    def discard_definitely_unsent(
        self,
        staged_input: StagedClientInputV1,
        send_outcome: DispatchWriteOutcome,
    ) -> None: ...

    def read_exported_artifact(self, job_id: str, existing_artifact_ref: ClientArtifactRefV1) -> bytes: ...


class ServiceClientDataPort(Protocol):
    def bind_request_envelope(self, request_id: str, role: ClientDataRole, envelope: JsonObject) -> None: ...

    def register_decoded(
        self, request_id: str, role: ClientDataRole, staged_path: Path
    ) -> StagingAdmissionRecordV1: ...

    def claim_image(self, request_id: str, staged_path: Path) -> ImportedClientFileV1: ...

    def claim_gcode(self, request_id: str, staged_path: Path) -> ImportedClientFileV1: ...

    def consume(self, request_id: str, *, expected_revision: int) -> StagingAdmissionRecordV1: ...

    def reconcile(
        self,
        trigger: str,
        clock: StagingClock,
        active_requests: frozenset[str],
        durable_admissions: tuple[StagingAdmissionRecordV1, ...],
    ) -> tuple[StagingRecoveryEvidenceV1, ...]: ...

    def publish_artifacts(
        self, job_id: str, exact_allowed_role_refs: tuple[ClientArtifactRefV1, ...]
    ) -> tuple[PublishedClientArtifactV1, ...]: ...


__all__ = [
    "EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1",
    "MAX_BUNDLES_PER_PHASE",
    "MAX_LEASE_BYTES",
    "STAGING_TTL_NS",
    "ClientArtifactRefV1",
    "ClientDataRole",
    "ClientInputStagingPort",
    "DispatchWriteOutcome",
    "ImportedClientFileV1",
    "PayloadIdentityV1",
    "PrepareObservationStatus",
    "PrepareObservationV1",
    "PrepareRecoveryCode",
    "PublishedClientArtifactV1",
    "ServiceClientDataPort",
    "StagedClientInputV1",
    "StagingAdmissionRecordV1",
    "StagingAdmissionState",
    "StagingClock",
    "StagingLeaseV1",
    "StagingOwnershipState",
    "StagingRecoveryAction",
    "StagingRecoveryEvidenceV1",
    "StagingRecoveryReasonCode",
    "StagingRecoveryStatus",
    "canonical_request_id",
    "parse_bundle_name",
    "staging_job_id",
]
