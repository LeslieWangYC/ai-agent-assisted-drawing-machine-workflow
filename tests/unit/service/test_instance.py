from __future__ import annotations

import socket
from pathlib import Path

import pytest

from drawingmachine.service._instance import InstanceLease, identity, unlink_if_owned


def test_instance_lease_rejects_duplicate_acquisition_and_wrong_handoff(tmp_path: Path) -> None:
    socket_path = tmp_path / "run/service.sock"
    lease = InstanceLease(socket_path)
    lease.acquire()
    try:
        lease.require_held_for(socket_path)
        with pytest.raises(RuntimeError, match="already acquired"):
            lease.acquire()
        with pytest.raises(RuntimeError, match="handoff is not held"):
            lease.require_held_for(tmp_path / "other.sock")
    finally:
        lease.release()
    lease.release()


def test_owned_path_unlink_rejects_mismatched_file_types(tmp_path: Path) -> None:
    regular_path = tmp_path / "service.pid"
    regular_path.write_text("7\n", encoding="ascii")
    assert unlink_if_owned(regular_path, identity(regular_path.stat()), require_socket=True) is False
    assert regular_path.is_file()

    socket_path = tmp_path / "service.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    try:
        assert unlink_if_owned(socket_path, identity(socket_path.stat()), require_socket=False) is False
        assert socket_path.exists()
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)
