// "Verify on 0G Storage": download an episode batch by root hash straight from the 0G Storage indexer,
// recompute every record hash, prev link and frame hash in the browser, and compare with the decision
// cards this viewer shows. No backend involved. Hash logic: ../episode.ts (shared with
// scripts/verify-check.mjs, which proves it reproduces Python's canonical_json on the real files).
import type { Card } from "../store";
import { store } from "../store";
import { CHECKPOINT_LABEL, CONFIG, zgDownloadUrl } from "../config";
import type { DecisionEvent, StorageEvent } from "../types";
import { EXPLORERS } from "../types";
import { EPISODE_FORMAT, FormatError, verifyBatch, type BatchReport, type RecordCheck } from "../episode";
import { esc, fmtClock } from "./dom";
import { copy, isRealHash, resolveSubmissionSeq, short, submissionUrl, txUrl } from "./explorer";
import { summarize } from "./panel";

export interface KnownData { decisions: DecisionEvent[]; storage: StorageEvent[]; }
let source: () => KnownData = () => ({ decisions: store.cards.map((c) => c.ev), storage: store.storage });
/** where to look for decision cards / storage uploads to cross-check against (replay: the whole recording) */
export function setVerifySource(fn: () => KnownData): void { source = fn; }

const ROOT_RE = /^0x[0-9a-fA-F]{64}$/;
const TIMEOUT_MS = 90_000;

let host: HTMLElement | null = null;
let abort: AbortController | null = null;
function close(): void {
  abort?.abort(); abort = null;
  host?.remove(); host = null;
  document.removeEventListener("keydown", onKey);
}
function onKey(e: KeyboardEvent): void {
  if (e.key !== "Escape") return;
  const lb = host?.querySelector(".vf-lightbox");
  if (lb) lb.remove(); else close();
}

