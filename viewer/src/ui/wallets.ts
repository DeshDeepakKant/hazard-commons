// Robot credit wallets ("Agents that pay for themselves"): shared state + helpers + the top-bar popover.
// Everything here is inert unless the backend reports wallets enabled (or a recording carries _hc.wallet).
import { CONFIG } from "../config";
import { store } from "../store";
import { fetchWallets, type RobotWallet, type WalletEntry, type WalletTier, type WalletsResponse } from "../live";
import { el, esc } from "./dom";
import { isRealHash, short, txUrl } from "./explorer";

export const walletState = { enabled: false, wallets: new Map<string, RobotWallet>() };

export const usd = (micro: number, digits = 4): string => `$${(micro / 1e6).toFixed(digits)}`;
const usdSigned = (micro: number): string => `${micro < 0 ? "−" : "+"}$${(Math.abs(micro) / 1e6).toFixed(5)}`;

export const TIER_LABEL: Record<string, string> = {
  normal: "normal", economy: "cheaper model", throttled: "throttled", paused: "SAFE STOP: out of credit",
};
export const tierChip = (tier: string, full = true): string =>
  `<span class="wchip wt-${esc(tier)}" title="${esc(TIER_LABEL[tier] ?? tier)}">${esc(full ? (TIER_LABEL[tier] ?? tier) : tier)}</span>`;

/** DEV FIXTURE: ?mockwallets=1 feeds this instead of /api/wallets. Fake numbers, only for checking the UI. */
function mockData(): WalletsResponse {
  const t = Date.now() / 1000;
  const mk = (id: string, op: string, kind: "rover" | "drone", frac: number, tier: WalletTier): RobotWallet => {
    const start = 5000, bal = Math.round(start * frac);
    return { robot_id: id, operator: op, kind, start_micro_usd: start, balance_micro_usd: bal, tier, spent: start - bal + 300, earned: 300, ledger: [
      { kind: "spend", amount_micro_usd: 90, ref: "req_mock_" + id, source: "router", t_wall: t - 20 },
      { kind: "earn", amount_micro_usd: 300, ref: "order-mock", source: "simulated", t_wall: t - 60 },
      { kind: "topup", amount_micro_usd: 1000, ref: "0x" + "ab".repeat(32), source: "chain", t_wall: t - 120 },
    ] };
  };
  return { enabled: true, wallets: [
    mk("R-01", "ZipKart", "rover", 0.8, "normal"), mk("R-02", "MinuteMart", "rover", 0.3, "economy"),
    mk("R-03", "DashKirana", "rover", 0.1, "throttled"), mk("D-01", "ZipKart", "drone", 0.0, "paused"), mk("D-02", "MinuteMart", "drone", 0.55, "normal"),
  ] };
}

let started = false;
export function startWalletPolling(): void {
  if (started || CONFIG.mode !== "live") return;
  started = true;
  const mock = new URLSearchParams(location.search).has("mockwallets");
  const tick = async () => {
    const r = mock ? mockData() : await fetchWallets();
    walletState.enabled = !!r?.enabled;
    walletState.wallets = new Map(walletState.enabled ? r!.wallets.map((w) => [w.robot_id, w]) : []);
  };
  void tick();
  window.setInterval(() => void tick(), 2000);
}

export interface HcWallet { balance_micro_usd: number; cost_micro_usd: number; tier: string; model?: string }
export function hcWallet(r: { zg_trace: Record<string, unknown> | null }): HcWallet | null {
  const w = (r.zg_trace?._hc as Record<string, unknown> | undefined)?.wallet as Record<string, unknown> | undefined;
  if (!w || typeof w.balance_micro_usd !== "number") return null;
  return { balance_micro_usd: w.balance_micro_usd, cost_micro_usd: Number(w.cost_micro_usd ?? 0), tier: String(w.tier ?? "normal"), model: w.model ? String(w.model) : undefined };
}

/** Per-robot wallet view for the fleet table: live API first, else the latest _hc.wallet seen in decisions (replay). */
export interface WalletView { balance: number; start: number; tier: string }
export function walletViews(): Map<string, WalletView> {
  const out = new Map<string, WalletView>();
  if (walletState.enabled) {
    for (const w of walletState.wallets.values()) out.set(w.robot_id, { balance: w.balance_micro_usd, start: w.start_micro_usd, tier: w.tier });
    return out;
  }
  for (const c of store.cards) {   // oldest -> newest
    const w = hcWallet(c.ev.result);
    if (!w) continue;
    const prev = out.get(c.ev.result.robot_id);
    out.set(c.ev.result.robot_id, { balance: w.balance_micro_usd, start: Math.max(prev?.start ?? 0, w.balance_micro_usd), tier: w.tier });
  }
  return out;
}

/** '− $0.00009 · balance $0.0021 · economy' (empty string when the decision has no wallet data) */
export function walletBadge(r: { zg_trace: Record<string, unknown> | null }): string {
  const w = hcWallet(r);
  if (!w) return "";
  return `<span class="badge wallet wt-${esc(w.tier)}" title="${esc(`This 0G call was paid from the robot's own credit${w.model ? " · model " + w.model : ""}`)}">− ${usd(w.cost_micro_usd, 5)} · balance ${usd(w.balance_micro_usd)} · ${esc(w.tier)}</span>`;
}

