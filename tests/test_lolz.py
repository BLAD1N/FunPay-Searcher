"""Тесты источника Lolzteam Market (без сети — httpx.MockTransport)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

# позволяет запускать `pytest tests/test_lolz.py` без conftest/pytest.ini
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Profile
from app.settings import LolzSettings
from app.sources.base import AuthError, SourceError
from app.sources.lolz import (
    DEFAULT_BASE_URL,
    FALLBACK_BASE_URL,
    KNOWN_CATEGORIES,
    KNOWN_STEAM_GAMES,
    LolzSource,
    encode_params,
)

PROD_HOST = "prod-api.lzt.market"
FALLBACK_HOST = "api.lzt.market"


# ----------------------------------------------------------------- helpers
def item(item_id: int, **extra) -> dict:
    base = {
        "item_id": item_id,
        "title": f"Аккаунт {item_id}",
        "title_en": f"Account {item_id}",
        "description": "описание",
        "price": 1500,
        "item_state": "active",
        "published_date": 1700000000,
        "item_origin": "personal",
        "category_id": 1,
        "seller": {"user_id": 777, "username": "seller77", "sold_items_count": 12},
    }
    base.update(extra)
    return base


def page_response(items: list[dict], per_page: int = 40, total: int | None = None, page: int = 1) -> dict:
    return {
        "items": items,
        "totalItems": total if total is not None else len(items),
        "perPage": per_page,
        "page": page,
    }


class Recorder:
    """Запоминает запросы и отдаёт ответы по правилам."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def make_source(
    handler: Callable[[httpx.Request], httpx.Response],
    token: str = "tok",
    base_url: str | None = None,
    delay: float = 0.0,
) -> tuple[LolzSource, Recorder]:
    rec = Recorder(handler)
    settings = LolzSettings(token=token, request_delay=delay, timeout=5)
    src = LolzSource(settings, base_url=base_url, transport=httpx.MockTransport(rec))
    src._sleep = lambda s: None
    return src, rec


