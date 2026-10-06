"""Тесты источника FunPay на фикстурах (сети нет — httpx.MockTransport)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.models import Profile
from app.settings import FunPaySettings
from app.sources.base import AuthError, SourceError
from app.sources.funpay import (
    FunPaySource,
    currency_from_text,
    parse_lot_form_html,
    price_from_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "funpay"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFunPay:
    """Мини-сервер FunPay: маршрутизация по пути URL, запись всех запросов."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logged_out = False
        self.fail_times = 0            # сколько первых запросов вернуть 500
        self.raise_times = 0           # сколько первых запросов «уронить» сетевой ошибкой
        self.forbidden = False
        self.save_response: dict | str = {"done": True, "error": None, "url": "https://funpay.com/lots/148/trade"}
        self.save_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_times > 0:
            self.raise_times -= 1
            raise httpx.ConnectError("connection reset", request=request)
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(500, text="Internal Server Error")
        if self.forbidden:
            return httpx.Response(403, text="Forbidden")

        path = request.url.path
        params = dict(request.url.params)

        if path == "/":
            html = load("main_page.html")
            if self.logged_out:
                html = html.replace('<div class="user-link-name">TestSeller</div>', "")
            return httpx.Response(200, text=html, headers={"set-cookie": "PHPSESSID=sess123; path=/; HttpOnly"})
        if path == "/lots/148/":
            return httpx.Response(200, text=load("lots_list.html"))
        if path == "/lots/148/trade":
            return httpx.Response(200, text=load("my_lots.html"))
        if path == "/lots/81/":
            return httpx.Response(200, text=load("lots_list.html").replace("tc-item", "tc-item-none"))
        if path == "/lots/offer":
            lot = params.get("id")
            if lot == "1001":
                return httpx.Response(200, text=load("lot_page.html"))
            if lot == "1002":
                return httpx.Response(200, text=load("lot_missing.html"))
            if lot == "1003":
                return httpx.Response(404, text="<html><body>Страница не найдена</body></html>")
            if lot == "1004":
                return httpx.Response(302, headers={"location": "https://funpay.com/lots/148/"})
            return httpx.Response(404, text="")
        if path == "/lots/offerEdit":
            data = json.loads(load("offer_edit.json"))
            if params.get("offer"):
                data["html"] = (data["html"]
                                .replace('name="offer_id" value="0"', f'name="offer_id" value="{params["offer"]}"')
                                .replace('name="fields[summary][ru]" value=""',
                                         'name="fields[summary][ru]" value="WoT | 15 топов, Об. 279(р) | RU"')
                                .replace('name="price" value=""', 'name="price" value="28990"')
                                .replace('<option value="101">RU</option>', '<option value="101" selected>RU</option>'))
            return httpx.Response(200, json=data)
        if path == "/lots/offerSave":
            if isinstance(self.save_response, str):
                return httpx.Response(self.save_status, text=self.save_response)
            return httpx.Response(self.save_status, json=self.save_response)
        if path == "/users/5001/":
            return httpx.Response(200, text=load("user_page.html"))
        return httpx.Response(404, text="<html><body>Страница не найдена</body></html>")

    def posted(self, path: str) -> dict[str, str]:
        """Тело последнего POST на path как dict (urlencoded)."""
        for req in reversed(self.requests):
            if req.method == "POST" and req.url.path == path:
                return {k: v[0] for k, v in parse_qs(req.content.decode("utf-8"), keep_blank_values=True).items()}
        raise AssertionError(f"нет POST на {path}")


@pytest.fixture
def fake() -> FakeFunPay:
    return FakeFunPay()


@pytest.fixture
def src(fake: FakeFunPay) -> FunPaySource:
    settings = FunPaySettings(golden_key="goldenkey-test", user_agent="TestUA/1.0", request_delay=0, timeout=5)
    source = FunPaySource(settings, transport=httpx.MockTransport(fake.handler))
    source.retry_backoff = 0
    return source


def make_profile(**funpay) -> Profile:
    return Profile(id="wot", name="WoT", game="wot", sources={"funpay": {"enabled": True, **funpay}})


