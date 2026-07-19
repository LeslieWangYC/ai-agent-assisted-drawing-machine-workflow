from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import drawingmachine.service.machine_coordinator as coordinator_module
from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.adapters.persistence import SQLiteRepository
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine import (
    HomeMachineCommand,
    MachineCommandMode,
    PrepareMachineCommand,
    RecoverMachineCommand,
    StreamMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult, MachineExecutionChain
from drawingmachine.domain.jobs import JobState
from drawingmachine.domain.machine import (
    AuthenticatedMachinePrincipal,
    MachinePhase,
    RecoveryDisposition,
    RecoveryIntent,
)
from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.fluidnc import CloseOutcome, CloseSessionRequest, InitialPreflightRequest, OpenSessionRequest
from drawingmachine.service.job_coordinator import JobCoordinator
from drawingmachine.service.machine_coordinator import MachineCoordinator
from tests.unit.application.machine.test_admission import (
    GCODE,
    NOW,
    ProbeFactory,
    ProbeSession,
    Reader,
    _authority,
    _config,
    _identities,
    _job,
    _preflight,
    _ready_snapshot,
    _seed,
    _uuid,
)
from tests.unit.application.machine.test_home import _home_outcome, _home_request


@dataclass(frozen=True, slots=True)
class _Admission:
    execution_id: str
    response: JsonObject


class _FakeChain:
    def __init__(
        self,
        *,
        admit_entered: threading.Event | None = None,
        admit_release: threading.Event | None = None,
        drive_entered: threading.Event | None = None,
        drive_release: threading.Event | None = None,
        commit_entered: threading.Event | None = None,
        commit_release: threading.Event | None = None,
        fatal_on_drive: bool = False,
    ) -> None:
        self.admit_entered = admit_entered
        self.admit_release = admit_release
        self.drive_entered = drive_entered
        self.drive_release = drive_release
        self.commit_entered = commit_entered
        self.commit_release = commit_release
        self.fatal_on_drive = fatal_on_drive
        self.owner_thread: int | None = None
        self.closed_thread: int | None = None
        self.admit_order: list[str] = []
        self.drive_count = 0
        self.status_reads = 0
        self.shutdown_count = 0
        self.close_count = 0
        self._revision = 0
        self._requests: dict[tuple[int, int, str], tuple[str, JsonObject]] = {}

    def initialize_and_reconcile(self) -> JsonObject:
        self.owner_thread = threading.get_ident()
        return {"schema_version": 1, "execution": None}

    def admit_action(
        self,
        command: PrepareMachineCommand,
        *,
        still_requested: object,
    ) -> _Admission | MachineCommandResult:
        assert threading.get_ident() == self.owner_thread
        self.admit_order.append(command.request_id)
        if self.admit_entered is not None:
            self.admit_entered.set()
        if self.admit_release is not None:
            self.admit_release.wait(timeout=2.0)
        assert callable(still_requested)
        if cast(object, still_requested)() is not True:  # type: ignore[operator]
            raise DrawingMachineError(
                ErrorPayload(
                    "MACHINE_ADMISSION_WITHDRAWN",
                    ErrorCategory.HARDWARE,
                    "withdrawn",
                    False,
                    {},
                )
            )
        if self.commit_entered is not None:
            self.commit_entered.set()
        if self.commit_release is not None:
            self.commit_release.wait(timeout=2.0)
        key = (command.requester.uid, command.requester.gid, command.request_id)
        digest = f"{command.job_id}:{command.job_revision}"
        previous = self._requests.get(key)
        if previous is not None:
            if previous[0] != digest:
                raise RuntimeError("REQUEST_ID_CONFLICT")
            return MachineCommandResult(previous[1], True)
        self._revision += 1
        response: JsonObject = {
            "schema_version": 1,
            "deduplicated": False,
            "executed": False,
            "execution_id": f"execution-{self._revision}",
            "phase": "PREPARING_SESSION",
        }
        self._requests[key] = (digest, response)
        return _Admission(cast(str, response["execution_id"]), response)

    def drive_admitted_action(self, admission: object) -> object:
        assert threading.get_ident() == self.owner_thread
        self.drive_count += 1
        if self.drive_entered is not None:
            self.drive_entered.set()
        if self.drive_release is not None:
            self.drive_release.wait(timeout=2.0)
        if self.fatal_on_drive:
            from drawingmachine.errors import ErrorCategory, ErrorPayload

            raise DrawingMachineError(
                ErrorPayload(
                    "MACHINE_COORDINATOR_FATAL_PERSISTENCE",
                    ErrorCategory.HARDWARE,
                    "fatal",
                    False,
                    {},
                )
            )
        value = cast(_Admission, admission)
        self._revision += 1
        return {"execution_id": value.execution_id, "revision": self._revision}

    def status_projection(self, execution_id: str | None = None) -> JsonObject:
        assert threading.get_ident() == self.owner_thread
        self.status_reads += 1
        return {
            "schema_version": 1,
            "execution": (
                None
                if execution_id is None and self._revision == 0
                else {
                    "schema_version": 1,
                    "execution_id": execution_id or f"execution-{max(1, self._revision - 1)}",
                    "job_id": "job-1",
                    "revision": self._revision,
                    "phase": "AWAITING_HOME_APPROVAL",
                }
            ),
        }

    def shutdown_for_recovery(self) -> JsonObject:
        assert threading.get_ident() == self.owner_thread
        self.shutdown_count += 1
        return self.status_projection()

    def close(self) -> None:
        assert threading.get_ident() == self.owner_thread
        self.close_count += 1
        self.closed_thread = threading.get_ident()


def _command(request_id: str, *, job_id: str = "job-1") -> PrepareMachineCommand:
    return PrepareMachineCommand(
        request_id=request_id,
        requester=AuthenticatedMachinePrincipal(1001, 1002, 3001),
        job_id=job_id,
        job_revision=7,
    )


def _real_prepare(request_id: str, *, job_id: str = "job-c9", pid: int = 31001) -> PrepareMachineCommand:
    return PrepareMachineCommand(
        request_id,
        AuthenticatedMachinePrincipal(1100, 1100, pid),
        job_id,
        7,
    )


def _coordinator(chain: _FakeChain, *, timeout: float = 0.5) -> MachineCoordinator:
    return MachineCoordinator(
        chain_factory=lambda _epoch: chain,
        service_epoch=str(uuid4()),
        acknowledgement_timeout_seconds=timeout,
    )


class _AnyJobReader(Reader):
    def read_bytes(
        self,
        job_id: str,
        artifact: object,
        *,
        expected_media_type: str,
        max_bytes: int,
    ) -> bytes:
        del job_id, artifact
        assert expected_media_type == "text/x.gcode"
        assert max_bytes >= len(self.data)
        self.calls += 1
        return self.data


class _FaultingRealChain:
    def __init__(self, chain: MachineExecutionChain) -> None:
        self.chain = chain
        self.invalid_admission = False
        self.fail_projection_after_drive = False
        self._projection_failed = False

    def initialize_and_reconcile(self) -> JsonObject:
        return self.chain.initialize_and_reconcile()

    def admit_action(self, command, *, still_requested):
        if self.invalid_admission:
            return object()
        return self.chain.admit_action(command, still_requested=still_requested)

    def drive_admitted_action(self, admission: object) -> object:
        result = self.chain.drive_admitted_action(admission)  # type: ignore[arg-type]
        if self.fail_projection_after_drive:
            self._projection_failed = True
        return result

    def status_projection(self, execution_id: str | None = None) -> JsonObject:
        if self._projection_failed:
            self._projection_failed = False
            raise RuntimeError("injected projection failure with an owned session")
        return self.chain.status_projection(execution_id)

    def shutdown_for_recovery(self) -> JsonObject:
        return self.chain.shutdown_for_recovery()

    def close(self) -> None:
        self.chain.close()


class _FixedClock:
    def now(self):
        return NOW + timedelta(seconds=30)


class _DurableJobProjectionChain:
    def __init__(self, database: Path) -> None:
        self.repository = SQLiteRepository(database, clock=_FixedClock())

    def initialize(self) -> None:
        self.repository.initialize()

    def reconcile_inflight_jobs(self):
        return ()

    def jobs(self):
        job = self.repository.get_job("job-c9")
        return () if job is None else (job,)

    def get(self, job_id: str):
        job = self.repository.get_job(job_id)
        if job is None:
            raise RuntimeError("durable job missing")
        return job

    def artifact_refs(self, job_id: str):
        return self.repository.list_artifacts(job_id)

    def register_cancellation(self, job_id: str, token: threading.Event) -> None:
        del job_id, token

    def close(self) -> None:
        self.repository.close()


def _real_coordinator_fixture(
    tmp_path: Path,
    factory: object,
    *,
    reader: object | None = None,
    job_ids: tuple[str, ...] = ("job-c9",),
    job_publisher: Callable[[str], None] | None = None,
) -> tuple[MachineCoordinator, Path, _FaultingRealChain]:
    database = tmp_path / "review-real-coordinator.db"
    setup = SQLiteMachineRepository(database)
    setup.initialize(applied_at=NOW.isoformat())
    for job_id in job_ids:
        _seed(setup, _job(_ready_snapshot(job_id)))
    setup.close()
    service_epoch = str(uuid4())
    wall_tick = 0
    wrapper: _FaultingRealChain | None = None

    def build_chain(epoch: str) -> _FaultingRealChain:
        nonlocal wall_tick, wrapper
        identities = _identities()

        def wall_now():
            nonlocal wall_tick
            wall_tick += 1
            return NOW + timedelta(microseconds=wall_tick)

        chain = MachineExecutionChain(
            SQLiteMachineRepository(database),
            cast(object, reader or _AnyJobReader(GCODE)),  # type: ignore[arg-type]
            cast(object, factory),  # type: ignore[arg-type]
            _config(),
            _authority(service_epoch=epoch),
            identity_factory=lambda: next(identities),
            wall_now=wall_now,
            monotonic_now=lambda: 1000.0,
        )
        wrapper = _FaultingRealChain(chain)
        return wrapper

    coordinator = MachineCoordinator(
        chain_factory=build_chain,
        service_epoch=service_epoch,
        acknowledgement_timeout_seconds=0.5,
        job_publisher=job_publisher,
    )
    coordinator.start()
    assert wrapper is not None
    return coordinator, database, wrapper


def _strict_factory(
    epoch: str,
    *steps: FakeFluidNCStep,
) -> StrictFakeFluidNCFactory:
    return StrictFakeFluidNCFactory(
        OpenSessionRequest(epoch),
        (
            FakeFluidNCStep(
                FakeFluidNCOperation.INITIAL_PREFLIGHT,
                InitialPreflightRequest(),
                _preflight(epoch),
            ),
            *steps,
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(epoch),
                CloseOutcome(epoch),
            ),
        ),
    )


