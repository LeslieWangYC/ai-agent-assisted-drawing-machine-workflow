from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from drawingmachine.json_types import JsonObject

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MANAGED_DESTINATIONS = (
    "agents/cnc-drawable-translator/AGENTS.md",
    "agents/cnc-drawable-translator/TOOLS.md",
    "agents/cnc-drawable-router/AGENTS.md",
    "agents/cnc-drawable-router/TOOLS.md",
)


class OpenClawCheckStatus(StrEnum):
    MATCHES = "MATCHES"
    MISSING = "MISSING"
    DIFFERS = "DIFFERS"
    UNSAFE = "UNSAFE"


class OpenClawRegistryStatus(StrEnum):
    MATCHES = "MATCHES"
    MISSING = "MISSING"
    DIFFERS = "DIFFERS"
    UNSAFE = "UNSAFE"


class OpenClawInitialState(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"


class OpenClawInstallFileStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    UNCHANGED = "UNCHANGED"
    INSTALLED = "INSTALLED"
    REPLACED = "REPLACED"
    RESTORED_PRESENT = "RESTORED_PRESENT"
    RESTORED_ABSENT = "RESTORED_ABSENT"
    FAILED = "FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class OpenClawCheckFileV1:
    destination: str
    expected_sha256: str
    observed_sha256: str | None
    status: OpenClawCheckStatus

    def __post_init__(self) -> None:
        _destination(self.destination)
        _digest(self.expected_sha256, "expected digest")
        _optional_digest(self.observed_sha256, "observed digest")
        if type(self.status) is not OpenClawCheckStatus:
            raise TypeError("check status must be exact")
        if self.status is OpenClawCheckStatus.MATCHES and self.observed_sha256 != self.expected_sha256:
            raise TypeError("MATCHES status requires the expected observed digest")
        if self.status is OpenClawCheckStatus.MISSING and self.observed_sha256 is not None:
            raise TypeError("MISSING status cannot have an observed digest")
        if self.status is OpenClawCheckStatus.DIFFERS and (
            self.observed_sha256 is None or self.observed_sha256 == self.expected_sha256
        ):
            raise TypeError("DIFFERS status requires a distinct observed digest")
        if self.status is OpenClawCheckStatus.UNSAFE and self.observed_sha256 is not None:
            raise TypeError("UNSAFE status cannot expose an observed digest")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "destination": self.destination,
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class OpenClawRegistryCheckV1:
    policy_sha256: str
    status: OpenClawRegistryStatus

    def __post_init__(self) -> None:
        _digest(self.policy_sha256, "registry policy digest")
        if type(self.status) is not OpenClawRegistryStatus:
            raise TypeError("registry status must be exact")

    def to_json(self) -> JsonObject:
        return {"schema_version": 1, "policy_sha256": self.policy_sha256, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class OpenClawCheckResultV1:
    in_sync: bool
    unsafe: bool
    registry: OpenClawRegistryCheckV1
    files: tuple[OpenClawCheckFileV1, ...]

    def __post_init__(self) -> None:
        if type(self.in_sync) is not bool or type(self.unsafe) is not bool:
            raise TypeError("check flags must be exact booleans")
        if type(self.registry) is not OpenClawRegistryCheckV1:
            raise TypeError("registry result must be exact")
        if type(self.files) is not tuple or any(type(item) is not OpenClawCheckFileV1 for item in self.files):
            raise TypeError("check files must be an exact immutable tuple")
        if tuple(item.destination for item in self.files) != _MANAGED_DESTINATIONS:
            raise TypeError("check result requires exactly four ordered managed destinations")
        expected_sync = self.registry.status is OpenClawRegistryStatus.MATCHES and all(
            item.status is OpenClawCheckStatus.MATCHES for item in self.files
        )
        expected_unsafe = self.registry.status is OpenClawRegistryStatus.UNSAFE or any(
            item.status is OpenClawCheckStatus.UNSAFE for item in self.files
        )
        if self.in_sync is not expected_sync or self.unsafe is not expected_unsafe:
            raise TypeError("check summary contradicts per-resource status")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "in_sync": self.in_sync,
            "unsafe": self.unsafe,
            "registry": self.registry.to_json(),
            "files": [item.to_json() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class OpenClawInstallFileV1:
    destination: str
    initial_state: OpenClawInitialState
    initial_sha256: str | None
    status: OpenClawInstallFileStatus
    replaced: bool
    restored: bool
    cleanup_completed: bool
    error_code: str | None

    def __post_init__(self) -> None:
        _destination(self.destination)
        if type(self.initial_state) is not OpenClawInitialState:
            raise TypeError("initial state must be exact")
        _optional_digest(self.initial_sha256, "initial digest")
        if (self.initial_state is OpenClawInitialState.PRESENT) is (self.initial_sha256 is None):
            raise TypeError("initial digest must exist exactly for a present target")
        if type(self.status) is not OpenClawInstallFileStatus:
            raise TypeError("install status must be exact")
        if any(type(value) is not bool for value in (self.replaced, self.restored, self.cleanup_completed)):
            raise TypeError("install file flags must be exact booleans")
        if self.error_code is not None and (
            type(self.error_code) is not str or _ERROR_CODE.fullmatch(self.error_code) is None
        ):
            raise TypeError("install error code must be bounded canonical ASCII")
        success = {
            OpenClawInstallFileStatus.NOT_ATTEMPTED,
            OpenClawInstallFileStatus.UNCHANGED,
            OpenClawInstallFileStatus.INSTALLED,
            OpenClawInstallFileStatus.REPLACED,
        }
        if self.status is OpenClawInstallFileStatus.NOT_ATTEMPTED and (
            self.replaced or self.restored or not self.cleanup_completed or self.error_code is not None
        ):
            raise TypeError("NOT_ATTEMPTED status contradicts file flags or error")
        if self.status is OpenClawInstallFileStatus.UNCHANGED and (
            self.replaced or self.restored or not self.cleanup_completed or self.error_code is not None
        ):
            raise TypeError("UNCHANGED status contradicts file flags or error")
        if self.status is OpenClawInstallFileStatus.INSTALLED and (
            self.initial_state is not OpenClawInitialState.ABSENT
            or self.replaced
            or self.restored
            or not self.cleanup_completed
            or self.error_code is not None
        ):
            raise TypeError("INSTALLED status contradicts initial state, flags, or error")
        if self.status is OpenClawInstallFileStatus.REPLACED and (
            self.initial_state is not OpenClawInitialState.PRESENT
            or not self.replaced
            or self.restored
            or not self.cleanup_completed
            or self.error_code is not None
        ):
            raise TypeError("REPLACED status contradicts initial state, flags, or error")
        if self.status is OpenClawInstallFileStatus.RESTORED_PRESENT and (
            self.initial_state is not OpenClawInitialState.PRESENT
            or not self.replaced
            or not self.restored
            or not self.cleanup_completed
            or self.error_code is None
        ):
            raise TypeError("RESTORED_PRESENT status contradicts initial state, flags, or error")
        if self.status is OpenClawInstallFileStatus.RESTORED_ABSENT and (
            self.initial_state is not OpenClawInitialState.ABSENT
            or self.replaced
            or not self.restored
            or not self.cleanup_completed
            or self.error_code is None
        ):
            raise TypeError("RESTORED_ABSENT status contradicts initial state, flags, or error")
        if self.status not in success and self.error_code is None:
            raise TypeError("failed install status requires an error code")
        if self.status is OpenClawInstallFileStatus.CLEANUP_FAILED and self.cleanup_completed:
            raise TypeError("CLEANUP_FAILED status contradicts cleanup flag")
        if self.status is OpenClawInstallFileStatus.FAILED and self.restored:
            raise TypeError("FAILED status cannot claim restoration")
        if self.status in {OpenClawInstallFileStatus.FAILED, OpenClawInstallFileStatus.CLEANUP_FAILED} and (
            self.replaced and self.initial_state is not OpenClawInitialState.PRESENT
        ):
            raise TypeError("failed status replacement flag contradicts initial state")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "destination": self.destination,
            "initial_state": self.initial_state.value,
            "initial_sha256": self.initial_sha256,
            "status": self.status.value,
            "replaced": self.replaced,
            "restored": self.restored,
            "cleanup_completed": self.cleanup_completed,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class OpenClawInstallResultV1:
    installed: bool
    mutation_started: bool
    deduplicated: bool
    registry_compliant: bool
    rollback_attempted: bool
    rollback_completed: bool | None
    cleanup_completed: bool
    files: tuple[OpenClawInstallFileV1, ...]

    globally_atomic: bool = False

    def __post_init__(self) -> None:
        flags = (
            self.installed,
            self.mutation_started,
            self.deduplicated,
            self.registry_compliant,
            self.rollback_attempted,
            self.cleanup_completed,
            self.globally_atomic,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("install result flags must be exact booleans")
        if self.globally_atomic:
            raise TypeError("multi-file installation is never globally atomic")
        if self.rollback_completed is not None and type(self.rollback_completed) is not bool:
            raise TypeError("rollback completion must be boolean or null")
        if self.rollback_attempted is (self.rollback_completed is None):
            raise TypeError("rollback completion must exist exactly when rollback was attempted")
        if type(self.files) is not tuple or any(type(item) is not OpenClawInstallFileV1 for item in self.files):
            raise TypeError("install files must be an exact immutable tuple")
        if tuple(item.destination for item in self.files) != _MANAGED_DESTINATIONS:
            raise TypeError("install result requires exactly four ordered managed destinations")
        if self.installed and (not self.registry_compliant or not self.cleanup_completed):
            raise TypeError("installed result requires compliant registry and verified cleanup")
        success_statuses = {
            OpenClawInstallFileStatus.UNCHANGED,
            OpenClawInstallFileStatus.INSTALLED,
            OpenClawInstallFileStatus.REPLACED,
        }
        if self.installed and any(item.status not in success_statuses for item in self.files):
            raise TypeError("installed result contradicts per-file status")
        changed = any(
            item.status in {OpenClawInstallFileStatus.INSTALLED, OpenClawInstallFileStatus.REPLACED}
            for item in self.files
        )
        if self.installed and self.mutation_started is not changed:
            raise TypeError("installed result mutation flag contradicts per-file status")
        if self.installed and self.rollback_attempted:
            raise TypeError("installed result cannot claim rollback")
        if self.cleanup_completed is not all(item.cleanup_completed for item in self.files):
            raise TypeError("result cleanup flag contradicts per-file cleanup")
        if not self.installed:
            allowed_failure = {
                OpenClawInstallFileStatus.NOT_ATTEMPTED,
                OpenClawInstallFileStatus.UNCHANGED,
                OpenClawInstallFileStatus.RESTORED_PRESENT,
                OpenClawInstallFileStatus.RESTORED_ABSENT,
            }
            if (
                not self.mutation_started
                or not self.rollback_attempted
                or self.rollback_completed is not True
                or not self.cleanup_completed
                or any(item.status not in allowed_failure for item in self.files)
                or not any(item.restored for item in self.files)
            ):
                raise TypeError("failed result contradicts mutation, rollback, cleanup, or per-file status")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "installed": self.installed,
            "mutation_started": self.mutation_started,
            "globally_atomic": False,
            "deduplicated": self.deduplicated,
            "registry_compliant": self.registry_compliant,
            "rollback_attempted": self.rollback_attempted,
            "rollback_completed": self.rollback_completed,
            "cleanup_completed": self.cleanup_completed,
            "files": [item.to_json() for item in self.files],
        }


class OpenClawDeploymentError(ValueError):
    def __init__(self, code: str, message: str, *, recovery_required: bool = False) -> None:
        if _ERROR_CODE.fullmatch(code) is None:
            raise TypeError("deployment error code must be canonical")
        super().__init__(message)
        self.code = code
        self.recovery_required = recovery_required


def _destination(value: str) -> None:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 512:
        raise TypeError("destination must be bounded relative text")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise TypeError("destination must be a normalized contained relative path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TypeError("destination contains a control character")


def _digest(value: str, field: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise TypeError(f"{field} must be canonical SHA-256")


def _optional_digest(value: str | None, field: str) -> None:
    if value is not None:
        _digest(value, field)


__all__ = [
    "OpenClawCheckFileV1",
    "OpenClawCheckResultV1",
    "OpenClawCheckStatus",
    "OpenClawDeploymentError",
    "OpenClawInitialState",
    "OpenClawInstallFileStatus",
    "OpenClawInstallFileV1",
    "OpenClawInstallResultV1",
    "OpenClawRegistryCheckV1",
    "OpenClawRegistryStatus",
]
