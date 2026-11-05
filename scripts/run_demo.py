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
    args = ap.parse_args()

    print("=" * 68)
    print("  HAZARD COMMONS — LIVE DEMO SERVER")
    print(f"  Server: http://{args.host}:{args.port}")
    print(f"  Viewer: http://localhost:5173")
    print(f"  DEMO mode: {'ON (DEMO=1)' if os.environ.get('DEMO') == '1' else 'OFF'}")
    print("=" * 68)

    # Initialize Engine (attaches itself to server.app via attach_engine)
    engine = Engine(
        enable_traffic=args.traffic,
        realtime_factor=args.rtf,
        attach_to_server=True,
    )
    # Attach scenario
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
