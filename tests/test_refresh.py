from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from conftest import make_market, make_quote

from odds_scanner.domain import (
    Event,
    League,
    MarketKind,
    OddsSnapshot,
    OpportunityStatus,
    Participant,
    Sport,
)
from odds_scanner.refresh import (
    AdaptivePollingPolicy,
    ApiBudgetManager,
    BudgetConfig,
    BudgetLevel,
    FreshnessConfig,
    FreshnessState,
    OddsRefreshService,
    ProviderCostProfile,
    RefreshConfig,
    RefreshRequest,
    RefreshResultStatus,
    RefreshTargetStrategy,
    freshness_state,
)
from odds_scanner.storage.sqlite import SQLiteQuoteRepository


@dataclass
class SnapshotProvider:
    snapshots: list[OddsSnapshot | Exception]
    request_count: int = 0

    @property
    def provider_id(self) -> str:
        return "provider"

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        assert league_keys
        assert market_keys
        self.request_count += 1
        result = self.snapshots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SequenceClock:
    def __init__(self, *moments: datetime) -> None:
        self.moments = list(moments)

    def __call__(self) -> datetime:
        return self.moments.pop(0)


def _event(now: datetime, event_id: str = "event-1", league_id: str = "nfl") -> Event:
    return Event(
        id=event_id,
        league_id=league_id,
        start_time=now + timedelta(days=2),
        home=Participant(f"{event_id}-home", "Home Team"),
        away=Participant(f"{event_id}-away", "Away Team"),
        name="Away Team at Home Team",
    )


def _snapshot(now: datetime, home_price: str) -> OddsSnapshot:
    event = _event(now)
    market = make_market(event_id=event.id)
    quotes = (
        make_quote(market, market.required_sides[0], home_price, now, book="alpha"),
        make_quote(market, market.required_sides[1], "1.80", now, book="alpha"),
        make_quote(market, market.required_sides[0], "1.90", now, book="beta"),
        make_quote(market, market.required_sides[1], "1.90", now, book="beta"),
        make_quote(market, market.required_sides[0], "1.90", now, book="gamma"),
        make_quote(market, market.required_sides[1], "1.90", now, book="gamma"),
    )
    return OddsSnapshot(
        provider_id="provider",
        sports=(Sport("american-football", "American Football"),),
        leagues=(League("nfl", "american-football", "NFL"),),
        events=(event,),
        quotes=quotes,
        fetched_at=now,
    )


def _request() -> RefreshRequest:
    return RefreshRequest(
        league_keys=("americanfootball_nfl",),
        league_ids=("nfl",),
        market_keys=("h2h",),
        market_kinds=(MarketKind.MONEYLINE,),
    )


def test_refresh_revalidates_and_deactivates_price_moved_opportunity(tmp_path, now):
    later = now + timedelta(minutes=10)
    repository = SQLiteQuoteRepository(tmp_path / "refresh.db")
    provider = SnapshotProvider([_snapshot(now, "2.20"), _snapshot(later, "1.90")])
    service = OddsRefreshService(
        provider,
        repository,
        RefreshConfig(minimum_ev=Decimal("0.02")),
        SequenceClock(now, now, later, later),
    )

    first = service.refresh(_request())
    active = repository.list_value_opportunities("provider", active_only=True)

    assert first.status is RefreshResultStatus.SUCCESS
    assert first.new_opportunities == 1
    assert len(active) == 1
    assert active[0].recommended_price == Decimal("2.20")
    assert active[0].status is OpportunityStatus.ACTIVE

    second = service.refresh(_request())
    history = repository.list_value_opportunities("provider")

    assert second.revalidated_opportunities == 1
    assert second.deactivated_opportunities == 1
    assert len(history) == 1
    assert history[0].is_active is False
    assert history[0].status is OpportunityStatus.INACTIVE_PRICE_MOVED
    assert history[0].current_price == Decimal("1.90")
    assert history[0].recommended_price == Decimal("2.20")


def test_failed_refresh_preserves_quotes_and_marks_old_recommendation_stale(tmp_path, now):
    later = now + timedelta(minutes=31)
    repository = SQLiteQuoteRepository(tmp_path / "failure.db")
    provider = SnapshotProvider([_snapshot(now, "2.20"), RuntimeError("provider offline")])
    service = OddsRefreshService(
        provider,
        repository,
        RefreshConfig(minimum_ev=Decimal("0.02")),
        SequenceClock(now, now, later, later),
    )

    service.refresh(_request())
    original_quotes = repository.load_latest_quotes("provider")
    failed = service.refresh(_request())
    opportunity = repository.list_value_opportunities("provider", active_only=True)[0]

    assert failed.status is RefreshResultStatus.FAILED
    assert failed.error_message == "provider offline"
    assert repository.load_latest_quotes("provider") == original_quotes
    assert opportunity.is_active is True
    assert opportunity.is_stale is True
    assert opportunity.status is OpportunityStatus.STALE
    usage = repository.api_usage_summary("provider", as_of=later)
    assert usage.successful_refreshes == 1
    assert usage.failed_refreshes == 1
    assert usage.requests_this_month == 2


