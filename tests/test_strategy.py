from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from conftest import make_market, make_quote

from odds_scanner.analytics import detect_consensus_value
from odds_scanner.domain import (
    Event,
    League,
    OddsSnapshot,
    Participant,
    Sport,
    Sportsbook,
)
from odds_scanner.refresh import OddsRefreshService, RefreshConfig, RefreshRequest
from odds_scanner.storage.sqlite import SQLiteQuoteRepository
from odds_scanner.strategy import (
    OFFICIAL_MAXIMUM_STAKE_FRACTION,
    OFFICIAL_MINIMUM_STAKE_FRACTION,
    OFFICIAL_STARTING_BANKROLL_UNITS,
    OFFICIAL_STRATEGY_KEY,
    OFFICIAL_UNIT_VALUE_DOLLARS,
    official_bankroll_units,
    publish_official_recommendations,
    select_official_recommendations,
)


def _named_quote(market, side, price, now, *, book_id: str, book_name: str):
    return replace(
        make_quote(market, side, price, now, book=book_id),
        sportsbook=Sportsbook(book_id, book_name),
    )


def _strategy_snapshot(now) -> OddsSnapshot:
    events: list[Event] = []
    quotes = []
    for index, offered_price in enumerate(("2.20", "2.16", "2.12", "2.08"), start=1):
        event = Event(
            id=f"strategy-event-{index}",
            league_id="nfl",
            start_time=now + timedelta(days=index),
            home=Participant(f"home-{index}", f"Home {index}"),
            away=Participant(f"away-{index}", f"Away {index}"),
            name=f"Away {index} at Home {index}",
        )
        market = make_market(event_id=event.id)
        events.append(event)
        quotes.extend(
            (
                _named_quote(
                    market,
                    market.required_sides[0],
                    offered_price,
                    now,
                    book_id="playnow",
                    book_name="PlayNow",
                ),
                _named_quote(
                    market,
                    market.required_sides[1],
                    "1.75",
                    now,
                    book_id="playnow",
                    book_name="PlayNow",
                ),
            )
        )
        for book_id in ("alpha", "beta", "gamma"):
            quotes.extend(
                (
                    _named_quote(
                        market,
                        market.required_sides[0],
                        "1.90",
                        now,
                        book_id=book_id,
                        book_name=book_id.title(),
                    ),
                    _named_quote(
                        market,
                        market.required_sides[1],
                        "1.90",
                        now,
                        book_id=book_id,
                        book_name=book_id.title(),
                    ),
                )
            )
    return OddsSnapshot(
        provider_id="provider",
        sports=(Sport("american-football", "American Football"),),
        leagues=(League("nfl", "american-football", "NFL"),),
        events=tuple(events),
        quotes=tuple(quotes),
        fetched_at=now,
    )


@dataclass
class StrategySnapshotProvider:
    snapshot: OddsSnapshot
    request_count: int = 0

    @property
    def provider_id(self) -> str:
        return self.snapshot.provider_id

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        assert league_keys and market_keys
        self.request_count += 1
        return self.snapshot


def test_official_strategy_selects_unique_events_and_sizes_quarter_kelly(now):
    snapshot = _strategy_snapshot(now)
    values = detect_consensus_value(
        snapshot.quotes,
        as_of=now,
        max_age=timedelta(minutes=30),
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=("PlayNow", "Betway"),
    )
    event_map = {event.id: event for event in snapshot.events}

    recommendations = select_official_recommendations(
        values,
        event_map,
        as_of=now,
        bankroll_units=OFFICIAL_STARTING_BANKROLL_UNITS,
    )

    assert len(recommendations) == 3
    assert len(
        {
            item.opportunity.quote.outcome.market.event_id
            for item in recommendations
        }
    ) == len(recommendations)
    assert all(
        OFFICIAL_STARTING_BANKROLL_UNITS * OFFICIAL_MINIMUM_STAKE_FRACTION
        <= item.stake_units
        <= OFFICIAL_STARTING_BANKROLL_UNITS * OFFICIAL_MAXIMUM_STAKE_FRACTION
        for item in recommendations
    )


def test_successful_refresh_publication_is_idempotent_and_keeps_strategy_metadata(
    tmp_path, now
):
    repository = SQLiteQuoteRepository(tmp_path / "strategy.db")
    repository.save_snapshot(_strategy_snapshot(now))

    first = publish_official_recommendations(
        repository,
        "provider",
        as_of=now,
        max_age=timedelta(minutes=30),
    )
    second = publish_official_recommendations(
        repository,
        "provider",
        as_of=now,
        max_age=timedelta(minutes=30),
    )

    assert len(first) == 3
    assert second == ()
    assert len(repository.list_bets()) == 3
    assert all(OFFICIAL_STRATEGY_KEY in bet.notes for bet in first)
    assert all(
        Decimal("25")
        <= bet.stake * OFFICIAL_UNIT_VALUE_DOLLARS
        <= Decimal("100")
        for bet in first
    )
    assert official_bankroll_units(repository.list_bets()) == Decimal("100")


def test_official_publication_considers_every_available_sportsbook(tmp_path, now):
    snapshot = _strategy_snapshot(now)
    first_market = snapshot.quotes[0].outcome.market
    global_book_quotes = (
        _named_quote(
            first_market,
            first_market.required_sides[0],
            "2.25",
            now,
            book_id="global-book",
            book_name="Global Book",
        ),
        _named_quote(
            first_market,
            first_market.required_sides[1],
            "1.75",
            now,
            book_id="global-book",
            book_name="Global Book",
        ),
    )
    repository = SQLiteQuoteRepository(tmp_path / "global-strategy.db")
    repository.save_snapshot(replace(snapshot, quotes=(*snapshot.quotes, *global_book_quotes)))

    published = publish_official_recommendations(
        repository,
        "provider",
        as_of=now,
        max_age=timedelta(minutes=30),
    )

    assert any(bet.sportsbook == "Global Book" for bet in published)


def test_refresh_engine_automatically_publishes_the_official_slate(tmp_path, now):
    repository = SQLiteQuoteRepository(tmp_path / "refresh-strategy.db")
    provider = StrategySnapshotProvider(_strategy_snapshot(now))
    service = OddsRefreshService(
        provider,
        repository,
        RefreshConfig(manual_only=False),
        now_factory=lambda: now,
    )
    request = RefreshRequest(
        league_keys=("americanfootball_nfl",),
        league_ids=("nfl",),
        market_keys=("h2h",),
        market_kinds=(provider.snapshot.quotes[0].outcome.market.kind,),
        trigger_type="automated",
    )

    service.refresh(request)

    assert len(repository.list_bets()) == 3
    settings = repository.load_settings()
    assert settings["official_recommendations_last_published"] == "3"
    assert settings["official_recommendations_last_run_at"] == now.isoformat()
