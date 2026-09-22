"""Controlled browser lifecycle for the local executor.

One :class:`BrowserSession` per worker process: it launches the browser
once, hands out an isolated context + page per execution, and closes
everything on exit (also via ``atexit`` so a crashed worker leaves no
orphaned browser). Timeouts are explicit and configurable; there are no
infinite waits.

Security: with ``profile_dir`` set the browser reuses a *local* persistent
profile (cookies, an already signed-in session). That directory is the
candidate's; it is never read by the server, never uploaded, never logged.
Without it every execution runs in a fresh, empty context.
"""

import atexit
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """True when the Playwright runtime is importable (a browser is checked at launch)."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means "not available"
        return False
    return True


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSession:
    """Launch once, use many times, always close."""

    def __init__(
        self,
        headless: Optional[bool] = None,
        browser_name: Optional[str] = None,
        profile_dir: Optional[str] = None,
        navigation_timeout_ms: Optional[int] = None,
        action_timeout_ms: Optional[int] = None,
        slow_mo_ms: int = 0,
    ):
        self.headless = settings.playwright_headless if headless is None else headless
        self.browser_name = browser_name or settings.playwright_browser
        self.profile_dir = profile_dir if profile_dir is not None else settings.playwright_profile_dir
        self.navigation_timeout_ms = navigation_timeout_ms or settings.playwright_navigation_timeout_ms
        self.action_timeout_ms = action_timeout_ms or settings.playwright_action_timeout_ms
        self.slow_mo_ms = slow_mo_ms
        self._playwright = None
        self._browser = None
        self._persistent_context = None
        self._lock = threading.Lock()
        self._closed = False
        self.launches = 0

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> "BrowserSession":
        if not is_available():
            raise BrowserUnavailable("playwright is not installed; run: pip install -e '.[browser]' && playwright install chromium")
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with self._lock:
            if self._playwright is not None:
                return self
            self._playwright = sync_playwright().start()
            launcher = getattr(self._playwright, self.browser_name)
            try:
                if self.profile_dir:
                    path = Path(self.profile_dir).expanduser()
                    path.mkdir(parents=True, exist_ok=True)
                    self._persistent_context = launcher.launch_persistent_context(
                        str(path), headless=self.headless, slow_mo=self.slow_mo_ms, accept_downloads=False
                    )
                else:
                    self._browser = launcher.launch(headless=self.headless, slow_mo=self.slow_mo_ms)
            except PlaywrightError as exc:
                self._playwright.stop()
                self._playwright = None
                raise BrowserUnavailable(f"could not launch {self.browser_name}: {str(exc).splitlines()[0]}") from exc
            self.launches += 1
            atexit.register(self.close)
            logger.info("Browser %s launched (headless=%s, profile=%s)", self.browser_name, self.headless, bool(self.profile_dir))
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for target in (self._persistent_context, self._browser):
                if target is not None:
                    try:
                        target.close()
                    except Exception:  # noqa: BLE001 - closing must never raise
                        logger.debug("Browser close raised", exc_info=True)
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("Playwright stop raised", exc_info=True)
            self._playwright = None
            self._browser = None
            self._persistent_context = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._playwright is not None and not self._closed

    def relaunch(self) -> None:
        """After a browser-level crash: close what is left and start again."""
        self.close()
        self._closed = False
        self.start()

    # ------------------------------------------------------------------ pages

    @contextmanager
    def page(self, **context_kwargs: Any):
        """An isolated page for one execution; closed on exit whatever happens."""
        if not self.running:
            self.start()
        context = None
        page = None
        try:
            if self._persistent_context is not None:
                page = self._persistent_context.new_page()
            else:
                context = self._browser.new_context(accept_downloads=False, **context_kwargs)
                page = context.new_page()
            page.set_default_navigation_timeout(self.navigation_timeout_ms)
            page.set_default_timeout(self.action_timeout_ms)
            yield page
        finally:
            for target in (page, context):
                if target is not None:
                    try:
                        target.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("Page/context close raised", exc_info=True)


class Pacer:
    """Minimum spacing between navigations so employer sites are not hammered."""

    def __init__(self, min_delay_seconds: Optional[float] = None):
        self.min_delay = settings.playwright_min_delay_seconds if min_delay_seconds is None else min_delay_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        with self._lock:
            now = time.monotonic()
            gap = self.min_delay - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            return max(0.0, gap)
