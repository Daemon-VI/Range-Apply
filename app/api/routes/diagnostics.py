"""/api/v1/ops/diagnostics — operational health for the current tenant
(Blueprint Phase 12). Counts only: no candidate data, no secrets.

    queue depth by state / action, stale leases, attempts by status, UNKNOWN
    and RUNNING executions, blocked attempts, signals awaiting review,
    discovery failures, AI gateway counters, cap utilisation, kill switches,
    24-hour execution throughput, latest learning snapshot age, warnings.
"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.database.models import AIUsageRow
from app.ai.gateway import get_gateway
from app.api.deps import get_tenant_id
from app.application.database.models import ApplicationRow
from app.application.killswitch import KillSwitchRow
from app.career.database.models import AuditEventRow
from app.core.timeutils import db_now, utc_now
from app.database import get_db
from app.execution.database.models import ExecutionRunRow
from app.jobs.database.models import DiscoveryRunRow, SourceHealthRow
from app.learning.database.models import LearningSnapshotRow
from app.pipeline.database.models import ApplicationQueueRow
from app.pipeline.repository import PolicyRepository
from app.scheduler.caps import CapLedger, period_keys
from app.signals.database.models import SignalRow

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])


def _counts(rows) -> dict[str, int]:
    return {str(k): int(v) for k, v in rows}


@router.get("/diagnostics")
def diagnostics(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> dict[str, Any]:
    now = db_now()
    day_ago = now - timedelta(hours=24)
    queue_by_state = _counts(db.query(ApplicationQueueRow.state, func.count(ApplicationQueueRow.id)).filter(ApplicationQueueRow.tenant_id == tenant_id).group_by(ApplicationQueueRow.state).all())
    queue_by_action = _counts(db.query(ApplicationQueueRow.action, func.count(ApplicationQueueRow.id)).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.state.in_(["PENDING", "RETRY_WAIT", "CLAIMED", "PROCESSING"])).group_by(ApplicationQueueRow.action).all())
    stale_leases = db.query(func.count(ApplicationQueueRow.id)).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.state.in_(["CLAIMED", "PROCESSING"]), ApplicationQueueRow.lease_expires_at < now).scalar() or 0
    attempts = _counts(db.query(ApplicationRow.status, func.count(ApplicationRow.id)).filter(ApplicationRow.tenant_id == tenant_id).group_by(ApplicationRow.status).all())
    runs = _counts(db.query(ExecutionRunRow.status, func.count(ExecutionRunRow.id)).filter(ExecutionRunRow.tenant_id == tenant_id).group_by(ExecutionRunRow.status).all())
    signals = _counts(db.query(SignalRow.status, func.count(SignalRow.id)).filter(SignalRow.tenant_id == tenant_id).group_by(SignalRow.status).all())
    discovery = _counts(db.query(DiscoveryRunRow.status, func.count(DiscoveryRunRow.id)).filter(DiscoveryRunRow.started_at >= day_ago).group_by(DiscoveryRunRow.status).all())
    failing_sources = db.query(func.count(SourceHealthRow.id)).filter(SourceHealthRow.consecutive_failures > 0).scalar() or 0
    ai_failures = db.query(func.count(AIUsageRow.id)).filter(AIUsageRow.tenant_id == tenant_id, AIUsageRow.created_at >= day_ago, AIUsageRow.status.in_(["TIMEOUT", "ERROR", "MALFORMED", "UNAVAILABLE"])).scalar() or 0
    throughput = _counts(db.query(AuditEventRow.action, func.count(AuditEventRow.id)).filter(AuditEventRow.tenant_id == tenant_id, AuditEventRow.entity_type == "application_attempt", AuditEventRow.created_at >= day_ago, AuditEventRow.action.in_(["execution:started", "execution:submitted", "execution:verified", "execution:unknown", "execution:handoff", "execution:retryable_failure", "execution:permanent_failure", "execution:retry"])).group_by(AuditEventRow.action).all())
    policy = PolicyRepository(db, tenant_id).get()
    keys = period_keys(utc_now(), policy.timezone)
    ledger = CapLedger(db, tenant_id)
    caps = {"day": {"key": keys.day.key, "used": ledger.usage("DAY", keys.day.key), "cap": policy.daily_cap}, "week": {"key": keys.week.key, "used": ledger.usage("WEEK", keys.week.key), "cap": policy.weekly_cap}}
    switches = [{"id": r.id, "paused": bool(r.paused)} for r in db.query(KillSwitchRow).all()]
    latest = db.query(LearningSnapshotRow).filter(LearningSnapshotRow.tenant_id == tenant_id).order_by(LearningSnapshotRow.generated_at.desc()).first()
    learning = {"latest_snapshot_id": latest.id if latest else None, "age_hours": round((now - latest.generated_at).total_seconds() / 3600, 1) if latest else None}
    warnings: list[str] = []
    if stale_leases:
        warnings.append(f"{stale_leases} queue lease(s) expired while claimed; run recovery or let a worker reclaim them")
    if runs.get("RUNNING"):
        warnings.append(f"{runs['RUNNING']} execution run(s) still RUNNING")
    if attempts.get("UNCERTAIN"):
        warnings.append(f"{attempts['UNCERTAIN']} attempt(s) UNCERTAIN: verify or confirm before anything else")
    if signals.get("NEEDS_REVIEW") or signals.get("UNMATCHED"):
        warnings.append(f"{signals.get('NEEDS_REVIEW', 0)} signal(s) need review, {signals.get('UNMATCHED', 0)} unmatched")
    if any(s["paused"] for s in switches):
        warnings.append("a kill switch is paused: no submission will pass the final gate")
    if failing_sources:
        warnings.append(f"{failing_sources} discovery source(s) with consecutive failures")
    if caps["day"]["cap"] and caps["day"]["used"] >= caps["day"]["cap"]:
        warnings.append("the daily cap is fully reserved")
    return {
        "tenant_id": tenant_id,
        "generated_at": now.isoformat(),
        "queue": {"by_state": queue_by_state, "active_by_action": queue_by_action, "stale_leases": int(stale_leases)},
        "attempts": {"by_status": attempts, "blocked": attempts.get("BLOCKED", 0), "needs_review": attempts.get("NEEDS_REVIEW", 0), "needs_user_input": attempts.get("NEEDS_USER_INPUT", 0), "uncertain": attempts.get("UNCERTAIN", 0), "failed": attempts.get("FAILED", 0)},
        "executions": {"by_status": runs, "unknown": runs.get("UNKNOWN", 0), "running": runs.get("RUNNING", 0), "last_24h": throughput},
        "signals": {"by_status": signals, "review_queue": signals.get("NEEDS_REVIEW", 0) + signals.get("UNMATCHED", 0)},
        "discovery": {"runs_last_24h": discovery, "sources_failing": int(failing_sources)},
        "ai": {"gateway": get_gateway().stats(), "failures_last_24h": int(ai_failures)},
        "caps": caps,
        "kill_switches": switches,
        "learning": learning,
        "warnings": warnings,
    }
