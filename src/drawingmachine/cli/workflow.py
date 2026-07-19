from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import NoReturn, cast
from uuid import uuid4

from drawingmachine.cli.client import ServiceClient, ServiceDispatchError
from drawingmachine.config import resolve_xdg_paths
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import ClientInputStagingPort, StagingClock
from drawingmachine.protocol import ProtocolResponse


def run_workflow(args: argparse.Namespace) -> ProtocolResponse:
    paths = resolve_xdg_paths()
    service = ServiceClient(paths.socket_file)
    if args.resume_job is not None:
        if any(
            value is not None
            for value in (
                args.job_name,
                args.input_image,
                args.route_mode,
                args.semantic_assessment_json,
                args.prompt,
                args.prompt_file,
            )
        ):
            _argument_invalid("resume review cannot include initial submission arguments")
        if args.review_json is None:
            _argument_invalid("resume review requires --review-json")
        review_path = Path(args.review_json).resolve()
        review = _read_json_object(review_path)
        from drawingmachine.cli.service import client_input_staging, staging_clock

        staging = cast(ClientInputStagingPort, client_input_staging(paths))
        status = service.call("job.status", {"schema_version": 1, "job_id": args.resume_job})
        if not status.ok:
            return status
        arguments: JsonObject = {
            "schema_version": 1,
            "mode": "resume_review",
            "job_id": args.resume_job,
            "review": review,
            "reviewed_image_sha256": _reviewed_export_sha256(
                review,
                review_path,
                args.resume_job,
                status.data,
                staging,
            ),
        }
    else:
        if args.review_json is not None:
            _argument_invalid("review arguments require --resume-job")
        if args.job_name is None or args.input_image is None or args.route_mode is None:
            _argument_invalid("initial workflow requires --job-name, --input-image, and --route-mode")
        assessment = (
            None if args.semantic_assessment_json is None else _read_json_object(Path(args.semantic_assessment_json))
        )
        if args.route_mode == "auto" and assessment is None:
            _argument_invalid("auto route mode requires --semantic-assessment-json")
        prompt = args.prompt
        if args.prompt_file is not None:
            prompt = _read_text(Path(args.prompt_file))
        request_id = str(uuid4())
        from drawingmachine.cli.service import client_input_staging, staging_clock

        staging = cast(ClientInputStagingPort, client_input_staging(paths))
        staged = staging.stage_image(
            Path(args.input_image),
            request_id,
            cast(StagingClock, staging_clock()),
        )
        arguments = {
            "schema_version": 1,
            "mode": "submit",
            "job_name": args.job_name,
            "input_path": str(staged.payload_path),
            "route_mode": args.route_mode,
            "semantic_assessment": assessment,
            "prompt": prompt,
        }

        def transfer() -> None:
            nonlocal staged
            staged = staging.transfer_before_write(staged)

        try:
            return service.call(
                "workflow.run",
                arguments,
                request_id=request_id,
                before_write=transfer,
            )
        except ServiceDispatchError as error:
            staging.discard_definitely_unsent(staged, error.send_outcome)
            raise
    return service.call("workflow.run", arguments)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DrawingMachineError(
            ErrorPayload("TEXT_FILE_INVALID", ErrorCategory.INPUT, "text file could not be read as UTF-8", False, {})
        ) from error


def _read_json_object(path: Path) -> JsonObject:
    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"non-finite JSON constant: {value}")

    def exact_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=exact_pairs,
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise DrawingMachineError(
            ErrorPayload("JSON_FILE_INVALID", ErrorCategory.INPUT, "JSON file is invalid", False, {})
        ) from error
    if not isinstance(value, dict):
        raise DrawingMachineError(
            ErrorPayload("JSON_FILE_INVALID", ErrorCategory.INPUT, "JSON file must contain an object", False, {})
        )
    return cast(JsonObject, value)


def _reviewed_export_sha256(
    review: JsonObject,
    review_path: Path,
    job_id: str,
    status_data: JsonObject,
    staging: ClientInputStagingPort,
) -> str:
    value = review.get("processed_image")
    if not isinstance(value, str) or not value:
        _argument_invalid("review processed_image must be a non-empty path string")
    image_path = Path(value)
    if not image_path.is_absolute():
        image_path = review_path.parent / image_path
    try:
        artifacts = status_data.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("job status artifacts are invalid")
        matches = [item for item in artifacts if isinstance(item, dict) and item.get("role") == "processed_image"]
        if len(matches) != 1:
            raise ValueError("processed image export authority is not unique")
        artifact = ArtifactRef.from_json(matches[0])
        exported = staging.read_exported_artifact(job_id, artifact)
        if image_path.read_bytes() != exported:
            raise DrawingMachineError(
                ErrorPayload(
                    "WORKFLOW_CONTRACT_INVALID",
                    ErrorCategory.VALIDATION,
                    "reviewed processed image differs from the controlled export",
                    False,
                    {},
                    job_id=job_id,
                )
            )
    except DrawingMachineError as error:
        if error.payload.code == "WORKFLOW_CONTRACT_INVALID":
            raise
        raise DrawingMachineError(
            ErrorPayload(
                "REVIEW_IMAGE_INVALID",
                ErrorCategory.INPUT,
                "reviewed processed image could not be read",
                False,
                {},
            )
        ) from error
    except (OSError, ValueError) as error:
        raise DrawingMachineError(
            ErrorPayload(
                "REVIEW_IMAGE_INVALID",
                ErrorCategory.INPUT,
                "reviewed processed image could not be read",
                False,
                {},
            )
        ) from error
    return hashlib.sha256(exported).hexdigest()


def _argument_invalid(message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("WORKFLOW_ARGUMENT_INVALID", ErrorCategory.INPUT, message, False, {}))


__all__ = ["run_workflow"]
