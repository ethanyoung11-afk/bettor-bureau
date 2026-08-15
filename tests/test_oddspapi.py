from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from odds_scanner.domain import MarketKind, OutcomeSide
from odds_scanner.providers.oddspapi import (
    ODDSPAPI_PRIMARY_TOURNAMENT_IDS,
    OddsPapiProvider,
    _full_game_market,
    _league_key,
)


class StubResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class StubSession:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
    ) -> StubResponse:
        del timeout
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, params))
        return StubResponse(self.payloads[endpoint])


def _catalog() -> list[dict[str, object]]:
    return [
        {
            "marketId": 201,
            "marketName": "Moneyline",
            "marketType": "moneyline",
            "period": "fulltime",
            "playerProp": False,
            "sportId": 14,
            "handicap": 0,
            "outcomes": [
                {"outcomeId": 2011, "outcomeName": "1"},
                {"outcomeId": 2012, "outcomeName": "2"},
            ],
        },
        {
            "marketId": 301,
            "marketName": "Point Spread",
            "marketType": "handicap",
            "period": "fulltime",
            "playerProp": False,
            "sportId": 14,
            "handicap": -3.5,
            "outcomes": [
                {"outcomeId": 3011, "outcomeName": "Home"},
                {"outcomeId": 3012, "outcomeName": "Away"},
            ],
        },
        {
            "marketId": 401,
            "marketName": "Total Points",
            "marketType": "totals",
            "period": "fulltime",
            "playerProp": False,
            "sportId": 14,
            "handicap": 47.5,
            "outcomes": [
                {"outcomeId": 4011, "outcomeName": "Over"},
                {"outcomeId": 4012, "outcomeName": "Under"},
            ],
        },
        {
            "marketId": 501,
            "marketName": "Over Under Pass Yards (incl. overtime)",
            "marketType": "playertotals-passyards",
            "period": "result",
            "playerProp": True,
            "sportId": 14,
            "handicap": 249.5,
            "outcomes": [
                {"outcomeId": 5011, "outcomeName": "Over"},
                {"outcomeId": 5012, "outcomeName": "Under"},
            ],
        },
    ]


def _market(outcome_ids: tuple[int, int], prices: tuple[str, str]) -> dict[str, object]:
    return {
        "marketActive": True,
        "outcomes": {
            str(outcome_id): {
                "players": {
                    "0": {
                        "active": True,
                        "playerName": None,
                        "price": price,
                        "changedAt": "2026-08-13T20:00:00Z",
                    }
                }
            }
            for outcome_id, price in zip(outcome_ids, prices, strict=True)
        },
    }


def _player_prop_market(prices: tuple[str, str]) -> dict[str, object]:
    return {
        "marketActive": True,
        "outcomes": {
            str(outcome_id): {
                "players": {
                    "44": {
                        "active": True,
                        "playerName": "Nathan Rourke",
                        "price": price,
                        "mainLine": True,
                    }
                }
            }
            for outcome_id, price in zip((5011, 5012), prices, strict=True)
        },
    }


