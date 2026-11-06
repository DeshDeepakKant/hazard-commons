#!/usr/bin/env python3
"""Run the live Hazard Commons demo server (FastAPI + WebSocket + Sim Engine).

When DEMO=1 is set, MOCK_BRAIN is strictly refused: decisions must come from 0G.
Streams to the Three.js viewer at http://localhost:5173.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from common.config import settings
from server.app import app
from sim.engine import Engine
from sim.scenario import Scenario

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hazard.demo")


def main() -> int:
    ap = argparse.ArgumentParser(description="Hazard Commons Demo Server")
    ap.add_argument("--port", type=int, default=8765, help="FastAPI port (default 8765)")
    ap.add_argument("--host", default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
    ap.add_argument("--traffic", action="store_true", help="Enable SUMO background traffic")
    ap.add_argument("--rtf", type=float, default=1.0, help="Realtime factor (1.0 = real time)")
    ap.add_argument("--loop", action=argparse.BooleanOptionalAction, default=None,
                    help="Replay the scripted story in cycles (default: settings "
                         "sim.scenario_loop.enabled, off)")
    ap.add_argument("--loop-every", type=float, default=None, metavar="SECONDS",
                    help="Loop period in sim seconds, from each cycle start (default 360)")
    ap.add_argument("--loop-max-cycles", type=int, default=None,
                    help="Stop looping after this many cycles in total (cost guard, default 5)")
    ap.add_argument("--brain-profile", default=None,
                    help="Per-checkpoint 0G model routes from settings brain.profiles "
                         "(e.g. tee-text, outage). Default: every checkpoint on the TEE vision model")
    ap.add_argument("--wallets", action="store_true",
                    help="Give every robot body its own credit wallet (brain.wallets; same as ZG_WALLETS=1)")
    args = ap.parse_args()
    if args.wallets:
        os.environ["ZG_WALLETS"] = "1"
    if args.brain_profile:
        os.environ["ZG_BRAIN_PROFILE"] = args.brain_profile

    loop_cfg = (settings().get("sim", {}) or {}).get("scenario_loop", {}) or {}
    loop_on = bool(loop_cfg.get("enabled", False)) if args.loop is None else args.loop
    loop_every = float(args.loop_every or loop_cfg.get("every_s", 360.0))
    loop_max = int(args.loop_max_cycles or loop_cfg.get("max_cycles", 5))
    if loop_on and (loop_every <= 0 or loop_max < 1):
        ap.error("--loop-every must be > 0 and --loop-max-cycles >= 1")

    print("=" * 68)
    print("  HAZARD COMMONS — LIVE DEMO SERVER")
    print(f"  Server: http://{args.host}:{args.port}")
    print(f"  Viewer: http://localhost:5173")
    print(f"  DEMO mode: {'ON (DEMO=1)' if os.environ.get('DEMO') == '1' else 'OFF'}")
    print(f"  Brain profile: {os.environ.get('ZG_BRAIN_PROFILE') or 'default (all checkpoints: TEE vision model)'}")
    if loop_on:
        print(f"  Scenario loop: every {loop_every:.0f} s sim time, max {loop_max} cycles "
              f"(each cycle: ~3 stakes of 0.0001 0G + gas, ~15-25 0G Router calls)")
    else:
        print("  Scenario loop: OFF (story plays once)")
    print("=" * 68)

    # Initialize Engine (attaches itself to server.app via attach_engine)
    engine = Engine(
        enable_traffic=args.traffic,
        realtime_factor=args.rtf,
        attach_to_server=True,
    )
    # Attach scenario (loop off = the single scripted story, unchanged)
    if loop_on:
        Scenario(engine, loop_every_s=loop_every, loop_max_cycles=loop_max)
    else:
        Scenario(engine)

    # Start engine loop in a background thread
    sim_thread = threading.Thread(target=engine.run, daemon=True, name="engine-sim")
    sim_thread.start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
