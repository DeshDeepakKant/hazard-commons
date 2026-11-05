"""PyBullet body world (DIRECT mode): ground, static building boxes, robots, hazard props,
kinematic mirrors of SUMO actors, physics stepping and checkpoint cameras.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence, Union

import numpy as np
import pybullet as p
import pybullet_data

from common.config import settings
from sim.schemas import ActorState, HazardProp

if TYPE_CHECKING:
    from sim.controllers.planner import RoadPlanner
    from sim.robot_base import RobotBase

Color = tuple[float, float, float, float]
HIDDEN_Z = -200.0

# full sizes (m): (length, width, height)
ACTOR_DIMS: dict[str, tuple[float, float, float]] = {
    "car": (4.2, 1.8, 1.5),
    "auto": (2.8, 1.4, 1.7),
    "bike": (2.0, 0.7, 1.3),
    "bus": (11.0, 2.5, 3.2),
    "pedestrian": (0.5, 0.5, 1.7),
}
ACTOR_COLORS: dict[str, Color] = {
    "car": (0.75, 0.75, 0.78, 1), "auto": (0.15, 0.55, 0.2, 1), "bike": (0.2, 0.2, 0.25, 1),
    "bus": (0.85, 0.2, 0.15, 1), "pedestrian": (0.85, 0.65, 0.45, 1),
}
# collision group for kinematic actors: hit by lidar rays but no contact forces on robots
ACTOR_GROUP, ACTOR_MASK = 0x4, 0x0


def hex_to_rgba(h: str, a: float = 1.0) -> Color:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)


def _quat_yaw(yaw: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


class BodyWorld:
    def __init__(self, boxes: Union[str, Path, list, None] = None, *,
                 road_graph: Union[str, Path, dict, None] = None,
                 physics_hz: Optional[float] = None, draw_roads: bool = True,
                 cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or settings()["sim"]
        self.physics_hz = float(physics_hz or self.cfg.get("physics_hz", 240))
        self.dt_phys = 1.0 / self.physics_hz
        self.client = p.connect(p.DIRECT)
        c = self.client
        p.setAdditionalSearchPath(pybullet_data.getDataPath(), physicsClientId=c)
        p.setGravity(0, 0, -9.81, physicsClientId=c)
        p.setTimeStep(self.dt_phys, physicsClientId=c)
        p.setPhysicsEngineParameter(numSolverIterations=10, physicsClientId=c)
        # ground: infinite collision plane + a big flat visual slab
        plane_col = p.createCollisionShape(p.GEOM_PLANE, physicsClientId=c)
        plane_vis = p.createVisualShape(p.GEOM_BOX, halfExtents=[3000, 3000, 0.01],
                                        rgbaColor=[0.55, 0.58, 0.5, 1], physicsClientId=c)
        self.ground = p.createMultiBody(0, plane_col, plane_vis, [0, 0, -0.011],
                                        physicsClientId=c)
        p.changeDynamics(self.ground, -1, lateralFriction=1.0, physicsClientId=c)
        p.resetBasePositionAndOrientation(self.ground, [0, 0, 0], [0, 0, 0, 1],
                                          physicsClientId=c)

        self.static_ids: dict[str, int] = {}
        self.robots: dict[str, "RobotBase"] = {}
        self.props: dict[str, list[int]] = {}
        self._actor_body: dict[str, tuple[str, int]] = {}      # actor id -> (kind, body)
        self._pool: dict[str, list[int]] = {k: [] for k in ACTOR_DIMS}
        self._actor_shapes: dict[str, tuple[int, int]] = {}
        self._hidden_n = 0
        self.planner: Optional["RoadPlanner"] = None
        self.sim_time = 0.0

        if boxes is not None:
            self.load_boxes(boxes)
        if road_graph is not None:
            from sim.controllers.planner import RoadPlanner
            g = road_graph if isinstance(road_graph, dict) else json.loads(Path(road_graph).read_text())
            self.planner = RoadPlanner(g)
            if draw_roads:
                self._draw_roads(g)

    # ------------------------------------------------------------------ static world
    def load_boxes(self, boxes: Union[str, Path, list]) -> int:
        if not isinstance(boxes, list):
            boxes = json.loads(Path(boxes).read_text())
        c = self.client
        for b in boxes:
            he = [float(v) for v in b["half_extents"]]
            col = p.createCollisionShape(p.GEOM_BOX, halfExtents=he, physicsClientId=c)
            shade = 0.62 + 0.25 * ((hash(b["id"]) % 100) / 100.0)
            vis = p.createVisualShape(p.GEOM_BOX, halfExtents=he,
                                      rgbaColor=[shade, shade * 0.95, shade * 0.88, 1],
                                      physicsClientId=c)
            bid = p.createMultiBody(0, col, vis, [float(v) for v in b["center"]],
                                    _quat_yaw(float(b.get("yaw", 0.0))), physicsClientId=c)
            self.static_ids[b["id"]] = bid
        return len(boxes)

    def _draw_roads(self, g: dict) -> None:
        """Visual-only dark strips for drive edges (one multi-shape body per edge)."""
        lane_w = self.planner.lane_width if self.planner else 3.2
        c = self.client
        for e in g["edges"]:
            if e.get("kind", "drive") != "drive":
                continue
            sh = np.asarray(e.get("shape") or [], dtype=float)
            if len(sh) < 2:
                continue
            w = max(1, int(e.get("lanes") or 1)) * lane_w
            he, pos, orn = [], [], []
            for a, b in zip(sh[:-1], sh[1:]):
                L = float(np.linalg.norm(b - a))
                if L < 0.05:
                    continue
                m = (a + b) / 2
                he.append([L / 2 + w / 4, w / 2, 0.004])
                pos.append([float(m[0]), float(m[1]), 0.0])
                orn.append(_quat_yaw(math.atan2(b[1] - a[1], b[0] - a[0])))
            if not he:
                continue
            for i in range(0, len(he), 16):    # createVisualShapeArray is limited in size
                vis = p.createVisualShapeArray(
                    shapeTypes=[p.GEOM_BOX] * len(he[i:i + 16]), halfExtents=he[i:i + 16],
                    visualFramePositions=pos[i:i + 16], visualFrameOrientations=orn[i:i + 16],
                    rgbaColors=[[0.22, 0.22, 0.24, 1]] * len(he[i:i + 16]), physicsClientId=c)
                p.createMultiBody(0, -1, vis, [0, 0, 0.003], physicsClientId=c)

    # ------------------------------------------------------------------ robots
    def add_rover(self, robot_id: str, operator: str, xy: Sequence[float], yaw: float = 0.0,
                  color: Union[str, Color, None] = None, **kw: Any):
        from sim.robot import Rover
        col = self._color(operator, color)
        r = Rover(self, robot_id, operator, xy, yaw, col, **kw)
        self.robots[robot_id] = r
        return r

    def add_drone(self, robot_id: str, operator: str, xy: Sequence[float],
                  color: Union[str, Color, None] = None, **kw: Any):
        from sim.drone import Drone
        col = self._color(operator, color)
        d = Drone(self.client, robot_id, operator, (float(xy[0]), float(xy[1])), col,
                  physics_hz=self.physics_hz, **kw)
        self.robots[robot_id] = d
        return d

    def _color(self, operator: str, color: Union[str, Color, None]) -> Color:
        if isinstance(color, str):
            return hex_to_rgba(color)
        if color is not None:
            return tuple(color)  # type: ignore[return-value]
        for o in settings().get("operators", []):
            if o["name"] == operator:
                return hex_to_rgba(o["color"])
        return (0.8, 0.8, 0.8, 1.0)

    def robot_states(self):
        return [r.state() for r in self.robots.values()]

    # ------------------------------------------------------------------ hazard props
    def add_prop(self, prop: HazardProp, yaw: Optional[float] = None) -> list[int]:
        """Spawn the physical scenario prop. `real=False` props (fake reports) spawn nothing."""
        self.remove_prop(prop.id)
        if not prop.real or prop.type == "none":
            self.props[prop.id] = []
            return []
        if yaw is None:
            yaw = 0.0
            if self.planner is not None and prop.edge_id in self.planner.edges:
                yaw = self.planner.edge_heading_at(prop.edge_id, (prop.x, prop.y))
        hw = (self.planner.half_width(prop.edge_id)
              if self.planner is not None and prop.edge_id in self.planner.edges else 3.2)
        c, s = math.cos(yaw), math.sin(yaw)

        def at(fwd: float, left: float) -> tuple[float, float]:
            return (prop.x + c * fwd - s * left, prop.y + s * fwd + c * left)

        ids: list[int] = []
        if prop.type == "road_works":
            # a barrier across the whole carriageway + a cone line before it
            for k in range(-2, 3):
                bx, by = at(0.0, k * hw / 2.5)
                ids.append(self._box([bx, by, 0.5], [0.15, hw / 5.2, 0.5], yaw,
                                     (1.0, 0.45, 0.05, 1) if k % 2 else (0.95, 0.95, 0.95, 1)))
            for k in range(-3, 4):
                cx, cy = at(-3.0 - abs(k) * 0.6, k * hw / 3.5)
                ids.append(self._cyl([cx, cy, 0.35], 0.15, 0.7, (1.0, 0.4, 0.0, 1)))
        elif prop.type == "accident":
            ax, ay = at(0.0, hw * 0.35)
            ids.append(self._box([ax, ay, 0.8], [2.1, 0.9, 0.75], yaw + 0.6,
                                 (0.7, 0.1, 0.1, 1), roll=0.25))
            bx, by = at(2.5, -hw * 0.4)
            ids.append(self._box([bx, by, 0.75], [2.1, 0.9, 0.75], yaw - 1.1,
                                 (0.2, 0.25, 0.6, 1), roll=-0.15))
            for k in range(5):
                dx, dy = at(-1.5 + k * 1.1, (k - 2) * hw / 3)
                ids.append(self._box([dx, dy, 0.1], [0.3, 0.2, 0.1], yaw + k, (0.3, 0.3, 0.3, 1),
                                     collide=False))
        elif prop.type == "waterlogging":
            vis = p.createVisualShape(p.GEOM_BOX, halfExtents=[9.0, hw + 1.0, 0.02],
                                      rgbaColor=[0.15, 0.35, 0.85, 0.6], physicsClientId=self.client)
            ids.append(p.createMultiBody(0, -1, vis, [prop.x, prop.y, 0.03], _quat_yaw(yaw),
                                         physicsClientId=self.client))
        self.props[prop.id] = ids
        return ids

    def remove_prop(self, prop_id: str) -> None:
        for b in self.props.pop(prop_id, []):
            p.removeBody(b, physicsClientId=self.client)

    def _box(self, pos, he, yaw, rgba, roll: float = 0.0, collide: bool = True) -> int:
        c = self.client
        col = p.createCollisionShape(p.GEOM_BOX, halfExtents=he, physicsClientId=c) if collide else -1
        vis = p.createVisualShape(p.GEOM_BOX, halfExtents=he, rgbaColor=rgba, physicsClientId=c)
        orn = p.getQuaternionFromEuler([roll, 0, yaw])
        return p.createMultiBody(0, col, vis, pos, orn, physicsClientId=c)

    def _cyl(self, pos, r, h, rgba) -> int:
        c = self.client
        col = p.createCollisionShape(p.GEOM_CYLINDER, radius=r, height=h, physicsClientId=c)
        vis = p.createVisualShape(p.GEOM_CYLINDER, radius=r, length=h, rgbaColor=rgba,
                                  physicsClientId=c)
        return p.createMultiBody(0, col, vis, pos, physicsClientId=c)

    # ------------------------------------------------------------------ kinematic actors
    def _actor_shape(self, kind: str) -> tuple[int, int]:
        if kind not in self._actor_shapes:
            L, W, H = ACTOR_DIMS[kind]
            c = self.client
            if kind == "pedestrian":
                col = p.createCollisionShape(p.GEOM_CAPSULE, radius=0.25, height=H - 0.5,
                                             physicsClientId=c)
                vis = p.createVisualShape(p.GEOM_CAPSULE, radius=0.25, length=H - 0.5,
                                          rgbaColor=ACTOR_COLORS[kind], physicsClientId=c)
            else:
                col = p.createCollisionShape(p.GEOM_BOX, halfExtents=[L / 2, W / 2, H / 2],
                                             physicsClientId=c)
                vis = p.createVisualShape(p.GEOM_BOX, halfExtents=[L / 2, W / 2, H / 2],
                                          rgbaColor=ACTOR_COLORS[kind], physicsClientId=c)
            self._actor_shapes[kind] = (col, vis)
        return self._actor_shapes[kind]

    def _hide_pos(self) -> list[float]:
        self._hidden_n += 1
        return [5000.0 + 10.0 * (self._hidden_n % 500), 5000.0, HIDDEN_Z]

    def upsert_kinematic_actors(self, actors: Iterable[ActorState]) -> None:
        """Mirror SUMO actors as kinematic (mass 0) bodies. Actors absent from `actors` are hidden
        and their bodies returned to a per-kind pool for reuse."""
        c = self.client
        seen: set[str] = set()
        for a in actors:
            kind = a.kind if a.kind in ACTOR_DIMS else "car"
            seen.add(a.id)
            cur = self._actor_body.get(a.id)
            if cur is not None and cur[0] != kind:
                self._release(a.id)
                cur = None
            if cur is None:
                if self._pool[kind]:
                    bid = self._pool[kind].pop()
                else:
                    col, vis = self._actor_shape(kind)
                    bid = p.createMultiBody(0, col, vis, self._hide_pos(), physicsClientId=c)
                    p.setCollisionFilterGroupMask(bid, -1, ACTOR_GROUP, ACTOR_MASK,
                                                  physicsClientId=c)
                self._actor_body[a.id] = (kind, bid)
            else:
                bid = cur[1]
            h = ACTOR_DIMS[kind][2]
            p.resetBasePositionAndOrientation(bid, [a.x, a.y, a.z + h / 2], _quat_yaw(a.yaw),
                                              physicsClientId=c)
        for aid in [k for k in self._actor_body if k not in seen]:
            self._release(aid)

    def _release(self, actor_id: str) -> None:
        kind, bid = self._actor_body.pop(actor_id)
        p.resetBasePositionAndOrientation(bid, self._hide_pos(), [0, 0, 0, 1],
                                          physicsClientId=self.client)
        self._pool[kind].append(bid)

    def actor_body_ids(self) -> set[int]:
        return {b for _, b in self._actor_body.values()}

    # ------------------------------------------------------------------ stepping
    def step(self, dt: float) -> None:
        """Advance physics by dt (one control tick) in 1/physics_hz substeps."""
        n = max(1, int(round(dt * self.physics_hz)))
        drones = [r for r in self.robots.values() if hasattr(r, "physics_substep")]
        c = self.client
        for _ in range(n):
            for d in drones:
                d.physics_substep(self.dt_phys)
            p.stepSimulation(physicsClientId=c)
        self.sim_time += n * self.dt_phys

    def tick(self, now: float, dt: float) -> None:
        """Convenience: run every robot's control tick at sim time `now`, then step physics."""
        for r in self.robots.values():
            r.tick(now)
        self.step(dt)

    # ------------------------------------------------------------------ camera
    def camera_frame(self, robot_id: str, w: Optional[int] = None, h: Optional[int] = None,
                     fov_deg: Optional[float] = None) -> bytes:
        """PNG bytes from the robot's camera (ER_TINY_RENDERER). Use at checkpoints only."""
        cam = self.cfg.get("camera", {})
        w = int(w or cam.get("width", 320))
        h = int(h or cam.get("height", 240))
        fov = float(fov_deg or cam.get("fov_deg", 70))
        r = self.robots[robot_id]
        eye, target, up = r.camera_pose()
        view = p.computeViewMatrix(eye, target, up, physicsClientId=self.client)
        proj = p.computeProjectionMatrixFOV(fov, w / h, 0.1, 400.0, physicsClientId=self.client)
        _, _, rgba, _, _ = p.getCameraImage(w, h, view, proj, renderer=p.ER_TINY_RENDERER,
                                            shadow=0, lightDirection=[0.4, -0.3, 1.0],
                                            flags=p.ER_NO_SEGMENTATION_MASK,
                                            physicsClientId=self.client)
        img = np.reshape(np.asarray(rgba, dtype=np.uint8), (h, w, 4))[:, :, :3]
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, format="PNG")
        return buf.getvalue()

    def close(self) -> None:
        try:
            p.disconnect(self.client)
        except Exception:
            pass
