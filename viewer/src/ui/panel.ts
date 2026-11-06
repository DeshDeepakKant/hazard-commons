// Live decision panel: one card per 0G checkpoint decision, newest on top, updated in place.
import type { Card } from "../store";
import { store } from "../store";
import { CHECKPOINT_LABEL, HAZARD_LABEL, opColor } from "../config";
import { opFilter } from "./filter";
import { esc, el, fmtClock, pct } from "./dom";
import { walletBadge } from "./wallets";
import { copy, isRealHash, openStorage, short, txUrl } from "./explorer";

const MAX_CARDS = 120;

export function summarize(c: Card): { verdict: string; tone: string; detail: string } {
  const r = c.ev.result;
  const o = (r.output ?? {}) as Record<string, unknown>;
  if (!r.ok) return { verdict: "SAFE STOP — 0G unreachable, nothing guessed", tone: "err", detail: r.error ?? "" };
  switch (r.checkpoint) {
    case "CP-H1": {
      if (o.is_hazard === false || o.type === "none") return { verdict: "NO HAZARD · proceed", tone: "ok", detail: String(o.description ?? "") };
      const pf = (o.passable_for as string[] | undefined) ?? [];
      const t = (HAZARD_LABEL[String(o.type)] ?? String(o.type)).toUpperCase();
      const sev = typeof o.severity === "number" ? o.severity.toFixed(1) : "?";
      return {
        verdict: `${t} · severity ${sev} · rovers ${pf.includes("rover") ? "✓" : "✗"} drones ${pf.includes("drone") ? "✓" : "✗"}`,
        tone: "hazard", detail: String(o.description ?? ""),
      };
    }
    case "CP-H2": {
      const a = String(o.action ?? "?");
      const act = a === "handoff_to_drone" ? "HAND-OFF TO DRONE" : a.toUpperCase();
      return { verdict: `${act}: ${o.trust_report ? "trusts report" : "doesn't trust report"}`, tone: a === "proceed" ? "ok" : "route", detail: String(o.reason ?? "") };
    }
    case "CP-V": {
      const v = String(o.verdict ?? "?");
      const obs = String(o.observed_type ?? "none");
      return v === "confirm"
        ? { verdict: `CONFIRM: ${(HAZARD_LABEL[obs] ?? obs).toLowerCase()} observed`, tone: "hazard", detail: String(o.reason ?? "") }
        : { verdict: `DISPUTE: ${obs === "none" ? "no hazard observed" : (HAZARD_LABEL[obs] ?? obs).toLowerCase() + " observed"}`, tone: "dispute", detail: String(o.reason ?? "") };
    }
    case "CP-Q": {
      const src = /\(simulated\)/.test(c.ev.action_taken) ? " · SIMULATED bystander" : "";
      const eta = typeof o.eta_minutes === "number" ? ` · ~${o.eta_minutes} min` : "";
      return {
        verdict: `INQUIRY: ${String(o.what_happened ?? "?")}${eta}${src}`,
        tone: o.agrees_with_photo === false ? "dispute" : "hazard",
        detail: [o.summary_hi, o.summary_en].filter(Boolean).join(" / "),
      };
    }
    case "CP-D":
      return o.at_correct_location
        ? { verdict: "AT CORRECT DOOR ✓", tone: "ok", detail: "" }
        : { verdict: "WRONG LOCATION ✗", tone: "dispute", detail: "" };
  }
  return { verdict: JSON.stringify(o), tone: "", detail: "" };
}

const DEFAULT_MODEL = "0gm-1.0-35b-a3b";

function trustMode(r: { zg_trace: Record<string, unknown> | null }): string | null {
  const hc = (r.zg_trace?._hc ?? null) as Record<string, unknown> | null;
  return typeof hc?.trust_mode === "string" ? hc.trust_mode : null;
}

