"""Per-robot credit wallets (brain/wallets.py + ZGBrain hook). No network: local fake router."""
from __future__ import annotations

import pytest

from brain.client import ZGBrain
from brain.wallets import RobotWalletRegistry
from brain.zg_router import ZGRouter
from tests.test_brain import FakeRouter, req

KEY = "sk-test-0123456789abcdef"
CFG = {"start_micro_usd": 1000, "max_spend_micro_usd": 0, "min_call_cost_micro_usd": 90,
       "throttle_min_interval_s": 20}


@pytest.fixture
def fake():
    f = FakeRouter()
    f.billing = {"total_cost_usd_micro": 100}
    yield f
    f.close()


@pytest.fixture
def fake_eco():
    f = FakeRouter()
    f.billing = {"total_cost_usd_micro": 40}
    yield f
    f.close()


def mk(fake, wallets="auto", eco=None):
    b = ZGBrain(router=ZGRouter(base_url=fake.base_url, api_key=KEY, model="main-model", timeout_s=3),
                wallets=wallets)
    if eco is not None:
        b.set_economy_routes({"CP-H1": ZGRouter(base_url=eco.base_url, api_key=KEY, model="eco-model",
                                                timeout_s=3, trust_mode="standard", verify_tee=False)})
    return b


def call(b, robot="R-07", cp="CP-H1", seq=1):
    r = req(cp=cp, seq=seq)
    r.robot_id = robot
    if robot.startswith("D"):
        r.robot_kind = "drone"
    return b.submit(r).result(timeout=20)


def test_debit_uses_router_billing(fake):
    w = RobotWalletRegistry(CFG)
    res = call(mk(fake, w))
    assert res.ok
    assert w.balance("R-07") == 900
    wi = res.zg_trace["_hc"]["wallet"]
    assert wi["balance_micro_usd"] == 900 and wi["cost_micro_usd"] == 100
    assert wi["tier"] == "normal" and wi["model"] == "main-model"
    e = w.snapshot("R-07")["ledger"][-1]
    assert e["kind"] == "spend" and e["source"] == "router" and e["request_id"] == res.zg_request_id


def test_estimate_when_no_billing(fake):
    fake.billing = None
    w = RobotWalletRegistry({**CFG, "estimate_in_micro_usd_per_1k": 1000,
                             "estimate_out_micro_usd_per_1k": 1000})
    res = call(mk(fake, w))
    assert res.ok and res.zg_trace["_hc"]["wallet"]["cost_source"] == "estimate"
    assert w.balance("R-07") == 1000 - 15     # 10 + 5 tokens


def test_failed_call_no_debit(fake):
    fake.status, fake.error_body = 500, "boom"
    w = RobotWalletRegistry(CFG)
    res = call(mk(fake, w))
    assert not res.ok
    assert w.balance("R-07") == 1000 and w.snapshot("R-07")["ledger"] == []


def test_tier_transitions_and_paused_makes_no_call(fake, fake_eco):
    w = RobotWalletRegistry({**CFG, "start_micro_usd": 400})
    b = mk(fake, w, eco=fake_eco)
    assert w.tier("R-07") == "normal"
    assert call(b, seq=1).ok and w.balance("R-07") == 300
    assert call(b, seq=2).ok and w.balance("R-07") == 200 and w.tier("R-07") == "normal"
    assert call(b, seq=3).ok and w.balance("R-07") == 100
    assert w.tier("R-07") == "economy"
    n_main, n_eco = len(fake.requests), len(fake_eco.requests)
    res = call(b, seq=4)
    assert res.ok and len(fake_eco.requests) == n_eco + 1 and len(fake.requests) == n_main
    assert res.zg_trace["_hc"]["wallet"]["route"] == "economy"
    assert res.zg_trace["_hc"]["wallet"]["model"] == "eco-model"
    assert res.zg_trace["_hc"]["trust_mode"] == "standard"
    assert w.balance("R-07") == 60 and w.tier("R-07") == "paused"   # < min_call_cost 90
    n_main, n_eco = len(fake.requests), len(fake_eco.requests)
    res = call(b, seq=5)
    assert not res.ok and res.output is None and res.error.startswith("budget")
    assert "tier paused" in res.error
    assert (len(fake.requests), len(fake_eco.requests)) == (n_main, n_eco)   # NO model call
    assert b.online()


def test_throttled_rate_limited(fake):
    w = RobotWalletRegistry({**CFG, "start_micro_usd": 2000, "min_call_cost_micro_usd": 50})
    w.debit("R-07", 1750)     # 12.5% -> throttled
    assert w.tier("R-07") == "throttled"
    b = mk(fake, w)
    assert call(b, seq=1).ok
    n = len(fake.requests)
    res = call(b, seq=2)      # too soon
    assert not res.ok and res.error.startswith("budget") and "throttled" in res.error
    assert len(fake.requests) == n


def test_credit_idempotent_and_topup_recovers(fake):
    w = RobotWalletRegistry({**CFG, "start_micro_usd": 100})
    w.debit("R-07", 100)
    assert w.tier("R-07") == "paused"
    assert w.credit("R-07", 500, ref="hazard:1:report", source="chain", tx_hash="0xabc")
    assert not w.credit("R-07", 500, ref="hazard:1:report", source="chain")
    assert w.balance("R-07") == 500 and w.snapshot("R-07")["earned"] == 500
    assert w.tier("R-07") == "normal"
    assert call(mk(fake, w)).ok


def test_spend_cap_pauses():
    w = RobotWalletRegistry({**CFG, "max_spend_micro_usd": 300})
    w.debit("R-07", 300)
    assert w.balance("R-07") == 700 and w.tier("R-07") == "paused"


def test_per_robot_isolation(fake):
    w = RobotWalletRegistry(CFG)
    b = mk(fake, w)
    assert call(b, "R-07").ok
    assert w.balance("R-07") == 900 and w.balance("D-01") == 1000
    w.debit("D-01", 1000)
    assert w.tier("D-01") == "paused" and w.tier("R-07") == "normal"
    assert call(b, "R-07", seq=2).ok
    assert not call(b, "D-01", seq=3).ok
    assert w.snapshot("D-01")["kind"] == "drone"


def test_disabled_identical(fake, monkeypatch):
    monkeypatch.delenv("ZG_WALLETS", raising=False)
    b = mk(fake)                                  # settings: enabled false
    assert b.wallets is None
    res = call(b)
    assert res.ok and "wallet" not in res.zg_trace["_hc"]
    monkeypatch.setenv("ZG_WALLETS", "1")
    assert mk(fake).wallets is not None
