// Shared helpers for the 0G Storage sidecar CLIs.
//
// Importing this module FIRST (before the SDK) does two things:
//   1. Reroutes everything that would hit stdout (console.* and raw
//      process.stdout.write) to stderr, because the SDK is very chatty.
//      Only `emitResult()` writes to the real stdout: exactly one JSON line.
//   2. Loads env from $ENV_FILE, or else <repo root>/.env. Variables already
//      set in the process env win.
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const realStdoutWrite = process.stdout.write.bind(process.stdout);
const stderrWrite = process.stderr.write.bind(process.stderr);
process.stdout.write = (...args) => stderrWrite(...args);
for (const level of ['log', 'info', 'debug', 'warn', 'trace']) {
  console[level] = (...args) => console.error(...args);
}

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, '..', '..');

const dotenv = await import('dotenv');
const envFile = process.env.ENV_FILE || resolve(REPO_ROOT, '.env');
// override:false => process env takes precedence; quiet => no banner.
dotenv.config({ path: envFile, override: false, quiet: true });

function envOr(name, fallback) {
  const v = process.env[name];
  return v !== undefined && v.trim() !== '' ? v.trim() : fallback;
}

export const RPC_URL = envOr('ZG_RPC_URL', 'https://evmrpc-testnet.0g.ai');
export const INDEXER_URL = envOr('ZG_STORAGE_INDEXER', 'https://indexer-storage-testnet-turbo.0g.ai');

/** Private key for paying storage fees. Never log the return value. */
export function privateKey() {
  const key = envOr('STORAGE_PRIVATE_KEY', envOr('DEPLOYER_PRIVATE_KEY', ''));
  if (!key) {
    throw new Error(`no private key: set STORAGE_PRIVATE_KEY or DEPLOYER_PRIVATE_KEY (env file: ${envFile})`);
  }
  return key.startsWith('0x') ? key : `0x${key}`;
}

export function intEnv(name, fallback) {
  const n = Number.parseInt(envOr(name, ''), 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function errText(err) {
  if (err instanceof Error) return err.message || String(err);
  if (typeof err === 'string') return err;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

/** Write exactly one JSON line to the real stdout, then exit 0. */
export function emitResult(obj) {
  realStdoutWrite(`${JSON.stringify(obj)}\n`, () => process.exit(0));
}

/** Print an error to stderr and exit non-zero. Nothing goes to stdout. */
export function fail(prefix, err) {
  stderrWrite(`${prefix}: ${errText(err)}\n`, () => process.exit(1));
}

/** Run `main`, mapping any throw / rejection to a non-zero exit. */
export function runMain(name, main) {
  process.on('unhandledRejection', (e) => fail(`${name}: unhandled rejection`, e));
  process.on('uncaughtException', (e) => fail(`${name}: uncaught exception`, e));
  main().catch((e) => fail(name, e));
}
