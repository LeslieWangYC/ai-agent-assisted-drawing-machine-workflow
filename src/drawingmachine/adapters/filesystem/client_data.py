from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import struct
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from drawingmachine.adapters.filesystem._secure_fs import (
    SecureFilesystemError,
    entry_matches_descriptor,
    fsync_directory,
    hidden_name,
    open_directory_at,
    open_regular_at,
    remove_regular_if_identity,
    remove_tree_at,
    rename_noreplace,
    stat_at,
)
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import MAX_GCODE_IMPORT_BYTES, MAX_IMAGE_IMPORT_BYTES, ArtifactReader
from drawingmachine.ports.client_data import (
    EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1,
    MAX_BUNDLES_PER_PHASE,
    MAX_LEASE_BYTES,
    STAGING_TTL_NS,
    ClientArtifactRefV1,
    ClientDataRole,
    DispatchWriteOutcome,
    ImportedClientFileV1,
    PayloadIdentityV1,
    PublishedClientArtifactV1,
    StagedClientInputV1,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingClock,
    StagingLeaseV1,
    StagingOwnershipState,
    StagingRecoveryEvidenceV1,
    canonical_request_id,
    parse_bundle_name,
)
from drawingmachine.ports.repository import Repository


class ClientDataPermissionOracle(Protocol):
    def validate_phase_directory(self, path: Path, role: ClientDataRole, phase: str) -> None: ...

    def validate_private_directory(self, path: Path) -> None: ...

    def validate_client_bundle(self, path: Path, role: ClientDataRole) -> None: ...

    def validate_client_file(self, path: Path, role: ClientDataRole) -> None: ...

    def validate_export_file(self, path: Path) -> None: ...

    def validate_export_directory(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class _DirectoryAuthority:
    path: Path
    identities: tuple[tuple[int, int], ...]

    @classmethod
    def capture(cls, path: Path) -> _DirectoryAuthority:
        descriptor, identities = _walk_absolute_directory(path)
        os.close(descriptor)
        return cls(path, identities)

    def open(self) -> int:
        descriptor, identities = _walk_absolute_directory(self.path)
        if identities != self.identities:
            os.close(descriptor)
            raise SecureFilesystemError(f"managed directory ancestor identity changed: {self.path}")
        return descriptor


class SystemStagingClock:
    def boot_id(self) -> str:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise _error("system boot identity cannot be read", "CLIENT_DATA_CLOCK_INVALID") from error
        return value

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class NativeClientDataPermissionOracle:
    def __init__(self, *, service_uid: int, automation_uid: int, service_gid: int) -> None:
        self._service_uid = service_uid
        self._automation_uid = automation_uid
        self._service_gid = service_gid

    def validate_phase_directory(self, path: Path, role: ClientDataRole, phase: str) -> None:
        del role
        if phase not in {"prepare", "drop"}:
            _invalid("client data phase is not authorized", code="CLIENT_DATA_POLICY_INVALID")
        value = os.lstat(path)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != self._service_uid
            or value.st_gid != self._service_gid
            or stat.S_IMODE(value.st_mode) != 0o2770
        ):
            _invalid("client data phase owner, group, mode, or type is not exact", code="CLIENT_DATA_POLICY_INVALID")
        self._validate_automation_acl(path, mode=0o2770, permissions=0o7, default=True, include_service=True)

    def validate_client_bundle(self, path: Path, role: ClientDataRole) -> None:
        del role
        value = os.lstat(path)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != self._automation_uid
            or value.st_gid != self._service_gid
            or stat.S_IMODE(value.st_mode) != 0o2770
        ):
            _invalid(
                "client staging bundle owner, group, mode, or type is not exact", code="CLIENT_DATA_POLICY_INVALID"
            )

    def validate_private_directory(self, path: Path) -> None:
        value = os.lstat(path)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != self._service_uid
            or value.st_gid != os.getegid()
            or stat.S_IMODE(value.st_mode) != 0o700
        ):
            _invalid("private client data directory authority is not exact", code="CLIENT_DATA_POLICY_INVALID")
        _validate_mode_only_acl(path, 0o700)

    def validate_client_file(self, path: Path, role: ClientDataRole) -> None:
        del role
        value = os.lstat(path)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != self._automation_uid
            or value.st_gid != self._service_gid
            or stat.S_IMODE(value.st_mode) != 0o640
            or value.st_nlink != 1
        ):
            _invalid(
                "client staging file owner, group, mode, link, or type is not exact",
                code="CLIENT_DATA_POLICY_INVALID",
            )

    def validate_export_file(self, path: Path) -> None:
        value = os.lstat(path)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != self._service_uid
            or value.st_gid != self._service_gid
            or stat.S_IMODE(value.st_mode) != 0o640
        ):
            _invalid("export file ownership, link, or type is unsafe", code="CLIENT_DATA_POLICY_INVALID")
        self._validate_automation_acl(path, mode=0o640, permissions=0o5, default=False)

    def validate_export_directory(self, path: Path) -> None:
        value = os.lstat(path)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != self._service_uid
            or value.st_gid != self._service_gid
            or stat.S_IMODE(value.st_mode) != 0o2750
        ):
            _invalid("export directory ownership, mode, or type is unsafe", code="CLIENT_DATA_POLICY_INVALID")
        self._validate_automation_acl(path, mode=0o2750, permissions=0o5, default=True)

    def _validate_automation_acl(
        self, path: Path, *, mode: int, permissions: int, default: bool, include_service: bool = False
    ) -> None:
        if self._automation_uid == self._service_uid:
            return
        entries: tuple[tuple[int, int | None, int], ...] = (
            (1, None, (mode >> 6) & 0o7),
            (2, self._automation_uid, permissions),
            (4, None, 0),
            (16, None, (mode >> 3) & 0o7),
            (32, None, mode & 0o7),
        )
        if include_service:
            entries = (*entries, (2, self._service_uid, 0o7))
        expected = tuple(sorted(entries))
        if _read_posix_acl(path, "system.posix_acl_access", mode) != expected or (
            default and _read_posix_acl(path, "system.posix_acl_default", mode) != expected
        ):
            _invalid("automation access/default ACL is not exact", code="CLIENT_DATA_POLICY_INVALID")
        if not default and _read_posix_acl(path, "system.posix_acl_default", mode):
            _invalid("export file default ACL is not empty", code="CLIENT_DATA_POLICY_INVALID")


