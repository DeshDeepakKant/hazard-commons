// Episode-batch verification (memory/episode_log.py), shared by the viewer and scripts/verify-check.mjs.
//
// No imports and only erasable TypeScript, so Node (>= 22.18, type stripping) can import this file
// directly. Works wherever WebCrypto (`crypto.subtle`), `atob` and `TextEncoder` exist.
//
// record_sha256 = sha256(canonical_json(record without "record_sha256")), where canonical_json is
// Python's json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8").
// JSON.parse + JSON.stringify cannot reproduce that (Python writes 1.0 / 1e-05, JS writes 1 / 0.00001),
// so we parse into a tree that keeps every number's source text and re-serialise Python style.

export const EPISODE_FORMAT = "hazard-commons/episode-batch/v1";

// ------------------------------------------------------------------ raw JSON tree

export class RawNum {
  raw: string;
  constructor(raw: string) { this.raw = raw; }
}
export class RawObj {
  entries: [string, Raw][];
  constructor(entries: [string, Raw][]) { this.entries = entries; }
  get(k: string): Raw | undefined {
    for (let i = this.entries.length - 1; i >= 0; i--) if (this.entries[i][0] === k) return this.entries[i][1];
    return undefined;
  }
}
export type Raw = null | boolean | string | RawNum | Raw[] | RawObj;

export class JsonSyntaxError extends Error {}

/** strict JSON parser (RFC 8259, plus Python's NaN/Infinity tokens) keeping numbers' raw text */
export function parseRaw(text: string): Raw {
  let i = 0;
  const n = text.length;
  const fail = (msg: string): never => { throw new JsonSyntaxError(`${msg} at offset ${i}`); };
  const ws = () => { while (i < n) { const c = text.charCodeAt(i); if (c === 32 || c === 9 || c === 10 || c === 13) i++; else break; } };
  const numRe = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;

  function str(): string {
    i++; // opening quote
    let out = "", start = i;
    for (;;) {
      if (i >= n) fail("unterminated string");
      const c = text.charCodeAt(i);
      if (c === 34) { out += text.slice(start, i); i++; return out; }
      if (c < 0x20) fail("control character in string");
      if (c === 92) {
        out += text.slice(start, i);
        const e = text[i + 1];
        switch (e) {
          case '"': out += '"'; break;
          case "\\": out += "\\"; break;
          case "/": out += "/"; break;
          case "b": out += "\b"; break;
          case "f": out += "\f"; break;
          case "n": out += "\n"; break;
          case "r": out += "\r"; break;
          case "t": out += "\t"; break;
          case "u": {
            const h = text.slice(i + 2, i + 6);
            if (!/^[0-9a-fA-F]{4}$/.test(h)) fail("bad \\u escape");
            out += String.fromCharCode(parseInt(h, 16));
            i += 4;
            break;
          }
          default: fail("bad escape");
        }
        i += 2; start = i;
        continue;
      }
      i++;
    }
  }

  function value(): Raw {
    ws();
    if (i >= n) fail("unexpected end");
    const c = text[i];
    if (c === "{") {
      i++; ws();
      const entries: [string, Raw][] = [];
      if (text[i] === "}") { i++; return new RawObj(entries); }
      for (;;) {
        ws();
        if (text[i] !== '"') fail("expected key");
        const k = str();
        ws();
        if (text[i] !== ":") fail("expected ':'");
        i++;
        entries.push([k, value()]);
        ws();
        if (text[i] === ",") { i++; continue; }
        if (text[i] === "}") { i++; return new RawObj(entries); }
        fail("expected ',' or '}'");
      }
    }
    if (c === "[") {
      i++; ws();
      const arr: Raw[] = [];
      if (text[i] === "]") { i++; return arr; }
      for (;;) {
        arr.push(value());
        ws();
        if (text[i] === ",") { i++; continue; }
        if (text[i] === "]") { i++; return arr; }
        fail("expected ',' or ']'");
      }
    }
    if (c === '"') return str();
    if (text.startsWith("true", i)) { i += 4; return true; }
    if (text.startsWith("false", i)) { i += 5; return false; }
    if (text.startsWith("null", i)) { i += 4; return null; }
    for (const t of ["NaN", "Infinity", "-Infinity"]) if (text.startsWith(t, i)) { i += t.length; return new RawNum(t); }
    numRe.lastIndex = i;
    const m = numRe.exec(text);
    if (!m) fail("unexpected token");
    i += m![0].length;
    return new RawNum(m![0]);
  }

  const v = value();
  ws();
  if (i < n) fail("trailing data");
  return v;
}

