from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

from odds_scanner.domain import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    MarketKey,
    OutcomeKey,
    OutcomeSide,
    Quote,
)


def implied_probability(decimal_odds: Decimal) -> Decimal:
    if decimal_odds <= Decimal("1"):
        raise ValueError("decimal_odds must be greater than 1")
    return Decimal("1") / decimal_odds


def is_fresh(quote: Quote, *, as_of: datetime, max_age: timedelta) -> bool:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if max_age < timedelta(0):
        raise ValueError("max_age cannot be negative")
    return quote.source_updated_at >= as_of - max_age


def deduplicate_quotes(quotes: Iterable[Quote]) -> tuple[Quote, ...]:
    """Keep one quote per provider/book/outcome.

    Source update time wins, followed by observation time. Equal snapshots prefer the higher price
    to make the behavior deterministic without allowing an older price to replace a newer one.
    """
    selected: dict[tuple[str, str, str], Quote] = {}
    for quote in quotes:
        key = (quote.provider_id, quote.sportsbook.id, quote.outcome.id)
        incumbent = selected.get(key)
        candidate_rank = (
            quote.source_updated_at,
            quote.observed_at,
            quote.decimal_odds,
        )
        if incumbent is None or candidate_rank > (
            incumbent.source_updated_at,
            incumbent.observed_at,
            incumbent.decimal_odds,
        ):
            selected[key] = quote
    return tuple(selected.values())


def best_prices(
    quotes: Iterable[Quote],
    *,
    as_of: datetime | None = None,
    max_age: timedelta | None = None,
) -> Mapping[OutcomeKey, Quote]:
    if (as_of is None) != (max_age is None):
        raise ValueError("as_of and max_age must be supplied together")
    candidates = deduplicate_quotes(quotes)
    if as_of is not None and max_age is not None:
        candidates = tuple(q for q in candidates if is_fresh(q, as_of=as_of, max_age=max_age))

    selected: dict[OutcomeKey, Quote] = {}
    for quote in candidates:
        incumbent = selected.get(quote.outcome)
        rank = (quote.decimal_odds, quote.source_updated_at, quote.observed_at)
        if incumbent is None or rank > (
            incumbent.decimal_odds,
            incumbent.source_updated_at,
            incumbent.observed_at,
        ):
            selected[quote.outcome] = quote
    return selected


def allocate_equal_payout(
    prices: Mapping[OutcomeSide, Decimal],
    bankroll: Decimal,
) -> Mapping[OutcomeSide, Decimal]:
    if bankroll <= 0:
        raise ValueError("bankroll must be positive")
    if len(prices) not in (2, 3):
        raise ValueError("Stake sizing supports two-way and three-way markets")
    with localcontext() as context:
        context.prec = 28
        inverse_sum = sum((implied_probability(price) for price in prices.values()), Decimal("0"))
        return {
            side: bankroll * implied_probability(price) / inverse_sum
            for side, price in prices.items()
        }


def detect_arbitrage(
    quotes: Iterable[Quote],
    *,
    bankroll: Decimal = Decimal("100"),
    as_of: datetime | None = None,
    max_age: timedelta | None = None,
) -> tuple[ArbitrageOpportunity, ...]:
    detected_at = as_of or datetime.now(UTC)
    best = best_prices(quotes, as_of=as_of, max_age=max_age)
    by_market: dict[MarketKey, dict[OutcomeSide, Quote]] = defaultdict(dict)
    for outcome, quote in best.items():
        by_market[outcome.market][outcome.side] = quote

    opportunities: list[ArbitrageOpportunity] = []
    for market, prices_by_side in by_market.items():
        required = market.required_sides
        if any(side not in prices_by_side for side in required):
            continue
        selected = {side: prices_by_side[side] for side in required}
        total_probability = sum(
            (implied_probability(quote.decimal_odds) for quote in selected.values()),
            Decimal("0"),
        )
        if total_probability >= Decimal("1"):
            continue

        prices = {side: quote.decimal_odds for side, quote in selected.items()}
        stakes = allocate_equal_payout(prices, bankroll)
        roi = Decimal("1") / total_probability - Decimal("1")
        legs = tuple(
            ArbitrageLeg(
                outcome=quote.outcome,
                quote=quote,
                stake=stakes[side],
                gross_payout=stakes[side] * quote.decimal_odds,
            )
            for side, quote in selected.items()
        )
        opportunities.append(
            ArbitrageOpportunity(
                market=market,
                legs=legs,
                total_implied_probability=total_probability,
                roi=roi,
                bankroll=bankroll,
                detected_at=detected_at,
            )
        )
    return tuple(sorted(opportunities, key=lambda item: item.roi, reverse=True))
