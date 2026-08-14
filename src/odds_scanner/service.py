from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from odds_scanner.domain import ArbitrageOpportunity, OddsSnapshot, Quote
from odds_scanner.opportunities import detect_arbitrage
from odds_scanner.providers.base import OddsProvider
from odds_scanner.storage.base import QuoteRepository


@dataclass(slots=True)
class ScannerService:
    provider: OddsProvider
    repository: QuoteRepository
    freshness: timedelta = timedelta(minutes=5)

    def refresh(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot:
        snapshot = self.provider.fetch_snapshot(league_keys, market_keys)
        self.repository.save_snapshot(snapshot)
        return snapshot

    def recent_quotes(self, *, as_of: datetime | None = None) -> tuple[Quote, ...]:
        effective_time = as_of or datetime.now(UTC)
        return self.repository.load_quotes_since(effective_time - self.freshness)

    def opportunities(
        self,
        quotes: Sequence[Quote] | None = None,
        *,
        bankroll: Decimal = Decimal("100"),
        as_of: datetime | None = None,
    ) -> tuple[ArbitrageOpportunity, ...]:
        effective_time = as_of or datetime.now(UTC)
        candidates = (
            tuple(quotes) if quotes is not None else self.recent_quotes(as_of=effective_time)
        )
        return detect_arbitrage(
            candidates,
            bankroll=bankroll,
            as_of=effective_time,
            max_age=self.freshness,
        )
