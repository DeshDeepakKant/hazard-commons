"""Deploy HazardRegistry + register the 3 operators.

    PYTHONPATH=. .venv/bin/python -m chain.deploy --network anvil      # local proof (funds keys via anvil_setBalance)
    PYTHONPATH=. .venv/bin/python -m chain.deploy --network galileo    # real testnet (deployer pays gas)

Keys come from the environment / .env (DEPLOYER_PRIVATE_KEY, OPERATOR_<NAME>_PRIVATE_KEY). Writes
chain/deployments/<network>.json and chain/abi/HazardRegistry.json. Never prints keys.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from eth_account import Account
from web3 import Web3

from chain.bindings import (ABI_PATH, CHAIN_DIR, DEFAULT_RPC, OPERATORS, REPO_ROOT, RPC_ENV, load_env,
                            operator_key_env, transact)

FOUNDRY_DIR = CHAIN_DIR / "foundry"
ARTIFACT = FOUNDRY_DIR / "out" / "HazardRegistry.sol" / "HazardRegistry.json"
FORGE = os.environ.get("FORGE", str(Path.home() / ".foundry" / "bin" / "forge"))


def build(force: bool = False) -> dict:
    if force or not ARTIFACT.exists():
        subprocess.run([FORGE, "build", "--skip", "test"], cwd=FOUNDRY_DIR, check=True, capture_output=True)
    art = json.loads(ARTIFACT.read_text())
    ABI_PATH.parent.mkdir(parents=True, exist_ok=True)
    ABI_PATH.write_text(json.dumps(art["abi"], indent=1) + "\n")
    return art


def chain_settings() -> dict:
    import yaml
    s = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text())["chain"]
    stake = int(s["report_stake_wei"])
    return {"min_stake": stake, "confirms": int(s["confirmations_needed"]),
            "disputes": int(s["disputes_needed"]), "reward": stake // 10}


def deploy_registry(w3: Web3, deployer_key: str, operators: dict[str, str], *, min_stake: int,
                    confirms: int, disputes: int, reward: int, art: Optional[dict] = None,
                    log=print) -> dict:
    """Deploy + registerOperator for each {name: address}. Returns the deployment dict (+ gas)."""
    art = art or build()
    acct = Account.from_key(deployer_key)
    C = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    rcpt = transact(w3, acct, C.constructor(min_stake, confirms, disputes, reward))
    if rcpt["status"] != 1:
        raise RuntimeError("deploy reverted")
    addr = rcpt["contractAddress"]
    gas = {"deploy": int(rcpt["gasUsed"])}
    txh = "0x" + bytes(rcpt["transactionHash"]).hex().removeprefix("0x")
    log(f"HazardRegistry deployed at {addr} (tx {txh}, gas {rcpt['gasUsed']})")
    reg = w3.eth.contract(address=addr, abi=art["abi"])
    op_txs = {}
    for name, a in operators.items():
        if reg.functions.isOperator(a).call():
            log(f"  {name} already registered")
            continue
        r = transact(w3, acct, reg.functions.registerOperator(a, name))
        gas[f"registerOperator:{name}"] = int(r["gasUsed"])
        op_txs[name] = "0x" + bytes(r["transactionHash"]).hex().removeprefix("0x")
        log(f"  registered {name} = {a} (gas {r['gasUsed']})")
    return {
        "chainId": w3.eth.chain_id,
        "HazardRegistry": {"address": addr,
                           "tx": txh,
                           "block": int(rcpt["blockNumber"])},
        "abi_path": str(ABI_PATH.relative_to(REPO_ROOT)),
        "deployer": acct.address,
        "operators": operators,
        "operator_txs": op_txs,
        "config": {"minStake": min_stake, "confirmsNeeded": confirms, "disputesNeeded": disputes,
                   "rewardPerConfirm": reward},
        "gas_used": gas,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", choices=["anvil", "galileo"], required=True)
    ap.add_argument("--rpc", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--fund-rewards-wei", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--out", default=None, help="deployment json path (default chain/deployments/<net>.json)")
    args = ap.parse_args(argv)

    load_env(args.env_file)
    rpc = args.rpc or os.environ.get(RPC_ENV[args.network]) or DEFAULT_RPC[args.network]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    dkey = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
    if not dkey or dkey == "0x...":
        print("DEPLOYER_PRIVATE_KEY missing", file=sys.stderr)
        return 2
    ops = {}
    for n in OPERATORS:
        k = os.environ.get(operator_key_env(n), "").strip()
        if not k or k == "0x...":
            print(f"{operator_key_env(n)} missing", file=sys.stderr)
            return 2
        ops[n] = Account.from_key(k).address
    deployer = Account.from_key(dkey).address

    if args.network == "anvil":
        for a in [deployer, *ops.values()]:
            w3.provider.make_request("anvil_setBalance", [a, hex(10**20)])

    art = build(force=args.rebuild)
    cfg = chain_settings()
    bal = w3.eth.get_balance(deployer)
    gp = w3.eth.gas_price
    # balance must cover gasLimit*gasPrice (limit = 1.25x estimate): deploy ~2.07M gas, registerOperator ~0.1M
    est = int(2_700_000 + 130_000 * len(ops)) * gp
    print(f"network={args.network} chainId={w3.eth.chain_id} deployer={deployer} "
          f"balance={Web3.from_wei(bal, 'ether')} gasPrice={gp} est_cost={Web3.from_wei(est, 'ether')}")
    if bal < est:
        print("deployer balance too low for deploy + 3 registerOperator; fund it from https://faucet.0g.ai",
              file=sys.stderr)
        return 3

    dep = deploy_registry(w3, dkey, ops, min_stake=cfg["min_stake"], confirms=cfg["confirms"],
                          disputes=cfg["disputes"], reward=cfg["reward"], art=art)
    dep["network"] = args.network
    dep["rpc"] = rpc
    if args.fund_rewards_wei:
        reg = w3.eth.contract(address=dep["HazardRegistry"]["address"], abi=art["abi"])
        r = transact(w3, Account.from_key(dkey), reg.functions.fundRewards(), value=args.fund_rewards_wei)
        dep["gas_used"]["fundRewards"] = int(r["gasUsed"])
        print(f"funded reward pool with {args.fund_rewards_wei} wei")
    out = Path(args.out) if args.out else CHAIN_DIR / "deployments" / f"{args.network}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dep, indent=2) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