const mb = (b: number) => (b >= 1e6 ? `${(b / 1e6).toFixed(2)} MB` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kB` : `${b} B`);
const ico = (ok: boolean | null) => `<span class="vf-ico ${ok === true ? "ok" : ok === false ? "bad" : "na"}">${ok === true ? "✓" : ok === false ? "✗" : "•"}</span>`;
const item = (ok: boolean | null, title: string, detail = "") => `<li>${ico(ok)}<div><b>${title}</b>${detail ? `<div class="vf-d">${detail}</div>` : ""}</div></li>`;
const imgSrc = (b64: string) => `data:image/${b64.startsWith("iVBOR") ? "png" : b64.startsWith("UklG") ? "webp" : "jpeg"};base64,${b64}`;

/** open the dialog; with a root it starts verifying at once */
export function openVerify(root?: string | null, opts: { storageTx?: string | null } = {}): void {
  close();
  host = document.createElement("div");
  host.className = "modal-back";
  host.innerHTML = `<div class="modal vf-modal" role="dialog" aria-modal="true" aria-label="Verify on 0G Storage">
    <header><div><b>Verify an episode log on 0G Storage</b> <span class="muted">· in your browser, no backend</span></div>
      <button class="x" aria-label="Close">✕</button></header>
    <div class="vf-body">
      <form class="vf-form">
        <input class="vf-root" spellcheck="false" autocomplete="off" placeholder="0x… root hash (0x + 64 hex)" value="${esc(root ?? "")}">
        <button class="primary" type="submit">Verify</button>
      </form>
      <div class="vf-known"></div>
      <p class="muted vf-how">Downloads the file from the 0G Storage indexer, then recomputes each record's
        <code>sha256(canonical_json(record))</code>, checks every <code>prev_record_sha256</code> link and every frame's
        <code>sha256</code> locally with WebCrypto.</p>
      <div class="vf-out"></div>
    </div></div>`;
  document.body.appendChild(host);
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
  host.querySelector(".x")!.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  const input = host.querySelector<HTMLInputElement>(".vf-root")!;
  host.querySelector("form")!.addEventListener("submit", (e) => { e.preventDefault(); void run(input.value.trim(), opts.storageTx ?? null); });

  // roots this viewer knows about (one click to verify them)
  const known = uniqueUploads(source().storage);
  if (known.length) {
    host.querySelector(".vf-known")!.innerHTML = `<span class="muted">Uploads in this ${CONFIG.mode === "replay" ? "recording" : "session"}:</span> ` +
      known.map((s) => `<button type="button" class="mini" data-root="${esc(s.root_hash)}" title="${esc(s.root_hash)}">${esc(s.robot_id)} · ${esc(short(s.root_hash, 6, 4))}</button>`).join(" ");
    host.querySelectorAll<HTMLButtonElement>(".vf-known button").forEach((b) => b.addEventListener("click", () => {
      input.value = b.dataset.root!; void run(b.dataset.root!, null);
    }));
  }
  if (root) void run(root.trim(), opts.storageTx ?? null); else input.focus();
}

function uniqueUploads(list: StorageEvent[]): StorageEvent[] {
  const seen = new Map<string, StorageEvent>();
  for (const s of list) if (ROOT_RE.test(s.root_hash ?? "")) seen.set(s.root_hash.toLowerCase(), s);
  return [...seen.values()].sort((a, b) => a.robot_id.localeCompare(b.robot_id));
}

type Fetched = { ok: true; bytes: Uint8Array } | { ok: false; html: string };

async function download(root: string, signal: AbortSignal, onProgress: (got: number, total: number) => void): Promise<Fetched> {
  let r: Response;
  try {
    r = await fetch(zgDownloadUrl(root), { signal, cache: "no-store" });
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, html: signal.reason === "timeout" ? "Timed out waiting for the 0G Storage indexer." : "Cancelled." };
    return { ok: false, html: `Network error: could not reach the 0G Storage indexer (offline, blocked by an extension, or CORS). <span class="muted">${esc((e as Error).message)}</span>` };
  }
  if (r.status === 404) return { ok: false, html: `Not found on 0G Storage (HTTP 404). Check the root, or wait a minute if it was uploaded just now.` };
  if (!r.ok) return { ok: false, html: `The 0G Storage indexer returned HTTP ${r.status} ${esc(r.statusText)}. Try again in a moment.` };
  const total = Number(r.headers.get("content-length") ?? 0);
  const chunks: Uint8Array[] = [];
  let got = 0;
  try {
    const reader = r.body!.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (value) { chunks.push(value); got += value.byteLength; onProgress(got, total); }
      if (done) break;
    }
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, html: signal.reason === "timeout" ? "Timed out during the download." : "Cancelled." };
    return { ok: false, html: `Download interrupted after ${mb(got)}: ${esc((e as Error).message)}` };
  }
  const bytes = new Uint8Array(got);
  let off = 0;
  for (const c of chunks) { bytes.set(c, off); off += c.byteLength; }
  // the indexer answers an unknown root with HTTP 200 + {"code":101,"message":"File not found"}
  if (got < 4096 && (r.headers.get("content-type") ?? "").includes("application/json")) {
    try {
      const j = JSON.parse(new TextDecoder().decode(bytes));
      if (j && typeof j === "object" && "code" in j && !("records" in j)) {
        const notFound = /not found/i.test(String(j.message ?? ""));
        return { ok: false, html: notFound
          ? `<b>File not found on 0G Storage.</b> Either the root hash is wrong, or the upload is very recent and has not reached the storage nodes yet (try again in a minute or two).`
          : `The 0G Storage indexer replied with an error: <code>${esc(JSON.stringify(j))}</code>` };
      }
    } catch { /* not the error envelope: fall through */ }
  }
  return { ok: true, bytes };
}

async function run(root: string, storageTx: string | null): Promise<void> {
  if (!host) return;
  const out = host.querySelector<HTMLElement>(".vf-out")!;
  const btn = host.querySelector<HTMLButtonElement>("form button")!;
  if (!ROOT_RE.test(root)) {
    out.innerHTML = `<ul class="vf-list">${item(false, "Not a root hash", `Expected <code>0x</code> followed by 64 hex characters; got ${root ? `<code>${esc(root.slice(0, 80))}</code> (${root.length} chars)` : "nothing"}.`)}</ul>`;
    return;
  }
  abort?.abort();
  const ctl = new AbortController();
  abort = ctl;
  const timer = setTimeout(() => ctl.abort("timeout"), TIMEOUT_MS);
  btn.disabled = true;
  out.innerHTML = `<div class="vf-progress"><div class="spinner"></div><div class="vf-pmain">
      <div>Downloading <code>${esc(short(root, 10, 8))}</code> from 0G Storage…</div>
      <div class="vf-bar"><i></i></div><div class="muted vf-pt">connecting to ${esc(new URL(zgDownloadUrl(root)).host)}</div></div></div>`;
  const bar = out.querySelector<HTMLElement>(".vf-bar i")!, pt = out.querySelector<HTMLElement>(".vf-pt")!;
  const t0 = performance.now();
  const res = await download(root, ctl.signal, (got, total) => {
    if (total) bar.style.width = `${Math.min(100, (100 * got) / total).toFixed(1)}%`; else bar.parentElement!.classList.add("indet");
    pt.textContent = `${mb(got)}${total ? ` / ${mb(total)}` : ""}`;
  });
  clearTimeout(timer);
  if (abort !== ctl || !host) return; // superseded or closed
  btn.disabled = false;
  if (!res.ok) {
    out.innerHTML = `<ul class="vf-list">${item(false, "Download from 0G Storage failed", res.html)}</ul>${retryHtml()}`;
    out.querySelector(".vf-retry")?.addEventListener("click", () => void run(root, storageTx));
    return;
  }
  const secs = (performance.now() - t0) / 1000;
  pt.textContent = `${mb(res.bytes.length)} · verifying hashes…`;

  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(res.bytes); } catch {
    out.innerHTML = `<ul class="vf-list">${dlItem(root, res.bytes.length, secs, storageTx)}${item(false, "Not our format", "The file is not UTF-8 text, so it is not a Hazard Commons episode batch.")}</ul>`;
    return;
  }
  let rep: BatchReport;
  try { rep = await verifyBatch(text); } catch (e) {
    const msg = e instanceof FormatError ? e.message : `unexpected error: ${(e as Error).message}`;
    out.innerHTML = `<ul class="vf-list">${dlItem(root, res.bytes.length, secs, storageTx)}${item(false, "Not a Hazard Commons episode batch", esc(msg))}</ul>`;
    return;
  }
  if (abort !== ctl || !host) return;
  render(out, root, res.bytes.length, secs, storageTx, rep);
}

const retryHtml = () => `<p><button type="button" class="vf-retry">Try again</button></p>`;

function dlItem(root: string, n: number, secs: number, storageTx: string | null): string {
  return item(true, `Downloaded ${n.toLocaleString("en-US")} bytes from 0G Storage`,
    `<span class="muted">${secs.toFixed(1)} s ·</span> <a href="${esc(zgDownloadUrl(root))}" target="_blank" rel="noopener">raw file ↗</a>
     · <a class="vf-scan" href="${esc(`${EXPLORERS.storage}/submissions`)}" target="_blank" rel="noopener" data-tx="${esc(storageTx ?? "")}">storagescan ↗</a>
     <div class="vf-root-line"><code>${esc(root)}</code><button class="cp" data-copy="${esc(root)}" title="copy">⧉</button></div>`);
}

interface Match { ev: DecisionEvent | null; same: boolean | null; diffs: string[]; }

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${stable((v as Record<string, unknown>)[k])}`).join(",")}}`;
  return JSON.stringify(v ?? null);
}

function matchCard(rec: Record<string, unknown>, decisions: DecisionEvent[]): Match {
  const req = rec.zg_request_id as string | null;
  const cli = rec.client_request_id as string | null;
  const ev = req
    ? decisions.find((d) => d.result.zg_request_id === req)
    : cli ? decisions.find((d) => !d.result.zg_request_id && d.result.client_request_id === cli && d.result.robot_id === rec.robot_id) : undefined;
  if (!ev) return { ev: null, same: null, diffs: [] };
  const diffs: string[] = [];
  if (ev.result.robot_id !== rec.robot_id) diffs.push("robot");
  if (ev.result.checkpoint !== rec.checkpoint) diffs.push("checkpoint");
  if ((ev.action_taken || "") !== (rec.action_taken || "")) diffs.push("action");
  if (stable(ev.result.output) !== stable(rec.output)) diffs.push("output");
  return { ev, same: diffs.length === 0, diffs };
}

function outputCell(rec: Record<string, unknown>): string {
  const inputs = (rec.inputs ?? {}) as Record<string, unknown>;
  const err = (inputs.error as string | undefined) ?? null;
  const pseudo = { ev: { result: { ok: rec.output != null && !err, output: rec.output, error: err, checkpoint: rec.checkpoint } } } as unknown as Card;
  const s = summarize(pseudo);
  return `<div class="tone-${esc(s.tone)}"><span class="verdict">${esc(s.verdict)}</span></div>${s.detail ? `<div class="muted vf-reason">${esc(s.detail)}</div>` : ""}`;
}

function render(out: HTMLElement, root: string, n: number, secs: number, storageTx: string | null, rep: BatchReport): void {
  const known = source();
  const tx = storageTx ?? known.storage.find((s) => s.root_hash?.toLowerCase() === root.toLowerCase())?.tx_hash ?? null;
  const recs = rep.records, K = recs.length;
  const intact = recs.filter((r) => r.hashOk && r.linkOk).length;
  const fb = rep.firstBroken != null ? recs[rep.firstBroken] : null;
  const chainDetail = fb
    ? `First break at record #${fb.index}: ${!fb.hashOk ? `stored hash <code>${esc(short(fb.claimedSha, 10, 6))}</code> but the record hashes to <code>${esc(short(fb.computedSha, 10, 6))}</code>` : ""}${!fb.hashOk && !fb.linkOk ? "; " : ""}${!fb.linkOk ? `prev link <code>${esc(short(fb.prevSha ?? "null", 10, 6))}</code> should be <code>${esc(short(fb.expectedPrev ?? "null", 10, 6))}</code>` : ""}.`
    : `Every <code>record_sha256</code> recomputed from canonical JSON matches, and every <code>prev_record_sha256</code> points at the record before it.
       ${rep.prevBatchLast ? `The first record continues an earlier batch ending in <code>${esc(short(rep.prevBatchLast, 10, 6))}</code> (verify that root too).` : "The first record starts this robot's log (prev = null)."}`;

  const fr = rep.frames, frOk = fr.filter((f) => f.ok).length;
  const missing = recs.filter((r) => r.frameStatus === "missing").length;
  const frameTitle = fr.length || rep.recordsWithFrame
    ? `Frames: ${frOk}/${fr.length} images match their sha256${rep.recordsWithFrame ? ` · used by ${rep.recordsWithFrame} record${rep.recordsWithFrame > 1 ? "s" : ""}` : ""}`
    : "Frames: none in this batch";
  const frameDetail = (fr.length || rep.recordsWithFrame)
    ? `${missing ? `<span class="bad">${missing} record(s) reference a frame that is not in the file.</span> ` : ""}Each record's <code>inputs.frame_sha256</code> is the sha256 of the exact image bytes sent to 0G. Click to enlarge.
       <div class="vf-thumbs">${fr.map((f) => `<figure class="${f.ok ? "" : "bad"}"><img src="${esc(imgSrc(f.b64))}" alt="frame ${esc(f.sha.slice(0, 8))}" data-sha="${esc(f.sha)}" data-ok="${f.ok ? 1 : 0}">
         <figcaption>${ico(f.ok)} <code>${esc(f.sha.slice(0, 10))}…</code> · ${mb(f.bytes)}${f.ok ? "" : `<br>computed <code>${esc((f.computed ?? f.error ?? "").slice(0, 10))}…</code>`}</figcaption></figure>`).join("")}</div>`
    : "These checkpoints were text-only (CP-H2) or failed before a frame was kept.";

  const matches = recs.map((r) => matchCard(r.record, known.decisions));
  const withCard = matches.filter((m) => m.ev).length, sameCard = matches.filter((m) => m.same).length;
  const cardOk = withCard === 0 ? null : sameCard === withCard;
  const cardTitle = withCard === 0
    ? "Live cards: no decision card in this viewer shares these 0G request ids"
    : `Live cards: ${sameCard}/${withCard} matching decision card${withCard > 1 ? "s" : ""} agree with the stored record${withCard > 1 ? "s" : ""}`;
  const cardDetail = withCard === 0
    ? "That's expected for a root from another run; the hash checks above stand on their own."
    : "Matched by 0G request id (or client request id for failed calls); robot, checkpoint, action and the full 0G output are compared.";

  const rows = recs.map((r, k) => recordRow(r, matches[k])).join("");
  const allOk = rep.formatOk && rep.chainOk && rep.framesOk && cardOk !== false;

  out.innerHTML = `
    <div class="vf-summary ${allOk ? "ok" : "bad"}">${allOk ? "✓ Verified" : "✗ Verification failed"}
      <span>${esc(rep.robotId ?? "?")} · ${K} record${K === 1 ? "" : "s"} · ${esc(rep.runId ?? "?")}</span></div>
    <ul class="vf-list">
      ${dlItem(root, n, secs, tx)}
      ${item(rep.formatOk, rep.formatOk ? "Format recognised" : "Unexpected format",
        `<code>${esc(rep.format ?? "(missing)")}</code>${rep.formatOk ? "" : ` (expected <code>${EPISODE_FORMAT}</code>)`} · run <code>${esc(rep.runId ?? "?")}</code> · robot <b>${esc(rep.robotId ?? "?")}</b> · batch ${rep.batch ?? "?"}`)}
      ${item(rep.chainOk, K === 0 ? "Hash chain: the batch has no records" : `Hash chain: ${intact}/${K} records intact`, K ? chainDetail : "")}
      ${item(fr.length || rep.recordsWithFrame ? rep.framesOk : null, frameTitle, frameDetail)}
      ${item(cardOk, cardTitle, cardDetail)}
    </ul>
    <div class="vf-table-wrap"><table class="vf-table">
      <thead><tr><th>#</th><th>Checkpoint</th><th>Sim time</th><th>0G request id</th><th>Action</th><th>0G output</th><th title="record hash + prev link">Chain</th><th>Frame</th><th>Live card</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;

  out.querySelectorAll<HTMLButtonElement>("button.cp").forEach((b) => b.addEventListener("click", () => copy(b.dataset.copy!, b)));
  out.querySelectorAll<HTMLImageElement>(".vf-thumbs img").forEach((img) => img.addEventListener("click", () => lightbox(img.src, img.dataset.sha!, img.dataset.ok === "1")));
  const scan = out.querySelector<HTMLAnchorElement>(".vf-scan");
  if (scan && isRealHash(tx)) {
    scan.href = txUrl(tx);
    scan.title = "storage transaction on chainscan (resolving the storagescan submission…)";
    resolveSubmissionSeq(tx).then((seq) => { if (seq != null) { scan.href = submissionUrl(seq); scan.title = `storagescan submission #${seq}`; scan.textContent = `storagescan #${seq} ↗`; } });
  } else if (scan) scan.title = "no storage tx known for this root: opens the recent submissions list";
}

