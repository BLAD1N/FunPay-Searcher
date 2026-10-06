"""Диагностика парсеров FunPay / Lolzteam: прогон проверок на реальных страницах и отчёт для разработчика.

Зачем: у FunPay нет API, приложение разбирает HTML. Когда FunPay меняет вёрстку, нужен точный и
безопасный для приватности отчёт — какие проверки упали, какие CSS-селекторы перестали находиться и
как выглядит (обезличенная) страница. Этот модуль:

- :func:`run_diagnostics` — последовательно выполняет проверки (``login`` -> категории -> список лотов ->
  фильтры -> страница лота -> форма лота -> наши лоты -> продажи -> чаты; для Lolzteam — ``/me``,
  ``/category``, ``/{category}/params``, одна страница поиска). Каждая проверка —
  ``{"name", "ok": True|False|None, "details", "elapsed_ms", "hint", "selectors", "snapshot"}``;
  ``ok=None`` — проверка пропущена (например, после неудачной авторизации). Исключения не наружу не
  выходят — записываются как ``ok=False`` с классом и текстом ошибки.
- :func:`sanitize_html` — обезличивание страницы: cookie/токены/id/балансы/имена/почты/телефоны -> ``***``.
- снимки страниц (обезличенные, до 400 КБ) — ``DATA_DIR/diagnostics/<check>.html``;
  отчёт — ``DATA_DIR/diagnostics/report.json``.
- :func:`summarize` — человекочитаемая сводка на русском.

Паузы между запросами (``request_delay``) соблюдают сами источники — модуль их не обходит.
"""

from __future__ import annotations

import contextlib
import json
import logging
import platform
import re
import time
import traceback
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString

from . import __version__
from .settings import DATA_DIR

log = logging.getLogger("diagnostics")

SNAPSHOT_MAX_BYTES = 400 * 1024
SNIPPET_CHARS = 300
DEFAULT_MAX_SECONDS = 240.0
SENTINEL = "***"

