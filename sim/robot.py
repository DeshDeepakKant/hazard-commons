"""Delivery rover: scaled pybullet_data husky with velocity-controlled wheels + operator cargo box.

Driving stack (all local, every 20 Hz control tick):
  planner (sim/controllers/planner.py) -> pure pursuit (sim/controllers/pure_pursuit.py)
  -> reflex speed limit from the lidar fan (sim/sensors/lidar.py) -> wheel velocities.
Checkpoints / SAFE_STOP come from sim/robot_base.py.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
import pybullet as p

from sim.controllers.planner import Route
from sim.util import quiet_stdout
from sim.controllers.pure_pursuit import PurePursuit
from sim.robot_base import RobotBase
from sim.schemas import FsmState
from sim.sensors.lidar import Lidar, LidarScan, reflex_speed_limit

if TYPE_CHECKING:
    from sim.bodies import BodyWorld

WHEELS = ("front_left_wheel", "front_right_wheel", "rear_left_wheel", "rear_right_wheel")


class Rover(RobotBase):
    kind = "rover"

    def __init__(self, world: "BodyWorld", robot_id: str, operator: str, xy: Sequence[float],
                 yaw: float = 0.0, color=(0.8, 0.8, 0.8, 1.0), *, mode: str = "physics",
                 timeout_s: Optional[float] = None, retry_s: float = 10.0) -> None:
        """mode: "physics" = pure wheel-velocity driving; "hybrid" = wheels + base velocity
        servo each tick (kinematic-dynamic fallback, robust at any scale)."""
        super().__init__(robot_id, operator, timeout_s=timeout_s, retry_s=retry_s)
        self.world = world
        self.client = c = world.client
        cfg = world.cfg
        self.scale = float(cfg.get("rover", {}).get("scale", 0.5))
        self.max_speed = float(cfg.get("rover", {}).get("max_speed_mps", 4.0))
        lc = cfg.get("lidar", {})
        self.brake_dist = float(lc.get("brake_dist_m", 2.0))
        self.mode = mode
        self.color = color
        z0 = 0.2 * self.scale + 0.02
        with quiet_stdout():   # the husky URDF spams "No inertial data" warnings
            self.body = p.loadURDF("husky/husky.urdf", [float(xy[0]), float(xy[1]), z0],
                                   p.getQuaternionFromEuler([0, 0, yaw]),
                                   globalScaling=self.scale, flags=p.URDF_MERGE_FIXED_LINKS,
                                   physicsClientId=c)
        names = {p.getJointInfo(self.body, j, physicsClientId=c)[1].decode(): j
                 for j in range(p.getNumJoints(self.body, physicsClientId=c))}
        self.wheel_joints = [names[n] for n in WHEELS]
        self.left = [names["front_left_wheel"], names["rear_left_wheel"]]
        self.right = [names["front_right_wheel"], names["rear_right_wheel"]]
        for j in self.wheel_joints:
            p.changeDynamics(self.body, j, lateralFriction=1.2, rollingFriction=0.0,
                             spinningFriction=0.0, physicsClientId=c)
        # tint the chassis plates in the operator colour
        for j in range(-1, p.getNumJoints(self.body, physicsClientId=c)):
            if j not in self.wheel_joints:
                p.changeVisualShape(self.body, j, rgbaColor=[0.25, 0.25, 0.27, 1],
                                    physicsClientId=c)
        aabb = p.getAABB(self.body, -1, physicsClientId=c)
        ws = [p.getLinkState(self.body, j, physicsClientId=c)[0] for j in self.wheel_joints]
        self.track = abs(ws[0][1] - ws[1][1]) if abs(ws[0][1] - ws[1][1]) > 0.05 else 0.28
        wi = p.getVisualShapeData(self.body, physicsClientId=c)
        self.wheel_r = 0.1651 * self.scale
        for v in wi:
            if v[1] == self.wheel_joints[0] and v[2] == p.GEOM_CYLINDER:
                self.wheel_r = v[3][1]
        # cargo box (operator colour), welded on top
        hs = [0.22 * self.scale * 2, 0.16 * self.scale * 2, 0.14 * self.scale * 2]
        top = 0.33 * self.scale * 2
        col = p.createCollisionShape(p.GEOM_BOX, halfExtents=hs, physicsClientId=c)
        vis = p.createVisualShape(p.GEOM_BOX, halfExtents=hs, rgbaColor=list(color),
                                  physicsClientId=c)
        bp, bo = p.getBasePositionAndOrientation(self.body, physicsClientId=c)
        self.cargo = p.createMultiBody(1.0, col, vis, [bp[0], bp[1], bp[2] + top + hs[2]], bo,
                                       physicsClientId=c)
        p.createConstraint(self.body, -1, self.cargo, -1, p.JOINT_FIXED, [0, 0, 0],
                           [0, 0, top + hs[2] - (bp[2] - bp[2])], [0, 0, 0], physicsClientId=c)
        p.setCollisionFilterPair(self.body, self.cargo, -1, -1, 0, physicsClientId=c)
        for j in range(p.getNumJoints(self.body, physicsClientId=c)):
            p.setCollisionFilterPair(self.body, self.cargo, j, -1, 0, physicsClientId=c)
        # kinematic SUMO mirrors are teleported each tick: they must not push rovers around
        # (lidar rays still hit them). Both sides of the group/mask test must exclude them.
        from sim.bodies import NOT_ACTORS as mask
        for j in range(-1, p.getNumJoints(self.body, physicsClientId=c)):
            p.setCollisionFilterGroupMask(self.body, j, 0x1, mask, physicsClientId=c)
        p.setCollisionFilterGroupMask(self.cargo, -1, 0x1, mask, physicsClientId=c)
        self.half_width = max(0.2, (aabb[1][1] - aabb[0][1]) / 2)
        self.lidar = Lidar(c, rays=int(lc.get("rays", 31)), fov_deg=float(lc.get("fov_deg", 120)),
                           range_m=float(lc.get("range_m", 12.0)), height=0.3,
                           start_offset=0.45)
        self.pp = PurePursuit(max_speed=self.max_speed)
        self.kerb_pp = PurePursuit(max_speed=1.0, lookahead_min=1.0, goal_tol=0.4)
        self.route: Optional[Route] = None
        self.arrived = False
        self.on_arrival: Optional[Callable[["Rover"], None]] = None
        self.last_scan: Optional[LidarScan] = None
        self.reflex_braking = False
        self.reflex_free_dist = math.inf
        self._v_cmd = 0.0
        self._kerb_active = False
        self._odo = 0.0
        self._last_xy = np.array(xy[:2], dtype=float)

    # ------------------------------------------------------------------ pose
    def pose(self) -> tuple[float, float, float, float, float]:
        c = self.client
        pos, orn = p.getBasePositionAndOrientation(self.body, physicsClientId=c)
        yaw = p.getEulerFromQuaternion(orn)[2]
        lin, _ = p.getBaseVelocity(self.body, physicsClientId=c)
        return pos[0], pos[1], pos[2], yaw, math.hypot(lin[0], lin[1])

    def camera_pose(self):
        x, y, z, yaw, _ = self.pose()
        c, s = math.cos(yaw), math.sin(yaw)
        eye = [x + c * 0.35, y + s * 0.35, 1.3]
        target = [x + c * 12.0, y + s * 12.0, 0.6]
        return eye, target, [0, 0, 1]

    # ------------------------------------------------------------------ commands
    def drive_route(self, route: Route, state: Optional[FsmState] = None) -> None:
        """Follow a planned route. If paused on the brain, it becomes the route to resume."""
        self.route = route
        self.route_edge_ids = list(route.edge_ids)
        self.pp.set_path(route.polyline)
        self.arrived = False
        if state is not None:
            self.set_state(state)

    def drive_to(self, goal_xy: Optional[Sequence[float]] = None, *, goal_node: Optional[str] = None,
                 state: Optional[FsmState] = None, blocked_edge_ids: Iterable[str] = (),
                 edge_penalty: Optional[Mapping[str, float]] = None) -> Optional[Route]:
        """Plan with the world's road planner and drive. Returns None (and stops) if unroutable."""
        planner = self.world.planner
        if planner is None:
            raise RuntimeError("BodyWorld has no road_graph/planner")
        x, y, *_ = self.pose()
        r = planner.plan((x, y), goal_xy, goal_node=goal_node, blocked_edge_ids=blocked_edge_ids,
                         edge_penalty=edge_penalty)
        if r is None:
            self.stop()
            self.note = "no route"
            return None
        self.drive_route(r, state)
        return r

    def replan(self, blocked_edge_ids: Iterable[str] = (),
               edge_penalty: Optional[Mapping[str, float]] = None) -> Optional[Route]:
        """Re-plan to the current goal with new blocked edges (engine calls this after a 0G
        'reroute' decision). Unverified reports are ignored while in SAFE_STOP."""
        if self.route is None or self.in_safe_stop:
            return None
        return self.drive_to(self.route.goal, blocked_edge_ids=blocked_edge_ids,
                             edge_penalty=edge_penalty)

    def stop(self) -> None:
        self.route = None
        self.route_edge_ids = []
        self.pp.clear()

    def current_edge(self) -> Optional[str]:
        if self.route is None:
            return None
        return self.route.edge_at_vertex(self.pp.idx)

    def upcoming_edges(self, n: int = 3) -> list[str]:
        """The current edge and the next n-1 edges on the route (for CP-H2 triggers)."""
        if self.route is None:
            return []
        cur = self.current_edge()
        ids = self.route.edge_ids
        k = ids.index(cur) if cur in ids else 0
        return ids[k:k + n]

    # ------------------------------------------------------------------ pause hooks
    def _on_pause_begin(self) -> None:
        self._kerb_active = False

    def _on_pause_end(self) -> None:
        self._kerb_active = False
        self.kerb_pp.clear()

    def _on_safe_stop(self) -> None:
        """Pull over to the left kerb (lateral offset), then stop and hold."""
        self._kerb_active = False
        if self.route is None or len(self.route.centerline) < 2:
            return
        x, y, _, yaw, _ = self.pose()
        pos = np.array([x, y])
        C = self.route.centerline
        i = min(max(self.pp.idx, 0), len(C) - 2)
        # find the centreline segment nearest to us around the progress index
        best, bi, bt = 1e18, i, 0.0
        for k in range(max(0, i - 3), min(len(C) - 1, i + 4)):
            a, b = C[k], C[k + 1]
            ab = b - a
            t = float(np.clip(np.dot(pos - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1))
            d = float(np.linalg.norm(a + t * ab - pos))
            if d < best:
                best, bi, bt = d, k, t
        a, b = C[bi], C[bi + 1]
        d = (b - a) / max(np.linalg.norm(b - a), 1e-9)
        n = np.array([-d[1], d[0]])
        proj = a + bt * (b - a)
        hw = float(self.route.half_width[min(bi, len(self.route.half_width) - 1)])
        ahead = 3.0 + 1.0 * min(self.pose()[4], 4.0)
        kerb = proj + d * ahead + n * max(hw - 0.35, 0.5)
        self.kerb_target = kerb
        self.kerb_pp.set_path(np.stack([pos, pos + d * 0.8, kerb]))
        self._kerb_active = True

    # ------------------------------------------------------------------ control tick
    def tick(self, now: float) -> None:
        self.now = now
        self._poll_checkpoint(now)
        x, y, z, yaw, v_now = self.pose()
        self.last_scan = self.lidar.scan(x, y, yaw, z=0.0)
        lim, free = reflex_speed_limit(self.last_scan, self.half_width, self.brake_dist)
        self.reflex_free_dist = free
        self.reflex_braking = lim == 0.0

        v, w = 0.0, 0.0
        if self.paused:
            if self._kerb_active:
                cmd = self.kerb_pp.compute(x, y, yaw, v_now, speed_limit=lim)
                if cmd.done:
                    self._kerb_active = False
                else:
                    v, w = cmd.v, cmd.omega
        elif self.route is not None and not self.arrived:
            cmd = self.pp.compute(x, y, yaw, v_now, speed_limit=lim)
            if cmd.done:
                self.arrived = True
                if self.on_arrival is not None:
                    self.on_arrival(self)
            else:
                v, w = cmd.v, cmd.omega
        # acceleration limit; braking is immediate (reflex)
        if v < self._v_cmd:
            self._v_cmd = v if (lim == 0.0 or v == 0.0) else max(v, self._v_cmd - 4.0 * 0.05)
        else:
            self._v_cmd = min(v, self._v_cmd + 2.0 * 0.05)
        self._apply(self._v_cmd, w, yaw)
        xy = np.array([x, y])
        self._odo += float(np.linalg.norm(xy - self._last_xy))
        self._last_xy = xy
        self.battery = max(0.0, 1.0 - self._odo / 20000.0)

    def _apply(self, v: float, w: float, yaw: float) -> None:
        c = self.client
        k_skid = 1.6                       # skid-steer: wheels must slip to turn
        if self.mode == "physics" and (abs(v) > 0.05 or abs(w) > 0.05):
            # closed loop on measured forward speed and yaw rate (skid steer slips)
            lin, ang = p.getBaseVelocity(self.body, physicsClientId=c)
            v_meas = lin[0] * math.cos(yaw) + lin[1] * math.sin(yaw)
            self._iv = float(np.clip(getattr(self, "_iv", 0.0) + (v - v_meas) * 0.05, -1, 1))
            v = v + 0.5 * (v - v_meas) + 0.5 * self._iv
            w = w + 0.6 * (w - ang[2])
        vl =(v - w * self.track * k_skid / 2) / self.wheel_r
        vr = (v + w * self.track * k_skid / 2) / self.wheel_r
        p.setJointMotorControlArray(self.body, self.left + self.right, p.VELOCITY_CONTROL,
                                    targetVelocities=[vl, vl, vr, vr], forces=[40.0] * 4,
                                    physicsClientId=c)
        if self.mode == "hybrid":
            lin, ang = p.getBaseVelocity(self.body, physicsClientId=c)
            p.resetBaseVelocity(self.body, [v * math.cos(yaw), v * math.sin(yaw), lin[2]],
                                [ang[0], ang[1], w], physicsClientId=c)