function modelBadge(r: { model: string | null; zg_trace: Record<string, unknown> | null }): string {
  const tm = trustMode(r);
  const noTee = tm !== null && tm !== "private";
  const other = r.model && !r.model.startsWith(DEFAULT_MODEL);
  return `${other ? `<span class="badge model" title="0G model used for this checkpoint">${esc(r.model)}</span>` : ""}${
    noTee ? `<span class="badge tee bad" title="0G Compute, but a non-TEE provider (trust mode ${esc(tm)})">NO TEE</span>` : ""}`;
}

function teeBadge(v: boolean | null): string {
  if (v === true) return `<span class="badge tee ok" title="0G TEE attestation verified">✓ TEE</span>`;
  if (v === false) return `<span class="badge tee bad" title="TEE verification failed">✗ TEE</span>`;
  return `<span class="badge tee na" title="TEE not reported">– TEE</span>`;
}

function render(c: Card, node: HTMLElement): void {
  const ev = c.ev, r = ev.result;
  const s = summarize(c);
  const color = opColor(ev.operator);
  node.className = `card tone-${s.tone}${r.ok ? "" : " error"}${r.mock ? " mock" : ""}`;
  node.style.setProperty("--op", color);
  node.dataset.robot = r.robot_id;
  const thumb = ev.frame_thumb_b64
    ? `<img class="thumb" alt="checkpoint frame" src="${ev.frame_thumb_b64.startsWith("data:") ? esc(ev.frame_thumb_b64) : `data:image/${ev.frame_thumb_b64.startsWith("iVBOR") ? "png" : ev.frame_thumb_b64.startsWith("UklG") ? "webp" : "jpeg"};base64,` + esc(ev.frame_thumb_b64)}">`
    : `<div class="thumb none">${r.ok ? "no frame" : "⛔"}</div>`;
  const lat = r.latency_ms != null ? `${(r.latency_ms / 1000).toFixed(2)} s` : "–";
  const conf = (r.output as Record<string, unknown> | null)?.confidence;
  const req = r.zg_request_id;
  const storage = c.storageRoot
    ? `<a href="#" class="hash storage-link" title="${esc(c.storageRoot)}">${esc(short(c.storageRoot))} ↗</a><button class="cp" data-copy="${esc(c.storageRoot)}" title="copy root hash">⧉</button>`
    : `<span class="pending">${r.ok ? "pending upload…" : "–"}</span>`;
  const tx = c.chainTx
    ? `${isRealHash(c.chainTx) ? `<a class="hash" target="_blank" rel="noopener" href="${txUrl(c.chainTx)}" title="${esc(c.chainTx)}">${esc(short(c.chainTx))} ↗</a>` : `<span class="hash">${esc(short(c.chainTx))}</span>`}<span class="txst st-${c.chainStatus ?? "sent"}">${esc(c.chainKind ?? "")} · ${esc(c.chainStatus ?? "sent")}</span>`
    : `<span class="pending">–</span>`;
  node.innerHTML = `
    <div class="card-head">
      <span class="chip"></span><b class="rid">${esc(r.robot_id)}</b><span class="op">${esc(ev.operator)}</span>
      <span class="t">t ${fmtClock(ev.sim_time)}</span>
      ${r.mock ? `<span class="badge mock" title="Dev-only mock brain: this is NOT a 0G decision">MOCK – NOT 0G</span>` : ""}
    </div>
    <div class="cp-row"><span class="cp-id">${esc(r.checkpoint)}</span><span class="cp-label">${esc(CHECKPOINT_LABEL[r.checkpoint] ?? "")}</span>${r.ok ? modelBadge(r) + (trustMode(r) && trustMode(r) !== "private" ? "" : teeBadge(r.tee_verified)) : ""}</div>
    <div class="card-body">
      ${thumb}
      <div class="verdict-col">
        <div class="verdict">${esc(s.verdict)}</div>
        ${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ""}
        ${walletBadge(r)}
        <div class="metrics">${r.ok ? `<span>conf <b>${pct(conf)}</b></span>` : ""}<span>latency <b>${lat}</b></span>${ev.action_taken ? `<span>→ <b>${esc(ev.action_taken)}</b></span>` : ""}</div>
      </div>
    </div>
    <dl class="proofs">
      <dt>0G req</dt><dd>${req ? `<code class="hash" title="${esc(req)}">${esc(short(req, 14, 6))}</code><button class="cp" data-copy="${esc(req)}" title="copy 0G request id">⧉</button>` : `<span class="pending">none — no 0G answer</span>`}</dd>
      <dt>Storage</dt><dd>${storage}</dd>
      <dt>Tx</dt><dd>${tx}</dd>
    </dl>`;
  const sl = node.querySelector<HTMLAnchorElement>(".storage-link");
  if (sl) sl.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openStorage(c.storageRoot!, c.storageTx); });
  node.querySelectorAll<HTMLButtonElement>("button.cp").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); copy(b.dataset.copy!, b); }));
}

