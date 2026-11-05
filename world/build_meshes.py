"""WS1 step 2: buildings -> world/out/buildings.json, buildings.glb, collision/boxes.json.

Heights: OSM `height` -> `building:levels` x floor_height -> deterministic pseudo-random 3-12 floors.
glb is glTF Y-up: vertex = (x, height, -y); one mesh/node per building named by building id.

    PYTHONPATH=. python -m world.build_meshes --osm world/cache/map.osm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re

import numpy as np
import shapely
import osmnx as ox
import trimesh
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.strtree import STRtree

from common.config import settings
from common.frame import _transformers, meta
from world.fetch_osm import DEFAULT_OSM, out_dir, resolve_osm

RESIDENTIAL = {"apartments", "house", "residential", "detached", "bungalow", "terrace", "dormitory",
               "semidetached_house", "flats"}
COMMERCIAL = {"commercial", "retail", "office", "supermarket", "shop", "hotel", "kiosk", "mall", "store"}
COLORS = {"residential": (176, 160, 146, 255), "commercial": (112, 146, 196, 255), "other": (168, 168, 168, 255)}


def _s(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return str(v)


def _num(v):
    v = _s(v)
    if not v:
        return None
    m = re.search(r"\d+(?:\.\d+)?", v.replace(",", "."))
    return float(m.group()) if m else None


def pseudo_floors(bid: str) -> int:
    h = int(hashlib.sha256(bid.encode()).hexdigest()[:8], 16)
    return 3 + h % 10  # 3..12


def _use(row, shop_pts_inside: bool) -> str:
    b = _s(row.get("building")) or "yes"
    if b in COMMERCIAL or _s(row.get("shop")) or shop_pts_inside:
        return "commercial"
    if b in RESIDENTIAL:
        return "residential"
    return "other"


def _address(row) -> str | None:
    hn, st = _s(row.get("addr:housenumber")), _s(row.get("addr:street"))
    if not st:
        return None
    parts = [f"{hn} {st}" if hn else st]
    for k in ("addr:suburb", "addr:city"):
        v = _s(row.get(k))
        if v and v not in parts:
            parts.append(v)
    if not any("Mumbai" in p for p in parts):
        parts.append("Mumbai")
    pc = _s(row.get("addr:postcode"))
    return ", ".join(parts) + (f" {pc}" if pc else "")


def load_buildings(osm_path) -> list[dict]:
    s = settings()["world"]
    w, so, e, n = s["bbox_wsen"]
    fh = float(s["floor_height_m"])
    bb = box(w, so, e, n)
    gdf = ox.features_from_xml(osm_path, tags={"building": True})
    shops = ox.features_from_xml(osm_path, tags={"shop": True})
    shop_pts = [g.centroid if g.geom_type != "Point" else g for g in shops.geometry]
    shop_tree = STRtree(shop_pts)

    out = []
    for (etype, oid), row in gdf.iterrows():
        g = row.geometry
        if isinstance(g, MultiPolygon):
            g = max(g.geoms, key=lambda p: p.area)
        if not isinstance(g, Polygon) or not bb.contains(g.centroid):
            continue
        bid = f"b{oid}" if etype == "way" else f"b{etype[0]}{oid}"
        n_shops = len(shop_tree.query(g, predicate="intersects"))
        lg = shapely.transform(g, lambda a: np.column_stack(_proj_arr(a[:, 0], a[:, 1])))
        lg = Polygon(lg.exterior).buffer(0)
        if isinstance(lg, MultiPolygon):
            lg = max(lg.geoms, key=lambda p: p.area)
        if lg.is_empty or lg.area < 8.0:
            continue
        lg = lg.simplify(0.4, preserve_topology=True)
        h_osm, lv = _num(row.get("height")), _num(row.get("building:levels"))
        if h_osm and 2.0 <= h_osm <= 250:
            height, src = h_osm, "osm_height"
        elif lv and 1 <= lv <= 80:
            height, src = lv * fh, "levels"
        else:
            height, src = pseudo_floors(bid) * fh, "pseudo"
        c = lg.centroid
        out.append({"id": bid, "osm_id": int(oid), "use": _use(row, n_shops > 0), "height": round(height, 2),
                    "height_source": src, "centroid": [round(c.x, 2), round(c.y, 2)],
                    "footprint": [[round(x, 2), round(y, 2)] for x, y in list(lg.exterior.coords)[:-1]],
                    "name": _s(row.get("name")), "address": _address(row),
                    "_building_tag": _s(row.get("building")), "_shops": n_shops, "_area": round(lg.area, 1)})
    out.sort(key=lambda b: b["id"])
    return out


def _proj_arr(x, y):
    e, n = _transformers()[0].transform(np.asarray(x), np.asarray(y))
    oe, on = meta()["origin_utm"]
    return e - oe, n - on


def export_glb(buildings: list[dict], dst) -> None:
    scene = trimesh.Scene()
    for b in buildings:
        poly = Polygon(b["footprint"])
        if not poly.is_valid:
            poly = poly.buffer(0)
            if isinstance(poly, MultiPolygon):
                poly = max(poly.geoms, key=lambda p: p.area)
        try:
            m = trimesh.creation.extrude_polygon(poly, b["height"])
        except Exception:
            continue
        v = m.vertices
        m.vertices = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]).astype(np.float32)  # glTF Y-up
        m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=np.tile(COLORS[b["use"]], (len(m.vertices), 1)))
        scene.add_geometry(m, node_name=b["id"], geom_name=b["id"])
    data = scene.export(file_type="glb")
    dst.write_bytes(data)


def collision_boxes(buildings: list[dict], road_graph: dict, buffer_m: float) -> list[dict]:
    lines = [LineString(e["shape"]) for e in road_graph["edges"] if e["kind"] == "drive" and len(e["shape"]) >= 2]
    tree = STRtree(lines)
    out = []
    for b in buildings:
        poly = Polygon(b["footprint"])
        if len(tree.query(poly, predicate="dwithin", distance=buffer_m)) == 0:
            continue
        rect = poly.minimum_rotated_rectangle
        if rect.geom_type != "Polygon":
            continue
        pts = np.array(rect.exterior.coords)[:4]
        e0, e1 = pts[1] - pts[0], pts[2] - pts[1]
        l0, l1 = np.linalg.norm(e0), np.linalg.norm(e1)
        long_e, long_l, short_l = (e0, l0, l1) if l0 >= l1 else (e1, l1, l0)
        yaw = math.atan2(long_e[1], long_e[0])
        yaw = (yaw + math.pi / 2) % math.pi - math.pi / 2  # box symmetric: keep in [-pi/2, pi/2)
        c = rect.centroid
        out.append({"id": b["id"], "center": [round(c.x, 2), round(c.y, 2), round(b["height"] / 2, 2)],
                    "half_extents": [round(long_l / 2, 2), round(short_l / 2, 2), round(b["height"] / 2, 2)],
                    "yaw": round(yaw, 4)})
    return out


def main(argv=None):
    """Returns the building records incl. private `_` fields (used by pois.py)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=DEFAULT_OSM)
    a = ap.parse_args(argv)
    od = out_dir()
    blds = load_buildings(resolve_osm(a.osm))
    public = [{k: v for k, v in b.items() if not k.startswith("_")} for b in blds]
    (od / "buildings.json").write_text(json.dumps(public, separators=(",", ":"), ensure_ascii=False))
    export_glb(public, od / "buildings.glb")
    rg = json.loads((od / "road_graph.json").read_text())
    boxes = collision_boxes(public, rg, float(settings()["world"]["collision_road_buffer_m"]))
    (od / "collision").mkdir(exist_ok=True)
    (od / "collision" / "boxes.json").write_text(json.dumps(boxes, separators=(",", ":")))
    srcs = {s: sum(b["height_source"] == s for b in public) for s in ("osm_height", "levels", "pseudo")}
    uses = {u: sum(b["use"] == u for b in public) for u in COLORS}
    print(f"[build_meshes] buildings={len(public)} uses={uses} heights={srcs} boxes={len(boxes)} "
          f"glb={(od / 'buildings.glb').stat().st_size / 1e6:.2f} MB")
    return blds


if __name__ == "__main__":
    main()
