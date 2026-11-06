// "Add a disaster": top-bar popover (type, real/fake, random, place on map) + list of injected disasters.
import * as THREE from "three";
import { el, esc } from "./dom";
import { clearInjectedHazard, injectHazard, injectRandomHazard, listHazardProps, type InjectedProp } from "../live";

type Toast = (html: string, cls?: string) => void;
const ICON: Record<string, string> = { accident: "💥", road_works: "🚧", waterlogging: "🌊" };

export interface DisasterDeps {
  topbarRoot: HTMLElement;
  dom: HTMLElement;              // renderer canvas
  camera: THREE.Camera;
  scene: THREE.Scene;
  toast: Toast;
  flyTo?: (x: number, y: number) => void;   // sim x,y metres
}

export class DisasterControl {
  private pop: HTMLElement;
  private btn: HTMLButtonElement;
  private placing = false;
  private ghost: THREE.Group;
  private list: HTMLElement;
  private timer: number | undefined;
  private plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

  constructor(private d: DisasterDeps) {
    this.btn = el("button", "disaster-btn", "🌪 Disaster");
    this.btn.title = "Add a disaster to the map";
    d.topbarRoot.querySelector(".ctrls")!.insertBefore(this.btn, d.topbarRoot.querySelector(".verify"));
    this.pop = el("div", "disaster-pop", `
      <h3>Add a disaster</h3>
      <div class="row types">
        <label><input type="radio" name="dtype" value="accident" checked> 💥 Accident</label>
        <label><input type="radio" name="dtype" value="road_works"> 🚧 Road works</label>
        <label><input type="radio" name="dtype" value="waterlogging"> 🌊 Waterlogging</label>
      </div>
      <label class="row"><input type="checkbox" class="fake"> Fake report (nothing there; rivals should dispute it)</label>
      <div class="row btns"><button class="rand">🎲 Random disaster</button><button class="place primary">📍 Place on map</button></div>
      <p class="hint muted small">Real: a rover meets it, 0G decides (CP-H1), the report goes on-chain, a rival drone verifies. Esc cancels placing.</p>
      <h4>Active</h4><div class="dlist"><span class="muted small">none</span></div>`);
    this.pop.hidden = true;
    document.body.appendChild(this.pop);
    this.list = this.pop.querySelector(".dlist")!;

    this.ghost = new THREE.Group();
    const ring = new THREE.Mesh(new THREE.RingGeometry(5, 7, 40).rotateX(-Math.PI / 2), new THREE.MeshBasicMaterial({ color: 0xf97316, transparent: true, opacity: 0.85, depthTest: false }));
    const dot = new THREE.Mesh(new THREE.CylinderGeometry(1.2, 1.2, 14, 12), new THREE.MeshBasicMaterial({ color: 0xf97316, transparent: true, opacity: 0.6, depthTest: false }));
    dot.position.y = 7; ring.renderOrder = dot.renderOrder = 999;
    this.ghost.add(ring, dot);
    this.ghost.visible = false;
    d.scene.add(this.ghost);

    this.btn.addEventListener("click", () => this.toggle());
    this.pop.querySelector(".rand")!.addEventListener("click", () => void this.random());
    this.pop.querySelector(".place")!.addEventListener("click", () => this.startPlacing());
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (this.placing) this.stopPlacing(); else this.toggle(false); } });

    let down: { x: number; y: number } | null = null;
    d.dom.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; }, true);
    d.dom.addEventListener("pointermove", (e) => { if (this.placing) this.moveGhost(e); });
    d.dom.addEventListener("pointerup", (e) => {
      if (!this.placing || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      e.stopImmediatePropagation();
      const p = this.groundPoint(e);
      if (p) void this.dropAt(p);
    }, true);
  }

  private get type(): string { return this.pop.querySelector<HTMLInputElement>("input[name=dtype]:checked")!.value; }
  private get real(): boolean { return !this.pop.querySelector<HTMLInputElement>("input.fake")!.checked; }

  toggle(on?: boolean): void {
    const want = on ?? !!this.pop.hidden;
    this.pop.hidden = !want;
    this.btn.classList.toggle("on", want);
    window.clearInterval(this.timer);
    if (want) {
      const r = this.btn.getBoundingClientRect();
      this.pop.style.top = `${r.bottom + 6}px`;
      this.pop.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
      void this.refresh();
      this.timer = window.setInterval(() => void this.refresh(), 2500);
    }
  }

  private startPlacing(): void {
    this.placing = true;
    this.d.dom.classList.add("placing-disaster");
    this.toggle(false);
    this.d.toast(`📍 Click the map to drop ${esc(this.type.replace("_", " "))}. Esc cancels.`);
  }
  private stopPlacing(): void {
    this.placing = false;
    this.ghost.visible = false;
    this.d.dom.classList.remove("placing-disaster");
  }

  private groundPoint(e: PointerEvent): THREE.Vector3 | null {
    const r = this.d.dom.getBoundingClientRect();
    const rc = new THREE.Raycaster();
    rc.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.d.camera);
    const out = new THREE.Vector3();
    return rc.ray.intersectPlane(this.plane, out) ? out : null;
  }
  private moveGhost(e: PointerEvent): void {
    const p = this.groundPoint(e);
    if (p) { this.ghost.position.set(p.x, 0.3, p.z); this.ghost.visible = true; }
  }

  private async dropAt(p: THREE.Vector3): Promise<void> {
    const type = this.type, real = this.real;
    this.stopPlacing();
    // three: x = sim x, z = -sim y
    this.report(await injectHazard({ type, real, x: p.x, y: -p.z }));
  }
  private async random(): Promise<void> {
    this.report(await injectRandomHazard(this.real));
  }
  private report(r: { ok: boolean; data?: { type: string; road_name: string; real: boolean }; error?: string }): void {
    if (!r.ok || !r.data) { this.d.toast(`Could not add disaster: ${esc(r.error ?? "unknown error")}`, "err"); return; }
    this.d.toast(`${ICON[r.data.type] ?? "⚠️"} ${r.data.real ? "" : "FAKE "}${esc(r.data.type.replace("_", " "))} added${r.data.road_name ? ` on ${esc(r.data.road_name)}` : ""}`, "hz-reported");
    void this.refresh();
  }

  async refresh(): Promise<void> {
    const r = await listHazardProps();
    if (!r.ok || !r.data) { this.list.innerHTML = `<span class="muted small">${esc(r.error ?? "unavailable")}</span>`; return; }
    const mine = r.data.props.filter((p: InjectedProp) => p.injected);
    if (!mine.length) { this.list.innerHTML = `<span class="muted small">none</span>`; return; }
    this.list.innerHTML = mine.map((p) => `<div class="drow">
      <span>${ICON[p.type] ?? "⚠️"} <b>${esc(p.type.replace("_", " "))}</b>${p.real ? "" : " <i>(fake)</i>"} · ${esc(p.road_name || p.edge_id)}
      <small class="muted">${esc(p.hazard_status ?? (p.reported ? "reported" : "waiting for a rover"))}</small></span>
      <button class="go" data-x="${p.x}" data-y="${p.y}" title="Fly to">⌖</button>
      <button class="clr" data-id="${esc(p.prop_id)}">Clear</button></div>`).join("");
    this.list.querySelectorAll<HTMLButtonElement>("button.clr").forEach((b) => b.addEventListener("click", async () => {
      const c = await clearInjectedHazard(b.dataset.id!);
      this.d.toast(c.ok ? "✅ Disaster cleared" : `Could not clear: ${esc(c.error ?? "")}`, c.ok ? "ok" : "err");
      void this.refresh();
    }));
    this.list.querySelectorAll<HTMLButtonElement>("button.go").forEach((b) => b.addEventListener("click", () => this.d.flyTo?.(Number(b.dataset.x), Number(b.dataset.y))));
  }
}
