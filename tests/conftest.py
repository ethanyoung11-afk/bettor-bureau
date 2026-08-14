from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from odds_scanner.domain import (
    Event,
    League,
    MarketKey,
    MarketKind,
    OutcomeKey,
    OutcomeSide,
    Participant,
    Quote,
    Sportsbook,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 13, 20, 0, tzinfo=UTC)


@pytest.fixture
def event(now: datetime) -> Event:
    return Event(
        id="event-1",
        league_id="nfl",
        start_time=now,
        home=Participant("home-team", "Home Team"),
        away=Participant("away-team", "Away Team"),
        name="Away Team at Home Team",
    )


@pytest.fixture
def league() -> League:
    return League(id="nfl", sport_id="american-football", name="NFL")


def make_market(
    *,
    event_id: str = "event-1",
    kind: MarketKind = MarketKind.MONEYLINE,
    sides: tuple[OutcomeSide, ...] = (OutcomeSide.HOME, OutcomeSide.AWAY),
    line: Decimal | None = None,
) -> MarketKey:
    return MarketKey(event_id=event_id, kind=kind, required_sides=sides, line=line)


def make_quote(
    market: MarketKey,
    side: OutcomeSide,
    price: str,
    now: datetime,
    *,
    book: str,
    provider: str = "provider",
    age_seconds: int = 0,
    observed_offset_seconds: int = 0,
) -> Quote:
    from datetime import timedelta

    return Quote(
        provider_id=provider,
        sportsbook=Sportsbook(id=book, name=book.title()),
        outcome=OutcomeKey(market=market, side=side),
        decimal_odds=Decimal(price),
        source_updated_at=now - timedelta(seconds=age_seconds),
        observed_at=now + timedelta(seconds=observed_offset_seconds),
    )
