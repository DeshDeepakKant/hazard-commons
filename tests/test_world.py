"""WS1 world/out sanity checks (run `python -m world.build_all` first; outputs are committed)."""
import json

import networkx as nx
import pytest

from common.config import path, settings
from common.frame import _transformers

OUT = path("world/out")
FILES = ["meta.json", "road_graph.json", "roads.json", "buildings.json", "buildings.glb",
         "collision/boxes.json", "pois.json"]


def _load(name):
    return json.loads((OUT / name).read_text())


@pytest.fixture(scope="module")
def graph():
    return _load("road_graph.json")


@pytest.fixture(scope="module")
def digraph(graph):
    G = nx.DiGraph()
    G.add_nodes_from(n["id"] for n in graph["nodes"])
    for e in graph["edges"]:
        if e["kind"] != "drive":
            continue
        G.add_edge(e["from"], e["to"], weight=e["length"])
        if not e["oneway"]:
            G.add_edge(e["to"], e["from"], weight=e["length"])
    return G


@pytest.mark.parametrize("name", FILES)
def test_files_exist(name):
    p = OUT / name
    assert p.exists() and p.stat().st_size > 0
    assert p.stat().st_size < 15e6


def test_meta_origin_matches_settings():
    m = _load("meta.json")
    lon, lat = settings()["world"]["origin_lonlat"]
    e, n = _transformers()[0].transform(lon, lat)
    assert m["origin_utm"] == pytest.approx([e, n], abs=1e-6)
    assert m["epsg"] == 32643 and m["sumo_net_offset"] is None
    assert "OpenStreetMap" in m["attribution"]


def test_edges_reference_nodes(graph):
    ids = {n["id"] for n in graph["nodes"]}
    eids = [e["id"] for e in graph["edges"]]
    assert len(eids) == len(set(eids))
    for e in graph["edges"]:
        assert e["from"] in ids and e["to"] in ids
        assert len(e["shape"]) >= 2 and e["length"] > 0 and e["lanes"] >= 1
        assert "osm_way_id" in e and "sumo_edge_id" in e


def test_graph_strongly_connected(digraph):
    assert nx.is_strongly_connected(digraph)


def test_pois(graph, digraph):
    p = _load("pois.json")
    ids = {n["id"] for n in graph["nodes"]}
    bids = {b["id"] for b in _load("buildings.json")}
    assert sorted(s["operator"] for s in p["dark_stores"]) == sorted(o["name"] for o in settings()["operators"])
    assert 40 <= len(p["homes"]) <= 60
    for poi in p["dark_stores"] + p["homes"]:
        assert poi["entrance_node"] in ids
        assert poi["building_id"] in bids
        assert len(poi["entrance_xy"]) == 2
    for h in p["homes"]:
        assert h["address"] and h["customer_name"] and h["height"] > 0
    st = p["dark_stores"]
    for i, a in enumerate(st):
        for b in st[i + 1:]:
            assert ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 >= 400


def test_routes_store_to_every_home(digraph):
    p = _load("pois.json")
    for s in p["dark_stores"]:
        lengths = nx.single_source_dijkstra_path_length(digraph, s["entrance_node"], weight="weight")
        for h in p["homes"]:
            assert h["entrance_node"] in lengths, (s["id"], h["id"])
            back = nx.shortest_path(digraph, h["entrance_node"], s["entrance_node"], weight="weight")
            assert back[0] == h["entrance_node"]


def test_extents(graph):
    xs = [n["x"] for n in graph["nodes"]]
    ys = [n["y"] for n in graph["nodes"]]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    assert 1500 < w < 1900, w
    assert 1400 < h < 1800, h
    assert abs((max(xs) + min(xs)) / 2) < 150 and abs((max(ys) + min(ys)) / 2) < 150


def test_buildings_and_boxes():
    b = _load("buildings.json")
    assert len(b) > 1500
    for r in b[:200]:
        assert r["use"] in ("residential", "commercial", "other")
        assert r["height_source"] in ("osm_height", "levels", "pseudo")
        assert r["height"] > 0 and len(r["footprint"]) >= 3
    boxes = _load("collision/boxes.json")
    assert 0 < len(boxes) <= len(b)
    for x in boxes[:200]:
        assert len(x["center"]) == 3 and len(x["half_extents"]) == 3 and min(x["half_extents"]) > 0
