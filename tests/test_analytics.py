from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from odds_scanner.analytics import (
    detect_consensus_value,
    detect_middles,
    plan_refreshes,
    rank_recommendations,
)
from odds_scanner.domain import MarketKind
from odds_scanner.opportunities import deduplicate_quotes, detect_arbitrage
from odds_scanner.providers.demo import generate_demo_snapshots


def test_demo_feed_exercises_complete_product(now):
    snapshots = generate_demo_snapshots(now)
    assert len(snapshots) == 12
    assert len(snapshots[-1].events) == 6
    assert len(snapshots[-1].quotes) == 336
    assert any(
        quote.outcome.market.kind is MarketKind.PLAYER_PROP
        for quote in snapshots[-1].quotes
    )
    assert {quote.sportsbook.id for quote in snapshots[-1].quotes} == {
        "draftkings",
        "fanduel",
        "betmgm",
        "caesars",
        "pinnacle",
        "playnow",
        "betway",
    }


def test_demo_feed_contains_arbitrage_but_rejects_stale_book(now):
    snapshots = generate_demo_snapshots(now)
    current = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    opportunities = detect_arbitrage(
        current,
        as_of=now,
        max_age=timedelta(minutes=5),
    )
    assert opportunities
    assert any(item.market.event_id == "demo-nfl-1" for item in opportunities)
    assert all(
        leg.quote.sportsbook.id != "pinnacle"
        for opportunity in opportunities
        for leg in opportunity.legs
    )


def test_middle_detection_finds_spreads_and_totals(now):
    current = deduplicate_quotes(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    middles = detect_middles(current, as_of=now, max_age=timedelta(minutes=5))
    assert any(item.kind is MarketKind.SPREAD and item.width == Decimal("1") for item in middles)
    assert any(item.kind is MarketKind.TOTAL and item.width == Decimal("1") for item in middles)
    assert all(item.first.sportsbook.id != item.second.sportsbook.id for item in middles)


def test_consensus_value_uses_multiple_reference_books(now):
    current = deduplicate_quotes(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    values = detect_consensus_value(
        current,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0.005"),
    )
    assert values
    assert all(item.expected_value > 0 for item in values)
    assert all(item.reference_books >= 2 for item in values)
    assert all(len(item.reference_sportsbooks) == item.reference_books for item in values)


def test_consensus_value_can_target_playnow_and_betway_only(now):
    current = deduplicate_quotes(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    values = detect_consensus_value(
        current,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=("PlayNow", "Betway"),
    )

    assert values
    assert {item.quote.sportsbook.name for item in values} <= {"PlayNow", "Betway"}
    assert all(item.quote.sportsbook.name not in item.reference_sportsbooks for item in values)
    assert all(item.reference_books >= 2 for item in values)


def test_consensus_value_can_include_stale_quotes_when_requested(now):
    stale_quotes = tuple(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    later = now + timedelta(hours=1)

    assert not detect_consensus_value(
        stale_quotes,
        as_of=later,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0.005"),
    )
    assert detect_consensus_value(
        stale_quotes,
        as_of=later,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0.005"),
        include_stale=True,
    )


def test_recommendations_rank_arbitrage_before_probabilistic_signals(now):
    current = deduplicate_quotes(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    arbs = detect_arbitrage(current, as_of=now, max_age=timedelta(minutes=5))
    middles = detect_middles(current, as_of=now, max_age=timedelta(minutes=5))
    values = detect_consensus_value(
        current,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0.005"),
    )
    recommendations = rank_recommendations(arbs, middles, values)
    assert recommendations
    assert recommendations[0].opportunity_type == "PURE ARB"
    assert recommendations[0].priority == 3
    assert any(item.opportunity_type == "CONSENSUS +EV" for item in recommendations)


def test_recommendations_prioritize_bc_sportsbooks(now):
    current = deduplicate_quotes(
        quote for snapshot in generate_demo_snapshots(now) for quote in snapshot.quotes
    )
    arbs = detect_arbitrage(current, as_of=now, max_age=timedelta(minutes=5))
    middles = detect_middles(current, as_of=now, max_age=timedelta(minutes=5))
    values = detect_consensus_value(
        current,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0.005"),
    )

    recommendations = rank_recommendations(
        arbs,
        middles,
        values,
        priority_sportsbooks=("PlayNow", "Betway"),
    )

    assert recommendations
    assert "Betway" in recommendations[0].sportsbooks or "PlayNow" in recommendations[0].sportsbooks


def test_refresh_plan_targets_useful_pregame_windows(now):
    events = generate_demo_snapshots(now)[-1].events

    plans = plan_refreshes(events, as_of=now)

    assert len(plans) == 5
    assert plans == tuple(
        sorted(plans, key=lambda item: (item.check_at, item.kickoff, item.event_name))
    )
    assert all(plan.check_at >= now for plan in plans)
    assert all(plan.kickoff > plan.check_at for plan in plans)
