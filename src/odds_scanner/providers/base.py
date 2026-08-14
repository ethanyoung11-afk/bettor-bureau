from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from odds_scanner.domain import OddsSnapshot


class OddsProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def fetch_snapshot(
        self,
        league_keys: Sequence[str],
        market_keys: Sequence[str],
    ) -> OddsSnapshot: ...
