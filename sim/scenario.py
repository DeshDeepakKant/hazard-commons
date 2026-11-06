"""Demo Scenario Engine (SPEC §12): deterministic, engineered so the story always unfolds.

Everything below is placed from the road graph + the seeded engine state, so the same seed gives
the same run. Decisions themselves always come from the brain (0G); the scenario only sets the
stage (orders, props, the fake report, the outage toggle).

  t = 0.5s  Orders: MinuteMart, ZipKart and DashKirana each get a delivery whose shortest route
            crosses the ACCIDENT edge A (chosen at start: an edge ~60-200 m out on a MinuteMart
            route that the other two operators' routes also cross). A 2nd MinuteMart order
            crossing A follows at t≈55 s.
  t = 30s   ACCIDENT prop spawns on A ~30 m ahead of the MinuteMart rover -> CP-H1 (0G) ->
            reportHazard -> nearest free drone of ANOTHER operator (ZipKart D-01) flies there ->
            CP-V (0G) -> verifyHazard -> Confirmed -> SUMO closes A -> every rover whose remaining
            route crosses A (all 3 operators) hits CP-H2 (0G) and reroutes.
  t = 90s   WATERLOGGING prop ~40 m ahead of a moving rover (ZipKart/DashKirana preferred) ->
            CP-H1: 0G decides passable_for by body type (rover vs drone) -> report -> rival drone
            verifies.
  t = 150s  DashKirana posts a FAKE road_works report (no prop, nothing physical) on a rival
            rover's route -> a rival drone verifies with a clear-road frame -> CP-V dispute ->
            Rejected, stake slashed. Unverified reports never trigger CP-H2, so rivals keep
            their route.
  t = 200s  0G outage. Road works appear ahead of two moving rovers: their CP-H1 calls fail ->
            SAFE_STOP (pull over, nothing guessed). t = 220s restore -> the SAFE_STOP retry gets a
            real 0G decision -> resume.
  t = 235s  The confirmed accident is cleared on-chain -> SUMO edge reopens.

LOOP mode (optional, off by default: `Scenario(..., loop_every_s=360, loop_max_cycles=5)`, CLI
`scripts/run_demo.py --loop`). Every `loop_every_s` of sim time (measured from the start of each
cycle) the previous cycle is reset locally, with no extra chain tx: its unfired beats are dropped,
an outage still on is ended, its props are removed, queued/dispatched verifications of them are
dropped (those drones fly home), and its hazards are retired from this run's live map
(`Engine.retire_run_hazards`: edges unblocked, SUMO closures reopened). The next cycle starts
once paused robots have recovered on their own and drones are landed/idle (at most
`settle_max_s`), then re-arms the same beats offset to its start, on a different accident edge
(chosen from the seed and cycle index) and avoiding the previous cycle's edges. Prop ids and
event names carry a `-cN` / `#cN` suffix from cycle 2 on. Cycle 1 is exactly the non-loop story.
"""
from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

import numpy as np

from server.bus import BUS
from sim.schemas import FsmState, HazardProp

if TYPE_CHECKING:
    from sim.controllers.planner import Route
    from sim.engine import Engine
    from sim.robot import Rover

log = logging.getLogger("hazard.scenario")

MOVING = (FsmState.DELIVERING, FsmState.TO_STORE, FsmState.RETURNING)
OPS = ("ZipKart", "MinuteMart", "DashKirana")

ITEMS = {
    "MinuteMart": (["Milk 1L", "Bread", "Eggs (6)"], "Aanya Sharma"),
    "ZipKart": (["Instant noodles", "Chips", "Cold drink 2L"], "Rohan Mehta"),
    "DashKirana": (["Chai masala", "Curd 400g", "Tomatoes 500g"], "Priya Patel"),
}


# ------------------------------------------------------------------ route geometry helpers
def _cum(route: "Route") -> np.ndarray:
    c = route.centerline
    return np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(c, axis=0), axis=1))])


