from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import drawingmachine.domain.machine.models as machine_models
from drawingmachine.domain.jobs.models import (
    ArtifactRef,
    ConfigSnapshot,
    MachineJobOutcomeTransition,
    ReadyToRunSnapshot,
)
from drawingmachine.domain.jobs.states import JobState
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
    MachineSessionSnapshot,
    OwnerRetirementEvidence,
    RetirementDisposition,
    StreamMilestone,
    ensure_revision_progression,
    recovery_intent_for_milestone,
)
from drawingmachine.domain.machine.phases import MachineAction, MachinePhase, RecoveryIntent
from drawingmachine.domain.machine.progress import MachineProgress
from drawingmachine.domain.machine.transitions import allowed_phase_transition
from drawingmachine.errors import DrawingMachineError

NOW = datetime(2026, 7, 12, 10, tzinfo=UTC)
DIGEST = "a" * 64


def ready_snapshot() -> ReadyToRunSnapshot:
    return ReadyToRunSnapshot(
        schema_version=1,
        job_id="job-1",
        job_revision=7,
        gcode=ArtifactRef("gcode", "jobs/job-1/drawing.gcode", DIGEST, 12, "text/x.gcode"),
        artifacts=(),
        static_result={"allowed": True},
        send_plan={"line_count": 2},
        readiness={"allows_stream": True},
        config=ConfigSnapshot("machine", "provider", 1, 1, 1, DIGEST, DIGEST, DIGEST),
        planning_parameters={"stroke": "fixed"},
        application_version="2.0.0",
    )


def execution(**changes: Any) -> MachineExecution:
    values: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": "execution-1",
        "job_id": "job-1",
        "ready_revision": 7,
        "revision": 0,
        "phase": MachinePhase.PREPARING_SESSION,
        "service_epoch": "service-1",
        "machine_session_epoch": "session-1",
        "gcode_sha256": DIGEST,
        "application_version": "2.0.0",
        "application_digest": DIGEST,
        "machine_digest": DIGEST,
        "provider_digest": DIGEST,
        "stream_milestone": StreamMilestone.NOT_STARTED,
        "recovery_intent": None,
        "predecessor_execution_id": None,
        "recovery_evidence": None,
        "retirement": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return MachineExecution(**values)


def recovery_evidence(milestone: StreamMilestone = StreamMilestone.NOT_STARTED) -> MachineRecoveryEvidence:
    return MachineRecoveryEvidence(
        schema_version=1,
        execution_id="execution-1",
        recovery_intent=recovery_intent_for_milestone(milestone),
        stream_milestone=milestone,
        reason_code="SESSION_LOST",
        failure=MachineFailureEvidenceV1(1, "SESSION_LOST", "session was lost", "Idle", True),
        occurred_at=NOW,
    )


def test_machine_execution_is_frozen_and_rejects_invalid_revision_types_and_time() -> None:
    value = execution()
    with pytest.raises(FrozenInstanceError):
        value.revision = 1  # type: ignore[misc]
    with pytest.raises(DrawingMachineError, match="integer"):
        execution(revision=True)
    with pytest.raises(DrawingMachineError, match="timezone-aware"):
        execution(updated_at=datetime(2026, 7, 12))
    with pytest.raises(DrawingMachineError, match="precede"):
        execution(updated_at=NOW - timedelta(seconds=1))


class HostileString(str):
    pass


