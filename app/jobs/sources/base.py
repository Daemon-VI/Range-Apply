"""Abstract base class for all job source adapters.

Every adapter gets, for free, from :meth:`JobSource.fetch_json`:

* per-source token-bucket rate limiting (``app/jobs/ratelimit.py``);
* bounded retries with exponential backoff **plus jitter**, honouring
  ``Retry-After`` on 429;
* explicit handling of 404 (board gone), 403 (forbidden: never retried,
  never hammered), 429 (rate limited) and 5xx (server error);
* a per-source **circuit breaker**: after ``SOURCE_CIRCUIT_FAILURE_THRESHOLD``
  consecutive terminal failures the source is skipped for
  ``SOURCE_CIRCUIT_OPEN_SECONDS`` instead of being retried on every run;
* request accounting (``adapter.stats``) the discovery run records as metrics.

Failure classes are on ``SourceError.kind`` so the pipeline can record *why*
a source failed without parsing messages.
"""

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.ratelimit import source_rate_limiter

logger = logging.getLogger(__name__)

USER_AGENT = "RangeApply/0.5 (+https://github.com/granzer69/Range-Apply)"

# Transient conditions worth retrying. 429 is handled separately so the
# Retry-After header can be honoured.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

KIND_NOT_FOUND = "not_found"
KIND_FORBIDDEN = "forbidden"
KIND_RATE_LIMITED = "rate_limited"
KIND_SERVER_ERROR = "server_error"
KIND_NETWORK = "network"
KIND_INVALID_RESPONSE = "invalid_response"
KIND_CIRCUIT_OPEN = "circuit_open"
KIND_HTTP_ERROR = "http_error"


class SourceError(Exception):
    """Raised when a job source fails to discover or fetch jobs."""

    def __init__(
        self,
        message: str,
        source: JobSourceType,
        identifier: str,
        status_code: Optional[int] = None,
        kind: str = KIND_HTTP_ERROR,
    ):
        super().__init__(message)
        self.source = source
        self.identifier = identifier
        self.status_code = status_code
        self.kind = kind


@dataclass
class RequestStats:
    """What one adapter instance did on the network (recorded per run)."""

    requests: int = 0
    retries: int = 0
    rate_limit_hits: int = 0
    server_errors: int = 0
    network_errors: int = 0
    seconds_waited: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limit_hits": self.rate_limit_hits,
            "server_errors": self.server_errors,
            "network_errors": self.network_errors,
            "seconds_waited": round(self.seconds_waited, 3),
        }


@dataclass
class _Circuit:
    consecutive_failures: int = 0
    open_until: float = 0.0


class CircuitBreakerRegistry:
    """In-process, per-source-type circuit state.

    Scope matches the rate limiter: a single process on a free tier. A
    source-identifier-level breaker would be more precise but the failure
    modes that matter (an ATS throttling us, an outage) are per host.
    """

    def __init__(self):
        self._circuits: Dict[str, _Circuit] = {}

    def is_open(self, key: str, now: Optional[float] = None) -> Optional[float]:
        circuit = self._circuits.get(key)
        if circuit is None:
            return None
        now = now if now is not None else time.monotonic()
        if circuit.open_until > now:
            return circuit.open_until - now
        return None

    def record_failure(self, key: str, threshold: int, open_seconds: float) -> bool:
        circuit = self._circuits.setdefault(key, _Circuit())
        circuit.consecutive_failures += 1
        if circuit.consecutive_failures >= threshold:
            circuit.open_until = time.monotonic() + open_seconds
            logger.warning(
                "Circuit opened for %s after %d consecutive failures (%.0fs)",
                key,
                circuit.consecutive_failures,
                open_seconds,
            )
            return True
        return False

    def record_success(self, key: str) -> None:
        self._circuits.pop(key, None)

    def reset(self) -> None:
        self._circuits.clear()

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        now = time.monotonic()
        return {
            key: {
                "consecutive_failures": c.consecutive_failures,
                "open_for_seconds": max(0.0, c.open_until - now),
            }
            for key, c in self._circuits.items()
        }


source_circuits = CircuitBreakerRegistry()


