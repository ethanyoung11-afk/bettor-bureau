from odds_scanner.providers.demo import generate_demo_snapshots
from odds_scanner.ui import (
    PRIORITY_BOOKS,
    SPORTSBOOK_URLS,
    _event_odds_frame,
    _password_matches,
    _selection_label,
)


def test_event_odds_frame_keeps_book_columns_and_marks_best_prices(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event_id = snapshot.events[0].id
    event_quotes = tuple(
        quote for quote in snapshot.quotes if quote.outcome.market.event_id == event_id
    )
    sportsbooks = ["PlayNow", "Betway", "Pinnacle"]

    frame = _event_odds_frame(event_quotes, sportsbooks, "American")

    assert list(frame.columns) == ["Market", "Bet", *sportsbooks]
    assert not frame.empty
    assert all(
        any(str(value).startswith("★") for value in row)
        for row in frame[sportsbooks].itertuples(index=False, name=None)
    )
    assert all(value == "—" or isinstance(value, str) for value in frame[sportsbooks].values.flat)


def test_owner_password_uses_a_sha256_digest():
    expected_hash = "889bf59808d9edbab7703dd7db993fc02298c7d1bb20c52ba3114a9185903124"

    assert _password_matches("owner-access", expected_hash)
    assert not _password_matches("wrong-password", expected_hash)
    assert not _password_matches("owner-access", "")


def test_selection_label_uses_the_team_name_when_event_is_available(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]
    quote = next(
        item
        for item in snapshot.quotes
        if item.outcome.market.event_id == event.id
        and item.outcome.market.kind.value == "moneyline"
    )

    assert _selection_label(quote, event) in {event.home.name, event.away.name}


def test_every_priority_book_has_a_bet_now_destination():
    assert set(PRIORITY_BOOKS) <= SPORTSBOOK_URLS.keys()
