from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import unquote as url_unquote
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from odds_scanner.analytics import (
    MiddleOpportunity,
    ValueOpportunity,
    audit_consensus_value,
    best_value_by_outcome,
    opportunities_from_value_audit,
)
from odds_scanner.domain import (
    ArbitrageOpportunity,
    BetStatus,
    Event,
    MarketKey,
    MarketKind,
    OutcomeSide,
    Quote,
    TrackedBet,
    stable_id,
)
from odds_scanner.opportunities import (
    best_prices,
    deduplicate_quotes,
    implied_probability,
    is_fresh,
)
from odds_scanner.presentation import decimal_to_american, format_odds
from odds_scanner.providers.base import OddsProvider
from odds_scanner.providers.demo import DemoOddsProvider, generate_demo_snapshots
from odds_scanner.providers.odds_api import FOOTBALL_LEAGUES, OddsApiProvider
from odds_scanner.providers.oddspapi import (
    ODDSPAPI_PRIMARY_TOURNAMENT_IDS,
    OddsPapiProvider,
)
from odds_scanner.providers.playnow import PlayNowEventResolver
from odds_scanner.refresh import (
    BudgetConfig,
    FreshnessConfig,
    OddsRefreshService,
    RefreshConfig,
    RefreshDiagnostics,
    RefreshRequest,
    RefreshResultStatus,
    freshness_state,
)
from odds_scanner.storage.base import QuoteRepository
from odds_scanner.storage.sqlite import SQLiteQuoteRepository
from odds_scanner.strategy import (
    OFFICIAL_MAXIMUM_AMERICAN_ODDS,
    OFFICIAL_MINIMUM_AMERICAN_ODDS,
    OFFICIAL_MINIMUM_BREAK_EVEN_PROBABILITY,
    OFFICIAL_MINIMUM_EV,
    OFFICIAL_MINIMUM_REFERENCE_BOOKS,
    OFFICIAL_STARTING_BANKROLL_UNITS,
    OFFICIAL_UNIT_VALUE_DOLLARS,
    select_official_recommendations,
)
from odds_scanner.strategy import official_bets as strategy_official_bets

LEAGUE_LABELS = {config.league_name: key for key, config in FOOTBALL_LEAGUES.items()}
LEAGUE_IDS = {config.league_name: config.league_id for config in FOOTBALL_LEAGUES.values()}
MARKET_LABELS = {
    "Moneyline": "h2h",
    "Spread": "spreads",
    "Total": "totals",
    "Player props": "player_props",
}
DATA_SOURCE_IDS = {
    "Demo": "demo",
    "OddsPapi Free": "oddspapi",
    "The Odds API": "the-odds-api",
}
PRIORITY_BOOKS = ("PlayNow", "Betway")
SPORTSBOOK_URLS = {
    "PlayNow": "https://www.playnow.com/sports/sports/matches",
    "Betway": "https://betway.com/g/en/sports",
    "Bet365": "https://www.bet365.ca/",
    "BetMGM": "https://sports.betmgm.ca/en/sports",
    "BetRivers": "https://www.betrivers.com/",
    "Caesars": "https://www.caesars.com/sportsbook-and-casino",
    "Circa Sports": "https://www.circasports.com/",
    "DraftKings": "https://sportsbook.draftkings.com/",
    "FanDuel": "https://sportsbook.fanduel.com/",
    "Pinnacle": "https://www.pinnacle.com/en/betting-sports",
}
SPORTSBOOK_DOMAINS = {
    "PlayNow": ("playnow.com",),
    "Betway": ("betway.com",),
    "Bet365": ("bet365.ca", "bet365.com"),
    "BetMGM": ("betmgm.ca", "betmgm.com"),
    "BetRivers": ("betrivers.com",),
    "Caesars": ("caesars.com",),
    "Circa Sports": ("circasports.com",),
    "DraftKings": ("draftkings.com",),
    "FanDuel": ("fanduel.com",),
    "Pinnacle": ("pinnacle.com",),
}
ODDSPAPI_FREE_CREDITS = 250
EV_INITIAL_BATCH_SIZE = 10
EV_BATCH_SIZE = 10
ODDSPAPI_BOOK_SLUGS = {
    "PlayNow": "playnow",
    "Betway": "betway",
    "Pinnacle": "pinnacle",
    "Circa Sports": "circasports",
    "Bet365": "bet365",
    "BetMGM": "betmgm",
    "Caesars": "caesars",
    "DraftKings": "draftkings",
    "FanDuel": "fanduel",
    "BetRivers": "betrivers",
}
STARTER_BOOKS = (
    *PRIORITY_BOOKS,
    "Pinnacle",
    "Circa Sports",
    "Bet365",
    "BetMGM",
    "Caesars",
    "DraftKings",
    "FanDuel",
    "BetRivers",
)
MARKET_NAMES = {
    MarketKind.MONEYLINE: "Moneyline",
    MarketKind.SPREAD: "Spread",
    MarketKind.TOTAL: "Total",
    MarketKind.PLAYER_PROP: "Player prop",
}
MARKET_KINDS = {
    "h2h": MarketKind.MONEYLINE,
    "spreads": MarketKind.SPREAD,
    "totals": MarketKind.TOTAL,
    "player_props": MarketKind.PLAYER_PROP,
}
LEAGUE_ICONS = {"NFL": "🏈", "NCAAF": "🏈", "NBA": "🏀", "NHL": "🏒", "CFL": "🏈"}
LEAGUE_SPORTS = {
    "nfl": "Football",
    "ncaaf": "Football",
    "cfl": "Football",
    "nba": "Basketball",
    "nhl": "Hockey",
}
CORE_REFRESH_LEAGUES = ("NFL", "NCAAF", "CFL", "NBA", "NHL")
DISPLAY_TIMEZONE = ZoneInfo("America/Vancouver")
DEFAULT_ODDS_FORMAT = "Decimal"
SCHEDULE_REFRESH_INTERVAL = timedelta(days=7)
RECOMMENDED_MINIMUM_EV = OFFICIAL_MINIMUM_EV
RECOMMENDED_MINIMUM_IMPLIED_PROBABILITY = OFFICIAL_MINIMUM_BREAK_EVEN_PROBABILITY
RECOMMENDED_MINIMUM_AMERICAN_ODDS = OFFICIAL_MINIMUM_AMERICAN_ODDS
RECOMMENDED_MAXIMUM_AMERICAN_ODDS = OFFICIAL_MAXIMUM_AMERICAN_ODDS
RECOMMENDED_MINIMUM_REFERENCE_BOOKS = OFFICIAL_MINIMUM_REFERENCE_BOOKS
_DEMO_SEED_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class EVFilterState:
    league_id: str | None
    market_kind: MarketKind | None
    minimum_ev: Decimal
    my_books: tuple[str, ...]
    minimum_implied_probability: Decimal
    minimum_american_odds: int | None
    maximum_american_odds: int | None
    minimum_consensus_books: int
    starts_before: datetime | None
    fresh_only: bool
    sort_by: str


@dataclass(frozen=True, slots=True)
class OfficialPerformance:
    wins: int
    losses: int
    voids: int
    pending: int
    units: Decimal
    roi: Decimal
    bankroll: Decimal


def _provider_id(mode: str) -> str:
    return DATA_SOURCE_IDS.get(mode, "demo")


def _book_sort_key(book: str) -> tuple[int, str]:
    try:
        return PRIORITY_BOOKS.index(book), book.lower()
    except ValueError:
        return len(PRIORITY_BOOKS), book.lower()


def _logo_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


@lru_cache(maxsize=1)
def _bundled_team_logo_catalogs() -> dict[str, dict[str, str]]:
    """Read the bundled ESPN logo index without blocking a page render on HTTP."""
    catalog_path = Path(__file__).resolve().parents[2] / "assets" / "team_logos.json"
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(league).casefold(): {
            str(name): str(url)
            for name, url in logos.items()
            if isinstance(name, str) and isinstance(url, str)
        }
        for league, logos in raw.items()
        if isinstance(logos, dict)
    }


@lru_cache(maxsize=8)
def _team_logo_catalog(league_id: str) -> dict[str, str]:
    return _bundled_team_logo_catalogs().get(league_id.casefold(), {})


@lru_cache(maxsize=8)
def _asset_data_uri(path: str) -> str:
    asset = Path(path)
    mime = "image/png" if asset.suffix.casefold() == ".png" else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(asset.read_bytes()).decode('ascii')}"


def _team_logo_url(team_name: str, league_id: str) -> str | None:
    return _team_logo_catalog(league_id).get(_logo_key(team_name))


def _team_logo_markup(team_name: str, league_id: str) -> str:
    initials = "".join(part[0] for part in team_name.split()[:2] if part) or "•"
    logo_url = _team_logo_url(team_name, league_id)
    if logo_url:
        return (
            '<span class="ev-team-logo has-logo">'
            f'<img src="{html.escape(logo_url, quote=True)}" alt="" loading="lazy">'
            "</span>"
        )
    return f'<span class="ev-team-logo"><span>{html.escape(initials.upper())}</span></span>'


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _local_secret(name: str) -> str:
    environment_value = os.getenv(name, "").strip()
    if environment_value:
        return environment_value
    try:
        hosted_value = str(st.secrets.get(name, "")).strip()
    except (FileNotFoundError, OSError):
        hosted_value = ""
    if hosted_value:
        return hosted_value
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _password_matches(candidate: str, expected_hash: str) -> bool:
    candidate_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return bool(expected_hash) and hmac.compare_digest(candidate_hash, expected_hash.lower())


def _owner_access() -> bool:
    shared_mode = _local_secret("SHARED_APP").casefold() in {"1", "true", "yes", "on"}
    if not shared_mode:
        return True
    return bool(st.session_state.get("owner_authenticated", False))


@lru_cache(maxsize=4)
def _repository_for(database_url: str, sqlite_path: str) -> QuoteRepository:
    if database_url:
        from odds_scanner.storage.postgres import PostgresQuoteRepository

        return PostgresQuoteRepository(database_url)
    return SQLiteQuoteRepository(Path(sqlite_path))


def _invalidate_repository_caches() -> None:
    st.session_state.pop("runtime_settings_cache", None)
    for key in tuple(st.session_state):
        if str(key).startswith("admin_status_cache_"):
            st.session_state.pop(key, None)


def _cached_settings(repository: QuoteRepository, *, ttl_seconds: int = 60) -> dict[str, str]:
    """Avoid repeated remote database round trips during ordinary widget reruns."""
    now = datetime.now(UTC)
    cached = st.session_state.get("runtime_settings_cache")
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and isinstance(cached[0], datetime)
        and isinstance(cached[1], dict)
        and now - cached[0] < timedelta(seconds=ttl_seconds)
    ):
        return dict(cached[1])
    settings = repository.load_settings()
    st.session_state["runtime_settings_cache"] = (now, settings)
    return dict(settings)


def _schedule_refresh_due(
    settings: dict[str, str],
    *,
    as_of: datetime | None = None,
) -> bool:
    current = as_of or datetime.now(UTC)
    try:
        refreshed_at = datetime.fromisoformat(settings.get("oddspapi_schedule_refreshed_at", ""))
        if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
            return True
    except ValueError:
        return True
    return current - refreshed_at.astimezone(UTC) >= SCHEDULE_REFRESH_INTERVAL


@st.fragment(run_every=60)  # type: ignore[untyped-decorator]
def _watch_for_shared_odds_updates(
    repository: QuoteRepository,
    provider_id: str,
) -> None:
    """Redraw the board only after the central worker stores a newer snapshot."""
    now = datetime.now(UTC)
    checked_key = f"shared_snapshot_checked_at_{provider_id}"
    last_checked = st.session_state.get(checked_key)
    if isinstance(last_checked, datetime) and now - last_checked < timedelta(seconds=55):
        return
    st.session_state[checked_key] = now
    latest = repository.api_usage_summary(provider_id, as_of=now).last_successful_refresh
    signature = latest.isoformat() if latest else "never"
    state_key = f"shared_snapshot_signature_{provider_id}"
    previous = st.session_state.get(state_key)
    st.session_state[state_key] = signature
    if previous is not None and previous != signature:
        st.session_state.pop(f"view_snapshot_{provider_id}", None)
        st.rerun()


