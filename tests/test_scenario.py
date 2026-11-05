"""Tests for sim/scenario.py: deterministic scenario progression."""
from __future__ import annotations

import pytest

from brain.mock import MockBrain
from sim.engine import Engine
from sim.scenario import Scenario
from sim.schemas import FsmState


@pytest.fixture
def mock_brain(monkeypatch):
    monkeypatch.setenv("MOCK_BRAIN", "1")
    monkeypatch.delenv("DEMO", raising=False)
    return MockBrain()


def test_scenario_stages_execute(mock_brain):
    eng = Engine(
        brain=mock_brain,
        enable_traffic=False,
        seed=123,
        attach_to_server=False,
    )
    scn = Scenario(eng, seed=123)

    # Initial state
    assert len(scn.events) >= 7

    # Execute initial orders
    eng.step(1.0)
    assert "initial_orders" in scn.executed
    assert len(eng.order_book.all()) >= 3

    # Fast forward to accident (t=30)
    eng.sim_time = 29.9
    eng.step(0.2)
    assert "accident" in scn.executed
    assert "P-scenario-accident" in eng.props

    # Fast forward to waterlogging (t=90)
    eng.sim_time = 89.9
    eng.step(0.2)
    assert "waterlogging" in scn.executed
    assert "P-scenario-waterlogging" in eng.props

    # Fast forward to fake report (t=150)
    eng.sim_time = 149.9
    eng.step(0.2)
    assert "fake_report" in scn.executed
    assert "P-scenario-fake-roadworks" in eng.props

    # Fast forward to outage start (t=200)
    eng.sim_time = 199.9
    eng.step(0.2)
    assert "outage_start" in scn.executed
    assert not eng.brain.online()

    # Fast forward to outage end (t=220)
    eng.sim_time = 219.9
    eng.step(0.2)
    assert "outage_end" in scn.executed
    assert eng.brain.online()

    eng.close()
