// Compact operator dashboards + on-chain activity feed.
import { store } from "../store";
import { OPERATORS, opColor } from "../config";
import { FSM_LABEL } from "../scene/robots";
import { esc, el } from "./dom";
import { isRealHash, short, txUrl } from "./explorer";

const STAKE = 0.0001;   // 0G per report (config/settings.yaml chain.report_stake_wei)

export class Dashboards {
  root: HTMLElement;
  private opsEl: HTMLElement;
  private txEl: HTMLElement;
  private last = 0;
  balances: Record<string, number | string> = {};   // from /api/meta if the engine exposes it
  onRobot: (id: string) => void = () => {};

  constructor(host: HTMLElement) {
    this.root = el("aside", "panel dashboards");
    this.root.innerHTML = `<header><h2>Operators</h2></header><div class="ops"></div>
      <header class="sub-h"><h3>0G Chain activity</h3><span class="sub">HazardRegistry · Galileo</span></header><div class="txs"><div class="empty">No transactions yet</div></div>`;
    this.opsEl = this.root.querySelector(".ops")!;
    this.txEl = this.root.querySelector(".txs")!;
    this.opsEl.addEventListener("click", (e) => {
      const r = (e.target as HTMLElement).closest<HTMLElement>("[data-robot]");
      if (r) this.onRobot(r.dataset.robot!);
    });
    host.appendChild(this.root);
  }

  /** throttled re-render */
  update(force = false): void {
    const now = performance.now();
    if (!force && now - this.last < 500) return;
    this.last = now;
    const stats = store.operatorStats();
    const names = Array.from(new Set([...Object.keys(OPERATORS), ...Object.keys(stats)]));
    this.opsEl.innerHTML = names.map((op) => {
      const s = stats[op] ?? { delivered: 0, inProgress: 0, failed: 0, reports: 0, confirmed: 0, rejected: 0, verifications: 0, decisions: 0, errors: 0, robots: [] };
      const bal = this.balances[op];
      const staked = (s.reports * STAKE).toFixed(4), slashed = (s.rejected * STAKE).toFixed(4);
      const robots = s.robots.slice().sort((a, b) => a.id.localeCompare(b.id)).map((r) =>
        `<span class="rpill" data-robot="${esc(r.id)}" data-state="${esc(r.fsm_state)}" title="${esc(r.id)} · ${esc(r.fsm_state)}${r.note ? " · " + esc(r.note) : ""}"><i>${r.kind === "drone" ? "✈" : "●"}</i>${esc(r.id)}<em>${esc(FSM_LABEL[r.fsm_state] ?? r.fsm_state)}</em></span>`).join("");
      return `<section class="op" style="--op:${opColor(op)}">
        <div class="op-head"><span class="chip"></span><b>${esc(op)}</b><span class="dec">${s.decisions} 0G calls${s.errors ? ` · <span class="err">${s.errors} safe-stops</span>` : ""}</span></div>
        <div class="kpis">
          <div><b>${s.delivered}</b><span>delivered</span></div>
          <div><b>${s.inProgress}</b><span>in progress</span></div>
          <div><b>${s.reports}</b><span>reports</span></div>
          <div><b class="good">${s.confirmed}</b><span>confirmed</span></div>
          <div><b class="bad">${s.rejected}</b><span>rejected</span></div>
        </div>
        <div class="stake">${bal !== undefined ? `balance <b>${esc(bal)}</b> 0G · ` : ""}staked <b>${staked}</b> 0G${s.rejected ? ` · slashed <b class="bad">${slashed}</b>` : ""}</div>
        <div class="robots">${robots || `<span class="muted">no robots yet</span>`}</div>
      </section>`;
    }).join("");
    this.renderTxs();
  }

  private renderTxs(): void {
    const txs = Array.from(store.txs.values()).sort((a, b) => b.sim_time - a.sim_time).slice(0, 7);
    if (!txs.length) { this.txEl.innerHTML = `<div class="empty">No transactions yet</div>`; return; }
    this.txEl.innerHTML = txs.map((t) => `<div class="tx st-${esc(t.status)}" style="--op:${opColor(t.operator)}">
      <span class="chip"></span><span class="kind">${esc(t.kind)}</span>
      ${isRealHash(t.tx_hash) ? `<a target="_blank" rel="noopener" href="${txUrl(t.tx_hash)}">${esc(short(t.tx_hash, 6, 4))} ↗</a>` : `<code>${esc(short(t.tx_hash, 6, 4))}</code>`}
      <span class="status">${esc(t.status)}</span></div>`).join("");
  }
}
