// "Pick on map" for the order drawer: crosshair cursor, a ring + address label snap to the nearest home under the
// pointer, click chooses it, Esc cancels. Same ground-plane raycast as the disaster placer.
import * as THREE from "three";
import type { Home } from "../scene/world";
import { el, esc } from "./dom";

export interface PickDeps {
  dom: HTMLElement;
  camera: THREE.Camera;
  scene: THREE.Scene;
  homes: Home[];
  toast: (msg: string, tone?: string) => void;
  onPick: (h: Home) => void;
}

const MAX_SNAP_M = 90;

export class MapPicker {
  private active = false;
  private ghost = new THREE.Group();
  private tip = el("div", "pick-tip");
  private plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
  private nearest: Home | null = null;

  constructor(private d: PickDeps) {
    const mat = (c: number, o: number) => new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: o, depthTest: false });
    const ring = new THREE.Mesh(new THREE.RingGeometry(4, 6, 40).rotateX(-Math.PI / 2), mat(0x38bdf8, 0.95));
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.9, 0.9, 40, 10), mat(0x38bdf8, 0.6));
    beam.position.y = 20; ring.renderOrder = beam.renderOrder = 999;
    this.ghost.add(ring, beam);
    this.ghost.visible = false;
    d.scene.add(this.ghost);
    this.tip.hidden = true;
    document.body.appendChild(this.tip);

    window.addEventListener("keydown", (e) => { if (e.key === "Escape" && this.active) this.stop(); });
    let down: { x: number; y: number } | null = null;
    d.dom.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; }, true);
    d.dom.addEventListener("pointermove", (e) => { if (this.active) this.hover(e); });
    d.dom.addEventListener("pointerup", (e) => {
      if (!this.active || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      e.stopImmediatePropagation();
      this.hover(e);
      const h = this.nearest;
      if (!h) { d.toast("No home near that spot: click closer to a building.", "err"); return; }
      this.stop();
      d.onPick(h);
    }, true);
  }

  get isActive(): boolean { return this.active; }

  start(): void {
    this.active = true;
    this.d.dom.classList.add("placing-order");
    this.d.toast("📍 Move over the map and click the building to deliver to. Esc cancels.");
  }

  stop(): void {
    this.active = false;
    this.nearest = null;
    this.ghost.visible = false;
    this.tip.hidden = true;
    this.d.dom.classList.remove("placing-order");
  }

  private hover(e: PointerEvent): void {
    const r = this.d.dom.getBoundingClientRect();
    const rc = new THREE.Raycaster();
    rc.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.d.camera);
    const p = new THREE.Vector3();
    if (!rc.ray.intersectPlane(this.plane, p)) return;
    // three: x = sim x, z = -sim y
    let best: Home | null = null, bd = MAX_SNAP_M;
    for (const h of this.d.homes) {
      const dd = Math.hypot(h.x - p.x, h.y + p.z);
      if (dd < bd) { bd = dd; best = h; }
    }
    this.nearest = best;
    if (!best) { this.ghost.visible = false; this.tip.hidden = true; return; }
    this.ghost.position.set(best.x, 0.3, -best.y);
    this.ghost.visible = true;
    this.tip.hidden = false;
    this.tip.innerHTML = `<b>${esc(best.customer_name)}</b><span>${esc(best.address)}</span>`;
    this.tip.style.left = `${e.clientX + 16}px`;
    this.tip.style.top = `${e.clientY + 14}px`;
  }
}
