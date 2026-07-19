from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn, cast
from uuid import uuid4

from drawingmachine.cli.client import ServiceClient, ServiceDispatchError
from drawingmachine.cli.jobs import ServiceCaller, validate_wait_arguments, wait_for_job
from drawingmachine.config import XdgPaths, resolve_xdg_paths
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.artifacts import ArtifactReader
from drawingmachine.ports.client_data import ClientInputStagingPort, StagingClock
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse

_MAX_STATIC_RESULT_BYTES = 1_048_576


def check_gcode(
    args: argparse.Namespace,
    *,
    client: ServiceCaller | None = None,
    artifact_reader: ArtifactReader | None = None,
    client_staging: ClientInputStagingPort | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProtocolResponse:
    invalid = validate_wait_arguments(args, command="gcode.check")
    if invalid is not None:
        return invalid
    deadline = monotonic() + args.timeout_seconds
    paths: XdgPaths | None = None
    if client is None or artifact_reader is None or client_staging is None:
        paths = resolve_xdg_paths()
    service: ServiceCaller = (
        ServiceClient(paths.socket_file) if client is None and paths is not None else cast(ServiceCaller, client)
    )
    remaining = deadline - monotonic()
    if remaining <= 0:
        return _wait_timeout(None)
    staging = client_staging
    if client is None and staging is None:
        assert paths is not None
        from drawingmachine.cli.service import client_input_staging

        staging = cast(ClientInputStagingPort, client_input_staging(paths))
    staged = None
    request_id = str(uuid4())
    if staging is not None:
        from drawingmachine.cli.service import staging_clock

        staged = staging.stage_gcode(Path(args.path), request_id, cast(StagingClock, staging_clock()))
        request_path = staged.payload_path
    else:
        request_path = Path(args.path).resolve()
    try:
        if staging is not None and staged is not None:

            def transfer() -> None:
                nonlocal staged
                assert staging is not None and staged is not None
                staged = staging.transfer_before_write(staged)

            submitted = cast(ServiceClient, service).call(
                "gcode.check",
                {"schema_version": 1, "path": str(request_path)},
                request_id=request_id,
                timeout_s=remaining,
                before_write=transfer,
            )
        else:
            submitted = service.call(
                "gcode.check",
                {"schema_version": 1, "path": str(request_path)},
                timeout_s=remaining,
            )
    except ServiceDispatchError as error:
        assert staging is not None and staged is not None
        staging.discard_definitely_unsent(staged, error.send_outcome)
        if monotonic() >= deadline:
            return _wait_timeout(None)
        raise
    except DrawingMachineError:
        if monotonic() >= deadline:
            return _wait_timeout(None)
        raise
    if monotonic() >= deadline:
        return _wait_timeout(None)
    if not submitted.ok:
        return submitted
    job_id = submitted.data.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return _result_failure(None, "G-code submission response did not contain a valid job ID")
    wait_args = argparse.Namespace(
        job_id=job_id,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    waited = wait_for_job(
        wait_args,
        client=service,
        sleep=sleep,
        monotonic=monotonic,
        deadline=deadline,
    )
    if not waited.ok:
        return replace(waited, command="gcode.check")
    job = waited.data.get("job")
    state = job.get("state") if isinstance(job, dict) else None
    if state in {"FAILED", "CANCELLED"}:
        return replace(
            waited,
            command="gcode.check",
            data={"schema_version": 1, "job_id": job_id, "state": state, "static_result": None},
        )
    if state not in {"COMPLETED", "BLOCKED"}:
        return _result_failure(job_id, "terminal G-code check response had an invalid state")
    try:
        reader = artifact_reader
        if staging is not None:
            static_result = _static_result_from_export(staging, job_id, waited.data)
        else:
            if reader is None:
                assert paths is not None
                from drawingmachine.cli.service import local_artifact_reader

                reader = local_artifact_reader(paths)
            if monotonic() >= deadline:
                return _wait_timeout(job_id)
            static_result = _static_result(reader, job_id, waited.data)
    except DrawingMachineError:
        return _result_failure(job_id, "G-code static result failed secure local validation")
    if monotonic() >= deadline:
        return _wait_timeout(job_id)
    data: JsonObject = {
        "schema_version": 1,
        "job_id": job_id,
        "state": state,
        "static_result": static_result,
    }
    return replace(waited, command="gcode.check", data=data)


def _static_result(reader: ArtifactReader, job_id: str, status_data: JsonObject) -> JsonObject:
    artifact = _static_artifact(job_id, status_data)
    contents = reader.read_bytes(
        job_id,
        artifact,
        expected_media_type="application/json",
        max_bytes=_MAX_STATIC_RESULT_BYTES,
    )
    return _strict_json_object(contents, job_id)


def _static_result_from_export(
    staging: ClientInputStagingPort,
    job_id: str,
    status_data: JsonObject,
) -> JsonObject:
    artifact = _static_artifact(job_id, status_data)
    return _strict_json_object(staging.read_exported_artifact(job_id, artifact), job_id)


def _static_artifact(job_id: str, status_data: JsonObject) -> ArtifactRef:
    artifacts = status_data.get("artifacts")
    if not isinstance(artifacts, list):
        _invalid_result("job status artifacts must be an array", job_id)
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("role") == "gcode_static"]
    if len(matches) != 1:
        _invalid_result("terminal G-code check must reference exactly one static result", job_id)
    try:
        artifact = ArtifactRef.from_json(matches[0])
    except DrawingMachineError as error:
        raise _result_error("G-code static artifact reference is invalid", job_id) from error
    if artifact.role != "gcode_static" or artifact.media_type != "application/json":
        _invalid_result("G-code static artifact role or media type is invalid", job_id)
    return artifact


def _strict_json_object(contents: bytes, job_id: str) -> JsonObject:
    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"non-finite JSON constant: {value}")

    def strict_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number: {value}")
        return parsed

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        value = json.loads(
            contents.decode("utf-8", errors="strict"),
            parse_constant=reject_constant,
            parse_float=strict_float,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise _result_error("G-code static result is not strict UTF-8 JSON", job_id) from error
    if not isinstance(value, dict):
        _invalid_result("G-code static result must contain a JSON object", job_id)
    return cast(JsonObject, value)


def _result_error(message: str, job_id: str | None) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload("GCODE_STATIC_RESULT_INVALID", ErrorCategory.VALIDATION, message, False, {}, job_id=job_id)
    )


def _invalid_result(message: str, job_id: str | None) -> NoReturn:
    raise _result_error(message, job_id)


def _result_failure(job_id: str | None, message: str) -> ProtocolResponse:
    request_id = str(uuid4())
    return ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        False,
        "gcode.check",
        request_id,
        {},
        ErrorPayload(
            "GCODE_STATIC_RESULT_INVALID",
            ErrorCategory.VALIDATION,
            message,
            False,
            {},
            request_id=request_id,
            job_id=job_id,
        ),
    )


def _wait_timeout(job_id: str | None) -> ProtocolResponse:
    request_id = str(uuid4())
    return ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        False,
        "gcode.check",
        request_id,
        {},
        ErrorPayload(
            "JOB_WAIT_TIMEOUT",
            ErrorCategory.INPUT,
            "timed out while waiting for job",
            False,
            {},
            request_id=request_id,
            job_id=job_id,
        ),
    )


__all__ = ["check_gcode"]
