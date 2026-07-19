from __future__ import annotations

import argparse
from collections.abc import Iterable

import pytest

from drawingmachine.cli.gcode import check_gcode
from drawingmachine.cli.jobs import wait_for_job
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolResponse


def _response(state: str, *, ok: bool = True) -> ProtocolResponse:
    return ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        ok,
        "job.status",
        f"req-{state.lower()}",
        {
            "schema_version": 1,
            "job": {"job_id": "job-1", "state": state},
            "artifacts": [],
            "cancellation_requested": False,
        }
        if ok
        else {},
        None if ok else ErrorPayload("JOB_NOT_FOUND", ErrorCategory.INPUT, "job not found", False, {}, job_id="job-1"),
    )


class ScriptedClient:
    def __init__(self, responses: Iterable[ProtocolResponse]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, object, float | None]] = []

    def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
        self.calls.append((command, arguments, timeout_s))
        return next(self.responses)


class FakeMonotonic:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _namespace(*, timeout: float = 1.0, interval: float = 0.01) -> argparse.Namespace:
    return argparse.Namespace(job_id="job-1", timeout_seconds=timeout, poll_interval_seconds=interval)


def test_job_wait_polls_status_without_sending_job_wait() -> None:
    client = ScriptedClient([_response("VALIDATING_GCODE"), _response("READY_TO_RUN")])

    result = wait_for_job(_namespace(), client=client, sleep=lambda _: None, monotonic=FakeMonotonic())

    assert [call[0] for call in client.calls] == ["job.status", "job.status"]
    assert all(call[1] == {"schema_version": 1, "job_id": "job-1"} for call in client.calls)
    assert all(isinstance(call[2], float) and 0 < call[2] <= 1.0 for call in client.calls)
    assert result.command == "job.wait"
    job = result.data["job"]
    assert isinstance(job, dict)
    assert job["state"] == "READY_TO_RUN"


def test_job_wait_preserves_status_failure_under_public_command() -> None:
    client = ScriptedClient([_response("MISSING", ok=False)])

    result = wait_for_job(_namespace(), client=client)

    assert result.ok is False
    assert result.command == "job.wait"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"


def test_job_wait_timeout_is_local_and_does_not_cancel() -> None:
    client = ScriptedClient([_response("QUEUED")])
    clock = FakeMonotonic()

    result = wait_for_job(
        _namespace(timeout=0.01),
        client=client,
        sleep=lambda seconds: clock.advance(seconds),
        monotonic=clock,
    )

    assert [call[0] for call in client.calls] == ["job.status"]
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"


@pytest.mark.parametrize("field,value", [("timeout_seconds", 0.0), ("poll_interval_seconds", 0.0)])
def test_job_wait_rejects_nonpositive_timing(field: str, value: float) -> None:
    client = ScriptedClient([])
    args = _namespace()
    setattr(args, field, value)

    result = wait_for_job(args, client=client)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_ARGUMENT_INVALID"
    assert client.calls == []


def test_job_wait_maps_ctrl_c_without_cancel_request() -> None:
    client = ScriptedClient([_response("QUEUED")])

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    result = wait_for_job(
        _namespace(),
        client=client,
        sleep=interrupt,
        monotonic=FakeMonotonic(),
    )

    assert [call[0] for call in client.calls] == ["job.status"]
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_INTERRUPTED"


def test_job_wait_slow_terminal_response_after_deadline_is_timeout() -> None:
    clock = FakeMonotonic()

    class SlowClient(ScriptedClient):
        def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
            response = super().call(command, arguments, timeout_s=timeout_s)
            clock.advance(1.01)
            return response

    client = SlowClient([_response("READY_TO_RUN")])

    result = wait_for_job(_namespace(timeout=1.0), client=client, monotonic=clock)

    assert len(client.calls) == 1
    assert client.calls[0][2] == pytest.approx(1.0)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"


def test_job_wait_oversleep_rechecks_deadline_without_extra_poll() -> None:
    clock = FakeMonotonic()
    client = ScriptedClient([_response("QUEUED")])
    sleeps: list[float] = []

    def oversleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds + 0.01)

    result = wait_for_job(
        _namespace(timeout=0.05, interval=1.0),
        client=client,
        sleep=oversleep,
        monotonic=clock,
    )

    assert len(client.calls) == 1
    assert sleeps == [pytest.approx(0.05)]
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"


