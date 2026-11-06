# Hazard Commons

**Rival delivery robots in a real 3D Mumbai share one verified truth about the streets: accidents, road works, waterlogging. 0G decides it, records it, and enforces it on 0G Chain.**

ETHGlobal Mumbai 2026 · 0G track *Physical AI: Agents with a Body* · lane *Robots and machines* (simulation).

Operators are fictional (ZipKart, MinuteMart, DashKirana), inspired by quick-commerce apps. No real brands or logos are used.

## The problem
Quick-commerce companies run fleets of delivery robots and drones from neighbourhood dark stores. When one robot finds an accident, road works or a waterlogged lane, every robot should learn it, including rivals' robots. But rivals don't trust each other: a company may withhold reports or post fake ones to slow competitors, and nobody accepts a rival owning the shared map.

## How 0G is used
| Layer | What it does here | Unplug it and... |
|---|---|---|
| **0G Compute** (Router, model `0gm-1.0-35b-a3b`, TEE provider) | Every checkpoint decision: is this a hazard, is it passable for *my body type*, do I trust a rival's report, does a verifier confirm or dispute | reports become unprovable claims; a cheating operator poisons the map |
| **0G Storage** | Hash-chained per-robot episode logs (one record per decision, each with its 0G request id) and the evidence frames | no durable record or audit trail |
| **0G Chain** (`HazardRegistry`) | Live shared map: report (with stake), verify by a *different* operator, confirm / reject + slash | no neutral shared state between rivals, no reason to report honestly |