def test_refresh_lock_blocks_duplicate_provider_request(tmp_path, now):
    repository = SQLiteQuoteRepository(tmp_path / "lock.db")
    assert repository.try_acquire_refresh_lock(
        "provider", "other-owner", now, now + timedelta(minutes=5)
    )
    provider = SnapshotProvider([_snapshot(now, "2.20")])
    service = OddsRefreshService(provider, repository, now_factory=SequenceClock(now))

    diagnostics = service.refresh(_request())

    assert diagnostics.status is RefreshResultStatus.ALREADY_RUNNING
    assert provider.request_count == 0


def test_manual_only_service_rejects_automated_trigger(tmp_path, now):
    repository = SQLiteQuoteRepository(tmp_path / "manual-only.db")
    provider = SnapshotProvider([_snapshot(now, "2.20")])
    service = OddsRefreshService(provider, repository, now_factory=SequenceClock(now))
    automated = RefreshRequest(
        league_keys=("americanfootball_nfl",),
        league_ids=("nfl",),
        market_keys=("h2h",),
        market_kinds=(MarketKind.MONEYLINE,),
        trigger_type="automated",
    )

    with pytest.raises(RuntimeError, match="disabled"):
        service.refresh(automated)

    assert provider.request_count == 0


def test_scoped_snapshot_replacement_preserves_other_leagues(tmp_path, now):
    repository = SQLiteQuoteRepository(tmp_path / "scope.db")
    nfl = _snapshot(now, "2.20")
    nhl_event = _event(now, event_id="event-nhl", league_id="nhl")
    nhl_market = make_market(event_id=nhl_event.id)
    nhl_quote = make_quote(nhl_market, nhl_market.required_sides[0], "2.05", now, book="delta")
    combined = OddsSnapshot(
        provider_id="provider",
        sports=(*nfl.sports, Sport("ice-hockey", "Ice Hockey")),
        leagues=(*nfl.leagues, League("nhl", "ice-hockey", "NHL")),
        events=(*nfl.events, nhl_event),
        quotes=(*nfl.quotes, nhl_quote),
        fetched_at=now,
    )
    repository.save_snapshot(combined)

    repository.save_snapshot(
        _snapshot(now + timedelta(minutes=1), "2.10"),
        replace_event_ids=("event-1",),
        replace_market_kinds=(MarketKind.MONEYLINE,),
    )

    latest = repository.load_latest_quotes("provider")
    assert any(quote.outcome.market.event_id == "event-nhl" for quote in latest)
    assert any(
        quote.outcome.market.event_id == "event-1" and quote.decimal_odds == Decimal("2.10")
        for quote in latest
    )


def test_future_polling_policy_prioritizes_active_ev_even_days_before_event(now):
    event = _event(now)
    policy = AdaptivePollingPolicy()

    normal = policy.interval_minutes(event, as_of=now)
    active = policy.interval_minutes(
        event,
        as_of=now,
        active_edges=(Decimal("0.06"),),
    )

    assert normal == 60
    assert active < normal
    assert active <= 15


def test_freshness_states_use_all_configured_thresholds(now):
    config = FreshnessConfig(fresh_minutes=5, warning_minutes=15, stale_minutes=30)

    assert (
        freshness_state(now, as_of=now + timedelta(minutes=4), config=config)
        is FreshnessState.FRESH
    )
    assert (
        freshness_state(now, as_of=now + timedelta(minutes=10), config=config)
        is FreshnessState.AGING
    )
    assert (
        freshness_state(now, as_of=now + timedelta(minutes=20), config=config)
        is FreshnessState.NEEDS_REFRESH
    )
    assert (
        freshness_state(now, as_of=now + timedelta(minutes=31), config=config)
        is FreshnessState.STALE
    )


def test_budget_manager_reduces_discovery_before_active_revalidation_reserve():
    manager = ApiBudgetManager(
        BudgetConfig(monthly_credit_limit=100, monthly_credit_reserve=5)
    )

    assert manager.level(69) is BudgetLevel.NORMAL
    assert manager.level(70) is BudgetLevel.WARNING
    assert manager.level(86) is BudgetLevel.CRITICAL
    assert manager.allow_background_discovery(86) is False
    assert manager.preserve_for_active_revalidation(94) is True
    assert manager.preserve_for_active_revalidation(95) is False


def test_provider_cost_profile_only_targets_events_when_that_is_cheaper():
    targeted = ProviderCostProfile(league_request_credits=10, event_request_credits=1)
    league_only = ProviderCostProfile(league_request_credits=1, event_request_credits=None)

    assert targeted.strategy(event_count=12, priority_event_count=2) is RefreshTargetStrategy.EVENT
    assert (
        league_only.strategy(event_count=12, priority_event_count=2)
        is RefreshTargetStrategy.LEAGUE
    )
