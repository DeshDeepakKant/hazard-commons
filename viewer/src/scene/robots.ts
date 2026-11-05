// Delivery rovers + drones in operator colours, FSM badges (CSS2D), optional route polylines.
import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import type { RobotState } from "../types";
import { interpolate, type Pose } from "../interp";
import { opColor } from "../config";
import { polylineForEdges, type WorldData } from "./world";

const ROVER_SCALE = 2.6;   // exaggerated so they read at city scale
const DRONE_SCALE = 3.2;

export const FSM_LABEL: Record<string, string> = {
  IDLE: "idle", TO_STORE: "to store", LOADING: "loading", DELIVERING: "delivering", AT_DOOR: "at door",
  HANDED_OVER: "handed over", RETURNING: "returning", WAITING_FOR_BRAIN: "asking 0G", SAFE_STOP: "SAFE STOP",
  VERIFYING: "verifying", LANDING: "landing",
};

function roverModel(color: string): THREE.Group {
  const g = new THREE.Group();
  const c = new THREE.Color(color);
  const bodyMat = new THREE.MeshStandardMaterial({ color: c, roughness: 0.45, metalness: 0.2, emissive: c, emissiveIntensity: 0.18 });
  const dark = new THREE.MeshStandardMaterial({ color: "#1f2430", roughness: 0.8 });
  const white = new THREE.MeshStandardMaterial({ color: "#f1f5f9", roughness: 0.5 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(1.0, 0.35, 0.7), bodyMat);
  body.position.y = 0.32;
  const cargo = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.45, 0.6), white);
  cargo.position.set(-0.05, 0.72, 0);
  const lid = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.06, 0.62), bodyMat);
  lid.position.set(-0.05, 0.97, 0);
  const sensor = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.12, 10), dark);
  sensor.position.set(0.42, 0.56, 0);
  const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.06, 8, 6), new THREE.MeshBasicMaterial({ color: "#fef9c3" }));
  lamp.position.set(0.51, 0.34, 0);
  g.add(body, cargo, lid, sensor, lamp);
  const wheelG = new THREE.CylinderGeometry(0.16, 0.16, 0.1, 12);
  wheelG.rotateX(Math.PI / 2);
  for (const [x, z] of [[0.33, 0.38], [-0.33, 0.38], [0.33, -0.38], [-0.33, -0.38], [0, 0.38], [0, -0.38]]) {
    const w = new THREE.Mesh(wheelG, dark);
    w.position.set(x, 0.16, z);
    g.add(w);
  }
  // flag pole for visibility
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.9, 4), dark);
  pole.position.set(-0.4, 1.25, 0.25);
  const flag = new THREE.Mesh(new THREE.PlaneGeometry(0.28, 0.18), new THREE.MeshBasicMaterial({ color: c, side: THREE.DoubleSide }));
  flag.position.set(-0.26, 1.6, 0.25);
  g.add(pole, flag);
  g.scale.setScalar(ROVER_SCALE);
  return g;
}

function droneModel(color: string): { g: THREE.Group; rotors: THREE.Mesh[] } {
  const g = new THREE.Group();
  const c = new THREE.Color(color);
  const bodyMat = new THREE.MeshStandardMaterial({ color: c, roughness: 0.4, metalness: 0.3, emissive: c, emissiveIntensity: 0.25 });
  const dark = new THREE.MeshStandardMaterial({ color: "#1f2430", roughness: 0.7 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.18, 0.5), bodyMat);
  g.add(body);
  const rotors: THREE.Mesh[] = [];
  const rotorG = new THREE.CylinderGeometry(0.28, 0.28, 0.02, 16);
  const rotorM = new THREE.MeshBasicMaterial({ color: "#e2e8f0", transparent: true, opacity: 0.45 });
  for (const [x, z] of [[0.45, 0.45], [-0.45, 0.45], [0.45, -0.45], [-0.45, -0.45]]) {
    const arm = new THREE.Mesh(new THREE.BoxGeometry(0.64, 0.04, 0.05), dark);
    arm.position.set(x / 2, 0, z / 2);
    arm.rotation.y = Math.atan2(-z, x);
    const r = new THREE.Mesh(rotorG, rotorM);
    r.position.set(x, 0.08, z);
    g.add(arm, r);
    rotors.push(r);
  }
  const cam = new THREE.Mesh(new THREE.SphereGeometry(0.09, 8, 6), dark);
  cam.position.set(0.18, -0.13, 0);
  g.add(cam);
  g.scale.setScalar(DRONE_SCALE);
  return { g, rotors };
}

interface RobotView {
  id: string; kind: string; operator: string;
  root: THREE.Group;
  model: THREE.Group;
  rotors: THREE.Mesh[];
  label: CSS2DObject;
  labelEl: HTMLDivElement;
  stateEl: HTMLSpanElement;
  tether?: THREE.Line;
  shadow?: THREE.Mesh;
  ring: THREE.Mesh;
  route?: THREE.Line;
  routeKey: string;
  lastState: string;
  pose: Pose;
}

export class Robots {
  group = new THREE.Group();
  routeGroup = new THREE.Group();
  views = new Map<string, RobotView>();
  showRoutes = true;
  highlighted = new Set<string>();
  onClick: (id: string) => void = () => {};

  constructor(private world: WorldData) {
    this.group.name = "robots";
    this.routeGroup.name = "routes";
  }

