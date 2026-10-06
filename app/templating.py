"""Генерация текста нашего лота на FunPay по найденному объявлению («наш стиль»).

Главная функция — :func:`render_lot`. Шаблоны из :class:`~app.models.LotTemplate`
заполняются плейсхолдерами в фигурных скобках (``str.format_map``):

=====================  ==========================================================
``{game}``             полное название игры («World of Tanks», «Dota 2»)
``{game_short}``       короткое название («WoT», «CS2»)
``{region}``           регион объявления или профиля (RU / EU / NA / ASIA / «»)
``{highlights}``       «фишки» через « • »: «Chieftain • Об. 279 (р) • ИС-7»
``{highlights_lines}`` «фишки» построчно с «✅ » в начале каждой строки
``{price}``            наша цена с разделителем тысяч: «29 000»
``{price_raw}``        наша цена числом без форматирования: «29000»
``{source_price}``     цена исходного объявления: «15 000»
``{source_title}``     заголовок исходного объявления
``{source_description}`` описание исходного объявления
``{seller}``           имя продавца исходника (в шаблонах по умолчанию НЕ используется)
``{source}``           площадка исходника: «FunPay» / «Lolzteam»
``{profile_name}``     название профиля поиска
``{currency}``         валюта («RUB»)
``{attr[имя]}``        атрибут объявления, например ``{attr[steam_level]}``;
                       отсутствующий атрибут даёт пустую строку
=====================  ==========================================================

Неизвестный плейсхолдер остаётся в тексте как есть (``{foo}``), ошибок не бывает.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .matching import normalize_region
from .models import Listing, MatchResult, Profile

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

TITLE_MAX = 100  # лимит длины заголовка лота на FunPay (с запасом)

GAME_NAMES: dict[str, str] = {
    "wot": "World of Tanks",
    "wot_blitz": "WoT Blitz",
    "dota2": "Dota 2",
    "cs2": "CS2",
    "fortnite": "Fortnite",
    "genshin": "Genshin Impact",
    "valorant": "Valorant",
    "lol": "League of Legends",
    "pubg": "PUBG",
    "apex": "Apex Legends",
    "brawl_stars": "Brawl Stars",
    "clash_royale": "Clash Royale",
    "standoff2": "Standoff 2",
    "roblox": "Roblox",
    "eft": "Escape from Tarkov",
    "warface": "Warface",
}

GAME_SHORT: dict[str, str] = {
    "wot": "WoT",
    "wot_blitz": "Blitz",
    "dota2": "Dota 2",
    "cs2": "CS2",
    "fortnite": "Fortnite",
    "genshin": "Genshin",
    "valorant": "Valorant",
    "lol": "LoL",
    "pubg": "PUBG",
    "apex": "Apex",
    "brawl_stars": "Brawl Stars",
    "clash_royale": "Clash Royale",
    "standoff2": "Standoff 2",
    "roblox": "Roblox",
    "eft": "Tarkov",
    "warface": "Warface",
}

SOURCE_NAMES: dict[str, str] = {"funpay": "FunPay", "lolz": "Lolzteam"}

HIGHLIGHT_PREFIX = "✅ "
HIGHLIGHTS_SEP = " • "
# Строка для блока «Что внутри», если «фишек» не нашлось (чтобы блок не оставался пустым)
HIGHLIGHTS_FALLBACK_LINE = "✅ Полный состав аккаунта — уточняйте в чате, ответим быстро"

DEFAULT_TITLE_RU = "{game} | {highlights} | {region}"

DEFAULT_DESCRIPTION_RU = """🔥 {game} — аккаунт | {highlights}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 Что внутри:
{highlights_lines}

🛡 Гарантии:
✔ Аккаунт проверяется перед передачей покупателю
✔ Данные для входа передаём полностью — вы меняете пароль и привязки на свои
✔ Помогаем с первым входом и сменой данных
✔ Сделка проходит через FunPay — ваши деньги под защитой площадки

🚚 Доставка и условия:
• Регион: {region}
• Выдача данных в чате после оплаты, обычно в течение 10–60 минут
• Все подробности по аккаунту (привязки, история, статистика) — по запросу
• Не передавайте данные третьим лицам до завершения сделки

