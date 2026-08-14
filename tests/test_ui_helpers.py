from odds_scanner.providers.demo import generate_demo_snapshots
from odds_scanner.ui import (
    SPORTSBOOK_URLS,
    STARTER_BOOKS,
    _event_odds_frame,
    _market_label,
    _password_matches,
    _selection_label,
)


def test_event_odds_frame_keeps_book_columns_and_marks_best_prices(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]
    event_id = event.id
    event_quotes = tuple(
        quote for quote in snapshot.quotes if quote.outcome.market.event_id == event_id
    )
    sportsbooks = ["PlayNow", "Betway", "Pinnacle"]

    frame = _event_odds_frame(event_quotes, sportsbooks, "American", event)

    assert list(frame.columns) == ["Market", "Bet", *sportsbooks]
    assert not frame.empty
    assert all(
        any(str(value).startswith("★") for value in row)
        for row in frame[sportsbooks].itertuples(index=False, name=None)
    )
    assert all(value == "—" or isinstance(value, str) for value in frame[sportsbooks].values.flat)
    assert any(event.home.name in str(value) for value in frame["Bet"])
    assert any(event.away.name in str(value) for value in frame["Bet"])


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


def test_every_available_book_has_a_bet_now_destination():
    assert set(STARTER_BOOKS) <= SPORTSBOOK_URLS.keys()


def test_player_prop_labels_name_the_player_stat_and_line(now):
    snapshot = generate_demo_snapshots(now)[-1]
    quote = next(
        item
        for item in snapshot.quotes
        if item.outcome.market.kind.value == "player_prop"
    )

    assert _market_label(quote.outcome.market) == "Passing yards"
    selection = _selection_label(quote)
    assert quote.outcome.market.variant in selection
    assert str(quote.outcome.market.line) in selection
