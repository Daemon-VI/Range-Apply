"""The window: pywebview over WebView2 (Windows), a thin shim and nothing more.

Imported lazily so the launcher, its tests and headless use never need a
display or the optional ``desktop`` extra.
"""

import logging

logger = logging.getLogger(__name__)

WINDOW_TITLE = "CareerOS"


def is_available() -> bool:
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


def open_window(url: str, width: int = 1280, height: int = 840) -> None:
    """Show ``url`` in a native window; blocks until the window is closed."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("pywebview is not installed: pip install -e '.[desktop]'") from exc
    webview.create_window(WINDOW_TITLE, url, width=width, height=height, min_size=(900, 600))
    # Private mode keeps the WebView2 profile ephemeral (no persisted cookies
    # or storage on disk beyond this run); the dashboard cookie is re-minted
    # by the launcher on every start.
    webview.start(private_mode=True)
    logger.info("Window closed")


def navigate(url: str) -> bool:
    """Point the open window at a local URL (a notification click); False when there is no window.

    Called from the request thread of ``GET /desktop/open``: pywebview's
    ``load_url`` marshals onto the GUI thread itself.
    """
    try:
        import webview
    except ImportError:
        return False
    windows = getattr(webview, "windows", None) or []
    if not windows:
        return False
    window = windows[0]
    try:
        window.load_url(url)
    except Exception as exc:  # noqa: BLE001 - a failed focus must never break the route
        logger.warning("Window navigation failed (%s)", type(exc).__name__)
        return False
    for attempt in ("restore", "show"):
        try:
            getattr(window, attempt)()
        except Exception:  # noqa: BLE001 - best effort only
            pass
    return True
