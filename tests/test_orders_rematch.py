"""Регрессия: старый заказ не должен «продавать» новый лот с таким же названием при повторной синхронизации."""
import tempfile
from pathlib import Path

from app.models import LotStatus, OurLot
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.services.orders import OrdersService
from app.settings import Settings
from app.storage import Storage


class _FakeFunPay:
    def __init__(self, sales):
        self.sales = sales

    def get_sales(self, **kw):
        return list(self.sales)


class _FakeNotifier:
    enabled = False

    def allowed(self, kind):
        return False

    def send(self, *a, **k):
        return False

    def format_order(self, *a, **k):
        return ""


def test_old_order_does_not_claim_new_lot(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    settings = Settings(); settings.funpay.golden_key = "k"
    ctx = AppContext(settings=settings, storage=Storage(tmp / "db"), profiles=ProfileStore(tmp / "p"))
    lot_a = ctx.storage.save_lot(OurLot(found_id=1, profile_id="p", title_ru="WoT | Chieftain | RU", price=29000,
                                        source_price=15000, source_url="https://funpay.com/lots/offer?id=1",
                                        status=LotStatus.ACTIVE, funpay_lot_id=11, subcategory_id=148))
    order = {"order_id": "AAA", "status": "paid", "title": "WoT | Chieftain | RU", "subcategory_name": None,
             "price": 29000.0, "currency": "RUB", "buyer_name": "b", "buyer_id": "1", "buyer_url": None,
             "order_url": "https://funpay.com/orders/AAA/", "date": None}
    ctx._funpay = _FakeFunPay([order])
    svc = OrdersService(ctx, _FakeNotifier())
    r1 = svc.sync()
    assert r1["new"] == 1 and ctx.storage.get_lot(lot_a.id).status == LotStatus.SOLD

    lot_b = ctx.storage.save_lot(OurLot(found_id=2, profile_id="p", title_ru="WoT | Chieftain | RU", price=31000,
                                        source_price=16000, source_url="https://funpay.com/lots/offer?id=2",
                                        status=LotStatus.ACTIVE, funpay_lot_id=12, subcategory_id=148))
    r2 = svc.sync()
    assert r2["new"] == 0
    assert ctx.storage.get_lot(lot_b.id).status == LotStatus.ACTIVE, "новый лот не должен быть помечен проданным"
    orders = ctx.storage.list_orders()
    assert len(orders) == 1 and orders[0].lot_id == lot_a.id
