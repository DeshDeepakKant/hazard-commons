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
from sim.inquiry import InquiryManager, start_cp_q
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

from common.config import is_demo

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
        elif os.environ.get("HC_STORAGE", "").lower() == "off":
            log.info("HC_STORAGE=off: episode records stored locally only")
            self.storage = None
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
        self.inquiries = InquiryManager(
            publish=lambda d: BUS.publish("inquiry", self.sim_time, d),
            start_cp=lambda p, a, s: start_cp_q(self, p, a, s))
        self.verified_props: set[str] = set()
        self.blocked_edges: set[str] = set()
        # user-injected disasters ("Add a disaster"): queued by API threads, applied in step()
        self._inject_q: list[tuple[Callable[[], Any], "Future[Any]"]] = []
        self._inject_lock = threading.Lock()
        self._sim_thread: Optional[int] = None
        self._inject_seq = 0
        self.injected: dict[str, dict[str, Any]] = {}
        self.robot_blocked_edges: dict[str, set[str]] = defaultdict(set)
        self.robot_decided_hazards: dict[str, set[str]] = defaultdict(set)
        self.robot_seq: dict[str, int] = defaultdict(int)
        self.delivery_state: dict[str, dict[str, Any]] = {}

        # Hazards reported by THIS run (hazard ids from our own report futures). A persistent
        # chain (Galileo) replays every hazard since deployment; old runs' reports must not
        # trigger CP-H2/CP-V, block edges or show up in this run's snapshots.
        self._run_hazard_lock = threading.Lock()
        self.run_hazard_ids: set[str] = set()
        self._deferred_chain_events: list[tuple[float, dict[str, Any]]] = []

        # verification queue (waiting for a free rival drone) and dispatched verifications
        self.verification_queue: list[dict[str, Any]] = []
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
        """Chain selection. `HC_CHAIN=local|anvil|galileo` forces one backend (dev/tests);
        unset = auto (Galileo if deployed, else Anvil, else in-memory). DEMO=1 always requires
        Galileo and refuses any other HC_CHAIN value."""
        galileo_path = ROOT / "chain" / "deployments" / "galileo.json"
        anvil_path = ROOT / "chain" / "deployments" / "anvil.json"
        forced = (os.environ.get("HC_CHAIN") or "").strip().lower()
        if forced not in ("", "local", "anvil", "galileo"):
            raise ValueError(f"HC_CHAIN must be local|anvil|galileo, got {forced!r}")
        if is_demo() and forced not in ("", "galileo"):
            raise RuntimeError(f"DEMO=1 requires the Galileo chain; HC_CHAIN={forced!r} is refused.")
        if forced == "local":
            log.info("HC_CHAIN=local: using LocalHazardChain (in-memory)")
            return LocalHazardChain(on_tx=self._on_chain_tx, sim_time_fn=lambda: self.sim_time)
        from chain.bindings import HazardChain
        if forced == "anvil":
            hc = HazardChain.from_deployment("anvil", on_tx=self._on_chain_tx,
                                             sim_time_fn=lambda: self.sim_time)
            if not hc.w3.is_connected():
                raise RuntimeError("HC_CHAIN=anvil but anvil RPC is not reachable")
            hc.start_polling()
            return hc

        if galileo_path.exists():
            for attempt in range(1, 6):   # the public RPC blips: retry before giving up
                try:
                    hc = HazardChain.from_deployment("galileo", on_tx=self._on_chain_tx,
                                                     sim_time_fn=lambda: self.sim_time)
                    if hc.w3.is_connected():
                        hc.start_polling()
                        return hc
                    log.warning("Galileo RPC not reachable (attempt %d/5)", attempt)
                except Exception as e:
                    log.warning("failed to connect to Galileo chain (attempt %d/5): %s", attempt, e)
                time.sleep(3)

        if is_demo():
            raise RuntimeError(
                "DEMO=1 requires the HazardRegistry deployed on 0G Galileo "
                "(chain/deployments/galileo.json). Anvil / in-memory chains are dev-only and are "
                "refused so no local transaction can appear in the demo. Fund the deployer and run "
                "chain/deploy_galileo.sh.")

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

        # drones per config (D-01: ZipKart, D-02: MinuteMart). The pad must be OUTSIDE every
        # building collision box: the store centroid is inside the store building, and a drone
        # spawned there can never take off (that is why no CP-V ever happened).
        for d in self.cfg.get("drones", []):
            did = d["id"]
            op_name = d["operator"]
            store = self.stores_by_op.get(op_name)
            if d.get("base_xy"):
                d_xy = self._free_pad_near([float(v) for v in d["base_xy"]], min_r=0.0)
            elif store:
                d_xy = self._free_pad_near(store["entrance_xy"])
            else:
                d_xy = self.world.planner.nodes[self.rng.choice(nodes)]
            self.world.add_drone(did, op_name, d_xy)

    def _free_pad_near(self, xy: Sequence[float], min_r: float = 7.0, max_r: float = 30.0,
                       margin: float = 2.0) -> tuple[float, float]:
        """Deterministic take-off pad near `xy`: first point on growing rings that is clear of
        all building boxes (with margin) and >= min_r from the rover spawn line."""
        boxes = []
        bp = self.world_dir / "collision" / "boxes.json"
        if bp.exists():
            for b in json.loads(bp.read_text()):
                cx, cy, _ = b["center"]
                if abs(cx - xy[0]) < max_r + 60 and abs(cy - xy[1]) < max_r + 60:
                    boxes.append(b)

        def clear(px: float, py: float) -> bool:
            for b in boxes:
                cx, cy, _ = b["center"]
                hx, hy, _ = b["half_extents"]
                yaw = float(b.get("yaw", 0.0))
                dx, dy = px - cx, py - cy
                lx = dx * math.cos(yaw) + dy * math.sin(yaw)
                ly = -dx * math.sin(yaw) + dy * math.cos(yaw)
                if abs(lx) < hx + margin and abs(ly) < hy + margin:
                    return False
            return True

        if min_r <= 0.0 and clear(float(xy[0]), float(xy[1])):
            return (float(xy[0]), float(xy[1]))
        r = max(min_r, 2.0)
        while r <= max_r:
            for k in range(16):
                a = math.pi / 2 + k * (2 * math.pi / 16)
                px, py = xy[0] + r * math.cos(a), xy[1] + r * math.sin(a)
                if clear(px, py):
                    return (px, py)
            r += 2.0
        return (float(xy[0]), float(xy[1]) + min_r)

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

    # ------------------------------------------------------------------ injected disasters
    INJECT_TYPES = ("accident", "road_works", "waterlogging")
    INJECT_PHOTOS = {"accident": "accident_1.jpg", "waterlogging": "waterlogging_1.jpg",
                     "road_works": None}   # same choices as sim/scenario.py

    def _on_sim_thread(self) -> bool:
        """True when applying directly is safe: caller is the sim thread, or no loop has run."""
        return self._sim_thread is None or self._sim_thread == threading.get_ident()

    def _run_on_sim(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run fn inside step() (PyBullet/SUMO are not thread-safe); re-raises its exception."""
        if self._on_sim_thread():
            return fn()
        fut: "Future[Any]" = Future()
        with self._inject_lock:
            self._inject_q.append((fn, fut))
        return fut.result(timeout=timeout)

    def _drain_injections(self) -> None:
        with self._inject_lock:
            q, self._inject_q = self._inject_q, []
        for fn, fut in q:
            try:
                fut.set_result(fn())
            except Exception as e:   # noqa: BLE001 - handed back to the API caller
                fut.set_exception(e)

    def nearest_drive_edge(self, x: float, y: float) -> tuple[str, float, float, float]:
        """Snap (x, y) to the nearest drive edge: (edge_id, px, py, dist_m). The point is moved
        onto the rovers' driving line so the prop sits where rovers actually drive."""
        import numpy as np
        pl = self.world.planner
        best: tuple[float, str, float, float] = (1e18, "", 0.0, 0.0)
        p = np.array([x, y], dtype=float)
        for eid, e in pl.edges.items():
            sh = e["shape"]
            if len(sh) < 2:
                continue
            a, b = sh[:-1], sh[1:]
            ab = b - a
            t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0, 1)
            q = a + t[:, None] * ab
            d = np.linalg.norm(q - p, axis=1)
            i = int(np.argmin(d))
            if d[i] < best[0]:
                n = np.array([-ab[i][1], ab[i][0]]) / max(float(np.linalg.norm(ab[i])), 1e-9)
                q2 = q[i] + n * pl.half_width(eid) * pl.offset_frac
                best = (float(d[i]), eid, float(q2[0]), float(q2[1]))
        if not best[1]:
            raise ValueError("no drive edge in the road graph")
        return best[1], best[2], best[3], best[0]

    def inject_hazard(self, type: str, edge_id: Optional[str] = None, x: Optional[float] = None,
                      y: Optional[float] = None, real: bool = True,
                      severity: Optional[float] = None,
                      reporter_op: str = "DashKirana") -> dict[str, Any]:
        """Place a user disaster as a HazardProp on the nearest drive edge. Real: a physical
        prop; rovers meet it and run the normal CP-H1 (0G) -> report -> CP-V -> CP-H2 flow.
        Not real: nothing physical; `reporter_op` posts a FAKE report (no 0G call, honestly
        marked) that rival drones verify and dispute. The edge is NOT pre-blocked: blocking comes
        from the genuine report/verify flow, as for the scripted hazards."""
        if type not in self.INJECT_TYPES:
            raise ValueError(f"type must be one of {self.INJECT_TYPES}")
        if edge_id is None and (x is None or y is None):
            raise ValueError("give edge_id or x and y")
        if severity is not None and not 0.0 <= float(severity) <= 1.0:
            raise ValueError("severity must be within 0..1")
        if edge_id is not None and edge_id not in self.world.planner.edges:
            raise ValueError(f"unknown drive edge {edge_id}")
        return self._run_on_sim(lambda: self._inject_now(type, edge_id, x, y, bool(real),
                                                        severity, reporter_op))

    def _inject_now(self, type: str, edge_id: Optional[str], x: Optional[float],
                    y: Optional[float], real: bool, severity: Optional[float],
                    reporter_op: str) -> dict[str, Any]:
        pl = self.world.planner
        snapped = 0.0
        if x is not None and y is not None:
            if edge_id is None:
                edge_id, px, py, snapped = self.nearest_drive_edge(float(x), float(y))
            else:
                if edge_id not in pl.edges:
                    raise ValueError(f"unknown drive edge {edge_id}")
                px, py = float(x), float(y)
        else:
            if edge_id not in pl.edges:
                raise ValueError(f"unknown drive edge {edge_id}")
            sh = pl.edge_shape(edge_id)
            _, px, py, _ = self.nearest_drive_edge(*(float(v) for v in sh[len(sh) // 2]))
        self._inject_seq += 1
        pid = f"P-user-{self._inject_seq}"
        while pid in self.props:
            self._inject_seq += 1
            pid = f"P-user-{self._inject_seq}"
        prop = HazardProp(id=pid, edge_id=edge_id, type=type, x=px, y=py, real=real,
                          photo=self.INJECT_PHOTOS[type] if real else None)
        self.add_prop(prop)
        sev = float(severity) if severity is not None else 0.75
        meta = {"prop_id": pid, "edge_id": edge_id, "road_name": pl.edges[edge_id].get("name") or "",
                "type": type, "real": real, "x": round(px, 2), "y": round(py, 2),
                "snapped_m": round(snapped, 1), "injected_at": round(self.sim_time, 1),
                "severity": sev, "reporter_op": None}
        if not real:
            ops = [r for r in self.world.robots.values()
                   if r.kind == "rover" and r.operator == reporter_op]
            rid = sorted(r.id for r in ops)[0] if ops else "R-07"
            fut = self.report_hazard(operator=reporter_op, robot_id=rid, edge_id=edge_id,
                                     hazard_type=type, severity=sev, evidence_root=None,
                                     zg_request_id="FAKE-NO-0G-CALL", passable_for=[])
            self.schedule_verification(prop, reporter_op=reporter_op, hazard_type=type,
                                       severity=sev, zg_request_id="FAKE-NO-0G-CALL",
                                       report_future=fut)
            meta["reporter_op"] = reporter_op
        self.injected[pid] = meta
        log.warning("[inject] %s %s on %s (%s) at (%.1f, %.1f)", "REAL" if real else "FAKE",
                    type, edge_id, meta["road_name"], px, py)
        return {"prop_id": pid, "edge_id": edge_id, "road_name": meta["road_name"], "type": type,
                "real": real, "x": meta["x"], "y": meta["y"], "snapped_m": meta["snapped_m"]}

    def inject_random(self, real: bool = True, type: Optional[str] = None) -> dict[str, Any]:
        """Random disaster, preferring an edge ahead on a moving rover's route (visible effect)."""
        if type is not None and type not in self.INJECT_TYPES:
            raise ValueError(f"type must be one of {self.INJECT_TYPES}")
        return self._run_on_sim(lambda: self._inject_random_now(real, type))

    def _inject_random_now(self, real: bool, type: Optional[str]) -> dict[str, Any]:
        from sim.scenario import MOVING, along, edge_spans, point_at
        rnd = random.SystemRandom()
        htype = type or rnd.choice(self.INJECT_TYPES)
        used = {p.edge_id for p in self.props.values()}
        edges = self.world.planner.edges
        cands: list[tuple[str, float, float]] = []
        for r in self.world.robots.values():
            if not isinstance(r, Rover) or r.route is None or r.arrived or r.paused \
                    or r.fsm_state not in MOVING:
                continue
            x, y, *_ = r.pose()
            s_r = along(r.route, (x, y))
            for eid, s0, s1 in edge_spans(r.route):
                if eid in used or eid not in edges or (s1 - s0) < 15.0 or s0 < s_r + 25.0 \
                        or s0 > s_r + 300.0:
                    continue
                px, py = point_at(r.route, s0 + min(8.0, (s1 - s0) / 2))
                cands.append((eid, px, py))
        if cands:
            eid, px, py = rnd.choice(cands)
            return self._inject_now(htype, eid, px, py, real, None, "DashKirana")
        pool = [e for e, v in edges.items() if e not in used and v["length"] >= 20]
        if not pool:
            raise ValueError("no free drive edge available")
        return self._inject_now(htype, rnd.choice(pool), None, None, real, None, "DashKirana")

    def clear_injected(self, prop_id: str) -> dict[str, Any]:
        """Remove a user-injected disaster. A confirmed on-chain hazard on its edge is cleared
        through the chain (reopens SUMO and unblocks via the normal event path); otherwise the
        edge is unblocked directly."""
        if prop_id not in self.injected:
            raise KeyError(prop_id)
        return self._run_on_sim(lambda: self._clear_now(prop_id))

    def _clear_now(self, prop_id: str) -> dict[str, Any]:
        meta = self.injected.pop(prop_id, None)
        if meta is None:
            raise KeyError(prop_id)
        self.remove_prop(prop_id)
        self.reported_props.discard(prop_id)
        self.verification_queue[:] = [v for v in self.verification_queue if v["prop"].id != prop_id]
        eid = meta["edge_id"]
        cleared = []
        for h in self.run_hazards():
            if h.edge_id == eid and h.status == "confirmed":
                self.chain.clear_hazard(operator=h.reporter_operator or "MinuteMart", hazard_id=h.id)
                cleared.append(h.id)
        if not cleared and not any(p.edge_id == eid for p in self.props.values()):
            self.unblock_edge(eid)
            if self.traffic_bridge:
                for se in self.edge_to_sumo_edges.get(eid, []):
                    self.traffic_bridge.reopen_edge(se)
        return {"ok": True, "prop_id": prop_id, "edge_id": eid, "cleared_hazard_ids": cleared}

    def list_props(self) -> list[dict[str, Any]]:
        hz: dict[str, Hazard] = {}
        for h in self.run_hazards():
            hz.setdefault(h.edge_id, h)
        out = []
        for p in list(self.props.values()):
            h = hz.get(p.edge_id)
            m = self.injected.get(p.id, {})
            out.append({"prop_id": p.id, "type": p.type, "edge_id": p.edge_id, "x": round(p.x, 2),
                        "y": round(p.y, 2), "real": p.real, "injected": p.id in self.injected,
                        "road_name": m.get("road_name")
                        or (self.world.planner.edges.get(p.edge_id, {}).get("name") or ""),
                        "reported": p.id in self.reported_props or (not p.real and bool(m)),
                        "hazard_id": h.id if h else None, "hazard_status": h.status if h else None})
        return out

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

    # ------------------------------------------------------------------ run-scoped hazards
    def report_hazard(self, operator: str, robot_id: str, edge_id: str, hazard_type: str,
                      severity: float, evidence_root: Optional[str] = None,
                      zg_request_id: Optional[str] = None,
                      passable_for: Optional[list[str]] = None) -> "Future[dict[str, Any]]":
        """reportHazard via the chain + remember the hazard id as belonging to this run."""
        fut = self.chain.report_hazard(operator=operator, robot_id=robot_id, edge_id=edge_id,
                                       hazard_type=hazard_type, severity=severity,
                                       evidence_root=evidence_root, zg_request_id=zg_request_id,
                                       passable_for=passable_for)

        def _done(f: "Future[dict[str, Any]]") -> None:
            try:
                res = f.result()
            except Exception:
                return
            hid = res.get("hazard_id") if res.get("status") == "mined" else None
            if hid is not None:
                with self._run_hazard_lock:
                    self.run_hazard_ids.add(str(hid))
            else:
                log.warning("reportHazard on %s failed: %s", edge_id, res.get("error"))

        fut.add_done_callback(_done)
        return fut

    def is_run_hazard(self, hazard_id: Any) -> bool:
        with self._run_hazard_lock:
            return str(hazard_id) in self.run_hazard_ids

    def run_hazards(self) -> list[Hazard]:
        """Hazards reported during this run (the live shared map for this episode)."""
        with self._run_hazard_lock:
            ids = set(self.run_hazard_ids)
        return [h for h in self.chain.hazards() if h.id in ids]

    def retire_run_hazards(self, extra_edges: Sequence[str] = ()) -> list[str]:
        """Scenario loop reset (no chain tx): forget every hazard reported so far in this run so
        it no longer drives CP-H2 or shows in snapshots; unblock its edge and reopen the SUMO
        closure. `extra_edges` (e.g. the old props' edges) are unblocked/reopened too.
        Returns the retired hazard ids."""
        with self._run_hazard_lock:
            ids = sorted(self.run_hazard_ids)
            self.run_hazard_ids.clear()
        edges = list(extra_edges)
        for hid in ids:
            hz = self.chain.hazard(hid)
            if hz is not None:
                edges.append(hz.edge_id)
        for eid in dict.fromkeys(edges):
            self.unblock_edge(eid)
            if self.traffic_bridge:
                for se in self.edge_to_sumo_edges.get(eid, []):
                    self.traffic_bridge.reopen_edge(se)
        return ids

    # ------------------------------------------------------------------ checkpoints
    def _check_cp_h1(self) -> None:
        """CP-H1: Hazard encounter when a PHYSICAL prop enters sensor range (fake reports have
        nothing physical, so no sensor ever fires on them)."""
        for r in self.world.robots.values():
            if not isinstance(r, Rover) or r.paused or r.in_safe_stop:
                continue
            if r.fsm_state not in (FsmState.DELIVERING, FsmState.TO_STORE, FsmState.RETURNING):
                continue

            rx, ry, _, ryaw, _ = r.pose()
            for prop in self.props.values():
                if prop.id in self.reported_props or not prop.real:
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
            if not out.get("is_hazard"):
                robot.note = "0G: no hazard, proceeding"
                return
            htype = out.get("type") or prop.type
            if htype == "none":
                htype = prop.type
            sev = float(out.get("severity", 0.8))
            passable = list(out.get("passable_for") or [])
            fut = self.report_hazard(
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
                report_future=fut,
            )
            if "rover" not in passable:
                # 0G says this body type cannot pass: the reporter reroutes around it itself
                # (others decide for themselves at CP-H2 once a rival has verified it).
                self.block_edge_for_robot(robot.id, prop.edge_id)
                robot.replan(blocked_edge_ids=self.get_blocked_edges(robot))
                robot.note = (f"0G: {htype} impassable for rovers"
                              f"{' (drone OK)' if 'drone' in passable else ''}; reported + rerouted")
            else:
                robot.note = f"0G: {htype} passable for rovers; reported, proceeding"
            # voice inquiry: ask bystanders (viewer speaker + mic); answers become a CP-Q call
            self.inquiries.open(robot.id, {
                "type": htype, "severity": sev, "edge_id": prop.edge_id,
                "description": str(out.get("description") or "")[:200],
                "location": {"x": round(prop.x, 2), "y": round(prop.y, 2)},
            }, frame_png_b64=obs.png_b64,
                private={"thumb": obs.thumb_jpeg_b64, "full": obs.frame_jpeg_b64 or None})
            robot.note = f"asking bystanders (hi) · {robot.note}"

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
                frame_full_b64=obs.frame_jpeg_b64 or None,
            ))

        rover.begin_checkpoint(self.brain, req, on_ok=on_ok, on_result=on_result, now=self.sim_time)

    def schedule_verification(self, prop: HazardProp, reporter_op: str, hazard_type: str,
                              severity: float, zg_request_id: Optional[str] = None,
                              evidence_root: Optional[str] = None,
                              report_future: Optional[Future] = None,
                              hazard_id: Optional[str] = None) -> None:
        """Queue a verification of a report. `_dispatch_verifications` sends the nearest free
        drone of a DIFFERENT operator than the reporter (contract rule) as soon as one exists."""
        self.verification_queue.append({
            "prop": prop,
            "reporter_op": reporter_op,
            "hazard_type": hazard_type,
            "severity": severity,
            "zg_request_id": zg_request_id,
            "evidence_root": evidence_root,
            "report_future": report_future,
            "hazard_id": hazard_id,
            "queued_at": self.sim_time,
        })
        self._dispatch_verifications()

    def _busy_drones(self) -> set[str]:
        return {v["drone_id"] for v in self.pending_verifications}

    def _dispatch_verifications(self) -> None:
        if not self.verification_queue:
            return
        busy = self._busy_drones()
        for v in list(self.verification_queue):
            prop = v["prop"]
            cands = [d for d in self.world.robots.values()
                     if isinstance(d, Drone) and d.operator != v["reporter_op"]
                     and d.id not in busy and not d.paused]
            if not cands:
                continue
            drone = min(cands, key=lambda d: math.hypot(d.pose()[0] - prop.x, d.pose()[1] - prop.y))
            busy.add(drone.id)
            self.verification_queue.remove(v)
            drone.fly_to((prop.x, prop.y), alt=40.0, state=FsmState.VERIFYING)
            drone.note = f"verifying {v['reporter_op']}'s {v['hazard_type']} report on {prop.edge_id}"
            v["drone_id"] = drone.id
            v["dispatched_at"] = self.sim_time
            self.pending_verifications.append(v)
            log.info("dispatched %s (%s) to verify %s's %s on %s", drone.id, drone.operator,
                     v["reporter_op"], v["hazard_type"], prop.edge_id)

    def _verification_hazard_id(self, v: dict[str, Any]) -> Optional[str]:
        """Hazard id of the report being verified: from our own report future (never from an
        edge-id lookup, which could match an older report on the same edge)."""
        if v.get("hazard_id"):
            return str(v["hazard_id"])
        fut = v.get("report_future")
        if fut is None or not fut.done():
            return None
        try:
            res = fut.result()
        except Exception:
            res = {"status": "failed"}
        if res.get("status") != "mined" or res.get("hazard_id") is None:
            v["failed"] = res.get("error") or "report failed"
            return None
        v["hazard_id"] = str(res["hazard_id"])
        return v["hazard_id"]

    def _check_cp_v(self) -> None:
        """CP-V: Aerial or rival verification when drone reaches the reported spot."""
        for v in list(self.pending_verifications):
            drone = self.world.robots.get(v["drone_id"])
            if not isinstance(drone, Drone) or drone.paused or drone.in_safe_stop:
                continue

            hid = self._verification_hazard_id(v)
            if v.get("failed"):
                log.warning("verification of %s dropped: %s", v["prop"].edge_id, v["failed"])
                self.pending_verifications.remove(v)
                drone.return_home()
                continue

            prop = v["prop"]
            dx, dy, dz, *_ = drone.pose()
            dist_xy = math.hypot(dx - prop.x, dy - prop.y)
            if hid is not None and (dist_xy <= 6.0 or drone.at_target):
                self.pending_verifications.remove(v)
                self.verified_props.add(prop.id)
                self._trigger_cp_v(drone, v, hid)

    def _trigger_cp_v(self, drone: Drone, v_data: dict[str, Any], hazard_id: str) -> None:
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
                    "hazard_id": hazard_id,
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
            verdict = out.get("verdict")
            if verdict in ("confirm", "dispute"):
                self.chain.verify_hazard(
                    operator=robot.operator,
                    robot_id=robot.id,
                    hazard_id=hazard_id,
                    confirm=(verdict == "confirm"),
                    evidence_root=obs.sha256,
                    zg_request_id=res.zg_request_id,
                )
            robot.return_home()
            robot.note = f"0G verdict on hazard #{hazard_id}: {verdict}"

        def on_result(robot: Any, res: CheckpointResult) -> None:
            action = (res.output or {}).get("verdict", "none") if res.ok else "safe_stop"
            rec = record_from_result(req, res, action_taken=action)
            self.episode_log.append(rec, frame_b64=obs.png_b64)
            BUS.publish("decision", self.sim_time, DecisionEvent(
                result=res,
                operator=robot.operator,
                sim_time=self.sim_time,
                action_taken=action,
                frame_thumb_b64=obs.thumb_jpeg_b64,
                frame_full_b64=obs.frame_jpeg_b64 or None,
            ))

        drone.begin_checkpoint(self.brain, req, on_ok=on_ok, on_result=on_result, now=self.sim_time)

    @staticmethod
    def remaining_route_edges(r: Rover) -> list[str]:
        """Edges from the rover's current edge to the end of its route."""
        if r.route is None or r.arrived:
            return []
        ids = list(r.route.edge_ids)
        cur = r.current_edge()
        k = ids.index(cur) if cur in ids else 0
        return ids[k:]

    def _check_cp_h2(self) -> None:
        """CP-H2: route-ahead decision when a CONFIRMED (rival-verified) report of this run lies
        anywhere on the rover's remaining route. Unverified reports are not acted upon (they are
        pending verification), which is what makes a fake report harmless."""
        active = [h for h in self.run_hazards() if h.status == "confirmed"]
        if not active:
            return

        for r in self.world.robots.values():
            if not isinstance(r, Rover) or r.paused or r.in_safe_stop:
                continue
            if not r.accepts_unverified_reports:
                continue
            if r.fsm_state not in (FsmState.DELIVERING, FsmState.TO_STORE, FsmState.RETURNING):
                continue
            ahead = self.remaining_route_edges(r)
            if not ahead:
                continue
            for h in active:
                if h.edge_id in ahead and h.id not in self.robot_decided_hazards[r.id] \
                        and h.edge_id not in self.robot_blocked_edges[r.id]:
                    self.robot_decided_hazards[r.id].add(h.id)
                    self._trigger_cp_h2(r, h, ahead)
                    break

    def _route_dist_to(self, r: Rover, edge_id: str, ahead: Sequence[str]) -> float:
        d = 0.0
        for e in ahead:
            if e == edge_id:
                break
            d += float(self.world.planner.edges.get(e, {}).get("length", 0.0))
        return d

    def _trigger_cp_h2(self, rover: Rover, h: Hazard, ahead: Sequence[str] = ()) -> None:
        seq = self._next_seq(rover.id)
        ep_id = self.episode_id_for(rover.id)
        edge = self.world.planner.edges.get(h.edge_id, {})

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
                    "road_name": edge.get("name"),
                    "type": h.type,
                    "severity": h.severity,
                    "status": h.status,
                    "passable_for": h.passable_for,
                    "evidence_root": h.evidence_root,
                    "zg_request_id": h.zg_request_id,
                },
                "confirms": h.confirms,
                "disputes": h.disputes,
                "reporter_operator": h.reporter_operator,
                "my_operator": rover.operator,
                "my_kind": rover.kind,
                "distance_along_route_m": round(self._route_dist_to(rover, h.edge_id, ahead), 1),
                "urgency": "normal",
                "task": rover.fsm_state.value,
                "_mock_truth": {"type": h.type, "real": True},
            },
        )

        def on_ok(robot: Any, res: CheckpointResult) -> None:
            out = res.output or {}
            action = out.get("action")
            if action == "reroute":
                self.block_edge_for_robot(robot.id, h.edge_id)
                robot.replan(blocked_edge_ids=self.get_blocked_edges(robot))
                robot.note = f"0G: reroute around {h.type} #{h.id} ({edge.get('name') or h.edge_id})"
            elif action == "wait":
                robot.note = f"0G: wait/slow for {h.type} #{h.id}"
            elif action == "handoff_to_drone":
                robot.note = f"0G: hand-off to drone suggested ({h.type} #{h.id})"
            else:
                robot.note = f"0G: proceed past {h.type} #{h.id}"

        def on_result(robot: Any, res: CheckpointResult) -> None:
            action = (res.output or {}).get("action", "none") if res.ok else "safe_stop"
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
        evs = self.chain.poll_events()
        # events whose hazard id is not (yet) known as ours: our report future may resolve a
        # moment after the poller saw the log. Keep them ~30 s, then drop (older runs' hazards).
        if self._deferred_chain_events:
            dropped, again = [], []
            for t0, ev in self._deferred_chain_events:
                (again if self.sim_time - t0 < 30.0 else dropped).append((t0, ev))
            self._deferred_chain_events = []
            evs = [ev for _, ev in again] + list(evs)
        for ev in evs:
            ev_name = ev.get("event")
            hid = str((ev.get("args") or {}).get("hazardId") or ev.get("hazard_id") or "")
            if not hid.isdigit():
                continue   # operator/robot registration or delivery events carry no hazard id
            if not self.is_run_hazard(hid):
                self._deferred_chain_events.append((self.sim_time, ev))
                continue
            hz = self.chain.hazard(hid)
            if not hz:
                continue

            BUS.publish("hazard", self.sim_time, hz)

            if ev_name == "HazardResolved" or hz.status in ("confirmed", "rejected", "cleared"):
                if hz.status == "confirmed":
                    # shared map: each rover decides at CP-H2 (0G) whether to reroute; the
                    # local planner does NOT silently avoid it. Background traffic is closed.
                    if self.traffic_bridge:
                        for se in self.edge_to_sumo_edges.get(hz.edge_id, []):
                            self.traffic_bridge.close_edge(se)
                elif hz.status in ("rejected", "cleared"):
                    self.unblock_edge(hz.edge_id)
                    if self.traffic_bridge:
                        for se in self.edge_to_sumo_edges.get(hz.edge_id, []):
                            self.traffic_bridge.reopen_edge(se)

    # ------------------------------------------------------------------ step loop
    def step(self, dt: Optional[float] = None) -> None:
        """Advance one fixed control tick."""
        dt = float(dt or self.dt)
        self._sim_thread = threading.get_ident()
        self._drain_injections()
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
        self.inquiries.tick()
        self._dispatch_verifications()
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
                hazards=self.run_hazards(),
                props=list(self.props.values()),
                brain_online=self.brain.online(),
            )
            BUS.publish("state", self.sim_time, snap)

    # ------------------------------------------------------------------ EngineControl protocol
    def place_order(self, operator: str, building_id: str, items: list[str],
                    customer_name: str, priority: str = "normal",
                    note: str = "") -> dict[str, Any]:
        home = self.homes_by_bldg.get(building_id) or self.homes_by_id.get(building_id)
        if not home:
            # pick random home if building_id unknown
            home = next(iter(self.homes_by_id.values()))
        order = self.order_book.place(operator, home["id"], items, customer_name, self.sim_time,
                                          priority=priority, note=note)
        return {**order.model_dump(mode="json"), **self.order_book.meta.get(order.id, {})}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order = self.order_book.cancel(order_id, self.sim_time)
        return order.model_dump(mode="json")

    def pending_inquiries(self) -> list[dict[str, Any]]:
        return self.inquiries.pending()

    def submit_inquiry_answers(self, inquiry_id: str, answers: list[dict[str, Any]],
                               source: str) -> dict[str, Any]:
        return self.inquiries.answer(inquiry_id, answers, source)

    def set_brain_unreachable(self, on: bool) -> None:
        self.brain.set_unreachable(on)
        if on:
            for r in self.world.robots.values():
                if r.paused:
                    r.enter_safe_stop("0G brain unreachable (sim toggle)")

    def set_speed(self, x: float) -> dict[str, Any]:
        self.clock.set_speed(max(0.0, min(float(x), 100.0)))
        return self.speed()

    def speed(self) -> dict[str, Any]:
        return {"target": self.clock.realtime_factor, "actual": round(self.clock.achieved, 2)}

    def snapshot_meta(self) -> dict[str, Any]:
        return {
            "speed": self.speed(),
            "engine": True,
            "run_id": self.run_id,
            "sim_time": round(self.sim_time, 2),
            "brain_online": self.brain.online(),
            "operators": [op["name"] for op in self.cfg.get("operators", [])],
            "order_stats": self.order_book.stats(),
            "hazards": [h.model_dump(mode="json") for h in self.run_hazards()],
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
