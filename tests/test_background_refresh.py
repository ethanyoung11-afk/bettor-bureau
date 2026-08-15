from datetime import UTC, datetime
from threading import Event
from time import monotonic

from odds_scanner.background_refresh import BackgroundRefreshRunner
from odds_scanner.refresh import RefreshDiagnostics, RefreshResultStatus


def _success(provider_id: str = "provider") -> RefreshDiagnostics:
    now = datetime.now(UTC)
    return RefreshDiagnostics(
        status=RefreshResultStatus.SUCCESS,
        provider_id=provider_id,
        started_at=now,
        finished_at=now,
    )


def _wait_until_finished(runner: BackgroundRefreshRunner, provider_id: str) -> None:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        status = runner.status(provider_id)
        if status is not None and not status.is_running:
            return
    raise AssertionError("background refresh did not finish")


def test_background_refresh_returns_immediately_and_prevents_duplicates():
    runner = BackgroundRefreshRunner()
    release = Event()

    def job() -> RefreshDiagnostics:
        release.wait(timeout=2)
        return _success()

    assert runner.start("provider", job)
    status = runner.status("provider")
    assert status is not None and status.is_running
    assert not runner.start("provider", job)

    release.set()
    _wait_until_finished(runner, "provider")
    status = runner.status("provider")
    assert status is not None
    assert status.state == "succeeded"


def test_background_refresh_surfaces_worker_errors():
    runner = BackgroundRefreshRunner()

    def job() -> RefreshDiagnostics:
        raise RuntimeError("provider unavailable")

    assert runner.start("provider", job)
    _wait_until_finished(runner, "provider")
    status = runner.status("provider")
    assert status is not None
    assert status.state == "failed"
    assert status.error_message == "provider unavailable"
