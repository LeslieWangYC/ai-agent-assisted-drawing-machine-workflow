from __future__ import annotations

import hashlib
import io
import json
import math
import mimetypes
import os
import re
import stat
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import KW_ONLY, InitVar, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import NoReturn, Protocol, cast

from PIL import Image

from drawingmachine.config.models import ProfileEnvelope
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject, JsonValue
from drawingmachine.ports.providers import (
    MAX_PROVIDER_PROMPT_BYTES,
    PreparedProviderRequest,
    ProviderPollResult,
    ProviderPollState,
    ProviderRecord,
    ProviderRequestV1,
    ProviderResultV1,
    ProviderStatus,
    ProviderSubmission,
)

_PROFILE_KEYS = frozenset(
    {
        "name",
        "endpoint",
        "workflow_template",
        "model_family",
        "scale_to_length",
        "timeout_seconds",
        "poll_interval_seconds",
        "free_after_run",
        "workflow_nodes",
        "sampler_defaults",
        "live_execution_requires_execute_flag",
    }
)
_WORKFLOW_NODE_KEYS = frozenset({"load_image", "prompt", "sampler", "save_image", "scale"})
_SAMPLER_KEYS = frozenset({"steps", "cfg", "denoise"})
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HARD_LOAD_NODE = "25"
_HARD_SAMPLER_NODE = "28"
_HARD_SAVE_NODE = "18"
_HARD_SCALE_NODE = "221"
_MAX_SCALE = 16_384
_MAX_TIMEOUT_SECONDS = 3_600.0
_MAX_POLL_INTERVAL_SECONDS = 300.0
_MAX_POLLS = 10_000
_MAX_WORKFLOW_BYTES = 8 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_DIMENSION = 16_384
_MAX_IMAGE_PIXELS = 64 * 1024 * 1024
MAX_PROVIDER_ENDPOINT_BYTES = 4_096
MAX_PROVIDER_MODEL_FAMILY_BYTES = 255
MAX_PROVIDER_WORKFLOW_NODE_ID_BYTES = 255
_ALLOWED_IMAGE_TYPES = frozenset({"output", "temp"})
_ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
_IMAGE_KEYS = frozenset({"filename", "subfolder", "type"})

# Public adapter admission limits. These bound all caller-owned bytes and identities
# retained when a caller never submits, polls, retrieves, or explicitly releases.
MAX_INPUT_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PREPARED_ENTRIES = 128
MAX_LIVE_SUBMISSIONS = 64
PREPARED_TTL_SECONDS = 300.0

_STATUS_REQUIRED_KEYS = frozenset({"status_str", "completed"})
_STATUS_ALLOWED_KEYS = frozenset({"status_str", "completed", "messages"})
_SUCCESS_STATUSES = frozenset({"success"})
_FAILURE_STATUSES = frozenset({"error", "failed"})
_PENDING_STATUSES = frozenset({"running", "pending"})
_MESSAGE_EVENTS = frozenset(
    {
        "execution_start",
        "execution_cached",
        "executing",
        "progress",
        "executed",
        "execution_error",
        "execution_interrupted",
        "execution_success",
    }
)
_MAX_STATUS_MESSAGES = 2_048
_MAX_STATUS_JSON_DEPTH = 64
_MAX_STATUS_JSON_ITEMS = 16_384
_MAX_STATUS_STRING_BYTES = 1 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _NONBLOCK | _CLOEXEC


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _configuration_error(message: str, *, field: str) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code="PROVIDER_CONFIG_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            retryable=False,
            details={"field": field},
        )
    )


def _config_invalid(message: str, *, field: str) -> NoReturn:
    raise _configuration_error(message, field=field)


def _provider_error(
    code: str,
    message: str,
    *,
    phase: str,
    request_id: str | None = None,
    retryable: bool = False,
) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.PROVIDER,
            message=message,
            retryable=retryable,
            details={"phase": phase},
            request_id=request_id,
        )
    )


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        _config_invalid(f"{field} has unknown keys: {', '.join(unknown)}", field=field)
    missing = sorted(expected - set(value))
    if missing:
        _config_invalid(f"{field} has missing keys: {', '.join(missing)}", field=field)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _config_invalid(f"{field} must be a table", field=field)
    result: dict[str, object] = {}
    invalid = False
    try:
        for key, item in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                invalid = True
                break
            result[key] = item
    except Exception:
        invalid = True
    if invalid:
        raise _configuration_error(f"{field} contains invalid data", field=field)
    return result


def _string(value: object, field: str, *, maximum_utf8_bytes: int | None = None) -> str:
    if type(value) is not str:
        _config_invalid(f"{field} must be a string", field=field)
    if not value:
        _config_invalid(f"{field} must not be empty", field=field)
    if maximum_utf8_bytes is not None and len(value) > maximum_utf8_bytes:
        _config_invalid(f"{field} must be at most {maximum_utf8_bytes} UTF-8 bytes", field=field)
    invalid_unicode = False
    encoded = b""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        invalid_unicode = True
    if invalid_unicode:
        raise _configuration_error(f"{field} contains invalid Unicode", field=field)
    if maximum_utf8_bytes is not None and len(encoded) > maximum_utf8_bytes:
        _config_invalid(f"{field} must be at most {maximum_utf8_bytes} UTF-8 bytes", field=field)
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _config_invalid(f"{field} must be a boolean", field=field)
    return value


def _integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _config_invalid(f"{field} must be an integer", field=field)
    if value < minimum or value > maximum:
        _config_invalid(f"{field} must be between {minimum} and {maximum}", field=field)
    return value


def _number(value: object, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _config_invalid(f"{field} must be a number", field=field)
    conversion_failed = False
    result = 0.0
    try:
        result = float(value)
    except OverflowError:
        conversion_failed = True
    if conversion_failed:
        raise _configuration_error(f"{field} must be finite", field=field)
    if not math.isfinite(result):
        _config_invalid(f"{field} must be finite", field=field)
    if result < minimum or result > maximum:
        _config_invalid(f"{field} must be between {minimum} and {maximum}", field=field)
    return result


def _endpoint(value: object) -> str:
    endpoint = _string(
        value,
        "profile.endpoint",
        maximum_utf8_bytes=MAX_PROVIDER_ENDPOINT_BYTES,
    ).rstrip("/")
    invalid = any(ord(character) < 32 or ord(character) == 127 for character in endpoint)
    parsed: urllib.parse.SplitResult | None = None
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        invalid = True
    if invalid or parsed is None:
        raise _configuration_error("profile.endpoint is invalid", field="profile.endpoint")
    if parsed.scheme not in {"http", "https"}:
        _config_invalid("profile.endpoint must use http or https", field="profile.endpoint")
    if not parsed.netloc or parsed.hostname is None:
        _config_invalid("profile.endpoint must include a host", field="profile.endpoint")
    if parsed.username is not None or parsed.password is not None:
        _config_invalid("profile.endpoint must not contain credentials", field="profile.endpoint")
    if parsed.query or parsed.fragment:
        _config_invalid("profile.endpoint must not contain query or fragment", field="profile.endpoint")
    return endpoint


def _workflow_relative_path(value: object) -> PurePosixPath:
    text = _string(value, "profile.workflow_template")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or "\\" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or str(relative) != text
        or any(part in {".", ".."} for part in relative.parts)
    ):
        _config_invalid(
            "profile.workflow_template must be a canonical relative path",
            field="profile.workflow_template",
        )
    return relative


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    with suppress(OSError):
        os.close(descriptor)


def _open_directory_chain(path: Path) -> int | None:
    if (
        not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or any(any(ord(character) < 32 or ord(character) == 127 for character in part) for part in path.parts)
    ):
        return None
    descriptor: int | None = None
    failed = False
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for part in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    part,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except (OSError, ValueError):
                failed = True
                break
            _close_descriptor(descriptor)
            descriptor = next_descriptor
    except (OSError, ValueError):
        failed = True
    if failed:
        _close_descriptor(descriptor)
        return None
    return descriptor


