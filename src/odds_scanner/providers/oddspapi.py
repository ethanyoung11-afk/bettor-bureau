from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic, sleep
from typing import Any

import requests

from odds_scanner.domain import (
    Event,
    MarketKind,
    OddsSnapshot,
    OutcomeSide,
    Quote,
    Sportsbook,
    stable_id,
)
from odds_scanner.normalization import (
    FOOTBALL_MARKETS,
    IdentityResolver,
    MarketDefinition,
    MarketNormalizer,
    NormalizationError,
    canonical_token,
    decimal_value,
    make_sport_and_league,
)
from odds_scanner.providers.odds_api import FOOTBALL_LEAGUES, parse_timestamp

AMERICAN_FOOTBALL_SPORT_ID = 14
ODDSPAPI_SPORT_IDS: Mapping[str, int] = {
    "americanfootball_nfl": AMERICAN_FOOTBALL_SPORT_ID,
    "americanfootball_ncaaf": AMERICAN_FOOTBALL_SPORT_ID,
    "americanfootball_cfl": AMERICAN_FOOTBALL_SPORT_ID,
    "basketball_nba": 11,
    "icehockey_nhl": 15,
}
# OddsPapi exposes several similarly named preseason and postseason tournaments.
# Keep the primary competitions explicit so an arbitrary catalog order cannot replace
# CFL/NCAAF with an inactive sibling tournament.
ODDSPAPI_PRIMARY_TOURNAMENT_IDS: Mapping[str, int] = {
    "americanfootball_nfl": 31,
    "americanfootball_ncaaf": 27653,
    "americanfootball_cfl": 790,
    "basketball_nba": 132,
    "icehockey_nhl": 234,
}
ODDSPAPI_FOOTBALL_MARKETS = {*FOOTBALL_MARKETS, "player_props"}

PLAYER_PROP_LABELS: Mapping[str, str] = {
    "playertotals-passyards": "Passing yards",
    "playertotals-rushyards": "Rushing yards",
    "playertotals-receivingyards": "Receiving yards",
    "playertotals-receptions": "Receptions",
    "playertotals-tdpasses": "Passing touchdowns",
    "playertotals-interceptions": "Interceptions thrown",
    "players-td": "Anytime touchdown",
    "players-td-other": "Anytime touchdown",
    "players-firsttd": "First touchdown scorer",
    "players-secondtd": "Second touchdown scorer",
    "players-thirdtd": "Third touchdown scorer",
}

BOOK_DISPLAY_NAMES: Mapping[str, str] = {
    "bet365": "Bet365",
    "betmgm": "BetMGM",
    "betrivers": "BetRivers",
    "betway": "Betway",
    "caesars": "Caesars",
    "circasports": "Circa Sports",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "pinnacle": "Pinnacle",
    "playnow": "PlayNow",
    "william-hill": "William Hill",
    "william_hill": "William Hill",
}


class OddsPapiError(RuntimeError):
    pass


def _friendly_book_name(slug: str) -> str:
    known = BOOK_DISPLAY_NAMES.get(slug.lower())
    if known:
        return known
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)


def _league_key(tournament_name: str) -> str | None:
    token = canonical_token(tournament_name)
    if (
        token == "nfl"
        or token.startswith("nfl-regular")
        or ("national-football-league" in token and "women" not in token)
    ):
        return "americanfootball_nfl"
    if token == "cfl" or token == "canadian-football-league":
        return "americanfootball_cfl"
    if (
        token == "ncaaf"
        or token == "ncaa-regular-season"
        or "college-football" in token
        or token == "ncaa-football"
    ):
        return "americanfootball_ncaaf"
    if token == "nba" or "national-basketball-association" in token:
        return "basketball_nba"
    if token == "nhl" or "national-hockey-league" in token:
        return "icehockey_nhl"
    return None


