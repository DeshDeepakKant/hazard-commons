"""Demo Scenario Engine (SPEC §12).

Scheduled, deterministic script in sim time:
  1. t = 0s: city running; initial customer orders placed for all 3 operators.
  2. t ≈ 30s: accident appears on a main road -> MinuteMart rover hits CP-H1
              -> 0G confirms -> reportHazard tx -> ZipKart drone D-01 verifies (CP-V)
              -> Confirmed on-chain -> SUMO edge closes -> rovers of all 3 operators hit CP-H2 & reroute.
  3. t ≈ 90s: waterlogging appears on a lane -> CP-H1 confirms impassable for rover, passable for drone.
  4. t ≈ 150s: DashKirana posts a fake "road works" report on a rival's route
              -> ZipKart drone verifies (CP-V) -> disputes -> Rejected on-chain, stake slashed.
  5. t ≈ 200s: 0G outage (brain unreachable) -> every robot enters SAFE_STOP & pulls to kerb.
  6. t ≈ 220s: 0G restored -> SAFE_STOP retries succeed -> all robots resume.
  7. t ≈ 235s: accident cleared on-chain -> SUMO edge reopened.
"""
from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING, Any, Callable, Optional

from sim.schemas import FsmState, HazardProp

if TYPE_CHECKING:
    from sim.engine import Engine
    from sim.robot import Rover

log = logging.getLogger("hazard.scenario")


