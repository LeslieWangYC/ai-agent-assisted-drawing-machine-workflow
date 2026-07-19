from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import cast
from uuid import uuid4

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse

from .parser import CliUsageError, CommandHandler, build_parser
from .render import render_json, render_text


def failed_cli_response(command: str, payload: ErrorPayload) -> ProtocolResponse:
    request_id = payload.request_id or str(uuid4())
    if payload.request_id is None:
        payload = replace(payload, request_id=request_id)
    return ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=False,
        command=command,
        request_id=request_id,
        data={},
        error=payload,
    )


def _requested_output(argv: Sequence[str]) -> str:
    output = "text"
    for index, argument in enumerate(argv):
        if argument == "--output" and index + 1 < len(argv) and argv[index + 1] in {"json", "text"}:
            output = argv[index + 1]
        elif argument.startswith("--output=") and argument.removeprefix("--output=") in {"json", "text"}:
            output = argument.removeprefix("--output=")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    output = _requested_output(raw_argv)
    command_name = "cli"
    try:
        args = build_parser().parse_args(raw_argv)
    except CliUsageError as error:
        response = failed_cli_response("cli", error.payload)
    else:
        command_name = cast(str, args.command_name)
        handler = cast(CommandHandler, args.handler)
        output = cast(str, args.output)
        try:
            response = handler(args)
        except DrawingMachineError as error:
            response = failed_cli_response(command_name, error.payload)
        except Exception:
            response = failed_cli_response(
                command_name,
                ErrorPayload(
                    code="INTERNAL_ERROR",
                    category=ErrorCategory.INTERNAL,
                    message="internal CLI error",
                    retryable=False,
                    details={},
                ),
            )

    if output == "json":
        try:
            rendered = render_json(response)
        except Exception:
            response = failed_cli_response(
                command_name,
                ErrorPayload(
                    code="INTERNAL_ERROR",
                    category=ErrorCategory.INTERNAL,
                    message="internal CLI error",
                    retryable=False,
                    details={},
                ),
            )
            rendered = render_json(response)
    else:
        rendered = render_text(response)
    sys.stdout.write(rendered)
    sys.stdout.write("\n")
    return 0 if response.ok else 1
