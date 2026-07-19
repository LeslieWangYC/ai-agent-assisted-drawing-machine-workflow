from __future__ import annotations

from typing import NoReturn

from drawingmachine.domain.machine import RecoveryDisposition, RecoveryIntent
from drawingmachine.domain.machine.transitions import allowed_recovery_disposition
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload


def validate_recovery_disposition(intent: RecoveryIntent, disposition: RecoveryDisposition) -> None:
    if not allowed_recovery_disposition(intent, disposition):
        _denied("recovery disposition is not legal for the frozen recovery intent")


def _denied(message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code="MACHINE_RECOVERY_DISPOSITION_DENIED",
            category=ErrorCategory.PERMISSION,
            message=message,
            retryable=False,
            details={},
        )
    )


__all__ = ["validate_recovery_disposition"]