def test_oddspapi_normalizes_all_books_and_featured_markets() -> None:
    betway_moneyline = _market((2011, 2012), ("2.02", "1.85"))
    betway_outcomes = betway_moneyline["outcomes"]
    assert isinstance(betway_outcomes, dict)
    home_players = betway_outcomes["2011"]["players"]
    assert isinstance(home_players, dict)
    home_players["0"]["betslip"] = (
        "https://betway.com/g/en/sports/betslip/new-england-moneyline"
    )
    event = {
        "fixtureId": "fixture-1",
        "tournamentId": 900,
        "participant1Name": "BC Lions",
        "participant2Name": "Calgary Stampeders",
        "startTime": "2026-08-14T02:00:00Z",
        "updatedAt": "2026-08-13T20:00:00Z",
        "bookmakerOdds": {
            "playnow": {
                "bookmakerIsActive": True,
                "bookmakerFixtureId": "12025970",
                "markets": {
                    "201": _market((2011, 2012), ("1.91", "1.95")),
                    "301": _market((3011, 3012), ("1.90", "1.92")),
                    "401": _market((4011, 4012), ("1.93", "1.89")),
                },
            },
            "betway": {
                "bookmakerIsActive": True,
                "fixturePath": "https://betway.com/g/en/sports/event/fixture-1",
                "markets": {"201": betway_moneyline},
            },
        },
    }
    session = StubSession(
        {
            "tournaments": [{"tournamentId": 900, "tournamentName": "CFL"}],
            "markets": _catalog(),
            "odds-by-tournaments": [event],
        }
    )
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("playnow",),
        bookmaker_cooldown_seconds=0,
        session=session,  # type: ignore[arg-type]
    )

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["h2h", "spreads", "totals"])

    assert snapshot.provider_id == "oddspapi"
    assert len(snapshot.events) == 1
    assert len(snapshot.quotes) == 8
    assert {quote.sportsbook.name for quote in snapshot.quotes} == {"PlayNow", "Betway"}
    assert {
        quote.source_url for quote in snapshot.quotes if quote.sportsbook.name == "PlayNow"
    } == {"https://www.playnow.com/sports/sports/event/12025970"}
    assert {
        quote.source_url for quote in snapshot.quotes if quote.sportsbook.name == "Betway"
    } == {
        "https://betway.com/g/en/sports/betslip/new-england-moneyline",
        "https://betway.com/g/en/sports/event/fixture-1",
    }
    assert {quote.outcome.market.kind for quote in snapshot.quotes} == {
        MarketKind.MONEYLINE,
        MarketKind.SPREAD,
        MarketKind.TOTAL,
    }
    spread_quotes = [
        quote for quote in snapshot.quotes if quote.outcome.market.kind is MarketKind.SPREAD
    ]
    assert {quote.outcome.side for quote in spread_quotes} == {
        OutcomeSide.HOME,
        OutcomeSide.AWAY,
    }
    assert {quote.outcome.market.line for quote in spread_quotes} == {Decimal("-3.5")}
    assert [call[0] for call in session.calls] == [
        "tournaments",
        "markets",
        "odds-by-tournaments",
    ]
    assert "bookmakers" not in session.calls[-1][1]
    assert provider.request_count == 3


def test_oddspapi_queries_each_selected_book_on_starter_accounts() -> None:
    session = StubSession({"odds-by-tournaments": []})
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("playnow", "pinnacle", "playnow"),
        include_all_bookmakers=False,
        bookmaker_cooldown_seconds=0,
        session=session,  # type: ignore[arg-type]
        tournament_ids={"americanfootball_nfl": 800},
        market_catalog={"201": _catalog()[0]},
    )

    provider.fetch_snapshot(["americanfootball_nfl"], ["h2h"])

    assert len(session.calls) == 2
    assert [params["bookmaker"] for _, params in session.calls] == [
        "playnow",
        "pinnacle",
    ]
    assert provider.request_count == 2


def test_oddspapi_reuses_discovery_catalogs() -> None:
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("pinnacle",),
        bookmaker_cooldown_seconds=0,
        session=StubSession({"odds-by-tournaments": []}),  # type: ignore[arg-type]
        tournament_ids={"americanfootball_nfl": 800},
        market_catalog={"201": _catalog()[0]},
    )

    snapshot = provider.fetch_snapshot(["americanfootball_nfl"], ["h2h"])

    assert snapshot.quotes == ()
    session = provider.session
    assert isinstance(session, StubSession)
    assert [call[0] for call in session.calls] == ["odds-by-tournaments"]
    assert provider.request_count == 1


def test_oddspapi_discovers_tournaments_for_each_requested_sport() -> None:
    nba_market = {**_catalog()[0], "marketId": 111, "sportId": 11}
    nhl_market = {**_catalog()[0], "marketId": 151, "sportId": 15}
    session = StubSession(
        {
            "tournaments": [
                {"tournamentId": 132, "tournamentName": "NBA"},
                {"tournamentId": 234, "tournamentName": "NHL"},
            ],
            "markets": [nba_market, nhl_market],
            "odds-by-tournaments": [],
        }
    )
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("pinnacle",),
        bookmaker_cooldown_seconds=0,
        session=session,  # type: ignore[arg-type]
    )

    provider.fetch_snapshot(["basketball_nba", "icehockey_nhl"], ["h2h"])

    tournament_calls = [params for endpoint, params in session.calls if endpoint == "tournaments"]
    assert [params["sportId"] for params in tournament_calls] == ["11", "15"]


