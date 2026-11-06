// Orders v2 (drawer): home (map click / search) -> cart -> operator (or auto-fastest) -> extras -> POST /api/orders,
// live tracker timeline, order list with follow + cancel. Replay: read-only, controls disabled.
import type { Order, OrderStatus } from "../types";
import { store } from "../store";
import { CATALOGUE, CONFIG, OPERATORS, opColor } from "../config";
import type { Home } from "../scene/world";
import { esc, el, fmtClock } from "./dom";
import { opFilter } from "./filter";

const STEPS: { s: OrderStatus; label: string }[] = [
  { s: "placed", label: "Order placed" },
  { s: "assigned", label: "Robot assigned" },
  { s: "picking", label: "Picked up at dark store" },
  { s: "out_for_delivery", label: "En route" },
  { s: "at_door", label: "At the door" },
  { s: "delivered", label: "Delivered" },
];

/** priority/note come back from POST /api/orders (not on the bus), so remember them client-side */
export const orderMeta = new Map<string, { priority?: string; note?: string }>();

const REPLAY_TIP = "Ordering is disabled in replay: this is a recording";
const AUTO = "auto";

export interface OrderHandlers {
  onSelectHome: (h: Home | null) => void;
  onTrack: (o: Order | null) => void;
  onFollow: (robotId: string) => void;
}

const isOpen = (o: Order) => o.status !== "delivered" && o.status !== "failed";

export class OrderPage {
  root: HTMLElement;
  open = false;
  private home: Home | null = null;
  private qty = new Map<string, number>();
  private operator = AUTO;
  private priority: "normal" | "express" = "normal";
  private note = "";
  private tracking: string | null = null;
  private search = "";
  private busy = false;
  private msg = "";
  private recentHtml = "";

  constructor(host: HTMLElement, private homes: Home[], private h: OrderHandlers) {
    this.root = el("aside", "panel order-page");
    this.root.hidden = true;
    host.appendChild(this.root);
    this.root.addEventListener("click", (e) => this.onClick(e));
    this.root.addEventListener("input", (e) => {
      const t = e.target as HTMLInputElement;
      if (t.classList.contains("search")) { this.search = t.value.toLowerCase(); this.renderHomes(); }
      else if (t.classList.contains("note")) this.note = t.value;
    });
    this.root.addEventListener("change", (e) => {
      const t = e.target as HTMLInputElement;
      if (t.name === "prio") { this.priority = t.value as "normal" | "express"; }
    });
    opFilter.on(() => { this.recentHtml = ""; this.renderRecent(); });
    store.on((what, p) => {
      if (what === "order" && this.open) { const o = p as Order; if (o.id === this.tracking) this.render(); else this.renderRecent(); }
    });
  }

  toggle(force?: boolean): void {
    this.open = force ?? !this.open;
    this.root.hidden = !this.open;
    document.body.classList.toggle("order-open", this.open);
    if (this.open) this.render();
  }

  pickHome(h: Home): void {
    this.home = h;
    this.tracking = null;
    this.h.onSelectHome(h);
    if (!this.open) this.toggle(true); else this.render();
  }

  /** operator with the most spare capacity right now (idle robots minus queued orders) */
  private suggest(): { op: string; idle: number; queued: number } {
    const robots = store.latest?.robots ?? [];
    let best = { op: Object.keys(OPERATORS)[0], idle: 0, queued: 0, score: -1e9 };
    for (const op of Object.keys(OPERATORS)) {
      const idle = robots.filter((r) => r.operator === op && r.fsm_state === "IDLE").length;
      const queued = Array.from(store.orders.values()).filter((o) => o.operator === op && o.status === "placed").length;
      const score = idle - queued;
      if (score > best.score) best = { op, idle, queued, score };
    }
    return best;
  }

