"""Order book + auto-demand generator (SPEC §9). Pure bookkeeping; no physics."""
from __future__ import annotations

import itertools
import json
import random
import threading
from typing import Callable, Optional

from common.config import path, settings
from sim.schemas import Order, OrderStatus

CATALOGUE = ["Milk 1L", "Bread", "Eggs (6)", "Bananas", "Tomatoes 500g", "Paracetamol strip",
             "Instant noodles", "Chai masala", "Curd 400g", "Mineral water 1L", "Chips", "Ice cream"]


class OrderBook:
    def __init__(self, on_change: Optional[Callable[[Order], None]] = None, seed: int | None = None):
        self._orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._rng = random.Random(settings()["sim"]["seed"] if seed is None else seed)
        self._on_change = on_change or (lambda o: None)
        pois = json.loads(path("world/out/pois.json").read_text())
        self.homes = {h["id"]: h for h in pois["homes"]}
        self.stores = {s["operator"]: s for s in pois["dark_stores"]}

    # ------------------------------------------------------------------ create / update
    def place(self, operator: str, home_id: str, items: list[str], customer_name: str,
              sim_time: float) -> Order:
        if operator not in self.stores:
            raise ValueError(f"unknown operator {operator!r}")
        if home_id not in self.homes:
            raise ValueError(f"unknown home {home_id!r}")
        with self._lock:
            oid = f"O-{next(self._ids):04d}"
            o = Order(id=oid, operator=operator, customer_building_id=home_id,
                      customer_name=customer_name or self.homes[home_id]["customer_name"],
                      items=items or self._rng.sample(CATALOGUE, k=self._rng.randint(1, 4)),
                      status="placed", timestamps={"placed": sim_time})
            self._orders[oid] = o
        self._on_change(o)
        return o

    def set_status(self, order_id: str, status: OrderStatus, sim_time: float,
                   robot_id: str | None = None) -> Order:
        with self._lock:
            o = self._orders[order_id]
            o.status = status
            o.timestamps[status] = sim_time
            if robot_id:
                o.robot_id = robot_id
        self._on_change(o)
        return o

    # ------------------------------------------------------------------ queries
    def get(self, order_id: str) -> Order:
        return self._orders[order_id]

    def pending(self, operator: str) -> list[Order]:
        with self._lock:
            return sorted((o for o in self._orders.values()
                           if o.operator == operator and o.status == "placed"),
                          key=lambda o: o.timestamps["placed"])

    def all(self) -> list[Order]:
        return list(self._orders.values())

    def stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for o in self._orders.values():
            s = out.setdefault(o.operator, {"placed": 0, "delivered": 0, "in_progress": 0, "failed": 0})
            s["placed"] += 1
            if o.status == "delivered":
                s["delivered"] += 1
            elif o.status == "failed":
                s["failed"] += 1
            else:
                s["in_progress"] += 1
        return out

    # ------------------------------------------------------------------ background demand
    def auto_demand(self, sim_time: float, rate_per_min: float, dt: float) -> list[Order]:
        """Poisson-ish arrivals: call once per tick; returns newly placed orders."""
        placed = []
        if self._rng.random() < rate_per_min / 60.0 * dt:
            op = self._rng.choice(list(self.stores))
            home = self._rng.choice(list(self.homes))
            placed.append(self.place(op, home, [], "", sim_time))
        return placed
