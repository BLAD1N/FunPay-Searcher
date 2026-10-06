"""E2E-сценарии веб-интерфейса: реальный сервер uvicorn + headless Chromium.

Каждый тест получает своё приложение (фикстура ``e2e_app``) и свою вкладку (``page``);
браузер общий на сессию. Селекторы — по видимому тексту и ролям, плюс устойчивые классы
компонентов (``found-card``, ``chip``, ``avail`` …). Вместо пауз — ожидания ``expect``.
"""

from __future__ import annotations

import re

import pytest

# exc_type=ImportError: пропускаем и при «сломанной» установке, а не только при отсутствии пакета
pytest.importorskip("playwright.sync_api", reason="Playwright для Python не установлен", exc_type=ImportError)
from playwright.sync_api import Page, expect

from .conftest import E2EApp, find_chromium

if not find_chromium():
    pytest.skip("Chromium не найден — см. tests/e2e/README.md", allow_module_level=True)


# ----------------------------------------------------------------------------
# помощники
# ----------------------------------------------------------------------------


def open_route(page: Page, app: E2EApp, route: str, heading: str) -> None:
    """Переход на hash-маршрут SPA и ожидание заголовка страницы."""
    page.goto(f"{app.base_url}/{route}")
    expect(page.locator("#main h1")).to_have_text(heading)


def toasts(page: Page):
    return page.locator("#toasts")


def status_toggle(page: Page, label: str):
    """Переключаемый чип в фильтрах (button.chip.toggle) по точному тексту."""
    return page.locator("button.chip.toggle", has_text=re.compile(rf"^{re.escape(label)}$"))


def row_status(row):
    """Чип статуса в первой колонке строки таблицы лотов."""
    return row.locator("td").first.locator(".chip")


# ----------------------------------------------------------------------------
# 1. Панель
# ----------------------------------------------------------------------------


def test_dashboard_search_run(page: Page, e2e_app: E2EApp):
    open_route(page, e2e_app, "#/dashboard", "Панель")
    expect(page.locator("#brand-name")).to_have_text("FunPay Searcher")
    expect(page.locator("#app-version")).to_contain_text("v")
    expect(page.locator(".nav-item.active")).to_have_text("Панель")

    last_card = page.locator(".card", has=page.locator("h2", has_text="Последний поиск"))
    expect(last_card).to_contain_text("Поиск ещё не запускался")

    # Поиск с фейками завершается мгновенно, а панель замечает завершение только по переходу
    # running -> не running между опросами. Делаем поиск заметно длящимся, чтобы сценарий был детерминирован.
    e2e_app.funpay.delay = 0.6
    page.get_by_role("button", name="Найти аккаунты").click()
    expect(toasts(page)).to_contain_text("Поиск запущен")
    progress = page.locator(".progress-panel")
    expect(progress).to_contain_text("Идёт поиск")
    expect(progress).to_contain_text("WoT тест")
    expect(progress.get_by_role("progressbar")).to_be_visible()
    expect(toasts(page)).to_contain_text("Поиск завершён")
    expect(progress).to_have_count(0)
    st = e2e_app.wait_search_done()
    assert sum(r["fetched"] for r in st["search"]["last"]) == 4

    rows = last_card.locator("tbody tr")
    expect(rows).to_have_count(2)
    expect(last_card).to_contain_text("WoT тест")
    expect(last_card.locator(".chip.funpay")).to_have_count(1)
    expect(last_card.locator(".chip.lolz")).to_have_count(1)
    # колонка «Получено»: funpay — 3, lolz — 1 (порядок задач детерминирован)
    expect(rows.nth(0).locator("td").nth(2)).to_have_text("3")
    expect(rows.nth(1).locator("td").nth(2)).to_have_text("1")
    # плитка кандидатов и бейдж в боковой панели
    expect(page.locator(".stat-tile", has_text="Кандидатов").locator(".stat-value")).to_have_text("2")
    expect(page.locator("#badge-candidates")).to_have_text("2")
    # блок «Последние события» подтянул журнал поиска
    events_card = page.locator(".card", has=page.locator("h2", has_text="Последние события"))
    expect(events_card).to_contain_text("поиск завершён")


