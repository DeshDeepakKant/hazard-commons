"""SUMO background traffic, in-process via libsumo, in the shared local frame.

    tb = TrafficBridge()
    tb.start(seed=42)
    actors = tb.step(sim_time)          # list[ActorState] (local frame, yaw rad CCW from east)
    tb.close_edge("-12345#0")           # hazard: traffic crawls/jams there, others reroute
    tb.upsert_robot("R-07", x, y, yaw)  # our rover as a SUMO delivery_bot so cars yield
    tb.close()

SUMO time = sim_time + warmup_s (the network is pre-filled during start()).
libsumo allows ONE simulation per process; do not create two bridges at once.
Nothing raises out of step(): errors are logged and the last good actor list is returned.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Optional

from common.config import ROOT, settings
from common.frame import local_to_sumo, sumo_to_local
from sim.schemas import ActorState

log = logging.getLogger("traffic")

KIND_BY_VTYPE = {
    "car": "car",
    "auto_rickshaw": "auto",
    "motorcycle": "bike",
    "bus": "bus",
    "pedestrian": "pedestrian",
}
ROBOT_VTYPE = "delivery_bot"
ROBOT_PREFIX = "bot_"
# vehicle classes blocked on a closed edge (pedestrians may still pass on sidewalks)
VEHICLE_CLASSES = ["passenger", "motorcycle", "bus", "delivery", "taxi", "truck", "bicycle", "moped"]
CLOSED_SPEED = 0.3          # m/s: crawl past the accident instead of a permanent deadlock
CLOSED_TRAVELTIME = 1e5     # s: routing weight so others avoid the edge


def _sumo_home() -> str:
    if os.environ.get("SUMO_HOME"):
        return os.environ["SUMO_HOME"]
    import sumo  # eclipse-sumo wheel

    os.environ["SUMO_HOME"] = sumo.SUMO_HOME
    return sumo.SUMO_HOME


def _sumo_binary() -> str:
    home = _sumo_home()
    p = os.path.join(home, "bin", "sumo")
    return p if os.path.exists(p) else "sumo"


def compass_to_yaw(angle_deg: float) -> float:
    """SUMO angle (deg, 0=north, clockwise) -> yaw (rad, 0=east, CCW), wrapped to (-pi, pi]."""
    y = math.radians(90.0 - angle_deg)
    return math.atan2(math.sin(y), math.cos(y))


def yaw_to_compass(yaw: float) -> float:
    return (90.0 - math.degrees(yaw)) % 360.0


class TrafficBridge:
    def __init__(self, cfg: Optional[str] = None, warmup_s: float = 240.0,
                 extra_args: Optional[list[str]] = None):
        tcfg = settings().get("traffic", {})
        self.cfg = str(ROOT / (cfg or tcfg.get("sumocfg", "traffic/sumo.sumocfg")))
        self.warmup_s = warmup_s
        self.extra_args = list(extra_args or [])
        self.max_vehicles = int(tcfg.get("max_vehicles", 150))
        self.max_pedestrians = int(tcfg.get("max_pedestrians", 120))
        self.ts = None                       # libsumo module once started
        self.dt = 0.1
        self._robots: dict[str, tuple[float, float, float]] = {}   # robot_id -> (x, y, yaw) local
        self._robot_last_xy: dict[str, tuple[float, float]] = {}
        self._closed: dict[str, list[tuple[str, float, list[str]]]] = {}  # edge -> [(lane, speed, disallowed)]
        self._lengths: dict[str, float] = {}
        self._last: list[ActorState] = []
        self._err_count = 0
        self.stats = {"steps": 0, "step_ms_total": 0.0}

    # ------------------------------------------------------------------ lifecycle
    def start(self, seed: Optional[int] = None) -> None:
        import libsumo

        _sumo_home()
        if seed is None:
            seed = int(settings()["traffic"]["seed"])
        args = [_sumo_binary(), "-c", self.cfg, "--seed", str(int(seed)),
                "--no-step-log", "true", "--no-warnings", "true"] + self.extra_args
        try:
            libsumo.close()
        except Exception:
            pass
        libsumo.start(args)
        self.ts = libsumo
        self.dt = libsumo.simulation.getDeltaT()
        t0 = time.perf_counter()
        n = int(round(self.warmup_s / self.dt))
        for _ in range(n):
            libsumo.simulationStep()
            self._cap_pedestrians()
        log.info("SUMO started seed=%s, warmup %.0fs in %.1fs: %d vehicles, %d persons",
                 seed, self.warmup_s, time.perf_counter() - t0,
                 libsumo.vehicle.getIDCount(), libsumo.person.getIDCount())

    def close(self) -> None:
        if self.ts is not None:
            try:
                self.ts.close()
            except Exception as e:  # noqa: BLE001
                log.warning("SUMO close failed: %s", e)
        self.ts = None
        self._robots.clear()
        self._closed.clear()

    @property
    def sumo_time(self) -> float:
        return self.ts.simulation.getTime() if self.ts else 0.0

    # ------------------------------------------------------------------ stepping
    def step(self, sim_time: float) -> list[ActorState]:
        """Advance SUMO to warmup + sim_time; return every vehicle and person (robots excluded)."""
        if self.ts is None:
            return []
        t0 = time.perf_counter()
        try:
            target = self.warmup_s + float(sim_time)
            guard = 0
            while self.ts.simulation.getTime() + 0.5 * self.dt <= target and guard < 200:
                self._apply_robots()
                self.ts.simulationStep()
                self._after_sumo_step()
                guard += 1
            self._last = self._collect()
        except Exception as e:  # noqa: BLE001 never escape step()
            self._err_count += 1
            if self._err_count <= 20 or self._err_count % 100 == 0:
                log.exception("traffic step failed (%d): %s", self._err_count, e)
        self.stats["steps"] += 1
        self.stats["step_ms_total"] += (time.perf_counter() - t0) * 1000.0
        return self._last

    def _after_sumo_step(self) -> None:
        ts = self.ts
        if self._closed:
            # new vehicles may have been routed over a closed edge before it closed
            for vid in ts.simulation.getDepartedIDList():
                if not vid.startswith(ROBOT_PREFIX):
                    self._reroute_if_needed(vid)
        self._cap_pedestrians()

    def _cap_pedestrians(self) -> None:
        ts = self.ts
        n = ts.person.getIDCount()
        if n > self.max_pedestrians:
            for pid in list(ts.simulation.getDepartedPersonIDList())[: n - self.max_pedestrians]:
                try:
                    ts.person.remove(pid)
                except Exception:  # noqa: BLE001
                    pass

    def _length(self, vtype: str) -> float:
        L = self._lengths.get(vtype)
        if L is None:
            L = self._lengths[vtype] = self.ts.vehicletype.getLength(vtype)
        return L

    def _collect(self) -> list[ActorState]:
        ts = self.ts
        out: list[ActorState] = []
        for vid in ts.vehicle.getIDList():
            if vid.startswith(ROBOT_PREFIX):
                continue
            try:
                vtype = ts.vehicle.getTypeID(vid)
                sx, sy = ts.vehicle.getPosition(vid)       # front bumper
                ang = ts.vehicle.getAngle(vid)
                spd = ts.vehicle.getSpeed(vid)
            except Exception:  # noqa: BLE001 vehicle vanished mid-read
                continue
            if sx < -1e9:      # INVALID_DOUBLE_VALUE (not on the net yet)
                continue
            yaw = compass_to_yaw(ang)
            half = 0.5 * self._length(vtype)
            x, y = sumo_to_local(sx - half * math.cos(yaw), sy - half * math.sin(yaw))
            out.append(ActorState(id=vid, kind=KIND_BY_VTYPE.get(vtype, "car"),
                                  x=x, y=y, z=0.0, yaw=yaw, speed=spd))
        for pid in ts.person.getIDList():
            try:
                sx, sy = ts.person.getPosition(pid)
                ang = ts.person.getAngle(pid)
                spd = ts.person.getSpeed(pid)
            except Exception:  # noqa: BLE001
                continue
            if sx < -1e9:
                continue
            x, y = sumo_to_local(sx, sy)
            out.append(ActorState(id=pid, kind="pedestrian", x=x, y=y, z=0.0,
                                  yaw=compass_to_yaw(ang), speed=spd))
        return out

    # ------------------------------------------------------------------ hazards
    def close_edge(self, sumo_edge_id: str) -> bool:
        """Block vehicles on an edge: lanes crawl at CLOSED_SPEED and are disallowed for new
        entries; traffic routed over it reroutes, vehicles already on/at it queue (a jam)."""
        ts = self.ts
        if ts is None or not sumo_edge_id or sumo_edge_id in self._closed:
            return False
        try:
            saved = []
            for i in range(ts.edge.getLaneNumber(sumo_edge_id)):
                lane = f"{sumo_edge_id}_{i}"
                allowed = ts.lane.getAllowed(lane)
                saved.append((lane, ts.lane.getMaxSpeed(lane), list(allowed)))
                if list(allowed) == ["pedestrian"]:
                    continue          # sidewalk stays open for people
                ts.lane.setMaxSpeed(lane, CLOSED_SPEED)
            self._closed[sumo_edge_id] = saved
            ts.edge.adaptTraveltime(sumo_edge_id, CLOSED_TRAVELTIME)
            ts.edge.setEffort(sumo_edge_id, CLOSED_TRAVELTIME)
            n = 0
            for vid in ts.vehicle.getIDList():
                if not vid.startswith(ROBOT_PREFIX) and self._reroute_if_needed(vid):
                    n += 1
            log.info("closed edge %s (%d lanes), rerouted %d vehicles", sumo_edge_id, len(saved), n)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("close_edge(%s) failed: %s", sumo_edge_id, e)
            return False

    def reopen_edge(self, sumo_edge_id: str) -> bool:
        ts = self.ts
        saved = self._closed.pop(sumo_edge_id, None)
        if ts is None or saved is None:
            return False
        try:
            for lane, speed, _allowed in saved:
                ts.lane.setMaxSpeed(lane, speed)
            ts.edge.adaptTraveltime(sumo_edge_id, -1)   # back to default (length/speed)
            ts.edge.setEffort(sumo_edge_id, -1)
            log.info("reopened edge %s", sumo_edge_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("reopen_edge(%s) failed: %s", sumo_edge_id, e)
            return False

    @property
    def closed_edges(self) -> list[str]:
        return list(self._closed)

    def _reroute_if_needed(self, vid: str) -> bool:
        ts = self.ts
        try:
            route = ts.vehicle.getRoute(vid)
            idx = ts.vehicle.getRouteIndex(vid)
            ahead = route[idx + 1:] if idx >= 0 else route
            if any(e in self._closed for e in ahead):
                ts.vehicle.rerouteTraveltime(vid, currentTravelTimes=False)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------ robots
    def _vid(self, robot_id: str) -> str:
        return ROBOT_PREFIX + robot_id

    def upsert_robot(self, robot_id: str, x: float, y: float, yaw: float) -> None:
        """Record the rover pose (local frame); it is pushed into SUMO before every SUMO step."""
        self._robots[robot_id] = (float(x), float(y), float(yaw))

    def remove_robot(self, robot_id: str) -> None:
        self._robots.pop(robot_id, None)
        self._robot_last_xy.pop(robot_id, None)
        if self.ts is None:
            return
        try:
            vid = self._vid(robot_id)
            if vid in self.ts.vehicle.getIDList() or vid in self.ts.simulation.getPendingVehicles():
                self.ts.vehicle.remove(vid)
        except Exception as e:  # noqa: BLE001
            log.debug("remove_robot(%s): %s", robot_id, e)

    def _apply_robots(self) -> None:
        if not self._robots:
            return
        ts = self.ts
        present = set(ts.vehicle.getIDList())
        for rid, (x, y, yaw) in self._robots.items():
            vid = self._vid(rid)
            try:
                sx, sy = local_to_sumo(x, y)
                # SUMO position is the front bumper
                L = self._length(ROBOT_VTYPE)
                fx, fy = sx + 0.5 * L * math.cos(yaw), sy + 0.5 * L * math.sin(yaw)
                if vid not in present:
                    self._insert_robot(vid, fx, fy)
                ts.vehicle.moveToXY(vid, "", -1, fx, fy, angle=yaw_to_compass(yaw), keepRoute=2)
            except Exception as e:  # noqa: BLE001
                log.debug("robot %s update failed: %s", rid, e)

    def _insert_robot(self, vid: str, sx: float, sy: float) -> None:
        ts = self.ts
        try:
            ts.vehicle.remove(vid)     # stale pending copy, if any
        except Exception:  # noqa: BLE001
            pass
        edge, _pos, _lane = ts.simulation.convertRoad(sx, sy, False, "delivery")
        if not edge or edge.startswith(":"):
            edge, _pos, _lane = ts.simulation.convertRoad(sx, sy, False, "passenger")
        rid = f"{ROBOT_PREFIX}route_{edge}"
        if rid not in ts.route.getIDList():
            ts.route.add(rid, [edge])
        ts.vehicle.add(vid, rid, typeID=ROBOT_VTYPE, depart="now", departLane="best",
                       departPos="base", departSpeed="0")
        ts.vehicle.setSpeedMode(vid, 0)
        ts.vehicle.setLaneChangeMode(vid, 0)

    # ------------------------------------------------------------------ queries
    def edge_at(self, x: float, y: float, vclass: str = "passenger") -> Optional[str]:
        """Nearest SUMO edge (non-internal) to local (x, y) usable by vclass, or None."""
        if self.ts is None:
            return None
        try:
            sx, sy = local_to_sumo(x, y)
            edge, _pos, _lane = self.ts.simulation.convertRoad(sx, sy, False, vclass)
            if not edge:
                return None
            if edge.startswith(":"):
                # inside a junction: take the first outgoing normal edge of that lane's successor
                lane = f"{edge}_{_lane}"
                for link in self.ts.lane.getLinks(lane):
                    nxt = link[0].rsplit("_", 1)[0]
                    if not nxt.startswith(":"):
                        return nxt
                return None
            return edge
        except Exception as e:  # noqa: BLE001
            log.debug("edge_at(%s,%s): %s", x, y, e)
            return None

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for a in self._last:
            c[a.kind] = c.get(a.kind, 0) + 1
        return c

    def mean_step_ms(self) -> float:
        return self.stats["step_ms_total"] / max(1, self.stats["steps"])
