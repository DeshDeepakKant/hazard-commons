"""Self-funding loop for robot wallets ("Agents that pay for themselves").

When a robot body's credit falls to the economy tier (or lower), its operator's agent reinvests the
operator's ON-CHAIN earnings into that robot's compute credit, with no human:

  1. real Galileo tx: operator wallet -> ComputeCreditVault.deposit(robot_id) (testnet 0G, tiny)
  2. the testnet-Router billing stand-in (testnet_router_mock/, because the real testnet Router API was
     unreachable all event) indexes the Deposited event and credits the robot's account
  3. we read the robot's balance from that Router API (GET /v1/account/balance) and credit the robot's
     wallet with exactly what the Router credited, as a ledger "topup" carrying the tx hash

Spending rule: an operator may only reinvest what it has earned on chain in this run (HazardRegistry
payouts for its confirmed hazard reports), tracked by add_operator_budget(). Nothing here makes or
alters a 0G decision; it only moves credit.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("hazard.topup")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "router_mock_url": "http://localhost:8790/v1",
    "amount_wei": 10_000_000_000_000,               # 0.00001 testnet 0G per top-up
    "trigger_tiers": ["economy", "throttled", "paused"],
    "max_topups_per_robot": 3,
    "budget_per_confirmed_hazard_wei": 100_000_000_000_000,   # = the 0.0001 0G stake refunded on confirm
    "initial_operator_budget_wei": 0,                # only on-chain earnings may be reinvested
    "poll_s": 3.0,
    "credit_timeout_s": 90.0,
}


def topup_settings() -> dict[str, Any]:
    from common.config import settings
    user = (((settings().get("brain", {}) or {}).get("wallets", {}) or {}).get("topup", {}) or {})
    cfg = {**DEFAULTS, **user}
    v = os.environ.get("ZG_TOPUP")
    if v is not None and v.strip():
        cfg["enabled"] = v.strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("ZG_TOPUP_ROUTER_URL"):
        cfg["router_mock_url"] = os.environ["ZG_TOPUP_ROUTER_URL"]
    return cfg


class RouterBalanceClient:
    """Reads a robot account on the (stand-in) testnet Router billing API."""

    def __init__(self, base_url: str, timeout_s: float = 10.0):
        import httpx
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout_s)

    def balance(self, robot_id: str, sync: bool = False) -> dict[str, Any]:
        h = {"X-HC-Account": robot_id}
        if sync:
            r = self._http.post(f"{self.base_url}/account/sync", headers=h)
        else:
            r = self._http.get(f"{self.base_url}/account/balance", headers=h)
        r.raise_for_status()
        return r.json()


class TopupAgent:
    def __init__(self, wallets: Any, deposit_fn: Callable[[str, int, str], str],
                 router: Any, operator_keys: dict[str, str], cfg: Optional[dict[str, Any]] = None,
                 on_event: Optional[Callable[[dict[str, Any]], None]] = None):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.wallets = wallets
        self.deposit_fn = deposit_fn          # (robot_id, amount_wei, private_key) -> tx_hash (waits for receipt)
        self.router = router                  # .balance(robot_id, sync=bool) -> dict
        self._keys = operator_keys            # operator name -> private key (never logged)
        self.on_event = on_event
        self._lock = threading.Lock()
        self._budget: dict[str, int] = {op: int(self.cfg["initial_operator_budget_wei"]) for op in operator_keys}
        self._budget_refs: set[str] = set()
        self._count: dict[str, int] = {}
        self._inflight: set[str] = set()
        self.history: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- budget (on-chain earnings)
    def add_operator_budget(self, operator: str, wei: int, ref: str) -> bool:
        with self._lock:
            if not operator or ref in self._budget_refs or wei <= 0:
                return False
            self._budget_refs.add(ref)
            self._budget[operator] = self._budget.get(operator, 0) + int(wei)
            return True

    def budget(self, operator: str) -> int:
        with self._lock:
            return self._budget.get(operator, 0)

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="topup-agent")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(float(self.cfg["poll_s"])):
            try:
                self.check_all()
            except Exception:
                log.exception("top-up check failed")

    def check_all(self) -> list[threading.Thread]:
        started = []
        for snap in self.wallets.snapshot_all() if hasattr(self.wallets, "snapshot_all") else []:
            t = self.maybe_topup(snap)
            if t is not None:
                started.append(t)
        return started

    def maybe_topup(self, snap: dict[str, Any]) -> Optional[threading.Thread]:
        rid, op, tier = snap["robot_id"], snap.get("operator", ""), snap.get("tier")
        amt = int(self.cfg["amount_wei"])
        with self._lock:
            if tier not in self.cfg["trigger_tiers"] or rid in self._inflight:
                return None
            if self._count.get(rid, 0) >= int(self.cfg["max_topups_per_robot"]):
                return None
            if op not in self._keys or self._budget.get(op, 0) < amt:
                return None
            self._budget[op] -= amt                 # reserve; refunded if the tx fails
            self._inflight.add(rid)
            self._count[rid] = self._count.get(rid, 0) + 1
        t = threading.Thread(target=self._do_topup, args=(rid, op, amt, tier), daemon=True,
                             name=f"topup-{rid}")
        t.start()
        return t

    def _do_topup(self, rid: str, op: str, amt: int, tier: str) -> None:
        ev: dict[str, Any] = {"robot_id": rid, "operator": op, "amount_wei": str(amt), "tier_before": tier,
                              "t_wall": time.time()}
        try:
            log.info("top-up: %s (%s) at tier %s -> depositing %d wei to ComputeCreditVault", rid, op, tier, amt)
            tx = self.deposit_fn(rid, amt, self._keys[op])
            ev["tx_hash"] = tx
            credited = self._wait_router_credit(rid, tx)
            ev["credited_micro_usd"] = credited
            ok = self.wallets.credit(rid, credited, kind="topup", source="chain", ref=f"vault:{tx}",
                                     tx_hash=tx, note=f"{op} reinvested on-chain earnings: ComputeCreditVault "
                                                      f"deposit, credited by testnet-Router stand-in")
            ev["status"] = "credited" if ok else "duplicate"
            log.info("top-up: %s credited %d micro-USD (tx %s)", rid, credited, tx)
        except Exception as e:
            ev["status"] = "failed"
            ev["error"] = str(e)[:200]
            log.warning("top-up for %s failed: %s", rid, e)
            with self._lock:
                if "tx_hash" not in ev:             # nothing left the wallet: give the budget back
                    self._budget[op] = self._budget.get(op, 0) + amt
                    self._count[rid] = max(0, self._count.get(rid, 1) - 1)
        finally:
            with self._lock:
                self._inflight.discard(rid)
                self.history.append(ev)
            if self.on_event:
                try:
                    self.on_event(ev)
                except Exception:
                    log.exception("top-up event callback failed")

    def _wait_router_credit(self, rid: str, tx: str) -> int:
        """Poll the Router billing API (forcing an index sync) until it lists this deposit tx."""
        deadline = time.time() + float(self.cfg["credit_timeout_s"])
        while time.time() < deadline:
            try:
                bal = self.router.balance(rid, sync=True)
                for d in bal.get("deposits", []) or []:
                    if str(d.get("tx_hash", "")).lower() == tx.lower():
                        return int(d.get("credited_micro_usd", 0))
            except Exception as e:
                log.debug("router balance read failed: %s", e)
            if self._stop.wait(2.0):
                break
        raise RuntimeError(f"router did not credit deposit {tx} within {self.cfg['credit_timeout_s']} s")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": True, "budget_wei": {k: str(v) for k, v in self._budget.items()},
                    "topups": list(self.history[-20:]), "inflight": sorted(self._inflight)}


def build_topup_agent(wallets: Any, on_event: Optional[Callable[[dict[str, Any]], None]] = None
                      ) -> Optional[TopupAgent]:
    """Real wiring: Galileo ComputeCreditVault + operator keys from .env + router stand-in URL."""
    cfg = topup_settings()
    if not cfg.get("enabled") or wallets is None:
        return None
    from chain.bindings import operator_key_env
    from chain.credit_vault import CreditVault
    keys = {}
    for op in ("ZipKart", "MinuteMart", "DashKirana"):
        k = os.environ.get(operator_key_env(op))
        if k:
            keys[op] = k
    if not keys:
        log.warning("top-up enabled but no operator keys in env; disabled")
        return None
    vault = CreditVault()

    def deposit(robot_id: str, amount_wei: int, private_key: str) -> str:
        return vault.deposit(robot_id, amount_wei, private_key)

    agent = TopupAgent(wallets, deposit, RouterBalanceClient(cfg["router_mock_url"]), keys, cfg, on_event)
    log.info("top-up agent on: vault %s, router %s, %d wei per top-up", vault.address,
             cfg["router_mock_url"], int(cfg["amount_wei"]))
    return agent
