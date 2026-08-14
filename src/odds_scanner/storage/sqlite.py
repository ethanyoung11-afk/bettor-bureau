from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock

from odds_scanner.domain import (
    BetStatus,
    Event,
    MarketKey,
    MarketKind,
    OddsSnapshot,
    OutcomeKey,
    OutcomeSide,
    Participant,
    Quote,
    Sportsbook,
    TrackedBet,
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
"""


class SQLiteQuoteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

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

    def save_snapshot(self, snapshot: OddsSnapshot) -> None:
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
            connection.execute(
                "DELETE FROM latest_quote_state WHERE provider_id = ?",
                (snapshot.provider_id,),
            )
            for quote in snapshot.quotes:
                self._save_quote(connection, quote)

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
            "source_updated_at, observed_at, source_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                quote.provider_id,
                quote.sportsbook.id,
                quote.outcome.id,
                str(quote.decimal_odds),
                quote.source_updated_at.isoformat(),
                quote.observed_at.isoformat(),
                quote.source_event_id,
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
                   q.source_event_id, b.id AS sportsbook_id, b.name AS sportsbook_name,
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
                   q.source_event_id, b.id AS sportsbook_id, b.name AS sportsbook_name,
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
                           q.source_event_id, b.id AS sportsbook_id, b.name AS sportsbook_name,
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

    def load_events(self) -> tuple[Event, ...]:
        query = """
            SELECT e.id, e.league_id, e.start_time, e.name,
                   hp.id AS home_id, hp.name AS home_name,
                   ap.id AS away_id, ap.name AS away_name
            FROM events e
            JOIN participants hp ON hp.id = e.home_id
            JOIN participants ap ON ap.id = e.away_id
            ORDER BY e.start_time
        """
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
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
        )
