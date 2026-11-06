# 0G evidence (final recorded run `demo-galileo-v3`, 2026-11-07)

Everything below comes from one real run with `DEMO=1`: real 0G Router decisions, the Galileo chain, and 0G Storage uploads. No mock brain, no local chain.

## 0G Chain (Galileo, chain id 16602)

- HazardRegistry: [`0x2098b78B804a9506f7A9074FBd35C76906421Ba0`](https://chainscan-galileo.0g.ai/address/0x2098b78B804a9506f7A9074FBd35C76906421Ba0)
- Deploy tx: [`0xf8f0b22fd4fe93559510b2a583c3fd686a10fcfd524fab0bc84f4264c9f2b3c7`](https://chainscan-galileo.0g.ai/tx/0xf8f0b22fd4fe93559510b2a583c3fd686a10fcfd524fab0bc84f4264c9f2b3c7)

### Transactions during the run (mined)

| sim t | kind | operator | hazard | tx |
|---|---|---|---|---|
| 48s | reportHazard | MinuteMart | 92 | [`0x8de8cb0c6c0e…`](https://chainscan-galileo.0g.ai/tx/0x8de8cb0c6c0e99b735b5d2ee5744b8b1a54812ed70b1d8a09c8a156ede8cabdb) |
| 95s | verifyHazard | ZipKart | 92 | [`0x9c39c6c6a145…`](https://chainscan-galileo.0g.ai/tx/0x9c39c6c6a145e953041d536e0c2411f8a7d9e445099467ca0f6920420d1da62c) |
| 115s | reportHazard | ZipKart | 93 | [`0x12ca90e275cd…`](https://chainscan-galileo.0g.ai/tx/0x12ca90e275cd6c232d71344514ac2d35347f560c1003c1f23903e6aacf531779) |
| 157s | reportHazard | DashKirana | 94 | [`0x45d4d3660612…`](https://chainscan-galileo.0g.ai/tx/0x45d4d3660612f58ad7ca71cf537b840c13d080671c400f160b219e1bb0ed605f) |
| 187s | verifyHazard | ZipKart | 94 | [`0x6e4444729a8d…`](https://chainscan-galileo.0g.ai/tx/0x6e4444729a8dbc77396d68a7f549feedeb7d2b1be6e2b894d880f86379789100) |
| 189s | verifyHazard | MinuteMart | 93 | [`0x730e7f3b0cfb…`](https://chainscan-galileo.0g.ai/tx/0x730e7f3b0cfb44cd0b2a24d674e02e93103e01bea9acc9a2dac07e50ef199fcb) |
| 232s | reportHazard | ZipKart | 95 | [`0xa577cd7fd9a3…`](https://chainscan-galileo.0g.ai/tx/0xa577cd7fd9a3cfa59884f1aa3c74e461b33e101d68e72ee6b1bafeaf495b604d) |
| 237s | reportHazard | MinuteMart | 96 | [`0x873ffddb9d2e…`](https://chainscan-galileo.0g.ai/tx/0x873ffddb9d2e6c3072d860e0586629ec12a8777e0e3d7ef7884b37ddb9c0710e) |
| 242s | clearHazard | MinuteMart | 92 | [`0x4f43917cfebd…`](https://chainscan-galileo.0g.ai/tx/0x4f43917cfebd774b9b9042e393ad5b50169fb252216e996400482aff1b7cbf66) |
| 257s | verifyHazard | MinuteMart | 95 | [`0x50655bde000f…`](https://chainscan-galileo.0g.ai/tx/0x50655bde000f3c9a1dbe8cb9d533052b737efe2aa0df1375489aad0f5bfa7163) |

### Hazards (final status)

| id | type | reporter | status | confirms/disputes |
|---|---|---|---|---|
| 92 | accident | MinuteMart | cleared | 1/0 |
| 93 | waterlogging | ZipKart | confirmed | 1/0 |
| 94 | road_works | DashKirana | rejected | 0/1 |
| 95 | road_works | ZipKart | confirmed | 1/0 |
| 96 | road_works | MinuteMart | reported | 0/0 |

## 0G Compute decisions (Router, model `0gm-1.0-35b-a3b`, provider-sealed)

| sim t | robot | checkpoint | result | 0G request id (`x_0g_trace.request_id`) |
|---|---|---|---|---|
| 40s | R-06 | CP-H1 | accident | `2f60ed60-2630-4989-a976-2df611da51e1` |
| 87s | D-01 | CP-V | confirm | `59857e37-71e5-467c-b6b0-3b3e9d90cadf` |
| 100s | R-07 | CP-H2 | reroute | `9a971383-f513-4f2f-ae1a-4f6dfb0e9b6f` |
| 100s | R-01 | CP-H2 | reroute | `4e03f86f-95cf-473a-9960-c9074c9c8d04` |
| 101s | R-04 | CP-H2 | reroute | `58286ecf-27b7-4f50-8341-c550fd34bba9` |
| 108s | R-03 | CP-H1 | waterlogging | `d669b48b-e385-417a-936a-ec5bbb8cf37a` |
| 182s | D-01 | CP-V | dispute | `58133155-88e5-4c86-83fe-35f803519736` |
| 182s | D-02 | CP-V | confirm | `d4d68efc-9235-4392-aa76-72bb201cab1a` |
| 210s | R-01 | CP-H1 | SAFE_STOP: 0G unreachable: cannot reach http://127.0.0.1:9/v1: Connecti | `—` |
| 214s | R-05 | CP-H1 | SAFE_STOP: 0G unreachable: cannot reach http://127.0.0.1:9/v1: Connecti | `—` |
| 226s | R-01 | CP-H1 | road_works | `13db1467-6c33-4b27-adb6-516ad45639cf` |
| 230s | R-05 | CP-H1 | road_works | `b076e2b6-1f97-462a-8964-607ec7133e2e` |
| 252s | D-02 | CP-V | confirm | `620294bd-b87c-462d-b771-07d386fe65c9` |
| 259s | R-04 | CP-H1 | road_works | `8a5eb1c7-dfff-4c48-abd9-9dda898707f5` |
| 259s | R-06 | CP-H2 | reroute | `9c1b56df-8097-478c-86b1-859404aa59d8` |
| 260s | R-07 | CP-H2 | reroute | `76ac5eb5-8122-4e41-99d7-948af75a6438` |

`tee_verified` is not reported by the Router for sealed (end-to-end encrypted) requests, so it is shown as “–”, not as verified. 2 decision(s) failed and became SAFE_STOP (during the scripted outage the Router is pointed at a dead address); none was guessed.

## 0G Storage (hash-chained episode batches, one record per decision)

| robot | batch | records | root hash |
|---|---|---|---|
| D-01 | batch-0.json | 2 | `0x7308156cac0c6db018b6d83640b338b2748e559e785f4ef9cae8f6414dccc152` |
| D-02 | batch-0.json | 2 | `0xe6b21fef0899fec057d6c048c6fd77f3ccfb870454d9e8cb1ba9fa6c45bf7934` |
| R-01 | batch-0.json | 3 | `0x354902a8ec5f035d86dab51e4829ef517cafe5cb2430d03ce26136425eb714e6` |
| R-03 | batch-0.json | 1 | `0xeb6e8ea09f92ed4926130a9a487e56a4014aadc5c4a7f24bd3526c263712212a` |
| R-04 | batch-0.json | 2 | `0x7bcc5425711aa227d6411ce0f36a12e1113e6f646a5d43f7ab7cd0de7ca18143` |
| R-05 | batch-0.json | 2 | `0x6db04bbf0183d8713ef65cf8956d85baa2913ddb279e2e1892b090e7370b7a9a` |
| R-06 | batch-0.json | 2 | `0x3c6a72f7cac02c4870a66457bf99a7ac390b773cbfdae188fe1a64d07274c11b` |
| R-07 | batch-0.json | 2 | `0xe48aa681cd331a05a82f005aaa71ee5402e3b3ee700e23864cbac89048173f24` |

Explorer: https://storagescan-galileo.0g.ai . Download: `node memory/zg-storage/download.mjs <root> <out>` (propagation can take minutes). The hosted viewer's **Verify log** button fetches each root from the 0G indexer and re-checks the hash chain in the browser.

## Replay
The hosted replay is this exact run (state frames thinned 4:1): https://hazard-commons.vercel.app/ . Raw stream: `viewer/public/runs/demo.jsonl`.

## Earlier probes
- Router spike request id `e3deff71-90f3-4022-858d-f483bbf32fab` (provider `0xf4FB8d2bD856e672b5f9848b0eD235A20079B250`).
- Storage probe root `0x3526e7fde02914bd22b2735c7b6148911aac1a6b5d016d37d7125fefe5a05791`.

## Why the UI shows "TEE: –" (researched 2026-11-06)
- `router-api.0g.ai` is 0G's E2EE gateway. It always seals `0gm-1.0-35b-a3b` (`x-0g-e2ee: sealed`), whatever trust mode is requested ([mainnet.env](https://github.com/0gfoundation/0g-pc-e2ee/blob/main/deploy/phala/env/mainnet.env)).
- For sealed requests the router deliberately omits `x_0g_trace.tee_verified`: it would be checking a different signing scheme. Authentication is done by the attested gateway's bind check ([async-e2ee-plan.md](https://github.com/0gfoundation/0g-pc-e2ee/blob/main/docs/design/async-e2ee-plan.md), [trust-chain.md](https://github.com/0gfoundation/0g-pc-e2ee/blob/main/docs/design/trust-chain.md)).
- The deployed gateway release (release-2026.11.05.1) sets `ZG_GATEWAY_VERIFY_RESPONSES=true`. Our reading of that config, not a documented guarantee: a sealed 200 response has had its TEE signature checked against the on-chain signer.
- So we show "–" (not reported) rather than claiming "verified". The gateway itself can be attested with `GET https://router-api.0g.ai/evidence?nonce=…` ([verifying-the-gateway.md](https://github.com/0gfoundation/0g-pc-e2ee/blob/main/docs/verifying-the-gateway.md)).

## Router outage on 2026-11-06 (from ~12:29 IST)
- Every chat call returned 502 `fetch provider quote … compute-network-20.integratenetwork.work … EOF` (e.g. request id `1dc9750ac183ce18`).
- The only provider for `0gm-1.0-35b-a3b` (`0xf4FB…B250`) was listed unhealthy. Its host dropped the TLS handshake.
- The only healthy TeeML chat model, `glm-5.3`, is text-only, so it can't do the vision checkpoints. We did not switch models.
- Robots safe-stopped as designed. No decision was faked.
