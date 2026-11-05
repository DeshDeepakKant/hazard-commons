// Event-sourced client state. Live (WebSocket) and replay (jsonl) both feed `apply(envelope)`.
import type {
  ChainTxEvent, DecisionEvent, Envelope, Hazard, Order, StateSnapshot, StorageEvent,
} from "./types";

export interface Card {
  key: string;
  seq: number;              // arrival order
  ev: DecisionEvent;
  storageRoot: string | null;
  storageTx: string | null;
  chainTx: string | null;
  chainStatus: ChainTxEvent["status"] | null;
  chainKind: ChainTxEvent["kind"] | null;
  hazardId: string | null;
}

export interface LogLine { t: number; level: string; msg: string; }

type Listener = (what: string, payload?: unknown) => void;

const ATTACH_WINDOW_S = 60;   // a chain tx attaches to a decision made at most this long before

export class Store {
  simTime = 0;
  brainOnline = true;
  latest: StateSnapshot | null = null;
  cards: Card[] = [];
  private cardByReq = new Map<string, Card>();
  hazards = new Map<string, Hazard>();
  orders = new Map<string, Order>();
  txs = new Map<string, ChainTxEvent>();
  storage: StorageEvent[] = [];
  logs: LogLine[] = [];
  private seq = 0;
  private listeners: Listener[] = [];
  silent = false;

  on(fn: Listener): void { this.listeners.push(fn); }
  emit(what: string, payload?: unknown): void {
    if (this.silent) return;
    for (const fn of this.listeners) fn(what, payload);
  }

  reset(): void {
    this.simTime = 0; this.brainOnline = true; this.latest = null;
    this.cards = []; this.cardByReq.clear(); this.hazards.clear(); this.orders.clear();
    this.txs.clear(); this.storage = []; this.logs = []; this.seq = 0;
  }

  apply(env: Envelope): void {
    if (typeof env.t === "number" && env.t > this.simTime) this.simTime = env.t;
    switch (env.topic) {
      case "state": {
        const s = env.data as StateSnapshot;
        this.latest = s;
        this.simTime = Math.max(this.simTime, s.sim_time);
        if (s.brain_online !== this.brainOnline) {
          this.brainOnline = s.brain_online;
          this.emit("brain", s.brain_online);
        }
        // the snapshot carries the full hazard list: keep the map in sync
        let changed = false;
        for (const h of s.hazards ?? []) {
          const old = this.hazards.get(h.id);
          if (!old || old.status !== h.status || old.confirms !== h.confirms || old.disputes !== h.disputes || old.tx_hash !== h.tx_hash) {
            this.hazards.set(h.id, h); changed = true;
            this.linkHazard(h);
          }
        }
        if (changed) this.emit("hazards");
        this.emit("state", s);
        break;
      }
      case "decision": this.onDecision(env.data as DecisionEvent); break;
      case "hazard": {
        const h = env.data as Hazard;
        this.hazards.set(h.id, h);
        this.linkHazard(h);
        this.emit("hazards");
        break;
      }
      case "order": {
        const o = env.data as Order;
        this.orders.set(o.id, o);
        this.emit("order", o);
        break;
      }
      case "chain_tx": this.onTx(env.data as ChainTxEvent); break;
      case "storage": this.onStorage(env.data as StorageEvent); break;
      case "log": {
        const d = env.data as { level?: string; msg?: string } | string;
        const line = typeof d === "string" ? { t: env.t, level: "info", msg: d }
          : { t: env.t, level: d.level ?? "info", msg: d.msg ?? JSON.stringify(d) };
        this.logs.push(line);
        if (this.logs.length > 200) this.logs.shift();
        this.emit("log", line);
        break;
      }
    }
  }

  private onDecision(ev: DecisionEvent): void {
    const r = ev.result;
    const key = r.zg_request_id ?? r.client_request_id ?? `${r.robot_id}@${ev.sim_time}#${r.checkpoint}`;
    let card = this.cardByReq.get(key);
    if (card) {
      card.ev = ev;
      if (ev.storage_root) card.storageRoot = ev.storage_root;
      if (ev.chain_tx) card.chainTx = ev.chain_tx;
    } else {
      card = {
        key, seq: this.seq++, ev, storageRoot: ev.storage_root, storageTx: null,
        chainTx: ev.chain_tx, chainStatus: null, chainKind: null, hazardId: null,
      };
      this.cards.push(card);
      this.cardByReq.set(key, card);
      if (r.client_request_id && r.client_request_id !== key) this.cardByReq.set(r.client_request_id, card);
    }
    if (card.chainTx) {
      const tx = this.txs.get(card.chainTx);
      if (tx) { card.chainStatus = tx.status; card.chainKind = tx.kind; }
    }
    this.emit("card", card);
  }