# ----------------------------------------------------------------------------
# 2. Найдено
# ----------------------------------------------------------------------------


def test_found_page_filters_and_actions(page: Page, e2e_app: E2EApp):
    e2e_app.run_search()
    open_route(page, e2e_app, "#/found", "Найдено")
    cards = page.locator(".found-card")
    count = page.locator(".page-actions span.muted")
    expect(cards).to_have_count(2)
    expect(count).to_have_text("2 из 2")
    expect(cards.locator("span.chip", has_text=re.compile("^Кандидат$"))).to_have_count(2)

    # чипы статусов: добавить отклонённые, затем убрать кандидатов — остаются 2 отклонённых
    status_toggle(page, "Отклонено").click()
    expect(cards).to_have_count(4)
    status_toggle(page, "Кандидат").click()
    expect(cards).to_have_count(2)
    expect(count).to_have_text("2 из 2")
    expect(cards.locator("span.chip", has_text=re.compile("^Отклонено$"))).to_have_count(2)
    expect(cards.filter(has_text="Аккаунт с баном")).to_have_count(1)
    # вернуть фильтр по умолчанию (между кликами ждём загрузку, чтобы ответы не перегнали друг друга)
    status_toggle(page, "Кандидат").click()
    expect(cards).to_have_count(4)
    status_toggle(page, "Отклонено").click()
    expect(cards).to_have_count(2)

    # текстовый фильтр (без запроса к серверу)
    search = page.get_by_placeholder("Поиск по названию, продавцу, региону")
    search.fill("чифтейн")
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("чифтейн")
    expect(count).to_have_text("1 из 2")
    search.fill("")
    expect(cards).to_have_count(2)

    # «Проверить»: индикатор доступности получает время проверки
    chieftain = cards.filter(has_text="Chieftain")
    expect(chieftain.locator(".avail")).to_have_text(re.compile(r"^доступен"))
    expect(chieftain.locator(".avail .avail-when")).to_have_count(0)
    chieftain.get_by_role("button", name="Проверить", exact=True).click()
    expect(toasts(page)).to_contain_text("Исходник доступен")
    expect(chieftain.locator(".avail .dot.ok")).to_have_count(1)
    expect(chieftain.locator(".avail .avail-when")).to_contain_text("только что")
    f = e2e_app.api.get("/api/found", params={"status": "candidate", "source": "funpay"}).json()[0]
    assert f["available"] is True and f["last_checked"]

    # «Скрыть»: объявление уходит из кандидатов и появляется в статусе «Скрыто»
    cards.filter(has_text="чифтейн").get_by_role("button", name="Скрыть", exact=True).click()
    expect(toasts(page)).to_contain_text("Объявление скрыто")
    expect(cards).to_have_count(1)
    expect(count).to_have_text("1 из 1")
    status_toggle(page, "Скрыто").click()
    expect(cards).to_have_count(2)
    hidden = cards.filter(has_text="чифтейн")
    expect(hidden.locator("span.chip", has_text=re.compile("^Скрыто$"))).to_have_count(1)
    expect(hidden.get_by_role("button", name="Вернуть")).to_be_visible()
    ignored = e2e_app.api.get("/api/found", params={"status": "ignored"}).json()
    assert [x["listing"]["source_id"] for x in ignored] == ["500"]


# ----------------------------------------------------------------------------
# 3. Предпросмотр лота -> черновик -> публикация
# ----------------------------------------------------------------------------


