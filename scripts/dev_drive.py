"""Headless WS3 driving run: rovers + drones + kinematic actors; prints positions and the
real-time factor (RTF), optionally saves camera PNGs.

    PYTHONPATH=. .venv/bin/python scripts/dev_drive.py                      # fixture
    PYTHONPATH=. .venv/bin/python scripts/dev_drive.py --world world/out    # real WS1 data
    ... --secs 60 --actors 250 --frames /tmp/claude-1000/ws3_frames

No brain is used here (no checkpoints are opened); this is a physics/perf harness only.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.config import settings  # noqa: E402
from sim.bodies import BodyWorld  # noqa: E402
from sim.schemas import ActorState, FsmState, HazardProp  # noqa: E402


class SyntheticTraffic:
    """Stand-in for the SUMO bridge (WS2) in this harness: actors moving along drive edges."""

    def __init__(self, planner, n: int, rng: random.Random) -> None:
        self.rng = rng
        self.edges = [e for e in planner.edges.values() if len(e["shape"]) >= 2]
        self.planner = planner
        self.actors = []
        for i in range(n):
            kind = "pedestrian" if i % 5 in (0, 1) else rng.choice(["car", "car", "auto", "bike", "bus"])
            self.actors.append(self._spawn(f"a{i}", kind))

    def _spawn(self, aid: str, kind: str) -> dict:
        e = self.rng.choice(self.edges)
        sh = e["shape"]
        seg = np.linalg.norm(np.diff(sh, axis=0), axis=1)
        return {"id": aid, "kind": kind, "edge": e, "s": self.rng.uniform(0, seg.sum()),
                "cum": np.concatenate([[0], np.cumsum(seg)]),
                "v": 1.3 if kind == "pedestrian" else self.rng.uniform(4, 9)}

    def step(self, dt: float) -> list[ActorState]:
        out = []
        for a in self.actors:
            a["s"] += a["v"] * dt
            if a["s"] >= a["cum"][-1]:
                a.update(self._spawn(a["id"], a["kind"]))
            sh, cum = a["edge"]["shape"], a["cum"]
            i = min(int(np.searchsorted(cum, a["s"], side="right") - 1), len(sh) - 2)
            t = (a["s"] - cum[i]) / max(cum[i + 1] - cum[i], 1e-9)
            p0, p1 = sh[i], sh[i + 1]
            d = (p1 - p0) / max(np.linalg.norm(p1 - p0), 1e-9)
            n = np.array([-d[1], d[0]])
            hw = self.planner.half_width(a["edge"]["id"])
            off = hw + 1.5 if a["kind"] == "pedestrian" else hw * 0.2
            x, y = p0 + t * (p1 - p0) + n * off
            out.append(ActorState(id=a["id"], kind=a["kind"], x=float(x), y=float(y),
                                  yaw=math.atan2(d[1], d[0]), speed=a["v"]))
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default=str(ROOT / "tests" / "fixtures"),
                    help="dir with road_graph.json + collision/boxes.json")
    ap.add_argument("--secs", type=float, default=60.0)
    ap.add_argument("--actors", type=int, default=250)
    ap.add_argument("--actor-hz", type=float, default=10.0)
    ap.add_argument("--drones", type=int, default=2)
    ap.add_argument("--mode", default="physics", choices=["physics", "hybrid"])
    ap.add_argument("--frames", default=None, help="dir to save a few camera PNGs")
    ap.add_argument("--print-every", type=float, default=10.0)
    args = ap.parse_args()

    cfg = settings()
    rng = random.Random(cfg["sim"]["seed"])
    wd = Path(args.world)
    t_load = time.perf_counter()
    w = BodyWorld(wd / "collision" / "boxes.json", road_graph=wd / "road_graph.json")
    print(f"world loaded in {time.perf_counter() - t_load:.2f}s: {len(w.static_ids)} boxes, "
          f"{len(w.planner.edges)} drive edges")
    nodes = list(w.planner.G.nodes)

    rovers = []
    for op in cfg["operators"]:
        for k in range(op["rovers"]):
            n0 = w.planner.nodes[rng.choice(nodes)]
            rid = f"R-{len(rovers) + 1:02d}"
            rv = w.add_rover(rid, op["name"], n0, yaw=rng.uniform(-math.pi, math.pi),
                             color=op["color"], mode=args.mode)
            rovers.append(rv)

    def new_goal(rv) -> None:
        for _ in range(10):
            if rv.drive_to(goal_node=rng.choice(nodes), state=FsmState.DELIVERING):
                return

    for rv in rovers:
        new_goal(rv)
        rv.on_arrival = new_goal

    drones = []
    try:
        for d in cfg["drones"][: args.drones]:
            n0 = w.planner.nodes[rng.choice(nodes)]
            dr = w.add_drone(d["id"], d["operator"], (n0[0] + 8.0, n0[1] + 8.0))  # off the road
            n1 = w.planner.nodes[rng.choice(nodes)]
            dr.fly_to(n1)
            drones.append(dr)
    except ImportError as e:
        print(f"(drones unavailable: {e})")

    # one prop of each type so the camera has something to look at
    e0 = rovers[0].route.edge_ids[0] if rovers[0].route and rovers[0].route.edge_ids else None
    if e0:
        sh = w.planner.edge_shape(e0)
        mid = sh[len(sh) // 2]
        w.add_prop(HazardProp(id="P-demo", edge_id=e0, type="road_works", x=float(mid[0]),
                              y=float(mid[1])))

    traffic = SyntheticTraffic(w.planner, args.actors, rng)
    dt = 1.0 / cfg["sim"]["control_hz"]
    actor_every = max(1, round(cfg["sim"]["control_hz"] / args.actor_hz))
    steps = int(args.secs / dt)
    t = 0.0
    timing = {"actors": 0.0, "robots": 0.0, "physics": 0.0}
    wall0 = time.perf_counter()
    for k in range(steps):
        t += dt
        a0 = time.perf_counter()
        if k % actor_every == 0:
            w.upsert_kinematic_actors(traffic.step(dt * actor_every))
        a1 = time.perf_counter()
        for r in w.robots.values():
            r.tick(t)
        a2 = time.perf_counter()
        w.step(dt)
        a3 = time.perf_counter()
        timing["actors"] += a1 - a0
        timing["robots"] += a2 - a1
        timing["physics"] += a3 - a2
        if k % int(args.print_every / dt) == 0 or k == steps - 1:
            wall = time.perf_counter() - wall0
            print(f"--- t={t:6.1f}s  wall={wall:6.2f}s  RTF={t / max(wall, 1e-9):5.2f}x")
            for st in w.robot_states():
                print(f"  {st.id:5s} {st.operator:10s} {st.fsm_state.value:11s} "
                      f"x={st.x:8.1f} y={st.y:8.1f} z={st.z:5.1f} v={st.speed:4.1f}")
    wall = time.perf_counter() - wall0
    print(f"\nsimulated {t:.1f}s in {wall:.2f}s wall -> RTF {t / wall:.2f}x "
          f"({len(rovers)} rovers, {len(drones)} drones, {args.actors} actors, camera off)")
    print("  time split: " + ", ".join(f"{k} {v / wall * 100:.0f}%" for k, v in timing.items()))

    if args.frames:
        out = Path(args.frames)
        out.mkdir(parents=True, exist_ok=True)
        for rid in [rovers[0].id, rovers[1].id] + [d.id for d in drones[:1]]:
            c0 = time.perf_counter()
            png = w.camera_frame(rid)
            (out / f"{rid}.png").write_bytes(png)
            print(f"  saved {out / (rid + '.png')} ({len(png)} B, {time.perf_counter() - c0:.2f}s)")
    w.close()


if __name__ == "__main__":
    main()
