"""Timezone-correct time handling.

Rules for the whole codebase:

* ``utc_now()`` replaces ``datetime.utcnow()`` (deprecated in Python 3.12).
* Domain models (Pydantic) carry **timezone-aware UTC** datetimes.
* The database stores **naive UTC** datetimes, because the existing schema uses
  ``DateTime`` (``TIMESTAMP WITHOUT TIME ZONE`` on PostgreSQL) and SQLite has no
  native timezone type at all. ``to_db()``/``from_db()`` are the only sanctioned
  boundary conversions, so "naive" never means "unknown timezone" here.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Attach UTC to a naive datetime; convert an aware one to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_db(value: Optional[datetime]) -> Optional[datetime]:
    """Convert a datetime to the naive-UTC form used for persistence."""
    aware = ensure_aware(value)
    return None if aware is None else aware.replace(tzinfo=None)


def from_db(value: Optional[datetime]) -> Optional[datetime]:
    """Read a persisted naive-UTC datetime back as timezone-aware UTC."""
    return ensure_aware(value)


def db_now() -> datetime:
    """Naive-UTC 'now', for SQLAlchemy column defaults."""
    return datetime.now(UTC).replace(tzinfo=None)


def parse_timestamp(value) -> Optional[datetime]:
    """Best-effort parse of a source timestamp into aware UTC.

    Handles the three shapes the ATS adapters actually return:

    * ISO-8601 strings, including a trailing ``Z`` (Greenhouse, Ashby)
    * epoch milliseconds as int/float (Lever ``createdAt``)
    * epoch seconds as int/float

    Returns ``None`` for anything unparseable — a missing date is always
    preferable to a fabricated one.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return ensure_aware(value)

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return _from_epoch(value)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return _from_epoch(int(raw))
        iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            return ensure_aware(datetime.fromisoformat(iso))
        except ValueError:
            return None

    return None


def _from_epoch(value) -> Optional[datetime]:
    """Interpret a number as epoch seconds or milliseconds, whichever is sane."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Anything past ~2001 in milliseconds exceeds 1e12; below that treat as seconds.
    if number > 1e11:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def age(value: Optional[datetime], now: Optional[datetime] = None) -> Optional[timedelta]:
    """Age of a timestamp relative to now, or ``None`` if unknown."""
    aware = ensure_aware(value)
    if aware is None:
        return None
    return (now or utc_now()) - aware
