"""Secrets must never reach a log line, whatever shape they arrive in."""

import logging

import pytest

from app.core.logging import REDACTED, RedactingFilter, configure_logging, redact


@pytest.mark.parametrize(
    "line, must_not_contain",
    [
        ("X-API-Key: abc123secret", "abc123secret"),
        ("x-api-key=abc123secret", "abc123secret"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature", "eyJhbGciOiJIUzI1NiJ9"),
        ("Cookie: careeros_key=verysecretcookie; other=1", "verysecretcookie"),
        ("api_key='sk-1234567890abcdef'", "sk-1234567890abcdef"),
        ('{"password": "hunter2"}', "hunter2"),
        ("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
        ("firecrawl key fc-abcdefghijklmnop failed", "fc-abcdefghijklmnop"),
        ("gemini AIzaSyA1234567890abcdefghijklmnop", "AIzaSyA1234567890abcdefghijklmnop"),
        ("session_id: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    ],
)
def test_redact_scrubs_secret_shapes(line, must_not_contain):
    out = redact(line)
    assert must_not_contain not in out
    assert REDACTED in out


def test_redact_keeps_context_readable():
    assert redact("X-API-Key: abc123") == f"X-API-Key: {REDACTED}"
    assert redact("nothing secret here, job 42 discovered") == "nothing secret here, job 42 discovered"


def test_filter_scrubs_message_and_args(caplog):
    logger = logging.getLogger("careeros.test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="careeros.test.redaction"):
        logger.info("calling with X-API-Key: %s and %s", "topsecretvalue", "Bearer abcdefghijkl")
    text = caplog.text
    assert "topsecretvalue" not in text
    assert "abcdefghijkl" not in text
    assert REDACTED in text


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO")
    root = logging.getLogger()
    for handler in root.handlers:
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
