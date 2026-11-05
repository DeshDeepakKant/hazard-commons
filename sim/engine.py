"""Simulation Engine (WS8 / SPEC §5 & §12).

Wires together:
  * BodyWorld (PyBullet physics, rovers + drones)
  * TrafficBridge (SUMO background traffic mirrored as kinematic bodies)
  * 3 rival operators (ZipKart, MinuteMart, DashKirana)
  * OrderBook & Dispatcher (quick-commerce delivery workflow)
  * Brain (0G Compute Router / MockBrain)
  * EpisodeLog & ZGStorage (0G Storage memory)
  * HazardChain / LocalHazardChain (0G Chain HazardRegistry)
  * Event Bus (WebSocket streaming to viewer)

Coordinates all 0G checkpoints (CP-H1, CP-H2, CP-V) and SAFE_STOP logic.
Never fabricates a 0G decision.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from brain.client import get_brain
from brain.perception import build_observation
from common.config import ROOT, settings
from common.interfaces import Brain, Chain, Storage
from memory.episode_log import EpisodeLog, record_from_result
from memory.zg_storage import ZGStorage
from ops.dispatcher import Dispatcher, RoverLike
from ops.orders import OrderBook
from server.app import attach_engine
from server.bus import BUS
from sim.bodies import BodyWorld
from sim.clock import SimClock
from sim.controllers.planner import Route
from sim.drone import Drone
from sim.robot import Rover
from sim.schemas import (
    ActorState,
    ChainTxEvent,
    CheckpointRequest,
    CheckpointResult,
    DecisionEvent,
    FsmState,
    Hazard,
    HazardProp,
    Order,
    RobotState,
    StateSnapshot,
    StorageEvent,
)

log = logging.getLogger("hazard.engine")


# ------------------------------------------------------------------ local chain fallback
class LocalHazardChain:
    """In-memory chain simulation implementing common.interfaces.Chain.
    Used when Anvil / Galileo testnet is not available."""

    def __init__(self, on_tx: Optional[Callable[[ChainTxEvent], None]] = None,
                 sim_time_fn: Optional[Callable[[], float]] = None,
                 confirms_needed: int = 1, disputes_needed: int = 1) -> None:
        self.on_tx = on_tx
        self.sim_time_fn = sim_time_fn
        self.confirms_needed = confirms_needed
        self.disputes_needed = disputes_needed
        self._lock = threading.RLock()
        self._hazards: dict[str, Hazard] = {}
        self._next_id = 1
        self._pending_events: list[dict[str, Any]] = []

    def _sim_time(self) -> float:
        return float(self.sim_time_fn()) if self.sim_time_fn else 0.0

    def _make_tx(self, kind: str, operator: str, hazard_id: Optional[str] = None) -> str:
        tx_hash = f"0xlocal{random.randbytes(16).hex()}"
        if self.on_tx:
            try:
                self.on_tx(ChainTxEvent(
                    kind=kind,  # type: ignore[arg-type]
                    tx_hash=tx_hash,
                    operator=operator,
                    hazard_id=hazard_id,
                    status="mined",
                    sim_time=self._sim_time(),
                ))
            except Exception:
                log.exception("on_tx failed")
        return tx_hash

    def report_hazard(self, operator: str, robot_id: str, edge_id: str, hazard_type: str,
                      severity: float, evidence_root: Optional[str] = None,
                      zg_request_id: Optional[str] = None, *,
                      passable_for: Optional[list[str]] = None, **kw: Any) -> Future[dict[str, Any]]:
        f: Future[dict[str, Any]] = Future()
        with self._lock:
            hid = str(self._next_id)
            self._next_id += 1
            tx = self._make_tx("reportHazard", operator, hid)
            h = Hazard(
                id=hid,
                edge_id=edge_id,
                type=hazard_type,  # type: ignore[arg-type]
                severity=severity,
                status="reported",
                reporter_operator=operator,
                reporter_robot=robot_id,
                evidence_root=evidence_root,
                zg_request_id=zg_request_id,
                tx_hash=tx,
                passable_for=passable_for or ["drone"],
                confirms=0,
                disputes=0,
            )
            self._hazards[hid] = h
            self._pending_events.append({"event": "HazardReported", "hazard_id": hid, "hazard": h})
        f.set_result({"tx_hash": tx, "hazard_id": hid, "status": "mined"})
        return f

    def verify_hazard(self, operator: str, robot_id: str, hazard_id: str, confirm: bool,
                      evidence_root: Optional[str] = None,
                      zg_request_id: Optional[str] = None) -> Future[dict[str, Any]]:
        f: Future[dict[str, Any]] = Future()
        with self._lock:
            h = self._hazards.get(str(hazard_id))
            if not h:
                f.set_result({"status": "failed", "error": f"hazard {hazard_id} not found"})
                return f
            tx = self._make_tx("verifyHazard", operator, str(hazard_id))
            if confirm:
                h.confirms += 1
            else:
                h.disputes += 1
            self._pending_events.append({"event": "HazardVerified", "hazard_id": str(hazard_id),
                                         "confirm": confirm, "operator": operator})
            # check resolution
            if h.confirms >= self.confirms_needed:
                h.status = "confirmed"
                self._pending_events.append({"event": "HazardResolved", "hazard_id": str(hazard_id),
                                             "status": "confirmed", "hazard": h})
            elif h.disputes >= self.disputes_needed:
                h.status = "rejected"
                self._pending_events.append({"event": "HazardResolved", "hazard_id": str(hazard_id),
                                             "status": "rejected", "hazard": h})
        f.set_result({"tx_hash": tx, "hazard_id": str(hazard_id), "status": "mined",
                      "resolved": h.status if h.status != "reported" else None})
        return f

    def clear_hazard(self, operator: str, hazard_id: str, evidence_root: Optional[str] = None,
                     zg_request_id: Optional[str] = None) -> Future[dict[str, Any]]:
        f: Future[dict[str, Any]] = Future()
        with self._lock:
            h = self._hazards.get(str(hazard_id))
            if not h:
                f.set_result({"status": "failed", "error": f"hazard {hazard_id} not found"})
                return f
            tx = self._make_tx("clearHazard", operator, str(hazard_id))
            h.status = "cleared"
            self._pending_events.append({"event": "HazardCleared", "hazard_id": str(hazard_id),
                                         "hazard": h})
        f.set_result({"tx_hash": tx, "hazard_id": str(hazard_id), "status": "mined"})
        return f

    def poll_events(self) -> list[dict[str, Any]]:
        with self._lock:
            evs = list(self._pending_events)
            self._pending_events.clear()
            return evs

    def hazards(self) -> list[Hazard]:
        with self._lock:
            return [h.model_copy(deep=True) for h in self._hazards.values()]

    def hazard(self, hazard_id: str | int) -> Optional[Hazard]:
        with self._lock:
            h = self._hazards.get(str(hazard_id))
            return h.model_copy(deep=True) if h else None

    def start_polling(self, interval_s: float = 2.0) -> None:
        pass

    def stop_polling(self) -> None:
        pass


# ------------------------------------------------------------------ rover adapter for dispatcher
class RoverAdapter:
    """Adapts a Rover to the RoverLike protocol expected by ops.dispatcher.Dispatcher."""

    def __init__(self, rover: Rover, engine: "Engine"):
        self.rover = rover
        self.engine = engine
        self.id = rover.id
        self.operator = rover.operator

    def is_idle(self) -> bool:
        return (self.rover.fsm_state == FsmState.IDLE
                and not self.rover.paused
                and not self.rover.in_safe_stop)

    def xy(self) -> tuple[float, float]:
        x, y, *_ = self.rover.pose()
        return float(x), float(y)

    def assign(self, order: Order, store: dict, home: dict) -> None:
        self.engine.assign_order(self.rover, order, store, home)


# ------------------------------------------------------------------ main engine
class Engine:
    """Fixed-step simulation engine wiring all submodules."""

    def __init__(self, run_id: Optional[str] = None,
                 world_dir: Optional[str | Path] = None,
                 brain: Optional[Brain] = None,
                 storage: Optional[Storage] = None,
                 chain: Optional[Chain] = None,
                 enable_traffic: bool = False,
                 seed: Optional[int] = None,
                 realtime_factor: float = 0.0,
                 attach_to_server: bool = True) -> None:
        cfg = settings()
        self.cfg = cfg
        self.seed = int(cfg["sim"]["seed"] if seed is None else seed)
        self.rng = random.Random(self.seed)
        self.run_id = run_id or f"run-{int(time.time())}"
        self.sim_time = 0.0

        self.control_hz = float(cfg["sim"].get("control_hz", 20.0))
        self.dt = 1.0 / self.control_hz
        self.state_publish_hz = float(cfg["sim"].get("state_publish_hz", 10.0))
        self.publish_every = max(1, round(self.control_hz / self.state_publish_hz))
        self.checkpoint_trigger_dist_m = float(cfg["sim"].get("checkpoint_trigger_dist_m", 18.0))

        # clock
        self.clock = SimClock(control_hz=self.control_hz, realtime_factor=realtime_factor,
                              seed=self.seed)

        # world & road graph
        wd = Path(world_dir or ROOT / cfg["world"]["out_dir"])
        self.world_dir = wd
        boxes_path = wd / "collision" / "boxes.json"
        graph_path = wd / "road_graph.json"
        pois_path = wd / "pois.json"

        if not graph_path.exists():
            raise FileNotFoundError(f"road graph missing: {graph_path}")

        self.world = BodyWorld(str(boxes_path) if boxes_path.exists() else None,
                               road_graph=str(graph_path),
                               cfg=cfg.get("sim", {}))

        # load POIs and build edge mappings
        self.pois = json.loads(pois_path.read_text()) if pois_path.exists() else {"dark_stores": [], "homes": []}
        self.stores_by_op = {s["operator"]: s for s in self.pois.get("dark_stores", [])}
        self.homes_by_id = {h["id"]: h for h in self.pois.get("homes", [])}
        self.homes_by_bldg = {h.get("building_id"): h for h in self.pois.get("homes", []) if h.get("building_id")}

        # map road graph edges -> SUMO edges
        self.edge_to_sumo_edges: dict[str, list[str]] = {}
        for e in self.world.planner.edges.values():
            s_ids = e.get("sumo_edge_ids") or []
            if not s_ids and e.get("sumo_edge_id"):
                s_ids = [e["sumo_edge_id"]]
            self.edge_to_sumo_edges[e["id"]] = [str(s) for s in s_ids]

        # traffic bridge
        self.enable_traffic = enable_traffic
        self.traffic_bridge: Optional[Any] = None
        if enable_traffic:
            try:
                from traffic.bridge import TrafficBridge
                self.traffic_bridge = TrafficBridge()
                self.traffic_bridge.start(seed=int(cfg["traffic"].get("seed", 42)))
            except Exception as e:
                log.warning("TrafficBridge failed to start (%s); running without traffic", e)
                self.traffic_bridge = None
        self.last_actors: list[ActorState] = []

        # brain
        if brain is not None:
            self.brain: Brain = brain
        else:
            self.brain = get_brain()

        # storage & episode log
        if storage is not None:
            self.storage: Optional[Storage] = storage
        else:
            try:
                self.storage = ZGStorage()
            except Exception as e:
                log.warning("ZGStorage unavailable (%s); episode records stored locally only", e)
                self.storage = None

        self.episode_log = EpisodeLog(
            run_id=self.run_id,
            storage=self.storage,
            on_uploaded=self._on_storage_uploaded,
        )

        # chain
        if chain is not None:
            self.chain: Chain = chain
        else:
            self.chain = self._init_chain()

        # ops
        self.order_book = OrderBook(on_change=self._on_order_changed, seed=self.seed)
        self.dispatcher = Dispatcher(self.order_book)

        # state tracking
        self.props: dict[str, HazardProp] = {}
        self.reported_props: set[str] = set()
        self.verified_props: set[str] = set()
        self.blocked_edges: set[str] = set()
        self.robot_blocked_edges: dict[str, set[str]] = defaultdict(set)
        self.robot_decided_hazards: dict[str, set[str]] = defaultdict(set)
        self.robot_seq: dict[str, int] = defaultdict(int)
        self.delivery_state: dict[str, dict[str, Any]] = {}

        # pending drone verifications: list of dicts
        self.pending_verifications: list[dict[str, Any]] = []

        # scenario engine
        self.scenario: Optional[Any] = None

        # spawn robots
        self._spawn_robots()
        self.rover_adapters = [RoverAdapter(r, self) for r in self.world.robots.values()
                               if isinstance(r, Rover)]

        # server attachment
        if attach_to_server:
            attach_engine(self)

    # ------------------------------------------------------------------ initialization helpers
    def _init_chain(self) -> Chain:
        galileo_path = ROOT / "chain" / "deployments" / "galileo.json"
        anvil_path = ROOT / "chain" / "deployments" / "anvil.json"
        from chain.bindings import HazardChain

        if galileo_path.exists():
            try:
                hc = HazardChain.from_deployment("galileo", on_tx=self._on_chain_tx,
                                                 sim_time_fn=lambda: self.sim_time)
                if hc.w3.is_connected():
                    hc.start_polling()
                    return hc
            except Exception as e:
                log.warning("failed to connect to Galileo chain (%s)", e)

        if anvil_path.exists():
            try:
                hc = HazardChain.from_deployment("anvil", on_tx=self._on_chain_tx,
                                                 sim_time_fn=lambda: self.sim_time)
                if hc.w3.is_connected():
                    hc.start_polling()
                    return hc
            except Exception as e:
                log.warning("failed to connect to Anvil chain (%s)", e)

        log.info("using LocalHazardChain (in-memory)")
        return LocalHazardChain(on_tx=self._on_chain_tx, sim_time_fn=lambda: self.sim_time)

    def _spawn_robots(self) -> None:
        nodes = list(self.world.planner.G.nodes)
        # rovers per operator config (ZipKart: 3, MinuteMart: 3, DashKirana: 2)
        rover_n = 1
        for op in self.cfg.get("operators", []):
            op_name = op["name"]
            color = op["color"]
            store = self.stores_by_op.get(op_name)
            s_xy = store["entrance_xy"] if store else self.world.planner.nodes[self.rng.choice(nodes)]
            for k in range(op["rovers"]):
                rid = f"R-{rover_n:02d}"
                rover_n += 1
                # spread rovers slightly around dark store entrance
                offset_x = (k - 1) * 2.5
                xy = (s_xy[0] + offset_x, s_xy[1])
                self.world.add_rover(rid, op_name, xy, color=color, mode="physics")

        # drones per config (D-01: ZipKart, D-02: MinuteMart)
        for d in self.cfg.get("drones", []):
            did = d["id"]
            op_name = d["operator"]
            store = self.stores_by_op.get(op_name)
            d_xy = (store["x"] + 5.0, store["y"] + 5.0) if store else self.world.planner.nodes[self.rng.choice(nodes)]
            self.world.add_drone(did, op_name, d_xy)

    # ------------------------------------------------------------------ bus callbacks
    def _on_order_changed(self, order: Order) -> None:
        BUS.publish("order", self.sim_time, order)

    def _on_chain_tx(self, ev: ChainTxEvent) -> None:
        BUS.publish("chain_tx", self.sim_time, ev)

    def _on_storage_uploaded(self, ev: StorageEvent) -> None:
        BUS.publish("storage", self.sim_time, ev)

    def _next_seq(self, robot_id: str) -> int:
        self.robot_seq[robot_id] += 1
        return self.robot_seq[robot_id]

    def episode_id_for(self, robot_id: str) -> str:
        return f"{self.run_id}-{robot_id}"

    # ------------------------------------------------------------------ props
    def add_prop(self, prop: HazardProp) -> None:
        self.props[prop.id] = prop
        self.world.add_prop(prop)

    def remove_prop(self, prop_id: str) -> None:
        self.props.pop(prop_id, None)
        self.world.remove_prop(prop_id)

    # ------------------------------------------------------------------ edge blocking
    def block_edge(self, edge_id: str) -> None:
        self.blocked_edges.add(edge_id)

    def unblock_edge(self, edge_id: str) -> None:
        self.blocked_edges.discard(edge_id)
        for r_blocked in self.robot_blocked_edges.values():
            r_blocked.discard(edge_id)

    def block_edge_for_robot(self, robot_id: str, edge_id: str) -> None:
        self.robot_blocked_edges[robot_id].add(edge_id)

    def get_blocked_edges(self, robot: Rover) -> set[str]:
        return self.blocked_edges | self.robot_blocked_edges[robot.id]

    # ------------------------------------------------------------------ order assignment & lifecycle
    def assign_order(self, rover: Rover, order: Order, store: dict, home: dict) -> None:
        rover.order_id = order.id
        rx, ry, *_ = rover.pose()
        sx, sy = store["entrance_xy"]
        dist = math.hypot(rx - sx, ry - sy)

        self.delivery_state[rover.id] = {
            "order": order,
            "store": store,
            "home": home,
            "phase": "TO_STORE",
            "timer": 0.0,
        }

        if dist <= 8.0:
            # already at the dark store
            self.delivery_state[rover.id]["phase"] = "LOADING"
            self.delivery_state[rover.id]["timer"] = self.sim_time + 3.0
            rover.set_state(FsmState.LOADING, note="loading items at dark store")
            self.order_book.set_status(order.id, "picking", self.sim_time, robot_id=rover.id)
        else:
            rover.drive_to(store["entrance_xy"], goal_node=store.get("entrance_node"),
                           state=FsmState.TO_STORE,
                           blocked_edge_ids=self.get_blocked_edges(rover))
            rover.note = "heading to dark store"
            self.order_book.set_status(order.id, "assigned", self.sim_time, robot_id=rover.id)

    def _update_deliveries(self) -> None:
        for rover_id, dstate in list(self.delivery_state.items()):
            rover = self.world.robots.get(rover_id)
            if not isinstance(rover, Rover) or rover.paused:
                continue

            phase = dstate["phase"]
            order = dstate["order"]
            store = dstate["store"]
            home = dstate["home"]
            timer = dstate["timer"]
            rx, ry, *_ = rover.pose()

            if phase == "TO_STORE":
                sx, sy = store["entrance_xy"]
                if rover.arrived or math.hypot(rx - sx, ry - sy) < 4.0:
                    dstate["phase"] = "LOADING"
                    dstate["timer"] = self.sim_time + 3.0
                    rover.set_state(FsmState.LOADING, note="loading items at dark store")
                    self.order_book.set_status(order.id, "picking", self.sim_time, robot_id=rover.id)

            elif phase == "LOADING":
                if self.sim_time >= timer:
                    dstate["phase"] = "DELIVERING"
                    self.order_book.set_status(order.id, "out_for_delivery", self.sim_time,
                                               robot_id=rover.id)
                    rover.drive_to(home["entrance_xy"], goal_node=home.get("entrance_node"),
                                   state=FsmState.DELIVERING,
                                   blocked_edge_ids=self.get_blocked_edges(rover))
                    rover.note = f"delivering order {order.id}"

            elif phase == "DELIVERING":
                hx, hy = home["entrance_xy"]
                if rover.arrived or math.hypot(rx - hx, ry - hy) < 4.0:
                    dstate["phase"] = "AT_DOOR"
                    dstate["timer"] = self.sim_time + 2.5
                    rover.set_state(FsmState.AT_DOOR, note="at door (confirming OTP)")
                    self.order_book.set_status(order.id, "at_door", self.sim_time, robot_id=rover.id)

            elif phase == "AT_DOOR":
                if self.sim_time >= timer:
                    dstate["phase"] = "HANDED_OVER"
                    dstate["timer"] = self.sim_time + 1.0
                    rover.set_state(FsmState.HANDED_OVER, note="parcel handed over")
                    self.order_book.set_status(order.id, "delivered", self.sim_time, robot_id=rover.id)

            elif phase == "HANDED_OVER":
                if self.sim_time >= timer:
                    dstate["phase"] = "RETURNING"
                    rover.drive_to(store["entrance_xy"], goal_node=store.get("entrance_node"),
                                   state=FsmState.RETURNING,
                                   blocked_edge_ids=self.get_blocked_edges(rover))
                    rover.note = "returning to store"

            elif phase == "RETURNING":
                sx, sy = store["entrance_xy"]
                if rover.arrived or math.hypot(rx - sx, ry - sy) < 4.0:
                    rover.set_state(FsmState.IDLE, note="idle at store")
                    rover.order_id = None
                    del self.delivery_state[rover_id]

    # ------------------------------------------------------------------ checkpoints
    def _check_cp_h1(self) -> None:
        """CP-H1: Hazard encounter when a prop enters sensor range."""
        for r in self.world.robots.values():
            if not isinstance(r, Rover) or r.paused or r.in_safe_stop:
                continue
            if r.fsm_state not in (FsmState.DELIVERING, FsmState.TO_STORE, FsmState.RETURNING):
                continue

            rx, ry, _, ryaw, _ = r.pose()
            for prop in self.props.values():
                if prop.id in self.reported_props:
                    continue

                dist = math.hypot(rx - prop.x, ry - prop.y)
                if dist > self.checkpoint_trigger_dist_m:
                    continue

                # check if ahead of robot
                dx, dy = prop.x - rx, prop.y - ry
                angle = math.atan2(dy, dx)
                diff = abs((angle - ryaw + math.pi) % (2 * math.pi) - math.pi)
                upcoming = r.upcoming_edges(n=2)
                if diff > math.radians(80) and prop.edge_id not in upcoming:
                    continue

                self.reported_props.add(prop.id)
                self._trigger_cp_h1(r, prop, dist)
                break

    def _trigger_cp_h1(self, rover: Rover, prop: HazardProp, dist: float) -> None:
        try:
            render_bytes = self.world.camera_frame(rover.id)
        except Exception:
            render_bytes = None

        obs = build_observation(
            render_bytes,
            hazard_type=prop.type,
            real=prop.real,
            photo=prop.photo,
            key=prop.id,
            caption=f"Rover {rover.id} approaching {prop.type}",
        )
        seq = self._next_seq(rover.id)
        ep_id = self.episode_id_for(rover.id)

        req = CheckpointRequest(
            robot_id=rover.id,
            operator=rover.operator,
            robot_kind=rover.kind,
            checkpoint="CP-H1",
            sim_time=self.sim_time,
            episode_id=ep_id,
            seq=seq,
            frame_png_b64=obs.png_b64,
            context={
                "task": rover.fsm_state.value,
                "location": {"edge_id": prop.edge_id, "x": round(prop.x, 2), "y": round(prop.y, 2)},
                "detection": {"source": "lidar", "distance_m": round(dist, 1)},
                "_mock_truth": {"type": prop.type, "real": prop.real},
            },
        )

        def on_ok(robot: Any, res: CheckpointResult) -> None:
            out = res.output or {}
            if out.get("is_hazard"):
                htype = out.get("type", "accident")
                sev = float(out.get("severity", 0.8))
                passable = out.get("passable_for", ["drone"])
                self.chain.report_hazard(
                    operator=robot.operator,
                    robot_id=robot.id,
                    edge_id=prop.edge_id,
                    hazard_type=htype,
                    severity=sev,
                    evidence_root=obs.sha256,
                    zg_request_id=res.zg_request_id,
                    passable_for=passable,
                )
                self.schedule_verification(
                    prop,
                    reporter_op=robot.operator,
                    hazard_type=htype,
                    severity=sev,
                    zg_request_id=res.zg_request_id,
                    evidence_root=obs.sha256,
                )
                if "rover" not in passable or sev >= 0.5:
                    self.block_edge(prop.edge_id)
                    robot.replan(blocked_edge_ids=self.get_blocked_edges(robot))

        def on_result(robot: Any, res: CheckpointResult) -> None:
            action = ("report_hazard" if (res.ok and res.output and res.output.get("is_hazard"))
                      else ("proceed" if res.ok else "safe_stop"))
            rec = record_from_result(req, res, action_taken=action)
            self.episode_log.append(rec, frame_b64=obs.png_b64)
            BUS.publish("decision", self.sim_time, DecisionEvent(
                result=res,
                operator=robot.operator,
                sim_time=self.sim_time,
                action_taken=action,
                frame_thumb_b64=obs.thumb_jpeg_b64,
            ))

        rover.begin_checkpoint(self.brain, req, on_ok=on_ok, on_result=on_result, now=self.sim_time)

    def schedule_verification(self, prop: HazardProp, reporter_op: str, hazard_type: str,
                              severity: float, zg_request_id: Optional[str] = None,
                              evidence_root: Optional[str] = None) -> None:
        """Dispatch a drone from a different operator to verify the reported hazard."""
        rival_drones = [d for d in self.world.robots.values()
                        if isinstance(d, Drone) and d.operator != reporter_op and not d.paused]
        if not rival_drones:
            rival_drones = [d for d in self.world.robots.values() if isinstance(d, Drone) and not d.paused]
        if not rival_drones:
            log.warning("no drone available to verify hazard on %s", prop.edge_id)
            return

        drone = rival_drones[0]
        drone.fly_to((prop.x, prop.y), alt=40.0, state=FsmState.VERIFYING)
        drone.note = f"verifying {hazard_type} on {prop.edge_id}"
        self.pending_verifications.append({
            "drone_id": drone.id,
            "prop": prop,
            "reporter_op": reporter_op,
            "hazard_type": hazard_type,
            "severity": severity,
            "zg_request_id": zg_request_id,
            "evidence_root": evidence_root,
        })

    def _check_cp_v(self) -> None:
        """CP-V: Aerial or rival verification when drone reaches the reported spot."""
        for v in list(self.pending_verifications):
            drone = self.world.robots.get(v["drone_id"])
            if not isinstance(drone, Drone) or drone.paused or drone.in_safe_stop:
                continue

            prop = v["prop"]
            dx, dy, dz, *_ = drone.pose()
            dist_xy = math.hypot(dx - prop.x, dy - prop.y)
            if dist_xy <= 6.0 or drone.at_target:
                self.pending_verifications.remove(v)
                self.verified_props.add(prop.id)
                self._trigger_cp_v(drone, v)

    def _trigger_cp_v(self, drone: Drone, v_data: dict[str, Any]) -> None:
        prop: HazardProp = v_data["prop"]
        drone.hover()

        try:
            render_bytes = self.world.camera_frame(drone.id)
        except Exception:
            render_bytes = None

        obs = build_observation(
            render_bytes,
            hazard_type=prop.type,
            real=prop.real,
            photo=prop.photo,
            key=f"V-{prop.id}",
            caption=f"Drone {drone.id} verifying {v_data['hazard_type']}",
        )
        seq = self._next_seq(drone.id)
        ep_id = self.episode_id_for(drone.id)

        # find on-chain hazard id if available
        matching_hid = ""
        for hz in self.chain.hazards():
            if hz.edge_id == prop.edge_id:
                matching_hid = hz.id
                break

        req = CheckpointRequest(
            robot_id=drone.id,
            operator=drone.operator,
            robot_kind=drone.kind,
            checkpoint="CP-V",
            sim_time=self.sim_time,
            episode_id=ep_id,
            seq=seq,
            frame_png_b64=obs.png_b64,
            context={
                "claim": {
                    "hazard_id": matching_hid,
                    "edge_id": prop.edge_id,
                    "type": v_data["hazard_type"],
                    "severity": v_data["severity"],
                    "reporter_operator": v_data["reporter_op"],
                },
                "location": {"edge_id": prop.edge_id, "x": round(prop.x, 2), "y": round(prop.y, 2)},
                "_mock_truth": {"type": prop.type, "real": prop.real},
            },
        )

        def on_ok(robot: Any, res: CheckpointResult) -> None:
            out = res.output or {}
            verdict = out.get("verdict", "confirm")
            confirm = (verdict == "confirm")
            hid = matching_hid
            if not hid:
                for hz in self.chain.hazards():
                    if hz.edge_id == prop.edge_id:
                        hid = hz.id
                        break
            if hid:
                self.chain.verify_hazard(
                    operator=robot.operator,
                    robot_id=robot.id,
                    hazard_id=hid,
                    confirm=confirm,
                    evidence_root=obs.sha256,
                    zg_request_id=res.zg_request_id,
                )
            robot.return_home()
            robot.note = f"verification complete: {verdict}"

        def on_result(robot: Any, res: CheckpointResult) -> None:
            action = res.output.get("verdict", "confirm") if res.ok else "safe_stop"
            rec = record_from_result(req, res, action_taken=action)
            self.episode_log.append(rec, frame_b64=obs.png_b64)
            BUS.publish("decision", self.sim_time, DecisionEvent(
                result=res,
                operator=robot.operator,
                sim_time=self.sim_time,
                action_taken=action,
                frame_thumb_b64=obs.thumb_jpeg_b64,
            ))

        drone.begin_checkpoint(self.brain, req, on_ok=on_ok, on_result=on_result, now=self.sim_time)

    def _check_cp_h2(self) -> None:
        """CP-H2: Route-ahead decision when an active report lies on upcoming edges."""
        active_hazards = [h for h in self.chain.hazards() if h.status in ("reported", "confirmed")]
        if not active_hazards:
            return

        for r in self.world.robots.values():
            if not isinstance(r, Rover) or r.paused or r.in_safe_stop:
                continue
            if not r.accepts_unverified_reports:
                continue

            upcoming = r.upcoming_edges(n=3)
            if not upcoming and r.route_edge_ids:
                upcoming = r.route_edge_ids[:3]
            for h in active_hazards:
                if h.edge_id in upcoming and h.id not in self.robot_decided_hazards[r.id]:
                    self.robot_decided_hazards[r.id].add(h.id)
                    self._trigger_cp_h2(r, h)
                    break

    def _trigger_cp_h2(self, rover: Rover, h: Hazard) -> None:
        seq = self._next_seq(rover.id)
        ep_id = self.episode_id_for(rover.id)

        req = CheckpointRequest(
            robot_id=rover.id,
            operator=rover.operator,
            robot_kind=rover.kind,
            checkpoint="CP-H2",
            sim_time=self.sim_time,
            episode_id=ep_id,
            seq=seq,
            frame_png_b64=None,
            context={
                "report": {
                    "hazard_id": h.id,
                    "edge_id": h.edge_id,
                    "type": h.type,
                    "severity": h.severity,
                    "status": h.status,
                    "passable_for": h.passable_for,
                },
                "confirms": h.confirms,
                "disputes": h.disputes,
                "reporter_operator": h.reporter_operator,
                "my_operator": rover.operator,
                "urgency": "normal",
                "task": rover.fsm_state.value,
                "_mock_truth": {"type": h.type, "real": True},
            },
        )

        def on_ok(robot: Any, res: CheckpointResult) -> None:
            out = res.output or {}
            action = out.get("action", "reroute")
            if action == "reroute":
                self.block_edge_for_robot(robot.id, h.edge_id)
                robot.replan(blocked_edge_ids=self.get_blocked_edges(robot))
                robot.note = f"rerouted around {h.edge_id}"
            elif action == "wait":
                robot.note = "waiting at checkpoint"
            elif action == "proceed":
                robot.note = "proceeding past report"

        def on_result(robot: Any, res: CheckpointResult) -> None:
            action = res.output.get("action", "reroute") if res.ok else "safe_stop"
            rec = record_from_result(req, res, action_taken=action)
            self.episode_log.append(rec, frame_b64=None)
            BUS.publish("decision", self.sim_time, DecisionEvent(
                result=res,
                operator=robot.operator,
                sim_time=self.sim_time,
                action_taken=action,
                frame_thumb_b64=None,
            ))

        rover.begin_checkpoint(self.brain, req, on_ok=on_ok, on_result=on_result, now=self.sim_time)

    # ------------------------------------------------------------------ chain events & SUMO sync
    def _process_chain_events(self) -> None:
        for ev in self.chain.poll_events():
            ev_name = ev.get("event")
            hid = str(ev.get("hazard_id") or "")
            hz = self.chain.hazard(hid)
            if not hz:
                continue

            BUS.publish("hazard", self.sim_time, hz)

            if ev_name == "HazardResolved" or hz.status in ("confirmed", "rejected", "cleared"):
                if hz.status == "confirmed":
                    self.block_edge(hz.edge_id)
                    # close SUMO lanes
                    for se in self.edge_to_sumo_edges.get(hz.edge_id, []):
                        if self.traffic_bridge:
                            self.traffic_bridge.close_edge(se)
                    # reroute rovers currently on that edge
                    for r in self.world.robots.values():
                        if isinstance(r, Rover) and hz.edge_id in r.route_edge_ids:
                            r.replan(blocked_edge_ids=self.get_blocked_edges(r))

                elif hz.status in ("rejected", "cleared"):
                    self.unblock_edge(hz.edge_id)
                    # reopen SUMO lanes
                    for se in self.edge_to_sumo_edges.get(hz.edge_id, []):
                        if self.traffic_bridge:
                            self.traffic_bridge.reopen_edge(se)

    # ------------------------------------------------------------------ step loop
    def step(self, dt: Optional[float] = None) -> None:
        """Advance one fixed control tick."""
        dt = float(dt or self.dt)
        self.sim_time += dt

        # 1. SUMO traffic step & mirror to PyBullet
        if self.traffic_bridge:
            try:
                for r in self.world.robots.values():
                    if isinstance(r, Rover):
                        x, y, _, yaw, _ = r.pose()
                        self.traffic_bridge.upsert_robot(r.id, x, y, yaw)
                self.last_actors = self.traffic_bridge.step(self.sim_time)
                self.world.upsert_kinematic_actors(self.last_actors)
            except Exception as e:
                log.warning("traffic step error: %s", e)

        # 2. Scenario step
        if self.scenario is not None:
            try:
                self.scenario.step(self.sim_time)
            except Exception as e:
                log.exception("scenario error: %s", e)

        # 3. Auto demand
        self.order_book.auto_demand(self.sim_time, rate_per_min=10.0, dt=dt)

        # 4. Dispatch orders to idle rovers
        self.dispatcher.tick(self.rover_adapters, self.sim_time)

        # 5. Advance active deliveries
        self._update_deliveries()

        # 6. Checkpoint triggers
        self._check_cp_h1()
        self._check_cp_v()
        self._check_cp_h2()

        # 7. Robot control ticks & physics step
        for r in self.world.robots.values():
            r.tick(self.sim_time)
        self.world.step(dt)

        # 8. Poll chain events
        self._process_chain_events()

        # 9. 10 Hz state snapshot
        if self.clock.every(1.0 / self.state_publish_hz):
            snap = StateSnapshot(
                sim_time=round(self.sim_time, 2),
                robots=[r.state() for r in self.world.robots.values()],
                actors=self.last_actors,
                hazards=self.chain.hazards(),
                props=list(self.props.values()),
                brain_online=self.brain.online(),
            )
            BUS.publish("state", self.sim_time, snap)

    # ------------------------------------------------------------------ EngineControl protocol
    def place_order(self, operator: str, building_id: str, items: list[str],
                    customer_name: str) -> dict[str, Any]:
        home = self.homes_by_bldg.get(building_id) or self.homes_by_id.get(building_id)
        if not home:
            # pick random home if building_id unknown
            home = next(iter(self.homes_by_id.values()))
        order = self.order_book.place(operator, home["id"], items, customer_name, self.sim_time)
        return order.model_dump(mode="json")

    def set_brain_unreachable(self, on: bool) -> None:
        self.brain.set_unreachable(on)
        if on:
            for r in self.world.robots.values():
                if r.paused:
                    r.enter_safe_stop("0G brain unreachable (sim toggle)")

    def snapshot_meta(self) -> dict[str, Any]:
        return {
            "engine": True,
            "run_id": self.run_id,
            "sim_time": round(self.sim_time, 2),
            "brain_online": self.brain.online(),
            "operators": [op["name"] for op in self.cfg.get("operators", [])],
            "order_stats": self.order_book.stats(),
            "hazards": [h.model_dump(mode="json") for h in self.chain.hazards()],
            "props_count": len(self.props),
        }

    # ------------------------------------------------------------------ execution
    def run(self, duration_s: Optional[float] = None,
            until: Optional[Callable[[], bool]] = None) -> None:
        """Run the simulation loop until duration_s or until() is True."""
        t_end = self.sim_time + duration_s if duration_s is not None else math.inf
        while self.sim_time < t_end:
            if until is not None and until():
                break
            self.clock.tick()
            self.step()

    def close(self) -> None:
        """Clean shutdown."""
        if self.traffic_bridge:
            try:
                self.traffic_bridge.close()
            except Exception:
                pass
        self.world.close()
        self.episode_log.flush_all()
        if hasattr(self.chain, "close"):
            self.chain.close()
