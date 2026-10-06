"""Автоответчик в чате FunPay: отвечает покупателям, которые пишут перед покупкой, и уведомляет в Telegram.

Поток работы ``run_once()`` (вызывается фоновым потоком раз в ``autoreply.poll_seconds`` или вручную из API):

1. список чатов: при первом запуске — ``GET /chat/`` (:meth:`FunPayChat.list_chats`), далее — дешёвый опрос
   ``POST /runner/`` (:meth:`FunPayChat.poll_updates`), который возвращает только изменившиеся чаты;
2. для каждого непрочитанного чата, последнее сообщение которого мы ещё не обрабатывали (по id из
   ``data-node-msg``), загружается история — так мы точно знаем автора последнего сообщения и полный текст;
3. если последнее сообщение не наше и не системное: уведомление в Telegram (вид ``messages``) и, если
   автоответчик включён, ответ — первое подошедшее правило из ``autoreply.keywords`` (``"в наличии|есть?"`` ->
   текст; синтаксис как у критериев: варианты через ``|``, ``*`` — любое окончание, регистр и ё/е не важны)
   либо ``autoreply.greeting``;
4. повторный ответ в том же чате — не чаще, чем раз в ``reply_once_per_chat_hours``; состояние
   (время последнего ответа и id последнего обработанного сообщения по чатам) хранится в
   ``data/autoreply_state.json`` и переживает перезапуск.

Сервис никогда не выбрасывает исключений из потока: ошибки попадают в результат запуска и в журнал
(``kind="chat"``, уровень ``error``).
"""
from __future__ import annotations

import html
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..matching import match_text
from ..models import utcnow
from ..settings import DATA_DIR
from ..sources.funpay_chat import FunPayChat, chat_url
from .context import AppContext

STATE_FILE_NAME = "autoreply_state.json"
MIN_POLL_SECONDS = 5          # защита от слишком частого опроса FunPay
INITIAL_DELAY_SECONDS = 10    # первый опрос после старта приложения
STATE_TTL_DAYS = 30           # записи о чатах старше этого срока удаляются из файла состояния
LOG_PREVIEW_LEN = 60
NOTIFY_PREVIEW_LEN = 300