def _full_game_market(raw_market: Mapping[str, Any]) -> str | None:
    if raw_market.get("playerProp"):
        return None
    period = canonical_token(str(raw_market.get("period", "")))
    name = canonical_token(str(raw_market.get("marketName", "")))
    market_type = canonical_token(str(raw_market.get("marketType", "")))
    full_game_periods = {"", "result", "fulltime", "full-time", "game", "match"}
    if period not in full_game_periods:
        return None
    if market_type in {"moneyline", "h2h", "1x2"} or any(
        token in name for token in ("moneyline", "match-winner", "full-time-result")
    ):
        outcome_tokens = {
            canonical_token(str(outcome.get("outcomeName", "")))
            for outcome in raw_market.get("outcomes", [])
            if isinstance(outcome, Mapping)
        }
        if market_type == "1x2" or outcome_tokens & {"x", "draw", "tie"}:
            return None
        if outcome_tokens and len(outcome_tokens) != 2:
            return None
        return "h2h"
    if market_type in {"spread", "spreads", "handicap", "point-spread"}:
        return "spreads"
    if market_type in {"total", "totals", "over-under"}:
        return "totals"
    return None


def _player_prop_definition(
    raw_market: Mapping[str, Any],
    player_name: str,
) -> MarketDefinition | None:
    if not raw_market.get("playerProp"):
        return None
    period = canonical_token(str(raw_market.get("period", "")))
    if period not in {"", "result", "fulltime", "full-time", "game", "match"}:
        return None
    outcome_tokens = {
        canonical_token(str(outcome.get("outcomeName", "")))
        for outcome in raw_market.get("outcomes", [])
        if isinstance(outcome, Mapping)
    }
    if outcome_tokens == {"over", "under"}:
        required_sides = (OutcomeSide.OVER, OutcomeSide.UNDER)
    elif outcome_tokens == {"yes", "no"}:
        required_sides = (OutcomeSide.YES, OutcomeSide.NO)
    else:
        return None
    market_type = canonical_token(str(raw_market.get("marketType", "")))
    stat_label = PLAYER_PROP_LABELS.get(market_type)
    if stat_label is None:
        stat_label = str(raw_market.get("marketName", "Player prop"))
        for removable in ("(incl. overtime)", "Over Under ", "Player "):
            stat_label = stat_label.replace(removable, "")
        stat_label = stat_label.strip() or "Player prop"
    return MarketDefinition(
        provider_key="player_props",
        kind=MarketKind.PLAYER_PROP,
        required_sides=required_sides,
        stat_key=stat_label,
        variant=player_name,
    )


def _supported_market(raw_market: Mapping[str, Any]) -> str | None:
    if raw_market.get("playerProp"):
        return "player_props" if _player_prop_definition(raw_market, "player") else None
    return _full_game_market(raw_market)


def _outcome_name(raw_name: str, market_key: str, event_home: str, event_away: str) -> str | None:
    token = canonical_token(raw_name)
    if market_key in {"h2h", "spreads"}:
        if token in {"1", "home", "participant-1", "team-1"}:
            return event_home
        if token in {"2", "away", "participant-2", "team-2"}:
            return event_away
        if token in {canonical_token(event_home), canonical_token(event_away)}:
            return raw_name
        return None
    if market_key in {"totals", "player_props"}:
        if token.startswith("over"):
            return "Over"
        if token.startswith("under"):
            return "Under"
        if token == "yes":
            return "Yes"
        if token == "no":
            return "No"
    return None