def along(route: "Route", xy: Sequence[float]) -> float:
    """Distance along the route centreline of the projection of xy."""
    c = route.centerline
    cum = _cum(route)
    p = np.asarray(xy[:2], dtype=float)
    best, s = 1e18, 0.0
    for i in range(len(c) - 1):
        a, b = c[i], c[i + 1]
        ab = b - a
        L2 = float(np.dot(ab, ab))
        t = 0.0 if L2 < 1e-12 else float(np.clip(np.dot(p - a, ab) / L2, 0.0, 1.0))
        d = float(np.linalg.norm(a + t * ab - p))
        if d < best:
            best, s = d, float(cum[i] + t * math.sqrt(L2))
    return s


def point_at(route: "Route", s: float) -> tuple[float, float]:
    c = route.centerline
    cum = _cum(route)
    s = float(np.clip(s, 0.0, cum[-1]))
    i = int(np.searchsorted(cum, s, side="right") - 1)
    i = min(max(i, 0), len(c) - 2)
    L = cum[i + 1] - cum[i]
    t = 0.0 if L < 1e-9 else (s - cum[i]) / L
    q = c[i] + t * (c[i + 1] - c[i])
    return float(q[0]), float(q[1])


def edge_spans(route: "Route") -> list[tuple[str, float, float]]:
    """[(edge_id, s_start, s_end)] along the route centreline."""
    cum = _cum(route)
    ei = route.edge_index
    out: list[tuple[str, float, float]] = []
    for k, eid in enumerate(route.edge_ids):
        idx = np.nonzero(ei == k)[0]
        if len(idx) == 0:
            continue
        lo = int(idx[0]) - 1 if idx[0] > 0 else int(idx[0])   # edge starts at the previous vertex
        out.append((eid, float(cum[max(lo, 0)]), float(cum[int(idx[-1])])))
    return out


