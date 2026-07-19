import hashlib
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from drawingmachine.config.models import ApplicationConfig, ConfigBundle, LogLevel, ProfileEnvelope
from drawingmachine.config.xdg import XdgPaths
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue

_APPLICATION_KEYS = frozenset({"schema_version", "machine_profile", "provider_profile", "log_level"})
_PROFILE_ENVELOPE_KEYS = frozenset({"schema_version", "profile"})
_SUPPORTED_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class _TomlDocument:
    data: dict[str, object]
    digest: str


def load_config(paths: XdgPaths) -> ConfigBundle:
    application_document = _load_toml(paths.config_file)
    application_data = application_document.data
    _require_exact_keys(application_data, _APPLICATION_KEYS, "application configuration")

    machine_profile = _require_string(application_data["machine_profile"], "machine_profile")
    provider_profile = _require_string(application_data["provider_profile"], "provider_profile")
    application = ApplicationConfig(
        schema_version=_require_schema_version(application_data["schema_version"], "application configuration"),
        machine_profile=machine_profile,
        provider_profile=provider_profile,
        log_level=_require_log_level(application_data["log_level"]),
    )

    machine_path = _resolve_profile_path(paths.config_dir, "machines", machine_profile)
    provider_path = _resolve_profile_path(paths.config_dir, "providers", provider_profile)
    machine, machine_digest = _load_profile_envelope(machine_path, "machine profile envelope")
    provider, provider_digest = _load_profile_envelope(provider_path, "provider profile envelope")

    return ConfigBundle(
        application=application,
        machine=machine,
        provider=provider,
        machine_path=machine_path,
        provider_path=provider_path,
        digests={
            "application": application_document.digest,
            "machine": machine_digest,
            "provider": provider_digest,
        },
    )


def _load_profile_envelope(path: Path, context: str) -> tuple[ProfileEnvelope, str]:
    document = _load_toml(path)
    data = document.data
    _require_exact_keys(data, _PROFILE_ENVELOPE_KEYS, context)
    profile_value = data["profile"]
    if not isinstance(profile_value, dict):
        raise _configuration_error(
            f"{context} profile must be a table",
            {"field": "profile", "path": str(path)},
        )
    return (
        ProfileEnvelope(
            schema_version=_require_schema_version(data["schema_version"], context),
            profile=_json_object(cast(dict[object, object], profile_value), f"{context}.profile"),
        ),
        document.digest,
    )


def _load_toml(path: Path) -> _TomlDocument:
    try:
        contents = path.read_bytes()
        parsed = cast(object, tomllib.loads(contents.decode("utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _configuration_error(
            f"could not load TOML configuration: {path}",
            {"path": str(path), "reason": str(error)},
        ) from error

    if not isinstance(parsed, dict):
        raise _configuration_error(
            f"TOML configuration root must be a table: {path}",
            {"path": str(path)},
        )
    return _TomlDocument(
        data=_string_object_mapping(cast(dict[object, object], parsed), str(path)),
        digest=hashlib.sha256(contents).hexdigest(),
    )


def _string_object_mapping(value: dict[object, object], context: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _configuration_error(
                f"{context} contains a non-string key",
                {"path": context},
            )
        result[key] = item
    return result


def _require_exact_keys(data: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    actual = set(data)
    unknown = sorted(actual - expected)
    if unknown:
        unknown_values: list[JsonValue] = [key for key in unknown]
        raise _configuration_error(
            f"{context} has unknown keys: {', '.join(unknown)}",
            {"unknown_keys": unknown_values},
        )
    missing = sorted(expected - actual)
    if missing:
        missing_values: list[JsonValue] = [key for key in missing]
        raise _configuration_error(
            f"{context} has missing keys: {', '.join(missing)}",
            {"missing_keys": missing_values},
        )


def _require_schema_version(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _configuration_error(
            f"{context} schema_version must be an integer",
            {"field": "schema_version"},
        )
    if value != _SUPPORTED_SCHEMA_VERSION:
        raise _configuration_error(
            f"{context} has unsupported schema_version: {value}",
            {"field": "schema_version", "value": value},
        )
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _configuration_error(
            f"{field} must be a string",
            {"field": field},
        )
    return value


def _require_log_level(value: object) -> LogLevel:
    log_level = _require_string(value, "log_level")
    try:
        return LogLevel(log_level)
    except ValueError as error:
        raise _configuration_error(
            f"unsupported log_level: {log_level}",
            {"field": "log_level", "value": log_level},
        ) from error


def _resolve_profile_path(config_dir: Path, directory: str, profile_name: str) -> Path:
    name_path = Path(profile_name)
    if not profile_name or name_path.is_absolute() or len(name_path.parts) != 1 or profile_name in {".", ".."}:
        raise _configuration_error(
            f"invalid profile name: {profile_name}",
            {"field": f"{directory[:-1]}_profile", "value": profile_name},
        )

    profile_dir = config_dir.resolve() / directory
    unresolved = profile_dir / f"{profile_name}.toml"
    try:
        resolved = unresolved.resolve(strict=True)
    except FileNotFoundError as error:
        raise _configuration_error(
            f"profile file does not exist: {unresolved}",
            {"path": str(unresolved)},
        ) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise _configuration_error(
            f"could not resolve profile file: {unresolved}",
            {"path": str(unresolved), "reason": str(error)},
        ) from error

    try:
        resolved.relative_to(profile_dir)
    except ValueError as error:
        raise _configuration_error(
            f"profile path escapes {directory} directory: {resolved}",
            {"path": str(resolved)},
        ) from error
    if not resolved.is_file():
        raise _configuration_error(
            f"profile path is not a file: {resolved}",
            {"path": str(resolved)},
        )
    return resolved


def _json_object(value: dict[object, object], context: str) -> JsonObject:
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _configuration_error(
                f"{context} contains a non-string key",
                {"field": context},
            )
        result[key] = _json_value(item, f"{context}.{key}")
    return result


def _json_value(value: object, context: str) -> JsonValue:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _configuration_error(
                f"{context} must be finite",
                {"field": context},
            )
        return value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{context}[]") for item in cast(list[object], value)]
    if isinstance(value, dict):
        return _json_object(cast(dict[object, object], value), context)
    raise _configuration_error(
        f"{context} must be JSON-compatible",
        {"field": context, "type": type(value).__name__},
    )


def _configuration_error(message: str, details: Mapping[str, JsonValue]) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code="CONFIG_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            retryable=False,
            details=details,
        )
    )