class JobSource(ABC):
    """Abstract interface for all job source adapters.

    Each adapter encapsulates its own discovery and fetching logic,
    returning uniform RawJob instances without core pipeline branching.
    """

    #: True when ``discover`` accepts ``geography=`` and filters at the source.
    #: None of the built-in public APIs can (probed 2026-09-14): Greenhouse and
    #: Ashby ignore any location parameter, and Lever's ``location=`` matches
    #: only the board's exact literal strings ("Hyderabad" misses "Hyderabad,
    #: Telangana"), so those boards are fetched whole and filtered client-side
    #: by the discovery service before ingestion.
    supports_geography: bool = False

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
        self.stats = RequestStats()

    @property
    @abstractmethod
    def source_type(self) -> JobSourceType:
        """Returns the JobSourceType for this adapter."""
        pass

    @abstractmethod
    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers and retrieves raw job postings for the given identifier."""
        pass

    async def fetch_json(self, url: str, identifier: str) -> Any:
        """GET ``url`` and decode JSON, with rate limiting, retries and a breaker.

        A 404 and a 403 are terminal (the board does not exist / we are not
        welcome); 429/5xx are retried with exponential backoff and jitter up
        to ``max_retries``. Every failure path raises :class:`SourceError`
        with a ``kind`` so the pipeline can isolate one source's outage from
        the rest of the run.
        """
        key = self.source_type.value
        remaining = source_circuits.is_open(key)
        if remaining is not None:
            raise SourceError(
                f"{key} circuit is open for another {remaining:.0f}s after repeated failures",
                source=self.source_type,
                identifier=identifier,
                kind=KIND_CIRCUIT_OPEN,
            )

        should_close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT})
            should_close = True

        last_error: Optional[str] = None
        last_status: Optional[int] = None
        last_kind = KIND_SERVER_ERROR

        try:
            for attempt in range(self.max_retries + 1):
                self.stats.seconds_waited += await source_rate_limiter.acquire(
                    key, self.rate_limit_per_minute
                )
                self.stats.requests += 1
                if attempt:
                    self.stats.retries += 1
                try:
                    response = await client.get(url)
                except httpx.RequestError as exc:
                    self.stats.network_errors += 1
                    last_error = f"request failed: {exc}"
                    last_kind = KIND_NETWORK
                    if attempt >= self.max_retries:
                        break
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                if response.status_code == 404:
                    raise SourceError(
                        f"{key} board not found: '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=404,
                        kind=KIND_NOT_FOUND,
                    )

                if response.status_code in (401, 403):
                    # Never retried: hammering a source that refused us is
                    # exactly how a free tier gets blocked for good.
                    self._record_failure()
                    raise SourceError(
                        f"{key} refused access ({response.status_code}) for '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=response.status_code,
                        kind=KIND_FORBIDDEN,
                    )

                if response.status_code in RETRYABLE_STATUS:
                    last_status = response.status_code
                    last_error = f"HTTP {response.status_code}"
                    if response.status_code == 429:
                        self.stats.rate_limit_hits += 1
                        last_kind = KIND_RATE_LIMITED
                    else:
                        self.stats.server_errors += 1
                        last_kind = KIND_SERVER_ERROR
                    if attempt >= self.max_retries:
                        break
                    # `or` would discard a legitimate 'Retry-After: 0'
                    # (retry immediately), because 0.0 is falsy.
                    retry_after = self._retry_after(response)
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    logger.warning(
                        "%s returned %s for '%s', retrying in %.1fs (attempt %d/%d)",
                        key,
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
                        f"{key} HTTP error {exc.response.status_code} for '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=exc.response.status_code,
                        kind=KIND_HTTP_ERROR,
                    ) from exc

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SourceError(
                        f"{key} returned non-JSON content for '{identifier}'",
                        source=self.source_type,
                        identifier=identifier,
                        status_code=response.status_code,
                        kind=KIND_INVALID_RESPONSE,
                    ) from exc
                source_circuits.record_success(key)
                return payload

            self._record_failure()
            raise SourceError(
                f"{key} request failed for '{identifier}' after "
                f"{self.max_retries + 1} attempts: {last_error}",
                source=self.source_type,
                identifier=identifier,
                status_code=last_status,
                kind=last_kind,
            )
        finally:
            if should_close:
                await client.aclose()

    def _record_failure(self) -> None:
        source_circuits.record_failure(
            self.source_type.value,
            settings.source_circuit_failure_threshold,
            settings.source_circuit_open_seconds,
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with up to 25% jitter so retries de-synchronise."""
        base = float(settings.retry_backoff_base**attempt)
        return base * (1.0 + random.uniform(0.0, settings.retry_jitter_ratio))

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


@dataclass
class DiscoveryTargetSpec:
    """One board to poll, as passed to ``JobDiscoveryService.run_many``."""

    source: JobSourceType
    identifier: str
    company_name: Optional[str] = None
    title_include: List[str] = field(default_factory=list)
    resume_from_run_id: Optional[str] = None
