"""web3.py binding for ComputeCreditVault (testnet-only compute-credit deposits on 0G Galileo).

Robot operators pay testnet 0G into a per-robot compute-credit account with a real tx; the mock
testnet Router stand-in indexes `Deposited` events (`deposits_since`) and credits compute balance.

    from chain.credit_vault import CreditVault
    v = CreditVault()                                   # reads chain/deployments/galileo.json
    tx = v.deposit("R-04", 10**13, private_key)         # 0x tx hash, after the receipt
    v.credit_of("R-04")                                 # cumulative wei deposited
    v.deposits_since(block)                             # decoded Deposited events

RPC: arg, else $ZG_RPC_URL, else the deployment json's "rpc", else https://evmrpc-testnet.0g.ai.
Gas: legacy gasPrice, estimate x1.25 + 10k (same as chain.bindings.transact). RPC reads and the send
are retried across short Galileo blips (~20 s). Never logs keys.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError, TimeExhausted

log = logging.getLogger("chain.credit_vault")

REPO_ROOT = Path(__file__).resolve().parent.parent
CHAIN_DIR = Path(__file__).resolve().parent
VAULT_ABI_PATH = CHAIN_DIR / "abi" / "ComputeCreditVault.json"
DEFAULT_DEPLOYMENT = "chain/deployments/galileo.json"
DEFAULT_RPC = "https://evmrpc-testnet.0g.ai"
CONTRACT_NAME = "ComputeCreditVault"
LOG_CHUNK = 1000
DEPOSITED_SIG = "Deposited(string,bytes32,address,uint256,uint256)"
DEPOSITED_TOPIC = "0x" + Web3.keccak(text=DEPOSITED_SIG).hex().removeprefix("0x")

T = TypeVar("T")


# ------------------------------------------------------------------ helpers
def load_vault_abi(path: str | Path = VAULT_ABI_PATH) -> list:
    return json.loads(Path(path).read_text())


def robot_key(robot_id: str) -> bytes:
    """keccak256(bytes(robotId)) as the contract computes it."""
    return bytes(Web3.keccak(text=robot_id))


def _hex0x(v: Any) -> str:
    if isinstance(v, str):
        return v if v.startswith("0x") else "0x" + v
    return "0x" + bytes(v).hex().removeprefix("0x")


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() or p.exists() else REPO_ROOT / p


def error_selectors(abi: list) -> dict[str, str]:
    out = {}
    for item in abi:
        if item.get("type") == "error":
            sig = f"{item['name']}({','.join(i['type'] for i in item.get('inputs', []))})"
            out["0x" + Web3.keccak(text=sig)[:4].hex().removeprefix("0x")] = item["name"]
    return out


def _decode_revert(exc: Exception, selectors: dict[str, str]) -> str:
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


def decode_deposited(contract, lg) -> Optional[dict]:
    """Decode one raw log into the deposits_since() dict, or None if it is not a Deposited log."""
    topics = lg.get("topics") or []
    if not topics or _hex0x(topics[0]).lower() != DEPOSITED_TOPIC.lower():
        return None
    d = contract.events.Deposited().process_log(lg)
    a = d["args"]
    return {"robot_id": a["robotId"], "from": Web3.to_checksum_address(a["from"]),
            "amount_wei": int(a["amount"]), "total_wei": int(a["total"]),
            "tx_hash": _hex0x(d["transactionHash"]), "log_index": int(d["logIndex"]),
            "block": int(d["blockNumber"])}


def _is_transient(e: Exception) -> bool:
    """Network-ish failures are retried; reverts and other RPC errors are not."""
    if isinstance(e, (ContractLogicError, ContractCustomError)):
        return False
    try:
        import requests
        if isinstance(e, requests.exceptions.RequestException):
            return True
    except ImportError:  # pragma: no cover
        pass
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    return _looks_network(e)


def _looks_network(e: Exception) -> bool:
    m = str(e).lower()
    return any(s in m for s in ("timeout", "timed out", "connection", "502", "503", "504", "429",
                                "too many", "temporarily", "unavailable", "reset", "eof"))


def retry(fn: Callable[[], T], *, what: str, budget_s: float = 45.0, delay_s: float = 2.0) -> T:
    """Call fn, retrying transient RPC errors for up to budget_s (Galileo blips for ~20 s)."""
    deadline = time.monotonic() + budget_s
    while True:
        try:
            return fn()
        except Exception as e:
            if not _is_transient(e) or time.monotonic() + delay_s > deadline:
                raise
            log.warning("%s failed (retrying): %s", what, str(e)[:200])
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.5, 8.0)


# ------------------------------------------------------------------ client
class CreditVault:
    """ComputeCreditVault client. Stateless apart from the web3 connection."""

    def __init__(self, rpc_url: str | None = None, deployment_path: str = DEFAULT_DEPLOYMENT, *,
                 address: str | None = None, rpc_timeout: float = 20.0):
        dep: dict = {}
        dpath = _resolve(deployment_path)
        if dpath.is_file():
            dep = json.loads(dpath.read_text())
        entry = dep.get(CONTRACT_NAME) or {}
        addr = address or entry.get("address")
        if not addr:
            raise RuntimeError(f"no {CONTRACT_NAME} address in {dpath} (run chain/deploy_vault.py)")
        self.rpc_url = rpc_url or os.environ.get("ZG_RPC_URL") or dep.get("rpc") or DEFAULT_RPC
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": rpc_timeout}))
        abi_path = entry.get("abi_path")
        self.abi = load_vault_abi(_resolve(abi_path) if abi_path else VAULT_ABI_PATH)
        self.address: str = Web3.to_checksum_address(addr)
        self.deploy_block: int = int(entry.get("block", 0))
        self.contract = self.w3.eth.contract(address=self.address, abi=self.abi)
        self._errsel = error_selectors(self.abi)
        self._chain_id: Optional[int] = None

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = retry(lambda: self.w3.eth.chain_id, what="chain_id")
        return self._chain_id

    def latest_block(self) -> int:
        return int(retry(lambda: self.w3.eth.block_number, what="block_number"))

    def credit_of(self, robot_id: str) -> int:
        return int(retry(lambda: self.contract.functions.creditOf(robot_id).call(), what="creditOf"))

    def deposit(self, robot_id: str, amount_wei: int, private_key: str, timeout_s: float = 120) -> str:
        """Send deposit(robotId) with value=amount_wei, wait for the receipt, return the 0x tx hash.

        Raises RuntimeError on revert (decoded custom error name when possible) or receipt timeout."""
        if amount_wei <= 0:
            raise RuntimeError("deposit: amount_wei must be > 0")
        n = len(robot_id.encode("utf-8"))
        if not 1 <= n <= 32:
            raise RuntimeError("deposit: robot_id must be 1..32 bytes")
        acct = Account.from_key(private_key)
        fn = self.contract.functions.deposit(robot_id)
        base = {"from": acct.address, "value": int(amount_wei)}
        try:
            est = retry(lambda: fn.estimate_gas(base), what="estimate_gas")
        except (ContractLogicError, ContractCustomError) as e:
            raise RuntimeError(f"deposit would revert: {_decode_revert(e, self._errsel)}") from None
        tx = fn.build_transaction({
            **base,
            "gas": int(est * 1.25) + 10_000,
            "gasPrice": retry(lambda: self.w3.eth.gas_price, what="gas_price"),
            "nonce": retry(lambda: self.w3.eth.get_transaction_count(acct.address, "pending"), what="nonce"),
            "chainId": self.chain_id,
        })
        signed = acct.sign_transaction(tx)
        txh = _hex0x(signed.hash)

        def send():
            try:
                self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception as e:  # a retried send of the same signed tx may already be in the pool
                if "known" in str(e).lower() or "already" in str(e).lower():
                    return
                raise

        retry(send, what="send_raw_transaction")
        deadline = time.monotonic() + timeout_s
        rcpt = None
        while rcpt is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError(f"deposit {txh}: no receipt after {timeout_s:.0f}s")
            try:
                rcpt = self.w3.eth.wait_for_transaction_receipt(txh, timeout=min(left, 30), poll_latency=0.5)
            except TimeExhausted:
                continue
            except Exception as e:  # RPC blip while waiting
                log.warning("receipt wait for %s failed (retrying): %s", txh, str(e)[:200])
                time.sleep(2)
        if rcpt["status"] != 1:
            raise RuntimeError(f"deposit {txh} reverted")
        return txh

    def deposits_since(self, from_block: int, to_block: int | None = None) -> list[dict]:
        """Decoded Deposited events in [from_block, to_block] (default: latest), oldest first.

        Each: {"robot_id", "from", "amount_wei", "total_wei", "tx_hash", "log_index", "block"}.
        get_logs is chunked in ranges of <= 1000 blocks, each chunk retried on RPC blips."""
        to = self.latest_block() if to_block is None else int(to_block)
        start = max(0, int(from_block))
        out: list[dict] = []
        while start <= to:
            end = min(to, start + LOG_CHUNK - 1)
            flt = {"address": self.address, "fromBlock": start, "toBlock": end, "topics": [DEPOSITED_TOPIC]}
            logs = retry(lambda: self.w3.eth.get_logs(flt), what=f"get_logs {start}-{end}")
            for lg in logs:
                ev = decode_deposited(self.contract, lg)
                if ev is not None:
                    out.append(ev)
            start = end + 1
        out.sort(key=lambda e: (e["block"], e["log_index"]))
        return out
