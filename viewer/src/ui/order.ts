// Customer order page (drawer): home -> items -> operator -> POST /api/orders -> live status timeline.
import type { Order, OrderStatus } from "../types";
import { store } from "../store";
import { CATALOGUE, CONFIG, OPERATORS, opColor } from "../config";
import type { Home } from "../scene/world";
import { esc, el, fmtClock } from "./dom";

const STEPS: { s: OrderStatus; label: string }[] = [
  { s: "placed", label: "Order placed" },
  { s: "assigned", label: "Robot assigned" },
  { s: "picking", label: "Picking at dark store" },
  { s: "out_for_delivery", label: "Out for delivery" },
  { s: "at_door", label: "At your door" },
  { s: "delivered", label: "Delivered" },
];

export interface OrderHandlers {
  onSelectHome: (h: Home | null) => void;
  onTrack: (o: Order | null) => void;
  onFollow: (robotId: string) => void;
}

export class OrderPage {
  root: HTMLElement;
  open = false;
  private home: Home | null = null;
  private qty = new Map<string, number>();
  private operator = Object.keys(OPERATORS)[0];
  private tracking: string | null = null;
  private search = "";
  private busy = false;
  private msg = "";

  constructor(host: HTMLElement, private homes: Home[], private h: OrderHandlers) {
    this.root = el("aside", "panel order-page");
    this.root.hidden = true;
    host.appendChild(this.root);
    this.root.addEventListener("click", (e) => this.onClick(e));
    this.root.addEventListener("input", (e) => {
      const t = e.target as HTMLInputElement;
      if (t.classList.contains("search")) { this.search = t.value.toLowerCase(); this.renderHomes(); }
    });
    store.on((what, p) => { if (what === "order" && this.open) { const o = p as Order; if (o.id === this.tracking || CONFIG.mode === "replay") this.render(); } });
  }

  toggle(force?: boolean): void {
    this.open = force ?? !this.open;
    this.root.hidden = !this.open;
    document.body.classList.toggle("order-open", this.open);
    if (this.open) this.render();
  }

  pickHome(h: Home): void {
    this.home = h;
    this.h.onSelectHome(h);
    if (!this.open) this.toggle(true); else this.render();
  }

  private onClick(e: Event): void {
    const t = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
    if (!t) return;
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
    else if (act === "track") { this.tracking = t.dataset.id!; this.h.onTrack(store.orders.get(this.tracking) ?? null); this.render(); }
    else if (act === "new") { this.tracking = null; this.h.onTrack(null); this.msg = ""; this.render(); }
    else if (act === "follow") this.h.onFollow(t.dataset.id!);
  }