def test_preview_modal_draft_and_publish(page: Page, e2e_app: E2EApp):
    e2e_app.run_search()
    open_route(page, e2e_app, "#/found", "Найдено")
    chieftain = page.locator(".found-card", has_text="Chieftain")
    expect(chieftain.locator(".price-ours")).to_contain_text("29 000 ₽")

    chieftain.get_by_role("button", name="Предпросмотр лота").click()
    modal = page.get_by_role("dialog", name="Предпросмотр лота")
    expect(modal.locator(".modal-header h2")).to_have_text("Предпросмотр лота")
    expect(modal).to_contain_text("Исходник:")
    expect(modal).to_contain_text("15 000 ₽")
    title = modal.locator(".field", has_text="Заголовок (RU)").locator("input")
    expect(title).to_have_value(re.compile("World of Tanks"))
    price = modal.locator(".field", has_text="Цена, ₽").locator("input")
    expect(price).to_have_value("29000")
    expect(modal.locator(".field", has_text="Подкатегория FunPay").locator("input")).to_have_value("148")

    price.fill("31000")
    modal.get_by_role("button", name="Сохранить черновик").click()
    expect(toasts(page)).to_contain_text("сохранён")
    expect(modal).to_have_count(0)
    expect(chieftain.locator("a.chip", has_text="Лот: Черновик")).to_have_count(1)

    open_route(page, e2e_app, "#/lots", "Лоты")
    rows = page.locator("#main table tbody tr")
    expect(rows).to_have_count(1)
    row = rows.first
    expect(row_status(row)).to_have_text("Черновик")
    expect(row).to_contain_text("World of Tanks")
    expect(row.locator("td.num").first).to_contain_text("31 000 ₽")
    expect(row.locator("td.num").first).to_contain_text("из 15 000 ₽")

    row.get_by_role("button", name="Опубликовать").click()
    expect(toasts(page)).to_contain_text("Лот опубликован")
    expect(row_status(row)).to_have_text("Активен")
    expect(row.locator("a", has_text="на FunPay")).to_have_attribute("href", "https://funpay.com/lots/offer?id=777")
    expect(page.locator("#badge-lots")).to_have_text("1")

    created = e2e_app.funpay.created[-1]
    assert created["price"] == 31000 and created["subcategory_id"] == 148
    assert created["extra_fields"] == {"fields[server]": "ru"}
    lot = e2e_app.api.get("/api/lots").json()[0]
    assert lot["status"] == "active" and lot["funpay_lot_id"] == 777 and lot["price"] == 31000
    assert e2e_app.api.get(f"/api/found/{lot['found_id']}").json()["status"] == "published"


# ----------------------------------------------------------------------------
# 4. Лоты: проверка исходника, активация, удаление
# ----------------------------------------------------------------------------


def test_lots_check_source_activate_delete(page: Page, e2e_app: E2EApp):
    e2e_app.run_search()
    found = e2e_app.api.get("/api/found", params={"status": "candidate", "source": "funpay"}).json()[0]
    r = e2e_app.api.post(f"/api/found/{found['id']}/create-lot", json={"price": 31000, "publish": True})
    assert r.status_code == 201, r.text
    lot = r.json()
    assert lot["status"] == "active"
    e2e_app.funpay.available["1"] = False  # исходник «продан»

    open_route(page, e2e_app, "#/lots", "Лоты")
    rows = page.locator("#main table tbody tr")
    expect(rows).to_have_count(1)
    row = rows.first
    expect(row_status(row)).to_have_text("Активен")

    # снятые лоты скрыты фильтром по умолчанию — включаем чип «Снят», чтобы увидеть результат проверки
    status_toggle(page, "Снят").click()
    expect(rows).to_have_count(1)
    row.get_by_role("button", name="Проверить исходник").click()
    expect(toasts(page)).to_contain_text("Исходник проверен")
    expect(row_status(row)).to_have_text("Снят")
    expect(row.locator(".avail")).to_have_text(re.compile(r"^недоступен"))
    expect(row.locator(".avail .dot.bad")).to_have_count(1)
    assert (777, False) in e2e_app.funpay.active_calls
    lot = e2e_app.api.get(f"/api/lots/{lot['id']}").json()
    assert lot["status"] == "deactivated" and lot["source_available"] is False
    assert e2e_app.api.get(f"/api/found/{found['id']}").json()["status"] == "sold"

    row.get_by_role("button", name="Активировать").click()
    expect(toasts(page)).to_contain_text("Лот активирован")
    expect(row_status(row)).to_have_text("Активен")
    # активация отправляет на FunPay полную форму лота (update_lot с active=True), а не только флаг
    update = e2e_app.funpay.created[-1]
    assert update.get("update") == 777 and update["active"] is True and update["price"] == 31000
    assert e2e_app.api.get(f"/api/lots/{lot['id']}").json()["status"] == "active"

    row.get_by_role("button", name="Удалить").click()
    dialog = page.get_by_role("dialog", name="Подтверждение")
    expect(dialog).to_contain_text(f"Удалить лот #{lot['id']}?")
    dialog.get_by_role("button", name="Удалить").click()
    expect(toasts(page)).to_contain_text("Лот удалён")
    expect(page.locator("#main .card")).to_contain_text("Лотов нет")
    assert e2e_app.api.get("/api/lots").json() == []


