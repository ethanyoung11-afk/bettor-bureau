from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd
import streamlit as st

from odds_scanner.analytics import (
    MiddleOpportunity,
    ValueOpportunity,
    detect_consensus_value,
    detect_middles,
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
    detect_arbitrage,
    implied_probability,
    is_fresh,
)
from odds_scanner.presentation import decimal_to_american, format_odds
from odds_scanner.providers.base import OddsProvider
from odds_scanner.providers.demo import DemoOddsProvider, generate_demo_snapshots
from odds_scanner.providers.odds_api import FOOTBALL_LEAGUES, OddsApiProvider
from odds_scanner.providers.oddspapi import OddsPapiProvider
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
ODDSPAPI_FREE_CREDITS = 250
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
CORE_REFRESH_LEAGUES = ("NFL", "NCAAF", "NBA", "NHL")
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


def _provider_id(mode: str) -> str:
    return DATA_SOURCE_IDS.get(mode, "demo")


def _book_sort_key(book: str) -> tuple[int, str]:
    try:
        return PRIORITY_BOOKS.index(book), book.lower()
    except ValueError:
        return len(PRIORITY_BOOKS), book.lower()


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

    expected_hash = _local_secret("ADMIN_PASSWORD_HASH")
    authenticated = bool(st.session_state.get("owner_authenticated", False))
    with st.sidebar:
        if authenticated:
            st.success("Owner mode", icon="🔓")
            if st.button("Return to viewer mode", width="stretch"):
                st.session_state["owner_authenticated"] = False
                st.session_state.pop("owner_password", None)
                st.rerun()
            return True

        st.caption("Read-only shared board")
        with st.expander("Owner access", expanded=False):
            password = st.text_input("Owner password", type="password", key="owner_password")
            if st.button("Unlock owner controls", width="stretch"):
                if _password_matches(password, expected_hash):
                    st.session_state["owner_authenticated"] = True
                    st.rerun()
                else:
                    st.error("That owner password is not correct.")
        return False


@lru_cache(maxsize=4)
def _repository_for(database_url: str, sqlite_path: str) -> QuoteRepository:
    if database_url:
        from odds_scanner.storage.postgres import PostgresQuoteRepository

        return PostgresQuoteRepository(database_url)
    return SQLiteQuoteRepository(Path(sqlite_path))


