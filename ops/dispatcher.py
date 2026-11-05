"""Per-operator dispatcher: assign the nearest IDLE rover to the oldest pending order.

Robots are accessed through the small `RoverLike` protocol so this is independent of the
PyBullet implementation (sim/robot.py). The engine supplies an adapter.
"""
from __future__ import annotations

import math
from typing import Protocol

from ops.orders import OrderBook
from sim.schemas import Order


class RoverLike(Protocol):
    id: str
    operator: str

    def is_idle(self) -> bool: ...
    def xy(self) -> tuple[float, float]: ...
    def assign(self, order: Order, store: dict, home: dict) -> None: ...


class Dispatcher:
    def __init__(self, book: OrderBook):
        self.book = book

    def tick(self, rovers: list[RoverLike], sim_time: float) -> list[tuple[str, str]]:
        """Returns [(order_id, robot_id)] assignments made this tick."""
        made: list[tuple[str, str]] = []
        for operator, store in self.book.stores.items():
            idle = [r for r in rovers if r.operator == operator and r.is_idle()]
            if not idle:
                continue
            for order in self.book.pending(operator):
                if not idle:
                    break
                sx, sy = store["x"], store["y"]
                r = min(idle, key=lambda r: math.hypot(r.xy()[0] - sx, r.xy()[1] - sy))
                idle.remove(r)
                home = self.book.homes[order.customer_building_id]
                self.book.set_status(order.id, "assigned", sim_time, robot_id=r.id)
                r.assign(order, store, home)
                made.append((order.id, r.id))
        return made
