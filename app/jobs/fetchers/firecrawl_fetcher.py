"""Firecrawl content fetcher for clean web page / JD extraction.

Role (per the PRD): Firecrawl is the **public-web intelligence layer** used only
where a structured ATS API does not cover a company's postings. It is never
required for the Greenhouse/Lever/Ashby path, and never used for application
submission.

Talks to the REST endpoint over ``httpx`` rather than the ``firecrawl-py`` SDK,
so the base install stays free of an extra dependency and the API version can
be repointed via ``FIRECRAWL_API_URL`` without a code change.
"""

import asyncio
import logging
from typing import Dict, Optional

import httpx

from app.config import settings
from app.jobs.fetchers.base import ContentFetcher, FetchResult
from app.jobs.ratelimit import source_rate_limiter

logger = logging.getLogger(__name__)


class FirecrawlFetcher(ContentFetcher):
    """Fetches and cleans public web pages using the Firecrawl scrape API.

    Supports:

    * Keyless mode (free tier via IP-based rate limiting)
    * Keyed mode via ``FIRECRAWL_API_KEY``
    * Bounded retries with exponential backoff, honouring ``Retry-After``
    * Markdown extraction format
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: Optional[int] = None,
        retry_backoff: Optional[float] = None,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
        api_url: Optional[str] = None,
        rate_limit_per_minute: Optional[int] = None,
    ):
        self.api_key = api_key or settings.firecrawl_api_key
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.retry_backoff = (
            retry_backoff if retry_backoff is not None else settings.retry_backoff_base
        )
        self.timeout = timeout
        self._client = client
        self.api_url = api_url or settings.firecrawl_api_url
        self.rate_limit_per_minute = rate_limit_per_minute or settings.firecrawl_rate_limit

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        """Scrape a public web page, retrying transient failures.

        The HTTP client is created and closed in a single ``try/finally`` around
        the whole retry loop, so it is released on **every** path — success,
        exhausted retries, or an unexpected error. The previous version only
        closed it on the final attempt, leaking a connection pool on success.
        """
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            # Sent as a header, never in the URL, so the key cannot leak into
            # access logs or exception messages.
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {"url": url, "formats": ["markdown", "html"], "onlyMainContent": True}

        client = self._client
        should_close_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)

        last_error: Optional[str] = None

        try:
            for attempt in range(self.max_retries + 1):
                # Firecrawl's free tier is IP rate limited; stay under it.
                await source_rate_limiter.acquire("firecrawl", self.rate_limit_per_minute)
                try:
                    response = await client.post(self.api_url, json=payload, headers=headers)

                    if response.status_code == 429:
                        last_error = "HTTP 429 (rate limited)"
                        if attempt >= self.max_retries:
                            break
                        # `or` would discard a legitimate 'Retry-After: 0'
                        # (retry immediately), because 0.0 is falsy.
                        retry_after = self._retry_after(response)
                        wait_time = (
                            retry_after
                            if retry_after is not None
                            else (self.retry_backoff**attempt) + 1.0
                        )
                        logger.warning(
                            "Firecrawl rate limited for %s, retrying in %.1fs (attempt %d/%d)",
                            url,
                            wait_time,
                            attempt + 1,
                            self.max_retries,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    response.raise_for_status()
                    data = response.json()

                    scrape_data = data.get("data", {}) if isinstance(data, dict) else {}
                    return FetchResult(
                        url=url,
                        content=scrape_data.get("markdown", "") or "",
                        content_type="markdown",
                        status_code=response.status_code,
                        metadata=scrape_data.get("metadata", {}),
                        success=True,
                    )

                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    last_error = str(exc)
                    logger.warning(
                        "Firecrawl fetch attempt %d/%d failed for %s: %s",
                        attempt + 1,
                        self.max_retries + 1,
                        url,
                        exc,
                    )
                    if attempt >= self.max_retries:
                        break
                    await asyncio.sleep(self.retry_backoff**attempt)
                except Exception as exc:  # noqa: BLE001 - non-retryable, stop early
                    last_error = str(exc)
                    logger.error("Unexpected error in FirecrawlFetcher for %s: %s", url, exc)
                    break

            return FetchResult(
                url=url,
                content="",
                status_code=0,
                success=False,
                error=(
                    f"Firecrawl fetch failed after {self.max_retries + 1} attempts: {last_error}"
                ),
            )
        finally:
            if should_close_client:
                await client.aclose()

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None