✉️ Пишите перед покупкой — подтвердим наличие и ответим на вопросы!"""

# ---------------------------------------------------------------------------
# Безопасная подстановка
# ---------------------------------------------------------------------------


class AttrMap(dict):
    """Атрибуты объявления для ``{attr[имя]}``: отсутствующий ключ -> «»."""

    def __missing__(self, key: str) -> str:
        # ищем без учёта регистра
        k = str(key).strip().lower()
        for dk, dv in self.items():
            if str(dk).strip().lower() == k:
                return _as_text(dv)
        return ""

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, key: Any) -> str:  # type: ignore[override]
        if dict.__contains__(self, key):
            return _as_text(dict.__getitem__(self, key))
        return self.__missing__(key)


class SafeDict(dict):
    """Контекст для ``format_map``: неизвестный плейсхолдер остаётся в тексте как «{key}»."""

    def __missing__(self, key: str) -> str:
        return "{" + str(key) + "}"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_as_text(v) for v in value)
    return str(value)


def render_template(template: str, context: Mapping[str, Any]) -> str:
    """Подставить плейсхолдеры; при любой ошибке шаблона вернуть текст как есть."""
    if not template:
        return ""
    ctx = context if isinstance(context, SafeDict) else SafeDict(context)
    try:
        return str(template).format_map(ctx)
    except (ValueError, KeyError, IndexError, AttributeError, TypeError):
        return str(template)


# ---------------------------------------------------------------------------
# Форматирование и очистка
# ---------------------------------------------------------------------------


def format_price(value: Any) -> str:
    """29000 -> «29 000», 29990.5 -> «29 990.50»."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    if f.is_integer():
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.2f}".replace(",", " ")


def price_raw(value: Any) -> int | float | str:
    """Цена числом (int, если целая) — чтобы работали форматы вида ``{price_raw:.0f}``."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    return int(f) if f.is_integer() else round(f, 2)


_SEP = r"[|•·—–]"
_SEP_RUN = re.compile(rf"(?:[ \t]*{_SEP}[ \t]*){{2,}}")
_SEP_LEAD = re.compile(rf"^(?:[ \t]*{_SEP}[ \t]*)+", re.M)
_SEP_TRAIL = re.compile(rf"(?:[ \t]*{_SEP}[ \t]*)+$", re.M)
_EMPTY_BRACKETS = re.compile(r"[ \t]*[(\[][ \t]*[)\]]")
_EMPTY_BULLET_LINE = re.compile(r"^[ \t]*[•\-–—✔✅☑]+[ \t]*[^\n:]{0,60}:[ \t]*$\n?", re.M)
_SPACES = re.compile(r"[ \t]{2,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.M)
_MANY_NL = re.compile(r"\n{3,}")


def clean_separators(text: str, strip_leading: bool = True) -> str:
    """Убрать пустые разделители: « | |» -> « | », замыкающие « | », « • », пустые скобки.

    ``strip_leading`` — убирать и ведущие разделители (для заголовков; в описаниях
    строки могут начинаться с маркера «•», поэтому там его не трогаем).
    """
    if not text:
        return ""
    t = _EMPTY_BRACKETS.sub("", text)
    t = _SEP_RUN.sub(lambda m: f" {m.group(0).strip()[0]} ", t)
    if strip_leading:
        t = _SEP_LEAD.sub("", t)
    t = _SEP_TRAIL.sub("", t)
    return _TRAILING_WS.sub("", t)


def clean_title(text: str) -> str:
    """Очистка заголовка/значения поля: одна строка, без пустых разделителей и двойных пробелов."""
    t = clean_separators(text.replace("\n", " "))
    return _SPACES.sub(" ", t).strip()


def clean_description(text: str) -> str:
    """Очистка описания: пустые пункты «• Регион:» удаляются, 3+ переводов строки -> 2."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _EMPTY_BULLET_LINE.sub("", t)
    t = clean_separators(t, strip_leading=False)
    t = _MANY_NL.sub("\n\n", t)
    return t.strip()


