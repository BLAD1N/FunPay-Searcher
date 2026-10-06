"""Тесты диагностики парсеров (app/diagnostics.py) на фикстурах FunPay (сети нет — httpx.MockTransport)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.diagnostics as diag
from app.diagnostics import FINAL_LINE, run_diagnostics, sanitize_html, summarize
from app.models import Listing
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.settings import FunPaySettings, Settings
from app.sources.funpay import FunPaySource
from app.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "funpay"
USERNAME = "TestSeller"
CSRF = "csrf-token-test-123"
GOLDEN_KEY = "goldenkey-diag-secret"
PRIVATE = (USERNAME, CSRF, GOLDEN_KEY, "Ivan_Seller", "EuroTrader", "na_dealer", "Buyer_One", "second_buyer",
           "refund_guy", "777001", "в наличии")


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFunPay:
    """Мини-сервер FunPay: главная, список лотов, лот, форма, наши лоты, продажи, чаты."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logged_out = False
        self.break_lots = False      # «сломать» разметку списка лотов (a.tc-item -> a.tc-row)
        self.explode = False         # 500 на всё, кроме главной

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = dict(request.url.params)
        if path == "/":
            html = load("main_page.html")
            if self.logged_out:
                html = html.replace(f'<div class="user-link-name">{USERNAME}</div>', "")
            return httpx.Response(200, text=html, headers={"set-cookie": "PHPSESSID=sess123; path=/; HttpOnly"})
        if self.explode:
            return httpx.Response(500, text="Internal Server Error")
        if path == "/lots/148/":
            html = load("lots_list.html")
            if self.break_lots:
                html = html.replace('class="tc-item"', 'class="tc-row"')
            return httpx.Response(200, text=html)
        if path == "/lots/148/trade":
            return httpx.Response(200, text=load("my_lots.html"))
        if path == "/lots/offer":
            if params.get("id") == "1001":
                return httpx.Response(200, text=load("lot_page.html"))
            return httpx.Response(404, text="<html><body>Страница не найдена</body></html>")
        if path == "/lots/offerEdit":
            return httpx.Response(200, json=json.loads(load("offer_edit.json")))
        if path == "/orders/trade":
            return httpx.Response(200, text=load("orders_trade.html"))
        if path == "/chat/":
            return httpx.Response(200, text=load("chat_list.html"))
        return httpx.Response(404, text="<html><body>Страница не найдена</body></html>")


