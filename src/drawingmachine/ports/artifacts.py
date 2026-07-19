from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from drawingmachine.domain.jobs import ArtifactRef, JobRecord

_MIB = 1024 * 1024
MAX_IMAGE_IMPORT_BYTES = 32 * _MIB
MAX_GCODE_IMPORT_BYTES = 16 * _MIB


@dataclass(frozen=True, slots=True)
class ExpectedArtifact:
    role: str
    relative_path: str
    media_type: str
    json_object_required: bool
    required_json_values: tuple[tuple[str, str | int], ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerArtifact:
    role: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class PromotedBundle:
    job_id: str
    attempt_id: str
    root: Path
    artifacts: tuple[ArtifactRef, ...]


class ArtifactReader(Protocol):
    def read_bytes(
        self,
        job_id: str,
        artifact: ArtifactRef,
        *,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        """Read one promoted artifact through validated descriptor-relative authority."""
        ...


class ArtifactStore(ArtifactReader, Protocol):
    """Store artifacts under a quiescent, serialized service-writer contract.

    Before promotion, the producer process has exited and can no longer mutate staging.
    The drawingmachine service is the sole artifact and projection writer, and its writes
    are serialized. Arbitrary malicious same-UID processes are outside the threat model;
    they can mutate final artifacts and persistence after any finite validation.
    Implementations still reject unsafe staging contents, path replacement, collisions,
    and incomplete publication caused by crashes or stale service-owned paths.
    """

    def import_file(
        self,
        job_id: str,
        attempt_id: str,
        source: Path,
        *,
        role: str,
        relative_path: str,
        media_type: str,
        max_bytes: int,
    ) -> PromotedBundle: ...

    def create_staging(self, job_id: str, attempt_id: str) -> Path: ...

    def discard_staging(self, job_id: str, attempt_id: str) -> None: ...

    def discard_promoted_bundle(
        self,
        job_id: str,
        attempt_id: str,
        exact_artifacts: tuple[ArtifactRef, ...],
    ) -> None:
        """Identity-safely revoke only one exact, uncommitted promoted attempt."""
        ...

    def read_staged_bytes(
        self,
        job_id: str,
        attempt_id: str,
        artifact: WorkerArtifact,
        *,
        expected_relative_path: str,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        """Read one declared worker output through service-owned staging authority."""
        ...

    def validate_and_promote(
        self,
        job_id: str,
        attempt_id: str,
        artifacts: tuple[WorkerArtifact, ...],
        *,
        expected: tuple[ExpectedArtifact, ...],
    ) -> PromotedBundle:
        """Validate and promote only after the producer process has exited.

        The B5 supervisor must enforce this precondition before returning WorkerOutcome.
        """
        ...

    def resolve(self, job_id: str, artifact: ArtifactRef) -> Path:
        """Securely validate and resolve a persisted job-relative artifact reference."""
        ...

    def write_projection(self, job: JobRecord, artifacts: tuple[ArtifactRef, ...]) -> None:
        """Write projection evidence under the serialized single-writer contract."""
        ...
