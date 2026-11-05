// Snapshot buffer + interpolation between 10 Hz state snapshots.
import type { StateSnapshot } from "./types";

export interface Pose { x: number; y: number; z: number; yaw: number; speed: number; }

export interface Sample { a: StateSnapshot; b: StateSnapshot; alpha: number; }

export class SnapshotBuffer {
  snaps: StateSnapshot[] = [];
  constructor(private maxLen = 0) {}   // 0 = unbounded (replay)

  push(s: StateSnapshot): void {
    const n = this.snaps.length;
    if (n && s.sim_time < this.snaps[n - 1].sim_time) {
      // out of order or sim restart: restart buffer if it jumped back a lot
      if (this.snaps[n - 1].sim_time - s.sim_time > 2) this.snaps = [];
      else return;
    }
    this.snaps.push(s);
    if (this.maxLen && this.snaps.length > this.maxLen) this.snaps.shift();
  }

  clear(): void { this.snaps = []; }
  get first(): StateSnapshot | null { return this.snaps[0] ?? null; }
  get last(): StateSnapshot | null { return this.snaps[this.snaps.length - 1] ?? null; }

  /** index of last snapshot with sim_time <= t (or -1) */
  indexAt(t: number): number {
    const s = this.snaps;
    let lo = 0, hi = s.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (s[mid].sim_time <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  sample(t: number): Sample | null {
    const s = this.snaps;
    if (!s.length) return null;
    const i = this.indexAt(t);
    if (i < 0) return { a: s[0], b: s[0], alpha: 0 };
    if (i >= s.length - 1) return { a: s[s.length - 1], b: s[s.length - 1], alpha: 0 };
    const a = s[i], b = s[i + 1];
    const dt = b.sim_time - a.sim_time;
    // big gaps (e.g. a teleport / scenario jump): don't interpolate across > 2 s
    const alpha = dt > 0 && dt < 2 ? (t - a.sim_time) / dt : 0;
    return { a, b, alpha: Math.min(1, Math.max(0, alpha)) };
  }
}

export function lerpAngle(a: number, b: number, t: number): number {
  let d = b - a;
  while (d > Math.PI) d -= 2 * Math.PI;
  while (d < -Math.PI) d += 2 * Math.PI;
  return a + d * t;
}

const indexCache = new WeakMap<object, Map<string, number>>();
export function byId<T extends { id: string }>(arr: T[]): Map<string, number> {
  let m = indexCache.get(arr);
  if (!m) {
    m = new Map();
    for (let i = 0; i < arr.length; i++) m.set(arr[i].id, i);
    indexCache.set(arr, m);
  }
  return m;
}

/** Interpolated pose of every item of b (matched by id in a). */
export function interpolate<T extends { id: string } & Pose>(
  aArr: T[], bArr: T[], alpha: number, out: (item: T, p: Pose) => void,
): void {
  const ai = byId(aArr);
  const p: Pose = { x: 0, y: 0, z: 0, yaw: 0, speed: 0 };
  for (const it of bArr) {
    const j = ai.get(it.id);
    const prev = j === undefined ? undefined : aArr[j];
    if (!prev || alpha >= 1) {
      p.x = it.x; p.y = it.y; p.z = it.z; p.yaw = it.yaw; p.speed = it.speed;
    } else {
      // jump guard (teleports): >40 m between snapshots -> snap
      const dx = it.x - prev.x, dy = it.y - prev.y;
      if (dx * dx + dy * dy > 1600) { p.x = it.x; p.y = it.y; p.z = it.z; p.yaw = it.yaw; }
      else {
        p.x = prev.x + dx * alpha; p.y = prev.y + dy * alpha; p.z = prev.z + (it.z - prev.z) * alpha;
        p.yaw = lerpAngle(prev.yaw, it.yaw, alpha);
      }
      p.speed = it.speed;
    }
    out(it, p);
  }
}
