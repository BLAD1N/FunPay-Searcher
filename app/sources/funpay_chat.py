"""Чат FunPay: список диалогов, история, опрос обновлений (runner) и отправка сообщений.

Модуль не ходит в сеть сам — он переиспользует запросную машинерию :class:`~app.sources.funpay.FunPaySource`
(cookie ``golden_key``/``PHPSESSID``, user-agent, csrf-токен, паузы между запросами, повторы, 403 -> AuthError).

Протокол FunPay (без официального API, повторяем поведение браузера):

- ``GET /chat/`` — страница чатов. Каждый диалог — ``a.contact-item`` с атрибутами ``data-id`` (id чата/узла),
  ``data-node-msg`` (id последнего сообщения в чате), ``data-user-msg`` (id последнего сообщения, которое
  видел текущий пользователь); внутри ``div.media-user-name`` (собеседник), ``div.contact-item-message``
  (превью последнего сообщения, до 250 символов), ``div.contact-item-time``; класс ``unread`` — есть непрочитанное.
- ``GET /chat/history?node=N&last_message=M`` (XHR) — JSON ``{"chat": {"node": {"name": "users-A-B", ...},
  "messages": [{"id", "author", "html"}, ...]}}``. ``author`` — id автора (0 — системное сообщение FunPay),
  ``html`` — разметка сообщения: ``div.media-user-name a`` (имя автора; у «склеенных» подряд сообщений
  блока нет), ``div.message-text`` (текст), ``a.chat-img-link`` (картинка), ``div.chat-msg-date[title]`` (время).
- ``POST /runner/`` с ``objects=[{"type": "orders_counters"|"chat_bookmarks", "id": user_id, "tag": tag,
  "data": false}]`` — опрос обновлений: FunPay возвращает объекты с новым ``tag`` и ``data`` (для
  ``chat_bookmarks`` — ``{"html": "<a class=contact-item ...>..."}`` только изменившихся чатов; если изменений
  нет — ``data`` пустое). Теги нужно передавать в следующий запрос — они хранятся в ``state``.
- ``POST /runner/`` с ``request={"action": "chat_message", "data": {"node": N, "last_message": -1,
  "content": text}}`` — отправка сообщения; ответ ``{"response": {"error": null|"текст"}, "objects": [...]}``.

Все разборы HTML защищены: неожиданная вёрстка приводит к пропуску элемента или к :class:`SourceError`
с понятным сообщением, но никогда к «голому» AttributeError.
"""
from __future__ import annotations

import json
import logging
import random
import string
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import AuthError, SourceError
from .funpay import BASE_URL, FunPaySource

log = logging.getLogger("funpay.chat")

# FunPay возвращает последние сообщения чата, если last_message больше любого существующего id
HISTORY_FROM_END = 99999999999999999999999
# FunPayCardinal помечает «ботовские» сообщения невидимым символом в начале — убираем его из текста
BOT_MARK = "⁤"
_FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"}


# ---------------------------------------------------------------------------
# Вспомогательные функции (без состояния)
# ---------------------------------------------------------------------------
def random_tag() -> str:
    """Случайный тег для первого запроса к ``/runner/`` (как делает браузер)."""
    return "".join(random.choice(string.digits + string.ascii_lowercase) for _ in range(10))


def chat_url(chat_id: int | str) -> str:
    return f"{BASE_URL}/chat/?node={chat_id}"


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(el: Optional[Tag]) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def parse_contact_item(a: Tag) -> Optional[dict]:
    """Один ``a.contact-item`` -> словарь чата (None, если нет корректного ``data-id``)."""
    chat_id = _to_int(a.get("data-id"))
    if chat_id is None:
        return None
    classes = a.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    last_message_id = _to_int(a.get("data-node-msg"))
    user_message_id = _to_int(a.get("data-user-msg"))
    href = a.get("href") or ""
    return {
        "chat_id": chat_id,
        "name": _text(a.select_one(".media-user-name")),
        "last_message_id": last_message_id,
        "last_user_message_id": user_message_id,
        "last_text": _text(a.select_one(".contact-item-message")).replace(BOT_MARK, ""),
        "time": _text(a.select_one(".contact-item-time")) or None,
        "unread": "unread" in classes,
        "url": urljoin(BASE_URL + "/", href) if href else chat_url(chat_id),
    }


def parse_contact_items(html: str | Tag | BeautifulSoup) -> list[dict]:
    """Все ``a.contact-item`` из HTML (страница ``/chat/`` или ``data.html`` из ответа runner'а)."""
    root = html if isinstance(html, (Tag, BeautifulSoup)) else _soup(html)
    chats: list[dict] = []
    seen: set[int] = set()
    for a in root.select("a.contact-item"):
        try:
            chat = parse_contact_item(a)
        except Exception as e:  # noqa: BLE001
            log.warning("FunPay чат: не удалось разобрать элемент списка чатов: %s", e)
            continue
        if chat is None or chat["chat_id"] in seen:
            continue
        seen.add(chat["chat_id"])
        chats.append(chat)
    return chats


