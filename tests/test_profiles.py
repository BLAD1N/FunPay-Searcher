"""Тесты стартовых профилей (config/profiles/*.yaml) и примера настроек."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.matching import _compile_term, evaluate
from app.models import Listing, Profile
from app.pricing import calculate_price
from app.profiles import ProfileStore
from app.settings import Settings
from app.templating import render_lot

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "config" / "profiles"
SETTINGS_EXAMPLE = ROOT / "config" / "settings.example.yaml"

EXPECTED_IDS = {
    "wot_ru_tops", "wot_eu_tops", "wot_na_tops", "wot_asia_tops", "wot_blitz",
    "dota2_high_mmr", "cs2_prime", "fortnite_skins", "genshin", "valorant",
}
ENABLED_BY_DEFAULT = {"wot_ru_tops", "dota2_high_mmr", "cs2_prime"}


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    store = ProfileStore(PROFILES_DIR)
    loaded = store.list()
    return {p.id: p for p in loaded}


def test_every_yaml_loads(profiles):
    files = {p.stem for p in PROFILES_DIR.glob("*.yaml")}
    assert files == EXPECTED_IDS
    assert set(profiles) == files  # ни один файл не отброшен как «битый»


def test_ids_match_file_names(profiles):
    for pid, p in profiles.items():
        assert p.id == pid
        raw = yaml.safe_load((PROFILES_DIR / f"{pid}.yaml").read_text(encoding="utf-8"))
        assert raw.get("id") == pid


def test_enabled_defaults(profiles):
    assert {pid for pid, p in profiles.items() if p.enabled} == ENABLED_BY_DEFAULT


def test_profiles_have_sane_fields(profiles):
    for p in profiles.values():
        assert p.name and p.game
        assert p.criteria.must_any, p.id
        assert p.criteria.price.min is not None and p.criteria.price.max is not None
        assert 0 < p.criteria.price.min < p.criteria.price.max
        fp, lz = p.funpay(), p.lolz()
        assert fp.enabled and (fp.game_query or fp.subcategory_id or fp.subcategory_ids), p.id
        assert lz.enabled and lz.category, p.id
        assert p.lot_template.amount == 1


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
    assert profiles["wot_ru_tops"].lolz().category == "world-of-tanks"


def test_all_terms_and_regexes_compile(profiles):
    for p in profiles.values():
        c = p.criteria
        for term in c.must_any + c.must_all + c.exclude + c.highlights:
            assert _compile_term(term), f"{p.id}: пустое условие {term!r}"
        for rx in c.regex_any + c.regex_exclude:
            re.compile(rx, re.I)


def test_pricing_gives_profit(profiles):
    for p in profiles.values():
        for price in (500, 1500, 3000, 15000, 42350, 60000, 80000):
            ours = calculate_price(price, p.pricing)
            assert ours > price, (p.id, price, ours)
            assert ours - price >= p.pricing.min_margin - p.pricing.round_to, (p.id, price, ours)
            assert ours % p.pricing.round_to == 0


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


def _listing(title: str, price: float, region=None, **attrs) -> Listing:
    return Listing(source="funpay", source_id="1", url="https://funpay.com/lots/offer?id=1",
                   title=title, price=price, region=region, attributes=attrs)


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


def test_cs2_profile(profiles):
    p = profiles["cs2_prime"]
    r = evaluate(_listing("CS2 Prime, FACEIT lvl 10, нож Karambit, перчатки", 8000), p.criteria)
    assert r.matched and "Prime" in r.highlights and "Knife" in r.highlights
    assert not evaluate(_listing("CS2 Prime, VAC ban", 8000), p.criteria).matched
    assert not evaluate(_listing("CS2 без прайма, 200 часов", 8000), p.criteria).matched


def test_other_profiles_match_samples(profiles):
    samples = {
        "wot_eu_tops": ("WoT EU account, all tier 10, Chieftain, Kranvagn", "EU", 12000),
        "wot_na_tops": ("WoT NA account, all tops, Object 279", "NA", 12000),
        "wot_asia_tops": ("WoT ASIA, все десятки, Об. 260", "ASIA", 12000),
        "wot_blitz": ("WoT Blitz: все топы, Е 100, легендарный камуфляж", None, 5000),
        "fortnite_skins": ("Fortnite OG: Renegade Raider, Black Knight, 150 скинов", None, 20000),
        "genshin": ("Genshin AR 60, 20 легендарных 5★: Райден C6, Ху Тао", None, 9000),
        "valorant": ("Valorant Immortal, 40 скинов: Reaver, Prime, Elderflame", None, 7000),
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
