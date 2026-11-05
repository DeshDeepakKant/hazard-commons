"""Pure pursuit path follower for a differential-drive rover (local, every control tick)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class PPCommand:
    v: float            # forward speed command (m/s)
    omega: float        # yaw rate command (rad/s)
    done: bool          # goal reached
    dist_to_goal: float
    progress_idx: int   # index of the polyline segment the robot is on


class PurePursuit:
    def __init__(self, max_speed: float = 4.0, lookahead_min: float = 1.5,
                 lookahead_gain: float = 0.6, max_lat_acc: float = 2.0, goal_tol: float = 1.0,
                 decel: float = 2.0, max_omega: float = 2.5) -> None:
        self.max_speed = max_speed
        self.lookahead_min = lookahead_min
        self.lookahead_gain = lookahead_gain
        self.max_lat_acc = max_lat_acc
        self.goal_tol = goal_tol
        self.decel = decel
        self.max_omega = max_omega
        self.path: Optional[np.ndarray] = None
        self._cum: Optional[np.ndarray] = None
        self.idx = 0

    def set_path(self, polyline: np.ndarray) -> None:
        self.path = np.asarray(polyline, dtype=float)
        seg = np.linalg.norm(np.diff(self.path, axis=0), axis=1) if len(self.path) > 1 else np.zeros(0)
        self._cum = np.concatenate([[0.0], np.cumsum(seg)])
        self.idx = 0

    def clear(self) -> None:
        self.path = None

    def _project(self, p: np.ndarray) -> tuple[int, float, float]:
        """Closest point on the path, searching forward from the current index (monotone progress)."""
        P = self.path
        assert P is not None and self._cum is not None
        best = (self.idx, 0.0, 1e18)
        hi = min(len(P) - 1, self.idx + 25)
        for i in range(self.idx, max(hi, self.idx + 1)):
            if i + 1 >= len(P):
                break
            a, b = P[i], P[i + 1]
            ab = b - a
            L2 = float(np.dot(ab, ab))
            t = 0.0 if L2 < 1e-12 else float(np.clip(np.dot(p - a, ab) / L2, 0.0, 1.0))
            d = float(np.linalg.norm(a + t * ab - p))
            if d < best[2] - 1e-6:
                best = (i, t, d)
        i, t, d = best
        s = self._cum[i] + t * (self._cum[min(i + 1, len(P) - 1)] - self._cum[i])
        return i, s, d

    def _point_at(self, s: float) -> np.ndarray:
        P, cum = self.path, self._cum
        assert P is not None and cum is not None
        if s >= cum[-1]:
            return P[-1]
        i = int(np.searchsorted(cum, s, side="right") - 1)
        i = min(max(i, 0), len(P) - 2)
        seg = cum[i + 1] - cum[i]
        t = 0.0 if seg < 1e-9 else (s - cum[i]) / seg
        return P[i] + t * (P[i + 1] - P[i])

    def compute(self, x: float, y: float, yaw: float, v_now: float,
                speed_limit: Optional[float] = None) -> PPCommand:
        if self.path is None or len(self.path) == 0:
            return PPCommand(0.0, 0.0, True, 0.0, 0)
        p = np.array([x, y])
        if len(self.path) == 1:
            d = float(np.linalg.norm(self.path[0] - p))
            return PPCommand(0.0, 0.0, d < self.goal_tol, d, 0)
        i, s, _ = self._project(p)
        self.idx = i
        total = float(self._cum[-1])
        remaining = total - s
        dist_goal = float(np.linalg.norm(self.path[-1] - p))
        if remaining < self.goal_tol and dist_goal < self.goal_tol * 1.5:
            return PPCommand(0.0, 0.0, True, dist_goal, i)
        L = max(self.lookahead_min, self.lookahead_gain * abs(v_now) + self.lookahead_min)
        tgt = self._point_at(s + L)
        dx, dy = tgt - p
        c, sn = math.cos(yaw), math.sin(yaw)
        lx, ly = c * dx + sn * dy, -sn * dx + c * dy     # target in robot frame
        Ld2 = max(lx * lx + ly * ly, 1e-6)
        kappa = 2.0 * ly / Ld2
        vmax = self.max_speed if speed_limit is None else min(self.max_speed, speed_limit)
        # curvature limit, heading error limit, and stopping distance to the goal
        v = min(vmax, math.sqrt(self.max_lat_acc / max(abs(kappa), 1e-6)))
        heading_err = abs(math.atan2(ly, lx))
        if heading_err > math.radians(60):
            v = min(v, 0.4)                               # turn on the spot-ish
        v = min(v, math.sqrt(max(2.0 * self.decel * max(remaining - 0.3, 0.0), 0.0)) + 0.2)
        omega = float(np.clip(v * kappa if v > 0.5 else 2.0 * math.atan2(ly, lx),
                              -self.max_omega, self.max_omega))
        return PPCommand(float(v), omega, False, dist_goal, i)