  private async place(): Promise<void> {
    if (!this.home || !this.qty.size || this.busy) return;
    this.busy = true; this.msg = ""; this.render();
    const items: string[] = [];
    for (const [id, q] of this.qty) { const c = CATALOGUE.find((x) => x.id === id)!; for (let i = 0; i < q; i++) items.push(c.name); }
    try {
      const r = await fetch(`${CONFIG.apiBase}/api/orders`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ operator: this.operator, building_id: this.home.building_id, items, customer_name: this.home.customer_name }),
      });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 120)}`);
      const o = (await r.json()) as Partial<Order>;
      if (o.id) {
        this.tracking = o.id;
        if (o.status) store.apply({ topic: "order", t: store.simTime, data: o as Order });
        this.h.onTrack(store.orders.get(o.id) ?? null);
      }
      this.qty.clear();
    } catch (err) {
      this.msg = `Could not place order: ${(err as Error).message}`;
    }
    this.busy = false;
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

  private timeline(o: Order): string {
    const idx = STEPS.findIndex((s) => s.s === o.status);
    const failed = o.status === "failed";
    const rb = o.robot_id ? store.latest?.robots.find((r) => r.id === o.robot_id) : undefined;
    return `<div class="otrack" style="--op:${opColor(o.operator)}">
      <div class="otrack-head"><span class="chip"></span><b>${esc(o.id)}</b><span>${esc(o.operator)}</span>
        ${o.robot_id ? `<button class="mini" data-act="follow" data-id="${esc(o.robot_id)}">🎥 ${esc(o.robot_id)}</button>` : ""}</div>
      <div class="muted small">${esc(o.customer_name)} · ${esc(o.items.join(", ") || "—")}</div>
      ${rb ? `<div class="robot-now" data-state="${esc(rb.fsm_state)}">Robot: <b>${esc(rb.fsm_state)}</b>${rb.note ? ` · ${esc(rb.note)}` : ""}</div>` : ""}
      <ol class="steps">${STEPS.map((s, i) => {
        const ts = o.timestamps?.[s.s];
        const cls = failed ? (i <= idx ? "done" : "") : i < idx ? "done" : i === idx ? "now" : "";
        return `<li class="${cls}"><span class="dot"></span><span>${s.label}</span><time>${ts != null ? fmtClock(ts) : ""}</time></li>`;
      }).join("")}${failed ? `<li class="failed"><span class="dot"></span><span>Failed</span><time>${o.timestamps?.failed != null ? fmtClock(o.timestamps.failed) : ""}</time></li>` : ""}</ol>
    </div>`;
  }

  render(): void {
    if (!this.open) return;
    const live = CONFIG.mode === "live";
    const tracked = this.tracking ? store.orders.get(this.tracking) : undefined;
    const orders = Array.from(store.orders.values()).reverse();
    let body = "";
    if (tracked) {
      body = this.timeline(tracked) + `<button class="ghost" data-act="new">${live ? "← New order" : "← All orders"}</button>`;
    } else if (!live) {
      body = `<p class="muted">Replay: orders placed in this recording. Click one to see its timeline.</p>
        <div class="order-list">${orders.map((o) => `<button class="order-row" data-act="track" data-id="${esc(o.id)}" style="--op:${opColor(o.operator)}"><span class="chip"></span><b>${esc(o.id)}</b><span>${esc(o.customer_name)}</span><em>${esc(o.status)}</em></button>`).join("") || `<div class="muted">No orders yet at this point of the replay</div>`}</div>`;
    } else {
      const total = Array.from(this.qty).reduce((a, [id, q]) => a + (CATALOGUE.find((c) => c.id === id)?.price ?? 0) * q, 0);
      body = `
        <h3><span class="n">1</span> Deliver to</h3>
        ${this.home ? `<div class="picked"><b>${esc(this.home.customer_name)}</b><span>${esc(this.home.address)}</span><button class="mini" data-act="clear-home">change</button></div>`
          : `<p class="muted small">Click a white marker on the map, or pick from the list:</p><input class="search" placeholder="Search address or name" value="${esc(this.search)}"><div class="home-list"></div>`}
        <h3><span class="n">2</span> Items</h3>
        <div class="catalogue">${CATALOGUE.map((c) => { const q = this.qty.get(c.id) ?? 0; return `<div class="item ${q ? "sel" : ""}"><span class="em">${c.emoji}</span><span class="nm">${esc(c.name)}</span><span class="pr">₹${c.price}</span><span class="qty"><button data-act="dec" data-id="${c.id}">−</button><b>${q}</b><button data-act="inc" data-id="${c.id}">+</button></span></div>`; }).join("")}</div>
        <h3><span class="n">3</span> Operator</h3>
        <div class="ops-pick">${Object.keys(OPERATORS).map((op) => `<button data-act="op" data-op="${op}" class="${op === this.operator ? "on" : ""}" style="--op:${opColor(op)}"><span class="chip"></span>${op}</button>`).join("")}</div>
        ${this.msg ? `<div class="msg err">${esc(this.msg)}</div>` : ""}
        <button class="primary big" data-act="place" ${!this.home || !this.qty.size || this.busy ? "disabled" : ""}>${this.busy ? "Placing…" : `Place order · ₹${total}`}</button>
        ${orders.length ? `<h3>Recent orders</h3><div class="order-list">${orders.slice(0, 8).map((o) => `<button class="order-row" data-act="track" data-id="${esc(o.id)}" style="--op:${opColor(o.operator)}"><span class="chip"></span><b>${esc(o.id)}</b><span>${esc(o.customer_name)}</span><em>${esc(o.status)}</em></button>`).join("")}</div>` : ""}`;
    }
    this.root.innerHTML = `<header><h2>${live ? "Order groceries" : "Orders"}</h2><button class="x" data-act="close">✕</button></header><div class="order-body">${body}</div>`;
    this.renderHomes();
  }

  /** called periodically so the robot state line stays fresh */
  tick(): void { if (this.open && this.tracking) this.render(); }
}
