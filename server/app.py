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
