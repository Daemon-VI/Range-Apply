"""Request-level guards applied by ``app.main`` middleware.

Two browser-shaped attacks the per-route dependencies cannot see:

* **DNS rebinding.** Many read endpoints are deliberately open, and the server
  listens on loopback. A web page on ``http://attacker.example:8000`` whose
  name is re-pointed at ``127.0.0.1`` becomes *same-origin* with the local
  server and can read every open endpoint. The browser still sends
  ``Host: attacker.example:8000``, so in solo mode (a loopback server) only
  loopback host names are served. Extra names can be allowed with the
  ``CAREEROS_ALLOWED_HOSTS`` environment variable (comma separated); a server
  bound to a wildcard address (``0.0.0.0`` / ``::``) or running in hosted mode
  is left alone, because its public names are not known here.

* **Cross-site form posts.** ``/dashboard/*`` HTML forms are authorized by the
  dashboard cookie alone. ``SameSite=Lax`` keeps other *sites* from sending
  it, but every ``http://127.0.0.1:<port>`` page is the *same site* whatever
  the port, so another local web page could post a form. A state-changing
  request to ``/dashboard`` or ``/desktop`` whose ``Origin`` names another
  origin, or whose ``Sec-Fetch-Site`` is not ``same-origin`` / ``none``, is
  refused. Clients that send neither header (curl, tests) are unaffected;
  they still need the credential.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BROWSER_WRITE_PREFIXES = ("/dashboard", "/desktop")


def host_name(host_header: Optional[str]) -> str:
    """The bare, lower-case host name from a ``Host`` header (port and IPv6 brackets removed)."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def allowed_hosts(api_host: str, extra: Optional[str] = None) -> Optional[frozenset[str]]:
    """Host names a solo server answers; ``None`` means "do not check" (wildcard bind)."""
    bind = (api_host or "").strip().lower()
    if bind in WILDCARD_BINDS:
        return None
    names = set(LOOPBACK_HOSTS) | {host_name(bind)}
    raw = extra if extra is not None else os.environ.get("CAREEROS_ALLOWED_HOSTS", "")
    names.update(host_name(part) for part in raw.split(",") if part.strip())
    return frozenset(names)


def host_is_allowed(host_header: Optional[str], allowed: Optional[Iterable[str]]) -> bool:
    if allowed is None:
        return True
    return host_name(host_header) in set(allowed)


def cross_site_browser_write(method: str, path: str, scheme: str, headers: Mapping[str, str]) -> bool:
    """True when a browser says this state-changing page request came from another origin."""
    if method.upper() not in UNSAFE_METHODS or not path.startswith(BROWSER_WRITE_PREFIXES):
        return False
    origin = headers.get("origin")
    if origin is not None:
        host = headers.get("host") or ""
        if origin.rstrip("/").lower() != f"{scheme}://{host}".lower():
            return True
    site = headers.get("sec-fetch-site")
    if site is not None and site.lower() not in ("same-origin", "none"):
        return True
    return False
