from __future__ import annotations

import pytest

from drawingmachine.domain.machine.models import StreamMilestone
from drawingmachine.domain.machine.phases import (
    MachineAction,
    MachinePhase,
    RecoveryDisposition,
    RecoveryIntent,
)
from drawingmachine.domain.machine.transitions import (
    allowed_phase_transition,
    allowed_recovery_disposition,
    allowed_stream_milestone_transition,
)


@pytest.mark.parametrize("intent", RecoveryIntent)
@pytest.mark.parametrize("prior", MachinePhase)
@pytest.mark.parametrize("action", MachineAction)
@pytest.mark.parametrize("result", MachinePhase)
def test_recovery_phase_graphs_are_exhaustive(
    intent: RecoveryIntent, prior: MachinePhase, action: MachineAction, result: MachinePhase
) -> None:
    pre_stream = {
        (MachinePhase.PREPARING_SESSION, MachineAction.PREPARE_SESSION, MachinePhase.AWAITING_HOME_APPROVAL),
        (MachinePhase.AWAITING_HOME_APPROVAL, MachineAction.HOME, MachinePhase.HOMING),
        (MachinePhase.HOMING, MachineAction.HOME, MachinePhase.AWAITING_ZCAL_APPROVAL),
        (MachinePhase.AWAITING_ZCAL_APPROVAL, MachineAction.ZCAL, MachinePhase.Z_CALIBRATING),
        (MachinePhase.Z_CALIBRATING, MachineAction.ZCAL, MachinePhase.AWAITING_ZCONFIRM_APPROVAL),
        (MachinePhase.AWAITING_ZCONFIRM_APPROVAL, MachineAction.ZCONFIRM, MachinePhase.Z_CONFIRMING),
        (MachinePhase.Z_CONFIRMING, MachineAction.ZCONFIRM, MachinePhase.AWAITING_STREAM_APPROVAL),
        (MachinePhase.AWAITING_STREAM_APPROVAL, MachineAction.STREAM, MachinePhase.STREAMING),
        (MachinePhase.STREAMING, MachineAction.STREAM, MachinePhase.COMPLETED),
        (MachinePhase.STREAMING, MachineAction.STREAM, MachinePhase.AWAITING_HOME_Z_APPROVAL),
        (MachinePhase.AWAITING_HOME_Z_APPROVAL, MachineAction.HOME_Z, MachinePhase.HOMING_Z),
        (MachinePhase.HOMING_Z, MachineAction.HOME_Z, MachinePhase.COMPLETED),
    }
    safe_home = {
        (MachinePhase.PREPARING_SESSION, MachineAction.PREPARE_SESSION, MachinePhase.AWAITING_HOME_APPROVAL),
        (MachinePhase.AWAITING_HOME_APPROVAL, MachineAction.HOME, MachinePhase.HOMING),
        (MachinePhase.HOMING, MachineAction.HOME, MachinePhase.COMPLETED),
    }
    expected = (prior, action, result) in (
        pre_stream
        if intent is RecoveryIntent.PRE_STREAM_RESTART
        else safe_home
        if intent is RecoveryIntent.POST_STREAM_SAFE_HOME
        else set()
    )
    assert allowed_phase_transition(prior, action, result, recovery_intent=intent) is expected


@pytest.mark.parametrize("intent", RecoveryIntent)
@pytest.mark.parametrize("disposition", RecoveryDisposition)
def test_recovery_dispositions_are_exact(intent: RecoveryIntent, disposition: RecoveryDisposition) -> None:
    expected = (
        disposition is RecoveryDisposition.RELEASE
        or (intent is RecoveryIntent.PRE_STREAM_RESTART and disposition is RecoveryDisposition.RESTART_SEQUENCE)
        or (intent is RecoveryIntent.POST_STREAM_SAFE_HOME and disposition is RecoveryDisposition.SAFE_HOME)
    )
    assert allowed_recovery_disposition(intent, disposition) is expected


@pytest.mark.parametrize("prior", StreamMilestone)
@pytest.mark.parametrize("result", StreamMilestone)
def test_stream_milestone_never_skips_or_reverses(prior: StreamMilestone, result: StreamMilestone) -> None:
    order = list(StreamMilestone)
    assert allowed_stream_milestone_transition(prior, result) is (
        order.index(result) in {order.index(prior), order.index(prior) + 1}
    )


def test_all_transition_functions_fail_closed_on_runtime_type_confusion() -> None:
    assert not allowed_phase_transition(
        "PREPARING_SESSION", MachineAction.PREPARE_SESSION, MachinePhase.AWAITING_HOME_APPROVAL, recovery_intent=None
    )
    assert not allowed_phase_transition(
        MachinePhase.PREPARING_SESSION, "PREPARE_SESSION", MachinePhase.AWAITING_HOME_APPROVAL, recovery_intent=None
    )
    assert not allowed_phase_transition(
        MachinePhase.PREPARING_SESSION, MachineAction.PREPARE_SESSION, "AWAITING_HOME_APPROVAL", recovery_intent=None
    )
    assert not allowed_phase_transition(
        MachinePhase.PREPARING_SESSION,
        MachineAction.PREPARE_SESSION,
        MachinePhase.AWAITING_HOME_APPROVAL,
        recovery_intent="none",
    )
    assert not allowed_recovery_disposition("PRE_STREAM_RESTART", RecoveryDisposition.RELEASE)
    assert not allowed_recovery_disposition(RecoveryIntent.PRE_STREAM_RESTART, "RELEASE")
    assert not allowed_stream_milestone_transition("NOT_STARTED", StreamMilestone.NOT_STARTED)
    assert not allowed_stream_milestone_transition(StreamMilestone.NOT_STARTED, "NOT_STARTED")
