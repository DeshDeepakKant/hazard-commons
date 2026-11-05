// Background actors (SUMO): one InstancedMesh per kind, interpolated between snapshots.
import * as THREE from "three";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import type { ActorKind, ActorState } from "../types";
import { interpolate } from "../interp";

function box(w: number, h: number, l: number, y: number, x = 0): THREE.BufferGeometry {
  // length along +x (yaw 0 = east), width along z
  const g = new THREE.BoxGeometry(l, h, w);
  g.translate(x, y + h / 2, 0);
  return g;
}

function vehicle(len: number, wid: number, h: number, cabinFrac = 0.55): THREE.BufferGeometry {
  const body = box(wid, h * 0.5, len, 0.25);
  const cabin = box(wid * 0.86, h * 0.42, len * cabinFrac, 0.25 + h * 0.5, -len * 0.05);
  return mergeGeometries([body.toNonIndexed(), cabin.toNonIndexed()])!;
}

const GEOMS: Record<ActorKind, () => THREE.BufferGeometry> = {
  car: () => vehicle(4.3, 1.8, 1.5),
  auto: () => {
    const body = box(1.35, 1.25, 2.7, 0.2);
    const roof = box(1.45, 0.12, 2.0, 1.45, -0.2);
    return mergeGeometries([body.toNonIndexed(), roof.toNonIndexed()])!;
  },
  bike: () => {
    const g = box(0.5, 0.7, 1.9, 0.35);
    const rider = box(0.45, 0.8, 0.45, 1.05, -0.15);
    return mergeGeometries([g.toNonIndexed(), rider.toNonIndexed()])!;
  },
  bus: () => vehicle(11.5, 2.5, 3.2, 0.95),
  pedestrian: () => {
    const g = new THREE.CapsuleGeometry(0.28, 1.1, 3, 8);
    g.translate(0, 0.85, 0);
    return g.toNonIndexed();
  },
};

const PALETTE: Record<ActorKind, string[]> = {
  car: ["#e5e7eb", "#9ca3af", "#4b5563", "#1f2937", "#b91c1c", "#f8fafc", "#64748b", "#7c2d12", "#d6d3d1"],
  auto: ["#facc15", "#eab308"],        // Mumbai autos: yellow/black
  bike: ["#111827", "#374151", "#7f1d1d", "#1e3a8a"],
  bus: ["#dc2626", "#b91c1c"],         // BEST buses are red
  pedestrian: ["#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6", "#f97316", "#e11d48", "#0ea5e9", "#a3e635", "#fde68a"],
};

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

class KindLayer {
  mesh: THREE.InstancedMesh;
  cap: number;
  constructor(public kind: ActorKind, private geom: THREE.BufferGeometry, private mat: THREE.Material, cap: number, parent: THREE.Object3D) {
    this.cap = cap;
    this.mesh = this.make(cap);
    parent.add(this.mesh);
  }
  private make(cap: number): THREE.InstancedMesh {
    const m = new THREE.InstancedMesh(this.geom, this.mat, cap);
    m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    m.count = 0;
    m.frustumCulled = false;
    m.setColorAt(0, new THREE.Color());
    return m;
  }
  ensure(n: number): void {
    if (n <= this.cap) return;
    const parent = this.mesh.parent!;
    parent.remove(this.mesh);
    this.mesh.dispose();
    this.cap = Math.max(n, this.cap * 2);
    this.mesh = this.make(this.cap);
    parent.add(this.mesh);
  }
}

export class Actors {
  group = new THREE.Group();
  private layers = new Map<ActorKind, KindLayer>();
  private tmpM = new THREE.Matrix4();
  private tmpQ = new THREE.Quaternion();
  private tmpP = new THREE.Vector3();
  private tmpS = new THREE.Vector3(1, 1, 1);
  private yAxis = new THREE.Vector3(0, 1, 0);
  private color = new THREE.Color();
  private counts: Record<string, number> = {};

  constructor() {
    this.group.name = "actors";
    const mat = new THREE.MeshStandardMaterial({ roughness: 0.6, metalness: 0.15 });
    for (const k of Object.keys(GEOMS) as ActorKind[]) {
      this.layers.set(k, new KindLayer(k, GEOMS[k](), mat, k === "car" || k === "pedestrian" ? 256 : 64, this.group));
    }
  }

  update(a: ActorState[], b: ActorState[], alpha: number): void {
    for (const k of this.layers.keys()) this.counts[k] = 0;
    // size pass
    const need: Record<string, number> = {};
    for (const it of b) need[it.kind] = (need[it.kind] ?? 0) + 1;
    for (const [k, L] of this.layers) L.ensure(need[k] ?? 0);
    interpolate(a, b, alpha, (it, p) => {
      const L = this.layers.get(it.kind) ?? this.layers.get("car")!;
      const i = this.counts[L.kind]++;
      this.tmpP.set(p.x, Math.max(0, p.z), -p.y);
      this.tmpQ.setFromAxisAngle(this.yAxis, p.yaw);   // yaw CCW from +x(east); three y-up rotation matches
      this.tmpM.compose(this.tmpP, this.tmpQ, this.tmpS);
      L.mesh.setMatrixAt(i, this.tmpM);
      const pal = PALETTE[L.kind];
      this.color.set(pal[hash(it.id) % pal.length]);
      L.mesh.setColorAt(i, this.color);
    });
    for (const [k, L] of this.layers) {
      L.mesh.count = this.counts[k] ?? 0;
      L.mesh.instanceMatrix.needsUpdate = true;
      if (L.mesh.instanceColor) L.mesh.instanceColor.needsUpdate = true;
    }
  }

  total(): number { let n = 0; for (const k in this.counts) n += this.counts[k]; return n; }
}
