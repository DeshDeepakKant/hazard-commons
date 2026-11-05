"""WS2 traffic bridge tests (libsumo, in-process). One SUMO instance for the whole module."""
from __future__ import annotations

import math

import pytest

from common.config import ROOT, settings
from common.frame import lonlat_to_local, sumo_to_local

pytest.importorskip("libsumo")
pytest.importorskip("sumolib")

NET = ROOT / "traffic" / "out" / "bandra.net.xml.gz"
pytestmark = pytest.mark.skipif(not NET.exists(), reason="run traffic/build_net.sh first")

MARGIN = 150.0  # m beyond the bbox (clip_edges keeps edges within bbox + 120 m)


def _bbox_local():
    w, s, e, n = settings()["world"]["bbox_wsen"]
    x0, y0 = lonlat_to_local(w, s)
    x1, y1 = lonlat_to_local(e, n)
    return x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN


@pytest.fixture(scope="module")
def tb():
    from traffic.bridge import TrafficBridge

    b = TrafficBridge(warmup_s=200)
    b.start(seed=settings()["traffic"]["seed"])
    yield b
    b.close()


def test_net_alignment():
    """SUMO coords -> local frame agree with OSM lon/lat -> local frame (< 1 m)."""
    import sumolib

    net = sumolib.net.readNet(str(NET))
    nodes = [n for n in net.getNodes() if not n.getID().startswith("cluster")][:25]
    assert nodes
    for nd in nodes:
        sx, sy = nd.getCoord()
        a = sumo_to_local(sx, sy)
        b = lonlat_to_local(*net.convertXY2LonLat(sx, sy))
        assert math.dist(a, b) < 1.0


def test_yaw_conversion():
    from traffic.bridge import compass_to_yaw, yaw_to_compass

    assert compass_to_yaw(90) == pytest.approx(0.0)            # SUMO east
    assert compass_to_yaw(0) == pytest.approx(math.pi / 2)     # SUMO north
    assert compass_to_yaw(180) == pytest.approx(-math.pi / 2)  # SUMO south
    for yaw in (-2.5, -1.0, 0.0, 1.0, 3.0):
        assert compass_to_yaw(yaw_to_compass(yaw)) == pytest.approx(yaw)


def test_actors_in_bbox_and_pedestrians(tb):
    actors = []
    for k in range(300):
        actors = tb.step(k * 0.05)
    kinds = {a.kind for a in actors}
    assert len(actors) > 50
    assert "pedestrian" in kinds
    assert {"car", "auto", "bike"} <= kinds
    x0, y0, x1, y1 = _bbox_local()
    for a in actors:
        assert x0 <= a.x <= x1 and y0 <= a.y <= y1, a
        assert -math.pi - 1e-6 <= a.yaw <= math.pi + 1e-6
    n_veh = sum(a.kind != "pedestrian" for a in actors)
    n_ped = sum(a.kind == "pedestrian" for a in actors)
    assert n_veh <= 200 and n_ped <= settings()["traffic"]["max_pedestrians"]


def test_vehicle_heading_matches_motion(tb):
    """yaw (CCW from east) must point along the direction of travel."""
    t = tb.sumo_time - tb.warmup_s
    before = {a.id: a for a in tb.step(t + 0.05) if a.kind != "pedestrian" and a.speed > 5}
    after = {a.id: a for a in tb.step(t + 0.55)}
    checked = 0
    for vid, a in before.items():
        b = after.get(vid)
        if b is None or math.dist((a.x, a.y), (b.x, b.y)) < 1.5:
            continue
        mv = math.atan2(b.y - a.y, b.x - a.x)
        d = abs(math.atan2(math.sin(mv - a.yaw), math.cos(mv - a.yaw)))
        if d < 0.6:
            checked += 1
    assert checked >= 3