export class DecisionPanel {
  root: HTMLElement;
  private list: HTMLElement;
  private nodes = new Map<string, HTMLElement>();
  private countEl: HTMLElement;
  private filter: string | null = null;
  onCardClick: (c: Card) => void = () => {};

  constructor(host: HTMLElement) {
    this.root = el("aside", "panel decisions");
    this.root.innerHTML = `<header><div><h2>0G decisions</h2><span class="sub">Every checkpoint = one 0G Compute call</span></div><span class="count">0</span></header>
      <div class="filter-row"><button data-f="" class="on">All</button><button data-f="CP-H1">CP-H1</button><button data-f="CP-H2">CP-H2</button><button data-f="CP-V">CP-V</button><button data-f="err">SAFE STOP</button></div>
      <div class="list"><div class="empty">Waiting for the first checkpoint…</div></div>`;
    this.list = this.root.querySelector(".list")!;
    this.countEl = this.root.querySelector(".count")!;
    this.root.querySelectorAll<HTMLButtonElement>(".filter-row button").forEach((b) => b.addEventListener("click", () => {
      this.root.querySelectorAll(".filter-row button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      this.filter = b.dataset.f || null;
      this.applyFilter();
    }));
    opFilter.on(() => this.applyFilter());
    host.appendChild(this.root);
  }

  private visible(c: Card): boolean {
    if (opFilter.op && c.ev.operator !== opFilter.op) return false;
    if (!this.filter) return true;
    if (this.filter === "err") return !c.ev.result.ok;
    return c.ev.result.checkpoint === this.filter;
  }
  private applyFilter(): void {
    for (const c of store.cards) { const n = this.nodes.get(c.key); if (n) n.hidden = !this.visible(c); }
  }

  upsert(c: Card, animate = true): void {
    let n = this.nodes.get(c.key);
    const isNew = !n;
    if (!n) {
      n = el("article");
      n.addEventListener("click", () => this.onCardClick(c));
      this.nodes.set(c.key, n);
      this.list.querySelector(".empty")?.remove();
      this.list.prepend(n);
      if (this.nodes.size > MAX_CARDS) {
        const last = this.list.lastElementChild as HTMLElement | null;
        if (last) { for (const [k, v] of this.nodes) if (v === last) { this.nodes.delete(k); break; } last.remove(); }
      }
    }
    render(c, n);
    n.hidden = !this.visible(c);
    if (isNew && animate) { n.classList.add("enter"); requestAnimationFrame(() => requestAnimationFrame(() => n!.classList.remove("enter"))); }
    else if (!isNew && animate) { n.classList.add("flash"); setTimeout(() => n!.classList.remove("flash"), 900); }
    this.countEl.textContent = String(store.cards.length);
  }

  rebuild(): void {
    this.nodes.clear();
    this.list.innerHTML = "";
    const cards = store.cards.slice(-MAX_CARDS);
    for (const c of cards) this.upsert(c, false);
    if (!cards.length) this.list.innerHTML = `<div class="empty">Waiting for the first checkpoint…</div>`;
    this.countEl.textContent = String(store.cards.length);
  }
}