**Brain, not spinal cord.** Steering, wheel control, lidar reflex braking and drone stabilisation run locally in PyBullet every tick. 0G is called only at checkpoints:
- `CP-H1` hazard encounter (camera frame → type, severity, passable for rover/drone)
- `CP-H2` route-ahead (a chain report is on my path → proceed / reroute / wait / hand off)
- `CP-V` verification (a rival's rover or a drone confirms or disputes a report)

**Safe stop, never a faked decision.** If 0G times out, is unreachable, returns a 503, or returns invalid JSON twice, the robot goes to `SAFE_STOP` (rover pulls to the kerb and holds; drone hovers, then lands). There is no heuristic, cache or other provider standing in. `MOCK_BRAIN=1` exists only for physics development, prints a banner, and is refused when `DEMO=1`. `DEMO=1` also refuses any chain other than Galileo.
Proof: `scripts/test_safestop.py` points the Router at a dead address; every robot safe-stops and no decision is fabricated.

## Architecture
```
            ┌────────────── 0G ───────────────┐
            │ Compute: checkpoint decisions    │
            │ Storage: episode logs + evidence │
            │ Chain:   HazardRegistry          │
            └───────▲──────────────▲───────────┘
                    │ checkpoints  │ txs / events
┌──────── Python sim (fixed timestep) ──────────────────────┐
│ PyBullet: rover & drone bodies, lidar, cameras             │
│ SUMO (libsumo): cars, autos, bikes, buses, pedestrians     │
│ Robot FSM · planner · pure pursuit · reflex brake           │
│ Operators, dispatcher, orders · scenario engine             │
│ FastAPI + WebSocket                                         │
└───────────────────────▲────────────────────────────────────┘
                        │ ws / recorded stream.jsonl
              Three.js viewer (live + replay)
```
World: OpenStreetMap Bandra West (~1.6 × 1.5 km, 2,013 buildings, 570 road segments, left-hand traffic).

## Run it
Requires Linux, `uv`, Node 22, Foundry.
```bash
uv python install 3.11 && uv venv -p 3.11 .venv
uv pip install -p .venv -e ".[dev]"
cp .env.example .env              # add ZG_ROUTER_API_KEY and funded testnet keys
.venv/bin/python scripts/gen_wallets.py   # fresh throwaway testnet keys (prints addresses only)
(cd memory/zg-storage && npm ci)  # 0G Storage sidecar (official TS SDK)
(cd chain/foundry && forge install --no-git foundry-rs/forge-std)
.venv/bin/pytest -q tests/test_wallets.py tests/test_brain.py   # targeted; the full suite takes >5 min
chain/deploy_galileo.sh --env-file .env               # deploy HazardRegistry to Galileo (chain 16602)
.venv/bin/python -m brain.spike --call                # one real 0G Router call
scripts/run_demo.sh --traffic                         # live sim server on :8765 (DEMO=1)
scripts/run_demo.sh --traffic --loop                  # same, replays the story every 360 s (max 5 cycles)
.venv/bin/python scripts/record_run.py --duration 240 --traffic --run-id demo-1
(cd viewer && npm ci && npm run dev)                  # viewer; ?replay=<stream.jsonl> for replay
.venv/bin/python scripts/test_safestop.py             # safe-stop proof
```

## Inject a disaster

In live mode the top bar has a **Disaster** button: pick accident / road works / waterlogging, then
**Random disaster** (placed ahead of a moving rover) or **Place on map** (click anywhere; it snaps to the
nearest drivable road; Esc cancels). A rover that reaches it runs the normal flow with real 0G calls:
CP-H1 photo decision, on-chain report, rival-drone CP-V verification, others reroute at CP-H2, SUMO
traffic closes the edge. Tick **Fake report** to report a hazard that is not there and watch rivals dispute
it. The list in the popover clears a disaster. REST: `POST /api/hazards`, `POST /api/hazards/random`,
`GET /api/hazards/props`, `DELETE /api/hazards/{prop_id}`. The edge is not pre-blocked: closures come only
from the genuine report/verify flow.

## Agents that pay for themselves (0G track 1.4)

Every body (8 rovers, 2 drones) is its own economic agent with its own credit wallet (`brain/wallets.py`).
Enable with `scripts/run_demo.sh --wallets` (or `ZG_WALLETS=1`); `ZG_WALLET_START=400` starts every body
nearly broke so the downgrade is visible within a minute.

```
 0G call ──> debit real cost (x_0g_trace.billing.total_cost_usd_micro)
   ▲                                   │
   │   tier by balance / start credit  ▼
   │   normal  ≥40%   default TEE model 0gm-1.0-35b-a3b   (~90–130 µUSD/call)
   │   economy <40%   cheaper 0G model qwen2.5-vl-72b, NO TEE (~60 µUSD with a photo, ~24 text-only)
   │   throttled <15% economy model + max one call per 20 s
   │   paused  < one call   no model call → SAFE_STOP (never a guessed decision)
   │
 income, no human in the loop:
   +600  reporter of a hazard that rivals confirmed on 0G Chain (HazardResolved event)
   +150  verifier (drone or rival rover) whose CP-V verdict matched the on-chain outcome
   +300  delivered order (SIMULATED fee, labelled "simulated" in the UI and ledger)
```

- The robot reads its own balance before each call; the gate runs before the global call budget, and a
  failed call is refunded. Each decision card shows cost, balance after, tier and route; the **Wallets**
  button and the fleet **Credit** column show balances, earnings and the ledger. `GET /api/wallets`.
- Per-robot lifetime spend cap (`max_spend_micro_usd`) so a runaway loop cannot drain the account.
- Economy cards say **NO TEE** — the robot trades verifiability for price, visibly, never silently.

**What is not real (honest limits).** Wallet balances are per-robot accounts inside the sim, debited with
the real per-call 0G cost but funded from one shared 0G Router account (USD-billed, $5.99 credit). Earnings
are credited to the robot's wallet when the on-chain event happens; they do **not** move money into the
0G Router account. The two paths that would close that loop were not usable this weekend: 0G's mainnet
self-payment route (comfy) needs a 0G-billed account and mainnet 0G (ours is USD-billed, switching is
manual); the testnet Router API (`router-api-testnet.integratenetwork.work`) did not complete a TLS
handshake for the whole event, so a deposit → Payment Layer → balance top-up could not be tested.
**Proof run** (`scripts/wallet_demo.py`, real 0G Router calls through the production `ZGBrain` +
wallet code, one body starting at 300 µUSD, real camera frame): 2 calls on `0gm-1.0-35b-a3b` (90, 92 µUSD)
→ economy → 2 calls on `qwen2.5-vl-72b-instruct` (62, 61 µUSD, still classified the accident) → paused →
5th checkpoint made **no** model call and returned SAFE_STOP → +600 hazard income → back to normal.
Request ids: `docs/EVIDENCE.md` § Agents that pay for themselves; raw output `docs/wallet_demo.json`.

## 0G evidence
Verified on 2026-11-06: a real Router call to `0gm-1.0-35b-a3b` with a composited accident photo returned a valid verdict in ~7 s.
- request id (`x_0g_trace.request_id`): `e3deff71-90f3-4022-858d-f483bbf32fab`
- provider: `0xf4FB8d2bD856e672b5f9848b0eD235A20079B250`; response header `x-0g-e2ee: sealed`
- `tee_verified` is **not** reported for sealed requests (per the Router docs), so it is shown as "–" in the UI, not as verified.

HazardRegistry on 0G Galileo: [`0x2098b78B804a9506f7A9074FBd35C76906421Ba0`](https://chainscan-galileo.0g.ai/address/0x2098b78B804a9506f7A9074FBd35C76906421Ba0). Live demo (replay of the real run): https://hazard-commons.vercel.app/ . All request ids, transaction hashes and Storage root hashes: `docs/EVIDENCE.md`.

**Verify any episode log yourself, in the browser.** In the viewer, open a decision card and click **Verify on 0G Storage**, or use **✓ Verify log** in the top bar and paste any root hash from `docs/EVIDENCE.md` (or open `?verify=<root>`, e.g. https://hazard-commons.vercel.app/?verify=0x407aae05b1c5373d648d826ba965ac227816f369533b13488bf31a51f69449cd). The page downloads the file straight from the 0G Storage indexer (no backend of ours involved), recomputes every record's `sha256(canonical_json(record))` and `prev_record_sha256` link, checks each camera frame against its `frame_sha256`, and compares each record with the matching decision card (by 0G request id). `cd viewer && node scripts/verify-check.mjs` runs the same code against the local batch files to show it reproduces Python's hashes byte for byte.

## Disclosures
- **Perception.** PyBullet renders look like toy scenes, so at hazard checkpoints a real CC-licensed photo of the hazard type is composited with the rendered frame and sent to the vision model. Photo credits: `assets/hazard_photos/ATTRIBUTION.md`.
- **Simulation only.** No hardware.
- **Map data** © OpenStreetMap contributors (ODbL). Building heights are mostly estimated (a seeded 3–12 floors when OSM has none).
- **AI tools.** The code was written with Claude Code (planning, Solidity, Python, viewer), with parallel subagents per workstream.
- Hosted replay viewer: see the live demo link in the submission.

## Layout
`world/` OSM → meshes, road graph, POIs · `traffic/` SUMO · `sim/` bodies, FSM, engine, scenario · `brain/` 0G Router client, prompts, perception · `memory/` episode log + Storage sidecar · `chain/` Foundry contract + bindings · `ops/` orders and dispatch · `server/` FastAPI/WS · `viewer/` Three.js · `scripts/` demo, record, safe-stop test · `docs/` spec, interfaces, evidence
