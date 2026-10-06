"""Тесты генерации текста лота (app/templating.py)."""

from __future__ import annotations

import pytest

from app.models import Listing, LotTemplate, MatchResult, Profile
from app.templating import (
    DEFAULT_DESCRIPTION_RU,
    DEFAULT_TITLE_RU,
    TITLE_MAX,
    SafeDict,
    build_context,
    clean_separators,
    clean_title,
    format_price,
    game_name,
    render_lot,
    render_template,
    trim_title,
)

SELLER = "SuperSellerXYZ"
URL = "https://funpay.com/lots/offer?id=123456"


def make_listing(**kw) -> Listing:
    base = dict(
        source="funpay",
        source_id="123456",
        url=URL,
        title="Продам акк WoT Chieftain",
        description="Описание исходника",
        price=15000,
        seller_name=SELLER,
        seller_url="https://funpay.com/users/777/",
        region="RU",
        attributes={"Steam_Level": 42, "tanks": "1 234 танка", "premium": True},
    )
    base.update(kw)
    return Listing(**base)


def make_profile(**tpl) -> Profile:
    return Profile(id="wot_ru_tops", name="WoT RU топы", game="wot", region="RU", lot_template=LotTemplate(**tpl))


MATCH = MatchResult(matched=True, score=3.5, highlights=["Chieftain", "Об. 279 (р)", "ИС-7"])


# ----------------------------------------------------------------- плейсхолдеры --


def test_all_placeholders_render():
    tpl = (
        "{game}|{game_short}|{region}|{highlights}|{price}|{price_raw}|{source_price}|"
        "{source_title}|{source_description}|{seller}|{source}|{profile_name}|{currency}|{attr[steam_level]}"
    )
    out = render_lot(make_listing(), MATCH, make_profile(description_ru=tpl), 29000)
    parts = out["description_ru"].split("|")
    assert parts == [
        "World of Tanks",
        "WoT",
        "RU",
        "Chieftain • Об. 279 (р) • ИС-7",
        "29 000",
        "29000",
        "15 000",
        "Продам акк WoT Chieftain",
        "Описание исходника",
        SELLER,
        "FunPay",
        "WoT RU топы",
        "RUB",
        "42",
    ]


def test_highlights_lines_placeholder():
    out = render_lot(make_listing(), MATCH, make_profile(description_ru="Фишки:\n{highlights_lines}"), 29000)
    assert out["description_ru"] == "Фишки:\n✅ Chieftain\n✅ Об. 279 (р)\n✅ ИС-7"


def test_unknown_placeholder_kept_literal():
    out = render_lot(make_listing(), MATCH, make_profile(title_ru="{game} {unknown_key} {region}"), 29000)
    assert out["title_ru"] == "World of Tanks {unknown_key} RU"


def test_malformed_template_does_not_raise():
    out = render_lot(make_listing(), MATCH, make_profile(title_ru="{game", description_ru="}{"), 29000)
    assert out["title_ru"] == "{game"
    assert out["description_ru"] == "}{"


def test_attr_access_missing_and_case_insensitive():
    ctx = build_context(make_listing(), MATCH, make_profile(), 29000)
    assert (
        render_template("[{attr[Steam_Level]}][{attr[steam_level]}][{attr[missing]}][{attr[premium]}]", ctx)
        == "[42][42][][да]"
    )
    assert render_template("{attr.tanks}", ctx) == "1 234 танка"


def test_price_formatting():
    assert format_price(29000) == "29 000"
    assert format_price(1234567.0) == "1 234 567"
    assert format_price(29990.5) == "29 990.50"
    assert format_price(None) == ""
    ctx = build_context(make_listing(), MATCH, make_profile(), 29990.5)
    assert render_template("{price} / {price_raw} / {price_raw:.0f}", ctx) == "29 990.50 / 29990.5 / 29990"


def test_region_fallbacks_and_synonyms():
    assert render_lot(make_listing(region=None), MATCH, make_profile(title_ru="{region}"), 1)["title_ru"] == "RU"
    assert render_lot(make_listing(region="Европа"), MATCH, make_profile(title_ru="{region}"), 1)["title_ru"] == "EU"
    prof = Profile(id="x", name="x", game="dota2", region=None, lot_template=LotTemplate(title_ru="[{region}]"))
    assert render_lot(make_listing(region=None), MATCH, prof, 1)["title_ru"] == ""


def test_game_names_and_fallback():
    assert game_name("wot") == "World of Tanks"
    assert game_name("cs2") == "CS2"
    assert game_name("eft") == "Escape from Tarkov"
    assert game_name("my_new_game") == "My New Game"
    assert game_name("") == ""


def test_source_names():
    lolz = make_listing(source="lolz")
    assert render_lot(lolz, MATCH, make_profile(title_ru="{source}"), 1)["title_ru"] == "Lolzteam"


# ----------------------------------------------------------------- заголовок --


def test_title_default_template():
    out = render_lot(make_listing(), MATCH, make_profile(title_ru=""), 29000)
    assert out["title_ru"] == "World of Tanks | Chieftain • Об. 279 (р) • ИС-7 | RU"
    assert DEFAULT_TITLE_RU == "{game} | {highlights} | {region}"