def _interlocutor_id(node: Any, my_id: Optional[int]) -> Optional[int]:
    """Из ``chat.node.name`` вида ``users-777001-5001`` достать id собеседника (не наш)."""
    name = node.get("name") if isinstance(node, dict) else None
    if not isinstance(name, str) or not name.startswith("users-"):
        return None
    ids = [_to_int(p) for p in name.split("-")[1:]]
    ids = [i for i in ids if i is not None]
    for i in ids:
        if i != my_id:
            return i
    return ids[0] if ids else None


def parse_messages(raw_messages: Any, my_id: Optional[int], names: Optional[dict[int, str]] = None) -> list[dict]:
    """Список ``{"id", "author", "html"}`` из JSON истории -> наши словари сообщений.

    ``names`` — известные имена авторов по id (наш id, собеседник); дополняется именами из HTML, так как
    FunPay показывает блок автора только у первого сообщения в серии.
    """
    names = dict(names or {})
    names.setdefault(0, "FunPay")
    out: list[dict] = []
    if not isinstance(raw_messages, list):
        return out
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        msg_id = _to_int(raw.get("id"))
        if msg_id is None:
            continue
        author_id = _to_int(raw.get("author"))
        soup = _soup(str(raw.get("html") or ""))
        author_el = soup.select_one(".media-user-name")
        if author_el is not None and author_id is not None and author_id not in names:
            link = author_el.find("a")
            name = _text(link) or _text(author_el)
            if name:
                names[author_id] = name
        image = soup.select_one("a.chat-img-link")
        image_url = image.get("href") if image is not None else None
        text_el = soup.select_one(".message-text, .chat-msg-text")
        if text_el is not None:
            text = text_el.get_text("\n", strip=True)
        elif author_id == 0:
            text = _text(soup.select_one(".alert")) or _text(soup)
        elif image_url:
            text = ""
        else:
            text = _text(soup)
        if text.startswith(BOT_MARK):
            text = text[len(BOT_MARK):]
        date_el = soup.select_one(".chat-msg-date")
        ts = (date_el.get("title") or _text(date_el)) if date_el is not None else None
        out.append({
            "id": msg_id,
            "author_id": author_id,
            "author": names.get(author_id, "") if author_id is not None else "",
            "text": text.strip(),
            "is_mine": my_id is not None and author_id == my_id,
            "system": author_id == 0,
            "image_url": image_url,
            "ts": ts or None,
        })
    out.sort(key=lambda m: m["id"])
    return out


