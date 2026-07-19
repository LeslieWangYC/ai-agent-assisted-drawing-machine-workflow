from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, TypeAlias, cast

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    FluidNCSession,
    HomeAllAxesRequest,
    HomeOutcome,
    HomeZAxisRequest,
    HomeZOutcome,
    InitialPreflightRequest,
    OpenSessionRequest,
    PreflightOutcome,
    StreamOutcome,
    StreamProgramRequest,
    StreamProgressCallback,
    StreamProgressUpdate,
    ZCalibrationOutcome,
    ZCalibrationRequest,
    ZConfirmationOutcome,
    ZConfirmationRequest,
)


class FakeFluidNCOperation(StrEnum):
    INITIAL_PREFLIGHT = "INITIAL_PREFLIGHT"
    HOME_ALL_AXES = "HOME_ALL_AXES"
    Z_CALIBRATION = "Z_CALIBRATION"
    Z_CONFIRMATION_RAISE = "Z_CONFIRMATION_RAISE"
    STREAM_PROGRAM = "STREAM_PROGRAM"
    HOME_Z_AXIS = "HOME_Z_AXIS"
    CLOSE = "CLOSE"


class FakeBarrierScenario(StrEnum):
    CONCURRENT_APPROVAL = "CONCURRENT_APPROVAL"
    SHUTDOWN = "SHUTDOWN"
    LOST_ACK = "LOST_ACK"
    PARTIAL_STREAM = "PARTIAL_STREAM"
    CONTROLLER_RESET = "CONTROLLER_RESET"
    COMMIT_AFTER_SIDE_EFFECT = "COMMIT_AFTER_SIDE_EFFECT"
    CONNECTION_LOSS = "CONNECTION_LOSS"


class FakeBarrierPoint(StrEnum):
    BEFORE_OPERATION = "BEFORE_OPERATION"
    AFTER_ACKNOWLEDGED_PREFIX = "AFTER_ACKNOWLEDGED_PREFIX"
    AFTER_SIDE_EFFECT = "AFTER_SIDE_EFFECT"


class FakeFluidNCBarrier:
    __slots__ = ("_acknowledged_prefix", "_entered", "_point", "_released", "_scenario")

    def __init__(
        self,
        scenario: FakeBarrierScenario,
        point: FakeBarrierPoint | None = None,
        *,
        acknowledged_prefix: int | None = None,
    ) -> None:
        if type(scenario) is not FakeBarrierScenario:
            raise TypeError("fake barrier scenario must be exact")
        if point is not None and type(point) is not FakeBarrierPoint:
            raise TypeError("fake barrier point must be exact")
        accepted_point = _default_barrier_point(scenario) if point is None else point
        _validate_barrier_contract(scenario, accepted_point, acknowledged_prefix)
        self._scenario = scenario
        self._point = accepted_point
        self._acknowledged_prefix = acknowledged_prefix
        self._entered = threading.Event()
        self._released = threading.Event()

    @property
    def scenario(self) -> FakeBarrierScenario:
        return self._scenario

    @property
    def point(self) -> FakeBarrierPoint:
        return self._point

    @property
    def acknowledged_prefix(self) -> int | None:
        return self._acknowledged_prefix

    def wait_until_entered(self, timeout_seconds: float = 5.0) -> bool:
        return self._entered.wait(timeout_seconds)

    def release(self) -> None:
        self._released.set()

    def _cross(self) -> None:
        self._entered.set()
        if not self._released.wait(5.0):
            _deny("FAKE_FLUIDNC_BARRIER_TIMEOUT", "scripted barrier was not released")


FakeRequest: TypeAlias = (
    InitialPreflightRequest
    | HomeAllAxesRequest
    | ZCalibrationRequest
    | ZConfirmationRequest
    | StreamProgramRequest
    | HomeZAxisRequest
    | CloseSessionRequest
)
FakeOutcome: TypeAlias = (
    PreflightOutcome
    | HomeOutcome
    | ZCalibrationOutcome
    | ZConfirmationOutcome
    | StreamOutcome
    | HomeZOutcome
    | CloseOutcome
)