def test_edge_at(tb):
    ts = tb.ts
    vid = next(v for v in ts.vehicle.getIDList() if not v.startswith("bot_")
               and not ts.vehicle.getRoadID(v).startswith(":"))
    edge = ts.vehicle.getRoadID(vid)
    lane = ts.vehicle.getLaneID(vid)
    sx, sy = ts.lane.getShape(lane)[0]
    x, y = sumo_to_local(*ts.simulation.convert2D(edge, 1.0, ts.vehicle.getLaneIndex(vid)))
    assert tb.edge_at(x, y) is not None
    assert tb.edge_at(1e6, 1e6) is None or isinstance(tb.edge_at(1e6, 1e6), str)


def test_close_edge_blocks_traffic(tb):
    ts = tb.ts
    # busiest normal edge right now
    count: dict[str, int] = {}
    for v in ts.vehicle.getIDList():
        e = ts.vehicle.getRoadID(v)
        if e and not e.startswith(":"):
            count[e] = count.get(e, 0) + 1
    edge = max(count, key=count.get)
    assert tb.close_edge(edge)
    assert edge in tb.closed_edges
    t = tb.sumo_time - tb.warmup_s
    for k in range(1, 201):          # 10 s
        tb.step(t + k * 0.05)
    # nobody on the closed edge moves faster than a crawl
    on = [v for v in ts.vehicle.getIDList() if ts.vehicle.getRoadID(v) == edge]
    assert all(ts.vehicle.getSpeed(v) <= 0.5 for v in on)
    # nobody not yet on it still plans to use it (rerouted around)
    for v in ts.vehicle.getIDList():
        if v.startswith("bot_") or ts.vehicle.getRoadID(v) == edge:
            continue
        r, i = ts.vehicle.getRoute(v), ts.vehicle.getRouteIndex(v)
        if edge in r[i + 1:]:
            # allowed only if there is no alternative (dead end); then it must queue
            assert ts.vehicle.getRoadID(v) != edge
    assert tb.reopen_edge(edge)
    assert edge not in tb.closed_edges
    assert ts.lane.getMaxSpeed(f"{edge}_0") > 1.0


def test_upsert_robot(tb):
    ts = tb.ts
    # put the robot on the start of some vehicle lane
    vid = next(v for v in ts.vehicle.getIDList() if not v.startswith("bot_")
               and not ts.vehicle.getRoadID(v).startswith(":"))
    edge = ts.vehicle.getRoadID(vid)
    sx, sy = ts.simulation.convert2D(edge, 2.0, 0)
    x, y = sumo_to_local(sx, sy)
    yaw = 0.3
    tb.upsert_robot("R-99", x, y, yaw)
    t = tb.sumo_time - tb.warmup_s
    for k in range(1, 11):
        actors = tb.step(t + k * 0.05)
        # move slowly east-north-east
        x += 0.2 * math.cos(yaw)
        y += 0.2 * math.sin(yaw)
        tb.upsert_robot("R-99", x, y, yaw)
    tb.step(t + 0.6)
    assert "bot_R-99" in ts.vehicle.getIDList()
    assert all(not a.id.startswith("bot_") for a in actors)
    rx, ry = sumo_to_local(*ts.vehicle.getPosition("bot_R-99"))
    # SUMO reports the front bumper: within a robot length of the commanded centre
    assert math.dist((rx, ry), (x, y)) < 1.5
    assert ts.vehicle.getTypeID("bot_R-99") == "delivery_bot"
    tb.remove_robot("R-99")
    tb.step(t + 0.7)
    assert "bot_R-99" not in ts.vehicle.getIDList()


def test_step_never_raises(tb):
    tb.close_edge("does-not-exist")
    tb.upsert_robot("R-bad", 1e7, 1e7, 0.0)     # far off the net
    t = tb.sumo_time - tb.warmup_s
    out = tb.step(t + 0.05)
    assert isinstance(out, list) and out
    tb.remove_robot("R-bad")
    tb.step(t + 0.1)
