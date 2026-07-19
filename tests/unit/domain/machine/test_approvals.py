from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import drawingmachine.domain.machine.approvals as approvals_module
from drawingmachine.domain.machine.approvals import (
    ApprovalPurpose,
    ApprovalStatus,
    AuthenticatedMachinePrincipal,
    KernelMachinePrincipal,
    MachineApprovalBinding,
    MachineApprovalChallenge,
    MachineApprovalDecision,
    MachineApprovalEvidenceV1,
    apply_approval_decision,
    consume_approval,
    expire_approval,
    issue_approval,
    supersede_approval,
)
from drawingmachine.domain.machine.models import MachineSessionSnapshot
from drawingmachine.domain.machine.phases import (
    MachineAction,
    MachinePhase,
    RecoveryDisposition,
)
from drawingmachine.errors import DrawingMachineError

NOW = datetime(2026, 7, 12, 10, tzinfo=UTC)
DIGESTS = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
CHALLENGE_ID = "123e4567-e89b-42d3-a456-426614174000"
NEXT_CHALLENGE_ID = "123e4567-e89b-42d3-a456-426614174001"
AUTOMATION = KernelMachinePrincipal(uid=1100, gid=1100)
OPERATOR = KernelMachinePrincipal(uid=2200, gid=2200)
AUTOMATION_PEER = AuthenticatedMachinePrincipal(uid=1100, gid=1100, pid=31001)
OPERATOR_PEER = AuthenticatedMachinePrincipal(uid=2200, gid=2200, pid=41001)


def binding(
    action: MachineAction = MachineAction.HOME,
    **changes: Any,
) -> MachineApprovalBinding:
    phases = {
        MachineAction.HOME: MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.ZCAL: MachinePhase.AWAITING_ZCAL_APPROVAL,
        MachineAction.ZCONFIRM: MachinePhase.AWAITING_ZCONFIRM_APPROVAL,
        MachineAction.STREAM: MachinePhase.AWAITING_STREAM_APPROVAL,
        MachineAction.HOME_Z: MachinePhase.AWAITING_HOME_Z_APPROVAL,
        MachineAction.RECOVER: MachinePhase.RECOVERY_REQUIRED,
    }
    values: dict[str, Any] = {
        "execution_id": "execution-1",
        "execution_revision": 9,
        "action": action,
        "recovery_disposition": RecoveryDisposition.RELEASE if action is MachineAction.RECOVER else None,
        "required_prior_phase": phases[action],
        "service_epoch": "service-1",
        "machine_session_epoch": "session-1",
        "job_id": "job-1",
        "ready_revision": 7,
        "application_digest": DIGESTS[0],
        "machine_digest": DIGESTS[1],
        "provider_digest": DIGESTS[2],
        "gcode_sha256": DIGESTS[3],
    }
    values.update(changes)
    return MachineApprovalBinding(**values)


def session(**changes: Any) -> MachineSessionSnapshot:
    values: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": "execution-1",
        "machine_session_epoch": "session-1",
        "controller_state": "Idle",
        "mpos": (0.0, 0.0, 0.0),
        "wpos": (96.0, 96.0, 3.5),
        "wco": (-96.0, -96.0, -3.5),
        "g54": (96.0, 96.0, 0.0),
        "stabilized": True,
        "observed_at": NOW,
    }
    values.update(changes)
    return MachineSessionSnapshot(**values)


def evidence(action: MachineAction = MachineAction.HOME, **changes: Any) -> MachineApprovalEvidenceV1:
    current = binding(action)
    values: dict[str, Any] = {
        "schema_version": 1,
        "action": action,
        "required_prior_phase": current.required_prior_phase,
        "session": None if action is MachineAction.RECOVER else session(),
    }
    values.update(changes)
    return MachineApprovalEvidenceV1(**values)


def challenge(
    action: MachineAction = MachineAction.HOME,
    *,
    requester: AuthenticatedMachinePrincipal = AUTOMATION_PEER,
    challenge_id: str = CHALLENGE_ID,
) -> MachineApprovalChallenge:
    return issue_approval(
        challenge_id=challenge_id,
        binding=binding(action),
        requester=requester,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        evidence=evidence(action),
        issued_at=NOW,
        monotonic_now=1000.0,
        ttl_seconds=60,
    )


