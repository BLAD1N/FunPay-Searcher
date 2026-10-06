"""Общие модели данных приложения (контракт между модулями).

Все модули (источники, матчинг, публикация, веб-API) обмениваются только этими объектами.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SourceName = Literal["funpay", "lolz"]


class Listing(BaseModel):
    """Найденное на площадке объявление (аккаунт на продажу)."""

    source: SourceName
    source_id: str
    url: str
    title: str = ""
    description: str = ""
    price: float
    currency: str = "RUB"
    seller_name: Optional[str] = None
    seller_url: Optional[str] = None
    seller_id: Optional[str] = None
    region: Optional[str] = None          # например "RU", "EU", "NA", "ASIA" (как удалось определить)
    game: Optional[str] = None            # идентификатор игры из профиля (wot, dota2, cs2, fortnite...)
    attributes: dict[str, Any] = Field(default_factory=dict)  # структурированные поля площадки
    online: Optional[bool] = None
    fetched_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def text_blob(self) -> str:
        """Весь текст объявления для поиска по ключевым словам."""
        parts = [self.title or "", self.description or ""]
        for k, v in self.attributes.items():
            if isinstance(v, (str, int, float)):
                parts.append(f"{k}: {v}")
        return "\n".join(parts).lower()


class MatchResult(BaseModel):
    """Результат проверки объявления по критериям профиля."""

    matched: bool
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)      # что совпало
    rejections: list[str] = Field(default_factory=list)   # почему отклонено
    highlights: list[str] = Field(default_factory=list)   # ключевые «фишки» для заголовка лота


class PriceRange(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None


class NumericRule(BaseModel):
    """Правило по числовому полю из attributes (например, количество танков)."""

    field: str
    min: Optional[float] = None
    max: Optional[float] = None


class Criteria(BaseModel):
    """Критерии отбора объявлений."""

    price: PriceRange = Field(default_factory=PriceRange)
    must_any: list[str] = Field(default_factory=list)   # хотя бы одно из слов должно встретиться
    must_all: list[str] = Field(default_factory=list)   # все слова должны встретиться
    exclude: list[str] = Field(default_factory=list)    # стоп-слова
    regex_any: list[str] = Field(default_factory=list)  # регулярные выражения (хотя бы одно)
    regex_exclude: list[str] = Field(default_factory=list)
    numeric: list[NumericRule] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)  # слова, которые считаем «фишками» (для заголовка)
    regions: list[str] = Field(default_factory=list)     # допустимые регионы (если определены)
    min_score: float = 1.0
    seller_min_reviews: Optional[int] = None


class PricingRule(BaseModel):
    """Правило наценки при создании лота.

    mode:
      - percent:    цена * (1 + percent/100)
      - multiplier: цена * multiplier
      - formula:    произвольная арифметика над `price` (например "price * 2 - 1000")
      - tiers:      ступени: [{up_to: 10000, percent: 100}, {up_to: 50000, percent: 90}, ...]
    """

    mode: Literal["percent", "multiplier", "formula", "tiers"] = "percent"
    percent: float = 90.0
    multiplier: float = 1.9
    formula: str = "price * 2 - 1000"
    tiers: list[dict[str, float]] = Field(default_factory=list)
    min_margin: float = 0.0        # минимальная абсолютная наценка
    round_to: int = 100            # округление итоговой цены (0 — без округления)
    round_mode: Literal["nearest", "down", "up"] = "nearest"
    price_ending: Optional[int] = None  # например 990 -> 28 990 (применяется после округления)


class LotTemplate(BaseModel):
    """Шаблон лота на FunPay в «нашем стиле»."""

    funpay_subcategory_id: Optional[int] = None  # узел /lots/{id}/ куда публикуем
    title_ru: str = "{game} | {highlights} | {region}"
    title_en: str = ""
    description_ru: str = ""
    description_en: str = ""
    amount: int = 1
    deactivate_after_sale: bool = True
    active: bool = True
    fields: dict[str, str] = Field(default_factory=dict)  # доп. поля формы FunPay (fields[server] и т.п.)


class SourceFunPayConfig(BaseModel):
    enabled: bool = True
    subcategory_id: Optional[int] = None     # /lots/{id}/ откуда ищем
    game_query: Optional[str] = None         # если id не задан: найти игру по названию ("World of Tanks")
    subcategory_query: Optional[str] = "Аккаунты"  # и подкатегорию по названию
    subcategory_ids: list[int] = Field(default_factory=list)  # либо несколько
    server_filter: list[str] = Field(default_factory=list)    # подстроки названия сервера/региона
    extra_query: dict[str, str] = Field(default_factory=dict)  # доп. GET-параметры фильтров
    max_items: int = 500


class SourceLolzConfig(BaseModel):
    enabled: bool = True
    category: Optional[str] = None            # steam, world-of-tanks, fortnite, ...
    params: dict[str, Any] = Field(default_factory=dict)  # любые параметры API (pmin, pmax, game[] ...)
    pages: int = 3
    max_items: int = 500


class Profile(BaseModel):
    """Профиль поиска — игра/регион/критерии/наценка/шаблон лота."""

    id: str
    name: str
    game: str
    region: Optional[str] = None
    enabled: bool = True
    description: str = ""
    sources: dict[str, Any] = Field(default_factory=dict)  # {"funpay": SourceFunPayConfig, "lolz": SourceLolzConfig}
    criteria: Criteria = Field(default_factory=Criteria)
    pricing: PricingRule = Field(default_factory=PricingRule)
    lot_template: LotTemplate = Field(default_factory=LotTemplate)

    def funpay(self) -> SourceFunPayConfig:
        return SourceFunPayConfig.model_validate(self.sources.get("funpay") or {"enabled": False})

    def lolz(self) -> SourceLolzConfig:
        return SourceLolzConfig.model_validate(self.sources.get("lolz") or {"enabled": False})


class FoundStatus(str, Enum):
    NEW = "new"              # найдено, ещё не обработано
    CANDIDATE = "candidate"  # прошло критерии
    REJECTED = "rejected"    # отклонено критериями
    IGNORED = "ignored"      # пользователь скрыл
    PUBLISHED = "published"  # создан наш лот
    SOLD = "sold"            # исходник продан / недоступен


class Found(BaseModel):
    """Запись о найденном объявлении + результат матчинга (хранится в БД)."""

    id: Optional[int] = None
    profile_id: str
    listing: Listing
    match: MatchResult
    suggested_price: Optional[float] = None
    status: FoundStatus = FoundStatus.NEW
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    available: Optional[bool] = None
    last_checked: Optional[datetime] = None


class LotStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    SOLD = "sold"
    ERROR = "error"


class OurLot(BaseModel):
    """Наш лот на FunPay, созданный по найденному объявлению."""

    id: Optional[int] = None
    found_id: int
    profile_id: str
    funpay_lot_id: Optional[int] = None
    funpay_url: Optional[str] = None
    subcategory_id: Optional[int] = None
    title_ru: str = ""
    title_en: str = ""
    description_ru: str = ""
    description_en: str = ""
    price: float = 0.0
    source_price: float = 0.0
    source_url: str = ""
    seller_url: Optional[str] = None
    fields: dict[str, str] = Field(default_factory=dict)
    status: LotStatus = LotStatus.DRAFT
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    source_available: Optional[bool] = None
    source_checked_at: Optional[datetime] = None


class SearchRunStats(BaseModel):
    profile_id: str
    source: str
    fetched: int = 0
    matched: int = 0
    new: int = 0
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
