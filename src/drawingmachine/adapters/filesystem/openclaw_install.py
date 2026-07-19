from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID

from drawingmachine.adapters.filesystem._secure_fs import (
    fsync_directory,
    fsync_file,
    identity,
    open_directory_at,
    read_stable_regular_bounded,
    rename_noreplace,
    stable_file_identity,
    stat_at,
    write_all,
)
from drawingmachine.config.openclaw_deployment import OpenClawDeploymentPolicyV1, ServiceWriterSnapshotV1
from drawingmachine.domain.openclaw import (
    OpenClawCheckFileV1,
    OpenClawCheckResultV1,
    OpenClawCheckStatus,
    OpenClawDeploymentError,
    OpenClawInitialState,
    OpenClawInstallFileStatus,
    OpenClawInstallFileV1,
    OpenClawInstallResultV1,
    OpenClawRegistryCheckV1,
    OpenClawRegistryStatus,
)
from drawingmachine.domain.openclaw.manifest import (
    PackagedOpenClawBundleV1,
    PackagedOpenClawResourceV1,
    load_packaged_openclaw_bundle,
)

_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_JSON_NODES = 4096
_MAX_JSON_DEPTH = 12
_MAX_JSON_STRING_BYTES = 65_536
_TRANSACTION = re.compile(r"\.drawingmachine-openclaw-([0-9a-f-]{36})-(tmp|bak)-([0-9a-f]{16})\Z")
_AncestorAuthority = Callable[[Path, os.stat_result], bool]


