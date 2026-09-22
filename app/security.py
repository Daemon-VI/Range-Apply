"""Authentication appropriate to a single-user, free-tier deployment.

Deliberately minimal: one shared secret (``API_KEY``) guards every write
endpoint and the dashboard, which holds personal career data. This is not a
user/session system and is not meant to become one before Phase 5.

Behaviour when ``API_KEY`` is unset:

* ``APP_ENV=development`` — allowed, with a warning logged once per process, so
  a fresh clone still runs with zero configuration.
* anything else — denied with 503, because an unprotected deployment would
  expose candidate data and let anyone trigger outbound discovery traffic.
"""

import hmac
import logging
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from app.config import settings

logger = logging.getLogger(__name__)

DASHBOARD_COOKIE = "careeros_key"

#: Desktop shell writes (Increment 2): the dashboard cookie is the credential;
#: this header is the request-context / CSRF guard, never a credential itself.
DESKTOP_HEADER = "X-Requested-With"
DESKTOP_HEADER_VALUE = "careeros-desktop"

_warned = False


def _warn_once() -> None:
    global _warned
    if not _warned:
        logger.warning(
            "API_KEY is not set. Write endpoints and the dashboard are UNPROTECTED. "
            "This is allowed only because APP_ENV=development."
        )
        _warned = True


def _is_development() -> bool:
    return settings.app_env.lower() in ("development", "dev", "test")


def _matches(candidate: Optional[str]) -> bool:
    """Constant-time comparison against the configured key."""
    if not candidate or not settings.api_key:
        return False
    return hmac.compare_digest(candidate, settings.api_key)


def check_api_key(candidate: Optional[str]) -> bool:
    """Whether ``candidate`` authorizes a privileged action."""
    if settings.api_key:
        return _matches(candidate)
    if _is_development():
        _warn_once()
        return True
    return False


def _same_origin(request: Request) -> bool:
    """True unless the browser says the request came from somewhere else.

    A cookie-authenticated write may only come from the page itself: with an
    ``Origin`` header it must name this server; with ``Sec-Fetch-Site`` it
    must be ``same-origin`` (or ``none``, a direct navigation). Requests
    without either header (non-browser clients) pass this check; they still
    need the valid cookie and the exact header.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host") or ""
        if origin.rstrip("/").lower() != f"{request.url.scheme}://{host}".lower():
            return False
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.lower() not in ("same-origin", "none"):
        return False
    return True


def desktop_write_allowed(request: Request, x_requested_with: Optional[str], cookie: Optional[str]) -> bool:
    """The desktop path: valid dashboard cookie AND the exact desktop header.

    The header alone is never a credential (it is checked *after* the cookie
    matched the configured key, in constant time); the cookie alone never
    authorizes a write (a plain browser tab holding the cookie cannot add the
    header without a script, and a cross-site page cannot send the cookie —
    SameSite=Lax — nor pass the origin check).
    """
    if x_requested_with != DESKTOP_HEADER_VALUE:
        return False
    if not _matches(cookie):
        return False
    return _same_origin(request)


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    x_requested_with: Optional[str] = Header(default=None),
    careeros_key: Optional[str] = Cookie(default=None),
) -> None:
    """FastAPI dependency guarding write endpoints.

    Two ways in, both against the one configured key, never logged:

    * ``X-API-Key`` header (API clients, the browser extension) — unchanged;
    * the dashboard cookie **and** ``X-Requested-With: careeros-desktop``
      (the desktop shell's pages, Increment 2).
    """
    if settings.api_key is None and not _is_development():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "API_KEY is not configured. Set it in the environment before "
                "using write endpoints outside development."
            ),
        )
    if check_api_key(x_api_key):
        return
    if desktop_write_allowed(request, x_requested_with, careeros_key):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-API-Key header.",
        headers={"WWW-Authenticate": "X-API-Key"},
    )


LOGIN_PAGE = """<!doctype html>
<title>CareerOS — sign in</title>
<style>
 body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 form{background:#1a1d24;padding:2rem;border-radius:10px;min-width:320px}
 h1{font-size:1.1rem;margin:0 0 1rem}
 input{width:100%;padding:.6rem;border-radius:6px;border:1px solid #333;
       background:#0f1115;color:#e6e6e6;box-sizing:border-box}
 button{margin-top:1rem;width:100%;padding:.6rem;border:0;border-radius:6px;
        background:#3b82f6;color:#fff;font-weight:600;cursor:pointer}
 p{color:#8b93a1;font-size:.85rem}
</style>
<form method="get">
  <h1>CareerOS dashboard</h1>
  <input type="password" name="key" placeholder="API key" autofocus>
  <button type="submit">Sign in</button>
  <p>This dashboard shows personal career data. Set <code>API_KEY</code> in your environment.</p>
</form>
"""


async def require_dashboard_auth(
    request: Request,
    key: Optional[str] = Query(default=None),
    careeros_key: Optional[str] = Cookie(default=None),
) -> None:
    """Guard the HTML dashboard.

    Browsers cannot set custom headers on a plain navigation, so the key may
    arrive as ``?key=...`` once; the response then stores it in an HttpOnly
    cookie (see ``DashboardAuthMiddleware``) and the query form is no longer
    needed. Unauthenticated requests get a small sign-in page rather than a
    JSON 401, so the dashboard stays usable from a browser.
    """
    if check_api_key(key or careeros_key):
        if key:
            # Signal to the middleware that it should persist this key.
            request.state.set_dashboard_cookie = key
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=LOGIN_PAGE,
        headers={"X-CareerOS-Login": "1"},
    )


def login_response() -> HTMLResponse:
    """The sign-in page, rendered as an HTML 401."""
    return HTMLResponse(content=LOGIN_PAGE, status_code=status.HTTP_401_UNAUTHORIZED)
