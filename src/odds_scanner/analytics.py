from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from odds_scanner.domain import Event, MarketKind, OutcomeSide, Quote
from odds_scanner.opportunities import deduplicate_quotes, implied_probability, is_fresh


@dataclass(frozen=True, slots=True)
class MiddleOpportunity:
    event_id: str
    kind: MarketKind
    first: Quote
    second: Quote
    lower_line: Decimal
    upper_line: Decimal
    width: Decimal

    @property
    def combined_implied_probability(self) -> Decimal:
        return implied_probability(self.first.decimal_odds) + implied_probability(
            self.second.decimal_odds
        )

    @property
    def label(self) -> str:
        if self.kind is MarketKind.TOTAL:
            return f"Over {self.lower_line} / Under {self.upper_line}"
        return f"Home {self.upper_line:+} / Away {-self.lower_line:+}"


@dataclass(frozen=True, slots=True)
class ValueOpportunity:
    quote: Quote
    fair_probability: Decimal
    expected_value: Decimal
    reference_books: int
    reference_sportsbooks: tuple[str, ...]

    @property
    def fair_odds(self) -> Decimal:
        return Decimal("1") / self.fair_probability


@dataclass(frozen=True, slots=True)
class Recommendation:
    opportunity_type: str
    event_id: str
    market: str
    selection: str
    sportsbooks: str
    prices: str
    edge: Decimal
    rationale: str
    risk: str
    priority: int


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    event_id: str
    event_name: str
    kickoff: datetime
    check_at: datetime
    window: str


def plan_refreshes(
    events: Iterable[Event],
    *,
    as_of: datetime,
    limit: int = 5,
) -> tuple[RefreshPlan, ...]:
    """Choose the next useful manual scan window for each upcoming event."""
    plans: list[RefreshPlan] = []
    for event in events:
        time_to_kickoff = event.start_time - as_of
        if time_to_kickoff <= timedelta(0):
            continue
        if time_to_kickoff > timedelta(hours=24):
            check_at = event.start_time - timedelta(hours=24)
            window = "24 hours before kickoff"
        elif time_to_kickoff > timedelta(minutes=90):
            check_at = event.start_time - timedelta(minutes=90)
            window = "90 minutes before kickoff"
        elif time_to_kickoff > timedelta(minutes=30):
            check_at = event.start_time - timedelta(minutes=30)
            window = "30 minutes before kickoff"
        elif time_to_kickoff > timedelta(minutes=10):
            check_at = event.start_time - timedelta(minutes=10)
            window = "10 minutes before kickoff"
        else:
            check_at = as_of
            window = "Refresh now"
        plans.append(
            RefreshPlan(
                event_id=event.id,
                event_name=event.name,
                kickoff=event.start_time,
                check_at=check_at,
                window=window,
            )
        )
    plans.sort(key=lambda item: (item.check_at, item.kickoff, item.event_name))
    return tuple(plans[:limit])


def rank_recommendations(
    arbitrage: Iterable[object],
    middles: Iterable[MiddleOpportunity],
    values: Iterable[ValueOpportunity],
    *,
    limit: int = 6,
    priority_sportsbooks: Iterable[str] = (),
) -> tuple[Recommendation, ...]:
    from odds_scanner.domain import ArbitrageOpportunity

    recommendations: list[Recommendation] = []
    for candidate in arbitrage:
        if not isinstance(candidate, ArbitrageOpportunity):
            continue
        recommendations.append(
            Recommendation(
                opportunity_type="PURE ARB",
                event_id=candidate.market.event_id,
                market=candidate.market.kind.value.replace("_", " ").title(),
                selection=" + ".join(leg.outcome.side.value.title() for leg in candidate.legs),
                sportsbooks=" + ".join(leg.quote.sportsbook.name for leg in candidate.legs),
                prices=" / ".join(str(leg.quote.decimal_odds) for leg in candidate.legs),
                edge=candidate.roi,
                rationale=(
                    f"Locks approximately {candidate.roi:.2%} ROI when every leg is accepted."
                ),
                risk="Prices can move or a sportsbook can reject a leg.",
                priority=3,
            )
        )
    for candidate in values:
        quote = candidate.quote
        recommendations.append(
            Recommendation(
                opportunity_type="CONSENSUS +EV",
                event_id=quote.outcome.market.event_id,
                market=quote.outcome.market.kind.value.replace("_", " ").title(),
                selection=quote.outcome.side.value.title(),
                sportsbooks=quote.sportsbook.name,
                prices=str(quote.decimal_odds),
                edge=candidate.expected_value,
                rationale=(
                    f"Offered price is {candidate.expected_value:.2%} above the no-vig "
                    f"consensus estimate from {candidate.reference_books} books."
                ),
                risk="Estimated value, not guaranteed profit.",
                priority=2,
            )
        )
    for candidate in middles:
        recommendations.append(
            Recommendation(
                opportunity_type="MIDDLE",
                event_id=candidate.event_id,
                market=candidate.kind.value.title(),
                selection=candidate.label,
                sportsbooks=(
                    f"{candidate.first.sportsbook.name} + {candidate.second.sportsbook.name}"
                ),
                prices=f"{candidate.first.decimal_odds} / {candidate.second.decimal_odds}",
                edge=candidate.width / Decimal("100"),
                rationale=f"Creates a {candidate.width:g}-point window where both legs can win.",
                risk="Probabilistic middle; either or both legs can lose net money.",
                priority=1,
            )
        )
    priority_names = tuple(name.casefold() for name in priority_sportsbooks)
    recommendations.sort(
        key=lambda item: (
            any(name in item.sportsbooks.casefold() for name in priority_names),
            item.priority,
            item.edge,
        ),
        reverse=True,
    )
    return tuple(recommendations[:limit])


