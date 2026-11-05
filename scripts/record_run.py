#!/usr/bin/env python3
"""Record a simulation run to a stream.jsonl file for replay and viewer hosting (SPEC §11 & §12).

Writes:
  * logs/runs/<run_id>/stream.jsonl (the full stream of bus envelopes: state, decision, hazard, order, ...)
  * logs/index.json (mirrors run metadata and episode batches)

Usage:
    # Record a full 240s run (as fast as CPU allows):
    MOCK_BRAIN=1 .venv/bin/python scripts/record_run.py --duration 240

    # Real demo recording with 0G router:
    .venv/bin/python scripts/record_run.py --duration 240 --run-id demo-galileo-1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.bus import BUS, Recorder
from sim.engine import Engine
from sim.scenario import Scenario

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hazard.record")


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a Hazard Commons run for replay")
    ap.add_argument("--duration", type=float, default=240.0, help="Sim duration in seconds (default 240)")
    ap.add_argument("--run-id", default=None, help="Run ID (default run-<timestamp>)")
    ap.add_argument("--seed", type=int, default=7, help="Simulation random seed")
    ap.add_argument("--rtf", type=float, default=0.0, help="Realtime factor (0 = as fast as possible)")
    ap.add_argument("--out", default=None, help="Custom output path for stream.jsonl")
    ap.add_argument("--traffic", action="store_true", help="Enable SUMO background traffic")
    args = ap.parse_args()

    run_id = args.run_id or f"run-{int(time.time())}"
    out_path = Path(args.out) if args.out else ROOT / "logs" / "runs" / run_id / "stream.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print(f"  RECORD RUN: {run_id}")
    print(f"  Duration: {args.duration}s  |  Seed: {args.seed}  |  RTF: {args.rtf}")
    print(f"  Output: {out_path}")
    print("=" * 68)

    # 1. Attach recorder to bus (records state every 2 ticks = 10 Hz)
    recorder = Recorder(BUS, out_path, state_every=1)

    # 2. Track envelopes by topic for summary statistics
    stats: dict[str, int] = {}

    def count_env(env):
        stats[env.topic] = stats.get(env.topic, 0) + 1

    BUS.subscribe_sync(count_env)

    # 3. Create engine and scenario
    engine = Engine(
        run_id=run_id,
        enable_traffic=args.traffic,
        seed=args.seed,
        realtime_factor=args.rtf,
        attach_to_server=False,
    )
    scenario = Scenario(engine, seed=args.seed)

    t0_wall = time.perf_counter()
    try:
        log.info("Starting simulation...")
        engine.run(duration_s=args.duration)
    finally:
        recorder.close()
        engine.close()

    wall_duration = time.perf_counter() - t0_wall
    sim_duration = engine.sim_time
    rtf_actual = sim_duration / max(wall_duration, 1e-9)

    # 4. Update logs/index.json
    index_file = ROOT / "logs" / "index.json"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_data = {"version": 1, "runs": []}
    if index_file.exists():
        try:
            index_data = json.loads(index_file.read_text())
            if not isinstance(index_data, dict):
                index_data = {"version": 1, "runs": []}
        except Exception:
            index_data = {"version": 1, "runs": []}

    runs = index_data.setdefault("runs", [])
    runs.append({
        "run_id": run_id,
        "stream_path": str(out_path.relative_to(ROOT)),
        "sim_duration_s": round(sim_duration, 2),
        "wall_duration_s": round(wall_duration, 2),
        "rtf": round(rtf_actual, 2),
        "recorded_at": time.time(),
        "stats": stats,
    })
    index_file.write_text(json.dumps(index_data, indent=2))

    size_mb = out_path.stat().st_size / (1024 * 1024) if out_path.exists() else 0.0

    print("\n" + "=" * 68)
    print("  RECORDING COMPLETE")
    print(f"  Sim time: {sim_duration:.1f}s in {wall_duration:.1f}s wall clock ({rtf_actual:.2f}x RTF)")
    print(f"  Stream file: {out_path} ({size_mb:.2f} MB)")
    print(f"  Total envelopes: {sum(stats.values())}")
    for topic, count in sorted(stats.items()):
        print(f"    - {topic:12s}: {count}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
