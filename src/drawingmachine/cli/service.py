from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from drawingmachine.cli.client import ServiceClient
from drawingmachine.config import XdgPaths, resolve_xdg_paths
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse

if TYPE_CHECKING:
    from drawingmachine.ports.artifacts import ArtifactReader
    from drawingmachine.service import ServiceRuntime
    from drawingmachine.service._instance import InstanceLease
    from drawingmachine.service.server import UnixServiceServer


def local_artifact_reader(paths: XdgPaths) -> ArtifactReader:
    from drawingmachine.bootstrap import build_artifact_reader

    return build_artifact_reader(paths)


def client_input_staging(paths: XdgPaths) -> object:
    from drawingmachine.bootstrap import build_client_input_staging

    return build_client_input_staging(paths)


def staging_clock() -> object:
    from drawingmachine.bootstrap import build_staging_clock

    return build_staging_clock()


def service_status(args: argparse.Namespace) -> ProtocolResponse:
    del args
    paths = resolve_xdg_paths()
    return ServiceClient(paths.socket_file).call("service.status", {})


def run_service(args: argparse.Namespace) -> ProtocolResponse:
    del args
    paths = resolve_xdg_paths()
    asyncio.run(_run_foreground(paths))
    return ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=True,
        command="service.run",
        request_id=str(uuid4()),
        data={"status": "STOPPED"},
        error=None,
    )


async def _run_foreground(paths: XdgPaths) -> None:
    from drawingmachine.bootstrap import build_instance_lease, build_runtime, build_server

    instance_lease: InstanceLease | None = None
    runtime: ServiceRuntime | None = None
    server: UnixServiceServer | None = None
    serve_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    installed_signals: list[signal.Signals] = []
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop_requested.set)
            installed_signals.append(signum)

        runtime = build_runtime(paths, require_service_access=True)
        instance_lease = build_instance_lease(paths, runtime.access_policy)
        runtime.start()
        server = build_server(paths, runtime, instance_lease=instance_lease)
        serve_task = asyncio.create_task(server.serve())
        await server.wait_started()

        stop_task = asyncio.create_task(stop_requested.wait())
        completed, _ = await asyncio.wait((serve_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if serve_task in completed:
            await serve_task
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        if stop_task is not None:
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
        try:
            if server is not None:
                await server.close()
        finally:
            try:
                if serve_task is not None:
                    await serve_task
            finally:
                try:
                    if runtime is not None:
                        runtime.stop()
                finally:
                    if instance_lease is not None:
                        instance_lease.release()
