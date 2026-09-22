"""Deterministic freshness classification (blueprint §7).

The reference date is, in order of trust: the source's ``posted_at``, then
the source's ``updated_at``, then when we first saw the posting. A job with
none of those is UNKNOWN, never guessed. Closed jobs are STALE regardless of
age. Thresholds are days and are the only tunables.
"""

from datetime import datetime
from typing import Optional

from app.core.timeutils import ensure_aware, utc_now
from app.jobs.models.enums import Freshness

FRESH_DAYS = 2
RECENT_DAYS = 7
AGING_DAYS = 30


def classify_freshness(
    posted_at: Optional[datetime],
    source_updated_at: Optional[datetime] = None,
    first_seen_at: Optional[datetime] = None,
    job_status: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Freshness:
    if job_status in ("CLOSED", "EXPIRED"):
        return Freshness.STALE
    reference = ensure_aware(posted_at) or ensure_aware(source_updated_at) or ensure_aware(first_seen_at)
    if reference is None:
        return Freshness.UNKNOWN
    age_days = (ensure_aware(now) or utc_now()) - reference
    days = age_days.total_seconds() / 86400.0
    if days <= FRESH_DAYS:
        return Freshness.FRESH
    if days <= RECENT_DAYS:
        return Freshness.RECENT
    if days <= AGING_DAYS:
        return Freshness.AGING
    return Freshness.STALE


def freshness_for_row(job, now: Optional[datetime] = None) -> Freshness:
    """Classify a ``JobRow`` (naive-UTC columns handled by ensure_aware)."""
    return classify_freshness(job.posted_at, job.source_updated_at, job.first_seen_at, job.job_status, now)
