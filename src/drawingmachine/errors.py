from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from drawingmachine.json_types import JsonObject, JsonValue


class ErrorCategory(StrEnum):
    INPUT = "input"
    CONFIGURATION = "configuration"
    PROVIDER = "provider"
    PLANNING = "planning"
    VALIDATION = "validation"
    SERVICE = "service"
    HARDWARE = "hardware"
    PERMISSION = "permission"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ErrorPayload:
    code: str
    category: ErrorCategory
    message: str
    retryable: bool
    details: Mapping[str, JsonValue]
    request_id: str | None = None
    job_id: str | None = None

    def to_json(self) -> JsonObject:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
            "request_id": self.request_id,
            "job_id": self.job_id,
        }


class DrawingMachineError(Exception):
    def __init__(self, payload: ErrorPayload) -> None:
        super().__init__(payload.message)
        self.payload = payload