def challenge_from_json(
    payload: object,
    *,
    trusted_requester: AuthenticatedMachinePrincipal = AUTOMATION_PEER,
    trusted_consumer: AuthenticatedMachinePrincipal | None = None,
) -> MachineApprovalChallenge:
    return MachineApprovalChallenge.from_json(
        payload,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        trusted_requester=trusted_requester,
        trusted_consumer=trusted_consumer,
    )


def consume(
    value: MachineApprovalChallenge,
    *,
    current_binding: MachineApprovalBinding | None = None,
    consumer: AuthenticatedMachinePrincipal = OPERATOR_PEER,
    operator_principal: KernelMachinePrincipal = OPERATOR,
    wall_now: datetime = NOW + timedelta(seconds=1),
    monotonic_now: float = 1001.0,
):
    return consume_approval(
        value,
        current_binding=current_binding or value.binding,
        consumer=consumer,
        operator_principal=operator_principal,
        wall_now=wall_now,
        monotonic_now=monotonic_now,
    )


def test_issue_binds_distinct_principals_fixed_home_purpose_and_typed_pose_evidence() -> None:
    value = challenge()
    assert value.status is ApprovalStatus.PENDING
    assert value.requester == AUTOMATION_PEER
    assert value.operator_principal == OPERATOR
    assert value.purpose is ApprovalPurpose.HOME
    assert value.purpose.value == "motor high-voltage connected, physical area clear, approve HOME"
    assert value.evidence.session == session()
    assert value.evidence.session is not None
    assert value.evidence.session.controller_state == "Idle"
    assert value.evidence.session.mpos == (0.0, 0.0, 0.0)
    assert value.evidence.session.wpos == (96.0, 96.0, 3.5)
    assert value.evidence.session.wco == (-96.0, -96.0, -3.5)
    assert value.evidence.session.g54 == (96.0, 96.0, 0.0)
    assert value.monotonic_deadline == 1060.0
    assert value.expires_at == NOW + timedelta(seconds=60)


def test_operator_may_request_and_changed_pid_with_same_uid_gid_may_consume() -> None:
    value = challenge(requester=OPERATOR_PEER)
    changed_pid = AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, 99999)
    decision = consume(value, consumer=changed_pid)
    consumed = apply_approval_decision(value, decision)
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumer == changed_pid
    assert consumed.status_changed_at == NOW + timedelta(seconds=1)


@pytest.mark.parametrize(
    "consumer",
    [
        AUTOMATION_PEER,
        AuthenticatedMachinePrincipal(OPERATOR.uid + 1, OPERATOR.gid, 5),
        AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid + 1, 5),
    ],
)
def test_only_exact_configured_operator_uid_gid_can_consume(
    consumer: AuthenticatedMachinePrincipal,
) -> None:
    with pytest.raises(DrawingMachineError, match="configured operator"):
        consume(challenge(), consumer=consumer)


def test_requester_must_be_exact_automation_or_operator_and_roles_are_disjoint() -> None:
    outsider = AuthenticatedMachinePrincipal(3300, 3300, 7)
    with pytest.raises(DrawingMachineError, match="requester"):
        challenge(requester=outsider)
    for automation, operator in [
        (KernelMachinePrincipal(1100, 1100), KernelMachinePrincipal(1100, 2200)),
        (KernelMachinePrincipal(1100, 1100), KernelMachinePrincipal(2200, 1100)),
    ]:
        with pytest.raises(DrawingMachineError, match="disjoint"):
            issue_approval(
                challenge_id=CHALLENGE_ID,
                binding=binding(),
                requester=AUTOMATION_PEER,
                automation_principal=automation,
                operator_principal=operator,
                evidence=evidence(),
                issued_at=NOW,
                monotonic_now=1.0,
                ttl_seconds=60,
            )