def test_contract_rejects_hostile_string_subclasses_and_non_finite_positions() -> None:
    with pytest.raises(DrawingMachineError, match="plain string"):
        execution(execution_id=HostileString("execution-1"))
    with pytest.raises(DrawingMachineError, match="finite"):
        MachineSessionSnapshot(
            1,
            "execution-1",
            "session-1",
            "Idle",
            (float("nan"), 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            True,
            NOW,
        )


@pytest.mark.parametrize(
    ("milestone", "intent"),
    [
        (StreamMilestone.NOT_STARTED, RecoveryIntent.PRE_STREAM_RESTART),
        (StreamMilestone.FIRST_WRITE_POSSIBLE, RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY),
        (StreamMilestone.STREAM_CONFIRMED, RecoveryIntent.POST_STREAM_SAFE_HOME),
    ],
)
def test_irreversible_milestone_derives_exact_recovery_intent(
    milestone: StreamMilestone, intent: RecoveryIntent
) -> None:
    assert recovery_intent_for_milestone(milestone) is intent
    with pytest.raises(DrawingMachineError, match="must match"):
        MachineRecoveryEvidence(
            1,
            "execution-1",
            RecoveryIntent.PRE_STREAM_RESTART
            if intent is not RecoveryIntent.PRE_STREAM_RESTART
            else RecoveryIntent.POST_STREAM_SAFE_HOME,
            milestone,
            "FAIL",
            MachineFailureEvidenceV1(1, "FAIL", "failure", None, True),
            NOW,
        )


def test_recovery_execution_requires_matching_evidence_and_successor_linkage() -> None:
    evidence = recovery_evidence()
    recovered = execution(
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        recovery_evidence=evidence,
    )
    assert recovered.recovery_evidence is evidence
    with pytest.raises(DrawingMachineError, match="must carry"):
        execution(phase=MachinePhase.RECOVERY_REQUIRED)
    with pytest.raises(DrawingMachineError, match="recovery intent"):
        execution(recovery_intent=RecoveryIntent.PRE_STREAM_RESTART)


def test_recovery_intent_is_bound_to_milestone_phase_predecessor_and_evidence() -> None:
    ambiguous = recovery_evidence(StreamMilestone.FIRST_WRITE_POSSIBLE)
    with pytest.raises(DrawingMachineError, match="recovery intent"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            recovery_evidence=ambiguous,
        )
    with pytest.raises(DrawingMachineError, match="release-only"):
        execution(
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
            predecessor_execution_id="execution-0",
        )
    with pytest.raises(DrawingMachineError, match="milestone"):
        execution(
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            predecessor_execution_id="execution-0",
        )
    with pytest.raises(DrawingMachineError, match="subgraph"):
        execution(
            phase=MachinePhase.AWAITING_ZCAL_APPROVAL,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
            predecessor_execution_id="execution-0",
        )
    with pytest.raises(DrawingMachineError, match="requires the confirmed"):
        execution(
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            stream_milestone=StreamMilestone.NOT_STARTED,
            recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
            predecessor_execution_id="execution-0",
        )


def test_retirement_distinguishes_release_completion_and_atomic_transfer() -> None:
    transfer = OwnerRetirementEvidence("execution-1", RetirementDisposition.TRANSFERRED, NOW, "execution-2")
    assert transfer.successor_execution_id == "execution-2"
    for disposition, successor in [
        (RetirementDisposition.RELEASED, "execution-2"),
        (RetirementDisposition.TRANSFERRED, None),
    ]:
        with pytest.raises(DrawingMachineError, match="successor"):
            OwnerRetirementEvidence("execution-1", disposition, NOW, successor)


def test_execution_retirement_disposition_is_bound_to_terminal_phase() -> None:
    with pytest.raises(DrawingMachineError, match="completed retirement"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            recovery_evidence=recovery_evidence(),
            retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.COMPLETED, NOW),
        )
    with pytest.raises(DrawingMachineError, match="release or transfer"):
        execution(
            phase=MachinePhase.COMPLETED,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
            retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.RELEASED, NOW),
        )
    completed = execution(
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.COMPLETED, NOW),
    )
    assert completed.retirement is not None
    released = execution(
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        recovery_evidence=recovery_evidence(),
        retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.RELEASED, NOW),
    )
    assert released.retirement is not None


def test_revision_progression_is_exact_and_monotonic() -> None:
    prior = execution()
    ensure_revision_progression(prior, replace(prior, revision=1, updated_at=NOW + timedelta(seconds=1)))
    with pytest.raises(DrawingMachineError, match="exactly one"):
        ensure_revision_progression(prior, replace(prior, revision=2))
    with pytest.raises(DrawingMachineError, match="timestamps"):
        ensure_revision_progression(prior, replace(prior, revision=1, created_at=NOW - timedelta(seconds=1)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "job-2"),
        ("ready_revision", 8),
        ("gcode_sha256", "b" * 64),
        ("application_version", "2.0.1"),
        ("application_digest", "b" * 64),
        ("machine_digest", "b" * 64),
        ("provider_digest", "b" * 64),
        ("service_epoch", "service-2"),
        ("machine_session_epoch", "session-2"),
    ],
)
def test_revision_progression_rejects_permanent_binding_changes(field: str, value: object) -> None:
    prior = execution()
    with pytest.raises(DrawingMachineError, match="binding"):
        ensure_revision_progression(prior, replace(prior, revision=1, **{field: value}))


