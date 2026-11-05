"""List edges of a SUMO net that lie (all shape points) within the world bbox + margin.

Usage: python traffic/clip_edges.py NET_FILE OUT_TXT [margin_m]
Used by build_net.sh for a second netconvert pass, because --keep-edges.in-geo-boundary
keeps whole edges that merely touch the bbox (they overhang by up to ~350 m).
"""
from __future__ import annotations

import sys

import sumolib

from common.config import settings


def main(net_file: str, out: str, margin: float = 120.0) -> None:
    net = sumolib.net.readNet(net_file, withInternal=False)
    w, s, e, n = settings()["world"]["bbox_wsen"]
    x0, y0 = net.convertLonLat2XY(w, s)
    x1, y1 = net.convertLonLat2XY(e, n)
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    keep = [ed.getID() for ed in net.getEdges()
            if all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in ed.getShape())]
    with open(out, "w") as f:
        f.write("\n".join(keep) + "\n")
    print(f"clip: keep {len(keep)}/{len(net.getEdges())} edges")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 120.0)