def test_each_action_has_one_distinct_fixed_non_configurable_purpose() -> None:
    actions = [
        MachineAction.HOME,
        MachineAction.ZCAL,
        MachineAction.ZCONFIRM,
        MachineAction.STREAM,
        MachineAction.HOME_Z,
        MachineAction.RECOVER,
    ]
    purposes = [
        challenge(action, challenge_id=f"123e4567-e89b-42d3-a456-42661417400{index}").purpose
        for index, action in enumerate(actions)
    ]
    assert len(set(purposes)) == len(actions)
    assert all(purpose.value.startswith(("motor", "physical", "manual", "drawing", "review")) for purpose in purposes)


def test_challenge_authorizes_exactly_one_action_and_never_implies_the_next() -> None:
    home = challenge()
    with pytest.raises(DrawingMachineError, match="binding"):
        consume(home, current_binding=binding(MachineAction.ZCAL))


def _binding_mismatches() -> list[tuple[str, MachineApprovalBinding]]:
    return [
        ("action/phase", binding(MachineAction.ZCAL)),
        (
            "disposition",
            binding(
                MachineAction.RECOVER,
                recovery_disposition=RecoveryDisposition.SAFE_HOME,
            ),
        ),
        ("execution", binding(execution_id="execution-other")),
        ("service epoch", binding(service_epoch="service-old")),
        ("session epoch", binding(machine_session_epoch="session-old")),
        ("job", binding(job_id="job-other")),
        ("execution revision", binding(execution_revision=10)),
        ("ready revision", binding(ready_revision=8)),
        ("application digest", binding(application_digest="e" * 64)),
        ("machine digest", binding(machine_digest="e" * 64)),
        ("provider digest", binding(provider_digest="e" * 64)),
        ("G-code digest", binding(gcode_sha256="e" * 64)),
    ]


@pytest.mark.parametrize(("label", "wrong_binding"), _binding_mismatches())
def test_consume_fails_closed_for_every_current_binding_mismatch(
    label: str, wrong_binding: MachineApprovalBinding
) -> None:
    del label
    with pytest.raises(DrawingMachineError, match="binding"):
        consume(challenge(), current_binding=wrong_binding)


def test_future_and_expired_wall_or_monotonic_deadlines_fail_closed() -> None:
    value = challenge()
    with pytest.raises(DrawingMachineError, match="future"):
        consume(value, wall_now=NOW - timedelta(microseconds=1), monotonic_now=1001.0)
    with pytest.raises(DrawingMachineError, match="future"):
        consume(value, wall_now=NOW, monotonic_now=999.0)
    with pytest.raises(DrawingMachineError, match="expired"):
        consume(value, wall_now=value.expires_at, monotonic_now=1001.0)
    with pytest.raises(DrawingMachineError, match="expired"):
        consume(value, wall_now=NOW + timedelta(seconds=1), monotonic_now=value.monotonic_deadline)


def test_consumed_superseded_and_expired_challenges_cannot_be_replayed() -> None:
    pending = challenge()
    consumed = apply_approval_decision(pending, consume(pending))
    with pytest.raises(DrawingMachineError, match="pending"):
        consume(consumed)

    pending = challenge(challenge_id=NEXT_CHALLENGE_ID)
    superseded = apply_approval_decision(
        pending,
        supersede_approval(
            pending,
            wall_now=NOW + timedelta(seconds=1),
            monotonic_now=1001.0,
        ),
    )
    assert superseded.status is ApprovalStatus.SUPERSEDED
    with pytest.raises(DrawingMachineError, match="pending"):
        consume(superseded)

    pending = challenge()
    expired = apply_approval_decision(
        pending,
        expire_approval(
            pending,
            wall_now=pending.expires_at,
            monotonic_now=1001.0,
        ),
    )
    assert expired.status is ApprovalStatus.EXPIRED
    with pytest.raises(DrawingMachineError, match="pending"):
        consume(expired)


