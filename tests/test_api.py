"""Интеграционные тесты HTTP API с фейковыми источниками (без сети)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Listing, Profile
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.settings import Settings
from app.storage import Storage


class FakeSource:
    """Источник, возвращающий заранее заданные объявления."""

    def __init__(self, name, listings):
        self.name = name
        self.listings = listings
        self.created: list[dict] = []
        self.active_calls: list[tuple] = []
        self.available = {l.source_id: True for l in listings}

    def search(self, profile, limit=None):
        return [l.model_copy(update={"game": profile.game}) for l in self.listings]

    def get_listing(self, source_id):
        return next((l for l in self.listings if l.source_id == source_id and self.available.get(source_id)), None)

    def is_available(self, source_id):
        return self.available.get(source_id, False)

    def check_auth(self):
        return {"ok": True, "username": f"{self.name}_user", "error": None}

    # --- FunPay-специфичное
    def resolve_subcategory_ids(self, cfg):
        return [148]

    def categories(self):
        return [{"id": 1, "name": "World of Tanks", "subcategories": [{"id": 148, "name": "Аккаунты", "type": "common"}]}]

    def create_lot(self, subcategory_id, **kw):
        self.created.append({"subcategory_id": subcategory_id, **kw})
        return {"lot_id": 777, "url": "https://funpay.com/lots/offer?id=777", "response": {"done": True}}

    def update_lot(self, lot_id, subcategory_id, **kw):
        self.created.append({"update": lot_id, **kw})

    def set_lot_active(self, lot_id, subcategory_id, active):
        self.active_calls.append((lot_id, active))


@pytest.fixture()
def client(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    settings = Settings()
    settings.funpay.golden_key = "test-key"
    settings.lolz.token = "test-token"
    settings.monitor.enabled = False
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    store = ProfileStore(tmp / "profiles")
    store.save(Profile(
        id="wot_test", name="WoT тест", game="wot", region="RU",
        sources={"funpay": {"enabled": True, "subcategory_id": 148}, "lolz": {"enabled": True, "category": "world-of-tanks"}},
        criteria={"price": {"min": 1000, "max": 50000}, "must_any": ["все топы", "chieftain|чифтейн"],
                  "exclude": ["бан"], "highlights": ["Chieftain", "Об. 279"]},
        pricing={"mode": "formula", "formula": "price * 2 - 1000", "round_to": 100},
        lot_template={"title_ru": "{game} | {highlights} | {region}", "fields": {"fields[server]": "ru"}},
    ))
    ctx = AppContext(settings=settings, storage=Storage(tmp / "db.sqlite"), profiles=store)
    funpay = FakeSource("funpay", [
        Listing(source="funpay", source_id="1", url="https://funpay.com/lots/offer?id=1",
                title="Аккаунт WoT все топы, Chieftain, Об. 279", price=15000, region="RU",
                seller_name="seller1", seller_url="https://funpay.com/users/10/",
                attributes={"subcategory_id": 148, "seller_reviews": 50}),
        Listing(source="funpay", source_id="2", url="https://funpay.com/lots/offer?id=2",
                title="Аккаунт с баном", price=15000, region="RU", attributes={"subcategory_id": 148}),
        Listing(source="funpay", source_id="3", url="https://funpay.com/lots/offer?id=3",
                title="все топы", price=99999, region="RU", attributes={"subcategory_id": 148}),
    ])
    lolz = FakeSource("lolz", [
        Listing(source="lolz", source_id="500", url="https://lzt.market/500", title="WoT чифтейн, 60 танков 10 лвл",
                price=20000, seller_name="lz", seller_url="https://lolz.live/members/5/"),
    ])
    ctx._funpay, ctx._lolz = funpay, lolz
    app = create_app(ctx, start_monitor=False)
    with TestClient(app) as c:
        c.fake_funpay, c.fake_lolz, c.ctx = funpay, lolz, ctx
        yield c


def test_status_and_settings(client):
    r = client.get("/api/status")
    assert r.status_code == 200 and r.json()["version"]
    s = client.get("/api/settings").json()
    assert s["funpay"]["golden_key_set"] is True and "…" in s["funpay"]["golden_key"] or "•" in s["funpay"]["golden_key"]
    # секрет не затирается маской
    r = client.put("/api/settings", json={"funpay": {"golden_key": s["funpay"]["golden_key"], "request_delay": 2.5}})
    assert r.status_code == 200
    assert client.ctx.settings.funpay.golden_key == "test-key"
    assert client.ctx.settings.funpay.request_delay == 2.5


def test_auth_check(client):
    r = client.post("/api/auth/check")
    assert r.json()["funpay"]["ok"] is True and r.json()["lolz"]["username"] == "lolz_user"


def test_search_match_and_lot_flow(client):
    assert client.post("/api/search", json={}).json()["started"] is True
    client.app.state.search._thread.join(timeout=10)
    st = client.get("/api/status").json()
    assert st["search"]["running"] is False
    assert sum(s["fetched"] for s in st["search"]["last"]) == 4

    cands = client.get("/api/found", params={"status": "candidate"}).json()
    assert {c["listing"]["source_id"] for c in cands} == {"1", "500"}
    rejected = client.get("/api/found", params={"status": "rejected"}).json()
    assert {c["listing"]["source_id"] for c in rejected} == {"2", "3"}
    c1 = next(c for c in cands if c["listing"]["source_id"] == "1")
    assert c1["suggested_price"] == 29000
    assert "Chieftain" in c1["match"]["highlights"]

    # предпросмотр лота
    p = client.post(f"/api/found/{c1['id']}/preview-lot").json()
    assert p["price"] == 29000 and "World of Tanks" in p["title_ru"] and p["subcategory_id"] == 148
    assert "seller1" not in p["description_ru"] and "funpay.com/lots" not in p["description_ru"]

    # черновик с правкой цены, затем публикация
    lot = client.post(f"/api/found/{c1['id']}/create-lot", json={"price": 31000, "publish": False}).json()
    assert lot["status"] == "draft" and lot["price"] == 31000
    lot = client.post(f"/api/lots/{lot['id']}/publish").json()
    assert lot["status"] == "active" and lot["funpay_lot_id"] == 777
    created = client.fake_funpay.created[-1]
    assert created["price"] == 31000 and created["extra_fields"] == {"fields[server]": "ru"}
    assert client.get(f"/api/found/{c1['id']}").json()["status"] == "published"

    # повторный поиск не ломает статус published
    client.app.state.search.run_sync()
    assert client.get(f"/api/found/{c1['id']}").json()["status"] == "published"

    # исходник продан -> монитор снимает лот
    client.fake_funpay.available["1"] = False
    r = client.post("/api/monitor/run").json()
    assert r["unavailable"] >= 1
    lot = client.get(f"/api/lots/{lot['id']}").json()
    assert lot["status"] == "deactivated" and lot["source_available"] is False
    assert (777, False) in client.fake_funpay.active_calls
    assert client.get(f"/api/found/{c1['id']}").json()["status"] == "sold"

    # активировать обратно и удалить
    assert client.post(f"/api/lots/{lot['id']}/activate").json()["status"] == "active"
    assert client.delete(f"/api/lots/{lot['id']}").json()["ok"] is True
    assert client.get("/api/lots").json() == []


def test_found_actions(client):
    client.app.state.search.run_sync()
    f = client.get("/api/found", params={"status": "candidate"}).json()[0]
    assert client.post(f"/api/found/{f['id']}/status", json={"status": "ignored"}).json()["status"] == "ignored"
    assert client.post(f"/api/found/{f['id']}/check").json()["available"] is True
    assert client.delete(f"/api/found/{f['id']}").json()["ok"] is True
    assert client.get(f"/api/found/{f['id']}").status_code == 404


def test_profiles_crud(client):
    p = client.get("/api/profiles/wot_test").json()
    p["id"] = "new_one"; p["name"] = "Новый"
    assert client.post("/api/profiles", json=p).status_code == 201
    assert client.post("/api/profiles", json=p).status_code == 409
    d = client.post("/api/profiles/new_one/duplicate").json()
    assert d["id"] == "new_one_copy"
    p["criteria"]["must_any"] = ["x"]
    assert client.put("/api/profiles/new_one", json=p).json()["criteria"]["must_any"] == ["x"]
    assert client.delete("/api/profiles/new_one").json()["ok"]
    assert client.get("/api/profiles/new_one").status_code == 404
    assert client.post("/api/profiles", json={**p, "id": "../evil"}).status_code in (400, 422)


def test_pricing_and_matching_helpers(client):
    r = client.post("/api/pricing/preview", json={"price": 30000, "pricing": {"mode": "formula", "formula": "price * 2 - 1000", "round_to": 100}})
    assert r.json()["price"] == 59000
    r = client.post("/api/pricing/preview", json={"price": 1, "pricing": {"mode": "formula", "formula": "__import__('os')"}})
    assert r.status_code == 400
    p = client.get("/api/profiles/wot_test").json()
    r = client.post("/api/matching/test", json={"profile": p, "text": "продам акк все топы чифтейн", "price": 10000}).json()
    assert r["matched"] is True and r["suggested_price"] == 19000
    r = client.post("/api/matching/test", json={"profile": p, "text": "все топы, но бан", "price": 10000}).json()
    assert r["matched"] is False


def test_events_and_index(client):
    assert isinstance(client.get("/api/events").json(), list)
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_export_market_and_orders(client):
    client.app.state.search.run_sync()
    r = client.get("/api/export/found.csv", params={"status": "candidate"})
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8-sig")
    assert body.splitlines()[0].startswith("id;profile;status;source;title;price;suggested_price")
    assert "Chieftain" in body and "lzt.market/500" in body
    assert client.get("/api/export/lots.csv").status_code == 200
    assert client.get("/api/export/orders.csv").status_code == 200

    f = client.get("/api/found", params={"status": "candidate", "source": "funpay"}).json()[0]
    m = client.get(f"/api/found/{f['id']}/market").json()
    assert m["all"]["count"] == 2 and m["all"]["min"] == 15000 and m["all"]["max"] == 20000
    assert m["by_source"]["funpay"]["count"] == 1 and m["percentile"] == 0
    assert client.get("/api/found/999999/market").status_code == 404

    assert client.get("/api/orders").json() == []
    st = client.get("/api/status").json()
    assert st["orders"] == {"paid": 0, "total": 0} and "telegram" in st


def test_seller_and_history(client):
    client.app.state.search.run_sync()
    f = client.get("/api/found", params={"status": "candidate", "source": "funpay"}).json()[0]
    h = client.get(f"/api/found/{f['id']}/history").json()
    assert h["history"] and h["first"] == 15000 and h["change_percent"] == 0.0
    client.fake_funpay.get_seller = lambda sid: {"id": sid, "name": "seller1", "reviews": 50, "lots": []}
    r = client.get("/api/funpay/seller", params={"url": "https://funpay.com/users/10/"}).json()
    assert r["id"] == "10" and r["lots_count"] == 0
    assert client.get("/api/funpay/seller").status_code == 400


def test_settings_roundtrip_keeps_all_secrets_and_origin_guard(client):
    client.ctx.settings.telegram.bot_token = "123:ABCDEFGHIJKLMNOP"
    masked = client.get("/api/settings").json()
    assert "…" in masked["telegram"]["bot_token"]
    client.put("/api/settings", json=masked)
    assert client.ctx.settings.telegram.bot_token == "123:ABCDEFGHIJKLMNOP"
    assert client.ctx.settings.funpay.golden_key == "test-key"
    # CSRF: изменяющий запрос с чужого сайта отклоняется, со своего — проходит
    r = client.post("/api/search/cancel", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/search/cancel", headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_csv_export_neutralises_formulas(client):
    from app.models import Found, Listing, MatchResult
    client.ctx.storage.upsert_found(Found(profile_id="wot_test", match=MatchResult(matched=True, score=1),
                                          listing=Listing(source="funpay", source_id="x1", url="u", price=10,
                                                          title="=HYPERLINK(\"http://evil\")", seller_name="+cmd")))
    body = client.get("/api/export/found.csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and "'+cmd" in body
