"""Local route planner on road_graph.json (INTERFACES.md).

Route *computation* is local (shortest path); the *decision* to reroute and the blocked edges come
from 0G / the shared HazardRegistry map and are passed in by the integrator.

Rovers route on kind=="drive" edges and drive on the LEFT side of the carriageway (India): the path
polyline is the edge centreline offset to the left of the travel direction.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import networkx as nx
import numpy as np

LANE_WIDTH_M = 3.2


@dataclass
class Route:
    edge_ids: list[str]
    node_ids: list[str]
    centerline: np.ndarray            # (N,2) road centreline along the route (travel order)
    polyline: np.ndarray              # (N,2) left-offset driving line (what the rover follows)
    half_width: np.ndarray            # (N,) carriageway half width at each polyline vertex
    length: float
    edge_index: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))  # vertex -> edge idx (-1 = approach)

    @property
    def goal(self) -> np.ndarray:
        return self.polyline[-1]

    def edge_at_vertex(self, i: int) -> Optional[str]:
        if not len(self.edge_index):
            return None
        k = int(self.edge_index[min(max(i, 0), len(self.edge_index) - 1)])
        return self.edge_ids[k] if 0 <= k < len(self.edge_ids) else None


def _left_normals(pts: np.ndarray) -> np.ndarray:
    """Per-vertex left unit normals (miter-averaged) of a polyline."""
    n = len(pts)
    seg = np.diff(pts, axis=0)
    seg_len = np.linalg.norm(seg, axis=1, keepdims=True)
    seg_len[seg_len < 1e-9] = 1.0
    d = seg / seg_len
    segn = np.stack([-d[:, 1], d[:, 0]], axis=1)
    out = np.zeros((n, 2))
    out[0] = segn[0]
    out[-1] = segn[-1]
    for i in range(1, n - 1):
        m = segn[i - 1] + segn[i]
        mn = np.linalg.norm(m)
        if mn < 1e-6:                        # U-turn: fall back to incoming normal
            out[i] = segn[i - 1]
            continue
        m /= mn
        cos_half = float(np.dot(m, segn[i]))
        out[i] = m / max(cos_half, 0.5)      # miter, capped at 2x
    return out


def _dedup(pts: np.ndarray, *others: np.ndarray, eps: float = 0.05):
    keep = [0]
    for i in range(1, len(pts)):
        if np.linalg.norm(pts[i] - pts[keep[-1]]) > eps:
            keep.append(i)
    keep = np.array(keep)
    return (pts[keep],) + tuple(o[keep] for o in others)


class RoadPlanner:
    def __init__(self, graph: dict, *, kinds: Sequence[str] = ("drive",),
                 lane_width: float = LANE_WIDTH_M, offset_frac: float = 0.75) -> None:
        """offset_frac: driving line at offset_frac * half-width left of centre (1.0 = kerb)."""
        self.lane_width = lane_width
        self.offset_frac = offset_frac
        self.nodes: dict[str, tuple[float, float]] = {n["id"]: (float(n["x"]), float(n["y"]))
                                                      for n in graph["nodes"]}
        self.edges: dict[str, dict] = {}
        self.G = nx.MultiDiGraph()
        for e in graph["edges"]:
            if e.get("kind", "drive") not in kinds:
                continue
            shape = np.asarray(e.get("shape") or [self.nodes[e["from"]], self.nodes[e["to"]]],
                               dtype=float)
            length = float(e.get("length") or np.linalg.norm(np.diff(shape, axis=0), axis=1).sum())
            lanes = max(1, int(e.get("lanes") or 1))
            rec = dict(e, shape=shape, length=length, lanes=lanes)
            self.edges[e["id"]] = rec
            self.G.add_edge(e["from"], e["to"], key=e["id"], eid=e["id"], length=length,
                            reverse=False)
            if not e.get("oneway", False):
                self.G.add_edge(e["to"], e["from"], key=e["id"] + "~r", eid=e["id"],
                                length=length, reverse=True)
        self._node_ids = [n for n in self.G.nodes]
        self._node_xy = np.array([self.nodes[n] for n in self._node_ids]) if self._node_ids \
            else np.zeros((0, 2))

    @classmethod
    def from_file(cls, path: str | Path, **kw) -> "RoadPlanner":
        with open(path) as f:
            return cls(json.load(f), **kw)

    # ------------------------------------------------------------------ queries
    def nearest_node(self, xy: Sequence[float]) -> str:
        d = np.linalg.norm(self._node_xy - np.asarray(xy[:2], dtype=float), axis=1)
        return self._node_ids[int(np.argmin(d))]

    def edge_shape(self, edge_id: str) -> np.ndarray:
        return self.edges[edge_id]["shape"]

    def edge_heading_at(self, edge_id: str, xy: Sequence[float]) -> float:
        """Heading (rad) of the edge segment nearest to xy (from->to direction)."""
        sh = self.edges[edge_id]["shape"]
        p = np.asarray(xy[:2], dtype=float)
        best, yaw = 1e18, 0.0
        for a, b in zip(sh[:-1], sh[1:]):
            ab = b - a
            t = np.clip(np.dot(p - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1)
            d = np.linalg.norm(a + t * ab - p)
            if d < best:
                best, yaw = d, math.atan2(ab[1], ab[0])
        return yaw

    def half_width(self, edge_id: str) -> float:
        return self.edges[edge_id]["lanes"] * self.lane_width / 2

    # ------------------------------------------------------------------ planning
    def plan(self, start_xy: Sequence[float], goal_xy: Optional[Sequence[float]] = None, *,
             goal_node: Optional[str] = None, start_node: Optional[str] = None,
             blocked_edge_ids: Iterable[str] = (),
             edge_penalty: Optional[Mapping[str, float]] = None) -> Optional[Route]:
        """Shortest path start -> goal. Returns None if no route exists (all blocked).

        blocked_edge_ids: edges that may not be used (both directions).
        edge_penalty: extra cost in metres per edge id (e.g. an unverified report ahead).
        """
        blocked = set(blocked_edge_ids)
        pen = dict(edge_penalty or {})
        s = start_node or self.nearest_node(start_xy)
        if goal_node is None:
            if goal_xy is None:
                raise ValueError("goal_xy or goal_node required")
            goal_node = self.nearest_node(goal_xy)

        def w(u, v, d):
            best = None
            for attrs in d.values():
                eid = attrs["eid"]
                if eid in blocked:
                    continue
                c = attrs["length"] + pen.get(eid, 0.0)
                if best is None or c < best:
                    best = c
            return best

        try:
            nodes = nx.dijkstra_path(self.G, s, goal_node, weight=w)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        edge_ids, center, hw, eidx = [], [], [], []
        for k, (u, v) in enumerate(zip(nodes[:-1], nodes[1:])):
            cands = [(a["length"] + pen.get(a["eid"], 0.0), a) for a in self.G[u][v].values()
                     if a["eid"] not in blocked]
            _, a = min(cands, key=lambda c: c[0])
            eid = a["eid"]
            edge_ids.append(eid)
            sh = self.edges[eid]["shape"]
            sh = sh[::-1] if a["reverse"] else sh
            center.extend(sh.tolist())
            hw.extend([self.half_width(eid)] * len(sh))
            eidx.extend([k] * len(sh))
        start = np.asarray(start_xy[:2], dtype=float)
        if not edge_ids:   # start and goal snap to the same node
            p = np.asarray(self.nodes[s])
            center, hw, eidx = [p.tolist()], [self.lane_width / 2], [-1]
        center_a = np.asarray(center, dtype=float)
        hw_a = np.asarray(hw, dtype=float)
        eidx_a = np.asarray(eidx, dtype=int)
        center_a, hw_a, eidx_a = _dedup(center_a, hw_a, eidx_a)
        if len(center_a) >= 2:
            line = center_a + _left_normals(center_a) * (hw_a * self.offset_frac)[:, None]
        else:
            line = center_a.copy()
        # approach / exit legs to arbitrary start & goal points
        pts = [start[None, :], line]
        cen = [start[None, :], center_a]
        hws = [np.array([hw_a[0]]), hw_a]
        eis = [np.array([-1]), eidx_a]
        if goal_xy is not None:
            g = np.asarray(goal_xy[:2], dtype=float)
            pts.append(g[None, :]); cen.append(g[None, :])
            hws.append(np.array([hw_a[-1]])); eis.append(np.array([len(edge_ids)]))
        poly = np.concatenate(pts)
        cent = np.concatenate(cen)
        hwa = np.concatenate(hws)
        eia = np.concatenate(eis)
        poly, cent, hwa, eia = _dedup(poly, cent, hwa, eia)
        eia = np.clip(eia, -1, max(len(edge_ids) - 1, -1))
        length = float(np.linalg.norm(np.diff(poly, axis=0), axis=1).sum()) if len(poly) > 1 else 0.0
        return Route(edge_ids=edge_ids, node_ids=list(nodes), centerline=cent, polyline=poly,
                     half_width=hwa, length=length, edge_index=eia)
