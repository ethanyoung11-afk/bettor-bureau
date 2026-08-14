from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from time import monotonic
from uuid import uuid4

from odds_scanner.analytics import ValueOpportunity, detect_consensus_value
from odds_scanner.domain import (
    Event,
    MarketKind,
    OpportunityStatus,
    RefreshRun,
    ValueOpportunityRecord,
    stable_id,
    utc_now,
)
from odds_scanner.opportunities import implied_probability
from odds_scanner.providers.base import OddsProvider
from odds_scanner.storage.base import QuoteRepository


class RefreshResultStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    ALREADY_RUNNING = "already_running"


@dataclass(frozen=True, slots=True)
class FreshnessConfig:
    fresh_minutes: int = 5
    warning_minutes: int = 15
    stale_minutes: int = 30

    def __post_init__(self) -> None:
        if not 0 < self.fresh_minutes <= self.warning_minutes <= self.stale_minutes:
            raise ValueError("Freshness thresholds must be positive and ordered")


@dataclass(frozen=True, slots=True)
class IntervalConfig:
    over_24h: int
    six_to_24h: int
    one_to_six_h: int
    under_one_h: int

    def __post_init__(self) -> None:
        if min(self.over_24h, self.six_to_24h, self.one_to_six_h, self.under_one_h) <= 0:
            raise ValueError("Refresh intervals must be positive")


@dataclass(frozen=True, slots=True)
class EvPriorityConfig:
    medium: Decimal = Decimal("0.02")
    high: Decimal = Decimal("0.05")
    very_high: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.medium <= self.high <= self.very_high:
            raise ValueError("EV priority thresholds must be ordered")


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    monthly_credit_limit: int | None = None
    daily_credit_target: int | None = None
    monthly_credit_reserve: int = 0
    usage_warning_percentage: Decimal = Decimal("0.70")
    usage_critical_percentage: Decimal = Decimal("0.85")
    usage_emergency_percentage: Decimal = Decimal("0.95")

    def __post_init__(self) -> None:
        if not (
            Decimal("0")
            <= self.usage_warning_percentage
            <= self.usage_critical_percentage
            <= self.usage_emergency_percentage
            <= Decimal("1")
        ):
            raise ValueError("API budget thresholds must be ordered percentages")


