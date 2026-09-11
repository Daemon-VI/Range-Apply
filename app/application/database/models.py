"""SQLAlchemy ORM models for the application engine (Phase 5)."""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.types import JSON

from app.core.timeutils import db_now
from app.jobs.database.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ApplicationRow(Base):
    """One application per job. The unique ``job_id`` is the FR-08 idempotency guard."""

    __tablename__ = "applications"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status = Column(String(32), nullable=False, default="DISCOVERED")
    adapter = Column(String(64), nullable=True)
    dry_run = Column(Boolean, nullable=False, default=True)
    tailored_artifact_ids = Column(JSON, default=list)
    confirmation = Column(String(512), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=db_now)
    updated_at = Column(DateTime, default=db_now, onupdate=db_now)


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
