from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from odds_scanner.analytics import best_value_by_outcome, detect_consensus_value
from odds_scanner.domain import BetStatus, TrackedBet
from odds_scanner.opportunities import deduplicate_quotes, implied_probability
from odds_scanner.presentation import decimal_to_american
from odds_scanner.providers.demo import generate_demo_snapshots
from odds_scanner.strategy import OFFICIAL_RECOMMENDATION_PREFIX
from odds_scanner.ui import (
    CORE_REFRESH_LEAGUES,
    DEFAULT_ODDS_FORMAT,
    RECOMMENDED_MAXIMUM_AMERICAN_ODDS,
    RECOMMENDED_MINIMUM_AMERICAN_ODDS,
    RECOMMENDED_MINIMUM_EV,
    RECOMMENDED_MINIMUM_IMPLIED_PROBABILITY,
    RECOMMENDED_MINIMUM_REFERENCE_BOOKS,
    SPORTSBOOK_URLS,
    STARTER_BOOKS,
    EVFilterState,
    _board_header_markup,
    _decode_sportsbook_preferences,
    _event_odds_frame,
    _event_team_names,
    _filter_value_opportunities,
    _format_strategy_dollars,
    _game_event_markup,
    _game_market_sections_markup,
    _market_label,
    _market_range_label,
    _official_bankroll_history,
    _password_matches,
    _recommended_value_opportunities,
    _selection_label,
    _sort_more_ev_values,
    _sportsbook_bet_url,
    _sportsbook_default_enabled,
    _sportsbook_event_url,
    _sportsbook_preferences_storage_key,
    _value_comparison_markup,
    _value_opportunities_for_books,
)


def test_strategy_money_is_dollar_first():
    assert _format_strategy_dollars(Decimal("2.50")) == "+$250"
    assert _format_strategy_dollars(Decimal("-1.25")) == "-$125"
    assert _format_strategy_dollars(Decimal("0")) == "+$0"


def test_decimal_odds_are_the_default_display_format():
    assert DEFAULT_ODDS_FORMAT == "Decimal"


def test_official_bankroll_history_uses_settlement_time_and_dollars(now):
    common = {
        "id": None,
        "event_id": "event",
        "event_name": "Away at Home",
        "market_label": "Moneyline",
        "selection": "Home",
        "sportsbook": "Book",
        "decimal_odds": Decimal("2.00"),
        "stake": Decimal("1"),
    }
    bets = (
        TrackedBet(
            **common,
            created_at=now - timedelta(days=3),
            status=BetStatus.WON,
            profit_loss=Decimal("0.50"),
            settled_at=now - timedelta(days=1),
            notes=f"{OFFICIAL_RECOMMENDATION_PREFIX}one",
        ),
        TrackedBet(
            **common,
            created_at=now - timedelta(days=2),
            status=BetStatus.LOST,
            profit_loss=Decimal("-1"),
            settled_at=now - timedelta(days=2),
            notes=f"{OFFICIAL_RECOMMENDATION_PREFIX}two",
        ),
        TrackedBet(
            **common,
            created_at=now - timedelta(hours=1),
            notes=f"{OFFICIAL_RECOMMENDATION_PREFIX}pending",
        ),
    )

    history = _official_bankroll_history(bets, as_of=now)

    assert list(history["Bankroll ($)"]) == [10000.0, 9900.0, 9950.0, 9950.0]
    assert list(history["Date"]) == sorted(history["Date"])


def test_board_tooltips_are_exclusive_and_every_book_is_enabled_by_default():
    header = _board_header_markup()

    assert header.count('name="board-tooltip"') == 4
    assert _sportsbook_default_enabled("PlayNow")
    assert _sportsbook_default_enabled("Betway")
    assert _sportsbook_default_enabled("Pinnacle")


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


def test_games_page_prices_are_clickable_and_best_price_is_highlighted(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]
    event_url = "https://sports.betway.com/en/sports/event/example"
    event_quotes = tuple(
        replace(quote, source_url=event_url) if quote.sportsbook.name == "Betway" else quote
        for quote in snapshot.quotes
        if quote.outcome.market.event_id == event.id
    )
    sportsbooks = ["PlayNow", "Betway", "Pinnacle"]

    markets = _game_market_sections_markup(
        event_quotes,
        sportsbooks,
        "American",
        event,
    )
    event_markup = _game_event_markup(
        event,
        event_quotes,
        sportsbooks,
        "American",
    )

    assert f'href="{event_url}"' in markets
    assert 'class="games-price-link best"' in markets
    assert 'aria-label="Bet ' in markets
    assert "Moneyline" in markets
    assert all(f"<th>{sportsbook}</th>" in markets for sportsbook in sportsbooks)
    assert "Available to you" not in markets
    assert '<details class="games-event">' in event_markup
    assert '<details class="games-event" open>' not in event_markup
    assert event.name in event_markup


