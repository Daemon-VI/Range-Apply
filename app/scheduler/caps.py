"""Daily / weekly application caps.

**What counts.** One *application attempt* (an ``applications`` row that
holds a reservation) counts once toward the day and the ISO week in which
the scheduler admitted it, measured in the tenant's policy timezone.
Discovery, matching, priority and preparation never count. Retries of the
same attempt never count again. A released attempt (cancelled, permanently
failed, opportunity closed, company blocked) gives its slot back to the
same period it was taken from. ``cap == 0`` means *paused*: nothing is
admitted until the candidate raises it.

**Why a ledger.** Counting rows and then inserting is a race: two workers
can both count 99 and both insert. The ledger row is incremented with a
single conditional ``UPDATE … WHERE reserved - released < cap``; the
database serialises that statement on both SQLite (single writer) and
PostgreSQL (row lock), so exactly ``cap`` reservations can ever succeed in
a period no matter how many workers run.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timeutils import UTC, db_now
from app.pipeline.models import AdmissionReason
from app.scheduler.database.models import ApplicationCapLedgerRow

DAY = "DAY"
WEEK = "WEEK"


def tz_for(name: Optional[str]) -> tzinfo:
    """The tenant's zone; UTC when the name is unknown or no tz database is installed.

    Windows has no system tz database, so ``tzdata`` (pure Python) is a
    declared dependency; this fallback only guards a broken install.
    """
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


@dataclass(frozen=True)
class Period:
    kind: str
    key: str
    starts_at: datetime  # aware UTC
    ends_at: datetime  # aware UTC, exclusive


@dataclass(frozen=True)
class PeriodKeys:
    day: Period
    week: Period
    now_local: datetime


def period_keys(now_utc: datetime, timezone: Optional[str]) -> PeriodKeys:
    """Day and ISO-week periods containing ``now_utc`` in the tenant's zone.

    Boundaries are local midnight and local Monday 00:00; DST is handled by
    ``zoneinfo`` so a 23- or 25-hour day is still one day.
    """
    zone = tz_for(timezone)
    local = now_utc.astimezone(zone)
    day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = (day_start_local + timedelta(days=1, hours=2)).replace(hour=0)
    iso_year, iso_week, iso_weekday = local.isocalendar()
    week_start_local = (day_start_local - timedelta(days=iso_weekday - 1)).replace(hour=0)
    week_end_local = (week_start_local + timedelta(days=7, hours=2)).replace(hour=0)
    day = Period(DAY, local.date().isoformat(), day_start_local.astimezone(UTC), day_end_local.astimezone(UTC))
    week = Period(
        WEEK,
        f"{iso_year}-W{iso_week:02d}",
        week_start_local.astimezone(UTC),
        week_end_local.astimezone(UTC),
    )
    return PeriodKeys(day=day, week=week, now_local=local)


class CapLedger:
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        # Periods whose ledger row this instance has already seen: saves one
        # SELECT per reservation. The row is never deleted, so this is safe.
        self._ensured: set[tuple[str, str]] = set()

    # ---------------------------------------------------------------- reads

    def usage(self, kind: str, key: str) -> int:
        row = self._get(kind, key)
        return 0 if row is None else max(0, row.reserved - row.released)

    def _get(self, kind: str, key: str) -> Optional[ApplicationCapLedgerRow]:
        return (
            self.db.query(ApplicationCapLedgerRow)
            .filter(
                ApplicationCapLedgerRow.tenant_id == self.tenant_id,
                ApplicationCapLedgerRow.period_kind == kind,
                ApplicationCapLedgerRow.period_key == key,
            )
            .first()
        )

    # --------------------------------------------------------------- writes

    def _ensure(self, kind: str, key: str) -> None:
        if (kind, key) in self._ensured:
            return
        if self._get(kind, key) is None:
            try:
                with self.db.begin_nested():
                    self.db.add(
                        ApplicationCapLedgerRow(
                            tenant_id=self.tenant_id, period_kind=kind, period_key=key, reserved=0, released=0
                        )
                    )
                    self.db.flush()
            except IntegrityError:
                # Another worker created it between our read and insert; fine.
                pass
        self._ensured.add((kind, key))

    def _increment(self, kind: str, key: str, cap: Optional[int]) -> bool:
        """Atomically take one slot; False when the period is full."""
        query = self.db.query(ApplicationCapLedgerRow).filter(
            ApplicationCapLedgerRow.tenant_id == self.tenant_id,
            ApplicationCapLedgerRow.period_kind == kind,
            ApplicationCapLedgerRow.period_key == key,
        )
        if cap is not None:
            query = query.filter(ApplicationCapLedgerRow.reserved - ApplicationCapLedgerRow.released < cap)
        updated = query.update(
            {
                ApplicationCapLedgerRow.reserved: ApplicationCapLedgerRow.reserved + 1,
                ApplicationCapLedgerRow.updated_at: db_now(),
            },
            synchronize_session=False,
        )
        return updated == 1

    def _decrement(self, kind: str, key: str) -> None:
        self.db.query(ApplicationCapLedgerRow).filter(
            ApplicationCapLedgerRow.tenant_id == self.tenant_id,
            ApplicationCapLedgerRow.period_kind == kind,
            ApplicationCapLedgerRow.period_key == key,
        ).update(
            {ApplicationCapLedgerRow.reserved: ApplicationCapLedgerRow.reserved - 1, ApplicationCapLedgerRow.updated_at: db_now()},
            synchronize_session=False,
        )

    def reserve(self, keys: PeriodKeys, daily_cap: int, weekly_cap: int) -> Optional[AdmissionReason]:
        """Take one day slot and one week slot together; ``None`` on success.

        A cap of 0 is *paused* and refuses before touching the ledger. When
        the week is full the day slot just taken is handed back, so the two
        counters never drift apart.
        """
        if daily_cap <= 0:
            return AdmissionReason.DAILY_CAP_REACHED
        if weekly_cap <= 0:
            return AdmissionReason.WEEKLY_CAP_REACHED
        self._ensure(DAY, keys.day.key)
        self._ensure(WEEK, keys.week.key)
        if not self._increment(DAY, keys.day.key, daily_cap):
            return AdmissionReason.DAILY_CAP_REACHED
        if not self._increment(WEEK, keys.week.key, weekly_cap):
            self._decrement(DAY, keys.day.key)
            return AdmissionReason.WEEKLY_CAP_REACHED
        return None

    def release(self, day_key: Optional[str], week_key: Optional[str]) -> None:
        """Give a reservation back to the periods it was taken from."""
        for kind, key in ((DAY, day_key), (WEEK, week_key)):
            if not key:
                continue
            self._ensure(kind, key)
            self.db.query(ApplicationCapLedgerRow).filter(
                ApplicationCapLedgerRow.tenant_id == self.tenant_id,
                ApplicationCapLedgerRow.period_kind == kind,
                ApplicationCapLedgerRow.period_key == key,
            ).update(
                {ApplicationCapLedgerRow.released: ApplicationCapLedgerRow.released + 1, ApplicationCapLedgerRow.updated_at: db_now()},
                synchronize_session=False,
            )
