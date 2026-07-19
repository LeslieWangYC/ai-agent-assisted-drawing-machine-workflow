from __future__ import annotations

import argparse
from pathlib import Path
from typing import NoReturn

from drawingmachine.cli.client import ServiceClient
from drawingmachine.config import resolve_xdg_paths
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import ProtocolResponse


def openclaw_command(args: argparse.Namespace) -> ProtocolResponse:
    root = _canonical_root(args.openclaw_root)
    return ServiceClient(resolve_xdg_paths().socket_file).call(
        args.command_name,
        {"schema_version": 1, "openclaw_root": str(root)},
    )


def _canonical_root(values: object) -> Path:
    if type(values) is not list or len(values) != 1 or type(values[0]) is not str:
        _invalid("exactly one --openclaw-root is required")
    text = values[0]
    if (
        not text.isascii()
        or len(text.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        _invalid("--openclaw-root must be bounded control-free canonical ASCII")
    path = Path(text)
    if (
        not path.is_absolute()
        or text.startswith("//")
        or str(path) != text
        or any(part in {".", ".."} for part in path.parts)
    ):
        _invalid("--openclaw-root must be a canonical absolute path")
    return path


def _invalid(message: str) -> NoReturn:
    raise DrawingMachineError(ErrorPayload("OPENCLAW_ARGUMENT_INVALID", ErrorCategory.INPUT, message, False, {}))


__all__ = ["openclaw_command"]
