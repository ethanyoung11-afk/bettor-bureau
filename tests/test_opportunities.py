from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from conftest import make_market, make_quote

from odds_scanner.domain import OutcomeSide
from odds_scanner.opportunities import best_prices, detect_arbitrage, implied_probability


def test_two_way_arbitrage(now):
    market = make_market()
    quotes = [
        make_quote(market, OutcomeSide.HOME, "2.10", now, book="alpha"),
        make_quote(market, OutcomeSide.AWAY, "2.10", now, book="beta"),
    ]
    result = detect_arbitrage(quotes, as_of=now, max_age=timedelta(minutes=5))
    assert len(result) == 1
    assert result[0].total_implied_probability == Decimal("2") / Decimal("2.10")
    assert result[0].roi == Decimal("0.05")


def test_no_arbitrage_at_minus_110_equivalent(now):
    market = make_market()
    quotes = [
        make_quote(market, OutcomeSide.HOME, "1.91", now, book="alpha"),
        make_quote(market, OutcomeSide.AWAY, "1.91", now, book="beta"),
    ]
    assert detect_arbitrage(quotes) == ()


def test_three_way_market_arbitrage(now):
    market = make_market(
        sides=(OutcomeSide.HOME, OutcomeSide.DRAW, OutcomeSide.AWAY),
    )
    quotes = [
        make_quote(market, OutcomeSide.HOME, "3.40", now, book="alpha"),
        make_quote(market, OutcomeSide.DRAW, "3.50", now, book="beta"),
        make_quote(market, OutcomeSide.AWAY, "3.60", now, book="gamma"),
    ]
    result = detect_arbitrage(quotes)
    assert len(result) == 1
    assert len(result[0].legs) == 3
    assert result[0].total_implied_probability < 1


def test_stale_quote_is_rejected(now):
    market = make_market()
    quotes = [
        make_quote(market, OutcomeSide.HOME, "2.20", now, book="alpha", age_seconds=301),
        make_quote(market, OutcomeSide.AWAY, "2.20", now, book="beta"),
    ]
    assert detect_arbitrage(quotes, as_of=now, max_age=timedelta(minutes=5)) == ()


def test_best_price_across_multiple_sportsbooks(now):
    market = make_market()
    quotes = [
        make_quote(market, OutcomeSide.HOME, "1.95", now, book="alpha"),
        make_quote(market, OutcomeSide.HOME, "2.05", now, book="beta"),
        make_quote(market, OutcomeSide.AWAY, "2.00", now, book="alpha"),
        make_quote(market, OutcomeSide.AWAY, "2.02", now, book="gamma"),
    ]
    selected = best_prices(quotes)
    home = selected[next(key for key in selected if key.side is OutcomeSide.HOME)]
    away = selected[next(key for key in selected if key.side is OutcomeSide.AWAY)]
    assert home.sportsbook.id == "beta"
    assert away.sportsbook.id == "gamma"


def test_newest_duplicate_quote_wins_even_when_price_is_lower(now):
    market = make_market()
    quotes = [
        make_quote(
            market,
            OutcomeSide.HOME,
            "2.30",
            now,
            book="alpha",
            age_seconds=30,
        ),
        make_quote(
            market,
            OutcomeSide.HOME,
            "2.00",
            now,
            book="alpha",
            age_seconds=10,
            observed_offset_seconds=1,
        ),
    ]
    selected = best_prices(quotes)
    assert next(iter(selected.values())).decimal_odds == Decimal("2.00")


def test_stake_sizing_equalizes_gross_payout(now):
    market = make_market()
    quotes = [
        make_quote(market, OutcomeSide.HOME, "2.20", now, book="alpha"),
        make_quote(market, OutcomeSide.AWAY, "2.05", now, book="beta"),
    ]
    opportunity = detect_arbitrage(quotes, bankroll=Decimal("250"))[0]
    payouts = [leg.gross_payout for leg in opportunity.legs]
    assert max(payouts) - min(payouts) < Decimal("0.0000001")
    assert sum(leg.stake for leg in opportunity.legs) == Decimal("250")


def test_incomplete_three_way_market_is_not_an_arbitrage(now):
    market = make_market(sides=(OutcomeSide.HOME, OutcomeSide.DRAW, OutcomeSide.AWAY))
    quotes = [
        make_quote(market, OutcomeSide.HOME, "2.20", now, book="alpha"),
        make_quote(market, OutcomeSide.AWAY, "2.20", now, book="beta"),
    ]
    assert detect_arbitrage(quotes) == ()


def test_implied_probability_rejects_invalid_decimal_odds():
    try:
        implied_probability(Decimal("1"))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected invalid odds to be rejected")
