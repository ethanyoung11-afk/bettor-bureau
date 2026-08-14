from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
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
from odds_scanner.opportunities import best_prices, detect_arbitrage, is_fresh
from odds_scanner.presentation import format_odds
from odds_scanner.providers.base import OddsProvider
from odds_scanner.providers.demo import DemoOddsProvider, generate_demo_snapshots
from odds_scanner.providers.odds_api import FOOTBALL_LEAGUES, OddsApiError, OddsApiProvider
from odds_scanner.providers.oddspapi import OddsPapiError, OddsPapiProvider
from odds_scanner.service import ScannerService
from odds_scanner.storage.base import QuoteRepository
from odds_scanner.storage.sqlite import SQLiteQuoteRepository

LEAGUE_LABELS = {config.league_name: key for key, config in FOOTBALL_LEAGUES.items()}
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
LEAGUE_ICONS = {"NFL": "🏈", "NCAAF": "🏈", "CFL": "🏈"}


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


def _inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root { --terminal-green: #39d98a; --terminal-amber: #ffb547; --panel: #111827; }
        .stApp { background: #070b12; }
        [data-testid="stHeader"] { background: rgba(7,11,18,.88); }
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
        .risk-note { color:#94a3b8; font-size:.78rem; }
        .stDataFrame { border: 1px solid #202b3d; border-radius: 8px; overflow:hidden; }
        [data-testid="stExpander"] { border-color: #202b3d; background: #0b111c; }
        button[data-baseweb="tab"] { font-weight: 650; }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"] {
            background: #123525 !important; border-color: #2a8a5d !important;
            color: #6ee7b7 !important;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"] p {
            color: #6ee7b7 !important; font-weight: 750;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="true"]:hover {
            background: #184c37 !important; border-color: #39d98a !important;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"]:focus-visible {
            outline: 2px solid #39d98a !important; outline-offset: 2px;
        }
        [data-testid="stButtonGroup"] button[data-variant="pills"][aria-pressed="false"]:hover {
            border-color: #39d98a !important; color: #9af0c7 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_page_header(mode: str) -> tuple[list[str], Any, Any]:
    badge_class = "demo-badge" if mode == "Demo" else "terminal-badge"
    badge_text = (
        "DEMO MARKET"
        if mode == "Demo"
        else "BC LIVE BOARD"
        if mode == "OddsPapi Free"
        else "LIVE MARKET"
    )
    title_column, status_column = st.columns([4.8, 2], vertical_alignment="center")
    with title_column:
        st.markdown(
            f'<span class="terminal-badge {badge_class}">{badge_text}</span>',
            unsafe_allow_html=True,
        )
        st.title("Advantage Betting Terminal")
        st.caption("PlayNow and Betway value versus the market — with secondary arbitrage tools")
    with status_column:
        status_container = st.container(key="header_odds_status")
    dashboard_container = st.container(key="header_dashboard")
    filter_label, filter_control = st.columns([1.1, 5])
    filter_label.markdown("**🏈 Football leagues**")
    selected = filter_control.pills(
        "Football leagues",
        list(LEAGUE_ICONS),
        selection_mode="multi",
        default=list(LEAGUE_ICONS),
        format_func=lambda league: f"{LEAGUE_ICONS[league]} {league}",
        key="top_league_filter",
        label_visibility="collapsed",
    )
    return list(selected or LEAGUE_ICONS), status_container, dashboard_container


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
    metric_columns[1].metric("Sportsbooks", sportsbook_count)
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
    target = container if container is not None else st
    if not quotes:
        target.info("No saved odds are available yet. Use Refresh latest odds to load the board.")
        return
    last_refresh = max(quote.observed_at for quote in quotes)
    timestamp = last_refresh.astimezone().strftime("%a %b %d at %I:%M:%S %p")
    age = _elapsed_label(last_refresh, as_of)
    if len(fresh_quotes) == len(quotes):
        target.success(f"FRESH ODDS · Last refreshed {timestamp} ({age})", icon="✅")
    elif fresh_quotes:
        target.warning(f"PARTLY STALE ODDS · Last refreshed {timestamp} ({age})", icon="⚠️")
    else:
        target.warning(f"STALE ODDS · Last refreshed {timestamp} ({age})", icon="⚠️")


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
    for snapshot in generate_demo_snapshots():
        repository.save_snapshot(snapshot)
    st.session_state["demo_seeded"] = True


def _sidebar(
    repository: QuoteRepository,
    available_books: list[str],
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
                    st.caption("Each enabled sportsbook uses one request per manual refresh.")
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
        active_leagues = list(LEAGUE_ICONS)
        with st.expander("Markets", expanded=False):
            market_options = ["Moneyline", "Spread", "Total"]
            if data_mode in {"Demo", "OddsPapi Free"}:
                market_options.append("Player props")
            active_markets = st.multiselect(
                "Markets",
                market_options,
                default=market_options,
            )
            if data_mode == "OddsPapi Free":
                st.caption("Player props use the same refresh. Futures require a separate feed.")
        with st.expander("Sportsbooks", expanded=False):
            st.caption(
                "PlayNow and Betway are pinned first. All comparison books are enabled by default."
            )
            active_books = [
                book
                for book in available_books
                if st.toggle(
                    f"★ {book}" if book in PRIORITY_BOOKS else book,
                    value=True,
                    key=f"sportsbook_enabled_{stable_id('ui-book-v2', data_mode, book)}",
                )
            ]
            if data_mode == "OddsPapi Free":
                st.caption(
                    f"{len(active_books)} enabled = about {len(active_books)} requests per refresh."
                )
        st.divider()
        refresh_label = {
            "Demo": "Refresh demo feed",
            "OddsPapi Free": "Refresh latest odds",
            "The Odds API": "Fetch live odds",
        }[data_mode]
        refresh = False
        if is_admin:
            refresh = st.button(
                refresh_label,
                type="primary",
                width="stretch",
            )
            if data_mode == "OddsPapi Free":
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
        "active_books": active_books,
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
                help="The actual odds offered by PlayNow or Betway."
            ),
            "Fair odds": st.column_config.TextColumn(
                "Estimated fair odds",
                help=(
                    "A margin-free estimate derived from the other enabled sportsbooks. It is "
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
        "Only PlayNow and Betway candidates are shown. Consensus uses other enabled books after "
        "removing their margin."
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
    for event in available_events:
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


def _render_priority_value_bets(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    odds_format: str,
    as_of: datetime,
) -> None:
    st.markdown('<div class="section-kicker">Primary betting board</div>', unsafe_allow_html=True)
    st.subheader("Best PlayNow + Betway +EV bets")
    minimum = Decimal(str(st.session_state["min_ev"])) / Decimal("100")
    qualifying = tuple(item for item in values if item.expected_value >= minimum)
    st.caption(
        f"Target books only · {minimum:.1%} minimum EV · target book excluded from its own "
        "no-vig consensus"
    )
    if not qualifying:
        return

    top = qualifying[0]
    event = event_map.get(top.quote.outcome.market.event_id)
    event_name = event.name if event else top.quote.outcome.market.event_id
    selection = _selection_label(top.quote, event)
    market_label = _market_label(top.quote.outcome.market)
    pick_label = (
        f"{selection} MONEYLINE"
        if top.quote.outcome.market.kind is MarketKind.MONEYLINE
        else selection
    )
    offered_odds = format_odds(top.quote.decimal_odds, odds_format)
    sportsbook_url = SPORTSBOOK_URLS.get(top.quote.sportsbook.name)
    with st.container(border=True, key="top_opportunity"):
        recommendation_column, action_column = st.columns(
            [3.2, 1.25], vertical_alignment="center"
        )
        with recommendation_column:
            st.markdown('<div class="best-bet-badge">★ #1 BEST BET</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="best-bet-pick">BET {html.escape(pick_label.upper())}</div>',
                unsafe_allow_html=True,
            )
            event_detail = f"{event_name} · {market_label}"
            if event is not None:
                event_detail += (
                    f" · {event.league_id.upper()} · "
                    f"{event.start_time.astimezone().strftime('%a %b %d, %I:%M %p')}"
                )
            st.markdown(
                f'<div class="best-bet-event">{html.escape(event_detail)}</div>',
                unsafe_allow_html=True,
            )
        with action_column:
            st.caption("BEST AVAILABLE PRICE")
            st.markdown(f"**{top.quote.sportsbook.name} · {offered_odds}**")
            if sportsbook_url:
                st.link_button(
                    f"Bet now on {top.quote.sportsbook.name} · {offered_odds}",
                    sportsbook_url,
                    type="primary",
                    icon=":material/open_in_new:",
                    width="stretch",
                )

        metrics = st.columns(4)
        metrics[0].metric(
            "Bet at",
            top.quote.sportsbook.name,
            help="The sportsbook offering the recommended price.",
        )
        metrics[1].metric(
            "Offered odds",
            offered_odds,
            help="The actual price currently offered by PlayNow or Betway.",
        )
        metrics[2].metric(
            "Estimated edge",
            f"{top.expected_value:.2%}",
            help="Estimated long-run return relative to the consensus fair probability.",
        )
        metrics[3].metric(
            "Consensus fair odds",
            format_odds(top.fair_odds, odds_format),
            help=(
                "A margin-free estimate derived from the other enabled sportsbooks. It is not "
                "an offered betting price."
            ),
        )
        st.markdown(
            '<div class="best-bet-proof">'
            f"Compared with {html.escape(', '.join(top.reference_sportsbooks))} · "
            f"Price updated {_age_label(top.quote, as_of)} ago · Confirm the price before betting"
            "</div>",
            unsafe_allow_html=True,
        )

    if len(qualifying) <= 1:
        return

    st.markdown("#### More +EV bets")
    st.caption("Open any bet for the complete price and consensus breakdown.")
    for rank, item in enumerate(qualifying[1:20], start=2):
        item_event = event_map.get(item.quote.outcome.market.event_id)
        item_event_name = (
            item_event.name if item_event is not None else item.quote.outcome.market.event_id
        )
        item_market = _market_label(item.quote.outcome.market)
        item_selection = _selection_label(item.quote, item_event)
        item_pick = (
            f"{item_selection} moneyline"
            if item.quote.outcome.market.kind is MarketKind.MONEYLINE
            else item_selection
        )
        item_odds = format_odds(item.quote.decimal_odds, odds_format)
        item_fair_odds = format_odds(item.fair_odds, odds_format)
        item_book = item.quote.sportsbook.name
        item_url = SPORTSBOOK_URLS.get(item_book)
        event_detail = f"{item_event_name} · {item_market}"
        if item_event is not None:
            event_detail += (
                f" · {item_event.league_id.upper()} · "
                f"{item_event.start_time.astimezone().strftime('%a %b %d, %I:%M %p')}"
            )

        expander_label = (
            f"#{rank} · BET {item_pick.upper()} · {item_book} {item_odds} · "
            f"{item.expected_value:.2%} +EV"
        )
        with st.container(key=f"value_opportunity_{rank}"), st.expander(
            expander_label, expanded=False
        ):
            detail_column, action_column = st.columns(
                [3.3, 1.3], vertical_alignment="center"
            )
            detail_column.markdown(
                f'<div class="value-bet-pick">BET {html.escape(item_pick.upper())}</div>'
                f'<div class="value-bet-event">{html.escape(event_detail)}</div>',
                unsafe_allow_html=True,
            )
            with action_column:
                st.caption("BEST AVAILABLE PRICE")
                st.markdown(f"**{item_book} · {item_odds}**")
                if item_url:
                    st.link_button(
                        f"Bet now on {item_book} · {item_odds}",
                        item_url,
                        type="primary",
                        icon=":material/open_in_new:",
                        width="stretch",
                    )

            item_metrics = st.columns(4)
            item_metrics[0].metric(
                "Bet at",
                item_book,
                help="The sportsbook offering the recommended price.",
            )
            item_metrics[1].metric(
                "Offered odds",
                item_odds,
                help="The actual price currently offered by the sportsbook.",
            )
            item_metrics[2].metric(
                "Estimated edge",
                f"{item.expected_value:.2%}",
                help="Estimated long-run return relative to the consensus fair probability.",
            )
            item_metrics[3].metric(
                "Consensus fair odds",
                item_fair_odds,
                help=(
                    "A margin-free estimate derived from the other enabled sportsbooks. "
                    "It is not an offered betting price."
                ),
            )
            st.markdown(
                '<div class="value-bet-proof">'
                f"Compared with {html.escape(', '.join(item.reference_sportsbooks))} · "
                f"Price updated {_age_label(item.quote, as_of)} ago · "
                "Confirm the price before betting"
                "</div>",
                unsafe_allow_html=True,
            )


def _render_overview(
    values: tuple[ValueOpportunity, ...],
    event_map: dict[str, Event],
    as_of: datetime,
    odds_format: str,
) -> None:
    _render_priority_value_bets(values, event_map, odds_format, as_of)


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
            "active_sportsbooks": controls["active_books"],
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


def run() -> None:
    app_icon = Path(__file__).resolve().parents[2] / "assets" / "advantage-betting-terminal.png"
    st.set_page_config(
        page_title="Advantage Betting Terminal",
        page_icon=str(app_icon),
        layout="wide",
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
    preload_time = datetime.now(UTC)
    preload_history = repository.load_quotes_since(preload_time - timedelta(hours=24))
    selected_source = str(st.session_state.get("data_source", "Demo"))
    selected_provider_id = _provider_id(selected_source)
    available_books = sorted(
        {
            quote.sportsbook.name
            for quote in preload_history
            if quote.provider_id == selected_provider_id
        }
        | set(STARTER_BOOKS),
        key=_book_sort_key,
    )
    controls = _sidebar(repository, available_books, is_admin=is_admin)
    active_leagues, header_odds_status, header_dashboard = _render_page_header(
        str(controls["mode"])
    )
    controls["active_leagues"] = active_leagues

    refresh_notice = st.session_state.pop("refresh_notice", None)
    if refresh_notice:
        st.toast(str(refresh_notice))

    selected_league_keys = [
        LEAGUE_LABELS[label] for label in controls["active_leagues"] if label in LEAGUE_LABELS
    ]
    selected_market_keys = [
        MARKET_LABELS[label] for label in controls["active_markets"] if label in MARKET_LABELS
    ]

    if controls["mode"] == "Demo":
        if controls["refresh"]:
            snapshot = DemoOddsProvider().fetch_snapshot(
                selected_league_keys,
                selected_market_keys,
            )
            repository.save_snapshot(snapshot)
            st.session_state["refresh_notice"] = (
                f"Demo feed refreshed: {len(snapshot.quotes)} quotes"
            )
            st.rerun()
    elif controls["refresh"]:
        if not controls["api_key"]:
            provider_name = "OddsPapi" if controls["mode"] == "OddsPapi Free" else "The Odds API"
            st.error(f"Enter an {provider_name} key before refreshing live odds.")
        else:
            provider: OddsProvider | None = None
            try:
                if controls["mode"] == "OddsPapi Free":
                    stored = repository.load_settings()
                    tournament_ids = {
                        str(key): int(value)
                        for key, value in _json_object(
                            stored.get("oddspapi_tournament_ids")
                        ).items()
                    }
                    market_catalog = {
                        str(key): dict(value)
                        for key, value in _json_object(
                            stored.get("oddspapi_market_catalog")
                        ).items()
                        if isinstance(value, dict)
                    }
                    provider = OddsPapiProvider(
                        api_key=controls["api_key"],
                        bookmaker_slugs=tuple(
                            ODDSPAPI_BOOK_SLUGS[book]
                            for book in controls["active_books"]
                            if book in ODDSPAPI_BOOK_SLUGS
                        ),
                        tournament_ids=tournament_ids,
                        market_catalog=market_catalog,
                    )
                else:
                    provider = OddsApiProvider(
                        api_key=controls["api_key"],
                        regions=controls["regions"],
                    )
                service = ScannerService(
                    provider=provider,
                    repository=repository,
                    freshness=controls["freshness"],
                )
                snapshot = service.refresh(selected_league_keys, selected_market_keys)
                if isinstance(provider, OddsPapiProvider):
                    repository.save_setting(
                        "oddspapi_tournament_ids",
                        json.dumps(provider.tournament_ids, separators=(",", ":")),
                    )
                    repository.save_setting(
                        "oddspapi_market_catalog",
                        json.dumps(provider.market_catalog, separators=(",", ":")),
                    )
                    _used, requests_remaining = _record_oddspapi_requests(
                        repository,
                        provider.request_count,
                    )
                st.session_state["refresh_notice"] = (
                    f"Stored {len(snapshot.quotes)} latest quotes from "
                    f"{len({quote.sportsbook.name for quote in snapshot.quotes})} sportsbooks"
                    + (
                        f" · about {requests_remaining} free requests remain"
                        if isinstance(provider, OddsPapiProvider)
                        else ""
                    )
                )
                st.rerun()
            except (OddsApiError, OddsPapiError, KeyError, ValueError) as exc:
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
                st.error(str(exc))

    try:
        bankroll = Decimal(str(controls["bankroll"]))
        if bankroll <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        st.error("Working bankroll must be a positive number.")
        return

    as_of = datetime.now(UTC)
    history_all = repository.load_quotes_since(as_of - timedelta(hours=24))
    provider_id = _provider_id(controls["mode"])
    history = tuple(quote for quote in history_all if quote.provider_id == provider_id)
    latest_quotes = repository.load_latest_quotes(provider_id)
    all_events = tuple(
        event
        for event in repository.load_events()
        if event.start_time > as_of
        and any(quote.outcome.market.event_id == event.id for quote in latest_quotes)
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
    selected_books = set(controls["active_books"])
    quotes = tuple(
        quote
        for quote in latest_quotes
        if quote.sportsbook.name in selected_books
        and quote.outcome.market.kind in selected_kinds
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

    arbs = detect_arbitrage(
        quotes,
        bankroll=bankroll,
        as_of=as_of,
        max_age=controls["freshness"],
    )
    middles = detect_middles(quotes, as_of=as_of, max_age=controls["freshness"])
    values = detect_consensus_value(
        quotes,
        as_of=as_of,
        max_age=controls["freshness"],
        minimum_ev=Decimal("0.005"),
        candidate_sportsbooks=PRIORITY_BOOKS,
        include_stale=True,
    )
    _render_odds_status(quotes, fresh_quotes, as_of, container=header_odds_status)
    _render_header_dashboard(header_dashboard, quotes, values)

    tab_names = ["Best Bets", "All Tools", "Games", "Movement"]
    if is_admin:
        tab_names.extend(["Bet Tracker", "Settings"])
    tabs = st.tabs(tab_names)
    overview_tab, opportunities_tab, games_tab, movement_tab = tabs[:4]
    with overview_tab:
        _render_overview(
            values,
            event_map,
            as_of,
            controls["odds_format"],
        )
    with opportunities_tab:
        arb_tab, middle_tab, value_tab, best_tab = st.tabs(
            ["Arbitrage", "Middles", "PlayNow + Betway +EV", "Best lines"]
        )
        with arb_tab:
            _render_arb_detail(arbs, event_map, as_of, controls["min_roi"])
        with middle_tab:
            _render_middles(middles, event_map)
        with value_tab:
            _render_value(values, event_map, controls["odds_format"])
        with best_tab:
            _render_best_lines(quotes, event_map, controls["odds_format"])
    with games_tab:
        _render_games(
            quotes,
            events,
            controls["odds_format"],
            list(controls["active_books"]),
            repository,
            is_admin=is_admin,
        )
    with movement_tab:
        _render_line_movement(history, events)
    if is_admin:
        bets_tab, settings_tab = tabs[4:]
        with bets_tab:
            _render_bets(quotes, events, repository)
        with settings_tab:
            _render_settings(repository, controls["mode"], controls)

    st.divider()
    footer_text = (
        "Demo prices are fictional. Verify live prices and availability directly with each "
        "sportsbook. Bet responsibly."
        if controls["mode"] == "Demo"
        else "Live feeds can be delayed or incomplete. Verify every price and market directly "
        "with each sportsbook before betting."
    )
    st.markdown(
        f'<div class="risk-note">{footer_text}</div>',
        unsafe_allow_html=True,
    )
