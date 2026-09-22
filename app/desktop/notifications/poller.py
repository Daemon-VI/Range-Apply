"""The notification poller: one lightweight thread, three read-only queries.

Supervised by the same :class:`~app.desktop.services.WorkerSupervisor` that
runs the optional local worker (Increment 1), so the desktop shell has one
way to start, stop and observe a background loop. Each poll opens a short
database session, asks :func:`~app.desktop.notifications.events.derive_events`
what is new since the cursor, hands every new notification to the in-process
center (the in-app fallback and the dedupe boundary) and to the native
adapter, then advances the cursor file. Nothing here writes to the database
and nothing here can start, retry or change an application.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from app.desktop.notifications.center import NotificationCenter, get_center
from app.desktop.notifications.cursor import CursorStore
from app.desktop.notifications.events import derive_events
from app.desktop.notifications.model import Notification
from app.desktop.notifications.native import Notifier, select_notifier

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 15.0


class NotificationPoller:
    """``run()`` polls until ``stop()``; ``poll_once()`` is the unit of work.

    ``launch_url_for`` turns a notification into the local URL a native toast
    opens on click (built by the shell: server URL + ``/desktop/open`` +
    single-use nonce). ``derive`` and ``session_factory`` are injectable for
    tests; the defaults are the real event derivation and the application's
    session factory.
    """

    def __init__(
        self,
        tenant_id: str,
        store: CursorStore,
        center: Optional[NotificationCenter] = None,
        notifier: Optional[Notifier] = None,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        launch_url_for: Optional[Callable[[Notification], Optional[str]]] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        derive: Callable[..., Any] = derive_events,
    ):
        self.tenant_id = tenant_id
        self.store = store
        self.center = center if center is not None else get_center()
        self.notifier = notifier if notifier is not None else select_notifier()
        self.interval = max(1.0, float(interval))
        self._launch_url_for = launch_url_for
        self._session_factory = session_factory
        self._derive = derive
        self._stop = threading.Event()
        self.totals: dict[str, Any] = {"polls": 0, "derived": 0, "notified": 0, "native": 0, "errors": 0, "last_poll_at": None, "last_error": None}

    # ------------------------------------------------------------ one poll

    def poll_once(self) -> list[Notification]:
        """Derive, dedupe, notify, advance. Returns the notifications that were new."""
        factory = self._session_factory
        if factory is None:
            from app.database import get_session_factory

            factory = get_session_factory()
        cursor = self.store.load(self.tenant_id)
        db = factory()
        try:
            events, cursor = self._derive(db, self.tenant_id, cursor)
        finally:
            db.close()
        fresh: list[Notification] = []
        for notification in events:
            if not self.center.add(notification):
                continue
            fresh.append(notification)
            launch_url = None
            if self._launch_url_for is not None:
                try:
                    launch_url = self._launch_url_for(notification)
                except Exception as exc:  # noqa: BLE001 - a toast without a link is better than none
                    logger.warning("Notification launch URL could not be built (%s)", type(exc).__name__)
            if self.notifier.send(notification, launch_url):
                self.totals["native"] += 1
        # Advance only after the notifications were handed over: a crash in
        # between re-announces at most once on restart, never loses one.
        self.store.save(self.tenant_id, cursor)
        self.totals["polls"] += 1
        self.totals["derived"] += len(events)
        self.totals["notified"] += len(fresh)
        self.totals["last_poll_at"] = time.time()
        if fresh:
            logger.info("Desktop notifications: %d new (%s)", len(fresh), ", ".join(sorted({n.kind for n in fresh})))
        return fresh

    # ---------------------------------------------------------------- loop

    def run(self) -> dict[str, Any]:
        logger.info("Notification poller started (every %.0fs, native=%s)", self.interval, getattr(self.notifier, "name", "?"))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad poll
                self.totals["errors"] += 1
                self.totals["last_error"] = type(exc).__name__
                logger.warning("Notification poll failed (%s); retrying next cycle", type(exc).__name__)
            self._stop.wait(self.interval)
        logger.info("Notification poller stopped")
        return dict(self.totals)

    def stop(self) -> None:
        self._stop.set()
