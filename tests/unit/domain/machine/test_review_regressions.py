from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import pytest

from drawingmachine.domain.jobs.models import (
    ArtifactRef,
    ConfigSnapshot,
    MachineJobOutcomeTransition,
    ReadyToRunSnapshot,
)
from drawingmachine.domain.jobs.states import JobState, allowed_transition
from drawingmachine.domain.machine.events import (
    MachineAuditPayloadV1,
    MachineEvent,
    MachineEventPayloadV1,
)
from drawingmachine.domain.machine.models import (
    MachineExecution,
    MachineFailureEvidenceV1,
    MachineOutcome,
    MachineOutcomeKind,
    MachineRecoveryEvidence,
    OwnerRetirementEvidence,
    RetirementDisposition,
    StreamMilestone,
    ensure_revision_progression,
)
from drawingmachine.domain.machine.phases import MachinePhase, RecoveryIntent
from drawingmachine.domain.machine.progress import MachineProgress
from drawingmachine.errors import DrawingMachineError

NOW = datetime(2026, 7, 12, 15, tzinfo=UTC)
DIGEST = "a" * 64


def failure(*, side_effect_possible: bool = True) -> MachineFailureEvidenceV1:
    return MachineFailureEvidenceV1(
        schema_version=1,
        code="SESSION_LOST",
        message="typed failure evidence",
        controller_state="Idle",
        side_effect_possible=side_effect_possible,
    )


def execution(**changes: Any) -> MachineExecution:
    values: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": "execution-1",
        "job_id": "job-1",
        "ready_revision": 7,
        "revision": 0,
        "phase": MachinePhase.AWAITING_HOME_APPROVAL,
        "service_epoch": "service-1",
        "machine_session_epoch": "session-1",
        "gcode_sha256": DIGEST,
        "application_version": "2.0.0",
        "application_digest": DIGEST,
        "machine_digest": DIGEST,
        "provider_digest": DIGEST,
        "stream_milestone": StreamMilestone.NOT_STARTED,
        "recovery_intent": RecoveryIntent.PRE_STREAM_RESTART,
        "predecessor_execution_id": "execution-0",
        "recovery_evidence": None,
        "retirement": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return MachineExecution(**values)


@pytest.mark.parametrize(
    "forbidden_phase",
    [MachinePhase.AWAITING_HOME_Z_APPROVAL, MachinePhase.HOMING_Z],
)
def test_post_stream_safe_home_successor_excludes_every_home_z_phase(
    forbidden_phase: MachinePhase,
) -> None:
    with pytest.raises(DrawingMachineError, match="safe-home subgraph"):
        execution(
            phase=forbidden_phase,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        )


