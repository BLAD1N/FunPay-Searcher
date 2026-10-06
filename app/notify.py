"""Уведомления в Telegram: новые кандидаты, продажа исходника, новые заказы, ошибки.

Отправка идёт через Bot API (``sendMessage``) с ``parse_mode=HTML``. Любой пользовательский текст
(заголовки объявлений, имена продавцов/покупателей, ссылки) экранируется через :func:`esc`.
Все публичные методы «тихие»: исключений наружу не выбрасывают, при ошибке пишут warning и возвращают False.
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

import httpx

from .models import Found, LotStatus, Order, OurLot, SearchRunStats
from .settings import TelegramSettings

# httpx пишет каждый запрос в лог на уровне INFO вместе с URL, а в URL Telegram — токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_URL = "https://api.telegram.org"
MAX_TEXT = 4000  # лимит Telegram — 4096, оставляем запас на закрывающие теги
TITLE_LEN = 80

SOURCE_BADGE = {"funpay": "FunPay", "lolz": "Lolz"}
CURRENCY_SIGN = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸"}

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^<>]*?(/?)>")


# ------------------------------------------------------------------ helpers
def esc(value: Any) -> str:
    """Экранировать произвольный текст для HTML-разметки Telegram (<, >, &, кавычки)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def fmt_money(value: Any, currency: str = "RUB") -> str:
    """15000 -> "15 000 ₽"; 1299.5 -> "1 299.50 ₽"; None -> "—"."""
    if value is None or value == "":
        return "—"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return esc(value)
    body = f"{round(x):,.0f}" if abs(x - round(x)) < 0.005 else f"{x:,.2f}"
    body = body.replace(",", " ")
    sign = CURRENCY_SIGN.get((currency or "RUB").upper(), currency or "")
    return f"{body} {sign}".strip()


