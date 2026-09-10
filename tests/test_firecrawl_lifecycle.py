"""Tests for FirecrawlFetcher's HTTP client lifecycle and retry behaviour.

THE REGRESSION under test: a previous version of FirecrawlFetcher.fetch only
closed the client it created itself on the *final* retry attempt, leaking a
connection pool on the success path. The fix wraps client creation/teardown
in a single try/finally around the whole retry loop. These tests pin that
down on all three exit paths (success, exhausted retries, unexpected
exception), and confirm an injected client is left alone since the caller
owns it.
"""

import httpx
import pytest

from app.config import settings
from app.jobs.fetchers.firecrawl_fetcher import FirecrawlFetcher

SUCCESS_BODY = {
    "success": True,
    "data": {"markdown": "# Job\n\nDetails here.", "metadata": {"title": "Job"}},
}


def _success_handler(request: httpx.Request):
    return httpx.Response(200, json=SUCCESS_BODY)


# ---------------------------------------------------------------------------
# Client lifecycle: self-created client is always closed
# ---------------------------------------------------------------------------


def _spy_on_aclose(monkeypatch):
    """Record every ``httpx.AsyncClient.aclose`` call without changing behaviour."""
    close_calls = []
    original_aclose = httpx.AsyncClient.aclose

    async def spy_aclose(self):
        close_calls.append(self)
        return await original_aclose(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy_aclose)
    return close_calls


def _no_real_retry_sleep(monkeypatch):
    """Stub out real waiting during a retry loop.

    ``FirecrawlFetcher``'s ``Retry-After: 0`` handling falls back to
    exponential backoff (``0`` is falsy, so ``header_value or backoff``
    picks the backoff term) plus a fixed +1s floor - real enough that these
    tests would otherwise burn several real seconds per run. ``asyncio`` is
    a singleton module, so this patches ``asyncio.sleep`` process-wide for
    the duration of the test; harmless here since nothing else in this file
    depends on real timing.
    """

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("app.jobs.fetchers.firecrawl_fetcher.asyncio.sleep", fake_sleep)


def _force_mock_transport(monkeypatch, handler):
    """Make every ``httpx.AsyncClient()`` constructed during the test route through a MockTransport.

    FirecrawlFetcher creates its client internally (``client=None``) with no
    hook to inject a transport, so the only way to observe *that specific*
    instance's lifecycle (and control what it receives) is to intercept
    construction itself.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


@pytest.mark.asyncio
async def test_self_created_client_is_closed_on_success(monkeypatch):
    close_calls = _spy_on_aclose(monkeypatch)
    _force_mock_transport(monkeypatch, _success_handler)

    fetcher = FirecrawlFetcher(api_key="test_key")
    result = await fetcher.fetch("https://example.com/job")

    assert result.success is True
    assert len(close_calls) == 1


@pytest.mark.asyncio
async def test_self_created_client_is_closed_after_exhausted_retries(monkeypatch):
    close_calls = _spy_on_aclose(monkeypatch)
    _no_real_retry_sleep(monkeypatch)

    def always_429(request: httpx.Request):
        return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"})

    _force_mock_transport(monkeypatch, always_429)

    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=1, retry_backoff=0)
    result = await fetcher.fetch("https://example.com/job")

    assert result.success is False
    assert len(close_calls) == 1


@pytest.mark.asyncio
async def test_self_created_client_is_closed_after_unexpected_exception(monkeypatch):
    close_calls = _spy_on_aclose(monkeypatch)

    def raise_unexpected(request: httpx.Request):
        # Not an httpx.HTTPStatusError/RequestError - the non-retryable
        # "unexpected error" branch in FirecrawlFetcher.fetch.
        raise RuntimeError("boom - not an httpx error, e.g. a bug in response handling")

    _force_mock_transport(monkeypatch, raise_unexpected)

    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=2)
    result = await fetcher.fetch("https://example.com/job")

    # An unexpected (non-httpx) exception is non-retryable and stops early,
    # but the client must still be released.
    assert result.success is False
    assert "boom" in result.error
    assert len(close_calls) == 1


# ---------------------------------------------------------------------------
# Injected client is never closed by the fetcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_client_is_not_closed_by_fetcher():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_success_handler))
    fetcher = FirecrawlFetcher(api_key="test_key", client=client)

    result = await fetcher.fetch("https://example.com/job")

    assert result.success is True
    assert client.is_closed is False

    await client.aclose()


@pytest.mark.asyncio
async def test_injected_client_is_not_closed_even_on_failure(monkeypatch):
    _no_real_retry_sleep(monkeypatch)

    def always_429(request: httpx.Request):
        return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(always_429))
    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=1, retry_backoff=0, client=client)

    result = await fetcher.fetch("https://example.com/job")

    assert result.success is False
    assert client.is_closed is False

    await client.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_then_200_retries_and_succeeds(monkeypatch):
    _no_real_retry_sleep(monkeypatch)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=SUCCESS_BODY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=2, retry_backoff=0, client=client)

    result = await fetcher.fetch("https://example.com/job")

    assert result.success is True
    assert "Details here" in result.content
    assert calls == 2

    await client.aclose()


@pytest.mark.asyncio
async def test_always_429_returns_failed_result_not_an_exception(monkeypatch):
    _no_real_retry_sleep(monkeypatch)

    def handler(request: httpx.Request):
        return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=2, retry_backoff=0, client=client)

    result = await fetcher.fetch("https://example.com/job")

    assert result.success is False
    assert result.error is not None
    assert "429" in result.error or "rate limited" in result.error.lower()

    await client.aclose()


# ---------------------------------------------------------------------------
# API key handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_sent_as_authorization_header_never_in_url():
    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=SUCCESS_BODY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = FirecrawlFetcher(api_key="super-secret-key", client=client)

    await fetcher.fetch("https://example.com/job")

    assert captured["authorization"] == "Bearer super-secret-key"
    assert "super-secret-key" not in captured["url"]

    await client.aclose()


# ---------------------------------------------------------------------------
# Endpoint configurability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_reads_endpoint_from_settings_firecrawl_api_url(monkeypatch):
    monkeypatch.setattr(settings, "firecrawl_api_url", "https://api.firecrawl.dev/v2/scrape")

    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=SUCCESS_BODY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # No explicit api_url passed - must pick up the (repointed) settings value.
    fetcher = FirecrawlFetcher(api_key="test_key", client=client)

    await fetcher.fetch("https://example.com/job")

    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"

    await client.aclose()