@dataclass(frozen=True, slots=True)
class FakeFluidNCStep:
    operation: FakeFluidNCOperation
    request: FakeRequest
    outcome: FakeOutcome
    progress: tuple[StreamProgressUpdate, ...] = ()
    barrier: FakeFluidNCBarrier | None = None

    def __post_init__(self) -> None:
        if type(self.operation) is not FakeFluidNCOperation:
            raise TypeError("fake operation must be exact")
        request_type, outcome_type = _operation_types(self.operation)
        if type(self.request) is not request_type or type(self.outcome) is not outcome_type:
            raise TypeError("fake step request/outcome types do not match its operation")
        if type(self.progress) is not tuple or any(type(item) is not StreamProgressUpdate for item in self.progress):
            raise TypeError("fake progress must be an exact tuple of exact updates")
        if self.operation is not FakeFluidNCOperation.STREAM_PROGRAM and self.progress:
            raise TypeError("only STREAM may script progress")
        if self.barrier is not None and type(self.barrier) is not FakeFluidNCBarrier:
            raise TypeError("fake barrier must be exact")
        if self.barrier is not None:
            _validate_step_barrier(self.operation, self.outcome, self.progress, self.barrier)
        _same_epoch(self.request, self.outcome)
        _validate_operation_invariants(self.request, self.outcome)
        if self.operation is FakeFluidNCOperation.STREAM_PROGRAM:
            _validate_stream_step(
                cast(StreamProgramRequest, self.request),
                cast(StreamOutcome, self.outcome),
                self.progress,
            )


@dataclass(frozen=True, slots=True)
class FakeFluidNCCall:
    operation: FakeFluidNCOperation
    request: FakeRequest
    outcome: FakeOutcome


class StrictFakeFluidNCFactory:
    """A closed, one-connection, exact scripted test adapter."""

    __slots__ = ("_expected_open", "_opened", "_session", "_steps")

    def __init__(self, expected_open: OpenSessionRequest, steps: tuple[FakeFluidNCStep, ...]) -> None:
        if type(expected_open) is not OpenSessionRequest:
            raise TypeError("fake expected-open request must be exact")
        if type(steps) is not tuple or any(type(step) is not FakeFluidNCStep for step in steps):
            raise TypeError("fake script must be an exact tuple of exact steps")
        for step in steps:
            if _outcome_epoch(step.outcome) != expected_open.machine_session_epoch:
                raise TypeError("every fake outcome must use the opened session epoch")
        close_indices = [index for index, step in enumerate(steps) if step.operation is FakeFluidNCOperation.CLOSE]
        if len(close_indices) > 1 or (close_indices and close_indices[0] != len(steps) - 1):
            raise TypeError("fake close step may appear once and only at the end")
        self._expected_open = expected_open
        self._steps = tuple(steps)
        self._opened = False
        self._session: _StrictFakeFluidNCSession | None = None

    @property
    def connections_opened(self) -> int:
        return int(self._opened)

    @property
    def session_epoch(self) -> str | None:
        return None if self._session is None else self._expected_open.machine_session_epoch

    @property
    def calls(self) -> tuple[FakeFluidNCCall, ...]:
        return () if self._session is None else self._session.calls

    @property
    def started_operations(self) -> tuple[FakeFluidNCOperation, ...]:
        return () if self._session is None else self._session.started_operations

    def open_session(self, request: OpenSessionRequest) -> FluidNCSession:
        if type(request) is not OpenSessionRequest or request != self._expected_open:
            _deny("FAKE_FLUIDNC_OPEN_MISMATCH", "undeclared FluidNC connection was denied")
        if self._opened:
            _deny("FAKE_FLUIDNC_REOPEN_DENIED", "FluidNC fake permits exactly one connection")
        self._opened = True
        self._session = _StrictFakeFluidNCSession(request.machine_session_epoch, self._steps)
        return self._session

    def assert_script_consumed(self) -> None:
        if self._session is None:
            _deny("FAKE_FLUIDNC_NOT_OPENED", "expected FluidNC connection was not opened")
        self._session.assert_script_consumed()


