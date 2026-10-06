"""Источник FunPay: разбор HTML-страниц (cookie ``golden_key``) и управление нашими лотами.

Официального API у FunPay нет, поэтому модуль работает как браузер:
- GET ``https://funpay.com/`` — проверка авторизации, csrf-токен, список категорий;
- GET ``/lots/{id}/`` — публичные предложения подкатегории (все на одной странице);
- GET ``/lots/offer?id=N`` — страница лота (проверка доступности);
- GET ``/users/{id}/`` — страница продавца;
- GET ``/lots/offerEdit?node=N[&offer=M]`` (XHR, JSON ``{"html": ...}``) — форма лота;
- POST ``/lots/offerSave`` — создание/редактирование лота;
- GET ``/lots/{id}/trade`` — наши предложения в подкатегории.

Разметка FunPay описана в докстрингах соответствующих методов; каждый ``find`` защищён —
при неожиданной вёрстке выбрасывается :class:`SourceError` с понятным сообщением.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..models import Listing, Profile, SourceFunPayConfig
from ..settings import FunPaySettings
from .base import AuthError, BaseSource, SourceError

BASE_URL = "https://funpay.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9"
LOGIN_TTL = 40 * 60  # сек; FunPay рекомендует обновлять PHPSESSID раз в 40–60 минут

_WS_RE = re.compile(r"[\s    ']+")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_INT_RE = re.compile(r"\d+")
_USER_ID_RE = re.compile(r"/users/(\d+)")
_NODE_ID_RE = re.compile(r"/(?:lots|chips)/(\d+)/")
_SPACES_RE = re.compile(r"\s+")

_FRIENDLY_FIELDS = {
    "title_ru": "fields[summary][ru]",
    "title_en": "fields[summary][en]",
    "description_ru": "fields[desc][ru]",
    "description_en": "fields[desc][en]",
    "price": "price",
    "amount": "amount",
}
_REQUIRED_FORM_KEYS = (
    "fields[summary][ru]", "fields[summary][en]", "fields[desc][ru]", "fields[desc][en]",
    "price", "amount",
)


# ---------------------------------------------------------------------------
# Вспомогательные функции (без состояния, покрыты тестами)
# ---------------------------------------------------------------------------
def price_from_text(text: Optional[str]) -> Optional[float]:
    """«15 000 ₽» -> 15000.0, «1 234,56 ₽» -> 1234.56, «$12.5» -> 12.5. None — число не найдено."""
    if not text:
        return None
    compact = _WS_RE.sub("", str(text))
    m = _NUM_RE.search(compact)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def currency_from_text(text: Optional[str], default: str = "RUB") -> str:
    """Определить валюту по символу/коду в тексте цены (₽/$/€/₴). По умолчанию RUB."""
    t = (text or "").lower()
    if "₽" in t or "руб" in t or "rub" in t:
        return "RUB"
    if "$" in t or "usd" in t:
        return "USD"
    if "€" in t or "eur" in t:
        return "EUR"
    if "₴" in t or "uah" in t or "грн" in t:
        return "UAH"
    return default


def _norm(text: Optional[str]) -> str:
    """Нормализация для сравнения: нижний регистр, схлопнутые пробелы."""
    return _SPACES_RE.sub(" ", (text or "").replace("\xa0", " ")).strip().lower()


def _text(el: Optional[Tag]) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _block_text(el: Optional[Tag]) -> str:
    """Текст блока с сохранением переносов строк (<br> и <p> -> \\n)."""
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for p in el.find_all(["p", "li"]):
        p.append("\n")
    lines = [ln.strip() for ln in el.get_text().replace("\xa0", " ").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _int_from_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = _INT_RE.search(_WS_RE.sub("", text))
    return int(m.group(0)) if m else None


def _fmt_number(value: Any) -> str:
    """Число для формы FunPay: 15000.0 -> «15000», 15000.5 -> «15000.5»."""
    if isinstance(value, bool):
        return "on" if value else ""
    if isinstance(value, (int, float)):
        f = float(value)
        return str(int(f)) if f.is_integer() else repr(f)
    return "" if value is None else str(value)


def _absolute(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return urljoin(BASE_URL + "/", url)


def _lot_id_from_href(href: Optional[str]) -> Optional[str]:
    """ID лота из ссылки: ``/lots/offer?id=123`` или ``/lots/offerEdit?node=1&offer=123``."""
    if not href:
        return None
    qs = parse_qs(urlparse(href).query)
    for key in ("id", "offer"):
        vals = qs.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return None


def parse_lot_form_html(html: str) -> dict:
    """Разобрать HTML формы лота (из ответа ``/lots/offerEdit``) в ``{"fields": {...}, "schema": [...]}``.

    ``fields`` — то, что отправит браузер при submit (чекбоксы — только отмеченные),
    ``schema`` — описание всех контролов для UI: name, type, label, value, options, required.
    """
    soup = BeautifulSoup(html or "", "lxml")
    form = soup.find("form") or soup
    fields: dict[str, str] = {}
    schema: list[dict] = []
    radios: dict[str, dict] = {}

    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        group = el.find_parent(class_="form-group")
        label_el = group.find("label") if group is not None else None
        label = _text(label_el) or (el.get("placeholder") or "").strip() or name
        required = el.has_attr("required")
        tag = el.name

        if tag == "input":
            itype = (el.get("type") or "text").lower()
            if itype in ("submit", "button", "image", "reset", "file"):
                continue
            if itype == "checkbox":
                value = el.get("value") or "on"
                checked = el.has_attr("checked")
                if checked:
                    fields[name] = value
                schema.append({"name": name, "type": "checkbox", "label": label,
                               "value": value if checked else "", "options": [], "required": required})
                continue
            if itype == "radio":
                value = el.get("value") or "on"
                entry = radios.get(name)
                if entry is None:
                    entry = {"name": name, "type": "radio", "label": label, "value": "",
                             "options": [], "required": required}
                    radios[name] = entry
                    schema.append(entry)
                entry["options"].append({"value": value, "label": label})
                if el.has_attr("checked"):
                    entry["value"] = value
                    fields[name] = value
                continue
            value = el.get("value") or ""
            stype = itype if itype in ("hidden", "number", "text") else "text"
            fields[name] = value
            schema.append({"name": name, "type": stype, "label": label, "value": value,
                           "options": [], "required": required})
        elif tag == "textarea":
            value = el.get_text()
            if value.startswith("\n"):
                value = value[1:]
            fields[name] = value
            schema.append({"name": name, "type": "textarea", "label": label, "value": value,
                           "options": [], "required": required})
        elif tag == "select":
            options = []
            selected = None
            for opt in el.find_all("option"):
                ov = opt.get("value")
                if ov is None:
                    ov = opt.get_text(strip=True)
                options.append({"value": ov, "label": opt.get_text(strip=True)})
                if opt.has_attr("selected") and selected is None:
                    selected = ov
            if selected is None:
                selected = options[0]["value"] if options else ""
            fields[name] = selected
            schema.append({"name": name, "type": "select", "label": label, "value": selected,
                           "options": options, "required": required})
    return {"fields": fields, "schema": schema}


def _checkbox_key(keys, base: str) -> str:
    """Имя чекбокса в форме: ``active`` или ``active[]`` (FunPay менял суффикс)."""
    for candidate in (base, base + "[]"):
        if candidate in keys:
            return candidate
    for k in keys:
        if k.startswith(base):
            return k
    return base


# ---------------------------------------------------------------------------
# Источник
# ---------------------------------------------------------------------------
class FunPaySource(BaseSource):
    """Источник объявлений FunPay + публикация/редактирование наших лотов.

    :param settings: настройки FunPay (golden_key, user_agent, proxy, request_delay, timeout).
    :param storage: необязательное хранилище с методом ``log(kind, message, level, data)`` для журнала событий.
    :param logger: логгер (по умолчанию ``logging.getLogger("funpay")``).
    :param transport: httpx-транспорт (для тестов — ``httpx.MockTransport``).
    """

    name = "funpay"
    retries = 2
    retry_backoff = 1.0  # сек, умножается на номер попытки

    def __init__(self, settings: FunPaySettings, storage: Any = None, logger: Optional[logging.Logger] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.settings = settings
        self.storage = storage
        self.log = logger or logging.getLogger("funpay")
        self.user_agent = settings.user_agent or DEFAULT_USER_AGENT
        self.phpsessid: Optional[str] = None
        self.csrf_token: Optional[str] = None
        self.user_id: Optional[int] = None
        self.username: Optional[str] = None
        self.app_data: dict = {}
        self._categories: list[dict] = []
        self._logged_in_at: float = 0.0
        self._last_request_at: float = 0.0
        self._lock = threading.RLock()
        client_kwargs: dict[str, Any] = dict(
            base_url=BASE_URL,
            timeout=httpx.Timeout(settings.timeout or 20.0),
            follow_redirects=False,
        )
        if transport is not None:
            client_kwargs["transport"] = transport
        elif settings.proxy:
            client_kwargs["proxy"] = settings.proxy
        self._client = httpx.Client(**client_kwargs)

    # ------------------------------------------------------------------ infra
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def _event(self, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        """Запись в журнал приложения (если передано хранилище) + в лог."""
        getattr(self.log, level if level in ("debug", "info", "warning", "error") else "info")(message)
        if self.storage is not None and hasattr(self.storage, "log"):
            try:
                self.storage.log("funpay", message, level, data)
            except Exception:  # noqa: BLE001
                self.log.debug("не удалось записать событие в хранилище", exc_info=True)

    def _cookie_header(self) -> str:
        cookie = f"golden_key={self.settings.golden_key}"
        if self.phpsessid:
            cookie += f"; PHPSESSID={self.phpsessid}"
        return cookie

    def _throttle(self) -> None:
        delay = float(self.settings.request_delay or 0)
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _request(self, method: str, path: str, *, params: Optional[dict] = None, data: Optional[dict] = None,
                 headers: Optional[dict] = None, ajax: bool = False) -> httpx.Response:
        """Запрос к FunPay с cookie/заголовками, паузой, повторами при сетевых ошибках и 5xx.

        403 -> :class:`AuthError`. Редиректы не выполняются (их интерпретирует вызывающий код).
        """
        hdrs = {
            "user-agent": self.user_agent,
            "accept-language": ACCEPT_LANGUAGE,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "cookie": self._cookie_header(),
        }
        if ajax:
            hdrs["accept"] = "*/*"
            hdrs["x-requested-with"] = "XMLHttpRequest"
        if headers:
            hdrs.update(headers)
        last_error: Optional[Exception] = None
        with self._lock:
            for attempt in range(1, self.retries + 2):
                self._throttle()
                try:
                    self._last_request_at = time.monotonic()
                    response = self._client.request(method, path, params=params, data=data, headers=hdrs)
                except httpx.TransportError as e:
                    last_error = e
                    self.log.warning("FunPay: сетевая ошибка (%s) при %s %s, попытка %d/%d",
                                     e.__class__.__name__, method, path, attempt, self.retries + 1)
                else:
                    sid = response.cookies.get("PHPSESSID")
                    if sid:
                        self.phpsessid = sid
                    if response.status_code == 403:
                        raise AuthError("FunPay вернул 403: golden_key невалиден или истёк "
                                        "(либо запрос заблокирован — проверьте user_agent/прокси)")
                    if response.status_code >= 500:
                        last_error = SourceError(f"FunPay вернул {response.status_code} при {method} {path}")
                        self.log.warning("FunPay: %s, попытка %d/%d", last_error, attempt, self.retries + 1)
                    else:
                        return response
                if attempt <= self.retries and self.retry_backoff > 0:
                    time.sleep(self.retry_backoff * attempt)
        if isinstance(last_error, SourceError):
            raise last_error
        raise SourceError(f"Ошибка сети при обращении к FunPay ({method} {path}): {last_error}") from last_error

    @staticmethod
    def _soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "lxml")

    @staticmethod
    def _app_data(soup: BeautifulSoup) -> dict:
        body = soup.find("body")
        raw = body.get("data-app-data") if body is not None else None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ auth
    def login(self) -> dict:
        """GET https://funpay.com/ — проверить golden_key, получить csrf-токен, id, имя и категории."""
        if not (self.settings.golden_key or "").strip():
            raise AuthError("golden_key не задан — укажите cookie golden_key в настройках")
        response = self._request("GET", "/")
        if 300 <= response.status_code < 400:
            raise SourceError(f"FunPay перенаправил главную страницу на {response.headers.get('location')}")
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} на главной странице")
        soup = self._soup(response.text)
        name_el = soup.select_one("div.user-link-name")
        if name_el is None or not _text(name_el):
            raise AuthError("golden_key невалиден или истёк")
        self.username = _text(name_el)
        self.app_data = self._app_data(soup)
        self.csrf_token = self.app_data.get("csrf-token") or self.csrf_token
        try:
            self.user_id = int(self.app_data.get("userId")) if self.app_data.get("userId") is not None else None
        except (TypeError, ValueError):
            self.user_id = None
        if not self.csrf_token:
            self.log.warning("FunPay: csrf-token не найден в data-app-data")
        self._categories = self._parse_categories(soup)
        self._logged_in_at = time.monotonic()
        self.log.info("FunPay: авторизован как %s (id=%s), категорий: %d",
                      self.username, self.user_id, len(self._categories))
        return {"username": self.username, "user_id": self.user_id, "csrf_token": self.csrf_token}

    def ensure(self, force: bool = False) -> None:
        """Авторизоваться, если ещё не сделали этого (или сессия устарела)."""
        if force or not self._logged_in_at or time.monotonic() - self._logged_in_at > LOGIN_TTL:
            self.login()

    def check_auth(self) -> dict:
        try:
            self.login()
            return {"ok": True, "username": self.username, "user_id": self.user_id, "error": None}
        except SourceError as e:  # AuthError — подкласс SourceError
            self._event(f"FunPay: проверка авторизации не пройдена: {e}", "warning")
            return {"ok": False, "username": None, "user_id": None, "error": str(e)}

    # ------------------------------------------------------------------ категории
    def _parse_categories(self, soup: BeautifulSoup) -> list[dict]:
        """``div.promo-game-list`` (берём самый полный) -> ``div.promo-game-item`` -> игра + подкатегории."""
        lists = soup.select("div.promo-game-list")
        if not lists:
            self.log.warning("FunPay: на главной странице нет списка игр (promo-game-list)")
            return []
        table = max(lists, key=lambda t: len(t.select("div.promo-game-item")))
        result: list[dict] = []
        seen: set[int] = set()
        for item in table.select("div.promo-game-item"):
            title = item.select_one("div.game-title")
            if title is None:
                continue
            try:
                game_id = int(title.get("data-id"))
            except (TypeError, ValueError):
                continue
            link = title.find("a")
            game_name = _text(link) or _text(title)
            subcats: list[dict] = []
            for a in item.select("ul li a[href], li a[href]"):
                href = a.get("href") or ""
                m = _NODE_ID_RE.search(href)
                if not m:
                    continue
                subcats.append({
                    "id": int(m.group(1)),
                    "name": _text(a),
                    "type": "currency" if "/chips/" in href else "common",
                })
            if game_id in seen:
                continue
            seen.add(game_id)
            result.append({"id": game_id, "name": game_name, "subcategories": subcats})
        return result

    def categories(self) -> list[dict]:
        """Список игр с подкатегориями: ``[{id, name, subcategories: [{id, name, type}]}]``."""
        if not self._categories:
            self.ensure()
        return self._categories

    def find_subcategory(self, game_query: str, subcategory_query: Optional[str] = "Аккаунты") -> Optional[int]:
        """Найти id подкатегории по названию игры (точное -> по началу -> по подстроке) и подкатегории."""
        q = _norm(game_query)
        if not q:
            return None
        cats = self.categories()
        game = None
        exact = [c for c in cats if _norm(c["name"]) == q]
        if exact:
            game = exact[0]
        else:
            starts = [c for c in cats if _norm(c["name"]).startswith(q)]
            if starts:
                game = min(starts, key=lambda c: len(c["name"]))
            else:
                contains = [c for c in cats if q in _norm(c["name"])]
                if contains:
                    game = min(contains, key=lambda c: len(c["name"]))
        if game is None:
            return None
        subs = game.get("subcategories") or []
        sq = _norm(subcategory_query)
        if not sq:
            return subs[0]["id"] if subs else None
        for s in subs:
            if _norm(s["name"]) == sq:
                return s["id"]
        for s in subs:
            if sq in _norm(s["name"]):
                return s["id"]
        return None

    def resolve_subcategory_ids(self, cfg: SourceFunPayConfig) -> list[int]:
        """Подкатегории для поиска: ``subcategory_ids`` -> ``subcategory_id`` -> ``game_query``/``subcategory_query``."""
        if cfg.subcategory_ids:
            return [int(i) for i in cfg.subcategory_ids]
        if cfg.subcategory_id:
            return [int(cfg.subcategory_id)]
        if cfg.game_query:
            sid = self.find_subcategory(cfg.game_query, cfg.subcategory_query or "")
            if sid is None:
                raise SourceError(f"FunPay: не найдена подкатегория для игры «{cfg.game_query}» "
                                  f"/ «{cfg.subcategory_query}»")
            return [sid]
        raise SourceError("В настройках профиля не указана подкатегория FunPay "
                          "(subcategory_id, subcategory_ids или game_query)")

    # ------------------------------------------------------------------ разбор лотов
    def _parse_seller(self, container: Optional[Tag]) -> dict:
        """Продавец из блока ``.media-user``: имя, ссылка, id, отзывы."""
        info: dict[str, Any] = {"name": None, "url": None, "id": None, "reviews": None, "online": None}
        if container is None:
            return info
        name_el = container.select_one(".media-user-name")
        link = None
        if name_el is not None:
            link = name_el.find("a", href=True) or name_el.find(attrs={"data-href": True})
            info["name"] = _text(name_el) or None
        if link is None:
            link = (container.find("a", href=_USER_ID_RE)
                    or container.find(attrs={"data-href": _USER_ID_RE}))
        if link is not None:
            url = link.get("href") or link.get("data-href")
            info["url"] = _absolute(url)
            m = _USER_ID_RE.search(url or "")
            info["id"] = m.group(1) if m else None
            if not info["name"]:
                info["name"] = _text(link) or None
        reviews_el = container.select_one(".media-user-reviews")
        count_el = container.select_one(".rating-mini-count, .rating-full-count")
        if count_el is not None:
            info["reviews"] = _int_from_text(_text(count_el))
        elif reviews_el is not None:
            t = _text(reviews_el)
            info["reviews"] = 0 if "нет отзывов" in t.lower() else _int_from_text(t)
        media = container.select_one(".media-user")
        if media is not None:
            info["online"] = "online" in (media.get("class") or [])
        return info

    def _parse_tc_item(self, a: Tag, subcategory_id: Optional[int]) -> Optional[Listing]:
        """Разобрать виджет лота ``a.tc-item`` (списки /lots/{id}/, /lots/{id}/trade, /users/{id}/)."""
        href = a.get("href") or ""
        lot_id = _lot_id_from_href(href)
        if not lot_id:
            self.log.debug("FunPay: tc-item без id лота: %s", href)
            return None
        price_el = a.select_one(".tc-price")
        price_text = _text(price_el)
        price = None
        if price_el is not None and price_el.get("data-s"):
            price = price_from_text(price_el.get("data-s"))
        if price is None:
            price = price_from_text(price_text)
        if price is None:
            self.log.warning("FunPay: у лота %s не удалось определить цену, пропускаем", lot_id)
            return None

        title = _text(a.select_one(".tc-desc-text")) or _text(a.select_one(".tc-desc"))
        server = _text(a.select_one(".tc-server")) or None
        side = _text(a.select_one(".tc-side")) or None
        amount = _text(a.select_one(".tc-amount")) or None
        seller = self._parse_seller(a.select_one(".tc-user"))

        online: Optional[bool]
        if a.has_attr("data-online"):
            online = str(a.get("data-online")).strip() in ("1", "true")
        else:
            online = seller["online"]

        data_f = {k[len("data-f-"):]: v for k, v in a.attrs.items() if k.startswith("data-f-")}
        classes = a.get("class") or []
        attributes: dict[str, Any] = {
            "server": server,
            "side": side,
            "seller_reviews": seller["reviews"],
            "data_f": data_f,
            "subcategory_id": subcategory_id,
        }
        if amount:
            attributes["amount"] = _int_from_text(amount) if _int_from_text(amount) is not None else amount
        info = _text(a.select_one(".media-user-info"))
        if info:
            attributes["seller_info"] = info
        if "warning" in classes:
            attributes["active"] = False
        elif "offer=" in href:
            attributes["active"] = True

        return Listing(
            source="funpay",
            source_id=str(lot_id),
            url=f"{BASE_URL}/lots/offer?id={lot_id}",
            title=title,
            price=price,
            currency=currency_from_text(price_text),
            seller_name=seller["name"],
            seller_url=seller["url"],
            seller_id=seller["id"],
            region=server,
            attributes=attributes,
            online=online,
        )

    def _parse_tc_items(self, root: Tag, subcategory_id: Optional[int], max_items: Optional[int] = None) -> list[Listing]:
        result: list[Listing] = []
        for a in root.select("a.tc-item"):
            listing = self._parse_tc_item(a, subcategory_id)
            if listing is not None:
                result.append(listing)
                if max_items and len(result) >= max_items:
                    break
        return result

    def _fetch_lots_page(self, subcategory_id: int, params: Optional[dict] = None, suffix: str = "") -> BeautifulSoup:
        path = f"/lots/{int(subcategory_id)}/{suffix}"
        response = self._request("GET", path, params=params or None)
        if response.status_code == 404:
            raise SourceError(f"FunPay: подкатегория {subcategory_id} не найдена (404)")
        if 300 <= response.status_code < 400:
            raise SourceError(f"FunPay перенаправил {path} на {response.headers.get('location')}")
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} для {path}")
        return self._soup(response.text)

    def list_lots(self, subcategory_id: int, extra_query: Optional[dict] = None, max_items: int = 500) -> list[Listing]:
        """Публичные предложения подкатегории ``/lots/{id}/`` (все на одной странице, без пагинации)."""
        soup = self._fetch_lots_page(subcategory_id, extra_query)
        items = self._parse_tc_items(soup, int(subcategory_id), max_items)
        if not items and soup.select_one("a.tc-item") is None and soup.select_one(".showcase, .tc") is None:
            raise SourceError(f"FunPay: страница /lots/{subcategory_id}/ не похожа на список предложений")
        self.log.info("FunPay: подкатегория %s — %d предложений", subcategory_id, len(items))
        return items

    def list_filters(self, subcategory_id: int) -> list[dict]:
        """Фильтры страницы списка (``.showcase-filters``): ``[{name, label, type, options:[{value,label}]}]``."""
        soup = self._fetch_lots_page(subcategory_id)
        result: list[dict] = []
        seen: set[str] = set()
        for root in soup.select(".showcase-filters, form.showcase-filters"):
            for el in root.find_all(["select", "input"]):
                name = el.get("name")
                if not name or name in seen:
                    continue
                group = el.find_parent(class_="form-group")
                label_el = group.find("label") if group is not None else None
                if el.name == "select":
                    options = [{"value": o.get("value") if o.get("value") is not None else o.get_text(strip=True),
                                "label": o.get_text(strip=True)} for o in el.find_all("option")]
                    empty = next((o["label"] for o in options if o["value"] == ""), None)
                    label = _text(label_el) or empty or name
                    result.append({"name": name, "label": label, "type": "select", "options": options})
                else:
                    itype = (el.get("type") or "text").lower()
                    if itype in ("submit", "button", "hidden", "image", "reset"):
                        continue
                    if itype == "checkbox":
                        label = _text(el.find_parent("label")) or _text(label_el) or name
                        result.append({"name": name, "label": label, "type": "checkbox",
                                       "options": [{"value": el.get("value") or "1", "label": label}]})
                    else:
                        label = _text(label_el) or (el.get("placeholder") or "").strip() or name
                        result.append({"name": name, "label": label, "type": "text", "options": []})
                seen.add(name)
        return result

    # ------------------------------------------------------------------ поиск
    def search(self, profile: Profile, limit: Optional[int] = None) -> list[Listing]:
        cfg = profile.funpay()
        if not cfg.enabled:
            self.log.info("FunPay: источник выключен в профиле %s", profile.id)
            return []
        ids = self.resolve_subcategory_ids(cfg)
        max_items = cfg.max_items or 500
        if limit:
            max_items = min(max_items, int(limit))
        filters = [_norm(s) for s in (cfg.server_filter or []) if _norm(s)]
        result: list[Listing] = []
        seen: set[str] = set()
        for sid in ids:
            try:
                items = self.list_lots(sid, extra_query=cfg.extra_query or None, max_items=max_items)
            except SourceError as e:
                self._event(f"FunPay: ошибка загрузки подкатегории {sid}: {e}", "error",
                            {"profile_id": profile.id, "subcategory_id": sid})
                if len(ids) == 1:
                    raise
                continue
            for item in items:
                if filters:
                    hay = _norm(f"{item.region or ''} {item.attributes.get('server') or ''}")
                    if not any(f in hay for f in filters):
                        continue
                if item.source_id in seen:
                    continue
                seen.add(item.source_id)
                item.game = profile.game
                result.append(item)
                if limit and len(result) >= limit:
                    return result
        return result

    # ------------------------------------------------------------------ лот / доступность
    def _fetch_lot_page(self, source_id: str) -> Optional[BeautifulSoup]:
        """Страница лота. None — лот удалён/продан (404, редирект на категорию, нет ``.param-list``)."""
        lot_id = str(source_id).strip()
        if not lot_id:
            raise SourceError("FunPay: пустой id лота")
        response = self._request("GET", "/lots/offer", params={"id": lot_id})
        if response.status_code == 404:
            self.log.info("FunPay: лот %s не найден (404)", lot_id)
            return None
        if 300 <= response.status_code < 400:
            self.log.info("FunPay: лот %s перенаправлен на %s — считаем снятым",
                          lot_id, response.headers.get("location"))
            return None
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} для лота {lot_id}")
        soup = self._soup(response.text)
        if soup.select_one(".param-list") is None:
            page_text = _text(soup.select_one("h1")) or _text(soup.select_one(".page-content"))
            self.log.info("FunPay: лот %s без описания (%s) — считаем снятым", lot_id, page_text[:80])
            return None
        return soup

    def get_listing(self, source_id: str) -> Optional[Listing]:
        """Страница лота ``/lots/offer?id=N``: ``div.param-list > div.param-item (h5 + div)``."""
        soup = self._fetch_lot_page(source_id)
        if soup is None:
            return None
        lot_id = str(source_id).strip()
        attributes: dict[str, Any] = {}
        title = ""
        description = ""
        price: Optional[float] = None
        price_text = ""
        seller_block: Optional[Tag] = None

        for item in soup.select(".param-item"):
            h5 = item.find("h5")
            if h5 is None:
                continue
            label = _text(h5)
            key = label.lower()
            value_el = h5.find_next_sibling()
            if item.select_one(".media-user") is not None:
                seller_block = item
                continue
            if "цена" in key or "price" in key:
                price_text = _text(value_el)
                price = price_from_text(price_text)
                attributes[key] = price_text
                continue
            value = _block_text(value_el)
            attributes[key] = value
            if key in ("краткое описание", "short description"):
                title = value.replace("\n", " ").strip()
            elif key in ("подробное описание", "detailed description", "full description"):
                description = value

        if price is None:
            fallback = soup.select_one("[data-s], .tc-price, .payment-value")
            if fallback is not None:
                price_text = _text(fallback)
                price = price_from_text(fallback.get("data-s") or price_text)
        if price is None:
            raise SourceError(f"FunPay: не удалось определить цену лота {lot_id}")

        seller = self._parse_seller(seller_block or soup)
        node_id = self._lot_page_node_id(soup)
        attributes["subcategory_id"] = node_id
        attributes["seller_reviews"] = seller["reviews"]
        if not title:
            title = _text(soup.select_one("h1"))

        return Listing(
            source="funpay",
            source_id=lot_id,
            url=f"{BASE_URL}/lots/offer?id={lot_id}",
            title=title,
            description=description,
            price=price,
            currency=currency_from_text(price_text),
            seller_name=seller["name"],
            seller_url=seller["url"],
            seller_id=seller["id"],
            region=attributes.get("сервер") or attributes.get("регион") or attributes.get("server") or None,
            attributes=attributes,
            online=seller["online"],
        )

    @staticmethod
    def _lot_page_node_id(soup: BeautifulSoup) -> Optional[int]:
        """ID подкатегории со страницы лота: back-link, хлебные крошки, ``input[name=node_id]``."""
        candidates: list[Optional[str]] = []
        for a in soup.select("a.back-link[href], .breadcrumb a[href], .page-header a[href]"):
            candidates.append(a.get("href"))
        inp = soup.select_one("input[name=node_id]")
        if inp is not None and inp.get("value"):
            return int(inp.get("value")) if str(inp.get("value")).isdigit() else None
        for a in soup.select("a[href*='/lots/']"):
            candidates.append(a.get("href"))
        for href in candidates:
            m = _NODE_ID_RE.search(href or "")
            if m:
                return int(m.group(1))
        return None

    def is_available(self, source_id: str) -> Optional[bool]:
        try:
            return self._fetch_lot_page(source_id) is not None
        except SourceError as e:
            self.log.warning("FunPay: не удалось проверить лот %s: %s", source_id, e)
            return None

    # ------------------------------------------------------------------ продавец
    def get_seller(self, seller_id: int | str) -> Optional[dict]:
        """Страница продавца ``/users/{id}/`` -> ``{id, name, reviews, online, lots: [Listing]}``. None — 404."""
        sid = str(seller_id).strip()
        response = self._request("GET", f"/users/{sid}/")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} для продавца {sid}")
        soup = self._soup(response.text)
        profile = soup.select_one(".profile") or soup
        name = (_text(profile.select_one("h1 .mr4")) or _text(profile.select_one("h1"))
                or _text(soup.select_one("span.mr4")))
        if not name:
            raise SourceError(f"FunPay: не удалось разобрать страницу продавца {sid}")
        reviews = None
        count_el = profile.select_one(".rating-full-count, .rating-mini-count")
        if count_el is not None:
            reviews = _int_from_text(_text(count_el))
        status = _text(profile.select_one(".media-user-status"))
        media = profile.select_one(".media-user")
        online = ("онлайн" in status.lower()) if status else (
            "online" in (media.get("class") or []) if media is not None else None)

        lots: list[Listing] = []
        blocks = soup.select(".offer-list-title-container")
        if blocks:
            for container in blocks:
                link = container.select_one("h3 a[href]") or container.find("a", href=True)
                m = _NODE_ID_RE.search(link.get("href") or "") if link is not None else None
                node_id = int(m.group(1)) if m else None
                block = container.find_parent(class_="offer") or container.parent
                for a in (block.select("a.tc-item") if block is not None else []):
                    listing = self._parse_tc_item(a, node_id)
                    if listing is not None:
                        listing.attributes["subcategory_name"] = _text(link) if link is not None else None
                        lots.append(listing)
        else:
            lots = self._parse_tc_items(soup, None)
        return {"id": sid, "name": name, "reviews": reviews, "online": online, "lots": lots}

    # ------------------------------------------------------------------ наши лоты
    def list_my_lots(self, subcategory_id: int) -> list[Listing]:
        """Наши предложения в подкатегории ``/lots/{id}/trade`` (ссылки вида offerEdit?offer=N)."""
        self.ensure()
        soup = self._fetch_lots_page(subcategory_id, suffix="trade")
        return self._parse_tc_items(soup, int(subcategory_id))

    def get_lot_form(self, subcategory_id: int, lot_id: Optional[int | str] = None) -> dict:
        """Форма лота ``/lots/offerEdit?node=N[&offer=M]`` (XHR, JSON ``{"html": ...}``).

        Возвращает ``{"fields": {name: value}, "schema": [{name, type, label, value, options, required}]}``.
        Гарантированно присутствуют ключи csrf_token, offer_id, node_id, location,
        fields[summary][ru|en], fields[desc][ru|en], price, amount, active, deactivate_after_sale.
        """
        self.ensure()
        params: dict[str, Any] = {"node": int(subcategory_id)}
        if lot_id is not None:
            params["offer"] = str(lot_id)
        response = self._request("GET", "/lots/offerEdit", params=params, ajax=True)
        if response.status_code == 404:
            raise SourceError(f"FunPay: форма лота не найдена (node={subcategory_id}, offer={lot_id})")
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} при запросе формы лота")
        try:
            payload = response.json()
        except ValueError:
            raise SourceError("FunPay вернул не JSON при запросе формы лота — "
                              "возможно, golden_key истёк или изменилась разметка") from None
        if not isinstance(payload, dict):
            raise SourceError("FunPay вернул неожиданный ответ при запросе формы лота")
        if payload.get("error"):
            raise SourceError(f"FunPay: ошибка формы лота: {payload.get('error')}")
        html = payload.get("html")
        if not html:
            raise SourceError("FunPay: в ответе формы лота нет html")
        form = parse_lot_form_html(html)
        fields = form["fields"]
        if not fields.get("csrf_token"):
            if not self.csrf_token:
                raise SourceError("FunPay: не найден csrf_token ни в форме, ни на главной странице")
            fields["csrf_token"] = self.csrf_token
        fields.setdefault("offer_id", str(lot_id) if lot_id is not None else "0")
        if not fields.get("offer_id"):
            fields["offer_id"] = str(lot_id) if lot_id is not None else "0"
        fields.setdefault("node_id", str(int(subcategory_id)))
        if not fields.get("node_id"):
            fields["node_id"] = str(int(subcategory_id))
        fields["location"] = fields.get("location") or "trade"
        for key in _REQUIRED_FORM_KEYS:
            fields.setdefault(key, "")
        for base in ("active", "deactivate_after_sale"):
            key = _checkbox_key(list(fields) + [s["name"] for s in form["schema"]], base)
            fields.setdefault(key, "")
        return form

    def _apply_lot_changes(self, fields: dict[str, str], changes: dict[str, Any]) -> dict[str, str]:
        """Наложить изменения на поля формы: дружелюбные ключи, bool-чекбоксы, сырые имена полей."""
        for key, value in changes.items():
            if value is None and key not in ("fields",):
                continue
            if key in ("active", "deactivate_after_sale"):
                fields[_checkbox_key(fields, key)] = "on" if value else ""
            elif key in ("fields", "extra_fields"):
                for k, v in (value or {}).items():
                    fields[str(k)] = _fmt_number(v)
            elif key in _FRIENDLY_FIELDS:
                fields[_FRIENDLY_FIELDS[key]] = _fmt_number(value)
            else:
                fields[str(key)] = _fmt_number(value)
        return fields

    def _save_lot_form(self, fields: dict[str, str]) -> dict:
        """POST ``/lots/offerSave``. Успех — ``{"done": true}``; ошибка — ``{"error": "..."}`` -> SourceError."""
        response = self._request("POST", "/lots/offerSave", data=fields, ajax=True,
                                 headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"})
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} при сохранении лота")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if payload is None:
            soup = self._soup(response.text)
            errors = [_text(e) for e in soup.select(".alert-danger, .help-block, .has-error, .error") if _text(e)]
            if errors:
                raise SourceError("FunPay отклонил лот: " + "; ".join(errors))
            raise SourceError("FunPay вернул неожиданный (не JSON) ответ при сохранении лота")
        if not isinstance(payload, dict):
            raise SourceError("FunPay вернул неожиданный ответ при сохранении лота")
        if payload.get("error"):
            msg = str(payload.get("error"))
            errs = payload.get("errors")
            if isinstance(errs, dict):
                msg += " (" + "; ".join(f"{k}: {v}" for k, v in errs.items()) + ")"
            elif isinstance(errs, list):
                msg += " (" + "; ".join(str(v) for v in errs) + ")"
            raise SourceError(f"FunPay отклонил лот: {msg}")
        if payload.get("done") is False:
            raise SourceError("FunPay не сохранил лот (done=false)")
        return payload

    @staticmethod
    def _lot_url(lot_id: Optional[int | str]) -> Optional[str]:
        return f"{BASE_URL}/lots/offer?id={lot_id}" if lot_id else None

    def _find_lot_id_by_title(self, subcategory_id: int, title_ru: str) -> Optional[int]:
        """Найти id свежесозданного лота по заголовку среди наших лотов (берём самый новый = больший id)."""
        wanted = _norm(title_ru)
        best: Optional[int] = None
        for lot in self.list_my_lots(subcategory_id):
            if _norm(lot.title) != wanted:
                continue
            try:
                lid = int(lot.source_id)
            except ValueError:
                continue
            if best is None or lid > best:
                best = lid
        return best

    def create_lot(self, subcategory_id: int, title_ru: str, title_en: str = "", description_ru: str = "",
                   description_en: str = "", price: Optional[float] = None, amount: int = 1, active: bool = True,
                   deactivate_after_sale: bool = True, extra_fields: Optional[dict] = None) -> dict:
        """Создать лот: форма ``offerEdit?node=N`` + наши значения -> POST ``offerSave``.

        ``extra_fields`` накладываются последними (например ``{"fields[server]": "101"}`` из шаблона профиля).
        Возвращает ``{"lot_id": int|None, "url": str|None, "response": dict}``; id ищется по заголовку
        в ``/lots/{id}/trade`` — если не найден, лот всё равно создан (lot_id=None, предупреждение в логе).
        """
        if price is None or float(price) <= 0:
            raise SourceError("FunPay: цена лота должна быть больше нуля")
        if not (title_ru or "").strip():
            raise SourceError("FunPay: не задано краткое описание лота (title_ru)")
        form = self.get_lot_form(subcategory_id)
        fields = dict(form["fields"])
        self._apply_lot_changes(fields, {
            "title_ru": title_ru, "title_en": title_en,
            "description_ru": description_ru, "description_en": description_en,
            "price": price, "amount": amount,
            "active": active, "deactivate_after_sale": deactivate_after_sale,
        })
        fields["offer_id"] = "0"
        fields["node_id"] = str(int(subcategory_id))
        fields["location"] = "trade"
        if extra_fields:
            self._apply_lot_changes(fields, {"fields": extra_fields})
        payload = self._save_lot_form(fields)
        lot_id = None
        try:
            lot_id = self._find_lot_id_by_title(int(subcategory_id), title_ru)
        except SourceError as e:
            self.log.warning("FunPay: лот создан, но не удалось получить список наших лотов: %s", e)
        if lot_id is None:
            self._event(f"FunPay: лот «{title_ru}» создан, но его id не найден в /lots/{subcategory_id}/trade",
                        "warning", {"subcategory_id": subcategory_id})
        else:
            self._event(f"FunPay: создан лот #{lot_id} «{title_ru}» за {_fmt_number(price)}", "info",
                        {"lot_id": lot_id, "subcategory_id": subcategory_id})
        return {"lot_id": lot_id, "url": self._lot_url(lot_id), "response": payload}

    def update_lot(self, lot_id: int | str, subcategory_id: int, **changes: Any) -> dict:
        """Изменить лот: ``title_ru/title_en/description_ru/description_en/price/amount/active/
        deactivate_after_sale/fields={...}`` или любые сырые имена полей формы."""
        form = self.get_lot_form(subcategory_id, lot_id)
        fields = dict(form["fields"])
        self._apply_lot_changes(fields, changes)
        fields["offer_id"] = str(lot_id)
        fields["node_id"] = fields.get("node_id") or str(int(subcategory_id))
        fields["location"] = "trade"
        payload = self._save_lot_form(fields)
        self._event(f"FunPay: лот #{lot_id} обновлён ({', '.join(changes.keys()) or 'без изменений'})", "info",
                    {"lot_id": lot_id, "subcategory_id": subcategory_id})
        return {"lot_id": int(lot_id) if str(lot_id).isdigit() else lot_id, "url": self._lot_url(lot_id),
                "response": payload}

    def set_lot_active(self, lot_id: int | str, subcategory_id: int, active: bool) -> dict:
        return self.update_lot(lot_id, subcategory_id, active=bool(active))

    def set_lot_price(self, lot_id: int | str, subcategory_id: int, price: float) -> dict:
        return self.update_lot(lot_id, subcategory_id, price=price)

    def delete_lot(self, lot_id: int | str, subcategory_id: int) -> dict:
        """Удалить лот: отправка формы с ``deleted=1`` (так делает кнопка «Удалить» в редакторе).

        Best-effort: FunPay не документирует этот параметр; если удаление не сработает,
        используйте :meth:`set_lot_active` (деактивация).
        """
        return self.update_lot(lot_id, subcategory_id, deleted="1")

    def get_balance(self) -> Optional[dict]:
        """Баланс не реализован (требует страницы лота с формой оплаты)."""
        return None
