from datetime import timedelta
from decimal import Decimal

from odds_scanner.domain import BetStatus, TrackedBet
from odds_scanner.strategy import OFFICIAL_RECOMMENDATION_PREFIX
from odds_scanner.strategy_review import build_weekly_report, summarize_strategy


def _bet(now, bet_id: int, status: BetStatus, profit_loss: str | None) -> TrackedBet:
    return TrackedBet(
        id=bet_id,
        created_at=now + timedelta(hours=bet_id),
        event_id=f"event-{bet_id}",
        event_name=f"Event {bet_id}",
        market_label="Moneyline",
        selection="Home",
        sportsbook="PlayNow",
        decimal_odds=Decimal("2.00"),
        stake=Decimal("1"),
        status=status,
        profit_loss=Decimal(profit_loss) if profit_loss is not None else None,
        notes=(
            f'{OFFICIAL_RECOMMENDATION_PREFIX}{bet_id}|'
            '{"expected_value":"0.04","strategy":"balanced-quarter-kelly-v1"}'
        ),
    )


def test_weekly_review_reports_record_roi_and_drawdown(now):
    bets = (
        _bet(now, 1, BetStatus.WON, "1"),
        _bet(now, 2, BetStatus.LOST, "-1"),
        _bet(now, 3, BetStatus.LOST, "-1"),
        _bet(now, 4, BetStatus.PENDING, None),
    )

    summary = summarize_strategy(bets)
    report = build_weekly_report(bets, as_of=now)

    assert summary.wins == 1
    assert summary.losses == 2
    assert summary.pending == 1
    assert summary.units == Decimal("-1")
    assert summary.roi == Decimal("-1") / Decimal("3")
    assert summary.maximum_drawdown == Decimal("2")
    assert "Record: 1-2" in report
    assert "Paper bankroll: $9,900" in report
    assert "fewer than 30 settled bets" in report
