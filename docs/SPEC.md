# Hazard Commons — Build Spec for Claude Code

> **Working name:** Hazard Commons (rename later; ETHGlobal name limit is 20 chars)
> **Target:** ETHGlobal Mumbai 2026 → 0G track "Physical AI: Agents with a Body" → *Robots and machines* lane
> **Hard deadline:** Saturday Nov 7, 2026, **09:00 IST** (portal locks). Aim to submit by **08:00**.
> **Builder:** solo, strong in AI/agents/voice/Python, new to Ethereum. Claude Code writes all code (incl. Solidity). Laptop: Ryzen 5 5500U, 16 GB RAM, integrated AMD GPU (no CUDA), Linux. **Simulation only — no hardware.**

---

## 0. How to use this document (instructions to Claude Code)

1. **Start in plan mode.** Read this whole file. Then ask me every question in **§17 "Questions you must ask me"** that you can't answer yourself, *before* writing code.
2. Re-verify every fact in **§4** against live docs (endpoints, chain ID, SDK names, model IDs). Things change; if a fact is wrong, tell me and adapt.
3. Do **§15 Workstream 0 (Foundations)** yourself first — it freezes the shared interfaces in **§14**. Only then fan out to **parallel subagents** (one per workstream, each in its own git worktree), merging through you.
4. Commit early and often with meaningful messages (ETHGlobal checks that commits fall inside the hackathon window; one giant commit looks bad).
5. Whenever you're blocked on something only I can provide (keys, tokens, choices, photos, accounts), **stop and ask** — don't invent placeholders that end up in the demo.
6. **Never fake a 0G decision.** No local heuristic, no other AI provider, no cached answer standing in for a 0G call in the demo path. A dev-only `MOCK_BRAIN=1` flag is allowed for physics testing, must print a loud banner, and must be impossible to enable in the demo/submission build.

---

## 1. One-line pitch & the problem

**Pitch:** *Rival delivery robots in a real 3D Mumbai share one verified truth about the streets — accidents, road works, waterlogging — decided by 0G, recorded on 0G, enforced on 0G Chain.*

**Problem:** In a near-future Mumbai, quick-commerce companies (think Blinkit/Instamart-style 10-minute delivery) run fleets of autonomous delivery robots and drones from neighbourhood **dark stores** to customers' homes. Streets change constantly. When one robot discovers an accident, road works or a waterlogged lane, every other robot should learn it — **including robots of rival companies**. But rivals don't trust each other: a company may withhold reports or post fake ones to slow competitors, and no company accepts a rival owning the shared map.

**Use fictional operator names** (e.g. *ZipKart*, *MinuteMart*, *DashKirana*) — do **not** use real brand names or logos in the app, video or repo. Pitch can say "inspired by quick-commerce apps".

---

## 2. Exact hackathon track text (verbatim) + compliance checklist

```
Physical AI: Agents with a Body ⸺ $5,000
1st place $2,500 · 2nd place $1,500 · Best build on real hardware $1,000

A robot, drone, wearable or pocket device whose decisions run on 0G. You don't need
hardware: simulated builds are eligible for every prize, and one prize is held for the
best build on real hardware. 0G is the brain, not the spinal cord: motion stays on the
device, while 0G makes the decisions at key checkpoints and keeps the record.

Lanes:
* Robots and machines: arms, rovers, drones, in simulation or hardware
* Personal devices: an open, verifiable alternative to Meta's Muse assistant, on glasses,
  a pocket device or a desktop companion, with the user's memory on 0G Storage and sealed inference

Build without hardware: start from zerobot, 0G's open-source robot control SDK, which
includes a mock robot so you can build and test with no hardware (it currently targets the
Unitree Go2 on 0G Galileo testnet). Or bring your own simulator: MuJoCo, PyBullet,
Gazebo + ROS 2 or LeRobot.

Not wanted: a chatbot with a robot avatar, pre-recorded motion, 0G used only for logs.

Must show:
* A 0G call inside the decision loop: vision, voice or decision model
* An episode log on 0G Storage with a request id per decision
* A replayable simulation, or a live hardware demo at the 0G booth
* A safe-stop fallback if 0G is unreachable. A fallback that fakes the decision disqualifies.

Submission requirements:
* Project name and a short description
* Public GitHub repo (README + setup instructions)
* Demo video (under 3 mins) and a live demo link
* Contract deployment addresses
* Team member names and contact info (Telegram and X)

Prizes are paid at 0G's discretion. We only pay out to projects that meet our quality
threshold, so not every place is guaranteed to be awarded.
```