def test_revision_progression_rejects_phase_jumps_and_milestone_skips_or_reversal() -> None:
    prior = execution()
    with pytest.raises(DrawingMachineError, match="phase"):
        ensure_revision_progression(prior, replace(prior, revision=1, phase=MachinePhase.AWAITING_ZCAL_APPROVAL))
    with pytest.raises(DrawingMachineError, match="milestone"):
        ensure_revision_progression(
            prior,
            replace(prior, revision=1, stream_milestone=StreamMilestone.STREAM_CONFIRMED),
        )
    streamed = execution(
        revision=4,
        phase=MachinePhase.STREAMING,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
    )
    with pytest.raises(DrawingMachineError, match="milestone"):
        ensure_revision_progression(
            streamed,
            replace(streamed, revision=5, stream_milestone=StreamMilestone.NOT_STARTED),
        )


def test_event_and_audit_payloads_are_deeply_immutable_detached_and_exact() -> None:
    payload = MachineEventPayloadV1(1, "request-1", MachinePhase.PREPARING_SESSION, "PREPARED")
    event = MachineEvent(
        1,
        "event-1",
        "execution-1",
        "job-1",
        1,
        "machine.prepared",
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.PREPARE_SESSION,
        payload,
        NOW,
    )
    encoded = event.to_json()
    assert MachineEvent.from_json(encoded) == event
    assert copy.deepcopy(event.payload) == event.payload
    audit = MachineAuditPayloadV1(1, "PREPARE", "ADMITTED", "execution-1", "job-1", 1, None, "POLICY_OK", "event-1")
    assert MachineAuditPayloadV1.from_json(audit.to_json()) == audit
    encoded["unknown"] = True
    with pytest.raises(DrawingMachineError, match="unknown or missing"):
        MachineEvent.from_json(encoded)


def test_progress_rejects_inconsistent_counts_percent_lines_and_failure() -> None:
    value = MachineProgress(1, "execution-1", 2, 4, 2, 2, 0, 50.0, 18, "G1 X1", NOW)
    assert value.percent == 50.0
    invalid_values = [
        (4, 5, 5, 0, 125.0, 18, None),
        (4, 2, 1, 0, 50.0, 18, None),
        (4, 2, 2, 0, float("inf"), 18, None),
        (4, 0, 0, 0, 0.0, 18, None),
        (4, 1, 0, 1, 25.0, 18, None),
    ]
    for total, ack, ok, errors, percent, line, failure in invalid_values:
        with pytest.raises(DrawingMachineError):
            MachineProgress(1, "execution-1", 2, total, ack, ok, errors, percent, line, "G1 X1", NOW, failure)


def test_progress_counts_only_successful_acknowledgements_and_freezes_last_command() -> None:
    progress = MachineProgress(
        1,
        "execution-1",
        3,
        4,
        2,
        2,
        1,
        50.0,
        19,
        "G1 X2",
        NOW,
        MachineFailureEvidenceV1(1, "CONTROLLER_ERROR", "controller error", "Alarm", True),
    )
    assert progress.acknowledged_lines == progress.ok_count == 2
    assert progress.error_count == 1
    with pytest.raises(DrawingMachineError, match="plain string"):
        replace(progress, last_command=HostileString("G1 X2"))


def test_progress_can_retain_final_idle_failure_after_every_command_ack() -> None:
    failure = MachineFailureEvidenceV1(1, "STREAM_IDLE_TIMEOUT", "Idle proof timed out", "Run", True)
    progress = MachineProgress(
        1,
        "execution-1",
        3,
        4,
        4,
        4,
        1,
        100.0,
        40,
        "M2",
        NOW,
        failure,
    )
    assert progress.acknowledged_lines == progress.total_lines
    assert progress.error_count == 1
    with pytest.raises(DrawingMachineError, match="counters"):
        replace(progress, error_count=2)


