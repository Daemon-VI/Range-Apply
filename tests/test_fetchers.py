"""Tests for ContentFetchers (Milestone 5)."""

import httpx
import pytest

from app.jobs.fetchers.firecrawl_fetcher import FirecrawlFetcher
from app.jobs.fetchers.http_fetcher import HttpFetcher


@pytest.mark.asyncio
async def test_http_fetcher_success():
    def mock_transport(request: httpx.Request):
        return httpx.Response(200, text="<html><body>Job details</body></html>", headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    fetcher = HttpFetcher(client=client)

    result = await fetcher.fetch("https://example.com/careers/job1")
    assert result.success is True
    assert result.status_code == 200
    assert "Job details" in result.content
    assert result.content_type == "html"


@pytest.mark.asyncio
async def test_firecrawl_fetcher_success():
    def mock_transport(request: httpx.Request):
        assert "api.firecrawl.dev" in str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Software Engineer\n\nWe are hiring a backend engineer.",
                    "metadata": {"title": "Software Engineer Job"},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    fetcher = FirecrawlFetcher(api_key="test_key", client=client)

    result = await fetcher.fetch("https://example.com/careers/job1")
    assert result.success is True
    assert result.content_type == "markdown"
    assert "backend engineer" in result.content


@pytest.mark.asyncio
async def test_firecrawl_fetcher_retry_on_failure():
    calls = 0

    def mock_transport(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(500, json={"error": "server error"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "Job description recovered on retry.",
                    "metadata": {},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    fetcher = FirecrawlFetcher(api_key="test_key", max_retries=2, retry_backoff=0.01, client=client)

    result = await fetcher.fetch("https://example.com/careers/job2")
    assert result.success is True
    assert "Job description recovered" in result.content
    assert calls == 2