# ----------------------------------------------------------------------------
# 5. Профили: редактор, критерии, тестер
# ----------------------------------------------------------------------------


def _open_criteria_tab(page: Page, app: E2EApp):
    open_route(page, app, "#/profiles", "Профили")
    card = page.locator(".profile-card", has_text="WoT тест")
    expect(card).to_be_visible()
    expect(card).to_contain_text("wot_test")
    expect(card).to_contain_text("price * 2 - 1000")
    card.get_by_role("link", name="Открыть").click()
    expect(page.locator("#main h1")).to_have_text("WoT тест")
    page.get_by_role("tab", name="Критерии").click()
    expect(page.get_by_role("tab", name="Критерии")).to_have_class(re.compile(r"\bactive\b"))


def _run_tester(page: Page, text: str, price: str):
    tester = page.locator(".card", has_text="Проверить критерии")
    tester.get_by_placeholder("Вставьте заголовок").fill(text)
    tester.locator(".field", has_text="Цена").locator("input").fill(price)
    with page.expect_response(lambda r: "/api/matching/test" in r.url) as resp:
        tester.get_by_role("button", name="Проверить критерии").click()
    return tester, resp.value.json()


def test_profile_editor_criteria_and_tester(page: Page, e2e_app: E2EApp):
    _open_criteria_tab(page, e2e_app)
    must_any = page.locator(".field", has_text="must_any")
    expect(must_any.locator(".tag")).to_have_count(2)
    tag_input = must_any.locator(".tags input")
    tag_input.fill("immortal")
    tag_input.press("Enter")
    expect(must_any.locator(".tag")).to_have_count(3)
    expect(must_any.locator(".tag").last).to_contain_text("immortal")
    expect(tag_input).to_have_value("")

    # тестер гоняет текст через текущие (ещё не сохранённые) критерии
    tester, data = _run_tester(page, "все топы чифтейн", "10000")
    assert data["matched"] is True and data["suggested_price"] == 19000
    result = tester.locator(".result-box")
    expect(result).to_contain_text("Подходит")
    expect(result).to_contain_text("Совпадения")
    _, data = _run_tester(page, "все топы, но бан", "10000")
    assert data["matched"] is False
    expect(result).to_contain_text("Не подходит")
    expect(result).to_contain_text("Причины отклонения")

    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(toasts(page)).to_contain_text("Профиль сохранён")
    p = e2e_app.api.get("/api/profiles/wot_test").json()
    assert "immortal" in p["criteria"]["must_any"]
    assert p["criteria"]["must_any"][:2] == ["все топы", "chieftain|чифтейн"]


