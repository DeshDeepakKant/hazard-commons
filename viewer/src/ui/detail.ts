// Decision detail modal: the exact frame sent to 0G, the full model output, and every proof id.
import type { Card } from "../store";
import { CHECKPOINT_LABEL } from "../config";
import { esc, fmtClock } from "./dom";
import { copy, isRealHash, openStorage, txUrl } from "./explorer";
import { summarize } from "./panel";

function imgSrc(b64: string): string {
  if (b64.startsWith("data:")) return b64;
  const mime = b64.startsWith("iVBOR") ? "png" : b64.startsWith("UklG") ? "webp" : "jpeg";
  return `data:image/${mime};base64,${b64}`;
}

let host: HTMLElement | null = null;
function close(): void { host?.remove(); host = null; document.removeEventListener("keydown", onKey); }
function onKey(e: KeyboardEvent): void { if (e.key === "Escape") close(); }

const row = (k: string, v: string) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
const code = (v: string, label = "copy") => `<code>${esc(v)}</code><button class="cp" data-copy="${esc(v)}" title="${label}">⧉</button>`;

export function openDecisionDetail(c: Card): void {
  close();
  const ev = c.ev, r = ev.result, s = summarize(c);
  const full = ev.frame_full_b64, thumb = ev.frame_thumb_b64;
  const sentImage = r.checkpoint !== "CP-H2";
  let figure: string;
  if (full) figure = `<img src="${esc(imgSrc(full))}" alt="frame sent to 0G">`;
  else if (thumb) figure = `<img class="lowres" src="${esc(imgSrc(thumb))}" alt="frame sent to 0G (thumbnail)">
      <figcaption>Thumbnail only: this recording was made before full frames were streamed. Live runs show the full frame.</figcaption>`;
  else figure = `<div class="noimg">${sentImage ? (r.ok ? "No frame was recorded for this decision." : "Call failed before any frame was kept.")
      : "CP-H2 is a text-only checkpoint: no image is sent to 0G, only the report, route and body type."}</div>`;
  const trace = (r.zg_trace ?? {}) as Record<string, unknown>;
  const out = r.output ? JSON.stringify(r.output, null, 2) : "";
  const tx = c.chainTx
    ? (isRealHash(c.chainTx) ? `<a target="_blank" rel="noopener" href="${txUrl(c.chainTx)}">${esc(c.chainTx)} ↗</a>` : code(c.chainTx)) + ` <span class="muted">${esc(c.chainKind ?? "")} · ${esc(c.chainStatus ?? "")}</span>`
    : `<span class="muted">none for this decision</span>`;
  const storage = c.storageRoot
    ? `<a href="#" class="storage-link">${esc(c.storageRoot)} ↗</a>`
    : `<span class="muted">${r.ok ? "pending upload…" : "nothing stored: no 0G answer"}</span>`;

  host = document.createElement("div");
  host.className = "modal-back";
  host.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
    <header><div><b>${esc(r.robot_id)}</b> · ${esc(ev.operator)} · <span class="cp-id">${esc(r.checkpoint)}</span>
      ${esc(CHECKPOINT_LABEL[r.checkpoint] ?? "")} <span class="muted">· t ${fmtClock(ev.sim_time)}</span></div>
      <button class="x" aria-label="Close">✕</button></header>
    <div class="modal-body">
      <figure>${figure}${sentImage && full ? `<figcaption>Exactly what 0G saw: a real CC photo of the hazard type composited with the sim render.</figcaption>` : ""}</figure>
      <div class="info">
        <div class="verdict tone-${esc(s.tone)}">${esc(s.verdict)}</div>
        ${s.detail ? `<p class="reason">${esc(s.detail)}</p>` : ""}
        ${r.ok ? "" : `<pre class="err">${esc(r.error ?? "")}</pre>`}
        <dl>
          ${row("Action taken", esc(ev.action_taken || "–"))}
          ${row("Latency", r.latency_ms != null ? `${(r.latency_ms / 1000).toFixed(2)} s` : "–")}
          ${row("Model", esc(r.model ?? "–"))}
          ${row("TEE", r.tee_verified === true ? "verified" : r.tee_verified === false ? "FAILED" : "not reported (sealed request)")}
          ${row("0G request id", r.zg_request_id ? code(r.zg_request_id) : `<span class="muted">none: no 0G answer</span>`)}
          ${row("Client request id", r.client_request_id ? code(r.client_request_id) : "–")}
          ${trace.provider ? row("Provider", code(String(trace.provider))) : ""}
          ${row("Storage root", storage)}
          ${row("Chain tx", tx)}
        </dl>
        ${out ? `<details open><summary>Full 0G output (JSON)</summary><pre>${esc(out)}</pre></details>` : ""}
        ${Object.keys(trace).length ? `<details><summary>x_0g_trace</summary><pre>${esc(JSON.stringify(trace, null, 2))}</pre></details>` : ""}
      </div>
    </div></div>`;
  document.body.appendChild(host);
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
  host.querySelector(".x")!.addEventListener("click", close);
  host.querySelector<HTMLAnchorElement>(".storage-link")?.addEventListener("click", (e) => {
    e.preventDefault(); openStorage(c.storageRoot!, c.storageTx);
  });
  host.querySelectorAll<HTMLButtonElement>("button.cp").forEach((b) => b.addEventListener("click", () => copy(b.dataset.copy!, b)));
  document.addEventListener("keydown", onKey);
}
