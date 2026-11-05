"""web3.py bindings for HazardRegistry (implements common.interfaces.Chain).

Threading: every operator has its own single-thread executor, so that operator's txs (and nonces)
are strictly serialised; different operators run in parallel. Writes return Futures that ALWAYS
resolve (never raise) to::

    {"tx_hash": str|None, "hazard_id": str|None, "status": "mined"|"failed",
     "gas_used": int|None, "block": int|None, "error": str|None, "resolved": str|None}

Reverts are caught at gas-estimation time (no gas spent) and come back as status="failed" with the
decoded custom error name in "error".

Reads: `poll_events()` fetches new logs since the last seen block (sync, chunked, never raises), or,
after `start_polling()`, just drains what a background thread already fetched. `hazards()` returns the
shared map rebuilt purely from events seen so far (no network).

Conversions:
- robot / edge / order ids on chain = keccak256(utf-8 string)  (`robot_id_bytes`, `edge_id_bytes`)
- hazard type: none=0, accident=1, road_works=2, waterlogging=3
- severity: float in [0,1] is scaled x100; values > 1 are taken as already 0..100. `Hazard.severity`
  comes back as 0..1.
- passable_for: bit0 rover, bit1 drone. Default when the caller passes nothing: ["drone"].
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError

from sim.schemas import ChainTxEvent, Hazard

log = logging.getLogger("chain")

REPO_ROOT = Path(__file__).resolve().parent.parent
CHAIN_DIR = Path(__file__).resolve().parent
ABI_PATH = CHAIN_DIR / "abi" / "HazardRegistry.json"
OPERATORS = ("ZipKart", "MinuteMart", "DashKirana")
DEFAULT_RPC = {"anvil": "http://127.0.0.1:8545", "galileo": "https://evmrpc-testnet.0g.ai"}
RPC_ENV = {"anvil": "ANVIL_RPC_URL", "galileo": "ZG_RPC_URL"}

HAZARD_TYPES = {"none": 0, "accident": 1, "road_works": 2, "waterlogging": 3}
HAZARD_TYPE_NAMES = {v: k for k, v in HAZARD_TYPES.items()}
STATUS_NAMES = {1: "reported", 2: "confirmed", 3: "rejected", 4: "cleared"}
KIND_BITS = {"rover": 1, "drone": 2}
ZERO32 = b"\x00" * 32


# ------------------------------------------------------------------ helpers
def keccak_text(s: str) -> bytes:
    return bytes(Web3.keccak(text=s))


def robot_id_bytes(robot_id: str) -> bytes:
    return keccak_text(robot_id)


def edge_id_bytes(edge_id: str) -> bytes:
    return keccak_text(edge_id)


def order_id_bytes(order_id: str) -> bytes:
    return keccak_text(order_id)


def to_bytes32(v: Optional[str | bytes]) -> bytes:
    """0x-prefixed 32-byte hex (e.g. a 0G Storage root) -> bytes32. None/'' -> zero."""
    if v is None or v == "" or v == b"":
        return ZERO32
    if isinstance(v, (bytes, bytearray)):
        if len(v) != 32:
            raise ValueError("bytes32 value must be 32 bytes")
        return bytes(v)
    h = v[2:] if v.startswith("0x") else v
    if len(h) != 64:
        raise ValueError(f"not a 32-byte hex value: {v!r}")
    return bytes.fromhex(h)


def severity_to_u8(sev: float) -> int:
    s = float(sev)
    s = s * 100.0 if s <= 1.0 else s
    return max(0, min(100, int(round(s))))


def passable_mask(passable_for: Optional[Iterable[str]]) -> int:
    if passable_for is None:
        passable_for = ["drone"]
    m = 0
    for k in passable_for:
        m |= KIND_BITS[k]
    return m


def mask_to_passable(mask: int) -> list[str]:
    return [k for k, bit in KIND_BITS.items() if mask & bit]


def robot_kind_of(robot_id: str) -> str:
    return "drone" if robot_id.upper().startswith("D") else "rover"


def _hex(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return [_hex(x) for x in v]
    return v


def load_abi(path: Path = ABI_PATH) -> list:
    return json.loads(Path(path).read_text())


def load_env(env_file: Optional[str | Path] = None) -> None:
    """Load .env without overriding the real environment. Looks at $HAZARD_ENV_FILE, then repo .env."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    cands = [env_file, os.environ.get("HAZARD_ENV_FILE"), REPO_ROOT / ".env"]
    for c in cands:
        if c and Path(c).is_file():
            load_dotenv(c, override=False)
            return