# ---------------------------------------------------------------------------
# Клиент чата
# ---------------------------------------------------------------------------
class FunPayChat:
    """Чат FunPay поверх :class:`FunPaySource` (авторизация, cookie, csrf и паузы — из источника).

    :param source: авторизуемый источник FunPay; ``source.ensure()`` вызывается перед каждым запросом.
    :param logger: логгер (по умолчанию ``funpay.chat``).
    """

    def __init__(self, source: FunPaySource, logger: Optional[logging.Logger] = None):
        self.source = source
        self.log = logger or log
        self.last_sent_message_id: Optional[int] = None

    # ------------------------------------------------------------ infra
    def _ensure(self) -> int:
        """Авторизоваться (если нужно) и вернуть наш user_id."""
        self.source.ensure()
        if self.source.user_id is None:
            self.source.ensure(force=True)
        if self.source.user_id is None:
            raise AuthError("FunPay чат: не удалось определить id аккаунта (проверьте golden_key)")
        return int(self.source.user_id)

    def _csrf(self) -> str:
        if not self.source.csrf_token:
            self.source.ensure(force=True)
        if not self.source.csrf_token:
            raise SourceError("FunPay чат: не найден csrf-токен — запросы к /runner/ невозможны")
        return str(self.source.csrf_token)

    def _runner(self, objects: list[dict], request: Any = False) -> dict:
        """POST ``/runner/`` (XHR). Возвращает разобранный JSON-объект."""
        payload = {
            "objects": json.dumps(objects, ensure_ascii=False),
            "request": json.dumps(request, ensure_ascii=False) if request else "false",
            "csrf_token": self._csrf(),
        }
        response = self.source._request("POST", "/runner/", data=payload, ajax=True, headers=_FORM_HEADERS)
        if 300 <= response.status_code < 400:
            raise AuthError("FunPay перенаправил запрос чата на вход: golden_key невалиден или истёк")
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} при запросе /runner/")
        try:
            data = response.json()
        except ValueError:
            raise SourceError("FunPay вернул не JSON при запросе /runner/ — возможно, golden_key истёк") from None
        if not isinstance(data, dict):
            raise SourceError("FunPay вернул неожиданный ответ при запросе /runner/")
        return data

    # ------------------------------------------------------------ chats
    def list_chats(self) -> list[dict]:
        """``GET /chat/`` -> ``[{chat_id, name, last_message_id, last_text, unread, ...}]`` (новые сверху)."""
        self._ensure()
        response = self.source._request("GET", "/chat/")
        if 300 <= response.status_code < 400:
            raise AuthError("FunPay перенаправил страницу чатов на вход: golden_key невалиден или истёк")
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} на странице чатов")
        soup = _soup(response.text)
        if soup.select_one("div.user-link-name") is None:
            raise AuthError("golden_key невалиден или истёк (страница чатов без авторизации)")
        chats = parse_contact_items(soup)
        self.log.debug("FunPay чат: получено чатов %d, непрочитанных %d", len(chats),
                       sum(1 for c in chats if c["unread"]))
        return chats

    def get_history(self, chat_id: int | str, last_message_id: Optional[int] = None,
                    interlocutor_name: Optional[str] = None) -> list[dict]:
        """``GET /chat/history`` -> сообщения по возрастанию id.

        ``[{id, author_id, author, text, is_mine, system, image_url, ts}]``; ``is_mine`` — автор равен
        нашему user_id. ``last_message_id`` — с какого сообщения (вниз) отдавать историю; по умолчанию — конец.
        """
        my_id = self._ensure()
        params = {"node": chat_id, "last_message": last_message_id if last_message_id is not None else HISTORY_FROM_END}
        response = self.source._request("GET", "/chat/history", params=params, ajax=True)
        if 300 <= response.status_code < 400:
            raise AuthError("FunPay перенаправил историю чата на вход: golden_key невалиден или истёк")
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise SourceError(f"FunPay вернул {response.status_code} при загрузке истории чата {chat_id}")
        try:
            data = response.json()
        except ValueError:
            raise SourceError(f"FunPay вернул не JSON для истории чата {chat_id} — возможно, golden_key истёк") from None
        chat = data.get("chat") if isinstance(data, dict) else None
        if not isinstance(chat, dict):
            return []
        names: dict[int, str] = {my_id: self.source.username or "я"}
        other = _interlocutor_id(chat.get("node"), my_id)
        if other is not None and interlocutor_name:
            names[other] = interlocutor_name
        return parse_messages(chat.get("messages"), my_id, names)

    def poll_updates(self, state: dict) -> dict:
        """Один опрос ``POST /runner/`` (orders_counters + chat_bookmarks).

        ``state`` — словарь, в котором между вызовами хранятся теги (``state["tags"]``); при первом вызове
        теги случайные и FunPay отдаёт все чаты. Возвращает
        ``{"chats": [изменившиеся чаты], "orders_counters": {...}|None, "changed": bool}``.
        """
        user_id = self._ensure()
        tags = state.setdefault("tags", {}) if isinstance(state, dict) else {}
        objects = [
            {"type": "orders_counters", "id": user_id, "tag": tags.get("orders_counters") or random_tag(), "data": False},
            {"type": "chat_bookmarks", "id": user_id, "tag": tags.get("chat_bookmarks") or random_tag(), "data": False},
        ]
        data = self._runner(objects)
        result: dict = {"chats": [], "orders_counters": None, "changed": False}
        for obj in data.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind not in ("orders_counters", "chat_bookmarks"):
                continue
            if obj.get("tag"):
                tags[kind] = str(obj["tag"])
            payload = obj.get("data")
            if not isinstance(payload, dict):
                continue
            if kind == "orders_counters":
                result["orders_counters"] = payload
                result["changed"] = True
            else:
                html = payload.get("html")
                if html:
                    result["chats"] = parse_contact_items(str(html))
                    result["changed"] = True
        return result

    def send_message(self, chat_id: int | str, text: str) -> bool:
        """Отправить сообщение в чат. ``True`` при успехе, иначе :class:`SourceError`."""
        text = str(text or "")
        if not text.strip():
            raise SourceError("FunPay чат: пустое сообщение не отправляется")
        self._ensure()
        node = _to_int(chat_id) if _to_int(chat_id) is not None else chat_id
        request = {"action": "chat_message", "data": {"node": node, "last_message": -1, "content": text}}
        objects = [{"type": "chat_node", "id": node, "tag": "00000000",
                    "data": {"node": node, "last_message": -1, "content": ""}}]
        data = self._runner(objects, request)
        resp = data.get("response")
        if not isinstance(resp, dict):
            raise SourceError(f"FunPay не подтвердил отправку сообщения в чат {chat_id}")
        if resp.get("error"):
            raise SourceError(f"FunPay отклонил сообщение в чат {chat_id}: {resp.get('error')}")
        self.last_sent_message_id = None
        for obj in data.get("objects") or []:
            if isinstance(obj, dict) and obj.get("type") == "chat_node" and isinstance(obj.get("data"), dict):
                msgs = obj["data"].get("messages")
                if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
                    self.last_sent_message_id = _to_int(msgs[-1].get("id"))
        self.log.info("FunPay чат: сообщение отправлено в чат %s (%d символов)", chat_id, len(text))
        return True


__all__ = ["FunPayChat", "parse_contact_items", "parse_contact_item", "parse_messages", "chat_url",
           "random_tag", "HISTORY_FROM_END", "BOT_MARK"]
