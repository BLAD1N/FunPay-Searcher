"""Тесты автоподнятия лотов: фейковый FunPay (categories + raise_lots), реальное хранилище (без сети)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from app.models import Found, Listing, LotStatus, MatchResult, OurLot
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.services.raiser import RaiserService
from app.settings import Settings
from app.storage import Storage

CATEGORIES = [
    {
        "id": 1,
        "name": "World of Tanks",
        "subcategories": [
            {"id": 148, "name": "Аккаунты", "type": "common"},
            {"id": 149, "name": "Голда", "type": "currency"},
        ],
    },
    {"id": 2, "name": "Dota 2", "subcategories": [{"id": 200, "name": "Аккаунты", "type": "common"}]},
    {"id": 3, "name": "CS2", "subcategories": [{"id": 300, "name": "Аккаунты", "type": "common"}]},
]


class FakeFunPay:
    def __init__(self, responses: dict[int, list[dict]] | None = None, categories_exc: Exception | None = None):
        self.responses = responses or {}
        self.categories_exc = categories_exc
        self.raise_calls: list[tuple[int, list[int] | None]] = []
        self.categories_calls = 0

    def categories(self):
        self.categories_calls += 1
        if self.categories_exc:
            raise self.categories_exc
        return CATEGORIES

    def raise_lots(self, category_id: int, subcategory_ids=None):
        self.raise_calls.append((category_id, list(subcategory_ids) if subcategory_ids else None))
        queue = self.responses.get(category_id)
        if not queue:
            return {"ok": True, "message": "Предложения подняты", "wait_seconds": None}
        resp = queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _make_ctx(monkeypatch, golden_key: str = "test-key") -> AppContext:
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    settings = Settings()
    settings.funpay.golden_key = golden_key
    return AppContext(settings=settings, storage=Storage(tmp / "db.sqlite"), profiles=ProfileStore(tmp / "profiles"))


def _add_lot(ctx: AppContext, subcategory_id: int | None, status: LotStatus = LotStatus.ACTIVE, i: str = "1") -> OurLot:
    found, _ = ctx.storage.upsert_found(
        Found(
            profile_id="p",
            listing=Listing(source="funpay", source_id=i, url=f"https://funpay.com/lots/offer?id={i}", price=1),
            match=MatchResult(matched=True),
        )
    )
    return ctx.storage.save_lot(
        OurLot(
            found_id=found.id,
            profile_id="p",
            subcategory_id=subcategory_id,
            title_ru=f"lot {i}",
            price=10,
            source_price=5,
            source_url="u",
            status=status,
            funpay_lot_id=int(i),
        )
    )


@pytest.fixture()
def env(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    _add_lot(ctx, 148, i="1")
    _add_lot(ctx, 149, i="2")
    _add_lot(ctx, 148, i="3")  # вторая подкатегория-дубль — id не должен повторяться
    _add_lot(ctx, 200, i="4")
    _add_lot(ctx, 300, status=LotStatus.DRAFT, i="5")  # черновик — не поднимаем
    _add_lot(ctx, 300, status=LotStatus.SOLD, i="6")  # продан — не поднимаем
    funpay = FakeFunPay(
        {
            1: [{"ok": True, "message": "Предложения подняты", "wait_seconds": None}],
            2: [{"ok": False, "message": "Подождите 1 час", "wait_seconds": 3600}],
        }
    )
    ctx._funpay = funpay
    return ctx, funpay, RaiserService(ctx)


def test_run_groups_by_category_and_remembers_wait(env):
    ctx, funpay, svc = env
    res = svc.run()
    assert res["errors"] == []
    assert res["raised"] == ["World of Tanks"]
    assert res["waiting"] == [{"category": "Dota 2", "wait_seconds": 3600, "message": "Подождите 1 час"}]
    assert sorted(funpay.raise_calls) == [(1, [148, 149]), (2, [200])]
    assert svc.last_run and svc.last_result == res

    st = svc.status()
    assert st["running"] is False and st["enabled"] is True and st["interval_hours"] == 4.0
    assert st["waiting"][0]["category"] == "Dota 2" and 3590 <= st["waiting"][0]["wait_seconds"] <= 3600
    assert "World of Tanks" in st["last_raised"]

    # второй запуск: Dota 2 пропускается до истечения ожидания, WoT поднимается снова
    res2 = svc.run()
    assert sorted(funpay.raise_calls) == [(1, [148, 149]), (1, [148, 149]), (2, [200])]
    assert res2["raised"] == ["World of Tanks"]
    assert res2["waiting"][0]["category"] == "Dota 2" and 0 < res2["waiting"][0]["wait_seconds"] <= 3600
    assert res2["skipped"][0]["category"] == "Dota 2"

    # время ожидания истекло — пробуем снова
    svc._next_allowed[2] = time.time() - 1
    res3 = svc.run()
    assert res3["raised"] == ["World of Tanks", "Dota 2"] and res3["waiting"] == []
    assert svc.status()["waiting"] == []
    events = [e for e in ctx.storage.events() if e["kind"] == "raise"]
    assert any("подняты" in e["message"] for e in events)


def test_run_without_golden_key_or_without_lots(monkeypatch):
    ctx = _make_ctx(monkeypatch, golden_key="")
    funpay = FakeFunPay()
    ctx._funpay = funpay
    res = RaiserService(ctx).run()
    assert res["raised"] == [] and res["errors"] and "golden_key" in res["errors"][0]
    assert funpay.categories_calls == 0

    ctx = _make_ctx(monkeypatch)
    ctx._funpay = funpay
    res = RaiserService(ctx).run()
    assert res == {"raised": [], "waiting": [], "errors": [], "skipped": []}
    assert funpay.categories_calls == 0 and funpay.raise_calls == []


def test_errors_do_not_raise(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    _add_lot(ctx, 148, i="1")
    _add_lot(ctx, 200, i="2")
    _add_lot(ctx, 999, i="3")  # неизвестная подкатегория
    funpay = FakeFunPay(
        {1: [RuntimeError("сеть упала")], 2: [{"ok": False, "message": "Ошибка", "wait_seconds": None}]}
    )
    ctx._funpay = funpay
    svc = RaiserService(ctx)
    res = svc.run()
    assert res["raised"] == [] and res["waiting"] == []
    assert any("999" in e for e in res["errors"])
    assert any("сеть упала" in e for e in res["errors"])
    assert any(e.startswith("Dota 2") and "Ошибка" in e for e in res["errors"])
    assert svc._next_allowed == {}

    # categories() упал — одна ошибка, без исключения
    ctx2 = _make_ctx(monkeypatch)
    _add_lot(ctx2, 148, i="1")
    ctx2._funpay = FakeFunPay(categories_exc=RuntimeError("401"))
    res = RaiserService(ctx2).run()
    assert res["raised"] == [] and len(res["errors"]) == 1 and "401" in res["errors"][0]


def test_status_before_first_run(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    ctx.settings.monitor.auto_raise_hours = 0
    svc = RaiserService(ctx)
    st = svc.status()
    assert st == {
        "running": False,
        "enabled": False,
        "interval_hours": 0.0,
        "last_run": None,
        "last_result": {},
        "last_raised": {},
        "waiting": [],
    }