def _wait_for_phase(coordinator: MachineCoordinator, phase: MachinePhase) -> str:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        execution = coordinator.status()["execution"]
        if type(execution) is dict and execution.get("phase") == phase.value:
            return cast(str, execution["execution_id"])
        time.sleep(0.005)
    raise AssertionError(f"coordinator did not publish {phase.value}")


def _challenge_id(result: MachineCommandResult) -> str:
    return cast(str, cast(dict[str, object], result.response["challenge"])["challenge_id"])


def test_one_nondaemon_owner_thread_creates_uses_and_closes_chain() -> None:
    chain = _FakeChain()
    coordinator = _coordinator(chain)

    coordinator.start()
    thread = coordinator.owner_thread
    assert thread is not None
    assert thread.daemon is False
    assert thread.ident == chain.owner_thread
    coordinator.stop()

    assert chain.closed_thread == chain.owner_thread
    assert not thread.is_alive()


def test_simultaneous_prepare_is_serial_and_duplicate_replay_never_redispatches() -> None:
    first_drive = threading.Event()
    release_first = threading.Event()
    chain = _FakeChain(drive_entered=first_drive, drive_release=release_first)
    coordinator = _coordinator(chain)
    coordinator.start()
    results: list[MachineCommandResult] = []

    first = threading.Thread(target=lambda: results.append(coordinator.submit(_command("same"))))
    second = threading.Thread(target=lambda: results.append(coordinator.submit(_command("same"))))
    first.start()
    assert first_drive.wait(timeout=1.0)
    second.start()
    time.sleep(0.03)
    assert chain.admit_order == ["same"]
    release_first.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert len(results) == 2
    assert sorted(result.deduplicated for result in results) == [False, True]
    assert chain.drive_count == 1
    coordinator.stop()


def test_ack_is_published_before_callback_and_adapter_waits_for_callback_return() -> None:
    callback_entered = threading.Event()
    callback_release = threading.Event()
    drive_entered = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered)
    coordinator = _coordinator(chain)
    coordinator.start()

    def committed(_result: MachineCommandResult) -> None:
        callback_entered.set()
        assert chain.drive_count == 0
        callback_release.wait(timeout=2.0)

    result = coordinator.submit(_command("barrier"), committed=committed)
    assert result.response["phase"] == "PREPARING_SESSION"
    assert callback_entered.wait(timeout=1.0)
    assert not drive_entered.is_set()
    callback_release.set()
    assert drive_entered.wait(timeout=1.0)
    coordinator.stop()


def test_timeout_while_still_queued_withdraws_without_admission_or_drive() -> None:
    drive_entered = threading.Event()
    release_drive = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered, drive_release=release_drive)
    coordinator = _coordinator(chain, timeout=0.05)
    coordinator.start()

    coordinator.submit(_command("blocking"))
    assert drive_entered.wait(timeout=1.0)
    with pytest.raises(DrawingMachineError) as error:
        coordinator.submit(_command("withdrawn"))
    assert error.value.payload.code == "MACHINE_COORDINATOR_ACK_TIMEOUT"
    release_drive.set()
    time.sleep(0.05)
    assert chain.admit_order == ["blocking"]
    assert chain.drive_count == 1
    coordinator.stop()


def test_postcommit_timeout_remains_durable_and_duplicate_replays_without_drive() -> None:
    callback_entered = threading.Event()
    callback_release = threading.Event()
    chain = _FakeChain()
    coordinator = _coordinator(chain, timeout=0.05)
    coordinator.start()

    def committed(_result: MachineCommandResult) -> None:
        callback_entered.set()
        callback_release.wait(timeout=2.0)

    first = coordinator.submit(_command("late"), committed=committed)
    assert callback_entered.wait(timeout=1.0)
    assert first.deduplicated is False
    callback_release.set()
    deadline = time.monotonic() + 1.0
    while chain.drive_count != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    replay = coordinator.submit(_command("late"))
    assert replay.deduplicated is True
    assert chain.drive_count == 1
    coordinator.stop()


def test_timeout_during_precommit_validation_withdraws_before_durable_admission() -> None:
    admit_entered = threading.Event()
    admit_release = threading.Event()
    chain = _FakeChain(admit_entered=admit_entered, admit_release=admit_release)
    coordinator = _coordinator(chain, timeout=0.05)
    coordinator.start()
    errors: list[str] = []

    def submit() -> None:
        try:
            coordinator.submit(_command("precommit-timeout"))
        except DrawingMachineError as error:
            errors.append(error.payload.code)

    caller = threading.Thread(target=submit)
    caller.start()
    assert admit_entered.wait(timeout=1.0)
    caller.join(timeout=1.0)
    assert errors == ["MACHINE_COORDINATOR_ACK_TIMEOUT"]
    admit_release.set()
    time.sleep(0.05)
    assert chain.drive_count == 0
    coordinator.stop()


def test_commit_finishing_after_caller_timeout_still_drives_once_and_replays_durably() -> None:
    commit_entered = threading.Event()
    commit_release = threading.Event()
    chain = _FakeChain(commit_entered=commit_entered, commit_release=commit_release)
    coordinator = _coordinator(chain, timeout=0.05)
    coordinator.start()
    errors: list[str] = []

    def submit() -> None:
        try:
            coordinator.submit(_command("commit-race"))
        except DrawingMachineError as error:
            errors.append(error.payload.code)

    caller = threading.Thread(target=submit)
    caller.start()
    assert commit_entered.wait(timeout=1.0)
    caller.join(timeout=1.0)
    assert errors == ["MACHINE_COORDINATOR_ACK_TIMEOUT"]
    commit_release.set()
    deadline = time.monotonic() + 1.0
    while chain.drive_count != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    replay = coordinator.submit(_command("commit-race"))
    assert replay.deduplicated is True
    assert chain.drive_count == 1
    coordinator.stop()


def test_status_during_blocked_adapter_reads_only_immutable_projection() -> None:
    drive_entered = threading.Event()
    drive_release = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered, drive_release=drive_release)
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.submit(_command("motion"))
    assert drive_entered.wait(timeout=1.0)
    reads_before = chain.status_reads

    first = coordinator.status()
    cast(dict[str, object], first)["execution"] = "tampered"
    second = coordinator.status()

    assert second["execution"] != "tampered"
    assert chain.status_reads == reads_before
    drive_release.set()
    coordinator.stop()


