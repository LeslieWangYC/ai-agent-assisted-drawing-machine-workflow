import asyncio
import logging
import os
import socket
import stat
import struct
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import uuid4

import drawingmachine.service._instance as instance_paths
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.clock import Clock
from drawingmachine.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    ProtocolResponse,
    decode_request,
    encode_response,
)
from drawingmachine.service.access_policy import RuntimeAccessPolicy
from drawingmachine.service.dispatcher import CommandDispatcher
from drawingmachine.service.models import PeerCredentials, RequestContext

_unlink_if_owned = instance_paths.unlink_if_owned

_LOGGER = logging.getLogger(__name__)
_PEER_CREDENTIALS_SIZE = struct.calcsize("3i")
_STALE_PROBE_TIMEOUT_S = 0.2
_PROC_NET_UNIX = Path("/proc/net/unix")


class UnixServiceServer:
    def __init__(
        self,
        socket_path: Path,
        dispatcher: CommandDispatcher,
        clock: Clock,
        *,
        instance_lease: instance_paths.InstanceLease | None = None,
        access_policy: RuntimeAccessPolicy | None = None,
        periodic_maintenance: Callable[[], None] | None = None,
        pre_ready_maintenance: Callable[[], None] | None = None,
        maintenance_interval_s: float = 30.0,
    ) -> None:
        self._socket_path = socket_path
        self._pid_path = socket_path.with_suffix(".pid")
        self._dispatcher = dispatcher
        self._clock = clock
        self._server: asyncio.AbstractServer | None = None
        self._owned_socket: instance_paths.FileIdentity | None = None
        self._owned_socket_descriptor: int | None = None
        self._owned_pid: instance_paths.FileIdentity | None = None
        self._owned_pid_descriptor: int | None = None
        self._pid: int | None = None
        self._access_policy = access_policy
        if maintenance_interval_s <= 0:
            raise ValueError("maintenance_interval_s must be positive")
        self._periodic_maintenance = periodic_maintenance
        self._pre_ready_maintenance = pre_ready_maintenance
        self._maintenance_interval_s = maintenance_interval_s
        self._maintenance_task: asyncio.Task[None] | None = None
        self._instance_lease = (
            instance_paths.InstanceLease(socket_path, access_policy) if instance_lease is None else instance_lease
        )
        self._owns_instance_lease = instance_lease is None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._late_writer_tasks: set[asyncio.Task[None]] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()
        self._startup_done = asyncio.Event()
        self._closed = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._cleanup_error: BaseException | None = None
        self._serve_called = False
        self._close_requested = False
        self._closing = False

    async def serve(self) -> None:
        if self._serve_called:
            raise RuntimeError("UnixServiceServer.serve() may only be called once")
        self._serve_called = True
        listener: socket.socket | None = None
        failure: BaseException | None = None
        cleanup_error: BaseException | None = None

        try:
            if self._owns_instance_lease:
                self._instance_lease.acquire()
            else:
                self._instance_lease.require_held_for(self._socket_path)
            self._remove_proven_stale_socket()
            listener = self._bind_listener()
            self._write_pid_file()
            if self._access_policy is not None:
                assert self._owned_socket is not None
                self._access_policy.validate_runtime_directory()
                self._access_policy.validate_bound_socket(self._owned_socket)
            if self._pre_ready_maintenance is not None:
                self._pre_ready_maintenance()
            self._server = await asyncio.start_unix_server(
                self._accept_client,
                sock=listener,
                limit=MAX_MESSAGE_BYTES,
            )
            listener = None
            if self._periodic_maintenance is not None:
                self._maintenance_task = asyncio.create_task(self._run_periodic_maintenance())

            serving: asyncio.Task[None] | None = None
            if not self._close_requested:
                serving = asyncio.create_task(self._server.serve_forever())
                await asyncio.sleep(0)
                if serving.done():
                    await serving
            self._startup_done.set()

            if serving is not None:
                try:
                    if self._maintenance_task is None:
                        await serving
                    else:
                        completed, _ = await asyncio.wait(
                            (serving, self._maintenance_task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if self._maintenance_task in completed:
                            await self._maintenance_task
                            raise RuntimeError("periodic maintenance stopped unexpectedly")
                        await serving
                finally:
                    if not serving.done():
                        serving.cancel()
                        with suppress(asyncio.CancelledError):
                            await serving
        except asyncio.CancelledError as error:
            self._record_startup_error(error)
            if not self._close_requested:
                failure = error
        except BaseException as error:
            self._record_startup_error(error)
            failure = error
        finally:
            if listener is not None:
                try:
                    listener.close()
                except BaseException as error:
                    cleanup_error = error
            try:
                shutdown_error = await self._shutdown()
            except BaseException as error:
                shutdown_error = error
            finally:
                descriptor_error = self._close_owned_descriptors()
                try:
                    if self._owns_instance_lease:
                        self._instance_lease.release()
                    lease_error: BaseException | None = None
                except BaseException as error:
                    lease_error = error
                cleanup_error = cleanup_error or shutdown_error or descriptor_error or lease_error
                self._cleanup_error = cleanup_error
                if not self._startup_done.is_set():
                    self._startup_error = (
                        failure or cleanup_error or RuntimeError("service server stopped before startup completed")
                    )
                    self._startup_done.set()
                self._closed.set()

        if failure is not None:
            raise failure
        if cleanup_error is not None:
            raise cleanup_error

    async def wait_started(self) -> None:
        await self._startup_done.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def close(self) -> None:
        self._close_requested = True
        self._closing = True
        if self._server is not None:
            self._server.close()
        if self._serve_called and not self._closed.is_set():
            await self._closed.wait()
        if self._cleanup_error is not None:
            raise self._cleanup_error

    async def _run_periodic_maintenance(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._maintenance_interval_s)
            assert self._periodic_maintenance is not None
            self._periodic_maintenance()

    def _record_startup_error(self, error: BaseException) -> None:
        if not self._startup_done.is_set():
            self._startup_error = error
            self._startup_done.set()

    def _remove_proven_stale_socket(self) -> None:
        existing = instance_paths.lstat(self._socket_path)
        if existing is None:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            raise instance_paths.socket_in_use(self._socket_path, "service socket path is not a socket")

        try:
            ownership_descriptor = instance_paths.open_path_descriptor(self._socket_path)
        except FileNotFoundError:
            return
        try:
            owned_stat = os.fstat(ownership_descriptor)
            identity = instance_paths.identity(owned_stat)
            if not stat.S_ISSOCK(owned_stat.st_mode) or identity != instance_paths.identity(existing):
                raise instance_paths.socket_in_use(self._socket_path, "service socket changed before stale probe")

            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(_STALE_PROBE_TIMEOUT_S)
            try:
                probe.connect(os.fspath(self._socket_path))
            except ConnectionRefusedError as connection_error:
                try:
                    kernel_socket_is_open = _kernel_has_unix_socket(self._socket_path)
                except OSError as proc_error:
                    raise instance_paths.socket_in_use(
                        self._socket_path,
                        "existing service socket kernel state could not be verified",
                    ) from proc_error
                if kernel_socket_is_open:
                    raise instance_paths.socket_in_use(
                        self._socket_path,
                        "existing service socket is still open in the kernel",
                    ) from connection_error
            except FileNotFoundError:
                return
            except OSError as error:
                raise instance_paths.socket_in_use(
                    self._socket_path,
                    "existing service socket could not be proven stale",
                ) from error
            else:
                raise instance_paths.socket_in_use(self._socket_path, "drawingmachine service is already running")
            finally:
                probe.close()

            current = instance_paths.lstat(self._socket_path)
            if current is None:
                return
            if not stat.S_ISSOCK(current.st_mode) or instance_paths.identity(current) != identity:
                raise instance_paths.socket_in_use(self._socket_path, "service socket changed during stale probe")
            _unlink_if_owned(self._socket_path, identity, require_socket=True)
        finally:
            os.close(ownership_descriptor)

    def _bind_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ownership_descriptor: int | None = None
        try:
            listener.bind(os.fspath(self._socket_path))
            socket_stat = os.lstat(self._socket_path)
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise instance_paths.socket_in_use(self._socket_path, "bound service socket path changed type")
            identity = instance_paths.identity(socket_stat)
            self._owned_socket = identity
            ownership_descriptor = instance_paths.open_path_descriptor(self._socket_path)
            owned_stat = os.fstat(ownership_descriptor)
            if not stat.S_ISSOCK(owned_stat.st_mode) or instance_paths.identity(owned_stat) != identity:
                raise instance_paths.socket_in_use(self._socket_path, "bound service socket path changed")
            self._owned_socket_descriptor = ownership_descriptor
            ownership_descriptor = None

            if self._access_policy is None:
                self._socket_path.chmod(0o600)
            else:
                self._access_policy.apply_and_validate_bound_socket(identity)
            current = os.lstat(self._socket_path)
            if not stat.S_ISSOCK(current.st_mode) or instance_paths.identity(current) != identity:
                raise instance_paths.socket_in_use(self._socket_path, "bound service socket path changed")
            _listen_socket(listener)
            listener.setblocking(False)
        except BaseException:
            if ownership_descriptor is not None:
                os.close(ownership_descriptor)
            listener.close()
            raise
        return listener

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            task = asyncio.create_task(_wait_writer_closed(writer))
            self._late_writer_tasks.add(task)
            task.add_done_callback(self._late_writer_tasks.discard)
            return

        self._client_writers.add(writer)
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._access_policy is not None:
                assert self._owned_socket is not None
                self._access_policy.validate_bound_socket(self._owned_socket)
            frame = await _read_frame(reader)
            try:
                request = decode_request(frame)
            except DrawingMachineError as error:
                writer.write(encode_response(_safe_protocol_error_response(error.payload)))
                await writer.drain()
                return
            context = RequestContext(
                peer=_peer_credentials(writer),
                received_at=self._clock.now(),
            )
            response = self._dispatcher.dispatch(request, context)
            writer.write(encode_response(response))
            await writer.drain()
        except (DrawingMachineError, ConnectionError, OSError):
            pass
        except Exception:
            _LOGGER.exception("unexpected Unix service connection failure")
        finally:
            self._client_writers.discard(writer)
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _write_pid_file(self) -> None:
        pid = os.getpid()
        payload = f"{pid}\n".encode("ascii")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._pid_path.name}.",
            suffix=".tmp",
            dir=self._pid_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            temporary_stat = os.fstat(descriptor)
            if not stat.S_ISREG(temporary_stat.st_mode):
                raise RuntimeError("temporary PID path is not a regular file")
            temporary_identity = instance_paths.identity(temporary_stat)
            os.replace(temporary_path, self._pid_path)

            self._pid = pid
            self._owned_pid = temporary_identity
            self._owned_pid_descriptor = descriptor
            descriptor = -1

            published_stat = os.lstat(self._pid_path)
            if (
                not stat.S_ISREG(published_stat.st_mode)
                or instance_paths.identity(published_stat) != temporary_identity
                or not instance_paths.pid_target_matches(self._pid_path, temporary_identity, payload)
            ):
                raise RuntimeError("PID path changed during publication")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    async def _shutdown(self) -> BaseException | None:
        cleanup_error: BaseException | None = None
        self._closing = True
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._maintenance_task
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except BaseException as error:
                cleanup_error = error

        for writer in tuple(self._client_writers):
            try:
                writer.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        tasks = tuple(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        self._client_tasks.clear()
        self._client_writers.clear()

        await asyncio.sleep(0)
        while self._late_writer_tasks:
            late_tasks = tuple(self._late_writer_tasks)
            try:
                await asyncio.gather(*late_tasks, return_exceptions=True)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            finally:
                self._late_writer_tasks.difference_update(late_tasks)

        try:
            self._remove_owned_pid_file()
        except BaseException as error:
            cleanup_error = cleanup_error or error
        if self._owned_socket is not None:
            try:
                _unlink_if_owned(self._socket_path, self._owned_socket, require_socket=True)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            self._owned_socket = None
        return cleanup_error

    def _close_owned_descriptors(self) -> BaseException | None:
        cleanup_error: BaseException | None = None
        pid_descriptor = self._owned_pid_descriptor
        self._owned_pid_descriptor = None
        if pid_descriptor is not None:
            try:
                os.close(pid_descriptor)
            except BaseException as error:
                cleanup_error = error

        socket_descriptor = self._owned_socket_descriptor
        self._owned_socket_descriptor = None
        if socket_descriptor is not None:
            try:
                os.close(socket_descriptor)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        return cleanup_error

    @property
    def _lease_descriptor(self) -> int | None:
        return self._instance_lease.descriptor

    def _remove_owned_pid_file(self) -> None:
        if self._owned_pid is None or self._owned_pid_descriptor is None or self._pid is None:
            return
        pid_stat = instance_paths.lstat(self._pid_path)
        if (
            pid_stat is None
            or not stat.S_ISREG(pid_stat.st_mode)
            or instance_paths.identity(pid_stat) != self._owned_pid
        ):
            return
        canonical_content = f"{self._pid}\n".encode("ascii")
        if not instance_paths.descriptor_has_exact_content(
            self._owned_pid_descriptor,
            self._owned_pid,
            canonical_content,
        ):
            return
        _unlink_if_owned(self._pid_path, self._owned_pid, require_socket=False)
        self._owned_pid = None


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    try:
        frame = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError as error:
        raise _protocol_error(
            "PROTOCOL_MESSAGE_TOO_LARGE",
            "protocol message too large",
            details={"maximum_bytes": MAX_MESSAGE_BYTES, "actual_bytes": error.consumed},
        ) from error
    except asyncio.IncompleteReadError as error:
        raise _protocol_error("PROTOCOL_EARLY_EOF", "early EOF while reading protocol message") from error
    if len(frame) > MAX_MESSAGE_BYTES:
        raise _protocol_error(
            "PROTOCOL_MESSAGE_TOO_LARGE",
            "protocol message too large",
            details={"maximum_bytes": MAX_MESSAGE_BYTES, "actual_bytes": len(frame)},
        )
    return frame


async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


def _peer_credentials(writer: asyncio.StreamWriter) -> PeerCredentials:
    peer_socket = cast(socket.socket | None, writer.get_extra_info("socket"))
    if peer_socket is None:
        raise RuntimeError("Unix connection has no socket")
    raw_credentials = peer_socket.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        _PEER_CREDENTIALS_SIZE,
    )
    pid, uid, gid = struct.unpack("3i", raw_credentials)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = payload
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write service PID file")
        remaining = remaining[written:]


def _listen_socket(listener: socket.socket) -> None:
    listener.listen(socket.SOMAXCONN)


def _kernel_has_unix_socket(path: Path) -> bool:
    target = os.fsencode(path)
    with _PROC_NET_UNIX.open("rb") as entries:
        for line in entries:
            fields = line.rstrip(b"\n").split(maxsplit=7)
            if len(fields) == 8 and fields[7] == target:
                return True
    return False


def _protocol_error(
    code: str,
    message: str,
    *,
    details: JsonObject | None = None,
) -> DrawingMachineError:
    return DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details={} if details is None else details,
        )
    )


def _safe_protocol_error_response(payload: ErrorPayload) -> ProtocolResponse:
    request_id = str(uuid4())
    if payload.code == "PROTOCOL_UNSUPPORTED_VERSION":
        code = payload.code
        message = "unsupported protocol version"
        details: JsonObject = {"expected": PROTOCOL_VERSION}
    else:
        code = "PROTOCOL_INVALID"
        message = "malformed protocol request"
        details = {}
    return ProtocolResponse(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        ok=False,
        command="protocol.error",
        request_id=request_id,
        data={},
        error=ErrorPayload(
            code=code,
            category=ErrorCategory.INPUT,
            message=message,
            retryable=False,
            details=details,
            request_id=request_id,
        ),
    )