class _TestPermissionOracle:
    def validate_phase_directory(self, path: Path, role: ClientDataRole, phase: str) -> None:
        del role, phase
        value = os.lstat(path)
        if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o2770:
            _invalid("test phase directory is unsafe", code="CLIENT_DATA_POLICY_INVALID")

    def validate_client_bundle(self, path: Path, role: ClientDataRole) -> None:
        del role
        value = os.lstat(path)
        if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o2770:
            _invalid("test staging bundle is unsafe", code="CLIENT_DATA_POLICY_INVALID")

    def validate_private_directory(self, path: Path) -> None:
        value = os.lstat(path)
        if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o700:
            _invalid("test private client data directory is unsafe", code="CLIENT_DATA_POLICY_INVALID")

    def validate_client_file(self, path: Path, role: ClientDataRole) -> None:
        del role
        value = os.lstat(path)
        if not stat.S_ISREG(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o640 or value.st_nlink != 1:
            _invalid("test staging file is unsafe", code="CLIENT_DATA_POLICY_INVALID")

    def validate_export_file(self, path: Path) -> None:
        value = os.lstat(path)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o640:
            _invalid("test export file is unsafe", code="CLIENT_DATA_POLICY_INVALID")

    def validate_export_directory(self, path: Path) -> None:
        value = os.lstat(path)
        if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o2750:
            _invalid("test export directory is unsafe", code="CLIENT_DATA_POLICY_INVALID")


class FilesystemClientInputStaging:
    def __init__(
        self,
        import_root: Path,
        export_root: Path,
        *,
        permission_oracle: ClientDataPermissionOracle,
        published_import_root: Path | None = None,
        published_export_root: Path | None = None,
        endpoint_validator: Callable[[], None] = lambda: None,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if not import_root.is_absolute() or not export_root.is_absolute() or import_root == export_root:
            raise ValueError("client import/export roots must be distinct absolute paths")
        self._published_import_root = import_root if published_import_root is None else published_import_root
        self._published_export_root = export_root if published_export_root is None else published_export_root
        if not self._published_import_root.is_absolute() or not self._published_export_root.is_absolute():
            raise ValueError("published client data roots must be absolute")
        self._import_root = self._published_import_root
        self._export_root = self._published_export_root
        self._import_authority = _DirectoryAuthority.capture(self._import_root)
        self._export_authority = _DirectoryAuthority.capture(self._export_root)
        self._permissions = permission_oracle
        self._endpoint_validator = self._bind_authority_validation(endpoint_validator)
        self._nonce_factory = nonce_factory

    def _bind_authority_validation(self, endpoint_validator: Callable[[], None]) -> Callable[[], None]:
        def validate() -> None:
            endpoint_validator()
            try:
                for authority in (self._import_authority, self._export_authority):
                    descriptor = authority.open()
                    os.close(descriptor)
            except (OSError, SecureFilesystemError) as error:
                raise _error("client data root ancestor identity changed", "CLIENT_DATA_IDENTITY_DRIFT") from error

        return validate

    @classmethod
    def for_test(cls, import_root: Path, export_root: Path | None = None) -> FilesystemClientInputStaging:
        root = import_root.resolve()
        exports = (root.parent / "exports") if export_root is None else export_root.resolve()
        for role in ClientDataRole:
            for phase in ("prepare", "drop"):
                directory = root / role.value / phase
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o2770)
        exports.mkdir(parents=True, exist_ok=True)
        return cls(root, exports, permission_oracle=_TestPermissionOracle())

    def stage_image(
        self,
        caller_supplied_local_path: Path,
        request_id: str,
        clock: StagingClock,
    ) -> StagedClientInputV1:
        return self._stage(ClientDataRole.IMAGE, caller_supplied_local_path, request_id, clock)

    def stage_gcode(
        self,
        caller_supplied_local_path: Path,
        request_id: str,
        clock: StagingClock,
    ) -> StagedClientInputV1:
        return self._stage(ClientDataRole.GCODE, caller_supplied_local_path, request_id, clock)

    def _stage(
        self,
        role: ClientDataRole,
        source_path: Path,
        request_id: str,
        clock: StagingClock,
    ) -> StagedClientInputV1:
        self._endpoint_validator()
        canonical_request_id(request_id)
        source_fd, source_status, resolved = _open_canonical_regular(source_path)
        prepare = self._import_root / role.value / "prepare"
        drop = self._import_root / role.value / "drop"
        self._validate_phase(prepare, role, "prepare")
        self._validate_phase(drop, role, "drop")
        limit = MAX_IMAGE_IMPORT_BYTES if role is ClientDataRole.IMAGE else MAX_GCODE_IMPORT_BYTES
        if source_status.st_size <= 0 or source_status.st_size > limit:
            os.close(source_fd)
            _invalid("client input size is outside the exact role limit", code="CLIENT_INPUT_INVALID")
        nonce = self._nonce_factory()
        if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
            os.close(source_fd)
            _invalid("staging nonce source returned a noncanonical value", code="CLIENT_DATA_INTERNAL")
        prepare_name = f"{role.value}--{request_id}--{nonce}.prepare"
        bundle_name = f"{role.value}--{request_id}--{nonce}.stage"
        parse_bundle_name(prepare_name, phase="prepare")
        parse_bundle_name(bundle_name, phase="stage")
        prepare_path = prepare / prepare_name
        published_path = drop / bundle_name
        bundle_fd = -1
        try:
            _require_phase_quota(prepare, role, limit)
            _require_phase_quota(drop, role, limit)
            os.mkdir(prepare_path, 0o2770)
            prepare_path.chmod(0o2770)
            bundle_fd = os.open(prepare_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            fcntl.flock(bundle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._permissions.validate_client_bundle(prepare_path, role)
            source_sha256, copied = _copy_payload(source_fd, source_status, bundle_fd, limit)
            payload_status = os.stat("payload", dir_fd=bundle_fd, follow_symlinks=False)
            identity = PayloadIdentityV1(
                payload_status.st_dev,
                payload_status.st_ino,
                payload_status.st_size,
                source_sha256,
            )
            published = clock.monotonic_ns()
            lease = StagingLeaseV1(
                request_id,
                role,
                StagingOwnershipState.PUBLISHED_UNSENT,
                source_sha256,
                copied,
                identity,
                clock.boot_id(),
                published,
                published + STAGING_TTL_NS,
            )
            _write_json_at(bundle_fd, "lease.json", lease.to_json())
            self._permissions.validate_client_file(prepare_path / "payload", role)
            self._permissions.validate_client_file(prepare_path / "lease.json", role)
            os.fsync(bundle_fd)
            _verify_source(source_fd, source_status, resolved)
            prepare_fd = _directory_fd(prepare)
            drop_fd = _directory_fd(drop)
            try:
                os.rename(prepare_name, bundle_name, src_dir_fd=prepare_fd, dst_dir_fd=drop_fd)
            finally:
                os.close(prepare_fd)
                os.close(drop_fd)
            _fsync_directory(prepare)
            _fsync_directory(drop)
            published_status = os.lstat(published_path)
            bundle_status = os.fstat(bundle_fd)
            if (published_status.st_dev, published_status.st_ino) != (bundle_status.st_dev, bundle_status.st_ino):
                _invalid("published staging bundle identity changed", code="CLIENT_DATA_IDENTITY_DRIFT")
            authority_path = self._published_import_root / role.value / "drop" / bundle_name
            return StagedClientInputV1(
                role,
                request_id,
                prepare_name,
                bundle_name,
                authority_path,
                authority_path / "payload",
                lease,
            )
        except DrawingMachineError:
            _remove_owned_prepare(prepare_path, bundle_fd)
            raise
        except (OSError, ValueError) as error:
            _remove_owned_prepare(prepare_path, bundle_fd)
            raise _error("client input staging failed", "CLIENT_DATA_IO_FAILED") from error
        finally:
            os.close(source_fd)
            if bundle_fd >= 0:
                os.close(bundle_fd)

    def transfer_before_write(self, staged_input: StagedClientInputV1) -> StagedClientInputV1:
        self._endpoint_validator()
        bundle_path = self._validate_staged(staged_input)
        if staged_input.lease.state is not StagingOwnershipState.PUBLISHED_UNSENT:
            _invalid("staging dispatch authority was already transferred", code="CLIENT_DATA_STATE_INVALID")
        bundle_fd = os.open(
            bundle_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            fcntl.flock(bundle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current, _ = _read_lease_at(bundle_fd)
            if current != staged_input.lease:
                _invalid("staging lease changed before dispatch", code="CLIENT_DATA_IDENTITY_DRIFT")
            transferred = replace(current, state=StagingOwnershipState.DISPATCH_UNCERTAIN)
            _replace_json_at(bundle_fd, "lease.json", transferred.to_json())
            self._permissions.validate_client_file(bundle_path / "lease.json", staged_input.role)
            os.fsync(bundle_fd)
            _fsync_directory(bundle_path.parent)
        except BlockingIOError as error:
            raise _error("service owns staging reconciliation", "CLIENT_DATA_LOCKED") from error
        finally:
            os.close(bundle_fd)
        return replace(staged_input, lease=transferred)

    def discard_definitely_unsent(
        self,
        staged_input: StagedClientInputV1,
        send_outcome: DispatchWriteOutcome,
    ) -> None:
        if type(send_outcome) is not DispatchWriteOutcome:
            raise TypeError("send_outcome must be exact")
        if send_outcome is DispatchWriteOutcome.WRITE_POSSIBLE:
            return
        self._endpoint_validator()
        bundle_path = self._validate_staged(staged_input)
        bundle_fd = os.open(
            bundle_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            fcntl.flock(bundle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current, _ = _read_lease_at(bundle_fd)
            if current != staged_input.lease or current.state is not StagingOwnershipState.PUBLISHED_UNSENT:
                _invalid("definitely-unsent cleanup lease identity changed", code="CLIENT_DATA_IDENTITY_DRIFT")
            parent_fd = _directory_fd(bundle_path.parent)
            try:
                opened = os.fstat(bundle_fd)
                _detach_and_remove_tree(
                    parent_fd,
                    bundle_path.name,
                    (opened.st_dev, opened.st_ino),
                    descriptor=bundle_fd,
                )
            finally:
                os.close(parent_fd)
        except (OSError, SecureFilesystemError) as error:
            raise _error("definitely-unsent cleanup failed safely", "CLIENT_DATA_CLEANUP_FAILED") from error
        finally:
            os.close(bundle_fd)

    def read_exported_artifact(self, job_id: str, existing_artifact_ref: ClientArtifactRefV1) -> bytes:
        self._endpoint_validator()
        if type(job_id) is not str or not job_id:
            _invalid("export artifact identity is invalid", code="CLIENT_EXPORT_INVALID")
        artifact = ArtifactRef(
            existing_artifact_ref.role,
            existing_artifact_ref.relative_path,
            existing_artifact_ref.sha256,
            existing_artifact_ref.size_bytes,
            existing_artifact_ref.media_type,
        )
        if artifact.role not in EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1:
            _invalid("artifact role is not exportable", code="CLIENT_EXPORT_DENIED")
        relative = PurePosixPath(artifact.relative_path)
        path = self._export_root / "jobs" / job_id / Path(*relative.parts)
        self._permissions.validate_export_file(path)
        parent_fd = self._export_authority.open()
        descriptor = -1
        try:
            for component in ("jobs", job_id, *relative.parts[:-1]):
                next_fd = open_directory_at(parent_fd, component)
                os.close(parent_fd)
                parent_fd = next_fd
            descriptor = open_regular_at(parent_fd, relative.name)
            before = os.fstat(descriptor)
            if before.st_size != artifact.size_bytes:
                _invalid("export artifact size differs from the durable reference", code="CLIENT_EXPORT_INVALID")
            contents = _read_bounded(descriptor, artifact.size_bytes)
            after = os.fstat(descriptor)
            if _stable_identity(before) != _stable_identity(after):
                _invalid("export artifact changed during read", code="CLIENT_EXPORT_INVALID")
            if hashlib.sha256(contents).hexdigest() != artifact.sha256:
                _invalid("export artifact digest differs from the durable reference", code="CLIENT_EXPORT_INVALID")
            return contents
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)

    def _validate_phase(self, path: Path, role: ClientDataRole, phase: str) -> None:
        if path.parent.parent != self._import_root or path.parent.name != role.value or path.name != phase:
            _invalid("client data phase path is outside policy", code="CLIENT_DATA_POLICY_INVALID")
        self._permissions.validate_phase_directory(path, role, phase)

    def _validate_staged(self, staged: StagedClientInputV1) -> Path:
        if type(staged) is not StagedClientInputV1:
            raise TypeError("staged input must be exact")
        expected = self._published_import_root / staged.role.value / "drop" / staged.bundle_name
        if staged.bundle_path != expected or staged.payload_path != expected / "payload":
            _invalid("staged input is outside the policy-bound dropbox", code="CLIENT_DATA_POLICY_INVALID")
        actual = self._import_root / staged.role.value / "drop" / staged.bundle_name
        self._validate_phase(actual.parent, staged.role, "drop")
        self._permissions.validate_client_bundle(actual, staged.role)
        self._permissions.validate_client_file(actual / "payload", staged.role)
        self._permissions.validate_client_file(actual / "lease.json", staged.role)
        return actual


class FilesystemServiceClientData:
    def __init__(
        self,
        import_root: Path,
        export_root: Path,
        *,
        repository: Repository,
        artifact_reader: ArtifactReader,
        permission_oracle: ClientDataPermissionOracle,
        utc_now: Callable[[], datetime],
        endpoint_validator: Callable[[], None],
        reconcile_callback: Callable[
            [str, StagingClock, frozenset[str], tuple[StagingAdmissionRecordV1, ...]],
            tuple[StagingRecoveryEvidenceV1, ...],
        ]
        | None = None,
    ) -> None:
        if not import_root.is_absolute() or not export_root.is_absolute() or import_root == export_root:
            raise ValueError("service import/export roots must be distinct absolute paths")
        self._import_root = import_root
        self._export_root = export_root
        self._import_authority = _DirectoryAuthority.capture(import_root)
        self._export_authority = _DirectoryAuthority.capture(export_root)
        self._repository = repository
        self._artifact_reader = artifact_reader
        self._permissions = permission_oracle
        self._utc_now = utc_now
        self._endpoint_validator = self._bind_authority_validation(endpoint_validator)
        self._reconcile_callback = reconcile_callback
        self._endpoint_validator()

    def _bind_authority_validation(self, endpoint_validator: Callable[[], None]) -> Callable[[], None]:
        def validate() -> None:
            endpoint_validator()
            try:
                for authority in (self._import_authority, self._export_authority):
                    descriptor = authority.open()
                    os.close(descriptor)
            except (OSError, SecureFilesystemError) as error:
                raise _error("client data root ancestor identity changed", "CLIENT_DATA_IDENTITY_DRIFT") from error

        return validate

    @classmethod
    def for_test(
        cls,
        import_root: Path,
        export_root: Path,
        *,
        repository: Repository,
        artifact_reader: ArtifactReader,
        utc_now: Callable[[], datetime],
    ) -> FilesystemServiceClientData:
        for role in ClientDataRole:
            for phase in ("prepare", "drop"):
                directory = import_root / role.value / phase
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o2770)
            for phase in ("admitted", "quarantine", "observations"):
                directory = import_root / role.value / phase
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
        export_root.mkdir(parents=True, exist_ok=True)
        jobs = export_root / "jobs"
        jobs.mkdir(mode=0o2750, exist_ok=True)
        jobs.chmod(0o2750)
        return cls(
            import_root.resolve(),
            export_root.resolve(),
            repository=repository,
            artifact_reader=artifact_reader,
            permission_oracle=_TestPermissionOracle(),
            utc_now=utc_now,
            endpoint_validator=lambda: None,
        )

    def bind_request_envelope(self, request_id: str, role: ClientDataRole, envelope: JsonObject) -> None:
        self._endpoint_validator()
        canonical_request_id(request_id)
        if type(role) is not ClientDataRole or type(envelope) is not dict:
            _invalid("staging request envelope is not exact", code="STAGING_REPLAY_MISMATCH")
        try:
            canonical = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as error:
            raise _error("staging request envelope is not canonical", "STAGING_REPLAY_MISMATCH") from error
        digest = hashlib.sha256(canonical).hexdigest()
        event_id = str(uuid5(NAMESPACE_URL, f"drawingmachine:staging-request:{role.value}:{request_id}"))
        bound = self._repository.bind_staging_request_envelope(event_id, request_id, role.value, digest)
        if bound != digest:
            _invalid("request_id was replayed with different arguments", code="STAGING_REPLAY_MISMATCH")

    def register_decoded(
        self,
        request_id: str,
        role: ClientDataRole,
        staged_path: Path,
    ) -> StagingAdmissionRecordV1:
        self._endpoint_validator()
        existing = self._repository.get_staging_admission(request_id)
        if existing is not None:
            self._validate_existing_replay(existing, role, staged_path)
            return existing
        lease, lease_sha256, _ = self._validate_service_bundle(request_id, role, staged_path, phase="drop")
        now = self._utc_now().isoformat()
        record = StagingAdmissionRecordV1(
            request_id,
            role,
            staged_path.parent.name,
            lease.payload_identity,
            lease.source_sha256,
            lease_sha256,
            lease.publisher_boot_id,
            lease.published_monotonic_ns,
            lease.deadline_monotonic_ns,
            StagingAdmissionState.REQUEST_DECODED,
            None,
            0,
            now,
            now,
            None,
        )
        self._repository.register_decoded_staging(record)
        return record

    def claim_image(self, request_id: str, staged_path: Path) -> ImportedClientFileV1:
        return self._claim(request_id, ClientDataRole.IMAGE, staged_path)

    def claim_gcode(self, request_id: str, staged_path: Path) -> ImportedClientFileV1:
        return self._claim(request_id, ClientDataRole.GCODE, staged_path)

    def _claim(self, request_id: str, role: ClientDataRole, staged_path: Path) -> ImportedClientFileV1:
        self._endpoint_validator()
        current = self._repository.get_staging_admission(request_id)
        if current is not None and current.state is StagingAdmissionState.CLAIMED:
            self._validate_existing_replay(current, role, staged_path)
            return self._imported_client_file(current)
        lease, lease_sha256, bundle_status = self._validate_service_bundle(
            request_id,
            role,
            staged_path,
            phase="drop",
        )
        if current is None or current.state is not StagingAdmissionState.REQUEST_DECODED:
            _invalid("staging request is not decoded and unclaimed", code="STAGING_STATE_INVALID")
        if current.lease_sha256 != lease_sha256 or current.payload_identity != lease.payload_identity:
            _invalid("staging authority changed before claim", code="STAGING_IDENTITY_DRIFT")
        drop = staged_path.parent.parent
        admitted = self._import_root / role.value / "admitted"
        source_parent = _directory_fd(drop)
        bundle_fd = -1
        role_fd = -1
        destination_parent = -1
        try:
            bundle_fd = open_directory_at(source_parent, current.bundle_name)
            if (os.fstat(bundle_fd).st_dev, os.fstat(bundle_fd).st_ino) != (
                bundle_status.st_dev,
                bundle_status.st_ino,
            ):
                _invalid("staging source changed before claim", code="STAGING_IDENTITY_DRIFT")
            fcntl.flock(bundle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            role_fd = _open_verified_directory(self._import_root / role.value)
            destination_parent = _open_private_directory(role_fd, admitted, self._permissions)
            rename_noreplace(source_parent, current.bundle_name, destination_parent, current.bundle_name)
            fsync_directory(source_parent)
            fsync_directory(destination_parent)
            claimed_status = stat_at(destination_parent, current.bundle_name)
            if (claimed_status.st_dev, claimed_status.st_ino) != (bundle_status.st_dev, bundle_status.st_ino):
                _invalid("claimed staging bundle identity changed", code="STAGING_IDENTITY_DRIFT")
        except DrawingMachineError:
            raise
        except (BlockingIOError, OSError, SecureFilesystemError) as error:
            raise _error("staging bundle could not be atomically claimed", "STAGING_CLAIM_FAILED") from error
        finally:
            os.close(source_parent)
            for descriptor in (bundle_fd, destination_parent, role_fd):
                if descriptor >= 0:
                    os.close(descriptor)
        claimed = self._repository.mark_staging_claimed(request_id, expected_revision=current.revision)
        return self._imported_client_file(claimed)

    def consume(self, request_id: str, *, expected_revision: int) -> StagingAdmissionRecordV1:
        self._endpoint_validator()
        current = self._repository.get_staging_admission(request_id)
        if current is None or current.state is not StagingAdmissionState.DURABLY_ADMITTED:
            _invalid("staging admission is not durably admitted", code="STAGING_STATE_INVALID")
        admitted = self._import_root / current.role.value / "admitted"
        bundle = admitted / current.bundle_name
        try:
            bundle_status = os.lstat(bundle)
        except FileNotFoundError:
            bundle_status = None
        if bundle_status is not None:
            lease, lease_sha256, _ = self._validate_service_bundle(
                current.request_id,
                current.role,
                bundle / "payload",
                phase="admitted",
            )
            if (
                current.payload_identity != lease.payload_identity
                or current.source_sha256 != lease.source_sha256
                or current.lease_sha256 != lease_sha256
            ):
                _invalid("durable staging cleanup authority changed", code="STAGING_IDENTITY_DRIFT")
            admitted_fd = _open_verified_directory(admitted)
            try:
                _detach_and_remove_tree(
                    admitted_fd,
                    current.bundle_name,
                    (bundle_status.st_dev, bundle_status.st_ino),
                )
            except (OSError, SecureFilesystemError) as error:
                raise _error("durable staging cleanup could not be completed", "STAGING_CLEANUP_FAILED") from error
            finally:
                os.close(admitted_fd)
        return self._repository.mark_staging_consumed(request_id, expected_revision=expected_revision)

    def publish_artifacts(
        self,
        job_id: str,
        exact_allowed_role_refs: tuple[ClientArtifactRefV1, ...],
    ) -> tuple[PublishedClientArtifactV1, ...]:
        self._endpoint_validator()
        if not _safe_component(job_id):
            _invalid("export job_id is invalid", code="CLIENT_EXPORT_INVALID")
        roles = [artifact.role for artifact in exact_allowed_role_refs]
        if len(roles) != len(set(roles)) or any(role not in EXPORTABLE_CLIENT_ARTIFACT_ROLES_V1 for role in roles):
            _invalid("export artifact role set is not authorized", code="CLIENT_EXPORT_DENIED")
        published: list[PublishedClientArtifactV1] = []
        export_fd = -1
        jobs_fd = -1
        job_fd = -1
        try:
            export_fd = self._export_authority.open()
            jobs_fd = _open_static_export_directory(
                export_fd,
                self._export_root / "jobs",
                self._permissions,
            )
            root = self._export_root / "jobs" / job_id
            job_fd = _ensure_runtime_export_directory(jobs_fd, root, self._permissions)
            for artifact in exact_allowed_role_refs:
                relative = PurePosixPath(artifact.relative_path)
                if relative.is_absolute() or any(not _safe_component(part) for part in relative.parts):
                    _invalid("export artifact path is unsafe", code="CLIENT_EXPORT_INVALID")
                destination = root.joinpath(*relative.parts)
                parent_fd = os.dup(job_fd)
                parent_path = root
                try:
                    for component in relative.parts[:-1]:
                        parent_path /= component
                        next_fd = _ensure_runtime_export_directory(parent_fd, parent_path, self._permissions)
                        os.close(parent_fd)
                        parent_fd = next_fd
                    name = relative.name
                    try:
                        _validate_existing_export_at(parent_fd, name, artifact.sha256, artifact.size_bytes)
                    except FileNotFoundError:
                        pass
                    else:
                        self._permissions.validate_export_file(destination)
                        published.append(PublishedClientArtifactV1(job_id, artifact, destination))
                        continue
                    contents = self._artifact_reader.read_bytes(
                        job_id,
                        cast(ArtifactRef, artifact),
                        expected_media_type=artifact.media_type,
                        max_bytes=max(1, artifact.size_bytes),
                    )
                    if len(contents) != artifact.size_bytes or hashlib.sha256(contents).hexdigest() != artifact.sha256:
                        _invalid("canonical artifact differs from its durable reference", code="CLIENT_EXPORT_INVALID")
                    temporary = hidden_name("export")
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o640,
                        dir_fd=parent_fd,
                    )
                    try:
                        os.fchmod(descriptor, 0o640)
                        _write_all(descriptor, contents)
                        os.fsync(descriptor)
                        try:
                            os.link(
                                temporary,
                                name,
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileExistsError:
                            _validate_existing_export_at(parent_fd, name, artifact.sha256, artifact.size_bytes)
                    finally:
                        try:
                            _remove_export_temporary(parent_fd, temporary, descriptor)
                        finally:
                            os.close(descriptor)
                    fsync_directory(parent_fd)
                    self._permissions.validate_export_file(destination)
                    published.append(PublishedClientArtifactV1(job_id, artifact, destination))
                finally:
                    os.close(parent_fd)
            return tuple(published)
        except DrawingMachineError:
            raise
        except (OSError, SecureFilesystemError) as error:
            raise _error("export authority could not be accessed safely", "CLIENT_EXPORT_INVALID") from error
        finally:
            if job_fd >= 0:
                os.close(job_fd)
            if jobs_fd >= 0:
                os.close(jobs_fd)
            if export_fd >= 0:
                os.close(export_fd)

    def reconcile(
        self,
        trigger: str,
        clock: StagingClock,
        active_requests: frozenset[str],
        durable_admissions: tuple[StagingAdmissionRecordV1, ...],
    ) -> tuple[StagingRecoveryEvidenceV1, ...]:
        callback = self._reconcile_callback
        if callback is None:
            _invalid("staging reconciliation authority is not installed", code="STAGING_RECONCILER_UNAVAILABLE")
        return callback(trigger, clock, active_requests, durable_admissions)

    def _validate_service_bundle(
        self,
        request_id: str,
        role: ClientDataRole,
        staged_path: Path,
        *,
        phase: str,
    ) -> tuple[StagingLeaseV1, str, os.stat_result]:
        canonical_request_id(request_id)
        if type(role) is not ClientDataRole or not staged_path.is_absolute():
            _invalid("staging request authority is invalid", code="STAGING_PATH_INVALID")
        bundle = staged_path.parent
        expected_parent = self._import_root / role.value / phase
        if staged_path.name != "payload" or bundle.parent != expected_parent:
            _invalid("staging path is outside the exact role dropbox", code="STAGING_PATH_INVALID")
        parsed_role, parsed_request = parse_bundle_name(bundle.name, phase="stage")
        if (parsed_role, parsed_request) != (role, request_id):
            _invalid("staging path name does not bind request and role", code="STAGING_PATH_INVALID")
        if phase == "drop":
            self._permissions.validate_phase_directory(expected_parent, role, "drop")
        elif phase == "admitted":
            self._permissions.validate_private_directory(expected_parent)
        else:
            _invalid("service bundle phase is not authorized", code="STAGING_PATH_INVALID")
        self._permissions.validate_client_bundle(bundle, role)
        self._permissions.validate_client_file(bundle / "payload", role)
        self._permissions.validate_client_file(bundle / "lease.json", role)
        bundle_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            entries = set(os.listdir(bundle_fd))
            if entries != {"payload", "lease.json"}:
                _invalid("staging bundle children are not exact", code="STAGING_PATH_INVALID")
            lease, lease_sha256 = _read_lease_at(bundle_fd)
            if lease.state is not StagingOwnershipState.DISPATCH_UNCERTAIN:
                _invalid("decoded staging lease is not dispatch-uncertain", code="STAGING_STATE_INVALID")
            if lease.request_id != request_id or lease.role is not role:
                _invalid("staging lease authority does not match request", code="STAGING_REPLAY_MISMATCH")
            payload = os.open("payload", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=bundle_fd)
            try:
                before = os.fstat(payload)
                contents = _read_bounded(payload, lease.source_size_bytes)
                after = os.fstat(payload)
            finally:
                os.close(payload)
            identity = PayloadIdentityV1(
                before.st_dev, before.st_ino, before.st_size, hashlib.sha256(contents).hexdigest()
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or _stable_identity(before) != _stable_identity(after)
                or identity != lease.payload_identity
            ):
                _invalid("staging payload identity differs from its lease", code="STAGING_IDENTITY_DRIFT")
            return lease, lease_sha256, os.fstat(bundle_fd)
        finally:
            os.close(bundle_fd)

    def _validate_existing_replay(
        self,
        existing: StagingAdmissionRecordV1,
        role: ClientDataRole,
        staged_path: Path,
    ) -> None:
        expected_drop = self._import_root / role.value / "drop" / existing.bundle_name / "payload"
        if existing.role is not role or staged_path != expected_drop:
            _invalid("request_id was reused for a different staging authority", code="STAGING_REPLAY_MISMATCH")
        if existing.state in {StagingAdmissionState.QUARANTINED, StagingAdmissionState.RECOVERY_REQUIRED}:
            _invalid("staging replay is blocked by recovery state", code="STAGING_STATE_INVALID")

        phase = "drop" if existing.state is StagingAdmissionState.REQUEST_DECODED else "admitted"
        bundle = self._import_root / role.value / phase / existing.bundle_name
        if bundle.exists():
            lease, lease_sha256, _ = self._validate_service_bundle(
                existing.request_id,
                role,
                bundle / "payload",
                phase=phase,
            )
            if (
                existing.payload_identity != lease.payload_identity
                or existing.source_sha256 != lease.source_sha256
                or existing.lease_sha256 != lease_sha256
            ):
                _invalid("replayed staging authority differs from its durable record", code="STAGING_REPLAY_MISMATCH")
            return
        if existing.state in {StagingAdmissionState.REQUEST_DECODED, StagingAdmissionState.CLAIMED}:
            _invalid("replayed staging authority is missing before durable admission", code="STAGING_STATE_INVALID")

        recreated_drop = expected_drop.parent
        if recreated_drop.exists():
            _invalid("consumed staging authority was recreated", code="STAGING_REPLAY_MISMATCH")

    def _imported_client_file(self, admission: StagingAdmissionRecordV1) -> ImportedClientFileV1:
        return ImportedClientFileV1(
            admission.request_id,
            admission.role,
            admission.bundle_name,
            self._import_root / admission.role.value / "admitted" / admission.bundle_name / "payload",
            admission.payload_identity,
            admission.source_sha256,
            admission.lease_sha256,
        )


def _open_canonical_regular(path: Path) -> tuple[int, os.stat_result, Path]:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _error("client input cannot be resolved to one regular file", "CLIENT_INPUT_INVALID") from error
    stack = ExitStack()
    try:
        parent_fd = stack.enter_context(_opened_directory(Path("/")))
        for component in resolved.parts[1:-1]:
            descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            stack.callback(os.close, descriptor)
            parent_fd = descriptor
        descriptor = os.open(
            resolved.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        status = os.fstat(descriptor)
        final = os.stat(resolved.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode) or _stable_identity(status) != _stable_identity(final):
            os.close(descriptor)
            _invalid("resolved client input is not one stable regular file", code="CLIENT_INPUT_INVALID")
        return descriptor, status, resolved
    except OSError as error:
        raise _error("client input cannot be opened no-follow", "CLIENT_INPUT_INVALID") from error
    finally:
        stack.close()


class _opened_directory:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor = -1

    def __enter__(self) -> int:
        self._descriptor = os.open(self._path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        return self._descriptor

    def __exit__(self, *args: object) -> None:
        os.close(self._descriptor)


def _verify_source(descriptor: int, expected: os.stat_result, resolved: Path) -> None:
    current = os.fstat(descriptor)
    path_status = os.lstat(resolved)
    if _stable_identity(current) != _stable_identity(expected) or _stable_identity(path_status) != _stable_identity(
        expected
    ):
        _invalid("client input changed during staging", code="CLIENT_INPUT_CHANGED")


def _copy_payload(source_fd: int, source_status: os.stat_result, bundle_fd: int, limit: int) -> tuple[str, int]:
    payload_fd = os.open(
        "payload",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o640,
        dir_fd=bundle_fd,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        os.fchmod(payload_fd, 0o640)
        while chunk := os.read(source_fd, min(1024 * 1024, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                _invalid("client input exceeds the exact role limit", code="CLIENT_INPUT_INVALID")
            _write_all(payload_fd, chunk)
            digest.update(chunk)
        os.fsync(payload_fd)
        current = os.fstat(payload_fd)
        if current.st_size != total or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode):
            _invalid("staged payload identity is unsafe", code="CLIENT_DATA_IDENTITY_DRIFT")
    finally:
        os.close(payload_fd)
    if total != source_status.st_size:
        _invalid("client input read was short or changed size", code="CLIENT_INPUT_CHANGED")
    return digest.hexdigest(), total


def _write_json_at(parent_fd: int, name: str, value: object) -> str:
    contents = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(contents) > MAX_LEASE_BYTES:
        _invalid("staging lease exceeds its byte limit", code="CLIENT_DATA_INTERNAL")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o640,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o640)
        _write_all(descriptor, contents)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(contents).hexdigest()


def _replace_json_at(parent_fd: int, name: str, value: object) -> str:
    temporary = ".lease-" + secrets.token_hex(16)
    digest = _write_json_at(parent_fd, temporary, value)
    os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)
    return digest


def _read_lease_at(parent_fd: int) -> tuple[StagingLeaseV1, str]:
    descriptor = os.open("lease.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size > MAX_LEASE_BYTES:
            _invalid("staging lease type, link, or size is unsafe", code="CLIENT_DATA_IDENTITY_DRIFT")
        contents = _read_bounded(descriptor, MAX_LEASE_BYTES)
        if _stable_identity(status) != _stable_identity(os.fstat(descriptor)):
            _invalid("staging lease changed during read", code="CLIENT_DATA_IDENTITY_DRIFT")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(contents, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        return StagingLeaseV1.from_json(value), hashlib.sha256(contents).hexdigest()
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _error("staging lease is not strict closed JSON", "CLIENT_DATA_LEASE_INVALID") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _require_phase_quota(path: Path, role: ClientDataRole, limit: int) -> None:
    entries = list(os.scandir(path))
    if len(entries) >= MAX_BUNDLES_PER_PHASE:
        _invalid("staging phase entry quota is exhausted", code="CLIENT_DATA_QUOTA_EXCEEDED")
    total = 0
    for entry in entries:
        parsed_role, _ = parse_bundle_name(entry.name)
        if parsed_role is not role or not entry.is_dir(follow_symlinks=False):
            _invalid("staging phase contains an unauthorized entry", code="CLIENT_DATA_POLICY_INVALID")
        payload = Path(entry.path) / "payload"
        if payload.exists():
            value = os.lstat(payload)
            if not stat.S_ISREG(value.st_mode):
                _invalid("staging phase contains a non-regular payload", code="CLIENT_DATA_POLICY_INVALID")
            total += value.st_size
    if total > MAX_BUNDLES_PER_PHASE * limit:
        _invalid("staging phase aggregate quota is exceeded", code="CLIENT_DATA_QUOTA_EXCEEDED")


def _directory_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_verified_directory(path: Path) -> int:
    descriptor, _ = _walk_absolute_directory(path)
    return descriptor


def _walk_absolute_directory(path: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    if not path.is_absolute():
        raise SecureFilesystemError(f"managed directory must be absolute: {path}")
    descriptor = os.open(Path("/"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    identities = [(os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)]
    try:
        for component in path.parts[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(next_fd)
                if (
                    stat.S_ISLNK(before.st_mode)
                    or not stat.S_ISDIR(before.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise SecureFilesystemError(f"managed directory component identity changed: {component}")
                os.close(descriptor)
            except BaseException:
                os.close(next_fd)
                raise
            descriptor = next_fd
            identities.append((opened.st_dev, opened.st_ino))
        return descriptor, tuple(identities)
    except OSError as error:
        os.close(descriptor)
        raise SecureFilesystemError(f"managed directory cannot be opened safely: {path}") from error
    except BaseException:
        os.close(descriptor)
        raise


def _remove_export_temporary(parent_fd: int, name: str, descriptor: int) -> None:
    try:
        stat_at(parent_fd, name)
    except FileNotFoundError:
        return
    if not remove_regular_if_identity(parent_fd, name, descriptor):
        raise SecureFilesystemError("export temporary identity changed before cleanup")


def _open_private_directory(
    role_fd: int,
    path: Path,
    permissions: ClientDataPermissionOracle,
) -> int:
    return _open_validated_directory(role_fd, path, permissions.validate_private_directory)


def _open_static_export_directory(
    parent_fd: int,
    path: Path,
    permissions: ClientDataPermissionOracle,
) -> int:
    return _open_validated_directory(parent_fd, path, permissions.validate_export_directory)


def _open_validated_directory(parent_fd: int, path: Path, validate: Callable[[Path], None]) -> int:
    before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    absolute_before = os.lstat(path)
    if (before.st_dev, before.st_ino) != (absolute_before.st_dev, absolute_before.st_ino):
        raise SecureFilesystemError("managed directory identity changed before validation")
    validate(path)
    descriptor = open_directory_at(parent_fd, path.name)
    try:
        opened = os.fstat(descriptor)
        relative_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        absolute_after = os.lstat(path)
        identity = (before.st_dev, before.st_ino)
        if any((value.st_dev, value.st_ino) != identity for value in (opened, relative_after, absolute_after)):
            raise SecureFilesystemError("managed directory identity changed during validation")
        validate(path)
        relative_final = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        absolute_final = os.lstat(path)
        if any((value.st_dev, value.st_ino) != identity for value in (relative_final, absolute_final)):
            raise SecureFilesystemError("managed directory identity changed after validation")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_runtime_export_directory(
    parent_fd: int,
    path: Path,
    permissions: ClientDataPermissionOracle,
) -> int:
    descriptor = _ensure_export_directory(parent_fd, path.name)
    try:
        permissions.validate_export_directory(path)
        opened = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SecureFilesystemError("runtime export directory identity changed")
        permissions.validate_export_directory(path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_export_directory(parent_fd: int, name: str) -> int:
    if not _safe_component(name):
        raise SecureFilesystemError("export directory component is unsafe")
    try:
        os.mkdir(name, 0o2750, dir_fd=parent_fd)
    except FileExistsError:
        descriptor = open_directory_at(parent_fd, name)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o2750:
            os.close(descriptor)
            raise SecureFilesystemError(f"existing export directory mode is unsafe: {name}") from None
        return descriptor
    descriptor = open_directory_at(parent_fd, name)
    try:
        os.fchmod(descriptor, 0o2750)
        fsync_directory(parent_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_posix_acl(path: Path, attribute: str, mode: int) -> tuple[tuple[int, int | None, int], ...]:
    try:
        raw = os.getxattr(path, attribute, follow_symlinks=False)
    except OSError as error:
        absent = {errno.ENODATA}
        if hasattr(errno, "ENOATTR"):
            absent.add(errno.ENOATTR)
        if error.errno not in absent:
            _invalid("client data ACL evidence cannot be read", code="CLIENT_DATA_POLICY_INVALID")
        if attribute.endswith("default"):
            return ()
        return tuple(
            sorted(
                (
                    (1, None, (mode >> 6) & 0o7),
                    (4, None, (mode >> 3) & 0o7),
                    (32, None, mode & 0o7),
                )
            )
        )
    if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != 2 or (len(raw) - 4) % 8:
        _invalid("client data ACL evidence is malformed", code="CLIENT_DATA_POLICY_INVALID")
    result: list[tuple[int, int | None, int]] = []
    for offset in range(4, len(raw), 8):
        tag, permissions, qualifier = struct.unpack_from("<HHI", raw, offset)
        if tag not in {1, 2, 4, 8, 16, 32}:
            _invalid("client data ACL evidence has an unknown tag", code="CLIENT_DATA_POLICY_INVALID")
        result.append((tag, qualifier if tag in {2, 8} else None, permissions))
    return tuple(sorted(result))


def _validate_mode_only_acl(path: Path, mode: int) -> None:
    expected = tuple(
        sorted(
            (
                (1, None, (mode >> 6) & 0o7),
                (4, None, (mode >> 3) & 0o7),
                (32, None, mode & 0o7),
            )
        )
    )
    if _read_posix_acl(path, "system.posix_acl_access", mode) != expected or _read_posix_acl(
        path,
        "system.posix_acl_default",
        mode,
    ):
        _invalid("private client data ACL is not exact", code="CLIENT_DATA_POLICY_INVALID")


def _safe_component(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


def _detach_and_remove_tree(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    descriptor: int | None = None,
) -> None:
    owned_descriptor = descriptor is None
    if descriptor is None:
        descriptor = open_directory_at(parent_fd, name)
    detached = hidden_name("cleanup")
    try:
        if owned_descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise SecureFilesystemError(f"cleanup authority identity changed: {name}")
        if not entry_matches_descriptor(parent_fd, name, descriptor, directory=True):
            raise SecureFilesystemError(f"cleanup authority entry changed: {name}")
        rename_noreplace(parent_fd, name, parent_fd, detached)
        fsync_directory(parent_fd)
        if not entry_matches_descriptor(parent_fd, detached, descriptor, directory=True):
            raise SecureFilesystemError(f"detached cleanup authority identity changed: {name}")
    finally:
        if owned_descriptor:
            os.close(descriptor)
    remove_tree_at(parent_fd, detached)


def _fsync_directory(path: Path) -> None:
    descriptor = _directory_fd(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _invalid("staged file write was short", code="CLIENT_DATA_IO_FAILED")
        view = view[written:]


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - total)):
        total += len(chunk)
        if total > limit:
            _invalid("bounded client data read exceeded its limit", code="CLIENT_DATA_QUOTA_EXCEEDED")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_existing_export_at(
    parent_fd: int,
    name: str,
    expected_sha256: str,
    expected_size: int,
) -> None:
    stat_at(parent_fd, name)
    descriptor = open_regular_at(parent_fd, name)
    try:
        before = os.fstat(descriptor)
        contents = _read_bounded(descriptor, expected_size)
        after = os.fstat(descriptor)
        current = stat_at(parent_fd, name)
    finally:
        os.close(descriptor)
    if (
        before.st_nlink != 1
        or before.st_size != expected_size
        or _stable_identity(before) != _stable_identity(after)
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        or hashlib.sha256(contents).hexdigest() != expected_sha256
    ):
        _invalid("export destination already contains different bytes", code="CLIENT_EXPORT_COLLISION")


def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _remove_owned_prepare(path: Path, descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        opened = os.fstat(descriptor)
        parent_fd = _directory_fd(path.parent)
        try:
            _detach_and_remove_tree(
                parent_fd,
                path.name,
                (opened.st_dev, opened.st_ino),
                descriptor=descriptor,
            )
        finally:
            os.close(parent_fd)
    except (OSError, SecureFilesystemError):
        return


def _error(message: str, code: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload(code, ErrorCategory.PERMISSION, message, False, {}))


def _invalid(message: str, *, code: str) -> NoReturn:
    raise _error(message, code)


__all__ = [
    "ClientDataPermissionOracle",
    "FilesystemClientInputStaging",
    "NativeClientDataPermissionOracle",
    "SystemStagingClock",
]
