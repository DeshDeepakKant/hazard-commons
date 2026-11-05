import os

import pytest

from brain.schemas import OUTPUT_SCHEMAS
from common.frame import lonlat_to_local, local_to_lonlat
from server.bus import Bus
from sim.clock import SimClock
from sim.schemas import CheckpointResult, Envelope, RobotState, StateSnapshot


def test_schema_roundtrip():
    r = RobotState(id="R-01", operator="ZipKart", kind="rover", x=1, y=2)
    s = StateSnapshot(sim_time=0.5, robots=[r], actors=[], hazards=[])
    assert StateSnapshot.model_validate_json(s.model_dump_json()) == s


def test_checkpoint_output_schemas_reject_garbage():
    with pytest.raises(Exception):
        OUTPUT_SCHEMAS["CP-H1"].model_validate({"is_hazard": "maybe"})
    ok = OUTPUT_SCHEMAS["CP-V"].model_validate(
        {"verdict": "dispute", "observed_type": "none", "confidence": 0.7})
    assert ok.verdict == "dispute"


def test_frame_roundtrip():
    x, y = lonlat_to_local(72.834, 19.059)
    assert abs(x) < 1 and abs(y) < 1
    lon, lat = local_to_lonlat(500, -300)
    x2, y2 = lonlat_to_local(lon, lat)
    assert abs(x2 - 500) < 1e-3 and abs(y2 + 300) < 1e-3


def test_bus_sync_subscriber():
    bus, got = Bus(), []
    bus.subscribe_sync(got.append, {"decision"})
    bus.publish("state", 0.0, {"a": 1})
    bus.publish("decision", 1.0, CheckpointResult(ok=False, checkpoint="CP-H1", robot_id="R", error="x"))
    assert len(got) == 1 and isinstance(got[0], Envelope) and got[0].data["ok"] is False


def test_clock_deterministic():
    a, b = SimClock(realtime_factor=0, seed=3), SimClock(realtime_factor=0, seed=3)
    assert [a.rng.random() for _ in range(3)] == [b.rng.random() for _ in range(3)]
    for _ in range(20):
        a.tick()
    assert abs(a.t - 1.0) < 1e-9


def test_mock_brain_refused_in_demo(monkeypatch):
    monkeypatch.setenv("MOCK_BRAIN", "1")
    monkeypatch.setenv("DEMO", "1")
    from brain.mock import MockBrain
    with pytest.raises(RuntimeError):
        MockBrain()