# ----------------------------------------------------------------------------- helpers
def test_price_from_text():
    assert price_from_text("15 000 ₽") == 15000.0
    assert price_from_text("15\xa0000 ₽") == 15000.0
    assert price_from_text("1 234,56 ₽") == 1234.56
    assert price_from_text("$120") == 120.0
    assert price_from_text("15000.0") == 15000.0
    assert price_from_text("") is None
    assert price_from_text(None) is None
    assert price_from_text("нет цены") is None


def test_currency_from_text():
    assert currency_from_text("15 000 ₽") == "RUB"
    assert currency_from_text("$120") == "USD"
    assert currency_from_text("99 €") == "EUR"
    assert currency_from_text("100 грн") == "UAH"
    assert currency_from_text("") == "RUB"
    assert currency_from_text(None) == "RUB"


# ----------------------------------------------------------------------------- auth / categories
def test_login_and_categories(src: FunPaySource, fake: FakeFunPay):
    info = src.login()
    assert info["username"] == "TestSeller"
    assert src.username == "TestSeller"
    assert src.user_id == 777001
    assert src.csrf_token == "csrf-token-test-123"
    assert src.phpsessid == "sess123"

    req = fake.requests[0]
    assert req.url == "https://funpay.com/"
    assert req.headers["user-agent"] == "TestUA/1.0"
    assert req.headers["accept-language"].startswith("ru-RU")
    assert "golden_key=goldenkey-test" in req.headers["cookie"]

    cats = src.categories()
    assert [c["name"] for c in cats] == ["World of Tanks", "Dota 2", "World of Tanks Blitz"]
    wot = cats[0]
    assert wot["id"] == 4
    assert wot["subcategories"] == [
        {"id": 148, "name": "Аккаунты", "type": "common"},
        {"id": 2, "name": "Золото", "type": "currency"},
        {"id": 149, "name": "Бонус-коды", "type": "common"},
    ]
    # категории кэшируются — второй вызов не ходит в сеть
    n = len(fake.requests)
    src.categories()
    assert len(fake.requests) == n

    # PHPSESSID отправляется в следующих запросах
    src.list_lots(148)
    assert "PHPSESSID=sess123" in fake.requests[-1].headers["cookie"]


def test_login_invalid_key(src: FunPaySource, fake: FakeFunPay):
    fake.logged_out = True
    with pytest.raises(AuthError, match="golden_key невалиден или истёк"):
        src.login()
    assert src.check_auth() == {"ok": False, "username": None, "user_id": None,
                                "error": "golden_key невалиден или истёк"}


def test_login_without_key(fake: FakeFunPay):
    source = FunPaySource(FunPaySettings(golden_key="", request_delay=0), transport=httpx.MockTransport(fake.handler))
    with pytest.raises(AuthError):
        source.login()
    assert fake.requests == []


def test_http_403_is_auth_error(src: FunPaySource, fake: FakeFunPay):
    fake.forbidden = True
    with pytest.raises(AuthError):
        src.list_lots(148)


def test_check_auth_ok(src: FunPaySource):
    assert src.check_auth() == {"ok": True, "username": "TestSeller", "user_id": 777001, "error": None}


def test_retry_on_5xx_and_network_errors(src: FunPaySource, fake: FakeFunPay):
    fake.fail_times = 2
    assert len(src.list_lots(148)) == 4
    assert len(fake.requests) == 3

    fake.requests.clear()
    fake.raise_times = 1
    assert len(src.list_lots(148)) == 4
    assert len(fake.requests) == 2

    fake.requests.clear()
    fake.fail_times = 3
    with pytest.raises(SourceError, match="500"):
        src.list_lots(148)
    assert len(fake.requests) == 3

    fake.requests.clear()
    fake.raise_times = 5
    with pytest.raises(SourceError, match="Ошибка сети"):
        src.list_lots(148)


def test_find_subcategory(src: FunPaySource):
    assert src.find_subcategory("World of Tanks") == 148
    assert src.find_subcategory("world of tanks", "Золото") == 2
    assert src.find_subcategory("WORLD OF TANKS", "аккаунт") == 148
    assert src.find_subcategory("World of Tanks Blitz") == 1181
    assert src.find_subcategory("Dota") == 81
    assert src.find_subcategory("tanks blitz") == 1181
    assert src.find_subcategory("Dota 2", "Предметы") == 82
    assert src.find_subcategory("Dota 2", "Золото") is None
    assert src.find_subcategory("Unknown Game") is None
    assert src.find_subcategory("") is None


