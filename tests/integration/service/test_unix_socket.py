import asyncio
import fcntl
import os
import socket
import stat
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID

import pytest

import drawingmachine.service.server as server_module
from drawingmachine.adapters.system.clock import SystemClock
from drawingmachine.bootstrap import build_runtime
from drawingmachine.cli.client import ServiceClient
from drawingmachine.config import XdgPaths
from drawingmachine.errors import DrawingMachineError, ErrorCategory
from drawingmachine.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    ProtocolRequest,
    ProtocolResponse,
    decode_response,
    encode_request,
    encode_response,
)
from drawingmachine.service.models import RequestContext
from drawingmachine.service.runtime import ServiceRuntime
from drawingmachine.service.server import UnixServiceServer


@asynccontextmanager
async def _running_service(
    paths: XdgPaths,
) -> AsyncIterator[tuple[ServiceRuntime, UnixServiceServer]]:
    runtime = build_runtime(paths)
    runtime.start()
    server = UnixServiceServer(paths.socket_file, runtime.dispatcher, SystemClock())
    server_task = asyncio.create_task(server.serve())
    try:
        await server.wait_started()
        yield runtime, server
    finally:
        await server.close()
        await server_task
        runtime.stop()


@asynccontextmanager
async def _raw_response_server(socket_path: Path, response: bytes) -> AsyncIterator[None]:
    connection_tasks: list[asyncio.Task[None]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\n")
            if response:
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection_tasks.append(asyncio.create_task(respond(reader, writer)))

    server = await asyncio.start_unix_server(accept, path=socket_path)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        if connection_tasks:
            await asyncio.gather(*connection_tasks)


def _assert_service_unavailable(error: DrawingMachineError) -> None:
    assert error.payload.code == "SERVICE_UNAVAILABLE"
    assert error.payload.category is ErrorCategory.SERVICE
    assert error.payload.retryable is True


def test_server_rejects_nonpositive_maintenance_interval(valid_xdg_paths: XdgPaths) -> None:
    with pytest.raises(ValueError, match="positive"):
        UnixServiceServer(
            valid_xdg_paths.socket_file,
            object(),  # type: ignore[arg-type]
            SystemClock(),
            maintenance_interval_s=0,
        )


def test_server_handles_immediate_serving_completion_and_prestartup_cleanup_fallback(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()

        class ImmediateServer:
            def __init__(self, listener: socket.socket) -> None:
                self.listener = listener

            async def serve_forever(self) -> None:
                return

            def close(self) -> None:
                self.listener.close()

            async def wait_closed(self) -> None:
                return

        async def immediate_start(*_args: object, sock: socket.socket, **_kwargs: object) -> ImmediateServer:
            return ImmediateServer(sock)

        monkeypatch.setattr(server_module.asyncio, "start_unix_server", immediate_start)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        await server.serve()
        await server.wait_started()

        fallback = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        monkeypatch.setattr(fallback, "_record_startup_error", lambda _error: None)
        monkeypatch.setattr(
            fallback,
            "_bind_listener",
            lambda: (_ for _ in ()).throw(RuntimeError("before startup")),
        )
        with pytest.raises(RuntimeError, match="before startup"):
            await fallback.serve()
        with pytest.raises(RuntimeError, match="before startup"):
            await fallback.wait_started()
        runtime.stop()

    asyncio.run(scenario())


def test_periodic_loop_and_shutdown_cover_empty_and_populated_task_sets(
    valid_xdg_paths: XdgPaths,
) -> None:
    async def scenario() -> None:
        server = UnixServiceServer(
            valid_xdg_paths.socket_file,
            object(),  # type: ignore[arg-type]
            SystemClock(),
            periodic_maintenance=lambda: None,
        )
        server._closing = True
        await server._run_periodic_maintenance()

        class Writer:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        writer = Writer()
        client_task = asyncio.create_task(asyncio.sleep(10))
        late_task = asyncio.create_task(asyncio.sleep(0))
        server._client_writers.add(writer)  # type: ignore[arg-type]
        server._client_tasks.add(client_task)
        server._late_writer_tasks.add(late_task)

        assert await server._shutdown() is None
        assert writer.closed is True
        assert client_task.cancelled() is True
        assert server._late_writer_tasks == set()

    asyncio.run(scenario())


def test_stale_socket_identity_checks_cover_preprobe_disappearance_and_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "service.sock"
    other_path = tmp_path / "other.sock"
    server = UnixServiceServer(socket_path, object(), SystemClock())  # type: ignore[arg-type]
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    other.bind(str(other_path))
    real_open = server_module.instance_paths.open_path_descriptor
    monkeypatch.setattr(server_module.instance_paths, "open_path_descriptor", lambda _path: real_open(other_path))
    try:
        with pytest.raises(DrawingMachineError, match="changed before stale probe"):
            server._remove_proven_stale_socket()
    finally:
        stale.close()
        other.close()
        socket_path.unlink(missing_ok=True)
        other_path.unlink(missing_ok=True)

    def probe_with_final(final: os.stat_result | None) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(socket_path))
        stale.close()
        initial = os.lstat(socket_path)
        calls = 0

        def staged_lstat(_path: Path) -> os.stat_result | None:
            nonlocal calls
            calls += 1
            return initial if calls == 1 else final

        monkeypatch.setattr(server_module.instance_paths, "open_path_descriptor", real_open)
        monkeypatch.setattr(server_module.instance_paths, "lstat", staged_lstat)
        try:
            if final is None:
                server._remove_proven_stale_socket()
            else:
                with pytest.raises(DrawingMachineError, match="changed during stale probe"):
                    server._remove_proven_stale_socket()
        finally:
            socket_path.unlink(missing_ok=True)

    probe_with_final(None)
    regular = tmp_path / "regular"
    regular.write_text("replacement", encoding="utf-8")
    probe_with_final(os.lstat(regular))


def test_listener_rejects_type_descriptor_and_postbind_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "service.sock"
    regular = tmp_path / "regular"
    regular.write_text("not socket", encoding="utf-8")
    regular_stat = os.lstat(regular)

    server = UnixServiceServer(socket_path, object(), SystemClock())  # type: ignore[arg-type]
    monkeypatch.setattr(server_module.os, "lstat", lambda _path: regular_stat)
    with pytest.raises(DrawingMachineError, match="changed type"):
        server._bind_listener()
    socket_path.unlink(missing_ok=True)

    monkeypatch.undo()
    other_path = tmp_path / "other.sock"
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(str(other_path))
    real_open = server_module.instance_paths.open_path_descriptor
    monkeypatch.setattr(server_module.instance_paths, "open_path_descriptor", lambda _path: real_open(other_path))
    with pytest.raises(DrawingMachineError, match="bound service socket path changed"):
        server._bind_listener()
    socket_path.unlink(missing_ok=True)
    other.close()
    other_path.unlink(missing_ok=True)

    monkeypatch.undo()
    real_lstat = os.lstat
    calls = 0

    def replace_after_descriptor(path: Path) -> os.stat_result:
        nonlocal calls
        calls += 1
        return regular_stat if calls == 2 else real_lstat(path)

    monkeypatch.setattr(server_module.os, "lstat", replace_after_descriptor)
    with pytest.raises(DrawingMachineError, match="bound service socket path changed"):
        server._bind_listener()
    server._close_owned_descriptors()
    socket_path.unlink(missing_ok=True)


def test_pid_frame_peer_and_write_guards_cover_terminal_branches(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = UnixServiceServer(valid_xdg_paths.socket_file, object(), SystemClock())  # type: ignore[arg-type]
    valid_xdg_paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_module.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(RuntimeError, match="temporary PID"):
        server._write_pid_file()
    monkeypatch.undo()

    async def oversized_frame() -> None:
        reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 2)
        reader.feed_data(b"x" * MAX_MESSAGE_BYTES + b"\n")
        reader.feed_eof()
        with pytest.raises(DrawingMachineError) as caught:
            await server_module._read_frame(reader)
        assert caught.value.payload.code == "PROTOCOL_MESSAGE_TOO_LARGE"

    asyncio.run(oversized_frame())

    writer = type("Writer", (), {"get_extra_info": lambda self, _name: None})()
    with pytest.raises(RuntimeError, match="no socket"):
        server_module._peer_credentials(writer)  # type: ignore[arg-type]
    monkeypatch.setattr(server_module.os, "write", lambda _descriptor, _payload: 0)
    with pytest.raises(OSError, match="failed to write"):
        server_module._write_all(1, b"pid")


def test_status_round_trip_uses_peer_credentials(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        async with _running_service(valid_xdg_paths):
            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "service.status",
                {},
                request_id="req-status",
            )

            assert response.ok is True
            assert response.command == "service.status"
            assert response.request_id == "req-status"
            assert response.data["status"] == "RUNNING"

    asyncio.run(scenario())


def test_request_context_comes_from_linux_peer_credentials(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        seen_contexts: list[RequestContext] = []

        async with _running_service(valid_xdg_paths) as (runtime, _server):

            def identify(request: ProtocolRequest, context: RequestContext) -> dict[str, object]:
                seen_contexts.append(context)
                return {
                    "pid": context.peer.pid,
                    "uid": context.peer.uid,
                    "gid": context.peer.gid,
                    "claimed_uid": request.arguments["claimed_uid"],
                }

            runtime.dispatcher.register("peer.identify", identify)
            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "peer.identify",
                {"claimed_uid": 0},
                request_id="req-peer",
            )

            assert response.data == {
                "pid": os.getpid(),
                "uid": os.getuid(),
                "gid": os.getgid(),
                "claimed_uid": 0,
            }
            assert len(seen_contexts) == 1
            assert seen_contexts[0].received_at.tzinfo is not None

    asyncio.run(scenario())


def test_machine_command_over_socket_rejects_spoofed_peer_identity_field(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        async with _running_service(valid_xdg_paths):
            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "machine.status",
                {"schema_version": 1, "execution_id": None, "uid": os.getuid()},
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "MACHINE_COMMAND_ARGUMENTS_INVALID"

    asyncio.run(scenario())


def test_server_tightens_permissions_and_owns_pid_lifecycle(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        valid_xdg_paths.runtime_dir.chmod(0o777)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            assert stat.S_IMODE(valid_xdg_paths.runtime_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(valid_xdg_paths.socket_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(valid_xdg_paths.pid_file.stat().st_mode) == 0o600
            assert valid_xdg_paths.pid_file.read_text(encoding="ascii").strip() == str(os.getpid())
        finally:
            await server.close()
            await server_task
            runtime.stop()

        assert not valid_xdg_paths.socket_file.exists()
        assert not valid_xdg_paths.pid_file.exists()

    asyncio.run(scenario())


def test_server_holds_an_exclusive_lifetime_lease(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        lock_path = valid_xdg_paths.socket_file.with_suffix(".lock")
        try:
            await server.wait_started()
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
            contender = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)

            socket_identity = valid_xdg_paths.socket_file.stat().st_ino
            pid_identity = valid_xdg_paths.pid_file.stat().st_ino
            pid_content = valid_xdg_paths.pid_file.read_bytes()
            second = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
            second_task = asyncio.create_task(second.serve())
            try:
                with pytest.raises(DrawingMachineError, match="lease is already held"):
                    await second.wait_started()
                with pytest.raises(DrawingMachineError, match="lease is already held"):
                    await second_task
            finally:
                await second.close()
            assert valid_xdg_paths.socket_file.stat().st_ino == socket_identity
            assert valid_xdg_paths.pid_file.stat().st_ino == pid_identity
            assert valid_xdg_paths.pid_file.read_bytes() == pid_content
        finally:
            await server.close()
            await server_task
            runtime.stop()

        contender = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)
        finally:
            os.close(contender)

    asyncio.run(scenario())


def test_server_rejects_a_non_regular_lease_file(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        lock_path = valid_xdg_paths.socket_file.with_suffix(".lock")
        os.mkfifo(lock_path, mode=0o600)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(DrawingMachineError, match="lease path is not a regular file"):
                await server.wait_started()
            with pytest.raises(DrawingMachineError, match="lease path is not a regular file"):
                await server_task
            assert stat.S_ISFIFO(lock_path.stat().st_mode)
        finally:
            await server.close()
            lock_path.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_lease_blocks_a_second_server_while_first_is_paused_before_listen(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_listen = server_module._listen_socket
    listen_entered = threading.Event()
    resume_listen = threading.Event()
    server_started = threading.Event()
    stop_requested = threading.Event()
    thread_errors: list[BaseException] = []

    def paused_listen(listener: socket.socket) -> None:
        listen_entered.set()
        if not resume_listen.wait(timeout=3.0):
            raise TimeoutError("test did not resume socket listen")
        original_listen(listener)

    monkeypatch.setattr(server_module, "_listen_socket", paused_listen)
    runtime = build_runtime(valid_xdg_paths)
    runtime.start()
    first = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())

    def run_first_server() -> None:
        async def scenario() -> None:
            server_task = asyncio.create_task(first.serve())
            await first.wait_started()
            server_started.set()
            await asyncio.to_thread(stop_requested.wait)
            await first.close()
            await server_task

        try:
            asyncio.run(scenario())
        except BaseException as error:
            thread_errors.append(error)

    first_thread = threading.Thread(target=run_first_server, name="paused-unix-server")
    first_thread.start()
    try:
        assert listen_entered.wait(timeout=3.0)
        bound_identity = valid_xdg_paths.socket_file.stat().st_ino

        async def contend() -> None:
            second = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
            second_task = asyncio.create_task(second.serve())
            try:
                with pytest.raises(DrawingMachineError):
                    await second.wait_started()
                with pytest.raises(DrawingMachineError):
                    await second_task
            finally:
                await second.close()

        asyncio.run(contend())
        assert valid_xdg_paths.socket_file.stat().st_ino == bound_identity
    finally:
        resume_listen.set()
        assert server_started.wait(timeout=3.0)
        stop_requested.set()
        first_thread.join(timeout=3.0)
        runtime.stop()

    assert first_thread.is_alive() is False
    assert thread_errors == []


def test_server_rejects_an_open_bound_socket_before_listen(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_socket.bind(str(valid_xdg_paths.socket_file))
        bound_identity = valid_xdg_paths.socket_file.stat().st_ino
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(DrawingMachineError):
                await server.wait_started()
            with pytest.raises(DrawingMachineError):
                await server_task
            assert valid_xdg_paths.socket_file.stat().st_ino == bound_identity
        finally:
            await server.close()
            bound_socket.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_failed_start_after_bind_removes_unchanged_provisional_socket(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()

        def fail_listen(listener: socket.socket) -> None:
            del listener
            raise RuntimeError("forced listen failure")

        monkeypatch.setattr(server_module, "_listen_socket", fail_listen)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(RuntimeError, match="forced listen failure"):
                await server.wait_started()
            with pytest.raises(RuntimeError, match="forced listen failure"):
                await server_task
            assert not valid_xdg_paths.socket_file.exists()
            assert not valid_xdg_paths.pid_file.exists()
        finally:
            await server.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_failed_start_after_bind_preserves_replacement_socket(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement_identity: int | None = None

        def replace_then_fail(listener: socket.socket) -> None:
            nonlocal replacement_identity
            del listener
            valid_xdg_paths.socket_file.unlink()
            replacement.bind(str(valid_xdg_paths.socket_file))
            replacement.listen()
            replacement_identity = valid_xdg_paths.socket_file.stat().st_ino
            raise RuntimeError("forced listen failure after replacement")

        monkeypatch.setattr(server_module, "_listen_socket", replace_then_fail)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(RuntimeError, match="failure after replacement"):
                await server.wait_started()
            with pytest.raises(RuntimeError, match="failure after replacement"):
                await server_task
            assert replacement_identity is not None
            assert valid_xdg_paths.socket_file.stat().st_ino == replacement_identity
        finally:
            await server.close()
            replacement.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_server_fails_closed_when_kernel_socket_state_is_unavailable(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_socket.bind(str(valid_xdg_paths.socket_file))
        bound_identity = valid_xdg_paths.socket_file.stat().st_ino
        monkeypatch.setattr(server_module, "_PROC_NET_UNIX", tmp_path / "missing-proc-net-unix")
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(DrawingMachineError, match="kernel state could not be verified"):
                await server.wait_started()
            with pytest.raises(DrawingMachineError, match="kernel state could not be verified"):
                await server_task
            assert valid_xdg_paths.socket_file.stat().st_ino == bound_identity
        finally:
            await server.close()
            bound_socket.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_server_replaces_only_a_connect_refused_stale_socket(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_socket.bind(str(valid_xdg_paths.socket_file))
        stale_socket.close()

        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "service.status",
                {},
            )
            assert response.ok is True
        finally:
            await server.close()
            await server_task
            runtime.stop()

    asyncio.run(scenario())


def test_server_rejects_and_preserves_a_live_socket(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        live_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live_socket.bind(str(valid_xdg_paths.socket_file))
        live_socket.listen()
        live_identity = valid_xdg_paths.socket_file.stat().st_ino
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(DrawingMachineError) as startup_error:
                await server.wait_started()
            assert startup_error.value.payload.category is ErrorCategory.SERVICE
            with pytest.raises(DrawingMachineError):
                await server_task
            assert valid_xdg_paths.socket_file.stat().st_ino == live_identity
        finally:
            await server.close()
            live_socket.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_server_rejects_and_preserves_a_non_socket_path(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        valid_xdg_paths.socket_file.write_text("owned by another process", encoding="utf-8")
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(DrawingMachineError):
                await server.wait_started()
            with pytest.raises(DrawingMachineError):
                await server_task
            assert valid_xdg_paths.socket_file.read_text(encoding="utf-8") == "owned by another process"
        finally:
            await server.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_shutdown_preserves_a_replacement_socket_inode(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            await server.wait_started()
            valid_xdg_paths.socket_file.unlink()
            replacement.bind(str(valid_xdg_paths.socket_file))
            replacement.listen()
            replacement_identity = valid_xdg_paths.socket_file.stat().st_ino

            await server.close()
            await server_task

            assert valid_xdg_paths.socket_file.stat().st_ino == replacement_identity
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(valid_xdg_paths.socket_file))
            finally:
                probe.close()
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            replacement.close()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_shutdown_preserves_a_pid_file_with_other_content(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            valid_xdg_paths.pid_file.write_text("999999\n", encoding="ascii")
            await server.close()
            await server_task
            assert valid_xdg_paths.pid_file.read_text(encoding="ascii") == "999999\n"
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_shutdown_preserves_pid_prefix_with_trailing_content_beyond_read_limit(
    valid_xdg_paths: XdgPaths,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        pid_prefix = str(os.getpid()).encode("ascii")
        foreign_payload = pid_prefix + b" " * (64 - len(pid_prefix)) + b"foreign trailing content\n"
        try:
            await server.wait_started()
            valid_xdg_paths.pid_file.write_bytes(foreign_payload)
            await server.close()
            await server_task
            assert valid_xdg_paths.pid_file.read_bytes() == foreign_payload
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_shutdown_preserves_a_pid_file_with_non_ascii_content(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            valid_xdg_paths.pid_file.write_bytes(b"\xff\n")
            await server.close()
            await server_task
            assert valid_xdg_paths.pid_file.read_bytes() == b"\xff\n"
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_shutdown_preserves_a_replacement_pid_inode(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        replacement_path = valid_xdg_paths.pid_file.with_name("replacement.pid")
        try:
            await server.wait_started()
            replacement_path.write_text("888888\n", encoding="ascii")
            replacement_path.chmod(0o600)
            os.replace(replacement_path, valid_xdg_paths.pid_file)
            replacement_identity = valid_xdg_paths.pid_file.stat().st_ino

            await server.close()
            await server_task

            assert valid_xdg_paths.pid_file.stat().st_ino == replacement_identity
            assert valid_xdg_paths.pid_file.read_text(encoding="ascii") == "888888\n"
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            replacement_path.unlink(missing_ok=True)
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_pid_publication_never_adopts_a_post_replace_inode(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        original_replace = os.replace
        replacement_identity: int | None = None

        def replace_then_substitute(source: str | Path, destination: str | Path) -> None:
            nonlocal replacement_identity
            original_replace(source, destination)
            if Path(destination) == valid_xdg_paths.pid_file:
                replacement_path = valid_xdg_paths.pid_file.with_name("replacement.pid")
                replacement_path.write_text("777777\n", encoding="ascii")
                replacement_path.chmod(0o600)
                original_replace(replacement_path, destination)
                replacement_identity = valid_xdg_paths.pid_file.stat().st_ino

        monkeypatch.setattr(server_module.os, "replace", replace_then_substitute)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(RuntimeError, match="PID path changed during publication"):
                await server.wait_started()
            with pytest.raises(RuntimeError, match="PID path changed during publication"):
                await server_task
            assert replacement_identity is not None
            assert valid_xdg_paths.pid_file.stat().st_ino == replacement_identity
            assert valid_xdg_paths.pid_file.read_text(encoding="ascii") == "777777\n"
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_pid_publication_fails_closed_when_target_cannot_be_opened(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        original_open = os.open

        def deny_pid_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if Path(os.fsdecode(path)) == valid_xdg_paths.pid_file:
                raise PermissionError("forced PID target open failure")
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(server_module.os, "open", deny_pid_open)
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            with pytest.raises(RuntimeError, match="PID path changed during publication"):
                await server.wait_started()
            with pytest.raises(RuntimeError, match="PID path changed during publication"):
                await server_task
            assert not valid_xdg_paths.pid_file.exists()
        finally:
            await server.close()
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

    asyncio.run(scenario())


def test_server_handles_only_one_request_per_connection(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        async with _running_service(valid_xdg_paths) as (runtime, _server):

            def record(request: ProtocolRequest, context: RequestContext) -> dict[str, object]:
                del context
                calls.append(request.request_id)
                return {"handled": request.request_id}

            runtime.dispatcher.register("test.record", record)
            reader, writer = await asyncio.open_unix_connection(valid_xdg_paths.socket_file)
            writer.write(
                encode_request(ProtocolRequest(PROTOCOL_VERSION, "test.record", "req-one", {}))
                + encode_request(ProtocolRequest(PROTOCOL_VERSION, "test.record", "req-two", {}))
            )
            await writer.drain()

            response = decode_response(await reader.readuntil(b"\n"))
            assert response.request_id == "req-one"
            assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
            assert calls == ["req-one"]
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frame", "expected_code"),
    [
        (
            b'{"protocol_version":99,"command":"attacker.command","request_id":"attacker-id","arguments":{}}\n',
            "PROTOCOL_UNSUPPORTED_VERSION",
        ),
        (
            b'{"protocol_version":1,"command":"attacker.command","request_id":"attacker-id","arguments":[]}\n',
            "PROTOCOL_INVALID",
        ),
    ],
    ids=["unsupported-version", "malformed-request"],
)
def test_server_returns_bounded_current_version_errors_without_echoing_untrusted_identity(
    valid_xdg_paths: XdgPaths,
    frame: bytes,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        async with _running_service(valid_xdg_paths):
            reader, writer = await asyncio.open_unix_connection(valid_xdg_paths.socket_file)
            writer.write(frame)
            await writer.drain()

            response_frame = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=1.0)
            response = decode_response(response_frame)
            assert len(response_frame) <= MAX_MESSAGE_BYTES
            assert response.protocol_version == PROTOCOL_VERSION
            assert response.schema_version == SCHEMA_VERSION
            assert response.ok is False
            assert response.command == "protocol.error"
            assert response.request_id != "attacker-id"
            assert UUID(response.request_id).version == 4
            assert response.data == {}
            assert response.error is not None
            assert response.error.code == expected_code
            assert response.error.request_id == response.request_id
            assert b"attacker.command" not in response_frame
            assert b"attacker-id" not in response_frame
            assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_server_recovers_after_a_request_ends_early(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        async with _running_service(valid_xdg_paths):
            reader, writer = await asyncio.open_unix_connection(valid_xdg_paths.socket_file)
            writer.write(b'{"protocol_version":1')
            await writer.drain()
            writer.write_eof()

            assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
            writer.close()
            await writer.wait_closed()

            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "service.status",
                {},
            )
            assert response.ok is True

    asyncio.run(scenario())


def test_unexpected_connection_failure_closes_only_that_connection(valid_xdg_paths: XdgPaths) -> None:
    class FailingClock:
        def now(self) -> object:
            raise RuntimeError("forced request context failure")

    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, FailingClock())  # type: ignore[arg-type]
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            with pytest.raises(DrawingMachineError) as error:
                await asyncio.to_thread(
                    ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                    "service.status",
                    {},
                )
            assert error.value.payload.code == "PROTOCOL_EARLY_EOF"
        finally:
            await server.close()
            await server_task
            runtime.stop()

    asyncio.run(scenario())


def test_server_bounds_oversized_requests_and_keeps_serving(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        async with _running_service(valid_xdg_paths):
            reader, writer = await asyncio.open_unix_connection(valid_xdg_paths.socket_file)
            writer.write(b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n")
            try:
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
            except ConnectionResetError:
                pass
            finally:
                writer.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()

            response = await asyncio.to_thread(
                ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None).call,
                "service.status",
                {},
            )
            assert response.ok is True

    asyncio.run(scenario())


def test_cancelling_serve_cleans_up_owned_files(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            server_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await server_task

            assert not valid_xdg_paths.socket_file.exists()
            assert not valid_xdg_paths.pid_file.exists()
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            runtime.stop()

    asyncio.run(scenario())


def test_server_instance_cannot_serve_twice(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        try:
            await server.wait_started()
            with pytest.raises(RuntimeError, match="only be called once"):
                await server.serve()
        finally:
            await server.close()
            await server_task
            runtime.stop()

    asyncio.run(scenario())


def test_close_requested_before_serve_still_cleans_up(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        try:
            await server.close()
            await server.serve()
            await server.wait_started()
            assert not valid_xdg_paths.socket_file.exists()
            assert not valid_xdg_paths.pid_file.exists()
        finally:
            await server.close()
            runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["missing", "refused"])
def test_unreachable_service_is_not_stale_success(tmp_path: Path, state: str) -> None:
    socket_path = tmp_path / "service.sock"
    if state == "refused":
        stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_socket.bind(str(socket_path))
        stale_socket.close()

    client = ServiceClient(socket_path, endpoint_validator=None)
    with pytest.raises(DrawingMachineError) as error:
        client.call("service.status", {})

    _assert_service_unavailable(error.value)


def test_client_does_not_cache_a_previous_success(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        client = ServiceClient(valid_xdg_paths.socket_file, endpoint_validator=None)
        async with _running_service(valid_xdg_paths):
            response = await asyncio.to_thread(client.call, "service.status", {})
            assert response.ok is True

        with pytest.raises(DrawingMachineError) as error:
            await asyncio.to_thread(client.call, "service.status", {})
        _assert_service_unavailable(error.value)

    asyncio.run(scenario())


def test_client_maps_response_timeout_to_service_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "service.sock"
        release = asyncio.Event()
        connection_tasks: list[asyncio.Task[None]] = []

        async def wait_without_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.readuntil(b"\n")
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            connection_tasks.append(asyncio.create_task(wait_without_response(reader, writer)))

        server = await asyncio.start_unix_server(accept, path=socket_path)
        try:
            client = ServiceClient(socket_path, timeout_s=0.05, endpoint_validator=None)
            with pytest.raises(DrawingMachineError) as error:
                await asyncio.to_thread(client.call, "service.status", {})
            _assert_service_unavailable(error.value)
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            if connection_tasks:
                await asyncio.gather(*connection_tasks)

    asyncio.run(scenario())


def test_client_maps_connection_reset_to_service_unavailable(tmp_path: Path) -> None:
    socket_path = tmp_path / "reset.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()
    thread_errors: list[BaseException] = []

    def reset_connection() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                assert connection.recv(1, socket.MSG_PEEK)
        except BaseException as error:
            thread_errors.append(error)

    reset_thread = threading.Thread(target=reset_connection, name="reset-unix-client")
    reset_thread.start()
    try:
        with pytest.raises(DrawingMachineError) as error:
            ServiceClient(socket_path, endpoint_validator=None).call("service.status", {}, request_id="req-reset")
        _assert_service_unavailable(error.value)
        assert error.value.payload.details["reason"] == "ConnectionResetError"
    finally:
        listener.close()
        reset_thread.join(timeout=3.0)

    assert reset_thread.is_alive() is False
    assert thread_errors == []


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (b"not-json\n", "PROTOCOL_INVALID"),
        (b'{"protocol_version":1', "PROTOCOL_EARLY_EOF"),
        (b"x" * (MAX_MESSAGE_BYTES + 1), "PROTOCOL_MESSAGE_TOO_LARGE"),
    ],
    ids=["malformed", "early-eof", "oversized"],
)
def test_client_maps_invalid_responses_to_protocol_errors(
    tmp_path: Path,
    response: bytes,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "service.sock"
        async with _raw_response_server(socket_path, response):
            with pytest.raises(DrawingMachineError) as error:
                await asyncio.to_thread(
                    ServiceClient(socket_path, endpoint_validator=None).call,
                    "service.status",
                    {},
                    request_id="req-invalid",
                )

        assert error.value.payload.code == expected_code
        assert error.value.payload.category is ErrorCategory.INPUT

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response_command", "response_request_id"),
    [
        ("other.command", "req-correlated"),
        ("service.status", "req-other"),
    ],
    ids=["command", "request-id"],
)
def test_client_rejects_a_response_for_another_request(
    tmp_path: Path,
    response_command: str,
    response_request_id: str,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "service.sock"
        response = encode_response(
            ProtocolResponse(
                protocol_version=1,
                schema_version=1,
                ok=True,
                command=response_command,
                request_id=response_request_id,
                data={"status": "RUNNING"},
                error=None,
            )
        )
        async with _raw_response_server(socket_path, response):
            with pytest.raises(DrawingMachineError) as error:
                await asyncio.to_thread(
                    ServiceClient(socket_path, endpoint_validator=None).call,
                    "service.status",
                    {},
                    request_id="req-correlated",
                )

        assert error.value.payload.code == "PROTOCOL_INVALID"
        assert error.value.payload.category is ErrorCategory.INPUT

    asyncio.run(scenario())


def test_close_terminates_stalled_connections_without_leaking_tasks(valid_xdg_paths: XdgPaths) -> None:
    async def scenario() -> None:
        baseline_tasks = set(asyncio.all_tasks())
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        writer: asyncio.StreamWriter | None = None
        try:
            await server.wait_started()
            reader, writer = await asyncio.open_unix_connection(valid_xdg_paths.socket_file)
            writer.write(b"{")
            await writer.drain()
            await asyncio.sleep(0)

            await server.close()
            await server_task

            try:
                connection_ended = await asyncio.wait_for(reader.read(), timeout=1.0) == b""
            except ConnectionResetError:
                connection_ended = True
            assert connection_ended is True
            await asyncio.sleep(0)
            leaked_tasks = [task for task in asyncio.all_tasks() if task not in baseline_tasks and not task.done()]
            assert leaked_tasks == []
        finally:
            await server.close()
            if not server_task.done():
                await server_task
            if writer is not None:
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()
            runtime.stop()

    asyncio.run(scenario())


def test_cleanup_failure_wakes_close_and_releases_descriptors_and_lease(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        baseline_descriptors = len(tuple(Path("/proc/self/fd").iterdir()))
        baseline_tasks = set(asyncio.all_tasks())
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        server_task = asyncio.create_task(server.serve())
        original_unlink = server_module._unlink_if_owned

        def deny_socket_unlink(
            path: Path,
            identity: tuple[int, int],
            *,
            require_socket: bool,
        ) -> bool:
            if path == valid_xdg_paths.socket_file:
                raise PermissionError("forced socket cleanup failure")
            return original_unlink(path, identity, require_socket=require_socket)

        try:
            await server.wait_started()
            monkeypatch.setattr(server_module, "_unlink_if_owned", deny_socket_unlink)
            with pytest.raises(PermissionError, match="forced socket cleanup failure"):
                await asyncio.wait_for(server.close(), timeout=1.0)
            with pytest.raises(PermissionError, match="forced socket cleanup failure"):
                await server_task

            assert server._owned_socket_descriptor is None
            assert server._owned_pid_descriptor is None
            assert server._lease_descriptor is None

            monkeypatch.setattr(server_module, "_unlink_if_owned", original_unlink)
            recovery = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
            recovery_task = asyncio.create_task(recovery.serve())
            await recovery.wait_started()
            await recovery.close()
            await recovery_task
        finally:
            monkeypatch.setattr(server_module, "_unlink_if_owned", original_unlink)
            if not server_task.done():
                server_task.cancel()
                with suppress(BaseException):
                    await server_task
            else:
                with suppress(BaseException):
                    server_task.result()
            valid_xdg_paths.socket_file.unlink(missing_ok=True)
            valid_xdg_paths.pid_file.unlink(missing_ok=True)
            runtime.stop()

        await asyncio.sleep(0)
        assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline_descriptors
        leaked_tasks = [task for task in asyncio.all_tasks() if task not in baseline_tasks and not task.done()]
        assert leaked_tasks == []

    asyncio.run(scenario())


def test_connection_arriving_after_closing_awaits_writer_shutdown(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        baseline_tasks = set(asyncio.all_tasks())
        runtime = build_runtime(valid_xdg_paths)
        runtime.start()
        closing_server = UnixServiceServer(valid_xdg_paths.socket_file, runtime.dispatcher, SystemClock())
        await closing_server.close()
        writer_waited = asyncio.Event()
        accepted = asyncio.Event()

        def route_late_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            original_wait_closed = writer.wait_closed

            async def tracked_wait_closed() -> None:
                writer_waited.set()
                await original_wait_closed()

            writer.wait_closed = tracked_wait_closed  # type: ignore[method-assign]
            closing_server._accept_client(reader, writer)
            accepted.set()

        socket_path = tmp_path / "late.sock"
        listener = await asyncio.start_unix_server(route_late_connection, path=socket_path)
        client_writer: asyncio.StreamWriter | None = None
        try:
            client_reader, client_writer = await asyncio.open_unix_connection(socket_path)
            await accepted.wait()
            assert await closing_server._shutdown() is None
            await asyncio.wait_for(writer_waited.wait(), timeout=1.0)
            assert await asyncio.wait_for(client_reader.read(), timeout=1.0) == b""
            await asyncio.sleep(0)
            leaked_tasks = [task for task in asyncio.all_tasks() if task not in baseline_tasks and not task.done()]
            assert leaked_tasks == []
        finally:
            listener.close()
            await listener.wait_closed()
            if client_writer is not None:
                client_writer.close()
                await client_writer.wait_closed()
            runtime.stop()

    asyncio.run(scenario())