@dataclass(frozen=True, slots=True)
class _Initial:
    resource: PackagedOpenClawResourceV1
    state: OpenClawInitialState
    sha256: str | None
    identity: tuple[int, int, int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class _FrozenFile:
    identity: tuple[int, int, int, int, int, int]
    sha256: str


@dataclass(frozen=True, slots=True)
class _RegistrySnapshot:
    identity: tuple[int, int, int, int, int, int]
    sha256: str


class _MutationStage(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    TEMP_CREATING = "TEMP_CREATING"
    TEMP_READY = "TEMP_READY"
    BACKUP_MOVED = "BACKUP_MOVED"
    PUBLISHED = "PUBLISHED"
    ROLLED_BACK = "ROLLED_BACK"


class _InstallPhase(StrEnum):
    ROLLBACK_CAPABLE = "ROLLBACK_CAPABLE"
    COMMIT_CLEANUP = "COMMIT_CLEANUP"


@dataclass(slots=True)
class _Mutation:
    initial: _Initial
    parent_fd: int
    target_name: str
    temporary_name: str | None = None
    temporary: _FrozenFile | None = None
    backup_name: str | None = None
    backup: _FrozenFile | None = None
    published: _FrozenFile | None = None
    stage: _MutationStage = _MutationStage.NOT_ATTEMPTED


class OpenClawFilesystemAdapter:
    """No-follow, service-writer-only deployment of the four closed managed resources."""

    def __init__(
        self,
        policy: OpenClawDeploymentPolicyV1,
        *,
        bundle: PackagedOpenClawBundleV1 | None = None,
        ancestor_authority: _AncestorAuthority | None = None,
    ) -> None:
        if type(policy) is not OpenClawDeploymentPolicyV1:
            raise TypeError("OpenClaw deployment policy must be exact")
        packaged = load_packaged_openclaw_bundle() if bundle is None else bundle
        if type(packaged) is not PackagedOpenClawBundleV1:
            raise TypeError("OpenClaw packaged bundle must be exact")
        if packaged.manifest_sha256 != policy.manifest_sha256:
            raise OpenClawDeploymentError("POLICY_DRIFT", "packaged manifest digest differs from deployment policy")
        if packaged.registry_policy_sha256 != policy.registry_policy_sha256:
            raise OpenClawDeploymentError("POLICY_DRIFT", "registry policy digest differs from deployment policy")
        self._policy = policy
        self._bundle = packaged
        self._ancestor_authority = ancestor_authority or self._default_ancestor_authority
        self._lock = threading.Lock()
        self._completed: dict[
            str,
            tuple[tuple[object, ...], OpenClawInstallResultV1, tuple[_Initial, ...]],
        ] = {}

    def check(
        self,
        explicit_root: Path,
        service_writer_snapshot: ServiceWriterSnapshotV1,
    ) -> OpenClawCheckResultV1:
        self._validate_call(explicit_root, service_writer_snapshot)
        try:
            self._validate_ancestor_chains()
            registry = self._check_registry()
            files = self._check_targets()
        except (OpenClawDeploymentError, OSError, RuntimeError):
            registry = OpenClawRegistryCheckV1(
                self._policy.registry_policy_sha256,
                OpenClawRegistryStatus.UNSAFE,
            )
            files = tuple(
                OpenClawCheckFileV1(
                    cast(str, item.destination),
                    item.sha256,
                    None,
                    OpenClawCheckStatus.UNSAFE,
                )
                for item in self._bundle.managed_resources
            )
        unsafe = registry.status is OpenClawRegistryStatus.UNSAFE or any(
            item.status is OpenClawCheckStatus.UNSAFE for item in files
        )
        in_sync = registry.status is OpenClawRegistryStatus.MATCHES and all(
            item.status is OpenClawCheckStatus.MATCHES for item in files
        )
        return OpenClawCheckResultV1(in_sync, unsafe, registry, files)

    def install(
        self,
        explicit_root: Path,
        request_id: str,
        service_writer_snapshot: ServiceWriterSnapshotV1,
    ) -> OpenClawInstallResultV1:
        self._validate_call(explicit_root, service_writer_snapshot)
        canonical_request = _request_id(request_id)
        binding = (
            service_writer_snapshot.uid,
            service_writer_snapshot.gid,
            str(explicit_root),
            self._policy.source_sha256,
            self._policy.manifest_sha256,
            self._policy.registry_policy_sha256,
        )
        with self._lock:
            cached = self._completed.get(canonical_request)
            if cached is not None:
                if cached[0] != binding:
                    raise OpenClawDeploymentError(
                        "REQUEST_REPLAY_MISMATCH", "request ID was reused with changed authority"
                    )
                self._validate_cached_success(cached[2])
                return replace(cached[1], deduplicated=True)
            result, final_facts = self._install_locked(canonical_request)
            self._completed[canonical_request] = (binding, result, final_facts)
            return result

    def _install_locked(self, request_id: str) -> tuple[OpenClawInstallResultV1, tuple[_Initial, ...]]:
        parent_fds: dict[str, int] = {}
        try:
            self._validate_ancestor_chains()
            registry, registry_snapshot = self._inspect_registry()
            if registry.status is not OpenClawRegistryStatus.MATCHES:
                raise OpenClawDeploymentError("REGISTRY_DRIFT", "OpenClaw registry is absent, unsafe, or differs")
            assert registry_snapshot is not None
            checked = self._check_targets()
            if any(item.status is OpenClawCheckStatus.UNSAFE for item in checked):
                raise OpenClawDeploymentError("UNSAFE_TARGET", "OpenClaw managed target is unsafe")
            initial = self._initial_targets()
            parent_fds = self._open_target_parents()
            self._reject_transaction_residue(parent_fds)
            self._assert_registry_unchanged(registry_snapshot)
        except OpenClawDeploymentError:
            for descriptor in parent_fds.values():
                os.close(descriptor)
            raise
        except (OSError, RuntimeError):
            for descriptor in parent_fds.values():
                os.close(descriptor)
            raise OpenClawDeploymentError(
                "UNSAFE_FILESYSTEM",
                "OpenClaw deployment filesystem could not be validated safely",
            ) from None
        if all(item.sha256 == item.resource.sha256 for item in initial):
            try:
                self._verify_target_facts(parent_fds, initial, require_packaged=True)
                self._assert_registry_unchanged(registry_snapshot)
                rows = tuple(self._row(item, OpenClawInstallFileStatus.UNCHANGED) for item in initial)
                return OpenClawInstallResultV1(True, False, False, True, False, None, True, rows), initial
            finally:
                for descriptor in parent_fds.values():
                    os.close(descriptor)
        mutations = [
            _Mutation(
                item,
                parent_fds[_destination_parts(cast(str, item.resource.destination))[0]],
                _destination_parts(cast(str, item.resource.destination))[1],
            )
            for item in initial
        ]
        registry_compliant = True
        phase = _InstallPhase.ROLLBACK_CAPABLE
        try:
            self._assert_registry_unchanged(registry_snapshot)
            for mutation in mutations:
                if mutation.initial.sha256 == mutation.initial.resource.sha256:
                    continue
                self._apply_one(mutation, request_id)
            self._assert_registry_unchanged(registry_snapshot)
            final_facts = self._final_target_facts(mutations)
            self._verify_target_facts(parent_fds, final_facts, require_packaged=True)
            self._assert_registry_unchanged(registry_snapshot)
            phase = _InstallPhase.COMMIT_CLEANUP
            cleanup_ok = self._cleanup(mutations)
            if not cleanup_ok:
                raise self._partial("transaction artifact cleanup could not be verified")
            self._assert_registry_unchanged(registry_snapshot)
            self._verify_target_facts(parent_fds, final_facts, require_packaged=True)
            self._assert_registry_unchanged(registry_snapshot)
            rows = tuple(
                self._mutation_row(
                    item,
                    OpenClawInstallFileStatus.UNCHANGED
                    if item.initial.sha256 == item.initial.resource.sha256
                    else (
                        OpenClawInstallFileStatus.INSTALLED
                        if item.initial.state is OpenClawInitialState.ABSENT
                        else OpenClawInstallFileStatus.REPLACED
                    ),
                )
                for item in mutations
            )
            return OpenClawInstallResultV1(True, True, False, True, False, None, True, rows), final_facts
        except BaseException as error:
            if isinstance(error, OpenClawDeploymentError) and error.recovery_required:
                raise
            if phase is _InstallPhase.COMMIT_CLEANUP:
                raise self._partial("irreversible commit cleanup or final validation failed") from None
            if isinstance(error, OpenClawDeploymentError) and error.code == "REGISTRY_RACE":
                registry_compliant = False
            mutation_started = any(item.stage is not _MutationStage.NOT_ATTEMPTED for item in mutations)
            if not mutation_started:
                raise OpenClawDeploymentError("INSTALL_FAILED", "installation failed before mutation") from None
            rollback_ok = self._rollback(mutations)
            if not rollback_ok:
                raise self._partial("installation rollback could not be verified") from None
            cleanup_ok = self._cleanup(mutations)
            if not cleanup_ok:
                raise self._partial("installation rollback or cleanup could not be verified") from None
            rollback_facts = tuple(item.initial for item in mutations)
            self._verify_target_facts(parent_fds, rollback_facts, require_packaged=False)
            rows = tuple(self._failed_mutation_row(item) for item in mutations)
            return (
                OpenClawInstallResultV1(
                    False,
                    mutation_started,
                    False,
                    registry_compliant,
                    mutation_started,
                    True if mutation_started else None,
                    True,
                    rows,
                ),
                rollback_facts,
            )
        finally:
            for descriptor in parent_fds.values():
                os.close(descriptor)

    def _validate_cached_success(self, expected_targets: tuple[_Initial, ...]) -> None:
        self._validate_ancestor_chains()
        registry, snapshot = self._inspect_registry()
        if registry.status is not OpenClawRegistryStatus.MATCHES or snapshot is None:
            raise OpenClawDeploymentError("REGISTRY_DRIFT", "OpenClaw registry differs from cached authority")
        parent_fds = self._open_target_parents()
        try:
            self._reject_transaction_residue(parent_fds)
            self._assert_registry_unchanged(snapshot)
            self._verify_target_facts(parent_fds, expected_targets, require_packaged=False)
            self._assert_registry_unchanged(snapshot)
        finally:
            for descriptor in parent_fds.values():
                os.close(descriptor)

    def _partial(self, message: str) -> OpenClawDeploymentError:
        return OpenClawDeploymentError("PARTIAL_INSTALL", f"recovery required: {message}", recovery_required=True)

    def _apply_one(self, mutation: _Mutation, request_id: str) -> None:
        self._verify_initial_identity(mutation)
        self._verify_parent_path(mutation)
        nonce = os.urandom(8).hex()
        temporary_name = f".drawingmachine-openclaw-{request_id}-tmp-{nonce}"
        backup_name = f".drawingmachine-openclaw-{request_id}-bak-{nonce}"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, self._policy.target_mode, dir_fd=mutation.parent_fd)
        mutation.temporary_name = temporary_name
        mutation.stage = _MutationStage.TEMP_CREATING
        try:
            status = os.fstat(descriptor)
            mutation.temporary = _FrozenFile(stable_file_identity(status), hashlib.sha256().hexdigest())
            path_status = stat_at(mutation.parent_fd, temporary_name)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise OpenClawDeploymentError(
                    "UNSAFE_TEMP", "created transaction file is not a single-link regular file"
                )
            if (
                status.st_uid != self._policy.managed_owner.uid
                or status.st_gid != self._policy.automation_read_group.gid
            ):
                raise OpenClawDeploymentError("UNSAFE_TEMP_OWNER", "created transaction file has unsafe ownership")
            if stable_file_identity(path_status) != stable_file_identity(status):
                raise OpenClawDeploymentError("UNSAFE_TEMP_IDENTITY", "created transaction file identity is unsafe")
            try:
                os.fchmod(descriptor, self._policy.target_mode)
                write_all(descriptor, mutation.initial.resource.contents)
                fsync_file(descriptor)
                after = os.fstat(descriptor)
                if stat.S_IMODE(after.st_mode) != self._policy.target_mode:
                    raise OpenClawDeploymentError("UNSAFE_TEMP_MODE", "transaction file mode drifted")
            finally:
                mutation.temporary = self._freeze_descriptor(descriptor)
        finally:
            os.close(descriptor)
        frozen = self._freeze_named(mutation.parent_fd, temporary_name, mutation.initial.resource.contents)
        if frozen != mutation.temporary or frozen.sha256 != mutation.initial.resource.sha256:
            raise OpenClawDeploymentError("SOURCE_DRIFT", "transaction bytes do not match packaged authority")
        mutation.stage = _MutationStage.TEMP_READY
        if mutation.initial.state is OpenClawInitialState.PRESENT:
            self._verify_initial_identity(mutation)
            rename_noreplace(mutation.parent_fd, mutation.target_name, mutation.parent_fd, backup_name)
            mutation.backup_name = backup_name
            backup = self._freeze_named(mutation.parent_fd, backup_name)
            if (
                mutation.initial.identity is None
                or not self._same_renamed_object(mutation.initial.identity, backup.identity)
                or backup.sha256 != mutation.initial.sha256
            ):
                raise self._partial("backup identity changed across rename")
            mutation.backup = backup
            mutation.stage = _MutationStage.BACKUP_MOVED
            fsync_directory(mutation.parent_fd)
        self._assert_frozen_named(mutation.parent_fd, temporary_name, mutation.temporary)
        rename_noreplace(mutation.parent_fd, temporary_name, mutation.parent_fd, mutation.target_name)
        mutation.temporary_name = None
        published = self._freeze_named(mutation.parent_fd, mutation.target_name)
        if (
            mutation.temporary is None
            or not self._same_renamed_object(mutation.temporary.identity, published.identity)
            or published.sha256 != mutation.temporary.sha256
        ):
            raise self._partial("published target identity changed across rename")
        mutation.published = published
        mutation.stage = _MutationStage.PUBLISHED
        fsync_directory(mutation.parent_fd)
        self._verify_parent_path(mutation)
        self._verify_final(mutation)

    def _freeze_descriptor(self, descriptor: int) -> _FrozenFile:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        offset = 0
        while chunk := os.pread(descriptor, 65_536, offset):
            offset += len(chunk)
            if offset > 65_536:
                raise OpenClawDeploymentError("UNSAFE_TEMP", "transaction file exceeds managed bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if stable_file_identity(before) != stable_file_identity(after):
            raise OpenClawDeploymentError("UNSAFE_TEMP", "transaction file changed while freezing identity")
        return _FrozenFile(stable_file_identity(after), digest.hexdigest())

    def _freeze_named(self, parent_fd: int, name: str, expected: bytes | None = None) -> _FrozenFile:
        maximum = len(expected) if expected is not None else 65_536
        copied, _ = read_stable_regular_bounded(parent_fd, name, max_bytes=maximum)
        return _FrozenFile(copied.identity, copied.sha256)

    def _assert_frozen_named(self, parent_fd: int, name: str, expected: _FrozenFile | None) -> None:
        if expected is None or self._freeze_named(parent_fd, name) != expected:
            raise self._partial("transaction artifact identity or digest changed")

    @staticmethod
    def _same_renamed_object(
        before: tuple[int, int, int, int, int, int],
        after: tuple[int, int, int, int, int, int],
    ) -> bool:
        return before[:4] == after[:4]

    def _verify_initial_identity(self, mutation: _Mutation) -> None:
        try:
            status = stat_at(mutation.parent_fd, mutation.target_name)
        except FileNotFoundError:
            if mutation.initial.state is OpenClawInitialState.ABSENT:
                return
            raise OpenClawDeploymentError("TARGET_RACE", "initial managed target disappeared") from None
        if (
            mutation.initial.state is OpenClawInitialState.ABSENT
            or stable_file_identity(status) != mutation.initial.identity
        ):
            raise OpenClawDeploymentError("TARGET_RACE", "initial managed target identity changed")

    def _verify_parent_path(self, mutation: _Mutation) -> None:
        agent_id, _ = _destination_parts(cast(str, mutation.initial.resource.destination))
        path_status = (self._policy.managed_root / "agents" / agent_id).lstat()
        descriptor_status = os.fstat(mutation.parent_fd)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise OpenClawDeploymentError("PARENT_RACE", "managed target parent changed")

    def _rollback(self, mutations: list[_Mutation]) -> bool:
        try:
            for mutation in reversed(mutations):
                if mutation.published is not None:
                    self._assert_frozen_named(mutation.parent_fd, mutation.target_name, mutation.published)
                    os.unlink(mutation.target_name, dir_fd=mutation.parent_fd)
                    fsync_directory(mutation.parent_fd)
                    mutation.published = None
                if mutation.initial.state is OpenClawInitialState.PRESENT and mutation.backup_name is not None:
                    self._assert_frozen_named(mutation.parent_fd, mutation.backup_name, mutation.backup)
                    rename_noreplace(
                        mutation.parent_fd,
                        mutation.backup_name,
                        mutation.parent_fd,
                        mutation.target_name,
                    )
                    mutation.backup_name = None
                    fsync_directory(mutation.parent_fd)
                    restored = self._freeze_named(mutation.parent_fd, mutation.target_name)
                    if (
                        mutation.backup is None
                        or not self._same_renamed_object(mutation.backup.identity, restored.identity)
                        or restored.sha256 != mutation.backup.sha256
                    ):
                        raise self._partial("restored managed target identity does not match frozen backup")
                    mutation.initial = replace(mutation.initial, identity=restored.identity)
                    mutation.backup = None
                elif mutation.initial.state is OpenClawInitialState.ABSENT:
                    with suppress(FileNotFoundError):
                        raise (
                            self._partial("initially absent target still exists")
                            if stat_at(mutation.parent_fd, mutation.target_name)
                            else None
                        )
                if mutation.stage is not _MutationStage.NOT_ATTEMPTED:
                    mutation.stage = _MutationStage.ROLLED_BACK
            return True
        except BaseException:
            return False

    def _cleanup(self, mutations: list[_Mutation]) -> bool:
        try:
            for mutation in mutations:
                changed = False
                for attribute in ("temporary_name", "backup_name"):
                    name = cast(str | None, getattr(mutation, attribute))
                    if name is None:
                        continue
                    frozen_attribute = "temporary" if attribute == "temporary_name" else "backup"
                    self._assert_frozen_named(
                        mutation.parent_fd,
                        name,
                        cast(_FrozenFile | None, getattr(mutation, frozen_attribute)),
                    )
                    os.unlink(name, dir_fd=mutation.parent_fd)
                    setattr(mutation, attribute, None)
                    setattr(mutation, frozen_attribute, None)
                    changed = True
                if changed:
                    fsync_directory(mutation.parent_fd)
            return True
        except BaseException:
            return False

    def _verify_final(self, mutation: _Mutation) -> None:
        status = stat_at(mutation.parent_fd, mutation.target_name)
        if not self._safe_managed_file(status):
            raise OpenClawDeploymentError("FINAL_AUTHORITY_DRIFT", "managed target authority is unsafe")
        copied, _ = read_stable_regular_bounded(
            mutation.parent_fd,
            mutation.target_name,
            max_bytes=len(mutation.initial.resource.contents),
        )
        if copied.sha256 != mutation.initial.resource.sha256:
            raise OpenClawDeploymentError("FINAL_DIGEST_DRIFT", "managed target digest is not the packaged digest")
        if mutation.published is None or copied.identity != mutation.published.identity:
            raise self._partial("managed target identity changed after publication")

    def _final_target_facts(self, mutations: list[_Mutation]) -> tuple[_Initial, ...]:
        result: list[_Initial] = []
        for mutation in mutations:
            if mutation.initial.sha256 == mutation.initial.resource.sha256:
                result.append(mutation.initial)
                continue
            if mutation.published is None:
                raise self._partial("published target fact is unavailable before success")
            result.append(
                _Initial(
                    mutation.initial.resource,
                    OpenClawInitialState.PRESENT,
                    mutation.initial.resource.sha256,
                    mutation.published.identity,
                )
            )
        return tuple(result)

    def _verify_target_facts(
        self,
        parent_fds: Mapping[str, int],
        facts: tuple[_Initial, ...],
        *,
        require_packaged: bool,
    ) -> None:
        if tuple(item.resource for item in facts) != self._bundle.managed_resources:
            raise OpenClawDeploymentError("TARGET_ORDER_DRIFT", "managed target facts are not manifest ordered")
        verified_parents: set[str] = set()
        for fact in facts:
            destination = cast(str, fact.resource.destination)
            agent_id, name = _destination_parts(destination)
            parent_fd = parent_fds[agent_id]
            if agent_id not in verified_parents:
                self._verify_parent_descriptor(agent_id, parent_fd)
                verified_parents.add(agent_id)
            try:
                status = stat_at(parent_fd, name)
            except FileNotFoundError:
                if fact.state is OpenClawInitialState.ABSENT:
                    continue
                raise OpenClawDeploymentError("TARGET_RACE", "managed target disappeared before result") from None
            if fact.state is OpenClawInitialState.ABSENT:
                raise OpenClawDeploymentError("TARGET_RACE", "unexpected managed target appeared before result")
            if not self._safe_managed_file(status):
                raise OpenClawDeploymentError("TARGET_RACE", "managed target authority changed before result")
            copied, _ = read_stable_regular_bounded(parent_fd, name, max_bytes=65_536)
            if copied.identity != fact.identity or copied.sha256 != fact.sha256:
                raise OpenClawDeploymentError("TARGET_RACE", "managed target identity or digest changed before result")
            if require_packaged and copied.sha256 != fact.resource.sha256:
                raise OpenClawDeploymentError("TARGET_RACE", "managed target no longer matches packaged authority")

    def _verify_parent_descriptor(self, agent_id: str, descriptor: int) -> None:
        path_status = (self._policy.managed_root / "agents" / agent_id).lstat()
        descriptor_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or identity(path_status) != identity(descriptor_status)
        ):
            raise OpenClawDeploymentError("PARENT_RACE", "managed target parent changed before result")

    def _validate_call(self, explicit_root: Path, writer: ServiceWriterSnapshotV1) -> None:
        if (
            type(explicit_root) is not type(Path())
            or not explicit_root.is_absolute()
            or explicit_root != self._policy.registry_root
        ):
            raise OpenClawDeploymentError("ROOT_MISMATCH", "explicit OpenClaw root does not equal immutable policy")
        if type(writer) is not ServiceWriterSnapshotV1 or writer != ServiceWriterSnapshotV1(
            self._policy.managed_owner.uid,
            self._policy.managed_owner.gid,
        ):
            raise OpenClawDeploymentError("WRITER_MISMATCH", "writer snapshot is not the policy service principal")
        if os.geteuid() != writer.uid or os.getegid() != writer.gid:
            raise OpenClawDeploymentError(
                "PROCESS_WRITER_MISMATCH", "current process is not the trusted service writer"
            )

    def _validate_ancestor_chains(self) -> None:
        for root in (self._policy.registry_root, self._policy.managed_root):
            for ancestor in reversed(root.parent.parents):
                status = ancestor.lstat()
                if not self._ancestor_authority(ancestor, status):
                    raise OpenClawDeploymentError("UNSAFE_ANCESTOR", "deployment ancestor authority is unsafe")
            parent_status = root.parent.lstat()
            if not self._ancestor_authority(root.parent, parent_status):
                raise OpenClawDeploymentError("UNSAFE_ANCESTOR", "deployment parent authority is unsafe")
        self._validate_root_separation()

    def _validate_root_separation(self) -> None:
        registry_fd = self._open_root(self._policy.registry_root, managed=False)
        managed_fd: int | None = None
        try:
            managed_fd = self._open_root(self._policy.managed_root, managed=True)
            registry_identity = identity(os.fstat(registry_fd))
            managed_identity = identity(os.fstat(managed_fd))
            if (
                registry_identity == managed_identity
                or self._descriptor_has_ancestor(registry_fd, managed_identity)
                or self._descriptor_has_ancestor(managed_fd, registry_identity)
            ):
                raise OpenClawDeploymentError(
                    "ROOT_ALIAS",
                    "registry and managed roots must be physically distinct non-nested directories",
                )
        finally:
            if managed_fd is not None:
                os.close(managed_fd)
            os.close(registry_fd)

    def _descriptor_has_ancestor(self, descriptor: int, forbidden: tuple[int, int]) -> bool:
        current = os.dup(descriptor)
        try:
            while True:
                current_identity = identity(os.fstat(current))
                parent = os.open(
                    "..",
                    os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )
                parent_identity = identity(os.fstat(parent))
                os.close(current)
                current = parent
                if parent_identity == forbidden:
                    return True
                if parent_identity == current_identity:
                    return False
        finally:
            os.close(current)

    def _default_ancestor_authority(self, path: Path, status: os.stat_result) -> bool:
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            return False
        if status.st_uid not in {0, self._policy.managed_owner.uid, self._policy.registry_owner.uid}:
            return False
        mode = stat.S_IMODE(status.st_mode)
        if mode & stat.S_IWOTH:
            return False
        principals = (self._policy.automation_principal, self._policy.operator_principal)
        return not any(
            (status.st_uid == item.uid and mode & stat.S_IWUSR) or (status.st_gid == item.gid and mode & stat.S_IWGRP)
            for item in principals
        )

    def _check_registry(self) -> OpenClawRegistryCheckV1:
        return self._inspect_registry()[0]

    def _inspect_registry(self) -> tuple[OpenClawRegistryCheckV1, _RegistrySnapshot | None]:
        root_fd = self._open_root(self._policy.registry_root, managed=False)
        try:
            try:
                status = stat_at(root_fd, "openclaw.json")
            except FileNotFoundError:
                return (
                    OpenClawRegistryCheckV1(
                        self._policy.registry_policy_sha256,
                        OpenClawRegistryStatus.MISSING,
                    ),
                    None,
                )
            if not self._safe_registry_file(status):
                return (
                    OpenClawRegistryCheckV1(
                        self._policy.registry_policy_sha256,
                        OpenClawRegistryStatus.UNSAFE,
                    ),
                    None,
                )
            try:
                copied, contents = read_stable_regular_bounded(
                    root_fd,
                    "openclaw.json",
                    max_bytes=_MAX_REGISTRY_BYTES,
                    nonblocking=True,
                )
                registry = _decode_registry(contents)
                matches = self._registry_matches(registry)
            except (ValueError, OSError):
                return (
                    OpenClawRegistryCheckV1(
                        self._policy.registry_policy_sha256,
                        OpenClawRegistryStatus.DIFFERS,
                    ),
                    None,
                )
            return (
                OpenClawRegistryCheckV1(
                    self._policy.registry_policy_sha256,
                    OpenClawRegistryStatus.MATCHES if matches else OpenClawRegistryStatus.DIFFERS,
                ),
                _RegistrySnapshot(copied.identity, copied.sha256) if matches else None,
            )
        finally:
            os.close(root_fd)

    def _assert_registry_unchanged(self, expected: _RegistrySnapshot) -> None:
        try:
            checked, actual = self._inspect_registry()
        except (OpenClawDeploymentError, OSError, RuntimeError):
            raise OpenClawDeploymentError(
                "REGISTRY_RACE",
                "OpenClaw registry could not be revalidated during installation",
            ) from None
        if checked.status is not OpenClawRegistryStatus.MATCHES or actual != expected:
            raise OpenClawDeploymentError(
                "REGISTRY_RACE",
                "OpenClaw registry identity or digest changed during installation",
            )

    def _registry_matches(self, registry: Mapping[str, object]) -> bool:
        agents_table = registry.get("agents")
        if type(agents_table) is not dict:
            return False
        values = cast(dict[str, object], agents_table).get("list")
        if type(values) is not list:
            return False
        declared = {item.agent_id: item for item in self._bundle.registry_policy.agents}
        found: dict[str, dict[str, object]] = {}
        for raw in cast(list[object], values):
            if type(raw) is not dict:
                continue
            agent = cast(dict[str, object], raw)
            agent_id = agent.get("id")
            if type(agent_id) is not str or agent_id not in declared:
                continue
            if agent_id in found:
                return False
            found[agent_id] = agent
        if set(found) != set(declared):
            return False
        for agent_id, expected in declared.items():
            actual = found[agent_id]
            if actual.get("workspace") != str(self._policy.managed_root / expected.workspace_relative_path):
                return False
            tools = actual.get("tools")
            allowed = cast(dict[str, object], tools).get("alsoAllow") if type(tools) is dict else None
            if type(allowed) is not list or any(type(item) is not str for item in allowed):
                return False
            if not set(expected.required_tools) <= set(cast(list[str], allowed)):
                return False
        return True

    def _check_targets(self) -> tuple[OpenClawCheckFileV1, ...]:
        parent_fds = self._open_target_parents()
        try:
            rows: list[OpenClawCheckFileV1] = []
            for resource in self._bundle.managed_resources:
                destination = cast(str, resource.destination)
                agent_id, name = _destination_parts(destination)
                parent_fd = parent_fds[agent_id]
                try:
                    status = stat_at(parent_fd, name)
                except FileNotFoundError:
                    rows.append(OpenClawCheckFileV1(destination, resource.sha256, None, OpenClawCheckStatus.MISSING))
                    continue
                if not self._safe_managed_file(status):
                    rows.append(OpenClawCheckFileV1(destination, resource.sha256, None, OpenClawCheckStatus.UNSAFE))
                    continue
                copied, _ = read_stable_regular_bounded(parent_fd, name, max_bytes=65_536, nonblocking=True)
                rows.append(
                    OpenClawCheckFileV1(
                        destination,
                        resource.sha256,
                        copied.sha256,
                        OpenClawCheckStatus.MATCHES
                        if copied.sha256 == resource.sha256
                        else OpenClawCheckStatus.DIFFERS,
                    )
                )
            return tuple(rows)
        finally:
            for descriptor in parent_fds.values():
                os.close(descriptor)

    def _initial_targets(self) -> tuple[_Initial, ...]:
        parent_fds = self._open_target_parents()
        try:
            result: list[_Initial] = []
            for resource in self._bundle.managed_resources:
                agent_id, name = _destination_parts(cast(str, resource.destination))
                try:
                    status = stat_at(parent_fds[agent_id], name)
                except FileNotFoundError:
                    result.append(_Initial(resource, OpenClawInitialState.ABSENT, None, None))
                    continue
                if not self._safe_managed_file(status):
                    raise OpenClawDeploymentError("UNSAFE_TARGET", "managed target authority is unsafe")
                copied, _ = read_stable_regular_bounded(parent_fds[agent_id], name, max_bytes=65_536)
                result.append(
                    _Initial(resource, OpenClawInitialState.PRESENT, copied.sha256, stable_file_identity(status))
                )
            return tuple(result)
        finally:
            for descriptor in parent_fds.values():
                os.close(descriptor)

    def _open_target_parents(self) -> dict[str, int]:
        managed_fd = self._open_root(self._policy.managed_root, managed=True)
        agents_fd: int | None = None
        result: dict[str, int] = {}
        try:
            agents_fd = open_directory_at(managed_fd, "agents")
            self._validate_managed_directory(os.fstat(agents_fd))
            for agent in self._bundle.registry_policy.agents:
                descriptor = open_directory_at(agents_fd, agent.agent_id)
                try:
                    self._validate_managed_directory(os.fstat(descriptor))
                except BaseException:
                    os.close(descriptor)
                    raise
                result[agent.agent_id] = descriptor
            return result
        except BaseException:
            for descriptor in result.values():
                os.close(descriptor)
            raise
        finally:
            if agents_fd is not None:
                os.close(agents_fd)
            os.close(managed_fd)

    def _open_root(self, path: Path, *, managed: bool) -> int:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise OpenClawDeploymentError("UNSAFE_ROOT", "deployment root must be a no-follow directory")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        after = os.fstat(descriptor)
        if identity(status) != identity(after):
            os.close(descriptor)
            raise OpenClawDeploymentError("ROOT_RACE", "deployment root changed while opening")
        try:
            if managed:
                self._validate_managed_directory(after)
            elif not self._default_ancestor_authority(path, after):
                raise OpenClawDeploymentError("UNSAFE_REGISTRY_ROOT", "registry root authority is unsafe")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_managed_directory(self, status: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != self._policy.managed_owner.uid
            or status.st_gid != self._policy.automation_read_group.gid
            or stat.S_IMODE(status.st_mode) != self._policy.managed_dir_mode
        ):
            raise OpenClawDeploymentError("UNSAFE_MANAGED_DIRECTORY", "managed directory authority drifted")

    def _safe_registry_file(self, status: os.stat_result) -> bool:
        mode = stat.S_IMODE(status.st_mode)
        return (
            stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and status.st_uid in {0, self._policy.managed_owner.uid, self._policy.registry_owner.uid}
            and not mode & (stat.S_IWGRP | stat.S_IWOTH)
            and status.st_size <= _MAX_REGISTRY_BYTES
        )

    def _safe_managed_file(self, status: os.stat_result) -> bool:
        return (
            stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and status.st_uid == self._policy.managed_owner.uid
            and status.st_gid == self._policy.automation_read_group.gid
            and stat.S_IMODE(status.st_mode) == self._policy.target_mode
        )

    def _reject_transaction_residue(self, parent_fds: Mapping[str, int]) -> None:
        for parent_fd in parent_fds.values():
            for name in os.listdir(parent_fd):
                if _TRANSACTION.fullmatch(name) is not None:
                    raise OpenClawDeploymentError(
                        "PARTIAL_INSTALL",
                        "prior transaction artifacts require recovery",
                        recovery_required=True,
                    )

    def _row(self, initial: _Initial, status: OpenClawInstallFileStatus) -> OpenClawInstallFileV1:
        return OpenClawInstallFileV1(
            cast(str, initial.resource.destination),
            initial.state,
            initial.sha256,
            status,
            status is OpenClawInstallFileStatus.REPLACED,
            False,
            True,
            None,
        )

    def _mutation_row(
        self,
        mutation: _Mutation,
        status: OpenClawInstallFileStatus,
        *,
        error_code: str | None = None,
    ) -> OpenClawInstallFileV1:
        return OpenClawInstallFileV1(
            cast(str, mutation.initial.resource.destination),
            mutation.initial.state,
            mutation.initial.sha256,
            status,
            status in {OpenClawInstallFileStatus.REPLACED, OpenClawInstallFileStatus.RESTORED_PRESENT}
            or (
                status in {OpenClawInstallFileStatus.FAILED, OpenClawInstallFileStatus.CLEANUP_FAILED}
                and mutation.initial.state is OpenClawInitialState.PRESENT
                and mutation.stage is not _MutationStage.NOT_ATTEMPTED
            ),
            status in {OpenClawInstallFileStatus.RESTORED_PRESENT, OpenClawInstallFileStatus.RESTORED_ABSENT},
            mutation.temporary_name is None and mutation.backup_name is None,
            error_code,
        )

    def _failed_mutation_row(self, mutation: _Mutation) -> OpenClawInstallFileV1:
        if mutation.stage is _MutationStage.NOT_ATTEMPTED:
            return self._mutation_row(mutation, OpenClawInstallFileStatus.NOT_ATTEMPTED)
        if mutation.stage is _MutationStage.ROLLED_BACK:
            return self._mutation_row(
                mutation,
                OpenClawInstallFileStatus.RESTORED_PRESENT
                if mutation.initial.state is OpenClawInitialState.PRESENT
                else OpenClawInstallFileStatus.RESTORED_ABSENT,
                error_code="INSTALL_FAILED",
            )
        return self._mutation_row(mutation, OpenClawInstallFileStatus.FAILED, error_code="INSTALL_FAILED")


def _request_id(value: str) -> str:
    if type(value) is not str:
        raise OpenClawDeploymentError("INVALID_REQUEST_ID", "request ID must be canonical UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise OpenClawDeploymentError("INVALID_REQUEST_ID", "request ID must be canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise OpenClawDeploymentError("INVALID_REQUEST_ID", "request ID must be canonical UUIDv4")
    return value


def _destination_parts(destination: str) -> tuple[str, str]:
    parts = destination.split("/")
    if len(parts) != 3 or parts[0] != "agents" or parts[2] not in {"AGENTS.md", "TOOLS.md"}:
        raise OpenClawDeploymentError("MANIFEST_ESCAPE", "managed destination is outside the closed layout")
    return parts[1], parts[2]


def _decode_registry(contents: bytes) -> dict[str, object]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("registry is not UTF-8") from error

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("registry contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: _invalid_registry("registry contains a non-finite number"),
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("registry is not bounded JSON") from error
    budget = [_MAX_JSON_NODES]
    _validate_json(value, budget, 0)
    if type(value) is not dict:
        raise ValueError("registry root must be an object")
    return cast(dict[str, object], value)


def _validate_json(value: object, budget: list[int], depth: int) -> None:
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_JSON_DEPTH:
        raise ValueError("registry exceeds JSON bounds")
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("registry number must be finite")
        return
    if type(value) is str:
        if len(value.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
            raise ValueError("registry string exceeds bound")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _validate_json(item, budget, depth + 1)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str or len(key.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
                raise ValueError("registry key exceeds bound")
            _validate_json(item, budget, depth + 1)
        return
    raise ValueError("registry contains unsupported JSON value")


def _invalid_registry(message: str) -> NoReturn:
    raise ValueError(message)


__all__ = ["OpenClawFilesystemAdapter"]