def test_oddspapi_refreshes_a_partial_market_catalog_for_new_sports() -> None:
    nba_market = {**_catalog()[0], "marketId": 111, "sportId": 11}
    session = StubSession(
        {
            "markets": [*_catalog(), nba_market],
            "odds-by-tournaments": [],
        }
    )
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("pinnacle",),
        bookmaker_cooldown_seconds=0,
        session=session,  # type: ignore[arg-type]
        tournament_ids={"basketball_nba": 132},
        market_catalog={"201": _catalog()[0]},
    )

    provider.fetch_snapshot(["basketball_nba"], ["h2h"])

    assert [call[0] for call in session.calls] == ["markets", "odds-by-tournaments"]
    assert any(raw.get("sportId") == 11 for raw in provider.market_catalog.values())


def test_oddspapi_normalizes_player_props_without_extra_requests() -> None:
    event = {
        "fixtureId": "fixture-props",
        "tournamentId": 900,
        "participant1Name": "BC Lions",
        "participant2Name": "Calgary Stampeders",
        "startTime": "2026-08-14T02:00:00Z",
        "bookmakerOdds": {
            "playnow": {
                "bookmakerIsActive": True,
                "markets": {"501": _player_prop_market(("2.05", "1.80"))},
            },
            "betway": {
                "bookmakerIsActive": True,
                "markets": {"501": _player_prop_market(("1.95", "1.91"))},
            },
        },
    }
    session = StubSession(
        {
            "tournaments": [{"tournamentId": 900, "tournamentName": "CFL"}],
            "markets": _catalog(),
            "odds-by-tournaments": [event],
        }
    )
    provider = OddsPapiProvider(
        api_key="test",
        bookmaker_slugs=("playnow",),
        bookmaker_cooldown_seconds=0,
        session=session,  # type: ignore[arg-type]
    )

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["player_props"])

    assert len(snapshot.quotes) == 4
    assert {quote.outcome.market.kind for quote in snapshot.quotes} == {
        MarketKind.PLAYER_PROP
    }
    market = snapshot.quotes[0].outcome.market
    assert market.variant == "Nathan Rourke"
    assert market.stat_key == "Passing yards"
    assert market.line == Decimal("249.5")
    assert {quote.outcome.side for quote in snapshot.quotes} == {
        OutcomeSide.OVER,
        OutcomeSide.UNDER,
    }
    assert provider.request_count == 3


def test_oddspapi_timestamps_are_aware() -> None:
    provider = OddsPapiProvider(api_key="test")
    parsed = datetime.fromisoformat("2026-08-13T20:00:00+00:00")
    assert parsed.tzinfo is UTC
    assert provider.provider_id == "oddspapi"


def test_market_classifier_rejects_partial_and_team_totals() -> None:
    assert (
        _full_game_market(
            {
                "marketName": "Total (incl. overtime)",
                "marketType": "totals",
                "period": "result",
                "playerProp": False,
            }
        )
        == "totals"
    )
    assert (
        _full_game_market(
            {
                "marketName": "Over Under Team 1 (incl. overtime)",
                "marketType": "teamtotals-team1",
                "period": "result",
                "playerProp": False,
            }
        )
        is None
    )
    assert (
        _full_game_market(
            {
                "marketName": "Regular Time Result",
                "marketType": "1x2",
                "period": "fulltime",
                "playerProp": False,
                "outcomes": [
                    {"outcomeName": "1"},
                    {"outcomeName": "X"},
                    {"outcomeName": "2"},
                ],
            }
        )
        is None
    )
    assert (
        _full_game_market(
            {
                "marketName": "Winner (incl. overtime)",
                "marketType": "moneyline",
                "period": "result",
                "playerProp": False,
                "outcomes": [{"outcomeName": "1"}, {"outcomeName": "2"}],
            }
        )
        == "h2h"
    )
    assert (
        _full_game_market(
            {
                "marketName": "Over Under First Half",
                "marketType": "totals",
                "period": "p1+p2",
                "playerProp": False,
            }
        )
        is None
    )


def test_league_classifier_supports_initial_mvp_sports() -> None:
    assert _league_key("NFL") == "americanfootball_nfl"
    assert _league_key("NCAAF") == "americanfootball_ncaaf"
    assert _league_key("NBA") == "basketball_nba"
    assert _league_key("NHL") == "icehockey_nhl"
    assert _league_key("CFL, Preseason") is None
    assert _league_key("NCAA, Regular Season") == "americanfootball_ncaaf"
    assert ODDSPAPI_PRIMARY_TOURNAMENT_IDS["americanfootball_cfl"] == 790
    assert ODDSPAPI_PRIMARY_TOURNAMENT_IDS["americanfootball_ncaaf"] == 27653
