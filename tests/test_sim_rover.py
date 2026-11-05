"""WS3 rover tests on the synthetic fixture (tests/fixtures/, INTERFACES.md formats).

The FakeBrain below is TEST-ONLY. It never lives in non-test code (CLAUDE.md: never fake 0G).
"""
from __future__ import annotations

import math
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest

from sim.bodies import BodyWorld
from sim.controllers.planner import RoadPlanner
from sim.schemas import CheckpointRequest, CheckpointResult, FsmState, HazardProp

FIX = Path(__file__).parent / "fixtures"
DT = 0.05


class FakeBrain:
    """Test-only stand-in for common.interfaces.Brain. mode: ok | fail | hang | script."""

    def __init__(self, mode: str = "ok", script: list[bool] | None = None) -> None:
        self.mode = mode
        self.script = list(script or [])
        self.calls: list[CheckpointRequest] = []
        self.pending: list[Future] = []

    def online(self) -> bool:
        return True

    def set_unreachable(self, on: bool) -> None:
        pass

    def submit(self, req: CheckpointRequest) -> "Future[CheckpointResult]":
        self.calls.append(req)
        fut: Future = Future()
        mode = self.mode
        if mode == "script":
            mode = "ok" if self.script.pop(0) else "fail"
        if mode == "ok":
            fut.set_result(CheckpointResult(ok=True, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                            output={"action": "proceed", "reason": "test",
                                                    "trust_report": False}))
        elif mode == "fail":
            fut.set_result(CheckpointResult(ok=False, checkpoint=req.checkpoint,
                                            robot_id=req.robot_id, error="test: unreachable"))
        else:
            fut.set_running_or_notify_cancel()   # like a pool worker mid-call: cannot be cancelled
            self.pending.append(fut)          # never resolves -> local deadline
        return fut


def req(robot_id: str = "R-01", t: float = 0.0) -> CheckpointRequest:
    return CheckpointRequest(robot_id=robot_id, operator="ZipKart", robot_kind="rover",
                             checkpoint="CP-H2", sim_time=t, episode_id="ep-test", seq=1)


@pytest.fixture
def world():
    w = BodyWorld(FIX / "collision" / "boxes.json", road_graph=FIX / "road_graph.json")
    yield w
    w.close()


def run(w: BodyWorld, t0: float, secs: float, until=None) -> float:
    t = t0
    for _ in range(int(round(secs / DT))):
        t += DT
        w.tick(t, DT)
        if until is not None and until():
            break
    return t


def lateral_offset(route, x: float, y: float) -> float:
    """Signed offset of (x,y) from the route centreline (+ = left of travel)."""
    C = route.centerline
    q = np.array([x, y])
    best = (1e18, 0.0)
    for a, b in zip(C[:-1], C[1:]):
        ab = b - a
        L2 = float(np.dot(ab, ab))
        if L2 < 1e-9:
            continue
        t = float(np.clip(np.dot(q - a, ab) / L2, 0, 1))
        d = q - (a + t * ab)
        dist = float(np.linalg.norm(d))
        if dist < best[0]:
            side = math.copysign(1.0, ab[0] * d[1] - ab[1] * d[0])
            best = (dist, side * dist)
    return best[1]


