#!/usr/bin/env node
// Usage: node upload.mjs <file>
// stdout: exactly one JSON line {"root_hash":"0x..","tx_hash":"0x.."|null}
// exit != 0 on any failure (error on stderr, nothing on stdout).
import { INDEXER_URL, RPC_URL, emitResult, intEnv, privateKey, runMain } from './common.mjs';
import { access, constants, stat } from 'node:fs/promises';

const nonEmpty = (s) => (typeof s === 'string' && s !== '' ? s : null);

async function main() {
  const path = process.argv[2];
  if (!path) throw new Error('usage: node upload.mjs <file>');
  await access(path, constants.R_OK);
  const st = await stat(path);
  if (!st.isFile()) throw new Error(`not a regular file: ${path}`);
  if (st.size === 0) throw new Error(`refusing to upload empty file: ${path}`);

  const key = privateKey();
  // SDK imported only after common.mjs has rerouted stdout.
  const { Indexer, ZgFile } = await import('@0gfoundation/0g-storage-ts-sdk');
  const { ethers } = await import('ethers');

  const provider = new ethers.JsonRpcProvider(RPC_URL);
  const signer = new ethers.Wallet(key, provider);
  const indexer = new Indexer(INDEXER_URL);

  const timeoutS = intEnv('ZG_UPLOAD_TIMEOUT_S', 900);
  const file = await ZgFile.fromFilePath(path);
  try {
    const [tree, treeErr] = await file.merkleTree();
    if (treeErr !== null || tree === null) throw new Error(`merkle tree: ${treeErr?.message ?? treeErr}`);
    const localRoot = tree.rootHash();
    console.error(`[zg-storage] uploading ${path} (${st.size} B) root=${localRoot} from ${signer.address}`);

    let timer;
    const timeout = new Promise((_, rej) => {
      timer = setTimeout(() => rej(new Error(`upload timed out after ${timeoutS}s`)), timeoutS * 1000);
    });
    const [tx, err] = await Promise.race([indexer.upload(file, RPC_URL, signer), timeout]).finally(() =>
      clearTimeout(timer),
    );
    if (err !== null && err !== undefined) throw err instanceof Error ? err : new Error(String(err));
    if (!tx) throw new Error('upload returned no result');

    // Single upload: {txHash, rootHash, txSeq}. Fragmented (>4GB): {txHashes[], rootHashes[], txSeqs[]}.
    // txHash is '' when the SDK skipped the tx (file already finalized on-chain) -> null.
    if (Array.isArray(tx.rootHashes)) {
      const rootHashes = tx.rootHashes;
      const txHashes = (tx.txHashes ?? []).map(nonEmpty);
      if (rootHashes.length === 0) throw new Error('fragmented upload returned no root hashes');
      if (rootHashes.length === 1) return { root_hash: rootHashes[0], tx_hash: txHashes[0] ?? null };
      return { root_hash: rootHashes[0], tx_hash: txHashes[0] ?? null, root_hashes: rootHashes, tx_hashes: txHashes };
    }
    const rootHash = nonEmpty(tx.rootHash) ?? localRoot;
    if (!rootHash) throw new Error('upload returned no root hash');
    return { root_hash: rootHash, tx_hash: nonEmpty(tx.txHash) };
  } finally {
    await file.close().catch(() => {});
  }
}

runMain('upload failed', async () => emitResult(await main()));
