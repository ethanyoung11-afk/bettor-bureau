from __future__ import annotations

from typing import Any

from odds_scanner.providers.playnow import PlayNowEventResolver


class StubResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class StubSession:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def get(self, _url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(kwargs)
        return StubResponse(self.payload)


def test_playnow_resolver_matches_teams_and_start_time(event, now) -> None:
    session = StubSession(
        {
            "data": {
                "search": {
                    "events": [
                        {
                            "ref": "12025970",
                            "detail": {
                                "id": "12025970",
                                "name": "Away Team at Home Team",
                                "startTime": now.isoformat(),
                            },
                        }
                    ]
                }
            }
        }
    )
    resolver = PlayNowEventResolver(session=session)  # type: ignore[arg-type]

    assert resolver.resolve(event) == (
        "https://www.playnow.com/sports/sports/event/12025970"
    )
    assert resolver.resolve(event) == (
        "https://www.playnow.com/sports/sports/event/12025970"
    )
    assert len(session.calls) == 1


def test_playnow_resolver_rejects_a_different_event(event, now) -> None:
    session = StubSession(
        {
            "data": {
                "search": {
                    "events": [
                        {
                            "ref": "12025970",
                            "detail": {
                                "id": "12025970",
                                "name": "Other Team at Home Team",
                                "startTime": now.isoformat(),
                            },
                        }
                    ]
                }
            }
        }
    )
    resolver = PlayNowEventResolver(session=session)  # type: ignore[arg-type]

    assert resolver.resolve(event) is None
