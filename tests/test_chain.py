"""End-to-end HazardRegistry test on a throwaway anvil node through chain.bindings.HazardChain.

Needs Foundry (anvil + forge) at ~/.foundry/bin or on PATH, and chain/foundry/lib/forge-std only for
`forge test` (not for this test). Skips cleanly if anvil is missing.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from chain import bindings as B
from chain.deploy import build, deploy_registry
from sim.schemas import ChainTxEvent

ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
STAKE = 10**14


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def anvil():
    if not Path(ANVIL).exists():
        pytest.skip("anvil not installed")
    port = _free_port()
    proc = subprocess.Popen([ANVIL, "--port", str(port), "--silent"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(url))
    for _ in range(100):
        try:
            if w3.is_connected():
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("anvil did not start")
    yield url, w3
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def chain(anvil):
    url, w3 = anvil
    deployer = Account.create()
    keys = {n: Account.create().key.hex() for n in B.OPERATORS}
    for a in [deployer.address, *(Account.from_key(k).address for k in keys.values())]:
        w3.provider.make_request("anvil_setBalance", [a, hex(10**20)])
    ops = {n: Account.from_key(k).address for n, k in keys.items()}
    dep = deploy_registry(w3, deployer.key.hex(), ops, min_stake=STAKE, confirms=1, disputes=1,
                          reward=STAKE // 10, art=build(), log=lambda *_: None)
    reg = w3.eth.contract(address=dep["HazardRegistry"]["address"], abi=B.load_abi())
    B.transact(w3, deployer, reg.functions.fundRewards(), value=10**15)
    txs: list[ChainTxEvent] = []
    hc = B.HazardChain(url, dep["HazardRegistry"]["address"], keys, start_block=dep["HazardRegistry"]["block"],
                       on_tx=txs.append, sim_time_fn=lambda: 42.0)
    hc._txs = txs  # test hook
    hc._dep = dep
    yield hc
    hc.close()


def test_helpers():
    assert B.robot_id_bytes("R-01") == bytes(Web3.keccak(text="R-01"))
    assert B.edge_id_bytes("e1") == bytes(Web3.keccak(text="e1"))
    assert B.severity_to_u8(0.8) == 80 and B.severity_to_u8(75) == 75 and B.severity_to_u8(7.0) == 7
    assert B.passable_mask(["rover", "drone"]) == 3 and B.mask_to_passable(2) == ["drone"]
    assert B.to_bytes32(None) == b"\x00" * 32
    with pytest.raises(ValueError):
        B.to_bytes32("0x1234")


def test_full_scenario(chain: B.HazardChain):
    root = "0x" + "11" * 32
    # 1. MinuteMart rover reports a real accident; ZipKart drone confirms -> Confirmed (+refund +reward)
    r = chain.report_hazard("MinuteMart", "R-04", "e-hill-road-3", "accident", 0.8, root, "zg-req-1",
                            passable_for=["drone"]).result(timeout=60)
    assert r["status"] == "mined", r
    hid = r["hazard_id"]
    assert hid == "1" and r["tx_hash"].startswith("0x") and r["gas_used"] > 0

    # same-operator verify is rejected at estimation (no gas spent)
    bad = chain.verify_hazard("MinuteMart", "R-05", hid, True, root, "zg-req-x").result(timeout=60)
    assert bad["status"] == "failed" and bad["error"] == "SameOperator", bad

    mm_before = chain.balance_wei("MinuteMart")
    v = chain.verify_hazard("ZipKart", "D-01", hid, True, root, "zg-req-2").result(timeout=60)
    assert v["status"] == "mined" and v["resolved"] == "confirmed", v
    assert chain.balance_wei("MinuteMart") == mm_before + STAKE + STAKE // 10  # refund + reward

    # a second vote after resolution fails cleanly
    late = chain.verify_hazard("DashKirana", "R-07", hid, False, None, "").result(timeout=60)
    assert late["status"] == "failed" and late["error"] == "WrongStatus"

    # 2. DashKirana posts a fake road-works report; ZipKart disputes -> Rejected, stake slashed to ZipKart
    f = chain.report_hazard("DashKirana", "R-07", "e-linking-rd-9", "road_works", 0.6, None,
                            "zg-req-3").result(timeout=60)
    assert f["status"] == "mined", f
    fid = f["hazard_id"]
    zk_before = chain.balance_wei("ZipKart")
    d = chain.verify_hazard("ZipKart", "D-01", fid, False, root, "zg-req-4").result(timeout=60)
    assert d["status"] == "mined" and d["resolved"] == "rejected", d
    gas_cost = d["gas_used"] * chain.w3.eth.get_transaction(d["tx_hash"])["gasPrice"]
    assert chain.balance_wei("ZipKart") == zk_before - gas_cost + STAKE

    # 3. clear the confirmed one; clearing the rejected one fails
    c = chain.clear_hazard("DashKirana", hid, root, "zg-req-5").result(timeout=60)
    assert c["status"] == "mined", c
    assert chain.clear_hazard("ZipKart", fid).result(timeout=60)["error"] == "WrongStatus"

    # 4. delivery receipt + validation failures never raise
    dl = chain.record_delivery("ZipKart", "O-001", "R-01", root).result(timeout=60)
    assert dl["status"] == "mined"
    assert chain.report_hazard("Nobody", "R-01", "e1", "accident", 0.5, None, "").result()["status"] == "failed"
    assert chain.report_hazard("ZipKart", "R-01", "e1", "none", 0.5, None, "").result()["status"] == "failed"
    assert chain.report_hazard("ZipKart", "R-01", "e1", "accident", 0.5, "0xnothex", "").result()["status"] == "failed"
    low = chain.report_hazard("ZipKart", "R-01", "e1", "accident", 0.5, None, "", stake_wei=STAKE - 1).result()
    assert low["error"] == "StakeTooLow"

    # 5. events rebuild the map
    evs = chain.poll_events()
    names = [e["event"] for e in evs]
    for n in ("OperatorRegistered", "RobotsRegistered", "HazardReported", "HazardVerified", "HazardResolved",
              "HazardCleared", "DeliveryRecorded"):
        assert n in names, n
    rep = next(e for e in evs if e["event"] == "HazardReported")
    assert rep["args"]["edgeName"] == "e-hill-road-3" and rep["args"]["zgRequestId"] == "zg-req-1"
    assert rep["args"]["edgeId"] == "0x" + B.edge_id_bytes("e-hill-road-3").hex()
    assert chain.poll_events() == []  # cursor advanced

    hz = {h.id: h for h in chain.hazards()}
    h1, h2 = hz[hid], hz[fid]
    assert (h1.status, h1.type, h1.edge_id, h1.reporter_operator, h1.reporter_robot) == \
        ("cleared", "accident", "e-hill-road-3", "MinuteMart", "R-04")
    assert h1.severity == pytest.approx(0.8) and h1.passable_for == ["drone"] and h1.confirms == 1
    assert h1.evidence_root == root and h1.zg_request_id == "zg-req-1" and h1.tx_hash == r["tx_hash"]
    assert (h2.status, h2.type, h2.reporter_operator, h2.disputes, h2.evidence_root) == \
        ("rejected", "road_works", "DashKirana", 1, None)

    # 6. a fresh subscriber rebuilds the same map from logs alone
    fresh = B.HazardChain(chain.w3.provider.endpoint_uri, chain.address, {},
                          start_block=chain._dep["HazardRegistry"]["block"])
    fresh.poll_events()
    assert [h.model_dump() for h in fresh.hazards()] == [h.model_dump() for h in chain.hazards()]

    # 7. on_tx callbacks: sent + mined for each tx
    kinds = {(t.kind, t.status) for t in chain._txs}
    assert ("reportHazard", "sent") in kinds and ("reportHazard", "mined") in kinds
    assert ("verifyHazard", "mined") in kinds and ("clearHazard", "mined") in kinds
    assert ("registerRobot", "mined") in kinds
    assert all(t.sim_time == 42.0 for t in chain._txs)


def test_background_poller(chain: B.HazardChain):
    chain.start_polling(interval_s=0.2)
    try:
        r = chain.report_hazard("ZipKart", "R-01", "e-bg", "waterlogging", 0.4, None, "zg-bg").result(timeout=60)
        assert r["status"] == "mined"
        got = []
        for _ in range(50):
            got += chain.poll_events()
            if any(e["event"] == "HazardReported" and e["args"]["hazardId"] == int(r["hazard_id"]) for e in got):
                break
            time.sleep(0.1)
        else:
            pytest.fail("background poller did not deliver the event")
        assert chain.hazard(r["hazard_id"]).type == "waterlogging"
    finally:
        chain.stop_polling()


def test_poll_survives_rpc_outage(chain: B.HazardChain):
    dead = B.HazardChain("http://127.0.0.1:1", chain.address, {}, start_block=0, rpc_timeout=1)
    assert dead.poll_events() == []
    assert dead.hazards() == []
