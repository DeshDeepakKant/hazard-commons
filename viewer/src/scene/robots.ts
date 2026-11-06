// Delivery rovers + drones in operator colours, FSM badges (CSS2D), optional route polylines.
import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import type { RobotState } from "../types";
import { interpolate, type Pose } from "../interp";
import { opColor } from "../config";
import { polylineForEdges, type WorldData } from "./world";
import { Pads } from "./pads";

const ROVER_SCALE = 2.6;   // exaggerated so they read at city scale
const DRONE_SCALE = 4.6;
const DRONE_MIN_PX_DIST = 170;   // beyond this camera distance the drone is scaled up to keep its screen size
const DRONE_MAX_BOOST = 5;
const FLYING_ALT = 1.5;          // m; below this a drone counts as landed
const BEACON_ABOVE = 90;         // light column reaches this far above the drone (shows over rooftops)
const TRAIL_N = 48;              // trail points
const TRAIL_STEP = 3;            // m between trail points

/** vertical alpha gradient (opaque at the bottom, clear at the top) for light columns */
let beamAlpha: THREE.CanvasTexture | null = null;
function beamAlphaMap(): THREE.CanvasTexture {
  if (beamAlpha) return beamAlpha;
  const c = document.createElement("canvas");
  c.width = 4; c.height = 128;
  const g = c.getContext("2d")!;
  const gr = g.createLinearGradient(0, 0, 0, 128);
  gr.addColorStop(0, "#000"); gr.addColorStop(0.35, "#888"); gr.addColorStop(1, "#fff");
  g.fillStyle = gr; g.fillRect(0, 0, 4, 128);
  beamAlpha = new THREE.CanvasTexture(c);
  return beamAlpha;
}

let haloTex: THREE.CanvasTexture | null = null;
function haloTexture(): THREE.CanvasTexture {
  if (haloTex) return haloTex;
  const s = 64, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d")!;
  const gr = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(0.25, "rgba(255,255,255,0.85)");
  gr.addColorStop(0.5, "rgba(255,255,255,0.25)"); gr.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = gr; g.fillRect(0, 0, s, s);
  haloTex = new THREE.CanvasTexture(c);
  return haloTex;
}

