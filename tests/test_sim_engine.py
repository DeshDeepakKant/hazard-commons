"""Tests for sim/engine.py: fixed-step engine, order dispatch, checkpoints CP-H1/CP-V/CP-H2,
and safe stop fallback.
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Optional

import pytest

from brain.mock import MockBrain
from common.config import ROOT
from server.bus import BUS
from sim.controllers.planner import Route
from sim.engine import Engine, LocalHazardChain
from sim.robot import Rover
from sim.drone import Drone
from sim.schemas import FsmState, HazardProp


@pytest.fixture
def mock_brain(monkeypatch):
    monkeypatch.setenv("MOCK_BRAIN", "1")
    monkeypatch.delenv("DEMO", raising=False)
    return MockBrain()


@pytest.fixture
def engine(mock_brain):
    eng = Engine(
        brain=mock_brain,
        enable_traffic=False,
        seed=42,
        attach_to_server=False,
    )
    yield eng
    eng.close()


def test_engine_init_and_robots(engine: Engine):
    assert len(engine.world.robots) == 10  # 8 rovers + 2 drones
    rovers = [r for r in engine.world.robots.values() if isinstance(r, Rover)]
    drones = [d for d in engine.world.robots.values() if isinstance(d, Drone)]
    assert len(rovers) == 8
    assert len(drones) == 2

    # Operator breakdown
    ops = [r.operator for r in rovers]
    assert ops.count("ZipKart") == 3
    assert ops.count("MinuteMart") == 3
    assert ops.count("DashKirana") == 2

    meta = engine.snapshot_meta()
    assert meta["engine"] is True
    assert "operators" in meta
    assert len(meta["operators"]) == 3


def test_engine_order_dispatch_and_delivery(engine: Engine):
    # Place an order for ZipKart
    order_dict = engine.place_order("ZipKart", "H-01", ["Milk 1L"], "Customer Bob")
    assert order_dict["status"] == "placed"
    oid = order_dict["id"]

    # Step engine to allow dispatcher to assign the order
    for _ in range(5):
        engine.step(0.05)

    order = engine.order_book.get(oid)
    assert order.status in ("assigned", "picking", "out_for_delivery")
    assert order.robot_id is not None
    rover = engine.world.robots[order.robot_id]
    assert rover.operator == "ZipKart"
    assert rover.order_id == oid


def test_engine_cp_h1_and_reporting(engine: Engine):
    # Find a rover and place a hazard prop directly on its path
    rover = next(r for r in engine.world.robots.values() if isinstance(r, Rover) and r.operator == "MinuteMart")
    rx, ry, _, ryaw, _ = rover.pose()

    # Place a prop 10 m ahead of the rover
    px = rx + 10.0 * math.cos(ryaw)
    py = ry + 10.0 * math.sin(ryaw)
    edge_id = list(engine.world.planner.edges.keys())[0]

    prop = HazardProp(id="P-test-accident", edge_id=edge_id, type="accident", x=px, y=py, real=True)
    engine.add_prop(prop)

    # Force rover into DELIVERING state heading forward
    rover.set_state(FsmState.DELIVERING)
    # Plan a dummy route past the prop
    dummy_path = [[rx, ry], [px, py], [px + 20.0, py + 20.0]]
    rover.pp.set_path(dummy_path)
    rover.route_edge_ids = [edge_id]

    # Step engine to trigger CP-H1
    decisions = []
    BUS.subscribe_sync(decisions.append, {"decision"})

    # Step until checkpoint triggers
    for _ in range(40):
        engine.step(0.05)
        if decisions:
            break

    assert len(decisions) >= 1
    dec = decisions[0].data
    assert dec["result"]["ok"] is True
    assert dec["result"]["checkpoint"] == "CP-H1"
    assert dec["action_taken"] == "report_hazard"

    # Verify on-chain hazard was created
    hazards = engine.chain.hazards()
    assert any(h.edge_id == edge_id and h.type == "accident" for h in hazards)


def test_engine_cp_h2_reroute(engine: Engine):
    rover = next(r for r in engine.world.robots.values() if isinstance(r, Rover) and r.operator == "ZipKart")
    home = engine.homes_by_id["H-21"]
    route = rover.drive_to(home["entrance_xy"], goal_node=home["entrance_node"], state=FsmState.DELIVERING)
    assert route is not None and len(route.edge_ids) > 4
    target_edge = route.edge_ids[3]

    # A rival report on that edge, confirmed by a third operator -> active on the shared map
    fut = engine.report_hazard("MinuteMart", "R-04", target_edge, "road_works", 0.8)
    hid = fut.result()["hazard_id"]
    # unverified reports never trigger CP-H2
    for _ in range(5):
        engine.step(0.05)
    assert hid not in engine.robot_decided_hazards[rover.id]
    engine.chain.verify_hazard("ZipKart", "D-01", hid, True)

    decisions = []
    BUS.subscribe_sync(decisions.append, {"decision"})

    for _ in range(60):
        engine.step(0.05)
        if any(d.data["result"]["checkpoint"] == "CP-H2" for d in decisions):
            break
    for _ in range(40):   # let the reroute apply
        engine.step(0.05)

    h2_decs = [d for d in decisions if d.data["result"]["checkpoint"] == "CP-H2"]
    assert len(h2_decs) >= 1
    assert h2_decs[0].data["action_taken"] == "reroute"
    assert target_edge in engine.get_blocked_edges(rover)
    assert target_edge not in rover.route.edge_ids


def test_engine_safe_stop_on_unreachable_brain(engine: Engine):
    engine.set_brain_unreachable(True)
    assert not engine.brain.online()

    rover = next(r for r in engine.world.robots.values() if isinstance(r, Rover))
    rover.enter_safe_stop("0G unreachable")
    assert rover.in_safe_stop
    assert rover.fsm_state == FsmState.SAFE_STOP

    # Restore
    engine.set_brain_unreachable(False)
    assert engine.brain.online()
