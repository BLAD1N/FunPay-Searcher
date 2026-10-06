"""Настройки приложения: config/settings.yaml (создаётся из settings.example.yaml)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

import sys

if getattr(sys, "frozen", False):
    # Сборка PyInstaller: конфиг и данные лежат рядом с .exe, а ресурсы — внутри архива (_MEIPASS)
    ROOT = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = ROOT
CONFIG_DIR = ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
DATA_DIR = ROOT / "data"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
SETTINGS_EXAMPLE = CONFIG_DIR / "settings.example.yaml"
BUNDLED_CONFIG_DIR = BUNDLE_DIR / "config"   # эталонные config/ из сборки (копируются при первом запуске)


def ensure_user_dirs() -> None:
    """При первом запуске (особенно из .exe) создаёт config/, профили и data/ рядом с программой."""
    import shutil
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if BUNDLED_CONFIG_DIR != CONFIG_DIR and BUNDLED_CONFIG_DIR.exists():
        if not SETTINGS_EXAMPLE.exists() and (BUNDLED_CONFIG_DIR / "settings.example.yaml").exists():
            shutil.copy(BUNDLED_CONFIG_DIR / "settings.example.yaml", SETTINGS_EXAMPLE)
        src_profiles = BUNDLED_CONFIG_DIR / "profiles"
        if src_profiles.exists() and not any(PROFILES_DIR.glob("*.yaml")):
            for f in src_profiles.glob("*.yaml"):
                shutil.copy(f, PROFILES_DIR / f.name)


class FunPaySettings(BaseModel):
    golden_key: str = ""                 # cookie golden_key из браузера
    user_agent: str = ""                 # User-Agent того же браузера
    proxy: Optional[str] = None          # http://user:pass@host:port
    request_delay: float = 1.5           # пауза между запросами, сек
    timeout: float = 20.0
    auto_publish: bool = False           # создавать лоты без подтверждения (по умолчанию — только черновики)


class LolzSettings(BaseModel):
    token: str = ""                      # https://lolz.team/account/api
    proxy: Optional[str] = None
    request_delay: float = 3.0           # лимит API: не чаще 1 запроса в 3 сек
    timeout: float = 30.0


class MonitorSettings(BaseModel):
    enabled: bool = True
    interval_minutes: int = 30           # как часто проверять доступность исходных объявлений
    auto_deactivate: bool = True         # снимать наш лот, если исходник продан/исчез
    auto_search_minutes: int = 0         # 0 — автопоиск по расписанию выключен
    orders_check_minutes: int = 5        # как часто проверять новые заказы (продажи) на FunPay; 0 — выключено
    auto_raise_hours: float = 4.0        # автоподнятие наших лотов на FunPay раз в N часов; 0 — выключено


class TelegramSettings(BaseModel):
    enabled: bool = False
    bot_token: str = ""                  # токен бота от @BotFather
    chat_id: str = ""                    # ваш chat_id (узнать у @userinfobot) или id канала/группы
    notify_new_candidates: bool = True   # новые подходящие аккаунты после поиска
    notify_source_sold: bool = True      # исходник продан/снят — наш лот деактивирован
    notify_new_orders: bool = True       # новый заказ (продажа) нашего лота на FunPay
    notify_errors: bool = True           # ошибки авторизации/публикации


class UISettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True
    brand: str = "FunPay Searcher"


class Settings(BaseModel):
    funpay: FunPaySettings = Field(default_factory=FunPaySettings)
    lolz: LolzSettings = Field(default_factory=LolzSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    ui: UISettings = Field(default_factory=UISettings)
    default_currency: str = "RUB"

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "Settings":
        if not path.exists():
            if SETTINGS_EXAMPLE.exists():
                path.write_text(SETTINGS_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                cls().save(path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        s = cls.model_validate(data)
        # переменные окружения имеют приоритет (удобно для запуска без правки файла)
        if os.getenv("FUNPAY_GOLDEN_KEY"):
            s.funpay.golden_key = os.environ["FUNPAY_GOLDEN_KEY"]
        if os.getenv("LOLZ_TOKEN"):
            s.lolz.token = os.environ["LOLZ_TOKEN"]
        if os.getenv("TELEGRAM_BOT_TOKEN"):
            s.telegram.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        return s

    def save(self, path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.model_dump(mode="json"), f, allow_unicode=True, sort_keys=False)

    def masked(self) -> dict:
        """Для отдачи в UI: секреты маскируются."""
        d = self.model_dump(mode="json")
        for sec, key in (("funpay", "golden_key"), ("lolz", "token"), ("telegram", "bot_token")):
            v = d[sec].get(key) or ""
            d[sec][key + "_set"] = bool(v)
            d[sec][key] = (v[:4] + "…" + v[-4:]) if len(v) > 12 else ("•" * len(v))
        return d
