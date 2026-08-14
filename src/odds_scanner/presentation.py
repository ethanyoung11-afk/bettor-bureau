from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def decimal_to_american(decimal_odds: Decimal) -> int:
    if decimal_odds <= Decimal("1"):
        raise ValueError("decimal_odds must be greater than 1")
    if decimal_odds >= Decimal("2"):
        value = (decimal_odds - Decimal("1")) * Decimal("100")
    else:
        value = -Decimal("100") / (decimal_odds - Decimal("1"))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_odds(decimal_odds: Decimal, odds_format: str) -> str:
    if odds_format == "American":
        american = decimal_to_american(decimal_odds)
        return f"+{american}" if american > 0 else str(american)
    return f"{decimal_odds:.3f}".rstrip("0").rstrip(".")
