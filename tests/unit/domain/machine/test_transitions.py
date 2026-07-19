from __future__ import annotations

import pytest

from drawingmachine.domain.machine.phases import MachineAction, MachinePhase, RecoveryIntent
from drawingmachine.domain.machine.transitions import allowed_phase_transition


@pytest.mark.parametrize(
    ("prior", "action", "result"),
    (
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
    ),
)
def test_normal_machine_graph_has_only_explicit_edges(
    prior: MachinePhase,
    action: MachineAction,
    result: MachinePhase,
) -> None:
    assert allowed_phase_transition(prior, action, result, recovery_intent=None)


@pytest.mark.parametrize("prior", MachinePhase)
@pytest.mark.parametrize("action", MachineAction)
@pytest.mark.parametrize("result", MachinePhase)
def test_normal_machine_graph_rejects_every_unlisted_edge(
    prior: MachinePhase,
    action: MachineAction,
    result: MachinePhase,
) -> None:
    expected = (prior, action, result) in {
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
    assert allowed_phase_transition(prior, action, result, recovery_intent=None) is expected


def test_recovery_subgraphs_cannot_restore_or_repeat_stream() -> None:
    assert allowed_phase_transition(
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.HOME,
        MachinePhase.HOMING,
        recovery_intent=RecoveryIntent.PRE_STREAM_RESTART,
    )
    assert not allowed_phase_transition(
        MachinePhase.AWAITING_HOME_APPROVAL,
        MachineAction.HOME,
        MachinePhase.HOMING,
        recovery_intent=RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY,
    )
    assert allowed_phase_transition(
        MachinePhase.HOMING,
        MachineAction.HOME,
        MachinePhase.COMPLETED,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
    )
    assert not allowed_phase_transition(
        MachinePhase.HOMING,
        MachineAction.ZCAL,
        MachinePhase.AWAITING_ZCAL_APPROVAL,
        recovery_intent=RecoveryIntent.POST_STREAM_SAFE_HOME,
    )