def test_machine_outcome_and_job_transition_retain_original_ready_snapshot() -> None:
    ready = ready_snapshot()
    evidence = recovery_evidence()
    outcome = MachineOutcome(1, "execution-1", "job-1", 3, MachineOutcomeKind.RECOVERY_REQUIRED, ready, evidence, NOW)
    assert outcome.job_state is JobState.RECOVERY_REQUIRED
    transition = MachineJobOutcomeTransition(
        "job-1", 7, JobState.RECOVERY_REQUIRED, "job.machine_recovery_required", ready
    )
    assert MachineJobOutcomeTransition.from_json(transition.to_json()) == transition
    with pytest.raises(DrawingMachineError, match="COMPLETED or RECOVERY_REQUIRED"):
        MachineJobOutcomeTransition("job-1", 7, JobState.READY_TO_RUN, "bad", ready)
    with pytest.raises(DrawingMachineError, match="only RECOVERY_REQUIRED"):
        MachineOutcome(1, "execution-1", "job-1", 3, MachineOutcomeKind.COMPLETED, ready, evidence, NOW)


@pytest.mark.parametrize(
    ("intent", "milestone"),
    [
        (RecoveryIntent.PRE_STREAM_RESTART, StreamMilestone.NOT_STARTED),
        (RecoveryIntent.POST_STREAM_SAFE_HOME, StreamMilestone.STREAM_CONFIRMED),
    ],
)
def test_recovery_successor_starts_preparing_before_real_session_observation(
    intent: RecoveryIntent,
    milestone: StreamMilestone,
) -> None:
    successor = execution(
        execution_id="successor-1",
        predecessor_execution_id="execution-1",
        recovery_intent=intent,
        stream_milestone=milestone,
        phase=MachinePhase.PREPARING_SESSION,
        machine_session_epoch="successor-session",
    )
    assert successor.phase is MachinePhase.PREPARING_SESSION
    assert allowed_phase_transition(
        MachinePhase.PREPARING_SESSION,
        MachineAction.PREPARE_SESSION,
        MachinePhase.AWAITING_HOME_APPROVAL,
        recovery_intent=intent,
    )
    assert not allowed_phase_transition(
        MachinePhase.PREPARING_SESSION,
        MachineAction.PREPARE_SESSION,
        MachinePhase.HOMING,
        recovery_intent=intent,
    )


def test_ambiguous_recovery_still_has_no_preparing_successor_or_motion_edge() -> None:
    with pytest.raises(DrawingMachineError, match="release-only"):
        execution(
            execution_id="successor-1",
            predecessor_execution_id="execution-1",
            recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            phase=MachinePhase.PREPARING_SESSION,
            machine_session_epoch="successor-session",
        )
    assert not allowed_phase_transition(
        MachinePhase.PREPARING_SESSION,
        MachineAction.PREPARE_SESSION,
        MachinePhase.AWAITING_HOME_APPROVAL,
        recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
    )


def test_unknown_and_missing_direct_constructor_fields_are_rejected_by_slots_signature() -> None:
    values = execution()
    with pytest.raises(TypeError):
        MachineExecution(**{**{field: getattr(values, field) for field in values.__slots__}, "unknown": 1})
    with pytest.raises(TypeError):
        MachineExecution(**{field: getattr(values, field) for field in values.__slots__ if field != "job_id"})


def test_low_level_contract_validators_cover_hostile_scalar_and_container_inputs() -> None:
    with pytest.raises(DrawingMachineError, match="invalid Unicode"):
        machine_models._string("\ud800", "field")
    with pytest.raises(DrawingMachineError, match="control"):
        machine_models._string("bad\nvalue", "field")
    with pytest.raises(DrawingMachineError, match="numeric"):
        machine_models._finite(object(), "field")
    assert machine_models._finite(1, "field") == 1.0
    with pytest.raises(DrawingMachineError, match="SHA-256"):
        machine_models._digest("bad", "field")
    with pytest.raises(DrawingMachineError, match="MachinePhase"):
        machine_models._enum("STREAMING", MachinePhase, "field")
    with pytest.raises(DrawingMachineError, match="unknown"):
        machine_models._enum_json("UNKNOWN", MachinePhase, "field")
    with pytest.raises(DrawingMachineError, match="must be an object"):
        machine_models._object([], frozenset(), "object")
    with pytest.raises(DrawingMachineError, match="plain strings"):
        machine_models._object({1: "bad"}, frozenset(), "object")
    with pytest.raises(DrawingMachineError, match="XYZ"):
        machine_models._position((0.0, 0.0), "position")