def test_resolve_subcategory_ids(src: FunPaySource):
    assert src.resolve_subcategory_ids(make_profile(subcategory_ids=[148, 81]).funpay()) == [148, 81]
    assert src.resolve_subcategory_ids(make_profile(subcategory_id=149).funpay()) == [149]
    assert src.resolve_subcategory_ids(make_profile(game_query="World of Tanks").funpay()) == [148]
    assert src.resolve_subcategory_ids(make_profile(game_query="Dota 2", subcategory_query="Предметы").funpay()) == [82]
    with pytest.raises(SourceError):
        src.resolve_subcategory_ids(make_profile(game_query="Nope").funpay())
    with pytest.raises(SourceError):
        src.resolve_subcategory_ids(make_profile().funpay())


# ----------------------------------------------------------------------------- списки
def test_list_lots_parsing(src: FunPaySource, fake: FakeFunPay):
    lots = src.list_lots(148)
    assert [l.source_id for l in lots] == ["1001", "1002", "1003", "1004"]
    assert fake.requests[-1].url == "https://funpay.com/lots/148/"

    a = lots[0]
    assert a.source == "funpay"
    assert a.key == "funpay:1001"
    assert a.url == "https://funpay.com/lots/offer?id=1001"
    assert a.title == "WoT аккаунт 15 топов, Объект 279(р), 60 000 боёв"
    assert a.price == 15000.0
    assert a.currency == "RUB"
    assert a.region == "RU"
    assert a.seller_name == "Ivan_Seller"
    assert a.seller_url == "https://funpay.com/users/5001/"
    assert a.seller_id == "5001"
    assert a.online is True
    assert a.attributes["server"] == "RU"
    assert a.attributes["side"] is None
    assert a.attributes["seller_reviews"] == 154
    assert a.attributes["data_f"] == {"server": "101", "side": ""}
    assert a.attributes["subcategory_id"] == 148
    assert a.attributes["seller_info"] == "3 года"

    b = lots[1]
    assert b.region == "EU"
    assert b.attributes["side"] == "Альянс"
    assert b.seller_name == "EuroTrader"
    assert b.seller_id == "5002"
    assert b.attributes["seller_reviews"] == 12
    assert b.online is False          # нет data-online и нет класса online
    assert b.price == 8500.0

    c = lots[2]
    assert c.region == "RU"           # вариант tc-server hidden-xs
    assert c.price == 120.0
    assert c.currency == "USD"
    assert c.attributes["seller_reviews"] == 0
    assert c.online is False

    d = lots[3]
    assert d.region == "NA"
    assert d.price == 2300.0
    assert d.attributes["amount"] == 3
    assert d.seller_name == "na_dealer"
    assert d.online is True

    assert len(src.list_lots(148, max_items=2)) == 2
    assert src.list_lots(81) == []
    with pytest.raises(SourceError, match="404"):
        src.list_lots(999)


def test_list_lots_extra_query(src: FunPaySource, fake: FakeFunPay):
    src.list_lots(148, extra_query={"f-server": "101"})
    assert dict(fake.requests[-1].url.params) == {"f-server": "101"}


def test_list_filters(src: FunPaySource):
    filters = src.list_filters(148)
    by_name = {f["name"]: f for f in filters}
    assert list(by_name) == ["f-server", "f-side", "f-online", "f-search"]
    assert by_name["f-server"]["label"] == "Сервер — любой"
    assert by_name["f-server"]["options"] == [
        {"value": "", "label": "Сервер — любой"},
        {"value": "101", "label": "RU"},
        {"value": "102", "label": "EU"},
        {"value": "103", "label": "NA"},
    ]
    assert by_name["f-side"]["label"] == "Сторона"
    assert by_name["f-online"]["type"] == "checkbox"
    assert by_name["f-online"]["options"] == [{"value": "1", "label": "Только продавцы онлайн"}]
    assert by_name["f-search"]["label"] == "Поиск по описанию"


# ----------------------------------------------------------------------------- search
def test_search_with_server_filter(src: FunPaySource, fake: FakeFunPay):
    profile = make_profile(subcategory_id=148, server_filter=["ru"], extra_query={"f-online": "1"})
    found = src.search(profile)
    assert [l.source_id for l in found] == ["1001", "1003"]
    assert all(l.game == "wot" for l in found)
    assert dict(fake.requests[-1].url.params) == {"f-online": "1"}

    found = src.search(make_profile(subcategory_id=148, server_filter=["EU", "na"]))
    assert [l.source_id for l in found] == ["1002", "1004"]


