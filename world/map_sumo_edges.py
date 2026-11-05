"""Fill road_graph.json `sumo_edge_ids` (list) + `sumo_edge_id` (first) by matching OSM way ids,
falling back to nearest SUMO edge to the graph edge midpoint.

Run after traffic/build_net.sh:  PYTHONPATH=. .venv/bin/python -m world.map_sumo_edges
"""
from __future__ import annotations

import gzip
import json
import math
import re
import shutil
import tempfile

import sumolib

from common.config import path
from common.frame import local_to_sumo

NET = path("traffic/out/bandra.net.xml.gz")
GRAPH = path("world/out/road_graph.json")


def _way_of(edge_id: str) -> str:
    return re.sub(r"#.*$", "", edge_id.lstrip("-"))


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".net.xml", delete=False) as tmp:
        with gzip.open(NET, "rb") as f:
            shutil.copyfileobj(f, tmp)
    net = sumolib.net.readNet(tmp.name)
    by_way: dict[str, list] = {}
    for e in net.getEdges():
        by_way.setdefault(_way_of(e.getID()), []).append(e)

    g = json.loads(GRAPH.read_text())
    matched = fallback = missing = 0
    for ge in g["edges"]:
        shape = ge["shape"]
        mx, my = shape[len(shape) // 2]
        sx, sy = local_to_sumo(mx, my)
        ways = ge["osm_way_id"] if isinstance(ge["osm_way_id"], list) else [ge["osm_way_id"]]
        cands = [e for w in ways for e in by_way.get(str(w), [])]
        chosen: list[str] = []
        if cands:
            # keep every SUMO edge of the way within 60 m of the graph-edge midpoint (both directions)
            for e in cands:
                d = sumolib.geomhelper.distancePointToPolygon((sx, sy), e.getShape())
                if d < 60:
                    chosen.append(e.getID())
            if chosen:
                matched += 1
        if not chosen:
            near = net.getNeighboringEdges(sx, sy, r=30)
            near = sorted(near, key=lambda t: t[1])
            chosen = [e.getID() for e, d in near[:2] if e.allows("passenger")]
            if chosen:
                fallback += 1
            else:
                missing += 1
        ge["sumo_edge_ids"] = chosen
        ge["sumo_edge_id"] = chosen[0] if chosen else None
    GRAPH.write_text(json.dumps(g, separators=(",", ":")))
    print(f"edges={len(g['edges'])} matched_by_way={matched} nearest_fallback={fallback} unmapped={missing}")


if __name__ == "__main__":
    main()