def test_c13_f2_blocked_stream_status_publishes_durable_phase_progress_and_outcome() -> None:
    drive_entered = threading.Event()
    drive_release = threading.Event()

    class BlockedStreamChain:
        def __init__(self) -> None:
            self.admitted = False
            self.driving = False
            self.publisher: Callable[[JsonObject], object] | None = None

        def install_projection_publisher(self, publisher: Callable[[JsonObject], object]) -> None:
            self.publisher = publisher

        def initialize_and_reconcile(self) -> JsonObject:
            return {
                "schema_version": 1,
                "execution": {
                    "schema_version": 1,
                    "execution_id": "stream-execution",
                    "job_id": "stream-job",
                    "ready_revision": 7,
                    "revision": 7,
                    "phase": "AWAITING_STREAM_APPROVAL",
                    "stream_milestone": "NOT_STARTED",
                    "recovery_intent": None,
                    "retired": False,
                },
                "progress": None,
                "last_outcome": None,
            }

        def admit_action(self, command: object, *, still_requested: object) -> _Admission:
            del command, still_requested
            self.admitted = True
            return _Admission(
                "stream-execution",
                {
                    "schema_version": 1,
                    "deduplicated": False,
                    "admitted": True,
                    "execution_id": "stream-execution",
                    "job_id": "stream-job",
                    "job_revision": 7,
                    "execution_revision": 8,
                    "phase": "STREAMING",
                    "action": "STREAM",
                    "approval_challenge_id": str(uuid4()),
                    "challenge_issued_at": NOW.isoformat(),
                    "approved_at": NOW.isoformat(),
                    "issuer_pid": 31001,
                    "consumer_pid": 41002,
                },
            )

        def drive_admitted_action(self, admission: object) -> object:
            del admission
            self.driving = True
            assert self.publisher is not None
            self.publisher(self.status_projection("stream-execution"))
            drive_entered.set()
            assert drive_release.wait(timeout=2.0)
            return object()

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            del execution_id
            projection = self.initialize_and_reconcile()
            if self.admitted:
                execution = cast(JsonObject, projection["execution"])
                execution["revision"] = 8
                execution["phase"] = "STREAMING"
            if self.driving:
                projection["progress"] = {
                    "schema_version": 1,
                    "execution_id": "stream-execution",
                    "execution_revision": 8,
                    "total": 10,
                    "acknowledged": 1,
                    "ok_count": 1,
                    "error_count": 0,
                    "percent": 10.0,
                    "last_source_line": 1,
                    "last_command": "G21",
                    "last_event_at": NOW.isoformat(),
                    "failure": None,
                }
            return projection

        def shutdown_for_recovery(self) -> JsonObject:
            return self.status_projection()

        def close(self) -> None:
            return

    chain = BlockedStreamChain()
    coordinator = MachineCoordinator(chain_factory=lambda _epoch: chain, service_epoch=str(uuid4()))
    coordinator.start()
    try:
        coordinator.submit(
            StreamMachineCommand(
                "blocked-stream",
                AuthenticatedMachinePrincipal(2200, 2200, 41002),
                "stream-execution",
                MachineCommandMode.EXECUTE,
                str(uuid4()),
            )
        )
        assert drive_entered.wait(timeout=1.0)
        status = coordinator.status("stream-execution")
        assert status["execution"]["phase"] == "STREAMING"  # type: ignore[index]
        assert status["progress"]["acknowledged"] == 1  # type: ignore[index]
        assert status["last_outcome"] is None
    finally:
        drive_release.set()
        coordinator.stop()


def test_null_status_never_chooses_a_terminal_record() -> None:
    chain = _FakeChain()
    coordinator = _coordinator(chain)
    coordinator.start()

    status = coordinator.status()

    assert status["execution"] is None
    assert status["service_epoch"] == coordinator.service_epoch
    coordinator.stop()


def test_stop_rejects_new_commands_reconciles_then_joins_and_closes() -> None:
    chain = _FakeChain()
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.stop()

    with pytest.raises(DrawingMachineError) as error:
        coordinator.submit(_command("after-stop"))
    assert error.value.payload.code == "MACHINE_COORDINATOR_NOT_STARTED"
    assert coordinator.owner_thread is None
    assert chain.closed_thread is not None


def test_fatal_persistence_terminates_owner_and_never_retries_drive() -> None:
    chain = _FakeChain(fatal_on_drive=True)
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.submit(_command("fatal"))

    deadline = time.monotonic() + 1.0
    while coordinator.fatal_error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert coordinator.fatal_error is not None
    with pytest.raises(DrawingMachineError) as error:
        coordinator.submit(_command("fatal-2"))
    assert error.value.payload.code == "MACHINE_COORDINATOR_FATAL"
    assert chain.drive_count == 1
    coordinator.stop()


def test_real_sqlite_chain_fake_fluidnc_ack_drive_projection_and_shutdown_recovery(tmp_path) -> None:
    database = tmp_path / "coordinator-real-chain.db"
    setup = SQLiteMachineRepository(database)
    setup.initialize(applied_at=NOW.isoformat())
    _seed(setup, _job(_ready_snapshot()))
    setup.close()
    session_epoch = _uuid(2)
    session = ProbeSession(_preflight(session_epoch), CloseOutcome(session_epoch))
    factory = ProbeFactory(session_epoch, session)
    service_epoch = str(uuid4())
    wall_tick = 0

    def build_chain(epoch: str) -> MachineExecutionChain:
        nonlocal wall_tick
        identities = _identities()

        def wall_now():
            nonlocal wall_tick
            wall_tick += 1
            return NOW + timedelta(microseconds=wall_tick)

        return MachineExecutionChain(
            SQLiteMachineRepository(database),
            Reader(GCODE),
            factory,
            _config(),
            _authority(service_epoch=epoch),
            identity_factory=lambda: next(identities),
            wall_now=wall_now,
            monotonic_now=lambda: 1000.0,
        )

    coordinator = MachineCoordinator(
        chain_factory=build_chain,
        service_epoch=service_epoch,
        acknowledgement_timeout_seconds=0.5,
    )
    coordinator.start()
    callback_seen = threading.Event()

    def committed(_result: MachineCommandResult) -> None:
        assert factory.opens == 0
        callback_seen.set()

    result = coordinator.submit(
        PrepareMachineCommand(
            "real-prepare",
            AuthenticatedMachinePrincipal(1100, 1100, 31001),
            "job-c9",
            7,
        ),
        committed=committed,
    )
    assert result.response["phase"] == MachinePhase.PREPARING_SESSION.value
    assert callback_seen.is_set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        execution = coordinator.status()["execution"]
        if type(execution) is dict and execution["phase"] == MachinePhase.AWAITING_HOME_APPROVAL.value:
            break
        time.sleep(0.005)
    assert coordinator.status()["execution"]["phase"] == MachinePhase.AWAITING_HOME_APPROVAL.value  # type: ignore[index]
    assert factory.opens == 1
    replay, observation = coordinator.submit_observed(
        PrepareMachineCommand(
            "real-prepare",
            AuthenticatedMachinePrincipal(1100, 1100, 31999),
            "job-c9",
            7,
        )
    )
    assert replay.deduplicated is True and replay.response["phase"] == MachinePhase.PREPARING_SESSION.value
    assert observation["execution"]["phase"] == MachinePhase.AWAITING_HOME_APPROVAL.value  # type: ignore[index]
    assert factory.opens == 1

    coordinator.stop()

    assert session.close_calls == 1
    check = SQLiteMachineRepository(database)
    try:
        owner = check.get_active_execution()
        assert owner is not None
        assert owner.phase is MachinePhase.RECOVERY_REQUIRED
        assert owner.recovery_evidence is not None
        assert owner.recovery_evidence.reason_code == "SERVICE_SHUTDOWN"
        job = check._get_connection().execute("SELECT state, revision FROM jobs WHERE job_id = 'job-c9'").fetchone()
        assert job == (JobState.RECOVERY_REQUIRED.value, 8)
    finally:
        check.close()


