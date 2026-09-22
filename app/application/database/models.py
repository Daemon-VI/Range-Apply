"""SQLAlchemy ORM models for the application engine (Phase 5)."""

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.types import JSON

from app.core.timeutils import db_now
from app.jobs.database.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ApplicationRow(Base):
    """One application attempt per job row (FR-08 guard on ``job_id``).

    Blueprint Phase 2 adds the candidate-side identity: ``tenant_id`` and
    ``opportunity_id``, unique together, so the same candidate cannot hold
    two applications for one real-world opening that surfaced through two
    source rows. ``CandidateOpportunity`` (``app/pipeline``) owns the broader
    relationship; this row is the submission attempt.
    """

    __tablename__ = "applications"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    opportunity_id = Column(
        String(36), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True
    )
    # The prepared package this attempt executes (blueprint Phase 4); the
    # execution phase reads artifacts and answers from it.
    preparation_id = Column(
        String(36), ForeignKey("application_preparations.id", ondelete="SET NULL"), nullable=True
    )
    candidate_opportunity_id = Column(
        String(36), ForeignKey("candidate_opportunities.id", ondelete="SET NULL"), nullable=True
    )
    # Blueprint Phase 5: the attempt is the unit that counts toward caps.
    # ``cap_day``/``cap_week`` are the tenant-local period keys the slot was
    # reserved in; ``released_at`` is set when the slot is given back.
    attempt_number = Column(Integer, nullable=False, default=1)
    lane = Column(String(8), nullable=True)
    tailoring_level = Column(String(4), nullable=True)
    cap_day = Column(String(10), nullable=True)
    cap_week = Column(String(10), nullable=True)
    reserved_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="DISCOVERED")
    # Blueprint Phase 6: execution foundation. ``submission_key`` is set by a
    # guarded UPDATE the moment an executor is allowed to press submit, so one
    # logical attempt can never be submitted twice; ``blocked_reason`` is the
    # human-handoff kind (CAPTCHA_REQUIRED, AUTH_REQUIRED, ...).
    status_reason = Column(String(256), nullable=True)
    blocked_reason = Column(String(32), nullable=True)
    submission_key = Column(String(160), nullable=True, unique=True)
    execution_count = Column(Integer, nullable=False, default=0)
    last_execution_id = Column(String(36), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    external_application_id = Column(String(256), nullable=True)
    result_url = Column(String(2048), nullable=True)
    adapter = Column(String(64), nullable=True)
    dry_run = Column(Boolean, nullable=False, default=True)
    tailored_artifact_ids = Column(JSON, default=list)
    confirmation = Column(String(512), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=db_now)
    updated_at = Column(DateTime, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "opportunity_id", name="uq_applications_tenant_opportunity"),
        Index("ix_applications_tenant_cap_day", "tenant_id", "cap_day"),
        Index("ix_applications_tenant_cap_week", "tenant_id", "cap_week"),
        Index("ix_applications_tenant_status", "tenant_id", "status"),
    )


class ApplicationEventRow(Base):
    """Audit trail of every status transition for an application."""

    __tablename__ = "application_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    application_id = Column(
        String(36),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=True)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=db_now)
