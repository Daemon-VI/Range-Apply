"""Source reliability as data, and the source-level polling checkpoint.

Updated once per discovery run from the run's terminal state. Nothing here
decides whether a job is ingested; later phases read ``success rate`` and
``last_fresh_posted_at`` into priority, and the scheduler reads
``next_poll_at`` to poll only what is due.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.core.timeutils import db_now
from app.jobs.database.models import DiscoveryRunRow, SourceHealthRow
from app.jobs.models.enums import JobSourceType
from app.jobs.sources.base import KIND_CIRCUIT_OPEN, KIND_RATE_LIMITED

logger = logging.getLogger(__name__)

MAX_BACKOFF_MINUTES = 24 * 60


class SourceHealthRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, source: JobSourceType | str, identifier: str) -> Optional[SourceHealthRow]:
        source_value = source.value if isinstance(source, JobSourceType) else source
        return (
            self.db.query(SourceHealthRow)
            .filter(
                SourceHealthRow.source == source_value,
                SourceHealthRow.source_identifier == identifier,
            )
            .first()
        )

    def upsert_target(
        self,
        source: JobSourceType | str,
        identifier: str,
        company_name: Optional[str] = None,
        poll_interval_minutes: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> SourceHealthRow:
        row = self.get(source, identifier)
        if row is None:
            row = SourceHealthRow(
                source=source.value if isinstance(source, JobSourceType) else source,
                source_identifier=identifier,
                company_name=company_name,
                poll_interval_minutes=poll_interval_minutes or settings.discovery_default_poll_minutes,
                next_poll_at=db_now(),
            )
            self.db.add(row)
        else:
            if company_name:
                row.company_name = company_name
            if poll_interval_minutes:
                row.poll_interval_minutes = poll_interval_minutes
        if enabled is not None:
            row.enabled = enabled
        self.db.flush()
        return row

    def list_all(self) -> list[SourceHealthRow]:
        return (
            self.db.query(SourceHealthRow)
            .order_by(SourceHealthRow.source, SourceHealthRow.source_identifier)
            .all()
        )

    def due(self, now: Optional[datetime] = None, limit: int = 50) -> list[SourceHealthRow]:
        """Enabled targets whose ``next_poll_at`` has passed, oldest first."""
        now = now or db_now()
        return (
            self.db.query(SourceHealthRow)
            .filter(
                SourceHealthRow.enabled.is_(True),
                (SourceHealthRow.next_poll_at.is_(None)) | (SourceHealthRow.next_poll_at <= now),
            )
            .order_by(SourceHealthRow.next_poll_at.asc().nulls_first())
            .limit(limit)
            .all()
        )

    def record_run(
        self,
        run: DiscoveryRunRow,
        fresh_posted_at: Optional[datetime] = None,
        commit: bool = False,
    ) -> SourceHealthRow:
        """Fold a finished run into the source's health and schedule the next poll."""
        row = self.upsert_target(run.source, run.source_identifier, run.company_name)
        now = db_now()
        row.last_run_id = run.id
        row.last_run_at = run.completed_at or now
        row.runs_total += 1
        row.jobs_fetched_total += run.candidates_discovered or 0
        # jobs_duplicate already includes the unchanged fast-path count.
        row.jobs_parsed_total += (run.jobs_new or 0) + (run.jobs_updated or 0) + (run.jobs_duplicate or 0)
        row.jobs_rejected_total += run.jobs_rejected or 0
        row.duplicates_total += run.jobs_duplicate or 0
        row.last_jobs_fetched = run.candidates_discovered or 0
        row.last_jobs_new = run.jobs_new or 0
        if fresh_posted_at is not None and (row.last_fresh_posted_at is None or fresh_posted_at > row.last_fresh_posted_at):
            row.last_fresh_posted_at = fresh_posted_at
        if run.duration_seconds is not None:
            if row.avg_duration_seconds is None:
                row.avg_duration_seconds = run.duration_seconds
            else:
                row.avg_duration_seconds = round(0.8 * row.avg_duration_seconds + 0.2 * run.duration_seconds, 3)

        succeeded = run.status in ("completed", "partial")
        if succeeded:
            row.runs_success += 1
            if run.status == "partial":
                row.runs_partial += 1
            row.last_success_at = row.last_run_at
            row.consecutive_failures = 0
            row.last_failure_kind = None
            interval = row.poll_interval_minutes
        else:
            row.runs_failed += 1
            row.last_failure_at = row.last_run_at
            row.last_failure_kind = run.failure_kind
            row.last_error = (run.errors or ["unknown failure"])[-1][:512]
            row.consecutive_failures += 1
            if run.failure_kind in (KIND_RATE_LIMITED, KIND_CIRCUIT_OPEN):
                row.runs_rate_limited += 1
            # Back off a failing source: double the interval per consecutive
            # failure, capped at a day. Never disable it automatically.
            interval = min(
                row.poll_interval_minutes * (2 ** min(row.consecutive_failures, 6)),
                MAX_BACKOFF_MINUTES,
            )
        row.next_poll_at = now + timedelta(minutes=interval)
        self.db.flush()
        if commit:
            self.db.commit()
        return row

    @staticmethod
    def success_rate(row: SourceHealthRow) -> Optional[float]:
        if not row.runs_total:
            return None
        return round(row.runs_success / row.runs_total, 3)