class FakeLolz:
    """Фейковый Lolzteam: без сети, с переключателем «токен невалиден»."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[str] = []

    def check_auth(self):
        self.calls.append("me")
        if not self.ok:
            return {"ok": False, "username": None, "user_id": None, "balance": None, "error": "токен Lolzteam невалиден"}
        return {"ok": True, "username": "neo", "user_id": 42, "balance": 10.5, "error": None}

    def categories(self):
        self.calls.append("category")
        return [{"name": "world-of-tanks", "title": "World of Tanks"}, {"name": "steam", "title": "Steam"}]

    def category_params(self, category):
        self.calls.append(f"params:{category}")
        return {"game": {"type": "list"}, "level_min": {"type": "int"}, "system_info": {"x": 1}}

    def search_category(self, category, params=None, pages=1, max_items=500):
        self.calls.append(f"search:{category}")
        return [Listing(source="lolz", source_id="500", url="https://lzt.market/500", title="WoT acc", price=2000,
                        seller_name="seller77", attributes={"raw_keys": ["item_id", "price", "seller"]})]


def make_ctx(tmp_path: Path, fake: FakeFunPay, lolz: FakeLolz | None = None, golden_key: str = GOLDEN_KEY) -> AppContext:
    settings = Settings()
    settings.funpay.golden_key = golden_key
    settings.funpay.request_delay = 0
    settings.lolz.token = "lolz-token-secret"
    ctx = AppContext(settings=settings, storage=Storage(tmp_path / "db.sqlite"), profiles=ProfileStore(tmp_path / "profiles"))
    src = FunPaySource(FunPaySettings(golden_key=golden_key, request_delay=0, timeout=5),
                       transport=httpx.MockTransport(fake.handler))
    src.retry_backoff = 0
    ctx._funpay = src
    ctx._lolz = lolz or FakeLolz()
    return ctx


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setattr(diag, "DATA_DIR", d)
    return d


def by_name(report: dict) -> dict[str, dict]:
    return {c["name"]: c for c in report["checks"]}


# ----------------------------------------------------------------------------- sanitize_html
def test_sanitize_html_removes_secrets_and_names():
    html = (load("main_page.html") + load("lots_list.html") + load("chat_list.html") + load("orders_trade.html")
            + '<input value="x" name="csrf_token"> Cookie: golden_key=abc.def; PHPSESSID=s1 '
            + 'mail user@example.com tel +7 (999) 123-45-67 <span class="badge-balance">1 234 ₽</span>')
    out = sanitize_html(html, extra_secrets=["abc.def"])
    for secret in (USERNAME, CSRF, "Ivan_Seller", "EuroTrader", "Buyer_One", "refund_guy", "user@example.com",
                   "123-45-67", "abc.def", "PHPSESSID=s1", "/users/5001/", "1 234 ₽", "в наличии"):
        assert secret not in out, secret
    assert 'data-app-data="***"' in out
    assert 'name="csrf_token"' in out            # структура формы сохранена, значение скрыто
    assert "golden_key=***" in out and "PHPSESSID=***" in out
    assert 'class="tc-item"' in out and "tc-desc-text" in out and "contact-item" in out   # селекторы остались
    assert "15 000 ₽" in out                      # цены лотов не трогаем
    assert sanitize_html(None) == "" and sanitize_html("") == ""


def test_sanitize_html_handles_json_wrapped_form():
    raw = load("offer_edit.json")
    out = sanitize_html(raw)
    assert CSRF not in out
    assert json.loads(out)["html"].count("***") >= 1


# ----------------------------------------------------------------------------- happy path
def test_run_diagnostics_all_ok(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    lolz = FakeLolz()
    ctx = make_ctx(tmp_path, fake, lolz)
    report = run_diagnostics(ctx)

    checks = by_name(report)
    expected = ["funpay_login", "funpay_categories", "funpay_list_lots", "funpay_list_filters", "funpay_get_listing",
                "funpay_lot_form", "funpay_my_lots", "funpay_sales", "funpay_chats",
                "lolz_me", "lolz_categories", "lolz_params", "lolz_search"]
    assert [c["name"] for c in report["checks"]] == expected
    for name in expected:
        assert checks[name]["ok"] is True, (name, checks[name])
        assert isinstance(checks[name]["elapsed_ms"], int)
        assert set(checks[name]) >= {"name", "ok", "details", "elapsed_ms", "hint"}
    assert report["ok"] is True
    assert report["app_version"] and report["python"] and report["platform"] and report["timestamp"]
    assert report["settings"] == {"golden_key_set": True, "token_set": True, "proxy_set": False, "user_agent_set": False}

    # содержимое проверок
    assert "World of Tanks: найдена" in checks["funpay_categories"]["details"]
    assert "id=148" in checks["funpay_categories"]["details"]
    assert "лотов 4, с ценой 4, с продавцом 4, с заголовком 4" in checks["funpay_list_lots"]["details"]
    assert checks["funpay_list_lots"]["selectors"]["a.tc-item"] == 4
    assert "f-server" in checks["funpay_list_filters"]["details"]
    assert "сервер" in checks["funpay_get_listing"]["details"] and "15 000" not in checks["funpay_get_listing"]["details"]
    assert "fields[summary][ru]" in checks["funpay_lot_form"]["details"]
    assert checks["funpay_lot_form"]["selectors"]["input[name=csrf_token]"] == 1
    assert "наших лотов в подкатегории 148: 3" in checks["funpay_my_lots"]["details"]
    assert "продаж на первой странице: 3" in checks["funpay_sales"]["details"]
    assert "чатов: 3, непрочитанных: 1" in checks["funpay_chats"]["details"]
    assert checks["lolz_params"]["details"].startswith("/steam/params: ключей 2")
    assert "объявлений 1" in checks["lolz_search"]["details"]
    assert lolz.calls == ["me", "category", "params:steam", "search:steam"]

    # снимки и отчёт на диске, приватные данные вырезаны
    snap_dir = data_dir / "diagnostics"
    assert (snap_dir / "report.json").exists()
    for name in expected[:9]:
        assert checks[name]["snapshot"] == f"{name}.html"
        assert (snap_dir / f"{name}.html").exists(), name
    assert (snap_dir / "lolz_params.json").exists()
    blob = "\n".join(p.read_text(encoding="utf-8") for p in snap_dir.iterdir())
    for secret in (*PRIVATE, "lolz-token-secret", "neo", "seller77"):
        assert secret not in blob, secret
    assert "tc-item" in (snap_dir / "funpay_list_lots.html").read_text(encoding="utf-8")
    assert 'name="csrf_token"' in (snap_dir / "funpay_lot_form.html").read_text(encoding="utf-8")
    saved = json.loads((snap_dir / "report.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["checks"]] == expected
    assert diag.load_report()["timestamp"] == report["timestamp"]

    # перехват _request снят
    assert "_request" not in vars(ctx._funpay)

    text = summarize(report)
    assert text.count("✅") == len(expected) and "❌" not in text
    assert text.rstrip().endswith(FINAL_LINE)
    for secret in PRIVATE:
        assert secret not in text


def test_run_diagnostics_only_funpay_with_explicit_subcategory(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    lolz = FakeLolz()
    report = run_diagnostics(make_ctx(tmp_path, fake, lolz), lolz=False, sample_subcategory_id=148)
    names = [c["name"] for c in report["checks"]]
    assert all(n.startswith("funpay_") for n in names) and len(names) == 9
    assert lolz.calls == []
    assert all(c["ok"] is True for c in report["checks"])
    report = run_diagnostics(make_ctx(tmp_path, fake, lolz), funpay=True, lolz=False, sample_subcategory_id=999)
    checks = by_name(report)
    assert checks["funpay_list_lots"]["ok"] is False and "404" in checks["funpay_list_lots"]["details"]


# ----------------------------------------------------------------------------- failures
def test_login_failure_skips_remaining_funpay_checks(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    fake.logged_out = True
    report = run_diagnostics(make_ctx(tmp_path, fake))
    checks = by_name(report)
    login = checks["funpay_login"]
    assert login["ok"] is False
    assert login["details"].startswith("AuthError:")
    assert "golden_key" in login["hint"]
    assert login["selectors"]["div.user-link-name"] == 0 and login["selectors"]["div.promo-game-list"] >= 1
    assert "snippet" in login and len(login["snippet"]) <= 300 and USERNAME not in login["snippet"]
    assert login["snapshot"] == "funpay_login.html"
    for name in ("funpay_categories", "funpay_list_lots", "funpay_list_filters", "funpay_get_listing",
                 "funpay_lot_form", "funpay_my_lots", "funpay_sales", "funpay_chats"):
        assert checks[name]["ok"] is None, name
        assert "пропущено" in checks[name]["details"]
    # только один запрос к FunPay — на главную
    assert [r.url.path for r in fake.requests] == ["/"]
    # Lolz при этом проверяется
    assert checks["lolz_me"]["ok"] is True
    assert report["ok"] is False
    text = summarize(report)
    assert "❌ funpay_login" in text and "⏭ funpay_chats" in text and "golden_key" in text


def test_missing_golden_key(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    report = run_diagnostics(make_ctx(tmp_path, fake, golden_key=""), lolz=False)
    checks = by_name(report)
    assert checks["funpay_login"]["ok"] is False and "golden_key" in checks["funpay_login"]["details"]
    assert fake.requests == []
    assert report["settings"]["golden_key_set"] is False


def test_broken_lots_markup_is_reported_with_selectors(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    fake.break_lots = True
    report = run_diagnostics(make_ctx(tmp_path, fake), lolz=False)
    checks = by_name(report)
    lots = checks["funpay_list_lots"]
    assert lots["ok"] is False
    assert "a.tc-item" in lots["hint"]
    assert lots["selectors"]["a.tc-item"] == 0 and lots["selectors"][".showcase-filters"] == 1
    assert lots["snippet"] and "tc-row" in (data_dir / "diagnostics" / "funpay_list_lots.html").read_text(encoding="utf-8")
    # фильтры на той же странице целы, лота-образца нет -> страница лота пропущена, форма и остальное — работают
    assert checks["funpay_list_filters"]["ok"] is True
    assert checks["funpay_get_listing"]["ok"] is None
    assert checks["funpay_lot_form"]["ok"] is True and checks["funpay_sales"]["ok"] is True
    assert report["ok"] is False
    assert "селекторы без совпадений: a.tc-item" in summarize(report)


def test_exceptions_are_recorded_not_raised(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    fake.explode = True
    report = run_diagnostics(make_ctx(tmp_path, fake, FakeLolz(ok=False)))
    checks = by_name(report)
    assert checks["funpay_login"]["ok"] is True
    assert checks["funpay_categories"]["ok"] is True          # категории с главной (кэш)
    for name in ("funpay_list_lots", "funpay_list_filters", "funpay_lot_form", "funpay_my_lots", "funpay_sales",
                 "funpay_chats"):
        assert checks[name]["ok"] is False, name
        assert checks[name]["details"].startswith("SourceError:"), checks[name]["details"]
        assert checks[name]["hint"]
    assert checks["lolz_me"]["ok"] is False and "токен" in checks["lolz_me"]["hint"].lower()
    assert all(checks[n]["ok"] is None for n in ("lolz_categories", "lolz_params", "lolz_search"))


def test_time_budget_skips_rest(tmp_path: Path, data_dir: Path):
    fake = FakeFunPay()
    report = run_diagnostics(make_ctx(tmp_path, fake), max_seconds=0)
    checks = by_name(report)
    assert checks["funpay_login"]["ok"] is None and "лимит времени" in checks["funpay_login"]["details"]
    assert fake.requests == []


def test_summarize_tolerates_empty_report():
    text = summarize({})
    assert FINAL_LINE in text


# ----------------------------------------------------------------------------- HTTP API
def test_api_routes(tmp_path: Path, data_dir: Path, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    fake = FakeFunPay()
    ctx = make_ctx(tmp_path, fake)
    with TestClient(create_app(ctx, start_monitor=False)) as client:
        assert client.get("/api/diagnostics/report").status_code == 404
        r = client.get("/api/diagnostics/run", params={"funpay": 1, "lolz": 1})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True and len(data["checks"]) == 13
        assert {c["name"] for c in data["checks"]} >= {"funpay_login", "lolz_search"}
        r = client.get("/api/diagnostics/report")
        assert r.status_code == 200 and r.json()["timestamp"] == data["timestamp"]
        r = client.get("/api/diagnostics/run", params={"funpay": 0, "lolz": 1})
        assert [c["name"] for c in r.json()["checks"]] == ["lolz_me", "lolz_categories", "lolz_params", "lolz_search"]
        r = client.get("/api/diagnostics/run", params={"funpay": 1, "lolz": 0, "subcategory_id": 148})
        assert [c["name"] for c in r.json()["checks"]][:3] == ["funpay_login", "funpay_categories", "funpay_list_lots"]
        body = r.text
        for secret in PRIVATE:
            assert secret not in body, secret
