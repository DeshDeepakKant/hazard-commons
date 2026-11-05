# Handover: remaining work (written 2026-11-06 ~03:30 IST)

Read first: `CLAUDE.md`, `docs/SPEC.md`, `docs/INTERFACES.md`. Deadline: portal locks **Sat 2026-11-07 09:00 IST**, submit by 08:00. Feature freeze 02:00 Sat.

## Hard rules (unchanged)
- Never fake a 0G decision. Any 0G failure -> robot SAFE_STOP. `MOCK_BRAIN` is dev-only and refused when `DEMO=1`.
- Never print or commit keys. `.env` is git-ignored. Testnet only, tiny stakes.
- Don't edit frozen interfaces (`sim/schemas.py`, `brain/schemas.py`, `common/*`, `viewer/src/types.ts`) without noting it in docs/INTERFACES.md.
- Commit often with meaningful messages. Python: `.venv/bin/python`, tests: `.venv/bin/pytest`. Foundry: `~/.foundry/bin`.

## Done and merged on `main` (61 tests pass)
WS0 foundations, WS1 world (`world/out/*`), WS2 traffic (`traffic/bridge.py`, `TrafficBridge`), WS4 brain+memory (`brain/client.py get_brain`, `brain/perception.py`, `memory/episode_log.py`, `memory/zg_storage.py`, Node sidecar `memory/zg-storage`), WS5 chain (`chain/foundry`, `chain/bindings.py HazardChain`, `chain/deploy_galileo.sh`), WS6 ops (`ops/orders.py`, `ops/dispatcher.py`; `Dispatcher` talks to robots via the `RoverLike` protocol), road-graph -> SUMO edge map (`sumo_edge_ids` in road_graph.json).

## Still running / NOT merged (check `git worktree list`)
- **WS3 bodies/driving** (`sim/`: BodyWorld, rover, drone, FSM, planner, lidar, safe stop): branch `worktree-agent-a49dc13cbe91e5c2e`.
- **WS7 viewer** (`viewer/`): branch `worktree-agent-a8dbd813e8918d740`.
Both are locked worktrees under `.claude/worktrees/`, still being worked by Claude subagents. Merge each into `main` once its branch has a final commit and its tests pass. Don't edit inside those worktrees.

## Remaining tasks (in order)
1. Merge WS3 and WS7 (above). Run the full test suite.
2. **`sim/engine.py`**: main fixed-step loop wiring BodyWorld + TrafficBridge + 3 operators (3/3/2 rovers per settings, drones D-01/D-02) + OrderBook/Dispatcher + brain (`get_brain()`) + EpisodeLog/ZGStorage + HazardChain + bus. Checkpoint triggers CP-H1 (hazard prop in range), CP-H2 (next edge has an active chain report), CP-V (drone or rival rover verifies). Apply decisions: reroute via planner blocked edges, wait, handoff. Chain write after CP-H1 (report) and CP-V (verify). Publish `state` 10 Hz, `decision`, `hazard`, `order`, `chain_tx`, `storage` envelopes; attach to `server/app.py` (`attach_engine`: `place_order`, `set_brain_unreachable`, `snapshot_meta`). Close/reopen SUMO edges for confirmed hazards.
3. **`sim/scenario.py`** per SPEC §12 (accident ~30 s, waterlogging ~90 s, DashKirana fake road-works report ~150 s -> disputed/slashed, 0G outage ~200 s -> SAFE_STOP, restore -> resume). Seeded, deterministic.
4. `scripts/test_safestop.py` (Router base URL pointed at a dead host -> every robot SAFE_STOP, zero decisions recorded), `scripts/record_run.py` (writes `logs/runs/<id>/stream.jsonl` + `logs/index.json`), `scripts/run_demo.sh` (sets `DEMO=1`).
5. Once the user supplies a **Router API key** in `.env` (`ZG_ROUTER_API_KEY`): `.venv/bin/python -m brain.spike --call` for a real call (request id, tee_verified). Once the **deployer is funded** (~0.0125 0G; operators ~0.003 each): `chain/deploy_galileo.sh --env-file .env`, then a real Storage upload through the sidecar. Do not proceed with placeholders; ask the user.
6. Record a replayable run, copy world files + stream into `viewer/public`, `npm run build`, deploy viewer to Vercel (live demo link).
7. README (setup, architecture diagram, contract addresses, sample request ids/root hashes/tx links, how AI tools were used, photo + OSM attribution, disclose composited hazard photos), push to public `github.com/DeshDeepakKant/hazard-commons` (check no secrets staged first).
8. Submission assets: name <=20 chars, tagline <=100, logo, banner, >=3 screenshots, demo video script (SPEC §20), team Telegram/X from the user.

## Known issues from workstream reports
- Only one TEE provider for `0gm-1.0-35b-a3b`: 503s are plausible -> SAFE_STOP; retry calmly.
- Log frames ~400 KB each; consider storing hashes only if Storage fees are high.
- `enable_thinking=false` passed to the router is unconfirmed.
- Traffic demand drains after 2 h sim time; libsumo allows one bridge per process.
- Hazards with no vote never expire on-chain.