def _read_workflow_snapshot(relative: PurePosixPath, *, profile_path: Path) -> tuple[Path, Mapping[str, JsonValue]]:
    if not isinstance(profile_path, Path) or not profile_path.is_absolute():
        _config_invalid("provider profile_path must be an absolute path", field="profile_path")
    if any(part in {".", ".."} for part in profile_path.parts) or profile_path.name in {"", ".", ".."}:
        _config_invalid("provider profile_path must be canonical", field="profile_path")
    directory_descriptor = _open_directory_chain(profile_path.parent)
    if directory_descriptor is None:
        _config_invalid("provider profile_path directory is invalid", field="profile_path")
    current_descriptor = directory_descriptor
    workflow_descriptor: int | None = None
    profile_descriptor: int | None = None
    invalid = False
    raw = b""
    try:
        try:
            profile_descriptor = os.open(profile_path.name, _READ_FLAGS, dir_fd=directory_descriptor)
            profile_stat = os.fstat(profile_descriptor)
            _close_descriptor(profile_descriptor)
            profile_descriptor = None
            if not stat.S_ISREG(profile_stat.st_mode):
                invalid = True
        except (OSError, ValueError):
            invalid = True
        if not invalid:
            for part in relative.parts[:-1]:
                try:
                    next_descriptor = os.open(
                        part,
                        _DIRECTORY_FLAGS,
                        dir_fd=current_descriptor,
                    )
                except (OSError, ValueError):
                    invalid = True
                    break
                if current_descriptor != directory_descriptor:
                    _close_descriptor(current_descriptor)
                current_descriptor = next_descriptor
        if not invalid:
            try:
                workflow_descriptor = os.open(relative.name, _READ_FLAGS, dir_fd=current_descriptor)
                workflow_stat = os.fstat(workflow_descriptor)
                if not stat.S_ISREG(workflow_stat.st_mode) or not 0 < workflow_stat.st_size <= _MAX_WORKFLOW_BYTES:
                    invalid = True
                else:
                    chunks: list[bytes] = []
                    remaining = _MAX_WORKFLOW_BYTES + 1
                    while remaining:
                        chunk = os.read(workflow_descriptor, min(64 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    if not raw or len(raw) > _MAX_WORKFLOW_BYTES:
                        invalid = True
            except (OSError, ValueError):
                invalid = True
    finally:
        _close_descriptor(profile_descriptor)
        _close_descriptor(workflow_descriptor)
        if current_descriptor != directory_descriptor:
            _close_descriptor(current_descriptor)
        _close_descriptor(directory_descriptor)
    if invalid:
        raise _configuration_error(
            "profile.workflow_template must name an existing regular file with bounded size and no symlinks",
            field="profile.workflow_template",
        )
    parse_invalid = False
    value: object = None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        parse_invalid = True
    if parse_invalid or not isinstance(value, dict):
        raise _configuration_error("workflow_template must be a JSON object", field="workflow_template")
    strict_invalid = False
    document: JsonValue = None
    try:
        document = _plain_json(value)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        strict_invalid = True
    if strict_invalid or not isinstance(document, dict):
        raise _configuration_error("workflow_template must contain strict JSON", field="workflow_template")
    freeze_invalid = False
    frozen: JsonValue = None
    try:
        frozen = _freeze_document(document)
    except RecursionError:
        freeze_invalid = True
    if freeze_invalid:
        raise _configuration_error("workflow_template exceeds the supported JSON depth", field="workflow_template")
    if not isinstance(frozen, Mapping):
        raise _configuration_error("workflow_template must be a JSON object", field="workflow_template")
    resolved = profile_path.parent.joinpath(*relative.parts)
    return resolved, cast(Mapping[str, JsonValue], frozen)


def _freeze_document(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(JsonValue, MappingProxyType({key: _freeze_document(item) for key, item in value.items()}))
    if isinstance(value, list):
        return cast(JsonValue, tuple(_freeze_document(item) for item in value))
    return value


def _workflow_nodes(value: object) -> Mapping[str, str]:
    data = _mapping(value, "profile.workflow_nodes")
    _exact_keys(data, _WORKFLOW_NODE_KEYS, "profile.workflow_nodes")
    result: dict[str, str] = {}
    for key in ("load_image", "prompt", "sampler", "save_image", "scale"):
        node = _string(
            data[key],
            f"profile.workflow_nodes.{key}",
            maximum_utf8_bytes=MAX_PROVIDER_WORKFLOW_NODE_ID_BYTES,
        )
        if _NODE_ID.fullmatch(node) is None:
            _config_invalid(
                f"profile.workflow_nodes.{key} must be a safe node id", field=f"profile.workflow_nodes.{key}"
            )
        result[key] = node
    return MappingProxyType(result)


def _sampler_defaults(value: object) -> JsonObject:
    data = _mapping(value, "profile.sampler_defaults")
    _exact_keys(data, _SAMPLER_KEYS, "profile.sampler_defaults")
    steps_value = data["steps"]
    cfg_value = data["cfg"]
    denoise_value = data["denoise"]
    steps = (
        None
        if steps_value is None
        else _integer(steps_value, "profile.sampler_defaults.steps", minimum=1, maximum=10_000)
    )
    cfg = None if cfg_value is None else _number(cfg_value, "profile.sampler_defaults.cfg", minimum=0.0, maximum=100.0)
    denoise = (
        None
        if denoise_value is None
        else _number(
            denoise_value,
            "profile.sampler_defaults.denoise",
            minimum=0.0,
            maximum=1.0,
        )
    )
    return cast(JsonObject, MappingProxyType({"steps": steps, "cfg": cfg, "denoise": denoise}))


def _validate_workflow_document(workflow: Mapping[str, JsonValue], *, prompt_node: str) -> None:
    required = {_HARD_LOAD_NODE, _HARD_SAMPLER_NODE, _HARD_SAVE_NODE, _HARD_SCALE_NODE, prompt_node}
    for node in required:
        item = workflow.get(node)
        if not isinstance(item, Mapping) or not isinstance(item.get("inputs"), Mapping):
            raise _configuration_error(
                "workflow_template is missing a required node inputs object",
                field="workflow_template",
            )
    prompt_inputs = cast(Mapping[str, JsonValue], cast(Mapping[str, JsonValue], workflow[prompt_node])["inputs"])
    _string(
        prompt_inputs.get("prompt"),
        "configured prompt node inputs.prompt",
        maximum_utf8_bytes=MAX_PROVIDER_PROMPT_BYTES,
    )


@dataclass(frozen=True, slots=True)
class LocalComfyUIConfig:
    name: str
    endpoint: str
    workflow_template: Path
    model_family: str
    scale_to_length: int
    timeout_seconds: float
    poll_interval_seconds: float
    free_after_run: bool
    workflow_nodes: Mapping[str, str]
    sampler_defaults: JsonObject
    live_execution_requires_execute_flag: bool
    _: KW_ONLY
    profile_path: InitVar[Path]
    _workflow_document: Mapping[str, JsonValue] = field(init=False, repr=False, compare=False)

    def __post_init__(self, profile_path: Path) -> None:
        if type(self.name) is not str or self.name != "local-comfyui":
            _config_invalid("profile.name must be exactly local-comfyui", field="profile.name")
        endpoint = _endpoint(self.endpoint)
        if not isinstance(self.workflow_template, Path):
            _config_invalid("profile.workflow_template must be a path", field="workflow_template")
        relative_workflow = _workflow_relative_path(self.workflow_template.as_posix())
        _string(
            self.model_family,
            "profile.model_family",
            maximum_utf8_bytes=MAX_PROVIDER_MODEL_FAMILY_BYTES,
        )
        _integer(self.scale_to_length, "profile.scale_to_length", minimum=1, maximum=_MAX_SCALE)
        timeout = _number(
            self.timeout_seconds,
            "profile.timeout_seconds",
            minimum=0.001,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        poll = _number(
            self.poll_interval_seconds,
            "profile.poll_interval_seconds",
            minimum=0.001,
            maximum=_MAX_POLL_INTERVAL_SECONDS,
        )
        if poll > timeout or math.ceil(timeout / poll) > _MAX_POLLS:
            _config_invalid(
                "profile.poll_interval_seconds must produce a positive bounded poll count",
                field="profile.poll_interval_seconds",
            )
        _boolean(self.free_after_run, "profile.free_after_run")
        nodes = _workflow_nodes(self.workflow_nodes)
        sampler = _sampler_defaults(self.sampler_defaults)
        execute_flag = _boolean(
            self.live_execution_requires_execute_flag,
            "profile.live_execution_requires_execute_flag",
        )
        if not execute_flag:
            _config_invalid(
                "profile.live_execution_requires_execute_flag must preserve the execute flag",
                field="profile.live_execution_requires_execute_flag",
            )
        canonical_workflow, workflow_document = _read_workflow_snapshot(
            relative_workflow,
            profile_path=profile_path,
        )
        _validate_workflow_document(workflow_document, prompt_node=nodes["prompt"])
        object.__setattr__(self, "workflow_nodes", nodes)
        object.__setattr__(self, "sampler_defaults", sampler)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "poll_interval_seconds", poll)
        object.__setattr__(self, "workflow_template", canonical_workflow)
        object.__setattr__(self, "_workflow_document", workflow_document)

    @classmethod
    def from_profile(cls, envelope: ProfileEnvelope, *, profile_path: Path) -> LocalComfyUIConfig:
        if isinstance(envelope.schema_version, bool) or envelope.schema_version != 1:
            _config_invalid("provider profile schema_version must be 1", field="schema_version")
        data = _mapping(envelope.profile, "profile")
        _exact_keys(data, _PROFILE_KEYS, "profile")
        name_value = data["name"]
        if type(name_value) is not str or name_value != "local-comfyui":
            _config_invalid("profile.name must be exactly local-comfyui", field="profile.name")
        name = "local-comfyui"
        workflow_template = Path(_workflow_relative_path(data["workflow_template"]))
        timeout = _number(
            data["timeout_seconds"],
            "profile.timeout_seconds",
            minimum=0.001,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        poll = _number(
            data["poll_interval_seconds"],
            "profile.poll_interval_seconds",
            minimum=0.001,
            maximum=_MAX_POLL_INTERVAL_SECONDS,
        )
        if poll > timeout or math.ceil(timeout / poll) > _MAX_POLLS:
            _config_invalid(
                "profile.poll_interval_seconds must produce a positive bounded poll count",
                field="profile.poll_interval_seconds",
            )
        execute_flag = _boolean(
            data["live_execution_requires_execute_flag"],
            "profile.live_execution_requires_execute_flag",
        )
        if not execute_flag:
            _config_invalid(
                "profile.live_execution_requires_execute_flag must preserve the execute flag",
                field="profile.live_execution_requires_execute_flag",
            )
        return cls(
            name=name,
            endpoint=_endpoint(data["endpoint"]),
            workflow_template=workflow_template,
            model_family=_string(
                data["model_family"],
                "profile.model_family",
                maximum_utf8_bytes=MAX_PROVIDER_MODEL_FAMILY_BYTES,
            ),
            scale_to_length=_integer(
                data["scale_to_length"],
                "profile.scale_to_length",
                minimum=1,
                maximum=_MAX_SCALE,
            ),
            timeout_seconds=timeout,
            poll_interval_seconds=poll,
            free_after_run=_boolean(data["free_after_run"], "profile.free_after_run"),
            workflow_nodes=_workflow_nodes(data["workflow_nodes"]),
            sampler_defaults=_sampler_defaults(data["sampler_defaults"]),
            live_execution_requires_execute_flag=execute_flag,
            profile_path=profile_path,
        )


class HttpTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
    ) -> JsonValue: ...

    def upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
    ) -> JsonValue: ...

    def request_bytes(self, url: str, *, timeout_seconds: float) -> bytes: ...


class _DenyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


class _ResponseProtocolError(Exception):
    """Internal marker for a bounded response whose bytes are not valid JSON."""


class _ReadableResponse(Protocol):
    def read(self, maximum: int) -> bytes: ...


def _transport_remaining(expires_at: float, monotonic: Callable[[], float]) -> float:
    remaining = expires_at - monotonic()
    if remaining <= 0:
        raise TimeoutError("transport deadline expired")
    return remaining


def _set_response_timeout(response: object, timeout: float) -> None:
    setter = getattr(response, "settimeout", None)
    if not callable(setter):
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        socket = getattr(raw, "_sock", None)
        setter = getattr(socket, "settimeout", None)
    if not callable(setter):
        raise ValueError("response does not expose a bounded read timeout")
    setter(timeout)


def _bounded_response_read(
    response: _ReadableResponse,
    *,
    maximum: int,
    expires_at: float,
    monotonic: Callable[[], float],
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= maximum:
        remaining = _transport_remaining(expires_at, monotonic)
        _set_response_timeout(response, remaining)
        chunk = response.read(maximum + 1 - total)
        _transport_remaining(expires_at, monotonic)
        if not isinstance(chunk, bytes):
            raise ValueError("invalid bounded response")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b"".join(chunks)
    if not raw or len(raw) > maximum:
        raise ValueError("invalid bounded response")
    return raw


class UrllibHttpTransport:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._opener = urllib.request.build_opener(_DenyRedirectHandler())
        self._monotonic = monotonic

    def _deadline(self, timeout_seconds: float, absolute_deadline: float | None = None) -> float:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise TimeoutError("transport deadline expired")
        relative_deadline = self._monotonic() + timeout_seconds
        return relative_deadline if absolute_deadline is None else min(relative_deadline, absolute_deadline)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
        absolute_deadline: float | None = None,
    ) -> JsonValue:
        deadline = self._deadline(timeout_seconds, absolute_deadline)
        body = None if payload is None else _json_bytes(payload)
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with self._opener.open(request, timeout=_transport_remaining(deadline, self._monotonic)) as response:
            _transport_remaining(deadline, self._monotonic)
            raw = _bounded_response_read(
                response,
                maximum=_MAX_JSON_RESPONSE_BYTES,
                expires_at=deadline,
                monotonic=self._monotonic,
            )
        _transport_remaining(deadline, self._monotonic)
        parse_failed = False
        parsed: JsonValue = None
        try:
            parsed = cast(JsonValue, json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            parse_failed = True
        if parse_failed:
            raise _ResponseProtocolError("invalid JSON response")
        _transport_remaining(deadline, self._monotonic)
        return parsed

    def upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
        absolute_deadline: float | None = None,
    ) -> JsonValue:
        deadline = self._deadline(timeout_seconds, absolute_deadline)
        boundary = "----DrawingMachineComfyUI" + uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
            f"--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            url,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with self._opener.open(request, timeout=_transport_remaining(deadline, self._monotonic)) as response:
            _transport_remaining(deadline, self._monotonic)
            raw = _bounded_response_read(
                response,
                maximum=_MAX_JSON_RESPONSE_BYTES,
                expires_at=deadline,
                monotonic=self._monotonic,
            )
        _transport_remaining(deadline, self._monotonic)
        parse_failed = False
        parsed: JsonValue = None
        try:
            parsed = cast(JsonValue, json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            parse_failed = True
        if parse_failed:
            raise _ResponseProtocolError("invalid JSON response")
        _transport_remaining(deadline, self._monotonic)
        return parsed

    def request_bytes(
        self,
        url: str,
        *,
        timeout_seconds: float,
        absolute_deadline: float | None = None,
    ) -> bytes:
        deadline = self._deadline(timeout_seconds, absolute_deadline)
        request = urllib.request.Request(url, method="GET")
        with self._opener.open(request, timeout=_transport_remaining(deadline, self._monotonic)) as response:
            _transport_remaining(deadline, self._monotonic)
            raw = _bounded_response_read(
                response,
                maximum=_MAX_IMAGE_RESPONSE_BYTES,
                expires_at=deadline,
                monotonic=self._monotonic,
            )
        _transport_remaining(deadline, self._monotonic)
        return raw


def _plain_json(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        if type(value) is not str:
            raise TypeError("non-built-in JSON string")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                raise TypeError("non-string JSON key")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_plain_json(item) for item in value]
    raise TypeError("non-JSON value")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_copy(value: object, *, phase: str, request_id: str | None = None) -> JsonValue:
    invalid = False
    copied: JsonValue = None
    try:
        copied = cast(JsonValue, json.loads(_json_bytes(value)))
    except Exception:
        invalid = True
    if invalid:
        raise _provider_error(
            "PROVIDER_PROTOCOL_INVALID",
            "provider returned an invalid response",
            phase=phase,
            request_id=request_id,
        )
    return copied


def _json_object(value: object, *, phase: str, request_id: str | None = None) -> JsonObject:
    copied = _json_copy(value, phase=phase, request_id=request_id)
    if not isinstance(copied, dict):
        raise _provider_error(
            "PROVIDER_PROTOCOL_INVALID",
            "provider returned an invalid response",
            phase=phase,
            request_id=request_id,
        )
    return copied


def _endpoint_url(endpoint: str, *components: str) -> str:
    invalid = False
    parsed: urllib.parse.SplitResult | None = None
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        invalid = True
    if invalid or parsed is None:
        raise _configuration_error("profile.endpoint is invalid", field="profile.endpoint")
    base_parts = [part for part in parsed.path.split("/") if part]
    encoded = "/".join(urllib.parse.quote(urllib.parse.unquote(part), safe="") for part in (*base_parts, *components))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"/{encoded}", "", ""))


def _validated_image_record(image: object, *, phase: str) -> JsonObject:
    if not isinstance(image, Mapping):
        raise _provider_error("PROVIDER_PROTOCOL_INVALID", "provider returned an invalid image record", phase=phase)
    raw = cast(Mapping[object, object], image)
    if any(type(key) is not str for key in raw) or set(raw) != _IMAGE_KEYS:
        raise _provider_error("PROVIDER_PROTOCOL_INVALID", "provider returned an invalid image record", phase=phase)
    filename = raw["filename"]
    subfolder = raw["subfolder"]
    image_type = raw["type"]
    if type(filename) is not str or type(subfolder) is not str or type(image_type) is not str:
        raise _provider_error("PROVIDER_PROTOCOL_INVALID", "provider returned an invalid image record", phase=phase)
    unicode_invalid = False
    try:
        filename.encode("utf-8")
        subfolder.encode("utf-8")
        image_type.encode("utf-8")
    except UnicodeEncodeError:
        unicode_invalid = True
    filename_invalid = (
        not filename
        or filename in {".", ".."}
        or PurePosixPath(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    )
    subfolder_path = PurePosixPath(subfolder)
    subfolder_invalid = bool(subfolder) and (
        subfolder_path.is_absolute()
        or str(subfolder_path) != subfolder
        or "\\" in subfolder
        or any(part in {".", ".."} for part in subfolder_path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in subfolder)
    )
    if unicode_invalid or filename_invalid or subfolder_invalid or image_type not in _ALLOWED_IMAGE_TYPES:
        raise _provider_error("PROVIDER_PROTOCOL_INVALID", "provider returned an invalid image record", phase=phase)
    return {"filename": filename, "subfolder": subfolder, "type": image_type}


def comfyui_view_url(endpoint: str, image: Mapping[str, object]) -> str:
    validated = _validated_image_record(image, phase="retrieve")
    query = urllib.parse.urlencode(validated)
    return f"{_endpoint_url(endpoint, 'view')}?{query}"


class _HistoryKind(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class _ParsedHistory:
    kind: _HistoryKind
    outputs: tuple[JsonObject, ...]


def _invalid_history(message: str) -> NoReturn:
    raise _provider_error("PROVIDER_PROTOCOL_INVALID", message, phase="poll")


def _validate_status_payload(payload: object) -> None:
    if not isinstance(payload, Mapping):
        _invalid_history("provider returned invalid history messages")
    stack: list[tuple[object, int]] = [(payload, 0)]
    item_count = 0
    while stack:
        value, depth = stack.pop()
        item_count += 1
        if item_count > _MAX_STATUS_JSON_ITEMS or depth > _MAX_STATUS_JSON_DEPTH:
            _invalid_history("provider returned oversized history messages")
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            integer_invalid = False
            try:
                str(value)
            except ValueError:
                integer_invalid = True
            if integer_invalid:
                _invalid_history("provider returned invalid history messages")
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                _invalid_history("provider returned invalid history messages")
            continue
        if isinstance(value, str):
            if type(value) is not str:
                _invalid_history("provider returned invalid history messages")
            string_invalid = False
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                string_invalid = True
                encoded = b""
            if string_invalid or len(encoded) > _MAX_STATUS_STRING_BYTES:
                _invalid_history("provider returned invalid history messages")
            continue
        if isinstance(value, Mapping):
            for key, item in cast(Mapping[object, object], value).items():
                if type(key) is not str:
                    _invalid_history("provider returned invalid history messages")
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            stack.extend((item, depth + 1) for item in value)
            continue
        _invalid_history("provider returned invalid history messages")


def _validate_status_messages(value: object) -> None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        _invalid_history("provider returned invalid history messages")
    if len(value) > _MAX_STATUS_MESSAGES:
        _invalid_history("provider returned oversized history messages")
    for record in value:
        if not isinstance(record, Sequence) or isinstance(record, str | bytes | bytearray) or len(record) != 2:
            _invalid_history("provider returned invalid history messages")
        event = record[0]
        payload = record[1]
        if type(event) is not str or event not in _MESSAGE_EVENTS:
            _invalid_history("provider returned invalid history messages")
        _validate_status_payload(payload)


def _parse_history_item(history_item: Mapping[str, object]) -> _ParsedHistory:
    if any(type(key) is not str for key in history_item) or set(history_item) != {"status", "outputs"}:
        _invalid_history("provider returned invalid history item")
    raw_status = history_item["status"]
    if not isinstance(raw_status, Mapping):
        _invalid_history("provider returned invalid history status")
    status = cast(Mapping[object, object], raw_status)
    if any(type(key) is not str for key in status):
        _invalid_history("provider returned invalid history status")
    status_keys = set(status)
    if not _STATUS_REQUIRED_KEYS <= status_keys <= _STATUS_ALLOWED_KEYS:
        _invalid_history("provider returned invalid history status")
    status_str = status["status_str"]
    completed = status["completed"]
    if type(status_str) is not str or not isinstance(completed, bool):
        _invalid_history("provider returned invalid history status")
    if "messages" in status:
        _validate_status_messages(status["messages"])
    if status_str in _SUCCESS_STATUSES and completed is True:
        kind = _HistoryKind.SUCCESS
    elif status_str in _FAILURE_STATUSES:
        kind = _HistoryKind.FAILURE
    elif status_str in _PENDING_STATUSES and completed is False:
        kind = _HistoryKind.PENDING
    else:
        _invalid_history("provider returned invalid history status")
    raw_outputs = history_item["outputs"]
    if not isinstance(raw_outputs, Mapping):
        _invalid_history("provider returned invalid history outputs")
    if kind is not _HistoryKind.SUCCESS and raw_outputs:
        _invalid_history("provider returned inconsistent history")
    outputs: list[JsonObject] = []
    for node_id, node_output in cast(Mapping[object, object], raw_outputs).items():
        if type(node_id) is not str or _NODE_ID.fullmatch(node_id) is None:
            _invalid_history("provider returned invalid history outputs")
        if not isinstance(node_output, Mapping):
            _invalid_history("provider returned invalid history outputs")
        node = cast(Mapping[object, object], node_output)
        if any(type(key) is not str for key in node) or set(node) != {"images"}:
            _invalid_history("provider returned invalid history outputs")
        images = node["images"]
        if not isinstance(images, Sequence) or isinstance(images, str | bytes | bytearray):
            _invalid_history("provider returned invalid history images")
        for image in images:
            outputs.append(_validated_image_record(image, phase="poll"))
    return _ParsedHistory(kind, tuple(outputs))


def collect_outputs(history_item: Mapping[str, object]) -> tuple[JsonObject, ...]:
    """Validate a supported Comfy history variant and preserve output order."""

    invalid = False
    parsed: _ParsedHistory | None = None
    error: DrawingMachineError | None = None
    try:
        parsed = _parse_history_item(history_item)
    except DrawingMachineError as caught:
        error = caught
    except Exception:
        invalid = True
    if error is not None:
        raise error
    if invalid or parsed is None:
        raise _provider_error("PROVIDER_PROTOCOL_INVALID", "provider returned invalid history item", phase="poll")
    return parsed.outputs


def _image_is_valid(image_bytes: object) -> bool:
    if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= _MAX_IMAGE_RESPONSE_BYTES:
        return False
    invalid = False
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            image_format = opened.format
            width, height = opened.size
            if (
                image_format not in _ALLOWED_IMAGE_FORMATS
                or width <= 0
                or height <= 0
                or width > _MAX_IMAGE_DIMENSION
                or height > _MAX_IMAGE_DIMENSION
                or width * height > _MAX_IMAGE_PIXELS
            ):
                invalid = True
            opened.verify()
        if not invalid:
            with Image.open(io.BytesIO(image_bytes)) as loaded:
                loaded.load()
                if loaded.format not in _ALLOWED_IMAGE_FORMATS:
                    invalid = True
    except Exception:
        invalid = True
    return not invalid


@dataclass(slots=True)
class _PreparedState:
    identity: int
    input_bytes: bytes
    prompt_source: str
    prompt_record: ProviderRecord
    config_record: ProviderRecord
    created_at: float
    expires_at: float
    lifecycle: _PreparedLifecycle = field(default_factory=lambda: _PreparedLifecycle.PREPARED)


class _PreparedLifecycle(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"


@dataclass(frozen=True, slots=True)
class _Deadline:
    expires_at: float


@dataclass(slots=True)
class _SubmissionState:
    identity: int
    prepared: PreparedProviderRequest
    system_stats: JsonObject
    upload: JsonObject
    deadline: _Deadline
    lifecycle: _SubmissionLifecycle = field(default_factory=lambda: _SubmissionLifecycle.POLLABLE)
    history: JsonObject | None = None
    outputs: tuple[JsonObject, ...] = ()
    error: ErrorPayload | None = None


class _SubmissionLifecycle(StrEnum):
    POLLABLE = "POLLABLE"
    POLLING = "POLLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRIEVING = "RETRIEVING"


class LocalComfyUIProvider:
    def __init__(
        self,
        config: LocalComfyUIConfig,
        *,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, LocalComfyUIConfig):
            raise _configuration_error("config must be LocalComfyUIConfig", field="config")
        self._config = config
        if not callable(monotonic):
            raise _configuration_error("monotonic must be callable", field="monotonic")
        self._monotonic = monotonic
        self._transport = transport if transport is not None else UrllibHttpTransport(monotonic=monotonic)
        self._lock = threading.Lock()
        self._preparing: dict[str, object] = {}
        self._prepared: dict[str, _PreparedState] = {}
        self._submissions: dict[str, _SubmissionState] = {}

    @property
    def name(self) -> str:
        return self._config.name

    def validate_config(self) -> None:
        return None

    def _load_workflow(self) -> JsonObject:
        return cast(JsonObject, _plain_json(self._config._workflow_document))

    def _clock_now(self, *, phase: str, request_id: str) -> float:
        failed = False
        value: object = None
        try:
            value = self._monotonic()
        except Exception:
            failed = True
        if failed or isinstance(value, bool) or not isinstance(value, int | float):
            raise _provider_error(
                "PROVIDER_CLOCK_INVALID",
                "provider monotonic clock failed",
                phase=phase,
                request_id=request_id,
            )
        conversion_failed = False
        normalized = 0.0
        try:
            normalized = float(value)
        except (OverflowError, ValueError):
            conversion_failed = True
        if conversion_failed or not math.isfinite(normalized):
            raise _provider_error(
                "PROVIDER_CLOCK_INVALID",
                "provider monotonic clock failed",
                phase=phase,
                request_id=request_id,
            )
        return normalized

    def _remaining(self, deadline: _Deadline, *, phase: str, request_id: str) -> float:
        remaining = deadline.expires_at - self._clock_now(phase=phase, request_id=request_id)
        if remaining <= 0:
            raise _provider_error(
                "PROVIDER_TIMEOUT",
                "provider execution reached its configured deadline",
                phase=phase,
                request_id=request_id,
            )
        return remaining

    def _purge_expired_locked(
        self,
        now: float,
        *,
        preserve_request_id: str | None = None,
        preserve_job_id: str | None = None,
    ) -> None:
        expired_jobs = [
            job_id
            for job_id, state in self._submissions.items()
            if job_id != preserve_job_id and state.deadline.expires_at <= now
        ]
        for job_id in expired_jobs:
            state = self._submissions.pop(job_id)
            request_id = state.prepared.request_id
            prepared = self._prepared.get(request_id)
            if prepared is not None and prepared.lifecycle is _PreparedLifecycle.SUBMITTED:
                self._prepared.pop(request_id, None)

        submitted_request_ids = {state.prepared.request_id for state in self._submissions.values()}
        expired_prepared = [
            request_id
            for request_id, state in self._prepared.items()
            if request_id != preserve_request_id
            and state.lifecycle is _PreparedLifecycle.PREPARED
            and state.expires_at <= now
        ]
        orphaned_submitted = [
            request_id
            for request_id, state in self._prepared.items()
            if request_id != preserve_request_id
            and state.lifecycle is _PreparedLifecycle.SUBMITTED
            and request_id not in submitted_request_ids
        ]
        for request_id in (*expired_prepared, *orphaned_submitted):
            self._prepared.pop(request_id, None)

    def _capacity_error(self, *, phase: str, request_id: str) -> DrawingMachineError:
        return _provider_error(
            "PROVIDER_CAPACITY_EXCEEDED",
            "provider local state capacity is currently exhausted",
            phase=phase,
            request_id=request_id,
            retryable=True,
        )

    def _prepared_admission_count_locked(self) -> int:
        return len(self._preparing) + len(self._prepared)

    def _live_submission_count_locked(self) -> int:
        submitting = sum(state.lifecycle is _PreparedLifecycle.SUBMITTING for state in self._prepared.values())
        return len(self._submissions) + submitting

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, JsonValue] | None,
        timeout_seconds: float,
        deadline: _Deadline,
    ) -> JsonValue:
        if isinstance(self._transport, UrllibHttpTransport):
            return self._transport.request_json(
                method,
                url,
                payload=payload,
                timeout_seconds=timeout_seconds,
                absolute_deadline=deadline.expires_at,
            )
        return self._transport.request_json(
            method,
            url,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )

    def _upload_image(
        self,
        url: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        timeout_seconds: float,
        deadline: _Deadline,
    ) -> JsonValue:
        if isinstance(self._transport, UrllibHttpTransport):
            return self._transport.upload_image(
                url,
                filename=filename,
                content=content,
                media_type=media_type,
                timeout_seconds=timeout_seconds,
                absolute_deadline=deadline.expires_at,
            )
        return self._transport.upload_image(
            url,
            filename=filename,
            content=content,
            media_type=media_type,
            timeout_seconds=timeout_seconds,
        )

    def _request_bytes(self, url: str, *, timeout_seconds: float, deadline: _Deadline) -> bytes:
        if isinstance(self._transport, UrllibHttpTransport):
            return self._transport.request_bytes(
                url,
                timeout_seconds=timeout_seconds,
                absolute_deadline=deadline.expires_at,
            )
        return self._transport.request_bytes(url, timeout_seconds=timeout_seconds)

    def _transport_call_error(self, deadline: _Deadline, *, phase: str, request_id: str) -> DrawingMachineError:
        deadline_error: DrawingMachineError | None = None
        try:
            self._remaining(deadline, phase=phase, request_id=request_id)
        except DrawingMachineError as error:
            deadline_error = error
        if deadline_error is not None:
            return deadline_error
        return _provider_error(
            "PROVIDER_TRANSPORT_FAILED",
            f"provider transport failed during {phase}",
            phase=phase,
            request_id=request_id,
            retryable=True,
        )

    def create_request(self, request: ProviderRequestV1) -> PreparedProviderRequest:
        if not isinstance(request, ProviderRequestV1):
            raise _provider_error("PROVIDER_REQUEST_INVALID", "provider request is invalid", phase="create_request")
        created_at = self._clock_now(phase="create_request", request_id=request.request_id)
        reservation = object()
        with self._lock:
            self._purge_expired_locked(created_at)
            if request.request_id in self._preparing or request.request_id in self._prepared:
                raise _provider_error(
                    "PROVIDER_REQUEST_DUPLICATE",
                    "provider request identity has already been prepared",
                    phase="create_request",
                    request_id=request.request_id,
                )
            if self._prepared_admission_count_locked() >= MAX_PREPARED_ENTRIES:
                raise self._capacity_error(phase="create_request", request_id=request.request_id)
            self._preparing[request.request_id] = reservation

        typed_error: DrawingMachineError | None = None
        unexpected_failure = False
        prepared: PreparedProviderRequest | None = None
        try:
            prepared = self._create_reserved_request(request, reservation)
        except DrawingMachineError as caught:
            typed_error = caught
        except Exception:
            unexpected_failure = True
        if typed_error is not None:
            self._release_prepared_reservation(request.request_id, reservation)
            raise typed_error
        if unexpected_failure or prepared is None:
            self._release_prepared_reservation(request.request_id, reservation)
            raise _provider_error(
                "PROVIDER_REQUEST_INVALID",
                "provider request preparation failed",
                phase="create_request",
                request_id=request.request_id,
            )
        return prepared

    def _create_reserved_request(
        self,
        request: ProviderRequestV1,
        reservation: object,
    ) -> PreparedProviderRequest:
        self.validate_config()
        input_path = Path(request.input_path)
        input_invalid = False
        input_bytes = b""
        try:
            if input_path.is_symlink() or not input_path.is_file():
                raise OSError("not a regular file")
            if input_path.stat().st_size > MAX_INPUT_IMAGE_BYTES:
                raise OSError("input exceeds local admission limit")
            with input_path.open("rb") as input_stream:
                input_bytes = input_stream.read(MAX_INPUT_IMAGE_BYTES + 1)
            if not input_bytes or len(input_bytes) > MAX_INPUT_IMAGE_BYTES:
                raise OSError("input exceeds local admission limit")
        except (OSError, ValueError):
            input_invalid = True
        if input_invalid:
            raise _provider_error(
                "PROVIDER_INPUT_INVALID",
                "provider input could not be read",
                phase="create_request",
                request_id=request.request_id,
            )
        if hashlib.sha256(input_bytes).hexdigest() != request.input_sha256:
            raise _provider_error(
                "PROVIDER_INPUT_DIGEST_MISMATCH",
                "provider input digest does not match request",
                phase="create_request",
                request_id=request.request_id,
            )
        workflow = self._load_workflow()
        prompt_node = self._config.workflow_nodes["prompt"]
        prompt_inputs = cast(JsonObject, cast(JsonObject, workflow[prompt_node])["inputs"])
        template_prompt = cast(str, prompt_inputs["prompt"])
        prompt_text = template_prompt if request.prompt is None else request.prompt
        prompt_source = "workflow_template_default" if request.prompt is None else "explicit_prompt_argument"
        if request.prompt is not None:
            prompt_inputs["prompt"] = request.prompt
        upload_name = input_path.name
        cast(JsonObject, cast(JsonObject, workflow[_HARD_LOAD_NODE])["inputs"])["image"] = upload_name
        sampler = cast(JsonObject, cast(JsonObject, workflow[_HARD_SAMPLER_NODE])["inputs"])
        sampler["seed"] = int(request.input_sha256[:13], 16)
        for key in ("steps", "cfg", "denoise"):
            configured = self._config.sampler_defaults[key]
            if configured is not None:
                sampler[key] = configured
        cast(JsonObject, cast(JsonObject, workflow[_HARD_SAVE_NODE])["inputs"])["filename_prefix"] = (
            request.output_prefix
        )
        cast(JsonObject, cast(JsonObject, workflow[_HARD_SCALE_NODE])["inputs"])["scale_to_length"] = (
            self._config.scale_to_length
        )

        prompt_record = ProviderRecord(
            role="provider.prompt",
            relative_path="qwen_image_edit_prompt.txt",
            media_type="text/plain; charset=utf-8",
            content=prompt_text.encode("utf-8"),
        )
        config_record = ProviderRecord(
            role="provider.config",
            relative_path="local_comfyui_provider_config.json",
            media_type="application/json",
            content=_json_bytes(self._legacy_config_record()),
        )
        request_record = ProviderRecord(
            role="provider.request",
            relative_path="qwen_image_edit_request.json",
            media_type="application/json",
            content=_json_bytes(
                self._legacy_request_record(
                    request,
                    input_path=input_path,
                    input_bytes=input_bytes,
                    prompt_record=prompt_record,
                    prompt_source=prompt_source,
                )
            ),
        )
        prepared = PreparedProviderRequest(1, request.request_id, workflow, upload_name, request_record)
        stored_at = self._clock_now(phase="create_request", request_id=request.request_id)
        state = _PreparedState(
            identity=id(prepared),
            input_bytes=input_bytes,
            prompt_source=prompt_source,
            prompt_record=prompt_record,
            config_record=config_record,
            created_at=stored_at,
            expires_at=stored_at + PREPARED_TTL_SECONDS,
        )
        with self._lock:
            self._purge_expired_locked(stored_at)
            if self._preparing.get(request.request_id) is not reservation or request.request_id in self._prepared:
                raise _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "provider request admission is no longer owned by this caller",
                    phase="create_request",
                    request_id=request.request_id,
                )
            self._preparing.pop(request.request_id)
            self._prepared[request.request_id] = state
        return prepared

    def _release_prepared_reservation(self, request_id: str, reservation: object) -> None:
        with self._lock:
            if self._preparing.get(request_id) is reservation:
                self._preparing.pop(request_id, None)

    def _legacy_config_record(self) -> JsonObject:
        return {
            "schema": "cnc_image_edit_provider_config_v2",
            "provider": "local-comfyui",
            "endpoint": self._config.endpoint,
            "workflow_template": self._config.workflow_template.name,
            "model_family": self._config.model_family,
            "scale_to_length": self._config.scale_to_length,
            "timeout_s": self._config.timeout_seconds,
            "poll_interval_s": self._config.poll_interval_seconds,
            "free_after_run": self._config.free_after_run,
            "workflow_nodes": dict(self._config.workflow_nodes),
            "sampler_defaults": cast(JsonObject, _plain_json(self._config.sampler_defaults)),
            "live_execution_requires_execute_flag": self._config.live_execution_requires_execute_flag,
        }

    def _legacy_request_record(
        self,
        request: ProviderRequestV1,
        *,
        input_path: Path,
        input_bytes: bytes,
        prompt_record: ProviderRecord,
        prompt_source: str,
    ) -> JsonObject:
        media_type = mimetypes.guess_type(input_path.name)[0] or "application/octet-stream"
        return {
            "schema": "local_comfyui_qwen_image_edit_request_record_v1",
            "created_at": _now_iso(),
            "provider": "local-comfyui",
            "provider_config": "local_comfyui_provider_config.json",
            "endpoint": self._config.endpoint,
            "workflow_template": self._config.workflow_template.name,
            "workflow_nodes": dict(self._config.workflow_nodes),
            "source_image": {
                "kind": "local_file",
                "value": request.input_path,
                "copied_input": request.input_path,
                "name": input_path.name,
                "sha256": request.input_sha256,
                "size_bytes": len(input_bytes),
                "mime_type": media_type,
                "size_px": None,
                "mode": None,
            },
            "prompt": {
                "path": prompt_record.relative_path,
                "sha256": hashlib.sha256(prompt_record.content).hexdigest(),
                "source": prompt_source,
                "workflow_node": self._config.workflow_nodes["prompt"],
            },
            "scale_to_side": "longest",
            "scale_to_length": self._config.scale_to_length,
            "sampler": {
                "seed": int(request.input_sha256[:13], 16),
                "steps": self._config.sampler_defaults["steps"],
                "cfg": self._config.sampler_defaults["cfg"],
                "denoise": self._config.sampler_defaults["denoise"],
            },
            "output_prefix": request.output_prefix,
            "dry_run": False,
            "execute": True,
            "timeout_s": self._config.timeout_seconds,
        }

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        now = self._clock_now(phase="submit", request_id=request.request_id)
        with self._lock:
            self._purge_expired_locked(now)
            state = self._prepared.get(request.request_id)
            if state is None or state.identity != id(request):
                raise _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "prepared request identity is not owned by this provider",
                    phase="submit",
                    request_id=request.request_id,
                )
            if state.lifecycle is not _PreparedLifecycle.PREPARED:
                raise _provider_error(
                    "PROVIDER_REQUEST_DUPLICATE",
                    "prepared request identity has already been submitted",
                    phase="submit",
                    request_id=request.request_id,
                )
            if self._live_submission_count_locked() >= MAX_LIVE_SUBMISSIONS:
                raise self._capacity_error(phase="submit", request_id=request.request_id)
            state.lifecycle = _PreparedLifecycle.SUBMITTING
        setup_error: DrawingMachineError | None = None
        setup_failed = False
        try:
            deadline = _Deadline(
                self._clock_now(phase="submit", request_id=request.request_id) + self._config.timeout_seconds
            )
            system_stats_timeout = min(
                30.0,
                self._remaining(deadline, phase="submit", request_id=request.request_id),
            )
        except DrawingMachineError as error:
            setup_error = error
        except Exception:
            setup_failed = True
        if setup_error is not None:
            self._discard_prepared(request.request_id, state)
            raise setup_error
        if setup_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        transport_failed = False
        protocol_failed = False
        system_stats_value: JsonValue = None
        validation_error: DrawingMachineError | None = None
        validation_failed = False
        try:
            system_stats_value = self._request_json(
                "GET",
                _endpoint_url(self._config.endpoint, "system_stats"),
                payload=None,
                timeout_seconds=system_stats_timeout,
                deadline=deadline,
            )
        except (_ResponseProtocolError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            protocol_failed = True
        except Exception:
            transport_failed = True
        if protocol_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        if transport_failed:
            failure = self._transport_call_error(deadline, phase="submit", request_id=request.request_id)
            if failure.payload.code == "PROVIDER_TIMEOUT":
                self._discard_prepared(request.request_id, state)
            elif not self._rollback_submit(request.request_id, state):
                failure = self._submit_identity_error(request.request_id)
            raise failure
        try:
            self._remaining(deadline, phase="submit", request_id=request.request_id)
            system_stats = _json_object(system_stats_value, phase="submit", request_id=request.request_id)
            upload_timeout = self._remaining(deadline, phase="submit", request_id=request.request_id)
        except DrawingMachineError as error:
            self._discard_prepared(request.request_id, state)
            raise error
        media_type = mimetypes.guess_type(request.upload_name)[0] or "application/octet-stream"
        transport_failed = False
        protocol_failed = False
        upload_value: JsonValue = None
        try:
            upload_value = self._upload_image(
                _endpoint_url(self._config.endpoint, "upload", "image"),
                filename=request.upload_name,
                content=state.input_bytes,
                media_type=media_type,
                timeout_seconds=upload_timeout,
                deadline=deadline,
            )
        except (_ResponseProtocolError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            protocol_failed = True
        except Exception:
            transport_failed = True
        if protocol_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        if transport_failed:
            failure = self._transport_call_error(deadline, phase="submit", request_id=request.request_id)
            if failure.payload.code == "PROVIDER_TIMEOUT":
                self._discard_prepared(request.request_id, state)
            elif not self._rollback_submit(request.request_id, state):
                failure = self._submit_identity_error(request.request_id)
            raise failure
        try:
            self._remaining(deadline, phase="submit", request_id=request.request_id)
            upload = _json_object(upload_value, phase="submit", request_id=request.request_id)
            uploaded_name = upload.get("name", request.upload_name)
            if type(uploaded_name) is not str:
                raise _provider_error(
                    "PROVIDER_PROTOCOL_INVALID",
                    "provider returned an invalid response",
                    phase="submit",
                    request_id=request.request_id,
                )
            validated_upload = _validated_image_record(
                {"filename": uploaded_name, "subfolder": "", "type": "output"},
                phase="submit",
            )
            workflow = cast(JsonObject, _plain_json(request.workflow))
            cast(JsonObject, cast(JsonObject, workflow[_HARD_LOAD_NODE])["inputs"])["image"] = validated_upload[
                "filename"
            ]
            submit_timeout = min(
                30.0,
                self._remaining(deadline, phase="submit", request_id=request.request_id),
            )
        except DrawingMachineError as error:
            validation_error = error
        except Exception:
            validation_failed = True
        if validation_error is not None:
            self._discard_prepared(request.request_id, state)
            raise validation_error
        if validation_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        transport_failed = False
        protocol_failed = False
        submit_value: JsonValue = None
        submission_error: DrawingMachineError | None = None
        submission_failed = False
        try:
            submit_value = self._request_json(
                "POST",
                _endpoint_url(self._config.endpoint, "prompt"),
                payload={"prompt": workflow, "client_id": "drawingmachine-local-comfyui"},
                timeout_seconds=submit_timeout,
                deadline=deadline,
            )
        except (_ResponseProtocolError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            protocol_failed = True
        except Exception:
            transport_failed = True
        if protocol_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        if transport_failed:
            failure = self._transport_call_error(deadline, phase="submit", request_id=request.request_id)
            if failure.payload.code == "PROVIDER_TIMEOUT":
                self._discard_prepared(request.request_id, state)
            elif not self._rollback_submit(request.request_id, state):
                failure = self._submit_identity_error(request.request_id)
            raise failure
        try:
            self._remaining(deadline, phase="submit", request_id=request.request_id)
            submitted = _json_object(submit_value, phase="submit", request_id=request.request_id)
            self._remaining(deadline, phase="submit", request_id=request.request_id)
            provider_job_id = submitted.get("prompt_id")
            if type(provider_job_id) is not str or not provider_job_id or "\x00" in provider_job_id:
                raise _provider_error(
                    "PROVIDER_PROTOCOL_INVALID",
                    "provider returned an invalid response",
                    phase="submit",
                    request_id=request.request_id,
                )
            submission = ProviderSubmission(1, request.request_id, provider_job_id)
        except DrawingMachineError as error:
            submission_error = error
        except Exception:
            submission_failed = True
        if submission_error is not None:
            self._discard_prepared(request.request_id, state)
            raise submission_error
        if submission_failed:
            self._discard_prepared(request.request_id, state)
            raise _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid response",
                phase="submit",
                request_id=request.request_id,
            )
        provider_job_id = cast(str, provider_job_id)
        with self._lock:
            if (
                self._prepared.get(request.request_id) is not state
                or state.lifecycle is not _PreparedLifecycle.SUBMITTING
            ):
                raise _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "prepared request identity is no longer owned by this provider",
                    phase="submit",
                    request_id=request.request_id,
                )
            if provider_job_id in self._submissions:
                self._prepared.pop(request.request_id, None)
                raise _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "provider returned a duplicate job identity",
                    phase="submit",
                    request_id=request.request_id,
                )
            state.lifecycle = _PreparedLifecycle.SUBMITTED
            self._submissions[provider_job_id] = _SubmissionState(
                identity=id(submission),
                prepared=request,
                system_stats=system_stats,
                upload=upload,
                deadline=deadline,
            )
        return submission

    def _rollback_submit(self, request_id: str, state: _PreparedState) -> bool:
        with self._lock:
            if self._prepared.get(request_id) is state and state.lifecycle is _PreparedLifecycle.SUBMITTING:
                state.lifecycle = _PreparedLifecycle.PREPARED
                return True
        return False

    def _submit_identity_error(self, request_id: str) -> DrawingMachineError:
        return _provider_error(
            "PROVIDER_IDENTITY_MISMATCH",
            "prepared request identity is no longer owned by this provider",
            phase="submit",
            request_id=request_id,
        )

    def _discard_prepared(self, request_id: str, state: _PreparedState) -> None:
        with self._lock:
            if self._prepared.get(request_id) is state:
                self._prepared.pop(request_id, None)

    def _submission_state_locked(self, submission: ProviderSubmission, phase: str) -> _SubmissionState:
        state = self._submissions.get(submission.provider_job_id)
        if state is None or state.identity != id(submission) or state.prepared.request_id != submission.request_id:
            raise _provider_error(
                "PROVIDER_IDENTITY_MISMATCH",
                "provider submission identity is not owned by this provider",
                phase=phase,
                request_id=submission.request_id,
            )
        return state

    def _owns_submission_state_locked(self, state: _SubmissionState) -> bool:
        return any(candidate is state for candidate in self._submissions.values())

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        now = self._clock_now(phase="poll", request_id=submission.request_id)
        with self._lock:
            self._purge_expired_locked(now, preserve_job_id=submission.provider_job_id)
            state = self._submission_state_locked(submission, "poll")
            if state.lifecycle is _SubmissionLifecycle.SUCCEEDED:
                return ProviderPollResult(1, ProviderPollState.SUCCEEDED, None, None)
            if state.lifecycle is _SubmissionLifecycle.FAILED:
                assert state.error is not None
                return ProviderPollResult(1, ProviderPollState.FAILED, None, state.error)
            if state.lifecycle is not _SubmissionLifecycle.POLLABLE:
                raise _provider_error(
                    "PROVIDER_REQUEST_DUPLICATE",
                    "provider submission already has an operation in progress",
                    phase="poll",
                    request_id=submission.request_id,
                )
            state.lifecycle = _SubmissionLifecycle.POLLING
        protocol_failed = False
        protocol_error: DrawingMachineError | None = None
        remaining = 0.0
        try:
            call_timeout = min(
                30.0,
                self._remaining(state.deadline, phase="poll", request_id=submission.request_id),
            )
            transport_failed = False
            response_protocol_failed = False
            history_value: JsonValue = None
            try:
                history_value = self._request_json(
                    "GET",
                    _endpoint_url(self._config.endpoint, "history", submission.provider_job_id),
                    payload=None,
                    timeout_seconds=call_timeout,
                    deadline=state.deadline,
                )
            except (_ResponseProtocolError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                response_protocol_failed = True
            except Exception:
                transport_failed = True
            if response_protocol_failed:
                raise _provider_error(
                    "PROVIDER_PROTOCOL_INVALID",
                    "provider returned an invalid response",
                    phase="poll",
                    request_id=submission.request_id,
                )
            if transport_failed:
                transport_error = self._transport_call_error(
                    state.deadline,
                    phase="poll",
                    request_id=submission.request_id,
                )
                return self._fail_poll(state, transport_error.payload)
            self._remaining(state.deadline, phase="poll", request_id=submission.request_id)
            history_root = _json_object(history_value, phase="poll", request_id=submission.request_id)
            self._remaining(state.deadline, phase="poll", request_id=submission.request_id)
            if history_root and set(history_root) != {submission.provider_job_id}:
                raise _provider_error(
                    "PROVIDER_PROTOCOL_INVALID",
                    "provider returned invalid history response",
                    phase="poll",
                    request_id=submission.request_id,
                )
            if submission.provider_job_id in history_root:
                history_value = history_root[submission.provider_job_id]
                history = _json_object(history_value, phase="poll", request_id=submission.request_id)
                parsed = _parse_history_item(history)
                self._remaining(state.deadline, phase="poll", request_id=submission.request_id)
                if parsed.kind is _HistoryKind.FAILURE:
                    execution_error = _provider_error(
                        "PROVIDER_EXECUTION_FAILED",
                        "provider reported execution failure",
                        phase="poll",
                        request_id=submission.request_id,
                    ).payload
                    return self._fail_poll(state, execution_error)
                if parsed.kind is _HistoryKind.PENDING:
                    remaining = self._remaining(
                        state.deadline,
                        phase="poll",
                        request_id=submission.request_id,
                    )
                    return self._pending_poll(state, remaining)
                if not parsed.outputs:
                    output_error = _provider_error(
                        "PROVIDER_OUTPUT_MISSING",
                        "provider completed without an output image",
                        phase="poll",
                        request_id=submission.request_id,
                    ).payload
                    return self._fail_poll(state, output_error)
                with self._lock:
                    if (
                        not self._owns_submission_state_locked(state)
                        or state.lifecycle is not _SubmissionLifecycle.POLLING
                    ):
                        raise _provider_error(
                            "PROVIDER_IDENTITY_MISMATCH",
                            "provider submission state changed unexpectedly",
                            phase="poll",
                            request_id=submission.request_id,
                        )
                    state.history = history
                    state.outputs = parsed.outputs
                    state.lifecycle = _SubmissionLifecycle.SUCCEEDED
                return ProviderPollResult(1, ProviderPollState.SUCCEEDED, None, None)
            remaining = self._remaining(state.deadline, phase="poll", request_id=submission.request_id)
        except DrawingMachineError as error:
            protocol_error = error
        except Exception:
            protocol_failed = True
        if protocol_error is not None:
            return self._fail_poll(state, protocol_error.payload)
        if protocol_failed:
            fresh_error = _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned invalid history response",
                phase="poll",
                request_id=submission.request_id,
            )
            return self._fail_poll(state, fresh_error.payload)
        return self._pending_poll(state, remaining)

    def _pending_poll(self, state: _SubmissionState, remaining: float) -> ProviderPollResult:
        with self._lock:
            if not self._owns_submission_state_locked(state) or state.lifecycle is not _SubmissionLifecycle.POLLING:
                raise _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "provider submission state changed unexpectedly",
                    phase="poll",
                    request_id=state.prepared.request_id,
                )
            state.lifecycle = _SubmissionLifecycle.POLLABLE
        return ProviderPollResult(
            1,
            ProviderPollState.PENDING,
            min(self._config.poll_interval_seconds, remaining),
            None,
        )

    def _fail_poll(self, state: _SubmissionState, error: ErrorPayload) -> ProviderPollResult:
        with self._lock:
            if not self._owns_submission_state_locked(state):
                error = _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "provider submission identity is no longer owned by this provider",
                    phase="poll",
                    request_id=state.prepared.request_id,
                ).payload
            elif state.lifecycle is _SubmissionLifecycle.POLLING:
                if error.retryable:
                    state.error = None
                    state.lifecycle = _SubmissionLifecycle.POLLABLE
                else:
                    state.error = error
                    state.lifecycle = _SubmissionLifecycle.FAILED
        return ProviderPollResult(1, ProviderPollState.FAILED, None, error)

    def retrieve(self, submission: ProviderSubmission) -> ProviderResultV1:
        now = self._clock_now(phase="retrieve", request_id=submission.request_id)
        with self._lock:
            self._purge_expired_locked(now, preserve_job_id=submission.provider_job_id)
            state = self._submission_state_locked(submission, "retrieve")
            if state.lifecycle is _SubmissionLifecycle.FAILED:
                assert state.error is not None
                result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), state.error)
                self._finish_submission_locked(submission)
                return result
            if state.lifecycle is _SubmissionLifecycle.POLLABLE:
                error = _provider_error(
                    "PROVIDER_NOT_READY",
                    "provider result is not ready",
                    phase="retrieve",
                    request_id=submission.request_id,
                ).payload
                return ProviderResultV1(1, ProviderStatus.BLOCKED, None, (), error)
            if state.lifecycle is not _SubmissionLifecycle.SUCCEEDED:
                raise _provider_error(
                    "PROVIDER_REQUEST_DUPLICATE",
                    "provider submission already has an operation in progress",
                    phase="retrieve",
                    request_id=submission.request_id,
                )
            state.lifecycle = _SubmissionLifecycle.RETRIEVING
            first_output = state.outputs[0]
        try:
            image_timeout = self._remaining(
                state.deadline,
                phase="retrieve",
                request_id=submission.request_id,
            )
        except DrawingMachineError as error:
            result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), error.payload)
            self._finish_submission(submission)
            return result
        transport_failed = False
        image: bytes = b""
        try:
            image = self._request_bytes(
                comfyui_view_url(self._config.endpoint, first_output),
                timeout_seconds=image_timeout,
                deadline=state.deadline,
            )
        except Exception:
            transport_failed = True
        if transport_failed:
            transport_error = self._transport_call_error(
                state.deadline,
                phase="retrieve",
                request_id=submission.request_id,
            )
            result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), transport_error.payload)
            if transport_error.payload.code == "PROVIDER_TIMEOUT":
                self._finish_submission(submission)
            else:
                with self._lock:
                    if self._owns_submission_state_locked(state) and state.lifecycle is _SubmissionLifecycle.RETRIEVING:
                        state.lifecycle = _SubmissionLifecycle.SUCCEEDED
                    else:
                        identity_error = _provider_error(
                            "PROVIDER_IDENTITY_MISMATCH",
                            "provider submission identity is no longer owned by this provider",
                            phase="retrieve",
                            request_id=submission.request_id,
                        ).payload
                        result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), identity_error)
            return result
        try:
            self._remaining(state.deadline, phase="retrieve", request_id=submission.request_id)
        except DrawingMachineError as error:
            result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), error.payload)
            self._finish_submission(submission)
            return result
        if not _image_is_valid(image):
            image_error = _provider_error(
                "PROVIDER_PROTOCOL_INVALID",
                "provider returned an invalid image",
                phase="retrieve",
                request_id=submission.request_id,
            ).payload
            result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), image_error)
            self._finish_submission(submission)
            return result
        try:
            self._remaining(state.deadline, phase="retrieve", request_id=submission.request_id)
        except DrawingMachineError as error:
            result = ProviderResultV1(1, ProviderStatus.FAILED, None, (), error.payload)
            self._finish_submission(submission)
            return result
        free_requested = False
        if self._config.free_after_run:
            try:
                free_timeout = min(
                    10.0,
                    self._remaining(
                        state.deadline,
                        phase="retrieve",
                        request_id=submission.request_id,
                    ),
                )
            except DrawingMachineError:
                free_timeout = None
            if free_timeout is not None:
                try:
                    self._request_json(
                        "POST",
                        _endpoint_url(self._config.endpoint, "free"),
                        payload={"unload_models": True, "free_memory": True},
                        timeout_seconds=free_timeout,
                        deadline=state.deadline,
                    )
                    free_requested = True
                except Exception:
                    free_requested = False
        with self._lock:
            prepared_state = self._prepared.get(submission.request_id)
            if prepared_state is None or state.lifecycle is not _SubmissionLifecycle.RETRIEVING:
                identity_error = _provider_error(
                    "PROVIDER_IDENTITY_MISMATCH",
                    "provider submission state changed unexpectedly",
                    phase="retrieve",
                    request_id=submission.request_id,
                ).payload
                return ProviderResultV1(1, ProviderStatus.FAILED, None, (), identity_error)
        response_record = self._response_record(submission, state, free_requested=free_requested)
        handoff_record = self._handoff_record(submission, state, free_requested=free_requested, image=image)
        records = (
            prepared_state.prompt_record,
            prepared_state.config_record,
            state.prepared.record,
            response_record,
            handoff_record,
        )
        result = ProviderResultV1(1, ProviderStatus.SUCCEEDED, image, records, None)
        self._finish_submission(submission)
        return result

    def _finish_submission(self, submission: ProviderSubmission) -> None:
        with self._lock:
            self._finish_submission_locked(submission)

    def _finish_submission_locked(self, submission: ProviderSubmission) -> None:
        state = self._submissions.get(submission.provider_job_id)
        if state is None or state.identity != id(submission) or state.prepared.request_id != submission.request_id:
            return
        self._submissions.pop(submission.provider_job_id, None)
        prepared = self._prepared.get(submission.request_id)
        if prepared is not None and prepared.lifecycle is _PreparedLifecycle.SUBMITTED:
            self._prepared.pop(submission.request_id, None)

    def release_local_state(self, identity: PreparedProviderRequest | ProviderSubmission) -> bool:
        """Idempotently abandon local adapter state without cancelling any remote job."""

        if not isinstance(identity, PreparedProviderRequest | ProviderSubmission):
            raise _provider_error(
                "PROVIDER_REQUEST_INVALID",
                "local provider identity is invalid",
                phase="release_local_state",
            )
        request_id = identity.request_id
        now = self._clock_now(phase="release_local_state", request_id=request_id)
        with self._lock:
            self._purge_expired_locked(now)
            if isinstance(identity, ProviderSubmission):
                state = self._submissions.get(identity.provider_job_id)
                if state is None or state.identity != id(identity) or state.prepared.request_id != identity.request_id:
                    return False
                self._submissions.pop(identity.provider_job_id, None)
                prepared = self._prepared.get(identity.request_id)
                if prepared is not None and prepared.lifecycle is _PreparedLifecycle.SUBMITTED:
                    self._prepared.pop(identity.request_id, None)
                return True

            prepared = self._prepared.get(identity.request_id)
            if prepared is None or prepared.identity != id(identity):
                return False
            self._prepared.pop(identity.request_id, None)
            linked_jobs = [
                job_id
                for job_id, state in self._submissions.items()
                if state.prepared.request_id == identity.request_id
            ]
            for job_id in linked_jobs:
                self._submissions.pop(job_id, None)
            return True

    def _response_record(
        self,
        submission: ProviderSubmission,
        state: _SubmissionState,
        *,
        free_requested: bool,
    ) -> ProviderRecord:
        return ProviderRecord(
            "provider.response",
            "qwen_image_edit_response.json",
            "application/json",
            _json_bytes(
                {
                    "schema": "local_comfyui_qwen_image_edit_response_record_v1",
                    "created_at": _now_iso(),
                    "status": "PROCESSED_IMAGE_CREATED",
                    "system_stats": state.system_stats,
                    "upload": state.upload,
                    "prompt_id": submission.provider_job_id,
                    "history": state.history,
                    "outputs": list(state.outputs),
                    "processed_image": "processed.png",
                    "free_requested": free_requested,
                }
            ),
        )

    def _handoff_record(
        self,
        submission: ProviderSubmission,
        state: _SubmissionState,
        *,
        free_requested: bool,
        image: bytes,
    ) -> ProviderRecord:
        prepared = self._prepared[submission.request_id]
        return ProviderRecord(
            "provider.handoff",
            "drawable_handoff.json",
            "application/json",
            _json_bytes(
                {
                    "schema": "cnc_drawable_workflow_handoff_v1",
                    "status": "PROCESSED_IMAGE_CREATED",
                    "created_at": _now_iso(),
                    "job_name": json.loads(state.prepared.record.content)["output_prefix"],
                    "stage": "qwen_image_edit_processed_image",
                    "provider": {
                        "name": "local-comfyui",
                        "endpoint": self._config.endpoint,
                        "workflow_template": self._config.workflow_template.name,
                        "workflow_nodes": dict(self._config.workflow_nodes),
                        "prompt_id": submission.provider_job_id,
                        "scale_to_length": self._config.scale_to_length,
                        "free_requested": free_requested,
                    },
                    "source_image": json.loads(state.prepared.record.content)["source_image"],
                    "prompt": {
                        "path": prepared.prompt_record.relative_path,
                        "sha256": hashlib.sha256(prepared.prompt_record.content).hexdigest(),
                        "source": prepared.prompt_source,
                        "workflow_node": self._config.workflow_nodes["prompt"],
                    },
                    "artifacts": {
                        "out_dir": ".",
                        "request_record": state.prepared.record.relative_path,
                        "response_record": "qwen_image_edit_response.json",
                        "processed_image": "processed.png",
                        "processed_image_sha256": hashlib.sha256(image).hexdigest(),
                    },
                    "dry_run": False,
                    "execute": True,
                    "next_allowed_stage": "review_processed_image",
                    "review_gate": {"required": True, "status": "pending"},
                }
            ),
        )
