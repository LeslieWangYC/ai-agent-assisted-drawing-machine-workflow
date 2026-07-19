from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from drawingmachine.json_types import JsonObject


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    schema_version: int
    machine_profile: str
    provider_profile: str
    log_level: LogLevel


@dataclass(frozen=True, slots=True)
class ProfileEnvelope:
    schema_version: int
    profile: JsonObject


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    application: ApplicationConfig
    machine: ProfileEnvelope
    provider: ProfileEnvelope
    machine_path: Path
    provider_path: Path
    digests: Mapping[str, str]
