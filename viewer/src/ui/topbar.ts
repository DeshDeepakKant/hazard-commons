// Top bar: brand, mode, sim clock, 0G brain indicator, outage toggle, view controls.
import { CONFIG } from "../config";
import { el, fmtClock } from "./dom";

export interface TopbarHandlers {
  onOutage: (on: boolean) => Promise<boolean>;
  onRoutes: (on: boolean) => void;
  onSatellite?: (on: boolean) => void;
  onCamera: (mode: "orbit" | "cinematic" | "reset") => void;
  onFollow: (id: string | null) => void;
  onOrder: () => void;
}

export class Topbar {
  root: HTMLElement;
  private clockEl: HTMLElement;
  private brainEl: HTMLElement;
  private connEl: HTMLElement;
  private outage: HTMLInputElement | null;
  private followSel: HTMLSelectElement;
  private robotsKey = "";

  constructor(host: HTMLElement, private h: TopbarHandlers) {
    const live = CONFIG.mode === "live";
    this.root = el("header", "topbar");
    this.root.innerHTML = `
      <div class="brand"><span class="logo">◆</span><div><b>Hazard Commons</b><small>Bandra West · rival robots, one shared truth</small></div></div>
      <div class="mode ${live ? "live" : "replay"}"><span class="dot"></span><span class="conn">${live ? "LIVE" : "REPLAY"}</span></div>
      <div class="clock" title="simulation time"><span>SIM</span><b>00:00</b></div>
      <div class="brain on" title="StateSnapshot.brain_online"><span class="pulse"></span><span class="txt">0G brain online</span></div>
      ${live ? `<label class="switch" title="POST /api/brain/unreachable"><input type="checkbox" class="outage"><span class="slider"></span><span>Simulate 0G outage</span></label>` : ""}
      <div class="spacer"></div>
      <div class="ctrls">
        <label class="tog"><input type="checkbox" class="satellite" checked> Satellite</label>
        <label class="tog"><input type="checkbox" class="routes" checked> Routes</label>
        <select class="follow" title="Follow a robot"><option value="">Follow robot…</option></select>
        <button class="cine" title="Auto-orbit camera for recording (C)">🎬 Cinematic</button>
        <button class="reset" title="Reset view (R)">⟲</button>
        <button class="order primary">${live ? "🛒 Place order" : "🧾 Orders"}</button>
      </div>`;
    host.appendChild(this.root);
    this.clockEl = this.root.querySelector(".clock b")!;
    this.brainEl = this.root.querySelector(".brain")!;
    this.connEl = this.root.querySelector(".mode")!;
    this.outage = this.root.querySelector<HTMLInputElement>("input.outage");
    this.followSel = this.root.querySelector("select.follow")!;
    this.outage?.addEventListener("change", async () => {
      const want = this.outage!.checked;
      this.outage!.disabled = true;
      const ok = await h.onOutage(want);
      if (!ok) this.outage!.checked = !want;
      this.outage!.disabled = false;
    });
    this.root.querySelector<HTMLInputElement>("input.satellite")?.addEventListener("change", (e) => h.onSatellite?.((e.target as HTMLInputElement).checked));
    this.root.querySelector<HTMLInputElement>("input.routes")!.addEventListener("change", (e) => h.onRoutes((e.target as HTMLInputElement).checked));
    this.root.querySelector(".cine")!.addEventListener("click", () => h.onCamera("cinematic"));
    this.root.querySelector(".reset")!.addEventListener("click", () => h.onCamera("reset"));
    this.root.querySelector(".order")!.addEventListener("click", () => h.onOrder());
    this.followSel.addEventListener("change", () => h.onFollow(this.followSel.value || null));
    window.addEventListener("cammode", (e) => {
      const d = (e as CustomEvent).detail as { mode: string; followId: string | null };
      this.root.querySelector(".cine")!.classList.toggle("on", d.mode === "cinematic");
      this.followSel.value = d.followId ?? "";
    });
  }

  setClock(t: number): void { this.clockEl.textContent = fmtClock(t); }

  setBrain(online: boolean): void {
    this.brainEl.classList.toggle("on", online);
    this.brainEl.classList.toggle("off", !online);
    this.brainEl.querySelector(".txt")!.textContent = online ? "0G brain online" : "0G brain OFFLINE — robots in SAFE STOP";
  }

  setConn(state: "connecting" | "open" | "closed" | "replay" | "error", text?: string): void {
    this.connEl.dataset.state = state;
    this.connEl.querySelector(".conn")!.textContent = text ?? (CONFIG.mode === "live" ? (state === "open" ? "LIVE" : state === "connecting" ? "CONNECTING…" : "OFFLINE") : "REPLAY");
  }

  setRobots(ids: { id: string; operator: string }[]): void {
    const key = ids.map((r) => r.id).join(",");
    if (key === this.robotsKey) return;
    this.robotsKey = key;
    const cur = this.followSel.value;
    this.followSel.innerHTML = `<option value="">Follow robot…</option>` + ids.map((r) => `<option value="${r.id}">${r.id} · ${r.operator}</option>`).join("");
    this.followSel.value = cur;
  }
}
