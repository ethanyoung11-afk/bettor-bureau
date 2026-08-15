from __future__ import annotations

from datetime import UTC, datetime, timedelta

from odds_scanner.live_refresh import (
    DEFAULT_LEAGUE_KEYS,
    build_refresh_request,
    estimated_request_count,
    record_requests,
    requests_used_this_month,
    schedule_refresh_due,
)
from odds_scanner.providers.oddspapi import OddsPapiProvider
from odds_scanner.storage.sqlite import SQLiteQuoteRepository


def test_default_live_refresh_includes_cfl_ncaaf_and_nhl() -> None:
    assert {
        "americanfootball_cfl",
        "americanfootball_ncaaf",
        "icehockey_nhl",
    }.issubset(DEFAULT_LEAGUE_KEYS)


def test_scheduled_request_uses_all_configured_leagues_and_markets() -> None:
    request = build_refresh_request(
        ("americanfootball_nfl", "americanfootball_cfl", "basketball_nba"),
        ("h2h", "spreads", "player_props"),
    )

    assert request.trigger_type == "automated"
    assert request.league_ids == ("nfl", "cfl", "nba")
    assert len(request.market_kinds) == 3


def test_estimated_request_count_includes_only_missing_discovery() -> None:
    cold = OddsPapiProvider(api_key="test", bookmaker_slugs=("playnow", "pinnacle"))
    warm = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("playnow", "pinnacle"),
        tournament_ids={
            "americanfootball_nfl": 1,
            "basketball_nba": 2,
        },
        market_catalog={
            "1": {"marketId": 1, "sportId": 14},
            "2": {"marketId": 2, "sportId": 11},
        },
    )
    leagues = ("americanfootball_nfl", "basketball_nba")

    assert estimated_request_count(cold, leagues) == 5
    assert estimated_request_count(warm, leagues) == 2
    warm.include_schedule = True
    assert estimated_request_count(warm, leagues) == 4


def test_schedule_catalog_refreshes_weekly() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)

    assert schedule_refresh_due({}, as_of=now)
    assert not schedule_refresh_due(
        {"oddspapi_schedule_refreshed_at": (now - timedelta(days=2)).isoformat()},
        as_of=now,
    )
    assert schedule_refresh_due(
        {"oddspapi_schedule_refreshed_at": (now - timedelta(days=8)).isoformat()},
        as_of=now,
    )


def test_monthly_usage_resets_without_erasing_prior_month(tmp_path) -> None:
    repository = SQLiteQuoteRepository(tmp_path / "usage.db")
    july = datetime(2026, 7, 31, tzinfo=UTC)
    august = datetime(2026, 8, 1, tzinfo=UTC)

    assert record_requests(repository, 7, as_of=july) == 7
    assert requests_used_this_month(repository, as_of=august) == 0
    assert record_requests(repository, 2, as_of=august) == 2
