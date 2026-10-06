"""Контекст приложения: настройки, хранилище, профили, источники (ленивая инициализация)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from ..models import utcnow
from ..profiles import ProfileStore
from ..settings import Settings
from ..storage import Storage

log = logging.getLogger("app")


class AppContext:
    def __init__(self, settings: Optional[Settings] = None, storage: Optional[Storage] = None,
                 profiles: Optional[ProfileStore] = None):
        self.settings = settings or Settings.load()
        self.storage = storage or Storage()
        self.profiles = profiles or ProfileStore()
        self._lock = threading.RLock()
        self._funpay = None
        self._lolz = None
        self.auth_state: dict[str, dict] = {
            "funpay": {"ok": None, "username": None, "error": None, "checked_at": None},
            "lolz": {"ok": None, "username": None, "error": None, "checked_at": None},
        }

    # --------------------------------------------------------- sources
    @property
    def funpay(self):
        with self._lock:
            if self._funpay is None:
                from ..sources.funpay import FunPaySource
                self._funpay = FunPaySource(self.settings.funpay)
            return self._funpay

    @property
    def lolz(self):
        with self._lock:
            if self._lolz is None:
                from ..sources.lolz import LolzSource
                self._lolz = LolzSource(self.settings.lolz)
            return self._lolz

    def source(self, name: str):
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
    def check_auth(self, which: Optional[list[str]] = None) -> dict:
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
            except Exception as e:  # noqa: BLE001
                state["error"] = str(e)
            self.auth_state[name] = state
            result[name] = state
            self.storage.log("auth", f"{name}: {'OK, ' + str(state['username']) if state['ok'] else 'ошибка: ' + str(state['error'])}",
                             level="info" if state["ok"] else "warning")
        return result

    def log(self, kind: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(f"[{kind}] {message}")
        try:
            self.storage.log(kind, message, level=level, data=data)
        except Exception:  # noqa: BLE001
            pass
