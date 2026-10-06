"""Тесты чата FunPay на фикстурах (сети нет — httpx.MockTransport поверх FunPaySource)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.settings import FunPaySettings
from app.sources.base import AuthError, SourceError
from app.sources.funpay import FunPaySource
from app.sources.funpay_chat import (
    BOT_MARK,
    HISTORY_FROM_END,
    FunPayChat,
    chat_url,
    parse_contact_items,
    parse_messages,
    random_tag,
)

FIXTURES = Path(__file__).parent / "fixtures" / "funpay"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFunPayChatServer:
    """Мини-сервер FunPay для чата: главная (логин), /chat/, /chat/history, /runner/."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logged_out = False
        self.send_response: dict | str = json.loads(load("chat_send_ok.json"))
        self.runner_no_changes = False
        self.history_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = dict(request.url.params)

        if path == "/":
            html = load("main_page.html")
            if self.logged_out:
                html = html.replace('<div class="user-link-name">TestSeller</div>', "")
            return httpx.Response(200, text=html, headers={"set-cookie": "PHPSESSID=sess123; path=/; HttpOnly"})
        if path == "/chat/":
            html = load("chat_list.html")
            if self.logged_out:
                html = html.replace('<div class="user-link-name">TestSeller</div>', "")
            return httpx.Response(200, text=html)
        if path == "/chat/history":
            if self.history_status != 200:
                return httpx.Response(self.history_status, text="")
            if params.get("node") == "5001001":
                return httpx.Response(200, json=json.loads(load("chat_history.json")))
            return httpx.Response(200, json={"chat": None})
        if path == "/runner/":
            form = self.form(request)
            if form.get("request") and form["request"] != "false":
                if isinstance(self.send_response, str):
                    return httpx.Response(200, text=self.send_response)
                return httpx.Response(200, json=self.send_response)
            data = json.loads(load("chat_runner_bookmarks.json"))
            if self.runner_no_changes:
                sent = {o["type"]: o for o in json.loads(form["objects"])}
                for obj in data["objects"]:
                    obj["data"] = False
                    obj["tag"] = sent[obj["type"]]["tag"]
            return httpx.Response(200, json=data)
        return httpx.Response(404, text="<html><body>Страница не найдена</body></html>")

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}

    def posted(self, path: str) -> dict[str, str]:
        for req in reversed(self.requests):
            if req.method == "POST" and req.url.path == path:
                return self.form(req)
        raise AssertionError(f"нет POST на {path}")


@pytest.fixture
def fake() -> FakeFunPayChatServer:
    return FakeFunPayChatServer()


@pytest.fixture
def chat(fake: FakeFunPayChatServer) -> FunPayChat:
    settings = FunPaySettings(golden_key="goldenkey-test", user_agent="TestUA/1.0", request_delay=0, timeout=5)
    source = FunPaySource(settings, transport=httpx.MockTransport(fake.handler))
    source.retry_backoff = 0
    return FunPayChat(source)


# ----------------------------------------------------------------------------- helpers
def test_random_tag_and_chat_url():
    tag = random_tag()
    assert len(tag) == 10 and tag.isalnum() and tag == tag.lower()
    assert random_tag() != random_tag()
    assert chat_url(5001001) == "https://funpay.com/chat/?node=5001001"


def test_parse_contact_items_tolerates_garbage():
    assert parse_contact_items("") == []
    assert parse_contact_items("<div>нет чатов</div>") == []
    html = ('<a class="contact-item unread" data-id="7" data-node-msg="x">'
            '<div class="contact-item-message">привет</div></a>'
            '<a class="contact-item" data-id="7"><div class="media-user-name">dup</div></a>'
            '<a class="contact-item">без id</a>')
    chats = parse_contact_items(html)
    assert len(chats) == 1
    assert chats[0] == {"chat_id": 7, "name": "", "last_message_id": None, "last_user_message_id": None,
                        "last_text": "привет", "time": None, "unread": True,
                        "url": "https://funpay.com/chat/?node=7"}


def test_parse_messages_without_names():
    raw = [{"id": "2", "author": "5", "html": "<div class='message-text'>два</div>"},
           {"id": 1, "author": 5, "html": "<div class='chat-msg-text'>один</div>"},
           {"id": "bad", "author": 5, "html": ""}, "мусор"]
    msgs = parse_messages(raw, my_id=9)
    assert [m["id"] for m in msgs] == [1, 2]
    assert [m["text"] for m in msgs] == ["один", "два"]
    assert all(m["author"] == "" and m["is_mine"] is False and m["system"] is False for m in msgs)
    assert parse_messages(None, 9) == [] and parse_messages({"x": 1}, 9) == []


