"""Фикстуры e2e-тестов: реальное приложение в фоновом потоке uvicorn + headless Chromium (Playwright).

Источники FunPay/Lolz заменены фейками (копия настройки из tests/test_api.py — тестовые модули
друг из друга не импортируем). Данные и профили каждого теста живут во временной папке.

Браузер один на сессию, приложение и контекст браузера — свои для каждого теста.
Playwright импортируется лениво (только в фикстурах), чтобы без него сбор тестов не падал.
"""
from __future__ import annotations

import glob
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.main import create_app
from app.models import Listing, Profile
from app.profiles import ProfileStore
from app.services.context import AppContext
from app.settings import Settings
from app.storage import Storage

# Где искать бинарник Chromium (первый найденный; переменная E2E_CHROMIUM имеет приоритет).
CHROMIUM_GLOBS = (
    "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
    str(Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome-linux" / "chrome"),
)
# Сообщения консоли, которые не считаем ошибками: ожидаемые 400/409 от валидации API.
IGNORED_CONSOLE = re.compile(r"Failed to load resource: the server responded with a status of (400|409)\b")
DEFAULT_TIMEOUT_MS = 10_000


def find_chromium() -> str | None:
    """Путь к Chromium или None (тогда e2e-тесты пропускаются)."""
    env = os.environ.get("E2E_CHROMIUM")
    if env:
        return env if Path(env).exists() else None
    for pattern in CHROMIUM_GLOBS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


# ----------------------------------------------------------------------------
# Фейковые источники (как в tests/test_api.py)
# ----------------------------------------------------------------------------

class FakeSource:
    """Источник, возвращающий заранее заданные объявления; запоминает вызовы публикации."""

    def __init__(self, name: str, listings: list[Listing]):
        self.name = name
        self.listings = listings
        self.created: list[dict] = []
        self.active_calls: list[tuple] = []
        self.available = {l.source_id: True for l in listings}
        self.delay = 0.0   # искусственная длительность поиска, сек (чтобы UI успел показать прогресс)

    def search(self, profile, limit=None):
        if self.delay:
            time.sleep(self.delay)
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
        return [{"id": 1, "name": "World of Tanks",
                 "subcategories": [{"id": 148, "name": "Аккаунты", "type": "common"}]}]

    def create_lot(self, subcategory_id, **kw):
        self.created.append({"subcategory_id": subcategory_id, **kw})
        return {"lot_id": 777, "url": "https://funpay.com/lots/offer?id=777", "response": {"done": True}}

    def update_lot(self, lot_id, subcategory_id, **kw):
        self.created.append({"update": lot_id, **kw})

    def set_lot_active(self, lot_id, subcategory_id, active):
        self.active_calls.append((lot_id, active))


def make_test_profile() -> Profile:
    return Profile(
        id="wot_test", name="WoT тест", game="wot", region="RU",
        sources={"funpay": {"enabled": True, "subcategory_id": 148},
                 "lolz": {"enabled": True, "category": "world-of-tanks"}},
        criteria={"price": {"min": 1000, "max": 50000}, "must_any": ["все топы", "chieftain|чифтейн"],
                  "exclude": ["бан"], "highlights": ["Chieftain", "Об. 279"]},
        pricing={"mode": "formula", "formula": "price * 2 - 1000", "round_to": 100},
        lot_template={"title_ru": "{game} | {highlights} | {region}", "fields": {"fields[server]": "ru"}},
    )


def make_fake_sources() -> tuple[FakeSource, FakeSource]:
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
        Listing(source="lolz", source_id="500", url="https://lzt.market/500",
                title="WoT чифтейн, 60 танков 10 лвл", price=20000,
                seller_name="lz", seller_url="https://lolz.live/members/5/"),
    ])
    return funpay, lolz


# ----------------------------------------------------------------------------
# Сервер uvicorn в фоновом потоке
# ----------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornThread:
    """Запуск ASGI-приложения в потоке; ждёт готовности, умеет корректно останавливаться."""

    def __init__(self, app: Any, port: int):
        import uvicorn
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                     log_config=None, access_log=False)
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="e2e-uvicorn", daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("uvicorn не смог запуститься (порт занят?)")
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn не запустился за отведённое время")
            time.sleep(0.02)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=timeout)


