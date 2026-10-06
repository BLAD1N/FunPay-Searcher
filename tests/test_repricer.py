"""Тесты переоценки: реальное хранилище во временной БД, профиль во временной папке, фейковый FunPay (без сети)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.models import Found, FoundStatus, Listing, LotStatus, MatchResult, OurLot, Profile
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.services.publisher import PublisherService
from app.services.repricer import RepricerService, format_reprice
from app.settings import Settings
from app.storage import Storage


class FakeFunPay:
    """Клиент FunPay: записывает вызовы update_lot, при необходимости падает."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.updates: list[dict] = []

    def update_lot(self, lot_id, subcategory_id, **kw):
        if self.exc:
            raise self.exc
        self.updates.append({"lot_id": lot_id, "subcategory_id": subcategory_id, **kw})


class FakeNotifier:
    """Нотификатор, запоминающий отправленные сообщения."""

    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, *, kind: str = "info", **kw) -> bool:
        self.sent.append((kind, text))
        return True

    def allowed(self, kind: str = "info") -> bool:
        return True


def _make_ctx(monkeypatch, auto_reprice: bool = True, min_change: float = 3.0) -> AppContext:
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    settings = Settings()
    settings.funpay.golden_key = "test-key"
    settings.monitor.auto_reprice = auto_reprice
    settings.monitor.reprice_min_change_percent = min_change
    store = ProfileStore(tmp / "profiles")
    store.save(
        Profile(
            id="wot",
            name="WoT",
            game="wot",
            region="RU",
            pricing={"mode": "formula", "formula": "price * 2 - 1000", "round_to": 100},
        )
    )
    ctx = AppContext(settings=settings, storage=Storage(tmp / "db.sqlite"), profiles=store)
    ctx._funpay = FakeFunPay()
    ctx._notifier = FakeNotifier()
    return ctx


def _found(
    ctx: AppContext,
    price: float,
    source_id: str = "1",
    status: FoundStatus = FoundStatus.CANDIDATE,
    suggested: float | None = None,
    profile_id: str = "wot",
) -> Found:
    """Создать/обновить найденное объявление (повторный вызов с той же source_id меняет цену исходника)."""
    found, _ = ctx.storage.upsert_found(
        Found(
            profile_id=profile_id,
            status=status,
            suggested_price=suggested,
            listing=Listing(
                source="lolz",
                source_id=source_id,
                url=f"https://lzt.market/{source_id}",
                title="WoT все топы",
                price=price,
            ),
            match=MatchResult(matched=True, score=2),
        )
    )
    return found


def _lot(
    ctx: AppContext,
    found: Found,
    price: float = 29000,
    source_price: float = 15000,
    status: LotStatus = LotStatus.ACTIVE,
    funpay_lot_id: int | None = 777,
    profile_id: str = "wot",
) -> OurLot:
    return ctx.storage.save_lot(
        OurLot(
            found_id=found.id,
            profile_id=profile_id,
            funpay_lot_id=funpay_lot_id,
            subcategory_id=148,
            funpay_url=f"https://funpay.com/lots/offer?id={funpay_lot_id}" if funpay_lot_id else None,
            title_ru="WoT | все топы | RU",
            price=price,
            source_price=source_price,
            source_url=found.listing.url,
            status=status,
        )
    )


