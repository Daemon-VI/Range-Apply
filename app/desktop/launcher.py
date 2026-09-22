"""Process lifecycle for the desktop shell: port, server, readiness, shutdown.

Everything here is testable without a window: :class:`DesktopApp` can run
"headless" (server + optional worker only) and is driven that way by the
tests; the window layer is a separate, thin module.
"""

import logging
import secrets
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from app.config import settings
from app.database import (
    UNSTAMPED_ADVICE,
    alembic_revision,
    missing_tables,
    safe_database_url,
    unstamped_schema,
)

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
LOGIN_PATH = "/desktop/login"
HOME_PATH = "/dashboard/"


# ------------------------------------------------------------------ port


def find_free_port(host: str = LOOPBACK, preferred: Optional[int] = None) -> int:
    """The preferred port when it is free, otherwise one the OS hands out.

    Always on the loopback address: the desktop shell never listens anywhere
    else, whatever ``API_HOST`` says.
    """
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, preferred))
                return preferred
            except OSError:
                logger.info("Port %d is busy; picking a free one", preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


# ------------------------------------------------------------- readiness


@dataclass
class Readiness:
    """What must be true before the window is worth opening."""

    ready: bool
    problems: list[str] = field(default_factory=list)
    database_url: str = ""
    database_reachable: bool = False
    database_migrated: bool = False
    missing_tables: list[str] = field(default_factory=list)
    api_key_configured: bool = False
    development: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "problems": list(self.problems),
            "database": {
                "url": self.database_url,
                "reachable": self.database_reachable,
                "migrated": self.database_migrated,
                "missing_tables": list(self.missing_tables),
            },
            "api_key_configured": self.api_key_configured,
            "development": self.development,
        }


def _is_development() -> bool:
    return settings.app_env.lower() in ("development", "dev", "test")


def check_readiness() -> Readiness:
    """Configuration and database checks, in the same terms ``/health`` uses.

    Never creates tables and never invents a key: a missing migration or a
    missing ``API_KEY`` outside development is reported with the command that
    fixes it, and the shell refuses to open.
    """
    report = Readiness(ready=True, database_url=safe_database_url(), api_key_configured=bool(settings.api_key), development=_is_development())
    if not report.api_key_configured and not report.development:
        report.ready = False
        report.problems.append(f"API_KEY is not set and APP_ENV={settings.app_env}: set API_KEY in .env (any long random string) before starting the desktop app.")
    try:
        absent = missing_tables()
        report.database_reachable = True
        report.missing_tables = list(absent)
        report.database_migrated = not absent
        if unstamped_schema():
            # A pre-Alembic bootstrap: the tables that exist were never
            # stamped, so ``upgrade head`` fails on the first migration.
            report.ready = False
            report.database_migrated = False
            report.problems.append(f"The database has application tables but no Alembic revision ({len(absent)} required table(s) missing). {UNSTAMPED_ADVICE}")
        elif absent:
            report.ready = False
            report.problems.append(f"The database is not migrated ({len(absent)} table(s) missing): run `alembic upgrade head` first.")
        elif alembic_revision() is None:
            report.ready = False
            report.database_migrated = False
            report.problems.append(f"The database has every required table but no Alembic revision. {UNSTAMPED_ADVICE}")
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        report.ready = False
        report.database_reachable = False
        report.problems.append(f"The database is not reachable ({type(exc).__name__}): check DATABASE_URL.")
    return report


# ---------------------------------------------------------------- server


class ServerHandle:
    """uvicorn in a daemon thread, stopped through ``should_exit``."""

    def __init__(self, app: Any, host: str = LOOPBACK, port: Optional[int] = None, log_level: str = "info"):
        if host != LOOPBACK:
            raise ValueError("the desktop shell binds the loopback address only")
        self.host = host
        self.port = port or find_free_port(host, settings.api_port)
        self._app = app
        self._log_level = log_level
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.started_at: Optional[float] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "ServerHandle":
        import uvicorn

        if self.running:
            return self
        config = uvicorn.Config(self._app, host=self.host, port=self.port, log_level=self._log_level, access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="careeros-server", daemon=True)
        self._thread.start()
        self.started_at = time.time()
        return self

    def wait_ready(self, timeout: float = 30.0, interval: float = 0.2) -> dict[str, Any]:
        """Poll ``/health`` until it answers; return the last health payload.

        ``status == "ok"`` means reachable *and* migrated; ``degraded`` means
        the server is up but the database is not ready.
        """
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {"status": "unreachable"}
        while time.monotonic() < deadline:
            if self._thread is not None and not self._thread.is_alive():
                last = {"status": "exited"}
                break
            try:
                response = httpx.get(f"{self.url}/health", timeout=2.0)
                if response.status_code == 200:
                    return response.json()
                last = {"status": f"http {response.status_code}"}
            except httpx.HTTPError:
                pass
            time.sleep(interval)
        return last

    def stop(self, timeout: float = 10.0) -> bool:
        if self._server is None:
            return True
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        stopped = not self.running
        if not stopped:
            logger.warning("Server thread did not stop within %.0fs", timeout)
        return stopped


# ---------------------------------------------------------------- tokens


