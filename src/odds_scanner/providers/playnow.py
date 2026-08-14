from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from odds_scanner.domain import Event
from odds_scanner.normalization import canonical_token


@dataclass(slots=True)
class PlayNowEventResolver:
    """Resolve canonical events against PlayNow's public sportsbook search."""

    endpoint: str = "https://content.sb.playnow.com/content-service/api/v1/q/search"
    timeout_seconds: float = 10.0
    session: requests.Session = field(default_factory=requests.Session)
    _cache: dict[str, str | None] = field(default_factory=dict, init=False)

    def resolve(self, event: Event) -> str | None:
        if event.id in self._cache:
            return self._cache[event.id]
        resolved = self._search(event)
        self._cache[event.id] = resolved
        return resolved

    def _search(self, event: Event) -> str | None:
        try:
            response = self.session.get(
                self.endpoint,
                params={"query": event.home.name},
                headers={
                    "Origin": "https://www.playnow.com",
                    "Referer": "https://www.playnow.com/sports/",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (requests.RequestException, ValueError):
            return None

        candidates = (
            payload.get("data", {}).get("search", {}).get("events", [])
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(candidates, list):
            return None

        home_token = canonical_token(event.home.name)
        away_token = canonical_token(event.away.name)
        matches: list[tuple[float, str]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            detail = candidate.get("detail")
            if not isinstance(detail, dict):
                continue
            name_token = canonical_token(str(detail.get("name") or ""))
            event_id = str(detail.get("id") or candidate.get("ref") or "").strip()
            if (
                not event_id.isdigit()
                or home_token not in name_token
                or away_token not in name_token
            ):
                continue
            try:
                candidate_start = datetime.fromisoformat(
                    str(detail.get("startTime") or "").replace("Z", "+00:00")
                )
                difference = abs((candidate_start - event.start_time).total_seconds())
            except ValueError:
                continue
            if difference <= 12 * 60 * 60:
                matches.append((difference, event_id))

        if not matches:
            return None
        _, event_id = min(matches)
        return f"https://www.playnow.com/sports/sports/event/{event_id}"
