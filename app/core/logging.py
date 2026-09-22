"""Logging configuration with secret redaction.

Security rule (PRD §Security, blueprint §11.11): logs never carry passwords,
cookies, API keys, bearer tokens or session identifiers. Rather than trusting
every call site to remember, a :class:`RedactingFilter` is attached to the
root logger and scrubs known secret shapes from every record before it is
formatted. Redaction is best-effort defence in depth; the primary rule is
still "do not log the secret".

Document text (resumes, cover letters, answers) is never logged at INFO or
above by convention; DEBUG may include excerpts only in development.
"""

import logging
import re
from typing import Iterable

REDACTED = "[REDACTED]"

# Each pattern must capture the secret in group "secret" so the surrounding
# context (header name, key name) survives and the log line stays readable.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # HTTP-style headers: "X-API-Key: abc", "Authorization: Bearer abc", "Cookie: a=b; c=d".
    # The whole value is the secret, including a "Bearer " scheme prefix and
    # every cookie pair on the line.
    re.compile(
        r"(?i)\b(x-api-key|authorization|cookie|set-cookie|proxy-authorization)\s*[:=]\s*"
        r"(?P<secret>(?:bearer\s+)?[^\s,;]+(?:;\s*[^\s,;]+)*)"
    ),
    # Bearer tokens anywhere.
    re.compile(r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9\-._~+/]+=*)"),
    # key=value pairs for common secret names (query strings, env dumps, dicts).
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|token|access[_-]?token|refresh[_-]?token|session[_-]?id|careeros_key)\b\s*['\"]?\s*[:=]\s*['\"]?(?P<secret>[^\s'\",;&}]+)"
    ),
    # The dashboard sign-in query parameter ("/dashboard/?key=..."), as it
    # appears in a request line or URL.
    re.compile(r"(?i)[?&]key=(?P<secret>[^\s&#\"']+)"),
    # Well-known key prefixes.
    re.compile(r"\b(?P<secret>sk-[A-Za-z0-9\-_]{8,})\b"),
    re.compile(r"\b(?P<secret>fc-[A-Za-z0-9\-_]{8,})\b"),
    re.compile(r"\b(?P<secret>AIza[0-9A-Za-z\-_]{20,})\b"),
    re.compile(r"\b(?P<secret>gh[pousr]_[A-Za-z0-9]{20,})\b"),
    re.compile(r"\b(?P<secret>eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{5,})\b"),
)


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret replaced by ``[REDACTED]``."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def _replace(match: re.Match) -> str:
    whole = match.group(0)
    start, end = match.span("secret")
    offset = match.start()
    return whole[: start - offset] + REDACTED + whole[end - offset :]


class RedactingFilter(logging.Filter):
    """Scrub secrets from the fully formatted message before it is emitted.

    Redacting the *formatted* text (not ``msg`` and ``args`` separately) is
    what makes ``logger.info("X-API-Key: %s", key)`` safe: the header pattern
    only sees the secret once the placeholder has been substituted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            formatted = record.getMessage()
        except Exception:
            # Mismatched placeholders: fall back to scrubbing the pieces so the
            # logging module can still report the formatting error itself.
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_value(a) for a in record.args)
            return True
        scrubbed = redact(formatted)
        if scrubbed == formatted:
            return True  # nothing to hide: keep args for structured formatters
        if record.name.startswith("uvicorn.access") and isinstance(record.args, tuple):
            # uvicorn's AccessFormatter unpacks a 5-tuple of args; scrub each
            # piece in place (the request line carries "?key=...") and only
            # flatten if that was not enough.
            args = tuple(_redact_value(a) for a in record.args)
            try:
                if redact(record.msg % args) == record.msg % args:
                    record.args = args
                    return True
            except Exception:  # noqa: BLE001 - fall through to flattening
                pass
        record.msg = scrubbed
        record.args = ()
        return True


def _redact_value(value):
    return redact(value) if isinstance(value, str) else value


def configure_logging(level: str = "INFO", extra_handlers: Iterable[logging.Handler] = ()) -> None:
    """Install the root handler once, with redaction on every handler.

    Safe to call more than once (tests, reloads): the redacting filter is only
    added to a handler that does not already carry one.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    else:
        root.setLevel(level)
    for handler in list(root.handlers) + list(extra_handlers):
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
        if handler not in root.handlers:
            root.addHandler(handler)
    # uvicorn's loggers do not propagate to root and own their handlers, which
    # uvicorn's dictConfig may replace later. A *logger* filter survives that
    # (dictConfig replaces handlers, not filters) and covers records logged
    # directly on these loggers: the access line ("GET /dashboard/?key=...")
    # and uvicorn's own error messages.
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
