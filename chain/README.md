# chain/: HazardRegistry on 0G Galileo

Shared, staked hazard map for rival operators (ZipKart, MinuteMart, DashKirana).
Contract: `foundry/src/HazardRegistry.sol` (solc 0.8.24, `evm_version = cancun`).
Python: `bindings.py` (`HazardChain`, implements `common.interfaces.Chain`), `deploy.py`.

## Install
```bash
cd chain/foundry
~/.foundry/bin/forge install foundry-rs/forge-std --no-git   # lib/ is git-ignored; only needed for `forge test`
```
Run `forge install` from `chain/foundry` (it has `foundry.toml`). Run it from the repo root and it lands in `./lib`.

## Test
```bash
(cd chain/foundry && ~/.foundry/bin/forge test --gas-report)       # 16 Solidity tests
PYTHONPATH=. .venv/bin/pytest tests/test_chain.py -q             # spawns its own anvil, full scenario via HazardChain
```

## Deploy
```bash
# local proof: start anvil, then (funds the .env keys with anvil_setBalance)
~/.foundry/bin/anvil &
PYTHONPATH=. .venv/bin/python -m chain.deploy --network anvil     # -> chain/deployments/anvil.json
# Galileo (deployer needs >= ~0.0125 0G; refuses and sends nothing if it has less)
chain/deploy_galileo.sh                                           # -> chain/deployments/galileo.json
```
Keys are read from the env or `.env` (`HAZARD_ENV_FILE=/path/.env` or `--env-file`), and nothing prints them.
Deploy writes `chain/abi/HazardRegistry.json` (ABI array) and `chain/deployments/<net>.json` in the
INTERFACES.md format, plus `operators`, `config` and `gas_used`. Constructor values come from
`config/settings.yaml` `chain:`: minStake = `report_stake_wei` (0.0001 0G), confirms/disputes
needed = 1/1, and rewardPerConfirm = stake/10, paid only while the reward pool (`fundRewards()`) has funds.

Operators pay gas for their own txs, so fund each operator wallet at https://faucet.0g.ai.

## Contract
| call | who | notes |
|---|---|---|
| `registerOperator(signer, name)` | owner | |
| `registerRobots(bytes32[] ids, uint8[] kinds)` | operator | 0=rover, 1=drone; the bindings register lazily on first use |
| `reportHazard(robotId, edgeId, edgeName, type, severity, passableMask, evidenceRoot, zgRequestId)` payable | operator, own robot | `msg.value >= minStake` → hazardId (1-based) |
| `verifyHazard(hazardId, robotId, confirm, evidenceRoot, zgRequestId)` | other operator's robot | one vote per operator. Confirms ≥ N → Confirmed (refund + reward). Disputes ≥ M → Rejected (stake split among disputers) |
| `clearHazard(hazardId, evidenceRoot, zgRequestId)` | any operator | only Confirmed → Cleared |
| `recordDelivery(orderId, robotId, episodeRoot)` | operator, own robot | event only |
| `setConfig`, `fundRewards`, `transferOwnership`, `withdraw` | owner / anyone / payee | payouts that fail to push are credited for `withdraw()` |

Events (`HazardReported` has everything needed to rebuild the map): `OperatorRegistered`, `RobotsRegistered`,
`HazardReported(id↑, edgeId↑, operator↑, robotId, edgeName, type, severity, passableMask, stake, evidenceRoot, zgRequestId)`,
`HazardVerified(id↑, operator↑, robotId↑, confirm, confirms, disputes, evidenceRoot, zgRequestId)`,
`HazardResolved(id↑, status 2|3, reporter↑, payout)`, `HazardCleared(id↑, operator↑, evidenceRoot, zgRequestId)`,
`DeliveryRecorded(orderId↑, operator↑, robotId↑, episodeRoot)`.
Custom errors: `NotOperator, NotYourRobot, RobotTaken, StakeTooLow, BadInput, UnknownHazard, WrongStatus, SameOperator, AlreadyVoted, ...`

## Python bindings
```python
from chain.bindings import HazardChain
hc = HazardChain.from_deployment("galileo", on_tx=lambda ev: bus.publish("chain_tx", ev), sim_time_fn=clock.now)
f = hc.report_hazard("MinuteMart", "R-04", edge_id, "accident", 0.8, storage_root, zg_request_id,
                     passable_for=["drone"])          # Future
...
if f.done(): r = f.result()   # {"tx_hash","hazard_id","status":"mined"|"failed","gas_used","block","error","resolved"}
hc.verify_hazard("ZipKart", "D-01", r["hazard_id"], True, root, req_id)
hc.clear_hazard(op, hazard_id, evidence_root=None, zg_request_id=None)
hc.record_delivery(op, order_id, robot_id, episode_root)
hc.register_robots(op, ["R-01", ("D-01", "drone")])   # optional, saves the lazy-registration tx later
hc.start_polling(2.0)          # optional background poller; then poll_events() never touches the network
events = hc.poll_events()      # [{"event","block","tx_hash","log_index","args":{...bytes as 0x-hex}}]
hazards = hc.hazards()         # list[sim.schemas.Hazard] rebuilt from events only
```
- Futures never raise. Reverts are caught at `estimate_gas`, cost no gas, and return `status="failed"` with `error="SameOperator"` and similar.
- Each operator has its own single-thread executor (serialised nonces). Different operators run in parallel.
- `severity`: a float ≤ 1 is scaled ×100, otherwise it is taken as 0–100. `Hazard.severity` comes back as 0–1.
- `passable_for` defaults to `["drone"]`. `Hazard.edge_id` = the edge id string (`edgeName` in the event).
- `reporter_robot` is reverse-mapped from keccak for R-01..R-30, D-01..D-10 and any robot used through this client. Otherwise it is the 0x hash.
- `on_tx(ChainTxEvent)` fires `sent` then `mined` or `failed` (from the executor thread). The `recordDelivery` kind is `"other"`.
- Env: `ZG_RPC_URL` (galileo) / `ANVIL_RPC_URL` (anvil), `OPERATOR_<NAME>_PRIVATE_KEY`.

## Gas (measured on anvil, same bytecode; Galileo gasPrice ≈ 4 gwei)
| call | gas | ≈ 0G at 4 gwei |
|---|---|---|
| deploy | 2,066,308 (Galileo `estimateGas`: 2,101,443) | 0.0084 |
| registerOperator | 77–94k | 0.0003–0.0004 |
| registerRobots (1 / 2 / 4 robots) | 51.9k / 76.5k / 125.6k | 0.0002–0.0005 |
| reportHazard (+ 0.0001 stake) | 174k | 0.0007 |
| verifyHazard → Confirmed (refund + reward) | 92.8k | 0.0004 |
| verifyHazard → Rejected (slash to 1 disputer) | 128k | 0.0005 |
| clearHazard | 36.3k | 0.00015 |
| recordDelivery | 29.6k | 0.00012 |

Deployer total ≈ 0.0095 0G, but the script needs ~0.0125 in the wallet because the gas limit has 25% headroom.
The demo scenario (per operator: register robots + ~2 reports or votes) costs < 0.003 0G per operator.