class _StrictFakeFluidNCSession:
    __slots__ = ("_calls", "_closed", "_epoch", "_index", "_started", "_steps")

    def __init__(self, epoch: str, steps: tuple[FakeFluidNCStep, ...]) -> None:
        self._epoch = epoch
        self._steps = steps
        self._index = 0
        self._closed = False
        self._started: list[FakeFluidNCOperation] = []
        self._calls: list[FakeFluidNCCall] = []

    @property
    def calls(self) -> tuple[FakeFluidNCCall, ...]:
        return tuple(self._calls)

    @property
    def started_operations(self) -> tuple[FakeFluidNCOperation, ...]:
        return tuple(self._started)

    def read_initial_preflight(self, request: InitialPreflightRequest) -> PreflightOutcome:
        return cast(PreflightOutcome, self._consume(FakeFluidNCOperation.INITIAL_PREFLIGHT, request))

    def home_all_axes(self, request: HomeAllAxesRequest) -> HomeOutcome:
        return cast(HomeOutcome, self._consume(FakeFluidNCOperation.HOME_ALL_AXES, request))

    def run_z_calibration(self, request: ZCalibrationRequest) -> ZCalibrationOutcome:
        return cast(ZCalibrationOutcome, self._consume(FakeFluidNCOperation.Z_CALIBRATION, request))

    def raise_after_z_confirmation(self, request: ZConfirmationRequest) -> ZConfirmationOutcome:
        return cast(ZConfirmationOutcome, self._consume(FakeFluidNCOperation.Z_CONFIRMATION_RAISE, request))

    def stream_program(
        self,
        request: StreamProgramRequest,
        progress: StreamProgressCallback,
    ) -> StreamOutcome:
        if not callable(progress):
            _deny("FAKE_FLUIDNC_PROGRESS_INVALID", "STREAM progress callback must be callable")
        step = self._begin(FakeFluidNCOperation.STREAM_PROGRAM, request)
        barrier = step.barrier
        if (
            barrier is not None
            and barrier.point is FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX
            and barrier.acknowledged_prefix == 0
        ):
            barrier._cross()
        for index, update in enumerate(step.progress, start=1):
            try:
                progress(update)
            except DrawingMachineError:
                raise
            except Exception:
                _deny(
                    "FAKE_FLUIDNC_PROGRESS_CALLBACK_FAILED",
                    "STREAM progress callback failed without exposing its raw exception",
                )
            if (
                barrier is not None
                and barrier.point is FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX
                and barrier.acknowledged_prefix == index
            ):
                barrier._cross()
        if barrier is not None and barrier.point is FakeBarrierPoint.AFTER_SIDE_EFFECT:
            barrier._cross()
        outcome = cast(StreamOutcome, self._complete(step))
        return outcome

    def home_z_axis(self, request: HomeZAxisRequest) -> HomeZOutcome:
        return cast(HomeZOutcome, self._consume(FakeFluidNCOperation.HOME_Z_AXIS, request))

    def close(self, request: CloseSessionRequest) -> CloseOutcome:
        outcome = cast(CloseOutcome, self._consume(FakeFluidNCOperation.CLOSE, request))
        self._closed = True
        return outcome

    def assert_script_consumed(self) -> None:
        if self._index != len(self._steps):
            _deny("FAKE_FLUIDNC_SCRIPT_UNCONSUMED", "one or more expected FluidNC operations were not consumed")
        if not self._closed:
            _deny("FAKE_FLUIDNC_NOT_CLOSED", "scripted FluidNC session was not closed")

    def _peek(self, operation: FakeFluidNCOperation, request: FakeRequest) -> FakeFluidNCStep:
        if self._closed:
            _deny("FAKE_FLUIDNC_CLOSED", "operation on a closed FluidNC session was denied")
        if self._index >= len(self._steps):
            _deny("FAKE_FLUIDNC_UNDECLARED", "undeclared FluidNC operation was denied")
        step = self._steps[self._index]
        if step.operation is not operation:
            _deny("FAKE_FLUIDNC_ORDER_MISMATCH", "FluidNC operation did not match the exact script order")
        if type(request) is not type(step.request) or request != step.request:
            _deny("FAKE_FLUIDNC_ARGUMENT_MISMATCH", "FluidNC operation arguments did not exactly match the script")
        return step

    def _consume(
        self,
        operation: FakeFluidNCOperation,
        request: FakeRequest,
    ) -> FakeOutcome:
        return self._consume_step(self._peek(operation, request))

    def _consume_step(self, step: FakeFluidNCStep) -> FakeOutcome:
        self._begin_step(step)
        return self._complete(step)

    def _begin(self, operation: FakeFluidNCOperation, request: FakeRequest) -> FakeFluidNCStep:
        step = self._peek(operation, request)
        self._begin_step(step, defer_after_side_effect=True)
        return step

    def _begin_step(self, step: FakeFluidNCStep, *, defer_after_side_effect: bool = False) -> None:
        barrier = step.barrier
        if barrier is not None and barrier.point is FakeBarrierPoint.BEFORE_OPERATION:
            barrier._cross()
        self._index += 1
        self._started.append(step.operation)
        if barrier is not None and barrier.point is FakeBarrierPoint.AFTER_SIDE_EFFECT and not defer_after_side_effect:
            barrier._cross()

    def _complete(self, step: FakeFluidNCStep) -> FakeOutcome:
        call = FakeFluidNCCall(step.operation, step.request, step.outcome)
        self._calls.append(call)
        return step.outcome


