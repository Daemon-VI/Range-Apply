"""Async token-bucket rate limiting for outbound source requests.

The ATS APIs are free and unauthenticated; staying politely under their limits
is what keeps them usable. ``settings.source_rate_limit_per_minute`` used to be
configured but never read — this module is what actually enforces it.

Buckets are per-key (one per source) and live in-process. That is the correct
scope for the current single-process free-tier deployment; a distributed
limiter would require shared state (Redis), which is explicitly deferred to the
scaling phase.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per minute, burst up to ``capacity``."""

    def __init__(self, rate_per_minute: int, capacity: Optional[int] = None):
        self.rate_per_second = max(rate_per_minute, 1) / 60.0
        self.capacity = float(capacity if capacity is not None else max(rate_per_minute, 1))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available. Returns the seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited

                deficit = tokens - self._tokens
                delay = deficit / self.rate_per_second
                waited += delay
                await asyncio.sleep(delay)


class RateLimiterRegistry:
    """Keyed collection of token buckets."""

    def __init__(self):
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, rate_per_minute: Optional[int] = None) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            async with self._lock:
                bucket = self._buckets.get(key)
                if bucket is None:
                    rate = rate_per_minute or settings.source_rate_limit_per_minute
                    bucket = TokenBucket(rate)
                    self._buckets[key] = bucket
        waited = await bucket.acquire()
        if waited > 0.1:
            logger.info("Rate limiter held '%s' for %.2fs", key, waited)
        return waited

    def reset(self) -> None:
        """Clear all buckets (used by tests)."""
        self._buckets.clear()


# Process-wide registry shared by every source adapter.
source_rate_limiter = RateLimiterRegistry()
