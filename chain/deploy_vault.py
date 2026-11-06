"""Deploy ComputeCreditVault to 0G Galileo (chain 16602, TESTNET ONLY).

    .venv/bin/python chain/deploy_vault.py            # from the repo root
    .venv/bin/python chain/deploy_vault.py --rebuild  # force `forge build` first

Uses DEPLOYER_PRIVATE_KEY from the environment / .env. Writes chain/abi/ComputeCreditVault.json and
ADDS a "ComputeCreditVault": {address, tx, abi_path, block} entry to chain/deployments/galileo.json
(other keys untouched). Refuses if chainId != 16602. Never prints keys (addresses only).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # allow `python chain/deploy_vault.py`
    sys.path.insert(0, str(REPO_ROOT))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from chain.credit_vault import (CHAIN_DIR, CONTRACT_NAME, DEFAULT_RPC, VAULT_ABI_PATH,  # noqa: E402
                                retry)

GALILEO_CHAIN_ID = 16602
FOUNDRY_DIR = CHAIN_DIR / "foundry"
ARTIFACT = FOUNDRY_DIR / "out" / "ComputeCreditVault.sol" / "ComputeCreditVault.json"
FORGE = os.environ.get("FORGE", str(Path.home() / ".foundry" / "bin" / "forge"))
DEPLOYMENT = CHAIN_DIR / "deployments" / "galileo.json"


def load_env(env_file: str | None = None) -> None:
    """Load .env without overriding the real environment (same lookup as chain.bindings.load_env)."""
    from dotenv import load_dotenv
    for c in [env_file, os.environ.get("HAZARD_ENV_FILE"), REPO_ROOT / ".env"]:
        if c and Path(c).is_file():
            load_dotenv(c, override=False)
            return


def build(force: bool = False) -> dict:
    if force or not ARTIFACT.exists():
        subprocess.run([FORGE, "build", "--skip", "test"], cwd=FOUNDRY_DIR, check=True, capture_output=True)
    art = json.loads(ARTIFACT.read_text())
    VAULT_ABI_PATH.parent.mkdir(parents=True, exist_ok=True)
    VAULT_ABI_PATH.write_text(json.dumps(art["abi"], indent=1) + "\n")
    return art


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--force", action="store_true", help="redeploy even if an entry already exists")
    args = ap.parse_args(argv)

    load_env(args.env_file)
    dep = json.loads(DEPLOYMENT.read_text()) if DEPLOYMENT.is_file() else {}
    if dep.get(CONTRACT_NAME) and not args.force:
        print(f"{CONTRACT_NAME} already deployed at {dep[CONTRACT_NAME]['address']} (use --force to redeploy)")
        return 0
    rpc = args.rpc or os.environ.get("ZG_RPC_URL") or dep.get("rpc") or DEFAULT_RPC
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    chain_id = retry(lambda: w3.eth.chain_id, what="chain_id")
    if chain_id != GALILEO_CHAIN_ID:
        print(f"refusing: chainId={chain_id}, expected 0G Galileo {GALILEO_CHAIN_ID}", file=sys.stderr)
        return 4
    dkey = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
    if not dkey or dkey == "0x...":
        print("DEPLOYER_PRIVATE_KEY missing", file=sys.stderr)
        return 2
    acct = Account.from_key(dkey)

    art = build(force=args.rebuild)
    C = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    ctor = C.constructor()
    base = {"from": acct.address, "value": 0}
    est = retry(lambda: ctor.estimate_gas(base), what="estimate_gas")
    gp = retry(lambda: w3.eth.gas_price, what="gas_price")
    bal = retry(lambda: w3.eth.get_balance(acct.address), what="balance")
    gas = int(est * 1.25) + 10_000
    print(f"network=galileo chainId={chain_id} deployer={acct.address} balance={Web3.from_wei(bal, 'ether')} "
          f"gasPrice={gp} est_gas={est} max_cost={Web3.from_wei(gas * gp, 'ether')}")
    if bal < gas * gp:
        print("deployer balance too low; fund it from https://faucet.0g.ai", file=sys.stderr)
        return 3

    tx = ctor.build_transaction({
        **base, "gas": gas, "gasPrice": gp, "chainId": chain_id,
        "nonce": retry(lambda: w3.eth.get_transaction_count(acct.address, "pending"), what="nonce"),
    })
    signed = acct.sign_transaction(tx)
    txh = "0x" + bytes(signed.hash).hex().removeprefix("0x")

    def send():
        try:
            w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:  # retried send of the same signed tx
            if "known" not in str(e).lower() and "already" not in str(e).lower():
                raise

    retry(send, what="send_raw_transaction")
    print(f"sent deploy tx {txh}; waiting for receipt")
    rcpt = retry(lambda: w3.eth.wait_for_transaction_receipt(txh, timeout=180, poll_latency=0.5),
                 what="receipt", budget_s=300)
    if rcpt["status"] != 1:
        print(f"deploy reverted (tx {txh})", file=sys.stderr)
        return 5
    addr = rcpt["contractAddress"]
    print(f"{CONTRACT_NAME} deployed at {addr} (tx {txh}, block {rcpt['blockNumber']}, gas {rcpt['gasUsed']})")

    # re-read just before writing so concurrent edits to other keys are not lost
    dep = json.loads(DEPLOYMENT.read_text()) if DEPLOYMENT.is_file() else {}
    dep[CONTRACT_NAME] = {"address": addr, "tx": txh,
                          "abi_path": str(VAULT_ABI_PATH.relative_to(REPO_ROOT)),
                          "block": int(rcpt["blockNumber"])}
    DEPLOYMENT.parent.mkdir(parents=True, exist_ok=True)
    DEPLOYMENT.write_text(json.dumps(dep, indent=2) + "\n")
    print(f"wrote {DEPLOYMENT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