def test_games_page_keeps_scheduled_events_before_odds_are_posted(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]

    markup = _game_event_markup(event, (), ["PlayNow", "Betway"], "American")

    assert "Odds not available yet" in markup
    assert event.name in markup


def test_stale_sportsbook_prices_remain_visible_but_are_not_clickable(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]
    event_url = "https://sports.betway.com/en/sports/event/example"
    stale_quotes = tuple(
        replace(
            quote,
            source_updated_at=now - timedelta(days=1),
            observed_at=now,
            source_url=event_url,
        )
        for quote in snapshot.quotes
        if quote.outcome.market.event_id == event.id
    )

    markup = _game_market_sections_markup(
        stale_quotes,
        ["PlayNow", "Betway", "Pinnacle"],
        "American",
        event,
        as_of=now,
    )

    assert "stale" in markup
    assert event_url not in markup


def test_stale_provider_prices_cannot_create_value_bets(now):
    snapshot = generate_demo_snapshots(now)[-1]
    stale_quotes = tuple(
        replace(quote, source_updated_at=now - timedelta(days=1), observed_at=now)
        for quote in snapshot.quotes
    )

    values = _value_opportunities_for_books(
        stale_quotes,
        tuple({quote.sportsbook.name for quote in stale_quotes}),
        max_age=timedelta(minutes=30),
    )

    assert values == ()


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


def test_team_names_fall_back_to_the_event_title_for_generic_feed_participants(now):
    snapshot = generate_demo_snapshots(now)[-1]
    event = snapshot.events[0]
    generic_event = replace(
        event,
        home=replace(event.home, name="Home"),
        away=replace(event.away, name="Away"),
        name="Missouri Tigers at Kansas Jayhawks",
    )

    assert _event_team_names(generic_event) == (
        "Kansas Jayhawks",
        "Missouri Tigers",
    )


def test_every_available_book_has_a_bet_now_destination():
    assert set(STARTER_BOOKS) <= SPORTSBOOK_URLS.keys()


def test_default_live_refresh_includes_cfl():
    assert "CFL" in CORE_REFRESH_LEAGUES


def test_saved_sportsbook_preferences_restore_only_available_books():
    available = ("PlayNow", "Betway", "Pinnacle")

    assert _decode_sportsbook_preferences(
        '["Pinnacle","Unavailable Book","PlayNow"]',
        available,
    ) == ("PlayNow", "Pinnacle")
    assert _decode_sportsbook_preferences("[]", available) == ()
    assert _decode_sportsbook_preferences(
        "%5B%22Betway%22%2C%22PlayNow%22%5D",
        available,
    ) == ("PlayNow", "Betway")
    assert _decode_sportsbook_preferences("not-json", available) is None
    assert _sportsbook_preferences_storage_key("OddsPapi Free").startswith("bettor_bureau_")


def test_bet_now_prefers_a_verified_event_deep_link(now):
    snapshot = generate_demo_snapshots(now)[-1]
    quote = next(item for item in snapshot.quotes if item.sportsbook.name == "Betway")
    event_url = "https://sports.betway.com/en/sports/event/example"

    direct_quote = replace(quote, source_url=event_url)
    unsafe_quote = replace(quote, source_url="https://malicious.example/phishing")

    assert _sportsbook_event_url(direct_quote) == event_url
    assert _sportsbook_bet_url(direct_quote) == event_url
    assert _sportsbook_event_url(unsafe_quote) is None
    assert _sportsbook_bet_url(unsafe_quote) == SPORTSBOOK_URLS["Betway"]


def test_player_prop_labels_name_the_player_stat_and_line(now):
    snapshot = generate_demo_snapshots(now)[-1]
    quote = next(
        item for item in snapshot.quotes if item.outcome.market.kind.value == "player_prop"
    )

    assert _market_label(quote.outcome.market) == "Passing yards"
    selection = _selection_label(quote)
    assert quote.outcome.market.variant in selection
    assert str(quote.outcome.market.line) in selection