def test_coordinator_constructor_envelope_and_startup_failure_boundaries() -> None:
    with pytest.raises(ValueError):
        MachineCoordinator(chain_factory=lambda _epoch: _FakeChain(), service_epoch="")
    for timeout in (0.0, 1.1, 1):
        with pytest.raises(ValueError):
            MachineCoordinator(
                chain_factory=lambda _epoch: _FakeChain(),
                service_epoch=str(uuid4()),
                acknowledgement_timeout_seconds=timeout,  # type: ignore[arg-type]
            )

    envelope = coordinator_module._Envelope(_command("private"), time.monotonic() + 1.0, threading.Event(), None)
    assert envelope.withdraw_queued() is True
    assert envelope.begin_admission() is False
    assert envelope.committed(MachineCommandResult({"schema_version": 1}, False)) is False
    envelope.failed(RuntimeError("ignored"))
    assert envelope.error is None
    admitting = coordinator_module._Envelope(
        _command("admitting"),
        time.monotonic() - 1.0,
        threading.Event(),
        None,
    )
    assert admitting.begin_admission() is True
    assert admitting.begin_admission() is False
    assert admitting.withdraw_queued() is False
    assert admitting.still_requested() is False

    def fail_factory(_epoch: str):
        raise RuntimeError("startup factory failed")

    coordinator = MachineCoordinator(chain_factory=fail_factory, service_epoch=str(uuid4()))
    with pytest.raises(RuntimeError, match="startup factory failed"):
        coordinator.start()
    assert coordinator.owner_thread is None

    chain = _FakeChain()
    running = _coordinator(chain)
    running.start()
    try:
        with pytest.raises(DrawingMachineError) as already:
            running.start()
        assert already.value.payload.code == "MACHINE_COORDINATOR_ALREADY_STARTED"
    finally:
        running.stop()
    running.stop()


def test_admission_callback_projection_and_nonfatal_drive_failure_boundaries() -> None:
    class InvalidAdmissionChain(_FakeChain):
        def admit_action(self, command, *, still_requested):
            del command, still_requested
            return object()

    invalid = _coordinator(InvalidAdmissionChain())
    invalid.start()
    with pytest.raises(DrawingMachineError) as rejected:
        invalid.submit(_command("invalid-admission"))
    assert rejected.value.payload.code == "MACHINE_COORDINATOR_ADMISSION_INVALID"
    invalid.stop()

    class AdmissionErrorChain(_FakeChain):
        def admit_action(self, command, *, still_requested):
            del command, still_requested
            raise RuntimeError("admission failed")

    admission_error = _coordinator(AdmissionErrorChain())
    admission_error.start()
    try:
        with pytest.raises(RuntimeError, match="admission failed"):
            admission_error.submit(_command("admission-error"))
    finally:
        admission_error.stop()

    class FatalAdmissionChain(_FakeChain):
        def admit_action(self, command, *, still_requested):
            del command, still_requested
            raise DrawingMachineError(
                ErrorPayload(
                    "MACHINE_COORDINATOR_FATAL_PERSISTENCE",
                    ErrorCategory.HARDWARE,
                    "fatal admission persistence",
                    False,
                    {},
                )
            )

    fatal_admission_chain = FatalAdmissionChain()
    fatal_admission = _coordinator(fatal_admission_chain)
    fatal_admission.start()
    with pytest.raises(DrawingMachineError) as fatal_admission_error:
        fatal_admission.submit(_command("fatal-admission"))
    assert fatal_admission_error.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"
    fatal_admission.stop()
    assert fatal_admission_chain.shutdown_count == 1

    class NonfatalDriveChain(_FakeChain):
        def drive_admitted_action(self, admission: object) -> object:
            del admission
            self.drive_count += 1
            raise DrawingMachineError(ErrorPayload("HOME_FAILED", ErrorCategory.HARDWARE, "typed failure", False, {}))

    nonfatal_chain = NonfatalDriveChain()
    nonfatal = _coordinator(nonfatal_chain)
    nonfatal.start()
    nonfatal.submit(_command("nonfatal"))
    deadline = time.monotonic() + 1.0
    while nonfatal.fatal_error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert nonfatal_chain.drive_count == 1
    assert isinstance(nonfatal.fatal_error, DrawingMachineError)
    assert nonfatal_chain.shutdown_count == 1
    nonfatal.stop()

    callback_chain = _FakeChain()
    callback_failure = _coordinator(callback_chain)
    callback_failure.start()
    callback_failure.submit(
        _command("callback-failure"),
        committed=lambda _result: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )
    deadline = time.monotonic() + 1.0
    while callback_failure.fatal_error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert isinstance(callback_failure.fatal_error, DrawingMachineError)
    assert callback_failure.fatal_error.payload.code == "MACHINE_COORDINATOR_FATAL"
    assert callback_chain.drive_count == 0
    callback_failure.stop()


def test_projection_validation_monotonic_terminal_and_job_publication_boundaries() -> None:
    published: list[str] = []
    coordinator = MachineCoordinator(
        chain_factory=lambda _epoch: _FakeChain(),
        service_epoch=str(uuid4()),
        job_publisher=published.append,
    )
    for invalid in (None, {"execution": []}, {"execution": {"execution_id": "", "revision": 0}}):
        with pytest.raises(DrawingMachineError) as rejected:
            coordinator._publish_projection(invalid)  # type: ignore[arg-type]
        assert rejected.value.payload.code == "MACHINE_COORDINATOR_PROJECTION_INVALID"
    projection_execution: JsonObject = {
        "execution_id": "invalid-sidecar",
        "job_id": "job-sidecar",
        "revision": 1,
        "phase": "STREAMING",
    }
    for invalid in (
        {"execution": projection_execution, "progress": []},
        {"execution": projection_execution, "last_outcome": []},
    ):
        with pytest.raises(DrawingMachineError) as rejected:
            coordinator._publish_projection(invalid)
        assert rejected.value.payload.code == "MACHINE_COORDINATOR_PROJECTION_INVALID"
    coordinator._publish_projection({"schema_version": 1, "execution": None})
    coordinator._publish_projection(
        {
            "execution": {
                "execution_id": "execution-projection",
                "job_id": "job-projection",
                "revision": 2,
                "phase": "STREAMING",
            }
        }
    )
    coordinator._publish_projection(
        {
            "execution": {
                "execution_id": "execution-projection",
                "job_id": "job-projection",
                "revision": 1,
                "phase": "PREPARING_SESSION",
            }
        }
    )
    assert coordinator.status()["execution"]["revision"] == 2  # type: ignore[index]
    coordinator._publish_projection(coordinator.status())
    assert coordinator.status()["execution"]["phase"] == "STREAMING"  # type: ignore[index]
    coordinator._publish_job({"execution": None})
    coordinator._publish_job({"execution": {"job_id": 7}})
    coordinator._publish_job({"execution": {"job_id": "job-projection"}})
    assert published == ["job-projection"]
    coordinator._publish_projection(
        {
            "execution": {
                "execution_id": "other-terminal",
                "job_id": "job-other",
                "revision": 1,
                "phase": "COMPLETED",
            }
        }
    )
    assert coordinator.status()["execution"]["execution_id"] == "execution-projection"  # type: ignore[index]
    coordinator._publish_projection(
        {
            "execution": {
                "execution_id": "execution-projection",
                "job_id": "job-projection",
                "revision": 3,
                "phase": "RECOVERY_REQUIRED",
                "retired": True,
            }
        }
    )
    assert coordinator.status()["execution"] is None
    assert coordinator._execution_id({}) is None
    assert coordinator._is_fatal(RuntimeError("ordinary")) is False


def test_startup_close_shutdown_error_and_optional_lifecycle_boundaries() -> None:
    class StartupFailureChain(_FakeChain):
        def initialize_and_reconcile(self) -> JsonObject:
            self.owner_thread = threading.get_ident()
            raise RuntimeError("startup reconcile failed")

    startup_chain = StartupFailureChain()
    startup = _coordinator(startup_chain)
    with pytest.raises(RuntimeError, match="startup reconcile failed"):
        startup.start()
    assert startup_chain.closed_thread is not None

    class StartupCleanupFailureChain(StartupFailureChain):
        def shutdown_for_recovery(self) -> JsonObject:
            raise RuntimeError("startup cleanup failed")

    startup_cleanup = _coordinator(StartupCleanupFailureChain())
    with pytest.raises(DrawingMachineError) as startup_cleanup_error:
        startup_cleanup.start()
    assert startup_cleanup_error.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"

    class ShutdownFailureChain(_FakeChain):
        def shutdown_for_recovery(self) -> JsonObject:
            raise RuntimeError("shutdown recovery failed")

    shutdown = _coordinator(ShutdownFailureChain())
    shutdown.start()
    with pytest.raises(DrawingMachineError) as shutdown_error:
        shutdown.stop()
    assert shutdown_error.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"

    class MinimalChain:
        def admit_action(self, command, *, still_requested):
            del command, still_requested
            return MachineCommandResult({"schema_version": 1}, False)

        def drive_admitted_action(self, admission: object) -> object:
            return admission

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            del execution_id
            return {"schema_version": 1, "execution": None}

    minimal = MachineCoordinator(chain_factory=lambda _epoch: MinimalChain(), service_epoch=str(uuid4()))
    minimal.start()
    minimal.stop()


