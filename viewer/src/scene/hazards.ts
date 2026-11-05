// Hazard markers (on road edges, coloured by status) + physical hazard props.
import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import type { Hazard, HazardProp } from "../types";
import { HAZARD_LABEL, opColor } from "../config";
import { edgeMidpoint, ribbon, type WorldData } from "./world";

const STATUS_COLOR: Record<string, string> = {
  reported: "#facc15", confirmed: "#ef4444", rejected: "#9ca3af", cleared: "#22c55e",
};
const ICON: Record<string, string> = { accident: "💥", road_works: "🚧", waterlogging: "🌊", none: "?" };

interface MarkerView {
  id: string; status: string;
  group: THREE.Group;
  overlay: THREE.Mesh;
  beam: THREE.Mesh;
  label: CSS2DObject;
  el: HTMLDivElement;
  clearedAt: number | null;
}

export class Hazards {
  group = new THREE.Group();
  propGroup = new THREE.Group();
  private markers = new Map<string, MarkerView>();
  private props = new Map<string, THREE.Group>();
  onClick: (h: Hazard) => void = () => {};

  constructor(private world: WorldData) {
    this.group.name = "hazards";
    this.propGroup.name = "props";
  }

  private create(h: Hazard): MarkerView | null {
    const road = this.world.roadById.get(h.edge_id);
    const mid = edgeMidpoint(this.world, h.edge_id);
    if (!road || !mid) return null;
    const g = new THREE.Group();
    const pos: number[] = [], idx: number[] = [];
    ribbon(road.shape, (road.width || 6) + 1.5, 0.14, pos, idx);
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geom.setIndex(idx);
    const overlay = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({ color: STATUS_COLOR.reported, transparent: true, opacity: 0.55, depthWrite: false, side: THREE.DoubleSide }));
    g.add(overlay);
    const beam = new THREE.Mesh(
      new THREE.CylinderGeometry(1.2, 1.2, 60, 16, 1, true),
      new THREE.MeshBasicMaterial({ color: STATUS_COLOR.reported, transparent: true, opacity: 0.18, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending }),
    );
    beam.position.set(mid[0], 30, -mid[1]);
    g.add(beam);
    const el = document.createElement("div");
    el.className = "hazard-label";
    el.addEventListener("pointerdown", (e) => { e.stopPropagation(); const cur = this.latest.get(h.id); if (cur) this.onClick(cur); });
    const label = new CSS2DObject(el);
    label.position.set(mid[0], 26, -mid[1]);
    label.center.set(0.5, 1);
    g.add(label);
    this.group.add(g);
    const v: MarkerView = { id: h.id, status: "", group: g, overlay, beam, label, el, clearedAt: null };
    this.markers.set(h.id, v);
    return v;
  }

  private latest = new Map<string, Hazard>();

  sync(hazards: Iterable<Hazard>, nowS: number): void {
    const seen = new Set<string>();
    for (const h of hazards) {
      seen.add(h.id);
      this.latest.set(h.id, h);
      const v = this.markers.get(h.id) ?? this.create(h);
      if (!v) continue;
      const sig = `${h.status}|${h.confirms}|${h.disputes}`;
      if (v.status !== sig) {
        v.status = sig;
        const col = STATUS_COLOR[h.status] ?? "#facc15";
        (v.overlay.material as THREE.MeshBasicMaterial).color.set(col);
        (v.beam.material as THREE.MeshBasicMaterial).color.set(col);
        v.el.dataset.status = h.status;
        v.el.style.setProperty("--st", col);
        v.el.style.setProperty("--op", opColor(h.reporter_operator));
        v.el.innerHTML = `<div class="hz-top"><span class="hz-icon">${ICON[h.type] ?? "⚠"}</span><span class="hz-type">${HAZARD_LABEL[h.type] ?? h.type}</span><span class="hz-status">${h.status}</span></div>
          <div class="hz-sub"><span class="opdot"></span>${h.reporter_operator} · ✓${h.confirms} ✗${h.disputes}</div>`;
        v.clearedAt = h.status === "cleared" ? (v.clearedAt ?? nowS) : null;
      }
      // fade-out for cleared, pulse for reported
      const om = v.overlay.material as THREE.MeshBasicMaterial, bm = v.beam.material as THREE.MeshBasicMaterial;
      if (h.status === "cleared") {
        const k = Math.max(0, 1 - (nowS - (v.clearedAt ?? nowS)) / 4);
        om.opacity = 0.5 * k; bm.opacity = 0.18 * k;
        v.el.style.opacity = String(Math.max(0, k));
        v.group.visible = k > 0.01;
      } else if (h.status === "rejected") {
        om.opacity = 0.3; bm.opacity = 0.06; v.el.style.opacity = "0.85"; v.group.visible = true;
      } else {
        const pulse = h.status === "reported" ? 0.5 + 0.5 * Math.sin(nowS * 4) : 1;
        om.opacity = 0.35 + 0.3 * pulse; bm.opacity = 0.1 + 0.12 * pulse; v.el.style.opacity = "1"; v.group.visible = true;
      }
    }
    for (const [id, v] of this.markers) {
      if (!seen.has(id)) { this.group.remove(v.group); v.el.remove(); this.markers.delete(id); this.latest.delete(id); }
    }
  }

  syncProps(props: HazardProp[]): void {
    const seen = new Set<string>();
    for (const p of props) {
      seen.add(p.id);
      if (this.props.has(p.id)) continue;
      const g = makeProp(p);
      g.position.set(p.x, 0, -p.y);
      this.propGroup.add(g);
      this.props.set(p.id, g);
    }
    for (const [id, g] of this.props) if (!seen.has(id)) { this.propGroup.remove(g); this.props.delete(id); }
  }

  /** world position (three) of a hazard marker */
  position(id: string): THREE.Vector3 | null {
    const h = this.latest.get(id);
    if (!h) return null;
    const m = edgeMidpoint(this.world, h.edge_id);
    return m ? new THREE.Vector3(m[0], 0, -m[1]) : null;
  }
}

