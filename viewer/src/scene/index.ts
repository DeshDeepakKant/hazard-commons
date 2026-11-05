// Renderer, lights, camera modes (orbit / follow robot / cinematic), picking.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import type { WorldData } from "./world";

export type CamMode = "orbit" | "follow" | "cinematic";

export class SceneView {
  renderer: THREE.WebGLRenderer;
  labels: CSS2DRenderer;
  scene = new THREE.Scene();
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  mode: CamMode = "orbit";
  followId: string | null = null;
  getRobotPos: (id: string) => THREE.Vector3 | null = () => null;
  private center: THREE.Vector3;
  private extent: number;
  private fly: { from: THREE.Vector3; to: THREE.Vector3; camFrom: THREE.Vector3; camTo: THREE.Vector3; t: number; dur: number } | null = null;
  private cineFocus: { pos: THREE.Vector3; until: number } | null = null;
  private clockS = 0;
  pickables: THREE.Object3D[] = [];
  onPick: (obj: THREE.Intersection) => void = () => {};

  constructor(private host: HTMLElement, world: WorldData) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    host.appendChild(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = "labels-layer";
    host.appendChild(this.labels.domElement);

    const b = world.bounds;
    this.center = new THREE.Vector3((b.minX + b.maxX) / 2, 0, -(b.minY + b.maxY) / 2);
    this.extent = Math.max(b.maxX - b.minX, b.maxY - b.minY, 400);

    const bg = new THREE.Color("#0d1420");
    this.scene.background = bg;
    this.scene.fog = new THREE.Fog(bg, this.extent * 0.9, this.extent * 2.6);

    const hemi = new THREE.HemisphereLight("#c7d4f0", "#1a2130", 1.5);
    const sun = new THREE.DirectionalLight("#ffe4c2", 2.4);
    sun.position.set(-600, 900, 400);
    const rim = new THREE.DirectionalLight("#6d8dff", 0.6);
    rim.position.set(700, 300, -600);
    this.scene.add(hemi, sun, rim, new THREE.AmbientLight("#ffffff", 0.15));

    this.camera = new THREE.PerspectiveCamera(42, 1, 1, this.extent * 6);
    this.camera.position.copy(this.center).add(new THREE.Vector3(-this.extent * 0.35, this.extent * 0.55, this.extent * 0.6));
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(this.center);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.minDistance = 15;
    this.controls.maxDistance = this.extent * 2.2;
    this.controls.screenSpacePanning = false;
    this.controls.addEventListener("start", () => {
      this.fly = null;
      if (this.mode === "cinematic") this.setMode("orbit");
    });

    // click (not drag) picking
    let down: { x: number; y: number } | null = null;
    const el = this.renderer.domElement;
    el.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; });
    el.addEventListener("pointerup", (e) => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const r = el.getBoundingClientRect();
      const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      const rc = new THREE.Raycaster();
      rc.setFromCamera(ndc, this.camera);
      const hits = rc.intersectObjects(this.pickables, false);
      if (hits.length) this.onPick(hits[0]);
    });

    new ResizeObserver(() => this.resize()).observe(host);
    this.resize();
  }

  resize(): void {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = w + "px";
    this.renderer.domElement.style.height = h + "px";
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  setMode(m: CamMode, followId: string | null = null): void {
    this.mode = m;
    this.followId = m === "follow" ? followId : null;
    this.controls.autoRotate = m === "cinematic";
    this.controls.autoRotateSpeed = 0.35;
    if (m === "follow" && followId) {
      const p = this.getRobotPos(followId);
      if (p) this.flyTo(p, 90);
    }
    if (m === "cinematic") this.flyTo(this.center.clone(), this.extent * 0.75, 3);
    window.dispatchEvent(new CustomEvent("cammode", { detail: { mode: m, followId: this.followId } }));
  }

  /** cinematic: swing over to an interesting spot (e.g. robot that just asked 0G) */
  focus(pos: THREE.Vector3, holdS = 9): void {
    if (this.mode !== "cinematic") return;
    this.cineFocus = { pos: pos.clone(), until: this.clockS + holdS };
    this.flyTo(pos, 160, 2.2);
  }

  flyTo(target: THREE.Vector3, dist: number, dur = 1.2): void {
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    if (dir.y < 0.35) { dir.y = 0.55; dir.normalize(); }
    this.fly = {
      from: this.controls.target.clone(), to: target.clone(),
      camFrom: this.camera.position.clone(), camTo: target.clone().add(dir.multiplyScalar(dist)),
      t: 0, dur,
    };
  }

  resetView(): void {
    this.setMode("orbit");
    this.flyTo(this.center.clone(), this.extent * 0.85);
  }

  update(dt: number): void {
    this.clockS += dt;
    if (this.fly) {
      this.fly.t += dt / this.fly.dur;
      const k = this.fly.t >= 1 ? 1 : 1 - Math.pow(1 - this.fly.t, 3);
      this.controls.target.lerpVectors(this.fly.from, this.fly.to, k);
      this.camera.position.lerpVectors(this.fly.camFrom, this.fly.camTo, k);
      if (this.fly.t >= 1) this.fly = null;
    } else if (this.mode === "follow" && this.followId) {
      const p = this.getRobotPos(this.followId);
      if (p) {
        const delta = p.clone().sub(this.controls.target).multiplyScalar(Math.min(1, dt * 4));
        this.controls.target.add(delta);
        this.camera.position.add(delta);
      }
    } else if (this.mode === "cinematic") {
      if (this.cineFocus && this.clockS > this.cineFocus.until) {
        this.cineFocus = null;
        this.flyTo(this.center.clone(), this.extent * 0.7, 4);
      }
    }
    this.controls.update(dt);
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }
}
