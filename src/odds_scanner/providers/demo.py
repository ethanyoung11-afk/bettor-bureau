from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from odds_scanner.domain import (
    Event,
    League,
    MarketKey,
    MarketKind,
    OddsSnapshot,
    OutcomeKey,
    OutcomeSide,
    Participant,
    Quote,
    Sport,
    Sportsbook,
)

SPORT = Sport(id="american-football", name="American Football")
LEAGUES = (
    League(id="nfl", sport_id=SPORT.id, name="NFL"),
    League(id="ncaaf", sport_id=SPORT.id, name="NCAAF"),
    League(id="cfl", sport_id=SPORT.id, name="CFL"),
)
BOOKS = (
    Sportsbook(id="draftkings", name="DraftKings"),
    Sportsbook(id="fanduel", name="FanDuel"),
    Sportsbook(id="betmgm", name="BetMGM"),
    Sportsbook(id="caesars", name="Caesars"),
    Sportsbook(id="pinnacle", name="Pinnacle"),
    Sportsbook(id="playnow", name="PlayNow"),
    Sportsbook(id="betway", name="Betway"),
)


def _event(
    event_id: str,
    league_id: str,
    away_name: str,
    home_name: str,
    start_time: datetime,
) -> Event:
    return Event(
        id=event_id,
        league_id=league_id,
        start_time=start_time,
        away=Participant(id=f"{league_id}-{away_name.lower().replace(' ', '-')}", name=away_name),
        home=Participant(id=f"{league_id}-{home_name.lower().replace(' ', '-')}", name=home_name),
        name=f"{away_name} at {home_name}",
    )


def demo_events(as_of: datetime) -> tuple[Event, ...]:
    anchor = as_of.astimezone(UTC).replace(second=0, microsecond=0)
    return (
        _event(
            "demo-nfl-1",
            "nfl",
            "Kansas City Chiefs",
            "Buffalo Bills",
            anchor + timedelta(hours=4),
        ),
        _event(
            "demo-nfl-2",
            "nfl",
            "Green Bay Packers",
            "Chicago Bears",
            anchor + timedelta(hours=28),
        ),
        _event(
            "demo-ncaaf-1",
            "ncaaf",
            "Georgia Bulldogs",
            "Alabama Crimson Tide",
            anchor + timedelta(hours=8),
        ),
        _event(
            "demo-ncaaf-2",
            "ncaaf",
            "Oregon Ducks",
            "Washington Huskies",
            anchor + timedelta(hours=32),
        ),
        _event("demo-cfl-1", "cfl", "Calgary Stampeders", "BC Lions", anchor + timedelta(hours=6)),
        _event(
            "demo-cfl-2",
            "cfl",
            "Toronto Argonauts",
            "Montreal Alouettes",
            anchor + timedelta(hours=30),
        ),
    )


def _quote(
    event: Event,
    book: Sportsbook,
    kind: MarketKind,
    side: OutcomeSide,
    price: str,
    observed_at: datetime,
    *,
    line: str | None = None,
    source_age: timedelta = timedelta(seconds=20),
) -> Quote:
    required_sides = (
        (OutcomeSide.OVER, OutcomeSide.UNDER)
        if kind is MarketKind.TOTAL
        else (OutcomeSide.HOME, OutcomeSide.AWAY)
    )
    market = MarketKey(
        event_id=event.id,
        kind=kind,
        required_sides=required_sides,
        line=Decimal(line) if line is not None else None,
    )
    return Quote(
        provider_id="demo",
        sportsbook=book,
        outcome=OutcomeKey(market=market, side=side),
        decimal_odds=Decimal(price),
        source_updated_at=observed_at - source_age,
        observed_at=observed_at,
        source_event_id=event.id,
    )


def _book_prices(event_index: int, book_index: int, step: int) -> tuple[str, str]:
    home_base = Decimal("1.78") + Decimal(event_index) * Decimal("0.08")
    movement = Decimal(step - 8) * Decimal("0.006")
    book_shift = Decimal(book_index - 2) * Decimal("0.018")
    home = max(Decimal("1.55"), home_base + movement + book_shift)
    away = max(Decimal("1.55"), Decimal("3.75") - home + Decimal("0.10"))
    return f"{home:.3f}", f"{away:.3f}"


