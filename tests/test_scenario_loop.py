"""Scenario LOOP mode: two cycles, fast-forwarded (like test_scenario.py) on MOCK_BRAIN + the
in-memory chain. A fake SUMO bridge records edge closures so the reset's reopen is visible."""
from __future__ import annotations

import pytest

from brain.mock import MockBrain
from memory.episode_log import EpisodeLog
from server.bus import BUS
from sim.engine import Engine
from sim.scenario import Scenario

BEATS = ("initial_orders", "second_mm_order", "accident", "waterlogging", "fake_report",
         "outage_start", "outage_hazards", "outage_end", "clear_accident")
OFFSETS = (0.5, 12.0, 30.0, 90.0, 150.0, 200.0, 200.5, 220.0, 235.0)
PERIOD = 240.0


class FakeBridge:
    """Stands in for traffic.bridge.TrafficBridge: no SUMO, just the closed-edge set."""

    def __init__(self) -> None:
        self.closed: set[str] = set()
        self.reopened: list[str] = []

    def close_edge(self, se: str) -> bool:
        self.closed.add(se)
        return True

    def reopen_edge(self, se: str) -> bool:
        if se not in self.closed:
            return False
        self.closed.discard(se)
        self.reopened.append(se)
        return True

    def upsert_robot(self, *a, **k) -> None:
        pass

    def step(self, t: float) -> list:
        return []

    def close(self) -> None:
        pass


@pytest.fixture
def mock_brain(monkeypatch):
    monkeypatch.setenv("MOCK_BRAIN", "1")
    monkeypatch.delenv("DEMO", raising=False)
    return MockBrain()


def _jump(eng: Engine, t: float) -> None:
    eng.sim_time = t - 0.1
    eng.step(0.2)


def _confirm(eng: Engine, prop, reporter: str, verifier: str) -> str:
    """Report + rival-confirm a hazard on a prop's edge (stands in for CP-H1/CP-V, which need the
    robots to actually drive there)."""
    res = eng.report_hazard(operator=reporter, robot_id="R-04", edge_id=prop.edge_id,
                            hazard_type=prop.type, severity=0.9, zg_request_id="TEST").result()
    hid = res["hazard_id"]
    eng.chain.verify_hazard(operator=verifier, robot_id="D-01", hazard_id=hid, confirm=True)
    eng.step(0.05)   # _process_chain_events -> Confirmed -> SUMO close
    return hid


def test_scenario_loop_two_cycles(mock_brain, tmp_path):
    logs: list[str] = []
    active = {"on": True}
    BUS.subscribe_sync(lambda e: logs.append(e.data.get("msg", "")) if active["on"] else None,
                       topics={"log"})
    eng = Engine(brain=mock_brain, enable_traffic=False, seed=123, attach_to_server=False,
                 run_id="test-loop")
    # keep episode files + index mirror out of the repo, never upload
    eng.episode_log = EpisodeLog(run_id="test-loop", storage=None, base_dir=tmp_path)
    bridge = FakeBridge()
    eng.traffic_bridge = bridge
    scn = Scenario(eng, seed=123, loop_every_s=PERIOD, loop_max_cycles=2)
    scn.settle_max_s = 3.0
    try:
        # ---------------- cycle 1 (the original story, names/ids unchanged)
        eng.step(0.05)
        assert "cycle_start" in scn.executed
        for t in OFFSETS:
            _jump(eng, t)
            if t == 90.0:
                wl = eng.props["P-scenario-waterlogging"]
                wl_hid = _confirm(eng, wl, "ZipKart", "MinuteMart")
        for b in BEATS:
            assert b in scn.executed, b
        c1_props = {p.id: p for p in scn.cycle_props}
        assert {"P-scenario-accident", "P-scenario-waterlogging",
                "P-scenario-fake-roadworks"} <= set(c1_props)
        c1_hids = {h.id for h in eng.run_hazards()}
        assert wl_hid in c1_hids and len(c1_hids) >= 2      # waterlogging + the fake report
        wl_sumo = eng.edge_to_sumo_edges.get(wl.edge_id) or []
        assert wl_sumo and set(wl_sumo) <= bridge.closed     # SUMO closed the confirmed edge

        # ---------------- reset at PERIOD, cycle 2 starts once settled (<= settle_max_s)
        _jump(eng, PERIOD)
        assert "cycle_reset#c2" in scn.executed
        for _ in range(int(scn.settle_max_s / 0.05) + 5):
            if scn.cycle == 1:
                break
            eng.step(0.05)
        assert scn.cycle == 1, "cycle 2 never started"
        t2 = scn.cycle_start_t
        assert PERIOD <= t2 <= PERIOD + scn.settle_max_s + 0.5

        # cycle-1 hazards cleared from the sim: props gone, hazards retired, edges reopened
        assert not set(c1_props) & set(eng.props)
        assert c1_hids <= set(scn.cycles[0]["retired_hazard_ids"])
        assert not c1_hids & {h.id for h in eng.run_hazards()}
        assert not set(wl_sumo) & bridge.closed
        c1_edges = set(scn.cycles[0]["edges"])
        assert not c1_edges & eng.blocked_edges
        assert all(not c1_edges & s for s in eng.robot_blocked_edges.values())
        assert not eng.verification_queue
        assert all(v["prop"].id not in c1_props for v in eng.pending_verifications)

        # ---------------- cycle 2 beats fire again, with new props on new edges
        for t in OFFSETS:
            _jump(eng, t2 + t)
        for b in BEATS:
            assert f"{b}#c2" in scn.executed, b
        c2_props = {p.id: p for p in scn.cycle_props}
        assert {"P-scenario-accident-c2", "P-scenario-waterlogging-c2",
                "P-scenario-fake-roadworks-c2"} <= set(c2_props)
        assert not set(c2_props) & set(c1_props)
        assert not {p.edge_id for p in c2_props.values()} & c1_edges
        assert scn.cycles[1]["accident_edge"] not in c1_edges
        c2_hids = {h.id for h in eng.run_hazards()}
        assert c2_hids and not c2_hids & c1_hids                # e.g. the cycle-2 fake report
        assert eng.brain.online()

        # ---------------- max cycles reached: the loop stops (and says so)
        _jump(eng, t2 + PERIOD)
        assert "loop_end" in scn.executed
        assert scn.cycle == 1 and not scn.events
        assert any("scenario cycle 1 started" in m for m in logs)
        assert any("scenario cycle 2 started" in m for m in logs)
        assert any("scenario loop stopped after 2 cycles" in m for m in logs)
    finally:
        active["on"] = False
        eng.close()


def test_scenario_no_loop_by_default(mock_brain):
    eng = Engine(brain=mock_brain, enable_traffic=False, seed=123, attach_to_server=False)
    try:
        scn = Scenario(eng, seed=123)
        names = [n for _, _, n in scn.events]
        assert names == ["initial_orders", "second_mm_order", "accident", "waterlogging",
                         "fake_report", "outage_start", "outage_hazards", "outage_end",
                         "clear_accident"]
        assert scn.loop_every_s is None
    finally:
        eng.close()