# ----------------------------------------------------------------------------- list_chats
def test_list_chats(chat: FunPayChat, fake: FakeFunPayChatServer):
    chats = chat.list_chats()
    # первый запрос — авторизация на главной, затем страница чатов с cookie и PHPSESSID
    assert [str(r.url) for r in fake.requests] == ["https://funpay.com/", "https://funpay.com/chat/"]
    req = fake.requests[-1]
    assert req.method == "GET"
    assert "golden_key=goldenkey-test" in req.headers["cookie"] and "PHPSESSID=sess123" in req.headers["cookie"]
    assert req.headers["user-agent"] == "TestUA/1.0"

    assert [c["chat_id"] for c in chats] == [5001001, 5001002, 5001003]   # сломанный элемент пропущен
    a, b, c = chats
    assert a == {"chat_id": 5001001, "name": "Buyer_One", "last_message_id": 910004, "last_user_message_id": 910003,
                 "last_text": "Здравствуйте, аккаунт ещё в наличии?", "time": "12:34", "unread": True,
                 "url": "https://funpay.com/chat/?node=5001001"}
    assert b["unread"] is False and b["name"] == "EuroTrader" and b["last_message_id"] == 910010
    assert b["last_text"] == "Спасибо, всё получил 👍"
    assert c["unread"] is False and c["last_message_id"] is None and c["last_user_message_id"] is None
    assert c["url"] == "https://funpay.com/chat/?node=5001003"   # относительный href -> абсолютный

    # повторный вызов не авторизуется заново
    chat.list_chats()
    assert [r.url.path for r in fake.requests] == ["/", "/chat/", "/chat/"]


def test_list_chats_logged_out(chat: FunPayChat, fake: FakeFunPayChatServer):
    fake.logged_out = True
    with pytest.raises(AuthError, match="golden_key невалиден"):
        chat.list_chats()


def test_list_chats_page_without_auth_block(chat: FunPayChat, fake: FakeFunPayChatServer):
    chat.source.login()
    fake.logged_out = True   # главная уже не запрашивается, но страница чатов пришла без блока пользователя
    with pytest.raises(AuthError, match="страница чатов без авторизации"):
        chat.list_chats()


# ----------------------------------------------------------------------------- history
def test_get_history(chat: FunPayChat, fake: FakeFunPayChatServer):
    msgs = chat.get_history(5001001)
    req = fake.requests[-1]
    assert req.method == "GET" and req.url.path == "/chat/history"
    assert dict(req.url.params) == {"node": "5001001", "last_message": str(HISTORY_FROM_END)}
    assert req.headers["x-requested-with"] == "XMLHttpRequest"

    assert [m["id"] for m in msgs] == [910000, 910001, 910002, 910003, 910004]
    system, first, image, mine, last = msgs
    assert system["system"] is True and system["author_id"] == 0 and system["author"] == "FunPay"
    assert system["text"].startswith("Покупатель Buyer_One оплатил заказ #ABC12345")
    assert system["is_mine"] is False

    assert first["author_id"] == 5001 and first["author"] == "Buyer_One" and first["is_mine"] is False
    assert first["text"] == "Привет! Аккаунт в наличии?" and first["ts"] == "06.10.2026 12:30:00"

    assert image["image_url"] == "https://funpay.com/uploads/chat/123.png" and image["text"] == ""
    assert image["author"] == "Buyer_One"   # имя взято из первого сообщения серии

    assert mine["is_mine"] is True and mine["author_id"] == 777001 and mine["author"] == "TestSeller"
    assert mine["text"] == "Да, в наличии ✅" and BOT_MARK not in mine["text"]

    assert last["author"] == "Buyer_One" and last["is_mine"] is False and last["system"] is False
    assert last["text"] == "Здравствуйте, аккаунт ещё в наличии?"


