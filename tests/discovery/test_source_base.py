"""Adapter hardening: failure kinds, no retry on 403, jitter, circuit breaker, stats."""

import httpx
import pytest

from app.config import settings
from app.jobs.sources.base import (
    KIND_CIRCUIT_OPEN,
    KIND_FORBIDDEN,
    KIND_NOT_FOUND,
    KIND_RATE_LIMITED,
    KIND_SERVER_ERROR,
    SourceError,
    source_circuits,
)
from app.jobs.sources.greenhouse import GreenhouseSource


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    import asyncio as real_asyncio

    class _Proxy:
        def __getattr__(self, name):
            return fake_sleep if name == "sleep" else getattr(real_asyncio, name)

    monkeypatch.setattr("app.jobs.sources.base.asyncio", _Proxy())
    return sleeps


def _adapter(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GreenhouseSource(client=client, **kwargs)


@pytest.mark.asyncio
async def test_403_is_terminal_forbidden_and_counts_toward_circuit():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(SourceError) as exc:
        await _adapter(handler, max_retries=3).discover("acme")
    assert exc.value.kind == KIND_FORBIDDEN and exc.value.status_code == 403
    assert calls == 1, "a refusal is never retried"
    assert source_circuits.snapshot()["GREENHOUSE"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_kinds_for_404_429_and_5xx(no_sleep):
    with pytest.raises(SourceError) as exc:
        await _adapter(lambda r: httpx.Response(404)).discover("gone")
    assert exc.value.kind == KIND_NOT_FOUND

    adapter = _adapter(lambda r: httpx.Response(429, headers={"Retry-After": "1"}), max_retries=1)
    with pytest.raises(SourceError) as exc:
        await adapter.discover("busy")
    assert exc.value.kind == KIND_RATE_LIMITED
    assert adapter.stats.rate_limit_hits == 2 and adapter.stats.requests == 2 and adapter.stats.retries == 1

    with pytest.raises(SourceError) as exc:
        await _adapter(lambda r: httpx.Response(502), max_retries=0).discover("down")
    assert exc.value.kind == KIND_SERVER_ERROR


@pytest.mark.asyncio
async def test_backoff_has_bounded_jitter(no_sleep, monkeypatch):
    monkeypatch.setattr(settings, "retry_backoff_base", 2.0)
    monkeypatch.setattr(settings, "retry_jitter_ratio", 0.25)
    adapter = _adapter(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(SourceError):
        await adapter.discover("flaky")
    assert len(no_sleep) == 2
    assert 1.0 <= no_sleep[0] <= 1.25  # 2**0 * (1 + jitter)
    assert 2.0 <= no_sleep[1] <= 2.5  # 2**1 * (1 + jitter)


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_and_resets_on_success(no_sleep, monkeypatch):
    monkeypatch.setattr(settings, "source_circuit_failure_threshold", 2)
    monkeypatch.setattr(settings, "source_circuit_open_seconds", 600.0)
    calls = 0

    def failing(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    for _ in range(2):
        with pytest.raises(SourceError):
            await _adapter(failing, max_retries=0).discover("acme")
    assert calls == 2
    assert source_circuits.snapshot()["GREENHOUSE"]["open_for_seconds"] > 0

    with pytest.raises(SourceError) as exc:
        await _adapter(failing, max_retries=0).discover("acme")
    assert exc.value.kind == KIND_CIRCUIT_OPEN
    assert calls == 2, "an open circuit makes no network request"

    source_circuits.reset()
    ok = _adapter(lambda r: httpx.Response(200, json={"jobs": []}))
    assert await ok.discover("acme") == []
    assert "GREENHOUSE" not in source_circuits.snapshot()
    assert ok.stats.requests == 1 and ok.stats.retries == 0
