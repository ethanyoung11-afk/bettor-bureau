from __future__ import annotations

from decimal import Decimal

from odds_scanner.domain import MarketKey, MarketKind, OutcomeSide
from odds_scanner.normalization import FOOTBALL_MARKETS, MarketNormalizer


def test_spread_matching_uses_home_referenced_line(event):
    normalizer = MarketNormalizer()
    home = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["spreads"], "Home Team", -3)
    away = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["spreads"], "Away Team", 3)
    assert home.market == away.market
    assert home.side is OutcomeSide.HOME
    assert away.side is OutcomeSide.AWAY
    assert home.market.line == Decimal("-3")


def test_spread_mismatch_is_a_different_contract(event):
    normalizer = MarketNormalizer()
    home = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["spreads"], "Home Team", -3)
    away = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["spreads"], "Away Team", 3.5)
    assert home.market != away.market


def test_total_matching_requires_exact_line(event):
    normalizer = MarketNormalizer()
    over = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["totals"], "Over", 45.5)
    under = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["totals"], "Under", 45.5)
    assert over.market == under.market
    assert over.market.kind is MarketKind.TOTAL


def test_total_mismatch_is_a_different_contract(event):
    normalizer = MarketNormalizer()
    over = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["totals"], "Over", 45.5)
    under = normalizer.normalize_outcome(event, FOOTBALL_MARKETS["totals"], "Under", 46.5)
    assert over.market != under.market


def test_equivalent_decimal_and_side_order_have_same_market_identity():
    first = MarketKey(
        event_id="event-1",
        kind=MarketKind.SPREAD,
        required_sides=(OutcomeSide.AWAY, OutcomeSide.HOME),
        line=Decimal("3.0"),
    )
    second = MarketKey(
        event_id="event-1",
        kind=MarketKind.SPREAD,
        required_sides=(OutcomeSide.HOME, OutcomeSide.AWAY),
        line=Decimal("3"),
    )
    assert first == second
    assert first.id == second.id
