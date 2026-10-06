"""Контекст приложения: настройки, хранилище, профили, источники (ленивая инициализация)."""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any

from ..models import utcnow
from ..profiles import ProfileStore
from ..settings import Settings
from ..storage import Storage

if TYPE_CHECKING:
    from ..notify import TelegramNotifier
    from ..sources.funpay import FunPaySource
    from ..sources.lolz import LolzSource

log = logging.getLogger("app")


class _NullNotifier:
    """Заглушка, если модуль уведомлений недоступен: ничего не отправляет."""

    enabled = False

    def send(self, text: str, **kwargs) -> bool:
        return False

    def test_connection(self) -> dict:
        return {"ok": False, "error": "модуль уведомлений недоступен"}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("format_"):
            return lambda *a, **k: ""
        raise AttributeError(name)


class AppContext:
    def __init__(
        self, settings: Settings | None = None, storage: Storage | None = None, profiles: ProfileStore | None = None
    ) -> None:
        self.settings = settings or Settings.load()
        self.storage = storage or Storage()
        self.profiles = profiles or ProfileStore()
        self._lock = threading.RLock()
        self._funpay: FunPaySource | None = None
        self._lolz: LolzSource | None = None
        self._notifier: TelegramNotifier | _NullNotifier | None = None
        self.auth_state: dict[str, dict] = {
            "funpay": {"ok": None, "username": None, "error": None, "checked_at": None},
            "lolz": {"ok": None, "username": None, "error": None, "checked_at": None},
        }

    # --------------------------------------------------------- sources
    @property
    def funpay(self) -> FunPaySource:
        with self._lock:
            if self._funpay is None:
                from ..sources.funpay import FunPaySource

                self._funpay = FunPaySource(self.settings.funpay, storage=self.storage)
            return self._funpay

    @property
    def lolz(self) -> LolzSource:
        with self._lock:
            if self._lolz is None:
                from ..sources.lolz import LolzSource

                self._lolz = LolzSource(self.settings.lolz)
            return self._lolz

    @property
    def notifier(self) -> TelegramNotifier | _NullNotifier:
        """Telegram-уведомления (настройки читаются при каждой отправке)."""
        with self._lock:
            if self._notifier is None:
                try:
                    from ..notify import TelegramNotifier

                    self._notifier = TelegramNotifier(lambda: self.settings.telegram)
                except Exception as e:
                    log.warning("уведомления Telegram недоступны: %s", e)
                    self._notifier = _NullNotifier()
            return self._notifier

    def notify(self, text: str, kind: str = "info") -> bool:
        try:
            return self.notifier.send(text, kind=kind)
        except Exception as e:
            log.warning("не удалось отправить уведомление: %s", e)
            return False

    def source(self, name: str) -> FunPaySource | LolzSource:
        if name == "funpay":
            return self.funpay
        if name == "lolz":
            return self.lolz
        raise KeyError(name)

    def reset_sources(self) -> None:
        """Пересоздать клиентов после смены настроек."""
        with self._lock:
            self._funpay = None
            self._lolz = None

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        settings.save()
        self.reset_sources()

    # ------------------------------------------------------------ auth
    def check_auth(self, which: list[str] | None = None) -> dict:
        result = {}
        for name in which or ["funpay", "lolz"]:
            state = {"ok": False, "username": None, "error": None, "checked_at": utcnow().isoformat()}
            try:
                creds = self.settings.funpay.golden_key if name == "funpay" else self.settings.lolz.token
                if not creds:
                    state["error"] = "не задан токен/cookie в настройках"
                else:
                    info = self.source(name).check_auth()
                    state.update({k: info.get(k) for k in ("ok", "username", "error") if k in info})
            except Exception as e:
                state["error"] = str(e)
            self.auth_state[name] = state
            result[name] = state
            self.storage.log(
                "auth",
                f"{name}: {'OK, ' + str(state['username']) if state['ok'] else 'ошибка: ' + str(state['error'])}",
                level="info" if state["ok"] else "warning",
            )
        return result

    def log(self, kind: str, message: str, level: str = "info", data: dict | None = None) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(f"[{kind}] {message}")
        with contextlib.suppress(Exception):
            self.storage.log(kind, message, level=level, data=data)
