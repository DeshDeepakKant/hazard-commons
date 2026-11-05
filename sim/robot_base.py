"""Shared robot core: FSM bookkeeping, the generic 0G checkpoint mechanism and SAFE_STOP.

Brain vs spinal cord (SPEC §5): this module never decides anything. It only
  * parks the robot (WAITING_FOR_BRAIN) while a checkpoint Future is in flight,
  * hands a validated `ok=True` CheckpointResult to the integrator's callback,
  * enters SAFE_STOP on `ok=False` or when the local deadline (settings brain.timeout_s) passes,
  * in SAFE_STOP retries a real brain call every `retry_s` and resumes only on a successful one.
There is no fallback decision path. Nothing is guessed.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from common.config import settings
from sim.fsm import allowed as fsm_allowed
from sim.schemas import CheckpointRequest, CheckpointResult, FsmState, RobotState

if TYPE_CHECKING:
    from common.interfaces import Brain

# Integrator callbacks. `on_ok(robot, result)` applies the decision (engine-side logic).
OnOk = Callable[["RobotBase", CheckpointResult], None]
# `on_result(robot, result)` sees every resolved result (ok or not), e.g. for logging/DecisionEvent.
OnResult = Callable[["RobotBase", CheckpointResult], None]
# `retry_factory(robot, now) -> CheckpointRequest` builds a fresh request (new seq, new frame) for
# SAFE_STOP retries. Return None to skip this retry slot.
RetryFactory = Callable[["RobotBase", float], Optional[CheckpointRequest]]

_PAUSE_STATES = (FsmState.WAITING_FOR_BRAIN, FsmState.SAFE_STOP)


@dataclass
class _Pending:
    brain: Any
    req: CheckpointRequest
    fut: "Future[CheckpointResult]"
    deadline: float
    is_retry: bool


@dataclass
class CheckpointSession:
    brain: Any
    req: CheckpointRequest
    on_ok: Optional[OnOk]
    on_result: Optional[OnResult]
    retry_factory: Optional[RetryFactory]
    attempts: int = 0
    pending: Optional[_Pending] = None
    next_retry_t: float = 0.0
    history: list[CheckpointResult] = field(default_factory=list)


class RobotBase:
    kind: str = "rover"

    def __init__(self, robot_id: str, operator: str, *, timeout_s: Optional[float] = None,
                 retry_s: float = 10.0) -> None:
        self.id = robot_id
        self.operator = operator
        self.fsm_state: FsmState = FsmState.IDLE
        self.resume_state: FsmState = FsmState.IDLE   # task state to return to after a checkpoint
        self.order_id: Optional[str] = None
        self.route_edge_ids: list[str] = []
        self.battery: float = 1.0
        self.note: Optional[str] = None
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else settings()["brain"]["timeout_s"])
        self.retry_s = float(retry_s)
        self.now: float = 0.0
        self.cp: Optional[CheckpointSession] = None
        self.safe_stop_reason: Optional[str] = None
        self.last_result: Optional[CheckpointResult] = None
        self.fsm_warnings: list[str] = []

    # ------------------------------------------------------------------ FSM
    def set_state(self, state: FsmState, note: Optional[str] = None) -> None:
        """Integrator sets task states (TO_STORE, LOADING, ...).

        While paused on the brain the requested state only becomes the state to resume into.
        Transitions not in sim/fsm.py tables are applied but recorded in `fsm_warnings`.
        """
        src = self.resume_state if self.fsm_state in _PAUSE_STATES else self.fsm_state
        if not fsm_allowed(self.kind, src, state):
            self.fsm_warnings.append(f"{self.now:.1f}s {src.value}->{state.value}")
        if self.fsm_state in _PAUSE_STATES and state not in _PAUSE_STATES:
            # a task transition requested while paused becomes the state to resume into
            self.resume_state = state
            return
        self.fsm_state = state
        if state not in _PAUSE_STATES:
            self.resume_state = state
        if note is not None:
            self.note = note

    @property
    def paused(self) -> bool:
        return self.fsm_state in _PAUSE_STATES

    @property
    def in_safe_stop(self) -> bool:
        return self.fsm_state == FsmState.SAFE_STOP

    @property
    def accepts_unverified_reports(self) -> bool:
        """While in SAFE_STOP the robot ignores unverified hazard reports (SPEC §8)."""
        return not self.in_safe_stop

    # ------------------------------------------------------------------ checkpoints
    def begin_checkpoint(self, brain: "Brain", req: CheckpointRequest, on_ok: Optional[OnOk] = None,
                         *, on_result: Optional[OnResult] = None,
                         retry_factory: Optional[RetryFactory] = None,
                         now: Optional[float] = None) -> bool:
        """Submit one checkpoint. The robot slows to a stop in WAITING_FOR_BRAIN until it resolves.

        Returns False (and does nothing) if a checkpoint call is already in flight.
        """
        if self.cp is not None and self.cp.pending is not None:
            return False
        t = self.now if now is None else now
        if not self.paused:
            self.resume_state = self.fsm_state
        self.cp = CheckpointSession(brain=brain, req=req, on_ok=on_ok, on_result=on_result,
                                    retry_factory=retry_factory)
        self._submit(req, t, is_retry=False)
        if not self.in_safe_stop:
            self.fsm_state = FsmState.WAITING_FOR_BRAIN
            self.note = f"waiting for 0G ({req.checkpoint})"
        self._on_pause_begin()
        return True

    def _submit(self, req: CheckpointRequest, t: float, is_retry: bool) -> None:
        assert self.cp is not None
        self.cp.attempts += 1
        fut = self.cp.brain.submit(req)
        self.cp.pending = _Pending(self.cp.brain, req, fut, t + self.timeout_s, is_retry)

    def _poll_checkpoint(self, t: float) -> None:
        cp = self.cp
        if cp is None:
            return
        p = cp.pending
        if p is not None:
            if p.fut.done():
                cp.pending = None
                try:
                    res = p.fut.result()
                except Exception as e:   # Brain contract says never raises; be defensive anyway
                    res = CheckpointResult(ok=False, checkpoint=p.req.checkpoint,
                                           robot_id=self.id, error=f"future raised: {e!r}")
                self._resolve(res, t)
            elif t >= p.deadline:
                p.fut.cancel()           # a late answer is ignored: never act on a stale decision
                cp.pending = None
                res = CheckpointResult(ok=False, checkpoint=p.req.checkpoint, robot_id=self.id,
                                       error=f"local deadline {self.timeout_s:.1f}s exceeded")
                self._resolve(res, t)
            return
        if self.in_safe_stop and t >= cp.next_retry_t:
            req = self._retry_request(t)
            if req is not None:
                cp.req = req
                self._submit(req, t, is_retry=True)
                self.note = f"SAFE_STOP: retrying 0G ({req.checkpoint}, try {cp.attempts})"
            else:
                cp.next_retry_t = t + self.retry_s

    def _retry_request(self, t: float) -> Optional[CheckpointRequest]:
        cp = self.cp
        assert cp is not None
        if cp.retry_factory is not None:
            return cp.retry_factory(self, t)
        ctx = dict(cp.req.context)
        ctx["retry_attempt"] = cp.attempts
        return cp.req.model_copy(update={"sim_time": t, "context": ctx})

    def _resolve(self, res: CheckpointResult, t: float) -> None:
        cp = self.cp
        assert cp is not None
        self.last_result = res
        cp.history.append(res)
        if cp.on_result is not None:
            cp.on_result(self, res)
        if res.ok:
            self.cp = None
            self.safe_stop_reason = None
            self.fsm_state = self.resume_state
            self.note = f"0G ok ({res.checkpoint})"
            self._on_pause_end()
            if cp.on_ok is not None:
                cp.on_ok(self, res)      # engine applies the decision (may set route/state)
        else:
            cp.next_retry_t = t + self.retry_s
            self.enter_safe_stop(res.error or "brain returned ok=False")

    def enter_safe_stop(self, reason: str) -> None:
        if not self.in_safe_stop:
            if self.fsm_state != FsmState.WAITING_FOR_BRAIN:
                self.resume_state = self.fsm_state
            self.fsm_state = FsmState.SAFE_STOP
            self._on_safe_stop()
        self.safe_stop_reason = reason
        self.note = f"SAFE_STOP: {reason}"[:120]

    # ------------------------------------------------------------------ hooks for bodies
    def _on_pause_begin(self) -> None: ...
    def _on_pause_end(self) -> None: ...
    def _on_safe_stop(self) -> None: ...

    # ------------------------------------------------------------------ export
    def pose(self) -> tuple[float, float, float, float, float]:
        """(x, y, z, yaw, speed)"""
        raise NotImplementedError

    def state(self) -> RobotState:
        x, y, z, yaw, v = self.pose()
        return RobotState(id=self.id, operator=self.operator, kind=self.kind,  # type: ignore[arg-type]
                          x=x, y=y, z=z, yaw=yaw, speed=v, fsm_state=self.fsm_state,
                          order_id=self.order_id, route_edge_ids=list(self.route_edge_ids),
                          battery=self.battery, note=self.note)