function cone(): THREE.Mesh {
  const m = new THREE.Mesh(new THREE.ConeGeometry(0.35, 0.9, 10), new THREE.MeshStandardMaterial({ color: "#f97316", emissive: "#7c2d12", emissiveIntensity: 0.4 }));
  m.position.y = 0.45;
  return m;
}

function makeProp(p: HazardProp): THREE.Group {
  const g = new THREE.Group();
  const rnd = mulberry(hashStr(p.id));
  if (p.type === "accident") {
    const mk = (color: string, x: number, z: number, yaw: number, roll: number) => {
      const car = new THREE.Mesh(new THREE.BoxGeometry(4.3, 1.4, 1.8), new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.3 }));
      car.position.set(x, 0.75, z); car.rotation.set(roll, yaw, 0);
      return car;
    };
    g.add(mk("#e5e7eb", 0, 0, 0.5, 0.05), mk("#b91c1c", 2.6, 1.2, -0.9, -0.08));
    for (let i = 0; i < 5; i++) {
      const debris = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.15, 0.3), new THREE.MeshStandardMaterial({ color: "#475569" }));
      debris.position.set(rnd() * 6 - 2, 0.08, rnd() * 4 - 2); debris.rotation.y = rnd() * 3;
      g.add(debris);
    }
    for (const [x, z] of [[-4, -2], [-4, 2], [6, -2]]) { const c = cone(); c.position.x = x; c.position.z = z; g.add(c); }
  } else if (p.type === "road_works") {
    const barrierMat = new THREE.MeshStandardMaterial({ color: "#facc15", emissive: "#713f12", emissiveIntensity: 0.3 });
    for (let i = -2; i <= 2; i++) {
      const b = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.9, 0.25), barrierMat);
      b.position.set(i * 2, 0.45, 0); g.add(b);
    }
    const pile = new THREE.Mesh(new THREE.ConeGeometry(2.2, 1.4, 12), new THREE.MeshStandardMaterial({ color: "#78716c", roughness: 1 }));
    pile.position.set(0, 0.7, 2.6); g.add(pile);
    for (const x of [-5, 5]) { const c = cone(); c.position.x = x; g.add(c); }
  } else if (p.type === "waterlogging") {
    const water = new THREE.Mesh(
      new THREE.CircleGeometry(9, 40),
      new THREE.MeshStandardMaterial({ color: "#38bdf8", transparent: true, opacity: 0.55, roughness: 0.05, metalness: 0.6, emissive: "#0c4a6e", emissiveIntensity: 0.5 }),
    );
    water.rotation.x = -Math.PI / 2; water.position.y = 0.2; water.scale.set(1.4, 0.8, 1);
    g.add(water);
  }
  if (!p.real) {
    // a reported-but-fake hazard has no physical prop; nothing rendered beyond the marker
  }
  return g;
}

function hashStr(s: string): number { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function mulberry(a: number) { return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
