from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from odds_scanner.domain import (
    ApiUsageSummary,
    BetStatus,
    Event,
    MarketKind,
    OddsSnapshot,
    OpportunityCounts,
    Quote,
    RefreshRun,
    TrackedBet,
    ValueOpportunityRecord,
)


class QuoteRepository(Protocol):
    def save_snapshot(
        self,
        snapshot: OddsSnapshot,
        *,
        replace_event_ids: Sequence[str] | None = None,
        replace_market_kinds: Sequence[MarketKind] | None = None,
    ) -> None: ...

    def load_quotes_since(self, since: datetime) -> tuple[Quote, ...]: ...

    def load_latest_quotes(self, provider_id: str) -> tuple[Quote, ...]: ...

    def load_events(self, provider_id: str | None = None) -> tuple[Event, ...]: ...

    def list_value_opportunities(
        self, provider_id: str, *, active_only: bool = False
    ) -> tuple[ValueOpportunityRecord, ...]: ...

    def save_value_opportunities(
        self, opportunities: tuple[ValueOpportunityRecord, ...]
    ) -> None: ...

    def mark_stale_opportunities(
        self, provider_id: str, stale_before: datetime, marked_at: datetime
    ) -> int: ...

    def opportunity_counts(self, provider_id: str) -> OpportunityCounts: ...

    def try_acquire_refresh_lock(
        self,
        provider_id: str,
        owner_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> bool: ...

    def release_refresh_lock(self, provider_id: str, owner_id: str) -> None: ...

    def record_refresh_run(self, run: RefreshRun) -> None: ...

    def api_usage_summary(self, provider_id: str, *, as_of: datetime) -> ApiUsageSummary: ...

    def save_setting(self, key: str, value: str) -> None: ...

    def load_settings(self) -> dict[str, str]: ...

    def add_bet(self, bet: TrackedBet) -> int: ...

    def list_bets(self) -> tuple[TrackedBet, ...]: ...

    def update_bet(self, bet_id: int, status: BetStatus, profit_loss: Decimal | None) -> None: ...

    def watched_event_ids(self) -> frozenset[str]: ...

    def set_event_watched(self, event_id: str, watched: bool, created_at: datetime) -> None: ...
