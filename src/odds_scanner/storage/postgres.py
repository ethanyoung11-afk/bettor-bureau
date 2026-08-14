from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Cursor, connect
from psycopg.rows import dict_row

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

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS sports (
        id TEXT PRIMARY KEY, name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS leagues (
        id TEXT PRIMARY KEY, sport_id TEXT NOT NULL REFERENCES sports(id), name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS participants (
        id TEXT PRIMARY KEY, name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        league_id TEXT NOT NULL REFERENCES leagues(id),
        start_time TEXT NOT NULL,
        home_id TEXT NOT NULL REFERENCES participants(id),
        away_id TEXT NOT NULL REFERENCES participants(id),
        name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS markets (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id),
        kind TEXT NOT NULL,
        required_sides TEXT NOT NULL,
        period TEXT NOT NULL,
        line TEXT,
        subject_id TEXT,
        stat_key TEXT,
        variant TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        id TEXT PRIMARY KEY, market_id TEXT NOT NULL REFERENCES markets(id), side TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sportsbooks (
        id TEXT PRIMARY KEY, name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS quotes (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL,
        sportsbook_id TEXT NOT NULL REFERENCES sportsbooks(id),
        outcome_id TEXT NOT NULL REFERENCES outcomes(id),
        decimal_odds TEXT NOT NULL,
        source_updated_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        source_event_id TEXT,
        UNIQUE(provider_id, sportsbook_id, outcome_id, source_updated_at, observed_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quotes_source_updated ON quotes(source_updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_outcome ON quotes(outcome_id)",
    """CREATE TABLE IF NOT EXISTS latest_quote_state (
        provider_id TEXT NOT NULL,
        sportsbook_id TEXT NOT NULL,
        outcome_id TEXT NOT NULL,
        quote_id BIGINT NOT NULL REFERENCES quotes(id),
        PRIMARY KEY(provider_id, sportsbook_id, outcome_id)
    )""",
    """CREATE TABLE IF NOT EXISTS user_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tracked_bets (
        id BIGSERIAL PRIMARY KEY,
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
    )""",
    """CREATE TABLE IF NOT EXISTS watchlist (
        event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
    )""",
)


class PostgresQuoteRepository:
    """Shared persistent quote storage for the hosted application."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        with self._connection() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)

    @contextmanager
    def _connection(self) -> Iterator[Cursor[dict[str, Any]]]:
        with (
            connect(self.database_url, row_factory=dict_row) as connection,
            connection.cursor() as cursor,
        ):
            yield cursor

    def save_snapshot(self, snapshot: OddsSnapshot) -> None:
        with self._connection() as cursor:
            cursor.executemany(
                "INSERT INTO sports(id, name) VALUES (%s, %s) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                ((sport.id, sport.name) for sport in snapshot.sports),
            )
            cursor.executemany(
                "INSERT INTO leagues(id, sport_id, name) VALUES (%s, %s, %s) "
                "ON CONFLICT(id) DO UPDATE SET sport_id=excluded.sport_id, name=excluded.name",
                ((league.id, league.sport_id, league.name) for league in snapshot.leagues),
            )
            participants = {
                participant.id: participant
                for event in snapshot.events
                for participant in (event.home, event.away)
            }
            cursor.executemany(
                "INSERT INTO participants(id, name) VALUES (%s, %s) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                ((item.id, item.name) for item in participants.values()),
            )
            cursor.executemany(
                "INSERT INTO events(id, league_id, start_time, home_id, away_id, name) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET "
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
            cursor.execute(
                "DELETE FROM latest_quote_state WHERE provider_id = %s",
                (snapshot.provider_id,),
            )
            for quote in snapshot.quotes:
                self._save_quote(cursor, quote)

    @staticmethod
    def _save_quote(cursor: Cursor[dict[str, Any]], quote: Quote) -> None:
        market = quote.outcome.market
        cursor.execute(
            "INSERT INTO markets(id, event_id, kind, required_sides, period, line, subject_id, "
            "stat_key, variant) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(id) DO NOTHING",
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
        cursor.execute(
            "INSERT INTO outcomes(id, market_id, side) VALUES (%s, %s, %s) "
            "ON CONFLICT(id) DO NOTHING",
            (quote.outcome.id, market.id, quote.outcome.side.value),
        )
        cursor.execute(
            "INSERT INTO sportsbooks(id, name) VALUES (%s, %s) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (quote.sportsbook.id, quote.sportsbook.name),
        )
        cursor.execute(
            "INSERT INTO quotes(provider_id, sportsbook_id, outcome_id, decimal_odds, "
            "source_updated_at, observed_at, source_event_id) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(provider_id, sportsbook_id, outcome_id, source_updated_at, observed_at) "
            "DO NOTHING",
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
        cursor.execute(
            "SELECT id FROM quotes WHERE provider_id = %s AND sportsbook_id = %s "
            "AND outcome_id = %s AND source_updated_at = %s AND observed_at = %s",
            (
                quote.provider_id,
                quote.sportsbook.id,
                quote.outcome.id,
                quote.source_updated_at.isoformat(),
                quote.observed_at.isoformat(),
            ),
        )
        quote_row = cursor.fetchone()
        if quote_row is None:
            raise RuntimeError("Failed to persist quote state")
        cursor.execute(
            "INSERT INTO latest_quote_state(provider_id, sportsbook_id, outcome_id, quote_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT(provider_id, sportsbook_id, outcome_id) "
            "DO UPDATE SET quote_id=excluded.quote_id",
            (quote.provider_id, quote.sportsbook.id, quote.outcome.id, int(quote_row["id"])),
        )

    def load_quotes_since(self, since: datetime) -> tuple[Quote, ...]:
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone-aware")
        with self._connection() as cursor:
            cursor.execute(
                self._quote_select() + " WHERE q.source_updated_at >= %s",
                (since.isoformat(),),
            )
            rows = cursor.fetchall()
        return tuple(self._row_to_quote(row) for row in rows)

    def load_latest_quotes(self, provider_id: str) -> tuple[Quote, ...]:
        query = self._quote_select(
            "FROM latest_quote_state current JOIN quotes q ON q.id = current.quote_id"
        ) + " WHERE current.provider_id = %s"
        with self._connection() as cursor:
            cursor.execute(query, (provider_id,))
            rows = cursor.fetchall()
        return tuple(self._row_to_quote(row) for row in rows)

    @staticmethod
    def _quote_select(from_clause: str = "FROM quotes q") -> str:
        return f"""
            SELECT q.provider_id, q.decimal_odds, q.source_updated_at, q.observed_at,
                   q.source_event_id, b.id AS sportsbook_id, b.name AS sportsbook_name,
                   o.side, m.event_id, m.kind, m.required_sides, m.period, m.line,
                   m.subject_id, m.stat_key, m.variant
              {from_clause}
              JOIN sportsbooks b ON b.id = q.sportsbook_id
              JOIN outcomes o ON o.id = q.outcome_id
              JOIN markets m ON m.id = o.market_id
        """

    def load_events(self) -> tuple[Event, ...]:
        with self._connection() as cursor:
            cursor.execute(
                """SELECT e.id, e.league_id, e.start_time, e.name,
                          hp.id AS home_id, hp.name AS home_name,
                          ap.id AS away_id, ap.name AS away_name
                     FROM events e
                     JOIN participants hp ON hp.id = e.home_id
                     JOIN participants ap ON ap.id = e.away_id
                    ORDER BY e.start_time"""
            )
            rows = cursor.fetchall()
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
        with self._connection() as cursor:
            cursor.execute(
                "INSERT INTO user_settings(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def load_settings(self) -> dict[str, str]:
        with self._connection() as cursor:
            cursor.execute("SELECT key, value FROM user_settings")
            rows = cursor.fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def add_bet(self, bet: TrackedBet) -> int:
        with self._connection() as cursor:
            cursor.execute(
                "INSERT INTO tracked_bets(created_at, event_id, event_name, market_label, "
                "selection, sportsbook, decimal_odds, stake, status, profit_loss, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
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
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Failed to create tracked bet")
            return int(row["id"])

    def list_bets(self) -> tuple[TrackedBet, ...]:
        with self._connection() as cursor:
            cursor.execute("SELECT * FROM tracked_bets ORDER BY created_at DESC")
            rows = cursor.fetchall()
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
        with self._connection() as cursor:
            cursor.execute(
                "UPDATE tracked_bets SET status = %s, profit_loss = %s WHERE id = %s",
                (status.value, str(profit_loss) if profit_loss is not None else None, bet_id),
            )

    def watched_event_ids(self) -> frozenset[str]:
        with self._connection() as cursor:
            cursor.execute("SELECT event_id FROM watchlist")
            rows = cursor.fetchall()
        return frozenset(str(row["event_id"]) for row in rows)

    def set_event_watched(self, event_id: str, watched: bool, created_at: datetime) -> None:
        with self._connection() as cursor:
            if watched:
                cursor.execute(
                    "INSERT INTO watchlist(event_id, created_at) VALUES (%s, %s) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    (event_id, created_at.isoformat()),
                )
            else:
                cursor.execute("DELETE FROM watchlist WHERE event_id = %s", (event_id,))

    @staticmethod
    def _row_to_quote(row: dict[str, Any]) -> Quote:
        market = MarketKey(
            event_id=str(row["event_id"]),
            kind=MarketKind(str(row["kind"])),
            required_sides=tuple(
                OutcomeSide(item) for item in json.loads(str(row["required_sides"]))
            ),
            period=str(row["period"]),
            line=Decimal(str(row["line"])) if row["line"] is not None else None,
            subject_id=row["subject_id"],
            stat_key=row["stat_key"],
            variant=str(row["variant"]),
        )
        return Quote(
            provider_id=str(row["provider_id"]),
            sportsbook=Sportsbook(
                id=str(row["sportsbook_id"]),
                name=str(row["sportsbook_name"]),
            ),
            outcome=OutcomeKey(market=market, side=OutcomeSide(str(row["side"]))),
            decimal_odds=Decimal(str(row["decimal_odds"])),
            source_updated_at=datetime.fromisoformat(str(row["source_updated_at"])),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            source_event_id=(
                str(row["source_event_id"]) if row["source_event_id"] is not None else None
            ),
        )
