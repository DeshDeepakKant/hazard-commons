"""Drone: real cf2x + vendored DSLPID flies to 40 m / a target, SAFE_STOP hover+land, checkpoint resume.

The fake brain below is TEST-ONLY. It never lives in non-test code.
"""
from __future__ import annotations

from concurrent.futures import Future

import numpy as np
import pybullet as p
import pytest

from sim.drone import Drone
from sim.schemas import CheckpointRequest, CheckpointResult, FsmState

DT_PHYS = 1.0 / 240.0
SUBSTEPS = 12   # 20 Hz control


class _FakeBrain:
    """Test-only brain. `results` is consumed per submit(); None => leave the Future pending."""

    def __init__(self, results: list[bool | None]):
        self.results = list(results)
        self.calls: list[CheckpointRequest] = []
        self.futures: list[Future] = []

    def submit(self, req: CheckpointRequest) -> "Future[CheckpointResult]":
        self.calls.append(req)
        fut: Future = Future()
        ok = self.results.pop(0) if self.results else False
        if ok is not None:
            fut.set_result(_result(req, ok))
        self.futures.append(fut)
        return fut

    def online(self) -> bool:
        return True

    def set_unreachable(self, on: bool) -> None: ...


def _result(req: CheckpointRequest, ok: bool) -> CheckpointResult:
    return CheckpointResult(ok=ok, checkpoint=req.checkpoint, robot_id=req.robot_id,
                            output={"verdict": "confirm", "observed_type": "accident",
                                    "confidence": 0.9} if ok else None,
                            error=None if ok else "test: brain failure")


def _req(t: float) -> CheckpointRequest:
    return CheckpointRequest(robot_id="D-01", operator="ZipKart", robot_kind="drone",
                             checkpoint="CP-V", sim_time=t, episode_id="ep-test", seq=1)


class _World:
    def __init__(self):
        self.c = p.connect(p.DIRECT)
        p.setGravity(0, 0, -9.81, physicsClientId=self.c)
        p.setTimeStep(DT_PHYS, physicsClientId=self.c)
        plane = p.createCollisionShape(p.GEOM_PLANE, physicsClientId=self.c)
        p.createMultiBody(0, plane, physicsClientId=self.c)
        self.t = 0.0

    def run(self, drone: Drone, seconds: float, until=None) -> bool:
        for _ in range(int(round(seconds * 20))):
            for _ in range(SUBSTEPS):
                drone.physics_substep(DT_PHYS)
                p.stepSimulation(physicsClientId=self.c)
            self.t += SUBSTEPS * DT_PHYS
            drone.tick(self.t)
            if until is not None and until():
                return True
        return False

    def close(self):
        p.disconnect(self.c)


@pytest.fixture
def world():
    w = _World()
    yield w
    w.close()


def test_drone_reaches_cruise_alt_and_target(world):
    d = Drone(world.c, "D-01", "ZipKart", (0.0, 0.0), retry_s=3.0)
    assert d.landed
    d.fly_to((48.0, 36.0))                         # 60 m away, diagonal
    assert d.fsm_state == FsmState.VERIFYING
    assert world.run(d, 15.0, until=lambda: d.pose()[2] > 39.0)
    assert world.run(d, 30.0, until=lambda: d.at_target)
    x, y, z, yaw, v = d.pose()
    assert abs(z - 40.0) < 1.5
    assert np.hypot(x - 48.0, y - 36.0) < 2.0
    st = d.state()
    assert st.kind == "drone" and st.fsm_state == FsmState.VERIFYING and st.z > 38
    eye, tgt, up = d.camera_pose()
    assert tgt[2] < eye[2] < z                     # looks down, from under the body
    # return home and land -> IDLE
    d.return_home()
    assert d.fsm_state == FsmState.RETURNING
    assert world.run(d, 60.0, until=lambda: d.landed)
    x, y, z, *_ = d.pose()
    assert z < 0.3 and np.hypot(x, y) < 2.0
    assert d.fsm_state == FsmState.IDLE


def test_drone_safe_stop_hovers_then_lands(world):
    d = Drone(world.c, "D-01", "ZipKart", (0.0, 0.0), retry_s=3.0)
    d.fly_to((0.0, 0.0), alt=15.0)
    assert world.run(d, 15.0, until=lambda: d.at_target)
    brain = _FakeBrain([False, False, False, False, False, False])
    called = []
    assert d.begin_checkpoint(brain, _req(world.t), on_ok=lambda r, res: called.append(res),
                              now=world.t)
    assert d.fsm_state == FsmState.WAITING_FOR_BRAIN
    world.run(d, 0.1)
    assert d.fsm_state == FsmState.SAFE_STOP
    assert not d.accepts_unverified_reports
    world.run(d, 3.0)
    assert not d.landed and d.pose()[2] > 13.0     # hovering first
    assert world.run(d, 30.0, until=lambda: d.landed)
    assert d.pose()[2] < 0.3
    world.run(d, 4.0)                              # retries are real brain calls; all fail
    assert d.fsm_state == FsmState.SAFE_STOP and d.landed
    assert len(brain.calls) >= 2 and not called


def test_drone_checkpoint_ok_continues_mission(world):
    d = Drone(world.c, "D-01", "ZipKart", (0.0, 0.0), retry_s=3.0)
    d.fly_to((40.0, 0.0), alt=20.0)
    assert world.run(d, 15.0, until=lambda: d.pose()[0] > 8.0)   # cruising at speed
    brain = _FakeBrain([None])                     # pending until we resolve it
    called = []
    d.begin_checkpoint(brain, _req(world.t), on_ok=lambda r, res: called.append(res), now=world.t)
    world.run(d, 4.0)
    assert d.fsm_state == FsmState.WAITING_FOR_BRAIN
    assert d.pose()[4] < 1.0                       # holding position while 0G decides
    brain.futures[0].set_result(_result(brain.calls[0], True))
    world.run(d, 0.1)
    assert called and called[0].ok
    assert d.fsm_state == FsmState.VERIFYING
    assert world.run(d, 30.0, until=lambda: d.at_target)
    assert abs(d.pose()[0] - 40.0) < 2.0


def test_drone_resumes_after_safe_stop_on_successful_retry(world):
    d = Drone(world.c, "D-01", "ZipKart", (0.0, 0.0), retry_s=3.0)
    d.fly_to((30.0, 0.0), alt=10.0)
    assert world.run(d, 6.0, until=lambda: d.pose()[2] > 5.0)
    brain = _FakeBrain([False, None])              # fail, then a retry we resolve ok later
    called = []
    d.begin_checkpoint(brain, _req(world.t), on_ok=lambda r, res: called.append(res), now=world.t)
    assert world.run(d, 30.0, until=lambda: d.landed)
    assert d.fsm_state == FsmState.SAFE_STOP
    assert world.run(d, 5.0, until=lambda: len(brain.calls) >= 2)
    assert d.fsm_state == FsmState.SAFE_STOP       # retry in flight: still stopped
    brain.futures[1].set_result(_result(brain.calls[1], True))
    world.run(d, 0.1)
    assert called and d.fsm_state == FsmState.VERIFYING and not d.landed
    assert world.run(d, 40.0, until=lambda: d.at_target)
    x, y, z, *_ = d.pose()
    assert np.hypot(x - 30.0, y) < 2.0 and abs(z - 10.0) < 1.5
