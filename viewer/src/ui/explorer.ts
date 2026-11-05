// Explorer links for 0G Chain (chainscan) and 0G Storage (storagescan).
//
// storagescan-galileo addresses uploads by *submission sequence* (/submission/<txSeq>), not by root
// hash. We resolve the sequence from the storage tx receipt: the Flow contract's `Submit` event
// carries `submissionIndex` as the first data word. Fallback: the storage tx on chainscan.
import { EXPLORERS } from "../types";

const RPC = "https://evmrpc-testnet.0g.ai";
const SUBMIT_TOPIC = "0x167ce04d2aa1981994d3a31695da0d785373335b1078cec239a1a3a2c7675555";

export const txUrl = (hash: string) => `${EXPLORERS.chain}/tx/${hash}`;
export const submissionUrl = (seq: number | string) => `${EXPLORERS.storage}/submission/${seq}`;

export const isRealHash = (h: string | null | undefined): h is string => !!h && /^0x[0-9a-fA-F]{64}$/.test(h);

const seqCache = new Map<string, Promise<number | null>>();

export function resolveSubmissionSeq(storageTx: string): Promise<number | null> {
  let p = seqCache.get(storageTx);
  if (!p) {
    p = (async () => {
      try {
        const r = await fetch(RPC, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_getTransactionReceipt", params: [storageTx] }),
        });
        const j = await r.json();
        for (const log of j?.result?.logs ?? []) {
          if (log.topics?.[0]?.toLowerCase() === SUBMIT_TOPIC && typeof log.data === "string" && log.data.length >= 66) {
            return parseInt(log.data.slice(2, 66), 16);
          }
        }
      } catch { /* offline */ }
      return null;
    })();
    seqCache.set(storageTx, p);
  }
  return p;
}

/** open the best explorer page for a storage upload (root hash + its tx) */
export function openStorage(root: string, storageTx: string | null): void {
  if (!isRealHash(storageTx)) {
    window.open(`${EXPLORERS.storage}/submissions`, "_blank", "noopener");
    return;
  }
  const w = window.open("about:blank", "_blank");
  resolveSubmissionSeq(storageTx).then((seq) => {
    const url = seq != null ? submissionUrl(seq) : txUrl(storageTx);
    if (w) w.location.href = url; else window.open(url, "_blank", "noopener");
  });
  void root;
}

export function short(h: string | null | undefined, a = 8, b = 6): string {
  if (!h) return "";
  return h.length > a + b + 1 ? `${h.slice(0, a)}…${h.slice(-b)}` : h;
}

export async function copy(text: string, el?: HTMLElement): Promise<void> {
  try { await navigator.clipboard.writeText(text); } catch {
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (el) { el.classList.add("copied"); setTimeout(() => el.classList.remove("copied"), 900); }
}
