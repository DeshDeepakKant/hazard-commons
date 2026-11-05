"""Extract <location netOffset=...> from a SUMO net and write traffic/out/net_meta.json.

Usage: python traffic/net_meta.py NET_FILE OUT_JSON
"""
from __future__ import annotations

import gzip
import json
import re
import sys


def read_location(net_file: str) -> dict:
    opener = gzip.open if net_file.endswith(".gz") else open
    with opener(net_file, "rt") as f:
        head = f.read(50000)
    m = re.search(r"<location\s[^>]*>", head)
    if not m:
        raise SystemExit(f"no <location> in {net_file}")
    tag = m.group(0)
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
    ox, oy = (float(v) for v in attrs["netOffset"].split(","))
    return {"sumo_net_offset": [ox, oy], "proj": attrs.get("projParameter"),
            "conv_boundary": [float(v) for v in attrs["convBoundary"].split(",")]}


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    loc = read_location(src)
    with open(dst, "w") as f:
        json.dump(loc, f, indent=1)
    print("net_meta:", loc)
