"""Тесты матчинга объявлений по критериям (app/matching.py)."""

from __future__ import annotations

import random
import time

import pytest

from app.matching import (
    evaluate,
    get_attribute,
    match_text,
    normalize,
    normalize_region,
    parse_number,
)
from app.models import Criteria, Listing, NumericRule, PriceRange


def make_listing(title: str = "", description: str = "", price: float = 10000, region=None, **attrs) -> Listing:
    return Listing(
        source="funpay",
        source_id="1",
        url="https://funpay.com/lots/offer?id=1",
        title=title,
        description=description,
        price=price,
        region=region,
        attributes=attrs,
    )


# ----------------------------------------------------------------- normalize --


def test_normalize_basic():
    assert normalize("  Привет,   МИР! ") == "привет мир"
    assert normalize("Ёлка ёж") == "елка еж"
    assert normalize("Об. 279 (р)") == "об 279 р"
    assert normalize("1 234 танка") == "1 234 танка"
    assert normalize("") == ""


def test_normalize_keeps_decimal_and_apostrophe():
    assert normalize("1.5k и 2,5к don't") == "1.5k и 2,5к dоn't"  # «o» -> кириллица


def test_normalize_folds_homoglyphs():
    assert normalize("E 100") == normalize("Е 100")  # латиница == кириллица
    assert normalize("x lvl") == normalize("х lvl")


# ----------------------------------------------------------------- match_text --


@pytest.mark.parametrize(
    "text, term, expected",
    [
        ("Продам акк, Chieftain есть", "chieftain", True),
        ("CHIEFTAIN", "Chieftain", True),
        ("все 100 танков", "все 10", False),  # граница слова
        ("все 10 танков", "все 10", True),
        ("собака dog", "og", False),
        ("og скины", "og", True),
        ("топовый аккаунт", "топ*", True),  # хвостовой wildcard
        ("стоп", "топ*", False),
        ("топовые танки", "топ* танк*", True),  # wildcard внутри фразы
        ("чифтейн и 279", "chieftain|чифтейн", True),  # альтернативы
        ("нет ничего", "chieftain|чифтейн", False),
        ("Об. 279 (р) в ангаре", "об 279", True),  # пунктуация не важна
        ("Об.279", "об 279", True),
        ("10   lvl", "10 lvl", True),  # лишние пробелы
        ("Е 100 (кириллица)", "e 100", True),  # двойники букв
        ("без бана", "бан", False),
        ("есть бан", "бан", True),
        ("", "бан", False),
        ("текст", "", False),
        ("текст", "|", False),
    ],
)
def test_match_text(text, term, expected):
    assert match_text(text, term) is expected


def test_match_label_syntax():
    assert match_text("чифтейна нет", "Chieftain => chieftain|чифт*")
    assert not match_text("ничего", "Chieftain => chieftain|чифт*")


# ----------------------------------------------------------------- регионы --


