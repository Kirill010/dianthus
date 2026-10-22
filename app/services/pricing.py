# app/services/pricing.py
"""Деньги: округление до копеек без ошибок float.

В модели цены лежат во float. Само по себе это допустимо, пока каждый
итог округляется до копейки при расчёте: иначе в заказ попадает
2499.7500000000005 и в 1С/счёт уходят «хвосты».
"""
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")


def _d(value) -> Decimal:
    # через str: Decimal(0.1) тащит двоичную погрешность, Decimal("0.1") — нет
    return Decimal(str(value if value is not None else 0))


def money(value) -> float:
    """Округление до копеек (математическое: 0.005 → 0.01)."""
    return float(_d(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def line_total(price, pack, quantity) -> float:
    """Сумма позиции: цена за штуку × штук в упаковке × число упаковок."""
    return money(_d(price) * _d(pack or 1) * _d(quantity))


def clamp_percent(value) -> float:
    try:
        pct = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, pct))


def apply_discount(subtotal, percent) -> tuple[float, float]:
    """(сумма скидки, итог к оплате) — обе величины округлены до копеек."""
    sub = _d(subtotal)
    pct = _d(clamp_percent(percent))
    if pct <= 0:
        return 0.0, money(sub)
    discount = (sub * pct / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return float(discount), float((sub - discount).quantize(_CENT, rounding=ROUND_HALF_UP))