def test_search_limit_and_dedup(src: FunPaySource):
    found = src.search(make_profile(subcategory_ids=[148, 148]))
    assert [l.source_id for l in found] == ["1001", "1002", "1003", "1004"]
    assert len(src.search(make_profile(subcategory_id=148), limit=2)) == 2
    assert src.search(make_profile(game_query="World of Tanks"))[0].source_id == "1001"


def test_search_disabled(src: FunPaySource, fake: FakeFunPay):
    profile = Profile(id="x", name="x", game="x", sources={"funpay": {"enabled": False}})
    assert src.search(profile) == []
    assert fake.requests == []


# ----------------------------------------------------------------------------- лот
def test_get_listing(src: FunPaySource, fake: FakeFunPay):
    lot = src.get_listing("1001")
    assert lot is not None
    assert fake.requests[-1].url == "https://funpay.com/lots/offer?id=1001"
    assert lot.source_id == "1001"
    assert lot.title == "WoT аккаунт 15 топов, Объект 279(р), 60 000 боёв"
    assert lot.description == "Аккаунт World of Tanks RU.\n15 топов, Объект 279(р), Chieftain.\nПривязка к почте, полный доступ."
    assert lot.price == 15000.0
    assert lot.currency == "RUB"
    assert lot.region == "RU"
    assert lot.seller_name == "Ivan_Seller"
    assert lot.seller_url == "https://funpay.com/users/5001/"
    assert lot.seller_id == "5001"
    assert lot.online is True
    assert lot.attributes["сервер"] == "RU"
    assert lot.attributes["способ передачи"] == "Чат"
    assert lot.attributes["subcategory_id"] == 148
    assert lot.attributes["seller_reviews"] == 154
    assert "15" in lot.attributes["цена"]


def test_get_listing_missing(src: FunPaySource):
    assert src.get_listing("1002") is None    # 200, но «Предложение не найдено»
    assert src.get_listing("1003") is None    # 404
    assert src.get_listing("1004") is None    # 302 на страницу категории


def test_is_available(src: FunPaySource, fake: FakeFunPay):
    assert src.is_available("1001") is True
    assert src.is_available("1002") is False
    assert src.is_available("1003") is False
    assert src.is_available("1004") is False
    fake.raise_times = 10
    assert src.is_available("1001") is None


def test_get_seller(src: FunPaySource):
    seller = src.get_seller(5001)
    assert seller is not None
    assert seller["id"] == "5001"
    assert seller["name"] == "Ivan_Seller"
    assert seller["reviews"] == 154
    assert seller["online"] is True
    lots = seller["lots"]
    assert [l.source_id for l in lots] == ["1001", "1010", "2001"]
    assert lots[0].attributes["subcategory_id"] == 148
    assert lots[0].attributes["subcategory_name"] == "World of Tanks, Аккаунты"
    assert lots[2].attributes["subcategory_id"] == 81
    assert lots[2].price == 3500.0
    assert src.get_seller(9999) is None


