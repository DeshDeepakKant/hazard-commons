"""2D lidar fan via pybullet.rayTestBatch, and the reflex brake that reads it.

The reflex is a spinal-cord speed limit (brake if something is in the corridor ahead). It is never a
decision: it does not classify hazards, reroute, or replace a 0G checkpoint.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pybullet as p


@dataclass
class LidarScan:
    angles: np.ndarray      # (n,) ray angles in the robot frame (rad, 0 = forward, + = left)
    dists: np.ndarray       # (n,) range (m); == range_m when nothing was hit
    body_ids: np.ndarray    # (n,) hit body unique id, -1 when no hit


class Lidar:
    def __init__(self, client: int, rays: int = 31, fov_deg: float = 120.0, range_m: float = 12.0,
                 height: float = 0.3, start_offset: float = 0.45) -> None:
        self.client = client
        self.rays = int(rays)
        self.range_m = float(range_m)
        self.height = height
        self.start_offset = start_offset      # rays start outside the robot's own hull
        half = math.radians(fov_deg) / 2
        self.angles = np.linspace(-half, half, self.rays) if self.rays > 1 else np.zeros(1)
        self._ca, self._sa = np.cos(self.angles), np.sin(self.angles)

    def scan(self, x: float, y: float, yaw: float, z: float = 0.0) -> LidarScan:
        c, s = math.cos(yaw), math.sin(yaw)
        dx = c * self._ca - s * self._sa
        dy = s * self._ca + c * self._sa
        zz = z + self.height
        r0, r1 = self.start_offset, self.range_m
        frm = np.stack([x + dx * r0, y + dy * r0, np.full(self.rays, zz)], axis=1)
        to = np.stack([x + dx * r1, y + dy * r1, np.full(self.rays, zz)], axis=1)
        res = p.rayTestBatch(frm.tolist(), to.tolist(), physicsClientId=self.client)
        frac = np.array([r[2] for r in res])
        ids = np.array([r[0] for r in res])
        d = r0 + frac * (r1 - r0)
        d[ids < 0] = r1
        return LidarScan(self.angles, d, ids)


def reflex_speed_limit(scan: LidarScan, half_width: float, brake_dist: float,
                       decel: float = 3.0, margin: float = 0.35,
                       ignore_ids: Optional[set[int]] = None) -> tuple[float, float]:
    """Max safe forward speed given obstacles in the straight corridor ahead.

    Returns (speed_limit, free_dist). speed_limit == 0 means: emergency brake.
    """
    fwd = scan.dists * np.cos(scan.angles)
    lat = scan.dists * np.sin(scan.angles)
    hit = (scan.body_ids >= 0) & (fwd > 0) & (np.abs(lat) < half_width + margin)
    if ignore_ids:
        hit &= ~np.isin(scan.body_ids, list(ignore_ids))
    if not hit.any():
        return math.inf, math.inf
    free = float(fwd[hit].min())
    if free <= brake_dist:
        return 0.0, free
    return math.sqrt(2.0 * decel * (free - brake_dist)), free