def test_job_wait_passes_subsecond_remaining_timeout_to_each_status_call() -> None:
    clock = FakeMonotonic()
    client = ScriptedClient([_response("READY_TO_RUN")])

    result = wait_for_job(_namespace(timeout=0.025), client=client, monotonic=clock)

    assert result.ok is True
    assert client.calls[0][2] == pytest.approx(0.025)


def test_job_wait_call_failure_at_deadline_becomes_timeout() -> None:
    clock = FakeMonotonic()

    class FailingClient:
        calls = 0

        def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
            del command, arguments, timeout_s
            self.calls += 1
            clock.advance(1.0)
            raise DrawingMachineError(
                ErrorPayload("SERVICE_UNAVAILABLE", ErrorCategory.SERVICE, "unavailable", True, {})
            )

    client = FailingClient()
    result = wait_for_job(_namespace(timeout=1.0), client=client, monotonic=clock)

    assert client.calls == 1
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"


def test_job_wait_call_failure_before_deadline_preserves_service_error() -> None:
    clock = FakeMonotonic()
    expected = DrawingMachineError(ErrorPayload("SERVICE_UNAVAILABLE", ErrorCategory.SERVICE, "unavailable", True, {}))

    class FailingClient:
        def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
            del command, arguments, timeout_s
            raise expected

    with pytest.raises(DrawingMachineError) as error:
        wait_for_job(_namespace(timeout=1.0), client=FailingClient(), monotonic=clock)

    assert error.value is expected


def test_gcode_check_uses_one_deadline_for_enqueue_and_status_poll() -> None:
    clock = FakeMonotonic()
    submitted = ProtocolResponse(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        True,
        "gcode.check",
        "req-submit",
        {"schema_version": 1, "job_id": "gcode-job", "state": "QUEUED", "static_result": None},
        None,
    )

    class GcodeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float | None]] = []

        def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
            del arguments
            self.calls.append((command, timeout_s))
            if command == "gcode.check":
                clock.advance(0.02)
                return submitted
            clock.advance(0.031)
            return _response("COMPLETED")

    class Reader:
        def read_bytes(self, *args: object, **kwargs: object) -> bytes:
            pytest.fail("expired terminal status must not read an artifact")

    client = GcodeClient()
    result = check_gcode(
        argparse.Namespace(path="candidate.gcode", timeout_seconds=0.05, poll_interval_seconds=0.01),
        client=client,
        artifact_reader=Reader(),
        monotonic=clock,
        sleep=lambda seconds: clock.advance(seconds),
    )

    assert [call[0] for call in client.calls] == ["gcode.check", "job.status"]
    assert client.calls[0][1] == pytest.approx(0.05)
    assert client.calls[1][1] == pytest.approx(0.03)
    assert result.ok is False
    assert result.command == "gcode.check"
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"


def test_gcode_check_artifact_read_cannot_exceed_end_to_end_deadline() -> None:
    clock = FakeMonotonic()
    contents = b'{"ok":true}'
    responses = iter(
        [
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "gcode.check",
                "req-submit",
                {"schema_version": 1, "job_id": "gcode-job", "state": "QUEUED", "static_result": None},
                None,
            ),
            ProtocolResponse(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                True,
                "job.status",
                "req-status",
                {
                    "schema_version": 1,
                    "job": {"job_id": "gcode-job", "state": "COMPLETED"},
                    "artifacts": [
                        {
                            "role": "gcode_static",
                            "relative_path": "artifacts/attempt-1/gcode_static.json",
                            "sha256": "0" * 64,
                            "size_bytes": len(contents),
                            "media_type": "application/json",
                        }
                    ],
                    "cancellation_requested": False,
                },
                None,
            ),
        ]
    )

    class Client:
        def call(self, command: str, arguments: object, *, timeout_s: float | None = None) -> ProtocolResponse:
            del command, arguments, timeout_s
            return next(responses)

    class Reader:
        def read_bytes(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            clock.advance(0.051)
            return contents

    result = check_gcode(
        argparse.Namespace(path="candidate.gcode", timeout_seconds=0.05, poll_interval_seconds=0.01),
        client=Client(),
        artifact_reader=Reader(),
        monotonic=clock,
        sleep=lambda seconds: clock.advance(seconds),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "JOB_WAIT_TIMEOUT"
