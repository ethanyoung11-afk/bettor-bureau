from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from odds_scanner.domain import MarketKind
from odds_scanner.providers.odds_api import FOOTBALL_LEAGUES
from odds_scanner.providers.oddspapi import ODDSPAPI_SPORT_IDS, OddsPapiProvider
from odds_scanner.providers.playnow import PlayNowEventResolver
from odds_scanner.refresh import (
    BudgetConfig,
    FreshnessConfig,
    OddsRefreshService,
    RefreshConfig,
    RefreshRequest,
    RefreshResultStatus,
)
from odds_scanner.storage.base import QuoteRepository
from odds_scanner.storage.postgres import PostgresQuoteRepository
from odds_scanner.storage.sqlite import SQLiteQuoteRepository

DEFAULT_MONTHLY_CREDIT_LIMIT = 250
DEFAULT_MONTHLY_CREDIT_RESERVE = 25
DEFAULT_LEAGUE_KEYS = tuple(FOOTBALL_LEAGUES)
DEFAULT_MARKET_KEYS = ("h2h", "spreads", "totals", "player_props")
MARKET_KINDS = {
    "h2h": MarketKind.MONEYLINE,
    "spreads": MarketKind.SPREAD,
    "totals": MarketKind.TOTAL,
    "player_props": MarketKind.PLAYER_PROP,
}


def _environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip()
    return ""


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _csv_setting(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    configured = _environment_value(name)
    if not configured:
        return defaults
    return tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))


def _integer_setting(name: str, default: int) -> int:
    configured = _environment_value(name)
    if not configured:
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc


def repository_from_environment() -> QuoteRepository:
    database_url = _environment_value("DATABASE_URL")
    if database_url:
        return PostgresQuoteRepository(database_url)
    return SQLiteQuoteRepository(_environment_value("ODDS_DB_PATH") or "odds_scanner.db")


def requests_used_this_month(
    repository: QuoteRepository,
    *,
    as_of: datetime | None = None,
) -> int:
    effective_time = as_of or datetime.now(UTC)
    settings = repository.load_settings()
    if settings.get("oddspapi_usage_month") != effective_time.strftime("%Y-%m"):
        return 0
    try:
        return max(0, int(settings.get("oddspapi_requests_used", "0")))
    except ValueError:
        return 0


def record_requests(
    repository: QuoteRepository,
    request_count: int,
    *,
    as_of: datetime | None = None,
) -> int:
    effective_time = as_of or datetime.now(UTC)
    used = requests_used_this_month(repository, as_of=effective_time) + max(0, request_count)
    repository.save_setting("oddspapi_usage_month", effective_time.strftime("%Y-%m"))
    repository.save_setting("oddspapi_requests_used", str(used))
    return used


def estimated_request_count(
    provider: OddsPapiProvider,
    league_keys: tuple[str, ...],
) -> int:
    missing_sports = {
        ODDSPAPI_SPORT_IDS[key]
        for key in league_keys
        if key not in provider.tournament_ids
    }
    discovery_requests = len(missing_sports) + (0 if provider.market_catalog else 1)
    return discovery_requests + 1


def build_refresh_request(
    league_keys: tuple[str, ...],
    market_keys: tuple[str, ...],
) -> RefreshRequest:
    unknown_leagues = set(league_keys) - FOOTBALL_LEAGUES.keys()
    unknown_markets = set(market_keys) - MARKET_KINDS.keys()
    if unknown_leagues:
        raise ValueError(f"Unsupported REFRESH_LEAGUES: {sorted(unknown_leagues)}")
    if unknown_markets:
        raise ValueError(f"Unsupported REFRESH_MARKETS: {sorted(unknown_markets)}")
    return RefreshRequest(
        league_keys=league_keys,
        league_ids=tuple(FOOTBALL_LEAGUES[key].league_id for key in league_keys),
        market_keys=market_keys,
        market_kinds=tuple(MARKET_KINDS[key] for key in market_keys),
        trigger_type="automated",
    )


def main() -> int:
    api_key = _environment_value("ODDSPAPI_API_KEY")
    if not api_key:
        raise RuntimeError("ODDSPAPI_API_KEY is required")

    repository = repository_from_environment()
    settings = repository.load_settings()
    league_keys = _csv_setting("REFRESH_LEAGUES", DEFAULT_LEAGUE_KEYS)
    market_keys = _csv_setting("REFRESH_MARKETS", DEFAULT_MARKET_KEYS)
    request = build_refresh_request(league_keys, market_keys)
    provider = OddsPapiProvider(
        api_key=api_key,
        include_all_bookmakers=True,
        tournament_ids={
            str(key): int(value)
            for key, value in _json_object(settings.get("oddspapi_tournament_ids")).items()
        },
        market_catalog={
            str(key): dict(value)
            for key, value in _json_object(settings.get("oddspapi_market_catalog")).items()
            if isinstance(value, dict)
        },
        event_url_resolver=PlayNowEventResolver().resolve,
    )

    credit_limit = _integer_setting(
        "ODDSPAPI_MONTHLY_CREDIT_LIMIT", DEFAULT_MONTHLY_CREDIT_LIMIT
    )
    credit_reserve = _integer_setting(
        "ODDSPAPI_MONTHLY_CREDIT_RESERVE", DEFAULT_MONTHLY_CREDIT_RESERVE
    )
    repository.save_setting("oddspapi_monthly_credit_limit", str(credit_limit))
    repository.save_setting("oddspapi_monthly_credit_reserve", str(credit_reserve))
    used_before = requests_used_this_month(repository)
    estimated = estimated_request_count(provider, league_keys)
    if used_before + estimated > max(0, credit_limit - credit_reserve):
        print(
            "Refresh skipped: monthly API reserve protected "
            f"({used_before} used, approximately {estimated} needed, {credit_reserve} reserved)."
        )
        return 0

    diagnostics = OddsRefreshService(
        provider=provider,
        repository=repository,
        config=RefreshConfig(
            manual_only=False,
            minimum_ev=Decimal("0.02"),
            freshness=FreshnessConfig(fresh_minutes=5, warning_minutes=15, stale_minutes=30),
            budget=BudgetConfig(
                monthly_credit_limit=credit_limit,
                monthly_credit_reserve=credit_reserve,
            ),
        ),
    ).refresh(request)

    repository.save_setting(
        "oddspapi_tournament_ids",
        json.dumps(provider.tournament_ids, separators=(",", ":")),
    )
    repository.save_setting(
        "oddspapi_market_catalog",
        json.dumps(provider.market_catalog, separators=(",", ":")),
    )
    used_after = record_requests(repository, provider.request_count)
    print(
        f"{diagnostics.status.value}: {diagnostics.events_checked} events, "
        f"{diagnostics.sportsbooks_checked} books, {diagnostics.quotes_stored} prices, "
        f"{provider.request_count} API requests, {used_after}/{credit_limit} monthly calls used."
    )
    return 1 if diagnostics.status is RefreshResultStatus.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
