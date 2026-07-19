from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import NoReturn, Protocol, cast

from drawingmachine.application.machine.commands import MachineActionCommand
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject


class _MachineChain(Protocol):
    def admit_action(
        self,
        command: MachineActionCommand,
        *,
        still_requested: Callable[[], bool] | None = None,
    ) -> object: ...

    def drive_admitted_action(self, admission: object) -> object: ...

    def status_projection(self, execution_id: str | None = None) -> JsonObject: ...


class _EnvelopeState(StrEnum):
    QUEUED = "QUEUED"
    ADMITTING = "ADMITTING"
    COMMITTED = "COMMITTED"
    WITHDRAWN = "WITHDRAWN"


@dataclass(slots=True)
class _Envelope:
    command: MachineActionCommand
    deadline: float
    acknowledged: threading.Event
    committed_callback: Callable[[MachineCommandResult], None] | None
    result: MachineCommandResult | None = None
    error: BaseException | None = None
    projection_ready: threading.Event = dataclass_field(default_factory=threading.Event)
    _state: _EnvelopeState = dataclass_field(default=_EnvelopeState.QUEUED, init=False)
    _lock: threading.Lock = dataclass_field(default_factory=threading.Lock, init=False, repr=False)

    def begin_admission(self) -> bool:
        with self._lock:
            if self._state is not _EnvelopeState.QUEUED:
                return False
            self._state = _EnvelopeState.ADMITTING
            return True

    def still_requested(self) -> bool:
        with self._lock:
            return self._state is _EnvelopeState.ADMITTING and time.monotonic() < self.deadline

    def withdraw_queued(self) -> bool:
        with self._lock:
            if self._state is not _EnvelopeState.QUEUED:
                return False
            self._state = _EnvelopeState.WITHDRAWN
            return True

    def reject_queued(self, error: BaseException) -> bool:
        with self._lock:
            if self._state is not _EnvelopeState.QUEUED:
                return False
            self._state = _EnvelopeState.WITHDRAWN
            self.error = error
            self.acknowledged.set()
            self.projection_ready.set()
            return True

    def committed(self, result: MachineCommandResult) -> bool:
        with self._lock:
            if self._state is _EnvelopeState.WITHDRAWN:
                return False
            self._state = _EnvelopeState.COMMITTED
            self.result = result
            self.acknowledged.set()
            return True

    def failed(self, error: BaseException) -> None:
        with self._lock:
            if self._state in {_EnvelopeState.WITHDRAWN, _EnvelopeState.COMMITTED}:
                return
            self.error = error
            self.acknowledged.set()
            self.projection_ready.set()


@dataclass(frozen=True, slots=True)
class _Stop:
    pass


_MailboxItem = _Envelope | _Stop