  private onStorage(se: StorageEvent): void {
    this.storage.push(se);
    for (const id of se.zg_request_ids ?? []) {
      const c = this.cardByReq.get(id);
      if (c) { c.storageRoot = se.root_hash; c.storageTx = se.tx_hash; this.emit("card", c); }
    }
    this.emit("storage", se);
  }

  private onTx(tx: ChainTxEvent): void {
    const prev = this.txs.get(tx.tx_hash);
    this.txs.set(tx.tx_hash, { ...prev, ...tx });
    // already attached?
    let card = this.cards.find((c) => c.chainTx === tx.tx_hash);
    if (!card && tx.hazard_id) {
      const h = this.hazards.get(tx.hazard_id);
      if (tx.kind === "reportHazard" && h?.zg_request_id) card = this.cardByReq.get(h.zg_request_id);
    }
    if (!card && (tx.kind === "reportHazard" || tx.kind === "verifyHazard")) {
      // newest card of that operator for the matching checkpoint without a tx yet
      const cp = tx.kind === "reportHazard" ? "CP-H1" : "CP-V";
      for (let i = this.cards.length - 1; i >= 0; i--) {
        const c = this.cards[i];
        if (tx.sim_time - c.ev.sim_time > ATTACH_WINDOW_S) break;
        if (c.chainTx || !c.ev.result.ok || c.ev.result.checkpoint !== cp) continue;
        if (tx.operator && c.ev.operator !== tx.operator) continue;
        card = c; break;
      }
    }
    if (card) {
      card.chainTx = tx.tx_hash; card.chainStatus = tx.status; card.chainKind = tx.kind;
      if (tx.hazard_id) card.hazardId = tx.hazard_id;
      this.emit("card", card);
    }
    this.emit("tx", this.txs.get(tx.tx_hash));
  }

  private linkHazard(h: Hazard): void {
    if (!h.zg_request_id) return;
    const c = this.cardByReq.get(h.zg_request_id);
    if (!c) return;
    let changed = false;
    if (!c.hazardId) { c.hazardId = h.id; changed = true; }
    if (h.tx_hash && !c.chainTx) {
      c.chainTx = h.tx_hash;
      const tx = this.txs.get(h.tx_hash);
      c.chainStatus = tx?.status ?? "mined"; c.chainKind = tx?.kind ?? "reportHazard";
      changed = true;
    }
    if (h.evidence_root && !c.storageRoot) { c.storageRoot = h.evidence_root; changed = true; }
    if (changed) this.emit("card", c);
  }

  operatorStats(): Record<string, OpStats> {
    const out: Record<string, OpStats> = {};
    const get = (op: string) => (out[op] ??= {
      delivered: 0, inProgress: 0, failed: 0, reports: 0, confirmed: 0, rejected: 0,
      verifications: 0, decisions: 0, errors: 0, robots: [],
    });
    for (const o of this.orders.values()) {
      const s = get(o.operator);
      if (o.status === "delivered") s.delivered++;
      else if (o.status === "failed") s.failed++;
      else s.inProgress++;
    }
    for (const h of this.hazards.values()) {
      const s = get(h.reporter_operator);
      s.reports++;
      if (h.status === "confirmed" || h.status === "cleared") s.confirmed++;
      if (h.status === "rejected") s.rejected++;
    }
    for (const c of this.cards) {
      const s = get(c.ev.operator);
      s.decisions++;
      if (!c.ev.result.ok) s.errors++;
      if (c.ev.result.checkpoint === "CP-V" && c.ev.result.ok) s.verifications++;
    }
    for (const r of this.latest?.robots ?? []) get(r.operator).robots.push(r);
    return out;
  }
}

export interface OpStats {
  delivered: number; inProgress: number; failed: number;
  reports: number; confirmed: number; rejected: number; verifications: number;
  decisions: number; errors: number;
  robots: StateSnapshot["robots"];
}

export const store = new Store();