def test_concurrent_consume_decisions_carry_one_expected_state_and_second_apply_fails() -> None:
    pending = challenge()
    first = consume(pending, consumer=AuthenticatedMachinePrincipal(2200, 2200, 100))
    second = consume(pending, consumer=AuthenticatedMachinePrincipal(2200, 2200, 200))
    consumed = apply_approval_decision(pending, first)
    assert consumed.consumer is not None and consumed.consumer.pid == 100
    with pytest.raises(DrawingMachineError, match="stale"):
        apply_approval_decision(consumed, second)


def test_supersede_rejects_expired_input_and_expire_requires_reached_deadline() -> None:
    pending = challenge()
    with pytest.raises(DrawingMachineError, match="not expired"):
        expire_approval(pending, wall_now=NOW + timedelta(seconds=1), monotonic_now=1001.0)
    with pytest.raises(DrawingMachineError, match="expired"):
        supersede_approval(
            pending,
            wall_now=pending.expires_at,
            monotonic_now=1001.0,
        )


def test_superseded_chronology_is_strict_for_constructor_json_and_decision_apply() -> None:
    pending = challenge()
    with pytest.raises(DrawingMachineError, match="SUPERSEDED decision must strictly precede"):
        late_result = replace(
            pending,
            status=ApprovalStatus.SUPERSEDED,
            status_changed_at=pending.expires_at,
        )
        late_decision = MachineApprovalDecision(expected=pending, result=late_result)
        apply_approval_decision(pending, late_decision)

    payload = pending.to_json()
    payload["status"] = ApprovalStatus.SUPERSEDED.value
    payload["status_changed_at"] = pending.expires_at.isoformat()
    with pytest.raises(DrawingMachineError, match="SUPERSEDED decision must strictly precede"):
        challenge_from_json(payload)

    just_before = pending.expires_at - timedelta(microseconds=1)
    just_before_monotonic = pending.monotonic_deadline - 0.000001
    superseded = apply_approval_decision(
        pending,
        supersede_approval(
            pending,
            wall_now=just_before,
            monotonic_now=just_before_monotonic,
        ),
    )
    assert superseded.status is ApprovalStatus.SUPERSEDED
    assert superseded.status_changed_at == just_before

    pending = challenge()
    expired = apply_approval_decision(
        pending,
        expire_approval(
            pending,
            wall_now=pending.expires_at,
            monotonic_now=pending.monotonic_deadline,
        ),
    )
    assert expired.status is ApprovalStatus.EXPIRED
    assert expired.status_changed_at == pending.expires_at


def test_challenge_json_schema_round_trip_is_exact_and_deeply_immutable() -> None:
    value = challenge()
    payload = value.to_json()
    assert challenge_from_json(payload) == value
    assert isinstance(payload["binding"], dict)
    assert isinstance(payload["evidence"], dict)
    assert isinstance(payload["requester"], dict)
    with pytest.raises(FrozenInstanceError):
        value.status = ApprovalStatus.CONSUMED  # type: ignore[misc]
    changed = payload.copy()
    changed["raw_command"] = "$X"
    with pytest.raises(DrawingMachineError, match="unknown or missing"):
        challenge_from_json(changed)
    session_payload = payload["evidence"]["session"]  # type: ignore[index]
    session_payload["mpos"][0] = 999.0  # type: ignore[index]
    assert value.evidence.session is not None
    assert value.evidence.session.mpos == (0.0, 0.0, 0.0)


def test_json_identity_fields_cannot_override_current_kernel_operator_authority() -> None:
    payload = challenge().to_json()
    payload["operator_principal"] = {"uid": 3300, "gid": 3300}
    with pytest.raises(DrawingMachineError, match="JSON operator identity"):
        challenge_from_json(payload)

    payload = challenge().to_json()
    payload["requester"] = {"uid": 3300, "gid": 3300, "pid": 7}
    with pytest.raises(DrawingMachineError, match="trusted authenticated requester"):
        challenge_from_json(payload)

    payload = challenge().to_json()
    payload["requester"] = OPERATOR_PEER.to_json()
    with pytest.raises(DrawingMachineError, match="trusted authenticated requester"):
        challenge_from_json(payload)

    payload = challenge().to_json()
    payload["requester"] = AuthenticatedMachinePrincipal(1100, 1100, 99999).to_json()
    with pytest.raises(DrawingMachineError, match="trusted authenticated requester"):
        challenge_from_json(payload)

    consumed = apply_approval_decision(challenge(), consume(challenge()))
    payload = consumed.to_json()
    payload["consumer"] = AuthenticatedMachinePrincipal(2200, 2200, 99999).to_json()
    with pytest.raises(DrawingMachineError, match="trusted authenticated consumer"):
        challenge_from_json(payload, trusted_consumer=OPERATOR_PEER)

    payload = consumed.to_json()
    payload["consumer"] = AUTOMATION_PEER.to_json()
    with pytest.raises(DrawingMachineError):
        challenge_from_json(payload, trusted_consumer=OPERATOR_PEER)