def operator_key_env(name: str) -> str:
    return f"OPERATOR_{name.upper()}_PRIVATE_KEY"


def _error_selectors(abi: list) -> dict[str, str]:
    out = {}
    for item in abi:
        if item.get("type") == "error":
            sig = f"{item['name']}({','.join(i['type'] for i in item.get('inputs', []))})"
            out["0x" + Web3.keccak(text=sig)[:4].hex().removeprefix("0x")] = item["name"]
    return out


def decode_revert(exc: Exception, selectors: dict[str, str]) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        name = selectors.get(data[:10].lower())
        if name:
            return name
    msg = str(exc)
    for sel, name in selectors.items():
        if sel in msg.lower():
            return name
    return msg[:300]


def transact(w3: Web3, acct, fn=None, *, value: int = 0, data: Optional[dict] = None,
             gas_mult: float = 1.25, timeout: float = 180.0) -> dict:
    """Sign + send one tx (legacy gasPrice; works on anvil and Galileo) and wait for the receipt.

    `fn` is a bound contract function (or a contract constructor). Raises on revert during estimation.
    """
    base = {"from": acct.address, "value": value}
    gas = int(fn.estimate_gas(base) * gas_mult) + 10_000
    tx = fn.build_transaction({
        **base,
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "chainId": w3.eth.chain_id,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=0.5)
    return rcpt


# ------------------------------------------------------------------ main class
class HazardChain:
    """HazardRegistry client. One signer per operator name."""

    def __init__(self, rpc_url: str, address: str, operator_keys: dict[str, str], *,
                 abi: Optional[list] = None, start_block: int | str = 0,
                 on_tx: Optional[Callable[[ChainTxEvent], None]] = None,
                 sim_time_fn: Optional[Callable[[], float]] = None,
                 known_robot_ids: Iterable[str] = (), log_chunk: int = 10_000,
                 max_chunks_per_poll: int = 20, receipt_timeout: float = 180.0,
                 rpc_timeout: float = 20.0):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
        self.abi = abi or load_abi()
        self.address = Web3.to_checksum_address(address)
        self.contract = self.w3.eth.contract(address=self.address, abi=self.abi)
        self._accts = {name: Account.from_key(k) for name, k in operator_keys.items()}
        self._exec = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"chain-{name}")
                      for name in self._accts}
        self.on_tx = on_tx
        self.sim_time_fn = sim_time_fn
        self.receipt_timeout = receipt_timeout
        self._errsel = _error_selectors(self.abi)
        self._chain_id: Optional[int] = None

        # event decoding
        self._event_by_topic = {}
        for item in self.abi:
            if item.get("type") == "event":
                sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
                topic = "0x" + Web3.keccak(text=sig).hex().removeprefix("0x")
                self._event_by_topic[topic] = getattr(self.contract.events, item["name"])()
        self.log_chunk = log_chunk
        self.max_chunks_per_poll = max_chunks_per_poll
        self._start_block = start_block
        self._next_block: Optional[int] = None if start_block == "latest" else int(start_block)

        # map state rebuilt from events
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._hazards: dict[int, Hazard] = {}
        self._op_names: dict[str, str] = {a.address.lower(): n for n, a in self._accts.items()}
        self._robot_names: dict[str, str] = {}
        self._registered: dict[str, set[str]] = {n: set() for n in self._accts}
        for rid in [*known_robot_ids, *(f"R-{i:02d}" for i in range(1, 31)),
                    *(f"D-{i:02d}" for i in range(1, 11))]:
            self._robot_names["0x" + robot_id_bytes(rid).hex()] = rid

        self._bg: Optional[threading.Thread] = None
        self._bg_stop = threading.Event()
        self._bg_q: "queue.Queue[dict]" = queue.Queue()

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_deployment(cls, network: str = "galileo", *, deployment_path: Optional[str | Path] = None,
                        rpc_url: Optional[str] = None, env_file: Optional[str | Path] = None,
                        operators: Iterable[str] = OPERATORS, **kw) -> "HazardChain":
        """Build from chain/deployments/<network>.json + operator keys from env/.env."""
        load_env(env_file)
        dpath = Path(deployment_path or CHAIN_DIR / "deployments" / f"{network}.json")
        dep = json.loads(dpath.read_text())
        rpc = rpc_url or os.environ.get(RPC_ENV.get(network, "ZG_RPC_URL")) or dep.get("rpc") \
            or DEFAULT_RPC.get(network)
        keys = {}
        for name in operators:
            k = os.environ.get(operator_key_env(name), "").strip()
            if k and k != "0x...":
                keys[name] = k
        abi_path = dep.get("abi_path")
        abi = load_abi(REPO_ROOT / abi_path) if abi_path else None
        reg = dep["HazardRegistry"]
        kw.setdefault("start_block", reg.get("block", 0))
        return cls(rpc, reg["address"], keys, abi=abi, **kw)

    def close(self) -> None:
        self.stop_polling()
        for ex in self._exec.values():
            ex.shutdown(wait=True)

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = self.w3.eth.chain_id
        return self._chain_id

    def operator_address(self, operator: str) -> str:
        return self._accts[operator].address

    # ---------------------------------------------------------------- writes
    def register_robots(self, operator: str, robots: Iterable[str | tuple[str, str]]) -> "Future[dict]":
        """robots: robot id strings (kind inferred: 'D-*' = drone) or (id, kind) tuples."""
        items = [(r, robot_kind_of(r)) if isinstance(r, str) else (r[0], r[1]) for r in robots]

        def call():
            return self._send(operator, "registerRobot", self._robots_fn(items))

        return self._submit(operator, call)

    def report_hazard(self, operator: str, robot_id: str, edge_id: str, hazard_type: str,
                      severity: float, evidence_root: Optional[str], zg_request_id: Optional[str], *,
                      passable_for: Optional[Iterable[str]] = None, stake_wei: Optional[int] = None
                      ) -> "Future[dict]":
        def call():
            t = HAZARD_TYPES.get(hazard_type)
            if not t:
                return self._fail(f"cannot report hazard_type={hazard_type!r}")
            self._ensure_robot(operator, robot_id)
            stake = stake_wei if stake_wei is not None else self.contract.functions.minStake().call()
            fn = self.contract.functions.reportHazard(
                robot_id_bytes(robot_id), edge_id_bytes(edge_id), edge_id, t, severity_to_u8(severity),
                passable_mask(passable_for), to_bytes32(evidence_root), zg_request_id or "")
            return self._send(operator, "reportHazard", fn, value=stake)

        return self._submit(operator, call)

    def verify_hazard(self, operator: str, robot_id: str, hazard_id: str, confirm: bool,
                      evidence_root: Optional[str], zg_request_id: Optional[str]) -> "Future[dict]":
        def call():
            self._ensure_robot(operator, robot_id)
            fn = self.contract.functions.verifyHazard(int(hazard_id), robot_id_bytes(robot_id), bool(confirm),
                                                      to_bytes32(evidence_root), zg_request_id or "")
            return self._send(operator, "verifyHazard", fn, hazard_id=str(hazard_id))

        return self._submit(operator, call)

    def clear_hazard(self, operator: str, hazard_id: str, evidence_root: Optional[str] = None,
                     zg_request_id: Optional[str] = None) -> "Future[dict]":
        def call():
            fn = self.contract.functions.clearHazard(int(hazard_id), to_bytes32(evidence_root),
                                                     zg_request_id or "")
            return self._send(operator, "clearHazard", fn, hazard_id=str(hazard_id))

        return self._submit(operator, call)

    def record_delivery(self, operator: str, order_id: str, robot_id: str,
                        episode_root: Optional[str] = None) -> "Future[dict]":
        def call():
            self._ensure_robot(operator, robot_id)
            fn = self.contract.functions.recordDelivery(order_id_bytes(order_id), robot_id_bytes(robot_id),
                                                        to_bytes32(episode_root))
            return self._send(operator, "other", fn)

        return self._submit(operator, call)

    # ---------------------------------------------------------------- reads
    def poll_events(self) -> list[dict[str, Any]]:
        """New decoded events since the last poll. Never raises."""
        if self._bg is not None:
            out = []
            while True:
                try:
                    out.append(self._bg_q.get_nowait())
                except queue.Empty:
                    return out
        return self._fetch_new()

    def hazards(self) -> list[Hazard]:
        with self._lock:
            return [h.model_copy(deep=True) for _, h in sorted(self._hazards.items())]

    def hazard(self, hazard_id: str | int) -> Optional[Hazard]:
        with self._lock:
            h = self._hazards.get(int(hazard_id))
            return h.model_copy(deep=True) if h else None

    def start_polling(self, interval_s: float = 2.0) -> None:
        """Background log poller; poll_events() then only drains its queue (fully non-blocking)."""
        if self._bg is not None:
            return
        self._bg_stop.clear()

        def loop():
            while not self._bg_stop.is_set():
                for ev in self._fetch_new():
                    self._bg_q.put(ev)
                self._bg_stop.wait(interval_s)

        self._bg = threading.Thread(target=loop, name="chain-poller", daemon=True)
        self._bg.start()

    def stop_polling(self) -> None:
        if self._bg is not None:
            self._bg_stop.set()
            self._bg.join(timeout=10)
            self._bg = None

    def balance_wei(self, operator: str) -> int:
        return self.w3.eth.get_balance(self._accts[operator].address)

    # ---------------------------------------------------------------- internals
    def _submit(self, operator: str, call: Callable[[], dict]) -> "Future[dict]":
        if operator not in self._exec:
            f: Future = Future()
            f.set_result(self._fail(f"no signer key for operator {operator!r}"))
            return f

        def safe():
            try:
                return call()
            except Exception as e:  # never raise into the sim
                log.warning("chain call failed for %s: %s", operator, e)
                return self._fail(str(e)[:300])

        return self._exec[operator].submit(safe)

    @staticmethod
    def _fail(err: str, tx_hash: Optional[str] = None, hazard_id: Optional[str] = None) -> dict:
        return {"tx_hash": tx_hash, "hazard_id": hazard_id, "status": "failed", "gas_used": None,
                "block": None, "error": err, "resolved": None}

    def _emit(self, kind: str, tx_hash: str, operator: str, hazard_id: Optional[str], status: str) -> None:
        if not self.on_tx:
            return
        try:
            st = float(self.sim_time_fn()) if self.sim_time_fn else 0.0
            self.on_tx(ChainTxEvent(kind=kind, tx_hash=tx_hash, operator=operator, hazard_id=hazard_id,
                                    status=status, sim_time=st))
        except Exception as e:
            log.warning("on_tx callback error: %s", e)

    def _robots_fn(self, items: list[tuple[str, str]]):
        ids = [robot_id_bytes(r) for r, _ in items]
        kinds = [0 if k == "rover" else 1 for _, k in items]
        for r, _ in items:
            self._robot_names["0x" + robot_id_bytes(r).hex()] = r
        return self.contract.functions.registerRobots(ids, kinds)

    def _ensure_robot(self, operator: str, robot_id: str) -> None:
        """Lazily register the robot for this operator (runs inside the operator's executor)."""
        self._robot_names["0x" + robot_id_bytes(robot_id).hex()] = robot_id
        if robot_id in self._registered[operator]:
            return
        owner, _kind = self.contract.functions.robots(robot_id_bytes(robot_id)).call()
        me = self._accts[operator].address
        if owner.lower() != me.lower():
            r = self._send(operator, "registerRobot", self._robots_fn([(robot_id, robot_kind_of(robot_id))]))
            if r["status"] != "mined":
                raise RuntimeError(f"registerRobots({robot_id}) failed: {r.get('error')}")
        self._registered[operator].add(robot_id)

    def _send(self, operator: str, kind: str, fn, *, value: int = 0, hazard_id: Optional[str] = None) -> dict:
        acct = self._accts[operator]
        base = {"from": acct.address, "value": value}
        try:
            gas = int(fn.estimate_gas(base) * 1.25) + 10_000
        except (ContractLogicError, ContractCustomError) as e:
            return self._fail(decode_revert(e, self._errsel), hazard_id=hazard_id)
        tx = fn.build_transaction({
            **base, "gas": gas, "gasPrice": self.w3.eth.gas_price, "chainId": self.chain_id,
            "nonce": self.w3.eth.get_transaction_count(acct.address, "pending"),
        })
        signed = acct.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        txh = "0x" + bytes(tx_hash).hex().removeprefix("0x")
        self._emit(kind, txh, operator, hazard_id, "sent")
        try:
            rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.receipt_timeout,
                                                            poll_latency=0.5)
        except Exception as e:
            self._emit(kind, txh, operator, hazard_id, "failed")
            return self._fail(f"receipt wait: {e}", tx_hash=txh, hazard_id=hazard_id)

        resolved = None
        for lg in rcpt["logs"]:
            ev = self._decode(lg)
            if ev is None:
                continue
            if ev["event"] == "HazardReported":
                hazard_id = str(ev["args"]["hazardId"])
            elif ev["event"] == "HazardResolved":
                resolved = STATUS_NAMES.get(ev["args"]["status"])
        ok = rcpt["status"] == 1
        self._emit(kind, txh, operator, hazard_id, "mined" if ok else "failed")
        return {"tx_hash": txh, "hazard_id": hazard_id, "status": "mined" if ok else "failed",
                "gas_used": int(rcpt["gasUsed"]), "block": int(rcpt["blockNumber"]),
                "error": None if ok else "reverted", "resolved": resolved}

    def _decode(self, lg) -> Optional[dict]:
        try:
            if Web3.to_checksum_address(lg["address"]) != self.address or not lg["topics"]:
                return None
            t0 = "0x" + bytes(lg["topics"][0]).hex().removeprefix("0x")
            evt = self._event_by_topic.get(t0)
            if evt is None:
                return None
            d = evt.process_log(lg)
            args = {k: _hex(v) for k, v in dict(d["args"]).items()}
            return {"event": d["event"], "block": int(d["blockNumber"]),
                    "tx_hash": "0x" + bytes(d["transactionHash"]).hex().removeprefix("0x"),
                    "log_index": int(d["logIndex"]), "args": args}
        except Exception as e:
            log.warning("could not decode log: %s", e)
            return None

    def _fetch_new(self) -> list[dict[str, Any]]:
        if not self._poll_lock.acquire(blocking=False):
            return []
        try:
            latest = self.w3.eth.block_number
            if self._next_block is None:
                self._next_block = latest + 1
            out: list[dict] = []
            chunks = 0
            while self._next_block <= latest and chunks < self.max_chunks_per_poll:
                to = min(latest, self._next_block + self.log_chunk - 1)
                logs = self.w3.eth.get_logs({"address": self.address, "fromBlock": self._next_block,
                                             "toBlock": to})
                for lg in logs:
                    ev = self._decode(lg)
                    if ev is not None:
                        self._apply(ev)
                        out.append(ev)
                self._next_block = to + 1
                chunks += 1
            return out
        except Exception as e:  # RPC hiccup: keep cursor, retry next poll
            log.warning("poll_events failed (will retry): %s", e)
            return []
        finally:
            self._poll_lock.release()

    def _apply(self, ev: dict) -> None:
        a = ev["args"]
        name = ev["event"]
        with self._lock:
            if name == "OperatorRegistered":
                self._op_names[a["operator"].lower()] = a["name"]
            elif name == "RobotsRegistered":
                pass
            elif name == "HazardReported":
                hid = int(a["hazardId"])
                op = self._op_names.get(a["operator"].lower(), a["operator"])
                root = a["evidenceRoot"]
                self._hazards[hid] = Hazard(
                    id=str(hid), edge_id=a["edgeName"],
                    type=HAZARD_TYPE_NAMES.get(a["hazardType"], "none"),
                    severity=a["severity"] / 100.0, status="reported", reporter_operator=op,
                    reporter_robot=self._robot_names.get(a["robotId"], a["robotId"]),
                    evidence_root=None if root == "0x" + "00" * 32 else root,
                    zg_request_id=a["zgRequestId"] or None, tx_hash=ev["tx_hash"],
                    passable_for=mask_to_passable(a["passableMask"]))
            elif name == "HazardVerified":
                h = self._hazards.get(int(a["hazardId"]))
                if h:
                    h.confirms, h.disputes = int(a["confirms"]), int(a["disputes"])
            elif name == "HazardResolved":
                h = self._hazards.get(int(a["hazardId"]))
                if h:
                    h.status = STATUS_NAMES.get(a["status"], h.status)
            elif name == "HazardCleared":
                h = self._hazards.get(int(a["hazardId"]))
                if h:
                    h.status = "cleared"
