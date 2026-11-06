// Drone landing pads: an "H" helipad disc in the operator colour at each drone's home, with a
// CSS2D label. Home = the drone's first landed position (seeded from the replay's first
// snapshots, or observed live). The engine's /api/meta doesn't carry the drone config.
import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import type { RobotState, StateSnapshot } from "../types";
import { opColor } from "../config";

const PAD_R = 12;          // metres
const PAD_Y = 0.45;       // slightly raised above roads / ground imagery

function padTexture(color: string): THREE.CanvasTexture {
  const s = 256, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d")!;
  const m = s / 2;
  g.fillStyle = "rgba(10, 14, 22, 0.92)";
  g.beginPath(); g.arc(m, m, m - 2, 0, Math.PI * 2); g.fill();
  g.strokeStyle = color; g.lineWidth = 22;
  g.beginPath(); g.arc(m, m, m - 14, 0, Math.PI * 2); g.stroke();
  g.strokeStyle = "rgba(255,255,255,0.85)"; g.lineWidth = 4;
  g.beginPath(); g.arc(m, m, m - 34, 0, Math.PI * 2); g.stroke();
  // the "H"
  g.fillStyle = color;
  const hw = 92, hh = 120, bar = 26;
  g.fillRect(m - hw / 2, m - hh / 2, bar, hh);
  g.fillRect(m + hw / 2 - bar, m - hh / 2, bar, hh);
  g.fillRect(m - hw / 2, m - bar / 2, hw, bar);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

export interface Pad { droneId: string; operator: string; x: number; y: number; root: THREE.Group; }

export class Pads {
  group = new THREE.Group();
  pads = new Map<string, Pad>();
  onClick: (droneId: string) => void = () => {};
  private geom = new THREE.CircleGeometry(PAD_R, 48);

  constructor() { this.group.name = "drone-pads"; }

  /** replay: take each drone's first landed position from the recording */
  seed(snaps: StateSnapshot[]): void {
    const n = Math.min(snaps.length, 3000);
    for (let i = 0; i < n; i++) for (const r of snaps[i].robots ?? []) this.observe(r);
  }

  /** first landed sighting of a drone defines its pad */
  observe(r: RobotState): void {
    if (r.kind !== "drone" || this.pads.has(r.id) || r.z > 1) return;
    this.add(r.id, r.operator, r.x, r.y);
  }

  home(id: string): Pad | undefined { return this.pads.get(id); }

  private add(droneId: string, operator: string, x: number, y: number): void {
    const color = opColor(operator);
    const root = new THREE.Group();
    root.position.set(x, PAD_Y, -y);
    const disc = new THREE.Mesh(this.geom, new THREE.MeshBasicMaterial({
      map: padTexture(color), transparent: true, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4,
    }));
    disc.rotation.x = -Math.PI / 2;
    disc.renderOrder = 2;
    // low rim so the pad reads as a raised platform at oblique angles
    const rim = new THREE.Mesh(
      new THREE.CylinderGeometry(PAD_R, PAD_R, 0.6, 48, 1, true),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.8, side: THREE.DoubleSide }),
    );
    rim.position.y = -0.2;
    root.add(disc, rim);

    const el = document.createElement("div");
    el.className = "pad-label";
    el.style.setProperty("--op", color);
    el.innerHTML = `<span class="h">H</span><span></span>`;
    (el.lastElementChild as HTMLElement).textContent = `${operator} drone pad`;
    el.title = `${droneId} home pad · click to follow`;
    el.addEventListener("pointerdown", (e) => { e.stopPropagation(); this.onClick(droneId); });
    const label = new CSS2DObject(el);
    label.position.set(0, 0, PAD_R + 1);     // just south of the disc, hanging below the anchor
    label.center.set(0.5, 0);
    root.add(label);

    this.group.add(root);
    this.pads.set(droneId, { droneId, operator, x, y, root });
  }
}
