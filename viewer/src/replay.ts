// Replay: load a recorded stream.jsonl (same envelopes as the WebSocket) and play it back.
import type { Envelope, StateSnapshot } from "./types";
import { SnapshotBuffer } from "./interp";
import { store } from "./store";

export interface Marker { t: number; kind: "decision" | "error" | "hazard" | "tx" | "storage"; label: string; }

export async function loadJsonl(url: string, onProgress: (bytes: number, total: number) => void): Promise<Envelope[]> {
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`replay fetch failed: ${r.status} ${url}`);
  const total = Number(r.headers.get("content-length") ?? 0);
  let stream: ReadableStream<Uint8Array> = r.body;
  const gz = /\.gz($|\?)/.test(url) && !(r.headers.get("content-encoding") ?? "").includes("gzip");
  if (gz) stream = stream.pipeThrough(new DecompressionStream("gzip") as unknown as TransformStream<Uint8Array, Uint8Array>);
  const reader = stream.getReader();
  const dec = new TextDecoder();
  const out: Envelope[] = [];
  let buf = "", bytes = 0, bad = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (value) {
      bytes += value.byteLength;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) { try { out.push(JSON.parse(line)); } catch { bad++; } }
      }
      onProgress(bytes, total);
    }
    if (done) break;
  }
  if (buf.trim()) { try { out.push(JSON.parse(buf)); } catch { bad++; } }
  if (bad) console.warn(`[replay] skipped ${bad} bad lines`);
  return out;
}

export class ReplayPlayer {
  buffer = new SnapshotBuffer(0);
  events: Envelope[] = [];        // non-state, sorted by t
  markers: Marker[] = [];
  t0 = 0; t1 = 0;
  time = 0;
  speed = 1;
  playing = true;
  private applied = 0;            // number of events applied to the store
  private lastSnapIdx = -1;
  onRebuild: () => void = () => {};

  constructor(envs: Envelope[]) {
    for (const e of envs) {
      if (e.topic === "state") this.buffer.push(e.data as StateSnapshot);
      else this.events.push(e);
    }
    this.events.sort((a, b) => a.t - b.t);
    const ts = [this.buffer.first?.sim_time, this.events[0]?.t].filter((x): x is number => typeof x === "number");
    const te = [this.buffer.last?.sim_time, this.events[this.events.length - 1]?.t].filter((x): x is number => typeof x === "number");
    this.t0 = ts.length ? Math.min(...ts) : 0;
    this.t1 = te.length ? Math.max(...te) : 0;
    this.time = this.t0;
    for (const e of this.events) {
      const d = e.data as Record<string, any>;
      if (e.topic === "decision") {
        const r = d.result ?? {};
        this.markers.push({ t: e.t, kind: r.ok === false ? "error" : "decision", label: `${r.robot_id} ${r.checkpoint}${r.ok === false ? " SAFE STOP" : ""}` });
      } else if (e.topic === "hazard") this.markers.push({ t: e.t, kind: "hazard", label: `${d.type} ${d.status}` });
      else if (e.topic === "chain_tx" && d.status !== "sent") this.markers.push({ t: e.t, kind: "tx", label: `${d.kind} ${d.status}` });
    }
  }

  get duration(): number { return Math.max(0.001, this.t1 - this.t0); }

  /** advance by wall-clock dt (seconds) */
  tick(dt: number): void {
    if (this.playing) {
      this.time = Math.min(this.t1, this.time + dt * this.speed);
      if (this.time >= this.t1) this.playing = false;
    }
    this.sync();
  }

  seek(t: number): void {
    const nt = Math.max(this.t0, Math.min(this.t1, t));
    const jump = nt < this.time || nt - this.time > 5;
    this.time = nt;
    if (jump) this.rebuild(); else this.sync();
  }

  /** apply events up to the play head; push the current snapshot to the store */
  private sync(): void {
    while (this.applied < this.events.length && this.events[this.applied].t <= this.time) {
      store.apply(this.events[this.applied++]);
    }
    const i = this.buffer.indexAt(this.time);
    if (i !== this.lastSnapIdx && i >= 0) {
      this.lastSnapIdx = i;
      store.apply({ topic: "state", t: this.buffer.snaps[i].sim_time, data: this.buffer.snaps[i] });
    }
    store.simTime = this.time;
  }

  rebuild(): void {
    store.reset();
    store.silent = true;
    this.applied = 0;
    this.lastSnapIdx = -1;
    this.sync();
    store.silent = false;
    this.onRebuild();
  }
}
