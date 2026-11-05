"""Robot FSM tables (SPEC §8).

Task transitions are driven by the integrator (engine/dispatcher); the pause states
WAITING_FOR_BRAIN and SAFE_STOP are entered/left only by the checkpoint mechanism in
`sim/robot_base.py` (resume happens only after a successful 0G decision).

Rover:  IDLE -> TO_STORE -> LOADING -> DELIVERING -> AT_DOOR -> HANDED_OVER -> RETURNING -> IDLE
Drone:  IDLE -> VERIFYING -> (hover) -> RETURNING -> LANDING -> IDLE
Both:   any task state -> WAITING_FOR_BRAIN -> (ok) task state | (fail/timeout) SAFE_STOP
        SAFE_STOP -> (successful retry) task state
"""
from __future__ import annotations

from sim.schemas import FsmState as S

PAUSE_STATES = frozenset({S.WAITING_FOR_BRAIN, S.SAFE_STOP})

ROVER_TASK_TRANSITIONS: dict[S, frozenset[S]] = {
    S.IDLE: frozenset({S.TO_STORE, S.DELIVERING, S.RETURNING}),
    S.TO_STORE: frozenset({S.LOADING, S.RETURNING, S.IDLE}),
    S.LOADING: frozenset({S.DELIVERING, S.IDLE}),
    S.DELIVERING: frozenset({S.AT_DOOR, S.RETURNING}),
    S.AT_DOOR: frozenset({S.HANDED_OVER, S.RETURNING, S.DELIVERING}),
    S.HANDED_OVER: frozenset({S.RETURNING, S.IDLE}),
    S.RETURNING: frozenset({S.IDLE, S.TO_STORE}),
}

DRONE_TASK_TRANSITIONS: dict[S, frozenset[S]] = {
    S.IDLE: frozenset({S.VERIFYING, S.DELIVERING, S.RETURNING}),
    S.VERIFYING: frozenset({S.VERIFYING, S.RETURNING, S.DELIVERING}),
    S.DELIVERING: frozenset({S.AT_DOOR, S.RETURNING}),
    S.AT_DOOR: frozenset({S.HANDED_OVER, S.RETURNING}),
    S.HANDED_OVER: frozenset({S.RETURNING}),
    S.RETURNING: frozenset({S.LANDING, S.VERIFYING}),
    S.LANDING: frozenset({S.IDLE}),
}


def allowed(kind: str, src: S, dst: S) -> bool:
    """Is a task transition src -> dst legal? (Pause states are handled by the checkpoint logic.)"""
    if src == dst:
        return True
    if dst in PAUSE_STATES or src in PAUSE_STATES:
        return True
    if dst == S.IDLE:            # abort / reset to idle is always allowed
        return True
    table = ROVER_TASK_TRANSITIONS if kind == "rover" else DRONE_TASK_TRANSITIONS
    return dst in table.get(src, frozenset())