# ----------------------------------------------------------------------------- форма лота
def test_get_lot_form_schema(src: FunPaySource, fake: FakeFunPay):
    form = src.get_lot_form(148)
    req = fake.requests[-1]
    assert req.url.path == "/lots/offerEdit"
    assert dict(req.url.params) == {"node": "148"}
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    assert req.headers["accept"] == "*/*"

    fields = form["fields"]
    assert fields["csrf_token"] == "csrf-token-test-123"
    assert fields["offer_id"] == "0"
    assert fields["node_id"] == "148"
    assert fields["location"] == "trade"
    assert fields["fields[server]"] == ""          # первая option
    assert fields["fields[type]"] == "1"           # selected
    assert fields["fields[summary][ru]"] == ""
    assert fields["fields[desc][ru]"] == ""
    assert fields["price"] == ""
    assert fields["amount"] == "1"
    assert fields["active"] == "on"                # checked
    assert fields["deactivate_after_sale"] == ""   # не отмечен, но ключ гарантирован
    assert "fields[auto_delivery]" not in fields   # прочие неотмеченные чекбоксы не отправляются
    assert "submit" not in fields

    schema = {s["name"]: s for s in form["schema"]}
    assert schema["fields[server]"]["type"] == "select"
    assert schema["fields[server]"]["label"] == "Сервер"
    assert schema["fields[server]"]["options"][1] == {"value": "101", "label": "RU"}
    assert schema["fields[summary][ru]"] == {"name": "fields[summary][ru]", "type": "text",
                                             "label": "Краткое описание (RU)", "value": "", "options": [],
                                             "required": False}
    assert schema["fields[desc][ru]"]["type"] == "textarea"
    assert schema["price"]["type"] == "number"
    assert schema["price"]["required"] is True
    assert schema["price"]["label"] == "Цена, ₽"
    assert schema["csrf_token"]["type"] == "hidden"
    assert schema["active"]["type"] == "checkbox"
    assert schema["active"]["value"] == "on"
    assert schema["deactivate_after_sale"]["value"] == ""
    assert schema["fields[auto_delivery]"]["label"] == "Автовыдача"
    assert schema["fields[payment_msg][ru]"]["label"] == "Сообщение покупателю после оплаты"


def test_parse_lot_form_html_fallbacks():
    form = parse_lot_form_html("<div><input name='a' value='1'><input type='radio' name='r' value='x'>"
                               "<input type='radio' name='r' value='y' checked><select name='s'></select></div>")
    assert form["fields"] == {"a": "1", "r": "y", "s": ""}
    assert [s["name"] for s in form["schema"]] == ["a", "r", "s"]
    assert form["schema"][1]["options"] == [{"value": "x", "label": "r"}, {"value": "y", "label": "r"}]


def test_get_lot_form_not_json(src: FunPaySource, fake: FakeFunPay):
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/lots/offerEdit":
            return httpx.Response(200, text="<html>login</html>")
        return original(request)

    src._client = httpx.Client(base_url="https://funpay.com", transport=httpx.MockTransport(handler),
                               follow_redirects=False)
    with pytest.raises(SourceError, match="не JSON"):
        src.get_lot_form(148)


# ----------------------------------------------------------------------------- создание / редактирование
def test_create_lot(src: FunPaySource, fake: FakeFunPay):
    result = src.create_lot(
        148,
        title_ru="WoT | 15 топов, Об. 279(р) | RU",
        title_en="WoT | 15 tier X | RU",
        description_ru="Описание\nвторая строка",
        description_en="Description",
        price=28990,
        amount=1,
        active=True,
        deactivate_after_sale=True,
        extra_fields={"fields[server]": "101", "fields[type]": "2"},
    )
    assert result["lot_id"] == 9001
    assert result["url"] == "https://funpay.com/lots/offer?id=9001"
    assert result["response"]["done"] is True

    # порядок запросов: главная (login) -> форма -> сохранение -> мои лоты
    paths = [(r.method, r.url.path) for r in fake.requests]
    assert paths == [("GET", "/"), ("GET", "/lots/offerEdit"), ("POST", "/lots/offerSave"),
                     ("GET", "/lots/148/trade")]

    post = next(r for r in fake.requests if r.method == "POST")
    assert post.headers["x-requested-with"] == "XMLHttpRequest"
    assert post.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "golden_key=goldenkey-test" in post.headers["cookie"]

    body = fake.posted("/lots/offerSave")
    assert body["csrf_token"] == "csrf-token-test-123"
    assert body["offer_id"] == "0"
    assert body["node_id"] == "148"
    assert body["location"] == "trade"
    assert body["fields[summary][ru]"] == "WoT | 15 топов, Об. 279(р) | RU"
    assert body["fields[summary][en]"] == "WoT | 15 tier X | RU"
    assert body["fields[desc][ru]"] == "Описание\nвторая строка"
    assert body["fields[desc][en]"] == "Description"
    assert body["price"] == "28990"
    assert body["amount"] == "1"
    assert body["active"] == "on"
    assert body["deactivate_after_sale"] == "on"
    assert body["fields[server]"] == "101"      # extra_fields перекрывает значение формы
    assert body["fields[type]"] == "2"
    assert body["fields[payment_msg][ru]"] == ""
    assert "fields[auto_delivery]" not in body


