"""Per-robot credit ledger + deposit indexer for the LOCAL testnet-Router billing stand-in.

This is NOT 0G. It mimics only the *billing* side of the 0G testnet Router (Payment Layer deposit
-> credited Router balance -> debit per inference). Inference itself is proxied to the real 0G
Router by `testnet_router_mock.app`; nothing here produces or touches a model answer.

Conversion: `credited_micro_usd = amount_wei * micro_usd_per_0g // 10**18`. The default rate
(50_000_000 µUSD per 0G, i.e. 1 testnet 0G = $50, so 0.00001 0G = 500 µUSD) is a DECLARED DEMO
CONVERSION chosen so tiny testnet deposits fund a handful of calls. Testnet 0G has no price.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("testnet_router_mock")

DEFAULT_MICRO_USD_PER_0G = 50_000_000
WEI_PER_0G = 10**18
MAX_DEPOSITS_SHOWN = 20


class DepositSource(Protocol):
    """Interface of chain.credit_vault.CreditVault (the real on-chain source)."""

    def latest_block(self) -> int: ...
    def deposits_since(self, from_block: int, to_block: Optional[int] = None) -> list[dict]: ...


def normalize_account(robot_id: Any) -> str:
    """Map a vault robot_id (str, bytes, or bytes32 hex of an ASCII id) to an account key."""
    if isinstance(robot_id, (bytes, bytearray)):
        return bytes(robot_id).rstrip(b"\x00").decode("utf-8", "replace").strip()
    s = str(robot_id).strip()
    if s.startswith("0x") and len(s) == 66:
        try:
            raw = bytes.fromhex(s[2:]).rstrip(b"\x00")
            txt = raw.decode("ascii")
            if txt and txt.isprintable():
                return txt.strip()
        except ValueError:
            pass
    return s


class Ledger:
    """Thread-safe in-memory balances with optional JSON persistence."""

    def __init__(self, start_micro_usd: int = 0, micro_usd_per_0g: int = DEFAULT_MICRO_USD_PER_0G,
                 state_file: Optional[str] = None):
        self.start_micro_usd = int(start_micro_usd)
        self.micro_usd_per_0g = int(micro_usd_per_0g)
        self.state_file = state_file
        self._lock = threading.RLock()
        self._accounts: dict[str, dict[str, Any]] = {}
        self._seen: set[tuple[str, int]] = set()
        self.cursor: Optional[int] = None
        if state_file and os.path.exists(state_file):
            self._load()

    # ---- accounts
    def _acct(self, account: str) -> dict[str, Any]:
        a = self._accounts.get(account)
        if a is None:
            a = {"balance_micro_usd": self.start_micro_usd, "deposited_wei": 0,
                 "spent_micro_usd": 0, "deposits": []}
            self._accounts[account] = a
        return a

    def balance(self, account: str) -> int:
        with self._lock:
            return int(self._acct(account)["balance_micro_usd"])

    def snapshot(self, account: str) -> dict[str, Any]:
        with self._lock:
            a = self._acct(account)
            return {
                "account": account,
                "total_balance_micro_usd": int(a["balance_micro_usd"]),
                "total_deposited_wei": str(a["deposited_wei"]),
                "total_spent_micro_usd": int(a["spent_micro_usd"]),
                "currency": "usd",
                "deposits": [dict(d) for d in a["deposits"][-MAX_DEPOSITS_SHOWN:]],
            }

    def debit(self, account: str, micro_usd: int) -> int:
        micro_usd = max(0, int(micro_usd))
        with self._lock:
            a = self._acct(account)
            a["balance_micro_usd"] -= micro_usd
            a["spent_micro_usd"] += micro_usd
            self._save()
            return int(a["balance_micro_usd"])

    def credit_deposit(self, dep: dict[str, Any]) -> bool:
        """Credit one vault deposit. Idempotent per (tx_hash, log_index). True if newly credited."""
        tx = str(dep.get("tx_hash", "")).lower()
        li = int(dep.get("log_index", 0) or 0)
        key = (tx, li)
        account = normalize_account(dep.get("robot_id", ""))
        amount_wei = int(dep.get("amount_wei", 0) or 0)
        if not tx or not account or amount_wei <= 0:
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            credited = amount_wei * self.micro_usd_per_0g // WEI_PER_0G
            a = self._acct(account)
            a["balance_micro_usd"] += credited
            a["deposited_wei"] += amount_wei
            a["deposits"].append({"tx_hash": tx, "log_index": li, "from": dep.get("from"),
                                  "amount_wei": str(amount_wei), "credited_micro_usd": credited,
                                  "block": int(dep.get("block", 0) or 0)})
            a["deposits"] = a["deposits"][-200:]
            self._save()
        log.info("credited %s µUSD to %s (deposit %s wei, tx %s#%d)", credited, account,
                 amount_wei, tx, li)
        return True

    def set_cursor(self, block: int) -> None:
        with self._lock:
            self.cursor = int(block)
            self._save()

    # ---- persistence
    def _load(self) -> None:
        with open(self.state_file) as f:
            st = json.load(f)
        self.cursor = st.get("cursor")
        self._seen = {(str(t), int(i)) for t, i in st.get("seen", [])}
        for name, a in st.get("accounts", {}).items():
            self._accounts[name] = {"balance_micro_usd": int(a["balance_micro_usd"]),
                                    "deposited_wei": int(a["deposited_wei"]),
                                    "spent_micro_usd": int(a["spent_micro_usd"]),
                                    "deposits": list(a.get("deposits", []))}

    def _save(self) -> None:
        if not self.state_file:
            return
        st = {"version": 1, "mock": True, "cursor": self.cursor,
              "seen": sorted([t, i] for t, i in self._seen),
              "accounts": {n: {**a, "deposited_wei": str(a["deposited_wei"])}
                           for n, a in self._accounts.items()}}
        d = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, self.state_file)


def default_source_factory() -> DepositSource:
    # Lazy import: chain/credit_vault.py may not exist yet (built in parallel).
    from chain.credit_vault import CreditVault
    return CreditVault()


class DepositIndexer:
    """Polls a deposit source and credits the ledger. Never raises out of poll_once()."""

    def __init__(self, ledger: Ledger, source: Optional[DepositSource] = None,
                 source_factory: Callable[[], DepositSource] = default_source_factory,
                 poll_s: float = 4.0, from_block: Optional[int] = None, lookback: int = 2000):
        self.ledger = ledger
        self._source = source
        self._factory = source_factory
        self.poll_s = poll_s
        self._from_block = from_block
        self.lookback = lookback
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self.last_latest_block: Optional[int] = None

    def _get_source(self) -> DepositSource:
        if self._source is None:
            self._source = self._factory()
        return self._source

    def poll_once(self) -> int:
        """One indexing pass. Returns number of newly credited deposits (0 on error)."""
        with self._poll_lock:
            try:
                src = self._get_source()
                latest = int(src.latest_block())
                self.last_latest_block = latest
                if self.ledger.cursor is None:
                    start = self._from_block if self._from_block is not None else max(0, latest - self.lookback)
                    self.ledger.set_cursor(start)
                start = int(self.ledger.cursor)
                if latest < start:
                    self.last_error = None
                    return 0
                n = 0
                for dep in src.deposits_since(start, latest):
                    if self.ledger.credit_deposit(dep):
                        n += 1
                self.ledger.set_cursor(latest + 1)
                self.last_error = None
                return n
            except Exception as e:  # vault not deployed / RPC down / module missing
                msg = f"{type(e).__name__}: {e}"
                if msg != self.last_error:
                    log.warning("deposit indexer: %s (will keep retrying)", msg)
                self.last_error = msg
                if isinstance(e, ImportError):
                    self._source = None
                return 0

    def status(self) -> dict[str, Any]:
        return {"cursor": self.ledger.cursor, "latest_block": self.last_latest_block,
                "last_error": self.last_error, "poll_s": self.poll_s}

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="deposit-indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
