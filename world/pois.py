"""WS1 step 3: dark stores + homes -> world/out/pois.json.

3 dark stores (one per fictional operator) in commercial buildings next to the drive network, >= 400 m apart;
~50 homes in residential buildings, spread out by farthest-point sampling. Everything is deterministic.

    PYTHONPATH=. python -m world.pois --osm world/cache/map.osm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from common.config import operator_names
from world.build_meshes import load_buildings
from world.fetch_osm import DEFAULT_OSM, out_dir, resolve_osm

N_HOMES = 50
STORE_MIN_SEP_M = 400.0
STORE_RING_M = 380.0
MAX_ROAD_DIST_M = 30.0
EDGE_MARGIN_M = 80.0

FIRST = ["Aarav", "Vivaan", "Aditya", "Arjun", "Rohan", "Kabir", "Ishaan", "Reyansh", "Siddharth", "Nikhil",
         "Rahul", "Vikram", "Karan", "Amit", "Sanjay", "Farhan", "Imran", "Joseph", "Anthony", "Neville",
         "Ananya", "Diya", "Saanvi", "Aanya", "Priya", "Neha", "Pooja", "Kavya", "Meera", "Riya",
         "Sneha", "Tanvi", "Zoya", "Ayesha", "Sana", "Fatima", "Maria", "Sheryl", "Lavanya", "Gauri"]
LAST = ["Sharma", "Patel", "Mehta", "Shah", "Iyer", "Nair", "Menon", "Desai", "Kulkarni", "Joshi",
        "Deshpande", "Patil", "Pawar", "Rao", "Reddy", "Gupta", "Agarwal", "Kapoor", "Malhotra", "Khanna",
        "Fernandes", "D'Souza", "Pereira", "Rodrigues", "Pinto", "Khan", "Shaikh", "Qureshi", "Sayed", "Ansari",
        "Bhatt", "Chopra", "Saxena", "Verma", "Mishra", "Banerjee", "Chatterjee", "Bose", "Sen", "Pillai"]


class Net:
    """Drive edges + nodes for nearest-point queries."""

    def __init__(self, rg: dict):
        self.nodes = {n["id"]: (n["x"], n["y"]) for n in rg["nodes"]}
        self.edges = [e for e in rg["edges"] if e["kind"] == "drive"]
        self.lines = [LineString(e["shape"]) for e in self.edges]
        self.tree = STRtree(self.lines)
        named = [i for i, e in enumerate(self.edges) if e.get("name")]
        self.named_idx = named
        self.named_tree = STRtree([self.lines[i] for i in named])

    def dist(self, geom) -> float:
        i = int(self.tree.nearest(geom))
        return float(self.lines[i].distance(geom))

    def entrance(self, poly: Polygon) -> dict:
        i = int(self.tree.nearest(poly))
        line, e = self.lines[i], self.edges[i]
        p = nearest_points(poly, line)[1]
        s = line.project(p)
        node = e["from"] if s <= line.length / 2 else e["to"]
        return {"entrance_node": node, "entrance_xy": [round(p.x, 2), round(p.y, 2)], "entrance_edge": e["id"],
                "_dist": float(poly.distance(line))}

    def street_name(self, poly: Polygon) -> str | None:
        if not self.named_idx:
            return None
        j = int(self.named_tree.nearest(poly))
        i = self.named_idx[j]
        return self.edges[i]["name"] if self.lines[i].distance(poly) < 120 else None


def _seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:12], 16)


def customer_name(home_id: str, used: set) -> str:
    rng = random.Random(_seed("customer:" + home_id))
    while True:
        nm = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if nm not in used:
            used.add(nm)
            return nm


def display_address(b: dict, street: str | None) -> str:
    if b.get("address"):
        return b["address"]
    label = b.get("name") or f"Bldg {b['osm_id']}"
    return f"{label}, {street or 'Bandra West'}{', Bandra West' if street else ''}, Mumbai"


def _interior(b, half_w, half_h) -> bool:
    x, y = b["centroid"]
    return abs(x) < half_w - EDGE_MARGIN_M and abs(y) < half_h - EDGE_MARGIN_M


def build(buildings: list[dict], rg: dict) -> dict:
    net = Net(rg)
    xs = [n[0] for n in net.nodes.values()]
    ys = [n[1] for n in net.nodes.values()]
    half_w, half_h = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    polys = {b["id"]: Polygon(b["footprint"]) for b in buildings}
    near = {b["id"]: net.dist(polys[b["id"]]) for b in buildings}

    # --- dark stores: commercial, close to a drive road, on a ring around the centre, >= 400 m apart
    cands = [b for b in buildings if b["use"] == "commercial" and near[b["id"]] < 20.0
             and 80 <= b["_area"] <= 5000 and _interior(b, half_w, half_h)]
    stores, chosen = [], []
    for k, op in enumerate(operator_names()):
        ang = math.radians(90 + 120 * k)
        ax, ay = STORE_RING_M * math.cos(ang), STORE_RING_M * math.sin(ang)
        ok = [b for b in cands if b not in chosen and all(
            math.dist(b["centroid"], c["centroid"]) >= STORE_MIN_SEP_M for c in chosen)]
        b = min(ok, key=lambda b: (math.dist(b["centroid"], (ax, ay)) + 3.0 * near[b["id"]], b["id"]))
        chosen.append(b)
        ent = net.entrance(polys[b["id"]])
        stores.append({"id": f"DS-{op}", "operator": op, "building_id": b["id"], "name": f"{op} Dark Store",
                       "x": b["centroid"][0], "y": b["centroid"][1], "entrance_node": ent["entrance_node"],
                       "entrance_xy": ent["entrance_xy"], "entrance_edge": ent["entrance_edge"],
                       "address": display_address(b, net.street_name(polys[b["id"]]))})

    # --- homes: residential, close to a drive road, spread by farthest-point sampling
    store_ids = {b["id"] for b in chosen}
    hc = [b for b in buildings if b["use"] == "residential" and near[b["id"]] < MAX_ROAD_DIST_M
          and b["_area"] >= 60 and b["id"] not in store_ids and _interior(b, half_w, half_h)]
    hc.sort(key=lambda b: b["id"])
    pts = np.array([b["centroid"] for b in hc])
    picked = [int(np.argmin(np.hypot(pts[:, 0], pts[:, 1])))]
    dmin = np.hypot(*(pts - pts[picked[0]]).T)
    for c in chosen:  # keep homes away from the stores themselves
        dmin = np.minimum(dmin, np.hypot(*(pts - np.array(c["centroid"])).T))
    while len(picked) < min(N_HOMES, len(hc)):
        i = int(np.argmax(dmin))
        picked.append(i)
        dmin = np.minimum(dmin, np.hypot(*(pts - pts[i]).T))
    # order homes north->south, west->east so ids are readable on the map
    picked.sort(key=lambda i: (-round(pts[i][1] / 200), pts[i][0]))
    homes, used = [], set()
    for k, i in enumerate(picked, 1):
        b = hc[i]
        hid = f"H-{k:02d}"
        ent = net.entrance(polys[b["id"]])
        homes.append({"id": hid, "building_id": b["id"],
                      "address": display_address(b, net.street_name(polys[b["id"]])),
                      "customer_name": customer_name(hid, used), "x": b["centroid"][0], "y": b["centroid"][1],
                      "entrance_node": ent["entrance_node"], "entrance_xy": ent["entrance_xy"],
                      "entrance_edge": ent["entrance_edge"], "height": b["height"]})
    return {"dark_stores": stores, "homes": homes}


def main(argv=None, buildings: list[dict] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=DEFAULT_OSM)
    a = ap.parse_args(argv)
    od = out_dir()
    if buildings is None:
        buildings = load_buildings(resolve_osm(a.osm))
    rg = json.loads((od / "road_graph.json").read_text())
    pois = build(buildings, rg)
    (od / "pois.json").write_text(json.dumps(pois, indent=1, ensure_ascii=False) + "\n")
    st = pois["dark_stores"]
    seps = [round(math.dist((p["x"], p["y"]), (q["x"], q["y"]))) for i, p in enumerate(st) for q in st[i + 1:]]
    print(f"[pois] dark_stores={[s['id'] + ':' + s['building_id'] for s in st]} separations_m={seps} "
          f"homes={len(pois['homes'])}")
    return pois


if __name__ == "__main__":
    main()