def test_session_recovery_and_retirement_cover_invalid_runtime_branches() -> None:
    position = (0.0, 0.0, 0.0)
    snapshot = MachineSessionSnapshot(
        1, "execution-1", "session-1", "Idle", position, position, position, position, True, NOW
    )
    assert snapshot.g54 == position
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(snapshot, schema_version=2)
    with pytest.raises(DrawingMachineError, match="boolean"):
        replace(snapshot, stabilized=1)  # type: ignore[arg-type]
    evidence = recovery_evidence()
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(evidence, schema_version=2)
    with pytest.raises(DrawingMachineError, match="itself"):
        OwnerRetirementEvidence("execution-1", RetirementDisposition.TRANSFERRED, NOW, "execution-1")


def test_execution_rejects_invalid_nested_evidence_retirement_and_schema_branches() -> None:
    with pytest.raises(DrawingMachineError, match="schema"):
        execution(schema_version=2)
    with pytest.raises(DrawingMachineError, match="own predecessor"):
        execution(
            predecessor_execution_id="execution-1",
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
        )
    with pytest.raises(DrawingMachineError, match="exact MachineRecoveryEvidence"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            recovery_evidence=object(),
        )
    wrong_evidence = replace(recovery_evidence(), execution_id="execution-2")
    with pytest.raises(DrawingMachineError, match="identify"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            recovery_evidence=wrong_evidence,
        )
    with pytest.raises(DrawingMachineError, match="exact OwnerRetirementEvidence"):
        execution(retirement=object())
    wrong_retirement = OwnerRetirementEvidence("execution-2", RetirementDisposition.RELEASED, NOW)
    with pytest.raises(DrawingMachineError, match="identify"):
        execution(retirement=wrong_retirement)
    with pytest.raises(DrawingMachineError, match="only valid"):
        execution(recovery_evidence=recovery_evidence())
    with pytest.raises(DrawingMachineError, match="recovery intent"):
        execution(
            phase=MachinePhase.RECOVERY_REQUIRED,
            recovery_evidence=recovery_evidence(),
        )
    with pytest.raises(DrawingMachineError, match="requires a recovery intent"):
        execution(
            predecessor_execution_id="execution-0",
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
        )


def test_execution_phase_milestone_and_successor_subgraph_branches() -> None:
    recovered_successor = execution(
        phase=MachinePhase.RECOVERY_REQUIRED,
        stream_milestone=StreamMilestone.NOT_STARTED,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id="execution-0",
        recovery_evidence=recovery_evidence(),
    )
    assert recovered_successor.phase is MachinePhase.RECOVERY_REQUIRED
    with pytest.raises(DrawingMachineError, match="confirmed stream"):
        execution(
            phase=MachinePhase.COMPLETED,
            stream_milestone=StreamMilestone.NOT_STARTED,
            recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
            predecessor_execution_id="execution-0",
            retirement=OwnerRetirementEvidence(
                "execution-1",
                RetirementDisposition.COMPLETED,
                NOW,
            ),
        )
    with pytest.raises(DrawingMachineError, match="release-only"):
        execution(
            phase=MachinePhase.STREAMING,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
            recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
            predecessor_execution_id="execution-0",
        )
    safe_home = execution(
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        predecessor_execution_id="execution-0",
    )
    assert safe_home.phase is MachinePhase.AWAITING_HOME_APPROVAL
    with pytest.raises(DrawingMachineError, match="first-write"):
        execution(
            phase=MachinePhase.AWAITING_HOME_APPROVAL,
            stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        )
    with pytest.raises(DrawingMachineError, match="confirmed stream"):
        execution(
            phase=MachinePhase.AWAITING_ZCAL_APPROVAL,
            stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        )
    with pytest.raises(DrawingMachineError, match="requires the confirmed"):
        execution(phase=MachinePhase.AWAITING_HOME_Z_APPROVAL)


