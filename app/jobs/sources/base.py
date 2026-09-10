"""Abstract base class for all job source adapters."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, List, Optional

import httpx

from app.config import settings
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.ratelimit import source_rate_limiter

logger = logging.getLogger(__name__)

USER_AGENT = "RangeApply/0.4 (+https://github.com/granzer69/Range-Apply)"

# Transient conditions worth retrying. 429 is handled separately so the
# Retry-After header can be honoured.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class SourceError(Exception):
    """Raised when a job source fails to discover or fetch jobs."""

    def __init__(
        self,
        message: str,
        source: JobSourceType,
        identifier: str,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.source = source
        self.identifier = identifier
        self.status_code = status_code


class JobSource(ABC):
    """Abstract interface for all job source adapters.

    Each adapter encapsulates its own discovery and fetching logic,
    returning uniform RawJob instances without core pipeline branching.

    Subclasses get rate limiting, bounded retries with exponential backoff and
    timeout handling for free via :meth:`fetch_json`.
    """

    def __init__(
        self,
        timeout: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
        max_retries: Optional[int] = None,
        rate_limit_per_minute: Optional[int] = None,
    ):
        self.timeout = timeout if timeout is not None else settings.source_request_timeout
        self._client = client
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.rate_limit_per_minute = rate_limit_per_minute

    @property
    @abstractmethod
    def source_type(self) -> JobSourceType:
        """Returns the JobSourceType for this adapter."""
        pass

    @abstractmethod
    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers and retrieves raw job postings for the given identifier.

        Args:
            identifier: Board token, company slug, or endpoint identifier.
            **kwargs: Optional adapter-specific parameters.

        Returns:
            List of RawJob instances preserving all original content and metadata.
        """
        pass

    async def fetch_json(self, url: str, identifier: str) -> Any:
        """GET ``url`` and decode JSON, with rate limiting and bounded retries.

        A 404 is terminal (the board does not exist); 429/5xx are retried with
        exponential backoff up to ``max_retries``. Every failure path raises
        :class:`SourceError` so the pipeline can isolate one source's outage
        from the rest of the run.
        """
        should_close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT})
            should_close = True

        last_error: Optional[str] = None
        last_status: Optional[int] = None

        try:
            for attempt in range(self.max_retries + 1):
                await source_rate_limiter.acquire(
                    self.source_type.value, self.rate_limit_per_minute
                )
                try:
                    response = await client.get(url)
                except httpx.RequestError as exc:
                    last_error = f"request failed: {exc}"
                    if attempt >= self.max_retries:
                        break
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                if response.status_code == 404:
                    raise SourceError(
                        f"{self.source_type.value} board not found: '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=404,
                    )

                if response.status_code in RETRYABLE_STATUS:
                    last_status = response.status_code
                    last_error = f"HTTP {response.status_code}"
                    if attempt >= self.max_retries:
                        break
                    # `or` would discard a legitimate 'Retry-After: 0'
                    # (retry immediately), because 0.0 is falsy.
                    retry_after = self._retry_after(response)
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    logger.warning(
                        "%s returned %s for '%s', retrying in %.1fs (attempt %d/%d)",
                        self.source_type.value,
                        response.status_code,
                        identifier,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SourceError(
                        f"{self.source_type.value} HTTP error {exc.response.status_code} "
                        f"for '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=exc.response.status_code,
                    ) from exc

                try:
                    return response.json()
                except ValueError as exc:
                    raise SourceError(
                        f"{self.source_type.value} returned non-JSON content for '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=response.status_code,
                    ) from exc

            raise SourceError(
                f"{self.source_type.value} request failed for '{identifier}' after "
                f"{self.max_retries + 1} attempts: {last_error}",
                source=self.source_type,
                identifier=identifier,
                status_code=last_status,
            )
        finally:
            if should_close:
                await client.aclose()

    def _backoff(self, attempt: int) -> float:
        return float(settings.retry_backoff_base**attempt)

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        """Honour a numeric ``Retry-After`` header when the source sends one."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None