/** convert to ordinary JS values (numbers become JS numbers: fine for display, not for hashing) */
export function toPlain(v: Raw): unknown {
  if (v instanceof RawNum) return Number(v.raw);
  if (Array.isArray(v)) return v.map(toPlain);
  if (v instanceof RawObj) {
    const o: Record<string, unknown> = {};
    for (const [k, x] of v.entries) Object.defineProperty(o, k, { value: toPlain(x), enumerable: true, writable: true, configurable: true });
    return o;
  }
  return v;
}

// ------------------------------------------------------------------ Python-style serialisation

/** Python float.__repr__ for a finite double (shortest round-trip digits, Python's layout rules) */
export function pyFloatRepr(x: number): string {
  if (Number.isNaN(x)) return "NaN";
  if (x === Infinity) return "Infinity";
  if (x === -Infinity) return "-Infinity";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  const sign = x < 0 ? "-" : "";
  // JS toExponential() with no argument yields the shortest round-trip digits, like Python's repr
  const [mant, expS] = Math.abs(x).toExponential().split("e");
  const digits = mant.replace(".", "");
  const exp = parseInt(expS, 10);
  const decpt = exp + 1; // digits before the decimal point
  if (decpt <= -4 || decpt > 16) {
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const e = Math.abs(exp);
    return `${sign}${m}e${exp < 0 ? "-" : "+"}${e < 10 ? "0" : ""}${e}`;
  }
  if (decpt <= 0) return `${sign}0.${"0".repeat(-decpt)}${digits}`;
  if (decpt >= digits.length) return `${sign}${digits}${"0".repeat(decpt - digits.length)}.0`;
  return `${sign}${digits.slice(0, decpt)}.${digits.slice(decpt)}`;
}

/** a JSON number token as Python's json.dumps would re-emit it after json.loads */
export function pyNumber(raw: string): string {
  if (/^-?\d+$/.test(raw)) return BigInt(raw).toString(); // Python int: arbitrary precision
  return pyFloatRepr(Number(raw));
}

/** json.dumps string escaping with ensure_ascii=False */
export function pyString(s: string): string {
  let out = '"';
  for (let k = 0; k < s.length; k++) {
    const c = s.charCodeAt(k);
    if (c === 34) out += '\\"';
    else if (c === 92) out += "\\\\";
    else if (c < 0x20) {
      out += c === 10 ? "\\n" : c === 13 ? "\\r" : c === 9 ? "\\t" : c === 8 ? "\\b" : c === 12 ? "\\f"
        : "\\u" + c.toString(16).padStart(4, "0");
    } else out += s[k];
  }
  return out + '"';
}

/** Python str ordering = Unicode code point order (differs from UTF-16 order only for astral chars) */
export function cmpCodePoints(a: string, b: string): number {
  const ia = a[Symbol.iterator](), ib = b[Symbol.iterator]();
  for (;;) {
    const x = ia.next(), y = ib.next();
    if (x.done || y.done) return x.done && y.done ? 0 : x.done ? -1 : 1;
    const cx = x.value.codePointAt(0)!, cy = y.value.codePointAt(0)!;
    if (cx !== cy) return cx - cy;
  }
}

/** canonical_json(obj) as a JS string (UTF-8 encode it before hashing) */
export function canonical(v: Raw, dropKey?: string): string {
  if (v === null) return "null";
  if (v === true) return "true";
  if (v === false) return "false";
  if (typeof v === "string") return pyString(v);
  if (v instanceof RawNum) return pyNumber(v.raw);
  if (Array.isArray(v)) return `[${v.map((x) => canonical(x)).join(",")}]`;
  // Python dicts keep the last duplicate; sort_keys orders by code point
  const m = new Map<string, Raw>();
  for (const [k, x] of v.entries) if (k !== dropKey) m.set(k, x);
  const keys = [...m.keys()].sort(cmpCodePoints);
  return `{${keys.map((k) => `${pyString(k)}:${canonical(m.get(k)!)}`).join(",")}}`;
}

// ------------------------------------------------------------------ hashing

export async function sha256Hex(data: Uint8Array | string): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const d = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function b64ToBytes(b64: string): Uint8Array {
  const s = atob(b64.replace(/\s+/g, ""));
  const out = new Uint8Array(s.length);
  for (let k = 0; k < s.length; k++) out[k] = s.charCodeAt(k);
  return out;
}

// ------------------------------------------------------------------ batch verification

export class FormatError extends Error {}

export interface FrameCheck { sha: string; computed: string | null; ok: boolean; b64: string; bytes: number; error?: string; }

export interface RecordCheck {
  index: number;
  record: Record<string, unknown>;      // plain JS view, for display
  claimedSha: string | null;
  computedSha: string;
  hashOk: boolean;
  prevSha: string | null;
  expectedPrev: string | null;
  linkOk: boolean;
  frameSha: string | null;
  frameStatus: "ok" | "mismatch" | "missing" | "none";
}

