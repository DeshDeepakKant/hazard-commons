#!/usr/bin/env python3
"""Upload every episode batch of a recorded run to 0G Storage (sequentially, one signer).

    .venv/bin/python scripts/upload_run_episodes.py logs/runs/<run_id>
Writes <run_dir>/storage_roots.json: [{robot_id, file, root_hash, tx_hash, record_count, zg_request_ids}]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.episode_log import verify_chain
from memory.zg_storage import ZGStorage


def main() -> int:
    run = Path(sys.argv[1]).resolve()
    store = ZGStorage()
    out_path = run / "storage_roots.json"
    out = json.loads(out_path.read_text()) if out_path.exists() else []
    done = {o["file"] for o in out}
    for f in sorted((run / "episodes").glob("*/batch-*.json")):
        rel = str(f.relative_to(run))
        if rel in done:
            continue
        data = json.loads(f.read_text())
        recs = data["records"] if isinstance(data, dict) and "records" in data else data
        rids = [r.get("zg_request_id") for r in recs if r.get("zg_request_id")]
        try:
            res = store.upload_file_sync(str(f))
            entry = {"robot_id": f.parent.name, "file": rel, "root_hash": res["root_hash"],
                     "tx_hash": res.get("tx_hash"), "record_count": len(recs), "zg_request_ids": rids}
            print("OK ", rel, res["root_hash"])
        except Exception as e:  # report honestly, keep going
            entry = {"robot_id": f.parent.name, "file": rel, "error": str(e)[:300],
                     "record_count": len(recs), "zg_request_ids": rids}
            print("ERR", rel, str(e)[:120])
        out.append(entry)
        out_path.write_text(json.dumps(out, indent=1))
    ok = [o for o in out if "root_hash" in o]
    print(f"uploaded {len(ok)}/{len(out)}; chain verify: {verify_chain(str(run / 'episodes'))}")
    return 0 if len(ok) == len(out) else 1


if __name__ == "__main__":
    sys.exit(main())
