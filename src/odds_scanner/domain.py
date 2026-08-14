from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _stable_part(part: object) -> str | None:
    if part is None:
        return None
    if isinstance(part, Decimal):
        if part == 0:
            return "0"
        return format(part.normalize(), "f")
    return str(part)


def stable_id(namespace: str, *parts: object) -> str:
    """Return a compact, deterministic identifier without relying on display formatting."""
    encoded = json.dumps(
        [namespace, *(_stable_part(part) for part in parts)],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def ensure_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class MarketKind(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    PLAYER_PROP = "player_prop"


class OutcomeSide(StrEnum):
    HOME = "home"
    AWAY = "away"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"


class BetStatus(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    VOID = "void"


SIDE_ORDER = {
    OutcomeSide.HOME: 0,
    OutcomeSide.DRAW: 1,
    OutcomeSide.AWAY: 2,
    OutcomeSide.OVER: 3,
    OutcomeSide.UNDER: 4,
    OutcomeSide.YES: 5,
    OutcomeSide.NO: 6,
}


@dataclass(frozen=True, slots=True)
class Sport:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class League:
    id: str
    sport_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Participant:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    league_id: str
    start_time: datetime
    home: Participant
    away: Participant
    name: str

    def __post_init__(self) -> None:
        ensure_aware(self.start_time, "start_time")


@dataclass(frozen=True, slots=True)
class MarketKey:
    """Identity of one economic contract, independent of any sportsbook.

    ``line`` is the exact total, the home-referenced spread, or a prop line. ``subject_id`` and
    ``stat_key`` distinguish player props. They are deliberately identifiers, not display names.
    """

    event_id: str
    kind: MarketKind
    required_sides: tuple[OutcomeSide, ...]
    period: str = "full_game"
    line: Decimal | None = None
    subject_id: str | None = None
    stat_key: str | None = None
    variant: str = "standard"

    def __post_init__(self) -> None:
        if len(self.required_sides) not in (2, 3):
            raise ValueError("A supported market must have two or three outcomes")
        if len(set(self.required_sides)) != len(self.required_sides):
            raise ValueError("required_sides must be unique")
        # Provider adapters may list outcomes in different orders. Canonical ordering keeps the
        # same economic contract equal (and gives it the same persistent ID) across providers.
        object.__setattr__(
            self,
            "required_sides",
            tuple(sorted(self.required_sides, key=SIDE_ORDER.__getitem__)),
        )
        if (
            self.kind in {MarketKind.SPREAD, MarketKind.TOTAL, MarketKind.PLAYER_PROP}
            and self.line is None
        ):
            raise ValueError(f"{self.kind.value} markets require an exact line")
        if self.kind is MarketKind.PLAYER_PROP and (not self.subject_id or not self.stat_key):
            raise ValueError("Player props require subject_id and stat_key")

    @property
    def id(self) -> str:
        return stable_id(
            "market",
            self.event_id,
            self.kind.value,
            self.period,
            self.line,
            self.subject_id,
            self.stat_key,
            self.variant,
            ",".join(side.value for side in self.required_sides),
        )


@dataclass(frozen=True, slots=True)
class OutcomeKey:
    market: MarketKey
    side: OutcomeSide

    def __post_init__(self) -> None:
        if self.side not in self.market.required_sides:
            raise ValueError(f"{self.side.value} is not valid for this market")

    @property
    def id(self) -> str:
        return stable_id("outcome", self.market.id, self.side.value)


@dataclass(frozen=True, slots=True)
class Sportsbook:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Quote:
    provider_id: str
    sportsbook: Sportsbook
    outcome: OutcomeKey
    decimal_odds: Decimal
    source_updated_at: datetime
    observed_at: datetime
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if self.decimal_odds <= Decimal("1"):
            raise ValueError("decimal_odds must be greater than 1")
        ensure_aware(self.source_updated_at, "source_updated_at")
        ensure_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    provider_id: str
    sports: tuple[Sport, ...]
    leagues: tuple[League, ...]
    events: tuple[Event, ...]
    quotes: tuple[Quote, ...]
    fetched_at: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.fetched_at, "fetched_at")


@dataclass(frozen=True, slots=True)
class ArbitrageLeg:
    outcome: OutcomeKey
    quote: Quote
    stake: Decimal
    gross_payout: Decimal


@dataclass(frozen=True, slots=True)
class ArbitrageOpportunity:
    market: MarketKey
    legs: tuple[ArbitrageLeg, ...]
    total_implied_probability: Decimal
    roi: Decimal
    bankroll: Decimal
    detected_at: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.detected_at, "detected_at")

    @property
    def guaranteed_profit(self) -> Decimal:
        return self.bankroll * self.roi


@dataclass(frozen=True, slots=True)
class TrackedBet:
    id: int | None
    created_at: datetime
    event_id: str
    event_name: str
    market_label: str
    selection: str
    sportsbook: str
    decimal_odds: Decimal
    stake: Decimal
    status: BetStatus = BetStatus.PENDING
    profit_loss: Decimal | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, "created_at")
        if self.decimal_odds <= Decimal("1"):
            raise ValueError("decimal_odds must be greater than 1")
        if self.stake <= 0:
            raise ValueError("stake must be positive")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_jsonable(value: Any) -> Any:
    """Small helper for diagnostics and future provider raw-payload logging."""
    if isinstance(value, (Decimal, datetime, StrEnum)):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")
