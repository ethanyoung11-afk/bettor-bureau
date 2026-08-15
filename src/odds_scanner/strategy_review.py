from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from odds_scanner.domain import BetStatus, TrackedBet
from odds_scanner.live_refresh import repository_from_environment
from odds_scanner.strategy import (
    OFFICIAL_STARTING_BANKROLL_UNITS,
    OFFICIAL_STRATEGY_KEY,
    OFFICIAL_UNIT_VALUE_DOLLARS,
    official_bets,
)


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    settled: int
    wins: int
    losses: int
    voids: int
    pending: int
    units: Decimal
    roi: Decimal
    maximum_drawdown: Decimal
    average_recorded_ev: Decimal | None


def _metadata(bet: TrackedBet) -> dict[str, object]:
    if "|" not in bet.notes:
        return {}
    _, raw = bet.notes.split("|", 1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def summarize_strategy(bets: tuple[TrackedBet, ...]) -> ReviewSummary:
    official = official_bets(bets)
    wins = sum(bet.status is BetStatus.WON for bet in official)
    losses = sum(bet.status is BetStatus.LOST for bet in official)
    voids = sum(bet.status is BetStatus.VOID for bet in official)
    pending = sum(bet.status is BetStatus.PENDING for bet in official)
    settled = wins + losses
    units = sum((bet.profit_loss or Decimal("0") for bet in official), Decimal("0"))
    settled_stake = sum(
        (
            bet.stake
            for bet in official
            if bet.status in {BetStatus.WON, BetStatus.LOST}
        ),
        Decimal("0"),
    )
    roi = units / settled_stake if settled_stake else Decimal("0")

    running = Decimal("0")
    peak = Decimal("0")
    maximum_drawdown = Decimal("0")
    for bet in sorted(official, key=lambda item: item.created_at):
        if bet.profit_loss is None:
            continue
        running += bet.profit_loss
        peak = max(peak, running)
        maximum_drawdown = max(maximum_drawdown, peak - running)

    recorded_evs = []
    for bet in official:
        raw_ev = _metadata(bet).get("expected_value")
        if raw_ev is None:
            continue
        try:
            recorded_evs.append(Decimal(str(raw_ev)))
        except ArithmeticError:
            continue
    average_ev = (
        sum(recorded_evs, Decimal("0")) / Decimal(len(recorded_evs))
        if recorded_evs
        else None
    )
    return ReviewSummary(
        settled=settled,
        wins=wins,
        losses=losses,
        voids=voids,
        pending=pending,
        units=units,
        roi=roi,
        maximum_drawdown=maximum_drawdown,
        average_recorded_ev=average_ev,
    )


def _segment_lines(bets: tuple[TrackedBet, ...]) -> list[str]:
    segments: dict[tuple[str, str], list[TrackedBet]] = defaultdict(list)
    for bet in official_bets(bets):
        if bet.status not in {BetStatus.WON, BetStatus.LOST}:
            continue
        segments[(bet.market_label, bet.sportsbook)].append(bet)
    lines = []
    for (market, sportsbook), items in sorted(segments.items()):
        units = sum((item.profit_loss or Decimal("0") for item in items), Decimal("0"))
        risked = sum((item.stake for item in items), Decimal("0"))
        roi = units / risked if risked else Decimal("0")
        lines.append(
            f"- {market} at {sportsbook}: {len(items)} settled, {units:+.2f}u, {roi:+.1%} ROI"
        )
    return lines or ["- No settled segment data yet."]


def build_weekly_report(
    bets: tuple[TrackedBet, ...],
    *,
    as_of: datetime,
) -> str:
    summary = summarize_strategy(bets)
    bankroll_units = OFFICIAL_STARTING_BANKROLL_UNITS + summary.units
    bankroll_dollars = bankroll_units * OFFICIAL_UNIT_VALUE_DOLLARS
    average_ev = (
        f"{summary.average_recorded_ev:.2%}"
        if summary.average_recorded_ev is not None
        else "not yet recorded"
    )
    decision = (
        "Keep the production strategy unchanged; fewer than 30 settled bets is not enough "
        "evidence for a parameter change."
        if summary.settled < 30
        else "Evaluate challenger strategies out of sample; do not promote one using W/L alone."
    )
    return "\n".join(
        [
            f"# Weekly strategy review — {as_of.astimezone().date().isoformat()}",
            "",
            f"Strategy: `{OFFICIAL_STRATEGY_KEY}`",
            (
                f"Record: {summary.wins}-{summary.losses} "
                f"({summary.voids} void, {summary.pending} pending)"
            ),
            f"Profit/loss: {summary.units:+.2f}u",
            f"ROI: {summary.roi:+.1%}",
            f"Paper bankroll: ${bankroll_dollars:,.0f}",
            f"Maximum drawdown: {summary.maximum_drawdown:.2f}u",
            f"Average recorded EV: {average_ev}",
            "",
            "## Performance by market and sportsbook",
            *_segment_lines(bets),
            "",
            "## Weekly decision rule",
            decision,
            "",
            "## Challenger strategies to evaluate",
            "- Conservative: half the maximum stake and require at least five reference books.",
            "- Pure growth: rank only by expected logarithmic bankroll growth.",
            "- Sharp consensus: weight historically sharper reference books more heavily.",
            "",
            "## Data-quality checks",
            "- Confirm every pending event has a settlement path.",
            "- Compare the recorded price with the closing price when closing odds are available.",
            "- Treat this as a forward test until historical game results support a true backtest.",
        ]
    )


def main() -> int:
    repository = repository_from_environment()
    print(build_weekly_report(repository.list_bets(), as_of=datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