def detect_middles(
    quotes: Iterable[Quote],
    *,
    as_of: datetime,
    max_age: timedelta,
    minimum_price: Decimal = Decimal("1.70"),
) -> tuple[MiddleOpportunity, ...]:
    candidates = tuple(
        quote
        for quote in deduplicate_quotes(quotes)
        if quote.decimal_odds >= minimum_price and is_fresh(quote, as_of=as_of, max_age=max_age)
    )
    groups: dict[tuple[str, MarketKind, str], list[Quote]] = defaultdict(list)
    for quote in candidates:
        market = quote.outcome.market
        if market.kind in {MarketKind.SPREAD, MarketKind.TOTAL}:
            groups[(market.event_id, market.kind, market.period)].append(quote)

    results: list[MiddleOpportunity] = []
    for (event_id, kind, _period), group in groups.items():
        if kind is MarketKind.TOTAL:
            first_side, second_side = OutcomeSide.OVER, OutcomeSide.UNDER
        else:
            first_side, second_side = OutcomeSide.AWAY, OutcomeSide.HOME

        first_quotes = [quote for quote in group if quote.outcome.side is first_side]
        second_quotes = [quote for quote in group if quote.outcome.side is second_side]
        pairs: list[MiddleOpportunity] = []
        for first in first_quotes:
            for second in second_quotes:
                if first.sportsbook.id == second.sportsbook.id:
                    continue
                first_line = first.outcome.market.line
                second_line = second.outcome.market.line
                if first_line is None or second_line is None:
                    continue
                lower, upper = sorted((first_line, second_line))
                if kind is MarketKind.TOTAL:
                    valid = first_line < second_line
                    width = second_line - first_line
                else:
                    # Spread lines are canonical home handicaps. Away at the lower home line and
                    # home at the higher home line overlap when the latter is numerically larger.
                    valid = first_line < second_line
                    width = second_line - first_line
                if valid:
                    pairs.append(
                        MiddleOpportunity(
                            event_id=event_id,
                            kind=kind,
                            first=first,
                            second=second,
                            lower_line=lower,
                            upper_line=upper,
                            width=width,
                        )
                    )
        if pairs:
            results.append(
                max(
                    pairs,
                    key=lambda item: (
                        item.width,
                        item.first.decimal_odds + item.second.decimal_odds,
                    ),
                )
            )
    return tuple(sorted(results, key=lambda item: item.width, reverse=True))


def detect_consensus_value(
    quotes: Iterable[Quote],
    *,
    as_of: datetime,
    max_age: timedelta,
    minimum_ev: Decimal = Decimal("0.01"),
    candidate_sportsbooks: Iterable[str] | None = None,
    include_stale: bool = False,
) -> tuple[ValueOpportunity, ...]:
    eligible = tuple(
        quote
        for quote in deduplicate_quotes(quotes)
        if include_stale or is_fresh(quote, as_of=as_of, max_age=max_age)
    )
    markets: dict[str, list[Quote]] = defaultdict(list)
    for quote in eligible:
        markets[quote.outcome.market.id].append(quote)

    candidate_names = (
        None
        if candidate_sportsbooks is None
        else {name.casefold() for name in candidate_sportsbooks}
    )
    results: list[ValueOpportunity] = []
    for group in markets.values():
        by_book: dict[str, dict[OutcomeSide, Quote]] = defaultdict(dict)
        for quote in group:
            by_book[quote.sportsbook.id][quote.outcome.side] = quote
        required_sides = group[0].outcome.market.required_sides
        complete_books = {
            book_id: side_quotes
            for book_id, side_quotes in by_book.items()
            if all(side in side_quotes for side in required_sides)
        }
        if len(complete_books) < 2:
            continue

        fair_by_book: dict[str, dict[OutcomeSide, Decimal]] = {}
        for book_id, side_quotes in complete_books.items():
            raw = {
                side: implied_probability(side_quotes[side].decimal_odds) for side in required_sides
            }
            overround = sum(raw.values(), Decimal("0"))
            fair_by_book[book_id] = {
                side: probability / overround for side, probability in raw.items()
            }

        for candidate in group:
            if (
                candidate_names is not None
                and candidate.sportsbook.name.casefold() not in candidate_names
            ):
                continue
            consensus = [
                (book_id, fair[candidate.outcome.side])
                for book_id, fair in fair_by_book.items()
                if book_id != candidate.sportsbook.id
            ]
            if len(consensus) < 2:
                continue
            fair_probability = sum(
                (probability for _, probability in consensus), Decimal("0")
            ) / Decimal(len(consensus))
            expected_value = fair_probability * candidate.decimal_odds - Decimal("1")
            if expected_value >= minimum_ev:
                consensus_names = tuple(
                    sorted(
                        complete_books[book_id][required_sides[0]].sportsbook.name
                        for book_id, _ in consensus
                    )
                )
                results.append(
                    ValueOpportunity(
                        quote=candidate,
                        fair_probability=fair_probability,
                        expected_value=expected_value,
                        reference_books=len(consensus),
                        reference_sportsbooks=consensus_names,
                    )
                )
    return tuple(sorted(results, key=lambda item: item.expected_value, reverse=True))
