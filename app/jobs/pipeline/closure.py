"""Closing jobs that have disappeared from their source board.

A posting that vanishes from a board is filled or withdrawn. Leaving it ACTIVE
forever makes every shortlist progressively less trustworthy, so a successful
ingestion run reconciles what it saw against what is stored.

Safety rules, in order of importance:

* The sweep is scoped to ``(source, source_identifier)``. Ingesting Stripe's
  board must never touch Cloudflare's jobs.
* It only runs after a run that actually succeeded and returned jobs. A network
  blip returning zero results must not close an entire board.
* It refuses to run when too large a share of the run's jobs failed to process,
  because the observed set would be incomplete.
* Closing is reversible: seeing the job again reopens it (handled in the
  deduplicator).
"""

import logging
from typing import Iterable, Optional, Set

from sqlalchemy.orm import Session

from app.config import settings
from app.core.timeutils import db_now
from app.jobs.database.models import JobRow
from app.jobs.models.enums import JobSourceType, JobStatus

logger = logging.getLogger(__name__)


class ClosureDecision:
    """Why a sweep did or did not run, and what it changed."""

    def __init__(self, performed: bool, closed: int = 0, reason: str = ""):
        self.performed = performed
        self.closed = closed
        self.reason = reason


def should_sweep(
    run_status: str,
    candidates_discovered: int,
    jobs_failed: int,
    max_failure_ratio: Optional[float] = None,
) -> ClosureDecision:
    """Decide whether the observed set is complete enough to close anything."""
    if run_status != "completed":
        return ClosureDecision(False, reason=f"run status is '{run_status}', not 'completed'")

    if candidates_discovered <= 0:
        return ClosureDecision(
            False, reason="source returned no jobs; cannot distinguish empty board from failure"
        )

    ratio_limit = (
        max_failure_ratio if max_failure_ratio is not None else settings.closure_max_failure_ratio
    )
    failure_ratio = jobs_failed / candidates_discovered
    if failure_ratio > ratio_limit:
        return ClosureDecision(
            False,
            reason=(
                f"{jobs_failed}/{candidates_discovered} jobs failed "
                f"({failure_ratio:.0%} > {ratio_limit:.0%} limit); observed set is incomplete"
            ),
        )

    return ClosureDecision(True)


def close_missing_jobs(
    db: Session,
    source: JobSourceType,
    source_identifier: str,
    observed_job_ids: Iterable[str],
    commit: bool = True,
) -> int:
    """Mark jobs of this board that were not observed in the run as CLOSED.

    Args:
        db: Active session.
        source: The source that was ingested.
        source_identifier: Board token / company slug that was ingested.
        observed_job_ids: ``JobRow.id`` values seen during the run.
        commit: Commit the change (the pipeline manages its own boundary).

    Returns:
        Number of jobs closed.
    """
    observed: Set[str] = set(observed_job_ids)
    now = db_now()

    query = (
        db.query(JobRow)
        .filter(JobRow.source == source.value)
        .filter(JobRow.source_identifier == source_identifier)
        .filter(JobRow.job_status == JobStatus.ACTIVE.value)
    )

    closed = 0
    for job in query.all():
        if job.id in observed:
            continue
        job.job_status = JobStatus.CLOSED.value
        job.closed_at = now
        job.updated_at = now
        job.freshness = "STALE"
        closed += 1

    if closed:
        logger.info(
            "Closed %d job(s) no longer listed on %s board '%s'",
            closed,
            source.value,
            source_identifier,
        )
        if commit:
            db.commit()
        else:
            db.flush()

    return closed