def test_json_rehydration_requires_exact_disjoint_trusted_principals() -> None:
    payload = challenge().to_json()
    with pytest.raises(DrawingMachineError, match="trusted automation principal"):
        MachineApprovalChallenge.from_json(  # type: ignore[arg-type]
            payload,
            automation_principal=object(),
            operator_principal=OPERATOR,
            trusted_requester=AUTOMATION_PEER,
            trusted_consumer=None,
        )
    with pytest.raises(DrawingMachineError, match="trusted operator principal"):
        MachineApprovalChallenge.from_json(  # type: ignore[arg-type]
            payload,
            automation_principal=AUTOMATION,
            operator_principal=object(),
            trusted_requester=AUTOMATION_PEER,
            trusted_consumer=None,
        )
    with pytest.raises(DrawingMachineError, match="trusted requester"):
        MachineApprovalChallenge.from_json(  # type: ignore[arg-type]
            payload,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            trusted_requester=object(),
            trusted_consumer=None,
        )
    with pytest.raises(DrawingMachineError, match="trusted consumer"):
        MachineApprovalChallenge.from_json(  # type: ignore[arg-type]
            payload,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            trusted_requester=AUTOMATION_PEER,
            trusted_consumer=object(),
        )
    with pytest.raises(DrawingMachineError, match="roles must be disjoint"):
        MachineApprovalChallenge.from_json(
            payload,
            automation_principal=AUTOMATION,
            operator_principal=KernelMachinePrincipal(2200, AUTOMATION.gid),
            trusted_requester=AUTOMATION_PEER,
            trusted_consumer=None,
        )
    with pytest.raises(DrawingMachineError, match="trusted authenticated requester"):
        MachineApprovalChallenge.from_json(
            payload,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            trusted_requester=AuthenticatedMachinePrincipal(3300, 3300, 7),
            trusted_consumer=None,
        )
    with pytest.raises(DrawingMachineError, match="trusted authenticated consumer"):
        MachineApprovalChallenge.from_json(
            payload,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            trusted_requester=AUTOMATION_PEER,
            trusted_consumer=AUTOMATION_PEER,
        )


def test_json_round_trip_preserves_each_terminal_status_and_audit_pid() -> None:
    pending = challenge()
    later_operator_process = AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, 99999)
    consumed = apply_approval_decision(pending, consume(pending, consumer=later_operator_process))
    assert challenge_from_json(consumed.to_json(), trusted_consumer=later_operator_process) == consumed
    assert consumed.consumer is not None and consumed.consumer.pid == later_operator_process.pid

    pending = challenge(challenge_id=NEXT_CHALLENGE_ID)
    superseded = apply_approval_decision(
        pending,
        supersede_approval(pending, wall_now=NOW + timedelta(seconds=1), monotonic_now=1001.0),
    )
    assert challenge_from_json(superseded.to_json()) == superseded

    pending = challenge()
    expired = apply_approval_decision(
        pending,
        expire_approval(pending, wall_now=pending.expires_at, monotonic_now=1001.0),
    )
    assert challenge_from_json(expired.to_json()) == expired


