"""Тесты Telegram-уведомлений: форматирование сообщений и отправка через httpx.MockTransport (без сети)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import Found, Listing, LotStatus, MatchResult, Order, OrderStatus, OurLot, SearchRunStats
from app.notify import MAX_TEXT, TelegramNotifier, esc, fmt_money, trim, truncate_html
from app.settings import TelegramSettings


class Telegram:
    """Фейковый Bot API: записывает запросы, отвечает заранее заданным ответом."""

    def __init__(self, status: int = 200, body: dict | None = None, exc: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else {"ok": True, "result": {"message_id": 1}}
        self.exc = exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json=self.body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def payload(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def make_notifier(settings: TelegramSettings, tg: Telegram | None = None) -> tuple[TelegramNotifier, Telegram]:
    tg = tg or Telegram()
    n = TelegramNotifier(lambda: settings, transport=tg.transport())
    n.min_interval = 0  # в тестах не ждём секунду между сообщениями
    return n, tg


@pytest.fixture()
def settings() -> TelegramSettings:
    return TelegramSettings(enabled=True, bot_token="123:ABC", chat_id="42")


def _found(
    i: int = 1,
    title: str = "Аккаунт WoT все топы",
    price: float = 15000,
    suggested: float | None = 29000,
    source: str = "funpay",
    seller_url: str | None = "https://funpay.com/users/10/",
    seller_name: str | None = "seller1",
    highlights: list[str] | None = None,
) -> Found:
    return Found(
        id=i,
        profile_id="wot",
        listing=Listing(
            source=source,
            source_id=str(i),
            url=f"https://funpay.com/lots/offer?id={i}",
            title=title,
            price=price,
            seller_name=seller_name,
            seller_url=seller_url,
        ),
        match=MatchResult(
            matched=True, score=3, highlights=highlights if highlights is not None else ["Chieftain", "Об. 279"]
        ),
        suggested_price=suggested,
    )


def _lot(status: LotStatus = LotStatus.DEACTIVATED) -> OurLot:
    return OurLot(
        id=7,
        found_id=1,
        profile_id="wot",
        funpay_lot_id=777,
        funpay_url="https://funpay.com/lots/offer?id=777",
        title_ru="WoT | Chieftain, Об. 279 | RU",
        price=29000,
        source_price=15000,
        source_url="https://lzt.market/123",
        status=status,
    )


def _order() -> Order:
    return Order(
        funpay_order_id="ABCDEFGH",
        status=OrderStatus.PAID,
        title="WoT | Chieftain, Об. 279 | RU",
        subcategory_name="Аккаунты",
        price=29000,
        buyer_name="buyer<1>",
        buyer_id="55",
        buyer_url="https://funpay.com/users/55/",
        order_url="https://funpay.com/orders/ABCDEFGH/",
    )


# ------------------------------------------------------------------ helpers
def test_fmt_money():
    assert fmt_money(15000) == "15 000 ₽"
    assert fmt_money(1234567.0) == "1 234 567 ₽"
    assert fmt_money(999) == "999 ₽"
    assert fmt_money(1299.5) == "1 299.50 ₽"
    assert fmt_money(10, "USD") == "10 $"
    assert fmt_money(None) == "—"
    assert fmt_money(-1000) == "-1 000 ₽"


def test_esc_and_trim():
    assert esc('<b>&"') == "&lt;b&gt;&amp;&quot;"
    assert esc(None) == ""
    assert trim("a" * 100, 10).endswith("…") and len(trim("a" * 100, 10)) == 10
    assert trim("  a   b ", 10) == "a b"


def test_truncate_html_keeps_markup_valid():
    text = '<b>Заголовок</b> <a href="https://x">' + "x" * 5000 + "</a> хвост"
    out = truncate_html(text, MAX_TEXT)
    assert len(out) <= MAX_TEXT
    assert out.endswith("…</a>")
    assert out.count("<a") == out.count("</a>") == 1
    # короткий текст не трогаем
    assert truncate_html("<b>ok</b>", 100) == "<b>ok</b>"
    # разрез внутри тега: тег выбрасывается, разметка остаётся валидной
    almost = "y" * 3930 + '<a href="https://example.com/very/long/url/that/goes/on/and/on">link</a>' + "z" * 300
    assert len(almost) > MAX_TEXT
    out2 = truncate_html(almost, MAX_TEXT)
    assert "<a" not in out2 and out2.endswith("…") and out2.startswith("y" * 3930)
    # разрез внутри HTML-сущности: сущность отбрасывается целиком
    ent = "w" * 3934 + "&amp;" + "v" * 300
    out3 = truncate_html(ent, MAX_TEXT)
    assert "&" not in out3 and out3.endswith("…")


# --------------------------------------------------------------- builders
def test_format_candidates_escapes_and_links(settings):
    n, _ = make_notifier(settings)
    f1 = _found(1, title="<b>Chieftain</b> & Об. 279 аккаунт", seller_name="evil<script>")
    f2 = _found(
        2, title="Lolz аккаунт", source="lolz", suggested=None, seller_url=None, seller_name=None, highlights=[]
    )
    text = n.format_candidates("WoT RU", [f1, f2])
    assert "Новые аккаунты: WoT RU (2)" in text
    # пользовательский HTML экранирован
    assert "&lt;b&gt;Chieftain&lt;/b&gt; &amp; Об. 279" in text
    assert "<b>Chieftain</b>" not in text
    assert "evil&lt;script&gt;" in text and "<script>" not in text
    # цены и предложенная цена
    assert "15 000 ₽" in text and "29 000 ₽" in text
    # бейджи источников
    assert "[FunPay]" in text and "[Lolz]" in text
    # ссылки на объявление и на продавца — ровно по одной
    assert 'href="https://funpay.com/lots/offer?id=1"' in text
    assert text.count("https://funpay.com/users/10/") == 1
    assert 'href="https://funpay.com/users/10/"' in text
    # фишки
    assert "Chieftain, Об. 279" in text
    # у второго нет продавца — строки с 👤 ровно одна (от первого)
    assert text.count("👤") == 1
    assert "→" not in text.split("2. ")[1]


def test_format_candidates_limits_items(settings):
    n, _ = make_notifier(settings)
    items = [_found(i, title=f"Аккаунт {i}") for i in range(1, 13)]
    text = n.format_candidates("WoT", items, max_items=10)
    assert "(12)" in text and "10. " in text and "11. " not in text
    assert "и ещё 2" in text


def test_format_candidates_trims_long_title(settings):
    n, _ = make_notifier(settings)
    text = n.format_candidates("P", [_found(1, title="Д" * 200)])
    assert "Д" * 80 not in text and "Д" * 70 in text and "…" in text


def test_format_source_sold(settings):
    n, _ = make_notifier(settings)
    text = n.format_source_sold(_lot(LotStatus.DEACTIVATED))
    assert "Исходник продан/снят" in text
    assert "WoT | Chieftain, Об. 279 | RU" in text
    assert "29 000 ₽" in text and "15 000 ₽" in text
    assert 'href="https://lzt.market/123"' in text and 'href="https://funpay.com/lots/offer?id=777"' in text
    assert "Лот деактивирован" in text and "Проверьте лот" not in text
    text2 = n.format_source_sold(_lot(LotStatus.ACTIVE))
    assert "Проверьте лот" in text2 and "Лот деактивирован" not in text2
    # явное указание важнее статуса
    assert "Лот деактивирован" in n.format_source_sold(_lot(LotStatus.ACTIVE), deactivated=True)


def test_format_order_with_and_without_lot(settings):
    n, _ = make_notifier(settings)
    text = n.format_order(_order(), _lot(LotStatus.SOLD))
    assert "Новый заказ на FunPay #ABCDEFGH" in text
    assert "WoT | Chieftain, Об. 279 | RU" in text
    assert "29 000 ₽" in text
    assert 'href="https://funpay.com/users/55/">buyer&lt;1&gt;</a>' in text
    assert 'href="https://funpay.com/orders/ABCDEFGH/"' in text
    assert "Купить исходник" in text and 'href="https://lzt.market/123"' in text
    assert "за 15 000 ₽" in text and "маржа 14 000 ₽" in text
    assert "Раздел: Аккаунты" in text

    text2 = n.format_order(_order(), None)
    assert "Купить исходник" not in text2 and "Лот в базе не найден" in text2

    # без лота, но с данными исходника в самом заказе
    o = _order()
    o.source_url, o.source_price = "https://lzt.market/999", 20000
    text3 = n.format_order(o, None)
    assert 'href="https://lzt.market/999"' in text3 and "маржа 9 000 ₽" in text3


def test_format_error_and_search_summary(settings):
    n, _ = make_notifier(settings)
    text = n.format_error("Ошибка <авторизации>", "golden_key & cookie протухли")
    assert "❌ <b>Ошибка &lt;авторизации&gt;</b>" in text
    assert "<code>golden_key &amp; cookie протухли</code>" in text
    assert n.format_error("x") == "❌ <b>x</b>"

    stats = [
        SearchRunStats(profile_id="wot", source="funpay", fetched=50, matched=5, new=2),
        SearchRunStats(profile_id="wot", source="lolz", fetched=30, matched=1, new=0, errors=["timeout <x>"]),
    ]
    text = n.format_search_summary(stats, profile_names={"wot": "WoT <RU>"})
    assert "Поиск завершён" in text
    assert "WoT &lt;RU&gt; [FunPay]: просмотрено 50, подошло 5, новых <b>2</b>" in text
    assert "[Lolz]" in text and "timeout &lt;x&gt;" in text
    assert "Итого: просмотрено 80, подошло 6, новых <b>2</b>" in text
    assert "Нечего искать" in n.format_search_summary([])


# ------------------------------------------------------------------- send
def test_send_disabled_when_not_configured():
    n, tg = make_notifier(TelegramSettings(enabled=False, bot_token="123:ABC", chat_id="42"))
    assert n.enabled is False
    assert n.send("hi") is False and tg.requests == []

    n, tg = make_notifier(TelegramSettings(enabled=True, bot_token="", chat_id="42"))
    assert n.send("hi") is False and tg.requests == []

    n, tg = make_notifier(TelegramSettings(enabled=True, bot_token="123:ABC", chat_id=""))
    assert n.enabled is False and n.send("hi") is False and tg.requests == []


def test_send_posts_correct_payload(settings):
    n, tg = make_notifier(settings)
    assert n.send("<b>Привет</b>", kind="info") is True
    assert len(tg.requests) == 1
    req = tg.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.telegram.org/bot123:ABC/sendMessage"
    body = tg.payload()
    assert body == {"chat_id": "42", "text": "<b>Привет</b>", "parse_mode": "HTML", "disable_web_page_preview": True}
    assert n.sent_count == 1 and n.last_error is None

    assert n.send("plain", parse_mode="", disable_preview=False) is True
    body = tg.payload()
    assert "parse_mode" not in body and body["disable_web_page_preview"] is False


def test_send_truncates_long_text(settings):
    n, tg = make_notifier(settings)
    assert n.send("<b>" + "x" * 6000 + "</b>") is True
    text = tg.payload()["text"]
    assert len(text) <= MAX_TEXT and text.endswith("…</b>")


def test_send_failure_returns_false(settings):
    n, _tg = make_notifier(
        settings, Telegram(status=400, body={"ok": False, "description": "Bad Request: chat not found"})
    )
    assert n.send("hi") is False
    assert n.last_error == "Bad Request: chat not found"

    n, _tg = make_notifier(settings, Telegram(exc=httpx.ConnectError("no network")))
    assert n.send("hi") is False
    assert "no network" in (n.last_error or "")

    # невалидный JSON в ответе тоже не роняет
    class Weird(Telegram):
        def handler(self, request):
            self.requests.append(request)
            return httpx.Response(502, text="<html>bad gateway</html>")

    n, _tg = make_notifier(settings, Weird())
    assert n.send("hi") is False and "502" in (n.last_error or "")


def test_send_empty_text_is_noop(settings):
    n, tg = make_notifier(settings)
    assert n.send("   ") is False and tg.requests == []


def test_kind_switches(settings):
    settings.notify_new_orders = False
    settings.notify_errors = False
    n, tg = make_notifier(settings)
    assert n.allowed("order") is False and n.allowed("error") is False
    assert n.allowed("candidates") is True and n.allowed("sold") is True and n.allowed("info") is True
    assert n.send("x", kind="order") is False
    assert n.send("x", kind="error") is False
    assert tg.requests == []
    assert n.send("x", kind="candidates") is True
    assert n.send("x", kind="sold") is True
    assert n.send("x", kind="info") is True
    assert n.send("x", kind="unknown-kind") is True
    assert len(tg.requests) == 4

    settings.notify_new_candidates = False
    settings.notify_source_sold = False
    assert n.send("x", kind="candidates") is False and n.send("x", kind="sold") is False
    assert len(tg.requests) == 4


def test_settings_getter_applies_changes_live():
    holder = {"s": TelegramSettings(enabled=False)}
    tg = Telegram()
    n = TelegramNotifier(lambda: holder["s"], transport=tg.transport())
    n.min_interval = 0
    assert n.enabled is False and n.send("x") is False
    holder["s"] = TelegramSettings(enabled=True, bot_token="999:ZZZ", chat_id="-100")
    assert n.enabled is True and n.send("x") is True
    assert str(tg.requests[-1].url).startswith("https://api.telegram.org/bot999:ZZZ/")
    assert tg.payload()["chat_id"] == "-100"


def test_rate_limit_between_messages(settings, monkeypatch):
    n, _tg = make_notifier(settings)
    n.min_interval = 1.0
    sleeps: list[float] = []
    monkeypatch.setattr("app.notify.time.sleep", lambda s: sleeps.append(s))
    assert n.send("1") and n.send("2")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


def test_test_connection(settings):
    n, tg = make_notifier(
        settings, Telegram(body={"ok": True, "result": {"id": 1, "is_bot": True, "username": "my_bot"}})
    )
    info = n.test_connection()
    assert info == {"ok": True, "error": None, "bot_name": "my_bot"}
    assert str(tg.requests[0].url) == "https://api.telegram.org/bot123:ABC/getMe" and tg.requests[0].method == "GET"

    n, _ = make_notifier(settings, Telegram(status=401, body={"ok": False, "description": "Unauthorized"}))
    info = n.test_connection()
    assert info["ok"] is False and info["error"] == "Unauthorized" and info["bot_name"] is None

    n, _ = make_notifier(settings, Telegram(exc=httpx.ConnectError("offline")))
    assert n.test_connection()["ok"] is False and "offline" in n.test_connection()["error"]

    n, tg = make_notifier(TelegramSettings(bot_token=""))
    assert n.test_connection()["ok"] is False and tg.requests == []

    # токен валиден, но chat_id не задан — ok, но с подсказкой
    n, _ = make_notifier(
        TelegramSettings(bot_token="123:ABC", chat_id=""), Telegram(body={"ok": True, "result": {"username": "b"}})
    )
    info = n.test_connection()
    assert info["ok"] is True and "chat_id" in info["error"]
