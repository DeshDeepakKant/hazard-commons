"""Thread-safe pub/sub bus. The sim thread publishes; asyncio WebSocket clients and the
recorder subscribe. Every message is an Envelope {topic, t, data}.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from sim.schemas import Envelope, Topic


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._async_subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue, set[str] | None]] = []
        self._sync_subs: list[tuple[Callable[[Envelope], None], set[str] | None]] = []
        self.last: dict[str, Envelope] = {}          # latest message per topic (for late joiners)
        # Event history (everything except high-rate `state`) so a late-joining viewer sees the whole story.
        self.history: deque[Envelope] = deque(maxlen=5000)

    def publish(self, topic: Topic, t: float, data: BaseModel | dict[str, Any]) -> Envelope:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        env = Envelope(topic=topic, t=t, data=payload)
        with self._lock:
            self.last[topic] = env
            if topic != "state":
                self.history.append(env)
            asubs = list(self._async_subs)
            ssubs = list(self._sync_subs)
        for fn, topics in ssubs:
            if topics is None or topic in topics:
                try:
                    fn(env)
                except Exception as e:  # never let a subscriber kill the sim
                    print(f"[bus] sync subscriber error: {e!r}")
        for loop, q, topics in asubs:
            if topics is None or topic in topics:
                try:
                    loop.call_soon_threadsafe(_put_drop_oldest, q, env)
                except RuntimeError:
                    pass  # loop closed
        return env

    def subscribe_with_history(self, maxsize: int = 8192) -> tuple[list[Envelope], asyncio.Queue]:
        """Atomically: snapshot (all past events in order, then the latest state) + live subscription,
        so a late joiner neither misses nor duplicates an event."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            snap = list(self.history)
            if "state" in self.last:
                snap.append(self.last["state"])
            self._async_subs.append((asyncio.get_running_loop(), q, None))
        return snap, q

    def subscribe_sync(self, fn: Callable[[Envelope], None], topics: set[str] | None = None) -> None:
        with self._lock:
            self._sync_subs.append((fn, topics))

    def subscribe_async(self, topics: set[str] | None = None, maxsize: int = 512) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._async_subs.append((asyncio.get_running_loop(), q, topics))
        return q

    def unsubscribe_async(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._async_subs = [s for s in self._async_subs if s[1] is not q]


def _put_drop_oldest(q: asyncio.Queue, item: Any) -> None:
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(item)


class Recorder:
    """Writes every envelope to a JSONL file: the replayable run (state stream + events)."""

    def __init__(self, bus: Bus, out_path: Path, state_every: int = 1) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(out_path, "w")
        self._lock = threading.Lock()
        self._n_state = 0
        self._state_every = state_every
        bus.subscribe_sync(self._on)

    def _on(self, env: Envelope) -> None:
        if env.topic == "state":
            self._n_state += 1
            if self._n_state % self._state_every:
                return
        with self._lock:
            self._f.write(json.dumps(env.model_dump(mode="json"), separators=(",", ":")) + "\n")

    def close(self) -> None:
        with self._lock:
            self._f.close()


BUS = Bus()
