#!/usr/bin/env node
// Proves the viewer's in-browser verifier (src/episode.ts) reproduces Python's canonical_json byte for
// byte: recomputes every record_sha256 / prev link / frame_sha256 in real episode batch files.
//
//   node scripts/verify-check.mjs [run dir]          default: ../logs/runs/demo-galileo-final
//   node scripts/verify-check.mjs [run dir] --fetch  also download each root in storage_roots.json from
//                                                    0G Storage and check it is byte-identical
// Needs Node >= 22.18 (imports the .ts module via built-in type stripping). Exit code 1 on any failure.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { FormatError, pyFloatRepr, pyString, canonical, parseRaw, verifyBatch } from "../src/episode.ts";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const fetchToo = args.includes("--fetch");
const runDir = resolve(args.find((a) => !a.startsWith("--")) ?? join(here, "../../logs/runs/demo-galileo-final"));
const INDEXER = "https://indexer-storage-testnet-turbo.0g.ai";
let failures = 0;
const fail = (msg) => { failures++; console.log(`  FAIL ${msg}`); };

// --- 1. serialiser unit checks (expected values are Python 3.11 repr / json.dumps output)
const floats = [[1.0, "1.0"], [100.0, "100.0"], [0.1, "0.1"], [1e-5, "1e-05"], [0.0001, "0.0001"], [1e16, "1e+16"],
  [1e15, "1000000000000000.0"], [-0.0, "-0.0"], [5e-324, "5e-324"], [1.7976931348623157e308, "1.7976931348623157e+308"],
  [85.64999999999728, "85.64999999999728"], [123456789012345680, "1.2345678901234568e+17"], [-2.5e-7, "-2.5e-07"]];
for (const [x, want] of floats) if (pyFloatRepr(x) !== want) fail(`pyFloatRepr(${x}) = ${pyFloatRepr(x)}, want ${want}`);
const strs = [["a\"b\\c", '"a\\"b\\\\c"'], ["\n\r\t\b\f", '"\\n\\r\\t\\b\\f"'], ["\u0001\u001f\u007f", '"\\u0001\\u001f\u007f"'],
  ["ड़ é 🚗 /", '"ड़ é 🚗 /"']];
for (const [s, want] of strs) if (pyString(s) !== want) fail(`pyString(${JSON.stringify(s)}) = ${pyString(s)}, want ${want}`);
const c = canonical(parseRaw('{"b": 1.0, "a": [1e-5, 2, "x"], "é": null, "Z": true}'));
if (c !== '{"Z":true,"a":[1e-05,2,"x"],"b":1.0,"é":null}') fail(`canonical() = ${c}`);
// not-our-format inputs must be rejected with FormatError
for (const bad of ["hello", '{"code":101,"message":"File not found","data":null}', '{"records": 3}', "[1,2]", '{"records":[1]}']) {
  try { await verifyBatch(bad); fail(`accepted non-batch input ${bad}`); } catch (e) { if (!(e instanceof FormatError)) fail(`${bad}: ${e}`); }
}
console.log(`serialiser unit checks: ${failures ? "FAILED" : "ok"} (${floats.length} floats, ${strs.length} strings, 1 object, 5 rejected inputs)`);

// --- 2. real batch files
const epDir = join(runDir, "episodes");
if (!existsSync(epDir)) { console.error(`no episodes/ under ${runDir}`); process.exit(2); }
const files = readdirSync(epDir).sort().flatMap((robot) =>
  readdirSync(join(epDir, robot)).filter((f) => /^batch-\d+\.json$/.test(f)).sort().map((f) => join("episodes", robot, f)));
console.log(`\n${files.length} batch files in ${runDir}`);
let totalRecs = 0, totalOk = 0;
for (const rel of files) {
  const text = readFileSync(join(runDir, rel), "utf8");
  const rep = await verifyBatch(text);
  const ok = rep.records.filter((r) => r.hashOk && r.linkOk).length;
  totalRecs += rep.records.length; totalOk += ok;
  const frameOk = rep.frames.filter((f) => f.ok).length;
  const line = `${rel.padEnd(28)} ${rep.robotId}  records ${ok}/${rep.records.length} intact  ` +
    `frames ${frameOk}/${rep.frames.length} ok (${rep.recordsWithFrame} records reference a frame)  ` +
    `format ${rep.formatOk ? "ok" : rep.format}  numbers JS would reformat: ${rep.numbersReformatted}`;
  const good = rep.formatOk && rep.chainOk && rep.framesOk;
  console.log(`${good ? "MATCH" : "FAIL "} ${line}`);
  if (!good) {
    failures++;
    for (const r of rep.records) if (!r.hashOk || !r.linkOk) console.log(`      record ${r.index}: claimed ${r.claimedSha} computed ${r.computedSha} link ${r.linkOk}`);
  }
  // tamper test: flip one character inside the first record's JSON and expect a detected break
  const i = text.indexOf('"action_taken"');
  if (i >= 0) {
    const j = text.indexOf('"', i + 16) + 1; // first char of the value
    const tampered = text.slice(0, j) + (text[j] === "X" ? "Y" : "X") + text.slice(j + 1);
    const t = await verifyBatch(tampered);
    if (t.chainOk) fail(`${rel}: tampered copy still verified`);
  }
}
console.log(`\nall files: ${totalOk}/${totalRecs} record hashes + prev links reproduced; tamper test ${failures ? "see above" : "detected every edit"}`);

// --- 3. optional: download from 0G Storage and compare to the local files
if (fetchToo) {
  const roots = JSON.parse(readFileSync(join(runDir, "storage_roots.json"), "utf8"));
  console.log(`\nfetching ${roots.length} roots from ${INDEXER}`);
  for (const e of roots) {
    try {
      const r = await fetch(`${INDEXER}/file?root=${e.root_hash}`);
      const got = new Uint8Array(await r.arrayBuffer());
      const want = readFileSync(join(runDir, e.file));
      const same = got.length === want.length && got.every((b, k) => b === want[k]);
      const rep = same ? await verifyBatch(new TextDecoder().decode(got)) : null;
      console.log(`${same && rep?.chainOk ? "MATCH" : "FAIL "} ${e.root_hash.slice(0, 12)}… ${e.robot_id} ${got.length} bytes ` +
        `${same ? "byte-identical to local" : "DIFFERS from local"}${rep ? `, ${rep.records.length} records verified` : ""}`);
      if (!same || !rep?.chainOk) failures++;
    } catch (err) { fail(`${e.root_hash}: ${err.message}`); }
  }
}

console.log(failures ? `\n${failures} failure(s)` : "\nALL OK");
process.exit(failures ? 1 : 0);
