from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.client_data import (
    MAX_LEASE_BYTES,
    ClientDataRole,
    PayloadIdentityV1,
    PrepareObservationStatus,
    PrepareObservationV1,
    PrepareRecoveryCode,
    StagingAdmissionRecordV1,
    StagingAdmissionState,
    StagingClock,
    StagingLeaseV1,
    StagingRecoveryAction,
    StagingRecoveryEvidenceV1,
    StagingRecoveryReasonCode,
    StagingRecoveryStatus,
    parse_bundle_name,
)
from drawingmachine.ports.repository import Repository

_TRIGGERS = frozenset({"startup", "shutdown", "periodic", "before-command", "after-command"})
_MAX_OBSERVATION_BYTES = 65_536


class SecureFilesystemError(RuntimeError):
    pass


class StagingFilesystemOperations(Protocol):
    def create_regular_at(self, parent_fd: int, name: str) -> int: ...

    def entry_matches_descriptor(self, parent_fd: int, name: str, descriptor: int, *, directory: bool) -> bool: ...

    def fsync_directory(self, descriptor: int) -> None: ...

    def hidden_name(self, prefix: str) -> str: ...

    def open_directory_at(self, parent_fd: int, name: str) -> int: ...

    def read_stable_regular_bounded(
        self,
        parent_fd: int,
        name: str,
        *,
        max_bytes: int,
        nonblocking: bool = False,
    ) -> tuple[object, bytes]: ...

    def remove_tree_at(self, parent_fd: int, name: str) -> None: ...

    def rename_noreplace(
        self,
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None: ...

    def replace_file(self, parent_fd: int, temporary_name: str, destination_name: str) -> None: ...

    def stat_at(self, parent_fd: int, name: str) -> os.stat_result: ...


@dataclass(frozen=True, slots=True)
class ReconciliationResultV1:
    trigger: str
    blocked_roles: frozenset[ClientDataRole]
    evidence: tuple[StagingRecoveryEvidenceV1, ...]


class StagingReconciler:
    def __init__(
        self,
        import_root: Path,
        repository: Repository,
        filesystem: StagingFilesystemOperations,
    ) -> None:
        if not import_root.is_absolute():
            raise ValueError("staging reconciliation root must be absolute")
        self._root = import_root
        self._repository = repository
        self._filesystem = filesystem
        self._locks = {role: threading.Lock() for role in ClientDataRole}
        self._blocked: set[ClientDataRole] = set()

    @property
    def blocked_roles(self) -> frozenset[ClientDataRole]:
        return frozenset(self._blocked)

    def reconcile(
        self,
        trigger: str,
        clock: StagingClock,
        active_requests: frozenset[str] = frozenset(),
        durable_admissions: tuple[StagingAdmissionRecordV1, ...] | None = None,
    ) -> ReconciliationResultV1:
        if trigger not in _TRIGGERS:
            raise ValueError("staging reconciliation trigger is not closed")
        if type(active_requests) is not frozenset or any(type(item) is not str for item in active_requests):
            raise TypeError("active staging request authority must be an exact frozenset")
        admissions = self._repository.list_staging_admissions() if durable_admissions is None else durable_admissions
        if type(admissions) is not tuple or any(type(item) is not StagingAdmissionRecordV1 for item in admissions):
            raise TypeError("durable staging admission authority must be an exact tuple")
        evidence: list[StagingRecoveryEvidenceV1] = []
        for role in ClientDataRole:
            with self._locks[role]:
                role_evidence, blocked = self._reconcile_role(role, clock, active_requests, admissions)
                evidence.extend(role_evidence)
                if blocked:
                    self._blocked.add(role)
                else:
                    self._blocked.discard(role)
        return ReconciliationResultV1(trigger, frozenset(self._blocked), tuple(evidence))

    def require_role_ready(self, role: ClientDataRole) -> None:
        if role in self._blocked:
            raise DrawingMachineError(
                ErrorPayload(
                    "STAGING_RECOVERY_REQUIRED",
                    ErrorCategory.SERVICE,
                    "staging reconciliation blocks this client-data role",
                    False,
                    {"role": role.value},
                )
            )

    def _reconcile_role(
        self,
        role: ClientDataRole,
        clock: StagingClock,
        active_requests: frozenset[str],
        durable_admissions: tuple[StagingAdmissionRecordV1, ...],
    ) -> tuple[list[StagingRecoveryEvidenceV1], bool]:
        role_root = self._root / role.value
        prepare = role_root / "prepare"
        drop = role_root / "drop"
        admitted = role_root / "admitted"
        quarantine = role_root / "quarantine"
        observations = role_root / "observations"
        try:
            role_fd = _open_verified_directory(role_root)
            try:
                for private in (admitted, quarantine, observations):
                    descriptor = _open_private_directory(role_fd, private, self._filesystem)
                    os.close(descriptor)
            finally:
                os.close(role_fd)
        except (OSError, SecureFilesystemError) as error:
            raise _error("private staging authority cannot be opened safely", "STAGING_PRIVATE_ROOT_INVALID") from error
        now = clock.monotonic_ns()
        boot = clock.boot_id()
        evidence: list[StagingRecoveryEvidenceV1] = []
        blocked = False

        known = {path.name: path for path in observations.glob("*.json")}
        for entry in sorted(prepare.iterdir(), key=lambda path: path.name):
            try:
                parsed_role, request_id = parse_bundle_name(entry.name, phase="prepare")
                if parsed_role is not role:
                    raise ValueError("wrong role")
            except ValueError:
                item = self._quarantine_path(role, entry, None, "INVALID_PREPARE_NAME", clock)
                evidence.append(item)
                blocked |= item.status is StagingRecoveryStatus.RECOVERY_REQUIRED
                continue
            observation_name = f"{entry.name}.json"
            if observation_name not in known:
                value = os.lstat(entry)
                observation = PrepareObservationV1(
                    role,
                    entry.name,
                    entry.name.removesuffix(".prepare") + ".stage",
                    (value.st_dev, value.st_ino),
                    request_id,
                    boot,
                    now,
                    now + 300_000_000_000,
                    PrepareObservationStatus.ACTIVE,
                    0,
                    None,
                )
                path = observations / observation_name
                self._write_observation(path, observation)
                known[observation_name] = path

        for observation_path in sorted(known.values(), key=lambda path: path.name):
            try:
                observation = self._read_observation(observation_path)
            except (OSError, SecureFilesystemError, ValueError, TypeError):
                evidence.append(self._recovery_marker(role, observation_path.name, None, "OBSERVATION_CORRUPT", clock))
                blocked = True
                continue
            if observation.status is PrepareObservationStatus.RECOVERY_REQUIRED:
                blocked = True
                continue
            if observation.request_id in active_requests:
                continue
            if observation.status in {
                PrepareObservationStatus.PUBLISHED_HANDOFF,
                PrepareObservationStatus.CLIENT_ABORTED,
                PrepareObservationStatus.QUARANTINED,
            }:
                try:
                    self._delete_terminal_observation(observation_path, observation, clock)
                except DrawingMachineError as error:
                    evidence.append(
                        _evidence(
                            role,
                            observation.prepare_bundle_name,
                            observation.request_id,
                            error.payload.code,
                            clock,
                            StagingRecoveryStatus.RECOVERY_REQUIRED,
                        )
                    )
                    blocked = True
                continue
            prepare_path = prepare / observation.prepare_bundle_name
            drop_path = drop / observation.published_bundle_name
            descriptor = -1
            lock_held = False
            if prepare_path.exists():
                try:
                    descriptor = os.open(
                        prepare_path,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    )
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_held = True
                except OSError:
                    evidence.append(
                        self._recovery_marker(
                            role,
                            observation.prepare_bundle_name,
                            observation.request_id,
                            "LOCK_RACE",
                            clock,
                        )
                    )
                    blocked = True
                    continue
            try:
                if prepare_path.exists():
                    current_identity = os.lstat(prepare_path)
                    if (current_identity.st_dev, current_identity.st_ino) != observation.bundle_identity:
                        evidence.append(
                            self._recovery_marker(
                                role,
                                observation.prepare_bundle_name,
                                observation.request_id,
                                "IDENTITY_DRIFT",
                                clock,
                            )
                        )
                        blocked = True
                        continue
                if lock_held and observation.first_seen_boot_id != boot:
                    evidence.append(
                        self._recovery_marker(
                            role,
                            observation.prepare_bundle_name,
                            observation.request_id,
                            "LOCK_RACE",
                            clock,
                        )
                    )
                    blocked = True
                    continue
                if lock_held and observation.first_seen_boot_id == boot:
                    if now < observation.deadline_monotonic_ns:
                        blocked = True
                        continue
                    self._update_observation(
                        observation_path,
                        observation,
                        PrepareObservationStatus.HELD_EXPIRED,
                    )
                    blocked = True
                    continue
                if drop_path.exists():
                    self._terminal_observation(
                        observation_path,
                        observation,
                        PrepareObservationStatus.PUBLISHED_HANDOFF,
                        clock,
                    )
                    continue
                if not prepare_path.exists():
                    admission = self._repository.get_staging_admission(observation.request_id)
                    if admission is None:
                        self._terminal_observation(
                            observation_path,
                            observation,
                            PrepareObservationStatus.CLIENT_ABORTED,
                            clock,
                        )
                        continue
                    if admission.state in {
                        StagingAdmissionState.CLAIMED,
                        StagingAdmissionState.DURABLY_ADMITTED,
                        StagingAdmissionState.CONSUMED,
                    }:
                        self._terminal_observation(
                            observation_path,
                            observation,
                            PrepareObservationStatus.PUBLISHED_HANDOFF,
                            clock,
                        )
                        continue
                if prepare_path.exists() and not lock_held:
                    try:
                        prepare_lease = _read_bundle_lease(prepare_path, role, phase="prepare")
                    except (DrawingMachineError, OSError, ValueError, TypeError):
                        prepare_lease = None
                    item = self._quarantine_path(
                        role,
                        prepare_path,
                        observation.request_id,
                        "STALE_PREPARE",
                        clock,
                        payload_identity=None if prepare_lease is None else prepare_lease.payload_identity,
                        source_sha256=None if prepare_lease is None else prepare_lease.source_sha256,
                        prior_state="CLIENT_PREPARED" if prepare_lease is None else prepare_lease.state.value,
                    )
                    evidence.append(item)
                    if item.status is StagingRecoveryStatus.COMPLETE:
                        self._terminal_observation(
                            observation_path,
                            observation,
                            PrepareObservationStatus.QUARANTINED,
                            clock,
                        )
                    else:
                        blocked = True
                else:
                    blocked = True
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

        admissions = {item.bundle_name: item for item in durable_admissions if item.role is role}
        for phase_root, phase in ((drop, "drop"), (admitted, "admitted")):
            for bundle in sorted(phase_root.iterdir(), key=lambda path: path.name):
                if not bundle.is_dir():
                    item = self._quarantine_path(role, bundle, None, "SPECIAL_STAGING_ENTRY", clock)
                    evidence.append(item)
                    blocked |= item.status is StagingRecoveryStatus.RECOVERY_REQUIRED
                    continue
                try:
                    lease = _read_bundle_lease(bundle, role)
                except (DrawingMachineError, OSError, ValueError, TypeError):
                    item = self._quarantine_path(role, bundle, None, "INVALID_STAGING_BUNDLE", clock)
                    evidence.append(item)
                    blocked |= item.status is StagingRecoveryStatus.RECOVERY_REQUIRED
                    continue
                admission = admissions.get(bundle.name)
                if lease.request_id in active_requests and (
                    admission is None
                    or admission.state in {StagingAdmissionState.REQUEST_DECODED, StagingAdmissionState.CLAIMED}
                ):
                    continue
                if admission is not None:
                    if (
                        admission.request_id != lease.request_id
                        or admission.role is not lease.role
                        or admission.payload_identity != lease.payload_identity
                        or admission.source_sha256 != lease.source_sha256
                    ):
                        evidence.append(
                            self._recovery_marker(role, bundle.name, admission.request_id, "PUBLISH_MISMATCH", clock)
                        )
                        blocked = True
                        continue
                    if (
                        admission.state
                        in {
                            StagingAdmissionState.REQUEST_DECODED,
                            StagingAdmissionState.CLAIMED,
                        }
                        and now < admission.deadline_monotonic_ns
                        and admission.publisher_boot_id == boot
                    ):
                        continue
                    if admission.state is StagingAdmissionState.DURABLY_ADMITTED and phase == "admitted":
                        try:
                            _detach_and_remove_bundle(phase_root, bundle.name, self._filesystem)
                        except (OSError, RuntimeError):
                            evidence.append(
                                self._recovery_marker(
                                    role,
                                    bundle.name,
                                    admission.request_id,
                                    "QUARANTINE_FAILED",
                                    clock,
                                )
                            )
                            blocked = True
                            continue
                        self._repository.mark_staging_consumed(
                            admission.request_id,
                            expected_revision=admission.revision,
                        )
                        continue
                    if admission.state is StagingAdmissionState.CONSUMED:
                        try:
                            _detach_and_remove_bundle(phase_root, bundle.name, self._filesystem)
                        except (OSError, RuntimeError):
                            evidence.append(
                                self._recovery_marker(
                                    role,
                                    bundle.name,
                                    admission.request_id,
                                    "QUARANTINE_FAILED",
                                    clock,
                                )
                            )
                            blocked = True
                        continue
                deadline = (
                    lease.deadline_monotonic_ns
                    if admission is None and lease.publisher_boot_id == boot
                    else 0
                    if admission is None or admission.publisher_boot_id != boot
                    else admission.deadline_monotonic_ns
                )
                if now >= deadline:
                    item = self._quarantine_path(
                        role,
                        bundle,
                        lease.request_id if admission is None else admission.request_id,
                        "STALE_DROP" if phase == "drop" else "STALE_ADMITTED",
                        clock,
                        payload_identity=lease.payload_identity,
                        source_sha256=lease.source_sha256,
                        prior_state=lease.state.value if admission is None else admission.state.value,
                    )
                    evidence.append(item)
                    blocked |= item.status is StagingRecoveryStatus.RECOVERY_REQUIRED
                    if admission is not None and item.status is StagingRecoveryStatus.COMPLETE:
                        self._repository.record_staging_recovery(
                            admission.request_id,
                            expected_revision=admission.revision,
                            recovery_code=(
                                StagingRecoveryReasonCode.STALE_DROP
                                if phase == "drop"
                                else StagingRecoveryReasonCode.STALE_ADMITTED
                            ),
                            quarantined=True,
                        )
        if any(path.name.startswith("recovery-") and path.suffix == ".json" for path in quarantine.iterdir()):
            blocked = True
        return evidence, blocked

    def _write_observation(self, path: Path, observation: PrepareObservationV1) -> None:
        self._atomic_json(path, _observation_json(observation))

    def _read_observation(self, path: Path) -> PrepareObservationV1:
        parent_fd = _open_verified_directory(path.parent)
        try:
            _, contents = self._filesystem.read_stable_regular_bounded(
                parent_fd,
                path.name,
                max_bytes=_MAX_OBSERVATION_BYTES,
                nonblocking=True,
            )
        finally:
            os.close(parent_fd)
        data = _strict_json(contents)
        expected = {
            "schema_version",
            "role",
            "prepare_bundle_name",
            "published_bundle_name",
            "bundle_identity",
            "request_id",
            "first_seen_boot_id",
            "first_seen_monotonic_ns",
            "deadline_monotonic_ns",
            "status",
            "revision",
            "recovery_code",
        }
        if set(data) != expected or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("observation fields are not exact")
        identity_value = data["bundle_identity"]
        if (
            type(identity_value) is not list
            or len(identity_value) != 2
            or any(type(item) is not int for item in identity_value)
        ):
            raise ValueError("observation identity is not exact")
        identity = cast(list[int], identity_value)
        for field in (
            "role",
            "prepare_bundle_name",
            "published_bundle_name",
            "request_id",
            "first_seen_boot_id",
            "status",
        ):
            if type(data[field]) is not str:
                raise ValueError("observation string fields are not exact")
        for field in ("first_seen_monotonic_ns", "deadline_monotonic_ns", "revision"):
            if type(data[field]) is not int:
                raise ValueError("observation integer fields are not exact")
        if data["recovery_code"] is not None and type(data["recovery_code"]) is not str:
            raise ValueError("observation recovery code is not exact")
        return PrepareObservationV1(
            ClientDataRole(cast(str, data["role"])),
            cast(str, data["prepare_bundle_name"]),
            cast(str, data["published_bundle_name"]),
            (identity[0], identity[1]),
            cast(str, data["request_id"]),
            cast(str, data["first_seen_boot_id"]),
            cast(int, data["first_seen_monotonic_ns"]),
            cast(int, data["deadline_monotonic_ns"]),
            PrepareObservationStatus(cast(str, data["status"])),
            cast(int, data["revision"]),
            None if data["recovery_code"] is None else PrepareRecoveryCode(data["recovery_code"]),
        )

    def _update_observation(
        self,
        path: Path,
        current: PrepareObservationV1,
        status: PrepareObservationStatus,
        recovery_code: PrepareRecoveryCode | None = None,
    ) -> PrepareObservationV1:
        observed = self._read_observation(path)
        if observed != current:
            raise _error("preparation observation CAS failed", "OBSERVATION_UPDATE_FAILED")
        allowed = {
            PrepareObservationStatus.ACTIVE: {
                PrepareObservationStatus.HELD_EXPIRED,
                PrepareObservationStatus.PUBLISHED_HANDOFF,
                PrepareObservationStatus.CLIENT_ABORTED,
                PrepareObservationStatus.QUARANTINED,
                PrepareObservationStatus.RECOVERY_REQUIRED,
            },
            PrepareObservationStatus.HELD_EXPIRED: {
                PrepareObservationStatus.PUBLISHED_HANDOFF,
                PrepareObservationStatus.CLIENT_ABORTED,
                PrepareObservationStatus.QUARANTINED,
                PrepareObservationStatus.RECOVERY_REQUIRED,
            },
        }
        if status not in allowed.get(current.status, set()):
            raise _error("preparation observation transition is illegal", "OBSERVATION_UPDATE_FAILED")
        updated = PrepareObservationV1(
            current.role,
            current.prepare_bundle_name,
            current.published_bundle_name,
            current.bundle_identity,
            current.request_id,
            current.first_seen_boot_id,
            current.first_seen_monotonic_ns,
            current.deadline_monotonic_ns,
            status,
            current.revision + 1,
            recovery_code,
        )
        self._write_observation(path, updated)
        return updated

    def _terminal_observation(
        self,
        path: Path,
        current: PrepareObservationV1,
        status: PrepareObservationStatus,
        clock: StagingClock,
    ) -> None:
        updated = self._update_observation(path, current, status)
        self._delete_terminal_observation(path, updated, clock)

    def _delete_terminal_observation(
        self,
        path: Path,
        current: PrepareObservationV1,
        clock: StagingClock,
    ) -> None:
        marker = self._root / current.role.value / "quarantine" / f"recovery-{path.name}"
        if marker.exists():
            raise _error("preparation observation recovery remains unresolved", "OBSERVATION_DELETE_FAILED")
        pending = _evidence(
            current.role,
            current.prepare_bundle_name,
            current.request_id,
            "OBSERVATION_DELETE_FAILED",
            clock,
            StagingRecoveryStatus.RECOVERY_REQUIRED,
        )
        self._atomic_json(marker, _evidence_json(pending))
        try:
            path.unlink()
        except OSError as error:
            raise _error("preparation observation deletion failed", "OBSERVATION_DELETE_FAILED") from error
        try:
            _fsync(path.parent)
        except OSError as error:
            failed = _evidence(
                current.role,
                current.prepare_bundle_name,
                current.request_id,
                "PARENT_FSYNC_FAILED",
                clock,
                StagingRecoveryStatus.RECOVERY_REQUIRED,
            )
            with suppress(OSError):
                self._atomic_json(marker, _evidence_json(failed))
            raise _error("preparation observation parent sync failed", "PARENT_FSYNC_FAILED") from error
        marker.unlink()
        _fsync(marker.parent)

    def _quarantine_path(
        self,
        role: ClientDataRole,
        path: Path,
        request_id: str | None,
        reason: str,
        clock: StagingClock,
        *,
        payload_identity: PayloadIdentityV1 | None = None,
        source_sha256: str | None = None,
        prior_state: str = "UNKNOWN",
    ) -> StagingRecoveryEvidenceV1:
        quarantine = self._root / role.value / "quarantine"
        name = f"{path.name}.{secrets.token_hex(8)}.quarantine"
        source_fd = -1
        quarantine_fd = -1
        try:
            source_fd = _open_verified_directory(path.parent)
            quarantine_fd = _open_verified_directory(quarantine)
            before = self._filesystem.stat_at(source_fd, path.name)
            self._filesystem.rename_noreplace(source_fd, path.name, quarantine_fd, name)
            after = self._filesystem.stat_at(quarantine_fd, name)
            if (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) != (
                after.st_dev,
                after.st_ino,
                stat.S_IFMT(after.st_mode),
            ):
                raise SecureFilesystemError("quarantined entry identity changed")
            self._filesystem.fsync_directory(source_fd)
            self._filesystem.fsync_directory(quarantine_fd)
            evidence = _evidence(
                role,
                name,
                request_id,
                reason,
                clock,
                StagingRecoveryStatus.COMPLETE,
                payload_identity=payload_identity,
                source_sha256=source_sha256,
                prior_state=prior_state,
            )
            self._atomic_json(quarantine / f"{name}.evidence.json", _evidence_json(evidence))
            return evidence
        except (OSError, RuntimeError):
            return self._recovery_marker(role, path.name, request_id, "QUARANTINE_FAILED", clock)
        finally:
            if quarantine_fd >= 0:
                os.close(quarantine_fd)
            if source_fd >= 0:
                os.close(source_fd)

    def _recovery_marker(
        self,
        role: ClientDataRole,
        bundle_name: str,
        request_id: str | None,
        reason: str,
        clock: StagingClock,
    ) -> StagingRecoveryEvidenceV1:
        evidence = _evidence(role, bundle_name, request_id, reason, clock, StagingRecoveryStatus.RECOVERY_REQUIRED)
        recovery = self._root / role.value / "quarantine"
        self._atomic_json(recovery / f"recovery-{uuid4()}.json", _evidence_json(evidence))
        return evidence

    def _atomic_json(self, path: Path, value: object) -> None:
        contents = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        parent_fd = _open_verified_directory(path.parent)
        temporary = ".tmp-" + secrets.token_hex(16)
        descriptor = -1
        try:
            descriptor = self._filesystem.create_regular_at(parent_fd, temporary)
            view = memoryview(contents)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short evidence write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._filesystem.replace_file(parent_fd, temporary, path.name)
            self._filesystem.fsync_directory(parent_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_fd)


def _observation_json(value: PrepareObservationV1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "role": value.role.value,
        "prepare_bundle_name": value.prepare_bundle_name,
        "published_bundle_name": value.published_bundle_name,
        "bundle_identity": list(value.bundle_identity),
        "request_id": value.request_id,
        "first_seen_boot_id": value.first_seen_boot_id,
        "first_seen_monotonic_ns": value.first_seen_monotonic_ns,
        "deadline_monotonic_ns": value.deadline_monotonic_ns,
        "status": value.status.value,
        "revision": value.revision,
        "recovery_code": None if value.recovery_code is None else value.recovery_code.value,
    }


def _evidence(
    role: ClientDataRole,
    bundle_name: str,
    request_id: str | None,
    reason: str,
    clock: StagingClock,
    status: StagingRecoveryStatus,
    *,
    payload_identity: PayloadIdentityV1 | None = None,
    source_sha256: str | None = None,
    prior_state: str = "UNKNOWN",
) -> StagingRecoveryEvidenceV1:
    return StagingRecoveryEvidenceV1(
        str(uuid4()),
        request_id,
        role,
        bundle_name,
        payload_identity,
        source_sha256,
        prior_state,
        StagingRecoveryReasonCode(reason),
        clock.boot_id(),
        clock.monotonic_ns(),
        StagingRecoveryAction.QUARANTINED
        if status is StagingRecoveryStatus.COMPLETE
        else StagingRecoveryAction.PRESERVED,
        status,
    )


def _evidence_json(value: StagingRecoveryEvidenceV1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": value.event_id,
        "request_id": value.request_id,
        "role": value.role.value,
        "bundle_name": value.bundle_name,
        "payload_identity": None if value.payload_identity is None else value.payload_identity.to_json(),
        "source_sha256": value.source_sha256,
        "prior_state": value.prior_state,
        "reason_code": value.reason_code.value,
        "observed_boot_id": value.observed_boot_id,
        "observed_monotonic_ns": value.observed_monotonic_ns,
        "action": value.action.value,
        "status": value.status.value,
    }


def _strict_json(contents: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(
        contents.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if type(value) is not dict:
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _read_bundle_lease(bundle: Path, role: ClientDataRole, *, phase: str = "stage") -> StagingLeaseV1:
    parsed_role, request_id = parse_bundle_name(bundle.name, phase=phase)
    if parsed_role is not role:
        raise ValueError("staging bundle role does not match its directory")
    directory = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if set(os.listdir(directory)) != {"payload", "lease.json"}:
            raise ValueError("staging bundle children are not exact")
        lease_fd = os.open("lease.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            lease_status = os.fstat(lease_fd)
            if (
                not stat.S_ISREG(lease_status.st_mode)
                or lease_status.st_nlink != 1
                or lease_status.st_size > MAX_LEASE_BYTES
            ):
                raise ValueError("staging lease facts are unsafe")
            lease_contents = _read_at_most(lease_fd, MAX_LEASE_BYTES)
        finally:
            os.close(lease_fd)
        lease = StagingLeaseV1.from_json(_strict_json(lease_contents))
        if lease.request_id != request_id or lease.role is not role:
            raise ValueError("staging lease authority does not match its bundle")
        payload_fd = os.open("payload", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            before = os.fstat(payload_fd)
            payload = _read_at_most(payload_fd, lease.source_size_bytes)
            after = os.fstat(payload_fd)
        finally:
            os.close(payload_fd)
        identity = PayloadIdentityV1(before.st_dev, before.st_ino, before.st_size, hashlib.sha256(payload).hexdigest())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or identity != lease.payload_identity
        ):
            raise ValueError("staging payload identity differs from its lease")
        return lease
    finally:
        os.close(directory)


def _read_at_most(descriptor: int, limit: int) -> bytes:
    result = bytearray()
    while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - len(result))):
        result.extend(chunk)
        if len(result) > limit:
            raise ValueError("bounded staging read exceeded its authority")
    return bytes(result)


def _open_verified_directory(path: Path) -> int:
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise SecureFilesystemError(f"managed staging directory cannot be opened safely: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SecureFilesystemError(f"managed staging directory identity changed: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_directory(
    role_fd: int,
    path: Path,
    filesystem: StagingFilesystemOperations,
) -> int:
    value = os.stat(path.name, dir_fd=role_fd, follow_symlinks=False)
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or stat.S_IMODE(value.st_mode) != 0o700
        or not _private_acl_is_exact(path)
    ):
        raise SecureFilesystemError(f"private staging directory facts are unsafe: {path.name}")
    descriptor = filesystem.open_directory_at(role_fd, path.name)
    try:
        opened = os.fstat(descriptor)
        after = os.stat(path.name, dir_fd=role_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino)
            or (after.st_dev, after.st_ino) != (value.st_dev, value.st_ino)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or not _private_acl_is_exact(path)
        ):
            raise SecureFilesystemError(f"private staging directory identity changed: {path.name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_acl_is_exact(path: Path) -> bool:
    for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, attribute, follow_symlinks=False)
        except OSError as error:
            absent = {errno.ENODATA}
            if hasattr(errno, "ENOATTR"):
                absent.add(errno.ENOATTR)
            if error.errno in absent:
                continue
            raise SecureFilesystemError(f"private staging ACL cannot be read: {path.name}") from error
        return False
    return True


def _detach_and_remove_bundle(
    parent: Path,
    name: str,
    filesystem: StagingFilesystemOperations,
) -> None:
    parent_fd = _open_verified_directory(parent)
    descriptor = -1
    detached = filesystem.hidden_name("cleanup")
    try:
        descriptor = filesystem.open_directory_at(parent_fd, name)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        filesystem.rename_noreplace(parent_fd, name, parent_fd, detached)
        filesystem.fsync_directory(parent_fd)
        if not filesystem.entry_matches_descriptor(parent_fd, detached, descriptor, directory=True):
            raise SecureFilesystemError(f"detached staging cleanup identity changed: {name}")
        os.close(descriptor)
        descriptor = -1
        filesystem.remove_tree_at(parent_fd, detached)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _error(message: str, code: str) -> DrawingMachineError:
    return DrawingMachineError(ErrorPayload(code, ErrorCategory.SERVICE, message, False, {}))


__all__ = ["ReconciliationResultV1", "StagingReconciler"]
