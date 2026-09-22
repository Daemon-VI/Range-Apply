"""Freshness is deterministic and never invents a date."""

from datetime import datetime, timedelta, timezone

from app.jobs.freshness import classify_freshness
from app.jobs.models.enums import Freshness

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def test_thresholds():
    assert classify_freshness(NOW - timedelta(days=1), now=NOW) is Freshness.FRESH
    assert classify_freshness(NOW - timedelta(days=2), now=NOW) is Freshness.FRESH
    assert classify_freshness(NOW - timedelta(days=5), now=NOW) is Freshness.RECENT
    assert classify_freshness(NOW - timedelta(days=20), now=NOW) is Freshness.AGING
    assert classify_freshness(NOW - timedelta(days=45), now=NOW) is Freshness.STALE


def test_fallback_order_and_unknown():
    assert classify_freshness(None, source_updated_at=NOW - timedelta(days=1), now=NOW) is Freshness.FRESH
    assert classify_freshness(None, None, first_seen_at=NOW - timedelta(days=10), now=NOW) is Freshness.AGING
    assert classify_freshness(None, None, None, now=NOW) is Freshness.UNKNOWN


def test_closed_is_stale_regardless_of_age():
    assert classify_freshness(NOW - timedelta(hours=1), job_status="CLOSED", now=NOW) is Freshness.STALE
    assert classify_freshness(None, job_status="EXPIRED", now=NOW) is Freshness.STALE


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - timedelta(days=3)).replace(tzinfo=None)
    assert classify_freshness(naive, now=NOW) is Freshness.RECENT
