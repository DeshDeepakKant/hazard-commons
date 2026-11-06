"""testnet_router_mock: local billing stand-in, upstream faked with httpx.MockTransport (no network)."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from testnet_router_mock.app import cost_micro_usd, create_app
from testnet_router_mock.ledger import DepositIndexer, Ledger

UPSTREAM = "https://upstream.invalid/v1"
KEY = "sk-upstream-key-0123456789abcdef"
ACCT = {"X-HC-Account": "R-04"}


class FakeUpstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.billing: dict | None = {"currency": "usd", "total_cost_usd_micro": "64"}
        self.usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        self.raise_exc: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "0gm-1.0-35b-a3b"}]})
        if self.status != 200:
            return httpx.Response(self.status, headers={"X-Request-ID": "req-err"},
                                  content=b'{"error":{"type":"provider_error","message":"boom"}}')
        trace = {"request_id": "req-abc123", "tee_verified": True}
        if self.billing is not None:
            trace["billing"] = self.billing
        body = {"id": "chatcmpl-x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "UPSTREAM-ANSWER"}}],
                "usage": self.usage, "x_0g_trace": trace}
        return httpx.Response(200, headers=[("X-Request-ID", "edge-1"), ("X-Request-ID", "req-abc123"),
                                            ("x-0g-e2ee", "on")],
                              content=json.dumps(body, separators=(",", ":")).encode())


class FakeVault:
    def __init__(self):
        self.latest = 100
        self.deps: list[dict] = []
        self.calls: list[tuple] = []
        self.fail = False

    def latest_block(self) -> int:
        if self.fail:
            raise ConnectionError("rpc down")
        return self.latest

    def deposits_since(self, from_block, to_block=None):
        self.calls.append((from_block, to_block))
        return [d for d in self.deps if from_block <= d["block"] <= (to_block or 10**12)]


def dep(robot="R-04", wei=10**13, tx="0xAA", li=0, block=95):
    return {"robot_id": robot, "from": "0xabc", "amount_wei": wei, "total_wei": wei,
            "tx_hash": tx, "log_index": li, "block": block}


@pytest.fixture
def env():
    up, vault = FakeUpstream(), FakeVault()
    ledger = Ledger(start_micro_usd=0)
    idx = DepositIndexer(ledger, source=vault, poll_s=999)
    app = create_app(ledger, idx, upstream_base_url=UPSTREAM, api_key=KEY,
                     transport=httpx.MockTransport(up.handler))
    return TestClient(app), up, vault, ledger


def chat_body():
    return {"model": "0gm-1.0-35b-a3b", "max_tokens": 20, "verify_tee": True,
            "messages": [{"role": "user", "content": "hi"}]}


def test_missing_account_header_400(env):
    c, up, *_ = env
    for r in (c.get("/v1/account/balance"), c.post("/v1/chat/completions", json=chat_body()),
              c.post("/v1/account/sync")):
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "missing_account"
        assert r.headers["X-HC-Mock"] == "testnet-router-billing-stand-in"
    assert up.requests == []


def test_zero_balance_402_no_upstream_call(env):
    c, up, *_ = env
    r = c.post("/v1/chat/completions", json=chat_body(), headers=ACCT)
    assert r.status_code == 402
    assert r.json()["error"]["type"] == "insufficient_balance"
    assert r.headers["X-HC-Mock"] == "testnet-router-billing-stand-in"
    assert up.requests == []


def test_deposit_credited_once(env):
    c, _, vault, ledger = env
    vault.deps = [dep(wei=10**13, tx="0xAA", li=0), dep(robot="R-07", wei=2 * 10**13, tx="0xBB")]
    r = c.post("/v1/account/sync", headers=ACCT)
    assert r.status_code == 200
    assert r.json()["total_balance_micro_usd"] == 500           # 0.00001 0G * 50e6 µUSD/0G
    assert r.json()["sync"]["credited_deposits"] == 2
    assert vault.calls[0] == (0, 100)                          # latest - 2000, clamped at 0
    # same deposit reported again (cursor rewound / re-org) -> not credited twice
    ledger.cursor = 0
    vault.latest = 120
    r = c.post("/v1/account/sync", headers=ACCT)
    assert r.json()["total_balance_micro_usd"] == 500
    assert r.json()["sync"]["credited_deposits"] == 0
    assert vault.calls[-1] == (0, 120)
    assert c.get("/v1/account/balance", headers={"X-HC-Account": "R-07"}).json()[
        "total_balance_micro_usd"] == 1000
    # a second log in the same tx is a distinct deposit
    vault.deps.append(dep(wei=10**13, tx="0xAA", li=1, block=110))
    ledger.cursor = 101
    assert c.post("/v1/account/sync", headers=ACCT).json()["total_balance_micro_usd"] == 1000


def test_proxy_forwards_replaces_auth_returns_body_and_debits(env):
    c, up, vault, ledger = env
    vault.deps = [dep(wei=10**13)]
    c.post("/v1/account/sync", headers=ACCT)
    body = chat_body()
    raw = json.dumps(body).encode()
    r = c.post("/v1/chat/completions", content=raw, headers={
        **ACCT, "Authorization": "Bearer robot-supplied-key", "Content-Type": "application/json",
        "X-0G-Provider-Trust-Mode": "private", "X-Client-Request-Id": "ep-1:3", "X-Other": "nope"})
    assert r.status_code == 200
    sent = up.requests[0]
    assert str(sent.url) == UPSTREAM + "/chat/completions"
    assert sent.content == raw
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["x-0g-provider-trust-mode"] == "private"
    assert sent.headers["x-client-request-id"] == "ep-1:3"
    assert sent.headers["content-type"] == "application/json"
    assert "x-other" not in sent.headers and "x-hc-account" not in sent.headers
    # upstream body and ids returned unchanged
    assert r.content == up.handler(httpx.Request("POST", UPSTREAM + "/chat/completions")).content
    assert r.json()["choices"][0]["message"]["content"] == "UPSTREAM-ANSWER"
    assert r.headers.get_list("X-Request-ID") == ["edge-1", "req-abc123"]
    assert r.headers["x-0g-e2ee"] == "on"
    assert r.headers["X-HC-Mock"] == "testnet-router-billing-stand-in"
    b = c.get("/v1/account/balance", headers=ACCT).json()
    assert b["total_balance_micro_usd"] == 500 - 64
    assert b["total_spent_micro_usd"] == 64


def test_debit_estimated_from_usage_when_billing_missing(env):
    c, up, _, ledger = env
    ledger.credit_deposit(dep(wei=10**13))
    up.billing = None
    assert c.post("/v1/chat/completions", json=chat_body(), headers=ACCT).status_code == 200
    assert ledger.balance("R-04") == 500 - (20 + 30)   # 1k prompt * 20 + 0.5k completion * 60
    assert cost_micro_usd({"x_0g_trace": {"billing": {"total_cost_usd_micro": 77}}}) == 77


def test_upstream_500_passes_through_no_debit(env):
    c, up, _, ledger = env
    ledger.credit_deposit(dep(wei=10**13))
    up.status = 500
    r = c.post("/v1/chat/completions", json=chat_body(), headers=ACCT)
    assert r.status_code == 500
    assert r.content == b'{"error":{"type":"provider_error","message":"boom"}}'
    assert r.headers["X-Request-ID"] == "req-err"
    assert ledger.balance("R-04") == 500


def test_upstream_unreachable_502_no_debit(env):
    c, up, _, ledger = env
    ledger.credit_deposit(dep(wei=10**13))
    up.raise_exc = httpx.ConnectError("tls handshake dropped")
    r = c.post("/v1/chat/completions", json=chat_body(), headers=ACCT)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_unreachable"
    assert ledger.balance("R-04") == 500


def test_balance_shape_and_models_proxy(env):
    c, up, *_ = env
    b = c.get("/v1/account/balance", headers=ACCT).json()
    assert set(b) >= {"account", "total_balance_micro_usd", "total_deposited_wei",
                      "total_spent_micro_usd", "currency", "deposits", "mock", "note"}
    assert b["account"] == "R-04" and b["mock"] is True and b["currency"] == "usd"
    assert isinstance(b["total_deposited_wei"], str) and b["deposits"] == []
    m = c.get("/v1/models")
    assert m.status_code == 200 and m.json()["data"][0]["id"] == "0gm-1.0-35b-a3b"
    assert up.requests[-1].headers["authorization"] == f"Bearer {KEY}"


def test_vault_failure_keeps_serving(env):
    c, _, vault, _ = env
    vault.fail = True
    r = c.post("/v1/account/sync", headers=ACCT)
    assert r.status_code == 200
    assert "rpc down" in r.json()["sync"]["last_error"]


def test_missing_vault_module_does_not_break_import():
    def factory():
        raise ImportError("No module named 'chain.credit_vault'")
    idx = DepositIndexer(Ledger(), source_factory=factory)
    assert idx.poll_once() == 0 and "ImportError" in idx.last_error


def test_loop_guard(env):
    c, up, *_ = env
    r = c.post("/v1/chat/completions", json=chat_body(), headers={**ACCT, "X-HC-Mock-Hop": "1"})
    assert r.status_code == 508 and up.requests == []


def test_state_file_roundtrip(tmp_path):
    f = str(tmp_path / "s.json")
    l1 = Ledger(start_micro_usd=7, state_file=f)
    l1.credit_deposit(dep(wei=10**13))
    l1.debit("R-04", 10)
    l1.set_cursor(55)
    l2 = Ledger(start_micro_usd=7, state_file=f)
    assert l2.balance("R-04") == 7 + 500 - 10 and l2.cursor == 55
    assert l2.credit_deposit(dep(wei=10**13)) is False         # seen set persisted
    assert l2.balance("R-09") == 7


def test_bytes32_robot_id_maps_to_account():
    from testnet_router_mock.ledger import normalize_account
    assert normalize_account("0x" + b"R-04".ljust(32, b"\0").hex()) == "R-04"
    assert normalize_account(b"R-04".ljust(32, b"\0")) == "R-04"
    assert normalize_account(" R-04 ") == "R-04"