# ---------------------------------------------------------------- planner
def test_planner_blocked_and_left_offset():
    pl = RoadPlanner.from_file(FIX / "road_graph.json")
    assert "w0" not in pl.edges                         # footpath ignored for rovers
    r = pl.plan((-120, -120), (120, -120))
    assert r is not None and r.edge_ids[:3] == ["e0", "e7", "e14"]
    r2 = pl.plan((-120, -120), (120, -120), blocked_edge_ids={"e7"})
    assert r2 is not None and "e7" not in r2.edge_ids
    # eastbound along y=-120: left of travel is +y
    mid = r.polyline[len(r.polyline) // 2]
    assert mid[1] > -120 + 1.0
    r3 = pl.plan((-120, -120), (120, -120), edge_penalty={"e7": 1e4})
    assert "e7" not in r3.edge_ids
    # one-way column (x=120 northbound) is not used southbound
    r4 = pl.plan((120, 120), (120, -120))
    assert not set(r4.edge_ids) & {"e21", "e22", "e23"} or r4.edge_ids == []


# ---------------------------------------------------------------- driving
def test_rover_drives_route_to_goal(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0)
    route = r.drive_to((40, -40), state=FsmState.TO_STORE)
    assert route is not None and len(route.edge_ids) >= 2
    arrivals = []
    r.on_arrival = lambda rv: arrivals.append(rv.id)
    run(world, 0.0, 120.0, until=lambda: r.arrived)
    x, y, *_ = r.pose()
    assert r.arrived and arrivals == ["R-01"]
    assert math.dist((x, y), (40, -40)) < 2.0
    st = r.state()
    assert st.kind == "rover" and st.fsm_state == FsmState.TO_STORE and st.route_edge_ids


def test_lidar_reflex_brakes_before_box(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0)
    route = r.drive_to((40, -120), state=FsmState.DELIVERING)
    # an obstacle on the driving line 30 m ahead (e.g. a crashed car)
    y_line = float(np.interp(-90.0, route.polyline[:, 0], route.polyline[:, 1]))
    world._box([-90.0, y_line, 0.75], [1.0, 1.0, 0.75], 0.0, (1, 0, 0, 1))
    t = run(world, 0.0, 25.0)
    x, y, _, _, v = r.pose()
    assert r.reflex_braking
    assert v < 0.2
    gap = (-90.0 - 1.0) - x            # rover centre to the box face
    assert 0.5 < gap < 4.0, gap
    assert not r.arrived


# ---------------------------------------------------------------- checkpoints
def test_checkpoint_fail_safe_stop_pulls_to_kerb(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0)
    route = r.drive_to((120, -120), state=FsmState.DELIVERING)
    t = run(world, 0.0, 12.0)
    x0, y0, *_ = r.pose()
    brain = FakeBrain("fail")
    called = []
    r.begin_checkpoint(brain, req(t=t), on_ok=lambda rb, res: called.append(res), now=t)
    t = run(world, t, 15.0)
    x, y, _, _, v = r.pose()
    assert r.fsm_state == FsmState.SAFE_STOP and not called
    assert v < 0.1
    assert x - x0 < 15.0                                  # stopped soon after the checkpoint
    hw = 3.2                                              # 2-lane fixture road
    off = lateral_offset(route, x, y)
    assert off > hw - 0.8, off                            # near the LEFT kerb
    assert off > 0.75 * hw + 0.2                          # moved left of the driving line
    assert not r.accepts_unverified_reports
    assert r.replan(blocked_edge_ids={"e7"}) is None      # unverified reports ignored
    assert r.state().fsm_state == FsmState.SAFE_STOP


def test_checkpoint_ok_calls_callback_and_continues(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0)
    r.drive_to((120, -120), state=FsmState.DELIVERING)
    t = run(world, 0.0, 5.0)
    brain = FakeBrain("ok")
    got = []
    r.begin_checkpoint(brain, req(t=t), on_ok=lambda rb, res: got.append(res.output), now=t)
    assert r.fsm_state == FsmState.WAITING_FOR_BRAIN
    t = run(world, t, 0.1)
    assert got == [{"action": "proceed", "reason": "test", "trust_report": False}]
    assert r.fsm_state == FsmState.DELIVERING
    x0 = r.pose()[0]
    run(world, t, 5.0)
    assert r.pose()[0] - x0 > 10.0                        # kept driving


def test_checkpoint_timeout_safe_stop(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0, timeout_s=2.0)
    r.drive_to((120, -120), state=FsmState.DELIVERING)
    t = run(world, 0.0, 5.0)
    brain = FakeBrain("hang")
    r.begin_checkpoint(brain, req(t=t), now=t)
    t = run(world, t, 1.0)
    assert r.fsm_state == FsmState.WAITING_FOR_BRAIN
    assert r.pose()[4] < 1.5                              # decelerating while waiting
    t = run(world, t, 1.5)
    assert r.fsm_state == FsmState.SAFE_STOP
    assert "deadline" in (r.safe_stop_reason or "")
    # a late answer for the timed-out call is ignored (nothing guessed)
    brain.pending[0].set_result(CheckpointResult(ok=True, checkpoint="CP-H2", robot_id="R-01"))
    run(world, t, 1.0)
    assert r.fsm_state == FsmState.SAFE_STOP


def test_safe_stop_retries_and_resumes_only_on_success(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0, retry_s=2.0)
    r.drive_to((120, -120), state=FsmState.DELIVERING)
    t = run(world, 0.0, 5.0)
    brain = FakeBrain("script", script=[False, False, True])
    oks = []
    seqs = iter(range(100, 200))
    factory = lambda rb, now: req(t=now).model_copy(update={"seq": next(seqs)})  # noqa: E731
    r.begin_checkpoint(brain, req(t=t), on_ok=lambda rb, res: oks.append(res), now=t,
                       retry_factory=factory)
    t = run(world, t, 3.0)
    assert r.fsm_state == FsmState.SAFE_STOP and len(brain.calls) == 2   # first + 1 retry
    t = run(world, t, 2.5)
    assert len(brain.calls) == 3 and len(oks) == 1
    assert [c.seq for c in brain.calls] == [1, 100, 101]
    assert r.fsm_state == FsmState.DELIVERING
    x0 = r.pose()[0]
    run(world, t, 6.0)
    assert r.pose()[0] - x0 > 5.0


# ---------------------------------------------------------------- props, actors, camera
def test_props_actors_and_camera_png(world):
    r = world.add_rover("R-01", "ZipKart", (-120, -120), yaw=0.0)
    for i, typ in enumerate(["accident", "road_works", "waterlogging"]):
        ids = world.add_prop(HazardProp(id=f"P{i}", edge_id="e0", type=typ, x=-100 + 12 * i, y=-120))
        assert ids
    assert world.add_prop(HazardProp(id="fake", edge_id="e7", type="road_works", x=0, y=-120,
                                     real=False)) == []
    world.remove_prop("P2")
    from sim.schemas import ActorState
    acts = [ActorState(id=f"v{i}", kind="car" if i % 2 else "pedestrian", x=-60 + i, y=-110)
            for i in range(10)]
    world.upsert_kinematic_actors(acts)
    world.upsert_kinematic_actors(acts[:4])
    assert len(world._actor_body) == 4 and sum(len(v) for v in world._pool.values()) == 6
    world.upsert_kinematic_actors(acts)                    # reuse pooled bodies
    assert sum(len(v) for v in world._pool.values()) == 0
    png = world.camera_frame("R-01")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    assert im.size == (320, 240)
    arr = np.asarray(im)
    assert arr.std() > 5                                   # not a blank frame
