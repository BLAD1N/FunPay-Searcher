"""Тесты автоответчика: фейковый клиент чата (без сети), реальное хранилище и файл состояния во временной папке."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import utcnow
from app.profiles import ProfileStore
from app.services import autoreply as autoreply_module
from app.services.autoreply import STATE_FILE_NAME, AutoReplyService, pick_reply
from app.services.context import AppContext
from app.settings import Settings
from app.sources.base import SourceError
from app.storage import Storage


def msg(id_: int, text: str, mine: bool = False, system: bool = False, author: str = "Buyer") -> dict:
    return {
        "id": id_,
        "author_id": 1 if mine else (0 if system else 5001),
        "author": "TestSeller" if mine else author,
        "text": text,
        "is_mine": mine,
        "system": system,
        "image_url": None,
        "ts": None,
    }


def chat_item(chat_id: int, name: str, last_id: int | None, text: str, unread: bool = True) -> dict:
    return {
        "chat_id": chat_id,
        "name": name,
        "last_message_id": last_id,
        "last_user_message_id": None,
        "last_text": text,
        "time": "12:00",
        "unread": unread,
        "url": f"https://funpay.com/chat/?node={chat_id}",
    }


class FakeChat:
    """Клиент чата: отдаёт заранее заданные чаты/истории и записывает все вызовы."""

    def __init__(self, chats: list[dict] | None = None, histories: dict[int, list[dict]] | None = None):
        self.chats = chats or []
        self.histories = histories or {}
        self.poll_chats: list[dict] = []
        self.calls: list = []
        self.sent: list[tuple[int, str]] = []
        self.list_exc: Exception | None = None
        self.send_exc: Exception | None = None
        self.last_sent_message_id: int | None = None

    def list_chats(self):
        self.calls.append("list_chats")
        if self.list_exc:
            raise self.list_exc
        return [dict(c) for c in self.chats]

    def poll_updates(self, state: dict):
        self.calls.append("poll_updates")
        state.setdefault("tags", {})["chat_bookmarks"] = "tag"
        chats = [dict(c) for c in self.poll_chats]
        self.poll_chats = []
        return {"chats": chats, "orders_counters": None, "changed": bool(chats)}

    def get_history(self, chat_id, last_message_id=None, interlocutor_name=None):
        self.calls.append(("history", chat_id))
        return [dict(m) for m in self.histories.get(chat_id, [])]

    def send_message(self, chat_id, text):
        self.calls.append(("send", chat_id))
        if self.send_exc:
            raise self.send_exc
        self.sent.append((chat_id, text))
        self.last_sent_message_id = 9000 + len(self.sent)
        return True


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.fail = False

    def send(self, text: str, kind: str = "info", **kw) -> bool:
        if self.fail:
            raise RuntimeError("telegram down")
        self.messages.append((kind, text))
        return True

    def allowed(self, kind: str = "info") -> bool:
        return True


def _make_ctx(monkeypatch, tmp: Path, golden_key: str = "test-key", enabled: bool = True) -> AppContext:
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    settings = Settings()
    settings.funpay.golden_key = golden_key
    settings.autoreply.enabled = enabled
    settings.autoreply.greeting = "Здравствуйте! Аккаунт в наличии ✅"
    settings.autoreply.keywords = {"в наличии|есть?|актуально": "Да, в наличии ✅", "скидк*|дешевле": "Скидок нет"}
    settings.autoreply.reply_once_per_chat_hours = 12
    ctx = AppContext(settings=settings, storage=Storage(tmp / "db.sqlite"), profiles=ProfileStore(tmp / "profiles"))
    ctx._notifier = FakeNotifier()
    return ctx


@pytest.fixture()
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(autoreply_module, "DATA_DIR", tmp / "data")
    ctx = _make_ctx(monkeypatch, tmp)
    fake = FakeChat(
        chats=[
            chat_item(101, "Buyer_One", 1003, "Здравствуйте, аккаунт ещё в наличии?"),  # ключевое слово
            chat_item(102, "Buyer_Two", 2002, "Какой сервер?"),  # приветствие
            chat_item(103, "Reader", 3001, "Спасибо", unread=False),  # прочитан
            chat_item(104, "Answered", 4002, "Да, в наличии"),  # последнее — наше
            chat_item(105, "SystemOnly", 5001, "Покупатель оплатил заказ"),  # системное
        ],
        histories={
            101: [
                msg(1001, "Привет"),
                msg(1002, "Отвечу позже", mine=True),
                msg(1003, "Здравствуйте, аккаунт ещё в наличии?"),
            ],
            102: [msg(2001, "Привет"), msg(2002, "Какой сервер?")],
            103: [msg(3001, "Спасибо")],
            104: [msg(4001, "В наличии?"), msg(4002, "Да, в наличии", mine=True)],
            105: [msg(5001, "Покупатель оплатил заказ #1", system=True)],
        },
    )
    monkeypatch.setattr(AutoReplyService, "_chat", lambda self: fake)
    svc = AutoReplyService(ctx)
    return ctx, fake, svc, tmp / "data" / STATE_FILE_NAME


# ----------------------------------------------------------------------------- pick_reply / preview
def test_pick_reply_rules():
    kw = {"в наличии|есть?": "Да, в наличии", "скидк*|дешевле": "Скидок нет", "": "пусто", "x": ""}
    assert pick_reply("Привет, ЕСТЬ в наличии?", kw, "Привет") == ("Да, в наличии", "в наличии|есть?")
    assert pick_reply("а скидку сделаете?", kw, "Привет") == ("Скидок нет", "скидк*|дешевле")
    assert pick_reply("Можно дешевле?", kw, "Привет") == ("Скидок нет", "скидк*|дешевле")
    assert pick_reply("Какой сервер?", kw, "Привет") == ("Привет", None)
    assert pick_reply("Какой сервер?", kw, "") == ("", None)
    assert pick_reply("", {}, "  Привет  ") == ("Привет", None)
    # ё = е, регистр не важен, слово целиком: «есть» не найдётся внутри «честь»
    assert pick_reply("ЕЩЁ ЕСТЬ?", {"ещё есть": "ok"}, "") == ("ok", "ещё есть")
    assert pick_reply("честь", {"есть": "ok"}, "no") == ("no", None)
    assert pick_reply("x", 123, "g") == ("g", None)  # type: ignore[arg-type] — кривой тип правил не ломает


def test_preview_reply_uses_settings(env):
    ctx, fake, svc, _ = env
    assert svc.preview_reply("Аккаунт актуально?") == "Да, в наличии ✅"
    assert svc.preview_reply("Есть скидка?") == "Да, в наличии ✅"  # первое подходящее правило по порядку
    assert svc.preview_reply("Сделаете дешевле?") == "Скидок нет"
    assert svc.preview_reply("Какой сервер?") == "Здравствуйте! Аккаунт в наличии ✅"
    ctx.settings.autoreply.greeting = ""
    assert svc.preview_reply("Какой сервер?") == ""
    assert fake.calls == []


# ----------------------------------------------------------------------------- run_once
def test_run_once_keyword_vs_greeting(env):
    ctx, fake, svc, state_file = env
    res = svc.run_once()
    assert res["errors"] == []
    # Telegram в базовой фикстуре выключен — уведомлений нет
    assert res["checked"] == 4 and res["replied"] == 2 and res["notified"] == 0 and res["throttled"] == 0
    assert fake.sent == [(101, "Да, в наличии ✅"), (102, "Здравствуйте! Аккаунт в наличии ✅")]
    # первый запуск — полная страница чатов, история только у непрочитанных, у системного/нашего ответа нет
    assert fake.calls[0] == "list_chats"
    assert ("history", 103) not in fake.calls and ("send", 104) not in fake.calls and ("send", 105) not in fake.calls
    assert ("history", 104) in fake.calls and ("history", 105) in fake.calls

    # журнал: событие kind="chat" с именем покупателя и началом ответа
    events = [e for e in ctx.storage.events() if e["kind"] == "chat"]
    assert any(e["message"] == "ответил Buyer_One: Да, в наличии ✅" for e in events)
    assert any(e["message"].startswith("ответил Buyer_Two: Здравствуйте! Аккаунт в наличии") for e in events)
    assert any(e["data"] and e["data"].get("rule") == "в наличии|есть?|актуально" for e in events)

    # файл состояния: время ответа и id последнего обработанного сообщения
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(data["last_reply"]) == {"101", "102"}
    assert data["last_message"] == {"101": 1003, "102": 2002, "104": 4002, "105": 5001}

    st = svc.status()
    assert st["enabled"] is True and st["running"] is False and st["last_run"] and st["last_result"] == res
    assert st["replied_total"] == 2 and st["notified_total"] == 0 and st["keywords"] == 2
    assert st["poll_seconds"] == 15 and st["last_error"] is None

    # повторный запуск: опрос runner'а без изменений — ничего не делаем
    res2 = svc.run_once()
    assert fake.calls[-1] == "poll_updates"
    assert res2["checked"] == 0 and res2["replied"] == 0 and len(fake.sent) == 2


def test_notifications(env):
    ctx, fake, svc, _ = env
    t = ctx.settings.telegram
    t.enabled, t.bot_token, t.chat_id, t.notify_messages = True, "123:abc", "42", True
    res = svc.run_once()
    assert res["notified"] == 2 and svc.notified_total == 2
    kinds = {k for k, _ in ctx.notifier.messages}
    assert kinds == {"messages"}
    text = ctx.notifier.messages[0][1]
    assert "Buyer_One" in text and "аккаунт ещё в наличии?" in text
    assert "Автоответ: Да, в наличии ✅" in text and "https://funpay.com/chat/?node=101" in text
    assert "Автоответ" in ctx.notifier.messages[1][1]

    # уведомления выключены переключателем notify_messages — не шлём
    ctx.notifier.messages.clear()
    t.notify_messages = False
    fake.poll_chats = [chat_item(106, "Third", 6001, "привет")]
    fake.histories[106] = [msg(6001, "привет")]
    res = svc.run_once()
    assert res["replied"] == 1 and res["notified"] == 0 and ctx.notifier.messages == []


def test_notify_only_when_autoreply_disabled(env):
    ctx, fake, svc, _ = env
    ctx.settings.autoreply.enabled = False
    t = ctx.settings.telegram
    t.enabled, t.bot_token, t.chat_id, t.notify_messages = True, "123:abc", "42", True
    res = svc.run_once()
    assert res["replied"] == 0 and res["notified"] == 2 and fake.sent == []
    assert "Автоответ" not in ctx.notifier.messages[0][1]
    assert svc.status()["active"] is True and svc.status()["enabled"] is False


def test_throttle_once_per_chat(env):
    ctx, fake, svc, state_file = env
    svc.run_once()
    assert len(fake.sent) == 2

    # покупатель пишет снова в тот же чат — в пределах 12 часов не отвечаем, но считаем throttled
    fake.poll_chats = [chat_item(101, "Buyer_One", 1004, "Ау, есть?")]
    fake.histories[101].append(msg(1004, "Ау, есть?"))
    res = svc.run_once()
    assert res["checked"] == 1 and res["replied"] == 0 and res["throttled"] == 1 and len(fake.sent) == 2
    assert json.loads(state_file.read_text(encoding="utf-8"))["last_message"]["101"] == 1004

    # прошло больше 12 часов — отвечаем снова
    state = svc._load_state()
    state["last_reply"]["101"] = (utcnow() - timedelta(hours=13)).isoformat()
    fake.poll_chats = [chat_item(101, "Buyer_One", 1005, "Так есть или нет?")]
    fake.histories[101].append(msg(1005, "Так есть или нет?"))
    res = svc.run_once()
    assert res["replied"] == 1 and fake.sent[-1] == (101, "Да, в наличии ✅")

    # throttle выключен (0 часов) — отвечаем каждый раз
    ctx.settings.autoreply.reply_once_per_chat_hours = 0
    fake.poll_chats = [chat_item(101, "Buyer_One", 1006, "ещё раз: в наличии?")]
    fake.histories[101].append(msg(1006, "ещё раз: в наличии?"))
    assert svc.run_once()["replied"] == 1


def test_our_reply_is_not_answered_again(env):
    _ctx, fake, svc, _ = env
    svc.run_once()
    sent_id = fake.last_sent_message_id
    # runner сообщает, что чат изменился: последнее сообщение — наш только что отправленный ответ
    fake.poll_chats = [chat_item(102, "Buyer_Two", sent_id, "Здравствуйте! Аккаунт в наличии ✅")]
    n = len(fake.calls)
    res = svc.run_once()
    assert res["checked"] == 1 and res["replied"] == 0
    assert ("history", 102) not in fake.calls[n:]  # без лишнего запроса истории


def test_disabled_makes_no_calls(env):
    ctx, fake, svc, state_file = env
    ctx.settings.autoreply.enabled = False
    res = svc.run_once()
    assert res == {
        "checked": 0,
        "replied": 0,
        "notified": 0,
        "throttled": 0,
        "errors": [],
        "skipped": "автоответчик выключен",
    }
    assert fake.calls == [] and not state_file.exists()
    assert svc.status()["active"] is False

    ctx.settings.autoreply.enabled = True
    ctx.settings.funpay.golden_key = ""
    res = svc.run_once()
    assert res["skipped"].startswith("не задан golden_key") and fake.calls == []


def test_errors_are_captured(env):
    ctx, fake, svc, _ = env
    fake.list_exc = SourceError("FunPay вернул 500")
    res = svc.run_once()
    assert res["replied"] == 0 and len(res["errors"]) == 1 and "FunPay вернул 500" in res["errors"][0]
    assert svc.status()["last_error"] == res["errors"][0]
    assert any(e["level"] == "error" and e["kind"] == "chat" for e in ctx.storage.events())

    # после ошибки списка снова берём полную страницу чатов
    fake.list_exc = None
    fake.send_exc = SourceError("FunPay отклонил сообщение")
    n = len(fake.calls)
    res = svc.run_once()
    assert fake.calls[n] == "list_chats"
    assert res["replied"] == 0 and len(res["errors"]) == 2
    assert all("FunPay отклонил сообщение" in e for e in res["errors"])
    assert "Buyer_One" in res["errors"][0]

    # неудачные ответы не попадают в throttle — после починки отвечаем
    fake.send_exc = None
    fake.poll_chats = [chat_item(101, "Buyer_One", 1004, "в наличии?")]
    fake.histories[101].append(msg(1004, "в наличии?"))
    res = svc.run_once()
    assert res["replied"] == 1 and res["errors"] == []

    # ошибка уведомления не ломает ответ
    t = ctx.settings.telegram
    t.enabled, t.bot_token, t.chat_id = True, "123:abc", "42"
    ctx.notifier.fail = True
    fake.poll_chats = [chat_item(107, "Seven", 7001, "привет")]
    fake.histories[107] = [msg(7001, "привет")]
    res = svc.run_once()
    assert res["replied"] == 1 and res["notified"] == 0 and res["errors"] == []


def test_state_survives_restart(env, monkeypatch):
    ctx, fake, svc, state_file = env
    svc.run_once()
    assert state_file.exists()

    # новый экземпляр сервиса (перезапуск приложения) читает файл: повторно не отвечает и не грузит историю
    fake2 = FakeChat(chats=fake.chats, histories=fake.histories)
    monkeypatch.setattr(AutoReplyService, "_chat", lambda self: fake2)
    svc2 = AutoReplyService(ctx)
    res = svc2.run_once()
    assert res["checked"] == 4 and res["replied"] == 0 and fake2.sent == []
    assert fake2.calls == ["list_chats"]

    # битый файл состояния не ломает запуск
    state_file.write_text("{ не json", encoding="utf-8")
    svc3 = AutoReplyService(ctx)
    assert svc3._load_state()["last_reply"] == {}
    # плоский старый формат {chat_id: iso}
    state_file.write_text(json.dumps({"101": utcnow().isoformat()}), encoding="utf-8")
    svc4 = AutoReplyService(ctx)
    assert set(svc4._load_state()["last_reply"]) == {"101"}


def test_concurrent_run_is_skipped(env):
    _ctx, fake, svc, _ = env
    svc._busy.acquire()
    try:
        assert svc.run_once()["skipped"] == "уже выполняется"
    finally:
        svc._busy.release()
    assert fake.calls == []


def test_thread_start_stop(env):
    ctx, fake, svc, _ = env
    svc.initial_delay = 0
    ctx.settings.autoreply.poll_seconds = 1
    svc.start()
    thread = svc._thread
    svc.start()  # повторный start не создаёт второй поток
    assert svc._thread is thread
    deadline = time.time() + 5
    while time.time() < deadline and len(fake.sent) < 2:
        time.sleep(0.05)
    assert len(fake.sent) == 2  # первый цикл выполнился из фонового потока
    assert svc.status()["thread_alive"] is True and svc.status()["next_run"]
    svc.stop()
    assert not thread.is_alive()
    assert svc.status()["thread_alive"] is False