def pick_reply(text: str, keywords: dict[str, str], greeting: str) -> tuple[str, Optional[str]]:
    """Подобрать ответ на текст покупателя: ``(ответ, сработавшее правило | None)``.

    Правила проверяются в порядке записи; ключ — варианты через ``|`` (см. :func:`app.matching.match_text`).
    Если ни одно не подошло — приветствие. Пустой ответ означает «не отвечать».
    """
    text = str(text or "")
    if not isinstance(keywords, dict):
        keywords = {}
    for key, reply in keywords.items():
        if not key or not str(reply or "").strip():
            continue
        try:
            if match_text(text, str(key)):
                return str(reply).strip(), str(key)
        except Exception:  # noqa: BLE001 — кривое правило не должно ломать автоответчик
            continue
    return str(greeting or "").strip(), None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _preview(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


class AutoReplyService:
    """Фоновый автоответчик чата FunPay. Создание объекта побочных эффектов не имеет."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()
        self._state: Optional[dict] = None            # содержимое autoreply_state.json (лениво)
        self._runner_state: Optional[dict] = None     # теги /runner/ между опросами; None — опроса ещё не было
        self._sent_ids: dict[int, Optional[int]] = {} # chat_id -> id нашего последнего ответа (если FunPay вернул)
        self.initial_delay = INITIAL_DELAY_SECONDS
        self.last_run: Optional[str] = None
        self.next_run: Optional[str] = None
        self.last_result: dict = {}
        self.last_error: Optional[str] = None
        self.replied_total = 0
        self.notified_total = 0

    # ------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._busy.locked()

    def _poll_seconds(self) -> int:
        try:
            return max(MIN_POLL_SECONDS, int(self.ctx.settings.autoreply.poll_seconds or 0))
        except (TypeError, ValueError):
            return 15

    def _notify_enabled(self) -> bool:
        """Уведомлять ли о новых сообщениях (отдельный переключатель Telegram, не зависит от автоответов)."""
        t = self.ctx.settings.telegram
        return bool(t.enabled and t.notify_messages and t.bot_token and t.chat_id)

    def _active(self) -> bool:
        """Есть ли смысл опрашивать FunPay: задан golden_key и включены ответы или уведомления."""
        s = self.ctx.settings
        return bool(s.funpay.golden_key) and (bool(s.autoreply.enabled) or self._notify_enabled())

    def status(self) -> dict:
        s = self.ctx.settings.autoreply
        return {
            "running": self.running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "enabled": bool(s.enabled),
            "notify_messages": self._notify_enabled(),
            "active": self._active(),
            "poll_seconds": self._poll_seconds(),
            "reply_once_per_chat_hours": s.reply_once_per_chat_hours,
            "keywords": len(s.keywords or {}),
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "replied_total": self.replied_total,
            "notified_total": self.notified_total,
        }

    def preview_reply(self, text: str) -> str:
        """Какой ответ получит покупатель на текст ``text`` (чистая функция для проверки правил в UI)."""
        s = self.ctx.settings.autoreply
        reply, _ = pick_reply(text, s.keywords, s.greeting)
        return reply

    # ----------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autoreply", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)

    def _loop(self) -> None:
        delay = max(0, int(self.initial_delay))
        self.next_run = (utcnow() + timedelta(seconds=delay)).isoformat()
        while not self._stop.wait(delay):
            try:
                if self._active():
                    self.run_once()
            except Exception as e:  # noqa: BLE001 — поток не должен умирать
                self.last_error = str(e)
                self.ctx.log("chat", f"ошибка автоответчика: {e}", level="error")
            delay = self._poll_seconds()
            self.next_run = (utcnow() + timedelta(seconds=delay)).isoformat()

    # ------------------------------------------------------- state file
    def _state_path(self) -> Path:
        # DATA_DIR читается при каждом вызове (глобальная переменная модуля) — в тестах её подменяют
        return Path(DATA_DIR) / STATE_FILE_NAME

    def _load_state(self) -> dict:
        with self._state_lock:
            if self._state is None:
                self._state = self._read_state_file()
            return self._state

    def _read_state_file(self) -> dict:
        state: dict = {"version": 1, "last_reply": {}, "last_message": {}}
        try:
            path = self._state_path()
            if not path.exists():
                return state
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "last_reply" in data or "last_message" in data:
                    for key in ("last_reply", "last_message"):
                        value = data.get(key)
                        state[key] = {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}
                else:  # плоский формат {chat_id: last_reply_iso}
                    state["last_reply"] = {str(k): v for k, v in data.items() if isinstance(v, str)}
        except Exception as e:  # noqa: BLE001
            self.ctx.log("chat", f"не удалось прочитать состояние автоответчика: {e}", level="warning")
        return state

    def _save_state(self) -> None:
        with self._state_lock:
            if self._state is None:
                return
            path = self._state_path()
            try:
                self._prune_state()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, path)
            except Exception as e:  # noqa: BLE001
                self.ctx.log("chat", f"не удалось сохранить состояние автоответчика: {e}", level="warning")

    def _prune_state(self) -> None:
        """Забыть чаты, в которых мы давно не отвечали (чтобы файл не рос бесконечно)."""
        cutoff = utcnow() - timedelta(days=STATE_TTL_DAYS)
        replies: dict = self._state.get("last_reply") or {}
        for chat_id in [k for k, v in replies.items() if (_parse_iso(v) or utcnow()) < cutoff]:
            replies.pop(chat_id, None)
            (self._state.get("last_message") or {}).pop(chat_id, None)

    # ------------------------------------------------------------- chat
    def _chat(self) -> FunPayChat:
        """Фабрика клиента чата (подменяется в тестах)."""
        return FunPayChat(self.ctx.funpay)

    def _fetch_chats(self, chat: FunPayChat) -> list[dict]:
        """Первый запуск — вся страница чатов; далее — только изменившиеся чаты из /runner/."""
        if self._runner_state is None:
            chats = chat.list_chats()
            self._runner_state = {}
            return chats
        return chat.poll_updates(self._runner_state).get("chats") or []

    def _throttled_until(self, chat_key: str, now: datetime) -> Optional[datetime]:
        hours = float(self.ctx.settings.autoreply.reply_once_per_chat_hours or 0)
        if hours <= 0:
            return None
        last = _parse_iso((self._load_state().get("last_reply") or {}).get(chat_key))
        if last is None:
            return None
        until = last + timedelta(hours=hours)
        return until if until > now else None

    # --------------------------------------------------------- run once
    def run_once(self) -> dict:
        """Один цикл: опросить чаты, ответить, уведомить. Никогда не выбрасывает исключений."""
        result: dict = {"checked": 0, "replied": 0, "notified": 0, "throttled": 0, "errors": []}
        s = self.ctx.settings
        if not s.funpay.golden_key:
            result["skipped"] = "не задан golden_key FunPay в настройках"
            return result
        reply_enabled = bool(s.autoreply.enabled)
        notify_enabled = self._notify_enabled()
        if not reply_enabled and not notify_enabled:
            result["skipped"] = "автоответчик выключен"
            return result
        if not self._busy.acquire(blocking=False):
            result["skipped"] = "уже выполняется"
            return result
        try:
            try:
                chat = self._chat()
                chats = self._fetch_chats(chat)
            except Exception as e:  # noqa: BLE001
                self._runner_state = None  # в следующий раз начнём с полной страницы чатов
                msg = f"не удалось получить список чатов FunPay: {e}"
                result["errors"].append(msg)
                self.ctx.log("chat", msg, level="error")
                return result
            for item in chats:
                if not item.get("unread"):
                    continue
                result["checked"] += 1
                try:
                    self._process_chat(chat, item, reply_enabled, notify_enabled, result)
                except Exception as e:  # noqa: BLE001
                    msg = f"чат с {item.get('name') or item.get('chat_id')}: {e}"
                    result["errors"].append(msg)
                    self.ctx.log("chat", f"ошибка автоответчика: {msg}", level="error")
            return result
        finally:
            self._save_state()
            self.last_run = utcnow().isoformat()
            self.last_result = result
            self.last_error = result["errors"][-1] if result["errors"] else None
            self._busy.release()

    def _process_chat(self, chat: FunPayChat, item: dict, reply_enabled: bool, notify_enabled: bool,
                      result: dict) -> None:
        chat_id = int(item["chat_id"])
        key = str(chat_id)
        name = str(item.get("name") or "").strip() or f"чат {chat_id}"
        state = self._load_state()
        processed: dict = state.setdefault("last_message", {})
        last_id = item.get("last_message_id")
        marker: Any = last_id if last_id is not None else f"text:{item.get('last_text') or ''}"

        # дешёвые отсечения без запросов к FunPay
        if processed.get(key) == marker:
            return                                   # это сообщение уже обрабатывали
        if last_id is not None and self._sent_ids.get(chat_id) == last_id:
            processed[key] = marker                  # последнее сообщение — наш ответ
            return

        messages = chat.get_history(chat_id, interlocutor_name=name)
        if not messages:
            processed[key] = marker
            return
        last = messages[-1]
        prev_id = processed.get(key) if isinstance(processed.get(key), int) else 0
        processed[key] = max(int(last["id"]), int(last_id or 0))
        if last.get("is_mine") or last.get("system"):
            return

        # новые сообщения покупателя после последнего обработанного (при первом знакомстве — только последнее)
        incoming = [m for m in messages if not m.get("is_mine") and not m.get("system") and int(m["id"]) > prev_id]
        if not prev_id:
            incoming = incoming[-1:]
        if not incoming:
            incoming = [last]
        text = "\n".join(str(m.get("text") or "") for m in incoming).strip()
        if not text:
            text = "[изображение]" if any(m.get("image_url") for m in incoming) else ""
        # имя собеседника из списка чатов надёжнее имени из HTML сообщения (его может не быть у «склеенных»)
        buyer = str(item.get("name") or "").strip() or str(last.get("author") or "").strip() or name

        reply_sent: Optional[str] = None
        if reply_enabled:
            reply_sent = self._reply(chat, chat_id, buyer, text, result)
        if notify_enabled:
            self._notify(chat_id, buyer, text, reply_sent, result)

    def _reply(self, chat: FunPayChat, chat_id: int, buyer: str, text: str, result: dict) -> Optional[str]:
        s = self.ctx.settings.autoreply
        reply, rule = pick_reply(text, s.keywords, s.greeting)
        if not reply:
            self.ctx.log("chat", f"сообщение от {buyer} без ответа: нет подходящего правила и приветствия",
                         level="debug")
            return None
        now = utcnow()
        key = str(chat_id)
        until = self._throttled_until(key, now)
        if until is not None:
            result["throttled"] += 1
            self.ctx.log("chat", f"{buyer}: ответ пропущен — уже отвечали в этом чате "
                                 f"(следующий не раньше {until.astimezone().strftime('%H:%M')})", level="debug")
            return None
        chat.send_message(chat_id, reply)
        self._sent_ids[chat_id] = getattr(chat, "last_sent_message_id", None)
        self._load_state().setdefault("last_reply", {})[key] = now.isoformat()
        self._save_state()
        result["replied"] += 1
        self.replied_total += 1
        self.ctx.log("chat", f"ответил {buyer}: {_preview(reply, LOG_PREVIEW_LEN)}",
                     data={"chat_id": chat_id, "rule": rule, "question": _preview(text, 200)})
        return reply

    def _notify(self, chat_id: int, buyer: str, text: str, reply: Optional[str], result: dict) -> None:
        lines = [f"💬 <b>Сообщение от {html.escape(buyer)}</b>", html.escape(_preview(text, NOTIFY_PREVIEW_LEN) or "—")]
        if reply:
            lines.append(f"🤖 Автоответ: {html.escape(_preview(reply, 120))}")
        lines.append(f'<a href="{html.escape(chat_url(chat_id))}">Открыть чат</a>')
        try:
            sent = self.ctx.notify("\n".join(lines), kind="messages")
        except Exception as e:  # noqa: BLE001
            sent = False
            self.ctx.log("chat", f"не удалось отправить уведомление о сообщении от {buyer}: {e}", level="warning")
        if sent:
            result["notified"] += 1
            self.notified_total += 1


__all__ = ["AutoReplyService", "pick_reply", "STATE_FILE_NAME"]
