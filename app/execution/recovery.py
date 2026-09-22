"""Startup recovery for execution (Blueprint Phase 12).

A process that died mid-execution leaves ``execution_runs`` rows RUNNING and
queue items CLAIMED / PROCESSING with a lease that will lapse. Nothing is
lost — the lazy path in ``ExecutionService.start`` already handles a
reclaimed item — but until then the attempt looks stuck. Recovery on boot
settles every such run per the one rule that matters: a run that had
``submit_invoked`` becomes UNKNOWN / UNCERTAIN (never resubmitted); one that
did not becomes retryable. Runs with a live lease are left to their worker.
"""

import logging

from sqlalchemy.orm import Session

from app.career.database.models import TenantRow
from app.execution.service import ExecutionService

logger = logging.getLogger(__name__)


def recover_execution(db: Session, tenant_ids: list[str] | None = None) -> dict[str, int]:
    """Run ``recover_lost_runs`` for every tenant (or the given ones); returns totals."""
    totals: dict[str, int] = {}
    ids = tenant_ids if tenant_ids is not None else [row.id for row in db.query(TenantRow.id).all()]
    for tenant_id in ids:
        try:
            counts = ExecutionService(db, tenant_id, actor="startup-recovery", executors={}).recover_lost_runs()
        except Exception as exc:  # noqa: BLE001 - recovery must never block startup
            db.rollback()
            logger.warning("Execution recovery failed for tenant %s: %s", tenant_id, type(exc).__name__)
            continue
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    if totals.get("uncertain") or totals.get("retryable"):
        logger.warning("Execution recovery at startup: %s", totals)
    return totals


__all__ = ["recover_execution"]
