// Mirror of sim/schemas.py (FROZEN). Keep in sync.
// Local frame: metres, x=east, y=north, z=up. three.js position = (x, z, -y).

export type ActorKind = "car" | "auto" | "bike" | "bus" | "pedestrian";
export type RobotKind = "rover" | "drone";
export type HazardType = "accident" | "road_works" | "waterlogging" | "none";
export type HazardStatus = "reported" | "confirmed" | "rejected" | "cleared";
export type CheckpointId = "CP-H1" | "CP-H2" | "CP-V" | "CP-D";
export type OrderStatus = "placed" | "assigned" | "picking" | "out_for_delivery" | "at_door" | "delivered" | "failed";
export type FsmState =
  | "IDLE" | "TO_STORE" | "LOADING" | "DELIVERING" | "AT_DOOR" | "HANDED_OVER" | "RETURNING"
  | "WAITING_FOR_BRAIN" | "SAFE_STOP" | "VERIFYING" | "LANDING";

export interface ActorState { id: string; kind: ActorKind; x: number; y: number; z: number; yaw: number; speed: number; }

export interface RobotState {
  id: string; operator: string; kind: RobotKind;
  x: number; y: number; z: number; yaw: number; speed: number;
  fsm_state: FsmState; order_id: string | null; route_edge_ids: string[]; battery: number; note: string | null;
}

export interface Hazard {
  id: string; edge_id: string; type: HazardType; severity: number; status: HazardStatus;
  reporter_operator: string; reporter_robot: string | null;
  evidence_root: string | null; zg_request_id: string | null; tx_hash: string | null;
  passable_for: RobotKind[]; confirms: number; disputes: number;
}

export interface HazardProp { id: string; edge_id: string; type: HazardType; x: number; y: number; real: boolean; photo: string | null; }

export interface CheckpointResult {
  ok: boolean; checkpoint: CheckpointId; robot_id: string;
  output: Record<string, unknown> | null; model: string | null;
  zg_request_id: string | null; client_request_id: string | null;
  zg_trace: Record<string, unknown> | null; tee_verified: boolean | null;
  latency_ms: number | null; error: string | null; mock: boolean;
}

export interface Order {
  id: string; operator: string; customer_building_id: string; customer_name: string;
  items: string[]; status: OrderStatus; robot_id: string | null; timestamps: Record<string, number>;
}

export interface StateSnapshot { sim_time: number; robots: RobotState[]; actors: ActorState[]; hazards: Hazard[]; props: HazardProp[]; brain_online: boolean; }

export interface DecisionEvent {
  result: CheckpointResult; operator: string; sim_time: number; action_taken: string;
  frame_thumb_b64: string | null; storage_root: string | null; chain_tx: string | null;
}

export interface ChainTxEvent {
  kind: "reportHazard" | "verifyHazard" | "clearHazard" | "registerRobot" | "registerOperator" | "other";
  tx_hash: string; operator: string | null; hazard_id: string | null; status: "sent" | "mined" | "failed"; sim_time: number;
}

export interface StorageEvent {
  robot_id: string; episode_id: string; root_hash: string; tx_hash: string | null;
  record_seqs: number[]; zg_request_ids: string[]; local_path: string;
}

export type Topic = "state" | "decision" | "hazard" | "order" | "chain_tx" | "storage" | "log";
export interface Envelope<T = unknown> { topic: Topic; t: number; data: T; }

export const EXPLORERS = {
  chain: "https://chainscan-galileo.0g.ai",
  storage: "https://storagescan-galileo.0g.ai",
};