@pytest.fixture()
def env(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    found = _found(ctx, 15000, status=FoundStatus.PUBLISHED, suggested=29000)
    lot = _lot(ctx, found)
    svc = RepricerService(ctx, PublisherService(ctx))
    return ctx, found, lot, svc


# ------------------------------------------------------------ auto mode
def test_active_lot_repriced_pushed_logged_notified(env):
    ctx, _, lot, svc = env
    _found(ctx, 13500, status=FoundStatus.PUBLISHED)  # исходник подешевел на 10%
    res = svc.run()
    assert res["errors"] == []
    assert res["checked"] == 1 and res["repriced"] == 1 and res["skipped"] == 0 and res["notified"] == 0

    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 26000 and saved.source_price == 13500 and saved.status == LotStatus.ACTIVE
    assert saved.error is None

    # отправлено на FunPay
    assert len(ctx._funpay.updates) == 1
    assert ctx._funpay.updates[0]["lot_id"] == 777 and ctx._funpay.updates[0]["price"] == 26000

    # журнал
    ev = [e for e in ctx.storage.events() if e["kind"] == "reprice"]
    assert any("13 500" in e["message"] and "26 000" in e["message"] and f"лот #{lot.id}" in e["message"] for e in ev)
    assert any(e["data"] and e["data"].get("new_price") == 26000 for e in ev)

    # уведомление
    assert len(ctx._notifier.sent) == 1
    kind, text = ctx._notifier.sent[0]
    assert kind == "price"
    assert "Лот переоценён" in text and "13 500" in text and "26 000" in text and "-10%" in text
    assert "https://lzt.market/1" in text and "funpay.com/lots/offer?id=777" in text

    # статус и повторный запуск — ничего не меняется
    st = svc.status()
    assert st["enabled"] is True and st["mode"] == "auto" and st["last_run"] and st["last_result"]["repriced"] == 1
    res2 = svc.run()
    assert res2["repriced"] == 0 and res2["skipped"] == 1 and len(ctx._funpay.updates) == 1
    assert len(ctx._notifier.sent) == 1


def test_change_below_threshold_is_skipped(env):
    ctx, _, lot, svc = env
    _found(ctx, 15300, status=FoundStatus.PUBLISHED)  # +2% < 3%
    res = svc.run()
    assert res == {"checked": 1, "repriced": 0, "notified": 0, "suggested_updated": 0, "skipped": 1, "errors": []}
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 29000 and saved.source_price == 15000
    assert ctx._funpay.updates == [] and ctx._notifier.sent == []
    assert not [e for e in ctx.storage.events() if e["kind"] == "reprice"]


def test_price_increase_above_threshold(env):
    ctx, _, lot, svc = env
    _found(ctx, 20000, status=FoundStatus.PUBLISHED)  # +33%
    res = svc.run()
    assert res["repriced"] == 1
    assert ctx.storage.get_lot(lot.id).price == 39000
    assert "📈" in ctx._notifier.sent[0][1] and "+33.3%" in ctx._notifier.sent[0][1]


def test_same_resulting_price_only_updates_source_price(monkeypatch):
    ctx = _make_ctx(monkeypatch, min_change=0.1)
    found = _found(ctx, 15000, status=FoundStatus.PUBLISHED)
    lot = _lot(ctx, found)
    svc = RepricerService(ctx, PublisherService(ctx))
    _found(ctx, 15020, status=FoundStatus.PUBLISHED)  # +0.13% >= 0.1%; 15020*2-1000=29040 -> 29000 (та же цена)
    res = svc.run()
    assert res["repriced"] == 0 and res["skipped"] == 1 and res["errors"] == []
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 29000 and saved.source_price == 15020
    assert ctx._funpay.updates == [] and ctx._notifier.sent == []


def test_draft_lot_repriced_without_funpay(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    found = _found(ctx, 15000)
    lot = _lot(ctx, found, status=LotStatus.DRAFT, funpay_lot_id=None)
    svc = RepricerService(ctx, PublisherService(ctx))
    _found(ctx, 13500)
    res = svc.run()
    assert res["repriced"] == 1 and res["errors"] == []
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 26000 and saved.source_price == 13500 and saved.status == LotStatus.DRAFT
    assert ctx._funpay.updates == []
    assert len(ctx._notifier.sent) == 1 and "Черновик" in ctx._notifier.sent[0][1]
    # кандидат с черновиком лота: рекомендуемая цена кандидата не трогается
    assert res["suggested_updated"] == 0


def test_deactivated_lot_repriced_silently(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    found = _found(ctx, 15000, status=FoundStatus.SOLD)
    lot = _lot(ctx, found, status=LotStatus.DEACTIVATED)
    svc = RepricerService(ctx, PublisherService(ctx))
    _found(ctx, 13500, status=FoundStatus.SOLD)
    res = svc.run()
    assert res["repriced"] == 1 and res["errors"] == []
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 26000 and saved.source_price == 13500 and saved.status == LotStatus.DEACTIVATED
    assert ctx._funpay.updates == [] and ctx._notifier.sent == []
    assert any("лот снят" in e["message"] for e in ctx.storage.events() if e["kind"] == "reprice")


def test_sold_and_error_lots_are_ignored(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    f1 = _found(ctx, 15000, source_id="a")
    f2 = _found(ctx, 15000, source_id="b")
    _lot(ctx, f1, status=LotStatus.SOLD)
    _lot(ctx, f2, status=LotStatus.ERROR)
    _found(ctx, 10000, source_id="a")
    _found(ctx, 10000, source_id="b")
    svc = RepricerService(ctx, PublisherService(ctx))
    res = svc.run()
    assert res["checked"] == 0 and res["repriced"] == 0
    assert {x.price for x in ctx.storage.list_lots()} == {29000}


def test_funpay_push_failure_is_retried_and_not_duplicated(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    found = _found(ctx, 15000, status=FoundStatus.PUBLISHED)
    lot = _lot(ctx, found)
    ctx._funpay = FakeFunPay(exc=RuntimeError("сессия протухла"))
    svc = RepricerService(ctx, PublisherService(ctx))
    _found(ctx, 13500, status=FoundStatus.PUBLISHED)

    res = svc.run()
    assert res["repriced"] == 0 and len(res["errors"]) == 1 and "сессия протухла" in res["errors"][0]
    saved = ctx.storage.get_lot(lot.id)
    # цена в базе возвращена к прежней (как на FunPay), source_price не обновлён — повторим позже, ошибка видна
    assert saved.price == 29000 and saved.source_price == 15000 and "сессия протухла" in saved.error
    assert ctx._notifier.sent == []
    assert any(
        e["level"] == "error" and "не удалось отправить" in e["message"]
        for e in ctx.storage.events()
        if e["kind"] == "reprice"
    )

    # FunPay ожил — повтор проходит, уведомление одно
    ctx._funpay = FakeFunPay()
    res = svc.run()
    assert res["repriced"] == 1 and res["errors"] == []
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 26000 and saved.source_price == 13500 and saved.error is None
    assert len(ctx._funpay.updates) == 1 and len(ctx._notifier.sent) == 1


# ---------------------------------------------------------- notify mode
def test_auto_reprice_off_notifies_once(monkeypatch):
    ctx = _make_ctx(monkeypatch, auto_reprice=False)
    found = _found(ctx, 15000, status=FoundStatus.PUBLISHED)
    lot = _lot(ctx, found)
    svc = RepricerService(ctx, PublisherService(ctx))
    assert svc.status()["enabled"] is False and svc.status()["mode"] == "notify"
    _found(ctx, 13500, status=FoundStatus.PUBLISHED)

    res = svc.run()
    assert res["repriced"] == 0 and res["notified"] == 1 and res["errors"] == []
    saved = ctx.storage.get_lot(lot.id)
    assert saved.price == 29000 and saved.source_price == 15000
    assert ctx._funpay.updates == []
    assert len(ctx._notifier.sent) == 1
    kind, text = ctx._notifier.sent[0]
    assert kind == "price" and "Цена исходника изменилась" in text and "Пересчитайте лот" in text
    assert "рекомендуемая: <b>26 000" in text and "29 000" in text
    ev = [e for e in ctx.storage.events() if e["kind"] == "reprice" and "пересчитайте лот" in e["message"]]
    assert len(ev) == 1 and ev[0]["level"] == "warning"

    # повторный запуск — без дубликатов
    res = svc.run()
    assert res["notified"] == 0 and res["skipped"] == 1 and len(ctx._notifier.sent) == 1
    assert len([e for e in ctx.storage.events() if e["kind"] == "reprice" and "пересчитайте" in e["message"]]) == 1

    # цена исходника изменилась ещё раз — новое напоминание
    _found(ctx, 12000, status=FoundStatus.PUBLISHED)
    res = svc.run()
    assert res["notified"] == 1 and len(ctx._notifier.sent) == 2 and "12 000" in ctx._notifier.sent[1][1]
    assert ctx.storage.get_lot(lot.id).price == 29000


def test_notifier_failure_does_not_raise(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    found = _found(ctx, 15000, status=FoundStatus.PUBLISHED)
    _lot(ctx, found)

    class Boom:
        def send(self, *a, **k):
            raise RuntimeError("telegram down")

    svc = RepricerService(ctx, PublisherService(ctx), notifier=Boom())
    _found(ctx, 13500, status=FoundStatus.PUBLISHED)
    res = svc.run()
    assert res["repriced"] == 1 and res["errors"] == []
    assert any("telegram down" in e["message"] for e in ctx.storage.events())


# ----------------------------------------------------------- candidates
def test_candidate_suggested_price_refreshed(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    stale = _found(ctx, 13500, source_id="c1", suggested=29000)  # устарела (должно быть 26000)
    fresh = _found(ctx, 15000, source_id="c2", suggested=29000)  # верная
    close = _found(ctx, 15000, source_id="c3", suggested=29000.4)  # расхождение < 1 ₽ — не трогаем
    none = _found(ctx, 10000, source_id="c4", suggested=None)  # не задана
    ignored = _found(ctx, 10000, source_id="c5", status=FoundStatus.IGNORED, suggested=1)  # не кандидат
    with_lot = _found(ctx, 10000, source_id="c6", suggested=1)
    _lot(ctx, with_lot, status=LotStatus.DRAFT, funpay_lot_id=None, source_price=10000, price=19000)

    svc = RepricerService(ctx, PublisherService(ctx))
    res = svc.run()
    assert res["errors"] == [] and res["suggested_updated"] == 2
    assert ctx.storage.get_found(stale.id).suggested_price == 26000
    assert ctx.storage.get_found(fresh.id).suggested_price == 29000
    assert ctx.storage.get_found(close.id).suggested_price == 29000.4
    assert ctx.storage.get_found(none.id).suggested_price == 19000
    assert ctx.storage.get_found(ignored.id).suggested_price == 1
    assert ctx.storage.get_found(with_lot.id).suggested_price == 1
    # повторный запуск — всё актуально
    assert svc.run()["suggested_updated"] == 0


def test_candidate_refresh_after_pricing_rule_change(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    c = _found(ctx, 10000, suggested=19000)
    p = ctx.profiles.get("wot")
    p.pricing.mode = "percent"
    p.pricing.percent = 50
    ctx.profiles.save(p)
    svc = RepricerService(ctx, PublisherService(ctx))
    assert svc.run()["suggested_updated"] == 1
    assert ctx.storage.get_found(c.id).suggested_price == 15000


# --------------------------------------------------------------- errors
def test_missing_found_and_profile_produce_errors_not_exceptions(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    good = _found(ctx, 15000, source_id="g", status=FoundStatus.PUBLISHED)
    dangling = ctx.storage.save_lot(
        OurLot(found_id=999999, profile_id="wot", price=1, source_price=1, source_url="x", status=LotStatus.ACTIVE)
    )
    orphan_found = _found(ctx, 15000, source_id="o", status=FoundStatus.PUBLISHED, profile_id="nope")
    orphan = _lot(ctx, orphan_found, profile_id="nope")
    cand = _found(ctx, 15000, source_id="cand", profile_id="nope", suggested=1)
    _found(ctx, 13500, source_id="g", status=FoundStatus.PUBLISHED)
    _found(ctx, 13500, source_id="o", status=FoundStatus.PUBLISHED, profile_id="nope")
    lot = _lot(ctx, good)
    svc = RepricerService(ctx, PublisherService(ctx))

    res = svc.run()
    assert res["checked"] == 3 and res["repriced"] == 1 and res["skipped"] == 2
    assert len(res["errors"]) == 3
    assert any(f"лот #{dangling.id}" in e and "не найдено" in e for e in res["errors"])
    assert any(f"лот #{orphan.id}" in e and "nope" in e for e in res["errors"])
    assert any("кандидатов" in e and "nope" in e for e in res["errors"])
    assert ctx.storage.get_lot(lot.id).price == 26000
    assert ctx.storage.get_lot(orphan.id).price == 29000
    assert ctx.storage.get_found(cand.id).suggested_price == 1

    # ошибки данных пишутся в журнал один раз, в результате — каждый раз
    n_events = len([e for e in ctx.storage.events() if e["kind"] == "reprice" and e["level"] in ("error", "warning")])
    res = svc.run()
    assert len(res["errors"]) == 3
    n_events2 = len([e for e in ctx.storage.events() if e["kind"] == "reprice" and e["level"] in ("error", "warning")])
    assert n_events2 == n_events + 1  # только итоговая строка «переоценка: ... ошибок 3»


def test_storage_failure_does_not_raise(env):
    ctx, _, _, svc = env
    ctx.storage.list_lots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))  # type: ignore[method-assign]
    res = svc.run()
    assert res["repriced"] == 0 and any("db locked" in e for e in res["errors"])


def test_busy_guard(env):
    _, _, _, svc = env
    svc._busy.acquire()
    try:
        res = svc.run()
        assert res["errors"] == ["переоценка уже выполняется"] and svc.running
    finally:
        svc._busy.release()


# ---------------------------------------------------------------- trend
def test_price_trend(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    f = _found(ctx, 15000)
    _found(ctx, 13500)
    _found(ctx, 14000)
    svc = RepricerService(ctx, PublisherService(ctx))
    t = svc.price_trend(f.id)
    assert [h["price"] for h in t["history"]] == [15000, 13500, 14000]
    assert all(h["ts"] for h in t["history"])
    assert t["min"] == 13500 and t["max"] == 15000 and t["first"] == 15000 and t["last"] == 14000
    assert t["change_percent"] == -6.7

    assert svc.price_trend(f.id, limit=2)["first"] == 13500
    empty = svc.price_trend(424242)
    assert empty == {"history": [], "min": None, "max": None, "first": None, "last": None, "change_percent": 0.0}


# --------------------------------------------------------------- format
def test_format_reprice_escapes_html():
    lot = OurLot(
        id=5,
        found_id=1,
        profile_id="wot",
        title_ru="WoT <b>&</b> топы",
        price=29000,
        source_price=15000,
        source_url="https://lzt.market/1?a=1&b=2",
        funpay_url=None,
        status=LotStatus.DRAFT,
    )
    text = format_reprice(lot, 15000, 13500, 29000, 26000)
    assert "WoT &lt;b&gt;&amp;&lt;/b&gt; топы" in text and "a=1&amp;b=2" in text and "Наш лот: #5" in text
    assert "15 000 ₽ → <b>13 500 ₽</b> (-10%)" in text
    text = format_reprice(lot, 15000, 13500, 29000, 26000, currency="USD", applied=False)
    assert "$" in text and "Пересчитайте лот" in text and "рекомендуемая" in text
    assert "(—)" in format_reprice(lot, 0, 13500, 29000, 26000)
