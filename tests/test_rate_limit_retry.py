"""Tests for rate limiting (TokenBucket / RateLimiterRegistry) and the retry
behaviour that app/jobs/sources/base.py's JobSource.fetch_json gives every
source adapter for free.

All sleeps are monkeypatched to no-ops (or use rates high enough that any
real wait is sub-millisecond) so this file runs fast and deterministically.
"""

import httpx
import pytest

from app.jobs.ratelimit import RateLimiterRegistry, TokenBucket
from app.jobs.sources.base import RETRYABLE_STATUS, SourceError
from app.jobs.sources.greenhouse import GreenhouseSource


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_bucket_hands_out_burst_immediately():
    # capacity defaults to rate_per_minute, so a 600/min bucket starts with
    # 600 tokens available with zero wait.
    bucket = TokenBucket(rate_per_minute=600)
    waited = await bucket.acquire()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_token_bucket_waits_once_burst_is_exhausted():
    # A tiny capacity bucket with a high rate: burst of 1 token, refilling
    # fast, so the forced wait for the 2nd acquire is real but only
    # milliseconds - keeping this test under budget without mocking time.
    bucket = TokenBucket(rate_per_minute=6000, capacity=1)
    first_wait = await bucket.acquire()
    assert first_wait == 0.0

    second_wait = await bucket.acquire()
    assert second_wait > 0.0
    assert second_wait < 1.0  # sanity bound so a regression can't hang the suite


# ---------------------------------------------------------------------------
# RateLimiterRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_creates_one_bucket_per_key():
    registry = RateLimiterRegistry()
    await registry.acquire("greenhouse", rate_per_minute=600)
    await registry.acquire("lever", rate_per_minute=600)

    assert set(registry._buckets.keys()) == {"greenhouse", "lever"}
    assert registry._buckets["greenhouse"] is not registry._buckets["lever"]


@pytest.mark.asyncio
async def test_registry_reuses_bucket_for_same_key():
    registry = RateLimiterRegistry()
    await registry.acquire("greenhouse", rate_per_minute=600)
    bucket_first = registry._buckets["greenhouse"]
    await registry.acquire("greenhouse", rate_per_minute=600)
    bucket_second = registry._buckets["greenhouse"]

    assert bucket_first is bucket_second


@pytest.mark.asyncio
async def test_registry_reset_clears_all_buckets():
    registry = RateLimiterRegistry()
    await registry.acquire("greenhouse", rate_per_minute=600)
    assert registry._buckets

    registry.reset()
    assert registry._buckets == {}


# ---------------------------------------------------------------------------
# Retry behaviour on JobSource.fetch_json (exercised via GreenhouseSource)
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_real_sleep(monkeypatch):
    """Retry backoff must never actually sleep.

    Patched only on ``app.jobs.sources.base``'s bound name (not module-wide,
    since ``asyncio`` is a shared singleton - patching ``asyncio.sleep``
    itself would also stub out TokenBucket's real waits used elsewhere in
    this file).
    """
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("app.jobs.sources.base.asyncio", _FakeAsyncioSleep(fake_sleep))
    return sleeps


class _FakeAsyncioSleep:
    """Proxies every ``asyncio`` attribute except ``sleep`` to the real module.

    Lets us stub out retry backoff in ``app.jobs.sources.base`` specifically,
    without touching the shared ``asyncio`` module object that TokenBucket
    (imported from ``app.jobs.ratelimit``) also relies on for real waits.
    """

    def __init__(self, fake_sleep):
        self._fake_sleep = fake_sleep

    def __getattr__(self, name):
        import asyncio as real_asyncio

        if name == "sleep":
            return self._fake_sleep
        return getattr(real_asyncio, name)


@pytest.mark.asyncio
async def test_503_twice_then_200_succeeds_after_exactly_three_requests(_no_real_sleep):
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"jobs": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GreenhouseSource(client=client, max_retries=2)

    raw_jobs = await adapter.discover("acme")
    assert raw_jobs == []
    assert calls == 3


@pytest.mark.asyncio
async def test_exhausted_retries_raise_source_error_with_status_and_attempt_count(_no_real_sleep):
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GreenhouseSource(client=client, max_retries=2)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("acme")

    assert exc_info.value.status_code == 503
    # max_retries=2 -> 3 total attempts; the message should surface that count.
    assert calls == 3
    assert "3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_404_is_terminal_and_not_retried():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GreenhouseSource(client=client, max_retries=3)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("nonexistent")

    assert exc_info.value.status_code == 404
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_after_header_honoured_over_exponential_backoff(_no_real_sleep):
    def handler(request: httpx.Request):
        return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "7"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GreenhouseSource(client=client, max_retries=1)

    with pytest.raises(SourceError):
        await adapter.discover("acme")

    # First retry must sleep exactly the Retry-After value (7s), not the
    # exponential backoff formula's value.
    assert _no_real_sleep
    assert _no_real_sleep[0] == 7.0


@pytest.mark.asyncio
async def test_non_json_200_response_raises_source_error_not_a_crash():
    def handler(request: httpx.Request):
        return httpx.Response(200, text="<html>not json</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GreenhouseSource(client=client, max_retries=2)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("acme")

    assert exc_info.value.status_code == 200


def test_retryable_status_set_includes_429_and_5xx_not_4xx_other_than_429():
    # Documents the contract fetch_json relies on: 404 is handled separately
    # (terminal), everything else in 4xx is not retried, 429/5xx are.
    assert RETRYABLE_STATUS == frozenset({429, 500, 502, 503, 504})
    assert 404 not in RETRYABLE_STATUS
    assert 400 not in RETRYABLE_STATUS