def test_defensive_missing_result_boundary() -> None:
    class ThreadStub:
        daemon = False

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            del timeout

    class AckCommandQueue:
        def put(self, item: object) -> None:
            if isinstance(item, coordinator_module._Envelope):
                item.acknowledged.set()

    missing = _coordinator(_FakeChain())
    missing._thread = ThreadStub()  # type: ignore[assignment]
    missing._mailbox = AckCommandQueue()  # type: ignore[assignment]
    with pytest.raises(DrawingMachineError) as no_result:
        missing.submit(_command("missing-result"))
    assert no_result.value.payload.code == "MACHINE_COORDINATOR_FAILED"
    missing._thread = None


def test_predrive_projection_failure_propagates_to_owner_seam_without_dispatch() -> None:
    class ProjectionFailureAfterDriveError(_FakeChain):
        def __init__(self) -> None:
            super().__init__()
            self.projection_calls = 0

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            self.projection_calls += 1
            if self.projection_calls == 1:
                raise RuntimeError("projection read failed")
            return super().status_projection(execution_id)

        def drive_admitted_action(self, admission: object) -> object:
            self.drive_count += 1
            return admission

    chain = ProjectionFailureAfterDriveError()
    coordinator = _coordinator(chain)
    chain.owner_thread = threading.get_ident()
    envelope = coordinator_module._Envelope(
        _command("projection-failure"),
        time.monotonic() + 1.0,
        threading.Event(),
        None,
    )
    assert envelope.begin_admission() is True

    with pytest.raises(RuntimeError, match="projection read failed"):
        coordinator._process(chain, envelope)

    assert chain.drive_count == 0
    assert chain.projection_calls == 1
    assert coordinator.fatal_error is None


def test_f1_durable_admission_is_acked_before_any_fallible_projection_read() -> None:
    class ProjectionFailureAfterAdmission(_FakeChain):
        def __init__(self) -> None:
            super().__init__()
            self.projection_calls = 0

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            self.projection_calls += 1
            if self.projection_calls == 1:
                raise RuntimeError("projection failed after durable admission")
            return super().status_projection(execution_id)

    chain = ProjectionFailureAfterAdmission()
    coordinator = _coordinator(chain, timeout=0.05)
    coordinator.start()
    result: MachineCommandResult | None = None
    error: BaseException | None = None
    try:
        result = coordinator.submit(_command("f1-projection"))
    except BaseException as caught:
        error = caught
    stop_errors: list[BaseException] = []
    _capture_error(coordinator.stop, stop_errors)

    assert error is None
    assert result is not None and result.response["execution_id"] == "execution-1"
    assert chain.drive_count == 0
    assert chain.shutdown_count == 1
    assert chain.close_count == 1
    assert stop_errors == []


@pytest.mark.parametrize("failure", ["callback", "invalid-admission", "projection"])
def test_f2_every_post_start_terminal_exception_runs_shutdown_then_close(failure: str) -> None:
    class TerminalFailureChain(_FakeChain):
        def admit_action(self, command, *, still_requested):
            if failure == "invalid-admission":
                return object()
            return super().admit_action(command, still_requested=still_requested)

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            if failure == "projection" and self.drive_count == 1:
                raise RuntimeError("terminal projection failed")
            return super().status_projection(execution_id)

    chain = TerminalFailureChain()
    coordinator = _coordinator(chain)
    coordinator.start()
    callback = (
        (lambda _result: (_ for _ in ()).throw(RuntimeError("callback disconnected")))
        if failure == "callback"
        else None
    )
    with suppress(DrawingMachineError):
        coordinator.submit(_command(f"f2-{failure}"), committed=callback)
    deadline = time.monotonic() + 1.0
    while coordinator.owner_thread is not None and coordinator.owner_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    stop_errors: list[BaseException] = []
    _capture_error(coordinator.stop, stop_errors)

    assert chain.shutdown_count == 1
    assert chain.close_count == 1
    if stop_errors:
        assert len(stop_errors) == 1
        assert isinstance(stop_errors[0], DrawingMachineError)
        assert stop_errors[0].payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"  # type: ignore[union-attr]


@pytest.mark.parametrize("failure", ["callback", "invalid-admission", "projection"])
def test_f2_real_sqlite_open_fake_session_is_recovered_and_closed_on_terminal_fault(
    tmp_path: Path,
    failure: str,
) -> None:
    epoch = _uuid(2)
    factory = _strict_factory(epoch)
    coordinator, database, wrapper = _real_coordinator_fixture(tmp_path, factory)
    execution_id: str
    if failure == "projection":
        wrapper.fail_projection_after_drive = True
        coordinator.submit(_real_prepare("real-projection"))
        execution_id = ""
    else:
        prepared = coordinator.submit(_real_prepare("real-open"))
        execution_id = cast(str, prepared.response["execution_id"])
        assert _wait_for_phase(coordinator, MachinePhase.AWAITING_HOME_APPROVAL) == execution_id
        if failure == "invalid-admission":
            wrapper.invalid_admission = True
            with pytest.raises(DrawingMachineError) as rejected:
                coordinator.submit(_real_prepare("real-invalid"))
            assert rejected.value.payload.code == "MACHINE_COORDINATOR_ADMISSION_INVALID"
        else:
            requested = coordinator.submit(
                HomeMachineCommand(
                    "real-home-request",
                    AuthenticatedMachinePrincipal(1100, 1100, 31002),
                    execution_id,
                    MachineCommandMode.REQUEST,
                    None,
                )
            )
            coordinator.submit(
                HomeMachineCommand(
                    "real-home-execute",
                    AuthenticatedMachinePrincipal(2200, 2200, 41002),
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    _challenge_id(requested),
                ),
                committed=lambda _result: (_ for _ in ()).throw(RuntimeError("callback disconnected")),
            )
    owner = coordinator.owner_thread
    assert owner is not None
    owner.join(timeout=2.0)
    assert not owner.is_alive()
    coordinator.stop()

    assert factory.connections_opened == 1
    assert FakeFluidNCOperation.CLOSE in factory.started_operations
    assert FakeFluidNCOperation.HOME_ALL_AXES not in factory.started_operations
    factory.assert_script_consumed()
    check = SQLiteMachineRepository(database)
    try:
        recovered = check.get_active_execution()
        assert recovered is not None
        assert recovered.phase is MachinePhase.RECOVERY_REQUIRED
        assert recovered.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
        assert recovered.recovery_evidence is not None
        assert recovered.recovery_evidence.reason_code == "SERVICE_SHUTDOWN"
        job = (
            check._get_connection()
            .execute(
                "SELECT state, revision FROM jobs WHERE job_id = 'job-c9'",
            )
            .fetchone()
        )
        assert job == (JobState.RECOVERY_REQUIRED.value, 8)
    finally:
        check.close()


def test_f3_stop_never_returns_while_the_owner_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive_entered = threading.Event()
    drive_release = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered, drive_release=drive_release)
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.submit(_command("f3-blocked"))
    assert drive_entered.wait(timeout=1.0)

    class ImmediateTimeoutEvent:
        def set(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            return False

    errors: list[BaseException] = []
    stopper = threading.Thread(target=lambda: _capture_error(coordinator.stop, errors))
    monkeypatch.setattr(coordinator_module.threading, "Event", ImmediateTimeoutEvent)
    stopper.start()
    stopper.join(timeout=0.1)
    remained_blocked = stopper.is_alive()
    drive_release.set()
    monkeypatch.undo()
    stopper.join(timeout=1.0)
    owner = coordinator.owner_thread
    if owner is not None:
        owner.join(timeout=1.0)

    assert remained_blocked
    assert errors == []
    assert chain.shutdown_count == 1
    assert chain.close_count == 1


def test_f4_stop_withdraws_queued_envelopes_before_the_next_admission() -> None:
    drive_entered = threading.Event()
    drive_release = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered, drive_release=drive_release)
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.submit(_command("f4-current"))
    assert drive_entered.wait(timeout=1.0)
    second_results: list[MachineCommandResult] = []
    second_errors: list[BaseException] = []
    second = threading.Thread(
        target=lambda: _capture_result(
            lambda: coordinator.submit(_command("f4-queued")),
            second_results,
            second_errors,
        )
    )
    second.start()
    deadline = time.monotonic() + 1.0
    while coordinator._mailbox.qsize() == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    stopper = threading.Thread(target=coordinator.stop)
    stopper.start()
    time.sleep(0.05)
    drive_release.set()
    second.join(timeout=1.0)
    stopper.join(timeout=1.0)

    assert chain.admit_order == ["f4-current"]
    assert chain.drive_count == 1
    assert second_results == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], DrawingMachineError)
    assert second_errors[0].payload.code == "MACHINE_COORDINATOR_STOPPED"  # type: ignore[union-attr]
    assert chain.shutdown_count == 1
    assert chain.close_count == 1


