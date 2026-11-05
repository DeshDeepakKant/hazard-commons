#!/usr/bin/env node
// Usage: node download.mjs <root_hash> <out_path>
// Downloads with merkle proof verification, retrying with exponential backoff
// (freshly uploaded files can take minutes to propagate to storage nodes).
// stdout: exactly one JSON line {"ok":true,"out":"<abs path>"}; exit != 0 on failure.
// Env: ZG_DOWNLOAD_RETRIES (attempts, default 6), ZG_DOWNLOAD_BACKOFF_S (first delay, default 5;
// doubles each attempt, capped at ZG_DOWNLOAD_BACKOFF_MAX_S, default 120).
import { INDEXER_URL, emitResult, intEnv, runMain, sleep } from './common.mjs';
import { mkdir, rename, rm } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';

async function main() {
  const [root, outArg] = process.argv.slice(2);
  if (!root || !outArg) throw new Error('usage: node download.mjs <root_hash> <out_path>');
  if (!/^0x[0-9a-fA-F]{64}$/.test(root)) throw new Error(`invalid root hash: ${root}`);
  const out = resolve(outArg);
  await mkdir(dirname(out), { recursive: true });

  const attempts = Math.max(1, intEnv('ZG_DOWNLOAD_RETRIES', 6));
  const baseS = intEnv('ZG_DOWNLOAD_BACKOFF_S', 5);
  const maxS = intEnv('ZG_DOWNLOAD_BACKOFF_MAX_S', 120);

  const { Indexer } = await import('@0gfoundation/0g-storage-ts-sdk');
  const indexer = new Indexer(INDEXER_URL);

  let lastErr = null;
  for (let i = 1; i <= attempts; i++) {
    // The SDK refuses to overwrite an existing path, so download to a fresh
    // temp file next to the target and rename it into place on success.
    const tmp = `${out}.part-${process.pid}-${i}`;
    try {
      const err = await indexer.download(root, tmp, true);
      if (err === null || err === undefined) {
        await rename(tmp, out);
        return { ok: true, out };
      }
      lastErr = err;
    } catch (e) {
      lastErr = e;
    }
    await rm(tmp, { force: true }).catch(() => {});
    const msg = lastErr instanceof Error ? lastErr.message : String(lastErr);
    if (i < attempts) {
      const delayS = Math.min(maxS, baseS * 2 ** (i - 1));
      console.error(`[zg-storage] download attempt ${i}/${attempts} failed: ${msg}; retrying in ${delayS}s`);
      await sleep(delayS * 1000);
    } else {
      console.error(`[zg-storage] download attempt ${i}/${attempts} failed: ${msg}`);
    }
  }
  const msg = lastErr instanceof Error ? lastErr.message : String(lastErr);
  throw new Error(`gave up after ${attempts} attempts: ${msg}`);
}

runMain('download failed', async () => emitResult(await main()));
