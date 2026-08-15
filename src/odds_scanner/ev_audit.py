from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from odds_scanner.analytics import (
    audit_consensus_value,
    best_value_by_outcome,
    opportunities_from_value_audit,
)
from odds_scanner.live_refresh import repository_from_environment
from odds_scanner.opportunities import deduplicate_quotes

PROVIDER_ID = "oddspapi"
DEFAULT_SELECTED_SPORTSBOOKS = ("PlayNow", "Betway")
DEFAULT_MINIMUM_EV = Decimal("0.02")


def _selected_sportsbooks() -> tuple[str, ...]:
    configured = os.getenv("AUDIT_SPORTSBOOKS", "").strip()
    if not configured:
        return DEFAULT_SELECTED_SPORTSBOOKS
    return tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))


def main() -> int:
    repository = repository_from_environment()
    as_of = datetime.now(UTC)
    future_event_ids = {
        event.id for event in repository.load_events(PROVIDER_ID) if event.start_time > as_of
    }
    quotes = deduplicate_quotes(
        quote
        for quote in repository.load_latest_quotes(PROVIDER_ID)
        if quote.outcome.market.event_id in future_event_ids
    )
    audit = audit_consensus_value(
        quotes,
        as_of=as_of,
        max_age=timedelta(minutes=30),
        include_stale=True,
    )

    if len(audit) != len(quotes) or {item.quote for item in audit} != set(quotes):
        raise RuntimeError("EV audit did not account for every current sportsbook price")

    all_evaluated = opportunities_from_value_audit(audit, minimum_ev=Decimal("-1"))
    all_positive = opportunities_from_value_audit(audit, minimum_ev=Decimal("0"))
    all_qualifying = opportunities_from_value_audit(audit, minimum_ev=DEFAULT_MINIMUM_EV)
    selected = {name.casefold() for name in _selected_sportsbooks()}
    selected_qualifying = tuple(
        item
        for item in all_qualifying
        if item.quote.sportsbook.name.casefold() in selected
    )
    displayed = best_value_by_outcome(selected_qualifying)

    summary = {
        "status": "ok",
        "future_events": len(future_event_ids),
        "prices_audited": len(audit),
        "prices_with_consensus": len(all_evaluated),
        "positive_ev_prices": len(all_positive),
        "prices_at_or_above_2_percent": len(all_qualifying),
        "default_selected_sportsbooks": list(_selected_sportsbooks()),
        "default_displayed_selections": len(displayed),
        "excluded_reasons": dict(
            sorted(Counter(item.exclusion_reason or "evaluated" for item in audit).items())
        ),
        "displayed_by_market": dict(
            sorted(Counter(item.quote.outcome.market.kind.value for item in displayed).items())
        ),
        "displayed_by_sportsbook": dict(
            sorted(Counter(item.quote.sportsbook.name for item in displayed).items())
        ),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