@dataclass(slots=True)
class OddsPapiProvider:
    api_key: str
    bookmaker_slugs: tuple[str, ...] = (
        "playnow",
        "betway",
        "pinnacle",
        "circasports",
        "bet365",
        "betmgm",
        "caesars",
        "draftkings",
        "fanduel",
        "betrivers",
    )
    include_all_bookmakers: bool = False
    include_schedule: bool = False
    bookmaker_cooldown_seconds: float = 1.6
    timeout_seconds: float = 30.0
    base_url: str = "https://api.oddspapi.io/v4"
    session: requests.Session = field(default_factory=requests.Session)
    tournament_ids: dict[str, int] = field(default_factory=dict)
    market_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    identity_resolver: IdentityResolver = field(default_factory=IdentityResolver)
    market_normalizer: MarketNormalizer = field(default_factory=MarketNormalizer)
    event_url_resolver: Callable[[Event], str | None] | None = None
    request_count: int = field(default=0, init=False)
    _last_request_at: float | None = field(default=None, init=False)

    @property
    def provider_id(self) -> str:
        return "oddspapi"

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        fetched_at = datetime.now(UTC)
        requested_leagues = tuple(dict.fromkeys(league_keys))
        requested_markets = tuple(dict.fromkeys(market_keys))
        unsupported_leagues = set(requested_leagues) - FOOTBALL_LEAGUES.keys()
        unsupported_markets = set(requested_markets) - ODDSPAPI_FOOTBALL_MARKETS
        if unsupported_leagues:
            raise OddsPapiError(f"Unsupported league keys: {sorted(unsupported_leagues)}")
        if unsupported_markets:
            raise OddsPapiError(f"Unsupported market keys: {sorted(unsupported_markets)}")
        if not requested_leagues or not requested_markets:
            raise OddsPapiError("Select at least one league and one market before refreshing.")
        requested_bookmakers = tuple(dict.fromkeys(self.bookmaker_slugs))
        if not self.include_all_bookmakers and not requested_bookmakers:
            raise OddsPapiError("Enable at least one sportsbook before refreshing.")

        self._ensure_tournaments(requested_leagues)
        self._ensure_market_catalog(requested_leagues)
        tournament_ids = [str(self.tournament_ids[key]) for key in requested_leagues]
        odds_params: dict[str, object] = {
            "tournamentIds": ",".join(tournament_ids),
            "language": "en",
            "verbosity": 3,
            "oddsFormat": "decimal",
        }
        raw_events: list[Mapping[str, Any]] = []
        if self.include_schedule:
            for tournament_id in tournament_ids:
                payload = self._request(
                    "fixtures",
                    {
                        "tournamentId": tournament_id,
                        "statusId": 0,
                        "language": "en",
                    },
                )
                raw_events.extend(self._event_list(payload))
        if self.include_all_bookmakers:
            payload = self._request("odds-by-tournaments", odds_params)
            raw_events.extend(self._event_list(payload))
        else:
            # Starter accounts accept exactly one bookmaker per request. Query every
            # configured book so consensus remains as broad as the available market.
            for bookmaker in requested_bookmakers:
                bookmaker_params = {**odds_params, "bookmaker": bookmaker}
                payload = self._request("odds-by-tournaments", bookmaker_params)
                raw_events.extend(self._event_list(payload))

        sports = {}
        leagues = {}
        events = {}
        quotes: list[Quote] = []
        league_by_tournament = {
            tournament_id: key for key, tournament_id in self.tournament_ids.items()
        }
        for raw_event in raw_events:
            tournament_id = int(raw_event.get("tournamentId", -1))
            league_key = league_by_tournament.get(tournament_id)
            if league_key not in requested_leagues:
                continue
            config = FOOTBALL_LEAGUES[league_key]
            sport, league = make_sport_and_league(
                config.sport_id,
                config.sport_name,
                config.league_id,
                config.league_name,
            )
            sports[sport.id] = sport
            leagues[league.id] = league
            event, event_quotes = self._normalize_event(
                raw_event,
                league,
                fetched_at,
                set(requested_markets),
            )
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

    def _ensure_tournaments(self, league_keys: Sequence[str]) -> None:
        missing = [key for key in league_keys if key not in self.tournament_ids]
        if not missing:
            return
        for sport_id in dict.fromkeys(ODDSPAPI_SPORT_IDS[key] for key in missing):
            payload = self._request(
                "tournaments",
                {"sportId": sport_id, "language": "en"},
            )
            for raw in self._event_list(payload):
                key = _league_key(str(raw.get("tournamentName", "")))
                if key and raw.get("tournamentId") is not None:
                    self.tournament_ids[key] = int(raw["tournamentId"])
        still_missing = [key for key in league_keys if key not in self.tournament_ids]
        if still_missing:
            labels = [FOOTBALL_LEAGUES[key].league_name for key in still_missing]
            raise OddsPapiError(f"OddsPapi did not return tournament IDs for: {', '.join(labels)}")

    def _ensure_market_catalog(self, league_keys: Sequence[str]) -> None:
        required_sport_ids = {ODDSPAPI_SPORT_IDS[key] for key in league_keys}
        cached_sport_ids = {
            int(raw.get("sportId", -1))
            for raw in self.market_catalog.values()
            if isinstance(raw, Mapping)
        }
        if required_sport_ids <= cached_sport_ids:
            return
        payload = self._request("markets", {"language": "en"})
        for raw in self._event_list(payload):
            if int(raw.get("sportId", -1)) not in set(ODDSPAPI_SPORT_IDS.values()):
                continue
            market_id = raw.get("marketId")
            if market_id is not None:
                self.market_catalog[str(market_id)] = dict(raw)
        loaded_sport_ids = {
            int(raw.get("sportId", -1))
            for raw in self.market_catalog.values()
            if isinstance(raw, Mapping)
        }
        missing_sports = required_sport_ids - loaded_sport_ids
        if missing_sports:
            raise OddsPapiError(
                "OddsPapi returned no market definitions for sport IDs: "
                + ", ".join(str(sport_id) for sport_id in sorted(missing_sports))
            )

    def _request(self, endpoint: str, params: Mapping[str, object]) -> object:
        request_params = {"apiKey": self.api_key}
        request_params.update({key: str(value) for key, value in params.items()})
        if self._last_request_at is not None and self.bookmaker_cooldown_seconds > 0:
            elapsed = monotonic() - self._last_request_at
            remaining = self.bookmaker_cooldown_seconds - elapsed
            if remaining > 0:
                sleep(remaining)
        self._last_request_at = monotonic()
        self.request_count += 1
        response = self.session.get(
            f"{self.base_url}/{endpoint}",
            params=request_params,
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:300]
            raise OddsPapiError(f"OddsPapi returned {response.status_code}: {detail}") from exc
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise OddsPapiError("OddsPapi returned an invalid JSON response.") from exc

    @staticmethod
    def _event_list(payload: object) -> list[Mapping[str, Any]]:
        candidate = payload
        if isinstance(payload, Mapping):
            candidate = payload.get("data", payload.get("fixtures", payload))
        if isinstance(candidate, Mapping):
            return [candidate]
        if not isinstance(candidate, list):
            raise OddsPapiError("OddsPapi response was not a list of records.")
        return [item for item in candidate if isinstance(item, Mapping)]

    def _normalize_event(
        self,
        raw: Mapping[str, Any],
        league: Any,
        fetched_at: datetime,
        requested_markets: set[str],
    ) -> tuple[Any, list[Quote]]:
        home_name = str(raw.get("participant1Name", "")).strip()
        away_name = str(raw.get("participant2Name", "")).strip()
        if not home_name or not away_name:
            raise NormalizationError("OddsPapi fixture is missing participant names")
        home = self.identity_resolver.participant(league, home_name)
        away = self.identity_resolver.participant(league, away_name)
        start_time = parse_timestamp(raw.get("startTime"), fetched_at)
        event = self.identity_resolver.event(league, start_time, home, away)
        source_event_id = str(raw.get("fixtureId", "")) or None
        result: list[Quote] = []

        raw_books = raw.get("bookmakerOdds", {})
        if not isinstance(raw_books, Mapping):
            return event, result
        for book_slug_value, raw_book_value in raw_books.items():
            if not isinstance(raw_book_value, Mapping):
                continue
            if raw_book_value.get("bookmakerIsActive") is False or raw_book_value.get("suspended"):
                continue
            book_slug = str(book_slug_value)
            sportsbook = Sportsbook(
                id=stable_id("sportsbook", self.provider_id, book_slug),
                name=_friendly_book_name(book_slug),
            )
            fixture_path = raw_book_value.get("fixturePath")
            source_url = str(fixture_path).strip() if fixture_path else None
            bookmaker_fixture_id = str(raw_book_value.get("bookmakerFixtureId") or "").strip()
            if not source_url and book_slug.lower() == "playnow" and bookmaker_fixture_id.isdigit():
                source_url = f"https://www.playnow.com/sports/sports/event/{bookmaker_fixture_id}"
            if (
                not source_url
                and book_slug.lower() == "playnow"
                and self.event_url_resolver is not None
            ):
                source_url = self.event_url_resolver(event)
            raw_markets = raw_book_value.get("markets", {})
            if not isinstance(raw_markets, Mapping):
                continue
            for market_id_value, raw_market_value in raw_markets.items():
                if (
                    not isinstance(raw_market_value, Mapping)
                    or raw_market_value.get("marketActive") is False
                ):
                    continue
                catalog = self.market_catalog.get(str(market_id_value))
                if not catalog:
                    continue
                provider_market_key = _supported_market(catalog)
                if provider_market_key not in requested_markets:
                    continue
                standard_definition = FOOTBALL_MARKETS.get(provider_market_key)
                handicap = catalog.get("handicap")
                outcome_catalog = {
                    str(item.get("outcomeId")): str(item.get("outcomeName", ""))
                    for item in catalog.get("outcomes", [])
                    if isinstance(item, Mapping)
                }
                raw_outcomes = raw_market_value.get("outcomes", {})
                if not isinstance(raw_outcomes, Mapping):
                    continue
                for outcome_id_value, raw_outcome_value in raw_outcomes.items():
                    if not isinstance(raw_outcome_value, Mapping):
                        continue
                    normalized_name = _outcome_name(
                        outcome_catalog.get(str(outcome_id_value), ""),
                        provider_market_key,
                        event.home.name,
                        event.away.name,
                    )
                    if normalized_name is None:
                        continue
                    players = raw_outcome_value.get("players", {})
                    if not isinstance(players, Mapping):
                        continue
                    for raw_price_value in players.values():
                        if (
                            not isinstance(raw_price_value, Mapping)
                            or raw_price_value.get("active") is False
                        ):
                            continue
                        if raw_price_value.get("mainLine") is False:
                            continue
                        try:
                            player_name = str(raw_price_value.get("playerName") or "").strip()
                            subject_id: str | None = None
                            if provider_market_key == "player_props":
                                if not player_name:
                                    continue
                                player = self.identity_resolver.player(league, player_name)
                                definition = _player_prop_definition(catalog, player.name)
                                if definition is None:
                                    continue
                                subject_id = player.id
                            else:
                                if player_name or standard_definition is None:
                                    continue
                                definition = standard_definition
                            point: object | None = handicap
                            if (
                                provider_market_key == "spreads"
                                and normalized_name == event.away.name
                            ):
                                point = -decimal_value(handicap)
                            outcome = self.market_normalizer.normalize_outcome(
                                event,
                                definition,
                                normalized_name,
                                point,
                                subject_id=subject_id,
                            )
                            price: Decimal = decimal_value(raw_price_value.get("price"))
                            if price <= Decimal("1"):
                                continue
                        except (NormalizationError, ValueError):
                            continue
                        betslip_value = raw_price_value.get("betslip")
                        betslip_url = str(betslip_value).strip() if betslip_value else None
                        result.append(
                            Quote(
                                provider_id=self.provider_id,
                                sportsbook=sportsbook,
                                outcome=outcome,
                                decimal_odds=price,
                                # An active snapshot verifies that this price is currently offered.
                                # OddsPapi's changedAt is the last price movement, which can be old
                                # even when a still-current line was just fetched.
                                source_updated_at=fetched_at,
                                observed_at=fetched_at,
                                source_event_id=source_event_id,
                                # A provider-supplied betslip URL is selection-specific. When it
                                # is absent, fixturePath still takes the user to the right event.
                                source_url=betslip_url or source_url,
                            )
                        )
        return event, result
