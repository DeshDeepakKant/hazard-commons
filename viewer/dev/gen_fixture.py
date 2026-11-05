#!/usr/bin/env python3
"""DEV-ONLY synthetic world fixture for the viewer (WS7). NOT real OSM data.

Writes a deterministic "Bandra-ish" layout (~1.4 x 1.4 km, centred on the local-frame origin)
into viewer/dev/fixtures/world/ in exactly the docs/INTERFACES.md formats:
    meta.json, roads.json, road_graph.json, buildings.json, pois.json

Local frame: metres, x = east, y = north. Stdlib only.

    .venv/bin/python viewer/dev/gen_fixture.py [--out viewer/dev/fixtures/world] [--seed 1729]
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORIGIN_LONLAT = (72.834, 19.059)
# EPSG:32643 projection of ORIGIN_LONLAT (pyproj, 2026-11-06); recomputed if pyproj is importable.
ORIGIN_UTM = (272066.1955488995, 2108763.0331085664)
HALF = 700.0
SIDE_W = 6.0
WALK_W = 3.0
SETBACK = 2.5


# ---------------------------------------------------------------- geometry helpers
def warp(u: float, v: float) -> tuple[float, float]:
    """Smooth global warp: makes the uv grid irregular and every road gently curved."""
    x = u + 14.0 * math.sin(v / 230.0 + 0.6) + 6.0 * math.sin(v / 97.0 + 1.9)
    y = v + 12.0 * math.sin(u / 270.0 + 1.2) + 5.0 * math.sin(u / 113.0 + 0.4)
    return x, y


def r1(a: float) -> float:
    return round(a, 1)


def poly_len(pts: list[list[float]]) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def poly_area_centroid(pts: list[tuple[float, float]]) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    a *= 0.5
    if abs(a) < 1e-9:
        return 0.0, sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    return a, cx / (6 * a), cy / (6 * a)


def seg_dist(p, a, b) -> tuple[float, float]:
    """distance from p to segment ab, and param t in [0,1]."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)), t


# ---------------------------------------------------------------- street layout
COLS = [  # north-south lines: (u, name, main?, lanes, width)
    (-690, "Chimbai Road", False, 2, SIDE_W),
    (-560, "Waroda Road", False, 2, SIDE_W),
    (-430, "Pali Mala Road", False, 2, SIDE_W),
    (-285, "Hill Road", True, 4, 13.0),
    (-150, "St. John Baptist Road", False, 2, SIDE_W),
    (-20, "Chapel Road", False, 2, SIDE_W),
    (115, "Perry Road", False, 2, SIDE_W),
    (255, "Linking Road", True, 4, 14.0),
    (390, "Waterfield Road", False, 2, SIDE_W),
    (515, "Khar Danda Road", False, 2, SIDE_W),
    (650, "SV Road", True, 3, 12.0),
]
ROWS = [  # east-west lines
    (-690, "Bazar Road", False, 2, SIDE_W),
    (-560, "Mount Mary Road", False, 2, SIDE_W),
    (-425, "St. Paul Road", False, 2, SIDE_W),
    (-290, "Pali Hill Road", False, 2, SIDE_W),
    (-155, "Turner Road", True, 3, 11.0),
    (-25, "Union Park Road", False, 2, SIDE_W),
    (105, "Nargis Dutt Road", False, 2, SIDE_W),
    (235, "Dr Peter Dias Road", False, 2, SIDE_W),
    (370, "16th Road", False, 2, SIDE_W),
    (505, "Ambedkar Road", False, 2, SIDE_W),
    (640, "Carter Road", True, 2, 10.0),
]


class Line:
    def __init__(self, idx, base, name, main, lanes, width, vertical):
        self.idx, self.base, self.name, self.main = idx, base, name, main
        self.lanes, self.width, self.vertical = lanes, width, vertical
        self.span = (0, 10)          # index range along the other family
        self.oneway = False
        self.wob_amp = 0.0
        self.wob_k = 150.0
        self.wob_ph = 0.0

    def coord(self, along: float) -> float:
        """for vertical lines: u as a function of v (gentle extra wobble)."""
        return self.base + self.wob_amp * math.sin(along / self.wob_k + self.wob_ph)