@pytest.mark.parametrize(
    "value, expected",
    [
        ("RU", "RU"),
        ("ru", "RU"),
        ("Россия", "RU"),
        ("Lesta", "RU"),
        ("лесты", "RU"),
        ("РФ", "RU"),
        ("RU (Lesta)", "RU"),
        ("EU", "EU"),
        ("Европа", "EU"),
        ("europe", "EU"),
        ("NA", "NA"),
        ("Америка", "NA"),
        ("США", "NA"),
        ("ASIA", "ASIA"),
        ("Азия", "ASIA"),
        ("SEA", "ASIA"),
        ("euw", "EUW"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_region(value, expected):
    assert normalize_region(value) == expected


def test_region_rejects_wrong_region():
    r = evaluate(make_listing("акк", region="EU"), Criteria(regions=["RU"]))
    assert not r.matched
    assert any("регион" in x for x in r.rejections)


def test_region_synonyms_in_criteria_and_listing():
    r = evaluate(make_listing("акк", region="Lesta"), Criteria(regions=["Россия"]))
    assert r.matched and not r.rejections


def test_region_unknown_adds_reason_only():
    r = evaluate(make_listing("акк", region=None), Criteria(regions=["RU"]))
    assert r.matched
    assert "регион не определён" in r.reasons


def test_region_not_checked_when_criteria_empty():
    r = evaluate(make_listing("акк", region=None), Criteria())
    assert r.matched and "регион не определён" not in r.reasons


# ----------------------------------------------------------------- цена --


def test_price_outside_range_rejects():
    c = Criteria(price=PriceRange(min=3000, max=60000))
    assert not evaluate(make_listing("акк", price=2999), c).matched
    assert not evaluate(make_listing("акк", price=60001), c).matched
    r = evaluate(make_listing("акк", price=70000), c)
    assert any(x.startswith("цена 70 000 вне диапазона") for x in r.rejections)
    assert evaluate(make_listing("акк", price=3000), c).matched
    assert evaluate(make_listing("акк", price=60000), c).matched


def test_price_open_bounds():
    assert evaluate(make_listing("акк", price=1), Criteria(price=PriceRange(max=100))).matched
    assert not evaluate(make_listing("акк", price=1), Criteria(price=PriceRange(min=5))).matched


# ----------------------------------------------------------------- стоп-слова --


def test_exclude_rejects_with_word():
    r = evaluate(make_listing("аккаунт, есть бан чата"), Criteria(exclude=["бан", "blitz"]))
    assert not r.matched
    assert "стоп-слово: бан" in r.rejections


def test_exclude_alternatives_report_matched_variant():
    r = evaluate(make_listing("account banned"), Criteria(exclude=["бан|banned"]))
    assert "стоп-слово: banned" in r.rejections


def test_regex_exclude():
    r = evaluate(make_listing("Аккаунт, есть бан"), Criteria(regex_exclude=[r"(есть|стоит)\s+бан"]))
    assert not r.matched and any(x.startswith("стоп-выражение") for x in r.rejections)
    assert evaluate(make_listing("без бана"), Criteria(regex_exclude=[r"(есть|стоит)\s+бан"])).matched


# ----------------------------------------------------------------- must_all --


def test_must_all_requires_every_term():
    c = Criteria(must_all=["prime", "faceit"])
    r = evaluate(make_listing("CS2 prime faceit 10"), c)
    assert r.matched and r.score == 2.0
    r = evaluate(make_listing("CS2 prime"), c)
    assert not r.matched
    assert "нет обязательного слова: faceit" in r.rejections


# ----------------------------------------------------------------- must_any --


def test_must_any_none_rejects():
    r = evaluate(make_listing("просто аккаунт"), Criteria(must_any=["топы", "chieftain"]))
    assert not r.matched
    assert "нет ни одного ключевого слова (must_any)" in r.rejections


def test_must_any_scores_and_highlights():
    c = Criteria(must_any=["все топы", "чифтейн|chieftain", "об 279"])
    r = evaluate(make_listing("Все топы, Chieftain, Об. 279"), c)
    assert r.matched
    assert r.score == 3.0
    assert r.highlights == ["все топы", "chieftain", "об 279"]


def test_must_any_cap_at_five():
    terms = [f"слово{i}" for i in range(8)]
    r = evaluate(make_listing(" ".join(terms)), Criteria(must_any=terms))
    assert r.score == 5.0
    assert len(r.highlights) == 8


def test_must_any_label_used_as_highlight():
    r = evaluate(make_listing("есть чифтейн"), Criteria(must_any=["Chieftain => chieftain|чифт*"]))
    assert r.highlights == ["Chieftain"]


# ----------------------------------------------------------------- regex_any --


def test_regex_any_required_when_set():
    c = Criteria(regex_any=[r"(\d{4,5})\s*(mmr|ммр)"])
    r = evaluate(make_listing("Dota 2, 7200 MMR"), c)
    assert r.matched and r.score == 1.0
    r = evaluate(make_listing("Dota 2, immortal"), c)
    assert not r.matched
    assert "нет совпадений по регулярным выражениям (regex_any)" in r.rejections


def test_regex_any_case_insensitive_and_scores_each():
    c = Criteria(regex_any=[r"immortal", r"arcana"])
    r = evaluate(make_listing("IMMORTAL + Arcana"), c)
    assert r.score == 2.0


def test_invalid_regex_does_not_crash():
    c = Criteria(regex_any=["(unclosed", "mmr"], regex_exclude=["[bad"])
    r = evaluate(make_listing("7000 mmr"), c)
    assert r.matched
    assert any("неверное регулярное выражение" in x for x in r.reasons)


# ----------------------------------------------------------------- числа --


@pytest.mark.parametrize(
    "value, expected",
    [
        (1234, 1234.0),
        (12.5, 12.5),
        ("1 234 танка", 1234.0),
        ("1 234", 1234.0),
        ("6,5k mmr", 6500.0),
        ("6.5K", 6500.0),
        ("2,500", 2500.0),
        ("2,5", 2.5),
        ("уровень: 42", 42.0),
        ("нет", None),
        (None, None),
        ("", None),
        (True, None),
        ("1m", 1_000_000.0),
    ],
)
def test_parse_number(value, expected):
    assert parse_number(value) == expected


def test_get_attribute_case_insensitive_and_nested():
    attrs = {"Steam_Level": 42, "dota": {"Solo_MMR": "7 200"}}
    assert get_attribute(attrs, "steam_level") == (True, 42)
    assert get_attribute(attrs, "dota.solo_mmr") == (True, "7 200")
    assert get_attribute(attrs, "missing") == (False, None)
    assert get_attribute(attrs, "dota.missing") == (False, None)


def test_numeric_rule_from_string_value():
    c = Criteria(numeric=[NumericRule(field="tanks", min=1000)])
    r = evaluate(make_listing("акк", tanks="1 234 танка"), c)
    assert r.matched and "поле tanks = 1 234" in r.reasons and r.score == 1.0  # нет «положительных» слов — базовый 1.0
    r = evaluate(make_listing("акк", tanks="1 234 танка"), Criteria(must_any=["акк"], numeric=c.numeric))
    assert r.matched and r.score == 2.0  # must_any +1, числовое правило +1
    r = evaluate(make_listing("акк", tanks="500 танков"), c)
    assert not r.matched and any("вне диапазона" in x for x in r.rejections)


def test_numeric_rule_missing_is_lenient():
    c = Criteria(numeric=[NumericRule(field="tanks", min=10, max=100)])
    r = evaluate(make_listing("акк"), c)
    assert r.matched
    assert "поле tanks отсутствует" in r.reasons


def test_numeric_rule_case_insensitive_field_and_nested():
    c = Criteria(numeric=[NumericRule(field="dota.solo_mmr", min=6000)])
    r = evaluate(make_listing("акк", dota={"Solo_MMR": 7200}), c)
    assert r.matched and "поле dota.solo_mmr = 7 200" in r.reasons


# ----------------------------------------------------------------- highlights --


def test_highlights_keep_criteria_casing_and_score():
    c = Criteria(highlights=["Chieftain", "Об. 279 (р)", "ИС-7", "Maus"])
    r = evaluate(make_listing("акк: chieftain, об 279 р, ис-7"), c)
    assert r.highlights == ["Chieftain", "Об. 279 (р)", "ИС-7"]
    assert r.score == 1.5
    assert r.matched


def test_highlights_dedupe_with_must_any():
    c = Criteria(must_any=["чифтейн|chieftain"], highlights=["Chieftain"])
    r = evaluate(make_listing("chieftain в ангаре"), c)
    assert r.highlights == ["Chieftain"]


def test_highlights_shorter_form_merged_into_longer():
    c = Criteria(highlights=["Об. 279", "Об. 279 (р)"])
    r = evaluate(make_listing("Об. 279 (р)"), c)
    assert r.highlights == ["Об. 279 (р)"]


def test_highlights_in_attributes_text():
    c = Criteria(highlights=["Prime"])
    r = evaluate(make_listing("акк", prime_status="Prime есть"), c)
    assert r.highlights == ["Prime"]


# ----------------------------------------------------------------- продавец --


def test_seller_min_reviews():
    c = Criteria(seller_min_reviews=10)
    assert not evaluate(make_listing("акк", seller_reviews=3), c).matched
    assert evaluate(make_listing("акк", seller_reviews="25 отзывов"), c).matched
    assert evaluate(make_listing("акк"), c).matched  # нет данных — не отклоняем


# ----------------------------------------------------------------- итог --


def test_min_score_threshold():
    c = Criteria(highlights=["Chieftain"], min_score=1.0)
    r = evaluate(make_listing("chieftain"), c)
    assert not r.matched and r.score == 0.5
    assert any(x.startswith("балл 0.5 ниже порога 1") for x in r.rejections)
    c2 = Criteria(highlights=["Chieftain"], min_score=0.5)
    assert evaluate(make_listing("chieftain"), c2).matched


def test_no_positive_terms_baseline():
    r = evaluate(make_listing("что угодно"), Criteria(min_score=5.0))
    assert r.matched and r.score == 1.0 and r.rejections == []


def test_rejection_with_positive_terms_keeps_score():
    c = Criteria(must_any=["топы"], exclude=["бан"])
    r = evaluate(make_listing("топы, есть бан"), c)
    assert not r.matched and r.score == 1.0 and r.highlights == ["топы"]


def test_full_realistic_profile():
    c = Criteria(
        price=PriceRange(min=3000, max=60000),
        must_any=["Все топы => все топы|топ*", "Chieftain => chieftain|чифт*"],
        exclude=["бан|banned", "blitz"],
        highlights=["Об. 279 (р) => об 279|279 р", "E 100 => е 100|e-100", "ИС-7 => ис-7|ис7"],
        regions=["RU"],
    )
    lst = make_listing("Аккаунт Мир танков: все топы, Chieftain, Об. 279 (р), E 100, ИС-7", price=15000, region="Lesta")
    r = evaluate(lst, c)
    assert r.matched
    assert r.highlights == ["Об. 279 (р)", "E 100", "ИС-7", "Все топы", "Chieftain"]
    assert r.score == 3.5


# ----------------------------------------------------------------- скорость --


def test_performance_5000_listings():
    random.seed(1)
    words = [
        "аккаунт",
        "танки",
        "chieftain",
        "об 279",
        "топы",
        "10 lvl",
        "прем",
        "голда",
        "бан",
        "ис-7",
        "e 100",
        "kranvagn",
        "maus",
        "продам",
        "дёшево",
        "срочно",
        "привязка",
        "почта",
    ]
    listings = [
        make_listing(
            " ".join(random.choices(words, k=12)),
            " ".join(random.choices(words, k=40)),
            price=random.randint(1000, 90000),
            region=random.choice(["RU", "EU", None]),
            tanks=f"{random.randint(1, 2000)} танков",
        )
        for _ in range(5000)
    ]
    c = Criteria(
        price=PriceRange(min=3000, max=60000),
        must_any=[
            "Все топы => все топы|топ*",
            "10 lvl|10 лвл|х лвл",
            "Chieftain => chieftain|чифт*",
            "об 279|объект 279",
            "прем*",
            "голд*|gold",
        ],
        exclude=["бан|banned|забанен*", "без почты", "blitz", "аренда"],
        regex_any=[r"(топ|chieftain|279|прем|голд)"],
        regex_exclude=[r"(есть|стоит)\s+бан"],
        numeric=[NumericRule(field="tanks", min=10)],
        highlights=[
            "Chieftain",
            "Об. 279 (р) => об 279|279 р",
            "E 100 => е 100|e-100",
            "ИС-7 => ис-7|ис7",
            "Kranvagn => kranvagn|крана",
            "Maus => maus|маус",
            "60TP",
            "Strv",
            "Progetto",
            "FV4005",
        ],
        regions=["RU"],
    )
    t0 = time.perf_counter()
    results = [evaluate(lst, c) for lst in listings]
    elapsed = time.perf_counter() - t0
    assert len(results) == 5000
    assert any(r.matched for r in results)
    assert elapsed < 6.0, f"слишком медленно: {elapsed:.2f}s"  # с запасом для загруженных CI-машин