def _operation_types(operation: FakeFluidNCOperation) -> tuple[type[object], type[object]]:
    return {
        FakeFluidNCOperation.INITIAL_PREFLIGHT: (InitialPreflightRequest, PreflightOutcome),
        FakeFluidNCOperation.HOME_ALL_AXES: (HomeAllAxesRequest, HomeOutcome),
        FakeFluidNCOperation.Z_CALIBRATION: (ZCalibrationRequest, ZCalibrationOutcome),
        FakeFluidNCOperation.Z_CONFIRMATION_RAISE: (ZConfirmationRequest, ZConfirmationOutcome),
        FakeFluidNCOperation.STREAM_PROGRAM: (StreamProgramRequest, StreamOutcome),
        FakeFluidNCOperation.HOME_Z_AXIS: (HomeZAxisRequest, HomeZOutcome),
        FakeFluidNCOperation.CLOSE: (CloseSessionRequest, CloseOutcome),
    }[operation]


def _same_epoch(request: FakeRequest, outcome: FakeOutcome) -> None:
    request_epoch = getattr(request, "machine_session_epoch", None)
    if request_epoch is not None and request_epoch != _outcome_epoch(outcome):
        raise TypeError("fake request and outcome session epochs differ")


def _validate_operation_invariants(
    request: FakeRequest,
    outcome: FakeOutcome,
) -> None:
    if type(outcome) in {HomeOutcome, ZCalibrationOutcome, ZConfirmationOutcome, HomeZOutcome}:
        bound_request = cast(HomeOutcome | ZCalibrationOutcome | ZConfirmationOutcome | HomeZOutcome, outcome).request
        if bound_request != request:
            raise TypeError("fake operation outcome is bound to different exact arguments")


def _outcome_epoch(outcome: FakeOutcome) -> str:
    return outcome.machine_session_epoch


def _validate_stream_step(
    request: StreamProgramRequest,
    outcome: StreamOutcome,
    progress: tuple[StreamProgressUpdate, ...],
) -> None:
    commands = request.program.commands
    if outcome.total_commands != len(commands):
        raise TypeError("scripted stream total must match the complete validated program")
    if len(progress) != outcome.acknowledged_commands:
        raise TypeError("scripted progress count must match acknowledged commands")
    for index, update in enumerate(progress, start=1):
        command = commands[index - 1]
        if (
            update.machine_session_epoch != outcome.machine_session_epoch
            or update.acknowledged_commands != index
            or update.source_line != command.source_line
            or update.command_digest != hashlib.sha256(command.command.encode("ascii")).hexdigest()
        ):
            raise TypeError("scripted progress must exactly map the validated program prefix")


