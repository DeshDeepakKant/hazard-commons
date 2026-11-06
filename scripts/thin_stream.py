#!/usr/bin/env python3
"""Shrink a recorded stream.jsonl for the hosted replay (keeps every event, thins state frames).

    .venv/bin/python scripts/thin_stream.py logs/runs/<id>/stream.jsonl viewer/public/runs/<id>.jsonl [--every 2]
Non-state envelopes (decision, hazard, order, chain_tx, storage, log) are copied unchanged.
Floats in state frames are rounded to 2 decimals.
"""
from __future__ import annotations

import argparse
import json


def _round(o):
    if isinstance(o, float):
        return round(o, 2)
    if isinstance(o, list):
        return [_round(x) for x in o]
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    return o


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--every", type=int, default=2, help="keep every Nth state frame")
    a = ap.parse_args()
    n_state = kept = total = 0
    with open(a.src) as f, open(a.dst, "w") as out:
        for line in f:
            total += 1
            env = json.loads(line)
            if env["topic"] == "state":
                n_state += 1
                if n_state % a.every:
                    continue
                env = _round(env)
            out.write(json.dumps(env, separators=(",", ":")) + "\n")
            kept += 1
    print(f"{total} -> {kept} envelopes")


if __name__ == "__main__":
    main()