# ---------------------------------------------------------------------------
# Обезличивание
# ---------------------------------------------------------------------------
_APP_DATA_RE = re.compile(r"""(data-app-data\s*=\s*)(?:"[^"]*"|'[^']*')""", re.IGNORECASE)
# <input ... name="csrf_token" ... value="..."> в любом порядке атрибутов; кавычки могут быть экранированы (JSON)
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_CSRF_NAME_RE = re.compile(r"""name\s*=\s*\\?["']?csrf[-_]token\\?["']?""", re.IGNORECASE)
_VALUE_ATTR_RE = re.compile(r"""(value\s*=\s*)(\\?["'])(.*?)(\2)""", re.IGNORECASE | re.DOTALL)
_QUOTE = r"""(?:&quot;|\\?["'])"""
_CSRF_JSON_RE = re.compile(
    "(" + _QUOTE + r"csrf[-_]token" + _QUOTE + r"\s*:\s*)" + _QUOTE + r"""[^"'&\\]*""" + _QUOTE,
    re.IGNORECASE,
)
_COOKIE_RE = re.compile(r"(golden_key|PHPSESSID)(\s*=\s*)[^;\"'\s&<]+", re.IGNORECASE)
_USER_ID_URL_RE = re.compile(r"(/users/)\d+(/?)")
_USER_ID_JSON_RE = re.compile("(" + _QUOTE + r"userId" + _QUOTE + r"\s*:\s*)\d+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w/=?&#-])\+?\d(?:[\d\s\-().]{8,18})\d(?![\w/-])")
# классы элементов, текст которых — персональные данные (имена, балансы, тексты сообщений)
PRIVATE_TEXT_CLASSES = (
    "user-link-name",
    "media-user-name",
    "badge-balance",
    "user-link-balance",
    "balance",
    "contact-item-message",
    "message-text",
    "chat-msg-text",
)
_PRIVATE_CLASS_RE = "|".join(re.escape(c) for c in PRIVATE_TEXT_CLASSES)
_PRIVATE_ELEMENT_RE = re.compile(
    r"""(<(\w+)\b[^>]*\bclass\s*=\s*\\?["'][^"']*\b(?:"""
    + _PRIVATE_CLASS_RE
    + r""")\b[^"']*\\?["'][^>]*>)(.*?)(</\2\s*>)""",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_NODE_RE = re.compile(r">([^<]*\S[^<]*)<")
_TRUNCATED_NOTE = f"\n<!-- diagnostics: snapshot truncated to {SNAPSHOT_MAX_BYTES // 1024} KB -->\n"


def _mask_phone(m: re.Match) -> str:
    digits = sum(ch.isdigit() for ch in m.group(0))
    return SENTINEL if 10 <= digits <= 15 else m.group(0)


def _mask_private_element(m: re.Match) -> str:
    inner = m.group(3)
    if "<" not in inner:
        replaced = SENTINEL if inner.strip() else inner
    else:
        replaced = _TEXT_NODE_RE.sub(f">{SENTINEL}<", inner)
        # текст до первого тега / после последнего
        head, _, _ = inner.partition("<")
        if head.strip():
            replaced = SENTINEL + replaced[len(head) :]
        _, _, tail = replaced.rpartition(">")
        if tail.strip():
            replaced = replaced[: len(replaced) - len(tail)] + SENTINEL
    return m.group(1) + replaced + m.group(4)


def _private_leftovers(html: str) -> bool:
    """Остались ли в элементах с «личными» классами тексты, отличные от ``***`` (проверка через BS4)."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return False
    selector = ", ".join("." + c for c in PRIVATE_TEXT_CLASSES)
    for el in soup.select(selector):
        for s in el.find_all(string=True):
            if s.strip() and s.strip() != SENTINEL:
                return True
    return False


def _mask_with_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    selector = ", ".join("." + c for c in PRIVATE_TEXT_CLASSES)
    for el in soup.select(selector):
        for s in list(el.find_all(string=True)):
            if s.strip():
                s.replace_with(NavigableString(SENTINEL))
    return str(soup)


def sanitize_html(html: str | None, extra_secrets: Iterable[str] = ()) -> str:
    """Обезличить HTML/JSON-страницу FunPay перед сохранением снимка.

    Заменяет на ``***``: значение ``data-app-data`` (csrf-токен, userId), value у ``input[name=csrf_token]``,
    ``csrf-token``/``userId`` в JSON, значения ``golden_key``/``PHPSESSID``, id в ссылках ``/users/N/``,
    текст элементов ``.user-link-name``/``.media-user-name``/балансов/сообщений чата, e-mail и телефоны.
    ``extra_secrets`` — дополнительные строки (например, сам golden_key), которые нужно вырезать.
    """
    text = html or ""
    if not isinstance(text, str):
        text = str(text)
    for secret in extra_secrets:
        if secret and len(str(secret)) >= 4:
            text = text.replace(str(secret), SENTINEL)
    text = _APP_DATA_RE.sub(lambda m: m.group(1) + f'"{SENTINEL}"', text)

    def _mask_input(m: re.Match) -> str:
        tag = m.group(0)
        if not _CSRF_NAME_RE.search(tag):
            return tag
        return _VALUE_ATTR_RE.sub(lambda v: v.group(1) + v.group(2) + SENTINEL + v.group(4), tag)

    text = _INPUT_TAG_RE.sub(_mask_input, text)
    text = _CSRF_JSON_RE.sub(lambda m: m.group(1) + f'"{SENTINEL}"', text)
    text = _USER_ID_JSON_RE.sub(lambda m: m.group(1) + "0", text)
    text = _COOKIE_RE.sub(lambda m: m.group(1) + m.group(2) + SENTINEL, text)
    text = _USER_ID_URL_RE.sub(lambda m: m.group(1) + SENTINEL + m.group(2), text)
    text = _PRIVATE_ELEMENT_RE.sub(_mask_private_element, text)
    text = _EMAIL_RE.sub(SENTINEL, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    if _private_leftovers(text):
        try:
            text = _mask_with_bs4(text)
        except Exception:
            log.debug("sanitize_html: резервное обезличивание через BS4 не удалось", exc_info=True)
    return text


def _truncate(text: str, limit: int = SNAPSHOT_MAX_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore") + _TRUNCATED_NOTE


def _snippet(html: str, limit: int = SNIPPET_CHARS) -> str:
    compact = re.sub(r"\s+", " ", html or "").strip()
    return compact[:limit]


def _count_selectors(html: str | None, selectors: Iterable[str]) -> dict[str, int]:
    """Сколько элементов находит каждый селектор на странице (0 — селектор «сломан»)."""
    out: dict[str, int] = {}
    if not html:
        return {s: 0 for s in selectors}
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return {s: 0 for s in selectors}
    for sel in selectors:
        try:
            out[sel] = len(soup.select(sel))
        except Exception:
            out[sel] = -1
    return out


# ---------------------------------------------------------------------------
# Перехват ответов источника
# ---------------------------------------------------------------------------
class _Capture:
    """Оборачивает ``source._request`` и запоминает последний HTTP-ответ (путь, статус, текст)."""

    def __init__(self, source: Any):
        self.source = source
        self.last: dict | None = None
        self._original: Callable | None = None
        self._had_instance_attr = False

    def __enter__(self) -> _Capture:
        original = getattr(self.source, "_request", None)
        if callable(original):
            self._original = original
            self._had_instance_attr = "_request" in getattr(self.source, "__dict__", {})

            def wrapper(method, path, *args, **kwargs):
                response = original(method, path, *args, **kwargs)
                try:
                    self.last = {
                        "method": method,
                        "path": str(path),
                        "status": getattr(response, "status_code", None),
                        "text": getattr(response, "text", None),
                    }
                except Exception:
                    self.last = None
                return response

            try:
                self.source._request = wrapper
            except Exception:
                self._original = None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._original is None:
            return
        try:
            if self._had_instance_attr:
                self.source._request = self._original
            else:
                del self.source._request
        except Exception:
            log.debug("диагностика: не удалось снять перехват _request", exc_info=True)

    def reset(self) -> None:
        self.last = None

    @property
    def text(self) -> str | None:
        return (self.last or {}).get("text")


# ---------------------------------------------------------------------------
# Запуск проверок
# ---------------------------------------------------------------------------
HINT_AUTH = "Обновите golden_key в настройках (cookie golden_key из браузера) и укажите User-Agent того же браузера."
HINT_LOLZ_AUTH = "Проверьте токен Lolzteam в настройках (https://lolz.team/account/api, права «market»)."
HINT_NETWORK = "Проверьте доступ в интернет / прокси: сайт не ответил."
FINAL_LINE = "Отправьте разработчику папку data/diagnostics/"


def _snapshot_dir() -> Path:
    return Path(DATA_DIR) / "diagnostics"


def _is_network_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(k in text for k in ("сети", "network", "connect", "timeout", "timed out", "ssl", "proxy"))


class _Runner:
    def __init__(self, ctx: Any, max_seconds: float):
        self.ctx = ctx
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self.checks: list[dict] = []
        self.secrets: list[str] = []
        with contextlib.suppress(Exception):
            self.secrets = [s for s in (ctx.settings.funpay.golden_key, ctx.settings.lolz.token) if s]
        self.dir = _snapshot_dir()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("диагностика: не удалось создать %s: %s", self.dir, e)

    # ---------------------------------------------------------------- helpers
    def out_of_time(self) -> bool:
        return time.monotonic() - self.started > self.max_seconds

    def skip(self, name: str, reason: str) -> dict:
        check = {
            "name": name,
            "ok": None,
            "details": f"пропущено: {reason}",
            "elapsed_ms": 0,
            "hint": "",
            "selectors": {},
            "snapshot": None,
        }
        self.checks.append(check)
        return check

    def save_snapshot(self, name: str, text: str | None, suffix: str = ".html") -> str | None:
        if not text:
            return None
        try:
            path = self.dir / f"{name}{suffix}"
            path.write_text(_truncate(sanitize_html(text, self.secrets)), encoding="utf-8")
            return path.name
        except Exception as e:
            log.warning("диагностика: не удалось сохранить снимок %s: %s", name, e)
            return None

    def run(
        self,
        name: str,
        fn: Callable[[dict], None],
        capture: _Capture | None = None,
        selectors: Iterable[str] = (),
        default_hint: str = "",
    ) -> dict:
        """Выполнить одну проверку. ``fn`` заполняет ``check`` (ok/details/hint); исключения -> ok=False."""
        if self.out_of_time():
            return self.skip(name, "превышен общий лимит времени диагностики")
        check: dict[str, Any] = {
            "name": name,
            "ok": True,
            "details": "",
            "elapsed_ms": 0,
            "hint": "",
            "selectors": {},
            "snapshot": None,
        }
        if capture is not None:
            capture.reset()
        t0 = time.monotonic()
        try:
            fn(check)
        except Exception as e:
            check["ok"] = False
            check["details"] = f"{e.__class__.__name__}: {e}"
            check["exception"] = e.__class__.__name__
            if not check.get("hint"):
                if e.__class__.__name__ == "AuthError":
                    check["hint"] = HINT_AUTH
                elif _is_network_error(e):
                    check["hint"] = HINT_NETWORK
                else:
                    check["hint"] = default_hint
            log.warning("диагностика %s: %s", name, check["details"])
            log.debug("диагностика %s: traceback:\n%s", name, traceback.format_exc())
        check["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        html = check.pop("_html", None)
        if html is None and capture is not None:
            html = capture.text
        if html:
            if selectors:
                check["selectors"] = _count_selectors(html, selectors)
            check["snapshot"] = self.save_snapshot(name, html, check.pop("_suffix", ".html"))
            if check["ok"] is False:
                check["snippet"] = _snippet(sanitize_html(html, self.secrets))
        if check["ok"] is False and not check.get("hint"):
            check["hint"] = default_hint
        if check["ok"] is True and not check.get("details"):
            check["details"] = "ok"
        self.checks.append(check)
        return check

    # ----------------------------------------------------------------- FunPay
    def funpay(self, sample_subcategory_id: int | None) -> None:
        names = [
            "funpay_login",
            "funpay_categories",
            "funpay_list_lots",
            "funpay_list_filters",
            "funpay_get_listing",
            "funpay_lot_form",
            "funpay_my_lots",
            "funpay_sales",
            "funpay_chats",
        ]
        try:
            src = self.ctx.funpay
        except Exception as e:
            self.checks.append(
                {
                    "name": names[0],
                    "ok": False,
                    "details": f"{e.__class__.__name__}: {e}",
                    "elapsed_ms": 0,
                    "hint": HINT_AUTH,
                    "selectors": {},
                    "snapshot": None,
                }
            )
            for n in names[1:]:
                self.skip(n, "источник FunPay недоступен")
            return

        with _Capture(src) as cap:
            # 1. login
            def _login(check: dict) -> None:
                info = src.login() or {}
                username, user_id, csrf = info.get("username"), info.get("user_id"), info.get("csrf_token")
                check["details"] = (
                    f"username={'есть' if username else 'нет'}, "
                    f"user_id={'есть' if user_id else 'нет'}, csrf={'есть' if csrf else 'нет'}"
                )
                if not username:
                    check["ok"] = False
                    check["hint"] = HINT_AUTH
                elif not csrf or not user_id:
                    check["ok"] = False
                    check["hint"] = (
                        "FunPay изменил разметку главной: в body[data-app-data] нет csrf-token/userId "
                        "(чат и сохранение лотов работать не будут)."
                    )

            login = self.run(
                names[0],
                _login,
                cap,
                selectors=[
                    "div.user-link-name",
                    "body[data-app-data]",
                    "div.promo-game-list",
                    "div.promo-game-item",
                    "div.game-title[data-id]",
                ],
                default_hint=HINT_AUTH,
            )
            if login["ok"] is not True:
                for n in names[1:]:
                    self.skip(n, "авторизация FunPay не прошла")
                return

            # 2. categories
            state: dict[str, Any] = {"subcategory_id": sample_subcategory_id, "lot_id": None}
            login_html = cap.text  # категории берутся из кэша главной — снимок делаем с неё же

            def _categories(check: dict) -> None:
                cats = src.categories() or []
                if cap.text is None:
                    check["_html"] = login_html
                subs = sum(len(c.get("subcategories") or []) for c in cats)
                wot = next((c for c in cats if "world of tanks" in str(c.get("name", "")).lower()), None)
                check["details"] = (
                    f"игр: {len(cats)}, подкатегорий: {subs}, World of Tanks: {'найдена' if wot else 'не найдена'}"
                )
                if not cats:
                    check["ok"] = False
                    check["hint"] = (
                        "FunPay изменил разметку главной: селектор div.promo-game-list / div.promo-game-item не найден."
                    )
                    return
                if wot is None:
                    check["ok"] = False
                    check["hint"] = (
                        "Категории разобраны, но игра World of Tanks не найдена — проверьте селекторы "
                        "div.game-title[data-id] и ссылок подкатегорий ul li a[href]."
                    )
                if state["subcategory_id"] is None:
                    for c in cats:
                        acc = next(
                            (
                                s
                                for s in (c.get("subcategories") or [])
                                if "аккаунт" in str(s.get("name", "")).lower() and s.get("type") != "currency"
                            ),
                            None,
                        )
                        if acc:
                            state["subcategory_id"] = int(acc["id"])
                            check["details"] += f"; образец: {c.get('name')} / {acc.get('name')} (id={acc['id']})"
                            break
                    if state["subcategory_id"] is None and subs:
                        first = next(s for c in cats for s in (c.get("subcategories") or []))
                        state["subcategory_id"] = int(first["id"])
                        check["details"] += f"; образец: подкатегория id={first['id']}"

            self.run(
                names[1],
                _categories,
                cap,
                selectors=[
                    "div.promo-game-list",
                    "div.promo-game-item",
                    "div.game-title[data-id]",
                    "div.promo-game-item ul li a[href]",
                ],
                default_hint="FunPay изменил разметку главной страницы (список игр).",
            )
            sid = state["subcategory_id"]
            if sid is None:
                for n in names[2:7]:
                    self.skip(n, "не найдена подкатегория-образец (укажите sample_subcategory_id)")
            else:
                # 3. list_lots
                def _list_lots(check: dict) -> None:
                    lots = src.list_lots(sid, max_items=200) or []
                    with_price = sum(1 for it in lots if it.price)
                    with_seller = sum(1 for it in lots if it.seller_name)
                    with_title = sum(1 for it in lots if (it.title or "").strip())
                    check["details"] = (
                        f"подкатегория {sid}: лотов {len(lots)}, с ценой {with_price}, "
                        f"с продавцом {with_seller}, с заголовком {with_title}"
                    )
                    if lots:
                        state["lot_id"] = lots[0].source_id
                    if not lots:
                        check["ok"] = False
                        check["hint"] = (
                            "FunPay изменил разметку списка лотов: селектор a.tc-item не найден "
                            "(или в подкатегории нет предложений — укажите другую через "
                            "sample_subcategory_id)."
                        )
                    elif with_title == 0 or with_seller == 0:
                        check["ok"] = False
                        broken = []
                        if with_title == 0:
                            broken.append(".tc-desc-text / .tc-desc")
                        if with_seller == 0:
                            broken.append(".tc-user .media-user-name")
                        check["hint"] = "FunPay изменил разметку лота в списке: не находятся " + ", ".join(broken) + "."

                self.run(
                    names[2],
                    _list_lots,
                    cap,
                    selectors=[
                        "a.tc-item",
                        "a.tc-item .tc-price",
                        "a.tc-item .tc-price[data-s]",
                        "a.tc-item .tc-desc-text",
                        "a.tc-item .tc-user .media-user-name",
                        "a.tc-item .tc-server",
                        ".showcase-filters",
                        ".showcase, .tc",
                    ],
                    default_hint="FunPay изменил разметку страницы /lots/{id}/ (список предложений).",
                )

                # 4. list_filters
                def _filters(check: dict) -> None:
                    filters = src.list_filters(sid) or []
                    check["details"] = f"фильтров: {len(filters)}" + (
                        " (" + ", ".join(str(f.get("name")) for f in filters[:8]) + ")" if filters else ""
                    )
                    if not filters:
                        check["hint"] = (
                            "Фильтры не найдены: селектор .showcase-filters select/input — "
                            "возможно, в подкатегории фильтров нет."
                        )

                self.run(
                    names[3],
                    _filters,
                    cap,
                    selectors=[".showcase-filters", ".showcase-filters select", ".showcase-filters input"],
                    default_hint="FunPay изменил разметку фильтров (.showcase-filters).",
                )

                # 5. get_listing
                lot_id = state["lot_id"]
                if not lot_id:
                    self.skip(names[4], "нет лота-образца (список лотов пуст)")
                else:

                    def _listing(check: dict) -> None:
                        listing = src.get_listing(lot_id)
                        if listing is None:
                            check["ok"] = False
                            check["details"] = f"лот {lot_id}: страница не распознана (None)"
                            check["hint"] = (
                                "FunPay изменил разметку страницы лота: селектор .param-list не найден "
                                "(либо лот только что продан)."
                            )
                            return
                        keys = sorted(k for k in (listing.attributes or {}) if not str(k).startswith("_"))
                        check["details"] = (
                            f"лот {lot_id}: title={'есть' if listing.title else 'нет'}, "
                            f"price={'есть' if listing.price else 'нет'}, "
                            f"seller={'есть' if listing.seller_name else 'нет'}; "
                            f"атрибуты: {', '.join(keys)[:300]}"
                        )
                        if not listing.title or not listing.price or not listing.seller_name:
                            check["ok"] = False
                            check["hint"] = (
                                "FunPay изменил разметку страницы лота: проверьте .param-item h5 + div, "
                                ".media-user-name, .payment-value / [data-s]."
                            )

                    self.run(
                        names[4],
                        _listing,
                        cap,
                        selectors=[
                            ".param-list",
                            ".param-item",
                            ".param-item h5",
                            ".media-user",
                            ".media-user-name",
                            ".payment-value, [data-s], .tc-price",
                            "a.back-link[href]",
                            ".breadcrumb a[href]",
                            "h1",
                        ],
                        default_hint="FunPay изменил разметку страницы лота /lots/offer?id=N.",
                    )

                # 6. lot form
                def _lot_form(check: dict) -> None:
                    form = src.get_lot_form(sid)
                    raw = cap.text
                    if raw:
                        try:
                            payload = json.loads(raw)
                            if isinstance(payload, dict) and payload.get("html"):
                                check["_html"] = str(payload["html"])
                        except ValueError:
                            pass
                    schema_names = [s.get("name") for s in (form.get("schema") or [])]
                    fields = form.get("fields") or {}
                    required = ["csrf_token", "node_id", "fields[summary][ru]", "price"]
                    missing = [k for k in required if k not in fields and k not in schema_names]
                    missing_schema = [k for k in required if k not in schema_names]
                    check["details"] = (
                        f"полей формы: {len(schema_names)}; имена: {', '.join(str(n) for n in schema_names)[:400]}"
                    )
                    if missing:
                        check["ok"] = False
                        check["hint"] = (
                            "FunPay изменил форму лота (/lots/offerEdit): нет полей "
                            + ", ".join(missing)
                            + " — публикация лотов не будет работать."
                        )
                    elif missing_schema:
                        check["details"] += f"; подставлены из главной: {', '.join(missing_schema)}"

                self.run(
                    names[5],
                    _lot_form,
                    cap,
                    selectors=[
                        "form",
                        "input[name=csrf_token]",
                        "input[name=node_id]",
                        "input[name=offer_id]",
                        "input[name='fields[summary][ru]']",
                        "textarea[name='fields[desc][ru]']",
                        "input[name=price]",
                        "input[name=amount]",
                        "input[name=active]",
                    ],
                    default_hint="FunPay изменил форму лота (/lots/offerEdit): ответ не JSON или без html.",
                )

                # 7. my lots
                def _my_lots(check: dict) -> None:
                    lots = src.list_my_lots(sid) or []
                    active = sum(1 for it in lots if (it.attributes or {}).get("active") is not False)
                    check["details"] = f"наших лотов в подкатегории {sid}: {len(lots)} (активных {active})"

                self.run(
                    names[6],
                    _my_lots,
                    cap,
                    selectors=["a.tc-item", "a.tc-item[href*='offerEdit']", "a.btn-add-offer", ".showcase"],
                    default_hint="FunPay изменил разметку страницы /lots/{id}/trade (наши предложения).",
                )

            # 8. sales
            def _sales(check: dict) -> None:
                sales = src.get_sales(max_pages=1) or []
                by_status: dict[str, int] = {}
                for s in sales:
                    by_status[str(s.get("status"))] = by_status.get(str(s.get("status")), 0) + 1
                check["details"] = f"продаж на первой странице: {len(sales)}" + (
                    " (" + ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) + ")" if by_status else ""
                )

            self.run(
                names[7],
                _sales,
                cap,
                selectors=[
                    "a.tc-item",
                    "a.tc-item .tc-order",
                    "a.tc-item .order-desc",
                    "a.tc-item .tc-price",
                    "a.tc-item .tc-date-time",
                    "a.tc-item .tc-user .media-user-name",
                    "input[name=continue]",
                    ".content-account-login",
                ],
                default_hint="FunPay изменил разметку страницы продаж /orders/trade.",
            )

            # 9. chats
            def _chats(check: dict) -> None:
                from .sources.funpay_chat import FunPayChat

                chats = FunPayChat(src).list_chats() or []
                unread = sum(1 for c in chats if c.get("unread"))
                check["details"] = f"чатов: {len(chats)}, непрочитанных: {unread}"
                if chats and sum(1 for c in chats if c.get("name")) == 0:
                    check["ok"] = False
                    check["hint"] = "FunPay изменил разметку списка чатов: в a.contact-item нет .media-user-name."

            self.run(
                names[8],
                _chats,
                cap,
                selectors=[
                    "a.contact-item",
                    "a.contact-item[data-id]",
                    "a.contact-item .media-user-name",
                    "a.contact-item .contact-item-message",
                    "div.user-link-name",
                ],
                default_hint="FunPay изменил разметку страницы чатов /chat/ (a.contact-item).",
            )

    # ------------------------------------------------------------------- Lolz
    def lolz(self) -> None:
        names = ["lolz_me", "lolz_categories", "lolz_params", "lolz_search"]
        try:
            src = self.ctx.lolz
        except Exception as e:
            self.checks.append(
                {
                    "name": names[0],
                    "ok": False,
                    "details": f"{e.__class__.__name__}: {e}",
                    "elapsed_ms": 0,
                    "hint": HINT_LOLZ_AUTH,
                    "selectors": {},
                    "snapshot": None,
                }
            )
            for n in names[1:]:
                self.skip(n, "источник Lolzteam недоступен")
            return

        def _me(check: dict) -> None:
            info = src.check_auth() or {}
            if not info.get("ok"):
                check["ok"] = False
                check["details"] = f"ошибка: {info.get('error') or 'неизвестно'}"
                check["hint"] = HINT_NETWORK if "сети" in str(info.get("error") or "") else HINT_LOLZ_AUTH
                return
            check["details"] = (
                f"username={'есть' if info.get('username') else 'нет'}, "
                f"user_id={'есть' if info.get('user_id') is not None else 'нет'}"
            )

        me = self.run(names[0], _me, default_hint=HINT_LOLZ_AUTH)
        if me["ok"] is not True:
            for n in names[1:]:
                self.skip(n, "авторизация Lolzteam не прошла")
            return

        state: dict[str, Any] = {"category": None}

        def _categories(check: dict) -> None:
            cats = src.categories() or []
            names_ = [str(c.get("name")) for c in cats if isinstance(c, dict)]
            check["details"] = f"категорий: {len(cats)}"
            if names_:
                check["details"] += " (" + ", ".join(names_[:10]) + ("…" if len(names_) > 10 else "") + ")"
            if not cats:
                check["ok"] = False
                check["hint"] = (
                    "API Lolzteam не вернул категории (GET /category) — проверьте токен/доступ к prod-api.lzt.market."
                )
                return
            state["category"] = "steam" if "steam" in names_ else names_[0]
            check["_html"] = json.dumps(names_, ensure_ascii=False)
            check["_suffix"] = ".json"

        self.run(names[1], _categories, default_hint="Ошибка GET /category Lolzteam.")
        cat = state["category"]
        if not cat:
            self.skip(names[2], "нет категории-образца")
            self.skip(names[3], "нет категории-образца")
            return

        def _params(check: dict) -> None:
            data = src.category_params(cat)
            if isinstance(data, dict):
                keys = [k for k in data if k != "system_info"]
                check["details"] = f"/{cat}/params: ключей {len(keys)}: {', '.join(str(k) for k in keys[:20])[:300]}"
                check["_html"] = json.dumps({k: data[k] for k in keys}, ensure_ascii=False)[:SNAPSHOT_MAX_BYTES]
            elif isinstance(data, list):
                check["details"] = f"/{cat}/params: элементов {len(data)}"
                check["_html"] = json.dumps(data, ensure_ascii=False)[:SNAPSHOT_MAX_BYTES]
            else:
                check["ok"] = False
                check["details"] = f"/{cat}/params: неожиданный тип ответа {type(data).__name__}"
            check["_suffix"] = ".json"

        self.run(names[2], _params, default_hint=f"Ошибка GET /{cat}/params Lolzteam — формат ответа изменился.")

        def _search(check: dict) -> None:
            items = src.search_category(cat, {}, pages=1, max_items=50) or []
            with_price = sum(1 for it in items if it.price)
            with_title = sum(1 for it in items if (it.title or "").strip())
            check["details"] = (
                f"/{cat} страница 1: объявлений {len(items)}, с ценой {with_price}, с заголовком {with_title}"
            )
            if items:
                keys = sorted({str(k) for it in items[:5] for k in (it.attributes or {}).get("raw_keys", [])})
                check["_html"] = json.dumps({"count": len(items), "raw_keys": keys}, ensure_ascii=False)
                check["_suffix"] = ".json"
            else:
                check["hint"] = "Поиск вернул 0 объявлений — возможно, изменился формат ответа (ключ items)."

        self.run(names[3], _search, default_hint=f"Ошибка GET /{cat} Lolzteam (поиск).")


def _settings_summary(ctx: Any) -> dict:
    try:
        s = ctx.settings
        return {
            "golden_key_set": bool(s.funpay.golden_key),
            "token_set": bool(s.lolz.token),
            "proxy_set": bool(s.funpay.proxy or s.lolz.proxy),
            "user_agent_set": bool(s.funpay.user_agent),
        }
    except Exception:
        return {"golden_key_set": False, "token_set": False, "proxy_set": False, "user_agent_set": False}


def run_diagnostics(
    ctx: Any,
    funpay: bool = True,
    lolz: bool = True,
    sample_subcategory_id: int | None = None,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> dict:
    """Прогнать проверки и вернуть отчёт (он же сохраняется в ``DATA_DIR/diagnostics/report.json``).

    Никогда не бросает исключений. ``sample_subcategory_id`` — подкатегория FunPay для проверок списка/формы
    (по умолчанию — первая «Аккаунты» первой игры, где она есть). ``max_seconds`` — общий лимит времени:
    при превышении оставшиеся проверки помечаются ``ok=None``.
    """
    started = time.monotonic()
    runner = _Runner(ctx, max_seconds)
    try:
        if funpay:
            runner.funpay(sample_subcategory_id)
    except Exception as e:
        log.exception("диагностика FunPay: непредвиденная ошибка")
        runner.checks.append(
            {
                "name": "funpay_internal",
                "ok": False,
                "details": f"{e.__class__.__name__}: {e}",
                "elapsed_ms": 0,
                "hint": "Внутренняя ошибка диагностики — отправьте отчёт разработчику.",
                "selectors": {},
                "snapshot": None,
            }
        )
    try:
        if lolz:
            runner.lolz()
    except Exception as e:
        log.exception("диагностика Lolzteam: непредвиденная ошибка")
        runner.checks.append(
            {
                "name": "lolz_internal",
                "ok": False,
                "details": f"{e.__class__.__name__}: {e}",
                "elapsed_ms": 0,
                "hint": "Внутренняя ошибка диагностики — отправьте отчёт разработчику.",
                "selectors": {},
                "snapshot": None,
            }
        )

    checks = runner.checks
    report = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "app_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "settings": _settings_summary(ctx),
        "ok": all(c.get("ok") is not False for c in checks) and any(c.get("ok") is True for c in checks),
        "total_elapsed_ms": int((time.monotonic() - started) * 1000),
        "snapshot_dir": str(runner.dir),
        "checks": checks,
    }
    try:
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        text = sanitize_html(text, runner.secrets)  # на всякий случай: отчёт тоже не должен содержать секретов
        runner.dir.mkdir(parents=True, exist_ok=True)
        (runner.dir / "report.json").write_text(text, encoding="utf-8")
    except Exception as e:
        log.warning("диагностика: не удалось сохранить report.json: %s", e)
    return report


def load_report() -> dict | None:
    """Последний сохранённый отчёт (``DATA_DIR/diagnostics/report.json``) или None."""
    path = _snapshot_dir() / "report.json"
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning("диагностика: не удалось прочитать %s: %s", path, e)
        return None


def summarize(report: dict) -> str:
    """Сводка на русском: ✅/❌/⏭ по каждой проверке, подсказки и финальная строка с просьбой прислать папку."""
    report = report or {}
    lines = [
        f"Диагностика FunPay Searcher v{report.get('app_version', '?')} — {report.get('timestamp', '')}",
        f"Python {report.get('python', '?')}, {report.get('platform', '?')}",
    ]
    s = report.get("settings") or {}
    lines.append(
        "Настройки: golden_key {}, токен Lolz {}, прокси {}, User-Agent {}".format(
            "задан" if s.get("golden_key_set") else "НЕ задан",
            "задан" if s.get("token_set") else "НЕ задан",
            "задан" if s.get("proxy_set") else "нет",
            "задан" if s.get("user_agent_set") else "по умолчанию",
        )
    )
    lines.append("")
    for c in report.get("checks") or []:
        ok = c.get("ok")
        mark = "✅" if ok is True else "❌" if ok is False else "⏭"
        lines.append(f"{mark} {c.get('name')} ({c.get('elapsed_ms', 0)} мс): {c.get('details', '')}")
        if ok is False:
            broken = [sel for sel, n in (c.get("selectors") or {}).items() if not n]
            if broken:
                lines.append("   селекторы без совпадений: " + ", ".join(broken))
        if c.get("hint"):
            lines.append(f"   → {c['hint']}")
    lines.append("")
    total_ok = sum(1 for c in report.get("checks") or [] if c.get("ok") is True)
    total_bad = sum(1 for c in report.get("checks") or [] if c.get("ok") is False)
    total_skip = sum(1 for c in report.get("checks") or [] if c.get("ok") is None)
    lines.append(
        f"Итого: успешно {total_ok}, с ошибками {total_bad}, пропущено {total_skip}; "
        f"время {report.get('total_elapsed_ms', 0)} мс."
    )
    lines.append(f"Снимки страниц и отчёт: {report.get('snapshot_dir', str(_snapshot_dir()))}")
    lines.append(FINAL_LINE)
    return "\n".join(lines)


__all__ = ["DATA_DIR", "FINAL_LINE", "load_report", "run_diagnostics", "sanitize_html", "summarize"]
