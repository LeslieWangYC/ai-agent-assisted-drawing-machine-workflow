from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn
from uuid import UUID

from drawingmachine.domain.machine import AuthenticatedMachinePrincipal, RecoveryDisposition
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class MachineCommandMode(StrEnum):
    REQUEST = "REQUEST"
    EXECUTE = "EXECUTE"


@dataclass(frozen=True, slots=True)
class MachineRuntimeAuthority:
    service_epoch: str
    application_version: str
    application_schema_version: int
    application_digest: str
    machine_profile: str
    machine_schema_version: int
    machine_digest: str
    provider_profile: str
    provider_schema_version: int
    provider_digest: str

    def __post_init__(self) -> None:
        _uuid4(self.service_epoch, "service_epoch")
        for field in ("application_version", "machine_profile", "provider_profile"):
            _text(getattr(self, field), field)
        for field in ("application_schema_version", "machine_schema_version", "provider_schema_version"):
            if type(getattr(self, field)) is not int or getattr(self, field) != 1:
                _invalid(f"{field} must be exactly schema version 1")
        for field in ("application_digest", "machine_digest", "provider_digest"):
            value = getattr(self, field)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                _invalid(f"{field} must be a canonical SHA-256")


@dataclass(frozen=True, slots=True)
class PrepareMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    job_id: str
    job_revision: int

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        _principal(self.requester)
        _text(self.job_id, "job_id")
        if type(self.job_revision) is not int or self.job_revision < 1:
            _invalid("job_revision must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class HomeMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _motion_command(self.request_id, self.requester, self.execution_id, self.mode, self.challenge_id)


@dataclass(frozen=True, slots=True)
class ZCalMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _motion_command(self.request_id, self.requester, self.execution_id, self.mode, self.challenge_id)


@dataclass(frozen=True, slots=True)
class ZConfirmMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _motion_command(self.request_id, self.requester, self.execution_id, self.mode, self.challenge_id)


@dataclass(frozen=True, slots=True)
class StreamMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _motion_command(self.request_id, self.requester, self.execution_id, self.mode, self.challenge_id)


@dataclass(frozen=True, slots=True)
class HomeZMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _motion_command(self.request_id, self.requester, self.execution_id, self.mode, self.challenge_id)


@dataclass(frozen=True, slots=True)
class RecoverMachineCommand:
    request_id: str
    requester: AuthenticatedMachinePrincipal
    execution_id: str
    disposition: RecoveryDisposition
    mode: MachineCommandMode
    challenge_id: str | None

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        _principal(self.requester)
        _text(self.execution_id, "execution_id")
        if type(self.disposition) is not RecoveryDisposition:
            _invalid("disposition must be exact RecoveryDisposition")
        if type(self.mode) is not MachineCommandMode:
            _invalid("mode must be exact MachineCommandMode")
        if (self.mode is MachineCommandMode.REQUEST) != (self.challenge_id is None):
            _invalid("request mode requires no challenge; execute mode requires one challenge")
        if self.challenge_id is not None:
            _text(self.challenge_id, "challenge_id")


MachineActionCommand = (
    PrepareMachineCommand
    | HomeMachineCommand
    | ZCalMachineCommand
    | ZConfirmMachineCommand
    | StreamMachineCommand
    | HomeZMachineCommand
    | RecoverMachineCommand
)


def _motion_command(
    request_id: object,
    requester: object,
    execution_id: object,
    mode: object,
    challenge_id: object,
) -> None:
    _text(request_id, "request_id")
    _principal(requester)
    _text(execution_id, "execution_id")
    if type(mode) is not MachineCommandMode:
        _invalid("mode must be exact MachineCommandMode")
    if (mode is MachineCommandMode.REQUEST) != (challenge_id is None):
        _invalid("request mode requires no challenge; execute mode requires one challenge")
    if challenge_id is not None:
        _text(challenge_id, "challenge_id")


def _principal(value: object) -> None:
    if type(value) is not AuthenticatedMachinePrincipal:
        _invalid("requester must be an exact authenticated kernel principal")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        _invalid(f"{field} must be a bounded non-empty exact string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{field} must be valid UTF-8")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid(f"{field} must not contain controls")
    return value


def _uuid4(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = UUID(text)
    except ValueError:
        _invalid(f"{field} must be a canonical UUID4")
    if parsed.version != 4 or str(parsed) != text:
        _invalid(f"{field} must be a canonical UUID4")
    return text


def _invalid(message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="MACHINE_COMMAND_INVALID",
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details={},
        )
    )


__all__ = [
    "HomeMachineCommand",
    "HomeZMachineCommand",
    "MachineActionCommand",
    "MachineCommandMode",
    "MachineRuntimeAuthority",
    "PrepareMachineCommand",
    "RecoverMachineCommand",
    "StreamMachineCommand",
    "ZCalMachineCommand",
    "ZConfirmMachineCommand",
]