def _oddspapi_requests_used(
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


def _record_oddspapi_requests(
    repository: QuoteRepository,
    request_count: int,
    *,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    effective_time = as_of or datetime.now(UTC)
    used = _oddspapi_requests_used(repository, as_of=effective_time) + max(0, request_count)
    repository.save_setting("oddspapi_usage_month", effective_time.strftime("%Y-%m"))
    repository.save_setting("oddspapi_requests_used", str(used))
    return used, max(0, ODDSPAPI_FREE_CREDITS - used)


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
    counts = repository.opportunity_counts(provider_id)
    usage = repository.api_usage_summary(provider_id, as_of=datetime.now(UTC))
    last_refresh = usage.last_successful_refresh
    last_refresh_label = (
        last_refresh.astimezone().strftime("%I:%M %p").lstrip("0")
        if last_refresh
        else "Not yet"
    )
    st.markdown(
        f"**Current recommendations**  \n{counts.active} active · {counts.stale} stale"
    )
    st.markdown(f"**Last successful refresh**  \n{last_refresh_label}")
    st.markdown(
        f"**API usage tracked by this app**  \n"
        f"{usage.requests_today} requests today · {usage.requests_this_month} this month"
    )
    if usage.last_failed_refresh is not None:
        failed_label = usage.last_failed_refresh.astimezone().strftime("%b %d, %I:%M %p")
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
        .st-key-ev_filter_bar [data-testid="stPopover"] button {
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
            grid-template-columns:.78fr .82fr 1.45fr .82fr .9fr;
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
            minmax(90px,.75fr) minmax(110px,.9fr) minmax(95px,.75fr) minmax(90px,.7fr) 24px;
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
        .ev-details { padding:.75rem 1rem .85rem; background:#08111b; }
        .ev-details-metrics { display:flex; flex-wrap:wrap; gap:1rem; margin-bottom:.65rem; }
        .ev-detail-metric { color:#8f9caf; font-size:.72rem; }
        .ev-detail-metric strong { color:#e4eaf2; display:block; font-size:.84rem; margin-top:2px; }
        .ev-book-prices { display:flex; flex-wrap:wrap; gap:7px; }
        .ev-book-price {
            min-width:128px; border:1px solid #233247; border-radius:7px; padding:7px 9px;
            background:#0d1826; color:#a7b4c5; font-size:.68rem;
        }
        .ev-book-price strong { display:block; color:#eef2f7; font-size:.79rem; margin-bottom:2px; }
        .ev-book-price.best { border-color:#1f9f59; background:#0d2119; }
        .ev-book-price a { color:#39df83; text-decoration:none; }
        .ev-empty {
            text-align:center; padding:2.25rem 1rem; border:1px dashed #2a394d; border-radius:10px;
            background:#0a121e; color:#8f9caf;
        }
        .ev-empty strong { display:block; color:#eef2f7; font-size:1.05rem; margin-bottom:.35rem; }
        .legal-strip {
            display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px; padding:.7rem .85rem;
            border:1px solid #263448; border-radius:10px; background:#0a131f;
            color:#aeb9c8; font-size:.76rem; line-height:1.45;
        }
        .legal-strip strong { color:#eef2f7; }
        .legal-strip a,.legal-details a { color:#55e79a !important; text-decoration:none; }
        .legal-age {
            display:inline-flex; align-items:center; justify-content:center;
            width:30px; height:30px;
            flex:0 0 30px; border:1px solid #4b5c70; border-radius:50%; color:#f2f6fa;
            font-size:.68rem; font-weight:850;
        }
        .legal-details { color:#9eabba; font-size:.76rem; line-height:1.55; }
        .legal-details strong { color:#e7edf4; }
        .ev-list-title {
            color:#f2f6fa; font-size:1.05rem; font-weight:850; margin:.9rem .15rem .4rem;
        }
        .ev-count-badge {
            display:inline-flex; margin-left:7px; padding:2px 8px; border-radius:999px;
            background:#0d3425; color:#39df83; font-size:.75rem; vertical-align:middle;
        }
        @media (max-width: 1450px) {
            .ev-grid {
                grid-template-columns:38px minmax(220px,2fr) 90px 80px 125px
                75px 85px 80px 24px;
            }
            .ev-fair,.ev-consensus,.ev-range { display:none; }
        }
        @media (max-width: 760px) {
            .block-container { padding:.6rem .75rem 1.2rem; }
            h1 { font-size:1.75rem; }
            .ev-page-subtitle { font-size:.82rem; }
            .ev-update-status { justify-content:flex-start; text-align:left; margin:.2rem 0; }
            .ev-table-head { display:none; }
            .ev-table-row summary {
                grid-template-columns:28px minmax(0,1fr) 74px 22px;
                grid-template-rows:auto auto auto; column-gap:8px; row-gap:7px;
                padding:.7rem .6rem;
            }
            .ev-market,.ev-fair,.ev-consensus,.ev-range,.ev-best-book { display:none; }
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
            .legal-strip { align-items:flex-start; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_page_header() -> Any:
    title_column, status_column = st.columns([4.8, 2], vertical_alignment="center")
    with title_column:
        st.title("+EV Bets")
        st.markdown(
            '<div class="ev-page-subtitle">Positive expected value bets identified by comparing '
            "the best available odds with the broader sportsbook market.</div>",
            unsafe_allow_html=True,
        )
    with status_column:
        status_container = st.container(key="header_odds_status")
    return status_container


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
        if quote.outcome.side is OutcomeSide.HOME:
            side = event.home.name
        elif quote.outcome.side is OutcomeSide.AWAY:
            side = event.away.name
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
    age = _elapsed_label(last_refresh, as_of)
    freshness = freshness_state(
        last_refresh,
        as_of=as_of,
        config=_refresh_config(str(st.session_state.get("data_source", "Demo"))).freshness,
    )
    state = f"{freshness.value} odds"
    state_class = freshness.name.casefold().replace("_", "-")
    refreshed_at = last_refresh.astimezone().strftime("%I:%M %p").lstrip("0")
    target.markdown(
        '<div class="ev-update-status">'
        f'<span class="ev-freshness-state {state_class}">{state}</span>'
        f'<span>Last refreshed at <strong>{refreshed_at}</strong> · {age} ↻</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def _load_defaults(repository: QuoteRepository) -> None:
    stored = repository.load_settings()
    st.session_state.setdefault("bankroll", stored.get("bankroll", "1000"))
    st.session_state.setdefault(
        "freshness_minutes", int(stored.get("freshness_minutes", "5"))
    )
    st.session_state.setdefault("min_roi", float(stored.get("min_roi", "0.25")))
    st.session_state.setdefault("min_ev", float(stored.get("min_ev", "2.0")))
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
        st.markdown("### ADVANTAGE TERMINAL")
        st.caption("BC sportsbook value scanner")
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
            requests_used = _oddspapi_requests_used(repository)
            requests_remaining = max(0, ODDSPAPI_FREE_CREDITS - requests_used)
            api_key = saved_oddspapi_key
            if is_admin:
                st.caption(f"Connected · {requests_remaining} estimated credits remaining")
                with st.expander("Feed account & usage", expanded=False):
                    api_key = st.text_input(
                        "OddsPapi API key",
                        value=saved_oddspapi_key,
                        type="password",
                    )
                    st.caption("Each available sportsbook uses one request per manual refresh.")
                    st.progress(
                        min(1.0, requests_used / ODDSPAPI_FREE_CREDITS),
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
            odds_format = st.radio("Odds", ["American", "Decimal"], horizontal=True)
        supported_leagues = list(LEAGUE_ICONS)
        supported_markets = ["Moneyline", "Spread", "Total"]
        if data_mode in {"Demo", "OddsPapi Free"}:
            supported_markets.append("Player props")
        if is_admin:
            with st.expander("Odds Data", expanded=False):
                st.caption("Enabled sports")
                active_leagues = [
                    league
                    for league in supported_leagues
                    if st.checkbox(
                        f"{LEAGUE_ICONS[league]} {league}",
                        value=league in CORE_REFRESH_LEAGUES,
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
                _render_refresh_admin_status(repository, data_mode)
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
    display_quotes = tuple(
        quote for quote in quotes if quote.sportsbook.name in selected_books
    )
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
            "background-color: #123525; color: #6ee7b7; font-weight: 750; "
            "border: 1px solid #1f6b48"
        )
    return ""


def _render_event_odds_matrix(
    event_quotes: tuple[Quote, ...],
    sportsbook_names: list[str],
    odds_format: str,
    event: Event | None = None,
) -> None:
    frame = _event_odds_frame(event_quotes, sportsbook_names, odds_format, event)
    if frame.empty:
        st.info("No current bets are available for this event.")
        return
    sportsbook_columns = [name for name in sportsbook_names if name in frame.columns]
    styled = frame.style.map(_highlight_best_price, subset=sportsbook_columns)
    st.dataframe(
        styled,
        hide_index=True,
        width="stretch",
        height=min(430, 40 + 35 * len(frame)),
    )
    st.caption("★ Best currently available price for that bet · — Not offered by that sportsbook")


def _render_event_board(
    quotes: tuple[Quote, ...],
    events: tuple[Event, ...],
    sportsbook_names: list[str],
    odds_format: str,
    repository: QuoteRepository | None = None,
    *,
    is_admin: bool = False,
) -> None:
    st.markdown('<div class="section-kicker">Upcoming events</div>', unsafe_allow_html=True)
    st.subheader(f"Event odds comparison · {len(events)} games")
    st.caption("Click an event to expand every available bet and compare sportsbooks side by side.")
    quoted_event_ids = {quote.outcome.market.event_id for quote in quotes}
    available_events = sorted(
        (event for event in events if event.id in quoted_event_ids),
        key=lambda event: (event.start_time, event.name),
    )
    if not available_events:
        st.info("No saved upcoming events match the selected leagues. Refresh the latest odds.")
        return
    watched_event_ids = (
        repository.watched_event_ids() if repository is not None and is_admin else set()
    )
    page_size = 10
    page_count = max(1, (len(available_events) + page_size - 1) // page_size)
    page = min(max(0, int(st.session_state.get("event_page", 0))), page_count - 1)
    st.session_state["event_page"] = page
    page_start = page * page_size
    page_events = available_events[page_start : page_start + page_size]
    for event in page_events:
        league = event.league_id.upper()
        icon = LEAGUE_ICONS.get(league, "🏟️")
        local_start = event.start_time.astimezone().strftime("%a %b %d · %I:%M %p")
        event_quotes = tuple(
            quote for quote in quotes if quote.outcome.market.event_id == event.id
        )
        with st.expander(f"{icon} {league} · {event.name} · {local_start}", expanded=False):
            if repository is not None and is_admin:
                watched = event.id in watched_event_ids
                if st.button(
                    "Remove from watchlist" if watched else "Add to watchlist",
                    key=f"watch_event_{event.id}",
                ):
                    repository.set_event_watched(event.id, not watched, datetime.now(UTC))
                    st.rerun()
            _render_event_odds_matrix(event_quotes, sportsbook_names, odds_format, event)

    showing_from = page_start + 1
    showing_to = page_start + len(page_events)
    page_label, previous_column, next_column = st.columns(
        [8, 1, 1],
        vertical_alignment="center",
    )
    page_label.caption(
        f"Showing games {showing_from}–{showing_to} of {len(available_events)}"
    )
    previous_column.button(
        "Previous games",
        icon=":material/chevron_left:",
        disabled=page == 0,
        on_click=_set_event_page,
        args=(page - 1,),
        width="stretch",
    )
    next_column.button(
        "Next games",
        icon=":material/chevron_right:",
        disabled=page >= page_count - 1,
        on_click=_set_event_page,
        args=(page + 1,),
        width="stretch",
    )


def _set_event_page(page: int) -> None:
    st.session_state["event_page"] = max(0, page)


def _sportsbook_toggle_key(mode: str, book: str) -> str:
    return f"my_sportsbook_{stable_id('ui-book-v3', mode, book)}"


def _set_sportsbook_selection(books: tuple[str, ...], mode: str, enabled: bool) -> None:
    for book in books:
        st.session_state[_sportsbook_toggle_key(mode, book)] = enabled
    st.session_state["ev_page"] = 0


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
    st.session_state["ev_custom_minimum"] = 2.0
    st.session_state["ev_sort_by"] = "EV % (High to Low)"


def _set_ev_page(page: int) -> None:
    st.session_state["ev_page"] = max(0, page)


def _render_ev_filter_bar(
    available_books: list[str],
    mode: str,
    events: tuple[Event, ...],
    quotes: tuple[Quote, ...],
    as_of: datetime,
) -> EVFilterState:
    st.session_state.setdefault("ev_custom_minimum", float(st.session_state["min_ev"]))
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
    preset_options = ["Any positive EV", "1%+", "2%+", "3%+", "5%+", "Custom"]
    st.session_state.setdefault(
        "ev_minimum_preset",
        f"{int(float(st.session_state['min_ev']))}%+"
        if float(st.session_state["min_ev"]) in {1.0, 2.0, 3.0, 5.0}
        else "Custom",
    )

    with st.container(key="ev_filter_bar"):
        sport_col, market_col, ev_col, book_col, more_col, _, sort_col = st.columns(
            [1.08, 1.05, 1.0, 1.25, 1.05, 1.65, 1.35], vertical_alignment="bottom"
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
                else "Custom EV"
                if option == "Custom"
                else f"EV ≥ {option.removesuffix('+')}"
            ),
            on_change=_set_ev_page,
            args=(0,),
        )

        selected_before = tuple(
            book
            for book in available_books
            if bool(st.session_state.get(_sportsbook_toggle_key(mode, book), True))
        )
        book_count = str(len(selected_before)) if selected_before else "All"
        with book_col.popover(f"My Sportsbooks ({book_count})", width="stretch"):
            st.caption(
                "Only these books can be recommended. All available books still inform the "
                "broader market comparison."
            )
            st.caption("Choose as many as you want, then apply once.")
            with st.form(
                f"sportsbook_filter_{stable_id('sportsbook-form-v1', mode)}",
                border=False,
            ):
                for book in available_books:
                    st.toggle(
                        book,
                        value=True,
                        key=_sportsbook_toggle_key(mode, book),
                    )
                st.form_submit_button(
                    "Apply sportsbooks",
                    type="primary",
                    width="stretch",
                    on_click=_set_ev_page,
                    args=(0,),
                )
                action_columns = st.columns(2)
                action_columns[0].form_submit_button(
                    "Select all",
                    on_click=_set_sportsbook_selection,
                    args=(tuple(available_books), mode, True),
                    width="stretch",
                )
                action_columns[1].form_submit_button(
                    "Use all books",
                    on_click=_set_sportsbook_selection,
                    args=(tuple(available_books), mode, False),
                    width="stretch",
                    help=(
                        "Clears the restriction so every eligible sportsbook may be "
                        "recommended."
                    ),
                )
                st.caption("No selections means all eligible sportsbooks.")

        with more_col.popover("More Filters", width="stretch"):
            st.caption("Adjust several filters, then apply once.")
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

    if minimum_preset == "Custom":
        custom_minimum = st.number_input(
            "Custom minimum EV %",
            min_value=0.0,
            max_value=100.0,
            step=0.25,
            key="ev_custom_minimum",
        )
        minimum_ev = Decimal(str(custom_minimum)) / Decimal("100")
    else:
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
    maximum_american = (
        int(st.session_state.get("ev_max_american", 300)) if use_odds_range else None
    )
    minimum_consensus = int(consensus_preset.rstrip("+")) if consensus_preset != "Any" else 2
    starts_before = {
        "Any time": None,
        "Next 6 hours": as_of + timedelta(hours=6),
        "Next 12 hours": as_of + timedelta(hours=12),
        "Next 24 hours": as_of + timedelta(hours=24),
        "Next 3 days": as_of + timedelta(days=3),
    }[start_window]

    chips: list[str] = []
    if implied_percent > 0:
        chips.append(f"Break-even Prob. ≥ {implied_percent}%")
    if use_odds_range:
        chips.append(f"Odds: {minimum_american:+d} to {maximum_american:+d}")
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


def _best_value_by_outcome(
    values: tuple[ValueOpportunity, ...],
) -> tuple[ValueOpportunity, ...]:
    selected: dict[str, ValueOpportunity] = {}
    for item in values:
        outcome_id = item.quote.outcome.id
        current = selected.get(outcome_id)
        if current is None or (
            item.quote.decimal_odds,
            item.expected_value,
            item.quote.source_updated_at,
        ) > (
            current.quote.decimal_odds,
            current.expected_value,
            current.quote.source_updated_at,
        ):
            selected[outcome_id] = item
    return tuple(selected.values())


def _filter_value_opportunities(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    filters: EVFilterState,
    *,
    as_of: datetime,
    max_age: timedelta,
) -> tuple[ValueOpportunity, ...]:
    filtered: list[ValueOpportunity] = []
    for item in _best_value_by_outcome(values):
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


def _quotes_for_opportunity(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
) -> tuple[Quote, ...]:
    matching = tuple(
        quote for quote in quotes if quote.outcome.id == opportunity.quote.outcome.id
    )
    return tuple(
        sorted(
            deduplicate_quotes(matching),
            key=lambda quote: (quote.decimal_odds, quote.sportsbook.name),
            reverse=True,
        )
    )


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


def _render_recommended_value_card(
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
    sportsbook_url = SPORTSBOOK_URLS.get(sportsbook)
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
                f" · {event.start_time.astimezone().strftime('%a %b %d, %I:%M %p')}"
                if event
                else ""
            )
            + "</div>"
            '<div class="recommended-card-grid">'
            '<div class="recommended-card-metric"><small>Bet at</small>'
            f'<strong>{html.escape(sportsbook)}</strong></div>'
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


def _render_priority_value_bets(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
) -> None:
    if not values:
        st.markdown(
            '<div class="ev-empty"><strong>No +EV bets match these filters.</strong>'
            "Try lowering your minimum EV or break-even probability, expanding your "
            "sportsbook selection, or clearing some filters.</div>",
            unsafe_allow_html=True,
        )
        st.button("Clear filters", on_click=_reset_ev_filters, type="primary")
        return

    ranked_values = tuple(
        sorted(values, key=lambda item: item.expected_value, reverse=True)
    )
    recommended = ranked_values[:3]
    st.markdown(
        f'<div class="ev-list-title">Recommended Bets '
        f'<span class="ev-count-badge">{len(recommended)}</span></div>',
        unsafe_allow_html=True,
    )
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
    sportsbook_url = SPORTSBOOK_URLS.get(top.quote.sportsbook.name)
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
                if top.quote.outcome.side is OutcomeSide.HOME:
                    opponent = f"vs {event.away.name}"
                elif top.quote.outcome.side is OutcomeSide.AWAY:
                    opponent = f"vs {event.home.name}"
                else:
                    opponent = event.name
                st.markdown(
                    f'<div class="best-bet-opponent">{html.escape(opponent)}</div>'
                    f'<div class="best-bet-event">{event.league_id.upper()} · '
                    f"{event.start_time.astimezone().strftime('%a %b %d, %I:%M %p')} · "
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
                '</span><em>vs.</em><span>'
                f"<strong>{offered_probability:.1%}</strong>"
                f"<small>Break-even at {offered_odds}</small></span></div></div>"
                '<div class="ev-featured-metric"><div class="ev-featured-label">Fair Odds '
                '<span class="ev-featured-info" title="The estimated fair price based on prices '
                'across multiple sportsbooks.">ⓘ</span></div>'
                f'<div class="ev-featured-value">{fair_odds}</div>'
                '<div class="ev-featured-sub">Consensus</div></div>'
                '<div class="ev-featured-metric"><div class="ev-featured-label">Consensus '
                '<span class="ev-featured-info" title="Sportsbooks contributing to this '
                'opportunity’s market estimate.">ⓘ</span></div>'
                f'<div class="ev-featured-value">{top.reference_books} books</div>'
                f'<div class="ev-featured-sub">Range: {market_range}</div></div></div>',
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
                _render_recommended_value_card(
                    opportunity,
                    index,
                    event_map,
                    quotes,
                    odds_format,
                    as_of,
                )

    ev_rank = {
        item.quote.outcome.id: rank
        for rank, item in enumerate(
            ranked_values,
            start=1,
        )
    }
    remaining = ranked_values[len(recommended) :]
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
        '<span class="ev-consensus">CONSENSUS</span><span class="ev-range">MARKET RANGE</span>'
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
        item_url = SPORTSBOOK_URLS.get(item_book)
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
            f'<span>{html.escape(item_event.name)}<br>{item_event.league_id.upper()} · '
            f"{item_event.start_time.astimezone().strftime('%a %b %d, %I:%M %p')}</span></span>"
            f'<span class="ev-market ev-cell-main">{html.escape(item_market)}</span>'
            f'<span class="ev-best-odds"><span class="ev-odds">{item_odds}</span>'
            f'<span class="ev-cell-sub">{html.escape(item_book)}</span></span>'
            '<span class="ev-probability-cell"><span>'
            f"{item.fair_probability:.1%}<small>Consensus</small></span>"
            '<span class="ev-probability-divider">vs.</span><span>'
            f"{item_implied:.1%}<small>Break-even</small></span></span>"
            f'<span class="ev-fair ev-cell-main">{item_fair_odds}'
            '<span class="ev-cell-sub">Consensus</span></span>'
            f'<span class="ev-positive">{_format_edge(item.expected_value)}'
            f'<small>{_recommendation_freshness(item.quote, as_of)}</small></span>'
            f'<span class="ev-consensus ev-cell-main">{item.reference_books} books</span>'
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
    page_label.caption(
        f"Showing {showing_from}–{showing_to} of {len(remaining)} additional bets"
    )
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


def _value_comparison_markup(
    opportunity: ValueOpportunity,
    quotes: tuple[Quote, ...],
    odds_format: str,
    as_of: datetime,
) -> str:
    offered_probability = implied_probability(opportunity.quote.decimal_odds)
    market_range = _market_range_label(opportunity, quotes, odds_format)
    fair_odds = format_odds(opportunity.fair_odds, odds_format)
    edge = _format_edge(opportunity.expected_value)
    updated = _age_label(opportunity.quote, as_of)
    metric_markup = (
        '<div class="ev-details-metrics">'
        f'<span class="ev-detail-metric">Fair odds<strong>{fair_odds}</strong></span>'
        '<span class="ev-detail-metric">Consensus win probability'
        f"<strong>{opportunity.fair_probability:.1%}</strong></span>"
        '<span class="ev-detail-metric">Break-even probability'
        f"<strong>{offered_probability:.1%}</strong></span>"
        f'<span class="ev-detail-metric">EV<strong>{edge}</strong></span>'
        f'<span class="ev-detail-metric">Market range<strong>{market_range}</strong></span>'
        f'<span class="ev-detail-metric">Updated<strong>{updated} ago</strong></span>'
        "</div>"
    )
    price_cards: list[str] = []
    for quote in _quotes_for_opportunity(opportunity, quotes):
        book = quote.sportsbook.name
        is_best = quote.sportsbook.id == opportunity.quote.sportsbook.id
        if is_best:
            role = "Best offer · not used in its own comparison"
        elif book in opportunity.reference_sportsbooks:
            role = "Consensus contributor"
        else:
            role = "Available price · not used"
        url = SPORTSBOOK_URLS.get(book)
        action = (
            f' · <a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">Bet ↗</a>'
            if url
            else ""
        )
        price_cards.append(
            f'<span class="ev-book-price{" best" if is_best else ""}">'
            f'<strong>{html.escape(book)} · {format_odds(quote.decimal_odds, odds_format)}</strong>'
            f'{html.escape(role)} · {_age_label(quote, as_of)} ago{action}</span>'
        )
    contributors = html.escape(", ".join(opportunity.reference_sportsbooks))
    contributor_markup = (
        '<div class="ev-detail-metric">Sportsbooks used for the consensus estimate'
        f"<strong>{contributors}</strong></div>"
    )
    return (
        metric_markup
        + contributor_markup
        + f'<div class="ev-book-prices">{"".join(price_cards)}</div>'
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
) -> None:
    _render_priority_value_bets(values, event_map, quotes, odds_format, as_of)


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
            "Starts": event.start_time.astimezone().strftime("%a %I:%M %p"),
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
                "Placed": bet.created_at.astimezone().strftime("%b %d %I:%M %p"),
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
    st.markdown(
        '<div class="legal-strip"><span class="legal-age">19+</span><span>'
        '<strong>Bet responsibly.</strong> Gambling involves risk. +EV is an estimate, not a '
        'guarantee of profit. Need help? Call Gambling Support BC at '
        '<a href="tel:+18887956111">1-888-795-6111</a> (free, confidential, 24/7) or '
        '<a href="https://www2.gov.bc.ca/gov/content/sports-culture/gambling-fundraising/'
        'gambling-support-bc" target="_blank" rel="noopener noreferrer">visit Gambling '
        'Support BC ↗</a>.</span></div>',
        unsafe_allow_html=True,
    )
    with st.expander("Disclosures, privacy & affiliate policy", expanded=False):
        st.markdown(
            '<div class="legal-details">'
            '<p><strong>Informational tool only.</strong> This product is an odds-comparison '
            'and analysis tool, not a sportsbook, betting operator, financial adviser, or '
            'guarantee of any outcome. It does not accept or place wagers. You are responsible '
            'for confirming legal eligibility and complying with the rules in your location.</p>'
            f'<p><strong>Odds and calculations.</strong> {data_warning} Fair odds, win '
            'probability, expected value, arbitrage, and middle estimates depend on third-party '
            'data and assumptions and may be incorrect. Verify the market, price, limits, rules, '
            'and availability directly with the sportsbook before betting.</p>'
            '<p><strong>Third-party links.</strong> Sportsbook links open independent third-party '
            'services. Their eligibility requirements, geographic restrictions, privacy '
            'practices, and terms apply. Sportsbook names and trademarks belong to their '
            'respective owners; inclusion does not imply endorsement or partnership.</p>'
            '<p><strong>Affiliate disclosure.</strong> If a sportsbook link is identified as an '
            'affiliate link, this product may receive compensation when you use it. Compensation '
            'does not change the calculation or ranking of opportunities. Unmarked links are not '
            'represented as affiliate relationships.</p>'
            '<p><strong>Privacy.</strong> This build stores preferences and optional bet-tracker '
            'entries in the product database. Never enter sportsbook passwords, payment details, '
            'or other sensitive account credentials. A hosted service should publish complete '
            'Terms of Use and a Privacy Policy before collecting user accounts, analytics, or '
            'other personal information.</p></div>',
            unsafe_allow_html=True,
        )


def run() -> None:
    app_icon = Path(__file__).resolve().parents[2] / "assets" / "advantage-betting-terminal.png"
    st.set_page_config(
        page_title="+EV Bets · Advantage Terminal",
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
    preload_quotes = repository.load_latest_quotes(selected_provider_id)
    available_books = sorted(
        {
            quote.sportsbook.name
            for quote in preload_quotes
        }
        | set(STARTER_BOOKS),
        key=_book_sort_key,
    )
    controls = _sidebar(repository, is_admin=is_admin)
    header_odds_status = _render_page_header()

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
            provider_name = (
                "OddsPapi" if controls["mode"] == "OddsPapi Free" else "The Odds API"
            )
            st.error(f"Enter an {provider_name} key before refreshing live odds.")
        else:
            provider: OddsProvider
            if controls["mode"] == "Demo":
                provider = DemoOddsProvider()
            elif controls["mode"] == "OddsPapi Free":
                stored = repository.load_settings()
                provider = OddsPapiProvider(
                    api_key=controls["api_key"],
                    bookmaker_slugs=tuple(
                        ODDSPAPI_BOOK_SLUGS[book]
                        for book in STARTER_BOOKS
                        if book in ODDSPAPI_BOOK_SLUGS
                    ),
                    tournament_ids={
                        str(key): int(value)
                        for key, value in _json_object(
                            stored.get("oddspapi_tournament_ids")
                        ).items()
                    },
                    market_catalog={
                        str(key): dict(value)
                        for key, value in _json_object(
                            stored.get("oddspapi_market_catalog")
                        ).items()
                        if isinstance(value, dict)
                    },
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
                        MARKET_KINDS[key]
                        for key in selected_market_keys
                        if key in MARKET_KINDS
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
                    _record_oddspapi_requests(repository, provider.request_count)
                st.session_state["refresh_notice"] = _diagnostic_message(diagnostics)
                st.rerun()
            except (KeyError, ValueError) as exc:
                st.error(str(exc))

    try:
        bankroll = Decimal(str(controls["bankroll"]))
        if bankroll <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        st.error("Working bankroll must be a positive number.")
        return

    as_of = datetime.now(UTC)
    provider_id = _provider_id(controls["mode"])
    latest_quotes = (
        preload_quotes
        if provider_id == selected_provider_id
        else repository.load_latest_quotes(provider_id)
    )
    quoted_event_ids = {quote.outcome.market.event_id for quote in latest_quotes}
    all_events = tuple(
        event
        for event in repository.load_events()
        if event.start_time > as_of
        and event.id in quoted_event_ids
    )
    all_event_map = {event.id: event for event in all_events}
    market_kinds = {
        "Moneyline": MarketKind.MONEYLINE,
        "Spread": MarketKind.SPREAD,
        "Total": MarketKind.TOTAL,
        "Player props": MarketKind.PLAYER_PROP,
    }
    selected_kinds = {
        market_kinds[label]
        for label in controls["active_markets"]
    }
    selected_leagues = {str(label).lower() for label in controls["active_leagues"]}
    quotes = tuple(
        quote
        for quote in latest_quotes
        if quote.outcome.market.kind in selected_kinds
        and quote.outcome.market.event_id in all_event_map
        and all_event_map[quote.outcome.market.event_id].league_id in selected_leagues
    )
    fresh_quotes = tuple(
        quote
        for quote in quotes
        if is_fresh(quote, as_of=as_of, max_age=controls["freshness"])
    )
    selected_event_ids = {quote.outcome.market.event_id for quote in quotes}
    events = tuple(event for event in all_events if event.id in selected_event_ids)
    event_map, league_names = _event_maps(events)

    ev_filters = _render_ev_filter_bar(
        available_books,
        str(controls["mode"]),
        events,
        quotes,
        as_of,
    )
    candidate_books: tuple[str, ...] | None = ev_filters.my_books or None
    values = detect_consensus_value(
        quotes,
        as_of=as_of,
        max_age=controls["freshness"],
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=candidate_books,
        include_stale=True,
    )
    stored_opportunities = repository.list_value_opportunities(provider_id)
    if stored_opportunities:
        active_opportunity_keys = {
            (item.sportsbook_id, item.outcome_id)
            for item in stored_opportunities
            if item.is_active
            and (
                _refresh_config(str(controls["mode"])).show_stale_recommendations
                or not item.is_stale
            )
        }
        values = tuple(
            item
            for item in values
            if (item.quote.sportsbook.id, item.quote.outcome.id) in active_opportunity_keys
        )
    filtered_values = _filter_value_opportunities(
        values,
        event_map,
        ev_filters,
        as_of=as_of,
        max_age=controls["freshness"],
    )
    controls["my_books"] = list(ev_filters.my_books)
    _render_odds_status(quotes, fresh_quotes, as_of, container=header_odds_status)
    _render_ev_summary(quotes, filtered_values)

    tab_names = ["Best Bets", "All Tools", "Games", "Movement"]
    if is_admin:
        tab_names.extend(["Bet Tracker", "Settings"])
    active_view = st.segmented_control(
        "Dashboard section",
        tab_names,
        default="Best Bets",
        key="dashboard_view",
        label_visibility="collapsed",
        width="stretch",
    ) or "Best Bets"

    if active_view == "Best Bets":
        _render_overview(
            filtered_values,
            event_map,
            quotes,
            as_of,
            controls["odds_format"],
        )
    elif active_view == "All Tools":
        active_tool = st.segmented_control(
            "Opportunity type",
            ["Arbitrage", "Middles", "My sportsbooks +EV", "Best lines"],
            default="Arbitrage",
            key="opportunity_view",
            label_visibility="collapsed",
        ) or "Arbitrage"
        if active_tool == "Arbitrage":
            arbs = detect_arbitrage(
                quotes,
                bankroll=bankroll,
                as_of=as_of,
                max_age=controls["freshness"],
            )
            _render_arb_detail(arbs, event_map, as_of, controls["min_roi"])
        elif active_tool == "Middles":
            middles = detect_middles(
                quotes,
                as_of=as_of,
                max_age=controls["freshness"],
            )
            _render_middles(middles, event_map)
        elif active_tool == "My sportsbooks +EV":
            _render_value(filtered_values, event_map, controls["odds_format"])
        else:
            _render_best_lines(quotes, event_map, controls["odds_format"])
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
    elif active_view == "Movement":
        history = tuple(
            quote
            for quote in repository.load_quotes_since(as_of - timedelta(hours=24))
            if quote.provider_id == provider_id
        )
        _render_line_movement(history, events)
    elif active_view == "Bet Tracker" and is_admin:
        _render_bets(quotes, events, repository)
    elif active_view == "Settings" and is_admin:
        _render_settings(repository, controls["mode"], controls)

    st.divider()
    _render_launch_disclosures(str(controls["mode"]))
