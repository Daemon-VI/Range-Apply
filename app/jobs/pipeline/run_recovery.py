"""Recovery for ingestion runs that never reached a terminal state.

A process killed mid-run (free-tier hosts restart containers routinely) leaves
a ``discovery_runs`` row stuck at ``running`` forever, which makes run history
lie about what actually happened. Reconciliation runs on startup and before
each new run, so the window in which a stuck row can be observed is bounded.
"""

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.core.timeutils import db_now
from app.jobs.database.models import DiscoveryRunRow

logger = logging.getLogger(__name__)

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_INTERRUPTED = "interrupted"

TERMINAL_STATUSES = (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_FAILED,
    RUN_STATUS_INTERRUPTED,
)


def reconcile_stale_runs(db: Session, timeout_minutes: Optional[int] = None) -> int:
    """Mark long-``running`` runs as ``interrupted``.

    Args:
        db: Active session.
        timeout_minutes: Age past which a running row is considered abandoned.
            Defaults to ``settings.stale_run_timeout_minutes``.

    Returns:
        Number of runs reconciled.
    """
    minutes = timeout_minutes if timeout_minutes is not None else settings.stale_run_timeout_minutes
    cutoff = db_now() - timedelta(minutes=minutes)

    stale = (
        db.query(DiscoveryRunRow)
        .filter(DiscoveryRunRow.status == RUN_STATUS_RUNNING)
        .filter(DiscoveryRunRow.started_at < cutoff)
        .all()
    )
    if not stale:
        return 0

    now = db_now()
    for run in stale:
        run.status = RUN_STATUS_INTERRUPTED
        run.completed_at = now
        errors = list(run.errors or [])
        errors.append(
            f"Run was still marked '{RUN_STATUS_RUNNING}' after {minutes} minutes "
            "and was reconciled as interrupted (process restart or crash)."
        )
        run.errors = errors
        logger.warning("Reconciled interrupted discovery run %s (%s)", run.id, run.source)

    db.commit()
    return len(stale)
