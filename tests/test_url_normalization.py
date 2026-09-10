"""Regression tests for URL normalization and LIKE-escaping.

The deduplicator used to resolve URL identity with a prefix ``LIKE`` query,
which meant a posting at ``.../jobs/123`` silently matched (and overwrote)
one at ``.../jobs/1234``. Identity is now decided by exact equality on the
output of :func:`normalize_url`. These tests pin down every normalization
rule that equality depends on, so a future change cannot reintroduce the
prefix-collision class of bug without failing here first.
"""

from app.jobs.normalization.urls import escape_like, normalize_url, urls_equal


def test_lowercases_scheme_and_host_and_strips_leading_www():
    assert normalize_url("HTTPS://WWW.Example.COM/Jobs/1") == "https://example.com/Jobs/1"
    assert normalize_url("http://WWW.boards.example.com/x") == "http://boards.example.com/x"


def test_strips_default_ports_but_keeps_non_default_port():
    assert normalize_url("https://example.com:443/jobs/1") == "https://example.com/jobs/1"
    assert normalize_url("http://example.com:80/jobs/1") == "http://example.com/jobs/1"
    # A non-default port carries identity and must survive normalization.
    assert normalize_url("https://example.com:8443/jobs/1") == "https://example.com:8443/jobs/1"
    # Cross-check: default-port and no-port forms of the same URL are equal,
    # but a non-default port is not swallowed into the same identity.
    assert urls_equal("https://example.com:443/jobs/1", "https://example.com/jobs/1")
    assert not urls_equal("https://example.com:8443/jobs/1", "https://example.com/jobs/1")


def test_drops_fragment():
    assert normalize_url("https://example.com/jobs/1#apply-section") == "https://example.com/jobs/1"
    assert urls_equal("https://example.com/jobs/1#a", "https://example.com/jobs/1#b")


def test_drops_tracking_params_but_preserves_identity_params():
    tracking = normalize_url(
        "https://example.com/jobs?utm_source=newsletter&utm_medium=email"
        "&utm_campaign=fall&fbclid=abc123&ref=homepage"
    )
    assert tracking == "https://example.com/jobs"

    # gh_jid encodes the actual job identity on embedded Greenhouse boards -
    # dropping it would merge genuinely different postings.
    with_identity = normalize_url("https://example.com/jobs?utm_source=x&gh_jid=98765")
    assert with_identity == "https://example.com/jobs?gh_jid=98765"

    # Two different jobs distinguished only by gh_jid must stay distinct.
    assert not urls_equal(
        "https://example.com/jobs?gh_jid=1",
        "https://example.com/jobs?gh_jid=2",
    )


def test_sorts_remaining_query_params_so_order_is_not_identity():
    assert normalize_url("https://example.com/jobs?b=2&a=1") == "https://example.com/jobs?a=1&b=2"
    assert urls_equal(
        "https://example.com/jobs?b=2&a=1&gh_jid=5",
        "https://example.com/jobs?gh_jid=5&a=1&b=2",
    )


def test_strips_trailing_slash():
    assert normalize_url("https://example.com/jobs/123/") == "https://example.com/jobs/123"
    assert urls_equal("https://example.com/jobs/123/", "https://example.com/jobs/123")


def test_preserves_path_case_for_case_sensitive_uuid_segments():
    # Lever and Ashby embed case-sensitive UUIDs/slugs in the path; lowercasing
    # them would merge distinct postings.
    lever_url = "https://jobs.lever.co/acme/AbC123-XyZ789"
    assert normalize_url(lever_url) == "https://jobs.lever.co/acme/AbC123-XyZ789"
    assert not urls_equal(
        "https://jobs.lever.co/acme/AbC123-XyZ789",
        "https://jobs.lever.co/acme/abc123-xyz789",
    )


def test_returns_none_for_empty_relative_or_non_http_input():
    unparseable = [
        None,
        "",
        "   ",
        "/relative/path",
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "not a url at all",
    ]
    for value in unparseable:
        assert normalize_url(value) is None

    # Two different pieces of junk must never be treated as the same identity -
    # None must mean "no identity signal", never a fallback value that two
    # unrelated postings could both produce.
    assert urls_equal(None, None) is False
    assert urls_equal("", "/relative/path") is False
    assert urls_equal("ftp://a.example.com/x", "ftp://b.example.com/x") is False
    assert urls_equal("not a url", "also not a url") is False


def test_idempotent():
    samples = [
        "HTTPS://WWW.Example.COM/Jobs/1",
        "https://example.com:443/jobs/1?b=2&a=1#frag",
        "http://example.com:80/jobs/1/",
        "https://example.com:8443/jobs/1",
        "https://jobs.lever.co/acme/AbC123-XyZ789",
        "https://example.com/jobs?utm_source=x&gh_jid=555",
    ]
    for url in samples:
        once = normalize_url(url)
        assert once is not None
        assert normalize_url(once) == once


def test_escape_like_escapes_percent_underscore_and_escape_char():
    # Without escaping, a literal '_' or '%' typed by a user acts as a LIKE
    # wildcard and matches characters it should not.
    assert escape_like("50%off") == "50\\%off"
    assert escape_like("under_score") == "under\\_score"
    assert escape_like("mixed_50%") == "mixed\\_50\\%"

    # The escape character itself must be escaped first, or a user-typed
    # backslash could be interpreted as escaping the following character.
    escaped_backslash = escape_like("a\\b")
    assert escaped_backslash == "a\\\\b"

    # Order matters: escaping the backslash must happen before % and _ are
    # escaped, otherwise the backslashes just introduced would themselves get
    # re-escaped (or worse, disarm the escaping of the following character).
    combined = escape_like("100%_off\\deal")
    assert combined == "100\\%\\_off\\\\deal"