def test_json_rehydration_rejects_out_of_contract_or_mismatched_ttl_durations() -> None:
    for wall_seconds, monotonic_seconds, message in [
        (4, 4.0, "5 to 900"),
        (901, 901.0, "5 to 900"),
        (60, 61.0, "equivalent"),
        (60.000001, 60.0, "equivalent"),
    ]:
        payload = challenge().to_json()
        payload["expires_at"] = (NOW + timedelta(seconds=wall_seconds)).isoformat()
        payload["monotonic_deadline"] = 1000.0 + monotonic_seconds
        with pytest.raises(DrawingMachineError, match=message):
            challenge_from_json(payload)


def test_issue_time_arithmetic_overflow_is_a_typed_fail_closed_error() -> None:
    with pytest.raises(DrawingMachineError, match="overflow"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(session=session(observed_at=datetime.max.replace(tzinfo=UTC))),
            issued_at=datetime.max.replace(tzinfo=UTC),
            monotonic_now=1.0,
            ttl_seconds=60,
        )


def test_duration_arithmetic_overflow_is_a_typed_fail_closed_error() -> None:
    class OverflowingDate:
        def __sub__(self, other: object) -> object:
            del other
            raise OverflowError("hostile datetime arithmetic")

    with pytest.raises(DrawingMachineError, match="overflow"):
        approvals_module._validate_approval_duration(  # type: ignore[arg-type]
            OverflowingDate(),
            OverflowingDate(),
            1.0,
            61.0,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(challenge_id="not-a-uuid"),
        lambda payload: payload.update(schema_version=True),
        lambda payload: payload.update(status="UNKNOWN"),
        lambda payload: payload.update(issued_at="not-time"),
        lambda payload: payload.update(monotonic_deadline=float("inf")),
        lambda payload: payload.update(expires_at="not-time"),
        lambda payload: payload.update(consumer={"uid": 2200, "gid": 2200, "pid": True}),
    ],
)
def test_challenge_json_rejects_malformed_and_hostile_values(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = challenge().to_json()
    mutate(payload)
    with pytest.raises(DrawingMachineError):
        challenge_from_json(payload)


def test_nested_json_schemas_reject_unknown_fields_and_wrong_exact_types() -> None:
    payload = challenge().to_json()
    for field in ("binding", "evidence", "requester", "operator_principal"):
        changed = challenge().to_json()
        nested = dict(changed[field])  # type: ignore[arg-type]
        nested["spoofed_uid"] = 0
        changed[field] = nested
        with pytest.raises(DrawingMachineError, match="unknown or missing"):
            challenge_from_json(changed)
    payload["binding"] = {**payload["binding"], "execution_revision": True}  # type: ignore[dict-item]
    with pytest.raises(DrawingMachineError, match="integer"):
        challenge_from_json(payload)


def test_direct_contracts_reject_mismatched_evidence_status_and_invalid_time_bounds() -> None:
    with pytest.raises(DrawingMachineError, match="evidence"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(MachineAction.ZCAL),
            issued_at=NOW,
            monotonic_now=1.0,
            ttl_seconds=60,
        )
    with pytest.raises(DrawingMachineError, match="stabilized"):
        evidence(session=session(stabilized=False))
    with pytest.raises(DrawingMachineError, match="session evidence"):
        evidence(session=None)
    with pytest.raises(DrawingMachineError, match="does not use session"):
        evidence(MachineAction.RECOVER, session=session())
    with pytest.raises(DrawingMachineError, match="TTL"):
        challenge_with_ttl(4)
    with pytest.raises(DrawingMachineError, match="TTL"):
        challenge_with_ttl(True)
    with pytest.raises(DrawingMachineError, match="monotonic"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(),
            issued_at=NOW,
            monotonic_now=-1.0,
            ttl_seconds=60,
        )


def challenge_with_ttl(ttl: Any) -> MachineApprovalChallenge:
    return issue_approval(
        challenge_id=CHALLENGE_ID,
        binding=binding(),
        requester=AUTOMATION_PEER,
        automation_principal=AUTOMATION,
        operator_principal=OPERATOR,
        evidence=evidence(),
        issued_at=NOW,
        monotonic_now=1.0,
        ttl_seconds=ttl,
    )


def test_challenge_constructor_rejects_impossible_status_metadata_and_purpose_spoofing() -> None:
    pending = challenge()
    with pytest.raises(DrawingMachineError, match="pending"):
        replace(pending, consumer=OPERATOR_PEER)
    with pytest.raises(DrawingMachineError, match="consumed"):
        replace(pending, status=ApprovalStatus.CONSUMED, status_changed_at=NOW)
    with pytest.raises(DrawingMachineError, match="non-consumed"):
        replace(
            pending,
            status=ApprovalStatus.SUPERSEDED,
            status_changed_at=NOW,
            consumer=OPERATOR_PEER,
        )
    with pytest.raises(DrawingMachineError, match="purpose"):
        replace(pending, purpose=ApprovalPurpose.STREAM)
    with pytest.raises(DrawingMachineError, match="duration"):
        replace(pending, expires_at=NOW)


def test_binding_and_evidence_json_are_closed_and_reject_invalid_action_phase_disposition() -> None:
    current = binding()
    assert MachineApprovalBinding.from_json(current.to_json()) == current
    projected = evidence()
    assert MachineApprovalEvidenceV1.from_json(projected.to_json()) == projected
    with pytest.raises(DrawingMachineError, match="prior phase"):
        binding(required_prior_phase=MachinePhase.AWAITING_STREAM_APPROVAL)
    with pytest.raises(DrawingMachineError, match="disposition"):
        binding(recovery_disposition=RecoveryDisposition.RELEASE)
    with pytest.raises(DrawingMachineError, match="requires a disposition"):
        binding(MachineAction.RECOVER, recovery_disposition=None)
    with pytest.raises(DrawingMachineError, match="approvable"):
        MachineApprovalBinding(
            **{
                **binding().to_python(),
                "action": MachineAction.PREPARE_SESSION,
                "required_prior_phase": MachinePhase.PREPARING_SESSION,
            }
        )


def test_status_transition_functions_require_exact_contract_types() -> None:
    pending = challenge()
    with pytest.raises(DrawingMachineError, match="exact challenge"):
        consume_approval(  # type: ignore[arg-type]
            object(),
            current_binding=binding(),
            consumer=OPERATOR_PEER,
            operator_principal=OPERATOR,
            wall_now=NOW,
            monotonic_now=1.0,
        )
    with pytest.raises(DrawingMachineError, match="operator binding"):
        consume(pending, operator_principal=KernelMachinePrincipal(2300, 2300))
    first = consume(pending)
    with pytest.raises(DrawingMachineError, match="challenge"):
        apply_approval_decision(challenge(challenge_id=NEXT_CHALLENGE_ID), first)


def test_branch_matrix_rejects_noncanonical_uuid_and_malformed_session_json() -> None:
    payload = challenge().to_json()
    payload["challenge_id"] = CHALLENGE_ID.upper()
    with pytest.raises(DrawingMachineError, match="canonical UUID4"):
        challenge_from_json(payload)

    for field, value, message in [
        ("mpos", [0.0, 0.0], "XYZ array"),
        ("mpos", (0.0, 0.0, 0.0), "XYZ array"),
        ("stabilized", 1, "boolean"),
    ]:
        payload = challenge().to_json()
        evidence_payload = dict(payload["evidence"])  # type: ignore[arg-type]
        session_payload = dict(evidence_payload["session"])  # type: ignore[arg-type]
        session_payload[field] = value
        evidence_payload["session"] = session_payload
        payload["evidence"] = evidence_payload
        with pytest.raises(DrawingMachineError, match=message):
            challenge_from_json(payload)


def test_branch_matrix_rejects_nested_schema_phase_and_session_binding_mismatches() -> None:
    with pytest.raises(DrawingMachineError, match="schema version"):
        replace(binding(), schema_version=2)
    with pytest.raises(DrawingMachineError, match="schema version"):
        replace(evidence(), schema_version=2)
    with pytest.raises(DrawingMachineError, match="prior phase"):
        replace(evidence(), required_prior_phase=MachinePhase.AWAITING_STREAM_APPROVAL)
    with pytest.raises(DrawingMachineError, match="execution/session"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(session=session(execution_id="execution-other")),
            issued_at=NOW,
            monotonic_now=1.0,
            ttl_seconds=60,
        )
    with pytest.raises(DrawingMachineError, match="observed after"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(session=session(observed_at=NOW + timedelta(microseconds=1))),
            issued_at=NOW,
            monotonic_now=1.0,
            ttl_seconds=60,
        )
    with pytest.raises(DrawingMachineError, match="execution/session"):
        issue_approval(
            challenge_id=CHALLENGE_ID,
            binding=binding(),
            requester=AUTOMATION_PEER,
            automation_principal=AUTOMATION,
            operator_principal=OPERATOR,
            evidence=evidence(session=session(machine_session_epoch="session-other")),
            issued_at=NOW,
            monotonic_now=1.0,
            ttl_seconds=60,
        )
    with pytest.raises(DrawingMachineError, match="fixed approval purpose"):
        approvals_module._purpose_for(MachineAction.PREPARE_SESSION)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"binding": object()}, "exact MachineApprovalBinding"),
        ({"requester": object()}, "exact AuthenticatedMachinePrincipal"),
        ({"operator_principal": object()}, "exact KernelMachinePrincipal"),
        ({"evidence": object()}, "exact MachineApprovalEvidenceV1"),
        ({"status_changed_at": NOW - timedelta(seconds=1)}, "precede"),
        ({"consumer": object()}, "exact AuthenticatedMachinePrincipal"),
        (
            {
                "status": ApprovalStatus.CONSUMED,
                "status_changed_at": NOW + timedelta(seconds=1),
                "consumer": AuthenticatedMachinePrincipal(2201, 2200, 5),
            },
            "configured operator",
        ),
        (
            {
                "status": ApprovalStatus.CONSUMED,
                "status_changed_at": NOW + timedelta(seconds=60),
                "consumer": OPERATOR_PEER,
            },
            "precede wall expiry",
        ),
        ({"status": ApprovalStatus.EXPIRED}, "decision time"),
    ],
)
def test_branch_matrix_rejects_impossible_challenge_fields(changes: dict[str, object], message: str) -> None:
    with pytest.raises(DrawingMachineError, match=message):
        replace(challenge(), **changes)  # type: ignore[arg-type]