const SRC_LABEL: Record<string, string> = { router: "0G Router", chain: "on-chain", simulated: "SIMULATED", manual: "manual" };
const refHtml = (e: WalletEntry): string => e.tx_hash && isRealHash(e.tx_hash)
  ? `<a target="_blank" rel="noopener" href="${txUrl(e.tx_hash)}" title="${esc(e.note ?? e.tx_hash)}">${esc(short(e.tx_hash, 8, 6))} ↗</a>`
  : !e.ref ? "–"
  : isRealHash(e.ref) ? `<a target="_blank" rel="noopener" href="${txUrl(e.ref)}" title="${esc(e.ref)}">${esc(short(e.ref, 8, 6))} ↗</a>`
  : `<code title="${esc(e.ref)}">${esc(short(e.ref, 14, 6))}</code>`;
const entryRow = (e: WalletEntry, who = ""): string => `<tr class="wk-${esc(e.kind)}"><td>${esc(new Date(e.t_wall * 1000).toLocaleTimeString())}</td>${who}<td>${esc(e.kind)}</td>
  <td class="amt">${usdSigned(e.kind === "spend" ? -Math.abs(e.amount_micro_usd) : Math.abs(e.amount_micro_usd))}</td>
  <td><span class="wsrc ws-${esc(e.source)}">${esc(SRC_LABEL[e.source] ?? e.source)}</span></td><td>${refHtml(e)}</td></tr>`;

/** Ledger block for the detail modal ("" when there is no live ledger for this robot). */
export function ledgerHtml(robotId: string): string {
  const w = walletState.wallets.get(robotId);
  if (!w) return "";
  const rows = w.ledger.slice(-25).reverse().map((e) => entryRow(e)).join("");
  return `<details class="wledger" open><summary>${esc(robotId)} credit ledger · balance ${usd(w.balance_micro_usd)} ${tierChip(w.tier, false)}</summary>
    ${rows ? `<table><thead><tr><th>time</th><th>kind</th><th>amount</th><th>source</th><th>ref</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="muted">no entries yet</div>`}
    <p class="muted small">Source "SIMULATED" = not real money (e.g. a modelled delivery fee). Chain hashes link to the Galileo explorer.</p></details>`;
}

export class WalletsControl {
  private pop: HTMLElement;
  private btn: HTMLButtonElement;
  private timer: number | undefined;

  constructor(topbarRoot: HTMLElement) {
    startWalletPolling();
    this.btn = el("button", "wallets-btn", "💳 Wallets");
    this.btn.title = "Per-robot 0G credit wallets";
    this.btn.hidden = true;
    topbarRoot.querySelector(".ctrls")!.insertBefore(this.btn, topbarRoot.querySelector(".verify"));
    this.pop = el("div", "wallets-pop");
    this.pop.hidden = true;
    document.body.appendChild(this.pop);
    this.btn.addEventListener("click", () => this.toggle());
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") this.toggle(false); });
    window.setInterval(() => { this.btn.hidden = !walletState.enabled; if (!walletState.enabled && !this.pop.hidden) this.toggle(false); }, 1000);
  }

  private toggle(on?: boolean): void {
    const want = on ?? !!this.pop.hidden;
    this.pop.hidden = !want;
    this.btn.classList.toggle("on", want);
    window.clearInterval(this.timer);
    if (!want) return;
    const r = this.btn.getBoundingClientRect();
    this.pop.style.top = `${r.bottom + 6}px`;
    this.pop.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
    this.render();
    this.timer = window.setInterval(() => this.render(), 2000);
  }

  private render(): void {
    const ws = Array.from(walletState.wallets.values()).sort((a, b) => a.robot_id.localeCompare(b.robot_id));
    const body = ws.map((w) => `<tr><td><b>${esc(w.robot_id)}</b> <span class="muted">${w.kind === "drone" ? "✈" : "●"} ${esc(w.operator)}</span></td>
      <td>${usd(w.balance_micro_usd)}</td><td class="good">${usd(w.earned)}</td><td>${usd(w.spent)}</td><td>${tierChip(w.tier, false)}</td></tr>`).join("");
    const feed = ws.flatMap((w) => w.ledger.filter((e) => e.kind !== "spend").map((e) => ({ w, e })))
      .sort((a, b) => b.e.t_wall - a.e.t_wall).slice(0, 12)
      .map(({ w, e }) => entryRow(e, `<td>${esc(w.robot_id)}</td>`)).join("");
    this.pop.innerHTML = `<h3>Robot wallets</h3>
      <p class="muted small">Each body pays for its own 0G calls from its own credit.</p>
      <table class="wtab"><thead><tr><th>Body</th><th>Balance</th><th>Earned</th><th>Spent</th><th>Tier</th></tr></thead><tbody>${body}</tbody></table>
      <h4>Recent top-ups &amp; earnings</h4>
      ${feed ? `<table class="wtab wfeed"><thead><tr><th>time</th><th>who</th><th>kind</th><th>amount</th><th>source</th><th>ref</th></tr></thead><tbody>${feed}</tbody></table>` : `<div class="muted small">none yet</div>`}`;
  }
}