class BudgetLevel(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass(frozen=True, slots=True)
class ApiBudgetManager:
    """Future polling guardrails; manual refresh remains an explicit admin decision."""

    config: BudgetConfig

    def level(self, credits_used: int) -> BudgetLevel:
        limit = self.config.monthly_credit_limit
        if limit is None or limit <= 0:
            return BudgetLevel.NORMAL
        fraction = Decimal(max(0, credits_used)) / Decimal(limit)
        if fraction >= self.config.usage_emergency_percentage:
            return BudgetLevel.EMERGENCY
        if fraction >= self.config.usage_critical_percentage:
            return BudgetLevel.CRITICAL
        if fraction >= self.config.usage_warning_percentage:
            return BudgetLevel.WARNING
        return BudgetLevel.NORMAL

    def allow_background_discovery(self, credits_used: int) -> bool:
        return self.level(credits_used) not in {BudgetLevel.CRITICAL, BudgetLevel.EMERGENCY}

    def preserve_for_active_revalidation(self, credits_used: int) -> bool:
        limit = self.config.monthly_credit_limit
        if limit is None:
            return True
        remaining = max(0, limit - max(0, credits_used))
        return remaining > self.config.monthly_credit_reserve


@dataclass(frozen=True, slots=True)
class RefreshConfig:
    manual_only: bool = True
    minimum_ev: Decimal = Decimal("0.02")
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    normal_intervals: IntervalConfig = field(
        default_factory=lambda: IntervalConfig(60, 15, 5, 2)
    )
    active_ev_intervals: IntervalConfig = field(
        default_factory=lambda: IntervalConfig(15, 10, 5, 2)
    )
    ev_priority: EvPriorityConfig = field(default_factory=EvPriorityConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    movement_window_minutes: int = 30
    refresh_lock_minutes: int = 5
    show_stale_recommendations: bool = True


DEFAULT_REFRESH_CONFIG = RefreshConfig()


@dataclass(frozen=True, slots=True)
class RefreshRequest:
    league_keys: tuple[str, ...]
    league_ids: tuple[str, ...]
    market_keys: tuple[str, ...]
    market_kinds: tuple[MarketKind, ...]
    trigger_type: str = "manual"

    def __post_init__(self) -> None:
        if (
            not self.league_keys
            or not self.league_ids
            or not self.market_keys
            or not self.market_kinds
        ):
            raise ValueError("Select at least one sport and market before refreshing")
        if self.trigger_type not in {"manual", "automated"}:
            raise ValueError("Refresh trigger must be manual or automated")


@dataclass(frozen=True, slots=True)
class RefreshDiagnostics:
    status: RefreshResultStatus
    provider_id: str
    started_at: datetime
    finished_at: datetime
    events_checked: int = 0
    sportsbooks_checked: int = 0
    new_opportunities: int = 0
    revalidated_opportunities: int = 0
    deactivated_opportunities: int = 0
    requests_made: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    quotes_stored: int = 0
    error_message: str | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())


class FreshnessState(StrEnum):
    FRESH = "Fresh"
    AGING = "Aging"
    NEEDS_REFRESH = "Needs refresh"
    STALE = "Stale"


def freshness_state(
    checked_at: datetime,
    *,
    as_of: datetime,
    config: FreshnessConfig | None = None,
) -> FreshnessState:
    effective_config = config or FreshnessConfig()
    age = max(timedelta(0), as_of - checked_at)
    if age < timedelta(minutes=effective_config.fresh_minutes):
        return FreshnessState.FRESH
    if age < timedelta(minutes=effective_config.warning_minutes):
        return FreshnessState.AGING
    if age < timedelta(minutes=effective_config.stale_minutes):
        return FreshnessState.NEEDS_REFRESH
    return FreshnessState.STALE


@dataclass(frozen=True, slots=True)
class AdaptivePollingPolicy:
    """Computes future polling intervals without scheduling or making requests."""

    config: RefreshConfig = DEFAULT_REFRESH_CONFIG

    def interval_minutes(
        self,
        event: Event,
        *,
        as_of: datetime,
        active_edges: Sequence[Decimal] = (),
        recent_price_changes: int = 0,
    ) -> int:
        time_to_event = max(timedelta(0), event.start_time - as_of)
        intervals = (
            self.config.active_ev_intervals
            if active_edges
            else self.config.normal_intervals
        )
        if time_to_event > timedelta(hours=24):
            interval = intervals.over_24h
        elif time_to_event > timedelta(hours=6):
            interval = intervals.six_to_24h
        elif time_to_event > timedelta(hours=1):
            interval = intervals.one_to_six_h
        else:
            interval = intervals.under_one_h

        strongest_edge = max(active_edges, default=Decimal("0"))
        if strongest_edge >= self.config.ev_priority.very_high:
            interval = max(1, interval // 2)
        elif strongest_edge >= self.config.ev_priority.high:
            interval = max(1, (interval * 3) // 4)
        if recent_price_changes >= 3:
            interval = max(1, interval // 2)
        return interval


class RefreshTargetStrategy(StrEnum):
    LEAGUE = "league"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class ProviderCostProfile:
    """Lets a future scheduler choose the cheaper provider-supported request shape."""

    league_request_credits: int = 1
    event_request_credits: int | None = None

    def strategy(self, *, event_count: int, priority_event_count: int) -> RefreshTargetStrategy:
        if self.event_request_credits is None or priority_event_count <= 0:
            return RefreshTargetStrategy.LEAGUE
        event_cost = self.event_request_credits * priority_event_count
        league_cost = self.league_request_credits
        if priority_event_count < event_count and event_cost < league_cost:
            return RefreshTargetStrategy.EVENT
        return RefreshTargetStrategy.LEAGUE


@dataclass(slots=True)
class OddsRefreshService:
    provider: OddsProvider
    repository: QuoteRepository
    config: RefreshConfig = DEFAULT_REFRESH_CONFIG
    now_factory: Callable[[], datetime] = utc_now

    def refresh(self, request: RefreshRequest) -> RefreshDiagnostics:
        if self.config.manual_only and request.trigger_type != "manual":
            raise RuntimeError("Automated refresh is disabled in manual-only mode")

        started_at = self.now_factory().astimezone(UTC)
        lock_owner = uuid4().hex
        lock_acquired = self.repository.try_acquire_refresh_lock(
            self.provider.provider_id,
            lock_owner,
            started_at,
            started_at + timedelta(minutes=self.config.refresh_lock_minutes),
        )
        if not lock_acquired:
            return RefreshDiagnostics(
                status=RefreshResultStatus.ALREADY_RUNNING,
                provider_id=self.provider.provider_id,
                started_at=started_at,
                finished_at=started_at,
                error_message="A refresh is already running.",
            )

        timer_started = monotonic()
        requests_before = self._request_count()
        try:
            try:
                snapshot = self.provider.fetch_snapshot(
                    request.league_keys,
                    request.market_keys,
                )
                existing_scope_events = {
                    event.id
                    for event in self.repository.load_events()
                    if event.league_id in request.league_ids
                }
                self.repository.save_snapshot(
                    snapshot,
                    replace_event_ids=tuple(
                        existing_scope_events | {event.id for event in snapshot.events}
                    ),
                    replace_market_kinds=request.market_kinds,
                )
                finished_at = self.now_factory().astimezone(UTC)
                diagnostics = self._reconcile(
                    request=request,
                    started_at=started_at,
                    fetched_at=snapshot.fetched_at,
                    finished_at=finished_at,
                    events_checked=len(snapshot.events),
                    sportsbooks_checked=len(
                        {quote.sportsbook.id for quote in snapshot.quotes}
                    ),
                    quotes_stored=len(snapshot.quotes),
                    requests_made=max(0, self._request_count() - requests_before),
                )
            except Exception as exc:
                finished_at = self.now_factory().astimezone(UTC)
                self.repository.mark_stale_opportunities(
                    self.provider.provider_id,
                    finished_at
                    - timedelta(minutes=self.config.freshness.stale_minutes),
                    finished_at,
                )
                diagnostics = self._failed_diagnostics(
                    started_at,
                    finished_at,
                    exc,
                    requests_made=max(0, self._request_count() - requests_before),
                )

            duration = max(0.0, monotonic() - timer_started)
            if diagnostics.finished_at == diagnostics.started_at:
                diagnostics = replace(
                    diagnostics,
                    finished_at=diagnostics.started_at + timedelta(seconds=duration),
                )
            self.repository.record_refresh_run(self._run_record(request, diagnostics))
            return diagnostics
        finally:
            self.repository.release_refresh_lock(self.provider.provider_id, lock_owner)

    def _reconcile(
        self,
        *,
        request: RefreshRequest,
        started_at: datetime,
        fetched_at: datetime,
        finished_at: datetime,
        events_checked: int,
        sportsbooks_checked: int,
        quotes_stored: int,
        requests_made: int,
    ) -> RefreshDiagnostics:
        provider_id = self.provider.provider_id
        all_events = self.repository.load_events()
        event_map = {event.id: event for event in all_events}
        scoped_event_ids = {
            event.id for event in all_events if event.league_id in request.league_ids
        }
        latest_quotes = tuple(
            quote
            for quote in self.repository.load_latest_quotes(provider_id)
            if quote.outcome.market.event_id in scoped_event_ids
            and quote.outcome.market.kind in request.market_kinds
        )
        evaluated = detect_consensus_value(
            latest_quotes,
            as_of=finished_at,
            max_age=timedelta(minutes=self.config.freshness.stale_minutes),
            minimum_ev=Decimal("-0.999999"),
        )
        evaluations = {
            self._opportunity_id(item): item
            for item in evaluated
        }
        observed_quotes = {
            (quote.sportsbook.id, quote.outcome.id): quote for quote in latest_quotes
        }
        existing = self.repository.list_value_opportunities(provider_id)
        existing_by_id = {item.id: item for item in existing}
        covered_existing = tuple(
            item
            for item in existing
            if item.event_id in scoped_event_ids and item.market_kind in request.market_kinds
        )
        prior_active_ids = {item.id for item in covered_existing if item.is_active}
        updates: list[ValueOpportunityRecord] = []
        activated = 0

        for opportunity_id, opportunity in evaluations.items():
            event = event_map.get(opportunity.quote.outcome.market.event_id)
            qualifies = (
                event is not None
                and event.start_time > finished_at
                and opportunity.expected_value >= self.config.minimum_ev
            )
            previous = existing_by_id.get(opportunity_id)
            if previous is None and not qualifies:
                continue
            if qualifies and (previous is None or not previous.is_active):
                activated += 1
            updates.append(
                self._record_for_evaluation(
                    opportunity_id,
                    opportunity,
                    previous,
                    qualifies=qualifies,
                    verified_at=finished_at,
                    snapshot_id=stable_id("api-snapshot", provider_id, fetched_at.isoformat()),
                )
            )

        evaluated_ids = set(evaluations)
        for previous in covered_existing:
            if previous.id in evaluated_ids:
                continue
            event = event_map.get(previous.event_id)
            observed = observed_quotes.get((previous.sportsbook_id, previous.outcome_id))
            if event is not None and event.start_time <= finished_at:
                status = OpportunityStatus.INACTIVE_EVENT_STARTED
            elif observed is None:
                status = OpportunityStatus.INACTIVE_MARKET_REMOVED
            else:
                status = OpportunityStatus.INACTIVE_PRICE_MOVED
            updates.append(
                replace(
                    previous,
                    last_seen_at=(observed.observed_at if observed else previous.last_seen_at),
                    last_verified_at=finished_at,
                    last_updated_at=(
                        observed.source_updated_at if observed else previous.last_updated_at
                    ),
                    is_active=False,
                    is_stale=False,
                    status=status,
                    current_price=(observed.decimal_odds if observed else previous.current_price),
                    implied_probability=(
                        implied_probability(observed.decimal_odds)
                        if observed
                        else previous.implied_probability
                    ),
                    deactivated_at=finished_at,
                    api_snapshot_id=stable_id(
                        "api-snapshot", provider_id, fetched_at.isoformat()
                    ),
                )
            )

        self.repository.save_value_opportunities(tuple(updates))
        self.repository.mark_stale_opportunities(
            provider_id,
            finished_at - timedelta(minutes=self.config.freshness.stale_minutes),
            finished_at,
        )
        active_after = {item.id for item in updates if item.is_active}
        deactivated = len(prior_active_ids - active_after)
        usage = self.repository.api_usage_summary(provider_id, as_of=finished_at)
        credits_remaining = (
            None
            if self.config.budget.monthly_credit_limit is None
            else max(
                0,
                self.config.budget.monthly_credit_limit
                - usage.credits_this_month
                - requests_made,
            )
        )
        return RefreshDiagnostics(
            status=RefreshResultStatus.SUCCESS,
            provider_id=provider_id,
            started_at=started_at,
            finished_at=finished_at,
            events_checked=events_checked,
            sportsbooks_checked=sportsbooks_checked,
            new_opportunities=activated,
            revalidated_opportunities=len(prior_active_ids),
            deactivated_opportunities=deactivated,
            requests_made=requests_made,
            credits_used=requests_made,
            credits_remaining=credits_remaining,
            quotes_stored=quotes_stored,
        )

    def _record_for_evaluation(
        self,
        opportunity_id: str,
        opportunity: ValueOpportunity,
        previous: ValueOpportunityRecord | None,
        *,
        qualifies: bool,
        verified_at: datetime,
        snapshot_id: str,
    ) -> ValueOpportunityRecord:
        quote = opportunity.quote
        reactivated = qualifies and (previous is None or not previous.is_active)
        if previous is not None and previous.current_price != quote.decimal_odds:
            recent = verified_at - previous.last_price_change_at <= timedelta(
                minutes=self.config.movement_window_minutes
            )
            change_count = previous.price_change_count_recent + 1 if recent else 1
            last_price_change_at = verified_at
        else:
            change_count = previous.price_change_count_recent if previous else 0
            last_price_change_at = previous.last_price_change_at if previous else verified_at
        return ValueOpportunityRecord(
            id=opportunity_id,
            provider_id=quote.provider_id,
            event_id=quote.outcome.market.event_id,
            sportsbook_id=quote.sportsbook.id,
            sportsbook=quote.sportsbook.name,
            outcome_id=quote.outcome.id,
            market_kind=quote.outcome.market.kind,
            selection=quote.outcome.side,
            first_seen_at=previous.first_seen_at if previous else verified_at,
            last_seen_at=quote.observed_at,
            last_verified_at=verified_at,
            last_price_change_at=last_price_change_at,
            last_updated_at=quote.source_updated_at,
            is_active=qualifies,
            is_stale=False,
            status=(
                OpportunityStatus.ACTIVE
                if qualifies
                else OpportunityStatus.INACTIVE_PRICE_MOVED
            ),
            recommended_price=(
                quote.decimal_odds
                if reactivated
                else previous.recommended_price if previous else quote.decimal_odds
            ),
            current_price=quote.decimal_odds,
            ev_at_activation=(
                opportunity.expected_value
                if reactivated
                else previous.ev_at_activation if previous else opportunity.expected_value
            ),
            current_ev=opportunity.expected_value,
            fair_probability=opportunity.fair_probability,
            implied_probability=implied_probability(quote.decimal_odds),
            price_change_count_recent=change_count,
            api_snapshot_id=snapshot_id,
            deactivated_at=None if qualifies else verified_at,
        )

    def _failed_diagnostics(
        self,
        started_at: datetime,
        finished_at: datetime,
        error: Exception,
        *,
        requests_made: int,
    ) -> RefreshDiagnostics:
        usage = self.repository.api_usage_summary(
            self.provider.provider_id,
            as_of=finished_at,
        )
        credits_remaining = (
            None
            if self.config.budget.monthly_credit_limit is None
            else max(
                0,
                self.config.budget.monthly_credit_limit
                - usage.credits_this_month
                - requests_made,
            )
        )
        return RefreshDiagnostics(
            status=RefreshResultStatus.FAILED,
            provider_id=self.provider.provider_id,
            started_at=started_at,
            finished_at=finished_at,
            requests_made=requests_made,
            credits_used=requests_made,
            credits_remaining=credits_remaining,
            error_message=str(error),
        )

    def _request_count(self) -> int:
        raw_count = getattr(self.provider, "request_count", 0)
        return max(0, int(raw_count))

    @staticmethod
    def _opportunity_id(opportunity: ValueOpportunity) -> str:
        quote = opportunity.quote
        return stable_id(
            "value-opportunity",
            quote.provider_id,
            quote.sportsbook.id,
            quote.outcome.id,
        )

    @staticmethod
    def _run_record(
        request: RefreshRequest,
        diagnostics: RefreshDiagnostics,
    ) -> RefreshRun:
        return RefreshRun(
            id=stable_id(
                "refresh-run",
                diagnostics.provider_id,
                diagnostics.started_at.isoformat(),
                uuid4().hex,
            ),
            provider_id=diagnostics.provider_id,
            trigger_type=request.trigger_type,
            status=diagnostics.status.value,
            started_at=diagnostics.started_at,
            finished_at=diagnostics.finished_at,
            league_keys=request.league_keys,
            market_keys=request.market_keys,
            requests_made=diagnostics.requests_made,
            credits_consumed=diagnostics.credits_used,
            credits_remaining=diagnostics.credits_remaining,
            events_checked=diagnostics.events_checked,
            sportsbooks_checked=diagnostics.sportsbooks_checked,
            new_opportunities=diagnostics.new_opportunities,
            revalidated_opportunities=diagnostics.revalidated_opportunities,
            deactivated_opportunities=diagnostics.deactivated_opportunities,
            error_message=diagnostics.error_message,
        )