def test_title_en_derived_from_ru_when_empty():
    out = render_lot(make_listing(), MATCH, make_profile(title_en=""), 29000)
    assert out["title_en"] == out["title_ru"]
    out = render_lot(make_listing(), MATCH, make_profile(title_en="{game} account"), 29000)
    assert out["title_en"] == "World of Tanks account"


def test_title_separators_cleanup_when_highlights_empty():
    empty = MatchResult(matched=True, score=1.0, highlights=[])
    out = render_lot(make_listing(), empty, make_profile(), 29000)
    assert out["title_ru"] == "World of Tanks | RU"
    out = render_lot(make_listing(region=None), empty, Profile(id="p", name="p", game="cs2"), 29000)
    assert out["title_ru"] == "CS2"


def test_clean_separators():
    assert clean_title("| World of Tanks | | RU | ") == "World of Tanks | RU"
    assert clean_title("A •  • B") == "A • B"
    assert clean_title("WoT () [] RU") == "WoT RU"
    assert clean_separators("A | B") == "A | B"


def test_trim_title_word_boundary():
    long = "слово " * 40
    t = trim_title(long)
    assert len(t) <= TITLE_MAX
    assert t.endswith("…")
    assert not t[:-1].endswith(" ") and t[:-1].endswith("слово")
    assert trim_title("короткий") == "короткий"


def test_title_overflow_drops_highlights_first():
    many = MatchResult(matched=True, score=9, highlights=[f"Highlight{i:02d}" for i in range(20)])
    out = render_lot(make_listing(), many, make_profile(title_ru=""), 29000)
    assert len(out["title_ru"]) <= TITLE_MAX
    assert out["title_ru"].startswith("World of Tanks | Highlight00")
    assert out["title_ru"].endswith("| RU")  # хвост с регионом сохранён, а не отрезан
    assert "…" not in out["title_ru"]


def test_title_overflow_without_highlights_is_cut():
    out = render_lot(make_listing(), MATCH, make_profile(title_ru="x" * 150), 29000)
    assert len(out["title_ru"]) <= TITLE_MAX and out["title_ru"].endswith("…")


# ----------------------------------------------------------------- описание --


def test_default_description_structure():
    out = render_lot(make_listing(), MATCH, make_profile(description_ru=""), 29000)
    d = out["description_ru"]
    assert "World of Tanks" in d
    assert "Что внутри" in d
    assert "✅ Chieftain\n✅ Об. 279 (р)\n✅ ИС-7" in d
    assert "Гарантии" in d
    assert "Регион: RU" in d
    assert "Пишите перед покупкой" in d
    assert "\n\n\n" not in d
    assert out["description_en"] == d  # EN по умолчанию берётся из RU


def test_default_description_does_not_leak_seller_or_url():
    for ph in ("{seller}", "{source", "{attr"):
        assert ph not in DEFAULT_DESCRIPTION_RU
    out = render_lot(make_listing(), MATCH, make_profile(), 29000)
    for text in (out["title_ru"], out["title_en"], out["description_ru"], out["description_en"]):
        assert SELLER not in text
        assert URL not in text
        assert "funpay.com/users" not in text
        assert "Продам акк WoT" not in text  # исходный заголовок тоже не показываем


def test_default_description_with_no_highlights_has_fallback_line():
    empty = MatchResult(matched=True, score=1.0, highlights=[])
    d = render_lot(make_listing(), empty, make_profile(), 29000)["description_ru"]
    assert "Что внутри:\n✅ " in d


def test_description_empty_bullet_lines_removed_and_newlines_collapsed():
    tpl = "Заголовок:\n\n\n\n• Регион: {region}\n• Уровень: {attr[nope]}\n• Цена: {price}"
    out = render_lot(
        make_listing(region=None),
        MATCH,
        Profile(id="p", name="p", game="cs2", lot_template=LotTemplate(description_ru=tpl)),
        5000,
    )
    assert out["description_ru"] == "Заголовок:\n\n• Цена: 5 000"


def test_description_en_custom():
    out = render_lot(make_listing(), MATCH, make_profile(description_en="EN {game} {price}"), 29000)
    assert out["description_en"] == "EN World of Tanks 29 000"


# ----------------------------------------------------------------- поля и цена --


def test_fields_rendered_with_placeholders():
    prof = make_profile(
        fields={"server": "{region}", "note": "{game_short} | {attr[steam_level]} | {missing}", "x": ""}
    )
    out = render_lot(make_listing(), MATCH, prof, 29000)
    assert out["fields"] == {"server": "RU", "note": "WoT | 42 | {missing}", "x": ""}
    assert out["price"] == 29000.0
    assert set(out) == {"title_ru", "title_en", "description_ru", "description_en", "price", "fields"}


def test_safe_dict_missing():
    assert "{a} b".format_map(SafeDict(b="B")) == "{a} b"


@pytest.mark.parametrize(
    "game, expected",
    [
        ("wot_blitz", "WoT Blitz"),
        ("genshin", "Genshin Impact"),
        ("lol", "League of Legends"),
        ("standoff2", "Standoff 2"),
    ],
)
def test_game_map(game, expected):
    prof = Profile(id="p", name="p", game=game, lot_template=LotTemplate(title_ru="{game}"))
    assert render_lot(make_listing(), MATCH, prof, 1)["title_ru"] == expected