def _quotes_for_event(
    event: Event,
    event_index: int,
    observed_at: datetime,
    step: int,
) -> list[Quote]:
    quotes: list[Quote] = []
    for book_index, book in enumerate(BOOKS):
        home_price, away_price = _book_prices(event_index, book_index, step)
        if event.id == "demo-nfl-1" and step == 11:
            if book.id == "draftkings":
                home_price, away_price = "2.100", "1.820"
            elif book.id == "fanduel":
                home_price, away_price = "1.850", "2.100"
        if event.id == "demo-cfl-1" and book.id == "caesars":
            away_price = "2.180"

        source_age = (
            timedelta(minutes=12)
            if book.id == "pinnacle" and step == 11
            else timedelta(seconds=15 + book_index * 8)
        )
        quotes.extend(
            (
                _quote(
                    event,
                    book,
                    MarketKind.MONEYLINE,
                    OutcomeSide.HOME,
                    home_price,
                    observed_at,
                    source_age=source_age,
                ),
                _quote(
                    event,
                    book,
                    MarketKind.MONEYLINE,
                    OutcomeSide.AWAY,
                    away_price,
                    observed_at,
                    source_age=source_age,
                ),
            )
        )

        spread_base = Decimal("-3.0") + Decimal(event_index % 3)
        spread_move = Decimal("0.5") if book.id in {"fanduel", "betmgm"} else Decimal("0")
        if book.id == "caesars":
            spread_move = Decimal("-0.5")
        spread_line = spread_base + spread_move
        quotes.extend(
            (
                _quote(
                    event,
                    book,
                    MarketKind.SPREAD,
                    OutcomeSide.HOME,
                    "1.91" if book.id != "caesars" else "2.04",
                    observed_at,
                    line=str(spread_line),
                    source_age=source_age,
                ),
                _quote(
                    event,
                    book,
                    MarketKind.SPREAD,
                    OutcomeSide.AWAY,
                    "1.91" if book.id != "fanduel" else "1.97",
                    observed_at,
                    line=str(spread_line),
                    source_age=source_age,
                ),
            )
        )

        total_base = Decimal("47.5") + Decimal(event_index % 4)
        total_move = Decimal("1.0") if book.id in {"fanduel", "caesars"} else Decimal("0")
        if step < 5:
            total_move -= Decimal("0.5")
        total_line = total_base + total_move
        quotes.extend(
            (
                _quote(
                    event,
                    book,
                    MarketKind.TOTAL,
                    OutcomeSide.OVER,
                    "1.91" if book.id != "fanduel" else "1.84",
                    observed_at,
                    line=str(total_line),
                    source_age=source_age,
                ),
                _quote(
                    event,
                    book,
                    MarketKind.TOTAL,
                    OutcomeSide.UNDER,
                    "1.91" if book.id != "caesars" else "2.02",
                    observed_at,
                    line=str(total_line),
                    source_age=source_age,
                ),
            )
        )
    return quotes


def generate_demo_snapshots(
    as_of: datetime | None = None,
    points: int = 12,
) -> tuple[OddsSnapshot, ...]:
    effective = (as_of or datetime.now(UTC)).astimezone(UTC)
    anchor = effective.replace(second=0, microsecond=0)
    events = demo_events(anchor)
    snapshots: list[OddsSnapshot] = []
    for step in range(points):
        observed_at = anchor - timedelta(minutes=5 * (points - 1 - step))
        quotes = tuple(
            quote
            for event_index, event in enumerate(events)
            for quote in _quotes_for_event(event, event_index, observed_at, step)
        )
        snapshots.append(
            OddsSnapshot(
                provider_id="demo",
                sports=(SPORT,),
                leagues=LEAGUES,
                events=events,
                quotes=quotes,
                fetched_at=observed_at,
            )
        )
    return tuple(snapshots)


class DemoOddsProvider:
    @property
    def provider_id(self) -> str:
        return "demo"

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        snapshot = generate_demo_snapshots(points=1)[0]
        leagues = {key.removeprefix("americanfootball_") for key in league_keys}
        kinds = {
            "h2h": MarketKind.MONEYLINE,
            "spreads": MarketKind.SPREAD,
            "totals": MarketKind.TOTAL,
        }
        selected_kinds = {kinds[key] for key in market_keys if key in kinds}
        event_ids = {event.id for event in snapshot.events if event.league_id in leagues}
        return OddsSnapshot(
            provider_id=self.provider_id,
            sports=snapshot.sports,
            leagues=tuple(league for league in snapshot.leagues if league.id in leagues),
            events=tuple(event for event in snapshot.events if event.id in event_ids),
            quotes=tuple(
                quote
                for quote in snapshot.quotes
                if quote.outcome.market.event_id in event_ids
                and quote.outcome.market.kind in selected_kinds
            ),
            fetched_at=snapshot.fetched_at,
        )