def _load_view_snapshot(
    repository: QuoteRepository,
    provider_id: str,
) -> tuple[tuple[Quote, ...], tuple[Event, ...]]:
    """Reuse immutable view data until the shared refresh signature changes."""
    state_key = f"view_snapshot_{provider_id}"
    cached = st.session_state.get(state_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        quotes, events = cached
        if isinstance(quotes, tuple) and isinstance(events, tuple):
            return quotes, events
    snapshot = (
        repository.load_latest_quotes(provider_id),
        repository.load_events(provider_id),
    )
    st.session_state[state_key] = snapshot
    return snapshot


def _invalidate_view_snapshot(provider_id: str) -> None:
    st.session_state.pop(f"view_snapshot_{provider_id}", None)
    st.session_state.pop(f"shared_snapshot_checked_at_{provider_id}", None)
    st.session_state.pop("value_opportunity_cache", None)


def _value_opportunities_for_books(
    quotes: tuple[Quote, ...],
    sportsbook_names: tuple[str, ...],
) -> tuple[ValueOpportunity, ...]:
    """Reuse consensus analysis while users move between views and unchanged filters."""
    latest_observation = max(
        (quote.observed_at for quote in quotes),
        default=datetime.min.replace(tzinfo=UTC),
    )
    cache_key = (
        len(quotes),
        latest_observation,
        tuple(sorted(sportsbook_names)),
    )
    cache = st.session_state.setdefault("value_opportunity_cache", {})
    if isinstance(cache, dict):
        cached = cache.get(cache_key)
        if isinstance(cached, tuple):
            return cached
    else:
        cache = {}
        st.session_state["value_opportunity_cache"] = cache

    audit = audit_consensus_value(
        quotes,
        as_of=latest_observation,
        max_age=timedelta(0),
        candidate_sportsbooks=sportsbook_names,
        include_stale=True,
    )
    values = opportunities_from_value_audit(audit, minimum_ev=Decimal("0"))
    if len(cache) >= 8:
        cache.pop(next(iter(cache)))
    cache[cache_key] = values
    return values


def _oddspapi_requests_used(
    repository: QuoteRepository,
    *,
    as_of: datetime | None = None,
) -> int:
    effective_time = as_of or datetime.now(UTC)
    settings = _cached_settings(repository)
    if settings.get("oddspapi_usage_month") != effective_time.strftime("%Y-%m"):
        return 0
    try:
        return max(0, int(settings.get("oddspapi_requests_used", "0")))
    except ValueError:
        return 0


def _oddspapi_credit_limit(repository: QuoteRepository) -> int:
    configured = _local_secret("ODDSPAPI_MONTHLY_CREDIT_LIMIT")
    if not configured:
        configured = _cached_settings(repository).get("oddspapi_monthly_credit_limit", "")
    try:
        return max(1, int(configured))
    except ValueError:
        return ODDSPAPI_FREE_CREDITS


def _record_oddspapi_requests(
    repository: QuoteRepository,
    request_count: int,
    *,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    effective_time = as_of or datetime.now(UTC)
    used = _oddspapi_requests_used(repository, as_of=effective_time) + max(0, request_count)
    credit_limit = _oddspapi_credit_limit(repository)
    repository.save_setting("oddspapi_usage_month", effective_time.strftime("%Y-%m"))
    repository.save_setting("oddspapi_requests_used", str(used))
    _invalidate_repository_caches()
    return used, max(0, credit_limit - used)


def _refresh_config(mode: str) -> RefreshConfig:
    fresh_minutes = int(st.session_state.get("freshness_minutes", 5))
    warning_minutes = max(15, fresh_minutes)
    return RefreshConfig(
        manual_only=True,
        minimum_ev=Decimal(str(st.session_state.get("min_ev", 2.0))) / Decimal("100"),
        freshness=FreshnessConfig(
            fresh_minutes=fresh_minutes,
            warning_minutes=warning_minutes,
            stale_minutes=max(30, warning_minutes),
        ),
        budget=BudgetConfig(
            monthly_credit_limit=ODDSPAPI_FREE_CREDITS if mode == "OddsPapi Free" else None
        ),
        show_stale_recommendations=True,
    )


def _diagnostic_message(diagnostics: RefreshDiagnostics) -> str:
    if diagnostics.status is RefreshResultStatus.ALREADY_RUNNING:
        return "Odds refresh already in progress."
    if diagnostics.status is RefreshResultStatus.FAILED:
        return f"Odds update failed: {diagnostics.error_message or 'Unknown provider error'}"
    return (
        f"Refresh complete · {diagnostics.events_checked} events · "
        f"{diagnostics.sportsbooks_checked} books · "
        f"{diagnostics.new_opportunities} new +EV · "
        f"{diagnostics.deactivated_opportunities} removed"
    )


def _render_refresh_admin_status(
    repository: QuoteRepository,
    mode: str,
) -> None:
    provider_id = _provider_id(mode)
    now = datetime.now(UTC)
    cache_key = f"admin_status_cache_{provider_id}"
    cached = st.session_state.get(cache_key)
    if (
        isinstance(cached, tuple)
        and len(cached) == 3
        and isinstance(cached[0], datetime)
        and now - cached[0] < timedelta(seconds=60)
    ):
        counts, usage = cached[1], cached[2]
    else:
        counts = repository.opportunity_counts(provider_id)
        usage = repository.api_usage_summary(provider_id, as_of=now)
        st.session_state[cache_key] = (now, counts, usage)
    last_refresh = usage.last_successful_refresh
    last_refresh_label = (
        last_refresh.astimezone(DISPLAY_TIMEZONE).strftime("%I:%M %p").lstrip("0")
        if last_refresh
        else "Not yet"
    )
    st.markdown(f"**Current recommendations**  \n{counts.active} active · {counts.stale} stale")
    st.markdown(f"**Last successful refresh**  \n{last_refresh_label}")
    if usage.last_failed_refresh is not None:
        failed_label = usage.last_failed_refresh.astimezone(DISPLAY_TIMEZONE).strftime(
            "%b %d, %I:%M %p"
        )
        st.caption(f"Last failed update: {failed_label}")
    latest = st.session_state.get("last_refresh_diagnostics")
    if isinstance(latest, RefreshDiagnostics):
        if latest.status is RefreshResultStatus.SUCCESS:
            st.success("Refresh successful")
            st.caption(
                f"{latest.events_checked} events · {latest.sportsbooks_checked} books · "
                f"{latest.new_opportunities} new +EV · "
                f"{latest.revalidated_opportunities} revalidated · "
                f"{latest.deactivated_opportunities} removed · "
                f"{latest.credits_used} credits · {latest.duration_seconds:.1f}s"
            )
        elif latest.status is RefreshResultStatus.FAILED:
            st.error("Odds update failed")
            st.caption(latest.error_message or "The provider did not return fresh odds.")


def _inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root { --terminal-green: #39d98a; --terminal-amber: #ffb547; --panel: #111827; }
        .stApp { background: #070b12; }
        [data-testid="stHeader"] { background: rgba(7,11,18,.88); }
        [data-testid="stAppDeployButton"], [data-testid="stMainMenu"],
        [data-testid="stDecoration"] { display:none !important; }
        [data-testid="stSidebar"] { background: #0b111c; border-right: 1px solid #1f2937; }
        .block-container { padding: .65rem 1.2rem 1.5rem; max-width: 1800px; }
        h1, h2, h3 { letter-spacing: -.025em; margin-top: .3rem; margin-bottom: .35rem; }
        h1 { font-size: 2rem; }
        h2 { font-size: 1.35rem; }
        h3 { font-size: 1.15rem; }
        [data-testid="stCaptionContainer"] { margin-bottom: .2rem; }
        [data-testid="stVerticalBlock"] { gap: .65rem; }
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .45rem; }
        [data-testid="stSidebar"] hr { margin: .45rem 0; }
        div[data-testid="stMetric"] {
            background: linear-gradient(145deg, #101827, #0b111c);
            border: 1px solid #253149; border-radius: 10px; padding: 9px 12px;
        }
        div[data-testid="stMetricLabel"] { font-size: .78rem; }
        div[data-testid="stMetricValue"] { font-size: 1.55rem; }
        div[data-testid="stMetricValue"] { color: var(--terminal-green); font-family: monospace; }
        .terminal-badge {
            display:inline-block; padding:4px 9px; border-radius:999px; font-size:.73rem;
            letter-spacing:.08em; font-weight:700; background:#123525; color:#6ee7b7;
            border:1px solid #1f6b48; margin-bottom:.2rem;
        }
        .demo-badge { background:#382a12; color:#ffd180; border-color:#785c24; }
        .section-kicker { color:#8290a8; text-transform:uppercase; letter-spacing:.12em;
            font-size:.72rem; font-weight:700; margin-bottom:.2rem; }
        .st-key-header_odds_status [data-testid="stAlert"] {
            border-radius:12px; padding:.7rem .8rem;
        }
        .st-key-header_odds_status [data-testid="stAlert"] p {
            font-size:.78rem; line-height:1.3;
        }
        .st-key-header_dashboard {
            margin:.2rem 0 .35rem;
        }
        .st-key-header_dashboard div[data-testid="stMetric"] {
            min-height:72px; padding:.65rem .9rem;
            background:linear-gradient(135deg, #111a29, #0c1420);
            border-color:#28364b;
        }
        .st-key-header_dashboard div[data-testid="stMetricValue"] {
            font-size:1.35rem;
        }
        .st-key-top_opportunity {
            background:
                radial-gradient(circle at 88% 5%, rgba(57,217,138,.16), transparent 34%),
                linear-gradient(145deg, #101a2b 0%, #0b121f 58%, #0c1718 100%);
            border: 1px solid #2b8a60 !important;
            border-radius: 16px;
            padding: 1rem 1.15rem 1.1rem;
            box-shadow: 0 14px 38px rgba(0,0,0,.28), inset 0 1px rgba(255,255,255,.03);
            margin: .45rem 0 1rem;
        }
        .st-key-top_opportunity .best-bet-badge {
            display:inline-flex; align-items:center; padding:4px 9px; border-radius:999px;
            background:#153d2d; border:1px solid #2a8a5d; color:#77efb2;
            font-size:.7rem; font-weight:800; letter-spacing:.12em; margin-bottom:.45rem;
        }
        .st-key-top_opportunity .best-bet-pick {
            color:#f8fafc; font-size:1.65rem; line-height:1.12; font-weight:850;
            letter-spacing:-.035em; margin:.05rem 0 .28rem;
        }
        .st-key-top_opportunity .best-bet-event {
            color:#aeb9ca; font-size:.92rem; margin-bottom:.25rem;
        }
        .st-key-top_opportunity div[data-testid="stMetric"] {
            background:rgba(7,11,18,.55); border-color:#2a394f; min-height:86px;
        }
        .st-key-top_opportunity [data-testid="stLinkButton"] a {
            min-height:54px; font-size:1rem; font-weight:850; letter-spacing:.01em;
            background:#22c55e !important; border-color:#39d98a !important;
            color:#04110a !important;
            box-shadow:0 8px 24px rgba(57,217,138,.22);
        }
        .st-key-top_opportunity [data-testid="stLinkButton"] a:hover {
            background:#39d98a !important; border-color:#6ee7b7 !important;
        }
        .st-key-top_opportunity .best-bet-proof {
            color:#8f9db1; font-size:.78rem; padding-top:.1rem;
        }
        div[class*="st-key-value_opportunity_"] {
            background:linear-gradient(135deg, #0f1827 0%, #0b121d 72%, #0d1b18 100%);
            border:1px solid #26354b !important; border-radius:12px;
            padding:.15rem .55rem .55rem; margin:.35rem 0;
            transition:border-color .16s ease, box-shadow .16s ease, transform .16s ease;
        }
        div[class*="st-key-value_opportunity_"]:hover {
            border-color:#2a8a5d !important;
            box-shadow:0 8px 24px rgba(0,0,0,.2), inset 3px 0 #39d98a;
            transform:translateY(-1px);
        }
        div[class*="st-key-value_opportunity_"]:has(details[open]) {
            border-color:#2a8a5d !important;
            box-shadow:0 8px 24px rgba(0,0,0,.22), inset 3px 0 #39d98a;
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stLinkButton"] a {
            min-height:45px; font-weight:850; letter-spacing:.01em;
            background:#22c55e !important; border-color:#39d98a !important;
            color:#04110a !important; box-shadow:0 6px 18px rgba(57,217,138,.18);
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stLinkButton"] a:hover {
            background:#39d98a !important; border-color:#77efb2 !important;
            box-shadow:0 8px 24px rgba(57,217,138,.3);
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] {
            border:0; background:transparent;
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] summary {
            padding:.65rem .25rem; border-radius:9px;
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"]:focus-within,
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] details:focus-within,
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] summary:focus,
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] summary:focus-visible {
            border-color:#39d98a !important; outline-color:#39d98a !important;
            box-shadow:0 0 0 1px #39d98a !important;
        }
        div[class*="st-key-value_opportunity_"] [data-testid="stExpander"] summary p {
            color:#f3f6fa; font-size:.96rem; font-weight:820; letter-spacing:-.01em;
        }
        .value-bet-pick {
            color:#f8fafc; font-size:1.25rem; line-height:1.15; font-weight:850;
            letter-spacing:-.025em; margin:.05rem 0 .25rem;
        }
        .value-bet-event { color:#a7b3c4; font-size:.84rem; }
        .value-bet-proof { color:#8492a6; font-size:.76rem; padding-top:.15rem; }
        .recommended-card-badge {
            color:#39df83; font-size:.7rem; font-weight:850; letter-spacing:.05em;
            text-transform:uppercase; margin:.2rem 0 .35rem;
        }
        .recommended-card-grid {
            display:grid; grid-template-columns:1fr 1fr 1fr; gap:.5rem;
            margin:.65rem 0 .55rem;
        }
        .recommended-card-metric {
            background:#0a121d; border:1px solid #253348; border-radius:9px;
            padding:.5rem .55rem; min-width:0;
        }
        .recommended-card-metric small {
            display:block; color:#8794a7; font-size:.61rem; text-transform:uppercase;
            letter-spacing:.04em; margin-bottom:.2rem;
        }
        .recommended-card-metric strong {
            display:block; color:#e8edf4; font-size:1rem; white-space:nowrap;
            overflow:hidden; text-overflow:ellipsis;
        }
        .recommended-card-metric strong.positive { color:#39df83; }
        .risk-note { color:#94a3b8; font-size:.78rem; }
        .stDataFrame { border: 1px solid #202b3d; border-radius: 8px; overflow:hidden; }
        [data-testid="stExpander"] { border-color: #202b3d; background: #0b111c; }
        button[data-baseweb="tab"] { font-weight: 650; }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"],
        [data-testid="stButtonGroup"] button[aria-checked="true"] {
            background: #123525 !important; border-color: #2a8a5d !important;
            color: #6ee7b7 !important;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"] p,
        [data-testid="stButtonGroup"] button[aria-checked="true"] p {
            color: #6ee7b7 !important; font-weight: 750;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"]:hover,
        [data-testid="stButtonGroup"] button[aria-checked="true"]:hover {
            background: #184c37 !important; border-color: #39d98a !important;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"]:focus-visible {
            outline: 2px solid #39d98a !important; outline-offset: 2px;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="false"]:hover {
            border-color: #39d98a !important; color: #9af0c7 !important;
        }
        .ev-page-subtitle { color:#b4bfce; font-size:.94rem; margin-top:-.15rem; }
        .ev-update-status {
            display:flex; justify-content:flex-end; align-items:center; flex-wrap:wrap; gap:7px;
            color:#a7b2c2; font-size:.78rem; text-align:right; line-height:1.35;
        }
        .ev-update-status strong { color:#d6dde8; font-weight:700; }
        .ev-freshness-state {
            display:inline-flex; padding:3px 7px; border-radius:999px; font-size:.65rem;
            font-weight:800; letter-spacing:.04em; text-transform:uppercase;
        }
        .ev-freshness-state.fresh { color:#74ebb0; background:#103824; border:1px solid #24794f; }
        .ev-freshness-state.aging { color:#ffd38a; background:#382a12; border:1px solid #735822; }
        .ev-freshness-state.needs-refresh {
            color:#ffbf69; background:#3d2610; border:1px solid #8a5424;
        }
        .ev-freshness-state.stale { color:#ffd38a; background:#382a12; border:1px solid #735822; }
        .st-key-ev_filter_bar {
            background:#0a111c; border:1px solid #1e2b3d; border-radius:12px;
            padding:.65rem .75rem .35rem; margin:.55rem 0 .45rem;
        }
        .st-key-ev_filter_bar [data-testid="stWidgetLabel"] p {
            color:#8997aa; font-size:.7rem; text-transform:uppercase; letter-spacing:.08em;
            font-weight:750;
        }
        .st-key-ev_filter_bar [data-baseweb="select"] > div,
        .st-key-ev_filter_bar [data-testid="stPopoverButton"] {
            background:#0e1825; border-color:#29374a; min-height:41px;
        }
        .ev-filter-chips { display:flex; flex-wrap:wrap; gap:7px; margin:.15rem 0 .15rem; }
        .ev-filter-chip {
            display:inline-flex; align-items:center; padding:5px 9px; border-radius:6px;
            color:#c4cedc; background:#142031; border:1px solid #26364b; font-size:.76rem;
        }
        .ev-summary-line {
            display:flex; gap:1rem; flex-wrap:wrap; color:#8f9caf; font-size:.78rem;
            padding:.1rem .15rem .5rem;
        }
        .ev-summary-line strong { color:#e4e9f0; }
        .st-key-top_opportunity {
            background:#0b1521;
            border:1px solid #24c96b !important;
            border-radius:12px; padding:.9rem 1rem .85rem;
            box-shadow:0 10px 30px rgba(0,0,0,.2); margin:.35rem 0 .75rem;
        }
        .st-key-top_opportunity .best-bet-badge {
            display:inline-flex; padding:4px 9px; border-radius:999px;
            background:#0d3425; color:#39e785; font-size:.7rem; font-weight:850;
            letter-spacing:.04em; margin-bottom:.55rem;
        }
        .st-key-top_opportunity .best-bet-pick {
            color:#f7f9fc; font-size:1.45rem; line-height:1.12; font-weight:850;
            letter-spacing:-.025em; margin-bottom:.2rem;
        }
        .st-key-top_opportunity .best-bet-opponent { color:#d1d8e2; font-size:.93rem; }
        .st-key-top_opportunity .best-bet-event {
            color:#94a2b5; font-size:.78rem; margin-top:.3rem;
        }
        .st-key-top_opportunity div[data-testid="stMetric"] {
            background:transparent; border:0; border-left:1px solid #2a3747;
            border-radius:0; min-height:88px; padding:.35rem .8rem;
        }
        .st-key-top_opportunity div[data-testid="stMetricValue"] { font-size:1.65rem; }
        .ev-featured-metrics {
            display:grid;
            grid-template-columns:.78fr .82fr 1.45fr .95fr;
            align-items:stretch;
        }
        .ev-featured-metric {
            min-width:0; border-left:1px solid #2a3747; padding:.25rem .65rem;
            text-align:center;
        }
        .ev-featured-label {
            color:#c1cad7; font-size:.7rem; white-space:nowrap; margin-bottom:.45rem;
        }
        .ev-featured-info { color:#8997aa; cursor:help; margin-left:3px; }
        .ev-featured-value {
            color:#c8d0dc; font-size:1.35rem; font-weight:800; white-space:nowrap;
            letter-spacing:-.02em;
        }
        .ev-featured-value.positive { color:#39df83; }
        .ev-featured-sub {
            color:#8d9aac; font-size:.65rem; margin-top:.35rem; white-space:nowrap;
            overflow:hidden; text-overflow:ellipsis;
        }
        .ev-probability-pair {
            display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:5px;
        }
        .ev-probability-pair strong {
            display:block; color:#e3e9f1; font-size:1.05rem; line-height:1.1; white-space:nowrap;
        }
        .ev-probability-pair small {
            display:block; color:#8794a7; font-size:.56rem; line-height:1.2; margin-top:4px;
        }
        .ev-probability-pair em { color:#667488; font-size:.58rem; font-style:normal; }
        .st-key-top_opportunity [data-testid="stLinkButton"] a {
            min-height:48px; font-size:.92rem; font-weight:850; background:#22c55e !important;
            border-color:#39d98a !important; color:#04110a !important; box-shadow:none;
        }
        .best-price-title {
            color:#f4f7fb; font-size:1.35rem; line-height:1.2; font-weight:850;
            letter-spacing:-.025em; margin:.25rem 0 .7rem;
        }
        .best-bet-support {
            border-top:1px solid #293647; margin-top:.55rem; padding-top:.65rem;
            color:#a5b1c1; font-size:.77rem; display:flex; flex-wrap:wrap; gap:1.2rem;
        }
        .ev-table-wrap {
            border:1px solid #202e40; border-radius:10px; overflow:hidden;
            background:#0a131f; margin-top:.35rem;
        }
        .ev-grid {
            display:grid; grid-template-columns:42px minmax(250px,2.3fr) minmax(100px,.9fr)
            minmax(92px,.8fr) minmax(100px,.85fr) minmax(90px,.8fr) minmax(80px,.65fr)
            minmax(110px,.9fr) minmax(95px,.75fr) minmax(90px,.7fr) 24px;
            align-items:center; column-gap:12px;
        }
        .ev-table-head {
            padding:.58rem .7rem; color:#8997aa; font-size:.66rem; text-transform:uppercase;
            letter-spacing:.06em; border-bottom:1px solid #253346; font-weight:750;
        }
        .ev-table-row { border-bottom:1px solid #1d2a3a; }
        .ev-table-row:last-child { border-bottom:0; }
        .ev-table-row summary {
            list-style:none; cursor:pointer; padding:.65rem .7rem; transition:background .15s ease;
        }
        .ev-table-row summary::-webkit-details-marker { display:none; }
        .ev-table-row summary:hover { background:#0e1b2a; }
        .ev-table-row[open] summary { background:#0e1b2a; border-bottom:1px solid #253346; }
        .ev-rank,.ev-positive { color:#39df83; font-weight:850; }
        .ev-positive small {
            display:block; color:#8794a7; font-size:.56rem; font-weight:600; margin-top:3px;
        }
        .ev-matchup strong { color:#f1f5f9; font-size:.84rem; display:block; }
        .ev-matchup span,.ev-cell-sub {
            color:#8e9bad; font-size:.68rem; display:block; margin-top:2px;
        }
        .ev-cell-main { color:#dbe2eb; font-size:.82rem; }
        .ev-probability-cell { display:flex; gap:9px; align-items:center; }
        .ev-probability-cell span { color:#dce3ec; font-size:.76rem; white-space:nowrap; }
        .ev-probability-cell small { color:#8390a3; font-size:.57rem; display:block; }
        .ev-probability-divider { color:#536177 !important; font-size:.62rem !important; }
        .ev-odds { color:#39df83; font-weight:850; font-size:1rem; }
        .ev-action {
            display:inline-flex; justify-content:center; padding:6px 9px; border:1px solid #1a9c55;
            color:#36df7e !important; border-radius:6px; text-decoration:none !important;
            font-size:.73rem; font-weight:800; white-space:nowrap;
        }
        .ev-chevron { color:#8794a6; font-size:1.05rem; }
        details[open] .ev-chevron { transform:rotate(180deg); }
        .ev-details { padding:.65rem .9rem .8rem; background:#08111b; }
        .ev-price-heading {
            display:flex; align-items:center; justify-content:space-between; gap:12px;
            color:#eef3f8; font-size:.88rem; font-weight:800; margin-bottom:.6rem;
        }
        .ev-price-heading small { color:#9eabbc; font-size:.72rem; font-weight:600; }
        .ev-price-grid {
            display:grid; grid-template-columns:minmax(145px,1.35fr) 92px 112px 102px 86px 82px;
            align-items:center; column-gap:12px;
        }
        .ev-price-header {
            color:#93a1b4; font-size:.67rem; text-transform:uppercase; letter-spacing:.05em;
            padding:0 .55rem .4rem;
        }
        .ev-price-row {
            color:#d8e0e8; font-size:.78rem; padding:.54rem .6rem;
            border-top:1px solid #1c2b3b;
        }
        .ev-price-row.best { background:rgba(21,128,71,.08); }
        .ev-price-book { color:#eef3f8; font-weight:750; }
        .ev-price-odds { color:#dfe6ee; font-size:.88rem; font-weight:800; }
        .ev-price-row.best .ev-price-odds { color:#39df83; }
        .ev-price-edge.positive { color:#39df83; font-weight:750; }
        .ev-price-edge.negative { color:#9ba8b7; }
        .ev-price-best {
            display:inline-flex; width:max-content; border-radius:999px; padding:2px 7px;
            background:#0d5d38; color:#63efa5; font-size:.58rem; font-weight:800;
        }
        .ev-price-action {
            display:inline-flex; justify-content:center; padding:4px 8px; border:1px solid #1a874d;
            border-radius:5px; color:#39df83 !important; text-decoration:none !important;
            font-size:.74rem; font-weight:800;
        }
        .ev-consensus-details { color:#95a2b4; font-size:.71rem; margin-top:.55rem; }
        .ev-consensus-details summary {
            width:max-content; cursor:pointer; color:#91a0b3; list-style:none;
        }
        .ev-consensus-details summary::before { content:"›"; margin-right:6px; }
        .ev-consensus-details[open] summary::before { content:"⌄"; }
        .ev-consensus-details span { display:block; margin:.35rem 0 0 14px; }
        .ev-empty {
            text-align:center; padding:2.25rem 1rem; border:1px dashed #2a394d; border-radius:10px;
            background:#0a121e; color:#8f9caf;
        }
        .ev-empty strong { display:block; color:#eef2f7; font-size:1.05rem; margin-bottom:.35rem; }
        .legal-details a { color:#55e79a !important; text-decoration:none; }
        .legal-details { color:#aab5c4; font-size:.86rem; line-height:1.62; }
        .legal-details strong { color:#e7edf4; }
        .games-heading {
            display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
            margin:.45rem 0 .7rem;
        }
        .games-heading h2 { margin:0; color:#f3f6fa; font-size:1.45rem; }
        .games-heading p { margin:4px 0 0; color:#a7b3c2; font-size:.88rem; }
        .games-heading span { color:#a7b3c2; font-size:.84rem; white-space:nowrap; }
        .st-key-games_filters { margin-bottom:.65rem; }
        .st-key-games_filters [data-testid="stHorizontalBlock"] { gap:8px; }
        .st-key-games_filters [data-baseweb="input"],
        .st-key-games_filters [data-baseweb="select"] > div {
            min-height:40px; border-color:#27374b; background:#0a1421;
        }
        .st-key-games_sport_filter [data-testid="stSegmentedControl"] { max-width:520px; }
        .games-day-heading {
            display:flex; align-items:center; justify-content:space-between;
            margin:1rem .2rem .4rem; color:#f1f5f9;
        }
        .games-day-heading strong { font-size:1.08rem; }
        .games-day-heading span { color:#9eabbc; font-size:.8rem; }
        .games-list {
            border:1px solid #25364a; border-radius:8px; overflow:hidden;
            background:#08131f;
        }
        .games-event + .games-event { border-top:1px solid #223247; }
        .games-event > summary {
            display:grid; grid-template-columns:86px minmax(300px,1fr) 24px;
            align-items:center; gap:14px; min-height:78px; padding:.82rem .95rem;
            cursor:pointer; list-style:none; transition:background .14s ease;
        }
        .games-event > summary::-webkit-details-marker { display:none; }
        .games-event > summary:hover,.games-event[open] > summary { background:#0c1927; }
        .games-event[open] > summary { border-bottom:1px solid #25364a; }
        .games-time { color:#b3bdca; font-size:.87rem; white-space:nowrap; }
        .games-matchup { display:flex; align-items:center; min-width:0; gap:12px; }
        .games-team-logos {
            display:flex; align-items:center; gap:6px; width:86px; flex:0 0 86px;
        }
        .games-team-logos .ev-team-logo { width:40px; height:40px; flex-basis:40px; }
        .games-matchup-copy { min-width:0; overflow:hidden; }
        .games-matchup-copy strong {
            display:block; color:#f3f6fa; font-size:1.03rem; line-height:1.22;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
        }
        .games-matchup-copy small {
            display:block; margin-top:4px; color:#a3afbf; font-size:.76rem;
        }
        .games-chevron {
            color:#9cabbc; font-size:1rem; text-align:center; transition:transform .14s ease;
        }
        .games-event[open] .games-chevron { transform:rotate(180deg); }
        .games-event-body { padding:.8rem .9rem 1rem; background:#0a1522; }
        .games-odds-pending {
            display:flex; align-items:center; justify-content:space-between; gap:18px;
            padding:.9rem 1rem; border:1px dashed #2b3b4f; border-radius:7px;
            color:#9eabbc; background:#08131f;
        }
        .games-odds-pending strong { color:#dce4ed; font-size:.9rem; }
        .games-odds-pending span { font-size:.8rem; text-align:right; }
        .games-market-group {
            border:1px solid #26374b; border-radius:7px; background:#08131f;
        }
        .games-market-group + .games-market-group { margin-top:8px; }
        .games-market-group > summary {
            padding:.72rem .8rem; cursor:pointer; color:#e7edf5; font-size:.88rem;
            font-weight:750; list-style:none;
        }
        .games-market-group > summary::-webkit-details-marker { display:none; }
        .games-market-group > summary::after {
            content:"⌄"; float:right; color:#8f9daf;
        }
        .games-market-group[open] > summary::after { transform:rotate(180deg); }
        .games-odds-scroll { overflow-x:auto; border-top:1px solid #26374b; }
        .games-odds-table {
            width:100%; min-width:860px; border-collapse:collapse; table-layout:fixed;
            color:#dbe3ec; font-size:.84rem;
        }
        .games-odds-table th,.games-odds-table td {
            border-bottom:1px solid #203043; text-align:center;
        }
        .games-odds-table tr:last-child td { border-bottom:0; }
        .games-odds-table th {
            padding:.66rem .55rem; color:#a0adbd; font-size:.7rem; font-weight:700;
            text-transform:uppercase; letter-spacing:.035em;
        }
        .games-odds-table th:first-child,.games-odds-table td:first-child {
            width:210px; padding:.65rem .7rem; text-align:left;
        }
        .games-selection strong { display:block; color:#edf2f7; font-size:.86rem; }
        .games-selection small { display:block; margin-top:3px; color:#9eabbc; font-size:.7rem; }
        .games-price-cell { padding:0; }
        .games-price-link {
            display:block; width:100%; padding:.8rem .5rem; color:#dce4ed !important;
            text-decoration:none !important; white-space:nowrap; transition:background .12s ease;
        }
        .games-price-link:hover,.games-price-link:focus-visible {
            color:#47e28b !important; background:#0b2e20;
        }
        .games-price-link.best {
            color:#43df88 !important; background:#0a3322; font-weight:850;
        }
        .games-unavailable { color:#5e6d80; }
        .games-empty {
            padding:2.2rem 1rem; border:1px dashed #2b3b4f; border-radius:8px;
            color:#96a4b5; text-align:center;
        }
        .ev-list-title {
            color:#f2f6fa; font-size:1.05rem; font-weight:850; margin:.9rem .15rem .4rem;
        }
        .ev-count-badge {
            display:inline-flex; margin-left:7px; padding:2px 8px; border-radius:999px;
            background:#0d3425; color:#39df83; font-size:.75rem; vertical-align:middle;
        }
        /* Dense +EV board — aligned to the approved desktop reference. */
        [data-testid="stSidebar"], [data-testid="collapsedControl"] {
            display:none !important;
        }
        [data-testid="stHeader"] { display:none !important; }
        .block-container {
            width:100%; max-width:1480px; padding:.9rem 1.25rem 1rem; margin:0 auto;
        }
        h1 {
            font-size:2rem !important; line-height:1.15 !important;
            margin:0 !important; padding:0 !important;
        }
        .st-key-bettor_bureau_brand {
            min-height:52px; display:flex; align-items:center;
        }
        .bettor-bureau-lockup {
            display:flex; align-items:center; gap:11px; min-width:0;
        }
        .bettor-bureau-lockup img {
            display:block; width:45px; height:45px; object-fit:contain; flex:0 0 45px;
        }
        .bettor-bureau-lockup strong {
            color:#f5f7fa; font-size:1.65rem; line-height:1; letter-spacing:-.045em;
            white-space:nowrap;
        }
        .ev-page-subtitle { margin-top:.25rem; }
        [data-testid="stVerticalBlock"] { gap:.48rem; }
        .st-key-ev_filter_bar {
            background:transparent; border:0; border-radius:0;
            padding:.2rem 0 .05rem; margin:.45rem 0 0;
        }
        .st-key-ev_filter_bar [data-baseweb="select"] > div,
        .st-key-ev_filter_bar [data-testid="stPopover"] button {
            background:#08121e; border-color:#27384b; border-radius:7px;
            box-sizing:border-box; min-height:42px; height:42px; font-size:.92rem;
        }
        .st-key-ev_filter_bar [data-baseweb="select"] > div { padding-left:10px; }
        .st-key-ev_filter_bar [data-testid="stPopoverButton"] {
            justify-content:flex-start; padding:0 10px; text-align:left;
        }
        .st-key-ev_filter_bar [data-testid="stPopoverButton"] > div {
            width:100%; justify-content:space-between;
        }
        .st-key-ev_filter_bar [data-testid="stPopoverButton"] p {
            flex:1; text-align:left; white-space:nowrap;
        }
        .st-key-ev_filter_bar [data-testid="stPopoverButton"] > div > div:last-child {
            margin-left:auto;
        }
        .st-key-ev_filter_bar [data-testid="stSelectbox"] { min-width:0; }
        .ev-sort-label {
            color:#b4becb; font-size:.84rem; height:42px; line-height:42px;
            padding:0; text-align:right; transform:translateY(-20px);
        }
        @media (min-width: 1200px) {
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) {
                display:grid; align-items:end; gap:12px;
                grid-template-columns:160px 155px 140px 185px 160px minmax(28px,1fr)
                58px 220px;
            }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"] {
                width:auto !important; min-width:0 !important; flex:unset !important;
            }
        }
        @media (min-width: 761px) and (max-width: 1199px) {
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) {
                display:grid; align-items:end; gap:10px;
                grid-template-columns:repeat(5,minmax(0,1fr));
            }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"] {
                width:auto !important; min-width:0 !important; flex:unset !important;
            }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(6) { display:none; }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(7) { grid-column:4; }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(8) { grid-column:5; }
        }
        .st-key-dashboard_nav { margin:.35rem 0 .8rem; }
        .st-key-dashboard_nav [data-testid="stSegmentedControl"] {
            width:max-content; padding:3px; border:1px solid #26364a;
            border-radius:8px; background:#08111c;
        }
        .st-key-dashboard_nav [data-testid="stSegmentedControl"] button {
            min-width:112px; min-height:34px; padding:6px 16px; border:0;
            border-radius:5px; color:#aab6c5; font-weight:750;
        }
        .st-key-dashboard_nav [data-testid="stSegmentedControl"] button[aria-pressed="true"] {
            background:#0d3926; color:#56e898;
        }
        .st-key-dashboard_nav [data-testid="stSegmentedControl"] button:hover {
            color:#f1f5f9; background:#112031;
        }
        .ev-filter-chips { gap:10px; margin:.05rem 0 .45rem; }
        .ev-filter-chip {
            padding:7px 12px; border-radius:6px; color:#d1d8e2;
            background:#101b29; border-color:#26364a; font-size:.84rem;
        }
        .ev-update-status { font-size:.83rem; white-space:nowrap; }
        .ev-update-status .ev-freshness-state { display:none; }
        .st-key-header_odds_format { white-space:nowrap; }
        .st-key-header_odds_format [data-testid="stCheckbox"] label {
            gap:8px; color:#d0d8e2; font-size:.84rem; font-weight:750;
        }
        .st-key-header_refresh button {
            width:34px; min-height:34px; padding:0; border:0; background:transparent;
        }
        .st-key-header_refresh button p { display:none; }
        .st-key-recommended_board {
            margin:.2rem 0 .45rem;
        }
        .st-key-recommended_board [data-testid="stVerticalBlock"] { gap:.2rem; }
        .recommended-section {
            border:1px solid #25364a; border-radius:8px;
            background:rgba(7,17,28,.62); overflow:hidden;
        }
        .recommended-section > summary {
            display:flex; align-items:center; gap:9px; min-height:48px;
            padding:11px 14px; list-style:none; cursor:pointer;
            border-bottom:1px solid #25364a; user-select:none;
        }
        .recommended-section > summary::-webkit-details-marker { display:none; }
        .recommended-section > summary:hover { background:#0d1b29; }
        .recommended-section > summary:focus { outline:none !important; }
        .recommended-section > summary:focus-visible {
            outline:none !important;
            box-shadow:inset 0 0 0 1px rgba(56,223,131,.58) !important;
        }
        .recommended-section > summary::after {
            content:"⌃"; margin-left:auto; color:#aeb8c6; font-size:1rem;
            transition:transform .15s ease;
        }
        .recommended-section:not([open]) > summary { border-bottom:0; }
        .recommended-section:not([open]) > summary::after { transform:rotate(180deg); }
        .recommended-count {
            display:inline-flex; align-items:center; justify-content:center;
            min-width:24px; height:22px; padding:0 7px; border-radius:999px;
            background:#0b5d39; color:#58e99b; font-size:.78rem; font-weight:850;
        }
        .recommended-content { padding:0 10px 12px; }
        .recommendation-heading {
            color:#f3f6fa; font-size:1.25rem; font-weight:850; line-height:1.2;
        }
        .recommendation-subtitle { color:#aeb8c6; font-size:.86rem; margin-top:4px; }
        .ev-table-wrap { margin-top:.15rem; border-radius:7px; background:#08131f; }
        .st-key-recommended_board .ev-table-wrap {
            margin:.5rem 0 .55rem; overflow:visible;
        }
        .block-container > [data-testid="stVerticalBlock"] {
            min-height:calc(100vh - 2rem);
        }
        [data-testid="stLayoutWrapper"]:has(> .st-key-launch_disclosures) {
            margin-top:auto !important;
        }
        .st-key-launch_disclosures { padding-top:2.25rem; clear:both; }
        .st-key-launch_disclosures [data-testid="stExpander"] {
            border-top:1px solid #263448;
        }
        .st-key-owner_panel { margin-top:.75rem; padding-bottom:1.5rem; clear:both; }
        .st-key-owner_panel [data-testid="stExpander"] {
            border:1px solid #263448; border-radius:8px;
        }
        .st-key-more_ev_header { margin:.65rem 0 .15rem; clear:both; }
        .st-key-more_ev_header [data-testid="stSelectbox"] {
            max-width:230px; margin-left:auto;
        }
        .board-grid {
            display:grid;
            grid-template-columns:40px minmax(260px,1.2fr) 92px 92px 112px 140px
            minmax(190px,1fr) 118px;
            align-items:center; column-gap:8px;
        }
        .board-table-head {
            padding:.65rem .55rem; color:#aab5c4; font-size:.75rem;
            text-transform:uppercase; letter-spacing:.04em; border-bottom:1px solid #253447;
        }
        .board-table-head > span { text-align:center; }
        .board-table-head > span:nth-child(2) { text-align:left; }
        .board-table-head .board-market { text-align:center; }
        .board-table-head .board-win { text-align:center; }
        .board-info { position:relative; display:inline-block; }
        .board-info[open] { z-index:90; }
        .board-info[open] > summary::before {
            content:""; position:fixed; inset:0; z-index:70; cursor:default;
        }
        .board-table-head:has(.board-info[open]) .board-info:not([open]) {
            z-index:100;
        }
        .board-info > summary {
            position:relative; z-index:80; list-style:none; cursor:pointer;
            user-select:none; white-space:nowrap;
        }
        .board-info > summary::-webkit-details-marker { display:none; }
        .board-info > summary:focus-visible {
            outline:2px solid #35df80; outline-offset:3px; border-radius:3px;
        }
        .board-tooltip {
            position:absolute; z-index:80; top:calc(100% + 9px); left:50%;
            width:min(390px,calc(100vw - 32px)); transform:translateX(-50%);
            padding:15px 18px;
            border:1px solid #34465c; border-radius:10px; background:#101b29;
            box-shadow:0 12px 30px rgba(0,0,0,.38); color:#d6dee8;
            font-size:1.08rem; font-weight:500; line-height:1.45; text-align:left;
            text-transform:none; letter-spacing:0; white-space:normal;
            overflow-wrap:anywhere;
        }
        .board-info.align-right .board-tooltip {
            right:0; left:auto; transform:none;
        }
        .board-row { border-bottom:1px solid #203043; }
        .board-row:last-child { border-bottom:0; }
        .board-row > summary {
            list-style:none; cursor:pointer; padding:.7rem .55rem; min-height:78px;
            transition:background .14s ease;
        }
        .recommended-row > summary { min-height:94px; padding:.8rem .65rem; }
        .board-table-head + .recommended-row > summary {
            border-left:3px solid #d8a928;
            background:linear-gradient(90deg,rgba(216,169,40,.10),transparent 34%);
            padding-left:5px;
        }
        .board-row > summary::-webkit-details-marker { display:none; }
        .board-row > summary:hover,.board-row[open] > summary { background-color:#0d1b29; }
        .board-row[open] > summary { border-bottom:1px solid #253447; }
        .board-rank { color:#38df83; font-weight:850; text-align:center; }
        .board-rank.top { font-size:.68rem; line-height:1.15; }
        .board-rank.top span { display:block; margin-top:3px; }
        .board-rank.gold {
            display:inline-flex; justify-content:center; align-items:center; justify-self:center;
            width:24px; height:24px; border-radius:4px; background:#d8a928; color:#171105;
            box-shadow:0 0 0 1px rgba(255,220,102,.35); font-size:.78rem;
        }
        .board-rank.pill {
            display:inline-flex; justify-content:center; align-items:center; justify-self:center;
            width:24px; height:24px; border-radius:4px; background:#8758bc; color:#0b1018;
        }
        .board-matchup { display:flex; align-items:center; min-width:0; gap:10px; }
        .board-market { justify-self:stretch; text-align:center; }
        .ev-team-logo {
            position:relative; display:inline-flex; align-items:center; justify-content:center;
            width:40px; height:40px; flex:0 0 40px; color:#718096; font-size:.65rem;
            font-weight:800; border-radius:50%; background:#101c2a;
        }
        .ev-team-logo.has-logo { background:transparent; border-radius:0; }
        .ev-team-logo img {
            width:100%; height:100%; object-fit:contain;
            filter:drop-shadow(0 2px 2px rgba(0,0,0,.35));
        }
        .board-matchup-copy { min-width:0; }
        .board-matchup-copy strong {
            color:#f3f6fa; font-size:1.04rem; line-height:1.18; display:block;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
        }
        .board-matchup-copy span {
            color:#c0c9d5; font-size:.86rem; line-height:1.28; display:block;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
        }
        .board-matchup-copy small { color:#a5b1c0; font-size:.76rem; }
        .board-cell { color:#dbe2ea; font-size:.88rem; text-align:center; }
        .board-cell strong { display:block; color:#f2f5f8; font-size:1rem; }
        .board-cell small { display:block; color:#a8b3c1; font-size:.73rem; margin-top:3px; }
        .board-ev strong,.board-odds strong { color:#35df80; font-size:1.14rem; }
        .board-win { justify-self:stretch; text-align:center; }
        .board-win-stack {
            display:flex; width:100%; flex-direction:column; align-items:center; gap:1px;
        }
        .board-win-stack strong { color:#c184f1; font-size:1.12rem; line-height:1.1; }
        .board-win-stack small { color:#a8b3c1; font-size:.72rem; }
        .board-win-stack em {
            color:#cbd3dd; font-size:.8rem; font-style:normal; white-space:nowrap;
        }
        .board-action-cell { justify-self:center; }
        .board-action {
            display:inline-flex; align-items:center; justify-content:center; min-height:38px;
            padding:7px 12px; border:1px solid #16894c; border-radius:6px;
            color:#35df80 !important; text-decoration:none !important; font-size:.86rem;
            font-weight:800; text-align:center; line-height:1.2;
        }
        .recommended-row .board-action {
            background:#22c55e; border-color:#28d66a; color:#04110a !important;
        }
        .all-bets-title {
            display:flex; align-items:center; gap:8px; margin:.45rem .5rem .15rem;
            color:#f2f6fa; font-size:1.2rem; font-weight:850;
        }
        .all-bets-count {
            display:inline-flex; padding:2px 9px; border-radius:999px;
            background:#063d26; color:#37df82; font-size:.8rem;
        }
        .board-pagination-note { color:#a9b4c2; font-size:.7rem; text-align:right; }
        .st-key-load_more_ev {
            clear:both; position:relative; z-index:1; max-width:240px;
            margin:1rem auto 1.5rem !important; padding-top:.25rem;
        }
        .st-key-load_more_ev [data-testid="stButton"] { width:100%; }
        .st-key-load_more_ev button {
            width:100%; min-height:40px; border-color:#237346; color:#54e596;
            background:#0b1c16; font-weight:750;
        }
        @media (max-width: 1320px) {
            .ev-grid {
                grid-template-columns:38px minmax(235px,2.15fr) 90px 82px 135px
                78px 92px 86px 24px;
            }
            .ev-fair,.ev-range { display:none; }
        }
        @media (min-width: 981px) and (max-width: 1320px) {
            .board-grid {
                grid-template-columns:32px minmax(220px,1.15fr) 72px 74px 90px 90px
                minmax(150px,1fr) 88px;
                column-gap:6px;
            }
        }
        @media (min-width: 761px) and (max-width: 980px) {
            .board-grid {
                grid-template-columns:36px minmax(220px,1.2fr) 82px 92px
                minmax(180px,1fr) 108px;
                column-gap:8px;
            }
            .board-market,.board-fair { display:none; }
        }
        @media (min-width: 761px) and (max-width: 900px) {
            .ev-price-grid {
                grid-template-columns:minmax(110px,1fr) 72px 88px 70px;
            }
            .ev-price-prob,.ev-price-edge { display:none; }
        }
        @media (max-width: 760px) {
            .block-container { padding:.6rem .75rem 1.2rem; }
            h1 { font-size:1.75rem; }
            .games-heading { align-items:flex-start; flex-direction:column; gap:4px; }
            .games-event > summary {
                grid-template-columns:64px minmax(0,1fr) 22px; gap:9px;
                min-height:66px; padding:.7rem .65rem;
            }
            .games-team-logos { display:none; }
            .games-matchup-copy strong { white-space:normal; }
            .games-event-body { padding:.6rem; }
            .st-key-bettor_bureau_brand { min-height:46px; }
            .bettor-bureau-lockup { gap:8px; }
            .bettor-bureau-lockup img { width:38px; height:38px; flex-basis:38px; }
            .bettor-bureau-lockup strong { font-size:1.35rem; }
            .st-key-page_header [data-testid="stHorizontalBlock"]:has(
                .st-key-bettor_bureau_brand
            ) {
                display:grid !important; grid-template-columns:minmax(0,1fr) 184px;
                align-items:center; gap:2px 8px;
            }
            .st-key-page_header [data-testid="stHorizontalBlock"]:has(
                .st-key-bettor_bureau_brand
            ) > [data-testid="stColumn"] {
                width:auto !important; min-width:0 !important; flex:unset !important;
            }
            .st-key-page_header [data-testid="stHorizontalBlock"]:has(
                .st-key-bettor_bureau_brand
            ) > [data-testid="stColumn"]:nth-child(1) {
                grid-column:1 / -1; grid-row:1;
            }
            .st-key-page_header [data-testid="stHorizontalBlock"]:has(
                .st-key-bettor_bureau_brand
            ) > [data-testid="stColumn"]:nth-child(2) {
                grid-column:1; grid-row:2;
            }
            .st-key-page_header [data-testid="stHorizontalBlock"]:has(
                .st-key-bettor_bureau_brand
            ) > [data-testid="stColumn"]:nth-child(3) { grid-column:2; grid-row:2; }
            .ev-page-subtitle { font-size:.82rem; }
            .ev-update-status {
                justify-content:flex-start; text-align:left; margin:.05rem 0 .2rem;
                white-space:normal;
            }
            .ev-table-head { display:none; }
            .ev-table-row summary {
                grid-template-columns:28px minmax(0,1fr) 74px 22px;
                grid-template-rows:auto auto auto; column-gap:8px; row-gap:7px;
                padding:.7rem .6rem;
            }
            .ev-market,.ev-fair,.ev-range,.ev-best-book { display:none; }
            .ev-rank { grid-column:1; grid-row:1 / span 3; align-self:start; }
            .ev-matchup { grid-column:2; grid-row:1; }
            .ev-positive { grid-column:3; grid-row:1; text-align:right; }
            .ev-chevron { grid-column:4; grid-row:1; }
            .ev-best-odds { grid-column:2; grid-row:2; }
            .ev-probability-cell { grid-column:3 / span 2; grid-row:2; justify-content:flex-end; }
            .ev-action-cell { grid-column:2 / span 3; grid-row:3; }
            .ev-action { width:100%; box-sizing:border-box; }
            .ev-featured-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); gap:.55rem 0; }
            .best-bet-support { gap:.45rem .8rem; }
            .board-table-head { display:none; }
            .board-grid {
                grid-template-columns:30px minmax(0,1fr) minmax(94px,.75fr);
                grid-template-rows:auto auto auto auto; gap:8px;
            }
            .board-row > summary { min-height:0; padding:.82rem .55rem; }
            .board-rank { grid-column:1; grid-row:1 / span 4; align-self:start; }
            .board-matchup { grid-column:2 / span 2; grid-row:1; }
            .board-ev { grid-column:2; grid-row:2; text-align:left; }
            .board-market,.board-fair { display:none; }
            .board-odds { grid-column:3; grid-row:2; text-align:left; }
            .board-win { grid-column:2 / span 2; grid-row:3; text-align:left; }
            .board-action-cell { grid-column:2 / span 2; grid-row:4; }
            .board-action { width:100%; box-sizing:border-box; }
            .board-ev::before,.board-odds::before,.board-win::before {
                display:block; color:#8795a8; font-size:.64rem; font-weight:800;
                letter-spacing:.06em; margin-bottom:2px;
            }
            .board-ev::before { content:"EV"; }
            .board-odds::before { content:"BEST ODDS"; }
            .board-win::before { content:"WIN PROBABILITY"; }
            .board-win-stack { align-items:flex-start; }
            .board-matchup-copy strong { font-size:1rem; }
            .board-matchup-copy span { font-size:.84rem; }
            .board-matchup-copy small { font-size:.74rem; }
            .board-cell { font-size:.86rem; }
            .board-action { min-height:42px; font-size:.9rem; }
            .ev-team-logo { width:40px; height:40px; flex-basis:40px; }
            .ev-price-grid {
                grid-template-columns:minmax(110px,1fr) 72px 88px 70px;
            }
            .ev-price-prob,.ev-price-edge { display:none; }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) {
                display:grid !important; grid-template-columns:repeat(2,minmax(0,1fr));
                gap:8px;
            }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"] {
                width:auto !important; min-width:0 !important; flex:unset !important;
            }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(6),
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(7) { display:none; }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(5),
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(8) { grid-column:1 / -1; }
            .st-key-more_ev_header [data-testid="stHorizontalBlock"] {
                display:grid !important; grid-template-columns:1fr; gap:6px;
            }
            .st-key-more_ev_header [data-testid="stColumn"] {
                width:auto !important; min-width:0 !important; flex:unset !important;
            }
            .st-key-more_ev_header [data-testid="stSelectbox"] {
                max-width:none; margin-left:0;
            }
            .all-bets-title { margin:.6rem .15rem .1rem; }
        }
        @media (max-width: 480px) {
            .block-container { padding:.55rem .55rem 1rem; }
            .st-key-ev_filter_bar [data-testid="stHorizontalBlock"]:has(
                .st-key-ev_sport_filter
            ) > [data-testid="stColumn"]:nth-child(8) { grid-column:1 / -1; }
            .ev-price-grid { grid-template-columns:minmax(100px,1fr) 64px 70px; }
            .ev-price-row > span:nth-child(5),
            .ev-price-header > span:nth-child(5) { display:none; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_page_header() -> Any:
    with st.container(key="page_header"):
        title_column, status_column, format_column = st.columns(
            [6.4, 1.25, 1.35], vertical_alignment="center"
        )
        with title_column:
            brand_mark = Path(__file__).resolve().parents[2] / "assets" / "bettor-bureau-mark.png"
            with st.container(key="bettor_bureau_brand"):
                st.markdown(
                    '<div class="bettor-bureau-lockup">'
                    f'<img src="{_asset_data_uri(str(brand_mark))}" alt="">'
                    "<strong>Bettor Bureau</strong></div>",
                    unsafe_allow_html=True,
                )
        with status_column:
            status_container = st.container(key="header_odds_status")
        with format_column, st.container(key="header_odds_format"):
            decimal_odds = st.toggle(
                "Decimal odds",
                value=st.session_state.get("odds_format", DEFAULT_ODDS_FORMAT) == "Decimal",
                key="decimal_odds",
                help="Switch off for American odds.",
            )
            st.session_state["odds_format"] = "Decimal" if decimal_odds else "American"
    return status_container


def _queue_owner_refresh() -> None:
    st.session_state["owner_refresh_requested"] = True


def _render_owner_panel(
    repository: QuoteRepository,
    mode: str,
    *,
    is_admin: bool,
) -> None:
    shared_mode = _local_secret("SHARED_APP").casefold() in {"1", "true", "yes", "on"}
    panel = st.container(key="owner_panel").expander("Owner controls", expanded=False)
    with panel:
        if is_admin:
            used = _oddspapi_requests_used(repository)
            credit_limit = _oddspapi_credit_limit(repository)
            remaining = max(0, credit_limit - used)
            st.markdown("#### Odds administration")
            st.metric("API calls remaining this month", remaining)
            st.progress(
                min(1.0, used / credit_limit),
                text=f"{used} of {credit_limit} calls used",
            )
            st.caption("Only the owner and the central scheduled updater can spend API calls.")
            st.button(
                "Refresh latest odds",
                icon=":material/refresh:",
                type="primary",
                width="stretch",
                key="admin_refresh_odds",
                on_click=_queue_owner_refresh,
            )
            st.divider()
            _render_refresh_admin_status(repository, mode)
            st.divider()
            _render_official_settlement_controls(repository)
            if shared_mode and st.button(
                "Return to viewer mode",
                width="stretch",
                key="admin_sign_out",
            ):
                st.session_state["owner_authenticated"] = False
                st.session_state.pop("owner_password", None)
                st.rerun()
        else:
            st.caption("Owner sign-in is required to refresh odds or view API usage.")
            password = st.text_input(
                "Owner password",
                type="password",
                key="owner_password",
            )
            if st.button(
                "Unlock owner controls",
                width="stretch",
                key="owner_unlock",
            ):
                if _password_matches(
                    password,
                    _local_secret("ADMIN_PASSWORD_HASH"),
                ):
                    st.session_state["owner_authenticated"] = True
                    st.rerun()
                else:
                    st.error("That owner password is not correct.")


def _render_header_dashboard(
    container: Any,
    quotes: tuple[Quote, ...],
    values: tuple[ValueOpportunity, ...],
) -> None:
    minimum_ev = Decimal(str(st.session_state["min_ev"])) / Decimal("100")
    upcoming_games = len({quote.outcome.market.event_id for quote in quotes})
    sportsbook_count = len({quote.sportsbook.id for quote in quotes})
    value_bet_count = sum(item.expected_value >= minimum_ev for item in values)
    metric_columns = container.columns(3)
    metric_columns[0].metric("Upcoming games", f"{upcoming_games:,}")
    metric_columns[1].metric("Sportsbooks compared", sportsbook_count)
    metric_columns[2].metric("+EV bets", value_bet_count)


def _market_label(market: MarketKey) -> str:
    if market.kind is MarketKind.PLAYER_PROP:
        return market.stat_key or "Player prop"
    label = MARKET_NAMES[market.kind]
    if market.line is not None:
        if market.kind is MarketKind.SPREAD:
            label += f" {market.line:+} home"
        else:
            label += f" {market.line}"
    return label


def _event_team_names(event: Event) -> tuple[str, str]:
    """Return usable home/away names when a feed supplies generic participants."""
    home_name = event.home.name.strip()
    away_name = event.away.name.strip()
    generic_home = home_name.casefold() in {"home", "home team"}
    generic_away = away_name.casefold() in {"away", "away team"}
    parsed_home: str | None = None
    parsed_away: str | None = None
    if " at " in event.name:
        parsed_away, parsed_home = (part.strip() for part in event.name.split(" at ", 1))
    elif " vs " in event.name:
        parsed_home, parsed_away = (part.strip() for part in event.name.split(" vs ", 1))
    if generic_home and parsed_home:
        home_name = parsed_home
    if generic_away and parsed_away:
        away_name = parsed_away
    return home_name, away_name


def _selection_label(quote: Quote, event: Event | None = None) -> str:
    market = quote.outcome.market
    if market.kind is MarketKind.PLAYER_PROP:
        player_name = market.variant if market.variant != "standard" else "Player"
        side = quote.outcome.side.value.title()
        if market.line is not None and market.line != 0:
            return f"{player_name} {side} {market.line}"
        return f"{player_name} {side}"
    side = quote.outcome.side.value.title()
    if event is not None:
        home_name, away_name = _event_team_names(event)
        if quote.outcome.side is OutcomeSide.HOME:
            side = home_name
        elif quote.outcome.side is OutcomeSide.AWAY:
            side = away_name
        elif quote.outcome.side is OutcomeSide.DRAW:
            side = "Draw"
    if market.kind is MarketKind.SPREAD and market.line is not None:
        line = market.line if quote.outcome.side is OutcomeSide.HOME else -market.line
        return f"{side} {line:+}"
    if market.line is not None:
        return f"{side} {market.line}"
    return side


def _age_label(quote: Quote, as_of: datetime) -> str:
    seconds = max(0, int((as_of - quote.source_updated_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"


def _recommendation_freshness(quote: Quote, as_of: datetime) -> str:
    config = _refresh_config(str(st.session_state.get("data_source", "Demo"))).freshness
    state = freshness_state(quote.observed_at, as_of=as_of, config=config)
    seconds = max(0, int((as_of - quote.observed_at).total_seconds()))
    checked_age = f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"
    return f"{state.value} · checked {checked_age} ago"


def _elapsed_label(moment: datetime, as_of: datetime) -> str:
    seconds = max(0, int((as_of - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    hours, remainder = divmod(seconds, 3600)
    if hours < 24:
        return f"{hours}h {remainder // 60}m ago"
    days, remainder = divmod(hours, 24)
    return f"{days}d {remainder}h ago"


def _elapsed_compact_label(moment: datetime, as_of: datetime) -> str:
    seconds = max(0, int((as_of - moment).total_seconds()))
    if seconds < 60:
        return "<1m ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _render_odds_status(
    quotes: tuple[Quote, ...],
    fresh_quotes: tuple[Quote, ...],
    as_of: datetime,
    container: Any | None = None,
) -> None:
    del fresh_quotes
    target = container if container is not None else st
    if not quotes:
        target.markdown(
            '<div class="ev-update-status"><strong>No saved odds</strong><br>'
            "Use Refresh latest odds</div>",
            unsafe_allow_html=True,
        )
        return
    last_refresh = max(quote.observed_at for quote in quotes)
    age = _elapsed_compact_label(last_refresh, as_of)
    target.markdown(
        f'<div class="ev-update-status"><span>Odds last refreshed: '
        f"<strong>{age}</strong></span></div>",
        unsafe_allow_html=True,
    )


def _load_defaults(repository: QuoteRepository) -> None:
    if st.session_state.get("terminal_defaults_loaded"):
        return
    stored = _cached_settings(repository)
    st.session_state.setdefault("bankroll", stored.get("bankroll", "1000"))
    st.session_state.setdefault("freshness_minutes", int(stored.get("freshness_minutes", "5")))
    st.session_state.setdefault("min_roi", float(stored.get("min_roi", "0.25")))
    st.session_state.setdefault("min_ev", float(stored.get("min_ev", "2.0")))
    st.session_state.setdefault("odds_format", DEFAULT_ODDS_FORMAT)
    st.session_state["terminal_defaults_loaded"] = True


def _seed_demo(repository: QuoteRepository) -> None:
    if st.session_state.get("demo_seeded"):
        return
    with _DEMO_SEED_LOCK:
        if not repository.load_latest_quotes("demo"):
            for snapshot in generate_demo_snapshots():
                repository.save_snapshot(snapshot)
        st.session_state["demo_seeded"] = True


def _sidebar(
    repository: QuoteRepository,
    *,
    is_admin: bool,
) -> dict[str, Any]:
    with st.sidebar:
        st.markdown("### BETTOR BUREAU")
        st.caption("Sports-market intelligence")
        saved_oddspapi_key = _local_secret("ODDSPAPI_API_KEY")
        if is_admin:
            data_mode = st.selectbox(
                "Data source",
                list(DATA_SOURCE_IDS),
                index=1 if saved_oddspapi_key else 0,
                key="data_source",
            )
        else:
            data_mode = "OddsPapi Free"
            st.caption("Live odds · refreshed by the owner")
        api_key = ""
        regions = "us"
        if data_mode == "OddsPapi Free":
            api_key = saved_oddspapi_key
            if is_admin:
                requests_used = _oddspapi_requests_used(repository)
                credit_limit = _oddspapi_credit_limit(repository)
                requests_remaining = max(0, credit_limit - requests_used)
                st.caption(f"Connected · {requests_remaining} estimated credits remaining")
                with st.expander("Feed account & usage", expanded=False):
                    api_key = st.text_input(
                        "OddsPapi API key",
                        value=saved_oddspapi_key,
                        type="password",
                    )
                    st.caption(
                        "Each full update checks every configured sportsbook. The first update "
                        "may also cache provider catalogs."
                    )
                    st.progress(
                        min(1.0, requests_used / credit_limit),
                        text=f"{requests_used} used · {requests_remaining} remaining",
                    )
                    st.link_button("OddsPapi account", "https://oddspapi.io/", width="stretch")
        elif data_mode == "The Odds API":
            api_key = st.text_input(
                "The Odds API key",
                value=os.getenv("ODDS_API_KEY", ""),
                type="password",
            )
            regions = st.selectbox(
                "Bookmaker regions",
                ["us", "uk", "us,uk"],
                format_func={
                    "us": "United States",
                    "uk": "United Kingdom (includes Betway)",
                    "us,uk": "US + UK (uses more credits)",
                }.get,
            )
        if is_admin and data_mode != "Demo" and not saved_oddspapi_key:
            st.caption("Your key stays in this local session.")
        with st.expander("Analysis settings", expanded=False):
            bankroll = st.text_input("Working bankroll", key="bankroll")
            freshness = st.slider(
                "Freshness window",
                min_value=1,
                max_value=30,
                key="freshness_minutes",
                format="%d min",
                help=(
                    "Older odds stay visible and are marked stale. They can still appear in "
                    "+EV recommendations; arbitrage and middle results require fresh prices."
                ),
            )
            min_roi = st.number_input(
                "Minimum arb ROI %",
                min_value=0.0,
                max_value=20.0,
                step=0.1,
                key="min_roi",
            )
            odds_format = str(st.session_state.get("odds_format", DEFAULT_ODDS_FORMAT))
        supported_leagues = list(LEAGUE_ICONS)
        supported_markets = ["Moneyline", "Spread", "Total"]
        if data_mode in {"Demo", "OddsPapi Free"}:
            supported_markets.append("Player props")
        if is_admin:
            league_defaults_key = f"refresh_league_defaults_v2_{_provider_id(data_mode)}"
            if not st.session_state.get(league_defaults_key):
                for league in supported_leagues:
                    st.session_state[f"refresh_league_{_provider_id(data_mode)}_{league}"] = True
                st.session_state[league_defaults_key] = True
            with st.expander("Odds Data", expanded=False):
                st.caption("Enabled sports")
                active_leagues = [
                    league
                    for league in supported_leagues
                    if st.checkbox(
                        f"{LEAGUE_ICONS[league]} {league}",
                        key=f"refresh_league_{_provider_id(data_mode)}_{league}",
                    )
                ]
                st.caption("Markets")
                active_markets = [
                    market
                    for market in supported_markets
                    if st.checkbox(
                        market,
                        value=market != "Player props",
                        key=f"refresh_market_{_provider_id(data_mode)}_{market}",
                        help=(
                            "Available on demand; not selected by default."
                            if market == "Player props"
                            else None
                        ),
                    )
                ]
        else:
            active_leagues = supported_leagues
            active_markets = supported_markets
        st.divider()
        refresh = False
        if is_admin:
            refresh = st.button(
                "Refresh Odds",
                type="primary",
                width="stretch",
            )
            st.caption("Manual refresh only—no credits are spent in the background.")
            if st.button("Save preferences", width="stretch"):
                repository.save_setting("bankroll", str(bankroll))
                repository.save_setting("freshness_minutes", str(freshness))
                repository.save_setting("min_roi", str(min_roi))
                repository.save_setting("min_ev", str(st.session_state["min_ev"]))
                st.toast("Preferences saved locally")
        else:
            st.caption("Only the owner can refresh odds or change saved settings.")
        st.caption("Analysis only. No automated betting or account access.")
    return {
        "mode": data_mode,
        "api_key": api_key,
        "regions": regions,
        "bankroll": bankroll,
        "freshness": timedelta(minutes=int(freshness)),
        "min_roi": Decimal(str(min_roi)) / Decimal("100"),
        "odds_format": odds_format,
        "active_leagues": active_leagues,
        "active_markets": active_markets,
        "refresh": refresh,
        "is_admin": is_admin,
    }


def _event_maps(events: tuple[Event, ...]) -> tuple[dict[str, Event], dict[str, str]]:
    event_map = {event.id: event for event in events}
    league_names = {"nfl": "NFL", "ncaaf": "NCAAF", "cfl": "CFL"}
    return event_map, league_names


def _arb_rows(
    opportunities: tuple[ArbitrageOpportunity, ...],
    event_map: dict[str, Event],
    league_names: dict[str, str],
    as_of: datetime,
    min_roi: Decimal,
) -> list[dict[str, object]]:
    rows = []
    for opportunity in opportunities:
        if opportunity.roi < min_roi:
            continue
        event = event_map.get(opportunity.market.event_id)
        rows.append(
            {
                "League": league_names.get(event.league_id, event.league_id) if event else "-",
                "Event": event.name if event else opportunity.market.event_id,
                "Market": _market_label(opportunity.market),
                "Legs": " | ".join(
                    f"{_selection_label(leg.quote)} @ {leg.quote.sportsbook.name} "
                    f"({leg.quote.decimal_odds})"
                    for leg in opportunity.legs
                ),
                "ROI": float(opportunity.roi),
                "Profit": float(opportunity.guaranteed_profit),
                "Freshest": min(_age_label(leg.quote, as_of) for leg in opportunity.legs),
            }
        )
    return rows


def _render_arb_detail(
    opportunities: tuple[ArbitrageOpportunity, ...],
    event_map: dict[str, Event],
    as_of: datetime,
    min_roi: Decimal,
) -> None:
    qualifying = tuple(item for item in opportunities if item.roi >= min_roi)
    rows = _arb_rows(qualifying, event_map, {}, as_of, min_roi)
    if not rows:
        st.info("No pure arbitrage currently clears your ROI and freshness rules.")
        return
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "ROI": st.column_config.NumberColumn(format="%.2%%"),
            "Profit": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    choices = {
        (
            f"{event_map[item.market.event_id].name} | "
            f"{_market_label(item.market)} | {item.roi:.2%}"
        ): item
        for item in qualifying
        if item.market.event_id in event_map
    }
    selected_label = st.selectbox("Execution plan", list(choices))
    selected = choices[selected_label]
    left, right, third = st.columns(3)
    left.metric("Total stake", f"${selected.bankroll:,.2f}")
    right.metric("Guaranteed payout", f"${selected.bankroll + selected.guaranteed_profit:,.2f}")
    third.metric("Guaranteed profit", f"${selected.guaranteed_profit:,.2f}")
    st.dataframe(
        [
            {
                "Selection": _selection_label(leg.quote),
                "Sportsbook": leg.quote.sportsbook.name,
                "Decimal": float(leg.quote.decimal_odds),
                "Stake": float(leg.stake),
                "Gross payout": float(leg.gross_payout),
                "Quote age": _age_label(leg.quote, as_of),
            }
            for leg in selected.legs
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Decimal": st.column_config.NumberColumn(format="%.3f"),
            "Stake": st.column_config.NumberColumn(format="$%.2f"),
            "Gross payout": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    st.warning("Re-check every leg at the sportsbook before placing anything; prices can move.")


def _render_middles(
    middles: tuple[MiddleOpportunity, ...],
    event_map: dict[str, Event],
) -> None:
    if not middles:
        st.info("No fresh spread or total middles are available.")
        return
    rows = []
    for item in middles:
        event = event_map.get(item.event_id)
        rows.append(
            {
                "Event": event.name if event else item.event_id,
                "Market": MARKET_NAMES[item.kind],
                "Middle": item.label,
                "Width": float(item.width),
                "First book": item.first.sportsbook.name,
                "First odds": float(item.first.decimal_odds),
                "Second book": item.second.sportsbook.name,
                "Second odds": float(item.second.decimal_odds),
                "Two-leg hold": float(item.combined_implied_probability - Decimal("1")),
            }
        )
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "First odds": st.column_config.NumberColumn(format="%.2f"),
            "Second odds": st.column_config.NumberColumn(format="%.2f"),
            "Two-leg hold": st.column_config.NumberColumn(format="%.2%%"),
        },
    )
    st.caption("Middles are probabilistic opportunities, not guaranteed arbitrage.")


def _render_value(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    odds_format: str,
) -> None:
    minimum = Decimal(
        str(
            st.number_input(
                "Minimum consensus edge %",
                min_value=0.0,
                max_value=25.0,
                step=0.5,
                key="min_ev",
                help="Only show bets with estimated +EV at or above this percentage.",
            )
        )
    ) / Decimal("100")
    filtered = [item for item in values if item.expected_value >= minimum]
    if not filtered:
        st.info("No quotes clear the selected consensus edge.")
        return
    st.dataframe(
        [
            {
                "Event": (
                    event_map[item.quote.outcome.market.event_id].name
                    if item.quote.outcome.market.event_id in event_map
                    else item.quote.outcome.market.event_id
                ),
                "Market": _market_label(item.quote.outcome.market),
                "Selection": _selection_label(item.quote),
                "Bet at": item.quote.sportsbook.name,
                "Book odds": format_odds(item.quote.decimal_odds, odds_format),
                "Fair odds": format_odds(item.fair_odds, odds_format),
                "Est. +EV": float(item.expected_value * Decimal("100")),
                "Compared with": ", ".join(item.reference_sportsbooks),
            }
            for item in filtered
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Event": st.column_config.TextColumn(width="large", pinned=True),
            "Book odds": st.column_config.TextColumn(
                help="The actual odds offered by one of your selected sportsbooks."
            ),
            "Fair odds": st.column_config.TextColumn(
                "Estimated fair odds",
                help=(
                    "A margin-free estimate derived from the other available sportsbooks. It is "
                    "not an offered betting price."
                ),
            ),
            "Est. +EV": st.column_config.NumberColumn(
                "Estimated +EV",
                format="%.2f%%",
                help="Estimated long-run return relative to the consensus fair probability.",
            ),
            "Compared with": st.column_config.TextColumn(
                help="The other sportsbooks used to calculate the estimated fair odds."
            ),
        },
    )
    st.caption(
        "Only your selected sportsbooks can be recommended. Consensus uses every other "
        "available book after removing its margin."
    )


def _render_best_lines(
    quotes: tuple[Quote, ...],
    event_map: dict[str, Event],
    odds_format: str,
) -> None:
    selected = best_prices(quotes)
    rows = []
    for outcome, quote in selected.items():
        event = event_map.get(outcome.market.event_id)
        rows.append(
            {
                "Event": event.name if event else outcome.market.event_id,
                "Market": _market_label(outcome.market),
                "Selection": _selection_label(quote),
                "Best book": quote.sportsbook.name,
                "Best price": format_odds(quote.decimal_odds, odds_format),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


def _event_odds_frame(
    quotes: tuple[Quote, ...],
    sportsbook_names: list[str],
    odds_format: str,
    event: Event | None = None,
) -> pd.DataFrame:
    selected_books = set(sportsbook_names)
    display_quotes = tuple(quote for quote in quotes if quote.sportsbook.name in selected_books)
    best_price_by_outcome: dict[str, Decimal] = {}
    for quote in display_quotes:
        outcome_id = quote.outcome.id
        best_price_by_outcome[outcome_id] = max(
            best_price_by_outcome.get(outcome_id, Decimal("0")),
            quote.decimal_odds,
        )

    rows: dict[str, dict[str, str]] = {}
    for quote in display_quotes:
        outcome_id = quote.outcome.id
        row = rows.setdefault(
            outcome_id,
            {
                "Market": _market_label(quote.outcome.market),
                "Bet": _selection_label(quote, event),
                **{sportsbook: "—" for sportsbook in sportsbook_names},
            },
        )
        price = format_odds(quote.decimal_odds, odds_format)
        if quote.decimal_odds == best_price_by_outcome[outcome_id]:
            price = f"★ {price}"
        row[quote.sportsbook.name] = price

    ordered_rows = sorted(rows.values(), key=lambda row: (row["Market"], row["Bet"]))
    return pd.DataFrame(ordered_rows, columns=["Market", "Bet", *sportsbook_names])


def _highlight_best_price(value: object) -> str:
    if isinstance(value, str) and value.startswith("★"):
        return (
            "background-color: #123525; color: #6ee7b7; font-weight: 750; border: 1px solid #1f6b48"
        )
    return ""


def _game_market_sections_markup(
    event_quotes: tuple[Quote, ...],
    sportsbook_names: list[str],
    odds_format: str,
    event: Event,
) -> str:
    kind_order = (
        MarketKind.MONEYLINE,
        MarketKind.SPREAD,
        MarketKind.TOTAL,
        MarketKind.PLAYER_PROP,
    )
    sections: list[str] = []
    for kind in kind_order:
        market_quotes = tuple(quote for quote in event_quotes if quote.outcome.market.kind is kind)
        if not market_quotes:
            continue
        quotes_by_outcome: dict[str, dict[str, Quote]] = {}
        for quote in market_quotes:
            book_quotes = quotes_by_outcome.setdefault(quote.outcome.id, {})
            current = book_quotes.get(quote.sportsbook.name)
            if current is None or quote.decimal_odds > current.decimal_odds:
                book_quotes[quote.sportsbook.name] = quote

        ordered_outcomes = sorted(
            quotes_by_outcome.values(),
            key=lambda book_quotes: (
                _market_label(next(iter(book_quotes.values())).outcome.market),
                _selection_label(next(iter(book_quotes.values())), event),
            ),
        )
        rows: list[str] = []
        for book_quotes in ordered_outcomes:
            representative = next(iter(book_quotes.values()))
            selection = _selection_label(representative, event)
            market_label = _market_label(representative.outcome.market)
            best_price = max(quote.decimal_odds for quote in book_quotes.values())
            cells: list[str] = []
            for sportsbook in sportsbook_names:
                book_quote = book_quotes.get(sportsbook)
                if book_quote is None:
                    cells.append('<td class="games-price-cell games-unavailable">—</td>')
                    continue
                displayed_price = format_odds(book_quote.decimal_odds, odds_format)
                is_best = book_quote.decimal_odds == best_price
                sportsbook_url = _sportsbook_event_url(book_quote) or _sportsbook_bet_url(
                    book_quote
                )
                if sportsbook_url:
                    aria_label = html.escape(
                        f"Bet {selection} {market_label} at {displayed_price} on {sportsbook}",
                        quote=True,
                    )
                    best_class = " best" if is_best else ""
                    external_marker = " ↗" if is_best else ""
                    cells.append(
                        '<td class="games-price-cell">'
                        f'<a class="games-price-link{best_class}" '
                        f'href="{html.escape(sportsbook_url, quote=True)}" target="_blank" '
                        f'rel="noopener noreferrer" aria-label="{aria_label}" '
                        f'title="{aria_label}">'
                        f"{displayed_price}{external_marker}</a></td>"
                    )
                else:
                    best_class = " best" if is_best else ""
                    cells.append(
                        f'<td class="games-price-cell"><span class="games-price-link'
                        f'{best_class}">{displayed_price}</span></td>'
                    )
            rows.append(
                '<tr><td class="games-selection">'
                f"<strong>{html.escape(selection)}</strong>"
                f"<small>{html.escape(market_label)}</small></td>" + "".join(cells) + "</tr>"
            )

        book_headers = "".join(
            f"<th>{html.escape(sportsbook)}</th>" for sportsbook in sportsbook_names
        )
        market_name = MARKET_NAMES[kind]
        open_attribute = " open" if not sections else ""
        sections.append(
            f'<details class="games-market-group"{open_attribute}>'
            f"<summary>{html.escape(market_name)}</summary>"
            '<div class="games-odds-scroll"><table class="games-odds-table">'
            f"<thead><tr><th>Selection</th>{book_headers}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></details>"
        )
    return "".join(sections)


def _game_event_markup(
    event: Event,
    event_quotes: tuple[Quote, ...],
    sportsbook_names: list[str],
    odds_format: str,
) -> str:
    local_start = event.start_time.astimezone(DISPLAY_TIMEZONE)
    time_label = local_start.strftime("%I:%M %p").lstrip("0")
    home_name, away_name = _event_team_names(event)
    away_logo = _team_logo_markup(away_name, event.league_id)
    home_logo = _team_logo_markup(home_name, event.league_id)
    market_markup = _game_market_sections_markup(
        event_quotes,
        sportsbook_names,
        odds_format,
        event,
    )
    if not market_markup:
        market_markup = (
            '<div class="games-odds-pending"><strong>Odds not available yet</strong>'
            "<span>This game will populate automatically after sportsbooks post lines.</span>"
            "</div>"
        )
    return (
        '<details class="games-event">'
        "<summary>"
        f'<span class="games-time">{html.escape(time_label)}</span>'
        '<span class="games-matchup"><span class="games-team-logos">'
        f"{away_logo}{home_logo}</span>"
        '<span class="games-matchup-copy">'
        f"<strong>{html.escape(event.name)}</strong>"
        f"<small>{html.escape(event.league_id.upper())}</small></span></span>"
        '<span class="games-chevron">⌄</span>'
        "</summary>"
        f'<div class="games-event-body">{market_markup}</div></details>'
    )


def _render_event_board(
    quotes: tuple[Quote, ...],
    events: tuple[Event, ...],
    sportsbook_names: list[str],
    odds_format: str,
    repository: QuoteRepository | None = None,
    *,
    is_admin: bool = False,
) -> None:
    available_events = sorted(
        events,
        key=lambda event: (event.start_time, event.name),
    )
    if not available_events:
        st.info("No saved upcoming events match the selected leagues. Refresh the latest odds.")
        return
    grouped_quotes: dict[str, list[Quote]] = {event.id: [] for event in available_events}
    for quote in quotes:
        event_quotes = grouped_quotes.get(quote.outcome.market.event_id)
        if event_quotes is not None:
            event_quotes.append(quote)
    quotes_by_event = {
        event_id: tuple(event_quotes) for event_id, event_quotes in grouped_quotes.items()
    }

    # Reset the older conflicting Games filters once. Previously a saved NFL league
    # could remain active while the sport control visibly said "All Sports".
    if not st.session_state.get("games_filter_defaults_v2"):
        st.session_state["games_sport"] = "All Sports"
        st.session_state["games_league"] = "All leagues"
        st.session_state["games_date"] = "All upcoming"
        st.session_state["games_filter_defaults_v2"] = True

    sport_counts: dict[str, int] = {}
    for event in available_events:
        sport = LEAGUE_SPORTS.get(event.league_id, "Other")
        sport_counts[sport] = sport_counts.get(sport, 0) + 1
    sport_options = [
        "All Sports",
        *sorted(sport_counts),
    ]
    if st.session_state.get("games_sport") not in sport_options:
        st.session_state["games_sport"] = "All Sports"
    with st.container(key="games_sport_filter"):
        selected_sport = (
            st.segmented_control(
                "Sport",
                sport_options,
                default="All Sports",
                key="games_sport",
                label_visibility="collapsed",
                format_func=lambda sport: (
                    f"All Sports ({len(available_events)})"
                    if sport == "All Sports"
                    else f"{sport} ({sport_counts[sport]})"
                ),
            )
            or "All Sports"
        )

    league_events = (
        available_events
        if selected_sport == "All Sports"
        else [
            event
            for event in available_events
            if LEAGUE_SPORTS.get(event.league_id, "Other") == selected_sport
        ]
    )
    league_options = [
        "All leagues",
        *sorted({event.league_id.upper() for event in league_events}),
    ]
    league_counts: dict[str, int] = {}
    for event in league_events:
        league = event.league_id.upper()
        league_counts[league] = league_counts.get(league, 0) + 1
    if st.session_state.get("games_league") not in league_options:
        st.session_state["games_league"] = "All leagues"
    with st.container(key="games_filters"):
        search_column, league_column, date_column = st.columns(
            [2.2, 1, 1], vertical_alignment="bottom"
        )
        with search_column:
            search_query = (
                st.text_input(
                    "Search games, teams, or players",
                    placeholder="Search games, teams, or players…",
                    key="games_search",
                )
                .strip()
                .casefold()
            )
        with league_column:
            selected_league = st.selectbox(
                "League",
                league_options,
                key="games_league",
                format_func=lambda league: (
                    f"All leagues ({len(league_events)})"
                    if league == "All leagues"
                    else f"{league} ({league_counts[league]})"
                ),
            )
        with date_column:
            selected_date = st.selectbox(
                "Date",
                ["All upcoming", "Today", "Tomorrow", "Next 7 days"],
                key="games_date",
            )

    local_today = datetime.now(DISPLAY_TIMEZONE).date()
    filtered_events: list[Event] = []
    for event in available_events:
        event_quote_rows = quotes_by_event[event.id]
        home_name, away_name = _event_team_names(event)
        searchable = " ".join(
            [
                event.name,
                home_name,
                away_name,
                event.league_id,
                *(_selection_label(quote, event) for quote in event_quote_rows),
            ]
        ).casefold()
        event_date = event.start_time.astimezone(DISPLAY_TIMEZONE).date()
        date_matches = (
            selected_date == "All upcoming"
            or (selected_date == "Today" and event_date == local_today)
            or (selected_date == "Tomorrow" and event_date == local_today + timedelta(days=1))
            or (
                selected_date == "Next 7 days"
                and local_today <= event_date <= local_today + timedelta(days=7)
            )
        )
        if (
            search_query
            and search_query not in searchable
            or selected_league != "All leagues"
            and event.league_id.upper() != selected_league
            or selected_sport != "All Sports"
            and LEAGUE_SPORTS.get(event.league_id, "Other") != selected_sport
            or not date_matches
        ):
            continue
        filtered_events.append(event)

    st.markdown(
        '<div class="games-heading"><div><h2>All Games</h2>'
        "<p>Find an event, open its markets, and compare every available sportsbook.</p>"
        f"</div><span>{len(filtered_events)} upcoming games</span></div>",
        unsafe_allow_html=True,
    )
    if not filtered_events:
        st.markdown(
            '<div class="games-empty">No games match those filters.</div>',
            unsafe_allow_html=True,
        )
        return

    visible_count = max(10, int(st.session_state.get("games_visible_count", 10)))
    visible_events = filtered_events[:visible_count]
    day_groups: dict[date, list[Event]] = {}
    for event in visible_events:
        day_groups.setdefault(event.start_time.astimezone(DISPLAY_TIMEZONE).date(), []).append(
            event
        )
    for event_date, day_events in day_groups.items():
        if event_date == local_today:
            day_label = "Today"
        elif event_date == local_today + timedelta(days=1):
            day_label = "Tomorrow"
        else:
            day_label = day_events[0].start_time.astimezone(DISPLAY_TIMEZONE).strftime("%A · %b %d")
        st.markdown(
            '<div class="games-day-heading">'
            f"<strong>{html.escape(day_label)}</strong>"
            f"<span>{len(day_events)} game{'s' if len(day_events) != 1 else ''}</span></div>",
            unsafe_allow_html=True,
        )
        event_rows = "".join(
            _game_event_markup(
                event,
                quotes_by_event[event.id],
                sportsbook_names,
                odds_format,
            )
            for event in day_events
        )
        st.markdown(
            f'<div class="games-list">{event_rows}</div>',
            unsafe_allow_html=True,
        )

    if len(filtered_events) > visible_count and st.button(
        "Load more games", key="games_load_more", width="stretch"
    ):
        st.session_state["games_visible_count"] = visible_count + 10
        st.rerun()


def _sportsbook_toggle_key(mode: str, book: str) -> str:
    return f"my_sportsbook_{stable_id('ui-book-v4', mode, book)}"


_SPORTSBOOK_PREFERENCES_COMPONENT = st.components.v2.component(
    "sportsbook_preferences_storage",
    html='<span class="sportsbook-preferences-storage" aria-hidden="true"></span>',
    css=".sportsbook-preferences-storage { display: none; }",
    js="""
    export default function({ data, setStateValue }) {
        const storageKey = data.storageKey;
        const fallback = Array.isArray(data.defaultSelected)
            ? data.defaultSelected
            : [];
        let selected = fallback;

        try {
            if (Array.isArray(data.writeSelected)) {
                selected = data.writeSelected;
                localStorage.setItem(storageKey, JSON.stringify(selected));
            } else {
                const stored = localStorage.getItem(storageKey);
                if (stored === null) {
                    localStorage.setItem(storageKey, JSON.stringify(fallback));
                } else {
                    const parsed = JSON.parse(stored);
                    if (
                        Array.isArray(parsed)
                        && parsed.every((book) => typeof book === "string")
                    ) {
                        selected = parsed;
                    }
                }
            }
        } catch (_error) {
            selected = fallback;
        }

        setStateValue("selected", selected);
        setStateValue("ready", true);
    }
    """,
)


def _sportsbook_preferences_storage_key(mode: str) -> str:
    provider = _provider_id(mode).replace("-", "_")
    return f"bettor_bureau_sportsbooks_{provider}"


def _sportsbook_preferences_loaded_key(mode: str) -> str:
    return f"sportsbook_preferences_loaded_{stable_id('browser-storage-v1', mode)}"


def _sportsbook_default_enabled(book: str) -> bool:
    _ = book
    return True


def _decode_sportsbook_preferences(
    raw_value: str | list[str] | tuple[str, ...] | None,
    available_books: tuple[str, ...],
) -> tuple[str, ...] | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        try:
            decoded = json.loads(url_unquote(raw_value))
        except (TypeError, ValueError):
            return None
    else:
        decoded = list(raw_value)
    if not isinstance(decoded, list) or not all(isinstance(book, str) for book in decoded):
        return None
    selected = set(decoded)
    return tuple(book for book in available_books if book in selected)


def _queue_sportsbook_preferences_storage(
    books: tuple[str, ...],
    mode: str,
) -> None:
    selected = [
        book
        for book in books
        if bool(st.session_state.get(_sportsbook_toggle_key(mode, book), False))
    ]
    storage_key = _sportsbook_preferences_storage_key(mode)
    st.session_state[f"pending_{storage_key}"] = selected
    st.session_state[_sportsbook_preferences_loaded_key(mode)] = True
    _set_ev_page(0)


def _render_sportsbook_preferences_storage(
    mode: str,
    available_books: tuple[str, ...],
) -> tuple[str, ...] | None:
    storage_key = _sportsbook_preferences_storage_key(mode)
    pending_value = st.session_state.pop(f"pending_{storage_key}", None)
    result = _SPORTSBOOK_PREFERENCES_COMPONENT(
        data={
            "storageKey": storage_key,
            "defaultSelected": list(available_books),
            "writeSelected": pending_value,
        },
        default={"selected": list(available_books), "ready": False},
        on_selected_change=lambda: None,
        on_ready_change=lambda: None,
        key=f"sportsbook_preferences_{stable_id('browser-storage-v1', mode)}",
        width=1,
        height=1,
    )
    if not result.ready:
        return None
    return _decode_sportsbook_preferences(result.selected, available_books)


def _set_sportsbook_selection(
    books: tuple[str, ...],
    mode: str,
    enabled: bool,
) -> None:
    for book in books:
        st.session_state[_sportsbook_toggle_key(mode, book)] = enabled
    _queue_sportsbook_preferences_storage(books, mode)


def _reset_secondary_ev_filters() -> None:
    st.session_state["ev_implied_preset"] = "Any"
    st.session_state["ev_custom_implied"] = 0.0
    st.session_state["ev_odds_range_enabled"] = False
    st.session_state["ev_min_american"] = -200
    st.session_state["ev_max_american"] = 300
    st.session_state["ev_consensus_books"] = "Any"
    st.session_state["ev_start_window"] = "Any time"
    st.session_state["ev_freshness_filter"] = "Include stale"
    st.session_state["ev_page"] = 0


def _reset_ev_filters() -> None:
    _reset_secondary_ev_filters()
    st.session_state["ev_sport_filter"] = "All Sports"
    st.session_state["ev_market_filter"] = "All Markets"
    st.session_state["ev_minimum_preset"] = "2%+"
    st.session_state["ev_sort_by"] = "EV % (High to Low)"


def _set_ev_page(page: int) -> None:
    st.session_state["ev_page"] = max(0, page)
    st.session_state["ev_visible_count"] = EV_INITIAL_BATCH_SIZE


def _show_more_ev_bets(increment: int = EV_BATCH_SIZE) -> None:
    current = max(
        EV_INITIAL_BATCH_SIZE,
        int(st.session_state.get("ev_visible_count", EV_INITIAL_BATCH_SIZE)),
    )
    st.session_state["ev_visible_count"] = current + increment


def _render_ev_filter_bar(
    available_books: list[str],
    mode: str,
    events: tuple[Event, ...],
    quotes: tuple[Quote, ...],
    as_of: datetime,
) -> EVFilterState:
    stored_sportsbooks = _render_sportsbook_preferences_storage(
        mode,
        tuple(available_books),
    )
    if not st.session_state.get("ev_reference_defaults_v5"):
        st.session_state["ev_implied_preset"] = "Any"
        st.session_state["ev_custom_implied"] = 0.0
        st.session_state["ev_odds_range_enabled"] = False
        st.session_state["ev_min_american"] = -200
        st.session_state["ev_max_american"] = 300
        st.session_state["ev_consensus_books"] = "Any"
        st.session_state["ev_start_window"] = "Any time"
        st.session_state["ev_market_filter"] = "All Markets"
        st.session_state["ev_minimum_preset"] = "2%+"
        st.session_state["min_ev"] = 2.0
        st.session_state["ev_reference_defaults_v5"] = True
    if not st.session_state.get("ev_all_markets_default_v1"):
        if st.session_state.get("ev_market_filter") in {None, "Moneyline"}:
            st.session_state["ev_market_filter"] = "All Markets"
        st.session_state["ev_all_markets_default_v1"] = True
    if not st.session_state.get("ev_default_two_v1"):
        st.session_state["ev_minimum_preset"] = "2%+"
        st.session_state["min_ev"] = 2.0
        st.session_state["ev_default_two_v1"] = True
    st.session_state.setdefault("ev_implied_preset", "Any")
    st.session_state.setdefault("ev_custom_implied", 0.0)
    st.session_state.setdefault("ev_odds_range_enabled", False)
    st.session_state.setdefault("ev_min_american", -200)
    st.session_state.setdefault("ev_max_american", 300)
    league_ids = sorted({event.league_id for event in events})
    league_options = ["All Sports", *(league_id.upper() for league_id in league_ids)]
    available_kinds = {quote.outcome.market.kind for quote in quotes}
    market_options = [
        "All Markets",
        *(
            MARKET_NAMES[kind]
            for kind in (
                MarketKind.MONEYLINE,
                MarketKind.SPREAD,
                MarketKind.TOTAL,
                MarketKind.PLAYER_PROP,
            )
            if kind in available_kinds
        ),
    ]
    preset_options = ["Any positive EV", "1%+", "2%+", "3%+", "5%+"]
    if st.session_state.get("ev_minimum_preset") == "Custom":
        st.session_state["ev_minimum_preset"] = "2%+"
    st.session_state.setdefault(
        "ev_minimum_preset",
        f"{int(float(st.session_state['min_ev']))}%+"
        if float(st.session_state["min_ev"]) in {1.0, 2.0, 3.0, 5.0}
        else "2%+",
    )

    with st.container(key="ev_filter_bar"):
        sport_col, market_col, ev_col, book_col, more_col, _, sort_label_col, sort_col = st.columns(
            [1.08, 1.05, 1.0, 1.25, 1.05, 1.5, 0.45, 1.35],
            vertical_alignment="bottom",
        )
        sport = sport_col.selectbox(
            "Sport",
            league_options,
            key="ev_sport_filter",
            label_visibility="collapsed",
            on_change=_set_ev_page,
            args=(0,),
        )
        market = market_col.selectbox(
            "Market",
            market_options,
            key="ev_market_filter",
            label_visibility="collapsed",
            on_change=_set_ev_page,
            args=(0,),
        )
        minimum_preset = ev_col.selectbox(
            "Minimum EV",
            preset_options,
            key="ev_minimum_preset",
            label_visibility="collapsed",
            format_func=lambda option: (
                "Any positive EV"
                if option == "Any positive EV"
                else f"EV ≥ {option.removesuffix('+')}"
            ),
            on_change=_set_ev_page,
            args=(0,),
        )

        preference_session_key = _sportsbook_preferences_loaded_key(mode)
        if not st.session_state.get(preference_session_key):
            if stored_sportsbooks is not None:
                for book in available_books:
                    st.session_state[_sportsbook_toggle_key(mode, book)] = (
                        book in stored_sportsbooks
                    )
                st.session_state[preference_session_key] = True
            else:
                for book in available_books:
                    st.session_state.setdefault(
                        _sportsbook_toggle_key(mode, book),
                        _sportsbook_default_enabled(book),
                    )
        else:
            for book in available_books:
                st.session_state.setdefault(
                    _sportsbook_toggle_key(mode, book),
                    _sportsbook_default_enabled(book),
                )
        selected_before = tuple(
            book
            for book in available_books
            if bool(st.session_state.get(_sportsbook_toggle_key(mode, book), True))
        )
        book_count = str(len(selected_before))
        with book_col.popover(f"My Sportsbooks ({book_count})", width="stretch"):
            st.caption(
                "Only bets available at your selected sportsbooks appear anywhere on "
                "the Best Bets page. Fair odds still use the complete sportsbook market."
            )
            st.caption("Choose your books, then apply once.")
            with st.form(
                f"sportsbook_filter_{stable_id('sportsbook-form-v1', mode)}",
                border=False,
            ):
                toggle_columns = st.columns(2, gap="small")
                for index, book in enumerate(available_books):
                    with toggle_columns[index % 2]:
                        st.toggle(
                            book,
                            key=_sportsbook_toggle_key(mode, book),
                        )
                st.form_submit_button(
                    "Apply sportsbooks",
                    type="primary",
                    width="stretch",
                    on_click=_queue_sportsbook_preferences_storage,
                    args=(tuple(available_books), mode),
                )
                st.form_submit_button(
                    "Use all books",
                    on_click=_set_sportsbook_selection,
                    args=(
                        tuple(available_books),
                        mode,
                        True,
                    ),
                    width="stretch",
                    help=(
                        "Selects every sportsbook currently available in the feed."
                    ),
                )
                st.caption(
                    "Selections are saved in this browser for your next visit."
                )

        with more_col.popover("More Filters", width="stretch"):
            st.caption(
                "These controls refine All +EV Bets. My Sportsbooks is the only "
                "personal filter applied to Recommended Bets; it does not change "
                "the global strategy or its tracking."
            )
            with st.form("secondary_ev_filters", border=False):
                implied_preset = st.selectbox(
                    "Minimum break-even probability",
                    ["Any", "10%+", "20%+", "30%+", "40%+", "50%+", "Custom"],
                    key="ev_implied_preset",
                    help=(
                        "How often the bet needs to win at the offered odds to break even over "
                        "time. For example, +225 requires a 30.8% win rate."
                    ),
                )
                st.number_input(
                    "Custom probability % (used when Custom is selected)",
                    min_value=0.0,
                    max_value=100.0,
                    step=1.0,
                    key="ev_custom_implied",
                )
                use_odds_range = st.toggle(
                    "Use offered odds range",
                    key="ev_odds_range_enabled",
                )
                odds_columns = st.columns(2)
                odds_columns[0].number_input(
                    "Minimum American odds",
                    step=10,
                    key="ev_min_american",
                )
                odds_columns[1].number_input(
                    "Maximum American odds",
                    step=10,
                    key="ev_max_american",
                )
                consensus_preset = st.selectbox(
                    "Minimum consensus books",
                    ["Any", "3+", "4+", "5+", "6+", "7+"],
                    key="ev_consensus_books",
                )
                start_window = st.selectbox(
                    "Game start time",
                    [
                        "Pre-game",
                        "Any time",
                        "Next 6 hours",
                        "Next 12 hours",
                        "Next 24 hours",
                        "Next 3 days",
                    ],
                    key="ev_start_window",
                )
                freshness_filter = st.selectbox(
                    "Price freshness",
                    ["Include stale", "Fresh only"],
                    key="ev_freshness_filter",
                )
                st.form_submit_button(
                    "Apply filters",
                    type="primary",
                    width="stretch",
                    on_click=_set_ev_page,
                    args=(0,),
                )
                st.form_submit_button(
                    "Reset secondary filters",
                    on_click=_reset_secondary_ev_filters,
                    width="stretch",
                )

        sort_label_col.markdown('<div class="ev-sort-label">Sort by:</div>', unsafe_allow_html=True)
        sort_by = sort_col.selectbox(
            "Sort by",
            [
                "EV % (High to Low)",
                "Starting Soon",
                "Best Odds",
                "Recently Updated",
            ],
            key="ev_sort_by",
            label_visibility="collapsed",
            on_change=_set_ev_page,
            args=(0,),
        )

    minimum_ev = {
        "Any positive EV": Decimal("0"),
        "1%+": Decimal("0.01"),
        "2%+": Decimal("0.02"),
        "3%+": Decimal("0.03"),
        "5%+": Decimal("0.05"),
    }[minimum_preset]
    st.session_state["min_ev"] = float(minimum_ev * Decimal("100"))

    my_books = tuple(
        book
        for book in available_books
        if bool(st.session_state.get(_sportsbook_toggle_key(mode, book), True))
    )
    implied_preset = str(st.session_state.get("ev_implied_preset", "Any"))
    if implied_preset == "Custom":
        implied_percent = Decimal(str(st.session_state.get("ev_custom_implied", 0.0)))
    elif implied_preset == "Any":
        implied_percent = Decimal("0")
    else:
        implied_percent = Decimal(implied_preset.rstrip("%+"))

    use_odds_range = bool(st.session_state.get("ev_odds_range_enabled", False))
    minimum_american = (
        int(st.session_state.get("ev_min_american", -200)) if use_odds_range else None
    )
    maximum_american = int(st.session_state.get("ev_max_american", 300)) if use_odds_range else None
    minimum_consensus = int(consensus_preset.rstrip("+")) if consensus_preset != "Any" else 2
    starts_before = {
        "Pre-game": None,
        "Any time": None,
        "Next 6 hours": as_of + timedelta(hours=6),
        "Next 12 hours": as_of + timedelta(hours=12),
        "Next 24 hours": as_of + timedelta(hours=24),
        "Next 3 days": as_of + timedelta(days=3),
    }[start_window]

    chips: list[str] = []
    if implied_percent > 0:
        chips.append(f"Recommended probability ≥ {implied_percent}%")
    if use_odds_range:
        chips.append(f"Recommended odds: {minimum_american:+d} to {maximum_american:+d}")
    if consensus_preset != "Any":
        chips.append(f"Consensus {consensus_preset}")
    if start_window != "Any time":
        chips.append(start_window)
    if freshness_filter == "Fresh only":
        chips.append("Fresh only")
    if chips:
        chip_markup = "".join(
            f'<span class="ev-filter-chip">{html.escape(chip)}</span>' for chip in chips
        )
        chip_width = min(6.0, max(1.2, 1.35 * len(chips)))
        chip_col, clear_col, _ = st.columns([chip_width, 1, 11 - chip_width])
        chip_col.markdown(
            f'<div class="ev-filter-chips">{chip_markup}</div>',
            unsafe_allow_html=True,
        )
        clear_col.button(
            "Clear all",
            on_click=_reset_ev_filters,
            type="tertiary",
            width="stretch",
        )

    label_to_kind = {label: kind for kind, label in MARKET_NAMES.items()}
    return EVFilterState(
        league_id=None if sport == "All Sports" else sport.lower(),
        market_kind=None if market == "All Markets" else label_to_kind[market],
        minimum_ev=minimum_ev,
        my_books=my_books,
        minimum_implied_probability=implied_percent / Decimal("100"),
        minimum_american_odds=minimum_american,
        maximum_american_odds=maximum_american,
        minimum_consensus_books=minimum_consensus,
        starts_before=starts_before,
        fresh_only=freshness_filter == "Fresh only",
        sort_by=sort_by,
    )


def _filter_value_opportunities(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    filters: EVFilterState,
    *,
    as_of: datetime,
    max_age: timedelta,
) -> tuple[ValueOpportunity, ...]:
    filtered: list[ValueOpportunity] = []
    for item in best_value_by_outcome(values):
        event = event_map.get(item.quote.outcome.market.event_id)
        if event is None:
            continue
        if filters.league_id is not None and event.league_id != filters.league_id:
            continue
        if (
            filters.market_kind is not None
            and item.quote.outcome.market.kind is not filters.market_kind
        ):
            continue
        if item.expected_value <= 0 or item.expected_value < filters.minimum_ev:
            continue
        offered_probability = implied_probability(item.quote.decimal_odds)
        if offered_probability < filters.minimum_implied_probability:
            continue
        american_odds = decimal_to_american(item.quote.decimal_odds)
        if (
            filters.minimum_american_odds is not None
            and american_odds < filters.minimum_american_odds
        ):
            continue
        if (
            filters.maximum_american_odds is not None
            and american_odds > filters.maximum_american_odds
        ):
            continue
        if item.reference_books < filters.minimum_consensus_books:
            continue
        if filters.starts_before is not None and event.start_time > filters.starts_before:
            continue
        if filters.fresh_only and not is_fresh(item.quote, as_of=as_of, max_age=max_age):
            continue
        filtered.append(item)

    if filters.sort_by == "Starting Soon":
        filtered.sort(key=lambda item: event_map[item.quote.outcome.market.event_id].start_time)
    elif filters.sort_by == "Best Odds":
        filtered.sort(key=lambda item: item.quote.decimal_odds, reverse=True)
    elif filters.sort_by == "Recently Updated":
        filtered.sort(key=lambda item: item.quote.source_updated_at, reverse=True)
    else:
        filtered.sort(key=lambda item: item.expected_value, reverse=True)
    return tuple(filtered)


def _sort_more_ev_values(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    sort_by: str,
) -> tuple[ValueOpportunity, ...]:
    if sort_by == "Starting Soon":
        return tuple(
            sorted(
                values,
                key=lambda item: event_map[item.quote.outcome.market.event_id].start_time,
            )
        )
    if sort_by == "Best Odds":
        return tuple(sorted(values, key=lambda item: item.quote.decimal_odds, reverse=True))
    if sort_by == "Win Probability":
        return tuple(sorted(values, key=lambda item: item.fair_probability, reverse=True))
    return tuple(sorted(values, key=lambda item: item.expected_value, reverse=True))


def _recommended_value_opportunities(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    *,
    as_of: datetime,
    limit: int | None = None,
    style: str = "Balanced",
) -> tuple[ValueOpportunity, ...]:
    """Return the official risk-adjusted slate used by the refresh publisher."""
    _ = style
    return tuple(
        recommendation.opportunity
        for recommendation in select_official_recommendations(
            values,
            event_map,
            as_of=as_of,
            bankroll_units=OFFICIAL_STARTING_BANKROLL_UNITS,
            limit=limit,
        )
    )


def _official_bets(bets: tuple[TrackedBet, ...]) -> tuple[TrackedBet, ...]:
    return strategy_official_bets(bets)


def _official_performance(bets: tuple[TrackedBet, ...]) -> OfficialPerformance:
    official = _official_bets(bets)
    wins = sum(bet.status is BetStatus.WON for bet in official)
    losses = sum(bet.status is BetStatus.LOST for bet in official)
    voids = sum(bet.status is BetStatus.VOID for bet in official)
    pending = sum(bet.status is BetStatus.PENDING for bet in official)
    units = sum((bet.profit_loss or Decimal("0") for bet in official), Decimal("0"))
    settled_stake = sum(
        (bet.stake for bet in official if bet.status in {BetStatus.WON, BetStatus.LOST}),
        Decimal("0"),
    )
    roi = units / settled_stake if settled_stake else Decimal("0")
    return OfficialPerformance(
        wins=wins,
        losses=losses,
        voids=voids,
        pending=pending,
        units=units,
        roi=roi,
        bankroll=OFFICIAL_STARTING_BANKROLL_UNITS + units,
    )


def _official_bankroll_history(
    bets: tuple[TrackedBet, ...],
    *,
    as_of: datetime,
) -> pd.DataFrame:
    """Build a dollar-denominated bankroll series from settled official bets."""
    official = _official_bets(bets)
    settled = sorted(
        (
            bet
            for bet in official
            if bet.status is not BetStatus.PENDING and bet.profit_loss is not None
        ),
        key=lambda bet: bet.settled_at or bet.created_at,
    )
    first_activity = min(
        (bet.created_at for bet in official),
        default=as_of - timedelta(days=1),
    )
    balance = OFFICIAL_STARTING_BANKROLL_UNITS * OFFICIAL_UNIT_VALUE_DOLLARS
    rows: list[dict[str, object]] = [
        {
            "Date": first_activity,
            "Bankroll ($)": float(balance),
        }
    ]
    for bet in settled:
        balance += (bet.profit_loss or Decimal("0")) * OFFICIAL_UNIT_VALUE_DOLLARS
        rows.append(
            {
                "Date": bet.settled_at or bet.created_at,
                "Bankroll ($)": float(balance),
            }
        )
    last_date = settled[-1].settled_at or settled[-1].created_at if settled else first_activity
    if last_date < as_of:
        rows.append({"Date": as_of, "Bankroll ($)": float(balance)})
    return pd.DataFrame(rows)


def _quotes_for_opportunity(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
) -> tuple[Quote, ...]:
    matching = tuple(quote for quote in quotes if quote.outcome.id == opportunity.quote.outcome.id)
    return tuple(
        sorted(
            deduplicate_quotes(matching),
            key=lambda quote: (quote.decimal_odds, quote.sportsbook.name),
            reverse=True,
        )
    )


def _quotes_by_outcome(quotes: tuple[Quote, ...]) -> dict[str, tuple[Quote, ...]]:
    """Index and deduplicate prices once for fast board-row comparisons."""
    grouped: dict[str, list[Quote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.outcome.id, []).append(quote)
    return {
        outcome_id: tuple(
            sorted(
                deduplicate_quotes(tuple(outcome_quotes)),
                key=lambda quote: (quote.decimal_odds, quote.sportsbook.name),
                reverse=True,
            )
        )
        for outcome_id, outcome_quotes in grouped.items()
    }


def _market_range_label(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
    odds_format: str,
) -> str:
    prices = [quote.decimal_odds for quote in _quotes_for_opportunity(opportunity, quotes)]
    if not prices:
        return "—"
    return f"{format_odds(min(prices), odds_format)} to {format_odds(max(prices), odds_format)}"


def _format_edge(value: Decimal) -> str:
    return f"+{value:.2%}" if value >= 0 else f"{value:.2%}"


def _sportsbook_event_url(quote: Quote) -> str | None:
    """Return an event deep link only when it matches the expected sportsbook domain."""
    candidate = (quote.source_url or "").strip()
    allowed_domains = SPORTSBOOK_DOMAINS.get(quote.sportsbook.name, ())
    if candidate and allowed_domains:
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains
        ):
            return candidate
    return None


def _sportsbook_bet_url(quote: Quote) -> str | None:
    return _sportsbook_event_url(quote) or SPORTSBOOK_URLS.get(quote.sportsbook.name)


def _render_ev_summary(
    quotes: tuple[Quote, ...],
    values: tuple[ValueOpportunity, ...],
) -> None:
    games = len({quote.outcome.market.event_id for quote in quotes})
    books = len({quote.sportsbook.id for quote in quotes})
    st.markdown(
        '<div class="ev-summary-line">'
        f"<span><strong>{len(values)}</strong> qualifying +EV bets</span>"
        f"<span><strong>{games}</strong> upcoming games</span>"
        f"<span><strong>{books}</strong> sportsbooks compared</span>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_recommended_value_card_legacy(
    opportunity: ValueOpportunity,
    rank: int,
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
) -> None:
    event = event_map.get(opportunity.quote.outcome.market.event_id)
    selection = _selection_label(opportunity.quote, event)
    market_label = _market_label(opportunity.quote.outcome.market)
    offered_odds = format_odds(opportunity.quote.decimal_odds, odds_format)
    fair_odds = format_odds(opportunity.fair_odds, odds_format)
    sportsbook = opportunity.quote.sportsbook.name
    sportsbook_event_url = _sportsbook_event_url(opportunity.quote)
    sportsbook_url = sportsbook_event_url or _sportsbook_bet_url(opportunity.quote)
    card_key = stable_id(
        "recommended-card",
        rank,
        opportunity.quote.sportsbook.id,
        opportunity.quote.outcome.id,
    )
    with st.container(border=True, key=f"value_opportunity_{card_key}"):
        st.markdown(
            f'<div class="recommended-card-badge">#{rank} Recommended bet</div>'
            f'<div class="value-bet-pick">{html.escape(selection)}</div>'
            f'<div class="value-bet-event">{html.escape(event.name) if event else "Event"} · '
            f"{html.escape(market_label)}"
            + (
                f" · {event.start_time.astimezone(DISPLAY_TIMEZONE).strftime('%a %b %d, %I:%M %p')}"
                if event
                else ""
            )
            + "</div>"
            '<div class="recommended-card-grid">'
            '<div class="recommended-card-metric"><small>Bet at</small>'
            f"<strong>{html.escape(sportsbook)}</strong></div>"
            '<div class="recommended-card-metric"><small>Best odds</small>'
            f'<strong class="positive">{offered_odds}</strong></div>'
            '<div class="recommended-card-metric"><small>Estimated EV</small>'
            f'<strong class="positive">{_format_edge(opportunity.expected_value)}</strong></div>'
            "</div>"
            f'<div class="value-bet-proof">Fair odds {fair_odds} · '
            f"{opportunity.reference_books}-book consensus · "
            f"{_recommendation_freshness(opportunity.quote, as_of)}</div>",
            unsafe_allow_html=True,
        )
        if sportsbook_url:
            st.link_button(
                f"Bet {offered_odds} on {sportsbook}",
                sportsbook_url,
                type="primary",
                icon=":material/open_in_new:",
                width="stretch",
            )
        with st.expander("View odds comparison", expanded=False):
            _render_value_comparison(opportunity, quotes, odds_format, as_of)


def _render_priority_value_bets_legacy(
    values: tuple[ValueOpportunity, ...],
    recommended_values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
) -> None:
    if not values:
        st.markdown(
            '<div class="ev-empty"><strong>No +EV bets match these filters.</strong>'
            "Try lowering your minimum EV, expanding your sportsbook selection, or "
            "clearing some filters.</div>",
            unsafe_allow_html=True,
        )
        return

    ranked_values = tuple(sorted(values, key=lambda item: item.expected_value, reverse=True))
    recommended = recommended_values
    st.markdown(
        f'<div class="ev-list-title">Recommended Bets '
        f'<span class="ev-count-badge">{len(recommended)}</span></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Our criteria: 2%+ EV · 30%+ break-even probability · odds -200 to +300 · "
        "4+ comparison books · upcoming events"
    )
    if not recommended:
        st.markdown(
            '<div class="ev-empty"><strong>No bets currently meet all recommendation '
            "criteria.</strong></div>",
            unsafe_allow_html=True,
        )
        return
    top = recommended[0]
    event = event_map.get(top.quote.outcome.market.event_id)
    selection = _selection_label(top.quote, event)
    market_label = _market_label(top.quote.outcome.market)
    offered_odds = format_odds(top.quote.decimal_odds, odds_format)
    fair_odds = format_odds(top.fair_odds, odds_format)
    offered_probability = implied_probability(top.quote.decimal_odds)
    market_range = _market_range_label(top, quotes, odds_format)
    probability_tooltip = html.escape(
        "How this works — Win Probability is our estimate of how likely the bet is to win, "
        "based on prices across multiple sportsbooks. Break-even Probability is how often the "
        f"bet needs to win at {offered_odds} for you to break even over time. Here, the bet is "
        f"estimated to win {top.fair_probability:.1%} of the time, while {offered_odds} only "
        f"requires a {offered_probability:.1%} win rate. That gap is why the bet has positive EV.",
        quote=True,
    )
    sportsbook_url = _sportsbook_bet_url(top.quote)
    with st.container(border=True, key="top_opportunity"):
        identity_column, metrics_column, action_column = st.columns(
            [2.6, 5.8, 2.35], vertical_alignment="center"
        )
        with identity_column:
            st.markdown(
                '<div class="best-bet-badge">🏆 #1 BEST OPPORTUNITY</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="best-bet-pick">{html.escape(selection)}</div>',
                unsafe_allow_html=True,
            )
            if event is not None:
                home_name, away_name = _event_team_names(event)
                if top.quote.outcome.side is OutcomeSide.HOME:
                    opponent = f"vs {away_name}"
                elif top.quote.outcome.side is OutcomeSide.AWAY:
                    opponent = f"vs {home_name}"
                else:
                    opponent = event.name
                event_start_label = event.start_time.astimezone(DISPLAY_TIMEZONE).strftime(
                    "%a %b %d, %I:%M %p"
                )
                st.markdown(
                    f'<div class="best-bet-opponent">{html.escape(opponent)}</div>'
                    f'<div class="best-bet-event">{event.league_id.upper()} · '
                    f"{event_start_label} · "
                    f"{html.escape(market_label)}</div>",
                    unsafe_allow_html=True,
                )
        with metrics_column:
            st.markdown(
                '<div class="ev-featured-metrics">'
                '<div class="ev-featured-metric"><div class="ev-featured-label">EV '
                '<span class="ev-featured-info" title="Estimated long-term return if this same '
                'edge could be bet repeatedly.">ⓘ</span></div>'
                f'<div class="ev-featured-value positive">{_format_edge(top.expected_value)}</div>'
                '<div class="ev-featured-sub">Edge</div></div>'
                '<div class="ev-featured-metric"><div class="ev-featured-label">Best Odds '
                '<span class="ev-featured-info" title="Best price at an eligible sportsbook.">'
                "ⓘ</span></div>"
                f'<div class="ev-featured-value positive">{offered_odds}</div>'
                f'<div class="ev-featured-sub">{html.escape(top.quote.sportsbook.name)}</div></div>'
                '<div class="ev-featured-metric"><div class="ev-featured-label">Win Probability '
                f'<span class="ev-featured-info" title="{probability_tooltip}">ⓘ</span></div>'
                '<div class="ev-probability-pair"><span>'
                f"<strong>{top.fair_probability:.1%}</strong><small>Consensus estimate</small>"
                "</span><em>vs.</em><span>"
                f"<strong>{offered_probability:.1%}</strong>"
                f"<small>Break-even at {offered_odds}</small></span></div></div>"
                '<div class="ev-featured-metric"><div class="ev-featured-label">Fair Odds '
                '<span class="ev-featured-info" title="The estimated fair price based on prices '
                'across multiple sportsbooks.">ⓘ</span></div>'
                f'<div class="ev-featured-value">{fair_odds}</div>'
                f'<div class="ev-featured-sub">By {top.reference_books}-book consensus</div>'
                "</div></div>",
                unsafe_allow_html=True,
            )
        with action_column:
            st.caption("BEST AVAILABLE PRICE")
            st.markdown(
                f'<div class="best-price-title">{html.escape(top.quote.sportsbook.name)} '
                f"{offered_odds}</div>",
                unsafe_allow_html=True,
            )
            if sportsbook_url:
                st.link_button(
                    f"Bet {offered_odds} on {top.quote.sportsbook.name}",
                    sportsbook_url,
                    type="primary",
                    icon=":material/open_in_new:",
                    width="stretch",
                )
        st.markdown(
            '<div class="best-bet-support">'
            f"<span>◎ Consensus fair odds: <strong>{fair_odds}</strong></span>"
            f"<span>▥ Market range: <strong>{market_range}</strong></span>"
            f"<span>◷ <strong>{_recommendation_freshness(top.quote, as_of)}</strong></span>"
            "</div>",
            unsafe_allow_html=True,
        )
        with st.expander("View comparison", expanded=False):
            _render_value_comparison(top, quotes, odds_format, as_of)

    if len(recommended) > 1:
        recommended_columns = st.columns(len(recommended) - 1)
        for index, opportunity in enumerate(recommended[1:], start=2):
            with recommended_columns[index - 2]:
                _render_recommended_value_card_legacy(
                    opportunity,
                    index,
                    event_map,
                    quotes,
                    odds_format,
                    as_of,
                )

    recommended_keys = {(item.quote.sportsbook.id, item.quote.outcome.id) for item in recommended}
    ordered_values = recommended + tuple(
        item
        for item in ranked_values
        if (item.quote.sportsbook.id, item.quote.outcome.id) not in recommended_keys
    )
    ev_rank = {
        item.quote.outcome.id: rank
        for rank, item in enumerate(
            ordered_values,
            start=1,
        )
    }
    remaining = ordered_values[len(recommended) :]
    st.markdown(
        f'<div class="ev-list-title">More +EV Bets '
        f'<span class="ev-count-badge">{len(remaining)}</span></div>',
        unsafe_allow_html=True,
    )
    if not remaining:
        st.caption("No additional matching opportunities.")
        return

    page_size = 5
    page_count = max(1, (len(remaining) + page_size - 1) // page_size)
    page = min(max(0, int(st.session_state.get("ev_page", 0))), page_count - 1)
    st.session_state["ev_page"] = page
    page_start = page * page_size
    page_values = remaining[page_start : page_start + page_size]

    header = (
        '<div class="ev-table-head ev-grid"><span>#</span><span>MATCHUP</span>'
        '<span class="ev-market">MARKET</span><span>BEST ODDS</span>'
        '<span title="Win Probability is the market estimate. Break-even Probability is the '
        'win rate required at the offered odds.">WIN PROBABILITY ⓘ</span>'
        '<span class="ev-fair">FAIR ODDS</span><span>EV</span>'
        '<span class="ev-range">MARKET RANGE</span>'
        '<span class="ev-best-book">BEST BOOK</span><span>ACTION</span><span></span></div>'
    )
    rows: list[str] = []
    for item in page_values:
        item_event = event_map[item.quote.outcome.market.event_id]
        item_selection = _selection_label(item.quote, item_event)
        item_market = _market_label(item.quote.outcome.market)
        item_odds = format_odds(item.quote.decimal_odds, odds_format)
        item_fair_odds = format_odds(item.fair_odds, odds_format)
        item_implied = implied_probability(item.quote.decimal_odds)
        item_range = _market_range_label(item, quotes, odds_format)
        item_book = item.quote.sportsbook.name
        item_start_label = item_event.start_time.astimezone(DISPLAY_TIMEZONE).strftime(
            "%a %b %d, %I:%M %p"
        )
        item_url = _sportsbook_bet_url(item.quote)
        link_markup = (
            f'<a class="ev-action" href="{html.escape(item_url)}" target="_blank" '
            f'rel="noopener noreferrer">Bet now ↗</a>'
            if item_url
            else '<span class="ev-cell-sub">Open book</span>'
        )
        details_markup = _value_comparison_markup(item, quotes, odds_format, as_of)
        rows.append(
            '<details class="ev-table-row"><summary class="ev-grid">'
            f'<span class="ev-rank">#{ev_rank[item.quote.outcome.id]}</span>'
            f'<span class="ev-matchup"><strong>{html.escape(item_selection)}</strong>'
            f"<span>{html.escape(item_event.name)}<br>{item_event.league_id.upper()} · "
            f"{item_start_label}</span></span>"
            f'<span class="ev-market ev-cell-main">{html.escape(item_market)}</span>'
            f'<span class="ev-best-odds"><span class="ev-odds">{item_odds}</span>'
            f'<span class="ev-cell-sub">{html.escape(item_book)}</span></span>'
            '<span class="ev-probability-cell"><span>'
            f"{item.fair_probability:.1%}<small>Consensus</small></span>"
            '<span class="ev-probability-divider">vs.</span><span>'
            f"{item_implied:.1%}<small>Break-even</small></span></span>"
            f'<span class="ev-fair ev-cell-main">{item_fair_odds}'
            f'<span class="ev-cell-sub">By {item.reference_books}-book consensus</span></span>'
            f'<span class="ev-positive">{_format_edge(item.expected_value)}'
            f"<small>{_recommendation_freshness(item.quote, as_of)}</small></span>"
            f'<span class="ev-range ev-cell-main">{item_range}</span>'
            f'<span class="ev-best-book ev-cell-main"><strong>{html.escape(item_book)}</strong>'
            f'<span class="ev-cell-sub">{item_odds}</span></span>'
            f'<span class="ev-action-cell">{link_markup}</span>'
            '<span class="ev-chevron">⌄</span>'
            f'</summary><div class="ev-details">{details_markup}</div></details>'
        )
    st.markdown(
        f'<div class="ev-table-wrap">{header}{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )
    showing_from = page_start + 1
    showing_to = page_start + len(page_values)
    page_label, previous_column, next_column = st.columns(
        [8, 1, 1],
        vertical_alignment="center",
    )
    page_label.caption(f"Showing {showing_from}–{showing_to} of {len(remaining)} additional bets")
    previous_column.button(
        "Previous",
        icon=":material/chevron_left:",
        disabled=page == 0,
        on_click=_set_ev_page,
        args=(page - 1,),
        width="stretch",
    )
    next_column.button(
        "Next",
        icon=":material/chevron_right:",
        disabled=page >= page_count - 1,
        on_click=_set_ev_page,
        args=(page + 1,),
        width="stretch",
    )


def _board_row_markup(
    opportunity: ValueOpportunity,
    rank: int,
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
    *,
    recommended: bool,
    quotes_by_outcome: dict[str, tuple[Quote, ...]] | None = None,
) -> str:
    event = event_map[opportunity.quote.outcome.market.event_id]
    selection = _selection_label(opportunity.quote, event)
    home_name, away_name = _event_team_names(event)
    if opportunity.quote.outcome.side is OutcomeSide.HOME:
        team_name = home_name
        opponent = away_name
    elif opportunity.quote.outcome.side is OutcomeSide.AWAY:
        team_name = away_name
        opponent = home_name
    else:
        team_name = selection
        opponent = event.name
    offered_odds = format_odds(opportunity.quote.decimal_odds, odds_format)
    fair_odds = format_odds(opportunity.fair_odds, odds_format)
    offered_probability = implied_probability(opportunity.quote.decimal_odds)
    sportsbook = opportunity.quote.sportsbook.name
    sportsbook_event_url = _sportsbook_event_url(opportunity.quote)
    sportsbook_url = sportsbook_event_url or _sportsbook_bet_url(opportunity.quote)
    market_label = html.escape(_market_label(opportunity.quote.outcome.market))
    edge_label = _format_edge(opportunity.expected_value)
    start_label = event.start_time.astimezone(DISPLAY_TIMEZONE).strftime("%a %b %d, %I:%M %p")
    logo_markup = _team_logo_markup(team_name, event.league_id)
    if rank == 1:
        rank_markup = '<span class="board-rank gold">1</span>'
    elif recommended:
        rank_markup = f'<span class="board-rank pill">{rank}</span>'
    else:
        rank_markup = f'<span class="board-rank">{rank}</span>'
    action_label = "Bet now"
    action_aria = html.escape(
        f"Bet {selection} {_market_label(opportunity.quote.outcome.market)} "
        f"at {offered_odds} on {sportsbook}",
        quote=True,
    )
    action_markup = (
        f'<a class="board-action" href="{html.escape(sportsbook_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer" aria-label="{action_aria}" '
        f'onclick="event.stopPropagation();">{action_label}</a>'
        if sportsbook_url
        else '<span class="board-action">Open book</span>'
    )
    details_markup = _value_comparison_markup(
        opportunity,
        quotes,
        odds_format,
        as_of,
        selection=selection,
        matching_quotes=(
            quotes_by_outcome.get(opportunity.quote.outcome.id, ())
            if quotes_by_outcome is not None
            else None
        ),
    )
    row_class = "board-row recommended-row" if recommended else "board-row"
    return (
        f'<details class="{row_class}"><summary class="board-grid">'
        f"{rank_markup}"
        '<span class="board-matchup">'
        f"{logo_markup}"
        '<span class="board-matchup-copy">'
        f"<strong>{html.escape(selection)}</strong>"
        f"<span>vs {html.escape(opponent)}</span>"
        f"<small>{event.league_id.upper()} · {start_label}</small></span></span>"
        f'<span class="board-cell board-market">{market_label}</span>'
        f'<span class="board-cell board-ev"><strong>{edge_label}</strong></span>'
        '<span class="board-cell board-odds">'
        f"<strong>{offered_odds}</strong><small>{html.escape(sportsbook)}</small></span>"
        '<span class="board-cell board-fair">'
        f"<strong>{fair_odds}</strong></span>"
        '<span class="board-cell board-win"><span class="board-win-stack">'
        f"<strong>{opportunity.fair_probability:.1%}</strong>"
        "<small>Consensus win probability</small>"
        f"<em>{offered_probability:.1%} break-even</em></span></span>"
        f'<span class="board-action-cell">{action_markup}</span>'
        f'</summary><div class="ev-details">{details_markup}</div></details>'
    )


def _board_header_markup() -> str:
    return (
        '<div class="board-table-head board-grid">'
        '<span>#</span><span>MATCHUP</span><span class="board-market">MARKET</span>'
        '<span class="board-ev"><details class="board-info" name="board-tooltip">'
        "<summary>EV ⓘ</summary>"
        '<div class="board-tooltip">Estimated long-term return using the consensus win '
        "probability and the best available price.</div></details></span>"
        '<span class="board-odds"><details class="board-info" name="board-tooltip">'
        "<summary>BEST ODDS ⓘ</summary>"
        '<div class="board-tooltip">The highest payout currently offered by a sportsbook '
        "you selected as available to bet with.</div></details></span>"
        '<span class="board-fair"><details class="board-info" name="board-tooltip">'
        "<summary>FAIR ODDS ⓘ</summary>"
        '<div class="board-tooltip">Our estimated no-vig price. We remove each sportsbook’s '
        "margin and average every other book with both sides of this exact market. The global "
        "sportsbook list can be larger because not every book covers every event and market."
        "</div></details></span>"
        '<span class="board-win"><details class="board-info align-right" '
        'name="board-tooltip">'
        '<summary>WIN PROBABILITY ⓘ</summary><div class="board-tooltip">The no-vig consensus '
        "chance of winning. Break-even is the probability required at the offered price."
        "</div></details></span>"
        "<span>ACTION</span></div>"
    )


def _render_priority_value_bets(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
    *,
    recommendation_values: tuple[ValueOpportunity, ...] | None = None,
) -> None:
    quote_index = _quotes_by_outcome(quotes)
    recommended = (
        _recommended_value_opportunities(
            values,
            event_map,
            as_of=as_of,
            style="Balanced",
        )
        if recommendation_values is None
        else recommendation_values
    )
    if recommended:
        recommendation_rows = "".join(
            _board_row_markup(
                opportunity,
                rank,
                event_map,
                quotes,
                odds_format,
                as_of,
                recommended=True,
                quotes_by_outcome=quote_index,
            )
            for rank, opportunity in enumerate(recommended, start=1)
        )
        recommendation_content = (
            f'<div class="ev-table-wrap">{_board_header_markup()}{recommendation_rows}</div>'
        )
    else:
        recommendation_content = (
            '<div class="ev-empty"><strong>No bets currently meet all recommendation '
            "criteria.</strong></div>"
        )
    with st.container(key="recommended_board"):
        st.markdown(
            '<details class="recommended-section" open>'
            '<summary><span class="recommendation-heading">Recommended Bets</span>'
            f'<span class="recommended-count">{len(recommended)}</span></summary>'
            f'<div class="recommended-content">{recommendation_content}</div>'
            "</details>",
            unsafe_allow_html=True,
        )

    if not values:
        st.markdown(
            '<div class="ev-empty"><strong>No +EV bets match your filters.</strong>'
            "Try lowering your minimum EV, expanding your sportsbook selection, or "
            "clearing some filters.</div>",
            unsafe_allow_html=True,
        )
        return
    with st.container(key="more_ev_header"):
        title_column, sort_column = st.columns([4, 1.15], vertical_alignment="bottom")
        title_column.markdown(
            '<div class="all-bets-title">All +EV Bets '
            f'<span class="all-bets-count">{len(values)}</span></div>',
            unsafe_allow_html=True,
        )
        more_sort = sort_column.selectbox(
            "Sort All +EV Bets",
            [
                "EV % (High to Low)",
                "Win Probability",
                "Best Odds",
                "Starting Soon",
            ],
            key="more_ev_sort",
            label_visibility="collapsed",
        )
    all_ev_values = _sort_more_ev_values(
        values,
        event_map,
        more_sort,
    )
    visible_count = max(
        EV_INITIAL_BATCH_SIZE,
        int(st.session_state.get("ev_visible_count", EV_INITIAL_BATCH_SIZE)),
    )
    visible_count = min(visible_count, len(all_ev_values))
    visible_values = all_ev_values[:visible_count]
    page_rows = "".join(
        _board_row_markup(
            opportunity,
            rank,
            event_map,
            quotes,
            odds_format,
            as_of,
            recommended=False,
            quotes_by_outcome=quote_index,
        )
        for rank, opportunity in enumerate(visible_values, start=1)
    )
    st.markdown(
        f'<div class="ev-table-wrap">{_board_header_markup()}{page_rows}</div>',
        unsafe_allow_html=True,
    )
    if visible_count < len(all_ev_values):
        with st.container(key="load_more_ev"):
            remaining_count = len(all_ev_values) - visible_count
            st.button(
                f"Load {min(EV_BATCH_SIZE, remaining_count)} more",
                on_click=_show_more_ev_bets,
                args=(EV_BATCH_SIZE,),
                width="stretch",
            )


def _value_comparison_markup(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
    *,
    selection: str | None = None,
    matching_quotes: tuple[Quote, ...] | None = None,
) -> str:
    price_rows: list[str] = []
    comparison_quotes = (
        _quotes_for_opportunity(opportunity, quotes)
        if matching_quotes is None
        else matching_quotes
    )
    for quote in comparison_quotes:
        book = quote.sportsbook.name
        is_best = quote.sportsbook.id == opportunity.quote.sportsbook.id
        implied = implied_probability(quote.decimal_odds)
        book_edge = opportunity.fair_probability * quote.decimal_odds - Decimal("1")
        edge_class = "positive" if book_edge > 0 else "negative"
        event_url = _sportsbook_event_url(quote)
        url = event_url or _sportsbook_bet_url(quote)
        action = (
            f'<a class="ev-price-action" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">'
            f"{'Bet' if event_url else 'Open'} ↗</a>"
            if url
            else '<span class="ev-price-action">Unavailable</span>'
        )
        price_rows.append(
            f'<div class="ev-price-row ev-price-grid{" best" if is_best else ""}">'
            f'<span class="ev-price-book">{html.escape(book)}</span>'
            f'<span class="ev-price-odds">{format_odds(quote.decimal_odds, odds_format)}</span>'
            f'<span class="ev-price-prob">{implied:.1%}</span>'
            f'<span class="ev-price-edge {edge_class}">{_format_edge(book_edge)}</span>'
            f"<span>{_age_label(quote, as_of)} ago</span>"
            f"<span>{action}</span></div>"
        )
    contributors = html.escape(", ".join(opportunity.reference_sportsbooks))
    heading = html.escape(selection or opportunity.quote.outcome.side.value.title())
    market_name = html.escape(_market_label(opportunity.quote.outcome.market).casefold())
    updated = _age_label(opportunity.quote, as_of)
    return (
        '<div class="ev-price-heading">'
        f"<span>Compare prices · {heading} {market_name}</span>"
        f"<small>{len(price_rows)} sportsbooks · updated {updated} ago</small></div>"
        '<div class="ev-price-header ev-price-grid">'
        '<span>Sportsbook</span><span>Odds</span><span class="ev-price-prob">Implied prob.</span>'
        '<span class="ev-price-edge">Edge vs fair</span><span>Updated</span><span>Action</span>'
        "</div>"
        + "".join(price_rows)
        + '<details class="ev-consensus-details"><summary>Books used for fair odds</summary>'
        f"<span>{contributors}</span></details>"
    )


def _render_value_comparison(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
) -> None:
    st.markdown(
        _value_comparison_markup(opportunity, quotes, odds_format, as_of),
        unsafe_allow_html=True,
    )


def _render_overview(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    as_of: datetime,
    odds_format: str,
    *,
    recommendation_values: tuple[ValueOpportunity, ...] | None = None,
) -> None:
    _render_priority_value_bets(
        values,
        event_map,
        quotes,
        odds_format,
        as_of,
        recommendation_values=recommendation_values,
    )


def _render_games(
    quotes: tuple[Quote, ...],
    events: tuple[Event, ...],
    odds_format: str,
    sportsbook_names: list[str],
    repository: QuoteRepository,
    *,
    is_admin: bool,
) -> None:
    _render_event_board(
        quotes,
        events,
        sportsbook_names,
        odds_format,
        repository,
        is_admin=is_admin,
    )


def _render_line_movement(history: tuple[Quote, ...], events: tuple[Event, ...]) -> None:
    event_ids = {quote.outcome.market.event_id for quote in history}
    choices = {
        f"{event.league_id.upper()} | {event.name}": event
        for event in events
        if event.id in event_ids
    }
    if not choices:
        st.info("No historical snapshots are available.")
        return
    event = choices[st.selectbox("Tracked game", list(choices), key="movement_event")]
    event_history = [quote for quote in history if quote.outcome.market.event_id == event.id]
    market_name = st.selectbox("Market", ["Moneyline", "Spread", "Total"])
    kind = {
        "Moneyline": MarketKind.MONEYLINE,
        "Spread": MarketKind.SPREAD,
        "Total": MarketKind.TOTAL,
    }[market_name]
    selected = [quote for quote in event_history if quote.outcome.market.kind is kind]
    price_rows = [
        {
            "Time": quote.observed_at,
            "Series": f"{quote.sportsbook.name} - {_selection_label(quote)}",
            "Decimal price": float(quote.decimal_odds),
            "Line": (
                float(quote.outcome.market.line) if quote.outcome.market.line is not None else None
            ),
        }
        for quote in selected
    ]
    frame = pd.DataFrame(price_rows)
    if frame.empty:
        st.info("No history for this market.")
        return
    price_chart = frame.pivot_table(
        index="Time", columns="Series", values="Decimal price", aggfunc="last"
    )
    st.markdown("#### Price movement")
    st.line_chart(price_chart, width="stretch")
    if kind is not MarketKind.MONEYLINE:
        line_chart = frame.pivot_table(
            index="Time", columns="Series", values="Line", aggfunc="last"
        )
        st.markdown("#### Line movement")
        st.line_chart(line_chart, width="stretch")
    series_count = frame["Series"].nunique()
    st.caption(f"Showing {len(frame):,} stored observations across {series_count} series.")


def _format_strategy_dollars(units: Decimal) -> str:
    dollars = units * OFFICIAL_UNIT_VALUE_DOLLARS
    sign = "+" if dollars >= 0 else "-"
    return f"{sign}${abs(dollars):,.0f}"


def _render_official_performance(
    repository: QuoteRepository,
    odds_format: str,
) -> None:
    bets = _official_bets(repository.list_bets())
    performance = _official_performance(bets)
    with st.container(border=True, key="official_track_record"):
        heading_column, note_column = st.columns([2.2, 5], vertical_alignment="bottom")
        heading_column.markdown("### The $10,000 Strategy")
        note_column.caption("Hypothetical paper bankroll tracking every official recommendation.")
        record_column, profit_column, roi_column, bankroll_column, pending_column = st.columns(5)
        record_column.metric("Record", f"{performance.wins}-{performance.losses}")
        profit_column.metric(
            "Profit / Loss",
            _format_strategy_dollars(performance.units),
            f"{performance.units:+.2f} units",
        )
        roi_column.metric("ROI", f"{performance.roi:+.1%}")
        bankroll_column.metric(
            "Bankroll",
            f"${performance.bankroll * OFFICIAL_UNIT_VALUE_DOLLARS:,.0f}",
        )
        pending_column.metric("Open bets", performance.pending)
        st.caption(
            "$10,000 starting bankroll · quarter-Kelly sizing · every qualifying pick "
            "tracked · 1% per-bet cap · portfolio exposure controls"
        )
        if performance.voids:
            st.caption(f"{performance.voids} voided recommendation(s) excluded from ROI.")

        st.markdown("#### Bankroll over time")
        bankroll_history = _official_bankroll_history(
            bets,
            as_of=datetime.now(UTC),
        )
        bankroll_low = float(bankroll_history["Bankroll ($)"].min())
        bankroll_high = float(bankroll_history["Bankroll ($)"].max())
        bankroll_padding = max(50.0, (bankroll_high - bankroll_low) * 0.15)
        bankroll_chart = (
            alt.Chart(bankroll_history)
            .mark_line(
                color="#21c96b",
                interpolate="step-after",
                point=alt.OverlayMarkDef(color="#21c96b", size=55),
                strokeWidth=3,
            )
            .encode(
                x=alt.X("Date:T", title=None),
                y=alt.Y(
                    "Bankroll ($):Q",
                    title=None,
                    scale=alt.Scale(
                        domain=[
                            bankroll_low - bankroll_padding,
                            bankroll_high + bankroll_padding,
                        ],
                        zero=False,
                    ),
                    axis=alt.Axis(format="$,.0f"),
                ),
                tooltip=[
                    alt.Tooltip("Date:T", title="Date", format="%b %d, %Y · %I:%M %p"),
                    alt.Tooltip("Bankroll ($):Q", title="Bankroll", format="$,.0f"),
                ],
            )
            .properties(height=300)
        )
        st.altair_chart(
            bankroll_chart,
            width="stretch",
        )

        if not bets:
            st.caption(
                "The first official recommendations will appear after the next successful "
                "live-odds refresh."
            )
            return

        event_map = {event.id: event for event in repository.load_events()}
        chronological_bets = sorted(
            bets,
            key=lambda bet: (
                event_map[bet.event_id].start_time
                if bet.event_id in event_map
                else bet.created_at
            ),
        )
        st.markdown("#### Tracked recommendations")
        st.dataframe(
            [
                {
                    "Event time": (
                        event_map[bet.event_id]
                        .start_time.astimezone(DISPLAY_TIMEZONE)
                        .strftime("%b %d, %I:%M %p")
                        if bet.event_id in event_map
                        else "—"
                    ),
                    "Published": bet.created_at.astimezone(DISPLAY_TIMEZONE).strftime(
                        "%b %d, %I:%M %p"
                    ),
                    "Event": bet.event_name,
                    "Pick": f"{bet.selection} · {bet.market_label}",
                    "Book": bet.sportsbook,
                    "Odds": format_odds(bet.decimal_odds, odds_format),
                    "Wager": (
                        f"${bet.stake * OFFICIAL_UNIT_VALUE_DOLLARS:,.0f} ({bet.stake:.2f}u)"
                    ),
                    "Result": bet.status.value.title(),
                    "P/L": (
                        f"{_format_strategy_dollars(bet.profit_loss)} ({bet.profit_loss:+.2f}u)"
                        if bet.profit_loss is not None
                        else "—"
                    ),
                }
                for bet in chronological_bets
            ],
            hide_index=True,
            width="stretch",
            height=min(600, 38 * (len(chronological_bets) + 1)),
        )


def _render_official_settlement_controls(repository: QuoteRepository) -> None:
    pending = tuple(
        bet for bet in _official_bets(repository.list_bets()) if bet.status is BetStatus.PENDING
    )
    st.markdown("#### Official recommendation results")
    if not pending:
        st.caption("No official recommendations are waiting for a result.")
        return

    choices = {
        f"#{bet.id} · {bet.event_name} · {bet.selection} @ {bet.decimal_odds}": bet
        for bet in pending
    }
    with st.form("settle_official_recommendation", border=False):
        choice = st.selectbox("Pending recommendation", list(choices))
        result = st.selectbox("Result", ["Won", "Lost", "Void"])
        submitted = st.form_submit_button("Save result", type="primary")
    if submitted:
        bet = choices[choice]
        status = BetStatus(result.lower())
        if status is BetStatus.WON:
            profit_loss = bet.stake * (bet.decimal_odds - Decimal("1"))
        elif status is BetStatus.LOST:
            profit_loss = -bet.stake
        else:
            profit_loss = Decimal("0")
        if bet.id is not None:
            repository.update_bet(bet.id, status, profit_loss)
        st.rerun()


def _render_bets(
    quotes: tuple[Quote, ...],
    events: tuple[Event, ...],
    repository: QuoteRepository,
) -> None:
    event_map = {event.id: event for event in events}
    watched = repository.watched_event_ids()
    watch_rows = [
        {
            "League": event.league_id.upper(),
            "Event": event.name,
            "Starts": event.start_time.astimezone(DISPLAY_TIMEZONE).strftime("%a %I:%M %p"),
        }
        for event in events
        if event.id in watched
    ]
    st.subheader("Watchlist")
    if watch_rows:
        st.dataframe(watch_rows, hide_index=True, width="stretch")
    else:
        st.caption("Add games from the Games page to keep them here.")

    st.subheader("Manual bet tracker")
    quote_choices = {
        (
            f"{event_map[quote.outcome.market.event_id].name} | "
            f"{_market_label(quote.outcome.market)} | {_selection_label(quote)} | "
            f"{quote.sportsbook.name} @ {quote.decimal_odds}"
        ): quote
        for quote in quotes
        if quote.outcome.market.event_id in event_map
    }
    if quote_choices:
        with st.form("track_bet", clear_on_submit=True):
            selected_label = st.selectbox("Selection", list(quote_choices))
            stake = st.number_input("Stake", min_value=1.0, value=25.0, step=5.0)
            notes = st.text_input("Notes", placeholder="Optional execution notes")
            submitted = st.form_submit_button("Add to bet tracker", type="primary")
            if submitted:
                quote = quote_choices[selected_label]
                event = event_map[quote.outcome.market.event_id]
                repository.add_bet(
                    TrackedBet(
                        id=None,
                        created_at=datetime.now(UTC),
                        event_id=event.id,
                        event_name=event.name,
                        market_label=_market_label(quote.outcome.market),
                        selection=_selection_label(quote),
                        sportsbook=quote.sportsbook.name,
                        decimal_odds=quote.decimal_odds,
                        stake=Decimal(str(stake)),
                        notes=notes,
                    )
                )
                st.success("Bet recorded locally. Nothing was sent to a sportsbook.")

    bets = repository.list_bets()
    if not bets:
        st.info("No tracked bets yet.")
        return
    pending = {
        f"#{bet.id} | {bet.event_name} | {bet.selection}": bet
        for bet in bets
        if bet.status is BetStatus.PENDING
    }
    if pending:
        with st.expander("Settle a pending bet"):
            choice = st.selectbox("Pending bet", list(pending))
            result = st.selectbox("Result", ["Won", "Lost", "Void"])
            if st.button("Save result"):
                bet = pending[choice]
                status = BetStatus(result.lower())
                if status is BetStatus.WON:
                    profit_loss = bet.stake * (bet.decimal_odds - Decimal("1"))
                elif status is BetStatus.LOST:
                    profit_loss = -bet.stake
                else:
                    profit_loss = Decimal("0")
                if bet.id is not None:
                    repository.update_bet(bet.id, status, profit_loss)
                st.rerun()
    total_staked = sum((bet.stake for bet in bets), Decimal("0"))
    realized = sum((bet.profit_loss or Decimal("0") for bet in bets), Decimal("0"))
    one, two, three = st.columns(3)
    one.metric("Tracked bets", len(bets))
    two.metric("Total staked", f"${total_staked:,.2f}")
    three.metric("Realized P/L", f"${realized:,.2f}")
    st.dataframe(
        [
            {
                "Placed": bet.created_at.astimezone(DISPLAY_TIMEZONE).strftime("%b %d %I:%M %p"),
                "Event": bet.event_name,
                "Market": bet.market_label,
                "Selection": bet.selection,
                "Book": bet.sportsbook,
                "Odds": float(bet.decimal_odds),
                "Stake": float(bet.stake),
                "Status": bet.status.value.title(),
                "P/L": float(bet.profit_loss) if bet.profit_loss is not None else None,
            }
            for bet in bets
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Odds": st.column_config.NumberColumn(format="%.2f"),
            "Stake": st.column_config.NumberColumn(format="$%.2f"),
            "P/L": st.column_config.NumberColumn(format="$%.2f"),
        },
    )


def _render_settings(
    repository: QuoteRepository,
    mode: str,
    controls: dict[str, Any],
) -> None:
    st.subheader("Product settings")
    local_path = getattr(repository, "path", None)
    storage_label = str(local_path.resolve()) if isinstance(local_path, Path) else "Shared Postgres"
    st.write("Preferences are stored in the product database.")
    st.json(
        {
            "data_source": mode,
            "bankroll": st.session_state["bankroll"],
            "maximum_quote_age_minutes": st.session_state["freshness_minutes"],
            "minimum_arb_roi_percent": st.session_state["min_roi"],
            "minimum_consensus_edge_percent": st.session_state["min_ev"],
            "active_leagues": controls["active_leagues"],
            "active_markets": controls["active_markets"],
            "my_sportsbooks": controls["my_books"],
            "database": storage_label,
        },
        expanded=True,
    )
    st.markdown("#### Safety and interpretation")
    st.markdown(
        """
        - Pure arbitrage requires every price to remain available and every bet to be accepted.
        - Middles and consensus value signals are probabilistic, not guaranteed profit.
        - Demo data is fictional and exists to exercise the complete product workflow.
        - The terminal never logs into sportsbooks or places wagers.
        """
    )


def _render_launch_disclosures(mode: str) -> None:
    data_warning = (
        "Demo prices are fictional and must not be used to place wagers."
        if mode == "Demo"
        else "Live odds may be delayed, incomplete, unavailable, or changed without notice."
    )
    disclosure = st.container(key="launch_disclosures").expander(
        "Disclosures, Privacy, and Affiliate Policy · Bet Responsibly · 19+",
        expanded=False,
    )
    with disclosure:
        st.markdown(
            '<div class="legal-details">'
            "<p><strong>Informational tool only.</strong> This product is an odds-comparison "
            "and analysis tool, not a sportsbook, betting operator, financial adviser, or "
            "guarantee of any outcome. It does not accept or place wagers. You are responsible "
            "for confirming legal eligibility and complying with the rules in your location.</p>"
            f"<p><strong>Odds and calculations.</strong> {data_warning} Fair odds, win "
            "probability, expected value, arbitrage, and middle estimates depend on third-party "
            "data and assumptions and may be incorrect. Verify the market, price, limits, rules, "
            "and availability directly with the sportsbook before betting.</p>"
            "<p><strong>Third-party links.</strong> Sportsbook links open independent third-party "
            "services. Their eligibility requirements, geographic restrictions, privacy "
            "practices, and terms apply. Sportsbook names and trademarks belong to their "
            "respective owners; inclusion does not imply endorsement or partnership.</p>"
            "<p><strong>Affiliate disclosure.</strong> If a sportsbook link is identified as an "
            "affiliate link, this product may receive compensation when you use it. Compensation "
            "does not change the calculation or ranking of opportunities. Unmarked links are not "
            "represented as affiliate relationships.</p>"
            "<p><strong>Privacy.</strong> Sportsbook preferences are stored in your browser. "
            "Optional bet-tracker entries are stored in the product database. Never enter "
            "sportsbook passwords, payment details, "
            "or other sensitive account credentials. A hosted service should publish complete "
            "Terms of Use and a Privacy Policy before collecting user accounts, analytics, or "
            "other personal information.</p>"
            "<p><strong>Need help?</strong> Call Gambling Support BC at "
            '<a href="tel:+18887956111">1-888-795-6111</a> (free, confidential, 24/7) or '
            '<a href="https://www2.gov.bc.ca/gov/content/sports-culture/gambling-fundraising/'
            'gambling-support-bc" target="_blank" rel="noopener noreferrer">visit Gambling '
            "Support BC ↗</a>.</p></div>",
            unsafe_allow_html=True,
        )


@st.fragment  # type: ignore[untyped-decorator]
def _render_primary_dashboard(
    available_books: list[str],
    controls: dict[str, Any],
    events: tuple[Event, ...],
    quotes: tuple[Quote, ...],
    event_map: dict[str, Event],
    repository: QuoteRepository,
    *,
    is_admin: bool,
) -> None:
    """Keep view and filter changes inside a lightweight partial rerun."""
    tab_names = ["Best Bets", "Games", "Results"]
    saved_view = str(
        st.session_state.get(
            "primary_dashboard_view",
            st.session_state.get("dashboard_view", "Best Bets"),
        )
    )
    default_view = saved_view if saved_view in tab_names else "Best Bets"
    with st.container(key="dashboard_nav"):
        active_view = (
            st.segmented_control(
                "Dashboard section",
                tab_names,
                default=default_view,
                key="primary_dashboard_view",
                label_visibility="collapsed",
            )
            or "Best Bets"
        )

    as_of = datetime.now(UTC)
    if active_view == "Best Bets":
        ev_filters = _render_ev_filter_bar(
            available_books,
            str(controls["mode"]),
            events,
            quotes,
            as_of,
        )
        market_values = _value_opportunities_for_books(quotes, ev_filters.my_books)
        all_filtered_values = _filter_value_opportunities(
            market_values,
            event_map,
            ev_filters,
            as_of=as_of,
            max_age=controls["freshness"],
        )
        visible_recommendations = _recommended_value_opportunities(
            market_values,
            event_map,
            as_of=as_of,
        )
        _render_overview(
            all_filtered_values,
            event_map,
            quotes,
            as_of,
            controls["odds_format"],
            recommendation_values=visible_recommendations,
        )
    elif active_view == "Games":
        comparison_books = sorted(
            {quote.sportsbook.name for quote in quotes},
            key=_book_sort_key,
        )
        _render_games(
            quotes,
            events,
            controls["odds_format"],
            comparison_books,
            repository,
            is_admin=is_admin,
        )
    else:
        _render_official_performance(repository, controls["odds_format"])


def run() -> None:
    app_icon = Path(__file__).resolve().parents[2] / "assets" / "bettor-bureau-app-icon.png"
    st.set_page_config(
        page_title="Bettor Bureau",
        page_icon=str(app_icon),
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_theme()
    is_admin = _owner_access()
    database_url = _local_secret("DATABASE_URL")
    repository = _repository_for(
        database_url,
        os.getenv("ODDS_DB_PATH", "odds_scanner.db"),
    )
    _load_defaults(repository)
    if not database_url:
        _seed_demo(repository)
    selected_source = str(st.session_state.get("data_source", "Demo"))
    selected_provider_id = _provider_id(selected_source)
    if selected_provider_id != "demo":
        _watch_for_shared_odds_updates(repository, selected_provider_id)
    preload_quotes, preload_events = _load_view_snapshot(
        repository,
        selected_provider_id,
    )
    available_books = sorted(
        {quote.sportsbook.name for quote in preload_quotes} | set(STARTER_BOOKS),
        key=_book_sort_key,
    )
    controls = _sidebar(repository, is_admin=is_admin)
    header_odds_status = _render_page_header()
    owner_refresh = bool(st.session_state.pop("owner_refresh_requested", False))
    controls["refresh"] = bool(controls["refresh"] or owner_refresh)
    controls["odds_format"] = str(st.session_state.get("odds_format", DEFAULT_ODDS_FORMAT))

    refresh_notice = st.session_state.pop("refresh_notice", None)
    if refresh_notice:
        st.toast(str(refresh_notice))

    selected_league_keys = [
        LEAGUE_LABELS[label] for label in controls["active_leagues"] if label in LEAGUE_LABELS
    ]
    selected_market_keys = [
        MARKET_LABELS[label] for label in controls["active_markets"] if label in MARKET_LABELS
    ]

    if controls["refresh"]:
        if controls["mode"] != "Demo" and not controls["api_key"]:
            provider_name = "OddsPapi" if controls["mode"] == "OddsPapi Free" else "The Odds API"
            st.error(f"Enter an {provider_name} key before refreshing live odds.")
        else:
            provider: OddsProvider
            if controls["mode"] == "Demo":
                provider = DemoOddsProvider()
            elif controls["mode"] == "OddsPapi Free":
                stored = _cached_settings(repository)
                playnow_resolver = PlayNowEventResolver()
                stored_tournaments = {
                    str(key): int(value)
                    for key, value in _json_object(stored.get("oddspapi_tournament_ids")).items()
                }
                provider = OddsPapiProvider(
                    api_key=controls["api_key"],
                    include_schedule=_schedule_refresh_due(stored),
                    bookmaker_slugs=tuple(
                        ODDSPAPI_BOOK_SLUGS[book]
                        for book in STARTER_BOOKS
                        if book in ODDSPAPI_BOOK_SLUGS
                    ),
                    tournament_ids={
                        **stored_tournaments,
                        **ODDSPAPI_PRIMARY_TOURNAMENT_IDS,
                    },
                    market_catalog={
                        str(key): dict(value)
                        for key, value in _json_object(
                            stored.get("oddspapi_market_catalog")
                        ).items()
                        if isinstance(value, dict)
                    },
                    event_url_resolver=playnow_resolver.resolve,
                )
            else:
                provider = OddsApiProvider(
                    api_key=controls["api_key"],
                    regions=controls["regions"],
                )

            try:
                refresh_request = RefreshRequest(
                    league_keys=tuple(selected_league_keys),
                    league_ids=tuple(
                        LEAGUE_IDS[label]
                        for label in controls["active_leagues"]
                        if label in LEAGUE_IDS
                    ),
                    market_keys=tuple(selected_market_keys),
                    market_kinds=tuple(
                        MARKET_KINDS[key] for key in selected_market_keys if key in MARKET_KINDS
                    ),
                )
                with st.spinner("Refreshing odds…"):
                    diagnostics = OddsRefreshService(
                        provider=provider,
                        repository=repository,
                        config=_refresh_config(str(controls["mode"])),
                    ).refresh(refresh_request)
                st.session_state["last_refresh_diagnostics"] = diagnostics
                if isinstance(provider, OddsPapiProvider):
                    repository.save_setting(
                        "oddspapi_tournament_ids",
                        json.dumps(provider.tournament_ids, separators=(",", ":")),
                    )
                    repository.save_setting(
                        "oddspapi_market_catalog",
                        json.dumps(provider.market_catalog, separators=(",", ":")),
                    )
                    if (
                        provider.include_schedule
                        and diagnostics.status is RefreshResultStatus.SUCCESS
                    ):
                        repository.save_setting(
                            "oddspapi_schedule_refreshed_at",
                            diagnostics.finished_at.isoformat(),
                        )
                    _record_oddspapi_requests(repository, provider.request_count)
                st.session_state["refresh_notice"] = _diagnostic_message(diagnostics)
                _invalidate_view_snapshot(provider.provider_id)
                st.rerun()
            except (KeyError, ValueError) as exc:
                st.error(str(exc))

    as_of = datetime.now(UTC)
    provider_id = _provider_id(controls["mode"])
    if provider_id == selected_provider_id:
        latest_quotes = preload_quotes
        stored_events = preload_events
    else:
        latest_quotes, stored_events = _load_view_snapshot(repository, provider_id)
    all_events = tuple(event for event in stored_events if event.start_time > as_of)
    all_event_map = {event.id: event for event in all_events}
    quotes = tuple(
        quote for quote in latest_quotes if quote.outcome.market.event_id in all_event_map
    )
    fresh_quotes = tuple(
        quote for quote in quotes if is_fresh(quote, as_of=as_of, max_age=controls["freshness"])
    )
    events = all_events
    event_map, _ = _event_maps(events)
    _render_odds_status(quotes, fresh_quotes, as_of, container=header_odds_status)

    _render_primary_dashboard(
        available_books,
        controls,
        events,
        quotes,
        event_map,
        repository,
        is_admin=is_admin,
    )

    _render_launch_disclosures(str(controls["mode"]))
    _render_owner_panel(
        repository,
        str(controls["mode"]),
        is_admin=is_admin,
    )
