"""Тесты сервиса заказов: фейковый FunPay (get_sales) + реальное хранилище во временной БД (без сети)."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.models import Found, Listing, LotStatus, MatchResult, Order, OrderStatus, OurLot
from app.notify import TelegramNotifier
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.services.orders import OrdersService, _normalize, _parse_status
from app.settings import Settings, TelegramSettings
from app.storage import Storage


class FakeFunPay:
    """Клиент FunPay, отдающий заранее заданные продажи."""

    def __init__(self, sales: list[dict] | None = None, exc: Exception | None = None):
        self.sales = sales or []
        self.exc = exc
        self.calls: list[dict] = []

    def get_sales(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return [dict(s) for s in self.sales]


class RecordingNotifier(TelegramNotifier):
    """Настоящий нотификатор с MockTransport: считаем отправленные сообщения."""

    def __init__(self, settings: TelegramSettings):
        self.requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        super().__init__(lambda: settings, transport=httpx.MockTransport(handler))
        self.min_interval = 0


def _sale(
    order_id: str, status: str = "paid", title: str = "WoT | Chieftain, Об. 279 | RU", price: float = 29000, **extra
) -> dict:
    d = {
        "order_id": order_id,
        "status": status,
        "title": title,
        "subcategory_name": "Аккаунты",
        "price": price,
        "currency": "RUB",
        "buyer_name": "buyer1",
        "buyer_id": "55",
        "buyer_url": "https://funpay.com/users/55/",
        "order_url": f"https://funpay.com/orders/{order_id}/",
        "date": datetime.now(UTC),  # заказ после создания лота (старые заказы к новым лотам не привязываются)
    }
    d.update(extra)
    return d


def _make_ctx(monkeypatch, golden_key: str = "test-key") -> AppContext:
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    settings = Settings()
    settings.funpay.golden_key = golden_key
    return AppContext(settings=settings, storage=Storage(tmp / "db.sqlite"), profiles=ProfileStore(tmp / "profiles"))


def _add_lot(
    ctx: AppContext,
    title: str,
    price: float,
    status: LotStatus = LotStatus.ACTIVE,
    source_url: str = "https://lzt.market/123",
    source_price: float = 15000,
    i: str = "1",
) -> OurLot:
    found, _ = ctx.storage.upsert_found(
        Found(
            profile_id="wot",
            listing=Listing(source="lolz", source_id=i + title, url=source_url, title=title, price=source_price),
            match=MatchResult(matched=True, score=2),
        )
    )
    return ctx.storage.save_lot(
        OurLot(
            found_id=found.id,
            profile_id="wot",
            funpay_lot_id=777,
            funpay_url="https://funpay.com/lots/offer?id=777",
            subcategory_id=148,
            title_ru=title,
            price=price,
            source_price=source_price,
            source_url=source_url,
            status=status,
        )
    )


@pytest.fixture()
def env(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    lot = _add_lot(ctx, "WoT | Chieftain, Об. 279 | RU", 29000)
    funpay = FakeFunPay([_sale("ABCDEFGH"), _sale("ZZZZZZZZ", status="closed", title="Другое", price=100)])
    ctx._funpay = funpay
    notifier = RecordingNotifier(TelegramSettings(enabled=True, bot_token="1:A", chat_id="1"))
    svc = OrdersService(ctx, notifier)
    return ctx, lot, funpay, notifier, svc


# ------------------------------------------------------------------ sync
def test_sync_new_orders_match_lot_and_notify(env):
    ctx, lot, funpay, notifier, svc = env
    res = svc.sync()
    assert res["errors"] == []
    assert res == {"new": 2, "updated": 0, "matched": 1, "errors": []}
    assert funpay.calls == [{"include_paid": True, "include_closed": True, "include_refunded": True, "max_pages": 1}]

    # лот продан
    assert ctx.storage.get_lot(lot.id).status == LotStatus.SOLD
    # заказ получил ссылку на исходник и лот
    orders = {o.funpay_order_id: o for o in ctx.storage.list_orders()}
    a, z = orders["ABCDEFGH"], orders["ZZZZZZZZ"]
    assert a.lot_id == lot.id and a.source_url == "https://lzt.market/123" and a.source_price == 15000
    assert a.status == OrderStatus.PAID and a.notified is True
    assert a.order_date is not None and a.order_date.tzinfo is not None
    assert a.buyer_name == "buyer1" and a.buyer_url == "https://funpay.com/users/55/"
    assert z.lot_id is None and z.source_url is None and z.status == OrderStatus.CLOSED and z.notified is False

    # уведомление — ровно одно (только о новом оплаченном заказе)
    assert len(notifier.requests) == 1
    text = notifier.requests[0]["text"]
    assert "Новый заказ на FunPay #ABCDEFGH" in text and "lzt.market/123" in text and "маржа 14 000 ₽" in text

    # журнал
    kinds = [e for e in ctx.storage.events() if e["kind"] == "orders"]
    assert any("продан" in e["message"] for e in kinds) and any("синхронизация заказов" in e["message"] for e in kinds)
    assert svc.last_sync and svc.last_result["new"] == 2
    assert svc.status()["interval_minutes"] == ctx.settings.monitor.orders_check_minutes


def test_resync_is_idempotent_and_status_change_updates(env):
    ctx, lot, funpay, notifier, svc = env
    svc.sync()
    assert len(notifier.requests) == 1

    res = svc.sync()
    assert res == {"new": 0, "updated": 0, "matched": 1, "errors": []}
    assert len(notifier.requests) == 1
    assert ctx.storage.get_lot(lot.id).status == LotStatus.SOLD

    funpay.sales[0] = _sale("ABCDEFGH", status="refunded")
    res = svc.sync()
    assert res["new"] == 0 and res["updated"] == 1
    a = next(o for o in ctx.storage.list_orders() if o.funpay_order_id == "ABCDEFGH")
    assert a.status == OrderStatus.REFUNDED and a.lot_id == lot.id
    assert len(notifier.requests) == 1
    # лот остаётся проданным — возврат не возвращает лот в продажу автоматически
    assert ctx.storage.get_lot(lot.id).status == LotStatus.SOLD
    assert any("paid → refunded" in e["message"] for e in ctx.storage.events())


def test_sync_without_golden_key(monkeypatch):
    ctx = _make_ctx(monkeypatch, golden_key="")
    funpay = FakeFunPay([_sale("A")])
    ctx._funpay = funpay
    svc = OrdersService(ctx, RecordingNotifier(TelegramSettings()))
    res = svc.sync()
    assert res["new"] == 0 and res["errors"] and "golden_key" in res["errors"][0]
    assert funpay.calls == []


def test_sync_get_sales_error_does_not_raise(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    ctx._funpay = FakeFunPay(exc=RuntimeError("сессия протухла"))
    svc = OrdersService(ctx, RecordingNotifier(TelegramSettings()))
    res = svc.sync()
    assert res["new"] == 0 and len(res["errors"]) == 1 and "сессия протухла" in res["errors"][0]
    assert any(e["level"] == "error" and e["kind"] == "orders" for e in ctx.storage.events())


def test_bad_order_is_skipped_others_processed(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    ctx._funpay = FakeFunPay([{"status": "paid", "title": "без id"}, _sale("OK1", title="x", price=5)])
    svc = OrdersService(ctx, RecordingNotifier(TelegramSettings()))
    res = svc.sync()
    assert res["new"] == 1 and len(res["errors"]) == 1
    assert [o.funpay_order_id for o in ctx.storage.list_orders()] == ["OK1"]


def test_notified_flag_when_telegram_disabled_and_retry_when_failed(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    ctx._funpay = FakeFunPay([_sale("A1", title="x", price=5)])
    # Telegram выключен — заказ считается обработанным, повторов нет
    svc = OrdersService(ctx, RecordingNotifier(TelegramSettings(enabled=False)))
    svc.sync()
    assert ctx.storage.list_orders()[0].notified is True

    # Telegram включён, но отправка падает — notified остаётся False и при следующей синхронизации повторяем
    ctx2 = _make_ctx(monkeypatch)
    ctx2._funpay = FakeFunPay([_sale("A2", title="x", price=5)])
    settings = TelegramSettings(enabled=True, bot_token="1:A", chat_id="1")
    failing = RecordingNotifier(settings)
    failing.send = lambda *a, **k: False  # type: ignore[method-assign]
    svc2 = OrdersService(ctx2, failing)
    svc2.sync()
    assert ctx2.storage.list_orders()[0].notified is False
    ok = RecordingNotifier(settings)
    svc3 = OrdersService(ctx2, ok)
    svc3.sync()
    assert len(ok.requests) == 1 and ctx2.storage.list_orders()[0].notified is True
    svc3.sync()
    assert len(ok.requests) == 1


# ------------------------------------------------------------- matching
def test_match_lot_strategies(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    active = _add_lot(ctx, "Dota 2 | Immortal | EU", 5000, i="a")
    deactivated = _add_lot(ctx, "Dota 2 | Immortal | EU", 5000, status=LotStatus.DEACTIVATED, i="b")
    sold = _add_lot(ctx, "CS2 | Prime | RU", 7000, status=LotStatus.SOLD, i="c")
    unique_price = _add_lot(ctx, "WoT | Топы | RU", 12345, i="d")
    _add_lot(ctx, "Fortnite | 100 skins", 9999, i="e")
    _add_lot(ctx, "Fortnite | 200 skins", 9999, i="f")
    lots = ctx.storage.list_lots(limit=1000)

    # точное совпадение, предпочитаем активный лот
    assert (
        OrdersService.match_lot(Order(funpay_order_id="1", title="Dota 2 | Immortal | EU", price=1), lots).id
        == active.id
    )
    # нормализованное (регистр/пробелы) — у проданного лота название совпадает только так
    assert (
        OrdersService.match_lot(Order(funpay_order_id="2", title="  cs2 |  prime | ru ", price=1), lots).id == sold.id
    )
    # по уникальной цене среди активных
    assert (
        OrdersService.match_lot(Order(funpay_order_id="3", title="что-то другое", price=12345), lots).id
        == unique_price.id
    )
    # цена неуникальна — не сопоставляем
    assert OrdersService.match_lot(Order(funpay_order_id="4", title="другое", price=9999), lots) is None
    # цена совпадает только с неактивным лотом — не сопоставляем
    assert OrdersService.match_lot(Order(funpay_order_id="5", title="другое", price=7000), lots) is None
    assert OrdersService.match_lot(Order(funpay_order_id="6", title="", price=0), lots) is None
    assert OrdersService.match_lot(Order(funpay_order_id="7", title="x", price=1), []) is None
    assert deactivated.id != active.id


def test_helpers():
    assert _normalize("  Dota  2 | IMMORTAL ") == "dota 2 | immortal"
    assert _parse_status("paid") == OrderStatus.PAID
    assert _parse_status("Refunded") == OrderStatus.REFUNDED
    assert _parse_status("Возврат") == OrderStatus.REFUNDED
    assert _parse_status("closed") == OrderStatus.CLOSED
    assert _parse_status("Закрыт") == OrderStatus.CLOSED
    assert _parse_status("что-то") == OrderStatus.PAID
    assert _parse_status(OrderStatus.CLOSED) == OrderStatus.CLOSED
    o = OrdersService.to_order(
        {"order_id": "X1", "status": "paid", "price": "12.5", "date": "2026-10-01T10:00:00+00:00"}
    )
    assert o.price == 12.5 and o.order_url == "https://funpay.com/orders/X1/" and o.order_date.year == 2026
    with pytest.raises(ValueError):
        OrdersService.to_order({"status": "paid"})


# ---------------------------------------------------------------- query
def test_list_and_set_note(env):
    ctx, lot, _funpay, _notifier, svc = env
    svc.sync()
    items = svc.list()
    assert len(items) == 2 and {i["funpay_order_id"] for i in items} == {"ABCDEFGH", "ZZZZZZZZ"}
    a = next(i for i in items if i["funpay_order_id"] == "ABCDEFGH")
    assert a["lot"]["id"] == lot.id and a["lot"]["status"] == "sold" and a["status"] == "paid"
    z = next(i for i in items if i["funpay_order_id"] == "ZZZZZZZZ")
    assert z["lot"] is None
    assert [i["funpay_order_id"] for i in svc.list(status="closed")] == ["ZZZZZZZZ"]
    assert [i["funpay_order_id"] for i in svc.list(status=[OrderStatus.PAID])] == ["ABCDEFGH"]
    assert [i["funpay_order_id"] for i in svc.list(status="paid,closed", limit=1)] and len(svc.list(limit=1)) == 1

    out = svc.set_note(a["id"], "исходник куплен, данные переданы")
    assert out["note"] == "исходник куплен, данные переданы"
    assert ctx.storage.get_order(a["id"]).note == "исходник куплен, данные переданы"
    assert svc.set_note(999999, "x") is None
    assert svc.get(a["id"])["lot"]["id"] == lot.id and svc.get(999999) is None