def test_outcome_invalid_schema_snapshot_identity_and_evidence_branches() -> None:
    ready = ready_snapshot()
    completed = MachineOutcome(1, "execution-1", "job-1", 3, MachineOutcomeKind.COMPLETED, ready, None, NOW)
    with pytest.raises(DrawingMachineError, match="schema"):
        replace(completed, schema_version=2)
    with pytest.raises(DrawingMachineError, match="ReadyToRunSnapshot"):
        replace(completed, ready_snapshot=object())
    with pytest.raises(DrawingMachineError, match="job ID"):
        replace(completed, job_id="job-2")
    wrong_evidence = replace(recovery_evidence(), execution_id="execution-2")
    with pytest.raises(DrawingMachineError, match="execution ID"):
        MachineOutcome(
            1,
            "execution-1",
            "job-1",
            3,
            MachineOutcomeKind.RECOVERY_REQUIRED,
            ready,
            wrong_evidence,
            NOW,
        )


def test_revision_progression_covers_legal_edges_recovery_and_retirement_permanence() -> None:
    prior = execution()
    successor = replace(prior, revision=1, phase=MachinePhase.AWAITING_HOME_APPROVAL)
    ensure_revision_progression(prior, successor)
    completed_for_retirement = execution(
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.COMPLETED, NOW),
    )
    retired = replace(
        completed_for_retirement,
        revision=1,
    )
    changed_retirement = replace(retired, revision=2, updated_at=NOW + timedelta(seconds=2))
    object.__setattr__(
        changed_retirement,
        "retirement",
        OwnerRetirementEvidence("execution-1", RetirementDisposition.COMPLETED, NOW + timedelta(seconds=1)),
    )
    with pytest.raises(DrawingMachineError, match="permanent"):
        ensure_revision_progression(retired, changed_retirement)
    with pytest.raises(DrawingMachineError, match="MachineExecution"):
        ensure_revision_progression(object(), prior)  # type: ignore[arg-type]

    evidence = recovery_evidence()
    recovery = replace(
        prior,
        revision=1,
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        recovery_evidence=evidence,
    )
    ensure_revision_progression(prior, recovery)
    completed = execution(
        revision=4,
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence("execution-1", RetirementDisposition.COMPLETED, NOW),
    )
    post_evidence = recovery_evidence(StreamMilestone.STREAM_CONFIRMED)
    impossible_recovery = replace(
        completed,
        revision=5,
        phase=MachinePhase.RECOVERY_REQUIRED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        recovery_evidence=post_evidence,
        retirement=None,
    )
    object.__setattr__(completed, "retirement", None)
    with pytest.raises(DrawingMachineError, match="terminal"):
        ensure_revision_progression(completed, impossible_recovery)


def test_revision_progression_defends_against_fabricated_intent_and_all_successor_edges() -> None:
    prior = execution()
    wrong_derived = replace(prior, revision=1)
    object.__setattr__(wrong_derived, "recovery_intent", RecoveryIntent.POST_STREAM_SAFE_HOME)
    with pytest.raises(DrawingMachineError, match="initial execution"):
        ensure_revision_progression(prior, wrong_derived)
    same_milestone = replace(prior, revision=1)
    object.__setattr__(same_milestone, "recovery_intent", RecoveryIntent.PRE_STREAM_RESTART)
    with pytest.raises(DrawingMachineError, match="initial execution"):
        ensure_revision_progression(prior, same_milestone)

    pre_home = execution(
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id="execution-0",
    )
    ensure_revision_progression(pre_home, replace(pre_home, revision=1, phase=MachinePhase.HOMING))

    admitted_stream = execution(
        phase=MachinePhase.STREAMING,
        stream_milestone=StreamMilestone.FIRST_WRITE_POSSIBLE,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
        predecessor_execution_id="execution-0",
    )
    confirmed = replace(
        admitted_stream,
        revision=1,
        phase=MachinePhase.COMPLETED,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        retirement=OwnerRetirementEvidence(
            admitted_stream.execution_id,
            RetirementDisposition.COMPLETED,
            NOW,
        ),
    )
    ensure_revision_progression(admitted_stream, confirmed)

    safe_home = execution(
        phase=MachinePhase.AWAITING_HOME_APPROVAL,
        stream_milestone=StreamMilestone.STREAM_CONFIRMED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
        predecessor_execution_id="execution-0",
    )
    ensure_revision_progression(
        safe_home,
        replace(safe_home, revision=1, phase=MachinePhase.HOMING),
    )
