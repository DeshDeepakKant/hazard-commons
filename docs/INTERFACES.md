# Frozen interfaces (WS0)

Code is the source of truth: `sim/schemas.py`, `brain/schemas.py`, `common/interfaces.py`, `common/frame.py`, `viewer/src/types.ts`.
This file pins the **file formats** that cross workstreams.

## Local frame
Metres, x = east, y = north, z = up, origin = bbox centre projected to EPSG:32643. See `common/frame.py`.
The viewer maps `three = (x, z, -y)`. `buildings.glb` is exported already in glTF Y-up (vertex = `(x, height, -y)`).

## world/out/ (WS1 writes, everyone reads)
- `meta.json`: `{"epsg":32643, "origin_lonlat":[lon,lat], "origin_utm":[E,N], "bbox_wsen":[w,s,e,n], "sumo_net_offset":[ox,oy]|null, "attribution":"© OpenStreetMap contributors"}`. `origin_utm` MUST equal the EPSG:32643 projection of `settings.world.origin_lonlat` (not recomputed from data). `sumo_net_offset` stays null here: WS2 writes `traffic/out/net_meta.json` `{"sumo_net_offset":[ox,oy]}` and `common/frame.meta()` merges it.
- Raw OSM input (shared, git-ignored): `world/cache/map.osm` (5 MB Overpass extract of the bbox, fetched 2026-11-06). **Single input for both WS1 and WS2 (netconvert).** Scripts take `--osm PATH` defaulting to `world/cache/map.osm`.
- `buildings.glb`: extruded footprints, one mesh per building; node name = building id; vertex colours by use.
- `buildings.json`: `[{"id":"b123", "osm_id":..., "use":"residential|commercial|other", "height":m, "height_source":"osm_height|levels|pseudo", "centroid":[x,y], "footprint":[[x,y],...], "name":str|null, "address":str|null}]`
- `collision/boxes.json`: `[{"id":"b123", "center":[x,y,z], "half_extents":[hx,hy,hz], "yaw":rad}]`: oriented boxes for PyBullet (buildings within 30 m of roads).
- `road_graph.json`:
  ```json
  {"nodes":[{"id":"n1","x":0,"y":0}],
   "edges":[{"id":"e1","from":"n1","to":"n2","length":42.1,"lanes":2,"name":"Hill Road",
             "kind":"drive","oneway":false,"shape":[[x,y],...],"sumo_edge_id":"-12345#0"|null}]}
  ```
  Edge ids are stable strings. Rovers route on `kind=="drive"` edges (drive on the LEFT edge of the carriageway; offset = −lane_width·lanes/2 to the left of travel direction).
- `pois.json`:
  ```json
  {"dark_stores":[{"id":"DS-ZipKart","operator":"ZipKart","building_id":"b9","name":"ZipKart Dark Store","x":0,"y":0,"entrance_node":"n7","entrance_xy":[x,y]}],
   "homes":[{"id":"H-01","building_id":"b12","address":"...","customer_name":"...","x":0,"y":0,"entrance_node":"n3","entrance_xy":[x,y],"height":18.0}]}
  ```
- `roads.json` (viewer): `[{"id":"e1","kind":"drive|walk","width":m,"shape":[[x,y],...]}]`

## traffic/ (WS2)
- `traffic/bridge.py` exposes `class TrafficBridge(start(seed), step(sim_time) -> list[ActorState], close_edge(edge_id), reopen_edge(edge_id), upsert_robot(robot_id, x, y, yaw))` using **libsumo** (same API as TraCI, in-process).
- Coordinates returned are already in the local frame.

## Bus envelopes (WebSocket `/ws`)
`{"topic": "state|decision|hazard|order|chain_tx|storage|log", "t": sim_time, "data": {...}}`. `data` is one of `StateSnapshot`, `DecisionEvent`, `Hazard`, `Order`, `ChainTxEvent`, `StorageEvent`.
A recorded run = `logs/runs/<run_id>/stream.jsonl` (the same envelopes) + `logs/index.json`. Replay mode plays this file.

## Checkpoint context keys
`CheckpointRequest.context` keys that start with `_` are **sim-internal** (for example `_mock_truth`) and MUST be stripped by the real brain before anything is sent to 0G.

## Chain
`chain/deployments/galileo.json`: `{"chainId":16602, "HazardRegistry": {"address":"0x..","tx":"0x..","block":n}, "abi_path":"chain/abi/HazardRegistry.json"}`
Hazard types on-chain: `uint8` 0=none, 1=accident, 2=road_works, 3=waterlogging. Severity: `uint8` 0–100.
`edgeId` on-chain: `bytes32` = keccak256(edge id string). `zgRequestId`: `string`. `evidenceRoot`: `bytes32`.

## 0G Storage sidecar
`memory/zg-storage/` (Node). CLI: `node upload.mjs <file>` → stdout one JSON line `{"root_hash":"0x..","tx_hash":"0x.."}`; exit≠0 on failure. `node download.mjs <root> <out>`.