def test_real_mailbox_two_different_prepares_leave_one_global_latch_and_one_open(tmp_path: Path) -> None:
    epoch = _uuid(2)
    factory = _strict_factory(epoch)
    coordinator, database, _wrapper = _real_coordinator_fixture(
        tmp_path,
        factory,
        job_ids=("job-c9", "job-other"),
    )
    callback_entered = threading.Event()
    callback_release = threading.Event()

    first = coordinator.submit(
        PrepareMachineCommand(
            "latch-first",
            AuthenticatedMachinePrincipal(1100, 1100, 31001),
            "job-c9",
            7,
        ),
        committed=lambda _result: (callback_entered.set(), callback_release.wait(timeout=2.0)),
    )
    assert callback_entered.wait(timeout=1.0)
    second_results: list[MachineCommandResult] = []
    second_errors: list[BaseException] = []
    second = threading.Thread(
        target=lambda: _capture_result(
            lambda: coordinator.submit(
                PrepareMachineCommand(
                    "latch-second",
                    AuthenticatedMachinePrincipal(1100, 1100, 31002),
                    "job-other",
                    7,
                )
            ),
            second_results,
            second_errors,
        )
    )
    second.start()
    callback_release.set()
    second.join(timeout=2.0)
    assert not second.is_alive()
    assert _wait_for_phase(coordinator, MachinePhase.AWAITING_HOME_APPROVAL) == first.response["execution_id"]

    assert second_results == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], DrawingMachineError)
    assert second_errors[0].payload.code == "MACHINE_OWNER_CONFLICT"  # type: ignore[union-attr]
    assert factory.connections_opened == 1
    check = SQLiteMachineRepository(database)
    try:
        active = check.get_active_execution()
        assert active is not None and active.job_id == "job-c9"
        assert check._get_connection().execute(
            "SELECT COUNT(*) FROM machine_executions WHERE retired_at IS NULL AND phase != 'COMPLETED'"
        ).fetchone() == (1,)
    finally:
        check.close()
    coordinator.stop()
    factory.assert_script_consumed()


def test_real_mailbox_prepare_cancel_before_reservation_has_one_durable_winner(tmp_path: Path) -> None:
    class BlockingReader(_AnyJobReader):
        def __init__(self) -> None:
            super().__init__(GCODE)
            self.entered = threading.Event()
            self.release = threading.Event()

        def read_bytes(self, *args, **kwargs):
            self.entered.set()
            assert self.release.wait(timeout=2.0)
            return super().read_bytes(*args, **kwargs)

    epoch = _uuid(2)
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(epoch), ())
    reader = BlockingReader()
    coordinator, database, _wrapper = _real_coordinator_fixture(tmp_path, factory, reader=reader)
    results: list[MachineCommandResult] = []
    errors: list[BaseException] = []
    submitter = threading.Thread(
        target=lambda: _capture_result(lambda: coordinator.submit(_real_prepare("prepare-cancel")), results, errors)
    )
    submitter.start()
    assert reader.entered.wait(timeout=1.0)
    connection = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE jobs SET state = 'CANCELLED', revision = 8, updated_at = ? WHERE job_id = 'job-c9'",
            ((NOW + timedelta(seconds=1)).isoformat(),),
        )
    finally:
        connection.close()
    reader.release.set()
    submitter.join(timeout=2.0)
    assert not submitter.is_alive()

    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], DrawingMachineError)
    assert factory.connections_opened == 0
    check = SQLiteMachineRepository(database)
    try:
        assert check.get_active_execution() is None
        assert check._get_connection().execute(
            "SELECT state, revision FROM jobs WHERE job_id = 'job-c9'"
        ).fetchone() == (JobState.CANCELLED.value, 8)
    finally:
        check.close()
    coordinator.stop()


def test_real_mailbox_two_consumers_of_one_challenge_dispatch_home_once(tmp_path: Path) -> None:
    epoch = _uuid(2)
    factory = _strict_factory(
        epoch,
        FakeFluidNCStep(
            FakeFluidNCOperation.HOME_ALL_AXES,
            _home_request(),
            _home_outcome(epoch),
        ),
    )
    coordinator, database, _wrapper = _real_coordinator_fixture(tmp_path, factory)
    prepared = coordinator.submit(_real_prepare("challenge-prepare"))
    execution_id = cast(str, prepared.response["execution_id"])
    assert _wait_for_phase(coordinator, MachinePhase.AWAITING_HOME_APPROVAL) == execution_id
    requested = coordinator.submit(
        HomeMachineCommand(
            "challenge-request",
            AuthenticatedMachinePrincipal(1100, 1100, 31003),
            execution_id,
            MachineCommandMode.REQUEST,
            None,
        )
    )
    challenge_id = _challenge_id(requested)
    callback_entered = threading.Event()
    callback_release = threading.Event()
    first = coordinator.submit(
        HomeMachineCommand(
            "challenge-consumer-first",
            AuthenticatedMachinePrincipal(2200, 2200, 41002),
            execution_id,
            MachineCommandMode.EXECUTE,
            challenge_id,
        ),
        committed=lambda _result: (callback_entered.set(), callback_release.wait(timeout=2.0)),
    )
    assert first.response["phase"] == MachinePhase.HOMING.value
    assert callback_entered.wait(timeout=1.0)
    second_results: list[MachineCommandResult] = []
    second_errors: list[BaseException] = []
    second = threading.Thread(
        target=lambda: _capture_result(
            lambda: coordinator.submit(
                HomeMachineCommand(
                    "challenge-consumer-second",
                    AuthenticatedMachinePrincipal(2200, 2200, 41003),
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    challenge_id,
                )
            ),
            second_results,
            second_errors,
        )
    )
    second.start()
    callback_release.set()
    second.join(timeout=2.0)
    assert not second.is_alive()
    assert _wait_for_phase(coordinator, MachinePhase.AWAITING_ZCAL_APPROVAL) == execution_id

    replay_request, request_observation = coordinator.submit_observed(
        HomeMachineCommand(
            "challenge-request",
            AuthenticatedMachinePrincipal(1100, 1100, 31998),
            execution_id,
            MachineCommandMode.REQUEST,
            None,
        )
    )
    replay_execute, execute_observation = coordinator.submit_observed(
        HomeMachineCommand(
            "challenge-consumer-first",
            AuthenticatedMachinePrincipal(2200, 2200, 41998),
            execution_id,
            MachineCommandMode.EXECUTE,
            challenge_id,
        )
    )
    assert replay_request.deduplicated is True
    assert replay_request.response["phase"] == MachinePhase.AWAITING_HOME_APPROVAL.value
    assert replay_execute.deduplicated is True
    assert replay_execute.response["phase"] == MachinePhase.HOMING.value
    assert request_observation["execution"]["phase"] == MachinePhase.AWAITING_ZCAL_APPROVAL.value  # type: ignore[index]
    assert execute_observation["execution"]["phase"] == MachinePhase.AWAITING_ZCAL_APPROVAL.value  # type: ignore[index]

    assert second_results == []
    assert len(second_errors) == 1 and isinstance(second_errors[0], DrawingMachineError)
    assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    check = SQLiteMachineRepository(database)
    try:
        challenge = check.get_challenge(challenge_id)
        assert challenge is not None and challenge.consumer is not None
        assert challenge.consumer.pid == 41002
    finally:
        check.close()
    coordinator.stop()
    factory.assert_script_consumed()


def test_f5_equal_revision_is_idempotent_only_for_the_exact_projection() -> None:
    published: list[str] = []
    coordinator = MachineCoordinator(
        chain_factory=lambda _epoch: _FakeChain(),
        service_epoch=str(uuid4()),
        job_publisher=published.append,
    )
    accepted: JsonObject = {
        "execution": {
            "execution_id": "f5-execution",
            "job_id": "f5-job",
            "revision": 2,
            "phase": "STREAMING",
        }
    }
    coordinator._publish_projection(accepted)
    coordinator._publish_projection(accepted)
    coordinator._publish_projection(
        {
            "execution": {
                "execution_id": "f5-execution",
                "job_id": "f5-job",
                "revision": 1,
                "phase": "PREPARING_SESSION",
            }
        }
    )
    conflict: JsonObject = {
        "execution": {
            "execution_id": "f5-execution",
            "job_id": "f5-job",
            "revision": 2,
            "phase": "PREPARING_SESSION",
        }
    }
    with pytest.raises(DrawingMachineError) as rejected:
        coordinator._publish_projection(conflict)
    assert rejected.value.payload.code == "MACHINE_COORDINATOR_PROJECTION_CONFLICT"
    assert coordinator.status()["execution"]["phase"] == "STREAMING"  # type: ignore[index]

    class ConflictChain(_FakeChain):
        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            del execution_id
            return conflict

    with pytest.raises(DrawingMachineError):
        projection = coordinator._refresh_projection(ConflictChain(), "f5-execution")
        coordinator._publish_job(projection)
    assert published == []


