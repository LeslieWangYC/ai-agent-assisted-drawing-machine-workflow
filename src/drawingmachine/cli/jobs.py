from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from drawingmachine.cli.client import ServiceClient
from drawingmachine.config import resolve_xdg_paths
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse

_TERMINAL_STATES = frozenset({"READY_TO_RUN", "BLOCKED", "FAILED", "CANCELLED", "RECOVERY_REQUIRED", "COMPLETED"})


class ServiceCaller(Protocol):
    def call(
        self,
        command: str,
        arguments: JsonObject,
        *,
        timeout_s: float | None = None,
    ) -> ProtocolResponse: ...


def job_status(args: argparse.Namespace) -> ProtocolResponse:
    return _client().call("job.status", _job_arguments(args.job_id))


def cancel_job(args: argparse.Namespace) -> ProtocolResponse:
    return _client().call("job.cancel", _job_arguments(args.job_id))


def wait_for_job(
    args: argparse.Namespace,
    *,
    client: ServiceCaller | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> ProtocolResponse:
    invalid = validate_wait_arguments(args)
    if invalid is not None:
        return invalid
    interval = args.poll_interval_seconds
    if deadline is None:
        deadline = monotonic() + args.timeout_seconds
    service = _client() if client is None else client
    arguments = _job_arguments(args.job_id)
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _wait_timeout(args.job_id)
            try:
                response = service.call("job.status", arguments, timeout_s=remaining)
            except DrawingMachineError:
                if monotonic() >= deadline:
                    return _wait_timeout(args.job_id)
                raise
            now = monotonic()
            if now >= deadline:
                return _wait_timeout(args.job_id)
            public = replace(response, command="job.wait")
            if not response.ok:
                return public
            job = response.data.get("job")
            state = job.get("state") if isinstance(job, dict) else None
            if state in _TERMINAL_STATES:
                return public
            sleep(min(interval, deadline - now))
    except KeyboardInterrupt:
        return _local_failure(
            "job.wait",
            "JOB_WAIT_INTERRUPTED",
            "job wait was interrupted",
            job_id=args.job_id,
        )


def _wait_timeout(job_id: str) -> ProtocolResponse:
    return _local_failure(
        "job.wait",
        "JOB_WAIT_TIMEOUT",
        "timed out while waiting for job",
        job_id=job_id,
    )


def _client() -> ServiceClient:
    return ServiceClient(resolve_xdg_paths().socket_file)


def _job_arguments(job_id: str) -> JsonObject:
    return {"schema_version": 1, "job_id": job_id}


def _positive_finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and value > 0


def validate_wait_arguments(args: argparse.Namespace, *, command: str = "job.wait") -> ProtocolResponse | None:
    if _positive_finite(args.timeout_seconds) and _positive_finite(args.poll_interval_seconds):
        return None
    return _local_failure(
        command,
        "JOB_WAIT_ARGUMENT_INVALID",
        "wait timeout and poll interval must be finite and greater than zero",
    )


def _local_failure(command: str, code: str, message: str, *, job_id: str | None = None) -> ProtocolResponse:
    request_id = str(uuid4())
    return ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        False,
        command,
        request_id,
        {},
        ErrorPayload(code, ErrorCategory.INPUT, message, False, {}, request_id=request_id, job_id=job_id),
    )


__all__ = ["cancel_job", "job_status", "validate_wait_arguments", "wait_for_job"]
