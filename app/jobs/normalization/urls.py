"""Deterministic URL normalization for job identity resolution.

Identity must never be decided by prefix/`LIKE` matching: a posting whose URL
ends in ``/123`` is not the same job as one ending in ``/1234``. Everything here
produces a canonical string that is compared with **exact equality**, so that
class of collision is impossible by construction.
"""

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that carry campaign/analytics data rather than identity.
# Anything not listed is preserved: some boards encode the job id in the query
# (e.g. Greenhouse embedded boards use `gh_jid`), and dropping it would merge
# genuinely different postings.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gh_src",
        "lever-origin",
        "lever-source",
        "ashby_jid_source",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "referrer",
        "trk",
        "trackingid",
        "source",
        "src",
    }
)

DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Return a canonical form of ``url``, or ``None`` if it cannot be trusted.

    Normalization steps (all deterministic and idempotent):

    * lowercase the scheme and host, drop a leading ``www.``
    * drop the default port for the scheme
    * drop the fragment
    * drop tracking query parameters, sort the rest
    * strip a trailing slash from the path (the root ``/`` becomes empty)

    Returns ``None`` for empty input or anything that is not an absolute
    ``http(s)`` URL. Callers must treat ``None`` as "no identity signal" rather
    than falling back to the raw string, otherwise unparseable junk from two
    different postings could compare equal.
    """
    if not url or not url.strip():
        return None

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return None

    host = parts.hostname or ""
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    # Path case is preserved: Lever and Ashby use case-sensitive UUID segments.
    path = parts.path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def urls_equal(left: Optional[str], right: Optional[str]) -> bool:
    """True only when both URLs normalize to the same non-empty canonical form."""
    a = normalize_url(left)
    b = normalize_url(right)
    return a is not None and a == b


def escape_like(term: str, escape_char: str = "\\") -> str:
    """Escape ``%``/``_``/the escape char for safe use inside a LIKE pattern.

    Search filters still use ``LIKE``; without escaping, a user typing ``_``
    would silently match any character.
    """
    out = term.replace(escape_char, escape_char * 2)
    out = out.replace("%", f"{escape_char}%")
    out = out.replace("_", f"{escape_char}_")
    return out