def test_ev_board_keeps_one_best_offer_per_bet_and_filters_implied_probability(now):
    snapshots = generate_demo_snapshots(now)
    quotes = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
    )
    events = {event.id: event for event in snapshots[-1].events}
    filters = EVFilterState(
        league_id=None,
        market_kind=None,
        minimum_ev=Decimal("0"),
        my_books=(),
        minimum_implied_probability=Decimal("0.30"),
        minimum_american_odds=None,
        maximum_american_odds=None,
        minimum_consensus_books=2,
        starts_before=None,
        fresh_only=False,
        sort_by="EV % (High to Low)",
    )

    filtered = _filter_value_opportunities(
        values,
        events,
        filters,
        as_of=now,
        max_age=timedelta(minutes=5),
    )

    assert filtered
    assert len({item.quote.outcome.id for item in filtered}) == len(filtered)
    assert all(implied_probability(item.quote.decimal_odds) >= Decimal("0.30") for item in filtered)
    assert tuple(item.expected_value for item in filtered) == tuple(
        sorted((item.expected_value for item in filtered), reverse=True)
    )
    assert len(best_value_by_outcome(values)) <= len(values)


def test_all_ev_keeps_recommendations_and_sportsbook_filter_controls_visibility(now):
    snapshots = generate_demo_snapshots(now)
    quotes = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
    )
    events = {event.id: event for event in snapshots[-1].events}
    filters = EVFilterState(
        league_id=None,
        market_kind=None,
        minimum_ev=Decimal("0"),
        my_books=(),
        minimum_implied_probability=Decimal("0"),
        minimum_american_odds=None,
        maximum_american_odds=None,
        minimum_consensus_books=2,
        starts_before=None,
        fresh_only=False,
        sort_by="EV % (High to Low)",
    )

    all_filtered = _filter_value_opportunities(
        values,
        events,
        filters,
        as_of=now,
        max_age=timedelta(minutes=5),
    )
    recommended = _recommended_value_opportunities(all_filtered, events, as_of=now)
    playnow_values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=("PlayNow",),
    )
    visible = _recommended_value_opportunities(playnow_values, events, as_of=now)
    hidden_values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
        candidate_sportsbooks=(),
    )

    recommended_keys = {(item.quote.sportsbook.id, item.quote.outcome.id) for item in recommended}
    all_keys = {(item.quote.sportsbook.id, item.quote.outcome.id) for item in all_filtered}
    assert recommended_keys <= all_keys
    assert all(item.quote.sportsbook.name == "PlayNow" for item in visible)
    assert hidden_values == ()


def test_more_ev_has_its_own_sorting(now):
    snapshots = generate_demo_snapshots(now)
    quotes = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
    )
    events = {event.id: event for event in snapshots[-1].events}

    by_probability = _sort_more_ev_values(values, events, "Win Probability")
    by_odds = _sort_more_ev_values(values, events, "Best Odds")

    assert tuple(item.fair_probability for item in by_probability) == tuple(
        sorted((item.fair_probability for item in values), reverse=True)
    )
    assert tuple(item.quote.decimal_odds for item in by_odds) == tuple(
        sorted((item.quote.decimal_odds for item in values), reverse=True)
    )


def test_recommended_bets_clear_the_product_criteria(now):
    snapshots = generate_demo_snapshots(now)
    quotes = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    values = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
    )
    events = {event.id: event for event in snapshots[-1].events}

    recommended = _recommended_value_opportunities(values, events, as_of=now)

    assert len(recommended) > 3
    assert all(item.expected_value >= RECOMMENDED_MINIMUM_EV for item in recommended)
    assert all(
        implied_probability(item.quote.decimal_odds) >= RECOMMENDED_MINIMUM_IMPLIED_PROBABILITY
        for item in recommended
    )
    assert all(
        RECOMMENDED_MINIMUM_AMERICAN_ODDS
        <= decimal_to_american(item.quote.decimal_odds)
        <= RECOMMENDED_MAXIMUM_AMERICAN_ODDS
        for item in recommended
    )
    assert all(item.reference_books >= RECOMMENDED_MINIMUM_REFERENCE_BOOKS for item in recommended)
    assert all(events[item.quote.outcome.market.event_id].start_time > now for item in recommended)


def test_expanded_comparison_shows_line_shopping_without_repeating_summary(now):
    snapshots = generate_demo_snapshots(now)
    quotes = deduplicate_quotes(quote for snapshot in snapshots for quote in snapshot.quotes)
    opportunity = detect_consensus_value(
        quotes,
        as_of=now,
        max_age=timedelta(minutes=5),
        minimum_ev=Decimal("0"),
    )[0]

    label = _market_range_label(opportunity, quotes, "American")
    details = _value_comparison_markup(opportunity, quotes, "American", now)

    assert " to " in label
    assert "Compare prices" in details
    assert "Implied prob." in details
    assert "Edge vs fair" in details
    assert "Books used for fair odds" in details
    assert "Consensus win probability" not in details
    assert "Break-even probability" not in details
    assert "Market range" not in details
