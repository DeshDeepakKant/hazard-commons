"""Generate the synthetic WS3 fixture (INTERFACES.md formats): a 4x4 grid of 2-lane two-way roads,
80 m blocks, one one-way street, one footpath, a building box inside each block.

    PYTHONPATH=. python tests/fixtures/make_fixture.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent
N = 4
SPACING = 80.0
OFF = -SPACING * (N - 1) / 2


def main() -> None:
    nodes, edges = [], []

    def nid(i: int, j: int) -> str:
        return f"n{i}_{j}"

    for i in range(N):
        for j in range(N):
            nodes.append({"id": nid(i, j), "x": OFF + i * SPACING, "y": OFF + j * SPACING})
    xy = {n["id"]: (n["x"], n["y"]) for n in nodes}
    k = 0
    for i in range(N):
        for j in range(N):
            for di, dj in ((1, 0), (0, 1)):
                a, b = (i, j), (i + di, j + dj)
                if b[0] >= N or b[1] >= N:
                    continue
                u, v = nid(*a), nid(*b)
                (x0, y0), (x1, y1) = xy[u], xy[v]
                mid = ((x0 + x1) / 2, (y0 + y1) / 2)
                # a slight kink in the middle so shapes are real polylines
                kink = (mid[0] + (3.0 if dj else 0.0), mid[1] + (3.0 if di else 0.0))
                shape = [[x0, y0], list(kink), [x1, y1]]
                length = math.dist((x0, y0), kink) + math.dist(kink, (x1, y1))
                oneway = (dj == 1 and i == N - 1)   # the east column is one-way northbound
                edges.append({"id": f"e{k}", "from": u, "to": v, "length": round(length, 2),
                              "lanes": 1 if oneway else 2, "name": f"Fixture Rd {k}",
                              "kind": "drive", "oneway": oneway, "shape": shape,
                              "sumo_edge_id": None})
                k += 1
    # one footpath edge (must be ignored by rover routing)
    edges.append({"id": "w0", "from": nid(0, 0), "to": nid(1, 1), "length": 113.1, "lanes": 1,
                  "name": "Footpath", "kind": "walk", "oneway": False,
                  "shape": [list(xy[nid(0, 0)]), list(xy[nid(1, 1)])], "sumo_edge_id": None})
    (OUT / "road_graph.json").write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=1))

    boxes = []
    b = 0
    for i in range(N - 1):
        for j in range(N - 1):
            cx, cy = OFF + (i + 0.5) * SPACING, OFF + (j + 0.5) * SPACING
            h = 6.0 + 3.2 * ((i * 3 + j) % 5)
            boxes.append({"id": f"b{b}", "center": [cx, cy, h / 2],
                          "half_extents": [24.0, 22.0, h / 2], "yaw": 0.1 * ((i + j) % 3 - 1)})
            b += 1
    (OUT / "collision").mkdir(exist_ok=True)
    (OUT / "collision" / "boxes.json").write_text(json.dumps(boxes, indent=1))
    print(f"{len(nodes)} nodes, {len(edges)} edges, {len(boxes)} boxes -> {OUT}")


if __name__ == "__main__":
    main()