def build_lines(rng: random.Random):
    cols = [Line(i, *c, vertical=True) for i, c in enumerate(COLS)]
    rows = [Line(i, *r, vertical=False) for i, r in enumerate(ROWS)]
    for ln in cols + rows:
        if not ln.main and 0 < ln.idx < 10:
            ln.base += rng.uniform(-12, 12)
            ln.oneway = rng.random() < 0.3
            if ln.oneway:
                ln.lanes = 1
    # gently curved main roads (columns get extra wobble along v)
    for ln in cols:
        if ln.main:
            ln.wob_amp, ln.wob_k, ln.wob_ph = rng.uniform(7, 11), rng.uniform(140, 200), rng.uniform(0, 6)
    # a few side streets stop short (T-junctions); endpoints land on full-span lines
    def full_idx(lines):
        return [l.idx for l in lines if l.main or l.idx in (0, 10)]
    for lines, others in ((cols, rows), (rows, cols)):
        cands = [l for l in lines if not l.main and 0 < l.idx < 10]
        for ln in rng.sample(cands, 3):
            k = rng.choice([i for i in full_idx(others) if 2 <= i <= 8])
            ln.span = (0, k) if rng.random() < 0.5 else (k, 10)
    return cols, rows


def generate(seed: int):
    rng = random.Random(seed)
    cols, rows = build_lines(rng)
    for ln in cols + rows:  # full-span partners must stay full span
        if ln.main or ln.idx in (0, 10):
            ln.span = (0, 10)

    def exists(c: int, r: int) -> bool:
        return cols[c].span[0] <= r <= cols[c].span[1] and rows[r].span[0] <= c <= rows[r].span[1]

    def node_uv(c: int, r: int) -> tuple[float, float]:
        v = rows[r].base
        return cols[c].coord(v), v

    nodes: list[dict] = []
    node_id: dict[tuple, str] = {}

    def add_node(key, x, y) -> str:
        nid = f"n{len(nodes) + 1}"
        nodes.append({"id": nid, "x": r1(x), "y": r1(y)})
        node_id[key] = nid
        return nid

    for r in range(11):
        for c in range(11):
            if exists(c, r):
                x, y = warp(*node_uv(c, r))
                add_node(("x", c, r), x, y)

    edges: list[dict] = []

    def add_edge(a: str, b: str, shape, lanes, name, kind, oneway, width, line=None):
        eid = f"e{len(edges) + 1}"
        shp = [[r1(x), r1(y)] for x, y in shape]
        edges.append({"id": eid, "from": a, "to": b, "length": r1(poly_len(shp)), "lanes": lanes,
                      "name": name, "kind": kind, "oneway": oneway, "shape": shp,
                      "sumo_edge_id": None, "_width": width, "_line": line})
        return eid

    def sample(u0, v0, u1, v1, fn=None, step=20.0):
        L = math.hypot(u1 - u0, v1 - v0)
        n = max(2, int(math.ceil(L / step)))
        pts = []
        for i in range(n + 1):
            t = i / n
            u, v = u0 + (u1 - u0) * t, v0 + (v1 - v0) * t
            if fn is not None:
                u = fn(v)
            pts.append(warp(u, v))
        return pts

    for ln in cols:
        rs = [r for r in range(11) if exists(ln.idx, r)]
        for ra, rb in zip(rs, rs[1:]):
            v0, v1 = rows[ra].base, rows[rb].base
            add_edge(node_id[("x", ln.idx, ra)], node_id[("x", ln.idx, rb)],
                     sample(0, v0, 0, v1, fn=ln.coord), ln.lanes, ln.name, "drive", ln.oneway,
                     ln.width, ("c", ln.idx))
    for ln in rows:
        cs = [c for c in range(11) if exists(c, ln.idx)]
        for ca, cb in zip(cs, cs[1:]):
            ua, ub = node_uv(ca, ln.idx)[0], node_uv(cb, ln.idx)[0]
            add_edge(node_id[("x", ca, ln.idx)], node_id[("x", cb, ln.idx)],
                     sample(ua, ln.base, ub, ln.base), ln.lanes, ln.name, "drive", ln.oneway,
                     ln.width, ("r", ln.idx))

    # ---------------- walk edges: Carter Road promenade + a few mid-block lanes
    carter = rows[10]
    pv = carter.base + carter.width / 2 + 7.0
    prom_u = [cols[c].base for c in range(11)]
    prev = None
    for i, u in enumerate(prom_u):
        nid = add_node(("p", i), *warp(u, pv))
        if prev is not None:
            add_edge(prev[0], nid, sample(prev[1], pv, u, pv), 1, "Carter Road Promenade", "walk",
                     False, WALK_W)
        prev = (nid, u)

    walk_cells: dict[tuple[int, int], float] = {}
    cell_list = [(c, r) for c in range(10) for r in range(10)]
    for (c, r) in rng.sample(cell_list, 7):
        u_mid = (cols[c].base + cols[c + 1].base) / 2 + rng.uniform(-10, 10)
        vb = rows[r].base + rows[r].width / 2 + 0.5
        vt = rows[r + 1].base - rows[r + 1].width / 2 - 0.5
        a = add_node(("w", c, r, 0), *warp(u_mid, vb))
        b = add_node(("w", c, r, 1), *warp(u_mid, vt))
        add_edge(a, b, sample(u_mid, vb, u_mid, vt, step=15.0), 1, "Lane", "walk", False, WALK_W)
        walk_cells[(c, r)] = u_mid

    # ---------------- road corridor index (for building rejection)
    GRID = 40.0
    seg_index: dict[tuple[int, int], list] = {}
    for e in edges:
        hw = e["_width"] / 2
        s = e["shape"]
        for i in range(len(s) - 1):
            a, b = s[i], s[i + 1]
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for gx in range(int(math.floor((x0 - 20) / GRID)), int(math.floor((x1 + 20) / GRID)) + 1):
                for gy in range(int(math.floor((y0 - 20) / GRID)), int(math.floor((y1 + 20) / GRID)) + 1):
                    seg_index.setdefault((gx, gy), []).append((a, b, hw, e))

    def clear_of_roads(pts) -> bool:
        samples = list(pts)
        n = len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            L = math.dist(a, b)
            k = max(1, int(L / 3.0))
            for j in range(1, k):
                t = j / k
                samples.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        _, cx, cy = poly_area_centroid(pts)
        samples.append((cx, cy))
        for p in samples:
            for (a, b, hw, _e) in seg_index.get((int(math.floor(p[0] / GRID)), int(math.floor(p[1] / GRID))), []):
                if seg_dist(p, a, b)[0] < hw + 1.5:
                    return False
        return True

    def nearest_drive(p):
        best = (1e18, None, None)
        for e in edges:
            if e["kind"] != "drive":
                continue
            s = e["shape"]
            for i in range(len(s) - 1):
                d, t = seg_dist(p, s[i], s[i + 1])
                if d < best[0]:
                    q = (s[i][0] + (s[i + 1][0] - s[i][0]) * t, s[i][1] + (s[i + 1][1] - s[i][1]) * t)
                    best = (d, e, q)
        return best

    # ---------------- buildings
    buildings: list[dict] = []
    parks = set(rng.sample([cl for cl in cell_list if cl not in walk_cells], 2))
    bnames_a = ["Sea Breeze", "Pali Heights", "Silver Oak", "Shanti Kunj", "Rizvi Mahal", "Mount View",
                "Sagar Darshan", "Gulmohar", "Rose Villa", "Bandstand View", "Krishna Niwas", "Sunflower",
                "Palm Grove", "Ocean Pearl", "Saint Anne", "Neelkanth", "Jasmine", "Hill Crest",
                "Carmel", "Ashiana", "Bella Vista", "Laxmi Bhuvan", "Al Noor", "Green Acres"]
    bnames_b = ["CHS", "Apartments", "Towers", "Co-op Housing Society", "Residency", "Villa", "Court"]
    cnames = ["Linking Point", "Bandra Bazaar", "Hill Road Arcade", "Chapel Market", "Turner House",
              "Carter Plaza", "SV Commercial Centre", "Perry Cross Shops", "Waterfield Hub",
              "Union Park Studios", "Ambedkar Chambers", "Pali Village Cafe Block"]
    onames = ["St. Andrew's School", "Bandra Gymkhana", "Municipal Dispensary", "Ganesh Mandir",
              "Mount Carmel Church Hall", "BMC Ward Office", "Jama Masjid Annex", "Community Hall"]

    def cell_side_inset(line: Line, cell_lo: int, cell_hi: int, idx_other_family_ok) -> float:
        # road present along this cell side?
        if line.span[0] <= cell_lo and cell_hi <= line.span[1]:
            return line.width / 2 + SETBACK + line.wob_amp
        return 2.0

    def split(rect, amax):
        u0, v0, u1, v1 = rect
        w, h = u1 - u0, v1 - v0
        if w * h <= amax or (w < 18 and h < 18):
            return [rect]
        f = rng.uniform(0.38, 0.62)
        if w >= h:
            m = u0 + w * f
            return split((u0, v0, m, v1), amax) + split((m, v0, u1, v1), amax)
        m = v0 + h * f
        return split((u0, v0, u1, m), amax) + split((u0, m, u1, v1), amax)

    def rect_poly(u0, v0, u1, v1):
        return [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]

    for (c, r) in cell_list:
        if (c, r) in parks:
            continue
        cl, cr, rb, rt = cols[c], cols[c + 1], rows[r], rows[r + 1]
        u0 = cl.base + cell_side_inset(cl, r, r + 1, None)
        u1 = cr.base - cell_side_inset(cr, r, r + 1, None)
        v0 = rb.base + cell_side_inset(rb, c, c + 1, None)
        v1 = rt.base - cell_side_inset(rt, c, c + 1, None)
        if u1 - u0 < 10 or v1 - v0 < 10:
            continue
        main_adj = any(l.main for l in (cl, cr, rb, rt))
        rects = [(u0, v0, u1, v1)]
        if (c, r) in walk_cells:
            um = walk_cells[(c, r)]
            rects = [(u0, v0, um - WALK_W / 2 - 1.5, v1), (um + WALK_W / 2 + 1.5, v0, u1, v1)]
        lots = []
        for rc in rects:
            lots += split(rc, rng.uniform(650, 1450))
        for (a0, b0, a1, b1) in lots:
            if rng.random() < 0.06:
                continue  # open plot / compound
            m = rng.uniform(1.0, 2.5)
            a0, b0, a1, b1 = a0 + m, b0 + m, a1 - m, b1 - m
            W, H = a1 - a0, b1 - b0
            if W < 5 or H < 5:
                continue
            cu, cv = (a0 + a1) / 2, (b0 + b1) / 2
            roll = rng.random()
            if roll < 0.58 or W < 12 or H < 12:
                poly_uv = rect_poly(a0, b0, a1, b1)
            elif roll < 0.85:
                th = math.radians(rng.uniform(3, 14)) * rng.choice((-1, 1))
                ct, st = abs(math.cos(th)), abs(math.sin(th))
                w, h = W * 0.9, H * 0.9
                for _ in range(30):
                    if w * ct + h * st <= W and w * st + h * ct <= H:
                        break
                    w *= 0.95
                    h *= 0.95
                poly_uv = []
                for (du, dv) in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
                    poly_uv.append((cu + du * math.cos(th) - dv * math.sin(th),
                                    cv + du * math.sin(th) + dv * math.cos(th)))
            else:  # L-shape: cut a corner
                fu, fv = rng.uniform(0.35, 0.55), rng.uniform(0.35, 0.55)
                k = rng.randrange(4)
                cut_u, cut_v = a0 + W * (1 - fu), b0 + H * (1 - fv)
                if k == 0:   # cut NE
                    poly_uv = [(a0, b0), (a1, b0), (a1, cut_v), (cut_u, cut_v), (cut_u, b1), (a0, b1)]
                elif k == 1:  # cut NW
                    cu2 = a0 + W * fu
                    poly_uv = [(a0, b0), (a1, b0), (a1, b1), (cu2, b1), (cu2, cut_v), (a0, cut_v)]
                elif k == 2:  # cut SW
                    cu2, cv2 = a0 + W * fu, b0 + H * fv
                    poly_uv = [(cu2, b0), (a1, b0), (a1, b1), (a0, b1), (a0, cv2), (cu2, cv2)]
                else:        # cut SE
                    cv2 = b0 + H * fv
                    poly_uv = [(a0, b0), (cut_u, b0), (cut_u, cv2), (a1, cv2), (a1, b1), (a0, b1)]
            pts = [warp(u, v) for u, v in poly_uv]
            pts = [(r1(x), r1(y)) for x, y in pts]
            if not clear_of_roads(pts):
                continue
            area, cx, cy = poly_area_centroid(pts)
            if area <= 0:
                pts = pts[::-1]
                area, cx, cy = poly_area_centroid(pts)
            # nearest cell side in uv -> fronting road name (for addresses)
            sides = [(cu - cl.base, cl), (cr.base - cu, cr), (cv - rb.base, rb), (rt.base - cv, rt)]
            sides = [s for s in sides if s[1].width > 0]
            front = min(sides, key=lambda s: s[0])[1]
            # use + height
            x = rng.random()
            front_main = front.main and min(sides, key=lambda s: s[0])[0] < 40
            if x < 0.07:
                use = "other"
            elif front_main and x < 0.7:
                use = "commercial"
            elif x < 0.16:
                use = "commercial"
            else:
                use = "residential"
            if use == "residential" and rng.random() < (0.07 if main_adj else 0.025) and area > 250:
                height = rng.uniform(36, 60)   # a few towers
            elif use == "residential":
                floors = rng.choices([2, 3, 4, 5, 6, 7, 8, 10], [8, 14, 18, 16, 12, 8, 6, 3])[0]
                height = floors * 3.2 + rng.uniform(0, 1.6)
            elif use == "commercial":
                height = rng.choice([2, 3, 3, 4, 5, 6]) * 3.6 + rng.uniform(0, 1.0)
            else:
                height = rng.uniform(6, 14)
            height = max(6.0, min(60.0, height))
            name = None
            if use == "residential" and (height > 30 or rng.random() < 0.12):
                name = f"{rng.choice(bnames_a)} {rng.choice(bnames_b)}"
            elif use == "commercial" and rng.random() < 0.15:
                name = rng.choice(cnames)
            elif use == "other" and rng.random() < 0.5:
                name = rng.choice(onames)
            address = None
            if rng.random() < 0.35:
                address = f"{rng.randint(1, 140)}, {front.name}, Bandra West, Mumbai 400050"
            bid = f"b{len(buildings) + 1}"
            buildings.append({"id": bid, "osm_id": None, "use": use, "height": round(height, 1),
                              "height_source": "pseudo", "centroid": [r1(cx), r1(cy)],
                              "footprint": [[x_, y_] for x_, y_ in pts], "name": name,
                              "address": address, "_front": front.name, "_area": area})

    # ---------------- POIs
    bykey = {b["id"]: b for b in buildings}

    def entrance(b):
        d, e, q = nearest_drive(b["centroid"])
        # put the entrance on the carriageway, 1 m inside the curb nearest the building
        hw = e["_width"] / 2
        vx, vy = b["centroid"][0] - q[0], b["centroid"][1] - q[1]
        L = math.hypot(vx, vy) or 1.0
        off = max(0.0, hw - 1.0)
        ex, ey = q[0] + vx / L * off, q[1] + vy / L * off
        na = next(n for n in nodes if n["id"] == e["from"])
        nb = next(n for n in nodes if n["id"] == e["to"])
        en = na if math.dist((na["x"], na["y"]), q) <= math.dist((nb["x"], nb["y"]), q) else nb
        return e, en["id"], [r1(ex), r1(ey)]

    store_specs = [("ZipKart", "Linking Road", (262.0, 160.0)),
                   ("MinuteMart", "Hill Road", (-280.0, -80.0)),
                   ("DashKirana", "Turner Road", (330.0, -150.0))]
    dark_stores = []
    used = set()
    for op, road, target in store_specs:
        cands = sorted((b for b in buildings if b["_front"] == road and b["_area"] > 150),
                       key=lambda b: math.dist(b["centroid"], target))
        for b in cands:
            e, en, exy = entrance(b)
            if e["name"] == road:
                break
        b["use"] = "commercial"
        b["height"] = round(rng.uniform(8.5, 11.5), 1)
        b["name"] = f"{op} Dark Store"
        b["address"] = f"{rng.randint(1, 140)}, {road}, Bandra West, Mumbai 400050"
        used.add(b["id"])
        dark_stores.append({"id": f"DS-{op}", "operator": op, "building_id": b["id"],
                            "name": f"{op} Dark Store", "x": b["centroid"][0], "y": b["centroid"][1],
                            "entrance_node": en, "entrance_xy": exy})

    first = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Sneha", "Kabir", "Meera", "Arjun", "Zoya",
             "Ishaan", "Fatima", "Neil", "Tara", "Farhan", "Kavya", "Dev", "Sana", "Yash", "Rhea",
             "Aditya", "Nikita", "Joel", "Pooja", "Imran", "Diya", "Cyrus", "Leela", "Omkar", "Natasha"]
    last = ["Mehta", "D'Souza", "Shaikh", "Kapoor", "Pereira", "Iyer", "Patil", "Khan", "Fernandes",
            "Sharma", "Desai", "Rodrigues", "Joshi", "Bhatt", "Mistry", "Gomes", "Kulkarni", "Ansari",
            "Nair", "Pinto"]
    homes = []
    pool = [b for b in buildings if b["use"] == "residential" and b["id"] not in used
            and b["height"] <= 48 and abs(b["centroid"][0]) < 660 and abs(b["centroid"][1]) < 660]
    rng.shuffle(pool)
    chosen: list[dict] = []
    for min_sp in (110.0, 80.0, 50.0, 0.0):
        for b in pool:
            if len(chosen) >= 50:
                break
            if b in chosen:
                continue
            if any(math.dist(b["centroid"], (s["x"], s["y"])) < 60 for s in dark_stores):
                continue
            if all(math.dist(b["centroid"], o["centroid"]) >= min_sp for o in chosen):
                chosen.append(b)
        if len(chosen) >= 50:
            break
    chosen.sort(key=lambda b: (-b["centroid"][1], b["centroid"][0]))
    for i, b in enumerate(chosen, 1):
        e, en, exy = entrance(b)
        num = rng.randint(1, 140)
        street = b["_front"]
        if b["name"]:
            addr = f"Flat {rng.randint(1, 9)}{rng.choice('ABCD')}, {b['name']}, {num}, {street}, Bandra West"
        else:
            addr = f"{num}, {street}, Bandra West"
        b["address"] = addr + ", Mumbai 400050"
        homes.append({"id": f"H-{i:02d}", "building_id": b["id"], "address": addr,
                      "customer_name": f"{rng.choice(first)} {rng.choice(last)}",
                      "x": b["centroid"][0], "y": b["centroid"][1], "entrance_node": en,
                      "entrance_xy": exy, "height": b["height"]})

    # ---------------- outputs
    roads = [{"id": e["id"], "kind": e["kind"], "width": e["_width"], "shape": e["shape"]} for e in edges]
    graph_edges = [{k: v for k, v in e.items() if not k.startswith("_")} for e in edges]
    bout = [{k: v for k, v in b.items() if not k.startswith("_")} for b in buildings]
    return {"nodes": nodes, "edges": graph_edges}, roads, bout, {"dark_stores": dark_stores, "homes": homes}