class MachineCoordinator:
    """Serialize all machine persistence and adapter ownership on one thread."""

    def __init__(
        self,
        *,
        chain_factory: Callable[[str], _MachineChain],
        service_epoch: str,
        acknowledgement_timeout_seconds: float = 1.0,
        job_publisher: Callable[[str], None] | None = None,
    ) -> None:
        if type(service_epoch) is not str or not service_epoch:
            raise ValueError("service_epoch must be a non-empty exact string")
        if (
            type(acknowledgement_timeout_seconds) is not float
            or acknowledgement_timeout_seconds <= 0.0
            or acknowledgement_timeout_seconds > 1.0
        ):
            raise ValueError("acknowledgement_timeout_seconds must be a float in (0, 1]")
        self._chain_factory = chain_factory
        self._service_epoch = service_epoch
        self._acknowledgement_timeout_seconds = acknowledgement_timeout_seconds
        self._job_publisher = job_publisher
        self._mailbox: queue.Queue[_MailboxItem] = queue.Queue()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._shutdown_error: BaseException | None = None
        self._fatal_error: BaseException | None = None
        self._stopping = False
        self._active_execution_id: str | None = None
        self._projections: dict[str, bytes] = {}
        self._observations: dict[str, bytes] = {}

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    @property
    def owner_thread(self) -> threading.Thread | None:
        with self._state_lock:
            return self._thread

    @property
    def fatal_error(self) -> BaseException | None:
        with self._state_lock:
            return self._fatal_error

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                self._raise("MACHINE_COORDINATOR_ALREADY_STARTED", "machine coordinator is already started")
            self._ready.clear()
            self._startup_error = None
            self._shutdown_error = None
            self._fatal_error = None
            self._stopping = False
            thread = threading.Thread(
                target=self._run,
                name="drawingmachine-machine-coordinator",
                daemon=False,
            )
            self._thread = thread
            thread.start()
        self._ready.wait()
        error = cast(BaseException | None, self._startup_error)
        if error is not None:
            thread.join()
            with self._state_lock:
                self._thread = None
            raise error

    def submit(
        self,
        command: MachineActionCommand,
        *,
        committed: Callable[[MachineCommandResult], None] | None = None,
    ) -> MachineCommandResult:
        return self._submit(command, committed=committed, wait_for_projection=False)

    def submit_observed(self, command: MachineActionCommand) -> tuple[MachineCommandResult, JsonObject]:
        result = self._submit(command, committed=None, wait_for_projection=True)
        execution_id = self._execution_id(result.response)
        return result, self.status(execution_id)

    def _submit(
        self,
        command: MachineActionCommand,
        *,
        committed: Callable[[MachineCommandResult], None] | None,
        wait_for_projection: bool,
    ) -> MachineCommandResult:
        deadline = time.monotonic() + self._acknowledgement_timeout_seconds
        envelope = _Envelope(command, deadline, threading.Event(), committed)
        with self._state_lock:
            self._require_admission_locked()
            self._mailbox.put(envelope)
        remaining = max(0.0, deadline - time.monotonic())
        if not envelope.acknowledged.wait(remaining):
            envelope.withdraw_queued()
            self._raise(
                "MACHINE_COORDINATOR_ACK_TIMEOUT",
                "machine command was not durably acknowledged before its absolute deadline",
            )
        if envelope.error is not None:
            raise envelope.error
        result = envelope.result
        if result is None:
            self._raise("MACHINE_COORDINATOR_FAILED", "machine command completed without a durable result")
        if wait_for_projection:
            envelope.projection_ready.wait()
        return result

    def status(self, execution_id: str | None = None) -> JsonObject:
        with self._state_lock:
            selected = self._active_execution_id if execution_id is None else execution_id
            encoded = None if selected is None else self._observations.get(selected)
            thread = self._thread
            accepting = thread is not None and thread.is_alive() and not self._stopping and self._fatal_error is None
        observation = None if encoded is None else cast(JsonObject, json.loads(encoded))
        return {
            "schema_version": 1,
            "service_epoch": self._service_epoch,
            "accepting": accepting,
            "execution": None if observation is None else observation["execution"],
            "progress": None if observation is None else observation["progress"],
            "last_outcome": None if observation is None else observation["last_outcome"],
        }

    def stop(self) -> None:
        with self._state_lock:
            thread = self._thread
            if thread is None:
                return
            if not self._stopping:
                self._stopping = True
                self._reject_queued_commands()
                if thread.is_alive():
                    self._mailbox.put(_Stop())
        if thread.is_alive():
            thread.join()
        shutdown_error = self._shutdown_error
        with self._state_lock:
            self._thread = None
            self._stopping = False
        if shutdown_error is not None:
            raise shutdown_error

    def _run(self) -> None:
        chain: _MachineChain | None = None
        try:
            chain = self._chain_factory(self._service_epoch)
            install_publisher = getattr(chain, "install_projection_publisher", None)
            if callable(install_publisher):
                install_publisher(self._publish_projection)
            initialize = getattr(chain, "initialize_and_reconcile", None)
            initial = initialize() if callable(initialize) else chain.status_projection()
            self._publish_projection(initial)
        except BaseException as error:
            startup_error = error
            if chain is not None:
                cleanup_error = self._shutdown_and_close(chain, publish_job=False)
                if cleanup_error is not None:
                    startup_error = cleanup_error
            self._startup_error = startup_error
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._mailbox.get()
                if isinstance(item, _Stop):
                    break
                self._before_admission_claim()
                if not self._claim_admission(item):
                    if self._is_stopping():
                        break
                    continue
                try:
                    self._process(chain, item)
                except BaseException as error:
                    item.failed(error)
                    self._set_fatal(error)
                    break
                if self._is_stopping():
                    break
        finally:
            self._reject_queued_commands()
            shutdown_error = self._shutdown_and_close(chain, publish_job=True)
            if shutdown_error is not None:
                self._shutdown_error = shutdown_error
                self._set_fatal(shutdown_error)

    def _process(self, chain: _MachineChain, envelope: _Envelope) -> None:
        try:
            admission = chain.admit_action(
                envelope.command,
                still_requested=envelope.still_requested,
            )
        except DrawingMachineError as error:
            envelope.failed(error)
            if self._is_fatal(error):
                raise
            return
        except BaseException as error:
            envelope.failed(error)
            raise
        if type(admission) is MachineCommandResult:
            result = admission
            envelope.committed(result)
            try:
                self._committed_callback(envelope, result)
                self._refresh_projection(chain, self._execution_id(result.response))
            finally:
                envelope.projection_ready.set()
            return
        response = getattr(admission, "response", None)
        if type(response) is not dict:
            admission_error = self._error(
                "MACHINE_COORDINATOR_ADMISSION_INVALID",
                "admission returned no closed response",
            )
            envelope.failed(admission_error)
            raise admission_error
        result = MachineCommandResult(cast(JsonObject, response), False)
        execution_id = self._execution_id(result.response)
        envelope.committed(result)
        try:
            self._committed_callback(envelope, result)
            self._refresh_projection(chain, execution_id)
        finally:
            envelope.projection_ready.set()
        chain.drive_admitted_action(admission)
        projection = self._refresh_projection(chain, execution_id)
        if projection is not None:
            self._publish_job(projection)

    @staticmethod
    def _committed_callback(envelope: _Envelope, result: MachineCommandResult) -> None:
        callback = envelope.committed_callback
        if callback is not None:
            callback(result)

    def _refresh_projection(self, chain: _MachineChain, execution_id: str | None) -> JsonObject | None:
        projection = chain.status_projection(execution_id)
        return self._publish_projection(projection)

    def _publish_projection(self, projection: object) -> JsonObject | None:
        if type(projection) is not dict:
            self._raise("MACHINE_COORDINATOR_PROJECTION_INVALID", "machine projection must be a plain object")
        execution = cast(dict[object, object], projection).get("execution")
        if execution is None:
            return cast(JsonObject, projection)
        if type(execution) is not dict:
            self._raise("MACHINE_COORDINATOR_PROJECTION_INVALID", "execution projection must be a plain object")
        execution_id = execution.get("execution_id")
        revision = execution.get("revision")
        phase = execution.get("phase")
        retired = execution.get("retired", False)
        if type(execution_id) is not str or not execution_id or type(revision) is not int or revision < 0:
            self._raise("MACHINE_COORDINATOR_PROJECTION_INVALID", "execution projection identity is invalid")
        encoded = json.dumps(execution, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        progress = cast(dict[object, object], projection).get("progress")
        last_outcome = cast(dict[object, object], projection).get("last_outcome")
        if progress is not None and type(progress) is not dict:
            self._raise("MACHINE_COORDINATOR_PROJECTION_INVALID", "progress projection must be a plain object or null")
        if last_outcome is not None and type(last_outcome) is not dict:
            self._raise("MACHINE_COORDINATOR_PROJECTION_INVALID", "outcome projection must be a plain object or null")
        observation: JsonObject = {
            "schema_version": 1,
            "execution": cast(JsonObject, execution),
            "progress": cast(JsonObject | None, progress),
            "last_outcome": cast(JsonObject | None, last_outcome),
        }
        encoded_observation = json.dumps(
            observation,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self._state_lock:
            previous = self._projections.get(execution_id)
            if previous is not None:
                previous_value = cast(dict[str, object], json.loads(previous))
                previous_revision = previous_value.get("revision")
                if type(previous_revision) is int and revision < previous_revision:
                    return None
                if type(previous_revision) is int and revision == previous_revision:
                    if encoded != previous:
                        self._raise(
                            "MACHINE_COORDINATOR_PROJECTION_CONFLICT",
                            "equal machine projection revision changed immutable fields",
                        )
                    self._accept_observation_locked(execution_id, encoded_observation)
                    return None
            self._projections[execution_id] = encoded
            self._accept_observation_locked(execution_id, encoded_observation)
            if phase == "COMPLETED" or retired is True:
                if self._active_execution_id == execution_id:
                    self._active_execution_id = None
            else:
                self._active_execution_id = execution_id
        return cast(JsonObject, json.loads(encoded_observation))

    def _accept_observation_locked(self, execution_id: str, encoded: bytes) -> None:
        previous_encoded = self._observations.get(execution_id)
        if previous_encoded is not None:
            previous = cast(JsonObject, json.loads(previous_encoded))
            current = cast(JsonObject, json.loads(encoded))
            self._require_monotonic_observation(previous, current)
        self._observations[execution_id] = encoded

    def _require_monotonic_observation(self, previous: JsonObject, current: JsonObject) -> None:
        previous_progress = previous.get("progress")
        current_progress = current.get("progress")
        if previous_progress is not None:
            if type(previous_progress) is not dict or type(current_progress) is not dict:
                self._raise("MACHINE_COORDINATOR_PROJECTION_CONFLICT", "durable progress projection regressed")
            for field in ("execution_revision", "acknowledged", "ok_count", "error_count"):
                before = previous_progress.get(field)
                after = current_progress.get(field)
                if type(before) is not int or type(after) is not int or after < before:
                    self._raise("MACHINE_COORDINATOR_PROJECTION_CONFLICT", "durable progress projection regressed")
        previous_outcome = previous.get("last_outcome")
        current_outcome = current.get("last_outcome")
        if previous_outcome is not None and previous_outcome != current_outcome:
            self._raise("MACHINE_COORDINATOR_PROJECTION_CONFLICT", "durable outcome projection changed")

    def _reject_queued_commands(self) -> None:
        while True:
            try:
                item = self._mailbox.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Envelope):
                item.reject_queued(self._stopped_error())

    def _is_stopping(self) -> bool:
        with self._state_lock:
            return self._stopping

    def _before_admission_claim(self) -> None:
        """Test seam immediately before the atomic stop-state/admission claim."""

    def _claim_admission(self, envelope: _Envelope) -> bool:
        with self._state_lock:
            if self._stopping:
                envelope.reject_queued(self._stopped_error())
                return False
            return envelope.begin_admission()

    def _set_fatal(self, error: BaseException) -> None:
        del error
        fixed = self._error(
            "MACHINE_COORDINATOR_FATAL",
            "machine coordinator terminated at a fail-closed ownership boundary",
        )
        with self._state_lock:
            self._fatal_error = fixed

    def _shutdown_and_close(
        self,
        chain: _MachineChain,
        *,
        publish_job: bool,
    ) -> DrawingMachineError | None:
        failed = False
        try:
            projection = self._shutdown_chain(chain)
            if publish_job and projection is not None:
                self._publish_job(projection)
        except BaseException:
            failed = True
        try:
            self._close_chain(chain)
        except BaseException:
            failed = True
        if failed:
            return self._error(
                "MACHINE_COORDINATOR_FATAL_PERSISTENCE",
                "machine coordinator shutdown persistence failed; startup reconciliation required",
            )
        return None

    @classmethod
    def _stopped_error(cls) -> DrawingMachineError:
        return cls._error(
            "MACHINE_COORDINATOR_STOPPED",
            "machine command was withdrawn when coordinator shutdown began",
        )

    def _publish_job(self, projection: JsonObject) -> None:
        publisher = self._job_publisher
        if publisher is None:
            return
        execution = projection.get("execution")
        if type(execution) is not dict:
            return
        job_id = execution.get("job_id")
        if type(job_id) is str and job_id:
            publisher(job_id)

    def _shutdown_chain(self, chain: _MachineChain) -> JsonObject | None:
        shutdown = getattr(chain, "shutdown_for_recovery", None)
        if callable(shutdown):
            projection = shutdown()
            return self._publish_projection(projection)
        return None

    @staticmethod
    def _close_chain(chain: _MachineChain) -> None:
        close = getattr(chain, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _execution_id(response: JsonObject) -> str | None:
        value = response.get("execution_id")
        return value if type(value) is str and value else None

    @staticmethod
    def _is_fatal(error: BaseException) -> bool:
        return isinstance(error, DrawingMachineError) and error.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"

    def _require_admission_locked(self) -> None:
        thread = self._thread
        if self._fatal_error is not None:
            self._raise("MACHINE_COORDINATOR_FATAL", "machine coordinator terminated after fatal persistence failure")
        if thread is None or not thread.is_alive() or self._stopping:
            self._raise("MACHINE_COORDINATOR_NOT_STARTED", "machine coordinator is not accepting commands")

    @staticmethod
    def _error(code: str, message: str) -> DrawingMachineError:
        return DrawingMachineError(ErrorPayload(code, ErrorCategory.SERVICE, message, False, {}))

    @classmethod
    def _raise(cls, code: str, message: str) -> NoReturn:
        raise cls._error(code, message)


__all__ = ["MachineCoordinator"]