class LoginTokens:
    """Single-use, in-memory tokens that let the window sign in once.

    The API key itself never appears in a URL (uvicorn would log it): the
    launcher mints a random token, the ``/desktop/login`` route consumes it
    and sets the same HttpOnly cookie the dashboard already uses.
    """

    def __init__(self, max_size: int = 64):
        # Insertion-ordered so the oldest token is evicted once the set is
        # full (notification click nonces accumulate in the Action Center).
        self._tokens: dict[str, None] = {}
        self._max_size = max(1, int(max_size))
        self._lock = threading.Lock()

    def mint(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = None
            while len(self._tokens) > self._max_size:
                self._tokens.pop(next(iter(self._tokens)))
        return token

    def consume(self, candidate: Optional[str]) -> bool:
        if not candidate:
            return False
        with self._lock:
            for token in list(self._tokens):
                if secrets.compare_digest(token, candidate):
                    self._tokens.pop(token, None)
                    return True
        return False

    def __len__(self) -> int:
        return len(self._tokens)


# ------------------------------------------------------------------ app


class DesktopApp:
    """Start everything, hand the window a login URL, stop everything.

    ``open_window`` is injected so the lifecycle is testable without a
    display: it receives the URL and must block until the window closes.
    """

    def __init__(
        self,
        app: Any,
        port: Optional[int] = None,
        worker_supervisor: Any = None,
        open_window: Optional[Callable[[str], None]] = None,
        ready_timeout: float = 30.0,
        log_level: str = "info",
        notification_supervisor: Any = None,
        autopilot_supervisor: Any = None,
    ):
        self.server = ServerHandle(app, port=port, log_level=log_level)
        self.worker = worker_supervisor
        #: Increment 5: the notification poller, supervised like the worker.
        self.notifications = notification_supervisor
        #: Discover → match → prepare on a timer (never submits), supervised like the worker.
        self.autopilot = autopilot_supervisor
        self._open_window = open_window
        self._ready_timeout = ready_timeout
        self.tokens = LoginTokens()
        #: Single-use nonces a native toast carries so a click may navigate
        #: the already-signed-in window to one local page (never a credential).
        self.open_nonces = LoginTokens(max_size=500)
        self.readiness: Optional[Readiness] = None
        self.health: dict[str, Any] = {}
        self._app = app
        self._stopped = threading.Event()

    # -- state the /desktop/status route reports (counts and flags only)
    def status(self) -> dict[str, Any]:
        return {
            "server": {"url": self.server.url, "running": self.server.running, "started_at": self.server.started_at},
            "readiness": self.readiness.as_dict() if self.readiness else None,
            "health": self.health,
            "worker": self.worker.status() if self.worker is not None else {"state": "disabled"},
            "notifications": self.notifications.status() if self.notifications is not None else {"state": "disabled"},
            "autopilot": self.autopilot.status() if self.autopilot is not None else {"state": "disabled"},
            "window": self._open_window is not None,
        }

    def start(self) -> bool:
        """Readiness → server → health. Returns False (nothing started) when not ready."""
        self.readiness = check_readiness()
        if not self.readiness.ready:
            for problem in self.readiness.problems:
                logger.error("Not ready: %s", problem)
            return False
        self._app.state.desktop = self
        self.server.start()
        self.health = self.server.wait_ready(self._ready_timeout)
        if self.health.get("status") != "ok":
            logger.error("Server did not become ready: %s", self.health)
            self.server.stop()
            return False
        logger.info("CareerOS desktop server ready at %s", self.server.url)
        if self.worker is not None:
            self.worker.start()
        if self.notifications is not None:
            self.notifications.start()
        if self.autopilot is not None:
            self.autopilot.start()
        return True

    def login_url(self) -> str:
        return f"{self.server.url}{LOGIN_PATH}?token={self.tokens.mint()}&next={HOME_PATH}"

    def stop(self) -> dict[str, bool]:
        """Reverse order: notifications, worker (its browser), then the server."""
        hook = getattr(self, "on_stopping", None)
        if hook is not None and not self._stopped.is_set():
            try:
                hook()
            except Exception:  # noqa: BLE001 - a marker must never block shutdown
                logger.warning("on_stopping hook failed", exc_info=True)
        if self._stopped.is_set():
            return {"notifications": True, "worker": True, "server": True}
        outcome = {"notifications": True, "worker": True, "server": True}
        if self.autopilot is not None:
            outcome["autopilot"] = self.autopilot.stop(timeout=10.0)
        if self.notifications is not None:
            outcome["notifications"] = self.notifications.stop(timeout=5.0)
        if self.worker is not None:
            outcome["worker"] = self.worker.stop()
        outcome["server"] = self.server.stop()
        self._stopped.set()
        logger.info("CareerOS desktop stopped: %s", outcome)
        return outcome

    def run(self) -> int:
        """Start, show the window (blocking) or wait for Ctrl-C, stop. Exit code."""
        if not self.start():
            return 2
        try:
            if self._open_window is not None:
                self._open_window(self.login_url())
            else:
                self._wait_for_interrupt()
        finally:
            self.stop()
        return 0

    def _wait_for_interrupt(self) -> None:
        """Block until Ctrl-C / SIGTERM (or Ctrl-Break on Windows) or the server dies."""
        logger.info("No window: serving at %s until Ctrl-C", self.server.url)
        stop = threading.Event()
        previous = {}
        signals = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
        in_main_thread = threading.current_thread() is threading.main_thread()
        if in_main_thread:
            for sig in signals:
                try:
                    previous[sig] = signal.signal(sig, lambda *_: stop.set())
                except (ValueError, OSError):  # pragma: no cover - platform-specific
                    continue
        try:
            while self.server.running and not stop.is_set():
                stop.wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