def test_successor_recovery_intent_is_frozen_while_milestone_advances() -> None:
    admitted = execution(phase=MachinePhase.STREAMING)
    first_write = replace(
        admitted,
        revision=1,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
    )
    ensure_revision_progression(admitted, first_write)
    completed = replace(
        first_write,
        revision=2,
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence(
            first_write.execution_id,
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    ensure_revision_progression(first_write, completed)
    assert completed.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART

    mutated = replace(completed, revision=3, recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME)
    with pytest.raises(DrawingMachineError, match="frozen"):
        ensure_revision_progression(completed, mutated)


@pytest.mark.parametrize("phase", [MachinePhase.AWAITING_HOME_APPROVAL, MachinePhase.HOMING])
def test_c15_initial_confirmed_execution_cannot_enter_post_stream_safe_home_graph(
    phase: MachinePhase,
) -> None:
    with pytest.raises(DrawingMachineError, match="POST_STREAM_SAFE_HOME"):
        execution(
            phase=phase,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            recovery_intent=None,
            predecessor_execution_id=None,
        )


def test_c15_confirmed_completion_progression_requires_completed_retirement() -> None:
    first_write = execution(
        phase=MachinePhase.STREAMING,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        recovery_intent=None,
        predecessor_execution_id=None,
    )
    completed_without_retirement = replace(
        first_write,
        revision=1,
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence(
            first_write.execution_id,
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    object.__setattr__(completed_without_retirement, "retirement", None)
    with pytest.raises(DrawingMachineError, match="retirement"):
        ensure_revision_progression(first_write, completed_without_retirement)

    completed = replace(
        completed_without_retirement,
        retirement=OwnerRetirementEvidence(
            first_write.execution_id,
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    ensure_revision_progression(first_write, completed)


def test_c15_completed_aggregate_construction_requires_completed_retirement() -> None:
    with pytest.raises(DrawingMachineError, match=r"COMPLETED.*retirement"):
        execution(
            phase=MachinePhase.COMPLETED,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            retirement=None,
        )

    completed = execution(
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence(
            "execution-1",
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    assert completed.retirement is not None
    assert completed.retirement.disposition is RetirementDisposition.COMPLETED


def test_successor_later_failure_derivation_is_separate_from_frozen_binding() -> None:
    evidence = MachineRecoveryEvidence(
        schema_version=1,
        execution_id="execution-1",
        recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        reason_code="STREAM_UNCERTAIN",
        failure=failure(),
        occurred_at=NOW,
    )
    recovered = execution(
        phase=MachinePhase.RECOVERY_REQUIRED,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        recovery_evidence=evidence,
    )
    assert recovered.recovery_intent is RecoveryIntent.PRE_STREAM_RESTART
    assert recovered.recovery_evidence is not None
    assert recovered.recovery_evidence.recovery_intent is RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY


AUTHORITY_ALIASES = [
    "raw_command",
    "raw-write",
    "sendCommand",
    "jog_vector",
    "unlock_alarm",
    "work_offset_update",
    "serialTransport",
    "resumeFromCheckpoint",
]


@pytest.mark.parametrize("unknown_key", AUTHORITY_ALIASES)
@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (
            MachineEventPayloadV1.from_json,
            {
                "schema_version": 1,
                "request_id": None,
                "prior_phase": "PREPARING_SESSION",
                "reason_code": "PREPARED",
            },
        ),
        (
            MachineAuditPayloadV1.from_json,
            {
                "schema_version": 1,
                "operation": "PREPARE",
                "decision": "ADMITTED",
                "execution_id": "execution-1",
                "job_id": "job-1",
                "execution_revision": 1,
                "request_id": None,
                "reason_code": "POLICY_OK",
                "event_id": "event-1",
            },
        ),
        (
            MachineFailureEvidenceV1.from_json,
            {
                "schema_version": 1,
                "code": "SESSION_LOST",
                "message": "failure",
                "controller_state": "Idle",
                "side_effect_possible": True,
            },
        ),
        (
            MachineRecoveryEvidence.from_json,
            {
                "schema_version": 1,
                "execution_id": "execution-1",
                "recovery_intent": "PRE_STREAM_RESTART",
                "stream_milestone": "NOT_STARTED",
                "reason_code": "SESSION_LOST",
                "failure": {
                    "schema_version": 1,
                    "code": "SESSION_LOST",
                    "message": "failure",
                    "controller_state": "Idle",
                    "side_effect_possible": True,
                },
                "occurred_at": NOW.isoformat(),
            },
        ),
    ],
)
def test_closed_machine_schemas_reject_every_unknown_authority_alias(
    factory: Any,
    payload: dict[str, object],
    unknown_key: str,
) -> None:
    hostile = {**payload, unknown_key: "unsafe"}
    with pytest.raises(DrawingMachineError, match="unknown or missing"):
        factory(hostile)


def test_typed_safe_values_allow_incidental_authority_substrings() -> None:
    event = MachineEventPayloadV1(
        schema_version=1,
        request_id="raw_command_digest_check",
        prior_phase=MachinePhase.PREPARING_SESSION,
        reason_code="serial_portable_report_written",
    )
    audit = MachineAuditPayloadV1(
        schema_version=1,
        operation="resume_reason_recorded",
        decision="DENIED",
        execution_id="execution-1",
        job_id="job-1",
        execution_revision=1,
        request_id=None,
        reason_code="unlock_attempt_denied",
        event_id="event-1",
    )
    assert event.reason_code == "serial_portable_report_written"
    assert audit.reason_code == "unlock_attempt_denied"


class OneShotEventMapping(dict[str, object]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def items(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return {
                "schema_version": 1,
                "request_id": None,
                "prior_phase": "PREPARING_SESSION",
                "reason_code": "SAFE",
            }.items()
        return {"raw_write": "G0 X1"}.items()


def test_closed_schema_snapshots_hostile_mapping_once() -> None:
    hostile = OneShotEventMapping()
    payload = MachineEventPayloadV1.from_json(hostile)
    assert payload.reason_code == "SAFE"
    assert hostile.calls == 1


def test_closed_schema_branch_matrix_and_round_trips() -> None:
    failure_value = failure(side_effect_possible=False)
    assert MachineFailureEvidenceV1.from_json(failure_value.to_json()) == failure_value
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(failure_value, schema_version=2)
    with pytest.raises(DrawingMachineError, match="boolean"):
        replace(failure_value, side_effect_possible=1)  # type: ignore[arg-type]
    failure_json = failure_value.to_json()
    failure_json["side_effect_possible"] = 1
    with pytest.raises(DrawingMachineError, match="boolean"):
        MachineFailureEvidenceV1.from_json(failure_json)

    empty_event_payload = MachineEventPayloadV1(1, None, None, None)
    assert MachineEventPayloadV1.from_json(empty_event_payload.to_json()) == empty_event_payload
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(empty_event_payload, schema_version=2)

    audit = MachineAuditPayloadV1(1, "PREPARE", "ADMITTED", "execution-1", "job-1", 1, None, None, "event-1")
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(audit, schema_version=2)

    event = MachineEvent(
        1,
        "event-1",
        "execution-1",
        "job-1",
        1,
        "machine.prepared",
        MachinePhase.AWAITING_HOME_APPROVAL,
        None,
        empty_event_payload,
        NOW,
    )
    assert MachineEvent.from_json(event.to_json()) == event
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(event, schema_version=2)
    with pytest.raises(DrawingMachineError, match="MachineEventPayloadV1"):
        replace(event, payload=object())  # type: ignore[arg-type]
    invalid_time = event.to_json()
    invalid_time["occurred_at"] = "not-a-time"
    with pytest.raises(DrawingMachineError, match="ISO 8601"):
        MachineEvent.from_json(invalid_time)


def test_recovery_and_successor_static_branch_matrix() -> None:
    with pytest.raises(DrawingMachineError, match="MachineFailureEvidenceV1"):
        MachineRecoveryEvidence(
            1,
            "execution-1",
            RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.NOT_STARTED,
            "FAIL",
            object(),  # type: ignore[arg-type]
            NOW,
        )
    mismatched = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW,
    )
    assert MachineRecoveryEvidence.from_json(mismatched.to_json()) == mismatched
    invalid_timestamp = mismatched.to_json()
    invalid_timestamp["occurred_at"] = "not-a-time"
    with pytest.raises(DrawingMachineError, match="ISO 8601"):
        MachineRecoveryEvidence.from_json(invalid_timestamp)
    with pytest.raises(DrawingMachineError, match="stream milestone"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            recovery_evidence=mismatched,
        )
    assert execution(phase=MachinePhase.PREPARING_SESSION).phase is MachinePhase.PREPARING_SESSION


def test_post_stream_successor_has_only_the_two_home_edges() -> None:
    awaiting = execution(
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
    )
    homing = replace(awaiting, revision=1, phase=MachinePhase.HOMING)
    ensure_revision_progression(awaiting, homing)
    completed = replace(
        homing,
        revision=2,
        phase=MachinePhase.COMPLETED,
        retirement=OwnerRetirementEvidence(
            homing.execution_id,
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    ensure_revision_progression(homing, completed)
    assert completed.recovery_intent is RecoveryIntent.POST_STREAM_SAFE_HOME


def test_recovery_required_same_execution_can_only_add_one_retirement() -> None:
    ambiguous_evidence = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
        StreamMilestone.FIRST_WRITE_POSSIBLE,
        "STREAM_UNCERTAIN",
        failure(),
        NOW,
    )
    recovered = execution(
        revision=4,
        phase=MachinePhase.RECOVERY_REQUIRED,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        recovery_evidence=ambiguous_evidence,
    )
    post_evidence = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.POST_STREAM_SAFE_HOME,
        StreamMilestone.STREAM_CONFIRMED,
        "RECLASSIFIED",
        failure(),
        NOW,
    )
    reclassified = replace(
        recovered,
        revision=5,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        recovery_evidence=post_evidence,
    )
    with pytest.raises(DrawingMachineError, match="RECOVERY_REQUIRED evidence is frozen"):
        ensure_revision_progression(recovered, reclassified)

    released = replace(
        recovered,
        revision=5,
        retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.RELEASED, NOW),
    )
    ensure_revision_progression(recovered, released)
    with pytest.raises(DrawingMachineError, match="exactly one monotonic retirement"):
        ensure_revision_progression(recovered, replace(recovered, revision=5))
    with pytest.raises(DrawingMachineError, match="exactly one monotonic retirement"):
        ensure_revision_progression(released, replace(released, revision=6))


@dataclass(frozen=True, slots=True)
class AuthorityFailure(MachineFailureEvidenceV1):
    raw_command: str = "G0 X1"


@dataclass(frozen=True, slots=True)
class AuthorityEventPayload(MachineEventPayloadV1):
    serial_port: str = "/dev/ttyUSB0"


@dataclass(frozen=True, slots=True)
class AuthorityRecovery(MachineRecoveryEvidence):
    resume_line: int = 42


@dataclass(frozen=True, slots=True)
class AuthorityRetirement(OwnerRetirementEvidence):
    unlock: bool = True


@dataclass(frozen=True, slots=True)
class AuthorityExecution(MachineExecution):
    jog: bool = True


@dataclass(frozen=True, slots=True)
class AuthorityReadySnapshot(ReadyToRunSnapshot):
    serial_authority: str = "unsafe"


class AuthorityDatetime(datetime):
    raw_write = "G0 X1"


class AuthorityTuple(tuple[float, float, float]):
    pass


def _authority_ready() -> AuthorityReadySnapshot:
    return AuthorityReadySnapshot(
        schema_version=1,
        job_id="job-1",
        job_revision=7,
        gcode=ArtifactRef("gcode", "jobs/job-1/file.gcode", DIGEST, 1, "text/x.gcode"),
        artifacts=(),
        static_result={},
        send_plan={},
        readiness={},
        config=ConfigSnapshot("machine", "provider", 1, 1, 1, DIGEST, DIGEST, DIGEST),
        planning_parameters={},
        application_version="2.0.0",
    )


def _authority_execution() -> AuthorityExecution:
    base = execution()
    values = {field.name: getattr(base, field.name) for field in fields(MachineExecution)}
    return AuthorityExecution(**values)


def test_nested_machine_boundaries_reject_dto_subclasses_with_authority_slots() -> None:
    hostile_failure = AuthorityFailure(1, "FAIL", "failure", "Idle", True)
    with pytest.raises(DrawingMachineError, match="exact MachineFailureEvidenceV1"):
        MachineRecoveryEvidence(
            1,
            "execution-1",
            RecoveryIntent.PRE_STREAM_RESTART,
            StreamMilestone.NOT_STARTED,
            "FAIL",
            hostile_failure,
            NOW,
        )
    with pytest.raises(DrawingMachineError, match="exact MachineFailureEvidenceV1"):
        MachineProgress(1, "execution-1", 1, 1, 0, 0, 1, 0.0, 1, "G1 X1", NOW, hostile_failure)

    hostile_payload = AuthorityEventPayload(1, None, None, "SAFE")
    with pytest.raises(DrawingMachineError, match="exact MachineEventPayloadV1"):
        MachineEvent(
            1,
            "event-1",
            "execution-1",
            "job-1",
            1,
            "machine.event",
            MachinePhase.PREPARING_SESSION,
            None,
            hostile_payload,
            NOW,
        )

    base_recovery = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW,
    )
    hostile_recovery = AuthorityRecovery(
        **{field.name: getattr(base_recovery, field.name) for field in fields(MachineRecoveryEvidence)}
    )
    with pytest.raises(DrawingMachineError, match="exact MachineRecoveryEvidence"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=hostile_recovery,
        )

    hostile_retirement = AuthorityRetirement("execution-1", RetirementDisposition.RELEASED, NOW)
    with pytest.raises(DrawingMachineError, match="exact OwnerRetirementEvidence"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=base_recovery,
            retirement=hostile_retirement,
        )


def test_outcome_job_progression_and_scalar_boundaries_reject_subclasses() -> None:
    hostile_ready = _authority_ready()
    with pytest.raises(DrawingMachineError, match="exact ReadyToRunSnapshot"):
        MachineOutcome(1, "execution-1", "job-1", 1, MachineOutcomeKind.COMPLETED, hostile_ready, None, NOW)
    with pytest.raises(DrawingMachineError, match="exact ReadyToRunSnapshot"):
        MachineJobOutcomeTransition("job-1", 7, JobState.COMPLETED, "job.completed", hostile_ready)
    exact_ready = ReadyToRunSnapshot.from_json(hostile_ready.to_json())
    base_recovery = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW,
    )
    hostile_recovery = AuthorityRecovery(
        **{field.name: getattr(base_recovery, field.name) for field in fields(MachineRecoveryEvidence)}
    )
    with pytest.raises(DrawingMachineError, match="exact MachineRecoveryEvidence"):
        MachineOutcome(
            1,
            "execution-1",
            "job-1",
            1,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            exact_ready,
            hostile_recovery,
            NOW,
        )

    hostile_execution = _authority_execution()
    with pytest.raises(DrawingMachineError, match="exact MachineExecution"):
        ensure_revision_progression(hostile_execution, hostile_execution)

    hostile_time = AuthorityDatetime(2026, 7, 12, tzinfo=UTC)
    with pytest.raises(DrawingMachineError, match="exact datetime"):
        MachineEvent(
            1,
            "event-1",
            "execution-1",
            "job-1",
            1,
            "machine.event",
            MachinePhase.PREPARING_SESSION,
            None,
            MachineEventPayloadV1(1, None, None, "SAFE"),
            hostile_time,
        )

    hostile_position = AuthorityTuple((0.0, 0.0, 0.0))
    from drawingmachine.domain.machine.models import MachineSessionSnapshot

    with pytest.raises(DrawingMachineError, match="exact XYZ tuple"):
        MachineSessionSnapshot(
            1,
            "execution-1",
            "session-1",
            "Idle",
            hostile_position,
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            True,
            NOW,
        )


class ForeignJobState(StrEnum):
    COMPLETED = "COMPLETED"


class ForeignJobKind(StrEnum):
    OFFLINE_WORKFLOW = "OFFLINE_WORKFLOW"


@pytest.mark.parametrize("hostile_state", ["COMPLETED", ForeignJobState.COMPLETED])
def test_machine_job_outcome_requires_exact_job_state(hostile_state: object) -> None:
    ready = ReadyToRunSnapshot.from_json(_authority_ready().to_json())
    with pytest.raises(DrawingMachineError, match="exact JobState"):
        MachineJobOutcomeTransition(
            "job-1",
            7,
            hostile_state,  # type: ignore[arg-type]
            "job.completed",
            ready,
        )


@pytest.mark.parametrize("result_state", [JobState.COMPLETED, JobState.RECOVERY_REQUIRED])
def test_machine_job_outcome_accepts_only_exact_terminal_members(result_state: JobState) -> None:
    ready = ReadyToRunSnapshot.from_json(_authority_ready().to_json())
    transition = MachineJobOutcomeTransition("job-1", 7, result_state, "job.machine_outcome", ready)
    assert transition.to_json()["result_state"] == result_state.value


def test_ordinary_job_transition_runtime_enums_are_exact() -> None:
    assert not allowed_transition(
        "QUEUED",  # type: ignore[arg-type]
        "PREPARING_IMAGE",  # type: ignore[arg-type]
        None,
    )
    assert not allowed_transition(
        JobState.QUEUED,
        JobState.PREPARING_IMAGE,
        None,
        job_kind=ForeignJobKind.OFFLINE_WORKFLOW,  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError):

        class InvalidJobStateSubclass(JobState):
            UNSAFE = "UNSAFE"


def test_retirement_timestamp_is_inside_execution_lifetime() -> None:
    before_created = OwnerRetirementEvidence("execution-1", RetirementDisposition.RELEASED, NOW.replace(hour=14))
    with pytest.raises(DrawingMachineError, match="retired_at"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=MachineRecoveryEvidence(
                1,
                "execution-1",
                RecoveryIntent.PRE_STREAM_RESTART,
                StreamMilestone.NOT_STARTED,
                "FAIL",
                failure(),
                NOW,
            ),
            retirement=before_created,
        )

    after_updated = OwnerRetirementEvidence("execution-1", RetirementDisposition.RELEASED, NOW.replace(hour=16))
    with pytest.raises(DrawingMachineError, match="retired_at"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=MachineRecoveryEvidence(
                1,
                "execution-1",
                RecoveryIntent.PRE_STREAM_RESTART,
                StreamMilestone.NOT_STARTED,
                "FAIL",
                failure(),
                NOW,
            ),
            retirement=after_updated,
        )


def test_recovery_retirement_addition_binds_commit_and_transfer_timestamps() -> None:
    evidence = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW,
    )
    recovered = execution(
        revision=4,
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_evidence=evidence,
    )
    committed_at = NOW.replace(minute=1)
    updated_at = NOW.replace(minute=2)
    transferred = replace(
        recovered,
        revision=5,
        updated_at=updated_at,
        retirement=OwnerRetirementEvidence(
            "execution-1",
            RetirementDisposition.TRANSFERRED,
            committed_at,
            "execution-2",
        ),
    )
    ensure_revision_progression(recovered, transferred)
    assert transferred.retirement is not None
    assert transferred.retirement.retired_at <= transferred.updated_at
    assert transferred.retirement.successor_execution_id == "execution-2"

    forged_after_commit = replace(transferred)
    object.__setattr__(
        forged_after_commit,
        "retirement",
        OwnerRetirementEvidence(
            "execution-1",
            RetirementDisposition.TRANSFERRED,
            NOW.replace(minute=3),
            "execution-2",
        ),
    )
    with pytest.raises(DrawingMachineError, match="retired_at"):
        ensure_revision_progression(recovered, forged_after_commit)


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(seconds=1)])
def test_recovery_evidence_timestamp_must_be_inside_execution_lifetime(
    offset: timedelta,
) -> None:
    evidence = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW + offset,
    )
    with pytest.raises(DrawingMachineError, match=r"recovery_evidence\.occurred_at"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=evidence,
        )