def test_create_lot_inactive_and_id_not_found(src: FunPaySource, fake: FakeFunPay):
    result = src.create_lot(148, "Совсем другой заголовок", price=100.5, active=False, deactivate_after_sale=False)
    assert result["lot_id"] is None
    assert result["url"] is None
    body = fake.posted("/lots/offerSave")
    assert body["active"] == ""
    assert body["deactivate_after_sale"] == ""
    assert body["price"] == "100.5"


def test_create_lot_validation(src: FunPaySource, fake: FakeFunPay):
    with pytest.raises(SourceError, match="цена"):
        src.create_lot(148, "x", price=0)
    with pytest.raises(SourceError, match="title_ru"):
        src.create_lot(148, "  ", price=10)
    assert fake.requests == []


def test_create_lot_error_response(src: FunPaySource, fake: FakeFunPay):
    fake.save_response = {"error": "Заполните поле «Сервер»", "errors": {"fields[server]": "обязательное поле"}}
    with pytest.raises(SourceError, match="Заполните поле «Сервер»"):
        src.create_lot(148, "Title", price=100)
    assert not any(r.url.path == "/lots/148/trade" for r in fake.requests)

    fake.save_response = '<html><div class="alert alert-danger">Слишком длинное описание</div></html>'
    with pytest.raises(SourceError, match="Слишком длинное описание"):
        src.create_lot(148, "Title", price=100)

    fake.save_response = {"done": True}
    fake.save_status = 500
    with pytest.raises(SourceError):
        src.create_lot(148, "Title", price=100)


def test_set_lot_active(src: FunPaySource, fake: FakeFunPay):
    result = src.set_lot_active(9001, 148, False)
    assert result["lot_id"] == 9001
    edit = next(r for r in fake.requests if r.url.path == "/lots/offerEdit")
    assert dict(edit.url.params) == {"node": "148", "offer": "9001"}
    body = fake.posted("/lots/offerSave")
    assert body["offer_id"] == "9001"
    assert body["node_id"] == "148"
    assert body["active"] == ""
    assert body["fields[summary][ru]"] == "WoT | 15 топов, Об. 279(р) | RU"   # остальные поля сохранены
    assert body["fields[server]"] == "101"
    assert body["price"] == "28990"

    src.set_lot_active(9001, 148, True)
    assert fake.posted("/lots/offerSave")["active"] == "on"


def test_set_lot_price_and_update(src: FunPaySource, fake: FakeFunPay):
    src.set_lot_price(9001, 148, 31990)
    body = fake.posted("/lots/offerSave")
    assert body["price"] == "31990"
    assert body["offer_id"] == "9001"

    src.update_lot(9001, 148, title_ru="Новый заголовок", description_en="New", fields={"fields[server]": "102"},
                   amount=2)
    body = fake.posted("/lots/offerSave")
    assert body["fields[summary][ru]"] == "Новый заголовок"
    assert body["fields[desc][en]"] == "New"
    assert body["fields[server]"] == "102"
    assert body["amount"] == "2"


def test_delete_lot(src: FunPaySource, fake: FakeFunPay):
    src.delete_lot(9001, 148)
    body = fake.posted("/lots/offerSave")
    assert body["deleted"] == "1"
    assert body["offer_id"] == "9001"


def test_list_my_lots(src: FunPaySource):
    lots = src.list_my_lots(148)
    assert [l.source_id for l in lots] == ["8999", "9001", "9000"]
    assert lots[0].attributes["active"] is False     # класс warning = неактивный
    assert lots[1].attributes["active"] is True
    assert lots[1].url == "https://funpay.com/lots/offer?id=9001"
    assert lots[1].price == 28990.0


def test_storage_events(fake: FakeFunPay):
    class Store:
        def __init__(self):
            self.events = []

        def log(self, kind, message, level="info", data=None):
            self.events.append((kind, level, message, data))

    store = Store()
    settings = FunPaySettings(golden_key="goldenkey-test", request_delay=0)
    source = FunPaySource(settings, storage=store, transport=httpx.MockTransport(fake.handler))
    source.retry_backoff = 0
    source.create_lot(148, "WoT | 15 топов, Об. 279(р) | RU", price=28990)
    assert store.events and store.events[-1][0] == "funpay"
    assert "9001" in store.events[-1][2]
