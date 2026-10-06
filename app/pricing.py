"""Расчёт цены нашего лота по цене исходного объявления."""

from __future__ import annotations

import ast
import math
import operator

from .models import PricingRule

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs, "floor": math.floor, "ceil": math.ceil}


def safe_eval(expr: str, variables: dict[str, float]) -> float:
    """Безопасное вычисление арифметического выражения (без exec/eval)."""
    tree = ast.parse(expr, mode="eval")

    def _ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in variables:
                return float(variables[node.id])
            raise ValueError(f"Неизвестная переменная: {node.id}")
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return float(_FUNCS[node.func.id](*[_ev(a) for a in node.args]))
        if isinstance(node, ast.IfExp):
            return _ev(node.body) if _ev(node.test) else _ev(node.orelse)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            lhs, rhs = _ev(node.left), _ev(node.comparators[0])
            op = node.ops[0]
            return {
                ast.Lt: lhs < rhs,
                ast.LtE: lhs <= rhs,
                ast.Gt: lhs > rhs,
                ast.GtE: lhs >= rhs,
                ast.Eq: lhs == rhs,
                ast.NotEq: lhs != rhs,
            }[type(op)]
        raise ValueError(f"Недопустимая конструкция в формуле: {ast.dump(node)}")

    return float(_ev(tree))


def _round(value: float, rule: PricingRule) -> float:
    if rule.round_to and rule.round_to > 0:
        q = value / rule.round_to
        if rule.round_mode == "down":
            q = math.floor(q)
        elif rule.round_mode == "up":
            q = math.ceil(q)
        else:
            q = round(q)
        value = q * rule.round_to
    if rule.price_ending is not None:
        base = math.floor(value / 1000) * 1000
        value = base + rule.price_ending
    return float(value)


def calculate_price(source_price: float, rule: PricingRule) -> float:
    """Главная функция: цена исходника -> цена нашего лота."""
    p = float(source_price)
    if rule.mode == "percent":
        value = p * (1 + rule.percent / 100.0)
    elif rule.mode == "multiplier":
        value = p * rule.multiplier
    elif rule.mode == "formula":
        value = safe_eval(rule.formula, {"price": p})
    elif rule.mode == "tiers":
        pct = rule.percent
        for tier in sorted(rule.tiers, key=lambda t: t.get("up_to", float("inf"))):
            if p <= tier.get("up_to", float("inf")):
                pct = tier.get("percent", pct)
                break
        value = p * (1 + pct / 100.0)
    else:
        raise ValueError(f"Неизвестный режим наценки: {rule.mode}")

    if rule.min_margin and value - p < rule.min_margin:
        value = p + rule.min_margin
    value = _round(value, rule)
    if value <= p:  # никогда не продаём дешевле закупки
        value = _round(p + max(rule.min_margin, rule.round_to or 1), rule)
        step = 1000.0 if rule.price_ending is not None else float(max(rule.round_to or 0, 1))
        while value <= p:
            value += step
    return max(value, 1.0)
