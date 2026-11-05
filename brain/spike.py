"""Manual 0G Router spike: `ENV_FILE=/path/.env python -m brain.spike [--call]`.

1. GET {base}/models and print the entry for the configured model (modalities / capabilities).
2. With --call and a real ZG_ROUTER_API_KEY: ONE real CP-H1 call with a composited hazard photo;
   prints latency, request id, tee_verified, provider, output. Never prints the key.
"""
from __future__ import annotations

import json
import sys

import httpx
import numpy as np

from brain.zg_router import (api_key_looks_real, configured_api_key, configured_base_url,
                             configured_model)


def models() -> None:
    base, model = configured_base_url(), configured_model()
    key = configured_api_key()
    hdrs = {"Authorization": f"Bearer {key}"} if api_key_looks_real(key) else {}
    r = httpx.get(base.rstrip("/") + "/models", headers=hdrs, timeout=20)
    print("GET /models ->", r.status_code)
    try:
        data = r.json().get("data", [])
    except Exception:
        print(r.text[:400])
        return
    print("models:", [m.get("id") for m in data])
    for m in data:
        if m.get("id") == model:
            print("configured model entry:", json.dumps(m, indent=1)[:2500])


def call() -> None:
    from brain.client import get_brain
    from brain.perception import build_observation
    from sim.schemas import CheckpointRequest

    a = np.zeros((240, 320, 3), np.uint8)
    a[120:] = 90
    a[:120, :, 2] = 200
    obs = build_observation(a, hazard_type="accident", key="spike")
    brain = get_brain(max_calls=2)
    req = CheckpointRequest(robot_id="R-SPIKE", operator="MinuteMart", robot_kind="rover",
                            checkpoint="CP-H1", sim_time=0.0, episode_id="spike", seq=0,
                            frame_png_b64=obs.png_b64,
                            context={"task": "delivering order O-1", "location": {"edge_id": "e1"}})
    res = brain.submit(req).result(timeout=120)
    print(json.dumps({"ok": res.ok, "error": res.error, "latency_ms": res.latency_ms,
                      "zg_request_id": res.zg_request_id, "tee_verified": res.tee_verified,
                      "model": res.model, "output": res.output, "photo": obs.photo,
                      "frame_sha256": obs.sha256,
                      "provider": (res.zg_trace or {}).get("_hc", {}).get("provider")}, indent=1))
    if res.zg_trace:
        print("trace:", json.dumps(res.zg_trace)[:2000])


if __name__ == "__main__":
    models()
    if "--call" in sys.argv:
        if not api_key_looks_real(configured_api_key()):
            sys.exit("ZG_ROUTER_API_KEY is a placeholder; skipping the real call.")
        call()