def test_recovery_evidence_execution_boundaries_are_inclusive() -> None:
    updated_at = NOW + timedelta(seconds=2)
    at_created = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW,
    )
    at_updated = replace(at_created, occurred_at=updated_at)
    assert (
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=at_created,
            updated_at=updated_at,
        ).recovery_evidence
        is at_created
    )
    assert (
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=at_updated,
            updated_at=updated_at,
        ).recovery_evidence
        is at_updated
    )


def test_machine_outcome_cannot_precede_recovery_evidence() -> None:
    ready = ReadyToRunSnapshot.from_json(_authority_ready().to_json())
    evidence = MachineRecoveryEvidence(
        1,
        "execution-1",
        RecoveryIntent.PRE_STREAM_RESTART,
        StreamMilestone.NOT_STARTED,
        "FAIL",
        failure(),
        NOW + timedelta(seconds=1),
    )
    with pytest.raises(DrawingMachineError, match=r"outcome\.occurred_at"):
        MachineOutcome(
            1,
            "execution-1",
            "job-1",
            2,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            ready,
            evidence,
            NOW,
        )
    boundary = MachineOutcome(
        1,
        "execution-1",
        "job-1",
        2,
        MachineOutcomeKind.RECOVERY_REQUIRED,
        ready,
        evidence,
        evidence.occurred_at,
    )
    assert boundary.occurred_at == evidence.occurred_at