def test_profile_tester_shows_suggested_price(page: Page, e2e_app: E2EApp):
    _open_criteria_tab(page, e2e_app)
    tester, data = _run_tester(page, "все топы чифтейн", "10000")
    assert data["suggested_price"] == 19000
    # короткий таймаут: результат уже отрисован, нечего ждать полные 10 с
    expect(tester.locator(".result-box"), "в результате тестера должна быть наша цена").to_contain_text(
        "19 000", timeout=1500
    )


# ----------------------------------------------------------------------------
# 6. Настройки
# ----------------------------------------------------------------------------


def test_settings_save_and_auth_check(page: Page, e2e_app: E2EApp):
    open_route(page, e2e_app, "#/settings", "Настройки")
    funpay_card = page.locator(".card", has_text="golden_key (cookie)")
    expect(funpay_card).to_contain_text("golden_key установлен")
    key_input = funpay_card.locator(".field", has_text="golden_key (cookie)").locator("input")
    expect(key_input).to_have_value("")
    expect(key_input).to_have_attribute("placeholder", re.compile("оставьте пустым"))
    delay = funpay_card.locator(".field", has_text="Пауза между запросами").locator("input")
    expect(delay).to_have_value("1.5")

    delay.fill("2.5")
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(toasts(page)).to_contain_text("Настройки сохранены")
    s = e2e_app.api.get("/api/settings").json()
    assert s["funpay"]["request_delay"] == 2.5
    assert s["funpay"]["golden_key_set"] is True and s["lolz"]["token_set"] is True
    assert e2e_app.ctx.settings.funpay.golden_key == "test-key"  # пустое поле не затёрло ключ
    assert e2e_app.ctx.settings.lolz.token == "test-token"
    # страница перерисована с сохранёнными значениями
    expect(funpay_card.locator(".field", has_text="Пауза между запросами").locator("input")).to_have_value("2.5")
    expect(funpay_card).to_contain_text("golden_key установлен")

    page.get_by_role("button", name="Проверить подключение").click()
    box = page.locator(".card", has_text="Проверка подключения").locator(".result-box")
    expect(box).to_contain_text("FunPay: подключено как funpay_user")
    expect(box).to_contain_text("Lolz: подключено как lolz_user")
    expect(box.locator(".dot.ok")).to_have_count(2)
    expect(box.locator(".dot.bad")).to_have_count(0)
    # боковая панель тоже показывает подключения
    expect(page.locator("#conn-funpay")).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#conn-funpay-user")).to_have_text("funpay_user")
    expect(page.locator("#conn-lolz-user")).to_have_text("lolz_user")
    assert e2e_app.api.get("/api/status").json()["auth"]["funpay"]["ok"] is True


# ----------------------------------------------------------------------------
# 7. Журнал
# ----------------------------------------------------------------------------


def test_log_page_shows_search_events(page: Page, e2e_app: E2EApp):
    e2e_app.run_search()
    open_route(page, e2e_app, "#/log", "Журнал")
    table = page.locator("#main table")
    rows = table.locator("tbody tr")
    expect(table).to_contain_text("поиск завершён")
    expect(table).to_contain_text("старт поиска")
    expect(table).to_contain_text("запуск FunPay Searcher")
    expect(table).to_contain_text("WoT тест / funpay: получено 3, подходит 1, новых 1")
    expect(page.locator(".page-actions span.muted")).to_have_text(re.compile(r"^\d+ из \d+$"))

    # фильтр по типу события
    page.locator("#main select.select").select_option("search")
    kind_cells = rows.locator("td.nowrap")
    expect(kind_cells.first).to_have_text("search")
    expect(kind_cells.filter(has_not_text="search")).to_have_count(0)

    # фильтр по уровню: ошибок в журнале нет
    status_toggle(page, "error").click()
    expect(page.locator("#main .card")).to_contain_text("Событий нет")
    status_toggle(page, "error").click()
    expect(rows.first).to_be_visible()
