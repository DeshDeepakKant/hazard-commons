"""Per-robot credit wallets: every robot body (rovers R-0x, drones D-0x) pays for its OWN 0G calls.

A wallet holds micro-USD credit. ZGBrain debits the router's reported cost after each call; income
(chain-confirmed hazards, simulated delivery fees, manual/test top-ups) credits it. Tiers degrade the
robot's brain honestly, never with a fake decision:

    normal     balance >= economy_fraction  of start   -> default route
    economy    balance >= throttled_fraction of start  -> cheaper 0G route (brain.wallets.economy_profile)
    throttled  balance >= min_call_cost                -> economy route + min wall seconds between calls
    paused     cannot afford a call (or spend cap hit) -> no model call; checkpoint fails -> SAFE_STOP

Every ledger entry carries a `source` ('router' | 'chain' | 'simulated' | 'manual') so the UI can
label what is real and what is simulated. Thread-safe. No network.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("hazard.wallets")

TIERS = ("normal", "economy", "throttled", "paused")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "start_micro_usd": 3000,            # ~30 calls at ~90 micro-USD
    "max_spend_micro_usd": 6000,        # lifetime per-robot spend cap (0 = none)
    "economy_fraction": 0.40,           # balance/start below this -> economy
    "throttled_fraction": 0.15,         # below this -> throttled
    "min_call_cost_micro_usd": 90,      # cannot afford a call below this -> paused
    "throttle_min_interval_s": 20.0,    # throttled: min wall seconds between calls per robot
    "economy_profile": "economy",       # brain.profiles.<name>
    "ledger_size": 200,
    "estimate_in_micro_usd_per_1k": 20.0,    # fallback when the router reports no billing
    "estimate_out_micro_usd_per_1k": 60.0,
    "income": {
        "hazard_reward_micro_usd": 600,      # chain: reporter of a confirmed hazard (stake refund + reward)
        "verifier_reward_micro_usd": 150,    # chain: drone whose verdict matched the outcome
        "delivery_fee_micro_usd": 300,       # SIMULATED flat fee per delivered order
    },
}


def wallet_settings() -> dict[str, Any]:
    from common.config import settings
    cfg = dict(DEFAULTS)
    user = ((settings().get("brain", {}) or {}).get("wallets", {}) or {})
    for k, v in user.items():
        if k == "income":
            cfg["income"] = {**DEFAULTS["income"], **(v or {})}
        else:
            cfg[k] = v
    return cfg


def wallets_enabled(cfg: Optional[dict[str, Any]] = None) -> bool:
    """settings brain.wallets.enabled, overridden by env ZG_WALLETS (1/true/on vs 0/false/off)."""
    v = os.environ.get("ZG_WALLETS")
    if v is not None and v.strip() != "":
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool((cfg or wallet_settings()).get("enabled", False))


@dataclass
class Gate:
    allowed: bool
    tier: str
    reason: str = ""


class _Wallet:
    def __init__(self, robot_id: str, operator: str, kind: str, start: int, ledger_size: int):
        self.robot_id, self.operator, self.kind = robot_id, operator, kind
        self.start = int(start)
        self.balance = int(start)
        self.spent = 0
        self.earned = 0
        self.topped_up = 0
        self.ledger: deque[dict[str, Any]] = deque(maxlen=ledger_size)
        self.last_call_wall = 0.0
        self.logged_tier = "normal"
        self.refs: set[str] = set()


class RobotWalletRegistry:
    def __init__(self, cfg: Optional[dict[str, Any]] = None):
        c = dict(DEFAULTS)
        for k, v in (cfg or {}).items():
            c[k] = {**DEFAULTS["income"], **(v or {})} if k == "income" else v
        self.cfg = c
        self.start_micro_usd = int(c["start_micro_usd"])
        self.max_spend = int(c["max_spend_micro_usd"] or 0)
        self.economy_fraction = float(c["economy_fraction"])
        self.throttled_fraction = float(c["throttled_fraction"])
        self.min_call_cost = int(c["min_call_cost_micro_usd"])
        self.throttle_interval_s = float(c["throttle_min_interval_s"])
        self.economy_profile = c.get("economy_profile")
        self.income: dict[str, Any] = c["income"]
        self._ledger_size = int(c["ledger_size"])
        self._lock = threading.RLock()
        self._w: dict[str, _Wallet] = {}

    @classmethod
    def from_settings(cls) -> "RobotWalletRegistry":
        return cls(wallet_settings())

    # ------------------------------------------------------------------ lookup
    def register(self, robot_id: str, operator: str = "", kind: Optional[str] = None,
                 start_micro_usd: Optional[int] = None) -> None:
        with self._lock:
            w = self._get(robot_id)
            if operator:
                w.operator = operator
            if kind:
                w.kind = kind
            if start_micro_usd is not None and not w.ledger and w.spent == 0:
                w.start = w.balance = int(start_micro_usd)

    def _get(self, robot_id: str) -> _Wallet:
        w = self._w.get(robot_id)
        if w is None:
            kind = "drone" if robot_id.upper().startswith("D") else "rover"
            w = self._w[robot_id] = _Wallet(robot_id, "", kind, self.start_micro_usd, self._ledger_size)
        return w

    def _tier_of(self, w: _Wallet) -> str:
        if w.balance < self.min_call_cost or (self.max_spend and w.spent >= self.max_spend):
            return "paused"
        frac = w.balance / w.start if w.start > 0 else 0.0
        if frac >= self.economy_fraction:
            return "normal"
        if frac >= self.throttled_fraction:
            return "economy"
        return "throttled"

    def _note_tier(self, w: _Wallet) -> None:
        t = self._tier_of(w)
        if t != w.logged_tier:
            log.warning("wallet %s: tier %s -> %s (balance %d of %d micro-USD)",
                        w.robot_id, w.logged_tier, t, w.balance, w.start)
            w.logged_tier = t

    def tier(self, robot_id: str) -> str:
        with self._lock:
            return self._tier_of(self._get(robot_id))

    def balance(self, robot_id: str) -> int:
        with self._lock:
            return self._get(robot_id).balance

    # ------------------------------------------------------------------ gating & debit
    def authorize(self, robot_id: str, now: Optional[float] = None,
                  rate_limit: bool = True) -> Gate:
        """May this robot make a model call now? Throttled robots are rate limited (wall clock);
        an allowed throttled call stamps the clock. Paused robots never pass."""
        now = time.monotonic() if now is None else now
        with self._lock:
            w = self._get(robot_id)
            t = self._tier_of(w)
            self._note_tier(w)
            if t == "paused":
                return Gate(False, t, "budget: robot credit exhausted (tier paused)")
            if t == "throttled" and rate_limit:
                wait = self.throttle_interval_s - (now - w.last_call_wall) if w.last_call_wall else 0.0
                if wait > 0:
                    return Gate(False, t, f"budget: robot credit low, throttled "
                                          f"(tier throttled, next call in {wait:.0f}s)")
                w.last_call_wall = now
            return Gate(True, t)

    def debit(self, robot_id: str, amount_micro_usd: float, ref: Optional[str] = None,
              source: str = "router", sim_time: Optional[float] = None,
              note: Optional[str] = None) -> dict[str, Any]:
        """Record a spend (call AFTER a billed call succeeded). Balance floors at 0."""
        amt = max(0, int(round(amount_micro_usd)))
        with self._lock:
            w = self._get(robot_id)
            w.balance = max(0, w.balance - amt)
            w.spent += amt
            self._append(w, "spend", amt, ref, None, source, sim_time, note)
            self._note_tier(w)
            return {"balance_micro_usd": w.balance, "tier": self._tier_of(w)}

    def credit(self, robot_id: str, amount_micro_usd: float, kind: str = "earn",
               source: str = "simulated", ref: Optional[str] = None, tx_hash: Optional[str] = None,
               sim_time: Optional[float] = None, note: Optional[str] = None) -> bool:
        """Add credit. Idempotent per (kind, ref): the same tx / hazard / order is credited once.
        Returns True if applied, False if it was a duplicate or non-positive."""
        if kind not in ("earn", "topup"):
            raise ValueError("kind must be 'earn' or 'topup'")
        amt = int(round(amount_micro_usd))
        if amt <= 0:
            return False
        with self._lock:
            w = self._get(robot_id)
            if ref is not None:
                key = f"{kind}:{ref}"
                if key in w.refs:
                    return False
                w.refs.add(key)
            w.balance += amt
            if kind == "earn":
                w.earned += amt
            else:
                w.topped_up += amt
            self._append(w, kind, amt, ref, tx_hash, source, sim_time, note)
            self._note_tier(w)
            return True

    def _append(self, w: _Wallet, kind: str, amt: int, ref: Optional[str], tx_hash: Optional[str],
                source: str, sim_time: Optional[float], note: Optional[str]) -> None:
        e: dict[str, Any] = {"kind": kind, "amount_micro_usd": amt, "source": source,
                             "t_wall": round(time.time(), 3), "balance_after_micro_usd": w.balance}
        if kind == "spend":
            e["request_id"] = ref
        else:
            e["ref"] = ref
            if tx_hash:
                e["tx_hash"] = tx_hash
        if sim_time is not None:
            e["sim_time"] = round(float(sim_time), 2)
        if note:
            e["note"] = note
        w.ledger.append(e)

    @staticmethod
    def new_ref(prefix: str = "manual") -> str:
        return f"{prefix}:{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------ views
    def snapshot(self, robot_id: str, ledger_n: int = 20) -> dict[str, Any]:
        with self._lock:
            w = self._get(robot_id)
            return {"robot_id": w.robot_id, "operator": w.operator, "kind": w.kind,
                    "start_micro_usd": w.start, "balance_micro_usd": w.balance,
                    "tier": self._tier_of(w), "spent": w.spent, "earned": w.earned,
                    "topped_up": w.topped_up, "max_spend_micro_usd": self.max_spend,
                    "ledger": list(w.ledger)[-ledger_n:]}

    def snapshot_all(self, ledger_n: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [self.snapshot(r, ledger_n) for r in sorted(self._w)]

    def wallet_info(self, robot_id: str, cost: int, model: Optional[str], **extra: Any) -> dict[str, Any]:
        with self._lock:
            w = self._get(robot_id)
            return {"balance_micro_usd": w.balance, "cost_micro_usd": int(cost),
                    "tier": self._tier_of(w), "model": model, **extra}
