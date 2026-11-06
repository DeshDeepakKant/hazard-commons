# 0G evidence (final recorded run `demo-galileo-final`, 2026-11-06)

Everything below comes from one real run with `DEMO=1`: real 0G Router decisions, the Galileo chain, and 0G Storage uploads. No mock brain, no local chain.

## 0G Chain (Galileo, chain id 16602)

- HazardRegistry: [`0x2098b78B804a9506f7A9074FBd35C76906421Ba0`](https://chainscan-galileo.0g.ai/address/0x2098b78B804a9506f7A9074FBd35C76906421Ba0)
- Deploy tx: [`0xf8f0b22fd4fe93559510b2a583c3fd686a10fcfd524fab0bc84f4264c9f2b3c7`](https://chainscan-galileo.0g.ai/tx/0xf8f0b22fd4fe93559510b2a583c3fd686a10fcfd524fab0bc84f4264c9f2b3c7)

### Transactions during the run (mined)

| sim t | kind | operator | tx |
|---|---|---|---|
| 54s | registerRobot | MinuteMart | [`0x79e089cc4d08…`](https://chainscan-galileo.0g.ai/tx/0x79e089cc4d08e69214603fa0b3f70eb081a94fb79f7e5c7fdc81b009ac6c5a7b) |
| 61s | reportHazard | MinuteMart | [`0x77009dbf73fb…`](https://chainscan-galileo.0g.ai/tx/0x77009dbf73fb4608b9a38d0fde1347a63957076f028dd221c3256029923469a5) |
| 91s | registerRobot | ZipKart | [`0xebb37bf65a76…`](https://chainscan-galileo.0g.ai/tx/0xebb37bf65a76fb99551cef39a0982fca00f69f97538e87ec0a9c468d59abb154) |
| 94s | verifyHazard | ZipKart | [`0x2fcd24960658…`](https://chainscan-galileo.0g.ai/tx/0x2fcd24960658e637abb3c11c5caa62caca7800212033d1c5cf53b14248378726) |
| 109s | registerRobot | ZipKart | [`0x2135708c7f77…`](https://chainscan-galileo.0g.ai/tx/0x2135708c7f77d15e8386299184a527818a0ae62b21003f2106c4ef117a92f975) |
| 113s | reportHazard | ZipKart | [`0x5dfe208a95f4…`](https://chainscan-galileo.0g.ai/tx/0x5dfe208a95f43b81e270a7ce9d08e041d6ffb51041056925f595f0c3273f2bc7) |
| 154s | registerRobot | DashKirana | [`0x75ea4528c3f6…`](https://chainscan-galileo.0g.ai/tx/0x75ea4528c3f66d740035310ff535cbfe28ce2e49eb86a3719cac7ec414e5b7e0) |
| 157s | reportHazard | DashKirana | [`0xe76ec52264be…`](https://chainscan-galileo.0g.ai/tx/0xe76ec52264be06c288a9448ec13c146e84639707e11ba929f28f240480fcf8a1) |
| 180s | registerRobot | MinuteMart | [`0xdaf5e3d5b07e…`](https://chainscan-galileo.0g.ai/tx/0xdaf5e3d5b07e247ead8918251b4ba1b3b59eb6fb307b820e218db3ed7f233b92) |
| 184s | verifyHazard | MinuteMart | [`0x4b68cf8b395a…`](https://chainscan-galileo.0g.ai/tx/0x4b68cf8b395a6caca67a47bde8b0e1fff260534f7eb46a82f03b23b344f5746d) |
| 200s | verifyHazard | ZipKart | [`0x29220481c3dc…`](https://chainscan-galileo.0g.ai/tx/0x29220481c3dcf6d451990e4b87bbe6056b38dc023b99573243f84733da765489) |
| 239s | registerRobot | MinuteMart | [`0xe7d5d3e7bf34…`](https://chainscan-galileo.0g.ai/tx/0xe7d5d3e7bf34991f53bb576d1c5443b30924a693f69d8cd520eb9a327556031e) |

### Hazards (final status)

| id | type | reporter | status | confirms/disputes |
|---|---|---|---|---|
| 1 | accident | MinuteMart | confirmed | 1/0 |
| 2 | waterlogging | ZipKart | confirmed | 1/0 |
| 3 | road_works | DashKirana | rejected | 0/1 |

## 0G Compute decisions (Router, model `0gm-1.0-35b-a3b`, provider-sealed)

| sim t | robot | checkpoint | result | 0G request id (`x_0g_trace.request_id`) |
|---|---|---|---|---|
| 45s | R-06 | CP-H1 | accident | `dc77588d-c277-48e5-8564-d9381d3581f3` |
| 88s | D-01 | CP-V | confirm | `758f8ce2-6ded-4183-bb6c-ea4dde81b97b` |
| 95s | R-07 | CP-H2 | reroute | `920a2fbb-db6f-4c59-8b70-ecbf66bd0eab` |
| 96s | R-01 | CP-H2 | reroute | `0e757608-275d-4f8c-be8d-fedabc5a8c07` |
| 96s | R-04 | CP-H2 | reroute | `952e1858-7cae-4e1a-b1ce-13a781db13a4` |
| 106s | R-03 | CP-H1 | waterlogging | `d0a84bac-15e8-4242-9fe8-c79154da6275` |
| 176s | D-02 | CP-V | confirm | `94b62d01-6cc8-440a-bee8-49dab5a04216` |
| 183s | D-01 | CP-V | SAFE_STOP: 0G timeout: no response within 15.0s (Request ti | `—` |
| 196s | D-01 | CP-V | dispute | `0f4a49dd-073f-491b-ad14-77e32b4c573e` |
| 209s | R-01 | CP-H1 | SAFE_STOP: 0G unreachable: cannot reach http://127.0.0.1:9/ | `—` |
| 214s | R-05 | CP-H1 | SAFE_STOP: 0G unreachable: cannot reach http://127.0.0.1:9/ | `—` |
| 219s | R-01 | CP-H1 | SAFE_STOP: 0G unreachable: cannot reach http://127.0.0.1:9/ | `—` |
| 233s | R-05 | CP-H1 | road_works | `7e6b826c-506a-4e4f-9d75-5a43f45a49e1` |
| 237s | R-01 | CP-H1 | road_works | `9e4c3d01-f5a5-4083-b551-f28763bfe068` |

`tee_verified` is not reported by the Router for sealed (end-to-end encrypted) requests, so it is shown as “–”, not as verified. Four decisions failed on purpose or by chance and became SAFE_STOP: one real 15 s timeout, and three during the simulated outage (Router pointed at a dead address).

## 0G Storage (hash-chained episode batches, one record per decision)

| robot | records | root hash |
|---|---|---|
| D-01 | 3 | `0x407aae05b1c5373d648d826ba965ac227816f369533b13488bf31a51f69449cd` |
| D-02 | 1 | `0x665a160d9e7d22ea40fd1aa679a31aef45b4b63002b903876ec5a128ac78583b` |
| R-01 | 4 | `0x2fbcad5032f42f45dfbc6aa27726318553f061c1e35abe5174e1d59cce44c09a` |
| R-03 | 1 | `0xfcfd1b8170a42a4c26005a616564f72349a1f7b43f5723d3c2d65b90c0efe099` |
| R-04 | 1 | `0xa2d12ed45927d6b918699811231aeee16588b37ded855eefa266aa2472c7082e` |
| R-05 | 2 | `0x9efd2cf7d9516320eefbc391b4473985e76d1f200a3cb3523569255f9423b26e` |
| R-06 | 1 | `0x66f49e6a2880424f38b23cd91360cc6b382fae74fc731a62c53ca5add3889d51` |
| R-07 | 1 | `0x6970b8bddc2628ed99f7880d5a4b03a0895b0efcf9a46f0a9c4ebc6954c62ab9` |

Explorer: https://storagescan-galileo.0g.ai . Download: `node memory/zg-storage/download.mjs <root> <out>` (propagation can take minutes).

## Replay
The hosted replay is this exact run (state frames thinned 4:1): https://hazard-commons.vercel.app/ . Raw stream: `viewer/public/runs/demo.jsonl`.

## Earlier probes
- Router spike request id `e3deff71-90f3-4022-858d-f483bbf32fab` (provider `0xf4FB8d2bD856e672b5f9848b0eD235A20079B250`).
- Storage probe root `0x3526e7fde02914bd22b2735c7b6148911aac1a6b5d016d37d7125fefe5a05791`.
