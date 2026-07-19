from __future__ import annotations

from drawingmachine.domain.machine.models import StreamMilestone
from drawingmachine.domain.machine.phases import (
    MachineAction,
    MachinePhase,
    RecoveryDisposition,
    RecoveryIntent,
)

_NORMAL_EDGES = frozenset(
    {
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
)
_PRE_STREAM_SUCCESSOR_EDGES = _NORMAL_EDGES
_POST_STREAM_SAFE_HOME_EDGES = frozenset(
    {
        (MachinePhase.PREPARING_SESSION, MachineAction.PREPARE_SESSION, MachinePhase.AWAITING_HOME_APPROVAL),
        (MachinePhase.AWAITING_HOME_APPROVAL, MachineAction.HOME, MachinePhase.HOMING),
        (MachinePhase.HOMING, MachineAction.HOME, MachinePhase.COMPLETED),
    }
)


def allowed_phase_transition(
    prior: object,
    action: object,
    result: object,
    *,
    recovery_intent: object | None,
) -> bool:
    """Return whether an explicit, already-authorized transition edge is legal."""
    if (
        not isinstance(prior, MachinePhase)
        or not isinstance(action, MachineAction)
        or not isinstance(result, MachinePhase)
    ):
        return False
    if recovery_intent is None:
        return (prior, action, result) in _NORMAL_EDGES
    if not isinstance(recovery_intent, RecoveryIntent):
        return False
    edges = {
        RecoveryIntent.PRE_STREAM_RESTART: _PRE_STREAM_SUCCESSOR_EDGES,
        RecoveryIntent.STREAM_AMBIGUOUS_RELEASE_ONLY: frozenset(),
        RecoveryIntent.POST_STREAM_SAFE_HOME: _POST_STREAM_SAFE_HOME_EDGES,
    }
    return (prior, action, result) in edges[recovery_intent]


def allowed_recovery_disposition(intent: object, disposition: object) -> bool:
    """Fail-closed recovery routing; release never creates a motion successor."""
    if not isinstance(intent, RecoveryIntent) or not isinstance(disposition, RecoveryDisposition):
        return False
    return (
        disposition is RecoveryDisposition.RELEASE
        or (intent is RecoveryIntent.PRE_STREAM_RESTART and disposition is RecoveryDisposition.RESTART_SEQUENCE)
        or (intent is RecoveryIntent.POST_STREAM_SAFE_HOME and disposition is RecoveryDisposition.SAFE_HOME)
    )


def allowed_stream_milestone_transition(prior: object, result: object) -> bool:
    """Allow only idempotent persistence or a one-step irreversible milestone advance."""
    if not isinstance(prior, StreamMilestone) or not isinstance(result, StreamMilestone):
        return False
    order = {
        StreamMilestone.NOT_STARTED: 0,
        StreamMilestone.FIRST_WRITE_POSSIBLE: 1,
        StreamMilestone.STREAM_CONFIRMED: 2,
    }
    return order[result] in {order[prior], order[prior] + 1}
