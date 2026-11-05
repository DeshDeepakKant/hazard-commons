"""The ONE local frame shared by SUMO, PyBullet and the viewer.

Local frame: metres, x = east, y = north, z = up, origin = bbox centre projected to EPSG:32643.

    local = utm - origin_utm
    sumo  = utm + netOffset          (netOffset from net.xml <location netOffset=...>)
    => local = sumo - netOffset - origin_utm

Viewer (three.js, glTF Y-up): three = (x_local, z_local, -y_local).
buildings.glb is exported already in glTF convention (Y-up), so the viewer loads it as-is.

world/out/meta.json (written by WS1) holds:
  {"epsg": 32643, "origin_lonlat": [lon, lat], "origin_utm": [E, N],
   "bbox_wsen": [...], "sumo_net_offset": [ox, oy] | null}
"""
from __future__ import annotations

import functools
import json

from common.config import path, settings


@functools.lru_cache(maxsize=1)
def _transformers():
    from pyproj import Transformer

    epsg = settings()["world"]["epsg"]
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return fwd, inv


@functools.lru_cache(maxsize=1)
def meta() -> dict:
    p = path(settings()["world"]["out_dir"]) / "meta.json"
    if p.exists():
        return json.loads(p.read_text())
    lon, lat = settings()["world"]["origin_lonlat"]
    e, n = _transformers()[0].transform(lon, lat)
    return {"epsg": settings()["world"]["epsg"], "origin_lonlat": [lon, lat],
            "origin_utm": [e, n], "bbox_wsen": settings()["world"]["bbox_wsen"],
            "sumo_net_offset": None}


def lonlat_to_local(lon: float, lat: float) -> tuple[float, float]:
    e, n = _transformers()[0].transform(lon, lat)
    oe, on = meta()["origin_utm"]
    return e - oe, n - on


def local_to_lonlat(x: float, y: float) -> tuple[float, float]:
    oe, on = meta()["origin_utm"]
    return _transformers()[1].transform(x + oe, y + on)


def sumo_to_local(sx: float, sy: float) -> tuple[float, float]:
    off = meta().get("sumo_net_offset")
    if off is None:
        raise RuntimeError("sumo_net_offset missing from world/out/meta.json (WS2 must write it)")
    oe, on = meta()["origin_utm"]
    return sx - off[0] - oe, sy - off[1] - on


def local_to_sumo(x: float, y: float) -> tuple[float, float]:
    off = meta().get("sumo_net_offset")
    if off is None:
        raise RuntimeError("sumo_net_offset missing from world/out/meta.json (WS2 must write it)")
    oe, on = meta()["origin_utm"]
    return x + oe + off[0], y + on + off[1]