def test_get_history_params_and_name(chat: FunPayChat, fake: FakeFunPayChatServer):
    msgs = chat.get_history(5001001, last_message_id=910002, interlocutor_name="Покупатель")
    assert dict(fake.requests[-1].url.params) == {"node": "5001001", "last_message": "910002"}
    # имя собеседника из параметра имеет приоритет над HTML
    assert msgs[1]["author"] == "Покупатель"
    assert chat.get_history(5009999) == []
    fake.history_status = 404
    assert chat.get_history(5001001) == []
    fake.history_status = 500
    with pytest.raises(SourceError, match="500"):
        chat.get_history(5001001)


# ----------------------------------------------------------------------------- runner / poll
def test_poll_updates(chat: FunPayChat, fake: FakeFunPayChatServer):
    state: dict = {}
    upd = chat.poll_updates(state)

    form = fake.posted("/runner/")
    assert form["csrf_token"] == "csrf-token-test-123"
    assert form["request"] == "false"
    objects = json.loads(form["objects"])
    assert [o["type"] for o in objects] == ["orders_counters", "chat_bookmarks"]
    for o in objects:
        assert o["id"] == 777001 and o["data"] is False and len(o["tag"]) == 10
    req = fake.requests[-1]
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")

    assert upd["changed"] is True
    assert upd["orders_counters"] == {"buyer": 0, "seller": 2}
    assert [c["chat_id"] for c in upd["chats"]] == [5001001, 5001004]
    assert upd["chats"][0]["unread"] is True and upd["chats"][0]["last_message_id"] == 910005
    assert upd["chats"][0]["last_text"] == "Беру, как оплатить?"
    assert upd["chats"][1]["unread"] is False and upd["chats"][1]["name"] == "NewGuy"
    assert state["tags"] == {"orders_counters": "tag-orders-2", "chat_bookmarks": "tag-chats-2"}

    # следующий опрос отправляет сохранённые теги; без изменений — пустой результат, теги на месте
    fake.runner_no_changes = True
    upd2 = chat.poll_updates(state)
    sent = {o["type"]: o["tag"] for o in json.loads(fake.posted("/runner/")["objects"])}
    assert sent == {"orders_counters": "tag-orders-2", "chat_bookmarks": "tag-chats-2"}
    assert upd2 == {"chats": [], "orders_counters": None, "changed": False}
    assert state["tags"] == {"orders_counters": "tag-orders-2", "chat_bookmarks": "tag-chats-2"}


# ----------------------------------------------------------------------------- send
def test_send_message_payload(chat: FunPayChat, fake: FakeFunPayChatServer):
    assert chat.send_message(5001001, "Здравствуйте! Аккаунт в наличии ✅") is True
    req = fake.requests[-1]
    assert req.method == "POST" and req.url.path == "/runner/"
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "golden_key=goldenkey-test" in req.headers["cookie"]

    form = fake.posted("/runner/")
    assert form["csrf_token"] == "csrf-token-test-123"
    request = json.loads(form["request"])
    assert request["action"] == "chat_message"
    assert request["data"] == {"node": 5001001, "last_message": -1, "content": "Здравствуйте! Аккаунт в наличии ✅"}
    objects = json.loads(form["objects"])
    assert isinstance(objects, list) and objects[0]["type"] == "chat_node" and objects[0]["id"] == 5001001
    assert chat.last_sent_message_id == 910006


def test_send_message_errors(chat: FunPayChat, fake: FakeFunPayChatServer):
    fake.send_response = json.loads(load("chat_send_error.json"))
    with pytest.raises(SourceError, match="Слишком длинное сообщение"):
        chat.send_message(5001001, "x" * 10)

    fake.send_response = {"objects": []}
    with pytest.raises(SourceError, match="не подтвердил"):
        chat.send_message(5001001, "привет")

    fake.send_response = "<html>login page</html>"
    with pytest.raises(SourceError, match="не JSON"):
        chat.send_message(5001001, "привет")

    n = len(fake.requests)
    with pytest.raises(SourceError, match="пустое сообщение"):
        chat.send_message(5001001, "   ")
    assert len(fake.requests) == n   # пустой текст не отправляется в сеть


def test_send_message_requires_auth(fake: FakeFunPayChatServer):
    fake.logged_out = True
    settings = FunPaySettings(golden_key="goldenkey-test", request_delay=0)
    chat = FunPayChat(FunPaySource(settings, transport=httpx.MockTransport(fake.handler)))
    with pytest.raises(AuthError):
        chat.send_message(5001001, "привет")
    assert all(r.url.path == "/" for r in fake.requests)
