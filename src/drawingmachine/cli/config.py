import argparse
from uuid import uuid4

from drawingmachine.config import load_config, parse_machine_execution_config, resolve_xdg_paths
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def check_config(args: argparse.Namespace) -> ProtocolResponse:
    del args
    bundle = load_config(resolve_xdg_paths())
    parse_machine_execution_config(bundle.machine, profile_path=bundle.machine_path)
    data: JsonObject = {
        "status": "VALID",
        "profiles": {
            "machine": bundle.application.machine_profile,
            "provider": bundle.application.provider_profile,
        },
        "schema_versions": {
            "application": bundle.application.schema_version,
            "machine": bundle.machine.schema_version,
            "provider": bundle.provider.schema_version,
        },
        "digests": dict(bundle.digests),
    }
    return ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=True,
        command="config.check",
        request_id=str(uuid4()),
        data=data,
        error=None,
    )
