"""WS1 step 1: drive network -> world/out/road_graph.json and world/out/roads.json.

    PYTHONPATH=. python -m world.build_graph --osm world/cache/map.osm
"""
from __future__ import annotations

import argparse
import json
import re

import networkx as nx
import osmnx as ox

from common.config import path, settings
from common.frame import lonlat_to_local
from world.fetch_osm import DEFAULT_OSM, out_dir, resolve_osm, walk_ways, write_drive_osm, write_meta

MAJOR = {"trunk", "primary", "secondary", "tertiary"}
ROAD_WIDTH = {"trunk": 14.0, "primary": 12.0, "secondary": 10.0, "tertiary": 8.0, "unclassified": 6.0,
              "residential": 6.0, "living_street": 5.0, "road": 6.0}
WALK_WIDTH = {"footway": 2.0, "pedestrian": 4.0, "path": 1.5, "steps": 2.0}


def _first(v):
    return v[0] if isinstance(v, list) else v


def _hw(v) -> str:
    """Highest-ranked highway class of a (possibly merged) edge."""
    vals = v if isinstance(v, list) else [v]
    order = ["trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street"]
    base = [str(x).replace("_link", "") for x in vals]
    for o in order:
        if o in base:
            return o
    return base[0]


def _lanes(d: dict, hw: str) -> int:
    v = d.get("lanes")
    if v is not None:
        nums = [int(m) for x in (v if isinstance(v, list) else [v]) for m in re.findall(r"\d+", str(x))]
        if nums:
            return max(1, min(6, max(nums)))
    return 2 if hw in MAJOR else 1


def _r(v: float) -> float:
    return round(float(v), 2)


def build(osm_path) -> dict:
    s = settings()["world"]
    w, so, e, n = s["bbox_wsen"]
    drive_osm = write_drive_osm(osm_path, path("world/cache/drive.osm"))
    G = ox.graph_from_xml(drive_osm, simplify=False, retain_all=True)
    G = ox.truncate.truncate_graph_bbox(G, (w, so, e, n), truncate_by_edge=False)
    G = ox.simplify_graph(G)
    # largest strongly connected component => every node can reach every other node (routable)
    scc = max(nx.strongly_connected_components(G), key=len)
    G = G.subgraph(scc).copy()

    loc = {u: lonlat_to_local(d["x"], d["y"]) for u, d in G.nodes(data=True)}
    nodes = [{"id": f"n{u}", "x": _r(loc[u][0]), "y": _r(loc[u][1])} for u in sorted(G.nodes)]

    edges, seen = [], set()
    for u, v, k, d in sorted(G.edges(keys=True, data=True), key=lambda t: (t[0], t[1], t[2])):
        oneway = bool(_first(d.get("oneway", False)))
        if "geometry" in d:
            coords = list(d["geometry"].coords)
        else:
            coords = [(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])]
        shape = [[_r(c) for c in lonlat_to_local(lon, lat)] for lon, lat in coords]
        length = float(d.get("length", 0.0))
        if not oneway:
            # two-way roads appear as u->v and v->u in osmnx; keep one undirected record
            key = (min(u, v), max(u, v), round(length, 1))
            if key in seen:
                continue
            seen.add(key)
        hw = _hw(d.get("highway", "road"))
        osm_ids = d.get("osmid")
        osm_ids = sorted(set(int(x) for x in osm_ids)) if isinstance(osm_ids, list) else int(osm_ids)
        name = d.get("name")
        if isinstance(name, list):
            name = " / ".join(sorted(set(str(x) for x in name)))  # osmnx list order is not stable
        eid = f"e{u}-{v}" + (f"-{k}" if k else "")
        edges.append({"id": eid, "from": f"n{u}", "to": f"n{v}", "length": round(length, 2),
                      "lanes": _lanes(d, hw), "name": name if isinstance(name, str) else None,
                      "kind": "drive", "oneway": oneway, "shape": shape, "sumo_edge_id": None,
                      "highway": hw, "osm_way_id": osm_ids})
    return {"nodes": nodes, "edges": edges}


def build_roads(graph: dict, osm_path) -> list[dict]:
    w, so, e, n = settings()["world"]["bbox_wsen"]
    roads = [{"id": ed["id"], "kind": "drive",
              "width": max(ROAD_WIDTH.get(ed["highway"], 6.0), 3.0 * ed["lanes"]), "shape": ed["shape"]}
             for ed in graph["edges"]]
    for wy in walk_ways(osm_path):
        pts = [(lon, lat) for lon, lat in wy["coords"] if w <= lon <= e and so <= lat <= n]
        if len(pts) < 2:
            continue
        roads.append({"id": f"w{wy['osm_id']}", "kind": "walk", "width": WALK_WIDTH.get(wy["highway"], 2.0),
                      "shape": [[_r(c) for c in lonlat_to_local(lon, lat)] for lon, lat in pts]})
    return roads


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=DEFAULT_OSM)
    a = ap.parse_args(argv)
    osm = resolve_osm(a.osm)
    write_meta()
    g = build(osm)
    od = out_dir()
    (od / "road_graph.json").write_text(json.dumps(g, separators=(",", ":"), ensure_ascii=False))
    roads = build_roads(g, osm)
    (od / "roads.json").write_text(json.dumps(roads, separators=(",", ":"), ensure_ascii=False))
    print(f"[build_graph] nodes={len(g['nodes'])} edges={len(g['edges'])} "
          f"oneway={sum(e['oneway'] for e in g['edges'])} roads={len(roads)} "
          f"(walk={sum(r['kind'] == 'walk' for r in roads)})")
    return g


if __name__ == "__main__":
    main()
