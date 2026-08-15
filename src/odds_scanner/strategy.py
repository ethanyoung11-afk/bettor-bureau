from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import log1p

from odds_scanner.analytics import ValueOpportunity, detect_consensus_value
from odds_scanner.domain import (
    BetStatus,
    Event,
    MarketKind,
    OutcomeSide,
    Quote,
    TrackedBet,
    stable_id,
)
from odds_scanner.opportunities import implied_probability
from odds_scanner.presentation import decimal_to_american
from odds_scanner.storage.base import QuoteRepository

OFFICIAL_STRATEGY_KEY = "balanced-quarter-kelly-v1"
OFFICIAL_RECOMMENDATION_PREFIX = "official-recommendation:"
OFFICIAL_TARGET_SPORTSBOOKS = ("PlayNow", "Betway")
OFFICIAL_STARTING_BANKROLL_UNITS = Decimal("100")
OFFICIAL_UNIT_VALUE_DOLLARS = Decimal("100")
OFFICIAL_MINIMUM_EV = Decimal("0.02")
OFFICIAL_MINIMUM_BREAK_EVEN_PROBABILITY = Decimal("0.30")
OFFICIAL_MINIMUM_AMERICAN_ODDS = -200
OFFICIAL_MAXIMUM_AMERICAN_ODDS = 300
OFFICIAL_MINIMUM_REFERENCE_BOOKS = 3
OFFICIAL_MAXIMUM_BETS_PER_SLATE = 3
OFFICIAL_KELLY_FRACTION = Decimal("0.25")
OFFICIAL_MAXIMUM_STAKE_FRACTION = Decimal("0.01")
OFFICIAL_MINIMUM_STAKE_FRACTION = Decimal("0.0025")
OFFICIAL_STAKE_INCREMENT_UNITS = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class OfficialRecommendation:
    opportunity: ValueOpportunity
    score: Decimal
    stake_units: Decimal
    full_kelly_fraction: Decimal


def _best_value_by_outcome(
    values: tuple[ValueOpportunity, ...],
) -> tuple[ValueOpportunity, ...]:
    selected: dict[str, ValueOpportunity] = {}
    for item in values:
        outcome_id = item.quote.outcome.id
        current = selected.get(outcome_id)
        if current is None or (
            item.quote.decimal_odds,
            item.expected_value,
            item.quote.source_updated_at,
        ) > (
            current.quote.decimal_odds,
            current.expected_value,
            current.quote.source_updated_at,
        ):
            selected[outcome_id] = item
    return tuple(selected.values())


def _kelly_fraction(opportunity: ValueOpportunity) -> Decimal:
    net_odds = opportunity.quote.decimal_odds - Decimal("1")
    if net_odds <= 0:
        return Decimal("0")
    return max(Decimal("0"), opportunity.expected_value / net_odds)