class Scenario:
    """Deterministic scenario driver for the live demo and video recording."""

    def __init__(self, engine: "Engine", seed: int = 7) -> None:
        self.engine = engine
        self.engine.scenario = self
        self.seed = seed
        self.rng = random.Random(seed)

        # Scheduled events: (sim_time, callback, name)
        self.events: list[tuple[float, Callable[[], None], str]] = []
        self.executed: set[str] = set()

        # State stored across stages
        self.accident_prop: Optional[HazardProp] = None
        self.waterlogging_prop: Optional[HazardProp] = None
        self.fake_prop: Optional[HazardProp] = None

        self._schedule_events()

    def _schedule_events(self) -> None:
        self.at(0.5, self.step_initial_orders, "initial_orders")
        self.at(30.0, self.step_accident, "accident")
        self.at(90.0, self.step_waterlogging, "waterlogging")
        self.at(150.0, self.step_fake_report, "fake_report")
        self.at(200.0, lambda: self.step_outage(True), "outage_start")
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

    # ------------------------------------------------------------------ stage implementations
    def step_initial_orders(self) -> None:
        """Place initial orders for all 3 operators so rovers leave dark stores immediately."""
        homes = list(self.engine.homes_by_id.keys())
        if len(homes) < 3:
            return

        # MinuteMart order to H-01
        self.engine.place_order("MinuteMart", homes[0], ["Milk 1L", "Bread", "Eggs (6)"], "Aanya Sharma")
        # ZipKart order to H-02
        self.engine.place_order("ZipKart", homes[1], ["Instant noodles", "Chips", "Cold drink 2L"], "Rohan Mehta")
        # DashKirana order to H-03
        self.engine.place_order("DashKirana", homes[2], ["Chai masala", "Curd 400g", "Tomatoes 500g"], "Priya Patel")

    def step_accident(self) -> None:
        """Accident appears on MinuteMart rover's path (or major road)."""
        # Find MinuteMart rover (e.g. R-04)
        mm_rover = next((r for r in self.engine.world.robots.values()
                         if r.kind == "rover" and r.operator == "MinuteMart"
                         and r.fsm_state in (FsmState.DELIVERING, FsmState.TO_STORE, FsmState.LOADING)), None)

        spot = None
        if mm_rover and mm_rover.route and len(mm_rover.route.edge_ids) > 1:
            # Pick edge ahead on the route
            for eid in mm_rover.route.edge_ids[1:4]:
                sh = self.engine.world.planner.edge_shape(eid)
                if len(sh) >= 2:
                    mid = sh[len(sh) // 2]
                    spot = (eid, float(mid[0]), float(mid[1]))
                    break

        if not spot:
            # Fallback to a well-known road edge in Bandra West
            for eid in self.engine.world.planner.edges:
                sh = self.engine.world.planner.edge_shape(eid)
                if len(sh) >= 2 and self.engine.world.planner.edges[eid].get("lanes", 1) >= 2:
                    mid = sh[len(sh) // 2]
                    spot = (eid, float(mid[0]), float(mid[1]))
                    break

        if not spot:
            eid = next(iter(self.engine.world.planner.edges))
            sh = self.engine.world.planner.edge_shape(eid)
            spot = (eid, float(sh[0][0]), float(sh[0][1]))

        edge_id, px, py = spot
        self.accident_prop = HazardProp(
            id="P-scenario-accident",
            edge_id=edge_id,
            type="accident",
            x=px,
            y=py,
            real=True,
            photo="accident_1.jpg",
        )
        self.engine.add_prop(self.accident_prop)
        log.info("[scenario] spawned accident on edge %s at (%.1f, %.1f)", edge_id, px, py)

    def step_waterlogging(self) -> None:
        """Waterlogging appears on a lane."""
        # Find a residential lane (1 lane)
        spot = None
        for eid, e in self.engine.world.planner.edges.items():
            if eid != getattr(self.accident_prop, "edge_id", None) and e.get("lanes", 1) == 1:
                sh = self.engine.world.planner.edge_shape(eid)
                if len(sh) >= 2:
                    mid = sh[len(sh) // 2]
                    spot = (eid, float(mid[0]), float(mid[1]))
                    break

        if not spot:
            eid = list(self.engine.world.planner.edges.keys())[10]
            sh = self.engine.world.planner.edge_shape(eid)
            spot = (eid, float(sh[0][0]), float(sh[0][1]))

        edge_id, px, py = spot
        self.waterlogging_prop = HazardProp(
            id="P-scenario-waterlogging",
            edge_id=edge_id,
            type="waterlogging",
            x=px,
            y=py,
            real=True,
            photo="waterlogging_1.jpg",
        )
        self.engine.add_prop(self.waterlogging_prop)
        log.info("[scenario] spawned waterlogging on edge %s at (%.1f, %.1f)", edge_id, px, py)

    def step_fake_report(self) -> None:
        """DashKirana posts a fake 'road works' report on a rival route."""
        # Pick a drive edge not occupied by physical hazards
        avoid = {getattr(self.accident_prop, "edge_id", None), getattr(self.waterlogging_prop, "edge_id", None)}
        candidate_edge = None
        for eid in self.engine.world.planner.edges:
            if eid not in avoid:
                candidate_edge = eid
                break
        candidate_edge = candidate_edge or next(iter(self.engine.world.planner.edges))
        sh = self.engine.world.planner.edge_shape(candidate_edge)
        mid = sh[len(sh) // 2] if len(sh) >= 2 else (0.0, 0.0)

        # fake prop has real=False -> no physical obstacle in PyBullet
        self.fake_prop = HazardProp(
            id="P-scenario-fake-roadworks",
            edge_id=candidate_edge,
            type="road_works",
            x=float(mid[0]),
            y=float(mid[1]),
            real=False,
            photo=None,
        )
        self.engine.add_prop(self.fake_prop)

        # DashKirana directly reports the fake hazard
        self.engine.chain.report_hazard(
            operator="DashKirana",
            robot_id="R-07",
            edge_id=candidate_edge,
            hazard_type="road_works",
            severity=0.75,
            evidence_root=None,
            zg_request_id="FAKE-SCENARIO-01",
            passable_for=[],
        )
        log.warning("[scenario] DashKirana posted a fake road_works report on %s (no physical hazard)", candidate_edge)

        # Dispatch ZipKart drone D-01 to verify it
        self.engine.schedule_verification(
            self.fake_prop,
            reporter_op="DashKirana",
            hazard_type="road_works",
            severity=0.75,
            zg_request_id="FAKE-SCENARIO-01",
        )

    def step_outage(self, on: bool) -> None:
        """Toggle 0G brain unreachable."""
        self.engine.set_brain_unreachable(on)
        log.warning("[scenario] 0G brain outage: unreachable=%s", on)

    def step_clear_accident(self) -> None:
        """Clear the accident on-chain once resolved."""
        for h in self.engine.chain.hazards():
            if h.type == "accident" and h.status == "confirmed":
                self.engine.chain.clear_hazard(operator="MinuteMart", hazard_id=h.id)
                log.info("[scenario] cleared confirmed accident on edge %s (hazard_id=%s)", h.edge_id, h.id)
                if self.accident_prop:
                    self.engine.remove_prop(self.accident_prop.id)
                break
