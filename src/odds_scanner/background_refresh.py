from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Thread

from odds_scanner.refresh import RefreshDiagnostics, RefreshResultStatus


@dataclass(frozen=True, slots=True)
class BackgroundRefreshStatus:
    provider_id: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    diagnostics: RefreshDiagnostics | None = None
    error_message: str | None = None

    @property
    def is_running(self) -> bool:
        return self.state in {"queued", "running"}


class BackgroundRefreshRunner:
    """Run one refresh per provider without tying it to a Streamlit request."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._statuses: dict[str, BackgroundRefreshStatus] = {}
        self._threads: dict[str, Thread] = {}

    def status(self, provider_id: str) -> BackgroundRefreshStatus | None:
        with self._lock:
            return self._statuses.get(provider_id)

    def start(
        self,
        provider_id: str,
        job: Callable[[], RefreshDiagnostics],
    ) -> bool:
        with self._lock:
            active = self._threads.get(provider_id)
            if active is not None and active.is_alive():
                return False
            started_at = datetime.now(UTC)
            self._statuses[provider_id] = BackgroundRefreshStatus(
                provider_id=provider_id,
                state="queued",
                started_at=started_at,
            )
            worker = Thread(
                target=self._run,
                args=(provider_id, started_at, job),
                name=f"odds-refresh-{provider_id}",
                daemon=True,
            )
            self._threads[provider_id] = worker
            worker.start()
            return True

    def _run(
        self,
        provider_id: str,
        started_at: datetime,
        job: Callable[[], RefreshDiagnostics],
    ) -> None:
        self._set_status(
            BackgroundRefreshStatus(
                provider_id=provider_id,
                state="running",
                started_at=started_at,
            )
        )
        try:
            diagnostics = job()
            state = (
                "succeeded"
                if diagnostics.status is RefreshResultStatus.SUCCESS
                else "already_running"
                if diagnostics.status is RefreshResultStatus.ALREADY_RUNNING
                else "failed"
            )
            self._set_status(
                BackgroundRefreshStatus(
                    provider_id=provider_id,
                    state=state,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    diagnostics=diagnostics,
                    error_message=diagnostics.error_message,
                )
            )
        except Exception as exc:
            self._set_status(
                BackgroundRefreshStatus(
                    provider_id=provider_id,
                    state="failed",
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    error_message=str(exc),
                )
            )

    def _set_status(self, status: BackgroundRefreshStatus) -> None:
        with self._lock:
            self._statuses[status.provider_id] = status


OWNER_REFRESH_RUNNER = BackgroundRefreshRunner()
