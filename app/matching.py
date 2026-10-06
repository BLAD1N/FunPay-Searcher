"""Проверка объявлений по критериям профиля (матчинг).

Главная функция — :func:`evaluate`: принимает объявление и критерии, возвращает
:class:`~app.models.MatchResult` с баллом, причинами совпадения, причинами отказа
и списком «фишек» для заголовка лота.

Как работают ключевые слова (must_any / must_all / exclude / highlights):

* сравнение регистронезависимое, «ё» = «е», латинские буквы-двойники
  (a, c, e, o, p, x, y) приводятся к кириллице — поэтому «Е 100» и «E 100»,
  «х лвл» и «x lvl» считаются одним и тем же словом;
* слово ищется целиком (по границам слов): «10» не найдётся внутри «100»,
  «og» — внутри «dog»;
* несколько вариантов через «|»: ``"чифтейн|chieftain"``;
* звёздочка — любое окончание слова: ``"топ*"`` найдёт «топы», «топовый», «топов»;
  можно и внутри фразы: ``"топ* танк*"``;
* фраза с пробелами ищется как фраза: ``"все топы"``; знаки препинания при этом
  не важны («Об. 279» == «об 279»);
* подпись для «фишки» через «=>»: ``"Chieftain => chieftain|чифт*"`` — в заголовок
  лота попадёт «Chieftain», какой бы вариант ни совпал. Без подписи в заголовок
  попадает совпавший вариант в том виде, как он записан в критериях.

Регулярные выражения (regex_any / regex_exclude) применяются к исходному тексту
объявления (заголовок + описание + атрибуты) без учёта регистра.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from .models import Criteria, Listing, MatchResult, NumericRule

# ---------------------------------------------------------------------------
# Нормализация текста
# ---------------------------------------------------------------------------

# Латинские буквы, внешне совпадающие с кириллическими, приводим к кириллице.
# Нормализуются и текст, и условия, поэтому «E 100» (латиница) == «Е 100» (кириллица).
_HOMOGLYPHS = str.maketrans(
    {
        "a": "а",
        "c": "с",
        "e": "е",
        "o": "о",
        "p": "р",
        "x": "х",
        "y": "у",
        "ё": "е",
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "’": "'",
        "‘": "'",
        "–": "-",
        "—": "-",
        " ": " ",
    }
)
_PUNCT = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")


def _punct_repl(m: re.Match) -> str:
    """Знаки препинания превращаем в пробел, кроме точки/запятой между цифрами
    (1.5k, 2,5к) и апострофа внутри слова (don't)."""
    s = m.group(0)
    if len(s) == 1 and s in ".,'":
        text = m.string
        i, j = m.start(), m.end()
        if i > 0 and j < len(text):
            left, right = text[i - 1], text[j]
            if s == "'" and left.isalpha() and right.isalpha():
                return s
            if s in ".," and left.isdigit() and right.isdigit():
                return s
    return " "


def normalize(text: str) -> str:
    """Привести текст к виду для поиска по словам.

    Нижний регистр, «ё» -> «е», латинские двойники -> кириллица, знаки препинания
    вокруг слов убираются, повторные пробелы схлопываются, цифры сохраняются.
    """
    if not text:
        return ""
    t = str(text).lower().translate(_HOMOGLYPHS)
    t = _PUNCT.sub(_punct_repl, t)
    return _WS.sub(" ", t).strip()


# ---------------------------------------------------------------------------
# Регионы
# ---------------------------------------------------------------------------

_REGION_SYNONYMS_RAW: dict[str, str] = {
    "ru": "RU",
    "россия": "RU",
    "рф": "RU",
    "lesta": "RU",
    "леста": "RU",
    "лесты": "RU",
    "ру": "RU",
    "снг": "RU",
    "cis": "RU",
    "russia": "RU",
    "lesta games": "RU",
    "ru lesta": "RU",
    "eu": "EU",
    "европа": "EU",
    "europe": "EU",
    "евро": "EU",
    "wg eu": "EU",
    "eu wargaming": "EU",
    "na": "NA",
    "америка": "NA",
    "сша": "NA",
    "usa": "NA",
    "us": "NA",
    "north america": "NA",
    "америка na": "NA",
    "сев америка": "NA",
    "asia": "ASIA",
    "азия": "ASIA",
    "sea": "ASIA",
    "asian": "ASIA",
    "apac": "ASIA",
    "sg": "ASIA",
    "global": "GLOBAL",
    "глобал": "GLOBAL",
    "мир": "GLOBAL",
    "world": "GLOBAL",
    "any": "GLOBAL",
    "любой": "GLOBAL",
}
# ключи приводим той же нормализацией, что и входные строки
REGION_SYNONYMS: dict[str, str] = {normalize(k): v for k, v in _REGION_SYNONYMS_RAW.items()}


def normalize_region(value: Any) -> str | None:
    """Привести обозначение региона к коду: RU / EU / NA / ASIA / GLOBAL / <как есть>.

    Понимает синонимы («Россия», «Lesta», «Европа», «США», «Азия» ...) и строки вида
    «RU (Lesta)». Неизвестное значение возвращается в верхнем регистре.
    """
    if value is None:
        return None
    key = normalize(str(value))
    if not key:
        return None
    if key in REGION_SYNONYMS:
        return REGION_SYNONYMS[key]
    for tok in key.split():
        if tok in REGION_SYNONYMS:
            return REGION_SYNONYMS[tok]
    return str(value).strip().upper()


# ---------------------------------------------------------------------------
# Ключевые слова
# ---------------------------------------------------------------------------

_WILD = "ωwildω"  # временная метка для «*» (состоит из букв, переживает normalize)
_LABEL_SEP = "=>"


def _alt_pattern(alt: str) -> str | None:
    """Собрать регулярное выражение для одного варианта слова (без «|»)."""
    alt = alt.strip()
    if not alt:
        return None
    core = normalize(alt.replace("*", _WILD))
    core = re.sub(rf"(?:{_WILD})+", _WILD, core)
    bare = core.replace(_WILD, "")
    if not bare.strip():
        return None
    pat = re.escape(core).replace("\\ ", " ").replace(" ", r"\s+").replace(_WILD, r"\w*")
    if not core.startswith(_WILD) and (core[0].isalnum() or core[0] == "_"):
        pat = r"(?<!\w)" + pat
    if not core.endswith(_WILD) and (core[-1].isalnum() or core[-1] == "_"):
        pat += r"(?!\w)"
    return pat


@lru_cache(maxsize=4096)
def _compile_term(term: str) -> tuple[tuple[str, re.Pattern], ...]:
    """Условие «Подпись => а|б*|в г» -> кортеж (что показывать, скомпилированный шаблон)."""
    term = str(term)
    label: str | None = None
    if _LABEL_SEP in term:
        label, term = (x.strip() for x in term.split(_LABEL_SEP, 1))
        label = label or None
    out: list[tuple[str, re.Pattern]] = []
    for alt in term.split("|"):
        pat = _alt_pattern(alt)
        if pat is None:
            continue
        shown = label or alt.strip().replace("*", "").strip()
        out.append((shown, re.compile(pat)))
    return tuple(out)


def _match_norm(norm_text: str, term: str) -> str | None:
    """Найти условие в уже нормализованном тексте. Возвращает совпавший вариант
    в том виде, как он записан в критериях (без «*»), либо None."""
    for shown, pat in _compile_term(term):
        if pat.search(norm_text):
            return shown
    return None


def match_text(text: str, term: str) -> bool:
    """Есть ли слово/фраза ``term`` в тексте (см. правила в описании модуля)."""
    return _match_norm(normalize(text), term) is not None


@lru_cache(maxsize=1024)
def _compile_regex(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern, re.I | re.S)
    except re.error:
        return None


# ---------------------------------------------------------------------------
# Числа и атрибуты
# ---------------------------------------------------------------------------

_NUM = re.compile(r"[-+]?(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?")
_THOUSANDS = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+")


def parse_number(value: Any) -> float | None:
    """Вытащить число из значения атрибута: 1234, "1 234 танка", "6,5k mmr" -> 6500."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    m = _NUM.search(s)
    if not m:
        return None
    num = m.group(0).replace(" ", "").replace(" ", "")
    if _THOUSANDS.fullmatch(num):
        num = num.replace(",", "")
    num = num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None
    tail = s[m.end() : m.end() + 2].lower()
    if tail[:1] in ("k", "к") and not tail[1:2].isalpha():
        v *= 1000
    elif tail[:1] in ("m", "м") and not tail[1:2].isalpha():
        v *= 1_000_000
    return v


def _lookup_key(d: dict, key: str) -> tuple[bool, Any]:
    if key in d:
        return True, d[key]
    k = key.strip().lower()
    for dk, dv in d.items():
        if isinstance(dk, str) and dk.strip().lower() == k:
            return True, dv
    return False, None


def get_attribute(attributes: dict[str, Any], field: str) -> tuple[bool, Any]:
    """Найти поле в attributes: точное имя, без учёта регистра, вложенные ключи «a.b»."""
    if not attributes or not field:
        return False, None
    found, value = _lookup_key(attributes, field)
    if found:
        return True, value
    if "." in field:
        cur: Any = attributes
        for part in field.split("."):
            if not isinstance(cur, dict):
                return False, None
            ok, cur = _lookup_key(cur, part)
            if not ok:
                return False, None
        return True, cur
    return False, None


def _fmt(v: Any) -> str:
    """Число для сообщений: 29000 -> «29 000», 2.5 -> «2.5»."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f.is_integer():
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


def _add_highlight(highlights: list[str], norm_forms: list[str], text: str) -> None:
    """Добавить «фишку» без дублей: «Об. 279» и «Об. 279 (р)» -> остаётся более полная."""
    text = text.strip()
    nf = normalize(text)
    if not nf:
        return
    for i, existing in enumerate(norm_forms):
        if nf == existing or f" {nf} " in f" {existing} ":
            return  # уже есть такая же или более полная
        if f" {existing} " in f" {nf} ":
            highlights[i] = text  # новая полнее — заменяем
            norm_forms[i] = nf
            return
    highlights.append(text)
    norm_forms.append(nf)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

MUST_ANY_CAP = 5.0  # максимум баллов за must_any
HIGHLIGHT_SCORE = 0.5  # балл за каждую найденную «фишку»


def evaluate(listing: Listing, criteria: Criteria) -> MatchResult:
    """Проверить объявление по критериям и посчитать балл.

    Правила (кратко):
      * цена вне диапазона -> отказ;
      * регион известен и не входит в допустимые -> отказ; не определён -> только пометка;
      * любое стоп-слово / стоп-выражение -> отказ;
      * каждое слово из must_all обязано встретиться (+1 балл за слово);
      * хотя бы одно из must_any (+1 балл за слово, максимум 5), иначе отказ;
      * хотя бы одно из regex_any (+1 за выражение), иначе отказ;
      * числовые правила: поле отсутствует — пометка, вне диапазона — отказ, в диапазоне +1;
      * «фишки» из highlights: +0.5 за каждую, попадают в result.highlights;
      * мало отзывов у продавца -> отказ;
      * matched = нет отказов и score >= min_score (если в критериях нет ни одного
        «положительного» условия, балл считается равным 1.0 и порог не применяется).
    """
    reasons: list[str] = []
    rejections: list[str] = []
    highlights: list[str] = []
    norm_forms: list[str] = []
    score = 0.0

    raw_text = listing.text_blob()
    norm_text = normalize(raw_text)

    # --- цена -------------------------------------------------------------
    pmin, pmax = criteria.price.min, criteria.price.max
    price = float(listing.price or 0)
    if (pmin is not None and price < pmin) or (pmax is not None and price > pmax):
        lo = _fmt(pmin) if pmin is not None else "0"
        hi = _fmt(pmax) if pmax is not None else "∞"
        rejections.append(f"цена {_fmt(price)} вне диапазона {lo}–{hi}")

    # --- регион -----------------------------------------------------------
    if criteria.regions:
        allowed = {r for r in (normalize_region(x) for x in criteria.regions) if r}
        region = normalize_region(listing.region)
        if region is None:
            reasons.append("регион не определён")
        elif allowed and region not in allowed:
            rejections.append(f"регион {region} не входит в {', '.join(sorted(allowed))}")

    # --- стоп-слова -------------------------------------------------------
    for term in criteria.exclude:
        hit = _match_norm(norm_text, term)
        if hit is not None:
            rejections.append(f"стоп-слово: {hit}")
    for pattern in criteria.regex_exclude:
        rx = _compile_regex(pattern)
        if rx is None:
            reasons.append(f"неверное регулярное выражение: {pattern}")
            continue
        if rx.search(raw_text):
            rejections.append(f"стоп-выражение: {pattern}")

    # --- обязательные слова ----------------------------------------------
    for term in criteria.must_all:
        hit = _match_norm(norm_text, term)
        if hit is None:
            rejections.append(f"нет обязательного слова: {term}")
        else:
            score += 1.0
            reasons.append(f"обязательное слово: {hit}")

    # --- хотя бы одно из --------------------------------------------------
    if criteria.must_any:
        any_score = 0.0
        for term in criteria.must_any:
            hit = _match_norm(norm_text, term)
            if hit is not None:
                any_score += 1.0
                reasons.append(f"ключевое слово: {hit}")
                _add_highlight(highlights, norm_forms, hit)
        if any_score == 0:
            rejections.append("нет ни одного ключевого слова (must_any)")
        score += min(any_score, MUST_ANY_CAP)

    # --- регулярные выражения --------------------------------------------
    if criteria.regex_any:
        valid = 0
        matched_rx = 0
        for pattern in criteria.regex_any:
            rx = _compile_regex(pattern)
            if rx is None:
                reasons.append(f"неверное регулярное выражение: {pattern}")
                continue
            valid += 1
            if rx.search(raw_text):
                matched_rx += 1
                score += 1.0
                reasons.append(f"регулярное выражение: {pattern}")
        if valid and not matched_rx:
            rejections.append("нет совпадений по регулярным выражениям (regex_any)")

    # --- числовые правила -------------------------------------------------
    for rule in criteria.numeric:
        if _apply_numeric(rule, listing.attributes, reasons, rejections):
            score += 1.0

    # --- «фишки» ------------------------------------------------------------
    if criteria.highlights:
        found: list[str] = []
        for term in criteria.highlights:
            hit = _match_norm(norm_text, term)
            if hit is not None:
                found.append(hit)
                score += HIGHLIGHT_SCORE
        if found:
            reasons.append("фишки: " + ", ".join(found))
            # фишки из highlights — в начало (они подобраны для заголовка)
            merged: list[str] = []
            merged_norm: list[str] = []
            for h in found + highlights:
                _add_highlight(merged, merged_norm, h)
            highlights = merged

    # --- продавец -----------------------------------------------------------
    if criteria.seller_min_reviews is not None:
        ok, raw = get_attribute(listing.attributes, "seller_reviews")
        n = parse_number(raw) if ok else None
        if n is not None and n < criteria.seller_min_reviews:
            rejections.append(f"отзывов продавца {_fmt(n)} < {criteria.seller_min_reviews}")

    # --- итог -----------------------------------------------------------------
    has_positive = bool(criteria.must_any or criteria.must_all or criteria.regex_any or criteria.highlights)
    score = round(score, 2)
    if has_positive:
        if not rejections and score < criteria.min_score:
            rejections.append(f"балл {_fmt(score)} ниже порога {_fmt(criteria.min_score)}")
    else:
        score = max(score, 1.0)
    matched = not rejections
    return MatchResult(matched=matched, score=score, reasons=reasons, rejections=rejections, highlights=highlights)


def _apply_numeric(rule: NumericRule, attributes: dict[str, Any], reasons: list[str], rejections: list[str]) -> bool:
    """Применить числовое правило. True — поле найдено и укладывается в диапазон."""
    found, raw = get_attribute(attributes, rule.field)
    value = parse_number(raw) if found else None
    if value is None:
        reasons.append(f"поле {rule.field} отсутствует")
        return False
    lo = rule.min if rule.min is not None else float("-inf")
    hi = rule.max if rule.max is not None else float("inf")
    if value < lo or value > hi:
        lo_s = _fmt(rule.min) if rule.min is not None else "0"
        hi_s = _fmt(rule.max) if rule.max is not None else "∞"
        rejections.append(f"поле {rule.field} = {_fmt(value)} вне диапазона {lo_s}–{hi_s}")
        return False
    reasons.append(f"поле {rule.field} = {_fmt(value)}")
    return True


def evaluate_many(listings: Iterable[Listing], criteria: Criteria) -> list[tuple[Listing, MatchResult]]:
    """Удобная обёртка: проверить список объявлений."""
    return [(lst, evaluate(lst, criteria)) for lst in listings]


__all__ = [
    "REGION_SYNONYMS",
    "evaluate",
    "evaluate_many",
    "get_attribute",
    "match_text",
    "normalize",
    "normalize_region",
    "parse_number",
]
