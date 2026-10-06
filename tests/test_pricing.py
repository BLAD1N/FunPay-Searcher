import pytest

from app.models import PricingRule
from app.pricing import calculate_price, safe_eval


def test_formula_from_spec():
    rule = PricingRule(mode="formula", formula="price * 2 - 1000", round_to=100)
    assert calculate_price(15000, rule) == 29000
    assert calculate_price(30000, rule) == 59000


def test_percent_and_rounding():
    assert calculate_price(10000, PricingRule(mode="percent", percent=50, round_to=0)) == 15000
    assert calculate_price(10000, PricingRule(mode="percent", percent=33, round_to=1000, round_mode="up")) == 14000
    assert calculate_price(10000, PricingRule(mode="percent", percent=33, round_to=1000, round_mode="down")) == 13000
    assert calculate_price(10000, PricingRule(mode="percent", percent=95, round_to=1000, round_mode="down", price_ending=990)) == 19990


def test_multiplier_tiers_margin():
    assert calculate_price(1000, PricingRule(mode="multiplier", multiplier=1.5, round_to=0)) == 1500
    tiers = PricingRule(mode="tiers", tiers=[{"up_to": 10000, "percent": 100}, {"up_to": 50000, "percent": 80}],
                        percent=50, round_to=0)
    assert calculate_price(5000, tiers) == 10000
    assert calculate_price(20000, tiers) == 36000
    assert calculate_price(100000, tiers) == 150000
    assert calculate_price(1000, PricingRule(mode="percent", percent=1, min_margin=500, round_to=0)) == 1500


def test_never_below_source():
    assert calculate_price(1000, PricingRule(mode="formula", formula="price - 500", round_to=100)) > 1000


def test_safe_eval_rejects_code():
    with pytest.raises(Exception):
        safe_eval("__import__('os').system('x')", {"price": 1})
    with pytest.raises(Exception):
        safe_eval("price.__class__", {"price": 1})
    assert safe_eval("max(price * 1.5, price + 3000)", {"price": 1000}) == 4000
    assert safe_eval("price * 2 if price < 10000 else price * 1.5", {"price": 20000}) == 30000


def test_price_ending_never_below_cost():
    rule = PricingRule(mode="percent", percent=3, round_to=100, price_ending=0)
    assert calculate_price(10500, rule) > 10500
    rule = PricingRule(mode="percent", percent=1, round_to=100, price_ending=990)
    v = calculate_price(14995, rule)
    assert v > 14995 and int(v) % 1000 == 990