def _validate_step_barrier(
    operation: FakeFluidNCOperation,
    outcome: FakeOutcome,
    progress: tuple[StreamProgressUpdate, ...],
    barrier: FakeFluidNCBarrier,
) -> None:
    if barrier.point is FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX:
        if operation is not FakeFluidNCOperation.STREAM_PROGRAM:
            raise TypeError("acknowledged-prefix barrier is valid only for STREAM")
        prefix = cast(int, barrier.acknowledged_prefix)
        if prefix > len(progress):
            raise TypeError("partial-stream barrier prefix exceeds scripted progress")
        stream_outcome = cast(StreamOutcome, outcome)
        if stream_outcome.failure is None:
            raise TypeError("partial-stream barrier requires a failed STREAM outcome")
        if prefix > stream_outcome.acknowledged_commands:
            raise TypeError("partial-stream barrier prefix exceeds the failed acknowledged prefix")
    if barrier.scenario in {
        FakeBarrierScenario.LOST_ACK,
        FakeBarrierScenario.COMMIT_AFTER_SIDE_EFFECT,
        FakeBarrierScenario.CONTROLLER_RESET,
    } and operation in {FakeFluidNCOperation.INITIAL_PREFLIGHT, FakeFluidNCOperation.CLOSE}:
        raise TypeError("side-effect barrier scenario requires a motion-capable operation")
    failure = getattr(outcome, "failure", None)
    if (
        barrier.point in {FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX, FakeBarrierPoint.AFTER_SIDE_EFFECT}
        and type(failure) is FluidNCFailure
        and not failure.side_effect_may_have_occurred
    ):
        raise TypeError("a failed post-side-effect barrier requires possible side effect evidence")
    required_failure = {
        FakeBarrierScenario.LOST_ACK: FluidNCFailureKind.LOST_ACK,
        FakeBarrierScenario.CONTROLLER_RESET: FluidNCFailureKind.CONTROLLER_RESET,
        FakeBarrierScenario.CONNECTION_LOSS: FluidNCFailureKind.TRANSPORT_LOSS,
    }.get(barrier.scenario)
    if required_failure is not None and (type(failure) is not FluidNCFailure or failure.kind is not required_failure):
        raise TypeError("fake barrier scenario requires its exact typed failure outcome")


def _deny(code: str, message: str) -> NoReturn:
    raise DrawingMachineError(
        ErrorPayload(
            code=code,
            category=ErrorCategory.HARDWARE,
            message=message,
            retryable=False,
            details={},
        )
    )


def _default_barrier_point(scenario: FakeBarrierScenario) -> FakeBarrierPoint:
    if scenario in {FakeBarrierScenario.CONCURRENT_APPROVAL, FakeBarrierScenario.SHUTDOWN}:
        return FakeBarrierPoint.BEFORE_OPERATION
    if scenario is FakeBarrierScenario.PARTIAL_STREAM:
        return FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX
    return FakeBarrierPoint.AFTER_SIDE_EFFECT


def _validate_barrier_contract(
    scenario: FakeBarrierScenario,
    point: FakeBarrierPoint,
    acknowledged_prefix: int | None,
) -> None:
    expected = _default_barrier_point(scenario)
    if point is not expected:
        raise TypeError("fake barrier scenario and point are semantically incompatible")
    if point is FakeBarrierPoint.AFTER_ACKNOWLEDGED_PREFIX:
        if type(acknowledged_prefix) is not int or acknowledged_prefix < 0:
            raise TypeError("partial-stream barrier requires a non-negative exact acknowledged prefix")
    elif acknowledged_prefix is not None:
        raise TypeError("only a partial-stream barrier accepts an acknowledged prefix")


__all__ = [
    "FakeBarrierPoint",
    "FakeBarrierScenario",
    "FakeFluidNCBarrier",
    "FakeFluidNCCall",
    "FakeFluidNCOperation",
    "FakeFluidNCStep",
    "StrictFakeFluidNCFactory",
]