  private create(r: RobotState): RobotView {
    const root = new THREE.Group();
    const color = opColor(r.operator);
    let model: THREE.Group, rotors: THREE.Mesh[] = [];
    if (r.kind === "drone") ({ g: model, rotors } = droneModel(color));
    else model = roverModel(color);
    root.add(model);

    const labelEl = document.createElement("div");
    labelEl.className = "robot-label";
    labelEl.style.setProperty("--op", color);
    labelEl.innerHTML = `<span class="chip"></span><span class="rid"></span><span class="st"></span>`;
    (labelEl.querySelector(".rid") as HTMLElement).textContent = r.id;
    const stateEl = labelEl.querySelector(".st") as HTMLSpanElement;
    labelEl.addEventListener("pointerdown", (e) => { e.stopPropagation(); this.onClick(r.id); });
    const label = new CSS2DObject(labelEl);
    label.position.set(0, r.kind === "drone" ? 3 : 5.2, 0);
    label.center.set(0.5, 1);
    root.add(label);

    // selection / highlight ring
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(3.2, 4.0, 40),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthWrite: false }),
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.15;
    ring.visible = false;
    root.add(ring);

    const v: RobotView = {
      id: r.id, kind: r.kind, operator: r.operator, root, model, rotors, label, labelEl, stateEl, ring,
      routeKey: "", lastState: "", pose: { x: r.x, y: r.y, z: r.z, yaw: r.yaw, speed: 0 },
    };
    if (r.kind === "drone") {
      const tg = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, -1, 0)]);
      v.tether = new THREE.Line(tg, new THREE.LineDashedMaterial({ color, dashSize: 1.5, gapSize: 1.2, transparent: true, opacity: 0.8 }));
      root.add(v.tether);
      v.shadow = new THREE.Mesh(new THREE.CircleGeometry(2.2, 24), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.25, depthWrite: false }));
      v.shadow.rotation.x = -Math.PI / 2;
      this.group.add(v.shadow);
    }
    this.group.add(root);
    this.views.set(r.id, v);
    return v;
  }

  update(a: RobotState[], b: RobotState[], alpha: number, nowS: number): void {
    const seen = new Set<string>();
    interpolate(a, b, alpha, (r, p) => {
      seen.add(r.id);
      const v = this.views.get(r.id) ?? this.create(r);
      v.pose = { ...p };
      const isDrone = r.kind === "drone";
      const alt = isDrone ? Math.max(p.z, 0.5) : 0;
      v.root.position.set(p.x, alt, -p.y);
      v.model.rotation.y = p.yaw;
      if (isDrone) {
        v.model.position.y = Math.sin(nowS * 2 + p.x) * 0.25;
        for (const ro of v.rotors) ro.rotation.y += 0.9;
        const pos = v.tether!.geometry.getAttribute("position") as THREE.BufferAttribute;
        pos.setXYZ(1, 0, -alt, 0); pos.needsUpdate = true;
        v.tether!.computeLineDistances();
        v.shadow!.position.set(p.x, 0.12, -p.y);
      } else if (r.fsm_state === "SAFE_STOP") {
        v.model.position.y = 0;
      }
      // state badge
      if (v.lastState !== r.fsm_state) {
        v.lastState = r.fsm_state;
        v.labelEl.dataset.state = r.fsm_state;
        v.stateEl.textContent = FSM_LABEL[r.fsm_state] ?? r.fsm_state;
      }
      const hl = this.highlighted.has(r.id);
      v.ring.visible = hl || r.fsm_state === "SAFE_STOP" || r.fsm_state === "WAITING_FOR_BRAIN";
      if (v.ring.visible) {
        const mat = v.ring.material as THREE.MeshBasicMaterial;
        if (r.fsm_state === "SAFE_STOP") mat.color.set("#f59e0b");
        else if (r.fsm_state === "WAITING_FOR_BRAIN") mat.color.set("#a78bfa");
        else mat.color.set(opColor(r.operator));
        const s = 1 + 0.25 * Math.sin(nowS * 5);
        v.ring.scale.setScalar(s);
        mat.opacity = 0.55 + 0.35 * Math.sin(nowS * 5);
        v.ring.position.y = isDrone ? -alt + 0.15 : 0.15;
      }
      v.labelEl.classList.toggle("hl", hl);
      this.updateRoute(v, r);
    });
    for (const [id, v] of this.views) {
      if (!seen.has(id)) {
        this.group.remove(v.root);
        if (v.shadow) this.group.remove(v.shadow);
        if (v.route) this.routeGroup.remove(v.route);
        v.label.element.remove();
        this.views.delete(id);
      }
    }
  }

  private updateRoute(v: RobotView, r: RobotState): void {
    const key = (r.route_edge_ids ?? []).join(",");
    if (key !== v.routeKey) {
      v.routeKey = key;
      if (v.route) { this.routeGroup.remove(v.route); v.route.geometry.dispose(); v.route = undefined; }
      const pts = polylineForEdges(this.world, r.route_edge_ids ?? []);
      if (pts.length >= 2) {
        const y = r.kind === "drone" ? Math.max(r.z, 1) : 0.6;
        const g = new THREE.BufferGeometry().setFromPoints(pts.map(([x, yy]) => new THREE.Vector3(x, y, -yy)));
        v.route = new THREE.Line(g, new THREE.LineBasicMaterial({ color: opColor(r.operator), transparent: true, opacity: 0.85 }));
        this.routeGroup.add(v.route);
      }
    }
    if (v.route) v.route.visible = this.showRoutes;
  }

  position(id: string): THREE.Vector3 | null {
    const v = this.views.get(id);
    return v ? v.root.position.clone() : null;
  }
}