function recordRow(r: RecordCheck, m: Match): string {
  const rec = r.record;
  const cp = String(rec.checkpoint ?? "?");
  const req = rec.zg_request_id as string | null;
  const chainTitle = r.hashOk && r.linkOk ? `record_sha256 ${r.computedSha}` : `${r.hashOk ? "" : `hash mismatch: stored ${r.claimedSha}, computed ${r.computedSha}. `}${r.linkOk ? "" : `prev link ${r.prevSha} ≠ ${r.expectedPrev}`}`;
  const frame = r.frameStatus === "none" ? `<span class="muted">–</span>` : `${ico(r.frameStatus === "ok")}${r.frameStatus === "missing" ? " missing" : ""}`;
  const card = m.ev
    ? m.same ? `${ico(true)} <span class="vf-match">matches the live card</span>` : `${ico(false)} differs: ${esc(m.diffs.join(", "))}`
    : `<span class="muted">no card</span>`;
  return `<tr class="${r.hashOk && r.linkOk ? "" : "bad"}">
    <td>${r.index}</td>
    <td><span class="cp-id">${esc(cp)}</span><div class="muted">${esc(CHECKPOINT_LABEL[cp] ?? "")}</div></td>
    <td>${typeof rec.sim_time === "number" ? fmtClock(rec.sim_time) : "–"}<div class="muted">${typeof rec.sim_time === "number" ? `${rec.sim_time.toFixed(1)} s` : ""}</div></td>
    <td>${req ? `<code title="${esc(req)}">${esc(short(req, 8, 4))}</code><button class="cp" data-copy="${esc(req)}" title="copy">⧉</button>` : `<span class="muted">none (no 0G answer)</span>`}</td>
    <td>${esc(String(rec.action_taken ?? "–"))}</td>
    <td class="vf-out-cell">${outputCell(rec)}</td>
    <td title="${esc(chainTitle)}">${ico(r.hashOk && r.linkOk)}</td>
    <td>${frame}</td>
    <td>${card}</td></tr>`;
}

function lightbox(src: string, sha: string, ok: boolean): void {
  const lb = document.createElement("div");
  lb.className = "vf-lightbox";
  lb.innerHTML = `<figure><img src="${esc(src)}" alt="frame"><figcaption>sha256 <code>${esc(sha)}</code> ${ok ? "✓ matches the image bytes" : "✗ does not match the image bytes"} · click anywhere to close</figcaption></figure>`;
  lb.addEventListener("click", () => lb.remove());
  host?.appendChild(lb);
}