@dataclass
class E2EApp:
    """Доступ к запущенному приложению из теста: URL, контекст, фейки и HTTP-клиент для проверок."""

    base_url: str
    app: Any
    ctx: AppContext
    funpay: FakeSource
    lolz: FakeSource
    api: httpx.Client

    def run_search(self) -> None:
        """Синхронный поиск без HTTP — быстрая подготовка находок для сценария."""
        self.app.state.search.run_sync()

    def status(self) -> dict:
        return self.api.get("/api/status").json()

    def wait_search_done(self, timeout: float = 10.0) -> dict:
        """Ждёт, пока запущенный через UI поиск завершится (status.search.running == False)."""
        deadline = time.monotonic() + timeout
        while True:
            st = self.status()
            s = st["search"]
            if not s["running"] and s["last"]:
                return st
            if time.monotonic() > deadline:
                raise AssertionError(f"поиск не завершился за {timeout} с: {s}")
            time.sleep(0.05)


# ----------------------------------------------------------------------------
# Фикстуры
# ----------------------------------------------------------------------------

@pytest.fixture(scope="session")
def chromium_path() -> str:
    path = find_chromium()
    if not path:
        pytest.skip("Chromium не найден: установите браузер Playwright или задайте E2E_CHROMIUM")
    return path


@pytest.fixture(scope="session")
def browser(chromium_path):
    """Один headless Chromium на всю сессию тестов (E2E_HEADED=1 — с окном и замедлением)."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright для Python не установлен",
                                   exc_type=ImportError)
    sync_api.expect.set_options(timeout=DEFAULT_TIMEOUT_MS)
    headed = os.environ.get("E2E_HEADED") == "1"
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=not headed, executable_path=chromium_path,
                                         slow_mo=250 if headed else 0)
        except Exception as e:  # noqa: BLE001 — нет системных библиотек и т.п.: CI без браузера остаётся зелёным
            pytest.skip(f"не удалось запустить Chromium ({chromium_path}): {e}")
        yield browser
        browser.close()


@pytest.fixture()
def e2e_app(tmp_path: Path, monkeypatch) -> E2EApp:
    """Реальное приложение (create_app) с фейковыми источниками, временной базой и профилями."""
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)   # не трогаем config/settings.yaml
    settings = Settings()
    settings.funpay.golden_key = "test-key"
    settings.lolz.token = "test-token"
    settings.monitor.enabled = False
    store = ProfileStore(tmp_path / "profiles")
    store.save(make_test_profile())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = AppContext(settings=settings, storage=Storage(data_dir / "db.sqlite"), profiles=store)
    funpay, lolz = make_fake_sources()
    ctx._funpay, ctx._lolz = funpay, lolz
    # После сохранения настроек ctx.reset_sources() пересоздаёт клиентов через классы источников —
    # подменяем сами классы, чтобы фейки пережили это и тест никогда не вышел в сеть.
    monkeypatch.setattr("app.sources.funpay.FunPaySource", lambda *a, **k: funpay)
    monkeypatch.setattr("app.sources.lolz.LolzSource", lambda *a, **k: lolz)
    app = create_app(ctx, start_monitor=False)

    port = free_port()
    server = UvicornThread(app, port)
    server.start()
    base_url = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base_url, timeout=10.0) as api:
        yield E2EApp(base_url=base_url, app=app, ctx=ctx, funpay=funpay, lolz=lolz, api=api)
    server.stop()
    ctx.storage.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Запоминаем исход теста, чтобы в teardown страницы отличать падение теста от ошибок консоли."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"_e2e_rep_{rep.when}", rep)


@pytest.fixture()
def page(browser, e2e_app: E2EApp, request, tmp_path: Path):
    """Новая вкладка на каждый тест. Собирает ошибки консоли и исключения страницы;
    по окончании успешного теста требует, чтобы их не было (ожидаемые 400/409 игнорируются)."""
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="ru-RU")
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)
    errors: list[str] = []

    def on_console(msg):
        if msg.type == "error" and not IGNORED_CONSOLE.search(msg.text):
            errors.append(f"console.error: {msg.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.console_errors = errors
    yield page

    rep = getattr(request.node, "_e2e_rep_call", None)
    failed = rep is not None and rep.failed
    if failed:
        shot = tmp_path / "failure.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            print(f"\n[e2e] скриншот страницы при падении: {shot}")
            print("[e2e] текст страницы (#main):\n" + page.locator("#main").inner_text(timeout=2000)[:1500])
        except Exception as e:  # noqa: BLE001
            print(f"[e2e] не удалось снять диагностику страницы: {e}")
        if errors:
            print("[e2e] ошибки консоли браузера:\n  " + "\n  ".join(errors))
    # вкладку закрываем до остановки сервера, чтобы фоновый опрос страницы не породил ошибок сети
    context.close()
    if not failed:
        assert not errors, "Ошибки в консоли браузера / исключения страницы:\n" + "\n".join(errors)
