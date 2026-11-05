"""WS1 step 0: locate the shared OSM extract, write world/out/meta.json, and provide OSM parsing helpers.

The OSM file is a local Overpass extract (git-ignored). We never hit Overpass unless `--fetch` is given
and the file is missing.

    PYTHONPATH=. python -m world.fetch_osm --osm world/cache/map.osm
"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from common.config import path, settings
from common.frame import _transformers

ATTRIBUTION = "© OpenStreetMap contributors"
DEFAULT_OSM = "world/cache/map.osm"

# osmnx "drive" network definition (highway values that are NOT drivable)
_NOT_DRIVE = {"abandoned", "bridleway", "bus_guideway", "construction", "corridor", "cycleway", "elevator",
              "escalator", "footway", "no", "path", "pedestrian", "planned", "platform", "proposed", "raceway",
              "razed", "service", "steps", "track", "bus_stop", "busway", "via_ferrata"}
_BAD_SERVICE = {"alley", "driveway", "emergency_access", "parking", "parking_aisle", "private"}
WALK_CLASSES = {"footway", "pedestrian", "path", "steps", "living_street"}


def out_dir() -> Path:
    d = path(settings()["world"]["out_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_osm(p: str | None) -> Path:
    q = Path(p or DEFAULT_OSM)
    if not q.is_absolute():
        q = path(str(q))
    return q


def origin_utm() -> tuple[float, float]:
    lon, lat = settings()["world"]["origin_lonlat"]
    e, n = _transformers()[0].transform(lon, lat)
    return float(e), float(n)


def write_meta() -> dict:
    s = settings()["world"]
    m = {"epsg": s["epsg"], "origin_lonlat": list(s["origin_lonlat"]), "origin_utm": list(origin_utm()),
         "bbox_wsen": list(s["bbox_wsen"]), "sumo_net_offset": None, "attribution": ATTRIBUTION}
    (out_dir() / "meta.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return m


def is_drive(tags: dict) -> bool:
    hw = tags.get("highway")
    if not hw or hw in _NOT_DRIVE or tags.get("area") == "yes":
        return False
    if tags.get("motor_vehicle") == "no" or tags.get("motorcar") == "no":
        return False
    if tags.get("service") in _BAD_SERVICE:
        return False
    if tags.get("access") in ("private", "no"):
        return False
    return True


def write_drive_osm(src: Path, dst: Path) -> Path:
    """Copy of the extract with only drivable highway ways (so osmnx builds a clean drive graph)."""
    tree = ET.parse(src)
    root = tree.getroot()
    for el in list(root):
        if el.tag == "relation":
            root.remove(el)
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            if not is_drive(tags):
                root.remove(el)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dst, encoding="utf-8", xml_declaration=True)
    return dst


def walk_ways(src: Path) -> list[dict]:
    """Raw pedestrian ways as lon/lat polylines: [{"osm_id", "highway", "coords":[(lon,lat),...]}]."""
    root = ET.parse(src).getroot()
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.iter("node")}
    out = []
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        if tags.get("highway") in WALK_CLASSES - {"living_street"}:
            coords = [nodes[nd.get("ref")] for nd in w.findall("nd") if nd.get("ref") in nodes]
            if len(coords) >= 2:
                out.append({"osm_id": int(w.get("id")), "highway": tags["highway"], "coords": coords})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=DEFAULT_OSM)
    ap.add_argument("--fetch", action="store_true", help="download from Overpass if the file is missing")
    a = ap.parse_args(argv)
    osm = resolve_osm(a.osm)
    if not osm.exists():
        if not a.fetch:
            raise SystemExit(f"OSM file not found: {osm} (pass --fetch to download from Overpass)")
        import urllib.parse
        import urllib.request
        w, s, e, n = settings()["world"]["bbox_wsen"]
        q = f"[out:xml][timeout:120];(nwr({s},{w},{n},{e}););(._;>;);out body;"
        data = urllib.parse.urlencode({"data": q}).encode()
        osm.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen("https://overpass-api.de/api/interpreter", data, timeout=180) as r:
            osm.write_bytes(r.read())
    m = write_meta()
    print(f"[fetch_osm] osm={osm} ({osm.stat().st_size/1e6:.1f} MB) origin_utm={m['origin_utm']}")
    return m


if __name__ == "__main__":
    main()
