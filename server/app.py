"""FastAPI + WebSocket server. Streams bus envelopes to the viewer and exposes control APIs.

Run: .venv/bin/uvicorn server.app:app --port 8765
The sim engine is attached via `attach_engine()` (done by sim/engine.py / scripts/run_demo.sh).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional, Protocol

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from common.config import path
from server.bus import BUS


class EngineControl(Protocol):
    def place_order(self, operator: str, building_id: str, items: list[str],
                    customer_name: str) -> dict[str, Any]: ...
    def set_brain_unreachable(self, on: bool) -> None: ...
    def set_speed(self, x: float) -> dict[str, Any]: ...
    def speed(self) -> dict[str, Any]: ...
    def snapshot_meta(self) -> dict[str, Any]: ...   # operators, pois summary, run info
    # --- "Add a disaster" (appended)
    def inject_hazard(self, type: str, edge_id: Optional[str] = None, x: Optional[float] = None,
                      y: Optional[float] = None, real: bool = True,
                      severity: Optional[float] = None) -> dict[str, Any]: ...
    def inject_random(self, real: bool = True, type: Optional[str] = None) -> dict[str, Any]: ...
    def clear_injected(self, prop_id: str) -> dict[str, Any]: ...
    def list_props(self) -> list[dict[str, Any]]: ...


_engine: Optional[EngineControl] = None


def attach_engine(engine: EngineControl) -> None:
    global _engine
    _engine = engine


app = FastAPI(title="Hazard Commons")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class OrderIn(BaseModel):
    operator: str
    building_id: str
    items: list[str] = []
    customer_name: str = "Customer"
    priority: str = "normal"      # "normal" | "express"
    note: str = ""


class ToggleIn(BaseModel):
    unreachable: bool


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "engine": _engine is not None}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    if _engine is None:
        return {"engine": False}
    return _engine.snapshot_meta()


@app.post("/api/orders")
def place_order(o: OrderIn) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    try:
        if o.priority != "normal" or o.note:
            return _engine.place_order(o.operator, o.building_id, o.items, o.customer_name,
                                       priority=o.priority, note=o.note)  # type: ignore[call-arg]
        return _engine.place_order(o.operator, o.building_id, o.items, o.customer_name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/brain/unreachable")
def brain_unreachable(t: ToggleIn) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    _engine.set_brain_unreachable(t.unreachable)
    return {"ok": True, "unreachable": t.unreachable}


class SpeedIn(BaseModel):
    speed: float


@app.get("/api/speed")
def get_speed() -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    return _engine.speed()


@app.post("/api/speed")
def set_speed(s: SpeedIn) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    if not 0.1 <= s.speed <= 100:
        raise HTTPException(400, "speed must be between 0.1 and 100")
    return _engine.set_speed(s.speed)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    snap, q = BUS.subscribe_with_history()
    try:
        # late joiner: full event history first, then the latest state
        for env in snap:
            await websocket.send_text(json.dumps(env.model_dump(mode="json")))
        while True:
            env = await q.get()
            await websocket.send_text(json.dumps(env.model_dump(mode="json")))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        BUS.unsubscribe_async(q)


# ---------------------------------------------------------------- "Add a disaster" (inject) API
class HazardIn(BaseModel):
    type: str
    edge_id: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    real: bool = True
    severity: Optional[float] = None


class RandomHazardIn(BaseModel):
    real: bool = True
    type: Optional[str] = None


_HAZARD_TYPES = ("accident", "road_works", "waterlogging")


def _inject_call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except KeyError as e:
        raise HTTPException(404, f"unknown injected hazard {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except TimeoutError:
        raise HTTPException(503, "sim loop did not apply the request in time")


@app.post("/api/hazards")
def inject_hazard(h: HazardIn) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    if h.type not in _HAZARD_TYPES:
        raise HTTPException(422, f"type must be one of {list(_HAZARD_TYPES)}")
    if h.edge_id is None and (h.x is None or h.y is None):
        raise HTTPException(422, "give edge_id or both x and y")
    if h.severity is not None and not 0.0 <= h.severity <= 1.0:
        raise HTTPException(422, "severity must be within 0..1")
    return _inject_call(_engine.inject_hazard, h.type, h.edge_id, h.x, h.y, h.real, h.severity)


@app.post("/api/hazards/random")
def inject_random_hazard(h: Optional[RandomHazardIn] = None) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    h = h or RandomHazardIn()
    if h.type is not None and h.type not in _HAZARD_TYPES:
        raise HTTPException(422, f"type must be one of {list(_HAZARD_TYPES)}")
    return _inject_call(_engine.inject_random, h.real, h.type)


@app.get("/api/hazards/props")
def list_hazard_props() -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    return {"props": _engine.list_props()}


@app.delete("/api/hazards/{prop_id}")
def clear_injected_hazard(prop_id: str) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(503, "sim engine not running")
    return _inject_call(_engine.clear_injected, prop_id)


# world artifacts (glb, road graph, pois) for the dev viewer
_world_out = path("world/out")
_world_out.mkdir(parents=True, exist_ok=True)
app.mount("/world", StaticFiles(directory=str(_world_out)), name="world")


# ======================================================================================
# BEGIN orders v2 (cancel): keep this block together
# ======================================================================================
@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict[str, Any]:
    if _engine is None or not hasattr(_engine, "cancel_order"):
        raise HTTPException(503, "sim engine not running")
    try:
        return _engine.cancel_order(order_id)  # type: ignore[attr-defined]
    except KeyError:
        raise HTTPException(404, f"unknown order {order_id}")
    except ValueError as e:
        raise HTTPException(409, str(e))
# END orders v2