  private onClick(e: Event): void {
    const t = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
    if (!t || (t as HTMLButtonElement).disabled) return;
    const act = t.dataset.act!;
    if (act === "close") this.toggle(false);
    else if (act === "home") { const h = this.homes.find((x) => x.id === t.dataset.id); if (h) this.pickHome(h); }
    else if (act === "clear-home") { this.home = null; this.h.onSelectHome(null); this.render(); }
    else if (act === "inc" || act === "dec") {
      const id = t.dataset.id!;
      const q = Math.max(0, Math.min(9, (this.qty.get(id) ?? 0) + (act === "inc" ? 1 : -1)));
      if (q) this.qty.set(id, q); else this.qty.delete(id);
      this.render();
    } else if (act === "op") { this.operator = t.dataset.op!; this.render(); }
    else if (act === "place") void this.place();
    else if (act === "quick") void this.quick(Number(t.dataset.n));
    else if (act === "cancel") void this.cancel(t.dataset.id!);
    else if (act === "clearf") { opFilter.set(null); this.render(); }
    else if (act === "track") {
      this.tracking = t.dataset.id!;
      const o = store.orders.get(this.tracking) ?? null;
      this.h.onTrack(o);
      if (o?.robot_id) this.h.onFollow(o.robot_id);   // fly the camera to its robot
      this.render();
    }
    else if (act === "new") { this.tracking = null; this.h.onTrack(null); this.msg = ""; this.render(); }
    else if (act === "follow") this.h.onFollow(t.dataset.id!);
  }

