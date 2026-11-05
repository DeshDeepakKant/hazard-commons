from ops.dispatcher import Dispatcher
from ops.orders import OrderBook


class FakeRover:
    def __init__(self, id, operator, x, y, idle=True):
        self.id, self.operator, self._xy, self._idle, self.got = id, operator, (x, y), idle, None

    def is_idle(self): return self._idle
    def xy(self): return self._xy
    def assign(self, order, store, home):
        self.got, self._idle = order.id, False


def test_order_lifecycle_and_stats():
    ev = []
    book = OrderBook(on_change=ev.append, seed=1)
    o = book.place("ZipKart", "H-01", [], "", 0.0)
    assert o.status == "placed" and o.items and o.customer_name
    book.set_status(o.id, "delivered", 99.0, robot_id="R-01")
    assert book.stats()["ZipKart"]["delivered"] == 1 and len(ev) == 2


def test_dispatch_nearest_idle_per_operator():
    book = OrderBook(seed=1)
    d = Dispatcher(book)
    store = book.stores["MinuteMart"]
    near = FakeRover("R-04", "MinuteMart", store["x"] + 5, store["y"])
    far = FakeRover("R-05", "MinuteMart", store["x"] + 400, store["y"])
    other = FakeRover("R-01", "ZipKart", store["x"], store["y"])
    o = book.place("MinuteMart", "H-02", [], "", 1.0)
    made = d.tick([far, near, other], 2.0)
    assert made == [(o.id, "R-04")] and other.got is None
    assert book.get(o.id).status == "assigned" and book.get(o.id).robot_id == "R-04"


def test_auto_demand_is_seeded():
    a, b = OrderBook(seed=5), OrderBook(seed=5)
    for i in range(2000):
        a.auto_demand(i * 0.05, 6, 0.05); b.auto_demand(i * 0.05, 6, 0.05)
    assert [(o.operator, o.customer_building_id) for o in a.all()] == \
           [(o.operator, o.customer_building_id) for o in b.all()]
    assert len(a.all()) > 3