def test_branch_matrix_rejects_impossible_decisions() -> None:
    pending = challenge()
    consumed = apply_approval_decision(pending, consume(pending))
    with pytest.raises(DrawingMachineError, match="exact challenge"):
        MachineApprovalDecision(object(), pending)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="expected state"):
        MachineApprovalDecision(consumed, consumed)
    with pytest.raises(DrawingMachineError, match="result must be terminal"):
        MachineApprovalDecision(pending, pending)
    other_pending = challenge(challenge_id=NEXT_CHALLENGE_ID)
    other_consumed = apply_approval_decision(other_pending, consume(other_pending))
    with pytest.raises(DrawingMachineError, match="mutate challenge authority"):
        MachineApprovalDecision(pending, other_consumed)


def test_branch_matrix_rejects_wrong_exact_issue_and_apply_types() -> None:
    common: dict[str, object] = {
        "challenge_id": CHALLENGE_ID,
        "binding": binding(),
        "requester": AUTOMATION_PEER,
        "automation_principal": AUTOMATION,
        "operator_principal": OPERATOR,
        "evidence": evidence(),
        "issued_at": NOW,
        "monotonic_now": 1.0,
        "ttl_seconds": 60,
    }
    for field, value, message in [
        ("binding", object(), "exact MachineApprovalBinding"),
        ("requester", object(), "exact AuthenticatedMachinePrincipal"),
        ("automation_principal", object(), "exact KernelMachinePrincipal"),
        ("operator_principal", object(), "exact KernelMachinePrincipal"),
        ("evidence", object(), "exact MachineApprovalEvidenceV1"),
    ]:
        arguments = {**common, field: value}
        with pytest.raises(DrawingMachineError, match=message):
            issue_approval(**arguments)  # type: ignore[arg-type]
    with pytest.raises(DrawingMachineError, match="exact decision"):
        apply_approval_decision(challenge(), object())  # type: ignore[arg-type]