| Requirement | How we satisfy it | Owner (§15) |
|---|---|---|
| Robot/drone whose decisions run on 0G | Delivery rovers + drones; all checkpoint decisions via 0G Router | WS3, WS4 |
| Brain not spinal cord | Steering, wheels, braking, flight stabilisation local in PyBullet; 0G only at checkpoints (§10) | WS3, WS4 |
| Simulator from the list | **PyBullet** is the physics sim (on 0G's list). SUMO only drives background traffic | WS2, WS3 |
| 0G call inside decision loop | Vision + decision model calls at every checkpoint | WS4 |
| Episode log on 0G Storage w/ request id per decision | Per-robot episode files uploaded to 0G Storage; each entry carries the 0G request id | WS4 |
| Replayable simulation | Deterministic seeds + recorded state stream; replay in viewer | WS3, WS7 |
| Safe stop, never fake | Timeout/unreachable → robot pulls over & holds; drone hovers then lands | WS3, WS4 |
| Not wanted (avatar / pre-recorded / logs-only) | Motion computed live; 0G decides | all |
| Contract addresses | `HazardRegistry` (+ optional `OrderReceipt`) on 0G Galileo | WS5 |
| Video < 3 min + live demo link | Video 2:30–2:50 (also satisfies portal's 2–4 min); hosted viewer/replay URL | WS7, WS8 |

---

## 3. Hard constraints

- CPU-only laptop. Keep the map **small (~1–1.5 km²)**, robots **6–10**, drones **1–2**, background cars/pedestrians in the low hundreds max.
- Simulation only. No hardware code paths needed.
- Time: ~30 hours from start. Feature freeze at **Sat 02:00 IST**; video + docs after that.
- Testnet only. Throwaway keys in `.env` (git-ignored). Never commit secrets.
- Faucet gives only **0.1 0G/day per wallet** → keep stakes/deposits tiny (e.g. 0.0001 0G) and minimise on-chain writes; ask me to get more tokens/credits from the 0G booth.
- Map data attribution: "© OpenStreetMap contributors" (ODbL) in viewer + README. Attribute any photos used.
- India drives on the **left** — the traffic network must be left-hand.

---

## 4. Verified technical facts (re-verify before use)

| Item | Value (as researched) | Source to re-check |
|---|---|---|
| 0G Galileo testnet chain ID | **16602** | docs.0g.ai/developer-hub/testnet/testnet-overview |
| Galileo RPC | `https://evmrpc-testnet.0g.ai` (dev only) | same |
| Chain explorer | `https://chainscan-galileo.0g.ai` | same |
| Storage explorer | `https://storagescan-galileo.0g.ai` | same |
| Faucet | `https://faucet.0g.ai` (0.1 0G/day) | same |
| 0G Compute Router | OpenAI-compatible (`/v1/chat/completions`, streaming, **tool calling**). Mainnet base `https://router-api.0g.ai/v1`; testnet base `https://router-api-testnet.integratenetwork.work/v1` | docs.0g.ai/developer-hub/building-on-0g/compute-network/router/overview |
| Router TEE / sealed | Router verifies provider TEE signature and reports `tee_verified` inside `x_0g_trace`; a privacy mode can force sealed (TEE-only) inference | router docs (privacy page) |
| Request id | **Find exactly which response field 0G treats as the request id** (response `id`, `x_0g_trace`, headers). Log all of them. Ask me to confirm with the 0G booth | router docs |
| Models | Pick from the Router model list: one **vision-capable** model (e.g. Qwen-VL family listed by 0G) + one text/decision model (e.g. `0GM-1.0-…`). Confirm availability on **testnet** vs mainnet | pc.0g.ai/models |
| 0G Storage testnet indexer | `https://indexer-storage-testnet-turbo.0g.ai` | docs.0g.ai/ai-context |
| Storage SDKs | Official: TS `@0gfoundation/0g-storage-ts-sdk`, Go CLI `0g-storage-client` (upload/download/KV). Python: community SDK (`0g-storage-sdk` on PyPI) — usable but not first-party | docs + PyPI |
| Storage latency | Uploads return a **root hash** quickly, but files can take **~3–5 min** to propagate before download works → **never put Storage on the real-time path** | og-py docs |
| PyBullet | CPU physics + CPU camera rendering (`getCameraImage`), bundled robot models in `pybullet_data` (e.g. husky, racecar) | pybullet.org |
| Drones | `gym-pybullet-drones` (Crazyflie models + PID control) | github utiasDSL/gym-pybullet-drones |
| Traffic | SUMO: `netconvert` (OSM import, `--lefthand`, `--sidewalks.guess`, `--crossings.guess`), `randomTrips.py` (vehicles + `--pedestrians`), Python **TraCI** for stepping and `vehicle.moveToXY` | sumo.dlr.de/docs |
| Mumbai 3D buildings | OSM footprints (+ `height` / `building:levels` where tagged). Better heights: **GlobalBuildingAtlas** (TU Munich) estimates; Overture Maps buildings also open | OSM, GBA, Overture |
| Projection | Mumbai = **UTM zone 43N (EPSG:32643)** | — |

---

## 5. System architecture

```
                ┌──────────────── 0G ────────────────┐
                │ Compute (Router): checkpoint brain  │
                │ Storage: episode logs + evidence    │
                │ Chain: HazardRegistry (live truth)  │
                └──────────▲───────────▲──────────────┘
                           │ checkpoint │ events / txs
┌──────────── Python sim process (fixed timestep) ────────────────┐
│ World (static meshes, road graph)                                │
│ SUMO bridge ── background cars, autos, bikes, pedestrians         │
│ PyBullet ── robot & drone bodies, lidar rays, cameras, collisions │
│ Robot agents: route planner · pure-pursuit · reflex brake · FSM   │
│ Operators: 3 rival companies, dark stores, dispatchers            │
│ Scenario engine: scheduled hazards + a cheating operator          │
│ FastAPI + WebSocket: state stream, orders API, decision feed      │
└───────────────────────────────▲──────────────────────────────────┘
                                │ ws/json
                  Three.js viewer (3D Mumbai + live decision panel,
                  customer order page, replay mode) → hosted = live demo link
```

### Brain vs spinal cord (non-negotiable split)

| Local "spinal cord" (ms, every tick) | 0G "brain" (seconds, checkpoints only) |
|---|---|
| Wheel/motor control, steering (pure pursuit), lane/sidewalk following | "Is this a real hazard? Type, severity, blocks *my* body type?" |
| Reflex braking from lidar rays (pedestrian/car suddenly close) | "Trust this rival's report? Reroute / wait / hand off to drone?" |
| Drone attitude/position PID | Drone: "Confirm or dispute the reported hazard from aerial view" |
| Path execution along a planned route | "Am I at the correct customer building/gate?" (optional) |

Route **computation** (shortest path on the road graph) is local; the **decision to reroute** and the **map facts it uses** come from 0G.

### Why each 0G component is necessary (unplug test)

| Unplug | What breaks |
|---|---|
| Compute | Hazard reports become unprovable claims; a cheating operator poisons the map |
| Storage | No durable per-robot record, no evidence anyone can audit after a dispute |
| Chain | No neutral shared state between rivals, no stakes/rewards/slashing, no reason to report honestly |

**Real-time design consequence:** live shared truth = **0G Chain events** (small: hazard id, road edge, type, status, evidence root, 0G request id). Heavy data (frames, full episode logs) = **0G Storage** (eventual). Storage KV for a live map is a *stretch* only if you verify read latency is low enough.

---

## 6. World: small 3D map of Mumbai

**Default area (confirm with me):** Bandra West residential core near the venue — approx. **lat 19.052–19.066, lon 72.826–72.842** (≈1.5 × 1.7 km). Alternative: BKC + Kalanagar (venue district, fewer homes). Verify the box on openstreetmap.org before fetching; shrink if the sim is slow.

Pipeline (`world/`):
1. Fetch OSM for the bbox (osmnx / Overpass): drivable + walkable network, building footprints, POIs (shops/commercial for dark stores; residential/apartments for homes), `addr:*` tags.
2. Heights: OSM `height` → else `building:levels × 3.2 m` → else (if time) GlobalBuildingAtlas estimate → else deterministic pseudo-random 3–12 floors seeded by building id. Record the source per building.
3. Project everything to EPSG:32643, then a local frame with origin at bbox centre (metres, x=east, y=north, z=up). **One frame for SUMO, PyBullet and the viewer** — align with SUMO's `netOffset` from `net.xml`.
4. Export:
   - `world/out/buildings.glb` — extruded footprints for the viewer (simple per-building colours by use).
   - `world/out/collision/` — simplified boxes/convex hulls for PyBullet (only buildings within ~30 m of roads need collision).
   - `world/out/road_graph.json` — nodes/edges with ids matching SUMO edge ids where possible, lengths, lane counts.
   - `world/out/pois.json` — 3 dark stores (one per operator) + ~50 home buildings with display addresses (real `addr:*` if present, else "<building name/OSM id>, <street name>, Bandra West"). Synthetic customer names only.
5. Optional visual upgrade (viewer only, not required): Google Photorealistic 3D Tiles needs a billed Google Maps API key, attribution, no caching/extraction, and can't enter the physics sim. Don't depend on it.

---

## 7. Autonomous humans & vehicles (background traffic)

Use **SUMO** as the traffic brain for non-robot actors (`traffic/`):
- `netconvert --osm-files map.osm --lefthand --sidewalks.guess --crossings.guess --output.street-names true` (+ `--geometry.remove`, `--roundabouts.guess`, `--ramps.guess`, `--junctions.join`).
- vTypes: car, motorcycle, bus, **auto-rickshaw** (custom vType: small, slow), pedestrian. Generate demand with `randomTrips.py` (vehicles and `--pedestrians`), seeded.
- `traffic/bridge.py`: steps SUMO via TraCI in lockstep with the sim clock; each step reads poses of all vehicles/persons and:
  - mirrors them into PyBullet as **kinematic** bodies (boxes/capsules) so robot lidar and cameras see them;
  - publishes them on the state stream for the viewer (instanced meshes).
- Inject our robots into SUMO each step via `traci.vehicle.moveToXY` (custom vType, e.g. "delivery_bot") so cars yield to them. If this misbehaves, fall back to robots on sidewalks only.
- Hazards close lanes in SUMO too (`traci.lane.setDisallowed` / edge speed 0) so background traffic jams realistically around accidents.

---

## 8. Robots & drones (bodies + local control)

`sim/`:
- **PyBullet in DIRECT mode** (no GUI; the viewer is separate). Physics 240 Hz substeps, control/sync at ~10–20 Hz.
- **Delivery rover**: a small wheeled robot (start from `pybullet_data` husky/racecar, scale down), velocity-controlled wheels, cargo box on top, coloured per operator.
- **Driving stack** (all local):
  1. Route planner: shortest path on `road_graph` with edge costs + blocked edges from the shared hazard state.
  2. Path follower: waypoints → pure pursuit steering → wheel velocities.
  3. Reflex layer: `rayTestBatch` lidar fan; emergency brake if obstacle < threshold. This is reflex, not a decision — it never replaces a 0G checkpoint.
  4. Camera: `getCameraImage` at **checkpoints only**, ~320×240.
- **Robot FSM:** `IDLE → TO_STORE → LOADING → DELIVERING → AT_DOOR → HANDED_OVER → RETURNING`, plus `WAITING_FOR_BRAIN` and `SAFE_STOP`.
- **Drone** (`gym-pybullet-drones`): cruise ~40 m altitude; tasks: verify a hazard report from above, and (stretch) carry a parcel across a waterlogged segment.
- **Safe stop:** if a 0G call times out (configurable, e.g. 15 s) or errors → rover pulls to the kerb and holds; drone hovers, then lands at a safe point. Robot also **ignores unverified hazard reports** while in safe stop. Resume only after a successful 0G decision.

---

## 9. Orders & operators (quick-commerce workflow)

`ops/`:
- **3 rival operators**, each with one dark store, 2–4 rovers, a shared pool of 1–2 drones (or one drone per operator if cheap enough).
- **Customer order page** (in the viewer app): pick a home building on the 3D map or from a list → choose items → choose operator → place order. Also an "auto-orders" generator for background demand.
- **Dispatcher** (local, per operator): assigns nearest idle robot; robot drives to dark store → LOADING (timed) → delivers to the building's entrance point (nearest walkable node to the building) → AT_DOOR → simulated customer OTP confirm → HANDED_OVER.
- Order status visible in the customer page and on the robot in the viewer.
- (Optional) `OrderReceipt` on-chain event at hand-over with the episode root hash — only if time allows; don't spend scarce faucet tokens on every order.

---

## 10. 0G integration (the brain, the memory, the rules)

### 10.1 Checkpoints (each = one 0G call, logged)

| ID | Trigger | Inputs | 0G output (strict JSON) |
|---|---|---|---|
| CP-H1 Hazard encounter | A hazard prop enters camera/lidar range on the robot's path | camera frame (+ composited real photo, §10.5), robot type, task, location | `{is_hazard, type: accident|road_works|waterlogging|none, severity 0-1, passable_for: [rover, drone], confidence}` |
| CP-H2 Route-ahead decision | Next edge on route has an active report in HazardRegistry | report data, confirmations/disputes, reporter operator, evidence summary, my task urgency | `{action: proceed|reroute|wait|handoff_to_drone, reason, trust_report: bool}` |
| CP-V Verification | Drone (or rover of a *different* operator) assigned to verify a report | aerial/ground frame of the reported spot + the claim | `{verdict: confirm|dispute, observed_type, confidence}` |
| CP-D Delivery arrival (optional) | Robot reaches target building | camera frame + expected building description | `{at_correct_location: bool, confidence}` |

Rules:
- Use structured output / JSON mode or tool calling; validate with pydantic; on invalid JSON → retry once → else **safe stop** (never guess).
- While a call is in flight the robot is in `WAITING_FOR_BRAIN` (slows to a stop at the checkpoint).
- Budget: only call at checkpoints; target ≤ ~60–100 0G calls per demo run. Cache nothing across checkpoints.
- Request privacy/sealed (TEE) mode if available; record `tee_verified`.

### 10.2 Episode log (0G Storage)
One record per decision, appended to the robot's episode and uploaded in batches (e.g. per delivery or every N decisions):
```json
{
  "episode_id": "...", "robot_id": "R-07", "operator": "MinuteMart",
  "sim_time": 312.4, "checkpoint": "CP-H2",
  "inputs": {"frame_sha256": "...", "context": {...}},
  "model": "...", "zg_request_id": "...", "zg_trace": {...}, "tee_verified": true,
  "output": {...}, "action_taken": "reroute",
  "chain_tx": "0x...", "prev_record_sha256": "..."
}
```
- Store the frame images too (or their hashes + a frames bundle) so a judge can audit.
- Keep a local mirror of every uploaded file + its root hash in `logs/index.json` (for the viewer and README), but the **0G Storage root hashes are the source of truth**.
- Hash-chain records (`prev_record_sha256`) so tampering is detectable.

### 10.3 HazardRegistry (0G Chain, Solidity via Foundry)
- `registerOperator`, `registerRobot(robotId, operator, kind)` (admin or self-registration with deposit).
- `reportHazard(edgeId, hazardType, severity, evidenceRoot, zgRequestId)` **payable** (tiny stake).
- `verifyHazard(hazardId, confirm|dispute, evidenceRoot, zgRequestId)` — only robots of a **different** operator than the reporter.
- Resolution: N confirms → `Confirmed` (stake refunded + small reward); disputes win → `Rejected` (stake slashed to verifiers). `clearHazard` when the road is clear again.
- Events: `HazardReported`, `HazardVerified`, `HazardResolved`, `HazardCleared` — robots subscribe to these as the **live shared map**.
- Python side: web3.py bindings; one signer key per operator (throwaway testnet keys); keep gas use small.

### 10.4 Never-fake policy
- No fallback decision path. If the brain is unavailable → `SAFE_STOP`.
- `MOCK_BRAIN=1` only for local physics dev, loud banner, excluded from the demo script and from any recorded video.

### 10.5 Realistic perception
PyBullet renders look like toy boxes. At hazard checkpoints, composite a **real photo** of the hazard type (accident / road works / waterlogging) into or alongside the rendered frame and send that to the vision model. Disclose this in the README. Photos: provided by me or CC-licensed (e.g. Wikimedia Commons) with `assets/hazard_photos/ATTRIBUTION.md`.

---

## 11. Viewer, live demo link & replay

`viewer/` (Vite + Three.js):
- Loads `buildings.glb`, roads, POIs; renders cars/autos/pedestrians (instanced), robots (operator colours), drones, hazard props.
- **Live decision panel** (the most important UI): each 0G decision as a card — robot, operator, checkpoint, frame thumbnail, verdict, confidence, **0G request id**, `tee_verified`, **Storage root hash** (link to storagescan), **tx hash** (link to chainscan).
- Operator dashboards: deliveries, reports made/confirmed/rejected, stake balance.
- Customer order page (§9).
- **Replay mode:** load a recorded run (state stream + episode index) and scrub the timeline. Build a static, hostable version (Vercel/Netlify/GitHub Pages) = the **live demo link**.
- Attribution footer: © OpenStreetMap contributors; photo credits.

---

## 12. Demo scenario (deterministic, seeded)

`sim/scenario.py`, scheduled in sim time:
1. t=0: city running; orders flowing for all 3 operators.
2. t≈30 s: **accident** appears on a main road → MinuteMart rover hits CP-H1 → 0G confirms → `reportHazard` tx → drone of ZipKart verifies (CP-V) → `Confirmed`.
3. Rovers of all 3 operators hit CP-H2 on that road → reroute live.
4. t≈90 s: **waterlogging** on a lane → CP-H1 says impassable for rovers, passable for drones → (stretch) drone hand-off.
5. t≈150 s: **DashKirana posts a fake "road works" report** on a rival's route → verifier disputes (CP-V) → `Rejected`, stake slashed; rivals keep their route.
6. t≈200 s: operator toggles "0G unreachable" (firewall/invalid endpoint) → robots enter **SAFE_STOP**, pull over, nothing guessed → restore → resume.
7. Show replay of one robot's episode loaded from its 0G Storage root.

Also `scripts/test_safestop.py` proving safe stop with 0G unreachable.

---

## 13. Repo structure

```
hazard-commons/
  CLAUDE.md                # generate from this spec (short, operational)
  docs/SPEC.md             # this file
  .env.example  config/settings.yaml
  world/       fetch_osm.py build_meshes.py build_graph.py pois.py out/
  traffic/     build_net.sh demand.sh sumo.sumocfg bridge.py
  sim/         engine.py clock.py robot.py drone.py controllers/ sensors/ fsm.py scenario.py
  brain/       zg_router.py checkpoints.py prompts/ schemas.py safestop.py
  memory/      zg_storage.py episode_log.py
  chain/       foundry/ (src/HazardRegistry.sol, test/, script/Deploy.s.sol) bindings.py
  ops/         operators.py dispatcher.py orders.py
  server/      app.py (FastAPI + WebSocket)
  viewer/      (Vite + Three.js)
  assets/      hazard_photos/ ATTRIBUTION.md
  scripts/     run_demo.sh test_safestop.py record_run.py
  tests/
```

---

## 14. Shared interfaces (freeze these in Workstream 0)

Define as pydantic models in `sim/schemas.py` (and TS types for the viewer):
- `ActorState{id, kind: car|auto|bike|bus|pedestrian, x, y, z, yaw, speed}`
- `RobotState{id, operator, kind: rover|drone, x, y, z, yaw, speed, fsm_state, order_id?, route_edge_ids[], battery}`
- `Hazard{id, edge_id, type, severity, status: reported|confirmed|rejected|cleared, reporter_operator, evidence_root, zg_request_id, tx_hash}`
- `CheckpointRequest{robot_id, checkpoint, frame_png_b64?, context}` / `CheckpointResult{ok, output, zg_request_id, zg_trace, tee_verified, latency_ms, error?}`
- `EpisodeRecord` (§10.2)
- `Order{id, operator, customer_building_id, items, status, robot_id?, timestamps}`
- WebSocket topics: `state` (10 Hz snapshot), `decision`, `hazard`, `order`, `chain_tx`
- World artifact paths + local frame definition (origin, EPSG, netOffset)
- Contract ABI + addresses in `chain/deployments/galileo.json`

---

## 15. Workstreams (for parallel subagents)

| WS | Scope | Depends on | Can stub with |
|---|---|---|---|
| **WS0 Foundations** (lead, first) | Repo, config, schemas (§14), sim clock, event bus, FastAPI skeleton, `.env.example`, CLAUDE.md | — | — |
| WS1 World builder | §6 pipeline, outputs in `world/out/` | WS0 | — |
| WS2 Traffic | §7 SUMO net from same OSM, demand, TraCI bridge | WS0, WS1 (bbox/OSM file) | a tiny test network |
| WS3 Bodies & driving | §8 PyBullet rovers + drones, controllers, lidar, camera, FSM, safe stop | WS0, WS1 | a flat test map |
| WS4 0G brain & memory | §10.1, 10.2, 10.4, 10.5 Router client, prompts, schemas, Storage upload, episode log | WS0 | `MOCK_BRAIN` (dev only) |
| WS5 Chain | §10.3 Foundry contract + tests, deploy to Galileo, Python bindings, event subscriber | WS0 | local anvil |
| WS6 Ops & orders | §9 operators, dispatcher, orders API, auto-demand | WS0, WS1 (POIs) | fake POIs |
| WS7 Viewer | §11 Three.js app, decision panel, order page, replay, hosted build | WS0, WS1 (glb) | dummy state stream |
| WS8 Scenario & demo | §12 scenario, test_safestop, recorder, README, submission assets | all | — |

Integration order: WS1 → (WS2, WS3) → first end-to-end loop with WS4 + WS5 → WS6 → WS7 polish → WS8.

---

## 16. Timeline (IST) & gates

| Window | Milestone / gate |
|---|---|
| Fri 02:00–03:30 | Plan mode Q&A (§17). WS0 done. **Spikes:** one real 0G Router call returns a request id; one 0G Storage upload returns a root hash; SUMO + PyBullet installed and import |
| 03:30–09:00 | **Gate 1:** Mumbai bbox built; one rover drives dark store → home on real roads in PyBullet; SUMO traffic running; viewer shows city + robot |
| 09:00–14:00 | **Gate 2:** one full loop — hazard → CP-H1 on 0G → episode record uploaded → `reportHazard` tx → rival CP-H2 → reroute visible in viewer |
| 14:00–20:00 | **Gate 3:** 3 operators, 6–10 rovers, drone verification, fake report slashed, safe stop, orders flowing |
| 20:00–Sat 02:00 | Polish decision panel, replay from 0G Storage roots, hosted live link, README. **Feature freeze 02:00** |
| Sat 02:00–08:00 | Record video (2:30–2:50, voice-over), screenshots/logo/banner, final README, submit by 08:00 |

Plan ~3–4 h of sleep for me; schedule long unattended runs (world build, deploy, recording runs) around it, and tell me before I sleep what needs my input.

---

## 17. Questions you must ask me (plan mode)

1. 0G Router **API key** and whether credits are on testnet or mainnet; which **vision** and **text** model IDs I have access to.
2. **Wallet private keys** for Galileo testnet (one deployer + one per operator) and whether they hold faucet tokens; whether I can get extra tokens/credits from the 0G booth.
3. Confirm the **map area** (default Bandra West bbox in §6) or give another.
4. **Operator names** and colours (default: ZipKart, MinuteMart, DashKirana).
5. **Hazard photos**: will I provide them, or may you fetch CC-licensed ones and write attributions?
6. Hosting for the **live demo link** (Vercel / Netlify / GitHub Pages) and the **GitHub repo** name (public).
7. Permission to install system packages (SUMO via apt/pip `eclipse-sumo`, Foundry, Node/pnpm) and Python version to use.
8. Python Storage path: community `0g-storage-sdk` vs official Go CLI/TS SDK — and whether to build the Go client.
9. Exact definition of the **request id** 0G judges expect (I'll ask at the 0G booth if docs are unclear).
10. Anything else that blocks you — ask, don't guess.

---

## 18. Risks & fallbacks

| Risk | Mitigation / fallback |
|---|---|
| Vision model unavailable on testnet Router | Ask me; use mainnet Router with booth credits; keep model id in config |
| Storage download latency (3–5 min) | Never on the real-time path; Chain events are the live map; replay uses local mirror + root hashes |
| SUMO ↔ PyBullet sync bugs | Robots on sidewalks only (no `moveToXY`); background actors kinematic only |
| CPU too slow | Shrink bbox, fewer actors, lower physics rate, render camera only at checkpoints, run sim faster-than-real-time for recording |
| Faucet limits | Tiny stakes, batch uploads, ask booth |
| OSM gaps in Mumbai (missing heights/addresses) | Height fallbacks (§6), synthetic display addresses |
| Running out of time | Cut in this order: drone hand-off → CP-D → OrderReceipt → Storage KV → 3rd operator |

---

## 19. Submission checklist (ETHGlobal portal + 0G)

- Name ≤ 20 chars, tagline ≤ 100 chars, logo (square), banner, **≥ 3 screenshots**.
- Long description: problem, architecture, how 0G Compute/Storage/Chain are used (with request ids, root hashes, tx links), **how AI tools were used** (Claude Code).
- Public repo, README with setup + run instructions, architecture diagram, contract addresses, sample request ids / root hashes / tx links, `test_safestop.py`.
- Demo video 2:30–2:50 with clear voice-over (no music-only), ≥ 720p.
- Live demo link (hosted viewer/replay).
- Team name + Telegram + X.
- Select prize: 0G — Physical AI: Agents with a Body.

---

## 20. Demo video script (~2:45)

| Time | Shot |
|---|---|
| 0:00 | Hook over 3D Bandra: "Robots from rival delivery companies. One shared truth about Mumbai's streets." |
| 0:15 | Architecture: PyBullet bodies (spinal cord) · 0G Compute brain · 0G Storage memory · 0G Chain rules |
| 0:30 | Customer places an order; MinuteMart rover leaves the dark store |
| 0:45 | Accident ahead → CP-H1 card: frame, verdict, request id, TEE ✓ → report tx |
| 1:05 | ZipKart drone verifies from above → Confirmed on-chain → rovers of all 3 operators reroute |
| 1:30 | Waterlogging: rover can't pass, drone can (decision depends on body type) |
| 1:50 | DashKirana posts a fake report → disputed → stake slashed |
| 2:10 | 0G unreachable → every robot pulls over; nothing guessed → restored → resume |
| 2:25 | Replay a robot's episode from its 0G Storage root hash |
| 2:40 | Close: "Unplug 0G and the city goes blind." |