def _stake_units(
    opportunity: ValueOpportunity,
    bankroll_units: Decimal,
) -> tuple[Decimal, Decimal]:
    full_kelly = _kelly_fraction(opportunity)
    proposed_fraction = full_kelly * OFFICIAL_KELLY_FRACTION
    stake_fraction = min(proposed_fraction, OFFICIAL_MAXIMUM_STAKE_FRACTION)
    if stake_fraction < OFFICIAL_MINIMUM_STAKE_FRACTION:
        return Decimal("0"), full_kelly
    raw_stake = bankroll_units * stake_fraction
    rounded_stake = (
        raw_stake / OFFICIAL_STAKE_INCREMENT_UNITS
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * OFFICIAL_STAKE_INCREMENT_UNITS
    return max(OFFICIAL_STAKE_INCREMENT_UNITS, rounded_stake), full_kelly


def _growth_score(
    opportunity: ValueOpportunity,
    stake_units: Decimal,
    bankroll_units: Decimal,
) -> Decimal:
    if bankroll_units <= 0 or stake_units <= 0:
        return Decimal("0")
    fraction = stake_units / bankroll_units
    net_odds = opportunity.quote.decimal_odds - Decimal("1")
    probability = opportunity.fair_probability
    expected_log_growth = probability * Decimal(
        str(log1p(float(fraction * net_odds)))
    ) + (Decimal("1") - probability) * Decimal(str(log1p(-float(fraction))))
    confidence = min(
        Decimal("1"),
        Decimal(opportunity.reference_books) / Decimal("8"),
    )
    return expected_log_growth * confidence


def select_official_recommendations(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    *,
    as_of: datetime,
    bankroll_units: Decimal,
    limit: int = OFFICIAL_MAXIMUM_BETS_PER_SLATE,
) -> tuple[OfficialRecommendation, ...]:
    """Choose a risk-aware slate without forcing bets that fail the policy."""
    candidates: list[OfficialRecommendation] = []
    for opportunity in _best_value_by_outcome(values):
        event = event_map.get(opportunity.quote.outcome.market.event_id)
        if event is None or event.start_time <= as_of:
            continue
        if opportunity.expected_value < OFFICIAL_MINIMUM_EV:
            continue
        if (
            implied_probability(opportunity.quote.decimal_odds)
            < OFFICIAL_MINIMUM_BREAK_EVEN_PROBABILITY
        ):
            continue
        american_odds = decimal_to_american(opportunity.quote.decimal_odds)
        if not OFFICIAL_MINIMUM_AMERICAN_ODDS <= american_odds <= OFFICIAL_MAXIMUM_AMERICAN_ODDS:
            continue
        if opportunity.reference_books < OFFICIAL_MINIMUM_REFERENCE_BOOKS:
            continue
        stake_units, full_kelly = _stake_units(opportunity, bankroll_units)
        if stake_units <= 0:
            continue
        candidates.append(
            OfficialRecommendation(
                opportunity=opportunity,
                score=_growth_score(opportunity, stake_units, bankroll_units),
                stake_units=stake_units,
                full_kelly_fraction=full_kelly,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.score,
            item.opportunity.reference_books,
            item.opportunity.expected_value,
        ),
        reverse=True,
    )
    selected: list[OfficialRecommendation] = []
    selected_events: set[str] = set()
    for candidate in candidates:
        event_id = candidate.opportunity.quote.outcome.market.event_id
        if event_id in selected_events:
            continue
        selected.append(candidate)
        selected_events.add(event_id)
        if len(selected) >= limit:
            break
    return tuple(selected)


def official_bets(bets: tuple[TrackedBet, ...]) -> tuple[TrackedBet, ...]:
    return tuple(
        bet for bet in bets if bet.notes.startswith(OFFICIAL_RECOMMENDATION_PREFIX)
    )


def official_bankroll_units(bets: tuple[TrackedBet, ...]) -> Decimal:
    realized = sum(
        (bet.profit_loss or Decimal("0") for bet in official_bets(bets)),
        Decimal("0"),
    )
    return OFFICIAL_STARTING_BANKROLL_UNITS + realized


def _market_label(quote: Quote) -> str:
    market = quote.outcome.market
    if market.kind is MarketKind.PLAYER_PROP:
        return market.stat_key or "Player prop"
    label = {
        MarketKind.MONEYLINE: "Moneyline",
        MarketKind.SPREAD: "Spread",
        MarketKind.TOTAL: "Total",
    }[market.kind]
    if market.line is not None:
        label += f" {market.line:+}" if market.kind is MarketKind.SPREAD else f" {market.line}"
    return label


def _selection_label(quote: Quote, event: Event) -> str:
    market = quote.outcome.market
    if market.kind is MarketKind.PLAYER_PROP:
        subject = market.variant if market.variant != "standard" else "Player"
        return f"{subject} {quote.outcome.side.value.title()} {market.line}"
    selection = quote.outcome.side.value.title()
    if quote.outcome.side is OutcomeSide.HOME:
        selection = event.home.name
    elif quote.outcome.side is OutcomeSide.AWAY:
        selection = event.away.name
    elif quote.outcome.side is OutcomeSide.DRAW:
        selection = "Draw"
    if market.kind is MarketKind.SPREAD and market.line is not None:
        line = market.line if quote.outcome.side is OutcomeSide.HOME else -market.line
        return f"{selection} {line:+}"
    if market.line is not None:
        return f"{selection} {market.line}"
    return selection


def _recommendation_note(recommendation: OfficialRecommendation) -> str:
    opportunity = recommendation.opportunity
    event_id = opportunity.quote.outcome.market.event_id
    recommendation_id = stable_id(
        "official-recommendation",
        OFFICIAL_STRATEGY_KEY,
        event_id,
    )
    metadata = json.dumps(
        {
            "strategy": OFFICIAL_STRATEGY_KEY,
            "outcome_id": opportunity.quote.outcome.id,
            "expected_value": str(opportunity.expected_value),
            "fair_probability": str(opportunity.fair_probability),
            "reference_books": opportunity.reference_books,
            "score": str(recommendation.score),
            "full_kelly_fraction": str(recommendation.full_kelly_fraction),
            "unit_value_dollars": str(OFFICIAL_UNIT_VALUE_DOLLARS),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{OFFICIAL_RECOMMENDATION_PREFIX}{recommendation_id}|{metadata}"


def publish_official_recommendations(
    repository: QuoteRepository,
    provider_id: str,
    *,
    as_of: datetime,
    max_age: timedelta,
) -> tuple[TrackedBet, ...]:
    """Publish the current official slate after a successful odds refresh."""
    events = repository.load_events()
    event_map = {event.id: event for event in events}
    quotes = tuple(
        quote
        for quote in repository.load_latest_quotes(provider_id)
        if quote.outcome.market.event_id in event_map
    )
    values = detect_consensus_value(
        quotes,
        as_of=as_of,
        max_age=max_age,
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=OFFICIAL_TARGET_SPORTSBOOKS,
        include_stale=False,
    )
    current_bets = repository.list_bets()
    current_bankroll = official_bankroll_units(current_bets)
    slate = select_official_recommendations(
        values,
        event_map,
        as_of=as_of,
        bankroll_units=current_bankroll,
    )
    previously_recorded_events = {bet.event_id for bet in official_bets(current_bets)}
    published: list[TrackedBet] = []
    for recommendation in slate:
        opportunity = recommendation.opportunity
        event = event_map[opportunity.quote.outcome.market.event_id]
        if event.id in previously_recorded_events:
            continue
        tracked = TrackedBet(
            id=None,
            created_at=as_of,
            event_id=event.id,
            event_name=event.name,
            market_label=_market_label(opportunity.quote),
            selection=_selection_label(opportunity.quote, event),
            sportsbook=opportunity.quote.sportsbook.name,
            decimal_odds=opportunity.quote.decimal_odds,
            stake=recommendation.stake_units,
            notes=_recommendation_note(recommendation),
        )
        bet_id = repository.add_bet(tracked)
        published.append(
            TrackedBet(
                id=bet_id,
                created_at=tracked.created_at,
                event_id=tracked.event_id,
                event_name=tracked.event_name,
                market_label=tracked.market_label,
                selection=tracked.selection,
                sportsbook=tracked.sportsbook,
                decimal_odds=tracked.decimal_odds,
                stake=tracked.stake,
                status=BetStatus.PENDING,
                notes=tracked.notes,
            )
        )
        previously_recorded_events.add(event.id)
    return tuple(published)
