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
.venv/bin/pytest -q                                   # 79 tests
chain/deploy_galileo.sh --env-file .env               # deploy HazardRegistry to Galileo (chain 16602)
.venv/bin/python -m brain.spike --call                # one real 0G Router call
scripts/run_demo.sh --traffic                         # live sim server on :8765 (DEMO=1)
.venv/bin/python scripts/record_run.py --duration 240 --traffic --run-id demo-1
(cd viewer && npm ci && npm run dev)                  # viewer; ?replay=<stream.jsonl> for replay
.venv/bin/python scripts/test_safestop.py             # safe-stop proof
```

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
