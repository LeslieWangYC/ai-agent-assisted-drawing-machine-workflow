from __future__ import annotations

import argparse

from drawingmachine.cli.client import ServiceClient
from drawingmachine.config import resolve_xdg_paths
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import ProtocolResponse


def machine_status(args: argparse.Namespace) -> ProtocolResponse:
    return _client().call(
        "machine.status",
        {"schema_version": 1, "execution_id": args.execution_id},
    )


def machine_prepare(args: argparse.Namespace) -> ProtocolResponse:
    return _client().call(
        "machine.prepare",
        {
            "schema_version": 1,
            "job_id": args.job_id,
            "job_revision": args.job_revision,
        },
    )


def machine_action(args: argparse.Namespace) -> ProtocolResponse:
    return _client().call(args.command_name, _approval_arguments(args.execution_id, args.approve))


def machine_recover(args: argparse.Namespace) -> ProtocolResponse:
    arguments = _approval_arguments(args.execution_id, args.approve)
    arguments["disposition"] = args.disposition
    return _client().call("machine.recover", arguments)


def _approval_arguments(execution_id: str, challenge_id: str | None) -> JsonObject:
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "mode": "request" if challenge_id is None else "execute",
        "challenge_id": challenge_id,
    }


def _client() -> ServiceClient:
    return ServiceClient(resolve_xdg_paths().socket_file)


__all__ = ["machine_action", "machine_prepare", "machine_recover", "machine_status"]
