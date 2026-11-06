#!/usr/bin/env python3
"""Agents that pay for themselves: one robot body spends its own credit on REAL 0G Router calls and
degrades normal -> economy -> throttled -> paused, then earns and recovers. No sim, no mock.

Uses the production code path (brain.client.ZGBrain + brain.wallets.RobotWalletRegistry, economy
routes from settings brain.profiles.economy) and a real camera frame from a recorded episode.
Each call costs a few hundred micro-USD at most (~$0.001 for the whole script).

usage: .venv/bin/python scripts/wallet_demo.py [--start 300] [--out docs/wallet_demo.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.client import ZGBrain                        # noqa: E402
from sim.schemas import CheckpointRequest               # noqa: E402
from brain.wallets import RobotWalletRegistry, wallet_settings  # noqa: E402

EPISODE = ROOT / "logs/runs/demo-galileo-v3/episodes/R-06/batch-0.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=300, help="starting credit, micro-USD")
    ap.add_argument("--robot", default="R-04")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    batch = json.loads(EPISODE.read_text())
    rec = batch["records"][0]
    frame = batch["frames"][rec["inputs"]["frame_sha256"]]
    ctx = rec["inputs"]["context"]

    cfg = {**wallet_settings(), "start_micro_usd": a.start, "throttle_min_interval_s": 3}
    wallets = RobotWalletRegistry(cfg)
    wallets.register(a.robot, "MinuteMart", "rover")
    brain = ZGBrain(wallets="auto", max_workers=1)      # builds economy routes from settings
    brain.wallets = wallets                             # ...but with this demo's own registry
    rows = []
    for seq in range(1, 9):
        before = wallets.snapshot(a.robot)
        r = CheckpointRequest(robot_id=a.robot, operator="MinuteMart", robot_kind="rover",
                              checkpoint="CP-H1", sim_time=float(seq), episode_id=f"wallet-demo-{a.robot}",
                              seq=seq, frame_png_b64=frame, context=dict(ctx))
        res = brain.submit(r).result(timeout=120)
        w = (res.zg_trace or {}).get("_hc", {}).get("wallet", {}) if res.zg_trace else {}
        row = {"seq": seq, "tier_before": before["tier"], "balance_before": before["balance_micro_usd"],
               "ok": res.ok, "model": w.get("model") or res.model, "route": w.get("route"),
               "cost_micro_usd": w.get("cost_micro_usd"), "balance_after": wallets.balance(a.robot),
               "tier_after": wallets.tier(a.robot), "zg_request_id": res.zg_request_id,
               "result": (res.output or {}).get("type") if res.ok else f"SAFE_STOP: {res.error}"}
        rows.append(row)
        print(json.dumps(row), flush=True)
        if not res.ok:
            break
        time.sleep(3.5)                                  # respect the throttled tier's spacing
    # income with no human: the same credit path the engine uses when a hazard is confirmed on chain
    wallets.credit(a.robot, cfg["income"]["hazard_reward_micro_usd"], kind="earn", source="chain",
                   ref="wallet-demo:hazard-confirmed", note="hazard confirmed on chain (demo credit)")
    after = wallets.snapshot(a.robot)
    print(json.dumps({"after_income": {"balance": after["balance_micro_usd"], "tier": after["tier"]}}))
    if a.out:
        Path(a.out).write_text(json.dumps({"start": a.start, "calls": rows,
                                           "after_income": {"balance": after["balance_micro_usd"],
                                                            "tier": after["tier"]}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