export interface BatchReport {
  format: string | null;
  formatOk: boolean;
  runId: string | null;
  robotId: string | null;
  batch: number | null;
  prevBatchLast: string | null;
  records: RecordCheck[];
  chainOk: boolean;
  firstBroken: number | null;   // index of the first record whose hash or prev link fails
  frames: FrameCheck[];
  framesOk: boolean;
  recordsWithFrame: number;
  numbersReformatted: number;   // number tokens where JS's String(Number(x)) != Python's text (the pitfall)
}

const strOrNull = (v: Raw | undefined): string | null => (typeof v === "string" ? v : null);

function countReformatted(v: Raw): number {
  if (v instanceof RawNum) return String(Number(v.raw)) !== v.raw ? 1 : 0;
  if (Array.isArray(v)) return v.reduce((a: number, x) => a + countReformatted(x), 0);
  if (v instanceof RawObj) return v.entries.reduce((a: number, [, x]) => a + countReformatted(x), 0);
  return 0;
}

/** verify one downloaded batch file (its UTF-8 text). Throws FormatError if it is not an episode batch. */
export async function verifyBatch(text: string): Promise<BatchReport> {
  let doc: Raw;
  try { doc = parseRaw(text); } catch (e) {
    let hint = "";
    try { const j = JSON.parse(text); if (j && typeof j === "object" && "code" in j && "message" in j) hint = ` (indexer said: ${j.message})`; } catch { /* not JSON */ }
    throw new FormatError(`not JSON: ${(e as Error).message}${hint}`);
  }
  if (!(doc instanceof RawObj)) throw new FormatError("not a JSON object");
  const recsRaw = doc.get("records");
  const format = strOrNull(doc.get("format"));
  if (!Array.isArray(recsRaw)) {
    const msg = strOrNull(doc.get("message"));
    throw new FormatError(msg ? `indexer replied: ${msg}` : "no `records` array: not a Hazard Commons episode batch");
  }
  const framesRaw = doc.get("frames");
  const frameMap = new Map<string, string>();
  if (framesRaw instanceof RawObj) for (const [k, v] of framesRaw.entries) if (typeof v === "string") frameMap.set(k, v);

  const frames: FrameCheck[] = [];
  const frameOk = new Map<string, boolean>();
  for (const [sha, b64] of frameMap) {
    try {
      const bytes = b64ToBytes(b64);
      const computed = await sha256Hex(bytes);
      frames.push({ sha, computed, ok: computed === sha, b64, bytes: bytes.length });
      frameOk.set(sha, computed === sha);
    } catch (e) {
      frames.push({ sha, computed: null, ok: false, b64, bytes: 0, error: (e as Error).message });
      frameOk.set(sha, false);
    }
  }

  const prevBatchLast = strOrNull(doc.get("prev_batch_last_sha256"));
  const records: RecordCheck[] = [];
  let expectedPrev: string | null = prevBatchLast;
  let firstBroken: number | null = null;
  for (let k = 0; k < recsRaw.length; k++) {
    const r = recsRaw[k];
    if (!(r instanceof RawObj)) throw new FormatError(`record ${k} is not an object`);
    const computedSha = await sha256Hex(canonical(r, "record_sha256"));
    const claimedSha = strOrNull(r.get("record_sha256"));
    const prevSha = strOrNull(r.get("prev_record_sha256"));
    const inputs = r.get("inputs");
    const frameSha = inputs instanceof RawObj ? strOrNull(inputs.get("frame_sha256")) : null;
    const frameStatus = !frameSha ? "none" : !frameOk.has(frameSha) ? "missing" : frameOk.get(frameSha) ? "ok" : "mismatch";
    const hashOk = claimedSha === computedSha;
    const linkOk = prevSha === expectedPrev;
    if ((!hashOk || !linkOk) && firstBroken === null) firstBroken = k;
    records.push({
      index: k, record: toPlain(r) as Record<string, unknown>, claimedSha, computedSha, hashOk,
      prevSha, expectedPrev, linkOk, frameSha, frameStatus,
    });
    expectedPrev = computedSha; // link to what the previous record really hashes to
  }
  const withFrame = records.filter((r) => r.frameSha);
  const batch = doc.get("batch");
  return {
    format, formatOk: format === EPISODE_FORMAT,
    runId: strOrNull(doc.get("run_id")), robotId: strOrNull(doc.get("robot_id")),
    batch: batch instanceof RawNum ? Number(batch.raw) : null,
    prevBatchLast, records,
    chainOk: records.length > 0 && firstBroken === null, firstBroken,
    frames, framesOk: frames.every((f) => f.ok) && withFrame.every((r) => r.frameStatus === "ok"),
    recordsWithFrame: withFrame.length,
    numbersReformatted: countReformatted(doc),
  };
}
