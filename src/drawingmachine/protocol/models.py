from dataclasses import dataclass

from drawingmachine.errors import ErrorPayload
from drawingmachine.json_types import JsonObject

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class ProtocolRequest:
    protocol_version: int
    command: str
    request_id: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ProtocolResponse:
    protocol_version: int
    schema_version: int
    ok: bool
    command: str
    request_id: str
    data: JsonObject
    error: ErrorPayload | None
