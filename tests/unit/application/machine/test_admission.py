from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import (
    MachineRuntimeAuthority,
    PrepareMachineCommand,
)
from drawingmachine.application.machine.execution import MachineCommandResult, MachineExecutionChain
from drawingmachine.config.machine import MachineExecutionConfig, MachinePrincipalConfig
from drawingmachine.domain.fluidnc.gates import (
    GateDisposition,
    SessionObservationPhase,
    classify_session_observation,
)
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot
from drawingmachine.domain.gcode import MachineBuildProfile
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.jobs import (
    ArtifactRef,
    ConfigSnapshot,
    JobKind,
    JobRecord,
    JobState,
    ReadyToRunSnapshot,
)
from drawingmachine.domain.machine import AuthenticatedMachinePrincipal, MachinePhase
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.ports.fluidnc import (
    CloseOutcome,
    CloseSessionRequest,
    FluidNCFailure,
    FluidNCFailureKind,
    InitialPreflightRequest,
    OpenSessionRequest,
    PreflightOutcome,
)
from drawingmachine.ports.machine_repository import MachineRequestKey

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)
AUTOMATION = AuthenticatedMachinePrincipal(1100, 1100, 31001)
APP_DIGEST = "a" * 64
MACHINE_DIGEST = "b" * 64
PROVIDER_DIGEST = "c" * 64
GCODE = Path("tests/fixtures/package_b/golden/expected/drawing.gcode").read_bytes()


def _uuid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _identities() -> Iterator[str]:
    index = 1
    while True:
        yield _uuid(index)
        index += 1


def _config() -> MachineExecutionConfig:
    return MachineExecutionConfig(
        Path("/config/machines/default.toml"),
        "FluidNC",
        "serial",
        "/dev/serial/by-id/fake-only",
        115200,
        "\n",
        2.0,
        15.0,
        1.0,
        10.0,
        300.0,
        30.0,
        60,
        True,
        (192.0, 192.0, 192.0),
        0.5,
        False,
        MachinePrincipalConfig(1100, 1100),
        MachinePrincipalConfig(2200, 2200),
        MachineBuildProfile.stage1_defaults(),
    )


def _authority(**changes: object) -> MachineRuntimeAuthority:
    values: dict[str, object] = {
        "service_epoch": _uuid(900),
        "application_version": "0.2.0.dev0",
        "application_schema_version": 1,
        "application_digest": APP_DIGEST,
        "machine_profile": "default",
        "machine_schema_version": 1,
        "machine_digest": MACHINE_DIGEST,
        "provider_profile": "local-comfyui",
        "provider_schema_version": 1,
        "provider_digest": PROVIDER_DIGEST,
    }
    values.update(changes)
    return MachineRuntimeAuthority(**values)  # type: ignore[arg-type]


def _ready_snapshot(job_id: str = "job-c9", **changes: object) -> ReadyToRunSnapshot:
    digest = hashlib.sha256(GCODE).hexdigest()
    checked = check_candidate(
        GCODE.decode(),
        profile=MachineBuildProfile.stage1_defaults(),
        expected_gcode_sha256=digest,
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "job_id": job_id,
        "job_revision": 7,
        "gcode": ArtifactRef("gcode", "artifacts/attempt/drawing.gcode", digest, len(GCODE), "text/x.gcode"),
        "artifacts": (),
        "static_result": checked.static_result.to_json(),
        "send_plan": checked.send_plan.to_json(),
        "readiness": checked.readiness.to_json(),
        "config": ConfigSnapshot(
            "default",
            "local-comfyui",
            1,
            1,
            1,
            APP_DIGEST,
            MACHINE_DIGEST,
            PROVIDER_DIGEST,
        ),
        "planning_parameters": {},
        "application_version": "0.2.0.dev0",
    }
    values.update(changes)
    return ReadyToRunSnapshot(**values)  # type: ignore[arg-type]


def _job(snapshot: ReadyToRunSnapshot, *, state: JobState = JobState.READY_TO_RUN) -> JobRecord:
    return JobRecord(
        1,
        snapshot.job_id,
        JobKind.OFFLINE_WORKFLOW,
        "C9 fixture",
        state,
        snapshot.job_revision,
        {},
        snapshot.config,
        None,
        None,
        snapshot,
        NOW,
        NOW,
    )