class Scenario:
    """Deterministic scenario driver for the live demo and video recording."""

    def __init__(self, engine: "Engine", seed: int = 7, loop_every_s: Optional[float] = None,
                 loop_max_cycles: int = 5) -> None:
        self.engine = engine
        self.engine.scenario = self
        self.seed = seed
        self.rng = random.Random(seed)

        self.events: list[tuple[float, Callable[[], None], str]] = []
        self.executed: set[str] = set()

        # loop mode (None = play the story once, exactly as before)
        self.loop_every_s: Optional[float] = float(loop_every_s) if loop_every_s else None
        self.loop_max_cycles = max(1, int(loop_max_cycles))
        self.settle_max_s = 30.0          # max wait for paused robots / flying drones at a reset
        self.cycle = 0                    # 0-based; shown to humans as cycle + 1
        self.cycle_start_t = 0.0
        self.cycle_props: list[HazardProp] = []      # props placed by the current cycle
        self.avoid_edges: set[str] = set()           # previous cycle's edges (never reused next)
        self.past_accident_edges: list[str] = []
        self.cycles: list[dict[str, Any]] = []       # per-cycle record (inspection / tests)
        self._pending_start: Optional[tuple[int, float]] = None
        self._closing_props: list[HazardProp] = []   # previous cycle's props, until next start
        self._outage_on = False
        self._edge_use: dict[str, dict[str, list[tuple[str, float]]]] = {}
        self._accident_ranking: list[str] = []

        self.accident_prop: Optional[HazardProp] = None
        self.waterlogging_prop: Optional[HazardProp] = None
        self.fake_prop: Optional[HazardProp] = None
        self.outage_props: list[HazardProp] = []
        self.fake_report_future: Any = None

        # the engineered plan (computed from the road graph)
        self.accident_edge: Optional[str] = None
        self.plan_homes: dict[str, list[str]] = {}
        self.mm_rover_id: Optional[str] = None
        self._plan()
        self._schedule_events()
        if self.loop_every_s:
            if self.loop_every_s < 240.0:
                log.warning("[scenario] loop period %.0f s is shorter than one story (~236 s): "
                            "late beats of each cycle are dropped at the next reset",
                            self.loop_every_s)
            self.at(0.0, self._announce_cycle, "cycle_start")
            self._schedule_next_boundary()

    # ------------------------------------------------------------------ planning
    def _store_routes(self) -> dict[tuple[str, str], "Route"]:
        eng = self.engine
        planner = eng.world.planner
        out: dict[tuple[str, str], "Route"] = {}
        for op in OPS:
            store = eng.stores_by_op.get(op)
            if not store:
                continue
            for hid in sorted(eng.homes_by_id):
                h = eng.homes_by_id[hid]
                r = planner.plan(store["entrance_xy"], h["entrance_xy"], goal_node=h.get("entrance_node"))
                if r is not None and r.edge_ids:
                    out[(op, hid)] = r
        return out

    def _plan(self) -> None:
        """Pick accident edge A and the homes whose shortest routes cross it."""
        try:
            routes = self._store_routes()
        except Exception:
            log.exception("[scenario] route table failed; falling back to unplanned scenario")
            routes = {}
        self.routes = routes
        edges = self.engine.world.planner.edges
        # edge -> op -> [(home, s_start)]
        use: dict[str, dict[str, list[tuple[str, float]]]] = {}
        for (op, hid), r in routes.items():
            for eid, s0, s1 in edge_spans(r):
                use.setdefault(eid, {}).setdefault(op, []).append((hid, s0))
        self._edge_use = use
        cands = []
        for eid, d in use.items():
            if "MinuteMart" not in d or edges[eid]["length"] < 30.0:
                continue
            mm_s = min(s for _, s in d["MinuteMart"])
            if not (60.0 <= mm_s <= 200.0):
                continue
            score = (len(d), min(len(d.get("ZipKart", [])), 3) + min(len(d.get("DashKirana", [])), 3),
                     edges[eid]["lanes"], -abs(mm_s - 110.0))
            cands.append((score, eid))
        # stable sort: the head is the first strict maximum, i.e. the original single pick
        self._accident_ranking = [e for _, e in sorted(cands, key=lambda c: c[0], reverse=True)]
        if not self._accident_ranking:
            log.warning("[scenario] no engineered accident edge found; accident will be placed "
                        "ahead of the MinuteMart rover at t=30 s")
            return
        self._set_accident_edge(self._accident_ranking[0])

    def _set_accident_edge(self, A: str) -> None:
        edges = self.engine.world.planner.edges
        self.accident_edge = A
        d = self._edge_use[A]
        for op in OPS:
            homes = d.get(op, [])
            if op == "MinuteMart":
                homes = sorted(homes, key=lambda t: abs(t[1] - 110.0))   # reach A at ~t=30 s
            else:
                homes = sorted(homes, key=lambda t: t[1])
            self.plan_homes[op] = [h for h, _ in homes]
        log.info("[scenario] accident edge %s (%s), homes crossing it: %s", A,
                 edges[A].get("name"), {k: v[:3] for k, v in self.plan_homes.items()})

    def _schedule_events(self, offset: float = 0.0) -> None:
        sfx = self._sfx()
        o = offset
        self.at(o + 0.5, self.step_initial_orders, "initial_orders" + sfx)
        self.at(o + 30.0, self.step_accident, "accident" + sfx)
        self.at(o + 12.0, self.step_second_mm_order, "second_mm_order" + sfx)
        self.at(o + 90.0, self.step_waterlogging, "waterlogging" + sfx)
        self.at(o + 150.0, self.step_fake_report, "fake_report" + sfx)
        self.at(o + 200.0, lambda: self.step_outage(True), "outage_start" + sfx)
        self.at(o + 200.5, self.step_outage_hazards, "outage_hazards" + sfx)
        self.at(o + 220.0, lambda: self.step_outage(False), "outage_end" + sfx)
        self.at(o + 235.0, self.step_clear_accident, "clear_accident" + sfx)

    def _sfx(self) -> str:
        """Event-name suffix: '' for cycle 1 (the original story), '#cN' afterwards."""
        return "" if self.cycle == 0 else f"#c{self.cycle + 1}"

    def _pid(self, base: str) -> str:
        """Prop id: unchanged in cycle 1, '-cN' afterwards (new props -> new CP-H1/CP-V)."""
        return base if self.cycle == 0 else f"{base}-c{self.cycle + 1}"

    def at(self, t: float, fn: Callable[[], None], name: str) -> None:
        self.events.append((t, fn, name))
        self.events.sort(key=lambda x: x[0])

    def step(self, sim_time: float) -> None:
        """Called every control tick by the Engine."""
        while self.events and self.events[0][0] <= sim_time:
            t, fn, name = self.events.pop(0)
            if name not in self.executed:
                self.executed.add(name)
                log.info("[scenario] t=%.1fs triggering %s", sim_time, name)
                try:
                    fn()
                except Exception as e:
                    log.exception("[scenario] failed executing %s: %s", name, e)
        if self._pending_start is not None:
            self._maybe_start_cycle(sim_time)

    # ------------------------------------------------------------------ loop mode
    def _bus_log(self, msg: str, level: str = "info") -> None:
        (log.warning if level == "warn" else log.info)("[scenario] %s", msg)
        try:
            BUS.publish("log", self.engine.sim_time, {"level": level, "msg": msg})
        except Exception:
            log.exception("[scenario] bus log failed")

    def _announce_cycle(self) -> None:
        n = self.cycle + 1
        self.cycles.append({"cycle": n, "started_at": round(self.engine.sim_time, 2),
                            "accident_edge": self.accident_edge})
        self._bus_log(f"scenario cycle {n} started (of max {self.loop_max_cycles}, "
                      f"every {self.loop_every_s:.0f} s)")

    def _schedule_next_boundary(self) -> None:
        assert self.loop_every_s
        t = self.cycle_start_t + self.loop_every_s
        if self.cycle + 1 < self.loop_max_cycles:
            self.at(t, self._cycle_boundary, f"cycle_reset#c{self.cycle + 2}")
        else:
            self.at(t, self._loop_finished, "loop_end")

    def _loop_finished(self) -> None:
        self._bus_log(f"scenario loop stopped after {self.cycle + 1} cycles "
                      f"(loop max cycles = {self.loop_max_cycles}); no further scripted beats",
                      level="warn")

    def _cycle_boundary(self) -> None:
        """End of a cycle: reset it locally now, start the next one once things have settled."""
        self._reset_cycle()
        self._pending_start = (self.cycle + 1, self.engine.sim_time + self.settle_max_s)
        self._maybe_start_cycle(self.engine.sim_time)

    def _reset_cycle(self) -> None:
        """Clear the current cycle from the sim. No chain tx: hazards are only retired from this
        run's live map (edges unblocked, SUMO closures reopened)."""
        eng = self.engine
        if self.events:   # beats of this cycle that never fired (period shorter than a story)
            log.warning("[scenario] dropping unfired beats: %s", [n for _, _, n in self.events])
            self.events = []
        if self._outage_on:
            self.step_outage(False)
        self._closing_props = list(self.cycle_props)
        self.cycle_props = []
        edges = sorted({p.edge_id for p in self._closing_props})
        retired = self._sweep(self._closing_props)
        self.avoid_edges = set(edges) | ({self.accident_edge} if self.accident_edge else set())
        if self.accident_edge:
            self.past_accident_edges.append(self.accident_edge)
        if self.cycles:
            self.cycles[-1].update(ended_at=round(eng.sim_time, 2), edges=edges,
                                   prop_ids=sorted(p.id for p in self._closing_props),
                                   retired_hazard_ids=retired)
        log.info("[scenario] cycle %d reset: removed props %s, retired hazards %s",
                 self.cycle + 1, [p.id for p in self._closing_props], retired)

    def _sweep(self, props: Sequence[HazardProp]) -> list[str]:
        """Remove old props, drop verifications of them (their drones fly home) and retire every
        hazard reported so far. Idempotent: it runs at the reset and again when the next cycle
        starts, to catch in-flight 0G decisions on old hazards that landed while settling."""
        eng = self.engine
        old = {p.id for p in props}
        eng.verification_queue[:] = [v for v in eng.verification_queue if v["prop"].id not in old]
        for v in list(eng.pending_verifications):
            if v["prop"].id in old:
                eng.pending_verifications.remove(v)
                d = eng.world.robots.get(v.get("drone_id"))
                if d is not None:
                    d.return_home()
                    d.note = "scenario reset: verification cancelled, returning"
        for p in props:
            eng.remove_prop(p.id)
        return eng.retire_run_hazards(extra_edges=[p.edge_id for p in props])

    def _unsettled(self) -> list[str]:
        """Robots still paused on the brain (they recover by themselves) or drones not yet
        landed/idle: the next cycle waits for them (bounded by settle_max_s)."""
        from sim.drone import Drone
        out = []
        for r in self.engine.world.robots.values():
            if r.paused:
                out.append(r.id)
            elif isinstance(r, Drone) and not (r.fsm_state == FsmState.IDLE or r.landed):
                out.append(r.id)
        return sorted(out)

    def _maybe_start_cycle(self, sim_time: float) -> None:
        assert self._pending_start is not None
        k, deadline = self._pending_start
        busy = self._unsettled()
        if busy and sim_time < deadline:
            return
        if busy:
            log.warning("[scenario] starting cycle %d with %s not settled after %.0f s",
                        k + 1, busy, self.settle_max_s)
        self._pending_start = None
        self._start_cycle(k, sim_time)

    def _start_cycle(self, k: int, t: float) -> None:
        late = self._sweep(self._closing_props)
        if late and self.cycles:
            self.cycles[-1]["retired_hazard_ids"] = self.cycles[-1].get("retired_hazard_ids", []) + late
        self._closing_props = []
        self.cycle = k
        self.cycle_start_t = t
        self.accident_prop = self.waterlogging_prop = self.fake_prop = None
        self.outage_props = []
        self.fake_report_future = None
        self.mm_rover_id = None
        self.accident_edge = None
        self.plan_homes = {}
        A = self._pick_cycle_accident_edge(k)
        if A:
            self._set_accident_edge(A)
        self._schedule_events(offset=t)
        self._announce_cycle()
        self._schedule_next_boundary()

    def _pick_cycle_accident_edge(self, k: int) -> Optional[str]:
        """A different engineered accident edge per cycle, deterministic from (seed, cycle)."""
        rank = [e for e in self._accident_ranking if e not in self.avoid_edges]
        fresh = [e for e in rank if e not in self.past_accident_edges] or rank
        if not fresh:
            return None
        return random.Random(f"hazard-commons-loop:{self.seed}:{k}").choice(fresh[:6])

    # ------------------------------------------------------------------ helpers
    def _order(self, op: str, home_id: str) -> None:
        items, name = ITEMS[op]
        self.engine.place_order(op, home_id, list(items), name)

    def _rovers(self, ops: Sequence[str] = OPS) -> list["Rover"]:
        from sim.robot import Rover
        rs = [r for r in self.engine.world.robots.values()
              if isinstance(r, Rover) and r.operator in ops and r.fsm_state in MOVING
              and r.route is not None and not r.arrived and not r.paused]
        return sorted(rs, key=lambda r: r.id)

    def _used_edges(self) -> set[str]:
        return {p.edge_id for p in self.engine.props.values()} | self.avoid_edges

    def _add_prop(self, prop: HazardProp) -> None:
        self.engine.add_prop(prop)
        self.cycle_props.append(prop)

    def _spot_ahead(self, rover: "Rover", min_ahead: float, max_ahead: float,
                    min_len: float = 15.0, into: float = 8.0) -> Optional[tuple[str, float, float, float]]:
        """(edge_id, x, y, dist_ahead): `into` metres into the first edge (>= min_len long)
        starting between min_ahead and max_ahead along the rover's remaining route."""
        r = rover.route
        if r is None:
            return None
        x, y, *_ = rover.pose()
        s_r = along(r, (x, y))
        used = self._used_edges() | ({self.accident_edge} if self.accident_edge else set())
        for eid, s0, s1 in edge_spans(r):
            if eid in used or (s1 - s0) < min_len:
                continue
            if s_r + min_ahead <= s0 <= s_r + max_ahead:
                s = s0 + min(into, (s1 - s0) / 2)
                px, py = point_at(r, s)
                return eid, px, py, s - s_r
        return None

    def _drone_dist(self, xy: Sequence[float], exclude_op: str) -> float:
        from sim.drone import Drone
        ds = [math.hypot(d.pose()[0] - xy[0], d.pose()[1] - xy[1])
              for d in self.engine.world.robots.values()
              if isinstance(d, Drone) and d.operator != exclude_op]
        return min(ds) if ds else 1e9

    # ------------------------------------------------------------------ stages
    def step_initial_orders(self) -> None:
        """One order per operator to a home whose route crosses the accident edge."""
        homes = sorted(self.engine.homes_by_id)
        if not homes:
            return
        for k, op in enumerate(("MinuteMart", "ZipKart", "DashKirana")):
            planned = self.plan_homes.get(op) or []
            self._order(op, planned[0] if planned else homes[k % len(homes)])

    def step_second_mm_order(self) -> None:
        planned = self.plan_homes.get("MinuteMart") or []
        if len(planned) > 1:
            self._order("MinuteMart", planned[1])
        elif planned:
            self._order("MinuteMart", planned[0])

    def step_accident(self) -> None:
        """Accident on A, ~30 m ahead of the MinuteMart rover that is driving over it."""
        spot = None
        A = self.accident_edge
        # the MinuteMart rover furthest along on A's approach goes first
        mm = sorted(self._rovers(("MinuteMart",)),
                    key=lambda r: -along(r.route, r.pose()[:2]) if A and A in r.route.edge_ids else 0.0)
        for r in mm:
            if A and A in r.route.edge_ids:
                for eid, s0, s1 in edge_spans(r.route):
                    if eid != A:
                        continue
                    s_r = along(r.route, r.pose()[:2])
                    s = float(np.clip(s_r + 30.0, s0 + 6.0, max(s0 + 6.0, s1 - 6.0)))
                    if s > s_r + 12.0:
                        px, py = point_at(r.route, s)
                        spot = (eid, px, py)
                        self.mm_rover_id = r.id
                    break
            if spot:
                break
        if spot is None:   # fallback: whatever is ~30-80 m ahead of a moving MinuteMart rover
            for r in mm:
                s = self._spot_ahead(r, 25.0, 80.0)
                if s:
                    spot = s[:3]
                    self.mm_rover_id = r.id
                    break
        if spot is None:
            for r in self._rovers():
                s = self._spot_ahead(r, 25.0, 80.0)
                if s:
                    spot = s[:3]
                    break
        if spot is None and A:   # nobody moving (e.g. fast-forwarded test): middle of A
            sh = self.engine.world.planner.edge_shape(A)
            spot = (A, float(sh[len(sh) // 2][0]), float(sh[len(sh) // 2][1]))
        if spot is None:
            log.warning("[scenario] no moving rover for the accident; skipped")
            return
        edge_id, px, py = spot
        self.accident_edge = edge_id
        self.accident_prop = HazardProp(id=self._pid("P-scenario-accident"), edge_id=edge_id, type="accident",
                                        x=px, y=py, real=True, photo="accident_1.jpg")
        self._add_prop(self.accident_prop)
        log.info("[scenario] accident on %s at (%.1f, %.1f) ahead of %s", edge_id, px, py,
                 self.mm_rover_id)

    def step_waterlogging(self) -> None:
        """Waterlogging ~40 m ahead of a moving rover (ZipKart/DashKirana first: MinuteMart
        already reported the accident), preferring one a free rival drone can reach quickly."""
        cands = []
        for pref, ops in enumerate((("ZipKart", "DashKirana"), ("MinuteMart",))):
            for r in self._rovers(ops):
                s = self._spot_ahead(r, 30.0, 70.0)
                if s:
                    cands.append((pref, self._drone_dist(s[1:3], r.operator), r.id, s))
        if not cands:   # nobody moving: a deterministic 1-lane edge away from other hazards
            used = self._used_edges() | {self.accident_edge}
            edges = self.engine.world.planner.edges
            eid = next((e for e in sorted(edges) if e not in used and edges[e]["lanes"] == 1
                        and edges[e]["length"] > 30), next(iter(sorted(edges))))
            sh = self.engine.world.planner.edge_shape(eid)
            cands = [(9, 0.0, None, (eid, float(sh[len(sh) // 2][0]), float(sh[len(sh) // 2][1]), 0.0))]
            log.warning("[scenario] no moving rover for waterlogging; placed on %s", eid)
        _, _, rid, (edge_id, px, py, ahead) = min(cands)
        self.waterlogging_prop = HazardProp(id=self._pid("P-scenario-waterlogging"), edge_id=edge_id,
                                            type="waterlogging", x=px, y=py, real=True,
                                            photo="waterlogging_1.jpg")
        self._add_prop(self.waterlogging_prop)
        log.info("[scenario] waterlogging on %s (%.0f m ahead of %s)", edge_id, ahead, rid)

    def step_fake_report(self) -> None:
        """DashKirana posts a fake road_works report on a rival rover's route (nothing there)."""
        cands = []
        for r in self._rovers(("ZipKart", "MinuteMart")):
            s = self._spot_ahead(r, 80.0, 400.0, min_len=20.0)
            if s:
                cands.append((self._drone_dist(s[1:3], "DashKirana"), r.id, s))
        if cands:
            _, victim, (edge_id, px, py, _) = min(cands)
        else:   # no rival moving: any drive edge away from the other hazards
            used = self._used_edges() | {self.accident_edge}
            edge_id = next(e for e in sorted(self.engine.world.planner.edges) if e not in used)
            sh = self.engine.world.planner.edge_shape(edge_id)
            px, py = (float(v) for v in sh[len(sh) // 2])
            victim = None
        # real=False: no physical obstacle; verifiers get a clear-road frame
        self.fake_prop = HazardProp(id=self._pid("P-scenario-fake-roadworks"), edge_id=edge_id,
                                    type="road_works", x=px, y=py, real=False, photo=None)
        self._add_prop(self.fake_prop)
        dk = sorted(r.id for r in self.engine.world.robots.values()
                    if r.operator == "DashKirana" and r.kind == "rover") or ["R-07"]
        self.fake_report_future = self.engine.report_hazard(
            operator="DashKirana", robot_id=dk[0], edge_id=edge_id, hazard_type="road_works",
            severity=0.75, evidence_root=None,
            zg_request_id="FAKE-NO-0G-CALL",   # honest marker: this report has no 0G decision
            passable_for=[])
        log.warning("[scenario] DashKirana posted a FAKE road_works report on %s (route of %s)",
                    edge_id, victim)
        self.engine.schedule_verification(self.fake_prop, reporter_op="DashKirana",
                                          hazard_type="road_works", severity=0.75,
                                          zg_request_id="FAKE-NO-0G-CALL",
                                          report_future=self.fake_report_future)

    def step_outage(self, on: bool) -> None:
        """Toggle 0G brain unreachable."""
        self.engine.set_brain_unreachable(on)
        self._outage_on = on
        log.warning("[scenario] 0G brain outage: unreachable=%s", on)

    def step_outage_hazards(self) -> None:
        """Road works ahead of up to three moving rovers (distinct operators first): their CP-H1
        calls hit the outage -> SAFE_STOP; after restore the retry gets a real 0G decision."""
        cands = [r for r in self._rovers() if r.id not in {self.mm_rover_id}]
        order = sorted(cands, key=lambda r: (r.operator, r.id))
        picked: list = []
        for pass_ in (0, 1):
            for r in order:
                if len(picked) >= 3:
                    break
                if r in picked or (pass_ == 0 and r.operator in {x.operator for x in picked}):
                    continue
                s = self._spot_ahead(r, 15.0, 80.0)
                if not s:
                    continue
                edge_id, px, py, _ = s
                prop = HazardProp(id=self._pid(f"P-scenario-roadworks-{r.id}"), edge_id=edge_id,
                                  type="road_works", x=px, y=py, real=True, photo=None)
                self._add_prop(prop)
                self.outage_props.append(prop)
                picked.append(r)
                log.info("[scenario] outage road works on %s ahead of %s", edge_id, r.id)

    def step_clear_accident(self) -> None:
        """Clear the accident on-chain once resolved."""
        for h in self.engine.run_hazards():
            if h.type == "accident" and h.status == "confirmed":
                self.engine.chain.clear_hazard(operator=h.reporter_operator or "MinuteMart",
                                               hazard_id=h.id)
                log.info("[scenario] cleared confirmed accident on edge %s (hazard_id=%s)",
                         h.edge_id, h.id)
                if self.accident_prop:
                    self.engine.remove_prop(self.accident_prop.id)
                break
