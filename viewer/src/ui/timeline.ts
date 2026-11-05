// Replay timeline: play/pause, speed, scrubber with jump-to-event markers.
import type { ReplayPlayer } from "../replay";
import { el, esc, fmtClock } from "./dom";

export class Timeline {
  root: HTMLElement;
  private track: HTMLElement;
  private head: HTMLElement;
  private fill: HTMLElement;
  private timeEl: HTMLElement;
  private playBtn: HTMLButtonElement;
  private dragging = false;

  constructor(host: HTMLElement, private p: ReplayPlayer) {
    this.root = el("div", "timeline");
    this.root.innerHTML = `
      <button class="play" title="Play/pause (space)">❚❚</button>
      <div class="speeds">${[1, 2, 4].map((s) => `<button data-s="${s}" class="${s === 1 ? "on" : ""}">${s}×</button>`).join("")}</div>
      <span class="time">00:00 / 00:00</span>
      <div class="track"><div class="fill"></div><div class="markers"></div><div class="head"></div></div>
      <div class="legend"><span class="lg decision">0G decision</span><span class="lg error">safe stop</span><span class="lg hazard">hazard</span><span class="lg tx">chain tx</span></div>`;
    host.appendChild(this.root);
    this.track = this.root.querySelector(".track")!;
    this.head = this.root.querySelector(".head")!;
    this.fill = this.root.querySelector(".fill")!;
    this.timeEl = this.root.querySelector(".time")!;
    this.playBtn = this.root.querySelector(".play")!;
    const mk = this.root.querySelector(".markers")!;
    mk.innerHTML = p.markers.map((m) => {
      const x = ((m.t - p.t0) / p.duration) * 100;
      return `<i class="mk ${m.kind}" style="left:${x.toFixed(3)}%" data-t="${m.t}" title="${fmtClock(m.t)} · ${esc(m.label)}"></i>`;
    }).join("");
    mk.addEventListener("click", (e) => {
      const t = (e.target as HTMLElement).dataset.t;
      if (t) { e.stopPropagation(); p.seek(Number(t) - 1.5); }
    });
    this.playBtn.addEventListener("click", () => this.togglePlay());
    this.root.querySelectorAll<HTMLButtonElement>(".speeds button").forEach((b) => b.addEventListener("click", () => {
      p.speed = Number(b.dataset.s);
      this.root.querySelectorAll(".speeds button").forEach((x) => x.classList.toggle("on", x === b));
    }));
    const seekAt = (clientX: number) => {
      const r = this.track.getBoundingClientRect();
      const k = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      p.seek(p.t0 + k * p.duration);
    };
    this.track.addEventListener("pointerdown", (e) => {
      if ((e.target as HTMLElement).classList.contains("mk")) return;
      this.dragging = true; this.track.setPointerCapture(e.pointerId); seekAt(e.clientX);
    });
    this.track.addEventListener("pointermove", (e) => { if (this.dragging) seekAt(e.clientX); });
    this.track.addEventListener("pointerup", () => { this.dragging = false; });
    window.addEventListener("keydown", (e) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if (e.code === "Space") { e.preventDefault(); this.togglePlay(); }
      if (e.code === "ArrowRight") p.seek(p.time + 5);
      if (e.code === "ArrowLeft") p.seek(p.time - 5);
    });
  }

  togglePlay(): void {
    if (!this.p.playing && this.p.time >= this.p.t1) this.p.seek(this.p.t0);
    this.p.playing = !this.p.playing;
  }

  update(): void {
    const k = (this.p.time - this.p.t0) / this.p.duration;
    this.head.style.left = `${k * 100}%`;
    this.fill.style.width = `${k * 100}%`;
    this.timeEl.textContent = `${fmtClock(this.p.time)} / ${fmtClock(this.p.t1)}`;
    this.playBtn.textContent = this.p.playing ? "❚❚" : "▶";
  }
}
