"""End-to-end demo story (SPEC §12) on MOCK_BRAIN + in-memory chain, asserted from the bus stream.

MockBrain is dev-only: it only proves the engine/scenario wiring (who is asked what, when, and what
happens on-chain after the answer). The real recording uses 0G with the same engine path.
"""
from __future__ import annotations

import time

import pytest

from brain.mock import MockBrain
from memory.episode_log import EpisodeLog
from server.bus import BUS
from sim.engine import Engine, LocalHazardChain
from sim.scenario import Scenario

DURATION_S = 238.0


def _d(x):
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("MOCK_BRAIN", "1")
    mp.setenv("HC_CHAIN", "local")
    mp.delenv("DEMO", raising=False)
    env: list = []
    active = {"on": True}

    def rec(e):
        if active["on"]:
            env.append((e.topic, e.t, _d(e.data)))

    BUS.subscribe_sync(rec)
    eng = Engine(brain=MockBrain(), enable_traffic=False, seed=7, attach_to_server=False,
                 run_id="test-story")
    assert isinstance(eng.chain, LocalHazardChain)
    # keep episode files + index mirror out of the repo, never upload
    eng.episode_log = EpisodeLog(run_id="test-story", storage=None,
                                 base_dir=tmp_path_factory.mktemp("story"))
    scn = Scenario(eng, seed=7)
    t0 = time.perf_counter()
    try:
        eng.run(duration_s=DURATION_S)
    finally:
        active["on"] = False
        eng.close()
        mp.undo()
    print(f"story run: {eng.sim_time:.0f} s sim in {time.perf_counter() - t0:.0f} s wall")
    return {"env": env, "engine": eng, "scenario": scn}


def _decisions(env, cp=None):
    out = [(t, d) for topic, t, d in env if topic == "decision"]
    return [(t, d) for t, d in out if cp is None or d["result"]["checkpoint"] == cp]


def _hazard_final(env):
    last = {}
    for topic, t, d in env:
        if topic == "hazard":
            last[d["id"]] = (t, d)
    return last


def _first_status_t(env, hid, status):
    return min((t for topic, t, d in env if topic == "hazard" and d["id"] == hid
                and d["status"] == status), default=None)


def test_accident_reported_by_minutemart_and_confirmed_by_rival(story):
    env = story["env"]
    h1 = [(t, d) for t, d in _decisions(env, "CP-H1")
          if d["operator"] == "MinuteMart" and d["action_taken"] == "report_hazard"]
    assert h1, "MinuteMart rover never hit CP-H1 on the accident"
    assert 25.0 <= h1[0][0] <= 60.0, f"accident CP-H1 at t={h1[0][0]:.1f}"
    acc = [d for _, d in _hazard_final(env).values() if d["type"] == "accident"]
    assert acc and acc[0]["reporter_operator"] == "MinuteMart"
    assert any(topic == "chain_tx" and d["kind"] == "reportHazard" and d["operator"] == "MinuteMart"
               for topic, _, d in env)
    hid = acc[0]["id"]
    t_conf = _first_status_t(env, hid, "confirmed")
    assert t_conf is not None, "accident never Confirmed"
    v = [(t, d) for t, d in _decisions(env, "CP-V") if d["action_taken"] == "confirm"]
    assert v and v[0][1]["operator"] != "MinuteMart"
    assert any(topic == "chain_tx" and d["kind"] == "verifyHazard" and d["hazard_id"] == hid
               and d["operator"] != "MinuteMart" for topic, _, d in env)
    story["t_confirm"] = t_conf


def test_rovers_of_several_operators_reroute_after_confirmation(story):
    env = story["env"]
    acc = [d for _, d in _hazard_final(env).values() if d["type"] == "accident"][0]
    t_conf = _first_status_t(env, acc["id"], "confirmed")
    h2 = [(t, d) for t, d in _decisions(env, "CP-H2")
          if t >= t_conf and d["action_taken"] == "reroute"]
    ops = {d["operator"] for _, d in h2}
    assert len(ops) >= 2, f"CP-H2 reroutes after confirmation only by {ops}"
    # the reroute is visible in the state stream: the rover's route no longer uses the edge
    rerouted = {d["result"]["robot_id"] for _, d in h2}
    t_last = max(t for t, _ in h2)
    later = [d for topic, t, d in env if topic == "state" and t > t_last + 1.0]
    assert later
    for r in later[0]["robots"]:
        if r["id"] in rerouted:
            assert acc["edge_id"] not in r["route_edge_ids"]


def test_waterlogging_cp_h1(story):
    env = story["env"]
    wl = [d for _, d in _hazard_final(env).values() if d["type"] == "waterlogging"]
    assert wl, "waterlogging never reported"
    assert "rover" not in wl[0]["passable_for"]
    assert any(d["result"]["output"] and d["result"]["output"].get("type") == "waterlogging"
               for _, d in _decisions(env, "CP-H1"))


def test_fake_report_disputed_and_rejected(story):
    env = story["env"]
    fake = [d for _, d in _hazard_final(env).values()
            if d["type"] == "road_works" and d["reporter_operator"] == "DashKirana"
            and d["zg_request_id"] == "FAKE-NO-0G-CALL"]
    assert fake, "fake report never posted"
    assert fake[0]["status"] == "rejected"
    disputes = [d for _, d in _decisions(env, "CP-V") if d["action_taken"] == "dispute"]
    assert disputes and all(d["operator"] != "DashKirana" for d in disputes)
    # rivals keep their route: nobody was ever asked to act on the (never confirmed) fake report
    fid = fake[0]["id"]
    assert all(fid not in s for s in story["engine"].robot_decided_hazards.values())
    assert not any(fake[0]["edge_id"] in s for s in story["engine"].robot_blocked_edges.values())


def test_outage_safe_stop_and_recovery(story):
    env = story["env"]
    failed = [(t, d) for t, d in _decisions(env) if not d["result"]["ok"] and 200.0 <= t <= 220.5]
    assert failed, "no 0G call failed during the outage"
    stopped = {d["result"]["robot_id"] for _, d in failed}
    in_ss = [t for topic, t, d in env if topic == "state" and 200.0 <= t <= 220.0
             and any(r["fsm_state"] == "SAFE_STOP" for r in d["robots"])]
    assert in_ss, "no robot in SAFE_STOP during the outage"
    recovered = {d["result"]["robot_id"] for t, d in _decisions(env)
                 if t > 220.0 and d["result"]["ok"] and d["result"]["robot_id"] in stopped}
    assert recovered, "no SAFE_STOP robot got a real decision after restore"
    last = [d for topic, t, d in env if topic == "state"][-1]
    for r in last["robots"]:
        if r["id"] in recovered:
            assert r["fsm_state"] != "SAFE_STOP"


def test_all_operators_involved_and_budget(story):
    env = story["env"]
    decs = _decisions(env)
    assert {d["operator"] for _, d in decs} == {"ZipKart", "MinuteMart", "DashKirana"}
    ok = [d for _, d in decs if d["result"]["ok"]]
    assert 12 <= len(ok), f"only {len(ok)} decisions"
    assert len(decs) <= 90, f"{len(decs)} brain calls would exhaust the 100-call budget"