def trim_title(text: str, limit: int = TITLE_MAX) -> str:
    """Обрезать заголовок до лимита по границе слова, добавив «…»."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    idx = cut.rfind(" ")
    if idx >= limit // 2:
        cut = cut[:idx]
    cut = _SEP_TRAIL.sub("", cut).rstrip(" ,;:-–—•|")
    return cut + "…"


# ---------------------------------------------------------------------------
# Контекст и рендер
# ---------------------------------------------------------------------------


def game_name(game: str | None) -> str:
    g = (game or "").strip()
    if not g:
        return ""
    return GAME_NAMES.get(g.lower()) or g.replace("_", " ").title()


def game_short(game: str | None) -> str:
    g = (game or "").strip()
    if not g:
        return ""
    return GAME_SHORT.get(g.lower()) or game_name(g)


def build_context(listing: Listing, match: MatchResult, profile: Profile, price: float) -> SafeDict:
    """Собрать словарь плейсхолдеров для шаблонов лота."""
    hl = [h.strip() for h in (match.highlights or []) if h and h.strip()]
    region = normalize_region(listing.region) or normalize_region(profile.region) or ""
    ctx = SafeDict(
        game=game_name(profile.game),
        game_short=game_short(profile.game),
        region=region,
        highlights=HIGHLIGHTS_SEP.join(hl),
        highlights_lines="\n".join(HIGHLIGHT_PREFIX + h for h in hl) if hl else HIGHLIGHTS_FALLBACK_LINE,
        price=format_price(price),
        price_raw=price_raw(price),
        source_price=format_price(listing.price),
        source_title=(listing.title or "").strip(),
        source_description=(listing.description or "").strip(),
        seller=(listing.seller_name or "").strip(),
        source=SOURCE_NAMES.get(listing.source, str(listing.source)),
        profile_name=profile.name or profile.id,
        currency=listing.currency or "RUB",
        attr=AttrMap(listing.attributes or {}),
    )
    return ctx


def _render_title(template: str, ctx: SafeDict, limit: int = TITLE_MAX) -> str:
    """Заголовок: если не влезает в лимит — сначала убираем «фишки» с конца,
    и только потом режем по границе слова."""
    title = clean_title(render_template(template, ctx))
    if len(title) <= limit or "{highlights}" not in template:
        return trim_title(title, limit)
    hl = [h for h in str(ctx.get("highlights", "")).split(HIGHLIGHTS_SEP) if h]
    while len(title) > limit and len(hl) > 1:
        hl.pop()
        short_ctx = SafeDict(ctx)
        short_ctx["highlights"] = HIGHLIGHTS_SEP.join(hl)
        title = clean_title(render_template(template, short_ctx))
    return trim_title(title, limit)


def render_lot(listing: Listing, match: MatchResult, profile: Profile, price: float) -> dict:
    """Сформировать тексты нашего лота.

    Возвращает ``{title_ru, title_en, description_ru, description_en, price, fields}``.
    Пустой шаблон заголовка/описания RU заменяется шаблоном по умолчанию,
    пустые EN-шаблоны берутся из RU-версии.
    """
    tpl = profile.lot_template
    ctx = build_context(listing, match, profile, price)

    title_ru = _render_title(tpl.title_ru or DEFAULT_TITLE_RU, ctx)
    title_en = _render_title(tpl.title_en, ctx) if (tpl.title_en or "").strip() else title_ru

    description_ru = clean_description(render_template(tpl.description_ru or DEFAULT_DESCRIPTION_RU, ctx))
    if (tpl.description_en or "").strip():
        description_en = clean_description(render_template(tpl.description_en, ctx))
    else:
        description_en = description_ru

    fields = {str(k): clean_title(render_template(str(v), ctx)) for k, v in (tpl.fields or {}).items()}

    return {
        "title_ru": title_ru,
        "title_en": title_en,
        "description_ru": description_ru,
        "description_en": description_en,
        "price": float(price),
        "fields": fields,
    }


__all__ = [
    "DEFAULT_DESCRIPTION_RU",
    "DEFAULT_TITLE_RU",
    "GAME_NAMES",
    "GAME_SHORT",
    "SOURCE_NAMES",
    "TITLE_MAX",
    "AttrMap",
    "SafeDict",
    "build_context",
    "clean_description",
    "clean_separators",
    "clean_title",
    "format_price",
    "game_name",
    "game_short",
    "render_lot",
    "render_template",
    "trim_title",
]
