"""Источник объявлений: Lolzteam Market (https://lzt.market) через официальный REST API.

Авторизация — Bearer-токен (https://lolz.team/account/api, права «market»).
Базовый адрес API: https://prod-api.lzt.market (резервный — https://api.lzt.market,
переключение автоматическое при ошибке соединения / 404 на /me).

Лимит API: не чаще одного запроса в 3 секунды (settings.lolz.request_delay),
при HTTP 429 клиент ждёт (Retry-After или 5 с) и повторяет запрос до 3 раз.

================================================================================
Параметры поиска по категории — GET /{category}  (SourceLolzConfig.params)
================================================================================
Общие параметры (работают во всех категориях):
  page        — номер страницы (подставляется автоматически, pages в профиле);
  title       — слово/слова в заголовке объявления;
  pmin, pmax  — цена от/до (включительно; подставляются из criteria.price профиля,
                если не заданы явно в params);
  order_by    — сортировка: price_to_up (дешевле), price_to_down (дороже),
                pdate_to_down (новые), pdate_to_up (старые),
                pdate_to_down_upload / pdate_to_up_upload (по дате загрузки);
  origin[]    — происхождение аккаунта: brute, stealer, phishing, autoreg,
                personal, resale, dummy, self_registration (список);
  not_origin[]— исключить происхождение (список);
  sb / nsb    — 1 = продавался ранее / не продавался ранее;
  sb_by_me / nsb_by_me — то же, но «мной»;
  parse_sticky_items, parse_same_items — включать закреплённые / историю.

Списки передаются как повторяющиеся key[]=v (game[]=570&game[]=730):
в params профиля достаточно написать {"game": [570, 730]} — скобки добавятся сами.
Булевы значения отправляются как 1/0, None пропускаются.

Фильтры, специфичные для категории, МЕНЯЮТСЯ — перед использованием ОБЯЗАТЕЛЬНО
ПРОВЕРЬТЕ ЧЕРЕЗ /params (метод category_params(category), кнопка «Параметры» в UI).
Примеры ниже даны только как ориентир (проверьте через /params!):

  steam           — game[] (570 = Dota 2, 730 = CS2/CS:GO, 578080 = PUBG,
                    252490 = Rust, 440 = TF2), level_min / level_max (уровень Steam),
                    inv_game / inv_min (инвентарь игры и его минимальная стоимость),
                    dota2_solo_mmr_min / dota2_solo_mmr_max, no_vac=1,
                    limit (ограничения), hours_played_min, daybreak (дней без входа).
                    Список игр: category_games("steam") (GET /steam/games).
  fortnite        — skin[] (id скинов), skins_min, vbucks_min, level_min,
                    platform, daybreak.
  world-of-tanks  — tank[] (id танков), tanks_min, battles_min, premium_tanks_min,
                    gold_min, wot_region (ru/eu/na/asia), daybreak.
  wot-blitz       — tank[], tanks_min, battles_min, gold_min.
  riot            — valorant_region / lol_region, skins_min, vp_min, level_min.
  mihoyo          — genshin_level_min, genshin_region, legendary_min.
  epicgames       — game[] (игры Epic), daybreak.
  supercell       — system (laser = Brawl Stars, scroll = Clash Royale,
                    magic = Clash of Clans), trophies_min и т.п.
  escape-from-tarkov — edition, level_min, region.

Имена полей КОНКРЕТНОГО объявления (для criteria.numeric в профиле) смотрите в
Listing.attributes["raw_keys"] — это список всех ключей, которые вернул API.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

import httpx

from ..models import Listing, Profile
from ..settings import LolzSettings
from .base import AuthError, BaseSource, SourceError

DEFAULT_BASE_URL = "https://prod-api.lzt.market"
FALLBACK_BASE_URL = "https://api.lzt.market"
ITEM_URL = "https://lzt.market/{item_id}"
MEMBER_URL = "https://lolz.live/members/{user_id}/"
USER_AGENT = "FunPaySearcher/0.1"

MAX_429_RETRIES = 3
DEFAULT_429_WAIT = 5.0
MAX_COLLECTION_SIZE = 50  # списки/словари длиннее не кладём в attributes

#: Steam AppID популярных игр для параметра game[] категории steam.
KNOWN_STEAM_GAMES: dict[str, int] = {
    "dota2": 570,
    "cs2": 730,
    "csgo": 730,
    "pubg": 578080,
    "rust": 252490,
    "gta5": 271590,
    "apex": 1172470,
    "tf2": 440,
}

#: Известные категории Маркета (резерв на случай недоступности GET /category).
KNOWN_CATEGORIES: list[tuple[str, str]] = [
    ("steam", "Steam"),
    ("fortnite", "Fortnite"),
    ("mihoyo", "miHoYo (Genshin Impact / Honkai)"),
    ("riot", "Riot (Valorant / LoL)"),
    ("world-of-tanks", "World of Tanks"),
    ("wot-blitz", "World of Tanks Blitz"),
    ("epicgames", "Epic Games"),
    ("supercell", "Supercell"),
    ("battlenet", "Battle.net"),
    ("ea", "EA (Origin)"),
    ("roblox", "Roblox"),
    ("warface", "Warface"),
    ("escape-from-tarkov", "Escape From Tarkov"),
    ("socialclub", "Social Club"),
    ("uplay", "Uplay"),
    ("minecraft", "Minecraft"),
    ("telegram", "Telegram"),
    ("discord", "Discord"),
]

#: Поля объявления, в которых может лежать регион (первое непустое побеждает).
REGION_KEYS: tuple[str, ...] = (
    "region",
    "account_region",
    "steam_country",
    "wot_region",
    "fortnite_region",
    "riot_region",
    "valorant_region",
    "lol_region",
    "genshin_region",
    "country",
)

#: Поля, которые всегда присутствуют в attributes (даже если API их не вернул).
ALWAYS_ATTRIBUTES: tuple[str, ...] = ("item_state", "published_date", "item_origin", "category_id")

_CATEGORY_RE = re.compile(r"^[a-z0-9_-]+$")
_NOT_FOUND_RE = re.compile(r"not\s+found|не\s+найден|does\s+not\s+exist|не\s+существует", re.IGNORECASE)


class NotFoundError(SourceError):
    """Объявление не найдено (HTTP 404 или ошибка «item not found»)."""


def encode_params(params: Optional[dict[str, Any]]) -> list[tuple[str, str]]:
    """Преобразовать словарь параметров в список пар для query-string.

    - списки/кортежи/множества -> повторяющиеся `key[]=v` (если ключ уже
      заканчивается на `[]`, он остаётся как есть);
    - словари -> `key[sub]=v`;
    - bool -> 1/0; целые float (1000.0) -> "1000"; None пропускаются.
    """
    out: list[tuple[str, str]] = []
    if not params:
        return out
    for key, value in params.items():
        key = str(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            list_key = key if key.endswith("[]") else key + "[]"
            for v in value:
                if v is None:
                    continue
                out.append((list_key, _scalar(v)))
        elif isinstance(value, dict):
            for sub, v in value.items():
                if v is None:
                    continue
                out.append((f"{key}[{sub}]", _scalar(v)))
        else:
            out.append((key, _scalar(value)))
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _error_message(data: Any, status: int) -> str:
    """Вытащить текст ошибки из JSON-ответа API."""
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, (list, tuple)) and errors:
            return "; ".join(str(e) for e in errors)
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}" for k, v in errors.items())
        if isinstance(errors, str) and errors:
            return errors
        err = data.get("error") or data.get("error_description") or data.get("message")
        if err:
            return str(err)
    return f"HTTP {status}"


def _is_small_collection(value: Any) -> bool:
    if len(value) > MAX_COLLECTION_SIZE:
        return False
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


class LolzSource(BaseSource):
    """Поиск и проверка объявлений на Lolzteam Market через официальное API."""

    name = "lolz"

    def __init__(
        self,
        settings: LolzSettings,
        logger: Optional[logging.Logger] = None,
        base_url: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("lolz")
        # Явно заданный адрес используется без резервного переключения.
        if base_url:
            self._hosts = [base_url.rstrip("/")]
        else:
            self._hosts = [DEFAULT_BASE_URL, FALLBACK_BASE_URL]
        self._host_confirmed = False
        self._transport = transport
        self._client: Optional[httpx.Client] = None
        self._lock = threading.Lock()
        self._last_request_ts: Optional[float] = None
        # Подменяемые в тестах функции времени.
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------------ infra
    @property
    def base_url(self) -> str:
        """Текущий базовый адрес API (тот, который сработал последним)."""
        return self._hosts[0]

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "LolzSource":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.settings.token}",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "headers": self._headers(),
                "timeout": self.settings.timeout,
                "follow_redirects": True,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            elif self.settings.proxy:
                kwargs["proxy"] = self.settings.proxy
            try:
                self._client = httpx.Client(**kwargs)
            except Exception as e:  # например, socks-прокси без установленного socksio
                raise SourceError(f"не удалось создать HTTP-клиент Lolzteam: {e}") from e
        return self._client

    def _throttle(self) -> None:
        """Выдержать паузу между запросами (лимит API)."""
        delay = float(self.settings.request_delay or 0)
        if delay <= 0:
            return
        now = self._monotonic()
        if self._last_request_ts is not None:
            wait = delay - (now - self._last_request_ts)
            if wait > 0:
                self.log.debug("Lolzteam: пауза %.2f с перед запросом", wait)
                self._sleep(wait)

    def _send_once(self, method: str, path: str, query: list[tuple[str, str]]) -> httpx.Response:
        """Один физический запрос с автоматическим переключением хоста."""
        client = self._get_client()
        last_error: Optional[Exception] = None
        for index, host in enumerate(list(self._hosts)):
            has_more = index < len(self._hosts) - 1
            url = host + path
            self._throttle()
            try:
                self._last_request_ts = self._monotonic()
                response = client.request(method, url, params=query or None)
            except httpx.HTTPError as e:
                last_error = e
                if has_more:
                    self.log.warning("Lolzteam: хост %s недоступен (%s), пробуем резервный", host, e)
                    continue
                raise SourceError(f"ошибка сети при запросе к Lolzteam: {e}") from e
            if response.status_code == 404 and not self._host_confirmed and has_more:
                self.log.warning("Lolzteam: хост %s ответил 404 на %s, пробуем резервный", host, path)
                continue
            if response.status_code != 404:
                self._host_confirmed = True
            if index != 0:
                # запоминаем сработавший хост первым в списке
                self._hosts.remove(host)
                self._hosts.insert(0, host)
                self.log.info("Lolzteam: используем адрес API %s", host)
            return response
        raise SourceError(f"ошибка сети при запросе к Lolzteam: {last_error}")

    def _request(self, method: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Выполнить запрос к API и вернуть разобранный JSON.

        401/403 -> AuthError, 404 -> NotFoundError, другие ошибки -> SourceError.
        При 429 ждёт (Retry-After или 5 с) и повторяет до MAX_429_RETRIES раз.
        """
        if not self.settings.token:
            raise AuthError("токен Lolzteam не задан")
        query = encode_params(params)
        attempts = 0
        with self._lock:
            while True:
                response = self._send_once(method, path, query)
                if response.status_code != 429:
                    break
                attempts += 1
                if attempts > MAX_429_RETRIES:
                    raise SourceError("превышен лимит запросов к API Lolzteam (HTTP 429)")
                wait = self._retry_after(response)
                self.log.warning(
                    "Lolzteam: лимит запросов (429), ждём %.0f с и повторяем (%d/%d)",
                    wait, attempts, MAX_429_RETRIES,
                )
                self._sleep(wait)
        return self._parse_response(response, path)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass
        return DEFAULT_429_WAIT

    def _parse_response(self, response: httpx.Response, path: str) -> Any:
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        if status in (401, 403):
            self.log.error("Lolzteam: HTTP %s на %s — токен невалиден", status, path)
            raise AuthError("токен Lolzteam невалиден")
        if status == 404:
            raise NotFoundError(_error_message(data, status))
        if status >= 400:
            msg = _error_message(data, status)
            self.log.error("Lolzteam: ошибка API %s на %s: %s", status, path, msg)
            raise SourceError(f"ошибка API Lolzteam ({status}): {msg}")
        if data is None:
            raise SourceError(f"API Lolzteam вернул не-JSON ответ на {path}")
        return data

    # ------------------------------------------------------------- public API
    def check_auth(self) -> dict:
        """GET /me — проверить токен и получить имя пользователя/баланс."""
        result: dict[str, Any] = {"ok": False, "username": None, "user_id": None, "balance": None, "error": None}
        if not self.settings.token:
            result["error"] = "токен Lolzteam не задан"
            return result
        try:
            data = self._request("GET", "/me")
        except AuthError as e:
            result["error"] = str(e)
            return result
        except SourceError as e:
            self.log.warning("Lolzteam: не удалось проверить токен: %s", e)
            result["error"] = str(e)
            return result
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            user = data if isinstance(data, dict) and "username" in data else None
        if not user:
            result["error"] = "неожиданный ответ API Lolzteam на /me"
            return result
        result.update(
            ok=True,
            username=user.get("username"),
            user_id=user.get("user_id"),
            balance=user.get("balance"),
        )
        self.log.info("Lolzteam: авторизация OK, пользователь %s", result["username"])
        return result

    def categories(self) -> list[dict[str, str]]:
        """GET /category -> [{name, title}]; при ошибке — встроенный список."""
        try:
            data = self._request("GET", "/category")
        except SourceError as e:
            self.log.warning("Lolzteam: не удалось получить категории (%s), используем встроенный список", e)
            return self._fallback_categories()
        parsed = self._parse_categories(data)
        if not parsed:
            self.log.warning("Lolzteam: неожиданный формат ответа /category, используем встроенный список")
            return self._fallback_categories()
        return parsed

    @staticmethod
    def _fallback_categories() -> list[dict[str, str]]:
        return [{"name": n, "title": t} for n, t in KNOWN_CATEGORIES]

    @classmethod
    def _parse_categories(cls, data: Any) -> list[dict[str, str]]:
        entries: Any = None
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            for key in ("categories", "category_list", "category", "list"):
                if key in data and isinstance(data[key], (list, dict)):
                    entries = data[key]
                    break
            if entries is None:
                entries = {k: v for k, v in data.items() if k != "system_info"}
        if entries is None:
            return []
        if isinstance(entries, dict):
            pairs = list(entries.items())
        else:
            pairs = [(None, e) for e in entries]
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for key, entry in pairs:
            if isinstance(entry, str):
                name, title = (key or entry), entry
            elif isinstance(entry, dict):
                name = (
                    entry.get("category_name")
                    or entry.get("name")
                    or entry.get("category_url")
                    or entry.get("slug")
                    or key
                )
                title = entry.get("category_title") or entry.get("title") or entry.get("category_name") or name
            else:
                continue
            if not name or not isinstance(name, str):
                continue
            name = name.strip().lower()
            if name in seen or not _CATEGORY_RE.match(name):
                continue
            seen.add(name)
            out.append({"name": name, "title": str(title)})
        return out

    @staticmethod
    def _clean_category(category: str) -> str:
        cat = (category or "").strip().strip("/").lower()
        if not cat or not _CATEGORY_RE.match(cat):
            raise SourceError(f"некорректное имя категории Lolzteam: {category!r}")
        return cat

    def category_params(self, category: str) -> Any:
        """GET /{category}/params — доступные фильтры категории (сырой JSON)."""
        return self._request("GET", f"/{self._clean_category(category)}/params")

    def category_games(self, category: str) -> Any:
        """GET /{category}/games — список игр категории (сырой JSON)."""
        return self._request("GET", f"/{self._clean_category(category)}/games")

    def search_category(
        self,
        category: str,
        params: Optional[dict[str, Any]] = None,
        pages: int = 1,
        max_items: int = 500,
    ) -> list[Listing]:
        """GET /{category}?...&page=N для N = 1..pages. Останавливается на неполной странице."""
        cat = self._clean_category(category)
        base = dict(params or {})
        start_page = 1
        if "page" in base:
            try:
                start_page = max(1, int(base.pop("page")))
            except (TypeError, ValueError):
                base.pop("page", None)
        pages = max(1, int(pages or 1))
        max_items = int(max_items) if max_items else 0

        out: list[Listing] = []
        seen: set[str] = set()
        for offset in range(pages):
            page = start_page + offset
            query = dict(base)
            query["page"] = page
            data = self._request("GET", f"/{cat}", query)
            items = data.get("items") if isinstance(data, dict) else None
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, list):
                items = []
            per_page = _to_int(data.get("perPage") if isinstance(data, dict) else None)
            total = _to_int(data.get("totalItems") if isinstance(data, dict) else None)
            self.log.info(
                "Lolzteam: %s страница %d — %d объявлений (всего %s)",
                cat, page, len(items), total if total is not None else "?",
            )
            for item in items:
                listing = self._map_item(item)
                if listing is None or listing.source_id in seen:
                    continue
                seen.add(listing.source_id)
                out.append(listing)
                if max_items and len(out) >= max_items:
                    return out
            if not items:
                break
            if per_page and len(items) < per_page:
                break
            if total is not None and per_page and page * per_page >= total:
                break
        return out

    def search(self, profile: Profile, limit: Optional[int] = None) -> list[Listing]:
        cfg = profile.lolz()
        if not cfg.enabled:
            self.log.info("Lolzteam: источник выключен в профиле «%s»", profile.name)
            return []
        if not cfg.category:
            self.log.warning("Lolzteam: в профиле «%s» не задана категория — пропускаем", profile.name)
            return []
        params = dict(cfg.params or {})
        price = profile.criteria.price
        if price.min is not None and params.get("pmin") is None:
            params["pmin"] = price.min
        if price.max is not None and params.get("pmax") is None:
            params["pmax"] = price.max
        max_items = int(limit) if limit else int(cfg.max_items)
        self.log.info(
            "Lolzteam: поиск по профилю «%s» в категории %s, params=%s",
            profile.name, cfg.category, params,
        )
        listings = self.search_category(cfg.category, params, pages=cfg.pages, max_items=max_items)
        for listing in listings:
            listing.game = profile.game
            # регион не выдумываем: если площадка его не сообщила, остаётся None
        self.log.info("Lolzteam: профиль «%s» — найдено %d объявлений", profile.name, len(listings))
        return listings

    def get_listing(self, source_id: str) -> Optional[Listing]:
        item_id = self._item_id(source_id)
        try:
            data = self._request("GET", f"/{item_id}")
        except NotFoundError:
            return None
        item = data.get("item") if isinstance(data, dict) else None
        if not isinstance(item, dict) or not item:
            msg = _error_message(data, 200)
            if _NOT_FOUND_RE.search(msg):
                return None
            if isinstance(data, dict) and (data.get("errors") or data.get("error")):
                raise SourceError(f"ошибка API Lolzteam: {msg}")
            raise SourceError(f"неожиданный ответ API Lolzteam на /{item_id}")
        listing = self._map_item(item)
        if listing is None:
            # объявление есть, но без item_id/цены — не считаем его удалённым
            raise SourceError(f"неожиданный формат объявления Lolzteam /{item_id}")
        return listing

    def is_available(self, source_id: str) -> Optional[bool]:
        try:
            listing = self.get_listing(source_id)
        except SourceError as e:
            self.log.warning("Lolzteam: не удалось проверить объявление %s: %s", source_id, e)
            return None
        if listing is None:
            return False
        state = listing.attributes.get("item_state")
        if state is None:
            return None
        return str(state).lower() == "active"

    # ---------------------------------------------------------------- mapping
    @staticmethod
    def _item_id(source_id: Any) -> str:
        sid = str(source_id).strip()
        if sid.startswith("lolz:"):
            sid = sid[len("lolz:"):]
        if not sid.isdigit():
            raise SourceError(f"некорректный id объявления Lolzteam: {source_id!r}")
        return sid

    def _map_item(self, item: Any) -> Optional[Listing]:
        """Преобразовать объявление из ответа API в Listing."""
        if not isinstance(item, dict):
            return None
        item_id = item.get("item_id")
        if item_id is None or item_id == "":
            self.log.warning("Lolzteam: объявление без item_id пропущено")
            return None
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            self.log.warning("Lolzteam: объявление %s без цены пропущено", item_id)
            return None

        currency = item.get("price_currency") or item.get("currency") or "RUB"
        currency = str(currency).upper()

        seller = item.get("seller")
        seller_name = seller_id = seller_url = None
        if isinstance(seller, dict):
            seller_name = seller.get("username") or None
            uid = seller.get("user_id")
            if uid is not None and uid != "":
                seller_id = str(uid)
                seller_url = MEMBER_URL.format(user_id=seller_id)

        region: Optional[str] = None
        for key in REGION_KEYS:
            value = item.get(key)
            if value is None or value == "" or isinstance(value, bool):
                continue
            if isinstance(value, str):
                region = value.strip().upper() or None
            elif isinstance(value, (int, float)):
                region = str(value)
            if region:
                break

        attributes: dict[str, Any] = {}
        for key, value in item.items():
            if key in ("title", "description", "seller"):
                continue
            if isinstance(value, bool) or isinstance(value, (str, int, float)):
                attributes[key] = value
            elif isinstance(value, (list, dict)) and _is_small_collection(value):
                attributes[key] = value
        for key in ALWAYS_ATTRIBUTES:
            attributes.setdefault(key, item.get(key))
        if isinstance(seller, dict):
            # полезно для фильтров по продавцу (sold_items_count, restore_percents ...)
            attributes["seller"] = {k: v for k, v in seller.items() if isinstance(v, (str, int, float, bool))}
        attributes["raw_keys"] = sorted(str(k) for k in item.keys())

        return Listing(
            source="lolz",
            source_id=str(item_id),
            url=ITEM_URL.format(item_id=item_id),
            title=str(item.get("title") or item.get("title_en") or ""),
            description=str(item.get("description") or ""),
            price=price,
            currency=currency,
            seller_name=seller_name,
            seller_url=seller_url,
            seller_id=seller_id,
            region=region,
            game=None,
            attributes=attributes,
            online=None,
        )

    # обратная совместимость с именем из задания
    _encode_params = staticmethod(encode_params)


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
