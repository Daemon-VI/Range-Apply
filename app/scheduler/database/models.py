"""Scheduler persistence: run records and the application cap ledger.

``scheduler_runs`` is the observable record of every scheduling pass (what
was considered, admitted, blocked and why). ``application_cap_ledger`` is
the concurrency-safe counter behind the daily/weekly caps: one row per
tenant and period, incremented with a conditional ``UPDATE`` so two workers
can never both take the last slot.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.types import JSON

from app.core.ids import new_id
from app.core.timeutils import db_now
from app.jobs.database.models import Base


class SchedulerRunRow(Base):
    __tablename__ = "scheduler_runs"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(16), nullable=False, default="RUNNING")
    trigger = Column(String(32), nullable=False, default="manual")
    actor = Column(String(128), nullable=False, default="scheduler")
    dry_run = Column(Boolean, nullable=False, default=False)
    policy_version = Column(Integer, nullable=False, default=1)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    window_size = Column(Integer, nullable=False, default=0)
    resumed_from_run_id = Column(String(36), nullable=True)
    started_at = Column(DateTime, nullable=False, default=db_now)
    heartbeat_at = Column(DateTime, nullable=False, default=db_now)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    # counts
    considered = Column(Integer, nullable=False, default=0)
    admissible = Column(Integer, nullable=False, default=0)
    admitted = Column(Integer, nullable=False, default=0)
    enqueued = Column(Integer, nullable=False, default=0)
    requeued = Column(Integer, nullable=False, default=0)
    already_queued = Column(Integer, nullable=False, default=0)
    already_completed = Column(Integer, nullable=False, default=0)
    ready_for_execution = Column(Integer, nullable=False, default=0)
    needs_review = Column(Integer, nullable=False, default=0)
    needs_user_input = Column(Integer, nullable=False, default=0)
    released = Column(Integer, nullable=False, default=0)
    #: PREPARE items parked on "needs_user_input" and re-queued because the
    #: answer bank gained an approved answer since (2026-09-22).
    reprepare_requeued = Column(Integer, nullable=False, default=0)
    #: SUBMIT queue items created for READY attempts during this run (Phase 6).
    execution_enqueued = Column(Integer, nullable=False, default=0)
    deferred = Column(Integer, nullable=False, default=0)
    blocked = Column(Integer, nullable=False, default=0)
    blocked_by_reason = Column(JSON, nullable=False, default=dict)
    samples = Column(JSON, nullable=False, default=dict)
    preparation = Column(JSON, nullable=True)
    errors = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (
        Index("ix_scheduler_runs_tenant_started", "tenant_id", "started_at"),
        Index("ix_scheduler_runs_tenant_status", "tenant_id", "status"),
    )


class ApplicationCapLedgerRow(Base):
    """Slots reserved per tenant and period. ``reserved - released`` is usage."""

    __tablename__ = "application_cap_ledger"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    period_kind = Column(String(8), nullable=False)
    period_key = Column(String(16), nullable=False)
    reserved = Column(Integer, nullable=False, default=0)
    released = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "period_kind", "period_key", name="uq_cap_ledger_period"),
    )
