# Hazard Commons: operational notes

Full design: `docs/SPEC.md`. Frozen interfaces: `docs/INTERFACES.md`, `sim/schemas.py`, `brain/schemas.py`, `common/interfaces.py`, `common/frame.py`, `viewer/src/types.ts`. Don't change these without the lead.

## Rules
- **Never fake a 0G decision.** No heuristic, cache or other AI provider standing in for a 0G call. If 0G fails, the robot goes to `SAFE_STOP`. `MOCK_BRAIN=1` is dev-only (`brain/mock.py`), prints a banner, and is refused when `DEMO=1`.
- 0G is the brain, not the spinal cord: steering, PID, lidar reflex and path following are local; 0G is called only at checkpoints CP-H1/H2/V/D.
- The sim never blocks on the network: slow calls return Futures that the sim polls each tick.
- Strip `_`-prefixed context keys before anything goes to 0G.
- Secrets live only in `.env` (git-ignored). Never print private keys. Testnet only. Keep stakes tiny (0.0001 0G).
- Fictional operators only: ZipKart, MinuteMart, DashKirana. Attribution: "© OpenStreetMap contributors"; photo credits in `assets/hazard_photos/ATTRIBUTION.md`.
- India drives on the left (SUMO `--lefthand`).
- Commit often with meaningful messages.

## Env
- Python 3.11 venv at `.venv` (uv). Run things with `.venv/bin/python -m ...` from the repo root. Tests: `.venv/bin/pytest`.
- SUMO via pip (`eclipse-sumo`; `sumo.SUMO_HOME`, `netconvert` in `.venv/bin`). Use `libsumo` in-process.
- Foundry: `~/.foundry/bin/forge` (evm_version cancun for 0G Galileo, chain 16602).
- Node 22; viewer uses Vite + three; Storage sidecar `memory/zg-storage` uses `@0gfoundation/0g-storage-ts-sdk`.

## 0G facts (verified 2026-11-06)
- Router (OpenAI-compatible): `https://router-api.0g.ai/v1` (the testnet router was unreachable). Model `0gm-1.0-35b-a3b` (vision + tools + json, TeeML).
- Request id = `X-Request-ID` response header == `x_0g_trace.request_id`. Also send `X-Client-Request-Id: {episode_id}:{seq}`.
- TEE: header `X-0G-Provider-Trust-Mode: private`, body `verify_tee: true` → `x_0g_trace.tee_verified`. A 503 `no_provider_for_trust_mode` → SAFE_STOP.
- Galileo RPC `https://evmrpc-testnet.0g.ai`, chainscan-galileo.0g.ai, storagescan-galileo.0g.ai, indexer `https://indexer-storage-testnet-turbo.0g.ai`.