  private async post(body: Record<string, unknown>): Promise<Order | null> {
    const r = await fetch(`${CONFIG.apiBase}/api/orders`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 120)}`);
    const o = (await r.json()) as Order & { priority?: string; note?: string };
    if (!o.id) return null;
    orderMeta.set(o.id, { priority: o.priority, note: o.note });
    if (o.status) store.apply({ topic: "order", t: store.simTime, data: o });
    return store.orders.get(o.id) ?? o;
  }

  private resolveOp(): string { return this.operator === AUTO ? this.suggest().op : this.operator; }

  private async place(): Promise<void> {
    if (CONFIG.mode !== "live" || !this.home || !this.qty.size || this.busy) return;
    this.busy = true; this.msg = ""; this.render();
    const items: string[] = [];
    for (const [id, q] of this.qty) { const c = CATALOGUE.find((x) => x.id === id)!; for (let i = 0; i < q; i++) items.push(c.name); }
    try {
      const o = await this.post({
        operator: this.resolveOp(), building_id: this.home.building_id, items,
        customer_name: this.home.customer_name, priority: this.priority, note: this.note.trim(),
      });
      if (o) { this.tracking = o.id; this.h.onTrack(o); }
      this.qty.clear(); this.note = "";
    } catch (err) {
      this.msg = `Could not place order: ${(err as Error).message}`;
    }
    this.busy = false;
    this.render();
  }

  /** stress-test: n random orders (random homes, random 1-3 items); n=1 is "random order" */
  private async quick(n: number): Promise<void> {
    if (CONFIG.mode !== "live" || this.busy || !this.homes.length) return;
    this.busy = true; this.msg = ""; this.render();
    try {
      for (let i = 0; i < n; i++) {
        const home = this.homes[Math.floor(Math.random() * this.homes.length)];
        const ops = Object.keys(OPERATORS);
        const op = this.operator === AUTO && n > 1 ? ops[Math.floor(Math.random() * ops.length)] : this.resolveOp();
        const items = Array.from({ length: 1 + Math.floor(Math.random() * 3) }, () => CATALOGUE[Math.floor(Math.random() * CATALOGUE.length)].name);
        await this.post({ operator: op, building_id: home.building_id, items, customer_name: home.customer_name, priority: this.priority, note: this.note.trim() });
      }
      this.msg = `Placed ${n} random order${n > 1 ? "s" : ""}`;
    } catch (err) {
      this.msg = `Could not place order: ${(err as Error).message}`;
    }
    this.busy = false;
    this.render();
  }

  private async cancel(id: string): Promise<void> {
    if (CONFIG.mode !== "live") return;
    try {
      const r = await fetch(`${CONFIG.apiBase}/api/orders/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      if (!r.ok) throw new Error(r.status === 409 ? "a robot is already on it" : `${r.status}`);
      store.apply({ topic: "order", t: store.simTime, data: (await r.json()) as Order });
      this.msg = "";
    } catch (err) {
      this.msg = `Could not cancel ${id}: ${(err as Error).message}`;
    }
    this.render();
  }

  private renderHomes(): void {
    const list = this.root.querySelector(".home-list");
    if (!list) return;
    const q = this.search;
    const hs = this.homes.filter((h) => !q || h.address.toLowerCase().includes(q) || h.customer_name.toLowerCase().includes(q)).slice(0, 60);
    list.innerHTML = hs.map((h) => `<button class="home-row" data-act="home" data-id="${esc(h.id)}"><b>${esc(h.customer_name)}</b><span>${esc(h.address)}</span></button>`).join("")
      || `<div class="muted">No match</div>`;
  }

  private rerouted(o: Order): boolean {
    if (!o.robot_id) return false;
    return store.cards.some((c) => c.ev.result.robot_id === o.robot_id && /reroute|detour|hand/i.test(c.ev.action_taken ?? "")
      && c.ev.sim_time >= (o.timestamps?.assigned ?? 0));
  }

  private timeline(o: Order): string {
    const idx = STEPS.findIndex((s) => s.s === o.status);
    const failed = o.status === "failed";
    const cancelled = o.timestamps?.cancelled != null;
    const rb = o.robot_id ? store.latest?.robots.find((r) => r.id === o.robot_id) : undefined;
    const meta = orderMeta.get(o.id);
    const rr = this.rerouted(o);
    const steps = STEPS.map((s, i) => {
      const ts = o.timestamps?.[s.s];
      const cls = failed ? (ts != null ? "done" : "") : i < idx ? "done" : i === idx ? "now" : "";
      const li = `<li class="${cls}"><span class="dot"></span><span>${s.label}</span><time>${ts != null ? fmtClock(ts) : ""}</time></li>`;
      return s.s === "out_for_delivery" && rr ? li + `<li class="done warn"><span class="dot"></span><span>Rerouted around a hazard</span><time></time></li>` : li;
    }).join("");
    return `<div class="otrack" style="--op:${opColor(o.operator)}">
      <div class="otrack-head"><span class="chip"></span><b>${esc(o.id)}</b><span>${esc(o.operator)}</span>
        ${meta?.priority === "express" ? `<span class="badge express">EXPRESS</span>` : ""}
        ${o.robot_id ? `<button class="mini" data-act="follow" data-id="${esc(o.robot_id)}">🎥 ${esc(o.robot_id)}</button>` : ""}</div>
      <div class="muted small">${esc(o.customer_name)} · ${esc(o.items.join(", ") || "—")}</div>
      ${meta?.note ? `<div class="muted small">Note: ${esc(meta.note)}</div>` : ""}
      ${rb ? `<div class="robot-now" data-state="${esc(rb.fsm_state)}">Robot: <b>${esc(rb.fsm_state)}</b>${rb.note ? ` · ${esc(rb.note)}` : ""}</div>` : ""}
      <ol class="steps">${steps}${failed ? `<li class="failed"><span class="dot"></span><span>${cancelled ? "Cancelled" : "Failed"}</span><time>${o.timestamps?.failed != null ? fmtClock(o.timestamps.failed) : ""}</time></li>` : ""}</ol>
      ${o.status === "placed" && CONFIG.mode === "live" ? `<button class="ghost" data-act="cancel" data-id="${esc(o.id)}">Cancel order</button>` : ""}
    </div>`;
  }

  private recentRows(): string {
    const f = opFilter.op;
    const orders = Array.from(store.orders.values()).reverse().filter((o) => !f || o.operator === f).slice(0, 12);
    if (!orders.length) return `<div class="muted">No orders${f ? ` for ${esc(f)}` : ""} yet</div>`;
    return orders.map((o) => `<div class="order-row" data-act="track" data-id="${esc(o.id)}" style="--op:${opColor(o.operator)}" title="Track${o.robot_id ? " and follow " + esc(o.robot_id) : ""}">
      <span class="chip"></span><b>${esc(o.id)}</b><span>${esc(o.customer_name)}${orderMeta.get(o.id)?.priority === "express" ? " ⚡" : ""}</span><em>${esc(o.status)}</em>${
        o.status === "placed" && CONFIG.mode === "live" ? `<button class="mini" data-act="cancel" data-id="${esc(o.id)}" title="Cancel (only before a robot is assigned)">✕</button>` : ""}</div>`).join("");
  }

  /** refresh only the list, so typing in the note box is never interrupted */
  private renderRecent(): void {
    const box = this.root.querySelector(".recent-list");
    if (!box) return;
    const html = this.recentRows();
    if (html !== this.recentHtml) { this.recentHtml = html; box.innerHTML = html; }
  }

  render(): void {
    if (!this.open) return;
    const live = CONFIG.mode === "live";
    const tracked = this.tracking ? store.orders.get(this.tracking) : undefined;
    let body = "";
    if (tracked) {
      body = this.timeline(tracked) + `<button class="ghost" data-act="new">${live ? "← New order" : "← All orders"}</button>`;
    } else {
      const ro = live ? "" : `disabled title="${REPLAY_TIP}"`;
      const total = Array.from(this.qty).reduce((a, [id, q]) => a + (CATALOGUE.find((c) => c.id === id)?.price ?? 0) * q, 0);
      const sg = this.suggest();
      const robots = store.latest?.robots ?? [];
      body = `${live ? "" : `<p class="muted small">Replay: this is a recording, so ordering is disabled. Click an order below to see its timeline.</p>`}
        <h3><span class="n">1</span> Deliver to</h3>
        ${this.home ? `<div class="picked"><b>${esc(this.home.customer_name)}</b><span>${esc(this.home.address)}</span><button class="mini" data-act="clear-home" ${ro}>change</button></div>`
          : `<p class="muted small">Click a white home marker on the map, or search:</p><input class="search" ${ro} placeholder="Search address or name" value="${esc(this.search)}"><div class="home-list"></div>`}
        <h3><span class="n">2</span> Cart</h3>
        <div class="catalogue">${CATALOGUE.map((c) => { const q = this.qty.get(c.id) ?? 0; return `<div class="item ${q ? "sel" : ""}"><span class="em">${c.emoji}</span><span class="nm">${esc(c.name)}</span><span class="pr">₹${c.price}</span><span class="qty"><button data-act="dec" data-id="${c.id}" ${ro}>−</button><b>${q}</b><button data-act="inc" data-id="${c.id}" ${ro}>+</button></span></div>`; }).join("")}</div>
        <h3><span class="n">3</span> Operator</h3>
        <div class="ops-pick">
          <button data-act="op" data-op="${AUTO}" class="${this.operator === AUTO ? "on" : ""}" ${ro} title="Pick the operator with the most idle robots / shortest queue right now">⚡ Fastest${this.operator === AUTO ? ` · ${esc(sg.op)}` : ""}</button>
          ${Object.keys(OPERATORS).map((op) => { const idle = robots.filter((r) => r.operator === op && r.fsm_state === "IDLE").length; return `<button data-act="op" data-op="${op}" class="${op === this.operator ? "on" : ""}" ${ro} style="--op:${opColor(op)}" title="${idle} idle robot(s)"><span class="chip"></span>${op}<small>${idle} idle</small></button>`; }).join("")}
        </div>
        <h3><span class="n">+</span> Extras</h3>
        <textarea class="note" rows="2" maxlength="200" ${ro} placeholder="Delivery note (e.g. leave at the gate)">${esc(this.note)}</textarea>
        <div class="prio"><label><input type="radio" name="prio" value="normal" ${this.priority === "normal" ? "checked" : ""} ${ro}> Normal</label>
          <label><input type="radio" name="prio" value="express" ${this.priority === "express" ? "checked" : ""} ${ro}> ⚡ Express (dispatched first)</label></div>
        <div class="quick"><button class="ghost" data-act="quick" data-n="1" ${ro} ${this.busy ? "disabled" : ""}>🎲 Random order</button>
          <button class="ghost" data-act="quick" data-n="3" ${ro} ${this.busy ? "disabled" : ""}>Quick order ×3</button></div>
        ${this.msg ? `<div class="msg ${this.msg.startsWith("Could") ? "err" : ""}">${esc(this.msg)}</div>` : ""}
        <button class="primary big" data-act="place" ${!live ? `disabled title="${REPLAY_TIP}"` : !this.home || !this.qty.size || this.busy ? "disabled" : ""}>${this.busy ? "Placing…" : `Place order · ₹${total}`}</button>
        <h3>${live ? "Recent orders" : "Orders"}${opFilter.op ? ` · ${esc(opFilter.op)} <button class="mini" data-act="clearf">all</button>` : ""}</h3>
        <div class="order-list recent-list"></div>`;
    }
    this.root.innerHTML = `<header><h2>${live ? "Order groceries" : "Orders"}</h2><button class="x" data-act="close">✕</button></header><div class="order-body">${body}</div>`;
    this.recentHtml = "";
    this.renderHomes();
    this.renderRecent();
  }

  /** called periodically so the robot state line and order list stay fresh */
  tick(): void {
    if (!this.open) return;
    if (this.tracking) this.render(); else this.renderRecent();
  }
}
