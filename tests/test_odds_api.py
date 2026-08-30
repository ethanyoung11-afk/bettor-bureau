from __future__ import annotations

from typing import Any

from odds_scanner.domain import OutcomeSide
from odds_scanner.providers.odds_api import OddsApiProvider


class StubResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: object, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class StubSession:
    def __init__(self, response: StubResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        timeout: float,
    ) -> StubResponse:
        del timeout
        self.calls.append((url, params))
        return self.response


def _event_payload(*, include_timestamp: bool = True) -> list[dict[str, object]]:
    market: dict[str, object] = {
        "key": "h2h",
        "outcomes": [
            {"name": "BC Lions", "price": 1.8, "link": "https://example.test/bet"},
            {"name": "Calgary Stampeders", "price": 2.1},
        ],
    }
    if include_timestamp:
        market["last_update"] = "2026-08-30T15:58:00Z"
    return [
        {
            "id": "cfl-event-1",
            "commence_time": "2026-09-01T02:00:00Z",
            "home_team": "BC Lions",
            "away_team": "Calgary Stampeders",
            "bookmakers": [
                {"key": "playnow_ca", "title": "PlayNow", "markets": [market]},
                {"key": "betway", "title": "Betway", "markets": [market]},
            ],
        }
    ]


def test_fetches_canadian_and_international_books_with_real_timestamps() -> None:
    response = StubResponse(
        _event_payload(),
        headers={
            "x-requests-used": "24",
            "x-requests-remaining": "476",
            "x-requests-last": "4",
        },
    )
    session = StubSession(response)
    provider = OddsApiProvider(api_key="secret", session=session)  # type: ignore[arg-type]

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["h2h"])

    assert {quote.sportsbook.name for quote in snapshot.quotes} == {"PlayNow", "Betway"}
    assert all(
        quote.source_updated_at.isoformat() == "2026-08-30T15:58:00+00:00"
        for quote in snapshot.quotes
    )
    assert snapshot.quotes[0].source_url == "https://example.test/bet"
    assert session.calls[0][1]["regions"] == "ca,us,uk,eu"
    assert provider.quota_used == 24
    assert provider.quota_remaining == 476
    assert provider.last_request_cost == 4
    assert provider.request_count == 4


def test_skips_prices_without_a_provider_freshness_timestamp() -> None:
    session = StubSession(StubResponse(_event_payload(include_timestamp=False)))
    provider = OddsApiProvider(api_key="secret", session=session)  # type: ignore[arg-type]

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["h2h"])

    assert snapshot.events
    assert snapshot.quotes == ()


def test_skips_suspended_or_invalid_prices() -> None:
    payload = _event_payload()
    outcomes = payload[0]["bookmakers"][0]["markets"][0]["outcomes"]  # type: ignore[index]
    outcomes.extend(  # type: ignore[union-attr]
        [
            {"name": "BC Lions", "price": 1},
            {"name": "Calgary Stampeders", "price": 0},
            {"name": "BC Lions", "price": None},
        ]
    )
    session = StubSession(StubResponse(payload))
    provider = OddsApiProvider(api_key="secret", session=session)  # type: ignore[arg-type]

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["h2h"])

    assert len(snapshot.quotes) == 4
    assert all(quote.decimal_odds > 1 for quote in snapshot.quotes)


def test_separates_three_way_draw_markets_from_two_way_moneylines() -> None:
    payload = _event_payload()
    bookmakers = payload[0]["bookmakers"]  # type: ignore[index]
    bookmakers[1]["markets"] = [  # type: ignore[index]
        {
            "key": "h2h",
            "last_update": "2026-08-30T15:58:00Z",
            "outcomes": [
                {"name": "BC Lions", "price": 2.8},
                {"name": "Draw", "price": 3.5},
                {"name": "Calgary Stampeders", "price": 2.6},
            ],
        }
    ]
    session = StubSession(StubResponse(payload))
    provider = OddsApiProvider(api_key="secret", session=session)  # type: ignore[arg-type]

    snapshot = provider.fetch_snapshot(["americanfootball_cfl"], ["h2h"])

    assert len(snapshot.quotes) == 5
    two_way = [quote for quote in snapshot.quotes if quote.sportsbook.name == "PlayNow"]
    three_way = [quote for quote in snapshot.quotes if quote.sportsbook.name == "Betway"]
    assert {quote.outcome.side for quote in two_way} == {OutcomeSide.HOME, OutcomeSide.AWAY}
    assert {quote.outcome.side for quote in three_way} == {
        OutcomeSide.HOME,
        OutcomeSide.DRAW,
        OutcomeSide.AWAY,
    }
    assert {quote.outcome.market.variant for quote in two_way} == {"two_way"}
    assert {quote.outcome.market.variant for quote in three_way} == {"three_way"}
