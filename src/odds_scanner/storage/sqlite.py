from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock

from odds_scanner.domain import (
    ApiUsageSummary,
    BetStatus,
    Event,
    MarketKey,
    MarketKind,
    OddsSnapshot,
    OpportunityCounts,
    OpportunityStatus,
    OutcomeKey,
    OutcomeSide,
    Participant,
    Quote,
    RefreshRun,
    Sportsbook,
    TrackedBet,
    ValueOpportunityRecord,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leagues (
    id TEXT PRIMARY KEY,
    sport_id TEXT NOT NULL REFERENCES sports(id),
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL REFERENCES leagues(id),
    start_time TEXT NOT NULL,
    home_id TEXT NOT NULL REFERENCES participants(id),
    away_id TEXT NOT NULL REFERENCES participants(id),
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_events (
    provider_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    PRIMARY KEY(provider_id, event_id)
);
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    kind TEXT NOT NULL,
    required_sides TEXT NOT NULL,
    period TEXT NOT NULL,
    line TEXT,
    subject_id TEXT,
    stat_key TEXT,
    variant TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(id),
    side TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sportsbooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    sportsbook_id TEXT NOT NULL REFERENCES sportsbooks(id),
    outcome_id TEXT NOT NULL REFERENCES outcomes(id),
    decimal_odds TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_event_id TEXT,
    source_url TEXT,
    UNIQUE(provider_id, sportsbook_id, outcome_id, source_updated_at, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_quotes_source_updated ON quotes(source_updated_at);
CREATE INDEX IF NOT EXISTS idx_quotes_outcome ON quotes(outcome_id);
CREATE TABLE IF NOT EXISTS latest_quote_state (
    provider_id TEXT NOT NULL,
    sportsbook_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    quote_id INTEGER NOT NULL REFERENCES quotes(id),
    PRIMARY KEY(provider_id, sportsbook_id, outcome_id)
);
CREATE TABLE IF NOT EXISTS user_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    market_label TEXT NOT NULL,
    selection TEXT NOT NULL,
    sportsbook TEXT NOT NULL,
    decimal_odds TEXT NOT NULL,
    stake TEXT NOT NULL,
    status TEXT NOT NULL,
    profit_loss TEXT,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS watchlist (
    event_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS value_opportunities (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    sportsbook_id TEXT NOT NULL REFERENCES sportsbooks(id),
    sportsbook TEXT NOT NULL,
    outcome_id TEXT NOT NULL REFERENCES outcomes(id),
    market_kind TEXT NOT NULL,
    selection TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    last_price_change_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL,
    is_stale INTEGER NOT NULL,
    status TEXT NOT NULL,
    recommended_price TEXT NOT NULL,
    current_price TEXT NOT NULL,
    ev_at_activation TEXT NOT NULL,
    current_ev TEXT NOT NULL,
    fair_probability TEXT NOT NULL,
    implied_probability TEXT NOT NULL,
    price_change_count_recent INTEGER NOT NULL DEFAULT 0,
    api_snapshot_id TEXT,
    deactivated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_value_opportunities_provider_active
    ON value_opportunities(provider_id, is_active, is_stale);
CREATE INDEX IF NOT EXISTS idx_value_opportunities_event
    ON value_opportunities(event_id, market_kind);
CREATE TABLE IF NOT EXISTS refresh_runs (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    league_keys TEXT NOT NULL,
    market_keys TEXT NOT NULL,
    requests_made INTEGER NOT NULL,
    credits_consumed INTEGER NOT NULL,
    credits_remaining INTEGER,
    events_checked INTEGER NOT NULL,
    sportsbooks_checked INTEGER NOT NULL,
    new_opportunities INTEGER NOT NULL,
    revalidated_opportunities INTEGER NOT NULL,
    deactivated_opportunities INTEGER NOT NULL,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_runs_provider_finished
    ON refresh_runs(provider_id, finished_at);
CREATE TABLE IF NOT EXISTS refresh_locks (
    provider_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class SQLiteQuoteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            quote_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(quotes)")
            }
            if "source_url" not in quote_columns:
                connection.execute("ALTER TABLE quotes ADD COLUMN source_url TEXT")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_snapshot(
        self,
        snapshot: OddsSnapshot,
        *,
        replace_event_ids: Sequence[str] | None = None,
        replace_market_kinds: Sequence[MarketKind] | None = None,
    ) -> None:
        with self._write_lock, self._connection() as connection:
            connection.executemany(
                "INSERT INTO sports(id, name) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                ((sport.id, sport.name) for sport in snapshot.sports),
            )
            connection.executemany(
                "INSERT INTO leagues(id, sport_id, name) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET sport_id=excluded.sport_id, name=excluded.name",
                ((league.id, league.sport_id, league.name) for league in snapshot.leagues),
            )
            participants = {
                participant.id: participant
                for event in snapshot.events
                for participant in (event.home, event.away)
            }
            connection.executemany(
                "INSERT INTO participants(id, name) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                ((item.id, item.name) for item in participants.values()),
            )
            connection.executemany(
                "INSERT INTO events(id, league_id, start_time, home_id, away_id, name) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "start_time=excluded.start_time, name=excluded.name",
                (
                    (
                        event.id,
                        event.league_id,
                        event.start_time.isoformat(),
                        event.home.id,
                        event.away.id,
                        event.name,
                    )
                    for event in snapshot.events
                ),
            )
            connection.executemany(
                "INSERT INTO provider_events(provider_id, event_id) VALUES (?, ?) "
                "ON CONFLICT(provider_id, event_id) DO NOTHING",
                ((snapshot.provider_id, event.id) for event in snapshot.events),
            )
            self._clear_latest_scope(
                connection,
                snapshot.provider_id,
                tuple(replace_event_ids)
                if replace_event_ids is not None
                else tuple(event.id for event in snapshot.events),
                replace_market_kinds,
            )
            for quote in snapshot.quotes:
                self._save_quote(connection, quote)

    @staticmethod
    def _clear_latest_scope(
        connection: sqlite3.Connection,
        provider_id: str,
        event_ids: Sequence[str],
        market_kinds: Sequence[MarketKind] | None,
    ) -> None:
        if not event_ids:
            return
        event_placeholders = ", ".join("?" for _ in event_ids)
        parameters: list[object] = [provider_id, *event_ids]
        market_clause = ""
        if market_kinds:
            kind_placeholders = ", ".join("?" for _ in market_kinds)
            market_clause = f" AND m.kind IN ({kind_placeholders})"
            parameters.extend(kind.value for kind in market_kinds)
        connection.execute(
            "DELETE FROM latest_quote_state WHERE provider_id = ? AND outcome_id IN ("
            "SELECT o.id FROM outcomes o JOIN markets m ON m.id = o.market_id "
            f"WHERE m.event_id IN ({event_placeholders}){market_clause})",
            parameters,
        )

    @staticmethod
    def _save_quote(connection: sqlite3.Connection, quote: Quote) -> None:
        market = quote.outcome.market
        connection.execute(
            "INSERT INTO markets(id, event_id, kind, required_sides, period, line, subject_id, "
            "stat_key, variant) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (
                market.id,
                market.event_id,
                market.kind.value,
                json.dumps([side.value for side in market.required_sides]),
                market.period,
                str(market.line) if market.line is not None else None,
                market.subject_id,
                market.stat_key,
                market.variant,
            ),
        )
        connection.execute(
            "INSERT INTO outcomes(id, market_id, side) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (quote.outcome.id, market.id, quote.outcome.side.value),
        )
        connection.execute(
            "INSERT INTO sportsbooks(id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (quote.sportsbook.id, quote.sportsbook.name),
        )
        connection.execute(
            "INSERT OR IGNORE INTO quotes(provider_id, sportsbook_id, outcome_id, decimal_odds, "
            "source_updated_at, observed_at, source_event_id, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                quote.provider_id,
                quote.sportsbook.id,
                quote.outcome.id,
                str(quote.decimal_odds),
                quote.source_updated_at.isoformat(),
                quote.observed_at.isoformat(),
                quote.source_event_id,
                quote.source_url,
            ),
        )
        quote_row = connection.execute(
            "SELECT id FROM quotes WHERE provider_id = ? AND sportsbook_id = ? "
            "AND outcome_id = ? AND source_updated_at = ? AND observed_at = ?",
            (
                quote.provider_id,
                quote.sportsbook.id,
                quote.outcome.id,
                quote.source_updated_at.isoformat(),
                quote.observed_at.isoformat(),
            ),
        ).fetchone()
        if quote_row is None:
            raise RuntimeError("Failed to persist quote state")
        connection.execute(
            "INSERT INTO latest_quote_state(provider_id, sportsbook_id, outcome_id, quote_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(provider_id, sportsbook_id, outcome_id) DO UPDATE "
            "SET quote_id=excluded.quote_id",
            (quote.provider_id, quote.sportsbook.id, quote.outcome.id, int(quote_row["id"])),
        )

    def load_quotes_since(self, since: datetime) -> tuple[Quote, ...]:
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone-aware")
        query = """
            SELECT q.provider_id, q.decimal_odds, q.source_updated_at, q.observed_at,
                   q.source_event_id, q.source_url, b.id AS sportsbook_id,
                   b.name AS sportsbook_name,
                   o.side, m.event_id, m.kind, m.required_sides, m.period, m.line,
                   m.subject_id, m.stat_key, m.variant
            FROM quotes q
            JOIN sportsbooks b ON b.id = q.sportsbook_id
            JOIN outcomes o ON o.id = q.outcome_id
            JOIN markets m ON m.id = o.market_id
            WHERE q.source_updated_at >= ?
        """
        with self._connection() as connection:
            rows = connection.execute(query, (since.isoformat(),)).fetchall()
        return tuple(self._row_to_quote(row) for row in rows)

    def load_latest_quotes(self, provider_id: str) -> tuple[Quote, ...]:
        """Load the last observed price for every sportsbook outcome, regardless of age."""
        query = """
            SELECT q.provider_id, q.decimal_odds, q.source_updated_at, q.observed_at,
                   q.source_event_id, q.source_url, b.id AS sportsbook_id,
                   b.name AS sportsbook_name,
                   o.side, m.event_id, m.kind, m.required_sides, m.period, m.line,
                   m.subject_id, m.stat_key, m.variant
              FROM latest_quote_state current
              JOIN quotes q ON q.id = current.quote_id
              JOIN sportsbooks b ON b.id = q.sportsbook_id
              JOIN outcomes o ON o.id = q.outcome_id
              JOIN markets m ON m.id = o.market_id
             WHERE current.provider_id = ?
        """
        with self._connection() as connection:
            rows = connection.execute(query, (provider_id,)).fetchall()
            if not rows:
                rows = connection.execute(
                    """
                    SELECT q.provider_id, q.decimal_odds, q.source_updated_at, q.observed_at,
                           q.source_event_id, q.source_url, b.id AS sportsbook_id,
                           b.name AS sportsbook_name,
                           o.side, m.event_id, m.kind, m.required_sides, m.period, m.line,
                           m.subject_id, m.stat_key, m.variant
                      FROM (
                            SELECT quotes.*,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY provider_id, sportsbook_id, outcome_id
                                       ORDER BY observed_at DESC, source_updated_at DESC, id DESC
                                   ) AS latest_rank
                              FROM quotes
                             WHERE provider_id = ?
                           ) q
                      JOIN sportsbooks b ON b.id = q.sportsbook_id
                      JOIN outcomes o ON o.id = q.outcome_id
                      JOIN markets m ON m.id = o.market_id
                     WHERE q.latest_rank = 1
                    """,
                    (provider_id,),
                ).fetchall()
        return tuple(self._row_to_quote(row) for row in rows)

    def load_events(self, provider_id: str | None = None) -> tuple[Event, ...]:
        provider_clause = (
            "WHERE EXISTS (SELECT 1 FROM provider_events pe "
            "WHERE pe.event_id = e.id AND pe.provider_id = ?)"
            if provider_id
            else ""
        )
        query = f"""
            SELECT e.id, e.league_id, e.start_time, e.name,
                   hp.id AS home_id, hp.name AS home_name,
                   ap.id AS away_id, ap.name AS away_name
            FROM events e
            JOIN participants hp ON hp.id = e.home_id
            JOIN participants ap ON ap.id = e.away_id
            {provider_clause}
            ORDER BY e.start_time
        """
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO provider_events(provider_id, event_id) "
                "SELECT DISTINCT q.provider_id, m.event_id FROM quotes q "
                "JOIN outcomes o ON o.id = q.outcome_id "
                "JOIN markets m ON m.id = o.market_id"
            )
            rows = connection.execute(
                query,
                (provider_id,) if provider_id else (),
            ).fetchall()
        return tuple(
            Event(
                id=str(row["id"]),
                league_id=str(row["league_id"]),
                start_time=datetime.fromisoformat(str(row["start_time"])),
                home=Participant(id=str(row["home_id"]), name=str(row["home_name"])),
                away=Participant(id=str(row["away_id"]), name=str(row["away_name"])),
                name=str(row["name"]),
            )
            for row in rows
        )

    def list_value_opportunities(
        self,
        provider_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[ValueOpportunityRecord, ...]:
        query = "SELECT * FROM value_opportunities WHERE provider_id = ?"
        if active_only:
            query += " AND is_active = 1"
        query += " ORDER BY current_ev DESC, last_verified_at DESC"
        with self._connection() as connection:
            rows = connection.execute(query, (provider_id,)).fetchall()
        return tuple(self._row_to_value_opportunity(row) for row in rows)

    def save_value_opportunities(
        self,
        opportunities: tuple[ValueOpportunityRecord, ...],
    ) -> None:
        if not opportunities:
            return
        statement = """
            INSERT INTO value_opportunities(
                id, provider_id, event_id, sportsbook_id, sportsbook, outcome_id,
                market_kind, selection, first_seen_at, last_seen_at, last_verified_at,
                last_price_change_at, last_updated_at, is_active, is_stale, status,
                recommended_price, current_price, ev_at_activation, current_ev,
                fair_probability, implied_probability, price_change_count_recent,
                api_snapshot_id, deactivated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT(id) DO UPDATE SET
                provider_id=excluded.provider_id,
                event_id=excluded.event_id,
                sportsbook_id=excluded.sportsbook_id,
                sportsbook=excluded.sportsbook,
                outcome_id=excluded.outcome_id,
                market_kind=excluded.market_kind,
                selection=excluded.selection,
                first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at,
                last_verified_at=excluded.last_verified_at,
                last_price_change_at=excluded.last_price_change_at,
                last_updated_at=excluded.last_updated_at,
                is_active=excluded.is_active,
                is_stale=excluded.is_stale,
                status=excluded.status,
                recommended_price=excluded.recommended_price,
                current_price=excluded.current_price,
                ev_at_activation=excluded.ev_at_activation,
                current_ev=excluded.current_ev,
                fair_probability=excluded.fair_probability,
                implied_probability=excluded.implied_probability,
                price_change_count_recent=excluded.price_change_count_recent,
                api_snapshot_id=excluded.api_snapshot_id,
                deactivated_at=excluded.deactivated_at
        """
        with self._write_lock, self._connection() as connection:
            connection.executemany(
                statement,
                (self._value_opportunity_values(item) for item in opportunities),
            )

    def mark_stale_opportunities(
        self,
        provider_id: str,
        stale_before: datetime,
        marked_at: datetime,
    ) -> int:
        del marked_at
        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE value_opportunities SET is_stale = 1, status = ? "
                "WHERE provider_id = ? AND is_active = 1 AND last_verified_at < ?",
                (OpportunityStatus.STALE.value, provider_id, stale_before.isoformat()),
            )
            return max(0, cursor.rowcount)

    def opportunity_counts(self, provider_id: str) -> OpportunityCounts:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_count, "
                "SUM(CASE WHEN is_stale = 1 THEN 1 ELSE 0 END) AS stale_count "
                "FROM value_opportunities WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return OpportunityCounts(
            active=int(row["active_count"] or 0),
            stale=int(row["stale_count"] or 0),
        )

    def try_acquire_refresh_lock(
        self,
        provider_id: str,
        owner_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> bool:
        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO refresh_locks(provider_id, owner_id, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(provider_id) DO UPDATE SET "
                "owner_id=excluded.owner_id, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at WHERE refresh_locks.expires_at <= ?",
                (
                    provider_id,
                    owner_id,
                    acquired_at.isoformat(),
                    expires_at.isoformat(),
                    acquired_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def release_refresh_lock(self, provider_id: str, owner_id: str) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM refresh_locks WHERE provider_id = ? AND owner_id = ?",
                (provider_id, owner_id),
            )

    def record_refresh_run(self, run: RefreshRun) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO refresh_runs(id, provider_id, trigger_type, status, started_at, "
                "finished_at, league_keys, market_keys, requests_made, credits_consumed, "
                "credits_remaining, events_checked, sportsbooks_checked, new_opportunities, "
                "revalidated_opportunities, deactivated_opportunities, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.provider_id,
                    run.trigger_type,
                    run.status,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat(),
                    json.dumps(run.league_keys),
                    json.dumps(run.market_keys),
                    run.requests_made,
                    run.credits_consumed,
                    run.credits_remaining,
                    run.events_checked,
                    run.sportsbooks_checked,
                    run.new_opportunities,
                    run.revalidated_opportunities,
                    run.deactivated_opportunities,
                    run.error_message,
                ),
            )

    def api_usage_summary(
        self,
        provider_id: str,
        *,
        as_of: datetime,
    ) -> ApiUsageSummary:
        day_prefix = as_of.strftime("%Y-%m-%d")
        month_prefix = as_of.strftime("%Y-%m")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT "
                "SUM(CASE WHEN started_at LIKE ? THEN requests_made ELSE 0 END) requests_today, "
                "SUM(CASE WHEN started_at LIKE ? THEN requests_made ELSE 0 END) requests_month, "
                "SUM(CASE WHEN started_at LIKE ? THEN credits_consumed ELSE 0 END) credits_month, "
                "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) successes, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) failures, "
                "MAX(CASE WHEN status = 'success' THEN finished_at END) last_success, "
                "MAX(CASE WHEN status = 'failed' THEN finished_at END) last_failure "
                "FROM refresh_runs WHERE provider_id = ?",
                (
                    f"{day_prefix}%",
                    f"{month_prefix}%",
                    f"{month_prefix}%",
                    provider_id,
                ),
            ).fetchone()
        return ApiUsageSummary(
            provider_id=provider_id,
            requests_today=int(row["requests_today"] or 0),
            requests_this_month=int(row["requests_month"] or 0),
            credits_this_month=int(row["credits_month"] or 0),
            successful_refreshes=int(row["successes"] or 0),
            failed_refreshes=int(row["failures"] or 0),
            last_successful_refresh=(
                datetime.fromisoformat(str(row["last_success"]))
                if row["last_success"] is not None
                else None
            ),
            last_failed_refresh=(
                datetime.fromisoformat(str(row["last_failure"]))
                if row["last_failure"] is not None
                else None
            ),
        )

    def save_setting(self, key: str, value: str) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO user_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def load_settings(self) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT key, value FROM user_settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def add_bet(self, bet: TrackedBet) -> int:
        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO tracked_bets(created_at, event_id, event_name, market_label, "
                "selection, sportsbook, decimal_odds, stake, status, profit_loss, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bet.created_at.isoformat(),
                    bet.event_id,
                    bet.event_name,
                    bet.market_label,
                    bet.selection,
                    bet.sportsbook,
                    str(bet.decimal_odds),
                    str(bet.stake),
                    bet.status.value,
                    str(bet.profit_loss) if bet.profit_loss is not None else None,
                    bet.notes,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to create tracked bet")
            return int(cursor.lastrowid)

    def list_bets(self) -> tuple[TrackedBet, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tracked_bets ORDER BY created_at DESC"
            ).fetchall()
        return tuple(
            TrackedBet(
                id=int(row["id"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                event_id=str(row["event_id"]),
                event_name=str(row["event_name"]),
                market_label=str(row["market_label"]),
                selection=str(row["selection"]),
                sportsbook=str(row["sportsbook"]),
                decimal_odds=Decimal(str(row["decimal_odds"])),
                stake=Decimal(str(row["stake"])),
                status=BetStatus(str(row["status"])),
                profit_loss=(
                    Decimal(str(row["profit_loss"])) if row["profit_loss"] is not None else None
                ),
                notes=str(row["notes"]),
            )
            for row in rows
        )

    def update_bet(self, bet_id: int, status: BetStatus, profit_loss: Decimal | None) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE tracked_bets SET status = ?, profit_loss = ? WHERE id = ?",
                (status.value, str(profit_loss) if profit_loss is not None else None, bet_id),
            )

    def watched_event_ids(self) -> frozenset[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT event_id FROM watchlist").fetchall()
        return frozenset(str(row["event_id"]) for row in rows)

    def set_event_watched(self, event_id: str, watched: bool, created_at: datetime) -> None:
        with self._write_lock, self._connection() as connection:
            if watched:
                connection.execute(
                    "INSERT OR IGNORE INTO watchlist(event_id, created_at) VALUES (?, ?)",
                    (event_id, created_at.isoformat()),
                )
            else:
                connection.execute("DELETE FROM watchlist WHERE event_id = ?", (event_id,))

    @staticmethod
    def _value_opportunity_values(item: ValueOpportunityRecord) -> tuple[object, ...]:
        return (
            item.id,
            item.provider_id,
            item.event_id,
            item.sportsbook_id,
            item.sportsbook,
            item.outcome_id,
            item.market_kind.value,
            item.selection.value,
            item.first_seen_at.isoformat(),
            item.last_seen_at.isoformat(),
            item.last_verified_at.isoformat(),
            item.last_price_change_at.isoformat(),
            item.last_updated_at.isoformat(),
            int(item.is_active),
            int(item.is_stale),
            item.status.value,
            str(item.recommended_price),
            str(item.current_price),
            str(item.ev_at_activation),
            str(item.current_ev),
            str(item.fair_probability),
            str(item.implied_probability),
            item.price_change_count_recent,
            item.api_snapshot_id,
            item.deactivated_at.isoformat() if item.deactivated_at else None,
        )

    @staticmethod
    def _row_to_value_opportunity(row: sqlite3.Row) -> ValueOpportunityRecord:
        return ValueOpportunityRecord(
            id=str(row["id"]),
            provider_id=str(row["provider_id"]),
            event_id=str(row["event_id"]),
            sportsbook_id=str(row["sportsbook_id"]),
            sportsbook=str(row["sportsbook"]),
            outcome_id=str(row["outcome_id"]),
            market_kind=MarketKind(str(row["market_kind"])),
            selection=OutcomeSide(str(row["selection"])),
            first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
            last_seen_at=datetime.fromisoformat(str(row["last_seen_at"])),
            last_verified_at=datetime.fromisoformat(str(row["last_verified_at"])),
            last_price_change_at=datetime.fromisoformat(str(row["last_price_change_at"])),
            last_updated_at=datetime.fromisoformat(str(row["last_updated_at"])),
            is_active=bool(row["is_active"]),
            is_stale=bool(row["is_stale"]),
            status=OpportunityStatus(str(row["status"])),
            recommended_price=Decimal(str(row["recommended_price"])),
            current_price=Decimal(str(row["current_price"])),
            ev_at_activation=Decimal(str(row["ev_at_activation"])),
            current_ev=Decimal(str(row["current_ev"])),
            fair_probability=Decimal(str(row["fair_probability"])),
            implied_probability=Decimal(str(row["implied_probability"])),
            price_change_count_recent=int(row["price_change_count_recent"]),
            api_snapshot_id=(
                str(row["api_snapshot_id"]) if row["api_snapshot_id"] is not None else None
            ),
            deactivated_at=(
                datetime.fromisoformat(str(row["deactivated_at"]))
                if row["deactivated_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _row_to_quote(row: sqlite3.Row) -> Quote:
        market = MarketKey(
            event_id=str(row["event_id"]),
            kind=MarketKind(str(row["kind"])),
            required_sides=tuple(OutcomeSide(item) for item in json.loads(row["required_sides"])),
            period=str(row["period"]),
            line=Decimal(str(row["line"])) if row["line"] is not None else None,
            subject_id=row["subject_id"],
            stat_key=row["stat_key"],
            variant=str(row["variant"]),
        )
        outcome = OutcomeKey(market=market, side=OutcomeSide(str(row["side"])))
        return Quote(
            provider_id=str(row["provider_id"]),
            sportsbook=Sportsbook(
                id=str(row["sportsbook_id"]),
                name=str(row["sportsbook_name"]),
            ),
            outcome=outcome,
            decimal_odds=Decimal(str(row["decimal_odds"])),
            source_updated_at=datetime.fromisoformat(str(row["source_updated_at"])),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            source_event_id=row["source_event_id"],
            source_url=row["source_url"],
        )
