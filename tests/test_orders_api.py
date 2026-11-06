"""Order API: priority, note, cancel. Fake engine around a real OrderBook (no sim, no network)."""
from fastapi.testclient import TestClient

from ops.orders import OrderBook
from server import app as appmod


class FakeEngine:
    def __init__(self):
        self.t = 5.0
        self.book = OrderBook(seed=1)
        self.home = next(iter(self.book.homes))

    def place_order(self, operator, building_id, items, customer_name, priority="normal", note=""):
        o = self.book.place(operator, self.home, items, customer_name, self.t, priority=priority, note=note)
        return {**o.model_dump(mode="json"), **self.book.meta[o.id]}

    def cancel_order(self, order_id):
        return self.book.cancel(order_id, self.t).model_dump(mode="json")

    def set_brain_unreachable(self, on): ...
    def snapshot_meta(self): return {}


def _client():
    eng = FakeEngine()
    appmod.attach_engine(eng)
    return TestClient(appmod.app), eng


def test_place_with_priority_and_cancel():
    c, _ = _client()
    r = c.post("/api/orders", json={"operator": "ZipKart", "building_id": "x", "items": ["Bread"],
                                    "priority": "express", "note": "ring twice"})
    assert r.status_code == 200
    j = r.json()
    assert j["priority"] == "express" and j["note"] == "ring twice"
    r = c.post(f"/api/orders/{j['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert "cancelled" in r.json()["timestamps"]
    assert c.post(f"/api/orders/{j['id']}/cancel").status_code == 409   # already closed


def test_cancel_assigned_refused_and_unknown_404():
    c, eng = _client()
    j = c.post("/api/orders", json={"operator": "ZipKart", "building_id": "x"}).json()
    eng.book.set_status(j["id"], "assigned", 6.0, robot_id="R-01")
    assert c.post(f"/api/orders/{j['id']}/cancel").status_code == 409
    assert c.post("/api/orders/O-9999/cancel").status_code == 404


def test_bad_operator_is_400():
    c, _ = _client()
    r = c.post("/api/orders", json={"operator": "Nope", "building_id": "x"})
    assert r.status_code == 400


def test_express_dispatched_first():
    _, eng = _client()
    a = eng.book.place("ZipKart", eng.home, [], "", 1.0)
    b = eng.book.place("ZipKart", eng.home, [], "", 2.0, priority="express")
    assert [o.id for o in eng.book.pending("ZipKart")] == [b.id, a.id]
