from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from conftest import make_market, make_quote

from odds_scanner.domain import BetStatus, OddsSnapshot, OutcomeSide, Sport, TrackedBet
from odds_scanner.storage.sqlite import SQLiteQuoteRepository


def test_sqlite_snapshot_round_trip(tmp_path, now, event, league):
    market = make_market()
    quote = replace(
        make_quote(market, OutcomeSide.HOME, "2.10", now, book="alpha"),
        source_url="https://alpha.example/sports/event/event-1",
    )
    snapshot = OddsSnapshot(
        provider_id="provider",
        sports=(Sport("american-football", "American Football"),),
        leagues=(league,),
        events=(event,),
        quotes=(quote,),
        fetched_at=now,
    )
    repository = SQLiteQuoteRepository(tmp_path / "quotes.db")
    repository.save_snapshot(snapshot)
    loaded = repository.load_quotes_since(now - timedelta(minutes=1))
    assert loaded == (quote,)


def test_schedule_only_events_are_scoped_to_their_provider(tmp_path, now, event, league):
    repository = SQLiteQuoteRepository(tmp_path / "schedule.db")
    snapshot = OddsSnapshot(
        provider_id="oddspapi",
        sports=(Sport("american-football", "American Football"),),
        leagues=(league,),
        events=(event,),
        quotes=(),
        fetched_at=now,
    )

    repository.save_snapshot(snapshot)

    assert repository.load_events("oddspapi") == (event,)
    assert repository.load_events("another-provider") == ()


def test_latest_quotes_keep_last_price_without_an_age_cutoff(tmp_path, now, event, league):
    market = make_market()
    old_quote = make_quote(market, OutcomeSide.HOME, "2.10", now, book="alpha")
    new_quote = make_quote(
        market,
        OutcomeSide.HOME,
        "2.25",
        now,
        book="alpha",
        observed_offset_seconds=60,
    )
    repository = SQLiteQuoteRepository(tmp_path / "latest.db")
    for quote in (old_quote, new_quote):
        repository.save_snapshot(
            OddsSnapshot(
                provider_id="provider",
                sports=(Sport("american-football", "American Football"),),
                leagues=(league,),
                events=(event,),
                quotes=(quote,),
                fetched_at=quote.observed_at,
            )
        )

    assert repository.load_latest_quotes("provider") == (new_quote,)


def test_latest_quotes_match_the_most_recent_complete_snapshot(tmp_path, now, event, league):
    market = make_market()
    home = make_quote(market, OutcomeSide.HOME, "2.10", now, book="alpha")
    away = make_quote(market, OutcomeSide.AWAY, "1.80", now, book="alpha")
    refreshed_home = make_quote(
        market,
        OutcomeSide.HOME,
        "2.25",
        now,
        book="alpha",
        observed_offset_seconds=60,
    )
    repository = SQLiteQuoteRepository(tmp_path / "current-snapshot.db")
    repository.save_snapshot(
        OddsSnapshot(
            provider_id="provider",
            sports=(Sport("american-football", "American Football"),),
            leagues=(league,),
            events=(event,),
            quotes=(home, away),
            fetched_at=now,
        )
    )
    repository.save_snapshot(
        OddsSnapshot(
            provider_id="provider",
            sports=(Sport("american-football", "American Football"),),
            leagues=(league,),
            events=(event,),
            quotes=(refreshed_home,),
            fetched_at=refreshed_home.observed_at,
        )
    )

    assert repository.load_latest_quotes("provider") == (refreshed_home,)


def test_sqlite_configures_wal_and_waits_for_busy_database(tmp_path):
    repository = SQLiteQuoteRepository(tmp_path / "reliable.db")

    with repository._connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout == 30_000


def test_sqlite_serializes_concurrent_snapshot_writes(tmp_path, now, event, league):
    market = make_market()
    quote = make_quote(market, OutcomeSide.HOME, "2.10", now, book="alpha")
    snapshot = OddsSnapshot(
        provider_id="provider",
        sports=(Sport("american-football", "American Football"),),
        leagues=(league,),
        events=(event,),
        quotes=(quote,),
        fetched_at=now,
    )
    repository = SQLiteQuoteRepository(tmp_path / "concurrent.db")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(repository.save_snapshot, (snapshot,) * 12))

    assert repository.load_latest_quotes("provider") == (quote,)


def test_settings_watchlist_and_bet_tracker_round_trip(tmp_path, now, event):
    repository = SQLiteQuoteRepository(tmp_path / "terminal.db")
    repository.save_setting("bankroll", "750")
    repository.set_event_watched(event.id, True, now)
    bet_id = repository.add_bet(
        TrackedBet(
            id=None,
            created_at=now,
            event_id=event.id,
            event_name=event.name,
            market_label="Moneyline",
            selection="Home",
            sportsbook="Alpha",
            decimal_odds=Decimal("2.10"),
            stake=Decimal("50"),
        )
    )
    settled_at = now + timedelta(minutes=5)
    repository.update_bet(bet_id, BetStatus.WON, Decimal("55"), settled_at=settled_at)

    assert repository.load_settings()["bankroll"] == "750"
    assert repository.watched_event_ids() == frozenset({event.id})
    assert repository.list_bets()[0].status is BetStatus.WON
    assert repository.list_bets()[0].profit_loss == Decimal("55")
    assert repository.list_bets()[0].settled_at == settled_at
