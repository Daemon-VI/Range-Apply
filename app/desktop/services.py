"""Supervision of the optional local worker inside the desktop process.

The existing ``LocalWorker`` (Playwright, one browser, one item at a time)
runs unchanged in a dedicated thread. Playwright's sync API needs a thread
without a running asyncio loop, which is exactly what a plain worker thread
is; the API event loop lives in the server thread. Verified on Windows
(Phase 13 / Increment 1): a browser launched in a worker thread navigates to
the in-process server and shuts down cleanly.

Increment 1 exposes dry run only: the supervisor cannot be asked to submit.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)


def default_worker_factory(headless: bool = True) -> Callable[[], Any]:
    """Build the existing local worker in dry run; Playwright is imported lazily."""

    def build():
        from app.execution.worker import LocalWorker

        return LocalWorker(tenant_id=settings.default_tenant_id, worker_id="desktop-worker", dry_run=True, headless=headless)

    return build


def default_poller_factory(shell: Any, interval: float = 15.0, prefer_native: bool = True) -> Callable[[], Any]:
    """Build the Increment 5 notification poller for the desktop shell.

    Called by the supervisor at ``start()`` time, i.e. after the server is
    up, so the shell's URL is known. A toast's click URL is the local
    ``/desktop/open`` route with a single-use nonce: no key, no cookie, no
    token ever travels in a notification.
    """

    def build():
        from urllib.parse import quote

        from app.desktop.notifications.cursor import CursorStore
        from app.desktop.notifications.native import select_notifier
        from app.desktop.notifications.poller import NotificationPoller

        def launch_url_for(notification) -> str:
            return f"{shell.server.url}/desktop/open?to={quote(notification.path, safe='/')}&n={shell.open_nonces.mint()}"

        return NotificationPoller(
            tenant_id=settings.default_tenant_id,
            store=CursorStore(),
            notifier=select_notifier(prefer_native=prefer_native),
            interval=interval,
            launch_url_for=launch_url_for,
        )

    return build


class WorkerSupervisor:
    """Start / stop / observe one worker in its own thread.

    The worker object must offer ``run()`` (blocking loop) and ``stop()``
    (asks the loop to end); ``totals`` is read when present.
    """

    def __init__(self, factory: Callable[[], Any], name: str = "careeros-worker"):
        self._factory = factory
        self._name = name
        self._worker: Any = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None
        self._result: Any = None
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def target(self) -> Any:
        """The supervised object (the worker, poller or autopilot), or None before start."""
        return self._worker

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._error = None
            self._result = None
            self.stopped_at = None
            try:
                self._worker = self._factory()
            except Exception as exc:  # noqa: BLE001 - reported, never raised into the shell
                self._error = f"{type(exc).__name__}: {exc}"
                logger.error("Worker could not be built: %s", self._error)
                return False
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self.started_at = time.time()
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            self._result = self._worker.run()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            logger.exception("Worker thread failed")
        finally:
            self.stopped_at = time.time()

    def stop(self, timeout: float = 30.0) -> bool:
        if not self.running:
            return True
        try:
            self._worker.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker stop raised %s", type(exc).__name__)
        self._thread.join(timeout)
        if self.running:
            logger.warning("Worker thread did not stop within %.0fs", timeout)
            return False
        return True

    def status(self) -> dict[str, Any]:
        state = "running" if self.running else "failed" if self._error else "stopped" if self.started_at else "idle"
        totals = getattr(self._worker, "totals", None)
        return {
            "state": state,
            "dry_run": bool(getattr(getattr(self._worker, "executor", None), "dry_run", True)),
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "error": self._error,
            "totals": dict(totals) if isinstance(totals, dict) else None,
        }
