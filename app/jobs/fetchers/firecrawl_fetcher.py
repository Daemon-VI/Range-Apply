"""Firecrawl content fetcher for clean web page / JD extraction."""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.jobs.fetchers.base import ContentFetcher, FetchResult

logger = logging.getLogger(__name__)


class FirecrawlFetcher(ContentFetcher):
    """Fetches and cleans public web pages using Firecrawl API / SDK.
    
    Supports:
    - Keyless mode (free tier via public API / IP rate limiting)
    - Keyed mode via FIRECRAWL_API_KEY
    - Bounded retries with exponential backoff
    - Markdown extraction format
    """

    DEFAULT_API_URL = "https://api.firecrawl.dev/v1/scrape"

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: Optional[int] = None,
        retry_backoff: Optional[float] = None,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_key = api_key or settings.firecrawl_api_key
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.retry_backoff = retry_backoff if retry_backoff is not None else settings.retry_backoff_base
        self.timeout = timeout
        self._client = client

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        """Scrapes a public web page using Firecrawl API with retries and exponential backoff."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "url": url,
            "formats": ["markdown", "html"],
            "onlyMainContent": True,
        }

        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            should_close_client = True

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post(
                    self.DEFAULT_API_URL,
                    json=payload,
                    headers=headers,
                )

                if response.status_code == 429:
                    # Rate limit exceeded — wait and retry with backoff
                    wait_time = (self.retry_backoff ** attempt) + 1.0
                    logger.warning("Firecrawl rate limited (429) for %s, retrying in %.1fs (attempt %d/%d)", url, wait_time, attempt + 1, self.max_retries)
                    if attempt < self.max_retries:
                        await asyncio.sleep(wait_time)
                        continue

                response.raise_for_status()
                data = response.json()

                # Extract markdown content from Firecrawl response structure
                scrape_data = data.get("data", {}) if isinstance(data, dict) else {}
                markdown_content = scrape_data.get("markdown", "") or ""
                metadata = scrape_data.get("metadata", {})

                return FetchResult(
                    url=url,
                    content=markdown_content,
                    content_type="markdown",
                    status_code=response.status_code,
                    metadata=metadata,
                    success=True,
                )

            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = str(e)
                wait_time = self.retry_backoff ** attempt
                logger.warning(
                    "Firecrawl fetch attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    url,
                    e,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(wait_time)
            except Exception as e:
                last_error = str(e)
                logger.error("Unexpected error in FirecrawlFetcher for %s: %s", url, e)
                break
            finally:
                if should_close_client and attempt == self.max_retries:
                    await client.aclose()

        if should_close_client:
            await client.aclose()

        return FetchResult(
            url=url,
            content="",
            status_code=0,
            success=False,
            error=f"Firecrawl fetch failed after {self.max_retries + 1} attempts: {last_error}",
        )
