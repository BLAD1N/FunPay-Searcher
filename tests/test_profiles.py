"""Тесты стартовых профилей (config/profiles/*.yaml) и примера настроек."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.matching import _compile_term, _match_norm, evaluate, normalize
from app.models import Listing, MatchResult, Profile
from app.pricing import calculate_price
from app.profiles import ProfileStore
from app.settings import Settings
from app.templating import TITLE_MAX, render_lot

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "config" / "profiles"
SETTINGS_EXAMPLE = ROOT / "config" / "settings.example.yaml"

EXPECTED_IDS = {
    "wot_ru_tops", "wot_eu_tops", "wot_na_tops", "wot_asia_tops", "wot_blitz",
    "dota2_high_mmr", "cs2_prime", "fortnite_skins", "genshin", "valorant",
    "pubg", "apex", "brawl_stars", "clash_royale", "standoff2", "roblox", "eft",
    "warface", "lol", "steam_inventory", "any_game_cheap_tops",
}
ENABLED_BY_DEFAULT = {"wot_ru_tops", "dota2_high_mmr", "cs2_prime"}

# Плейсхолдеры, которые раскрывают покупателю исходник/продавца — в шаблонах запрещены.
LEAKING_PLACEHOLDERS = ("{seller}", "{source}", "{source_title}", "{source_description}")

# Слишком общие слова: в must_any они дают сплошные ложные срабатывания.
TOO_GENERIC_ALTS = {"акк", "аккаунт", "account", "премиум", "premium", "прем", "скин", "скины",
                    "skin", "skins", "инвентарь", "inventory", "рейтинг", "rating", "редкие", "редкий",
                    "rare", "танки", "tanks", "игра", "game"}
# Голые стоп-слова, которые отсекают хорошие объявления («без бана», «no ban», «обмен доступен»).
DANGEROUS_EXCLUDES = {"бан", "ban", "обмен", "trade", "временно", "без привязки", "прокачка", "чит*",
                      "vac", "ключ", "key", "холд", "чистый", "пин", "pin"}


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    store = ProfileStore(PROFILES_DIR)
    loaded = store.list()
    return {p.id: p for p in loaded}


@pytest.fixture(scope="module")
def raw_profiles() -> dict[str, dict]:
    return {
        p.stem: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in PROFILES_DIR.glob("*.yaml")
    }


def _alts(term: str) -> list[str]:
    """Варианты условия без подписи и «*»: "Chieftain => chieftain|чифт*" -> ["chieftain", "чифт"]."""
    if "=>" in term:
        term = term.split("=>", 1)[1]
    return [normalize(a.replace("*", "")) for a in term.split("|") if normalize(a.replace("*", ""))]


def _listing(title: str, price: float, region=None, source="funpay", **attrs) -> Listing:
    return Listing(source=source, source_id="1", url="https://funpay.com/lots/offer?id=1",
                   title=title, price=price, region=region, seller_name="SELLER_XYZ_777",
                   attributes=attrs)


def _example_listing(item, p: Profile) -> Listing:
    mid = (p.criteria.price.min + p.criteria.price.max) / 2
    if isinstance(item, str):
        item = {"text": item}
    return _listing(item["text"], item.get("price", mid), region=item.get("region", p.region),
                    **(item.get("attrs") or {}))


# ----------------------------------------------------------------- загрузка --


def test_every_yaml_loads(profiles):
    files = {p.stem for p in PROFILES_DIR.glob("*.yaml")}
    assert files == EXPECTED_IDS
    assert set(profiles) == files  # ни один файл не отброшен как «битый»


def test_ids_match_file_names(profiles):
    for pid, p in profiles.items():
        assert p.id == pid
        raw = yaml.safe_load((PROFILES_DIR / f"{pid}.yaml").read_text(encoding="utf-8"))
        assert raw.get("id") == pid


def test_no_duplicate_ids(raw_profiles):
    ids = [raw.get("id") for raw in raw_profiles.values()]
    assert len(ids) == len(set(ids)), ids
    names = [raw.get("name") for raw in raw_profiles.values()]
    assert len(names) == len(set(names)), "названия профилей должны быть уникальны"


def test_enabled_defaults(profiles):
    assert {pid for pid, p in profiles.items() if p.enabled} == ENABLED_BY_DEFAULT


def test_profiles_have_sane_fields(profiles):
    for p in profiles.values():
        assert p.name and p.game
        assert p.criteria.price.min is not None and p.criteria.price.max is not None
        assert 0 < p.criteria.price.min < p.criteria.price.max
        fp, lz = p.funpay(), p.lolz()
        assert fp.enabled or lz.enabled, p.id
        if fp.enabled:
            assert fp.game_query or fp.subcategory_id or fp.subcategory_ids, p.id
        if lz.enabled:
            assert lz.category, p.id
        assert p.lot_template.amount == 1


def test_profiles_have_positive_conditions(profiles):
    for p in profiles.values():
        c = p.criteria
        assert c.must_any or c.regex_any or c.highlights, p.id
        assert c.must_any, f"{p.id}: без must_any любой текст с подходящей ценой станет кандидатом"
        assert c.highlights, f"{p.id}: без highlights заголовок лота будет пустым"
        assert c.exclude, f"{p.id}: нет стоп-слов"


def test_regions_and_server_filters(profiles):
    assert profiles["wot_ru_tops"].criteria.regions == ["RU"]
    assert profiles["wot_eu_tops"].criteria.regions == ["EU"]
    assert profiles["wot_na_tops"].criteria.regions == ["NA"]
    assert profiles["wot_asia_tops"].criteria.regions == ["ASIA"]
    assert "Lesta" in profiles["wot_ru_tops"].funpay().server_filter
    assert "EU" in profiles["wot_eu_tops"].funpay().server_filter
    assert profiles["dota2_high_mmr"].lolz().category == "steam"
    assert profiles["dota2_high_mmr"].lolz().params["game"] == [570]
    assert profiles["cs2_prime"].lolz().params["game"] == [730]
    assert profiles["pubg"].lolz().params["game"] == [578080]
    assert profiles["wot_ru_tops"].lolz().category == "world-of-tanks"
    assert profiles["brawl_stars"].lolz().category == "supercell"
    assert profiles["standoff2"].lolz().enabled is False and profiles["standoff2"].funpay().enabled
    for p in profiles.values():
        if p.region and p.criteria.regions:
            assert p.region in p.criteria.regions, p.id


def test_all_terms_and_regexes_compile(profiles):
    for p in profiles.values():
        c = p.criteria
        for term in c.must_any + c.must_all + c.exclude + c.highlights:
            assert _compile_term(term), f"{p.id}: пустое условие {term!r}"
        for rx in c.regex_any + c.regex_exclude:
            re.compile(rx, re.I)


def test_no_generic_words_in_must_any(profiles):
    for p in profiles.values():
        for term in p.criteria.must_any:
            alts = _alts(term)
            bad = TOO_GENERIC_ALTS & set(alts)
            assert not bad, f"{p.id}: слишком общее слово {bad} в must_any {term!r}"


def test_no_dangerous_bare_excludes(profiles):
    for p in profiles.values():
        for term in p.criteria.exclude:
            raw_alts = {a.strip().lower() for a in term.split("|")}
            bad = DANGEROUS_EXCLUDES & raw_alts
            assert not bad, f"{p.id}: голое стоп-слово {bad} отсечёт хорошие объявления ({term!r})"


def test_excludes_do_not_contradict_positives(profiles):
    """Ни один вариант из must_any/must_all/highlights не должен сам по себе
    срабатывать как стоп-слово — иначе профиль противоречит сам себе."""
    for p in profiles.values():
        c = p.criteria
        positives = [(kind, term, alt) for kind, terms in (("must_any", c.must_any), ("must_all", c.must_all),
                                                           ("highlights", c.highlights))
                     for term in terms for alt in _alts(term)]
        for kind, term, alt in positives:
            for ex in c.exclude:
                hit = _match_norm(alt, ex)
                assert hit is None, f"{p.id}: {kind} {term!r} (вариант {alt!r}) совпадает со стоп-словом {ex!r}"


def test_pricing_gives_profit(profiles):
    for p in profiles.values():
        for price in (500, 1500, 3000, 15000, 42350, 60000, 80000):
            ours = calculate_price(price, p.pricing)
            assert ours > price, (p.id, price, ours)
            assert ours - price >= p.pricing.min_margin - p.pricing.round_to, (p.id, price, ours)
            if p.pricing.price_ending is None:
                assert ours % p.pricing.round_to == 0
            else:
                assert ours % 1000 == p.pricing.price_ending


# ----------------------------------------------------------------- шаблоны --


def test_lot_templates_render_and_do_not_leak(profiles):
    match = MatchResult(matched=True, score=4.0,
                        highlights=["Chieftain", "Об. 279 (р)", "Carro 45t", "Kampfpanzer 07 RH",
                                    "VK 72.01 K", "Коллекционер", "Полный доступ"])
    for p in profiles.values():
        tpl = p.lot_template
        for field in ("title_ru", "title_en", "description_ru", "description_en"):
            text = getattr(tpl, field)
            for ph in LEAKING_PLACEHOLDERS:
                assert ph not in text, f"{p.id}.{field} раскрывает исходник: {ph}"
        assert tpl.title_ru.strip(), f"{p.id}: нужен свой title_ru"
        assert tpl.description_ru.strip(), f"{p.id}: нужен свой description_ru"
        assert "{highlights}" in tpl.title_ru, p.id
        assert "{highlights_lines}" in tpl.description_ru, p.id
        mid = (p.criteria.price.min + p.criteria.price.max) / 2
        lst = _listing("Исходный заголовок", mid, region=p.region, source="lolz")
        lot = render_lot(lst, match, p, calculate_price(mid, p.pricing))
        for key in ("title_ru", "title_en"):
            assert 0 < len(lot[key]) <= TITLE_MAX, (p.id, key, lot[key])
            assert "{" not in lot[key] and "}" not in lot[key], (p.id, key, lot[key])
            assert "Chieftain" in lot[key], (p.id, key, lot[key])
        for key in ("description_ru", "description_en"):
            assert "{" not in lot[key] and "}" not in lot[key], (p.id, key)
            assert "✅ Chieftain" in lot[key], (p.id, key)
            assert "SELLER_XYZ_777" not in lot[key] and "Lolzteam" not in lot[key], (p.id, key)
            assert "Исходный заголовок" not in lot[key], (p.id, key)
    titles = {pid: p.lot_template.title_ru for pid, p in profiles.items()}
    assert len(set(titles.values())) == len(titles), "title_ru должны быть разными у всех профилей"


def test_title_survives_many_highlights(profiles):
    match = MatchResult(matched=True, score=9.0, highlights=[f"Фишка номер {i}" for i in range(20)])
    for p in profiles.values():
        lot = render_lot(_listing("x", 5000, region=p.region), match, p, 9000)
        assert len(lot["title_ru"]) <= TITLE_MAX, (p.id, lot["title_ru"])
        assert "Фишка номер 0" in lot["title_ru"], p.id


# ----------------------------------------------------------------- примеры --


def test_every_profile_has_examples(raw_profiles):
    for pid, raw in raw_profiles.items():
        ex = raw.get("examples") or {}
        assert len(ex.get("positive") or []) >= 2, f"{pid}: нужно минимум 2 положительных примера"
        assert len(ex.get("negative") or []) >= 3, f"{pid}: нужно минимум 3 отрицательных примера"


def test_examples_match_and_reject(profiles, raw_profiles):
    for pid, raw in raw_profiles.items():
        p = profiles[pid]
        ex = raw.get("examples") or {}
        for item in ex.get("positive") or []:
            lst = _example_listing(item, p)
            r = evaluate(lst, p.criteria)
            assert r.matched, (pid, lst.title, r.rejections)
            assert r.highlights, (pid, lst.title, "положительный пример без «фишек» — заголовок будет пустым")
        for item in ex.get("negative") or []:
            lst = _example_listing(item, p)
            r = evaluate(lst, p.criteria)
            assert not r.matched, (pid, lst.title, r.reasons)


def test_negations_of_bans_are_not_excluded(profiles):
    """«без бана», «no ban», «VAC: нет» — хорошие объявления, стоп-слова их не трогают."""
    good = {
        "wot_ru_tops": "WoT Lesta все топы, Chieftain, без бана, обмен доступен",
        "cs2_prime": "CS2 Prime, нож, no VAC ban, trade ban: нет, без холда",
        "dota2_high_mmr": "Dota 2 7000 mmr immortal, VAC: нет, банов нет",
        "steam_inventory": "Steam инвентарь на 20 000 руб, нож, без VAC, no trade ban",
        "fortnite_skins": "Fortnite Renegade Raider, без бана, читайте описание",
        "roblox": "Roblox Headless, 5000 robux, без пина, no ban",
    }
    for pid, text in good.items():
        r = evaluate(_listing(text, 10000, region=profiles[pid].region), profiles[pid].criteria)
        assert r.matched, (pid, r.rejections)


def test_examples_are_ignored_by_model(raw_profiles):
    """Ключ examples — для тестов; модель Profile его молча пропускает."""
    for raw in raw_profiles.values():
        p = Profile.model_validate(raw)
        assert not hasattr(p, "examples")


# ----------------------------------------------------------------- settings --


def test_settings_example_loads():
    data = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8"))
    s = Settings.model_validate(data)
    assert s.funpay.golden_key == "" and s.lolz.token == ""
    assert s.funpay.auto_publish is False
    assert s.funpay.request_delay >= 1.0 and s.lolz.request_delay >= 3.0
    assert s.monitor.interval_minutes > 0
    assert s.ui.host == "127.0.0.1" and s.ui.port == 8787
    # все ключи примера существуют в модели (нет опечаток)
    dumped = Settings().model_dump()
    for section, values in data.items():
        assert section in dumped, section
        if isinstance(values, dict):
            for key in values:
                assert key in dumped[section], f"{section}.{key}"


def test_settings_example_has_comments_for_secrets():
    text = SETTINGS_EXAMPLE.read_text(encoding="utf-8")
    assert "golden_key" in text and "DevTools" in text
    assert "lolz.team/account/api" in text


# ----------------------------------------------------------------- матчинг --


def test_wot_ru_matches_realistic_listing(profiles):
    p = profiles["wot_ru_tops"]
    r = evaluate(_listing("Аккаунт Мир танков Lesta: все топы, Chieftain, Об. 279 (р), E 100, 20к голды",
                          15000, region="RU"), p.criteria)
    assert r.matched, r.rejections
    assert "Chieftain" in r.highlights and "Об. 279 (р)" in r.highlights and "E 100" in r.highlights
    lot = render_lot(_listing("x", 15000, region="RU"), r, p, calculate_price(15000, p.pricing))
    assert lot["price"] == 29000.0
    assert "Chieftain" in lot["title_ru"] and len(lot["title_ru"]) <= 100
    assert "{" not in lot["description_ru"]


def test_wot_ru_rejects_bad_listings(profiles):
    p = profiles["wot_ru_tops"]
    assert not evaluate(_listing("Акк WoT все топы, есть бан", 15000, region="RU"), p.criteria).matched
    assert not evaluate(_listing("Акк WoT все топы", 15000, region="EU"), p.criteria).matched
    assert not evaluate(_listing("Акк WoT все топы", 100000, region="RU"), p.criteria).matched
    assert not evaluate(_listing("Акк WoT Blitz все топы", 10000, region="RU"), p.criteria).matched
    assert not evaluate(_listing("Акк WoT 5 танков 8 уровня", 10000, region="RU"), p.criteria).matched


def test_dota_profile_requires_numeric_mmr(profiles):
    p = profiles["dota2_high_mmr"]
    assert evaluate(_listing("Dota 2, 7200 MMR, Immortal, аркана", 12000), p.criteria).matched
    assert evaluate(_listing("Dota 2, ммр: 6500, divine", 12000), p.criteria).matched
    assert not evaluate(_listing("Dota 2, Immortal, аркана", 12000), p.criteria).matched
    assert not evaluate(_listing("Dota 2, 7200 MMR, VAC", 12000), p.criteria).matched
    assert not evaluate(_listing("Dota 2, 4800 MMR, Ancient", 12000), p.criteria).matched


def test_cs2_profile(profiles):
    p = profiles["cs2_prime"]
    r = evaluate(_listing("CS2 Prime, FACEIT lvl 10, нож Karambit, перчатки", 8000), p.criteria)
    assert r.matched and "Prime" in r.highlights and "Karambit" in r.highlights and "FACEIT 10" in r.highlights
    assert not evaluate(_listing("CS2 Prime, VAC ban", 8000), p.criteria).matched
    assert not evaluate(_listing("CS2 без прайма, 200 часов", 8000), p.criteria).matched


def test_brawl_stars_trophies_regex(profiles):
    p = profiles["brawl_stars"]
    assert evaluate(_listing("Brawl Stars 35к кубков, Спайк", 3000), p.criteria).matched
    assert evaluate(_listing("Brawl Stars 12 000 трофеев, все легендарки", 3000), p.criteria).matched
    assert evaluate(_listing("Brawl Stars account 50k trophies, Leon", 3000), p.criteria).matched
    assert not evaluate(_listing("Brawl Stars 5000 кубков, Спайк", 3000), p.criteria).matched


def test_other_profiles_match_samples(profiles):
    samples = {
        "wot_eu_tops": ("WoT EU account, all tier 10, Chieftain, Kranvagn", "EU", 12000),
        "wot_na_tops": ("WoT NA account, all tops, Object 279", "NA", 12000),
        "wot_asia_tops": ("WoT ASIA, все десятки, Об. 260", "ASIA", 12000),
        "wot_blitz": ("WoT Blitz: все топы, Е 100, легендарный камуфляж", None, 5000),
        "fortnite_skins": ("Fortnite OG: Renegade Raider, Black Knight, 150 скинов", None, 20000),
        "genshin": ("Genshin AR 60, 20 легендарных 5★: Райден C6, Ху Тао", None, 9000),
        "valorant": ("Valorant Immortal, 40 скинов: Reaver, Prime, Elderflame", None, 7000),
        "pubg": ("PUBG Steam с 2017, Pajama, 3000 часов", None, 7000),
        "apex": ("Apex Legends Heirloom Wraith, Predator, 500 lvl", None, 7000),
        "clash_royale": ("Clash Royale 15 кт, 8000 кубков, все легендарки", None, 3000),
        "standoff2": ("Standoff 2 20к голды, нож бабочка, Легенда", None, 3000),
        "roblox": ("Roblox 5000 robux, Headless, Korblox", None, 3000),
        "eft": ("Tarkov EOD 50 lvl, Каппа, макс торговцы", None, 7000),
        "warface": ("Warface 85 ранг, вечное золотое оружие, донат", None, 3000),
        "lol": ("LoL все чемпионы, Victorious, 200 скинов, Diamond", None, 7000),
        "steam_inventory": ("Steam инвентарь на 30 000 руб, керамбит, 100 lvl", None, 20000),
        "any_game_cheap_tops": ("Все топы, Chieftain, Об. 279 (р), родная почта", None, 3000),
    }
    for pid, (title, region, price) in samples.items():
        r = evaluate(_listing(title, price, region=region), profiles[pid].criteria)
        assert r.matched, (pid, r.rejections)
        assert r.highlights, pid


def test_profiles_roundtrip_through_store(tmp_path, profiles):
    store = ProfileStore(tmp_path)
    for p in profiles.values():
        store.save(p)
        again = store.get(p.id)
        assert again == p
