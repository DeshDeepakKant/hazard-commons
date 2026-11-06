"""'Add a disaster': Engine.inject_hazard / inject_random / clear_injected and the REST layer."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import server.app as appmod
from brain.mock import MockBrain
from sim.engine import Engine


@pytest.fixture(scope="module")
def eng():
    import os
    os.environ["MOCK_BRAIN"] = "1"
    os.environ.pop("DEMO", None)
    e = Engine(brain=MockBrain(), enable_traffic=False, seed=5, attach_to_server=False)
    yield e
    e.close()


def _edge(eng):
    edges = eng.world.planner.edges
    return next(e for e in sorted(edges) if edges[e]["length"] > 30)


def test_inject_on_edge_and_clear(eng):
    eid = _edge(eng)
    r = eng.inject_hazard("accident", edge_id=eid)
    assert r["edge_id"] == eid and r["type"] == "accident" and r["prop_id"] in eng.props
    p = eng.props[r["prop_id"]]
    assert p.real and p.photo == "accident_1.jpg" and p.edge_id == eid
    assert any(d["prop_id"] == r["prop_id"] and d["injected"] for d in eng.list_props())
    eng.block_edge(eid)   # as the report/verify flow would
    eng.clear_injected(r["prop_id"])
    assert r["prop_id"] not in eng.props and eid not in eng.blocked_edges
    with pytest.raises(KeyError):
        eng.clear_injected(r["prop_id"])


def test_snap_to_nearest_edge(eng):
    eid = _edge(eng)
    sh = eng.world.planner.edge_shape(eid)
    mx, my = float(sh[len(sh) // 2][0]), float(sh[len(sh) // 2][1])
    r = eng.inject_hazard("waterlogging", x=mx + 1.5, y=my + 1.5)
    assert r["edge_id"] == eid or r["snapped_m"] < 3.0   # junction ties may pick the neighbour
    _, _, _, d = eng.nearest_drive_edge(mx, my)
    assert d < 0.5
    eng.clear_injected(r["prop_id"])


def test_fake_has_no_physical_prop_and_queues_verification(eng):
    r = eng.inject_hazard("road_works", edge_id=_edge(eng), real=False)
    assert eng.props[r["prop_id"]].real is False
    assert any(v["prop"].id == r["prop_id"] for v in eng.verification_queue) or eng.pending_verifications
    eng.clear_injected(r["prop_id"])
    assert not any(v["prop"].id == r["prop_id"] for v in eng.verification_queue)


def test_random_valid_edge(eng):
    r = eng.inject_random()
    assert r["edge_id"] in eng.world.planner.edges
    assert r["type"] in Engine.INJECT_TYPES
    eng.clear_injected(r["prop_id"])


def test_validation_errors(eng):
    with pytest.raises(ValueError):
        eng.inject_hazard("meteor", edge_id=_edge(eng))
    with pytest.raises(ValueError):
        eng.inject_hazard("accident")
    with pytest.raises(ValueError):
        eng.inject_hazard("accident", edge_id="nope")


def test_cross_thread_applies_in_step(eng):
    eng.step()   # records the sim thread
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(eng.inject_hazard("accident", edge_id=_edge(eng))))
    t.start()
    for _ in range(100):
        if not t.is_alive():
            break
        eng.step()
        t.join(0.02)
    t.join(2)
    assert out.get("prop_id") in eng.props
    eng.step()
    eng.clear_injected(out["prop_id"])


def test_rest(eng, monkeypatch):
    c = TestClient(appmod.app)
    monkeypatch.setattr(appmod, "_engine", None)
    assert c.post("/api/hazards", json={"type": "accident", "edge_id": "x"}).status_code == 503
    assert c.get("/api/hazards/props").status_code == 503
    monkeypatch.setattr(appmod, "_engine", eng)
    assert c.post("/api/hazards", json={"type": "meteor", "edge_id": "x"}).status_code == 422
    assert c.post("/api/hazards", json={"type": "accident"}).status_code == 422
    assert c.post("/api/hazards", json={"type": "accident", "edge_id": "nope"}).status_code == 422
    assert c.post("/api/hazards", json={"type": "accident", "edge_id": _edge(eng),
                                        "severity": 3}).status_code == 422
    stop = threading.Event()   # REST handlers run on other threads: something must step the sim

    def loop():
        while not stop.is_set():
            eng.step()
            stop.wait(0.01)
    th = threading.Thread(target=loop)
    th.start()
    try:
        _rest_applied(c, eng)
    finally:
        stop.set()
        th.join()


def _rest_applied(c, eng):
    ok = c.post("/api/hazards", json={"type": "accident", "edge_id": _edge(eng)})
    assert ok.status_code == 200
    pid = ok.json()["prop_id"]
    assert any(p["prop_id"] == pid for p in c.get("/api/hazards/props").json()["props"])
    assert c.post("/api/hazards/random", json={"type": "bad"}).status_code == 422
    rr = c.post("/api/hazards/random")
    assert rr.status_code == 200
    assert c.delete(f"/api/hazards/{rr.json()['prop_id']}").status_code == 200
    assert c.delete(f"/api/hazards/{pid}").status_code == 200
    assert c.delete(f"/api/hazards/{pid}").status_code == 404
