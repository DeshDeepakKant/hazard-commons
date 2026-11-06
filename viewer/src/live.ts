// Live mode: WebSocket /ws envelopes -> store + snapshot buffer, with a smoothed sim clock.
import type { Envelope, StateSnapshot } from "./types";
import { SnapshotBuffer } from "./interp";
import { store } from "./store";
import { CONFIG } from "./config";

export const liveSpeed = { target: 1 };   // set by the speed buttons: faster sim needs faster clock smoothing
const RENDER_DELAY_S = 0.15;   // render slightly in the past so we always interpolate

export class LiveSource {
  buffer = new SnapshotBuffer(40);
  private offset: number | null = null;   // sim_time - wall seconds
  private ws: WebSocket | null = null;
  private retry = 0;
  onConn: (s: "connecting" | "open" | "closed") => void = () => {};

  start(): void { this.connect(); }

  private connect(): void {
    this.onConn("connecting");
    const ws = new WebSocket(CONFIG.wsUrl);
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.onConn("open"); };
    ws.onmessage = (m) => {
      let env: Envelope;
      try { env = JSON.parse(m.data as string); } catch { return; }
      if (env.topic === "state") {
        const s = env.data as StateSnapshot;
        const now = performance.now() / 1000;
        const target = s.sim_time - now;
        if (this.offset === null || Math.abs(target - this.offset) > 1.5 * Math.max(1, liveSpeed.target)) this.offset = target;
        else this.offset += (target - this.offset) * (liveSpeed.target > 1.5 ? 0.35 : 0.08);
        this.buffer.push(s);
      }
      store.apply(env);
    };
    ws.onclose = () => {
      this.onConn("closed");
      this.ws = null;
      const delay = Math.min(5000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  renderTime(): number {
    const last = this.buffer.last;
    if (!last || this.offset === null) return store.simTime;
    const t = performance.now() / 1000 + this.offset - RENDER_DELAY_S;
    return Math.min(t, last.sim_time);
  }
}

export async function setBrainUnreachable(on: boolean): Promise<boolean> {
  try {
    const r = await fetch(`${CONFIG.apiBase}/api/brain/unreachable`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ unreachable: on }),
    });
    return r.ok;
  } catch { return false; }
}

export async function fetchMeta(): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetch(`${CONFIG.apiBase}/api/meta`);
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

export interface SpeedInfo { target: number; actual: number }

export async function fetchSpeed(): Promise<SpeedInfo | null> {
  try {
    const r = await fetch(`${CONFIG.apiBase}/api/speed`);
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

export async function setSpeed(x: number): Promise<SpeedInfo | null> {
  try {
    const r = await fetch(`${CONFIG.apiBase}/api/speed`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ speed: x }),
    });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}