def _seed(repository: SQLiteMachineRepository, job: JobRecord) -> None:
    def document(value: object) -> str:
        return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))

    repository._get_connection().execute(
        """INSERT INTO jobs (
               job_id, schema_version, kind, name, state, revision, request_json,
               config_snapshot_json, blocker_json, error_json, ready_snapshot_json,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
        (
            job.job_id,
            job.schema_version,
            job.kind.value,
            job.name,
            job.state.value,
            job.revision,
            document({}),
            document(job.config.to_json()),
            document(cast(ReadyToRunSnapshot, job.ready_snapshot).to_json()),
            job.created_at.isoformat(),
            job.updated_at.isoformat(),
        ),
    )


class Reader:
    def __init__(self, data: bytes = GCODE) -> None:
        self.data = data
        self.calls = 0

    def read_bytes(self, job_id: str, artifact: ArtifactRef, *, expected_media_type: str, max_bytes: int) -> bytes:
        assert job_id == "job-c9"
        assert artifact.relative_path == "artifacts/attempt/drawing.gcode"
        assert expected_media_type == "text/x.gcode"
        assert max_bytes >= len(self.data)
        self.calls += 1
        return self.data


class ProbeSession:
    def __init__(
        self,
        preflight: object,
        close_result: object | BaseException,
    ) -> None:
        self.preflight = preflight
        self.close_result = close_result
        self.close_calls = 0

    def read_initial_preflight(self, request: InitialPreflightRequest) -> object:
        assert type(request) is InitialPreflightRequest
        return self.preflight

    def close(self, request: CloseSessionRequest) -> object:
        assert type(request) is CloseSessionRequest
        self.close_calls += 1
        if isinstance(self.close_result, BaseException):
            raise self.close_result
        return self.close_result


class ProbeFactory:
    def __init__(self, epoch: str, session: ProbeSession) -> None:
        self.epoch = epoch
        self.session = session
        self.opens = 0

    def open_session(self, request: OpenSessionRequest) -> ProbeSession:
        assert request == OpenSessionRequest(self.epoch)
        self.opens += 1
        return self.session


def _preflight(epoch: str, *, fresh_reset: bool = True) -> PreflightOutcome:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000|WPos:0.000,0.000,0.000|WCO:0.000,0.000,0.000>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=("[G54:0.000,0.000,0.000]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC startup reset",) if fresh_reset else ("startup settled",)
    decision = classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION)
    assert decision.disposition is GateDisposition.AWAIT_HOME_ONLY
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        SessionObservationPhase.FRESH_STABILIZATION,
        decision,
    )


def _alarm_preflight(epoch: str) -> PreflightOutcome:
    snapshot = parse_preflight_snapshot(
        status_line="<Alarm|MPos:1.000,1.000,1.000|WPos:1.000,1.000,1.000|WCO:0.000,0.000,0.000>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=("[G54:0.000,0.000,0.000]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("startup settled",)
    decision = classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION)
    assert decision.disposition is GateDisposition.READY
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        SessionObservationPhase.FRESH_STABILIZATION,
        decision,
    )


def _late_reset_preflight(epoch: str) -> PreflightOutcome:
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000|WPos:0.000,0.000,0.000|WCO:0.000,0.000,0.000>",
        parser_state=(),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC reset after stable status",)
    phase = SessionObservationPhase.STABILIZED
    decision = classify_session_observation(evidence, snapshot.status, phase)
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        phase,
        decision,
        FluidNCFailure(
            FluidNCFailureKind.CONTROLLER_RESET,
            "CONTROLLER_RESET_DETECTED",
            "FluidNC reset evidence invalidated the stabilized session",
            True,
        ),
    )


def _real_hardware_preflight(epoch: str) -> PreflightOutcome:
    # Defect #17: the real controller's live-captured preflight status line. Grbl/FluidNC status
    # reports carry MPos XOR WPos (never both), and WCO only periodically. This is the exact line
    # captured from the real controller today: MPos only, no WPos, with a non-zero WCO and a
    # matching G54 offset from `$#`.
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:22.000,24.000,79.700>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=("[G54:22.000,24.000,79.700]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC startup reset",)
    decision = classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION)
    assert decision.disposition is GateDisposition.AWAIT_HOME_ONLY
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        SessionObservationPhase.FRESH_STABILIZATION,
        decision,
    )


def _all_present_distinct_preflight(epoch: str) -> PreflightOutcome:
    # All three of MPos/WPos/WCO are present and deliberately inconsistent with each other
    # (WPos != MPos - WCO) so a test can prove the reported values are stored verbatim rather
    # than silently recomputed by the derivation helper.
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:1.000,2.000,3.000|WPos:9.000,9.000,9.000|WCO:4.000,5.000,6.000>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=("[G54:4.000,5.000,6.000]",),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC startup reset",)
    decision = classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION)
    assert decision.disposition is GateDisposition.AWAIT_HOME_ONLY
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        SessionObservationPhase.FRESH_STABILIZATION,
        decision,
    )


def _chain(
    tmp_path: Path,
    *,
    snapshot: ReadyToRunSnapshot | None = None,
    reader: Reader | None = None,
    alarm_preflight: bool = False,
    outcome_factory: Callable[[str], PreflightOutcome] | None = None,
    close_after: bool = False,
    authority: MachineRuntimeAuthority | None = None,
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, StrictFakeFluidNCFactory, Reader]:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(snapshot or _ready_snapshot()))
    identities = _identities()
    session_epoch = _uuid(2)
    steps = [
        FakeFluidNCStep(
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            InitialPreflightRequest(),
            (
                outcome_factory(session_epoch)
                if outcome_factory is not None
                else (_alarm_preflight(session_epoch) if alarm_preflight else _preflight(session_epoch))
            ),
        )
    ]
    if alarm_preflight or close_after:
        steps.append(
            FakeFluidNCStep(
                FakeFluidNCOperation.CLOSE,
                CloseSessionRequest(session_epoch),
                CloseOutcome(session_epoch),
            )
        )
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(session_epoch), tuple(steps))
    accepted_reader = reader or Reader()
    chain = MachineExecutionChain(
        repository,
        accepted_reader,
        factory,
        _config(),
        _authority() if authority is None else authority,
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    return chain, repository, factory, accepted_reader


def _command(**changes: object) -> PrepareMachineCommand:
    values: dict[str, object] = {
        "request_id": "request-c9-prepare",
        "requester": AUTOMATION,
        "job_id": "job-c9",
        "job_revision": 7,
    }
    values.update(changes)
    return PrepareMachineCommand(**values)  # type: ignore[arg-type]


def test_prepare_commits_before_callback_and_opens_once_only_after_drive(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path)
    admission = chain.admit_action(_command())
    try:
        execution_id = cast(object, admission).execution_id  # type: ignore[attr-defined]
        committed = repository.get_execution(execution_id)
        assert committed is not None and committed.phase is MachinePhase.PREPARING_SESSION
        assert reader.calls == 1
        assert factory.connections_opened == 0

        callback_observed: list[MachinePhase] = []
        callback_observed.append(cast(MachinePhase, committed.phase))
        assert callback_observed == [MachinePhase.PREPARING_SESSION]
        assert factory.connections_opened == 0

        result = chain.drive_admitted_action(admission)
        assert result.phase is MachinePhase.AWAITING_HOME_APPROVAL
        assert factory.connections_opened == 1
        assert factory.started_operations == (FakeFluidNCOperation.INITIAL_PREFLIGHT,)
        assert repository.get_session(result.execution_id) is not None
        with pytest.raises(DrawingMachineError, match="already claimed"):
            chain.drive_admitted_action(admission)
    finally:
        repository.close()


def test_prepare_reaches_awaiting_home_approval_with_real_hardware_mpos_only_status(tmp_path: Path) -> None:
    # Defect #17 regression (today's live failure): the real controller's status report carries
    # MPos only (no WPos), with WCO periodic. `_machine_session_snapshot` used to require
    # status.mpos, status.wpos, AND status.wco all non-None from a single status line, which is
    # structurally impossible on real hardware. That made `_commit_preflight`'s `safe` conjunction
    # fail, latching a freshly opened session into RECOVERY_REQUIRED with PREFLIGHT_REJECTED
    # instead of parking it at AWAITING_HOME_APPROVAL. WPos must be derivable from MPos - WCO.
    chain, repository, factory, _reader = _chain(tmp_path, outcome_factory=_real_hardware_preflight)
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.AWAITING_HOME_APPROVAL
        assert factory.connections_opened == 1
        session = repository.get_session(result.execution_id)
        assert session is not None
        assert session.mpos == (0.0, 0.0, 0.0)
        assert session.wco == (22.0, 24.0, 79.7)
        assert session.wpos == (-22.0, -24.0, -79.7)
        assert session.g54 == (22.0, 24.0, 79.7)
    finally:
        repository.close()


def test_prepare_stores_all_present_positions_verbatim_without_derivation_overwrite(tmp_path: Path) -> None:
    # When MPos, WPos, and WCO are all present in the status report, the reported values must be
    # stored exactly as observed -- the derivation helper must never recompute or overwrite them,
    # even when they are (as here) not mutually consistent.
    chain, repository, _factory, _reader = _chain(tmp_path, outcome_factory=_all_present_distinct_preflight)
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.AWAITING_HOME_APPROVAL
        session = repository.get_session(result.execution_id)
        assert session is not None
        assert session.mpos == (1.0, 2.0, 3.0)
        assert session.wpos == (9.0, 9.0, 9.0)
        assert session.wco == (4.0, 5.0, 6.0)
        assert session.g54 == (4.0, 5.0, 6.0)
    finally:
        repository.close()


def test_late_reset_initial_preflight_is_durable_recovery_never_home_eligible(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(
        tmp_path,
        outcome_factory=_late_reset_preflight,
        close_after=True,
    )
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence is not None
        assert result.recovery_evidence.failure.code == "CONTROLLER_RESET_DETECTED"
        assert result.machine_session_epoch == factory.session_epoch
        assert factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.CLOSE,
        )
    finally:
        repository.close()


def test_wrong_epoch_preflight_is_never_accepted_and_close_uncertainty_is_retained(
    tmp_path: Path,
) -> None:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    identities = _identities()
    expected_epoch = _uuid(2)
    wrong_epoch = _uuid(900)
    session = ProbeSession(
        _preflight(wrong_epoch),
        CloseOutcome(wrong_epoch),
    )
    factory = ProbeFactory(expected_epoch, session)
    chain = MachineExecutionChain(
        repository,
        Reader(),
        factory,  # type: ignore[arg-type]
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    try:
        admitted = chain.admit_action(_command())
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert repository.get_session(result.execution_id) is None
        assert session.close_calls == 1
        assert chain.retained_session_epochs() == (expected_epoch,)
        assert repository._get_connection().execute(
            "SELECT COUNT(*) FROM audit_events WHERE payload_json LIKE '%SESSION_CLOSE_EPOCH_MISMATCH%'"
        ).fetchone() == (1,)
        assert chain.release_retained_sessions_for_shutdown() == (expected_epoch,)
        assert chain.retained_session_epochs() == ()
        assert session.close_calls == 1
    finally:
        repository.close()


def test_wrong_type_preflight_is_durable_recovery_and_exact_close_is_accepted(tmp_path: Path) -> None:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    identities = _identities()
    epoch = _uuid(2)
    session = ProbeSession(object(), CloseOutcome(epoch))
    factory = ProbeFactory(epoch, session)
    chain = MachineExecutionChain(
        repository,
        Reader(),
        factory,  # type: ignore[arg-type]
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence is not None
        assert result.recovery_evidence.failure.code == "SESSION_PREFLIGHT_OUTCOME_INVALID"
        assert session.close_calls == 1
        assert chain.retained_session_epochs() == ()
    finally:
        repository.close()


def test_retained_session_race_after_admission_blocks_open_and_commits_recovery(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(tmp_path)
    admitted = chain.admit_action(_command())
    epoch = _uuid(999)
    chain._retained_sessions[epoch] = ProbeSession(object(), object())  # type: ignore[attr-defined]
    try:
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_evidence is not None
        assert result.recovery_evidence.failure.code == "SESSION_OWNERSHIP_UNCERTAIN"
        assert factory.connections_opened == 0
        assert chain.retained_session_epochs() == (epoch,)
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("close_result", "expected_code"),
    [
        (
            CloseOutcome(
                _uuid(2),
                FluidNCFailure(
                    FluidNCFailureKind.CLOSE_ERROR,
                    "SCRIPTED_CLOSE_FAILURE",
                    "scripted close failure",
                    False,
                ),
            ),
            "SCRIPTED_CLOSE_FAILURE",
        ),
        (RuntimeError("secret close detail"), "SESSION_CLOSE_EXCEPTION"),
        (object(), "SESSION_CLOSE_OUTCOME_INVALID"),
    ],
)
def test_close_failure_exception_and_wrong_type_are_durable_and_never_retried(
    tmp_path: Path,
    close_result: object,
    expected_code: str,
) -> None:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    identities = _identities()
    epoch = _uuid(2)
    session = ProbeSession(_alarm_preflight(epoch), close_result)  # type: ignore[arg-type]
    factory = ProbeFactory(epoch, session)
    chain = MachineExecutionChain(
        repository,
        Reader(),
        factory,  # type: ignore[arg-type]
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert chain.retained_session_epochs() == (epoch,)
        assert session.close_calls == 1
        assert repository._get_connection().execute(
            "SELECT COUNT(*) FROM audit_events WHERE payload_json LIKE ?",
            (f"%{expected_code}%",),
        ).fetchone() == (1,)
        chain.release_retained_sessions_for_shutdown()
        assert session.close_calls == 1
    finally:
        repository.close()


def test_withdrawn_prepare_creates_no_execution_reads_no_artifact_and_opens_nothing(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path)
    try:
        with pytest.raises(DrawingMachineError, match="withdrawn"):
            chain.admit_action(_command(), still_requested=lambda: False)
        assert repository.get_active_execution() is None
        assert reader.calls == 0
        assert factory.connections_opened == 0
    finally:
        repository.close()


def test_withdrawal_after_validation_but_before_commit_creates_no_authority_or_open(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path)
    checks = iter((True, False))
    try:
        with pytest.raises(DrawingMachineError, match="withdrawn"):
            chain.admit_action(_command(), still_requested=lambda: next(checks))
        assert reader.calls == 1
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
    finally:
        repository.close()


@pytest.mark.parametrize(
    "command",
    [
        _command(job_revision=8),
    ],
)
def test_frozen_job_revision_profile_digest_and_version_drift_fail_before_open(
    tmp_path: Path,
    command: PrepareMachineCommand,
) -> None:
    chain, repository, factory, _ = _chain(tmp_path)
    try:
        with pytest.raises(DrawingMachineError):
            chain.admit_action(command)
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
    finally:
        repository.close()


@pytest.mark.parametrize(
    "authority",
    [
        _authority(application_digest="f" * 64),
        _authority(machine_profile="replacement"),
        _authority(provider_digest="e" * 64),
        _authority(application_version="replacement"),
    ],
)
def test_trusted_runtime_authority_drift_fails_before_open(
    tmp_path: Path,
    authority: MachineRuntimeAuthority,
) -> None:
    chain, repository, factory, _ = _chain(tmp_path, authority=authority)
    try:
        with pytest.raises(DrawingMachineError):
            chain.admit_action(_command())
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
    finally:
        repository.close()


def test_descriptor_identity_replacement_fails_before_reservation_or_open(tmp_path: Path) -> None:
    reader = Reader(GCODE + b"; replaced\n")
    chain, repository, factory, _ = _chain(tmp_path, reader=reader)
    try:
        with pytest.raises(DrawingMachineError, match="size changed"):
            chain.admit_action(_command())
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
    finally:
        repository.close()


def test_static_send_plan_and_readiness_tamper_are_recomputed_exactly(tmp_path: Path) -> None:
    original = _ready_snapshot()
    documents = original.to_json()
    cases = []
    static = cast(dict[str, object], documents["static_result"])
    static["summary"] = {**cast(dict[str, object], static["summary"]), "draw_segment_count": 999}
    cases.append(replace(original, static_result=static))
    send_plan = cast(dict[str, object], documents["send_plan"])
    send_plan["estimated_serial_bytes"] = cast(int, send_plan["estimated_serial_bytes"]) + 1
    cases.append(replace(original, send_plan=send_plan))
    readiness = cast(dict[str, object], documents["readiness"])
    readiness["allow_stream"] = False
    cases.append(replace(original, readiness=readiness))

    for index, snapshot in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        chain, repository, factory, _ = _chain(root, snapshot=snapshot)
        try:
            with pytest.raises(DrawingMachineError):
                chain.admit_action(_command())
            assert repository.get_active_execution() is None
            assert factory.connections_opened == 0
        finally:
            repository.close()


def test_request_id_replay_is_durable_and_conflict_fails_closed(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(tmp_path)
    try:
        first = chain.admit_action(_command())
        chain.drive_admitted_action(first)
        replay = chain.admit_action(replace(_command(), requester=AuthenticatedMachinePrincipal(1100, 1100, 31999)))
        assert cast(object, replay).response["deduplicated"] is True  # type: ignore[attr-defined]
        with pytest.raises(DrawingMachineError, match="different arguments"):
            chain.admit_action(_command(job_revision=8))
        assert factory.connections_opened == 1
    finally:
        repository.close()


def test_alarm_preflight_closes_session_and_atomically_marks_machine_and_job_recovery(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(tmp_path, alarm_preflight=True)
    try:
        admission = chain.admit_action(_command())
        result = chain.drive_admitted_action(admission)
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert result.recovery_intent is not None
        assert factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.CLOSE,
        )
        persisted_job = (
            repository._get_connection()
            .execute("SELECT state, revision, ready_snapshot_json FROM jobs WHERE job_id = 'job-c9'")
            .fetchone()
        )
        assert persisted_job is not None
        assert persisted_job[0:2] == (JobState.RECOVERY_REQUIRED.value, 8)
        assert persisted_job[2] is not None
        assert repository.get_session(result.execution_id) is not None
    finally:
        repository.close()


def test_second_prepare_owner_conflict_leaves_only_first_durable_execution_and_zero_open(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(tmp_path)
    try:
        first = chain.admit_action(_command())
        with pytest.raises(DrawingMachineError, match="safety latch"):
            chain.admit_action(_command(request_id="request-c9-second"))
        assert repository.get_active_execution() is not None
        assert factory.connections_opened == 0
        assert cast(object, first).response["admitted"] is True  # type: ignore[attr-defined]
    finally:
        repository.close()


def test_cancel_committed_after_artifact_read_wins_before_prepare_reservation(tmp_path: Path) -> None:
    class CancelReader(Reader):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

        def read_bytes(
            self,
            job_id: str,
            artifact: ArtifactRef,
            *,
            expected_media_type: str,
            max_bytes: int,
        ) -> bytes:
            data = super().read_bytes(
                job_id,
                artifact,
                expected_media_type=expected_media_type,
                max_bytes=max_bytes,
            )
            connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                connection.execute(
                    "UPDATE jobs SET state = 'CANCELLED', revision = 8, updated_at = ? WHERE job_id = ?",
                    ((NOW + timedelta(seconds=1)).isoformat(), job_id),
                )
            finally:
                connection.close()
            return data

    database = tmp_path / "machine.db"
    reader = CancelReader(database)
    chain, repository, factory, _ = _chain(tmp_path, reader=reader)
    try:
        with pytest.raises(DrawingMachineError, match="READY_TO_RUN authority"):
            chain.admit_action(_command())
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
        assert repository._get_connection().execute("SELECT state FROM jobs").fetchone() == ("CANCELLED",)
    finally:
        repository.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"application_schema_version": 2},
        {"machine_schema_version": True},
        {"provider_schema_version": 0},
        {"service_epoch": "not-a-uuid"},
        {"service_epoch": "00000000-0000-1000-8000-000000000001"},
        {"application_digest": "A" * 64},
        {"machine_digest": "short"},
        {"provider_digest": object()},
    ],
)
def test_runtime_authority_rejects_schema_and_digest_drift(changes: dict[str, object]) -> None:
    with pytest.raises(DrawingMachineError):
        _authority(**changes)


@pytest.mark.parametrize("value", ["", "x" * 4097, "bad\ntext", "\ud800"])
def test_prepare_rejects_bounded_text_failures(value: str) -> None:
    with pytest.raises(DrawingMachineError):
        _command(request_id=value)


def test_prepare_rejects_invalid_revision_requester_and_authority_types() -> None:
    for changes in (
        {"job_revision": True},
        {"job_revision": 0},
        {"requester": object()},
    ):
        with pytest.raises(DrawingMachineError):
            _command(**changes)


def test_result_and_internal_admission_defensive_contracts(tmp_path: Path) -> None:
    import drawingmachine.application.machine.execution as execution_module

    with pytest.raises(DrawingMachineError):
        MachineCommandResult(cast(dict[str, object], object()), False)
    with pytest.raises(DrawingMachineError):
        MachineCommandResult({}, cast(bool, 1))

    chain, repository, _, _ = _chain(tmp_path)
    try:
        admitted = chain.admit_action(_command())
        values = {
            "capability": admitted.capability,  # type: ignore[attr-defined]
            "program": admitted.program,  # type: ignore[attr-defined]
            "ready_snapshot": admitted.ready_snapshot,  # type: ignore[attr-defined]
            "response": admitted.response,  # type: ignore[attr-defined]
        }
        for field in values:
            invalid = dict(values)
            invalid[field] = object()
            with pytest.raises(DrawingMachineError):
                execution_module._AdmittedMachineAction(**invalid)
        with pytest.raises(DrawingMachineError, match="internal admitted"):
            chain.drive_admitted_action(MachineCommandResult({}, False))
    finally:
        repository.close()


def test_chain_rejects_non_config_and_unsupported_command(tmp_path: Path) -> None:
    chain, repository, factory, reader = _chain(tmp_path)
    try:
        with pytest.raises(DrawingMachineError, match="unsupported machine command"):
            chain.admit_action(cast(PrepareMachineCommand, object()))
        with pytest.raises(DrawingMachineError, match="exact validated"):
            MachineExecutionChain(
                repository,
                reader,
                factory,
                cast(MachineExecutionConfig, object()),
                _authority(),
            )
        with pytest.raises(DrawingMachineError, match="runtime authority"):
            MachineExecutionChain(repository, reader, factory, _config(), cast(MachineRuntimeAuthority, object()))
    finally:
        repository.close()


def test_command_dtos_cannot_carry_runtime_digest_epoch_or_profile_authority() -> None:
    from drawingmachine.application.machine.commands import RecoverMachineCommand

    forbidden = {
        "authority",
        "application_digest",
        "machine_digest",
        "provider_digest",
        "service_epoch",
        "machine_session_epoch",
        "machine_profile",
        "provider_profile",
    }
    assert forbidden.isdisjoint(field.name for field in fields(PrepareMachineCommand))
    assert forbidden.isdisjoint(field.name for field in fields(RecoverMachineCommand))


def _missing_position_preflight(epoch: str) -> PreflightOutcome:
    # Defect #17: MPos alone (with an observed G54) is now derivable -- WPos = MPos - G54 and WCO
    # falls back to G54 -- so it is no longer "incomplete" evidence. To keep exercising the
    # genuinely-incomplete recovery path, this omits G54 entirely (no offset_lines), so neither
    # WCO nor WPos can be derived from MPos alone.
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0.000,0.000,0.000>",
        parser_state=("[GC:G0 G54 G17 G21 G90]",),
        offset_lines=(),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = ("FluidNC reset",)
    return PreflightOutcome(
        epoch,
        snapshot,
        evidence,
        SessionObservationPhase.FRESH_STABILIZATION,
        classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION),
    )


def _failed_preflight(epoch: str) -> PreflightOutcome:
    base = _preflight(epoch)
    return PreflightOutcome(
        epoch,
        base.snapshot,
        base.evidence,
        base.observation_phase,
        base.decision,
        FluidNCFailure(
            FluidNCFailureKind.TIMEOUT,
            "PREFLIGHT_TIMEOUT",
            "bounded initial preflight timed out",
            False,
        ),
    )


@pytest.mark.parametrize("outcome_factory", [_missing_position_preflight, _failed_preflight])
def test_incomplete_or_typed_failed_preflight_enters_recovery_and_closes(
    tmp_path: Path,
    outcome_factory: Callable[[str], PreflightOutcome],
) -> None:
    chain, repository, factory, _ = _chain(
        tmp_path,
        outcome_factory=outcome_factory,
        close_after=True,
    )
    try:
        result = chain.drive_admitted_action(chain.admit_action(_command()))
        assert result.phase is MachinePhase.RECOVERY_REQUIRED
        assert factory.started_operations[-1] is FakeFluidNCOperation.CLOSE
    finally:
        repository.close()


def test_open_and_preflight_exceptions_are_sanitized_to_recovery_with_correct_close_boundary(tmp_path: Path) -> None:
    class RaisingSession:
        def __init__(self, *, close_raises: bool) -> None:
            self.close_raises = close_raises
            self.close_calls = 0

        def read_initial_preflight(self, request: InitialPreflightRequest) -> PreflightOutcome:
            del request
            raise RuntimeError("hidden fake preflight failure")

        def close(self, request: CloseSessionRequest) -> CloseOutcome:
            del request
            self.close_calls += 1
            if self.close_raises:
                raise RuntimeError("hidden fake close failure")
            return CloseOutcome(_uuid(2))

    class Factory:
        def __init__(self, session: RaisingSession | None) -> None:
            self.session = session
            self.opens = 0

        def open_session(self, request: OpenSessionRequest):
            del request
            self.opens += 1
            if self.session is None:
                raise RuntimeError("hidden fake open failure")
            return self.session

    for index, session in enumerate((None, RaisingSession(close_raises=True))):
        root = tmp_path / str(index)
        root.mkdir()
        repository = SQLiteMachineRepository(root / "machine.db")
        repository.initialize(applied_at=NOW.isoformat())
        _seed(repository, _job(_ready_snapshot()))
        reader = Reader()
        factory = Factory(session)
        identities = _identities()
        chain = MachineExecutionChain(
            repository,
            reader,
            factory,
            _config(),
            _authority(),
            identity_factory=lambda identities=identities: next(identities),
            wall_now=lambda: NOW + timedelta(seconds=1),
            monotonic_now=lambda: 1000.0,
        )
        try:
            result = chain.drive_admitted_action(chain.admit_action(_command()))
            assert result.phase is MachinePhase.RECOVERY_REQUIRED
            assert factory.opens == 1
            if session is not None:
                assert session.close_calls == 1
        finally:
            repository.close()


def test_status_projection_is_deterministic_for_null_active_and_explicit_selector(tmp_path: Path) -> None:
    chain, repository, _, _ = _chain(tmp_path)
    try:
        empty = {"schema_version": 1, "execution": None, "progress": None, "last_outcome": None}
        assert chain.status_projection() == empty
        admission = chain.admit_action(_command())
        active = chain.status_projection()
        assert cast(dict[str, object], active["execution"])["phase"] == MachinePhase.PREPARING_SESSION.value
        execution_id = cast(str, cast(dict[str, object], active["execution"])["execution_id"])
        assert chain.status_projection(execution_id) == active
        assert chain.status_projection("missing") == empty
        assert cast(object, admission).execution_id == execution_id  # type: ignore[attr-defined]
        published: list[JsonObject] = []
        chain.install_projection_publisher(published.append)
        chain._publish_observation(execution_id)
        assert published == [active]
        with pytest.raises(DrawingMachineError, match="installed exactly once"):
            chain.install_projection_publisher(published.append)
    finally:
        repository.close()


def test_same_size_digest_replacement_and_strict_utf8_failure_are_distinct(tmp_path: Path) -> None:
    replaced = bytearray(GCODE)
    replaced[-2] = 88 if replaced[-2] != 88 else 89
    chain, repository, factory, _ = _chain(tmp_path, reader=Reader(bytes(replaced)))
    try:
        with pytest.raises(DrawingMachineError, match="digest changed"):
            chain.admit_action(_command())
        assert factory.connections_opened == 0
    finally:
        repository.close()

    root = tmp_path / "utf8"
    root.mkdir()
    bad = b"\xff"
    original = _ready_snapshot()
    snapshot = replace(
        original,
        gcode=ArtifactRef(
            "gcode",
            original.gcode.relative_path,
            hashlib.sha256(bad).hexdigest(),
            len(bad),
            original.gcode.media_type,
        ),
    )
    chain, repository, factory, _ = _chain(root, snapshot=snapshot, reader=Reader(bad))
    try:
        with pytest.raises(DrawingMachineError, match="strict UTF-8"):
            chain.admit_action(_command())
        assert factory.connections_opened == 0
    finally:
        repository.close()


def test_unknown_job_unconfigured_requester_invalid_clock_and_identity_fail_closed(tmp_path: Path) -> None:
    chain, repository, factory, _ = _chain(tmp_path)
    try:
        with pytest.raises(DrawingMachineError, match="configured machine principal"):
            chain.admit_action(_command(requester=AuthenticatedMachinePrincipal(3300, 3300, 1)))
        repository._get_connection().execute("DELETE FROM jobs")
        with pytest.raises(DrawingMachineError, match="does not exist"):
            chain.admit_action(_command())
        assert factory.connections_opened == 0
    finally:
        repository.close()

    for index, wall, identity in (
        (1, lambda: datetime(2026, 7, 12), lambda: _uuid(1)),
        (2, lambda: NOW, lambda: ""),
        (3, lambda: NOW, lambda: "not-a-uuid"),
        (4, lambda: NOW, lambda: "00000000-0000-1000-8000-000000000001"),
    ):
        root = tmp_path / f"invalid-{index}"
        root.mkdir()
        repository = SQLiteMachineRepository(root / "machine.db")
        repository.initialize(applied_at=NOW.isoformat())
        _seed(repository, _job(_ready_snapshot()))
        reader = Reader()
        factory = StrictFakeFluidNCFactory(OpenSessionRequest(_uuid(2)), ())
        invalid_chain = MachineExecutionChain(
            repository,
            reader,
            factory,
            _config(),
            _authority(),
            identity_factory=identity,
            wall_now=wall,
        )
        try:
            with pytest.raises(DrawingMachineError):
                invalid_chain.admit_action(_command())
            assert factory.connections_opened == 0
        finally:
            repository.close()


class _RepositoryProxy:
    def __init__(self, delegate: SQLiteMachineRepository) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


def _proxy_chain(
    repository: object,
    reader: Reader,
    factory: object,
) -> MachineExecutionChain:
    identities = _identities()
    return MachineExecutionChain(
        repository,  # type: ignore[arg-type]
        reader,
        factory,  # type: ignore[arg-type]
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )


def test_prepare_transaction_race_replays_existing_durable_admission(tmp_path: Path) -> None:
    class MaskOuterRequest(_RepositoryProxy):
        def get_request(self, key: MachineRequestKey):
            del key
            return None

    chain, repository, factory, reader = _chain(tmp_path)
    try:
        chain.admit_action(_command())
        replay_chain = _proxy_chain(MaskOuterRequest(repository), reader, factory)
        replay = replay_chain.admit_action(_command())
        assert cast(object, replay).response["deduplicated"] is True  # type: ignore[attr-defined]
        assert factory.connections_opened == 0
    finally:
        repository.close()


def test_prepare_transaction_rechecks_job_and_rejects_deleted_authority(tmp_path: Path) -> None:
    class MaskSecondTransaction(_RepositoryProxy):
        def __init__(self, delegate: SQLiteMachineRepository) -> None:
            super().__init__(delegate)
            self.calls = 0

        @contextmanager
        def transaction(self):
            self.calls += 1
            with self.delegate.transaction() as transaction:
                if self.calls == 2:

                    class Proxy:
                        def __getattr__(self, name: str):
                            return getattr(transaction, name)

                        def get_job(self, job_id: str):
                            del job_id
                            return None

                    yield Proxy()
                else:
                    yield transaction

    _chain_value, repository, factory, reader = _chain(tmp_path)
    try:
        masked = _proxy_chain(MaskSecondTransaction(repository), reader, factory)
        with pytest.raises(DrawingMachineError, match="does not exist"):
            masked.admit_action(_command())
        assert repository.get_active_execution() is None
    finally:
        repository.close()


def test_incomplete_or_missing_replay_authority_fails_closed(tmp_path: Path) -> None:
    import drawingmachine.application.machine.execution as execution_module

    class MissingExecution(_RepositoryProxy):
        def get_execution(self, execution_id: str):
            del execution_id
            return None

    chain, repository, factory, reader = _chain(tmp_path)
    command = _command()
    try:
        chain.admit_action(command)
        key = MachineRequestKey(AUTOMATION.uid, AUTOMATION.gid, command.request_id)
        record = repository.get_request(key)
        assert record is not None
        with pytest.raises(DrawingMachineError, match="no completed response"):
            chain._replay(
                replace(record, response=None),
                command,
                execution_module._argument_digest(command),
            )
        missing_chain = _proxy_chain(MissingExecution(repository), reader, factory)
        with pytest.raises(DrawingMachineError, match="execution is missing"):
            missing_chain.admit_action(command)
    finally:
        repository.close()


def test_stale_preflight_and_nonpreparing_exception_paths_never_continue(tmp_path: Path) -> None:
    class MissingExecution(_RepositoryProxy):
        def get_execution(self, execution_id: str):
            del execution_id
            return None

    _chain_value, repository, factory, reader = _chain(tmp_path)
    try:
        stale_chain = _proxy_chain(MissingExecution(repository), reader, factory)
        admission = stale_chain.admit_action(_command())
        with pytest.raises(DrawingMachineError, match="authority changed"):
            stale_chain.drive_admitted_action(admission)
    finally:
        repository.close()

    class RaisingFactory:
        def open_session(self, request: OpenSessionRequest):
            del request
            raise RuntimeError("hidden open failure")

    class NonPreparing(_RepositoryProxy):
        def get_execution(self, execution_id: str):
            current = self.delegate.get_execution(execution_id)
            assert current is not None
            return replace(current, phase=MachinePhase.AWAITING_HOME_APPROVAL)

    root = tmp_path / "nonpreparing"
    root.mkdir()
    repository = SQLiteMachineRepository(root / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot()))
    reader = Reader()
    exceptional = _proxy_chain(NonPreparing(repository), reader, RaisingFactory())
    try:
        admission = exceptional.admit_action(_command())
        with pytest.raises(RuntimeError, match="hidden open failure"):
            exceptional.drive_admitted_action(admission)
    finally:
        repository.close()