def trim(text: str | None, limit: int = TITLE_LEN) -> str:
    """Обрезать строку до ``limit`` символов с многоточием."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def truncate_html(text: str, limit: int = MAX_TEXT) -> str:
    """Обрезать HTML-текст до лимита, не ломая разметку: не резать внутри тега/сущности, закрыть открытые теги."""
    if len(text) <= limit:
        return text
    budget = max(16, limit - 64)  # запас под многоточие и закрывающие теги
    cut = text[:budget]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:  # разрез пришёлся внутри тега
        cut = cut[:lt]
    amp, semi = cut.rfind("&"), cut.rfind(";")
    if amp > semi and len(cut) - amp <= 10:  # разрез внутри сущности (&amp; ...)
        cut = cut[:amp]
    cut = cut.rstrip()
    stack: list[str] = []
    for m in _TAG_RE.finditer(cut):
        closing, name, self_closing = m.group(1), m.group(2).lower(), m.group(3)
        if self_closing:
            continue
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return cut + "…" + "".join(f"</{t}>" for t in reversed(stack))


def _link(url: str | None, label: str | None = None) -> str:
    """HTML-ссылка; без url — просто экранированный текст."""
    if not url:
        return esc(label or "")
    return f'<a href="{esc(url)}">{esc(label or url)}</a>'


# ------------------------------------------------------------------ notifier
class TelegramNotifier:
    """Отправка уведомлений в Telegram.

    :param settings_getter: функция, возвращающая актуальные :class:`TelegramSettings`
        (чтобы изменения настроек применялись без пересоздания объекта).
    :param transport: httpx-транспорт (для тестов — ``httpx.MockTransport``).
    :param logger: логгер (по умолчанию ``logging.getLogger("telegram")``).
    """

    # соответствие вида сообщения переключателю в настройках; "info" разрешён всегда
    KIND_SWITCH: ClassVar[dict[str, str]] = {
        "candidates": "notify_new_candidates",
        "sold": "notify_source_sold",
        "order": "notify_new_orders",
        "error": "notify_errors",
        "price": "notify_price_changes",
        "messages": "notify_messages",
    }
    min_interval = 1.0  # не чаще одного сообщения в секунду
    timeout = 15.0

    def __init__(
        self,
        settings_getter: Callable[[], TelegramSettings],
        transport: httpx.BaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._get_settings = settings_getter
        self.log = logger or logging.getLogger("telegram")
        self._lock = threading.Lock()
        self._last_sent_at = 0.0
        self.last_error: str | None = None
        self.sent_count = 0
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(self.timeout)}
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(**kwargs)

    # ------------------------------------------------------------ state
    @property
    def settings(self) -> TelegramSettings:
        try:
            return self._get_settings()
        except Exception:
            return TelegramSettings()

    @property
    def enabled(self) -> bool:
        s = self.settings
        return bool(s.enabled and s.bot_token and s.chat_id)

    def allowed(self, kind: str = "info") -> bool:
        """Включены ли уведомления вообще и данный вид в частности."""
        if not self.enabled:
            return False
        attr = self.KIND_SWITCH.get(kind)
        if attr is None:
            return True
        return bool(getattr(self.settings, attr, True))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._http.close()

    # ------------------------------------------------------------- send
    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            time.sleep(wait)

    def _api_url(self, method: str) -> str:
        return f"{API_URL}/bot{self.settings.bot_token}/{method}"

    def send(self, text: str, *, kind: str = "info", parse_mode: str = "HTML", disable_preview: bool = True) -> bool:
        """Отправить сообщение. Никогда не выбрасывает исключений: при ошибке — warning и False."""
        try:
            s = self.settings
            if not self.enabled:
                self.log.debug("Telegram выключен или не настроен — сообщение (%s) пропущено", kind)
                return False
            if not self.allowed(kind):
                self.log.debug("уведомления вида %r отключены в настройках", kind)
                return False
            text = truncate_html(text or "", MAX_TEXT) if parse_mode == "HTML" else (text or "")[:MAX_TEXT]
            if not text.strip():
                return False
            payload: dict[str, Any] = {
                "chat_id": s.chat_id,
                "text": text,
                "disable_web_page_preview": bool(disable_preview),
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            with self._lock:
                self._throttle()
                try:
                    resp = self._http.post(self._api_url("sendMessage"), json=payload)
                finally:
                    self._last_sent_at = time.monotonic()
            data: dict = {}
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}
            if resp.status_code == 200 and data.get("ok"):
                self.last_error = None
                self.sent_count += 1
                return True
            err = data.get("description") or f"HTTP {resp.status_code}"
            self.last_error = str(err)
            self.log.warning("Telegram: не удалось отправить сообщение (%s): %s", kind, err)
            return False
        except Exception as e:
            self.last_error = str(e)
            self.log.warning("Telegram: ошибка отправки сообщения (%s): %s", kind, e)
            return False

    def test_connection(self) -> dict:
        """Проверить токен бота через ``getMe``. Возвращает {ok, error, bot_name}."""
        s = self.settings
        if not s.bot_token:
            return {"ok": False, "error": "не задан токен бота", "bot_name": None}
        try:
            resp = self._http.get(self._api_url("getMe"))
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}
            if resp.status_code == 200 and data.get("ok"):
                result = data.get("result") or {}
                name = result.get("username") or result.get("first_name")
                error = None if s.chat_id else "не задан chat_id — сообщения отправлять некуда"
                return {"ok": True, "error": error, "bot_name": name}
            return {"ok": False, "error": data.get("description") or f"HTTP {resp.status_code}", "bot_name": None}
        except Exception as e:
            return {"ok": False, "error": str(e), "bot_name": None}

    # --------------------------------------------------- message builders
    def format_candidates(self, profile_name: str, found_list: list[Found], max_items: int = 10) -> str:
        """Список новых подходящих аккаунтов по профилю."""
        items = list(found_list or [])
        n = len(items)
        lines = [f"🔍 <b>Новые аккаунты: {esc(profile_name)} ({n})</b>"]
        for i, f in enumerate(items[:max_items], 1):
            lst = f.listing
            badge = SOURCE_BADGE.get(lst.source, str(lst.source))
            title = trim(lst.title or "(без названия)", TITLE_LEN)
            lines.append("")
            lines.append(f"{i}. [{esc(badge)}] {_link(lst.url, title)}")
            price = f"💵 {fmt_money(lst.price, lst.currency)}"
            if f.suggested_price:
                price += f" → <b>{fmt_money(f.suggested_price, lst.currency)}</b>"
            lines.append(price)
            highlights = [h for h in (f.match.highlights or []) if h]
            if highlights:
                lines.append("✨ " + esc(", ".join(highlights[:8])))
            if lst.seller_url:
                lines.append("👤 " + _link(lst.seller_url, lst.seller_name or "продавец"))
            elif lst.seller_name:
                lines.append("👤 " + esc(lst.seller_name))
        if n > max_items:
            lines.append("")
            lines.append(f"… и ещё {n - max_items}")
        return "\n".join(lines)

    def format_source_sold(self, lot: OurLot, deactivated: bool | None = None) -> str:
        """Исходное объявление продано/снято. ``deactivated`` — снят ли наш лот (по умолчанию — по статусу лота)."""
        if deactivated is None:
            deactivated = lot.status in (LotStatus.DEACTIVATED, LotStatus.SOLD)
        lines = ["⚠️ <b>Исходник продан/снят</b>", esc(trim(lot.title_ru or f"лот #{lot.id}", 120))]
        lines.append(f"Наша цена: {fmt_money(lot.price)} (исходник {fmt_money(lot.source_price)})")
        if lot.source_url:
            lines.append("Исходник: " + _link(lot.source_url))
        if lot.funpay_url:
            lines.append("Наш лот: " + _link(lot.funpay_url))
        elif lot.id:
            lines.append(f"Наш лот: #{lot.id}")
        lines.append("✅ Лот деактивирован" if deactivated else "❗ Проверьте лот — он всё ещё может быть в продаже")
        return "\n".join(lines)

    def format_order(self, order: Order, lot: OurLot | None = None) -> str:
        """Новый заказ (продажа) на FunPay + где купить исходник."""
        lines = [f"💰 <b>Новый заказ на FunPay #{esc(order.funpay_order_id)}</b>"]
        title = trim(order.title or "", 120)
        if title:
            lines.append(esc(title))
        if order.subcategory_name:
            lines.append(f"Раздел: {esc(order.subcategory_name)}")
        lines.append(f"Сумма: <b>{fmt_money(order.price, order.currency)}</b>")
        buyer = order.buyer_name or (f"#{order.buyer_id}" if order.buyer_id else None)
        if buyer or order.buyer_url:
            lines.append("Покупатель: " + _link(order.buyer_url, buyer or "профиль"))
        if order.order_url:
            lines.append("Заказ: " + _link(order.order_url))
        source_url = (lot.source_url if lot and lot.source_url else None) or order.source_url
        source_price = (lot.source_price if lot and lot.source_price else None) or order.source_price
        if source_url:
            line = "🛒 Купить исходник: " + _link(source_url)
            if source_price:
                line += f" за {fmt_money(source_price, order.currency)}"
                margin = float(order.price or 0) - float(source_price)
                line += f" (маржа {fmt_money(margin, order.currency)})"
            lines.append(line)
        elif lot is None:
            lines.append("Лот в базе не найден — проверьте, откуда брать исходник")
        return "\n".join(lines)

    def format_error(self, title: str, details: Any = "") -> str:
        """Сообщение об ошибке (авторизация, публикация и т.п.)."""
        text = f"❌ <b>{esc(trim(title, 200))}</b>"
        details = str(details or "").strip()
        if details:
            text += f"\n<code>{esc(details[:1500])}</code>"
        return text

    def format_search_summary(
        self, stats: Iterable[SearchRunStats], profile_names: dict[str, str] | None = None
    ) -> str:
        """Итоги поиска по профилям/источникам."""
        stats = list(stats or [])
        names = profile_names or {}
        lines = ["📊 <b>Поиск завершён</b>"]
        total_fetched = total_matched = total_new = 0
        for s in stats:
            total_fetched += s.fetched
            total_matched += s.matched
            total_new += s.new
            badge = SOURCE_BADGE.get(s.source, s.source)
            name = names.get(s.profile_id, s.profile_id)
            line = f"• {esc(name)} [{esc(badge)}]: просмотрено {s.fetched}, подошло {s.matched}, новых <b>{s.new}</b>"
            if s.errors:
                line += f" ⚠️ {esc(trim('; '.join(s.errors), 150))}"
            lines.append(line)
        if not stats:
            lines.append("Нечего искать: нет включённых профилей/источников")
        else:
            lines.append(f"Итого: просмотрено {total_fetched}, подошло {total_matched}, новых <b>{total_new}</b>")
        return "\n".join(lines)
