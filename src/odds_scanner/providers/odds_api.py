from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import requests

from odds_scanner.domain import OddsSnapshot, Quote, Sportsbook, stable_id
from odds_scanner.normalization import (
    FOOTBALL_MARKETS,
    IdentityResolver,
    MarketNormalizer,
    NormalizationError,
    decimal_value,
    make_sport_and_league,
)


@dataclass(frozen=True, slots=True)
class LeagueConfig:
    provider_key: str
    sport_id: str
    sport_name: str
    league_id: str
    league_name: str


FOOTBALL_LEAGUES: Mapping[str, LeagueConfig] = {
    "americanfootball_nfl": LeagueConfig(
        "americanfootball_nfl", "american-football", "American Football", "nfl", "NFL"
    ),
    "americanfootball_ncaaf": LeagueConfig(
        "americanfootball_ncaaf", "american-football", "American Football", "ncaaf", "NCAAF"
    ),
    "americanfootball_cfl": LeagueConfig(
        "americanfootball_cfl", "american-football", "American Football", "cfl", "CFL"
    ),
    "basketball_nba": LeagueConfig("basketball_nba", "basketball", "Basketball", "nba", "NBA"),
    "icehockey_nhl": LeagueConfig("icehockey_nhl", "ice-hockey", "Ice Hockey", "nhl", "NHL"),
}


class OddsApiError(RuntimeError):
    pass


def parse_timestamp(value: object, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizationError(f"Invalid provider timestamp: {value!r}") from exc
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class OddsApiProvider:
    api_key: str
    regions: str = "ca,us,uk,eu"
    odds_format: str = "decimal"
    timeout_seconds: float = 20.0
    base_url: str = "https://api.the-odds-api.com/v4"
    session: requests.Session = field(default_factory=requests.Session)
    identity_resolver: IdentityResolver = field(default_factory=IdentityResolver)
    market_normalizer: MarketNormalizer = field(default_factory=MarketNormalizer)
    request_count: int = field(default=0, init=False)
    quota_used: int | None = field(default=None, init=False)
    quota_remaining: int | None = field(default=None, init=False)
    last_request_cost: int | None = field(default=None, init=False)

    @property
    def provider_id(self) -> str:
        return "the-odds-api"

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        fetched_at = datetime.now(UTC)
        sports = {}
        leagues = {}
        events = {}
        quotes: list[Quote] = []

        unsupported_markets = set(market_keys) - FOOTBALL_MARKETS.keys()
        if unsupported_markets:
            raise OddsApiError(f"Unsupported market keys: {sorted(unsupported_markets)}")

        for league_key in league_keys:
            config = FOOTBALL_LEAGUES.get(league_key)
            if config is None:
                raise OddsApiError(f"Unsupported league key: {league_key}")
            sport, league = make_sport_and_league(
                config.sport_id,
                config.sport_name,
                config.league_id,
                config.league_name,
            )
            sports[sport.id] = sport
            leagues[league.id] = league
            payload = self._request_league(config.provider_key, market_keys)
            for raw_event in payload:
                event, event_quotes = self._normalize_event(raw_event, league, fetched_at)
                events[event.id] = event
                quotes.extend(event_quotes)

        return OddsSnapshot(
            provider_id=self.provider_id,
            sports=tuple(sports.values()),
            leagues=tuple(leagues.values()),
            events=tuple(events.values()),
            quotes=tuple(quotes),
            fetched_at=fetched_at,
        )

    def _request_league(
        self,
        league_key: str,
        market_keys: Sequence[str],
    ) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/sports/{league_key}/odds",
            params={
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": ",".join(market_keys),
                "oddsFormat": self.odds_format,
                "dateFormat": "iso",
            },
            timeout=self.timeout_seconds,
        )
        self.request_count += 1
        self.quota_used = _header_integer(response.headers, "x-requests-used")
        self.quota_remaining = _header_integer(response.headers, "x-requests-remaining")
        self.last_request_cost = _header_integer(response.headers, "x-requests-last")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:300]
            raise OddsApiError(f"The Odds API returned {response.status_code}: {detail}") from exc
        payload = response.json()
        if not isinstance(payload, list):
            raise OddsApiError("The Odds API response was not an event list")
        return payload

    def _normalize_event(self, raw: Mapping[str, Any], league: Any, fetched_at: datetime) -> Any:
        home = self.identity_resolver.participant(league, str(raw["home_team"]))
        away = self.identity_resolver.participant(league, str(raw["away_team"]))
        start_time = parse_timestamp(raw.get("commence_time"), fetched_at)
        event = self.identity_resolver.event(league, start_time, home, away)
        result: list[Quote] = []

        for raw_book in raw.get("bookmakers", []):
            book_key = str(raw_book["key"])
            sportsbook = Sportsbook(
                id=stable_id("sportsbook", self.provider_id, book_key),
                name=str(raw_book.get("title", book_key)),
            )
            for raw_market in raw_book.get("markets", []):
                definition = FOOTBALL_MARKETS.get(str(raw_market.get("key")))
                if definition is None:
                    continue
                source_timestamp = raw_market.get("last_update") or raw_book.get("last_update")
                if not source_timestamp:
                    # Never manufacture freshness for a recommendation-bearing price.
                    continue
                market_updated = parse_timestamp(source_timestamp, fetched_at)
                for raw_outcome in raw_market.get("outcomes", []):
                    outcome = self.market_normalizer.normalize_outcome(
                        event,
                        definition,
                        str(raw_outcome["name"]),
                        raw_outcome.get("point"),
                    )
                    price: Decimal = decimal_value(raw_outcome["price"])
                    result.append(
                        Quote(
                            provider_id=self.provider_id,
                            sportsbook=sportsbook,
                            outcome=outcome,
                            decimal_odds=price,
                            source_updated_at=market_updated,
                            observed_at=fetched_at,
                            source_event_id=str(raw.get("id", "")) or None,
                            source_url=(
                                str(
                                    raw_outcome.get("link")
                                    or raw_market.get("link")
                                    or raw_book.get("link")
                                    or ""
                                )
                                or None
                            ),
                        )
                    )
        return event, result


def _header_integer(response_headers: Mapping[str, Any], name: str) -> int | None:
    value = response_headers.get(name)
    if value is None:
        return None
    try:
        return max(0, int(str(value)))
    except ValueError:
        return None
