"""Top-up agent (brain/topup.py): no network, fake vault + fake router billing API."""
from __future__ import annotations

from brain.topup import TopupAgent
from brain.wallets import RobotWalletRegistry

CFG = {"amount_wei": 10, "max_topups_per_robot": 2, "budget_per_confirmed_hazard_wei": 100,
       "credit_timeout_s": 5, "poll_s": 0.05}


class FakeVault:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, robot_id, amount_wei, key):
        if self.fail:
            raise RuntimeError("reverted")
        self.calls.append((robot_id, amount_wei, key))
        return f"0xtx{len(self.calls)}"


class FakeRouter:
    def __init__(self, vault, rate=50):
        self.vault, self.rate = vault, rate

    def balance(self, robot_id, sync=False):
        deps = [{"tx_hash": f"0xtx{i + 1}", "credited_micro_usd": a * self.rate}
                for i, (r, a, _k) in enumerate(self.vault.calls) if r == robot_id]
        return {"account": robot_id, "deposits": deps}


def mk(fail=False):
    w = RobotWalletRegistry({"start_micro_usd": 1000, "max_spend_micro_usd": 0, "min_call_cost_micro_usd": 40})
    w.register("R-04", "MinuteMart", "rover")
    v = FakeVault(fail)
    a = TopupAgent(w, v, FakeRouter(v), {"MinuteMart": "k-mm"}, CFG)
    return w, v, a


def drain(w, to=300):
    w.debit("R-04", w.balance("R-04") - to, ref="x")


def run(a, w):
    ts = a.check_all()
    for t in ts:
        t.join(5)
    return ts


def test_no_topup_without_onchain_earnings():
    w, v, a = mk()
    drain(w)
    assert w.tier("R-04") == "economy"
    assert run(a, w) == [] and v.calls == []


def test_topup_spends_earned_budget_and_credits_router_amount():
    w, v, a = mk()
    assert a.add_operator_budget("MinuteMart", 100, "hazard:1")
    assert not a.add_operator_budget("MinuteMart", 100, "hazard:1")      # idempotent per hazard
    drain(w)
    run(a, w)
    assert v.calls == [("R-04", 10, "k-mm")]
    assert w.balance("R-04") == 300 + 10 * 50
    e = w.snapshot("R-04")["ledger"][-1]
    assert e["kind"] == "topup" and e["tx_hash"] == "0xtx1" and e["source"] == "chain"
    assert a.budget("MinuteMart") == 90
    assert a.history[-1]["status"] == "credited"


def test_normal_tier_not_topped_up_and_cap_per_robot():
    w, v, a = mk()
    a.add_operator_budget("MinuteMart", 1000, "hazard:1")
    run(a, w)
    assert v.calls == []                                   # normal tier
    for _ in range(4):
        drain(w, 100)
        run(a, w)
    assert len(v.calls) == 2                               # max_topups_per_robot


def test_failed_deposit_refunds_budget():
    w, v, a = mk(fail=True)
    a.add_operator_budget("MinuteMart", 100, "hazard:1")
    drain(w)
    run(a, w)
    assert a.budget("MinuteMart") == 100 and a.history[-1]["status"] == "failed"
    assert w.balance("R-04") == 300