def make_meta() -> dict:
    utm = list(ORIGIN_UTM)
    try:
        from pyproj import Transformer
        utm = list(Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True).transform(*ORIGIN_LONLAT))
    except Exception:
        pass
    lon0, lat0 = ORIGIN_LONLAT
    dlat = HALF / 111320.0
    dlon = HALF / (111320.0 * math.cos(math.radians(lat0)))
    return {"epsg": 32643, "origin_lonlat": [lon0, lat0], "origin_utm": utm,
            "bbox_wsen": [round(lon0 - dlon, 5), round(lat0 - dlat, 5), round(lon0 + dlon, 5),
                          round(lat0 + dlat, 5)],
            "sumo_net_offset": None,
            "attribution": "Synthetic dev fixture (no OSM data)",
            "synthetic": True}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "fixtures" / "world"))
    ap.add_argument("--seed", type=int, default=1729)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    graph, roads, buildings, pois = generate(a.seed)
    files = {"meta.json": make_meta(), "roads.json": roads, "road_graph.json": graph,
             "buildings.json": buildings, "pois.json": pois}
    for name, obj in files.items():
        (out / name).write_text(json.dumps(obj, separators=(",", ":")))
    nd = sum(1 for e in graph["edges"] if e["kind"] == "drive")
    print(f"[gen_fixture] DEV FIXTURE (synthetic, not OSM) -> {out}")
    print(f"  nodes={len(graph['nodes'])} edges={len(graph['edges'])} (drive={nd}, walk={len(graph['edges']) - nd})")
    print(f"  buildings={len(buildings)} dark_stores={len(pois['dark_stores'])} homes={len(pois['homes'])}")
    for name in files:
        print(f"  {name:16s} {(out / name).stat().st_size / 1024:8.1f} KB")


if __name__ == "__main__":
    main()
