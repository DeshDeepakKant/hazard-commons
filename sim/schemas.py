"""FROZEN shared interfaces (SPEC §14). Change only via the lead, mirror in viewer/src/types.ts.

All coordinates are in the local frame (common/frame.py): metres, x=east, y=north, z=up.
yaw is radians, CCW from +x (east).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- enums
ActorKind = Literal["car", "auto", "bike", "bus", "pedestrian"]
RobotKind = Literal["rover", "drone"]
HazardType = Literal["accident", "road_works", "waterlogging", "none"]
HazardStatus = Literal["reported", "confirmed", "rejected", "cleared"]
CheckpointId = Literal["CP-H1", "CP-H2", "CP-V", "CP-D", "CP-Q"]
OrderStatus = Literal["placed", "assigned", "picking", "out_for_delivery", "at_door",
                      "delivered", "failed"]


class FsmState(str, Enum):
    IDLE = "IDLE"
    TO_STORE = "TO_STORE"
    LOADING = "LOADING"
    DELIVERING = "DELIVERING"
    AT_DOOR = "AT_DOOR"
    HANDED_OVER = "HANDED_OVER"
    RETURNING = "RETURNING"
    WAITING_FOR_BRAIN = "WAITING_FOR_BRAIN"
    SAFE_STOP = "SAFE_STOP"
    # drone-only task states
    VERIFYING = "VERIFYING"
    LANDING = "LANDING"


# ---------------------------------------------------------------- world state
class ActorState(BaseModel):
    id: str
    kind: ActorKind
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0
    speed: float = 0.0


class RobotState(BaseModel):
    id: str                      # e.g. "R-07", "D-01"
    operator: str                # operator name
    kind: RobotKind
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0
    speed: float = 0.0
    fsm_state: FsmState = FsmState.IDLE
    order_id: Optional[str] = None
    route_edge_ids: list[str] = Field(default_factory=list)
    battery: float = 1.0         # 0..1
    note: Optional[str] = None   # short human-readable status for the viewer


class Hazard(BaseModel):
    id: str                      # on-chain hazard id as string (or "local-N" before tx)
    edge_id: str                 # road_graph edge id
    type: HazardType
    severity: float = 0.0
    status: HazardStatus = "reported"
    reporter_operator: str
    reporter_robot: Optional[str] = None
    evidence_root: Optional[str] = None   # 0G Storage root hash
    zg_request_id: Optional[str] = None
    tx_hash: Optional[str] = None
    passable_for: list[RobotKind] = Field(default_factory=list)
    confirms: int = 0
    disputes: int = 0


class HazardProp(BaseModel):
    """Ground-truth scenario prop in the sim (what physically exists). NOT the shared map."""
    id: str
    edge_id: str
    type: HazardType
    x: float
    y: float
    real: bool = True            # False only for the fake-report scenario (nothing physical there)
    photo: Optional[str] = None  # assets/hazard_photos/<file> composited at checkpoints


# ---------------------------------------------------------------- brain
class CheckpointRequest(BaseModel):
    robot_id: str
    operator: str
    robot_kind: RobotKind
    checkpoint: CheckpointId
    sim_time: float
    episode_id: str
    seq: int                     # per-episode decision counter -> X-Client-Request-Id
    frame_png_b64: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)


class CheckpointResult(BaseModel):
    ok: bool
    checkpoint: CheckpointId
    robot_id: str
    output: Optional[dict[str, Any]] = None     # validated against brain/schemas.py
    model: Optional[str] = None
    zg_request_id: Optional[str] = None         # X-Request-ID == x_0g_trace.request_id
    client_request_id: Optional[str] = None     # our X-Client-Request-Id
    zg_trace: Optional[dict[str, Any]] = None
    tee_verified: Optional[bool] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None                 # set when ok=False -> SAFE_STOP
    mock: bool = False                          # True only under dev MOCK_BRAIN


# ---------------------------------------------------------------- memory
class EpisodeRecord(BaseModel):
    episode_id: str
    robot_id: str
    operator: str
    sim_time: float
    checkpoint: CheckpointId
    seq: int
    inputs: dict[str, Any]            # {"frame_sha256": ..., "context": {...}}
    model: Optional[str] = None
    zg_request_id: Optional[str] = None
    client_request_id: Optional[str] = None
    zg_trace: Optional[dict[str, Any]] = None
    tee_verified: Optional[bool] = None
    output: Optional[dict[str, Any]] = None
    action_taken: str
    chain_tx: Optional[str] = None
    prev_record_sha256: Optional[str] = None
    record_sha256: Optional[str] = None   # sha256 of canonical JSON of all fields above


# ---------------------------------------------------------------- orders
class Order(BaseModel):
    id: str
    operator: str
    customer_building_id: str
    customer_name: str = ""
    items: list[str] = Field(default_factory=list)
    status: OrderStatus = "placed"
    robot_id: Optional[str] = None
    timestamps: dict[str, float] = Field(default_factory=dict)   # status -> sim_time


# ---------------------------------------------------------------- bus / websocket
Topic = Literal["state", "decision", "hazard", "order", "chain_tx", "storage", "log", "inquiry"]


class StateSnapshot(BaseModel):
    """topic=state, ~10 Hz."""
    sim_time: float
    robots: list[RobotState]
    actors: list[ActorState]
    hazards: list[Hazard]
    props: list[HazardProp] = Field(default_factory=list)
    brain_online: bool = True


class DecisionEvent(BaseModel):
    """topic=decision. One card in the live decision panel."""
    result: CheckpointResult
    operator: str
    sim_time: float
    action_taken: str
    frame_thumb_b64: Optional[str] = None      # small jpeg for the card
    frame_full_b64: Optional[str] = None       # full-size jpeg of the image sent to 0G (detail view; optional)
    storage_root: Optional[str] = None         # filled later via topic=storage
    chain_tx: Optional[str] = None


class ChainTxEvent(BaseModel):
    """topic=chain_tx."""
    kind: Literal["reportHazard", "verifyHazard", "clearHazard", "registerRobot",
                  "registerOperator", "other"]
    tx_hash: str
    operator: Optional[str] = None
    hazard_id: Optional[str] = None
    status: Literal["sent", "mined", "failed"] = "sent"
    sim_time: float = 0.0


class StorageEvent(BaseModel):
    """topic=storage. Episode batch uploaded to 0G Storage."""
    robot_id: str
    episode_id: str
    root_hash: str
    tx_hash: Optional[str] = None
    record_seqs: list[int] = Field(default_factory=list)
    zg_request_ids: list[str] = Field(default_factory=list)
    local_path: str


class Envelope(BaseModel):
    topic: Topic
    t: float            # sim_time
    data: dict[str, Any]
