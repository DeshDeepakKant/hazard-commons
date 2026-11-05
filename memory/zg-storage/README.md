# zg-storage sidecar

Node CLIs for 0G Storage (`@0gfoundation/0g-storage-ts-sdk`). `npm install` once.

```sh
node upload.mjs <file>             # stdout: {"root_hash":"0x..","tx_hash":"0x.."|null}
node download.mjs <root> <out>     # stdout: {"ok":true,"out":"/abs/path"}  (proof-verified, retries)
```

Exactly one JSON line on stdout; all SDK logs go to stderr; exit != 0 on failure.
`tx_hash` is `null` when the file was already finalized on-chain and no new tx was sent.

Env (from `$ENV_FILE`, else repo-root `.env`; process env wins): `STORAGE_PRIVATE_KEY`
(falls back to `DEPLOYER_PRIVATE_KEY`), `ZG_RPC_URL`, `ZG_STORAGE_INDEXER`,
`ZG_UPLOAD_TIMEOUT_S` (900), `ZG_DOWNLOAD_RETRIES` (6), `ZG_DOWNLOAD_BACKOFF_S` (5),
`ZG_DOWNLOAD_BACKOFF_MAX_S` (120).