/** extras that make a drone findable: light column, see-through halo, ground trail */
interface DroneFx {
  beacon: THREE.Mesh;
  halo: THREE.Sprite;
  trail: THREE.Line;
  trailPts: Float32Array;   // xyz * TRAIL_N, newest first
  trailLen: number;
  hazardId: string | null;  // hazard of the current verify mission
  edge: string | null;      // edge named by the mission note (until the hazard id is known)
  note: string;
  text: string;
}

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
  fx?: DroneFx;
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
  /** drone landing pads (add `pads.group` to the scene) */
  pads = new Pads();
  /** set by main: used to keep drones a minimum on-screen size */
  camera: THREE.Camera | null = null;
  /** set by main: hazard id for an edge id (drone notes name the edge while verifying) */
  hazardForEdge: (edgeId: string) => string | null = () => null;

  constructor(private world: WorldData) {
    this.group.name = "robots";
    this.routeGroup.name = "routes";
    this.pads.onClick = (id) => this.onClick(id);
  }

  droneIds(): string[] {
    return [...this.views.values()].filter((v) => v.kind === "drone").map((v) => v.id).sort();
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
    labelEl.dataset.kind = r.kind;
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
      v.fx = this.droneFx(color);
      root.add(v.fx.halo);
      this.group.add(v.fx.beacon, v.fx.trail);
    }
    this.group.add(root);
    this.views.set(r.id, v);
    return v;
  }

  private droneFx(color: string): DroneFx {
    const bg = new THREE.CylinderGeometry(1.3, 1.3, 1, 12, 1, true);
    bg.translate(0, 0.5, 0);   // base at y = 0, scaled in y to the column height
    const beacon = new THREE.Mesh(bg, new THREE.MeshBasicMaterial({
      color, alphaMap: beamAlphaMap(), transparent: true, opacity: 0.8, depthWrite: false, side: THREE.DoubleSide,
    }));
    beacon.frustumCulled = false;
    beacon.renderOrder = 3;
    // constant screen size, drawn through buildings so the drone can be spotted anywhere
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: haloTexture(), color, transparent: true, depthTest: false, depthWrite: false,
      sizeAttenuation: false, blending: THREE.AdditiveBlending,
    }));
    halo.scale.setScalar(0.045);
    halo.renderOrder = 10;
    const trailPts = new Float32Array(TRAIL_N * 3);
    const tg = new THREE.BufferGeometry();
    tg.setAttribute("position", new THREE.BufferAttribute(trailPts, 3).setUsage(THREE.DynamicDrawUsage));
    const c = new THREE.Color(color), cols = new Float32Array(TRAIL_N * 4);
    for (let i = 0; i < TRAIL_N; i++) cols.set([c.r, c.g, c.b, 0.9 * (1 - i / TRAIL_N)], i * 4);
    tg.setAttribute("color", new THREE.BufferAttribute(cols, 4));
    tg.setDrawRange(0, 0);
    const trail = new THREE.Line(tg, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }));
    trail.frustumCulled = false;
    return { beacon, halo, trail, trailPts, trailLen: 0, hazardId: null, edge: null, note: "", text: "" };
  }

  private updateDrone(v: RobotView, r: RobotState, p: Pose, alt: number, nowS: number): void {
    const fx = v.fx!;
    const flying = p.z > FLYING_ALT;
    // keep a minimum screen size when the camera is far away
    let boost = 1, fade = 1;
    if (this.camera) {
      const d = this.camera.position.distanceTo(v.root.position);
      boost = Math.min(DRONE_MAX_BOOST, Math.max(1, d / DRONE_MIN_PX_DIST));
      fade = Math.min(1, Math.max(0.15, (d - 60) / 350));   // the column is for finding: faint up close
    }
    v.model.scale.setScalar(DRONE_SCALE * boost);
    v.label.position.y = (flying ? 2.2 : 5) + 1.6 * boost;   // landed: clear of the pad marker
    // light column from the ground through the drone; pulses while parked on the pad
    const b = fx.beacon;
    b.position.set(p.x, 0, -p.y);
    b.scale.set(boost, alt + BEACON_ABOVE, boost);
    (b.material as THREE.MeshBasicMaterial).opacity = fade * (flying ? 0.85 : 0.6 + 0.25 * Math.sin(nowS * 3.2));
    fx.halo.scale.setScalar(flying ? 0.045 : 0.034 + 0.008 * Math.sin(nowS * 3.2));
    // altitude cues only while airborne
    v.tether!.visible = flying;
    v.shadow!.visible = flying;
    (v.shadow!.material as THREE.MeshBasicMaterial).opacity = 0.35 - Math.min(0.2, p.z / 250);
    v.shadow!.scale.setScalar(boost);
    // trail: a point every TRAIL_STEP m; cleared on landing or a replay seek (jump)
    const t = fx.trailPts;
    if (!flying) fx.trailLen = 0;
    else {
      const dx = p.x - t[0], dy = alt - t[1], dz = -p.y - t[2];
      const d2 = dx * dx + dy * dy + dz * dz;
      if (fx.trailLen === 0 || d2 > 60 * 60) { t[0] = p.x; t[1] = alt; t[2] = -p.y; fx.trailLen = 1; }
      else if (d2 > TRAIL_STEP * TRAIL_STEP) {
        t.copyWithin(3, 0, (TRAIL_N - 1) * 3);
        t[0] = p.x; t[1] = alt; t[2] = -p.y;
        fx.trailLen = Math.min(TRAIL_N, fx.trailLen + 1);
      }
      (fx.trail.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    }
    fx.trail.geometry.setDrawRange(0, fx.trailLen);
    // label: "D-01 · flying 38 m · verifying hazard #3"
    const note = r.note ?? "";
    if (note !== fx.note) {
      fx.note = note;
      const m = /hazard #(\w+)/.exec(note);
      const e = /^verifying .* on (\S+)$/.exec(note);   // a new mission names the edge, not the id
      if (m) { fx.hazardId = m[1]; fx.edge = null; }
      else if (e) { fx.hazardId = null; fx.edge = e[1]; }
    }
    if (r.fsm_state === "IDLE") { fx.hazardId = null; fx.edge = null; }
    if (!fx.hazardId && fx.edge) fx.hazardId = this.hazardForEdge(fx.edge);   // hazard may be filed later
    const hz = fx.hazardId ? `hazard #${fx.hazardId}` : "hazard";
    const where = flying ? `flying ${Math.round(p.z)} m` : "on pad";
    const what: Record<string, string> = {
      IDLE: "idle", VERIFYING: flying ? `verifying ${hz}` : `taking off · ${hz}`,
      WAITING_FOR_BRAIN: `asking 0G · ${hz}`, SAFE_STOP: `SAFE STOP · ${hz}`,
      RETURNING: "returning to pad", LANDING: "landing",
    };
    const text = `${where} · ${what[r.fsm_state] ?? FSM_LABEL[r.fsm_state] ?? r.fsm_state}`;
    if (text !== fx.text) { fx.text = text; v.stateEl.textContent = "· " + text; }
  }

  update(a: RobotState[], b: RobotState[], alpha: number, nowS: number): void {
    const seen = new Set<string>();
    interpolate(a, b, alpha, (r, p) => {
      seen.add(r.id);
      if (r.kind === "drone") this.pads.observe(r);
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
        this.updateDrone(v, r, p, alt, nowS);
      } else if (r.fsm_state === "SAFE_STOP") {
        v.model.position.y = 0;
      }
      // state badge
      if (v.lastState !== r.fsm_state) {
        v.lastState = r.fsm_state;
        v.labelEl.dataset.state = r.fsm_state;
        if (!isDrone) v.stateEl.textContent = FSM_LABEL[r.fsm_state] ?? r.fsm_state;
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
        if (v.fx) this.group.remove(v.fx.beacon, v.fx.trail);
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
