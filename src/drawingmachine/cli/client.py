import math
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from drawingmachine.cli.service_access import ValidatedClientEndpointV1
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.client_data import DispatchWriteOutcome
from drawingmachine.protocol import (
    PROTOCOL_VERSION,
    ProtocolRequest,
    ProtocolResponse,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)

_AUTO_ENDPOINT_VALIDATOR = object()


class ServiceDispatchError(DrawingMachineError):
    def __init__(self, payload: ErrorPayload, send_outcome: DispatchWriteOutcome) -> None:
        super().__init__(payload)
        self.send_outcome = send_outcome


class ServiceClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_s: float = 5.0,
        endpoint_validator: Callable[[], ValidatedClientEndpointV1] | None | object = _AUTO_ENDPOINT_VALIDATOR,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_s = timeout_s
        self._endpoint_validator: Callable[[], ValidatedClientEndpointV1] | None
        if endpoint_validator is _AUTO_ENDPOINT_VALIDATOR:
            self._endpoint_validator = lambda: _installed_endpoint_validator(socket_path)()
        elif endpoint_validator is None or callable(endpoint_validator):
            self._endpoint_validator = cast(Callable[[], ValidatedClientEndpointV1] | None, endpoint_validator)
        else:
            raise TypeError("endpoint_validator must be callable, None, or omitted")

    def call(
        self,
        command: str,
        arguments: JsonObject,
        *,
        request_id: str | None = None,
        timeout_s: float | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> ProtocolResponse:
        request = ProtocolRequest(
            protocol_version=PROTOCOL_VERSION,
            command=command,
            request_id=str(uuid4()) if request_id is None else request_id,
            arguments=arguments,
        )
        encoded_request = encode_request(request)
        if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be finite and greater than zero")
        effective_timeout = self._timeout_s if timeout_s is None else min(self._timeout_s, float(timeout_s))

        validated = None if self._endpoint_validator is None else self._endpoint_validator()
        connect_path = self._socket_path if validated is None else validated.canonical_socket
        write_possible = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(effective_timeout)
                connection.connect(os.fspath(connect_path))
                if (
                    validated is not None
                    and self._endpoint_validator is not None
                    and self._endpoint_validator() != validated
                ):
                    raise DrawingMachineError(
                        ErrorPayload(
                            code="SERVICE_ENDPOINT_INVALID",
                            category=ErrorCategory.PERMISSION,
                            message="service endpoint changed during connect",
                            retryable=False,
                            details={},
                            request_id=request.request_id,
                        )
                    )
                stream = cast(BinaryIO, connection.makefile("rwb", buffering=0))
                with stream:
                    if before_write is not None:
                        write_possible = True
                        before_write()
                    write_frame(stream, encoded_request)
                    response = decode_response(read_frame(stream))
                    if response.command != request.command or response.request_id != request.request_id:
                        raise DrawingMachineError(
                            ErrorPayload(
                                code="PROTOCOL_INVALID",
                                category=ErrorCategory.INPUT,
                                message="service response does not match request",
                                retryable=False,
                                details={
                                    "expected_command": request.command,
                                    "received_command": response.command,
                                    "expected_request_id": request.request_id,
                                    "received_request_id": response.request_id,
                                },
                                request_id=request.request_id,
                            )
                        )
                    return response
        except DrawingMachineError as error:
            if before_write is not None:
                raise ServiceDispatchError(
                    error.payload,
                    DispatchWriteOutcome.WRITE_POSSIBLE
                    if write_possible
                    else DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES,
                ) from error
            raise
        except PermissionError as error:
            payload = ErrorPayload(
                code="SERVICE_PERMISSION_DENIED",
                category=ErrorCategory.PERMISSION,
                message="permission denied while connecting to drawingmachine service",
                retryable=False,
                details={
                    "socket": os.fspath(self._socket_path),
                    "reason": type(error).__name__,
                },
                request_id=request.request_id,
            )
            if before_write is not None:
                raise ServiceDispatchError(
                    payload,
                    DispatchWriteOutcome.WRITE_POSSIBLE
                    if write_possible
                    else DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES,
                ) from error
            raise DrawingMachineError(payload) from error
        except OSError as error:
            payload = ErrorPayload(
                code="SERVICE_UNAVAILABLE",
                category=ErrorCategory.SERVICE,
                message="drawingmachine service is unavailable",
                retryable=True,
                details={
                    "socket": os.fspath(self._socket_path),
                    "reason": type(error).__name__,
                },
                request_id=request.request_id,
            )
            if before_write is not None:
                raise ServiceDispatchError(
                    payload,
                    DispatchWriteOutcome.WRITE_POSSIBLE
                    if write_possible
                    else DispatchWriteOutcome.DEFINITELY_UNSENT_ZERO_BYTES,
                ) from error
            raise DrawingMachineError(payload) from error


def _installed_endpoint_validator(socket_path: Path) -> Callable[[], ValidatedClientEndpointV1]:
    from drawingmachine.cli.service_access import (
        load_client_access_snapshot,
        select_client_endpoint,
        validate_client_endpoint,
    )
    from drawingmachine.config import resolve_xdg_paths

    try:
        paths = resolve_xdg_paths()
        if socket_path != paths.socket_file:
            raise DrawingMachineError(
                ErrorPayload(
                    code="SERVICE_ENDPOINT_INVALID",
                    category=ErrorCategory.PERMISSION,
                    message="client socket is not the current XDG service endpoint",
                    retryable=False,
                    details={},
                )
            )
        snapshot = load_client_access_snapshot(paths)
        endpoint, principal = select_client_endpoint(snapshot)
        if socket_path != endpoint:
            raise DrawingMachineError(
                ErrorPayload(
                    code="SERVICE_ENDPOINT_INVALID",
                    category=ErrorCategory.PERMISSION,
                    message="client socket is not the selected private service endpoint",
                    retryable=False,
                    details={},
                )
            )
    except DrawingMachineError:
        raise
    except OSError as error:
        raise DrawingMachineError(
            ErrorPayload(
                code="SERVICE_ENDPOINT_INVALID",
                category=ErrorCategory.PERMISSION,
                message="installed service endpoint policy cannot be inspected",
                retryable=False,
                details={"reason": type(error).__name__},
            )
        ) from error
    return lambda: validate_client_endpoint(snapshot, endpoint, principal)


__all__ = ["ServiceClient", "ServiceDispatchError"]