def ok(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def make_profile(**overrides) -> Profile:
    data = {
        "id": "dota",
        "name": "Dota 2 RU",
        "game": "dota2",
        "region": "RU",
        "sources": {
            "lolz": {
                "enabled": True,
                "category": "steam",
                "params": {"game": [570, 730], "order_by": "price_to_up", "no_vac": True},
                "pages": 2,
                "max_items": 100,
            }
        },
        "criteria": {"price": {"min": 1000, "max": 5000}},
    }
    data.update(overrides)
    return Profile.model_validate(data)


# ------------------------------------------------------------ encode_params
def test_encode_params_lists_bools_none():
    encoded = encode_params(
        {
            "game": [570, 730],
            "origin[]": ["brute", "autoreg"],
            "no_vac": True,
            "sb": False,
            "title": None,
            "pmin": 1000.0,
            "pmax": 1500.5,
            "extra": {"a": 1, "b": None},
            "tags": [None, "x"],
        }
    )
    assert encoded == [
        ("game[]", "570"),
        ("game[]", "730"),
        ("origin[]", "brute"),
        ("origin[]", "autoreg"),
        ("no_vac", "1"),
        ("sb", "0"),
        ("pmin", "1000"),
        ("pmax", "1500.5"),
        ("extra[a]", "1"),
        ("tags[]", "x"),
    ]
    assert encode_params(None) == []
    assert LolzSource._encode_params({"page": 2}) == [("page", "2")]


def test_encoded_params_reach_query_string():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("game[]") == ["570", "730"]
        assert request.url.params["no_vac"] == "1"
        assert "title" not in request.url.params
        return ok(page_response([]))

    src, rec = make_source(handler)
    assert src.search_category("steam", {"game": [570, 730], "no_vac": True, "title": None}) == []
    assert len(rec.requests) == 1
    assert rec.requests[0].url.path == "/steam"


# --------------------------------------------------------------- check_auth
def test_check_auth_ok_and_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/me"
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "FunPaySearcher/0.1"
        return ok({"user": {"user_id": 42, "username": "neo", "balance": 1234.5}})

    src, _ = make_source(handler)
    result = src.check_auth()
    assert result == {"ok": True, "username": "neo", "user_id": 42, "balance": 1234.5, "error": None}


def test_check_auth_403_is_auth_error():
    src, _ = make_source(lambda r: ok({"errors": ["Invalid token"]}, 403))
    result = src.check_auth()
    assert result["ok"] is False
    assert "токен" in result["error"].lower()
    with pytest.raises(AuthError):
        src.search_category("steam", {})


def test_check_auth_without_token_does_not_call_api():
    src, rec = make_source(lambda r: ok({}), token="")
    result = src.check_auth()
    assert result["ok"] is False
    assert result["error"]
    assert rec.requests == []
    with pytest.raises(AuthError):
        src.get_listing("1")


def test_check_auth_network_error_returns_error_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    src, _ = make_source(handler)
    result = src.check_auth()
    assert result["ok"] is False
    assert "сети" in result["error"]


# ---------------------------------------------------------------- pagination
def test_search_category_pagination_stops_on_partial_page():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            return ok(page_response([item(i) for i in range(1, 41)], per_page=40, total=45, page=1))
        if page == 2:
            return ok(page_response([item(i) for i in range(41, 46)], per_page=40, total=45, page=2))
        raise AssertionError(f"лишний запрос страницы {page}")

    src, rec = make_source(handler)
    listings = src.search_category("steam", {"pmin": 100}, pages=5, max_items=500)
    assert len(listings) == 45
    assert [r.url.params["page"] for r in rec.requests] == ["1", "2"]
    assert all(r.url.params["pmin"] == "100" for r in rec.requests)
    assert listings[0].source_id == "1" and listings[-1].source_id == "45"


def test_search_category_stops_on_empty_page_and_missing_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            return ok({"items": [item(1), item(2), item(2)]})  # без perPage/totalItems + дубликат
        return ok({"items": []})

    src, rec = make_source(handler)
    listings = src.search_category("steam", {}, pages=4)
    assert [x.source_id for x in listings] == ["1", "2"]
    assert len(rec.requests) == 2


def test_search_category_max_items_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        start = (page - 1) * 40 + 1
        return ok(page_response([item(i) for i in range(start, start + 40)], per_page=40, total=1000, page=page))

    src, rec = make_source(handler)
    listings = src.search_category("steam", {}, pages=10, max_items=10)
    assert len(listings) == 10
    assert len(rec.requests) == 1


def test_search_category_rejects_bad_category():
    src, rec = make_source(lambda r: ok({}))
    with pytest.raises(SourceError):
        src.search_category("steam/../me", {})
    assert rec.requests == []


# ------------------------------------------------------------------- mapping
def test_listing_mapping():
    raw = item(
        123456,
        price="1500.50",
        steam_country="ru",
        steam_level=25,
        no_vac=True,
        tanks=["T-34", "IS-7"],
        steam_games={"570": {"title": "Dota 2"}},
        huge=list(range(100)),
        title="",
        title_en="Steam account",
    )
    src, _ = make_source(lambda r: ok({"item": raw}))
    listing = src.get_listing("123456")
    assert listing is not None
    assert listing.source == "lolz"
    assert listing.key == "lolz:123456"
    assert listing.url == "https://lzt.market/123456"
    assert listing.title == "Steam account"
    assert listing.description == "описание"
    assert listing.price == 1500.5 and isinstance(listing.price, float)
    assert listing.currency == "RUB"
    assert listing.seller_name == "seller77"
    assert listing.seller_id == "777"
    assert listing.seller_url == "https://lolz.live/members/777/"
    assert listing.region == "RU"
    assert listing.online is None
    attrs = listing.attributes
    assert attrs["item_state"] == "active"
    assert attrs["published_date"] == 1700000000
    assert attrs["item_origin"] == "personal"
    assert attrs["category_id"] == 1
    assert attrs["steam_level"] == 25
    assert attrs["no_vac"] is True
    assert attrs["tanks"] == ["T-34", "IS-7"]
    assert attrs["steam_games"] == {"570": {"title": "Dota 2"}}
    assert "huge" not in attrs
    assert "title" not in attrs and "description" not in attrs
    assert attrs["seller"]["sold_items_count"] == 12
    assert attrs["raw_keys"] == sorted(raw.keys())
    assert "steam_country" in attrs["raw_keys"]
    # атрибуты должны сериализоваться (хранятся в БД как JSON)
    json.dumps(attrs)


def test_listing_mapping_currency_and_missing_fields():
    raw = {"item_id": 5, "price": 10, "price_currency": "usd"}
    src, _ = make_source(lambda r: ok({"item": raw}))
    listing = src.get_listing("lolz:5")
    assert listing is not None
    assert listing.currency == "USD"
    assert listing.seller_name is None and listing.seller_url is None
    assert listing.region is None
    assert listing.title == ""
    assert listing.attributes["item_state"] is None
    assert listing.attributes["raw_keys"] == ["item_id", "price", "price_currency"]


def test_items_without_id_or_price_are_skipped():
    src, _ = make_source(lambda r: ok(page_response([{"price": 1}, {"item_id": 9}, item(10)], per_page=40)))
    listings = src.search_category("steam", {})
    assert [x.source_id for x in listings] == ["10"]


# ------------------------------------------------------------ search(profile)
def test_search_profile_applies_price_and_game_list():
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert request.url.path == "/steam"
        assert params["pmin"] == "1000"
        assert params["pmax"] == "5000"
        assert params.get_list("game[]") == ["570", "730"]
        assert params["order_by"] == "price_to_up"
        assert params["no_vac"] == "1"
        return ok(page_response([item(1), item(2, steam_country="eu")], per_page=40, total=2))

    src, rec = make_source(handler)
    listings = src.search(make_profile())
    assert len(rec.requests) == 1
    assert [x.source_id for x in listings] == ["1", "2"]
    assert all(x.game == "dota2" for x in listings)
    assert listings[0].region is None  # регион профиля не подставляем
    assert listings[1].region == "EU"


def test_search_profile_explicit_pmin_wins_and_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["pmin"] == "50"
        assert request.url.params["pmax"] == "5000"
        return ok(page_response([item(i) for i in range(1, 41)], per_page=40, total=80))

    profile = make_profile()
    profile.sources["lolz"]["params"] = {"pmin": 50}
    src, rec = make_source(handler)
    listings = src.search(profile, limit=3)
    assert len(listings) == 3
    assert len(rec.requests) == 1


def test_search_profile_without_category_or_disabled():
    src, rec = make_source(lambda r: ok({}))
    assert src.search(make_profile(sources={"lolz": {"enabled": True}})) == []
    assert src.search(make_profile(sources={})) == []
    assert src.search(make_profile(sources={"lolz": {"enabled": False, "category": "steam"}})) == []
    assert rec.requests == []


# -------------------------------------------------------------- get_listing
def test_get_listing_404_and_not_found_errors():
    src, _ = make_source(lambda r: ok({"errors": ["Item not found"]}, 404))
    assert src.get_listing("1") is None

    src, _ = make_source(lambda r: ok({"errors": ["Item not found"]}, 200))
    assert src.get_listing("1") is None

    src, _ = make_source(lambda r: ok({"errors": ["Доступ запрещён"]}, 200))
    with pytest.raises(SourceError):
        src.get_listing("1")

    src, _ = make_source(lambda r: ok({}))
    with pytest.raises(SourceError):
        src.get_listing("abc")

    # объявление без item_id/цены — ошибка, а не «не найдено»
    src, _ = make_source(lambda r: ok({"item": {"foo": 1}}))
    with pytest.raises(SourceError):
        src.get_listing("1")
    assert src.is_available("1") is None


def test_source_error_carries_api_message():
    src, _ = make_source(lambda r: ok({"errors": ["bad param pmin"]}, 400))
    with pytest.raises(SourceError) as exc:
        src.category_params("steam")
    assert "bad param pmin" in str(exc.value)

    src, _ = make_source(lambda r: ok({"error": "server exploded"}, 500))
    with pytest.raises(SourceError) as exc:
        src.category_games("steam")
    assert "server exploded" in str(exc.value)

    src, _ = make_source(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(SourceError):
        src.category_params("steam")


# ------------------------------------------------------------- is_available
@pytest.mark.parametrize(
    "response, expected",
    [
        (lambda r: ok({"item": item(1, item_state="active")}), True),
        (lambda r: ok({"item": item(1, item_state="paid")}), False),
        (lambda r: ok({"item": item(1, item_state="closed")}), False),
        (lambda r: ok({"errors": ["Item not found"]}, 404), False),
        (lambda r: ok({"errors": ["oops"]}, 500), None),
        (lambda r: ok({"errors": ["token"]}, 401), None),
        (lambda r: ok({"item": {"item_id": 1, "price": 5}}), None),
    ],
)
def test_is_available(response, expected):
    src, _ = make_source(response)
    assert src.is_available("1") is expected


# ------------------------------------------------------------------ 429 retry
def test_429_retry_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return ok({"errors": ["Too many requests"]}, 429, headers={"Retry-After": "2"})
        return ok({"user": {"user_id": 1, "username": "u"}})

    src, rec = make_source(handler)
    sleeps: list[float] = []
    src._sleep = sleeps.append
    result = src.check_auth()
    assert result["ok"] is True
    assert len(rec.requests) == 3
    assert sleeps == [2.0, 2.0]


def test_429_gives_up_after_retries():
    src, rec = make_source(lambda r: ok({}, 429))
    sleeps: list[float] = []
    src._sleep = sleeps.append
    with pytest.raises(SourceError) as exc:
        src.category_params("steam")
    assert "429" in str(exc.value)
    assert len(rec.requests) == 4  # 1 + 3 повтора
    assert sleeps == [5.0, 5.0, 5.0]


# ---------------------------------------------------------- base-url fallback
def test_base_url_fallback_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PROD_HOST:
            raise httpx.ConnectError("no route", request=request)
        assert request.url.host == FALLBACK_HOST
        return ok({"user": {"user_id": 1, "username": "u"}})

    src, rec = make_source(handler)
    assert src.base_url == DEFAULT_BASE_URL
    assert src.check_auth()["ok"] is True
    assert src.base_url == FALLBACK_BASE_URL
    # второй вызов идёт сразу на резервный хост
    assert src.check_auth()["ok"] is True
    hosts = [r.url.host for r in rec.requests]
    assert hosts == [PROD_HOST, FALLBACK_HOST, FALLBACK_HOST]


def test_base_url_fallback_on_404_me():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PROD_HOST:
            return httpx.Response(404, text="not here")
        return ok({"user": {"user_id": 1, "username": "u"}})

    src, rec = make_source(handler)
    assert src.check_auth()["username"] == "u"
    assert src.base_url == FALLBACK_BASE_URL
    assert [r.url.host for r in rec.requests] == [PROD_HOST, FALLBACK_HOST]


def test_404_after_host_confirmed_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PROD_HOST
        if request.url.path == "/me":
            return ok({"user": {"user_id": 1, "username": "u"}})
        return ok({"errors": ["Item not found"]}, 404)

    src, rec = make_source(handler)
    assert src.check_auth()["ok"] is True
    assert src.get_listing("5") is None
    assert len(rec.requests) == 2


def test_explicit_base_url_has_no_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.test"
        raise httpx.ConnectError("down", request=request)

    src, rec = make_source(handler, base_url="https://example.test/")
    with pytest.raises(SourceError):
        src.category_params("steam")
    assert len(rec.requests) == 1
    assert rec.requests[0].url.path == "/steam/params"


# ---------------------------------------------------------------- categories
def test_categories_shapes_and_fallback():
    src, _ = make_source(lambda r: ok({"categories": [{"category_name": "steam", "category_title": "Steam"}]}))
    assert src.categories() == [{"name": "steam", "title": "Steam"}]

    src, _ = make_source(lambda r: ok({"category_list": [{"name": "fortnite", "title": "Fortnite"}]}))
    assert src.categories() == [{"name": "fortnite", "title": "Fortnite"}]

    src, _ = make_source(
        lambda r: ok({"steam": {"title": "Steam"}, "riot": {"category_title": "Riot"}, "system_info": {"x": 1}})
    )
    assert src.categories() == [{"name": "steam", "title": "Steam"}, {"name": "riot", "title": "Riot"}]

    src, _ = make_source(lambda r: ok({"errors": ["boom"]}, 500))
    cats = src.categories()
    assert cats == [{"name": n, "title": t} for n, t in KNOWN_CATEGORIES]
    assert {"name": "world-of-tanks", "title": "World of Tanks"} in cats

    # словарь name -> title тоже понимаем
    src, _ = make_source(lambda r: ok({"steam": "Steam", "ea": "EA"}))
    assert src.categories() == [{"name": "steam", "title": "Steam"}, {"name": "ea", "title": "EA"}]

    # неразборчивый ответ -> встроенный список
    src, _ = make_source(lambda r: ok({"system_info": {"x": 1}, "count": 3}))
    assert src.categories() == [{"name": n, "title": t} for n, t in KNOWN_CATEGORIES]


def test_category_params_and_games_return_raw_json():
    raw = {"params": {"game": {"type": "list"}}, "system_info": {}}
    src, rec = make_source(lambda r: ok(raw))
    assert src.category_params("Steam") == raw
    assert src.category_games("steam") == raw
    assert [r.url.path for r in rec.requests] == ["/steam/params", "/steam/games"]


# ------------------------------------------------------------------ throttle
def test_request_delay_between_requests():
    src, _ = make_source(lambda r: ok({"user": {"user_id": 1, "username": "u"}}), delay=3.0)
    clock = {"t": 100.0}
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s

    src._monotonic = lambda: clock["t"]
    src._sleep = fake_sleep
    src.check_auth()
    clock["t"] += 1.0
    src.check_auth()
    assert sleeps == [pytest.approx(2.0)]


def test_known_steam_games():
    assert KNOWN_STEAM_GAMES["dota2"] == 570
    assert KNOWN_STEAM_GAMES["cs2"] == KNOWN_STEAM_GAMES["csgo"] == 730
