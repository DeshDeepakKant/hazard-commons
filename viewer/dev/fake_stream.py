#!/usr/bin/env python3
"""DEV-ONLY FAKE STREAM: a fake sim that emits bus envelopes for viewer development.

    !!!  FAKE STREAM — MOCK, NOT 0G  !!!
    Every decision here is invented locally (result.mock = true, model "MOCK"). Nothing talks to 0G,
    the chain or 0G Storage; hashes are random. Never use this for the demo (refused when DEMO=1).

Usage (from the repo root):
    PYTHONPATH=. .venv/bin/python viewer/dev/fake_stream.py live [--port 8765]
        drives the REAL server (server.app + server.bus.BUS) with a FakeEngine; serves /ws, /api/*.
        Also mounts the fixture world at /fixtures/world/ (meta.json, roads.json, ...).
    PYTHONPATH=. .venv/bin/python viewer/dev/fake_stream.py record [--out viewer/dev/fixtures/stream.jsonl] [--duration 240]
        same sim, faster than realtime, envelopes written as JSONL (state at 5 Hz, floats rounded).
    PYTHONPATH=. .venv/bin/python viewer/dev/fake_stream.py verify [--in viewer/dev/fixtures/stream.jsonl]
        parses + validates a JSONL stream against sim/schemas.py and prints topic counts.

World: viewer/dev/fixtures/world (run viewer/dev/gen_fixture.py first). Stdlib only (+ server deps in live).
"""
from __future__ import annotations

import argparse
import base64
import bisect
import heapq
import json
import math
import os
import random
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORLD = HERE / "fixtures" / "world"
BANNER = "FAKE STREAM — MOCK, NOT 0G"

OPERATORS = [  # (name, colour, rovers, id letter) mirrors config/settings.yaml
    ("ZipKart", "#ff6b1a", 3, "Z"),
    ("MinuteMart", "#2f8cff", 3, "M"),
    ("DashKirana", "#22c55e", 2, "D"),
]
DRONES = [("D-01", "ZipKart"), ("D-02", "MinuteMart")]
ROVER_SPEED = 4.0
DRONE_SPEED = 12.0
CRUISE_Z = 40.0
CP_TRIGGER_M = 18.0
ACTIVE = ("reported", "confirmed")
MOVING = ("TO_STORE", "DELIVERING", "RETURNING")
ITEMS = ["milk 1L", "bread", "eggs x12", "bananas", "atta 5kg", "paneer", "curd", "tomatoes 1kg",
         "onions 1kg", "Maggi x4", "chai patti", "coriander", "batteries AA", "detergent", "ice cream",
         "cold drink 2L", "rice 5kg", "dal 1kg", "biscuits", "chips"]


def banner() -> None:
    line = "!" * 64
    print(f"\n{line}\n!!!{BANNER:^58}!!!\n!!!{'dev-only fake engine: decisions are invented locally':^58}!!!\n{line}\n",
          flush=True)


def rd(v: float, n: int = 2) -> float:
    return round(v, n)


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