def test_f2_observed_submit_waits_for_projection_and_sidecars_never_regress() -> None:
    chain = _FakeChain()
    coordinator = _coordinator(chain)
    coordinator.start()
    try:
        result, observation = coordinator.submit_observed(_command("observed-submit"))
        assert result.response["execution_id"] == "execution-1"
        execution = observation["execution"]
        assert isinstance(execution, dict)
        assert execution["execution_id"] == "execution-1"
        assert execution["revision"] == 2
        replay, current = coordinator.submit_observed(_command("observed-submit"))
        assert replay.deduplicated is True
        assert replay.response["phase"] == "PREPARING_SESSION"
        current_execution = current["execution"]
        assert isinstance(current_execution, dict)
        assert current_execution["revision"] == 2
        assert chain.drive_count == 1
    finally:
        coordinator.stop()

    projection_coordinator = MachineCoordinator(
        chain_factory=lambda _epoch: _FakeChain(),
        service_epoch=str(uuid4()),
    )
    execution_projection: JsonObject = {
        "execution_id": "sidecar-execution",
        "job_id": "sidecar-job",
        "revision": 8,
        "phase": "STREAMING",
    }
    progress: JsonObject = {
        "execution_revision": 8,
        "acknowledged": 2,
        "ok_count": 2,
        "error_count": 0,
    }
    outcome: JsonObject = {"kind": "COMPLETED", "execution_revision": 8}
    projection_coordinator._publish_projection(
        {"execution": execution_projection, "progress": progress, "last_outcome": outcome}
    )
    regressions = (
        {"execution": execution_projection, "progress": None, "last_outcome": outcome},
        {
            "execution": execution_projection,
            "progress": {**progress, "acknowledged": 1},
            "last_outcome": outcome,
        },
        {
            "execution": execution_projection,
            "progress": progress,
            "last_outcome": {"kind": "RECOVERY_REQUIRED", "execution_revision": 8},
        },
    )
    for regression in regressions:
        with pytest.raises(DrawingMachineError) as rejected:
            projection_coordinator._publish_projection(regression)
        assert rejected.value.payload.code == "MACHINE_COORDINATOR_PROJECTION_CONFLICT"


def test_review_fix_defensive_stop_projection_and_close_branches() -> None:
    rejected = coordinator_module._Envelope(
        _command("already-admitting"),
        time.monotonic() + 1.0,
        threading.Event(),
        None,
    )
    assert rejected.begin_admission() is True
    assert rejected.reject_queued(RuntimeError("stop")) is False

    stopped_chain = _FakeChain()
    stopped = _coordinator(stopped_chain)
    stopped._mailbox.put(
        coordinator_module._Envelope(
            _command("owner-observes-stop"),
            time.monotonic() + 1.0,
            threading.Event(),
            None,
        )
    )
    stopped._stopping = True
    stopped._run()
    assert stopped_chain.admit_order == []
    assert stopped_chain.shutdown_count == 1

    lower_chain = _FakeChain()
    lower_chain.owner_thread = threading.get_ident()
    lower = _coordinator(lower_chain)
    lower._publish_projection(
        {
            "execution": {
                "execution_id": "execution-1",
                "job_id": "job-1",
                "revision": 99,
                "phase": "STREAMING",
            }
        }
    )
    envelope = coordinator_module._Envelope(
        _command("lower-after-drive"),
        time.monotonic() + 1.0,
        threading.Event(),
        None,
    )
    assert envelope.begin_admission() is True
    lower._process(lower_chain, envelope)
    assert lower_chain.drive_count == 1

    class CloseFailureChain(_FakeChain):
        def close(self) -> None:
            super().close()
            raise RuntimeError("close failed")

    close_failure = _coordinator(CloseFailureChain())
    close_failure.start()
    with pytest.raises(DrawingMachineError) as fixed:
        close_failure.stop()
    assert fixed.value.payload.code == "MACHINE_COORDINATOR_FATAL_PERSISTENCE"


def test_two_concurrent_stop_callers_both_wait_for_one_owner_cleanup() -> None:
    drive_entered = threading.Event()
    drive_release = threading.Event()
    chain = _FakeChain(drive_entered=drive_entered, drive_release=drive_release)
    coordinator = _coordinator(chain)
    coordinator.start()
    coordinator.submit(_command("two-stoppers"))
    assert drive_entered.wait(timeout=1.0)
    errors: list[BaseException] = []
    stoppers = [threading.Thread(target=lambda: _capture_error(coordinator.stop, errors)) for _index in range(2)]
    for stopper in stoppers:
        stopper.start()
    time.sleep(0.05)
    assert all(stopper.is_alive() for stopper in stoppers)
    drive_release.set()
    for stopper in stoppers:
        stopper.join(timeout=1.0)
        assert not stopper.is_alive()
    assert errors == []
    assert chain.shutdown_count == 1
    assert chain.close_count == 1


def test_f4_mailbox_get_to_admission_claim_observes_intervening_stop() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BarrierCoordinator(MachineCoordinator):
        def _before_admission_claim(self) -> None:
            entered.set()
            assert release.wait(timeout=2.0)

    chain = _FakeChain()
    coordinator = BarrierCoordinator(
        chain_factory=lambda _epoch: chain,
        service_epoch=str(uuid4()),
        acknowledgement_timeout_seconds=0.5,
    )
    coordinator.start()
    results: list[MachineCommandResult] = []
    errors: list[BaseException] = []
    submitter = threading.Thread(
        target=lambda: _capture_result(lambda: coordinator.submit(_command("f4-hostile-window")), results, errors)
    )
    submitter.start()
    barrier_observed = entered.wait(timeout=0.2)
    if not barrier_observed:
        coordinator.stop()
        submitter.join(timeout=1.0)
    else:
        stopper = threading.Thread(target=coordinator.stop)
        stopper.start()
        deadline = time.monotonic() + 1.0
        while not coordinator._stopping and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()
        submitter.join(timeout=1.0)
        stopper.join(timeout=1.0)
        assert not stopper.is_alive()

    assert barrier_observed
    assert not submitter.is_alive()
    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], DrawingMachineError)
    assert errors[0].payload.code == "MACHINE_COORDINATOR_STOPPED"  # type: ignore[union-attr]
    assert chain.admit_order == []
    assert chain.drive_count == 0


def test_n1_direct_durable_result_acks_callbacks_then_refreshes_retired_projection() -> None:
    callback_seen = threading.Event()

    class DirectReleaseChain(_FakeChain):
        def __init__(self) -> None:
            super().__init__()
            self.released = False

        def initialize_and_reconcile(self) -> JsonObject:
            self.owner_thread = threading.get_ident()
            return self.status_projection("release-execution")

        def admit_action(self, command, *, still_requested):
            del command, still_requested
            self.released = True
            return MachineCommandResult(
                {
                    "schema_version": 1,
                    "deduplicated": False,
                    "executed": True,
                    "execution_id": "release-execution",
                    "phase": "RECOVERY_REQUIRED",
                    "released": True,
                },
                False,
            )

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            del execution_id
            return {
                "schema_version": 1,
                "execution": {
                    "schema_version": 1,
                    "execution_id": "release-execution",
                    "job_id": "release-job",
                    "revision": 2 if self.released else 1,
                    "phase": "RECOVERY_REQUIRED",
                    "retired": self.released,
                },
            }

    chain = DirectReleaseChain()
    coordinator = _coordinator(chain)
    coordinator.start()
    result = coordinator.submit(
        _command("direct-release"),
        committed=lambda _result: callback_seen.set(),
    )
    assert result.response["released"] is True
    assert callback_seen.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while coordinator.status()["execution"] is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert coordinator.status()["execution"] is None
    coordinator.stop()


