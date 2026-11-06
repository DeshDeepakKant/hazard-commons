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
"""
from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

import numpy as np

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

    def __init__(self, engine: "Engine", seed: int = 7) -> None:
        self.engine = engine
        self.engine.scenario = self
        self.seed = seed
        self.rng = random.Random(seed)

        self.events: list[tuple[float, Callable[[], None], str]] = []
        self.executed: set[str] = set()

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
        best = None
        for eid, d in use.items():
            if "MinuteMart" not in d or edges[eid]["length"] < 30.0:
                continue
            mm_s = min(s for _, s in d["MinuteMart"])
            if not (60.0 <= mm_s <= 200.0):
                continue
            score = (len(d), min(len(d.get("ZipKart", [])), 3) + min(len(d.get("DashKirana", [])), 3),
                     edges[eid]["lanes"], -abs(mm_s - 110.0))
            if best is None or score > best[0]:
                best = (score, eid)
        if best is None:
            log.warning("[scenario] no engineered accident edge found; accident will be placed "
                        "ahead of the MinuteMart rover at t=30 s")
            return
        A = best[1]
        self.accident_edge = A
        d = use[A]
        for op in OPS:
            homes = d.get(op, [])
            if op == "MinuteMart":
                homes = sorted(homes, key=lambda t: abs(t[1] - 110.0))   # reach A at ~t=30 s
            else:
                homes = sorted(homes, key=lambda t: t[1])
            self.plan_homes[op] = [h for h, _ in homes]
        log.info("[scenario] accident edge %s (%s), homes crossing it: %s", A,
                 edges[A].get("name"), {k: v[:3] for k, v in self.plan_homes.items()})

    def _schedule_events(self) -> None:
        self.at(0.5, self.step_initial_orders, "initial_orders")
        self.at(30.0, self.step_accident, "accident")
        self.at(12.0, self.step_second_mm_order, "second_mm_order")
        self.at(90.0, self.step_waterlogging, "waterlogging")
        self.at(150.0, self.step_fake_report, "fake_report")
        self.at(200.0, lambda: self.step_outage(True), "outage_start")
        self.at(200.5, self.step_outage_hazards, "outage_hazards")
        self.at(220.0, lambda: self.step_outage(False), "outage_end")
        self.at(235.0, self.step_clear_accident, "clear_accident")

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
        return {p.edge_id for p in self.engine.props.values()}

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
        if spot is None:
            log.warning("[scenario] no moving rover for the accident; skipped")
            return
        edge_id, px, py = spot
        self.accident_edge = edge_id
        self.accident_prop = HazardProp(id="P-scenario-accident", edge_id=edge_id, type="accident",
                                        x=px, y=py, real=True, photo="accident_1.jpg")
        self.engine.add_prop(self.accident_prop)
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
        if not cands:
            log.warning("[scenario] no moving rover for waterlogging; skipped")
            return
        _, _, rid, (edge_id, px, py, ahead) = min(cands)
        self.waterlogging_prop = HazardProp(id="P-scenario-waterlogging", edge_id=edge_id,
                                            type="waterlogging", x=px, y=py, real=True,
                                            photo="waterlogging_1.jpg")
        self.engine.add_prop(self.waterlogging_prop)
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
        self.fake_prop = HazardProp(id="P-scenario-fake-roadworks", edge_id=edge_id,
                                    type="road_works", x=px, y=py, real=False, photo=None)
        self.engine.add_prop(self.fake_prop)
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
                prop = HazardProp(id=f"P-scenario-roadworks-{r.id}", edge_id=edge_id,
                                  type="road_works", x=px, y=py, real=True, photo=None)
                self.engine.add_prop(prop)
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
