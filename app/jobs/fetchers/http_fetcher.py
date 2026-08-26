"""Direct HTTP content fetcher using httpx."""

import logging
from typing import Optional

import httpx

from app.jobs.fetchers.base import ContentFetcher, FetchResult

logger = logging.getLogger(__name__)


class HttpFetcher(ContentFetcher):
    """Fetches public web pages directly via HTTP with configurable timeout and retries."""

    def __init__(self, timeout: float = 15.0, client: Optional[httpx.AsyncClient] = None):
        self.timeout = timeout
        self._client = client

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RangeApply/0.2.0"},
                follow_redirects=True,
            )
            should_close_client = True

        try:
            response = await client.get(url)
            response.raise_for_status()
            content_type = "html"
            if "application/json" in response.headers.get("content-type", ""):
                content_type = "json"
            elif "text/plain" in response.headers.get("content-type", ""):
                content_type = "plain"

            return FetchResult(
                url=url,
                content=response.text,
                content_type=content_type,
                status_code=response.status_code,
                metadata=dict(response.headers),
                success=True,
            )
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP fetch status error for %s: %s", url, e)
            return FetchResult(
                url=url,
                status_code=e.response.status_code,
                success=False,
                error=str(e),
            )
        except Exception as e:
            logger.warning("HTTP fetch failed for %s: %s", url, e)
            return FetchResult(
                url=url,
                status_code=0,
                success=False,
                error=str(e),
            )
        finally:
            if should_close_client:
                await client.aclose()