def test_n1_actual_recovery_release_refreshes_retired_owner_after_callback(tmp_path: Path) -> None:
    first_epoch = _uuid(2)
    first_factory = _strict_factory(first_epoch)
    first, database, _wrapper = _real_coordinator_fixture(tmp_path, first_factory)
    prepared = first.submit(_real_prepare("release-prepare"))
    execution_id = cast(str, prepared.response["execution_id"])
    assert _wait_for_phase(first, MachinePhase.AWAITING_HOME_APPROVAL) == execution_id
    first.stop()
    first_factory.assert_script_consumed()

    release_factory = StrictFakeFluidNCFactory(OpenSessionRequest(_uuid(3)), ())
    service_epoch = str(uuid4())
    identities = _identities()
    wall_tick = 0

    def build_release_chain(epoch: str) -> MachineExecutionChain:
        def wall_now():
            nonlocal wall_tick
            wall_tick += 1
            return NOW + timedelta(seconds=10, microseconds=wall_tick)

        return MachineExecutionChain(
            SQLiteMachineRepository(database),
            _AnyJobReader(GCODE),
            release_factory,
            _config(),
            _authority(service_epoch=epoch),
            identity_factory=lambda: next(identities),
            wall_now=wall_now,
            monotonic_now=lambda: 1000.0,
        )

    release = MachineCoordinator(
        chain_factory=build_release_chain,
        service_epoch=service_epoch,
        acknowledgement_timeout_seconds=0.5,
    )
    release.start()
    assert release.status()["execution"] is not None
    requested = release.submit(
        RecoverMachineCommand(
            "release-request",
            AuthenticatedMachinePrincipal(1100, 1100, 31010),
            execution_id,
            RecoveryDisposition.RELEASE,
            MachineCommandMode.REQUEST,
            None,
        )
    )
    callback_saw_active = threading.Event()
    released = release.submit(
        RecoverMachineCommand(
            "release-execute",
            AuthenticatedMachinePrincipal(2200, 2200, 41010),
            execution_id,
            RecoveryDisposition.RELEASE,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        ),
        committed=lambda _result: callback_saw_active.set() if release.status()["execution"] is not None else None,
    )
    assert released.response["released"] is True
    assert callback_saw_active.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while release.status()["execution"] is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert release.status()["execution"] is None
    replay_request, request_observation = release.submit_observed(
        RecoverMachineCommand(
            "release-request",
            AuthenticatedMachinePrincipal(1100, 1100, 31997),
            execution_id,
            RecoveryDisposition.RELEASE,
            MachineCommandMode.REQUEST,
            None,
        )
    )
    replay_release, release_observation = release.submit_observed(
        RecoverMachineCommand(
            "release-execute",
            AuthenticatedMachinePrincipal(2200, 2200, 41997),
            execution_id,
            RecoveryDisposition.RELEASE,
            MachineCommandMode.EXECUTE,
            _challenge_id(requested),
        )
    )
    assert replay_request.deduplicated is True and replay_release.deduplicated is True
    assert request_observation["execution"]["retired"] is True  # type: ignore[index]
    assert release_observation["execution"]["retired"] is True  # type: ignore[index]
    release.stop()
    assert release_factory.connections_opened == 0

    check = SQLiteMachineRepository(database)
    try:
        durable = check.get_execution(execution_id)
        assert durable is not None and durable.retirement is not None
        assert durable.retirement.disposition.value == "RELEASED"
        assert check.get_active_execution() is None
    finally:
        check.close()


def test_n1_direct_result_projection_fault_is_terminal_after_ack_and_callback() -> None:
    callback_seen = threading.Event()

    class DirectProjectionFault(_FakeChain):
        def __init__(self) -> None:
            super().__init__()
            self.direct = False

        def admit_action(self, command, *, still_requested):
            del command, still_requested
            self.direct = True
            return MachineCommandResult(
                {"schema_version": 1, "execution_id": "direct-fault", "phase": "RECOVERY_REQUIRED"},
                False,
            )

        def status_projection(self, execution_id: str | None = None) -> JsonObject:
            if self.direct and execution_id == "direct-fault":
                raise RuntimeError("direct result projection fault")
            return super().status_projection(execution_id)

    chain = DirectProjectionFault()
    coordinator = _coordinator(chain)
    coordinator.start()
    result = coordinator.submit(
        _command("direct-fault"),
        committed=lambda _result: callback_seen.set(),
    )
    assert result.response["execution_id"] == "direct-fault"
    assert callback_seen.wait(timeout=1.0)
    owner = coordinator.owner_thread
    assert owner is not None
    owner.join(timeout=1.0)
    assert not owner.is_alive()
    assert coordinator.fatal_error is not None
    assert chain.shutdown_count == 1
    assert chain.close_count == 1
    coordinator.stop()


@pytest.mark.parametrize("terminal", [False, True])
def test_n2_only_post_start_shutdown_publishes_accepted_recovery_job(terminal: bool) -> None:
    class ShutdownProjectionChain(_FakeChain):
        def shutdown_for_recovery(self) -> JsonObject:
            self.shutdown_count += 1
            self._revision += 1
            return {
                "schema_version": 1,
                "execution": {
                    "schema_version": 1,
                    "execution_id": "n2-execution",
                    "job_id": "job-1",
                    "revision": self._revision,
                    "phase": "RECOVERY_REQUIRED",
                },
            }

    published: list[str] = []
    chain = ShutdownProjectionChain(fatal_on_drive=terminal)
    coordinator = MachineCoordinator(
        chain_factory=lambda _epoch: chain,
        service_epoch=str(uuid4()),
        job_publisher=published.append,
    )
    coordinator.start()
    assert published == []
    if terminal:
        coordinator.submit(_command("n2-terminal"))
        owner = coordinator.owner_thread
        assert owner is not None
        owner.join(timeout=1.0)
    coordinator.stop()
    assert published == ["job-1"]


@pytest.mark.parametrize("terminal", ["normal", "callback", "projection"])
def test_n2_real_job_coordinator_rereads_sqlite_after_machine_shutdown(
    tmp_path: Path,
    terminal: str,
) -> None:
    database = tmp_path / "review-real-coordinator.db"
    job_coordinator = JobCoordinator(chain_factory=lambda: _DurableJobProjectionChain(database))  # type: ignore[arg-type]
    epoch = _uuid(2)
    factory = _strict_factory(epoch)
    machine, _database, wrapper = _real_coordinator_fixture(
        tmp_path,
        factory,
        job_publisher=job_coordinator.publish_external,
    )
    assert machine.owner_thread is not None and machine.owner_thread.is_alive()
    job_coordinator.start()
    assert job_coordinator.status("job-c9").job.state is JobState.READY_TO_RUN

    if terminal == "projection":
        wrapper.fail_projection_after_drive = True
        machine.submit(_real_prepare("n2-real-projection"))
    else:
        prepared = machine.submit(_real_prepare(f"n2-real-{terminal}"))
        execution_id = cast(str, prepared.response["execution_id"])
        assert _wait_for_phase(machine, MachinePhase.AWAITING_HOME_APPROVAL) == execution_id
        if terminal == "callback":
            requested = machine.submit(
                HomeMachineCommand(
                    "n2-real-home-request",
                    AuthenticatedMachinePrincipal(1100, 1100, 31020),
                    execution_id,
                    MachineCommandMode.REQUEST,
                    None,
                )
            )
            machine.submit(
                HomeMachineCommand(
                    "n2-real-home-execute",
                    AuthenticatedMachinePrincipal(2200, 2200, 41020),
                    execution_id,
                    MachineCommandMode.EXECUTE,
                    _challenge_id(requested),
                ),
                committed=lambda _result: (_ for _ in ()).throw(RuntimeError("n2 callback fault")),
            )
    owner = machine.owner_thread
    assert owner is not None
    if terminal != "normal":
        owner.join(timeout=2.0)
        assert not owner.is_alive()
    machine.stop()

    deadline = time.monotonic() + 2.0
    projection = job_coordinator.status("job-c9")
    while projection.job.state is not JobState.RECOVERY_REQUIRED and time.monotonic() < deadline:
        time.sleep(0.005)
        projection = job_coordinator.status("job-c9")
    assert projection.job.state is JobState.RECOVERY_REQUIRED
    assert projection.job.revision == 8
    factory.assert_script_consumed()
    job_coordinator.stop()


def _capture_error(action: object, errors: list[BaseException]) -> None:
    try:
        cast(Callable[[], object], action)()
    except BaseException as error:
        errors.append(error)


def _capture_result(
    action: Callable[[], MachineCommandResult],
    results: list[MachineCommandResult],
    errors: list[BaseException],
) -> None:
    try:
        results.append(action())
    except BaseException as error:
        errors.append(error)