# ======================================================================== tiny PNG thumbnails
def _png(w: int, h: int, rows: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def make_thumb(kind: str, w: int = 96, h: int = 72) -> str:
    """Synthetic 'camera frame': sky + road in perspective + an overlay per hazard type."""
    img = [[(0, 0, 0)] * w for _ in range(h)]
    hz = 26

    def blend(c0, c1, a):
        return tuple(int(c0[i] * (1 - a) + c1[i] * a) for i in range(3))

    for y in range(h):
        for x in range(w):
            if kind == "door":
                c = blend((168, 124, 88), (122, 86, 60), y / h)
                if 38 <= x <= 58 and y >= 30:
                    c = (82, 52, 34) if not (x == 55 and 50 <= y <= 52) else (230, 200, 90)
                elif (10 <= x <= 26 or 70 <= x <= 86) and (14 <= y <= 30 or 42 <= y <= 58):
                    c = (150, 190, 215) if (x + y) % 9 else (190, 220, 235)
                img[y][x] = c
                continue
            if y < hz:
                img[y][x] = blend((140, 182, 228), (226, 234, 242), y / hz)
                continue
            half = 4 + (y - hz) * 1.0
            if abs(x - 48) <= half:
                c = (72, 72, 76)
                if abs(x - 48) <= max(0, (y - hz) // 22) and ((y - hz) // 4) % 2 == 0:
                    c = (226, 204, 70)
                if abs(abs(x - 48) - half) < 1.2:
                    c = (200, 200, 200)
            else:
                c = blend((150, 138, 118), (104, 96, 84), (y - hz) / (h - hz))
                if y < hz + 10 and (x // 7) % 3 == 0:
                    c = (120, 110, 100)
            img[y][x] = c

    def ellipse(cx, cy, rx, ry, col, a=1.0):
        for y in range(max(0, int(cy - ry)), min(h, int(cy + ry) + 1)):
            for x in range(max(0, int(cx - rx)), min(w, int(cx + rx) + 1)):
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                    img[y][x] = blend(img[y][x], col, a)

    if kind == "accident":
        ellipse(52, 50, 15, 6, (160, 28, 28))
        ellipse(52, 46, 8, 3, (40, 40, 50))
        ellipse(36, 54, 9, 4, (230, 230, 235))
        ellipse(42, 44, 6, 5, (255, 140, 20), 0.85)
        for (dx, dy) in ((20, 60), (66, 58), (60, 63), (30, 62)):
            ellipse(dx, dy, 1.5, 1.0, (210, 210, 210))
    elif kind == "waterlogging":
        ellipse(48, 56, 40, 11, (58, 104, 176), 0.8)
        for k in range(4):
            ellipse(30 + k * 12, 54 + (k % 2) * 4, 6, 1.0, (170, 205, 240), 0.7)
    elif kind == "road_works":
        for y in range(40, 49):
            for x in range(26, 70):
                img[y][x] = (245, 120, 20) if ((x + y) // 4) % 2 == 0 else (245, 245, 245)
        for cx in (22, 74):
            for y in range(48, 60):
                hw = (y - 48) * 0.4
                for x in range(int(cx - hw), int(cx + hw) + 1):
                    img[y][x] = (250, 110, 10) if y != 54 else (250, 250, 250)
    rows = [bytearray(v for px in row for v in px) for row in img]
    return base64.b64encode(_png(w, h, rows)).decode()


# ======================================================================== world
class Edge:
    def __init__(self, d: dict, width: float) -> None:
        self.id, self.a, self.b = d["id"], d["from"], d["to"]
        self.kind, self.oneway, self.name, self.lanes = d["kind"], d["oneway"], d["name"], d["lanes"]
        self.pts = [tuple(p) for p in d["shape"]]
        self.cum = [0.0]
        for i in range(len(self.pts) - 1):
            self.cum.append(self.cum[-1] + math.dist(self.pts[i], self.pts[i + 1]))
        self.L = self.cum[-1]
        self.width = width
        self.main = width >= 10

    def at(self, s: float) -> tuple[float, float, float]:
        s = min(max(s, 0.0), self.L)
        i = min(max(bisect.bisect_right(self.cum, s) - 1, 0), len(self.pts) - 2)
        seg = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg <= 0 else (s - self.cum[i]) / seg
        (x0, y0), (x1, y1) = self.pts[i], self.pts[i + 1]
        return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, math.atan2(y1 - y0, x1 - x0)

    def project(self, p) -> tuple[float, float]:
        best = (1e18, 0.0)
        for i in range(len(self.pts) - 1):
            (ax, ay), (bx, by) = self.pts[i], self.pts[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
            d = math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)
            if d < best[0]:
                best = (d, self.cum[i] + t * (self.cum[i + 1] - self.cum[i]))
        return best


class World:
    def __init__(self, d: Path) -> None:
        g = json.loads((d / "road_graph.json").read_text())
        widths = {r["id"]: r["width"] for r in json.loads((d / "roads.json").read_text())}
        self.pois = json.loads((d / "pois.json").read_text())
        self.buildings = {b["id"]: b for b in json.loads((d / "buildings.json").read_text())}
        self.nodes = {n["id"]: (n["x"], n["y"]) for n in g["nodes"]}
        self.edges = {e["id"]: Edge(e, widths.get(e["id"], 6.0)) for e in g["edges"]}
        self.adj: dict[str, dict[str, list]] = {"drive": {}, "walk": {}}
        for e in self.edges.values():
            a = self.adj[e.kind]
            a.setdefault(e.a, []).append((e.id, e.b, True))
            a.setdefault(e.b, []).append((e.id, e.a, False))
        self.drive_ids = [e.id for e in self.edges.values() if e.kind == "drive"]
        self.walk_ids = [e.id for e in self.edges.values() if e.kind == "walk"]
        self.main_ids = [i for i in self.drive_ids if self.edges[i].main]

    def snap(self, p) -> tuple[str, float]:
        best = (1e18, None, 0.0)
        for eid in self.drive_ids:
            d, s = self.edges[eid].project(p)
            if d < best[0]:
                best = (d, eid, s)
        return best[1], best[2]

    # ---------------------------------------------------------------- rover routing
    def plan(self, se: str, ss: float, ge: str, gs: float,
             blocked: dict[str, float] | None = None) -> Optional[list[tuple[str, float, float]]]:
        """Shortest drive route from (edge se, s=ss) to (edge ge, s=gs) as legs (edge, s_from, s_to).
        blocked: edge_id -> s of the obstacle; a route never passes that point. Rovers may U-turn."""
        blocked = blocked or {}
        E = self.edges

        def w(e: Edge) -> float:
            return 0.85 if e.main else 1.0

        def crosses(eid, a, b) -> bool:
            return eid in blocked and min(a, b) - 0.5 <= blocked[eid] <= max(a, b) + 0.5

        best: Optional[tuple[float, list]] = None
        if se == ge and not crosses(se, ss, gs):
            best = (abs(gs - ss) * w(E[se]), [(se, ss, gs)])
        e0 = E[se]
        dist: dict[str, float] = {}
        prev: dict[str, Any] = {}
        heap: list = []
        for node, cost, leg in ((e0.a, ss, (se, ss, 0.0)), (e0.b, e0.L - ss, (se, ss, e0.L))):
            if crosses(se, leg[1], leg[2]):
                continue
            c = cost * w(e0)
            if c < dist.get(node, 1e18):
                dist[node] = c
                prev[node] = ("start", leg)
                heapq.heappush(heap, (c, node))
        while heap:
            c, n = heapq.heappop(heap)
            if c > dist.get(n, 1e18):
                continue
            for eid, other, fwd in self.adj["drive"].get(n, []):
                if eid in blocked:
                    continue
                e = E[eid]
                nc = c + e.L * w(e)
                if nc < dist.get(other, 1e18):
                    dist[other] = nc
                    prev[other] = (n, eid, fwd)
                    heapq.heappush(heap, (nc, other))
        g = E[ge]
        for node, part in ((g.a, (ge, 0.0, gs)), (g.b, (ge, g.L, gs))):
            if node not in dist or crosses(ge, part[1], part[2]):
                continue
            c = dist[node] + abs(gs - part[1]) * w(g)
            if best is None or c < best[0] - 1e-6:
                legs = [part]
                m = node
                while prev[m][0] != "start":
                    pn, eid, fwd = prev[m]
                    legs.append((eid, 0.0, E[eid].L) if fwd else (eid, E[eid].L, 0.0))
                    m = pn
                legs.append(prev[m][1])
                best = (c, legs[::-1])
        if best is None:
            return None
        legs = [lg for lg in best[1] if abs(lg[2] - lg[1]) > 0.01]
        return legs or [(ge, gs, gs)]


class Mover:
    """Follows legs (edge, s_from, s_to) along edge polylines. Lateral offset to the LEFT of travel
    (mode 'travel', left-hand traffic) or fixed to one side of the edge (mode 'edge', sidewalks)."""

    def __init__(self, world: World, legs: list, offset: float = 0.0, mode: str = "travel") -> None:
        self.w = world
        self.offset, self.mode = offset, mode
        self.set_legs(legs)

    def set_legs(self, legs: list) -> None:
        self.legs = list(legs)
        self.i = 0
        self.s = self.legs[0][1]
        self.finished = False

    @property
    def edge(self) -> str:
        return self.legs[self.i][0]

    def advance(self, d: float) -> bool:
        while d > 1e-9 and not self.finished:
            e, a, b = self.legs[self.i]
            dirn = 1.0 if b >= a else -1.0
            rem = (b - self.s) * dirn
            if d < rem:
                self.s += d * dirn
                return False
            d -= max(rem, 0.0)
            self.s = b
            if self.i + 1 < len(self.legs):
                self.i += 1
                self.s = self.legs[self.i][1]
            else:
                self.finished = True
        return self.finished

    def pose(self, extra: float = 0.0) -> tuple[float, float, float]:
        e, a, b = self.legs[self.i]
        x, y, h = self.w.edges[e].at(self.s)
        yaw = h if b >= a else h + math.pi
        ref = yaw if self.mode == "travel" else h
        off = self.offset + extra
        return x - math.sin(ref) * off, y + math.cos(ref) * off, wrap(yaw)

    def remaining(self) -> list[tuple[str, float, float]]:
        if self.finished:
            return []
        e, a, b = self.legs[self.i]
        return [(e, self.s, b)] + self.legs[self.i + 1:]

    def remaining_len(self) -> float:
        return sum(abs(b - a) for _, a, b in self.remaining())

    def route_edges(self) -> list[str]:
        out: list[str] = []
        for e, _, _ in self.remaining():
            if not out or out[-1] != e:
                out.append(e)
        return out

    def hits(self, eid: str, s_h: float) -> Optional[float]:
        """distance along the remaining route to point (eid, s_h), or None."""
        acc = 0.0
        for e, a, b in self.remaining():
            if e == eid and min(a, b) - 0.5 <= s_h <= max(a, b) + 0.5:
                return acc + abs(s_h - a)
            acc += abs(b - a)
        return None


# ======================================================================== agents
class Actor:
    def __init__(self, aid: str, kind: str, mover: Mover, speed: float, rng: random.Random) -> None:
        self.id, self.kind, self.m, self.base = aid, kind, mover, speed
        self.speed = speed
        self.phase = rng.uniform(0, 6.28)
        self.came: Optional[str] = None


class Robot:
    def __init__(self, rid: str, op: str, kind: str) -> None:
        self.id, self.op, self.kind = rid, op, kind
        self.x = self.y = self.yaw = self.speed = 0.0
        self.z = 0.0
        self.fsm = "IDLE"
        self.order: Optional[str] = None
        self.m: Optional[Mover] = None
        self.target: Optional[tuple[str, float]] = None
        self.battery = 1.0
        self.note: Optional[str] = None
        self.timer = 0.0
        self.pending: Optional[dict] = None     # checkpoint in flight
        self.failed: Optional[dict] = None      # checkpoint that failed -> retried after restore
        self.resume_fsm = "IDLE"
        self.wait_until = 0.0
        self.retry_at: Optional[float] = None
        self.probe_at: Optional[float] = None
        self.probe_epoch = 0
        self.pull = 0.0
        self.evaluated: set[str] = set()
        self.trusted: set[str] = set()
        self.seq = 0
        self.batch: list[tuple[int, Optional[str]]] = []
        self.n_uploads = 0
        # drones
        self.home = (0.0, 0.0, 0.0)
        self.wp: Optional[tuple[float, float, float]] = None
        self.task: Optional[str] = None
        self.queue: list[str] = []
        self.phase = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "operator": self.op, "kind": self.kind, "x": rd(self.x), "y": rd(self.y),
                "z": rd(self.z), "yaw": rd(self.yaw), "speed": rd(self.speed), "fsm_state": self.fsm,
                "order_id": self.order,
                "route_edge_ids": self.m.route_edges() if (self.m and self.kind == "rover" and self.fsm != "IDLE") else [],
                "battery": rd(self.battery, 3), "note": self.note}


# ======================================================================== engine
class FakeEngine:
    def __init__(self, sink: Callable[[str, float, dict], None], seed: int = 7, state_hz: float = 10.0,
                 dt: float = 0.1, live: bool = False, period: float = 240.0) -> None:
        self.sink = sink
        self.rng = random.Random(seed)
        self.w = World(WORLD)
        self.dt = dt
        self.tick_n = 0
        self.t = 0.0
        self.state_every = max(1, round(1.0 / (state_hz * dt)))
        self.live, self.period = live, period
        self.lock = threading.RLock()
        self.events: list = []
        self.ev_seq = 0
        self.manual_off = False
        self.sched_off = False
        self.outage_epoch = 0
        self.tee_flip = False
        self.run_id = "fake"
        self.hazards: dict[str, dict] = {}
        self.hz_s: dict[str, float] = {}
        self.props: dict[str, dict] = {}
        self.prop_s: dict[str, float] = {}
        self.prop_hz: dict[str, Optional[str]] = {}
        self.n_hz = self.n_prop = self.n_order = 0
        self.orders: dict[str, dict] = {}
        self.queue: dict[str, list[str]] = {op: [] for op, *_ in OPERATORS}
        self.order_target: dict[str, tuple[str, float]] = {}
        self.scn: dict[str, Any] = {}
        self.thumbs = {k: make_thumb(k) for k in ("accident", "waterlogging", "road_works", "none", "door")}
        self.stores = {s["operator"]: s for s in self.w.pois["dark_stores"]}
        self.store_target = {op: self.w.snap(s["entrance_xy"]) for op, s in self.stores.items()}
        self.homes = self.w.pois["homes"]
        self.home_by_bid = {h["building_id"]: h for h in self.homes}
        self._build_robots()
        self._build_actors()
        self._schedule_cycle(0)
        self.at(15.0, self._auto_order)

    # ------------------------------------------------------------ plumbing
    @property
    def brain_online(self) -> bool:
        return not (self.manual_off or self.sched_off)

    def emit(self, topic: str, data: dict) -> None:
        self.sink(topic, rd(self.t), data)

    def log(self, msg: str, level: str = "info") -> None:
        self.emit("log", {"level": level, "msg": msg})

    def at(self, t: float, fn: Callable[[], None]) -> None:
        self.ev_seq += 1
        heapq.heappush(self.events, (t, self.ev_seq, fn))

    def hexs(self, n: int = 64) -> str:
        return "".join(self.rng.choice("0123456789abcdef") for _ in range(n))

    # ------------------------------------------------------------ setup
    def _build_robots(self) -> None:
        self.robots: list[Robot] = []
        for op, _c, n, letter in OPERATORS:
            for k in range(1, n + 1):
                r = Robot(f"R-{letter}{k}", op, "rover")
                r.battery = self.rng.uniform(0.72, 1.0)
                self.robots.append(r)
        for did, op in DRONES:
            d = Robot(did, op, "drone")
            s = self.stores[op]
            roof = self.w.buildings[s["building_id"]]["height"] + 0.3
            d.home = (s["x"], s["y"], roof)
            d.x, d.y, d.z = s["x"] + self.rng.uniform(-60, 60), s["y"] + self.rng.uniform(-60, 60), CRUISE_Z
            d.battery = self.rng.uniform(0.8, 1.0)
            d.phase = "patrol"
            self.robots.append(d)
        self.by_id = {r.id: r for r in self.robots}
        # initial orders: one per rover. Some rovers start out for delivery from their store, the rest
        # are on the road heading to their store. R-M1 starts on its store's main road (scenario accident).
        for r in self.rovers():
            se, ss = self.store_target[r.op]
            sx, sy, _ = self.w.edges[se].at(ss)
            h = self._pick_home(r.op)
            o = self._new_order(r.op, h["building_id"], self._items(), h["customer_name"])
            if r.id in ("R-Z2", "R-M3", "R-D2"):
                r.m = Mover(self.w, [(se, ss, ss)], offset=self.w.edges[se].width / 2 - 0.8)
                self.queue[r.op].remove(o["id"])
                r.order = o["id"]
                self._order_status(o["id"], "assigned", r.id)
                self._order_status(o["id"], "picking")
                self._order_status(o["id"], "out_for_delivery")
                r.fsm = "DELIVERING"
                self._route(r, self.order_target[o["id"]])
                continue
            pool, lo, hi = self.w.drive_ids, 150.0, 330.0
            if r.id == "R-M1":
                pool, lo, hi = [e for e in self.w.main_ids if self.w.edges[e].name == self.w.edges[se].name], 230.0, 420.0
            for _ in range(200):
                eid = self.rng.choice(pool)
                s0 = self.rng.uniform(0, self.w.edges[eid].L)
                x, y, _ = self.w.edges[eid].at(s0)
                if lo < math.hypot(x - sx, y - sy) < hi:
                    break
            r.m = Mover(self.w, [(eid, s0, s0)], offset=self.w.edges[eid].width / 2 - 0.8)
            r.fsm = "TO_STORE"
            self._assign(r, o)

    def _pick_home(self, op: str) -> dict:
        """quick commerce: mostly homes within ~650 m of the operator's dark store."""
        s = self.stores[op]
        near = [h for h in self.homes if math.dist((h["x"], h["y"]), (s["x"], s["y"])) < 650]
        return self.rng.choice(near if near and self.rng.random() < 0.85 else self.homes)

    def _build_actors(self) -> None:
        self.actors: list[Actor] = []
        mix = [("car", 72, (8.0, 11.5)), ("auto", 36, (6.0, 8.0)), ("bike", 26, (7.5, 10.5)),
               ("bus", 6, (5.5, 7.0))]
        n = 0
        for kind, cnt, (lo, hi) in mix:
            for _ in range(cnt):
                n += 1
                pool = self.w.main_ids if kind == "bus" else self.w.drive_ids
                eid = self.rng.choice(pool)
                e = self.w.edges[eid]
                fwd = True if e.oneway else self.rng.random() < 0.5
                s = self.rng.uniform(0, e.L)
                legs = [(eid, s, e.L)] if fwd else [(eid, s, 0.0)]
                off = e.width / 4 + (0.8 if kind == "bike" else 0.0)
                self.actors.append(Actor(f"v{n}", kind, Mover(self.w, legs, offset=off), self.rng.uniform(lo, hi), self.rng))
        for k in range(40):
            if k < 16 and self.w.walk_ids:
                eid = self.rng.choice(self.w.walk_ids)
                e = self.w.edges[eid]
                off, mode = self.rng.choice((-0.7, 0.7)), "edge"
            else:
                eid = self.rng.choice(self.w.drive_ids)
                e = self.w.edges[eid]
                off, mode = self.rng.choice((-1, 1)) * (e.width / 2 + 1.3), "edge"
            s = self.rng.uniform(0, e.L)
            legs = [(eid, s, e.L)] if self.rng.random() < 0.5 else [(eid, s, 0.0)]
            self.actors.append(Actor(f"p{k + 1}", "pedestrian", Mover(self.w, legs, offset=off, mode=mode),
                                     self.rng.uniform(1.1, 1.5), self.rng))

    def rovers(self) -> list[Robot]:
        return [r for r in self.robots if r.kind == "rover"]

    def drones(self) -> list[Robot]:
        return [r for r in self.robots if r.kind == "drone"]

    # ------------------------------------------------------------ scenario (SPEC §12, compressed)
    def _schedule_cycle(self, k: int) -> None:
        base = k * self.period
        self.scn = {}
        self.at(base + 20.0, self.sc_accident)
        self.at(base + 90.0, self.sc_waterlogging)
        self.at(base + 150.0, self.sc_fake_report)
        self.at(base + 200.0, lambda: self.sc_outage(True))
        self.at(base + 220.0, lambda: self.sc_outage(False))
        self.at(base + 222.0, lambda: self.sc_clear("accident"))
        if self.live:
            self.at(base + 236.0, lambda: self.sc_clear("waterlogging"))
            self.at(base + self.period, lambda: self._next_cycle(k + 1))

    def _next_cycle(self, k: int) -> None:
        for hid in [h for h, z in self.hazards.items() if z["status"] not in ACTIVE]:
            del self.hazards[hid]
        self.log(f"[fake] scenario cycle {k} starts")
        self._schedule_cycle(k)

    def _spot_ahead(self, r: Robot, prefer: Optional[Callable[[Edge], bool]], dmin: float,
                    dmax: float) -> Optional[tuple[str, float, float]]:
        acc = 0.0
        fallback = None
        taken = {p["edge_id"] for p in self.props.values()} | {z["edge_id"] for z in self.hazards.values()
                                                                 if z["status"] in ACTIVE}
        tgt_edge = r.target[0] if r.target else None
        for e, a, b in r.m.remaining():
            E = self.w.edges[e]
            seg = abs(b - a)
            dirn = 1.0 if b >= a else -1.0
            lo, hi = (15.0, E.L - 15.0) if E.L >= 40 else (E.L / 2, E.L / 2)
            if e not in taken and e != tgt_edge:
                for off in (max(dmin - acc, 0.0), max(dmin - acc, 0.0) + 12.0, seg / 2):
                    if off > seg:
                        continue
                    s = a + dirn * off
                    if lo - 1e-6 <= s <= hi + 1e-6 and dmin <= acc + off <= dmax:
                        cand = (e, s, acc + off)
                        if prefer is None or prefer(E):
                            return cand
                        fallback = fallback or cand
                        break
            acc += seg
            if acc > dmax:
                break
        return fallback

    def _add_prop(self, typ: str, eid: str, s: float, real: bool) -> str:
        self.n_prop += 1
        pid = f"P-{self.n_prop}"
        x, y, _ = self.w.edges[eid].at(s)
        self.props[pid] = {"id": pid, "edge_id": eid, "type": typ, "x": rd(x), "y": rd(y), "real": real,
                           "photo": None}
        self.prop_s[pid] = s
        self.prop_hz[pid] = None
        return pid

    def _remove_prop(self, pid: Optional[str]) -> None:
        if pid and pid in self.props:
            del self.props[pid]

    def sc_accident(self) -> None:
        cands = sorted((r for r in self.rovers() if r.op == "MinuteMart" and r.fsm in MOVING),
                       key=lambda r: r.id != "R-M1")
        for r in cands:
            spot = self._spot_ahead(r, lambda E: E.main, 35, 220) or self._spot_ahead(r, None, 35, 220)
            if spot:
                pid = self._add_prop("accident", spot[0], spot[1], True)
                self.scn["accident"] = pid
                self.log(f"[scenario] accident on {self.w.edges[spot[0]].name} ({spot[0]}), "
                         f"{spot[2]:.0f} m ahead of {r.id}")
                return
        self.at(self.t + 1.0, self.sc_accident)

    def sc_waterlogging(self) -> None:
        cands = sorted((r for r in self.rovers() if r.fsm in MOVING),
                       key=lambda r: ({"ZipKart": 0, "DashKirana": 1, "MinuteMart": 2}[r.op], r.id))
        for r in cands:
            spot = self._spot_ahead(r, lambda E: not E.main, 35, 200)
            if spot:
                pid = self._add_prop("waterlogging", spot[0], spot[1], True)
                self.scn["waterlogging"] = pid
                self.log(f"[scenario] waterlogging on {self.w.edges[spot[0]].name} ({spot[0]}), "
                         f"{spot[2]:.0f} m ahead of {r.id}")
                return
        self.at(self.t + 1.0, self.sc_waterlogging)

    def sc_fake_report(self) -> None:
        cands = sorted((r for r in self.rovers() if r.op != "DashKirana" and r.fsm in MOVING),
                       key=lambda r: -r.m.remaining_len())
        for r in cands:
            spot = self._spot_ahead(r, None, 60, 400)
            if not spot:
                continue
            pid = self._add_prop("road_works", spot[0], spot[1], False)
            self.scn["road_works"] = pid
            hid = self._new_hazard(spot[0], spot[1], "road_works", 0.7, "DashKirana", "R-D1",
                                   evidence_root="0x" + self.hexs(), zg_request_id=None, passable=[],
                                   pid=pid)
            self.log(f"[scenario] DashKirana posted a road_works report on {self.w.edges[spot[0]].name} "
                     f"({spot[0]}) on {r.id}'s route — no 0G decision behind it", "warn")
            self._dispatch_drone("D-01", hid)
            return
        self.at(self.t + 1.0, self.sc_fake_report)

    def sc_outage(self, on: bool) -> None:
        self.sched_off = on
        self._brain_changed(f"scheduled 0G outage {'START' if on else 'END'}")

    def set_brain_unreachable(self, on: bool) -> None:   # EngineControl
        with self.lock:
            self.manual_off = bool(on)
            self._brain_changed(f"operator toggled 0G unreachable={bool(on)}")

    def _brain_changed(self, why: str) -> None:
        online = self.brain_online
        self.log(f"[fake] {why}: brain_online={online}", "warn" if not online else "info")
        if not online:
            self.outage_epoch += 1
            return
        for r in self.robots:
            r.probe_at = None
            if r.fsm == "SAFE_STOP":
                r.retry_at = self.t + self.rng.uniform(0.5, 2.5)

    def sc_clear(self, kind: str) -> None:
        pid = self.scn.get(kind)
        hid = self.prop_hz.get(pid) if pid else None
        self._remove_prop(pid)
        if hid and self.hazards.get(hid, {}).get("status") in ACTIVE:
            z = self.hazards[hid]
            z["status"] = "cleared"
            self.emit("hazard", dict(z))
            self._chain("clearHazard", z["reporter_operator"], hid)
            self.log(f"[scenario] {kind} cleared on {self.w.edges[z['edge_id']].name} (hazard {hid})")

    # ------------------------------------------------------------ hazards / chain / storage
    def _new_hazard(self, eid: str, s: float, typ: str, sev: float, op: str, robot: Optional[str],
                    evidence_root: Optional[str], zg_request_id: Optional[str], passable: list,
                    pid: Optional[str]) -> str:
        self.n_hz += 1
        hid = str(self.n_hz)
        tx = "0x" + self.hexs()
        z = {"id": hid, "edge_id": eid, "type": typ, "severity": rd(sev, 3), "status": "reported",
             "reporter_operator": op, "reporter_robot": robot, "evidence_root": evidence_root,
             "zg_request_id": zg_request_id, "tx_hash": tx, "passable_for": passable, "confirms": 0,
             "disputes": 0}
        self.hazards[hid] = z
        self.hz_s[hid] = s
        if pid:
            self.prop_hz[pid] = hid
        self.emit("hazard", dict(z))
        self._chain("reportHazard", op, hid, tx)
        return hid

    def _chain(self, kind: str, op: Optional[str], hid: Optional[str], tx: Optional[str] = None) -> str:
        tx = tx or "0x" + self.hexs()
        ev = {"kind": kind, "tx_hash": tx, "operator": op, "hazard_id": hid, "status": "sent",
              "sim_time": rd(self.t)}
        self.emit("chain_tx", ev)

        def mined() -> None:
            self.emit("chain_tx", dict(ev, status="mined", sim_time=rd(self.t)))
        self.at(self.t + self.rng.uniform(1.8, 2.6), mined)
        return tx

    def _flush_storage(self, r: Robot) -> Optional[str]:
        if not r.batch:
            return None
        r.n_uploads += 1
        root = "0x" + self.hexs()
        self.emit("storage", {"robot_id": r.id, "episode_id": self._episode(r), "root_hash": root,
                              "tx_hash": "0x" + self.hexs(), "record_seqs": [s for s, _ in r.batch],
                              "zg_request_ids": [z for _, z in r.batch if z],
                              "local_path": f"logs/runs/{self.run_id}/episodes/{r.id}/batch-{r.n_uploads:03d}.jsonl"})
        r.batch = []
        return root

    def _episode(self, r: Robot) -> str:
        return f"{self.run_id}-{r.id}"

    # ------------------------------------------------------------ checkpoints (MOCK brain)
    def request_cp(self, r: Robot, cp: str, ctx: dict, on_ok: Optional[Callable], resume: Optional[str] = None) -> None:
        if r.fsm not in ("WAITING_FOR_BRAIN", "SAFE_STOP"):
            r.resume_fsm = r.fsm
        if resume:
            r.resume_fsm = resume
        r.fsm = "WAITING_FOR_BRAIN"
        lat = (self.rng.uniform(1300, 1700) if cp == "CP-H1" else self.rng.uniform(800, 2500)) \
            if self.brain_online else self.rng.uniform(250, 600)
        r.pending = {"cp": cp, "ctx": ctx, "on_ok": on_ok, "lat": lat}
        r.wait_until = self.t + lat / 1000.0
        r.note = f"{cp}: asking 0G (MOCK)…"

    def _mock_output(self, cp: str, ctx: dict) -> tuple[dict, str]:
        if cp == "CP-H1":
            p = ctx["prop"]
            spec = {"accident": (0.8, 0.91, "Two-vehicle collision blocking the left carriageway; debris across the lane."),
                    "waterlogging": (0.6, 0.88, "Standing water ~30 cm deep across the lane; exceeds rover wading depth."),
                    "road_works": (0.5, 0.85, "Barricaded excavation occupying the lane.")}[p["type"]]
            return ({"is_hazard": True, "type": p["type"], "severity": spec[0], "passable_for": ["drone"],
                     "confidence": spec[1], "description": spec[2]}, "report_hazard")
        if cp == "CP-H2":
            z = ctx.get("hazard")
            if z is None:
                return ({"action": "proceed", "reason": "Route ahead clear.", "trust_report": True},
                        ctx.get("action", "proceed"))
            if ctx["_mock_truth_real"] and "rover" not in z["passable_for"]:
                return ({"action": "reroute", "trust_report": True,
                         "reason": f"{z['type']} ({z['status']}) on {self.w.edges[z['edge_id']].name}; "
                                   f"not passable for rovers — taking alternate route."}, "reroute")
            return ({"action": "proceed", "trust_report": False,
                     "reason": "Single unverified report, no corroborating evidence in frame; keeping route."},
                    "proceed")
        if cp == "CP-V":
            z = ctx["hazard"]
            if ctx["_mock_truth_real"]:
                return ({"verdict": "confirm", "observed_type": z["type"], "confidence": 0.93,
                         "reason": f"Aerial view shows {z['type'].replace('_', ' ')} on the reported edge."}, "confirm")
            return ({"verdict": "dispute", "observed_type": "none", "confidence": 0.94,
                     "reason": "No barriers, machinery or excavation visible; traffic flowing normally."}, "dispute")
        return ({"at_correct_location": True, "confidence": 0.96}, "handover")

    def _resolve_cp(self, r: Robot) -> None:
        p = r.pending
        r.pending = None
        r.seq += 1
        crid = f"{self._episode(r)}:{r.seq}"
        base = {"checkpoint": p["cp"], "robot_id": r.id, "client_request_id": crid,
                "latency_ms": rd(p["lat"], 1), "mock": True}
        thumb = self.thumbs.get(p["ctx"].get("thumb", "none"))
        if not self.brain_online:
            res = dict(base, ok=False, output=None, model=None, zg_request_id=None, zg_trace=None,
                       tee_verified=None, error="0G unreachable: connection refused")
            self.emit("decision", {"result": res, "operator": r.op, "sim_time": rd(self.t),
                                   "action_taken": "safe_stop", "frame_thumb_b64": thumb,
                                   "storage_root": None, "chain_tx": None})
            r.batch.append((r.seq, None))
            r.failed = p
            r.fsm = "SAFE_STOP"
            r.retry_at = None
            r.speed = 0.0
            r.note = "SAFE_STOP: 0G unreachable — holding position"
            return
        out, action = self._mock_output(p["cp"], p["ctx"])
        zg = "mock-" + self.hexs(24)
        self.tee_flip = not self.tee_flip
        tee = True if self.tee_flip else None
        res = dict(base, ok=True, output=out, model="MOCK", zg_request_id=zg,
                   zg_trace={"request_id": zg, "provider": "MOCK", "tee_verified": tee},
                   tee_verified=tee, error=None)
        self.emit("decision", {"result": res, "operator": r.op, "sim_time": rd(self.t), "action_taken": action,
                               "frame_thumb_b64": thumb, "storage_root": None, "chain_tx": None})
        r.batch.append((r.seq, zg))
        r.failed = None
        r.fsm = r.resume_fsm
        r.note = None
        if p["on_ok"]:
            p["on_ok"](r, out, zg)
        if len(r.batch) >= 3:
            self._flush_storage(r)

    # ------------------------------------------------------------ orders
    def _items(self) -> list[str]:
        return self.rng.sample(ITEMS, self.rng.randint(1, 4))

    def _new_order(self, op: str, bid: str, items: list[str], name: str) -> dict:
        self.n_order += 1
        o = {"id": f"O-{self.n_order:04d}", "operator": op, "customer_building_id": bid, "customer_name": name,
             "items": list(items), "status": "placed", "robot_id": None, "timestamps": {"placed": rd(self.t)}}
        self.orders[o["id"]] = o
        h = self.home_by_bid.get(bid)
        xy = h["entrance_xy"] if h else self.w.buildings[bid]["centroid"]
        self.order_target[o["id"]] = self.w.snap(xy)
        self.queue[op].append(o["id"])
        self.emit("order", dict(o, timestamps=dict(o["timestamps"])))
        return o

    def _order_status(self, oid: Optional[str], status: str, robot: Optional[str] = None) -> None:
        if not oid:
            return
        o = self.orders[oid]
        o["status"] = status
        o["timestamps"][status] = rd(self.t)
        if robot:
            o["robot_id"] = robot
        self.emit("order", dict(o, timestamps=dict(o["timestamps"])))

    def place_order(self, operator: str, building_id: str, items: list[str], customer_name: str) -> dict:  # EngineControl
        if operator not in self.queue or building_id not in self.w.buildings:
            from fastapi import HTTPException
            raise HTTPException(400, f"unknown operator or building: {operator!r} {building_id!r}")
        with self.lock:
            o = self._new_order(operator, building_id, items or self._items(), customer_name)
            self.log(f"[order] {o['id']} placed via API for {operator} -> {building_id}")
            return dict(o, timestamps=dict(o["timestamps"]))

    def _auto_order(self) -> None:
        op = OPERATORS[(self.n_order) % 3][0]
        h = self._pick_home(op)
        self._new_order(op, h["building_id"], self._items(), h["customer_name"])
        self.at(self.t + self.rng.uniform(13.0, 17.0), self._auto_order)

    def _assign(self, r: Robot, o: dict) -> None:
        self.queue[o["operator"]].remove(o["id"])
        r.order = o["id"]
        self._order_status(o["id"], "assigned", r.id)
        if r.fsm == "IDLE":
            self._start_loading(r)
        else:
            r.fsm = "TO_STORE"
            self._route(r, self.store_target[r.op])

    def snapshot_meta(self) -> dict:  # EngineControl
        return {"engine": True, "mock": True, "fake": True, "banner": BANNER, "run_id": self.run_id,
                "operators": [{"name": n, "color": c} for n, c, *_ in OPERATORS],
                "sim_time": rd(self.t), "brain_online": self.brain_online,
                "world": "/fixtures/world/",
                "pois": {"dark_stores": [s["id"] for s in self.w.pois["dark_stores"]], "homes": len(self.homes)}}

    # ------------------------------------------------------------ rover behaviour
    def _blocked(self, r: Robot) -> dict[str, float]:
        return {self.hazards[h]["edge_id"]: self.hz_s[h] for h in r.trusted
                if h in self.hazards and self.hazards[h]["status"] in ACTIVE}

    def _route(self, r: Robot, target: tuple[str, float], blocked: Optional[dict] = None) -> None:
        r.target = target
        legs = self.w.plan(r.m.edge, r.m.s, target[0], target[1], self._blocked(r) if blocked is None else blocked)
        if legs is None:
            legs = self.w.plan(r.m.edge, r.m.s, target[0], target[1], {})
        r.m.set_legs(legs)

    def _start_loading(self, r: Robot) -> None:
        r.fsm = "LOADING"
        r.timer = self.t + 5.0
        self._order_status(r.order, "picking")

    def _rover_arrived(self, r: Robot) -> None:
        r.speed = 0.0
        if r.fsm == "TO_STORE":
            self._start_loading(r)
        elif r.fsm == "DELIVERING":
            r.fsm = "AT_DOOR"
            r.timer = self.t + 2.0
            r.phase = "door"
            self._order_status(r.order, "at_door")
        elif r.fsm == "RETURNING":
            r.fsm = "IDLE"

    def _home_label(self, oid: Optional[str]) -> str:
        if not oid:
            return ""
        h = self.home_by_bid.get(self.orders[oid]["customer_building_id"])
        return f" -> {h['id'] if h else self.orders[oid]['customer_building_id']} ({oid})"

    def _rover_tick(self, r: Robot) -> None:
        dt, t = self.dt, self.t
        if r.fsm == "WAITING_FOR_BRAIN":
            r.speed = 0.0
            if t >= r.wait_until:
                self._resolve_cp(r)
        elif r.fsm == "SAFE_STOP":
            r.speed = 0.0
            r.pull = min(1.0, r.pull + dt)
            if r.retry_at is not None and t >= r.retry_at and self.brain_online and r.failed:
                f = r.failed
                r.retry_at = None
                self.request_cp(r, f["cp"], dict(f["ctx"], action="resume"), f["on_ok"], resume=r.resume_fsm)
        elif r.fsm == "IDLE":
            r.speed = 0.0
            r.battery = min(1.0, r.battery + 0.002 * dt)
            r.note = "idle at store"
        elif r.fsm == "LOADING":
            r.note = f"loading{self._home_label(r.order)}"
            if t >= r.timer:
                r.fsm = "DELIVERING"
                self._order_status(r.order, "out_for_delivery")
                self._route(r, self.order_target[r.order])
        elif r.fsm == "AT_DOOR":
            r.note = f"at door{self._home_label(r.order)}"
            if r.phase == "door" and t >= r.timer:
                r.phase = "cpd"
                self.request_cp(r, "CP-D", {"thumb": "door"}, self._on_cpd)
        elif r.fsm == "HANDED_OVER":
            if t >= r.timer:
                r.order = None
                if self.queue[r.op]:
                    self._assign_next(r)
                else:
                    r.fsm = "RETURNING"
                    self._route(r, self.store_target[r.op])
        elif r.fsm in MOVING:
            if r.pull > 0:
                r.pull = max(0.0, r.pull - dt)
            if self._rover_checks(r):
                r.speed = 0.0
            else:
                r.speed = min(ROVER_SPEED, r.speed + 2.0 * dt)
                r.battery = max(0.05, r.battery - 0.0005 * dt)
                r.note = {"TO_STORE": "to store", "DELIVERING": "delivering",
                          "RETURNING": "returning"}[r.fsm] + self._home_label(r.order)
                if r.m.advance(r.speed * dt):
                    self._rover_arrived(r)
        if r.m:
            r.x, r.y, r.yaw = r.m.pose(extra=r.pull * 1.0)

    def _on_cpd(self, r: Robot, out: dict, zg: str) -> None:
        r.fsm = "HANDED_OVER"
        r.timer = self.t + 3.0
        r.note = f"handed over{self._home_label(r.order)}"
        self._order_status(r.order, "delivered")

    def _assign_next(self, r: Robot) -> None:
        o = self.orders[self.queue[r.op][0]]
        r.fsm = "TO_STORE" if r.fsm != "IDLE" else "IDLE"
        self._assign(r, o)

    def _rover_checks(self, r: Robot) -> bool:
        """Returns True if a checkpoint was requested (rover stops this tick)."""
        if not self.brain_online:
            if r.probe_epoch != self.outage_epoch:
                r.probe_epoch = self.outage_epoch
                r.probe_at = self.t + self.rng.uniform(0.2, 3.0)
            if r.probe_at is not None and self.t >= r.probe_at:
                r.probe_at = None
                self.request_cp(r, "CP-H2", {"thumb": "none"}, None)
                return True
        # CP-H2: known hazard on my route that I have not evaluated
        for hid, z in self.hazards.items():
            if z["status"] not in ACTIVE or hid in r.evaluated:
                continue
            if r.m.hits(z["edge_id"], self.hz_s[hid]) is not None:
                pid = next((p for p, h in self.prop_hz.items() if h == hid), None)
                real = bool(pid and pid in self.props and self.props[pid]["real"])
                self.request_cp(r, "CP-H2", {"hazard": dict(z), "_mock_truth_real": real, "thumb": z["type"] if real else "none"},
                                lambda rr, out, zg, hid=hid: self._on_cph2(rr, hid, out))
                return True
        # CP-H1: physical prop within trigger distance, nobody has reported it yet
        for pid, p in self.props.items():
            if not p["real"]:
                continue
            hid = self.prop_hz.get(pid)
            if hid and self.hazards.get(hid, {}).get("status") in ACTIVE:
                continue
            d = r.m.hits(p["edge_id"], self.prop_s[pid])
            if d is not None and d <= CP_TRIGGER_M:
                self.request_cp(r, "CP-H1", {"prop": dict(p), "thumb": p["type"]},
                                lambda rr, out, zg, pid=pid: self._on_cph1(rr, pid, out, zg))
                return True
        return False

    def _on_cph1(self, r: Robot, pid: str, out: dict, zg: str) -> None:
        if pid not in self.props or not out.get("is_hazard"):
            return
        p = self.props[pid]
        root = self._flush_storage(r)
        hid = self._new_hazard(p["edge_id"], self.prop_s[pid], out["type"], out["severity"], r.op, r.id,
                               evidence_root=root, zg_request_id=zg, passable=list(out["passable_for"]), pid=pid)
        self.log(f"[hazard {hid}] {r.id} ({r.op}) reported {out['type']} on {self.w.edges[p['edge_id']].name}")
        r.evaluated.add(hid)
        r.trusted.add(hid)
        if r.target:
            self._route(r, r.target)
        verifier = next((d.id for d in self.drones() if d.op != r.op and not d.task), None) or \
            next(d.id for d in self.drones() if d.op != r.op)
        self._dispatch_drone(verifier, hid)

    def _on_cph2(self, r: Robot, hid: str, out: dict) -> None:
        r.evaluated.add(hid)
        if out.get("action") == "reroute" and out.get("trust_report"):
            r.trusted.add(hid)
            if r.target:
                self._route(r, r.target)
                self.log(f"[reroute] {r.id} ({r.op}) avoids hazard {hid}")

    def _rig_cph2(self, hid: str, per_op: int) -> None:
        """Make sure rovers of every operator meet the hazard: give the cheapest-detour moving rovers a
        route through the hazard edge (as if planned before it was known); the generic check then fires CP-H2."""
        z = self.hazards.get(hid)
        if not z or z["status"] not in ACTIVE:
            return
        eid = z["edge_id"]
        E = self.w.edges[eid]
        for op, *_ in OPERATORS:
            have = [r for r in self.rovers() if r.op == op and hid not in r.evaluated and r.fsm in MOVING
                    and r.m.hits(eid, self.hz_s[hid]) is not None]
            need = per_op - len(have)
            opts = []
            for r in self.rovers():
                if r.op != op or r.fsm not in MOVING or hid in r.evaluated or r in have or not r.target:
                    continue
                base = r.m.remaining_len()
                for sa, sb in ((0.0, E.L), (E.L, 0.0)):
                    p1 = self.w.plan(r.m.edge, r.m.s, eid, sa, {})
                    p2 = self.w.plan(eid, sb, r.target[0], r.target[1], {})
                    if not p1 or not p2:
                        continue
                    legs = [lg for lg in p1 + [(eid, sa, sb)] + p2 if abs(lg[2] - lg[1]) > 0.01]
                    cost = sum(abs(b - a) for _, a, b in legs)
                    if cost - base < 2500:
                        opts.append((cost - base, r.id, legs))
            used = set()
            for detour, rid, legs in sorted(opts):
                if need <= 0:
                    break
                if rid in used:
                    continue
                used.add(rid)
                self.by_id[rid].m.set_legs(legs)
                need -= 1

    # ------------------------------------------------------------ drones
    def _dispatch_drone(self, did: str, hid: str) -> None:
        d = self.by_id[did]
        if d.task or d.fsm in ("WAITING_FOR_BRAIN", "SAFE_STOP"):
            d.queue.append(hid)
            return
        self._start_verify(d, hid)

    def _start_verify(self, d: Robot, hid: str) -> None:
        z = self.hazards[hid]
        E = self.w.edges[z["edge_id"]]
        x, y, _ = E.at(self.hz_s[hid])
        d.task = hid
        d.fsm = "VERIFYING"
        d.phase = "fly"
        d.wp = (x, y, CRUISE_Z)
        d.note = f"verifying hazard {hid} ({z['type']})"
        self.log(f"[verify] {d.id} ({d.op}) dispatched to hazard {hid}")

    def _drone_move(self, d: Robot, tgt: tuple[float, float, float], vmax: float = DRONE_SPEED) -> bool:
        dx, dy, dz = tgt[0] - d.x, tgt[1] - d.y, tgt[2] - d.z
        hd = math.hypot(dx, dy)
        step_h = vmax * self.dt
        step_v = 4.0 * self.dt
        if hd > 0.05:
            k = min(1.0, step_h / hd)
            d.x += dx * k
            d.y += dy * k
            d.yaw = math.atan2(dy, dx)
            d.speed = min(vmax, hd / self.dt)
        else:
            d.speed = 0.0
        d.z += max(-step_v, min(step_v, dz))
        return hd <= 0.05 and abs(tgt[2] - d.z) < 0.05

    def _drone_tick(self, d: Robot) -> None:
        t = self.t
        airborne = d.z > d.home[2] + 0.5
        if d.fsm == "WAITING_FOR_BRAIN":
            d.speed = 0.0
            if t >= d.wait_until:
                self._resolve_cp(d)
            return
        if d.fsm == "SAFE_STOP":
            d.speed = 0.0
            if d.retry_at is not None and t >= d.retry_at and self.brain_online and d.failed:
                f = d.failed
                d.retry_at = None
                self.request_cp(d, f["cp"], dict(f["ctx"], action="resume"), f["on_ok"], resume=d.resume_fsm)
            return
        if airborne:
            d.battery = max(0.05, d.battery - 0.0012 * self.dt)
            if not self.brain_online:
                if d.probe_epoch != self.outage_epoch:
                    d.probe_epoch = self.outage_epoch
                    d.probe_at = t + self.rng.uniform(0.2, 3.0)
                if d.probe_at is not None and t >= d.probe_at:
                    d.probe_at = None
                    self.request_cp(d, "CP-H2", {"thumb": "none"}, None)
                    return
        if d.fsm == "VERIFYING":
            hid = d.task
            if d.phase == "fly":
                if self._drone_move(d, d.wp):
                    d.phase = "hover"
                    d.timer = t + 2.0
            elif d.phase == "hover":
                d.speed = 0.0
                if t >= d.timer:
                    d.phase = "cpv"
                    z = self.hazards[hid]
                    pid = next((p for p, h in self.prop_hz.items() if h == hid), None)
                    real = bool(pid and pid in self.props and self.props[pid]["real"])
                    self.request_cp(d, "CP-V", {"hazard": dict(z), "_mock_truth_real": real,
                                                "thumb": z["type"] if real else "none"},
                                    lambda dd, out, zg, hid=hid: self._on_cpv(dd, hid, out))
        elif d.fsm == "LANDING":
            hx, hy, hz = d.home
            if d.phase == "fly":
                if self._drone_move(d, (hx, hy, CRUISE_Z)):
                    d.phase = "descend"
            elif self._drone_move(d, d.home):
                d.fsm = "IDLE"
                d.phase = "parked"
                d.timer = t + 6.0
                d.note = "parked on store roof"
        else:  # IDLE
            while d.queue:
                nxt = d.queue.pop(0)
                if self.hazards.get(nxt, {}).get("status") in ACTIVE:
                    self._start_verify(d, nxt)
                    return
            if d.phase == "parked":
                d.speed = 0.0
                d.battery = min(1.0, d.battery + 0.004 * self.dt)
                if t >= d.timer:
                    d.phase = "takeoff"
            elif d.phase == "takeoff":
                d.note = "taking off"
                if self._drone_move(d, (d.x, d.y, CRUISE_Z)):
                    d.phase = "patrol"
                    d.wp = None
            else:
                d.note = "patrol"
                hx, hy, _ = d.home
                if d.wp is None:
                    d.wp = (max(-600, min(600, hx + self.rng.uniform(-350, 350))),
                            max(-600, min(600, hy + self.rng.uniform(-350, 350))), CRUISE_Z)
                if self._drone_move(d, d.wp):
                    d.wp = None

    def _on_cpv(self, d: Robot, hid: str, out: dict) -> None:
        z = self.hazards.get(hid)
        if z and z["status"] in ACTIVE:
            if out["verdict"] == "confirm":
                z["status"] = "confirmed"
                z["confirms"] += 1
                self.emit("hazard", dict(z))
                self._chain("verifyHazard", d.op, hid)
                self.log(f"[hazard {hid}] confirmed by {d.id} ({d.op})")
                per_op = 2 if z["type"] == "accident" else 1
                self.at(self.t + 1.0, lambda: self._rig_cph2(hid, per_op))
            else:
                z["status"] = "rejected"
                z["disputes"] += 1
                self.emit("hazard", dict(z))
                self._chain("verifyHazard", d.op, hid)
                pid = next((p for p, h in self.prop_hz.items() if h == hid), None)
                self._remove_prop(pid)
                self.log(f"[hazard {hid}] DISPUTED by {d.id} ({d.op}) -> rejected; "
                         f"{z['reporter_operator']} stake slashed (mock)", "warn")
        d.task = None
        while d.queue:
            nxt = d.queue.pop(0)
            if self.hazards.get(nxt, {}).get("status") in ACTIVE:
                d.fsm = "IDLE"
                self._start_verify(d, nxt)
                return
        d.fsm = "LANDING"
        d.phase = "fly"
        d.note = "returning to store"

    # ------------------------------------------------------------ actors
    def _actor_next(self, a: Actor, node: str, came: str) -> tuple[str, float, float]:
        kind = "walk" if self.w.edges[came].kind == "walk" else "drive"
        opts = self.w.adj[kind].get(node, [])
        prop_edges = {p["edge_id"] for p in self.props.values() if p["real"]}
        h_in = self.w.edges[came].at(self.w.edges[came].L if self.w.edges[came].b == node else 0.0)[2]
        if self.w.edges[came].a == node:
            h_in += math.pi
        cands = []
        for eid, other, fwd in opts:
            e = self.w.edges[eid]
            if eid == came or eid in prop_edges:
                continue
            if a.kind != "pedestrian" and e.oneway and not fwd:
                continue
            if a.kind == "bus" and not e.main:
                continue
            h_out = e.at(0.0 if fwd else e.L)[2] + (0 if fwd else math.pi)
            cands.append((1.25 + math.cos(wrap(h_out - h_in)), eid, fwd))
        if not cands:
            e = self.w.edges[came]
            return (came, e.L, 0.0) if e.b == node else (came, 0.0, e.L)
        tot = sum(c[0] for c in cands)
        x = self.rng.uniform(0, tot)
        for wgt, eid, fwd in cands:
            x -= wgt
            if x <= 0:
                break
        e = self.w.edges[eid]
        return (eid, 0.0, e.L) if fwd else (eid, e.L, 0.0)

    def _actor_tick(self, a: Actor) -> None:
        m = a.m
        a.speed = a.base * (0.85 + 0.15 * math.sin(self.t * 0.35 + a.phase))
        if a.kind != "pedestrian":
            e, s0, s1 = m.legs[m.i]
            for pid, p in self.props.items():
                if p["real"] and p["edge_id"] == e:
                    ps = self.prop_s[pid]
                    if (s1 >= s0 and m.s < ps < m.s + 25) or (s1 < s0 and m.s - 25 < ps < m.s):
                        # U-turn: drive back to where we entered, then leave by another edge
                        E = self.w.edges[e]
                        entry = E.a if s1 >= s0 else E.b
                        m.legs[m.i] = (e, m.s, s0)
                        del m.legs[m.i + 1:]
                        m.legs.append(self._actor_next(a, entry, e))
                        break
        if m.i >= len(m.legs) - 1:
            e, s0, s1 = m.legs[-1]
            E = self.w.edges[e]
            node = E.b if s1 >= s0 else E.a
            m.legs.append(self._actor_next(a, node, e))
            if m.i > 4:
                del m.legs[:m.i]
                m.i = 0
        m.advance(a.speed * self.dt)

    # ------------------------------------------------------------ main loop
    def step(self) -> None:
        with self.lock:
            self.tick_n += 1
            self.t = self.tick_n * self.dt
            while self.events and self.events[0][0] <= self.t + 1e-9:
                _, _, fn = heapq.heappop(self.events)
                fn()
            for op in self.queue:
                while self.queue[op]:
                    free = [r for r in self.rovers() if r.op == op and r.fsm in ("IDLE", "RETURNING") and not r.order]
                    if not free:
                        break
                    free.sort(key=lambda r: (r.fsm != "IDLE", r.m.remaining_len()))
                    self._assign(free[0], self.orders[self.queue[op][0]])
            for r in self.robots:
                if r.kind == "rover":
                    self._rover_tick(r)
                else:
                    self._drone_tick(r)
            for a in self.actors:
                self._actor_tick(a)
            if self.tick_n % self.state_every == 0:
                self.emit("state", self.snapshot())

    def snapshot(self) -> dict:
        acts = []
        for a in self.actors:
            x, y, yaw = a.m.pose()
            acts.append({"id": a.id, "kind": a.kind, "x": rd(x), "y": rd(y), "z": 0.0, "yaw": rd(yaw),
                         "speed": rd(a.speed, 1)})
        return {"sim_time": rd(self.t), "robots": [r.as_dict() for r in self.robots], "actors": acts,
                "hazards": [dict(z) for z in self.hazards.values()], "props": [dict(p) for p in self.props.values()],
                "brain_online": self.brain_online}


# ======================================================================== modes
def refuse_demo() -> None:
    if (os.environ.get("DEMO") or "0").lower() in ("1", "true", "yes"):
        sys.exit(f"{BANNER}: refusing to run with DEMO=1 (dev-only mock).")


def run_record(out: Path, duration: float, seed: int) -> None:
    out = out.resolve()
    public = (ROOT / "viewer" / "public").resolve()
    if out == public or public in out.parents:
        sys.exit("refusing to write a fake stream into viewer/public")
    out.parent.mkdir(parents=True, exist_ok=True)
    f = open(out, "w")

    def sink(topic: str, t: float, data: dict) -> None:
        f.write(json.dumps({"topic": topic, "t": t, "data": data}, separators=(",", ":"), ensure_ascii=False) + "\n")

    eng = FakeEngine(sink, seed=seed, state_hz=5.0, live=False)
    eng.log(f"{BANNER} (recorded fixture, seed={seed})", "warn")
    t0 = time.time()
    n = int(round(duration / eng.dt))
    for _ in range(n):
        eng.step()
    f.close()
    print(f"[record] {duration:.0f} s sim in {time.time() - t0:.1f} s wall -> {out} "
          f"({out.stat().st_size / 1e6:.2f} MB)")


def run_live(port: int, seed: int) -> None:
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from fastapi.staticfiles import StaticFiles
    from server.app import app, attach_engine
    from server.bus import BUS

    eng = FakeEngine(lambda topic, t, data: BUS.publish(topic, t, data), seed=seed, state_hz=10.0, live=True)
    attach_engine(eng)
    app.mount("/fixtures/world", StaticFiles(directory=str(WORLD)), name="fixtures-world")

    def loop() -> None:
        eng.log(f"{BANNER} (live)", "warn")
        nxt = time.monotonic()
        while True:
            try:
                eng.step()
            except Exception as e:  # keep the dev server alive
                print(f"[fake] sim error: {e!r}", flush=True)
                import traceback
                traceback.print_exc()
            nxt += eng.dt
            time.sleep(max(0.0, nxt - time.monotonic()))

    threading.Thread(target=loop, daemon=True, name="fake-sim").start()
    print(f"[live] {BANNER}: ws://127.0.0.1:{port}/ws  http://127.0.0.1:{port}/api/meta  "
          f"world: http://127.0.0.1:{port}/fixtures/world/roads.json", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run_verify(path: Path) -> int:
    sys.path.insert(0, str(ROOT))
    try:
        from sim import schemas as S
        models = {"state": S.StateSnapshot, "decision": S.DecisionEvent, "hazard": S.Hazard, "order": S.Order,
                  "chain_tx": S.ChainTxEvent, "storage": S.StorageEvent}
        env_model = S.Envelope
    except Exception as e:  # pragma: no cover
        print(f"[verify] sim.schemas not importable ({e!r}); JSON-only check")
        models, env_model = {}, None
    counts: dict[str, int] = {}
    bad = 0
    tmin, tmax, last_t = 1e18, -1e18, -1e18
    nonmono = 0
    actors_n = set()
    acts: dict[str, int] = {}
    hz_status: dict[str, list[str]] = {}
    with open(path) as f:
        for i, line in enumerate(f, 1):
            try:
                env = json.loads(line)
                if env_model:
                    env_model.model_validate(env)
                if env["topic"] in models:
                    models[env["topic"]].model_validate(env["data"])
            except Exception as e:
                bad += 1
                if bad < 5:
                    print(f"[verify] line {i}: {e!r}"[:300])
                continue
            tp = env["topic"]
            counts[tp] = counts.get(tp, 0) + 1
            t = env["t"]
            tmin, tmax = min(tmin, t), max(tmax, t)
            if t < last_t - 1e-9:
                nonmono += 1
            last_t = t
            d = env["data"]
            if tp == "state":
                actors_n.add(len(d["actors"]))
            elif tp == "decision":
                k = f"{d['result']['checkpoint']}:{d['action_taken']}:{'ok' if d['result']['ok'] else 'FAIL'}"
                acts[k] = acts.get(k, 0) + 1
            elif tp == "hazard":
                hz_status.setdefault(f"{d['id']}:{d['type']}", []).append(f"{d['status']}@{t}")
    print(f"[verify] {path} {path.stat().st_size / 1e6:.2f} MB, t={tmin}..{tmax}, invalid={bad}, "
          f"non-monotonic t={nonmono}")
    print(f"[verify] topics: {json.dumps(counts)}")
    print(f"[verify] actors per state: {sorted(actors_n)}")
    print(f"[verify] decisions: {json.dumps(dict(sorted(acts.items())))}")
    for k, v in hz_status.items():
        print(f"[verify] hazard {k}: {' -> '.join(v)}")
    return 1 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("--port", type=int, default=8765)
    lv.add_argument("--seed", type=int, default=7)
    rc = sub.add_parser("record")
    rc.add_argument("--out", default=str(HERE / "fixtures" / "stream.jsonl"))
    rc.add_argument("--duration", type=float, default=240.0)
    rc.add_argument("--seed", type=int, default=7)
    vf = sub.add_parser("verify")
    vf.add_argument("--in", dest="inp", default=str(HERE / "fixtures" / "stream.jsonl"))
    a = ap.parse_args()
    if a.mode == "verify":
        sys.exit(run_verify(Path(a.inp)))
    refuse_demo()
    banner()
    if not (WORLD / "road_graph.json").exists():
        sys.exit(f"missing {WORLD}; run viewer/dev/gen_fixture.py first")
    if a.mode == "record":
        run_record(Path(a.out), a.duration, a.seed)
    else:
        run_live(a.port, a.seed)


if __name__ == "__main__":
    main()
