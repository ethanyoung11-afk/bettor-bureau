from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from odds_scanner.domain import (
    Event,
    League,
    MarketKey,
    MarketKind,
    OutcomeKey,
    OutcomeSide,
    Participant,
    Sport,
    stable_id,
)


class NormalizationError(ValueError):
    """Raised when provider data cannot be mapped safely to a canonical contract."""


def canonical_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def decimal_value(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"Invalid decimal value: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    provider_key: str
    kind: MarketKind
    required_sides: tuple[OutcomeSide, ...]
    stat_key: str | None = None
    period: str = "full_game"
    variant: str = "standard"


FOOTBALL_MARKETS: Mapping[str, MarketDefinition] = {
    "h2h": MarketDefinition(
        provider_key="h2h",
        kind=MarketKind.MONEYLINE,
        required_sides=(OutcomeSide.HOME, OutcomeSide.AWAY),
    ),
    "spreads": MarketDefinition(
        provider_key="spreads",
        kind=MarketKind.SPREAD,
        required_sides=(OutcomeSide.HOME, OutcomeSide.AWAY),
    ),
    "totals": MarketDefinition(
        provider_key="totals",
        kind=MarketKind.TOTAL,
        required_sides=(OutcomeSide.OVER, OutcomeSide.UNDER),
    ),
}


class IdentityResolver:
    """Centralizes IDs so future providers can replace name aliases without engine changes."""

    def participant(self, league: League, display_name: str) -> Participant:
        token = canonical_token(display_name)
        if not token:
            raise NormalizationError("Participant name is empty")
        return Participant(id=stable_id("participant", league.id, token), name=display_name.strip())

    def event(
        self,
        league: League,
        start_time: datetime,
        home: Participant,
        away: Participant,
    ) -> Event:
        # Minute precision tolerates harmless provider timestamp formatting differences. A future
        # multi-provider resolver can add explicit event aliases without changing MarketKey.
        start_bucket = start_time.replace(second=0, microsecond=0).isoformat()
        event_id = stable_id("event", league.id, start_bucket, home.id, away.id)
        return Event(
            id=event_id,
            league_id=league.id,
            start_time=start_time,
            home=home,
            away=away,
            name=f"{away.name} at {home.name}",
        )

    def player(self, league: League, display_name: str) -> Participant:
        token = canonical_token(display_name)
        if not token:
            raise NormalizationError("Player name is empty")
        return Participant(id=stable_id("player", league.id, token), name=display_name.strip())


class MarketNormalizer:
    def normalize_outcome(
        self,
        event: Event,
        definition: MarketDefinition,
        outcome_name: str,
        point: object | None = None,
        *,
        subject_id: str | None = None,
    ) -> OutcomeKey:
        side = self._side(event, definition.kind, outcome_name)
        line = self._contract_line(definition.kind, side, point)
        market = MarketKey(
            event_id=event.id,
            kind=definition.kind,
            required_sides=definition.required_sides,
            period=definition.period,
            line=line,
            subject_id=subject_id,
            stat_key=definition.stat_key,
            variant=definition.variant,
        )
        return OutcomeKey(market=market, side=side)

    @staticmethod
    def _side(event: Event, kind: MarketKind, outcome_name: str) -> OutcomeSide:
        token = canonical_token(outcome_name)
        if kind in {MarketKind.MONEYLINE, MarketKind.SPREAD}:
            if token == canonical_token(event.home.name):
                return OutcomeSide.HOME
            if token == canonical_token(event.away.name):
                return OutcomeSide.AWAY
            if token in {"draw", "tie"}:
                return OutcomeSide.DRAW
        elif kind in {MarketKind.TOTAL, MarketKind.PLAYER_PROP}:
            if token == "over":
                return OutcomeSide.OVER
            if token == "under":
                return OutcomeSide.UNDER
            if token == "yes":
                return OutcomeSide.YES
            if token == "no":
                return OutcomeSide.NO
        raise NormalizationError(f"Cannot map outcome {outcome_name!r} for {kind.value}")

    @staticmethod
    def _contract_line(
        kind: MarketKind,
        side: OutcomeSide,
        point: object | None,
    ) -> Decimal | None:
        if kind is MarketKind.MONEYLINE:
            return None
        if point is None:
            raise NormalizationError(f"{kind.value} outcome is missing a line")
        line = decimal_value(point)
        if kind is MarketKind.SPREAD:
            # Canonical spreads are always represented as the home handicap. This makes home -3
            # and away +3 identical while keeping home -3 and away +3.5 safely separated.
            if side is OutcomeSide.AWAY:
                return -line
            if side is not OutcomeSide.HOME:
                raise NormalizationError("Spread outcomes must identify home or away")
        return line


def make_sport_and_league(
    sport_id: str,
    sport_name: str,
    league_id: str,
    league_name: str,
) -> tuple[Sport, League]:
    sport = Sport(id=sport_id, name=sport_name)
    return sport, League(id=league_id, sport_id=sport.id, name=league_name)
