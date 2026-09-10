"""SQLAlchemy ORM models for the jobs domain."""

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

# JSON type works for PostgreSQL (stores as native JSON/JSONB) and SQLite (stores as serialized JSON text)
from sqlalchemy.types import JSON

from app.core.timeutils import db_now

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class JobRow(Base):
    """Normalized job record."""

    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=_uuid)
    canonical_key = Column(String(128), nullable=False, unique=True, index=True)
    source = Column(String(32), nullable=False)
    source_job_id = Column(String(256), nullable=False)
    company = Column(String(256), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    original_title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    original_description = Column(Text, default="")
    location = Column(String(256), index=True)
    locations = Column(JSON, default=list)
    remote_type = Column(String(32), default="UNKNOWN", index=True)
    employment_type = Column(String(32), default="UNKNOWN", index=True)
    experience_level = Column(String(32), default="UNKNOWN")
    education_requirements = Column(JSON, default=list)
    graduation_requirement = Column(JSON, default=dict)
    graduation_year_requirement = Column(Integer)
    required_skills = Column(JSON, default=list)
    preferred_skills = Column(JSON, default=list)
    technologies = Column(JSON, default=list)
    responsibilities = Column(JSON, default=list)
    qualifications = Column(JSON, default=list)
    salary_text = Column(String(256))
    application_url = Column(String(1024))
    source_url = Column(String(1024), nullable=False)
    # Canonical, exact-comparison identity keys (see jobs/normalization/urls.py).
    # Indexed because deduplication looks them up once per ingested job.
    normalized_source_url = Column(String(1024), index=True)
    normalized_application_url = Column(String(1024), index=True)
    # Board token / company slug this job was discovered under. Scopes the
    # stale-job sweep so ingesting one board cannot close another board's jobs.
    source_identifier = Column(String(256), index=True)
    posted_at = Column(DateTime)
    source_updated_at = Column(DateTime)
    deadline = Column(DateTime)
    closed_at = Column(DateTime)
    first_seen_at = Column(DateTime, nullable=False, default=db_now)
    last_seen_at = Column(DateTime, nullable=False, default=db_now)
    content_hash = Column(String(64), nullable=False, index=True)
    processing_status = Column(String(32), nullable=False, default="DISCOVERED", index=True)
    job_status = Column(String(32), nullable=False, default="UNKNOWN", index=True)
    extraction_metadata = Column(JSON, default=dict)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=db_now)
    updated_at = Column(DateTime, default=db_now, onupdate=db_now)

    # Relationships
    source_references = relationship(
        "SourceReferenceRow", back_populates="job", cascade="all, delete-orphan"
    )
    versions = relationship(
        "JobVersionRow", back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_source_job"),
        Index("ix_jobs_source_source_job_id", "source", "source_job_id"),
    )


class SourceReferenceRow(Base):
    """Tracks where a canonical job was seen across different sources."""

    __tablename__ = "job_source_references"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(32), nullable=False)
    source_job_id = Column(String(256), nullable=False)
    source_url = Column(String(1024), nullable=False)
    application_url = Column(String(1024))
    first_seen_at = Column(DateTime, nullable=False, default=db_now)
    last_seen_at = Column(DateTime, nullable=False, default=db_now)
    metadata_ = Column("metadata", JSON, default=dict)

    job = relationship("JobRow", back_populates="source_references")

    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_source_ref"),
    )


class JobVersionRow(Base):
    """Records meaningful changes to a job over time."""

    __tablename__ = "job_versions"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    content_hash = Column(String(64), nullable=False)
    title = Column(String(512))
    description = Column(Text)
    changes_summary = Column(String(512))
    created_at = Column(DateTime, default=db_now)

    job = relationship("JobRow", back_populates="versions")


class DiscoveryRunRow(Base):
    """Records metadata for a single discovery pipeline execution."""

    __tablename__ = "discovery_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    source = Column(String(32), nullable=False, index=True)
    source_identifier = Column(String(256), nullable=False)
    started_at = Column(DateTime, nullable=False, default=db_now)
    completed_at = Column(DateTime)
    candidates_discovered = Column(Integer, default=0)
    pages_fetched = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    jobs_updated = Column(Integer, default=0)
    jobs_duplicate = Column(Integer, default=0)
    jobs_failed = Column(Integer, default=0)
    jobs_closed = Column(Integer, default=0)
    errors = Column(JSON, default=list)
    trigger = Column(String(32), default="manual")
    duration_seconds = Column(Float)
    status = Column(String(32), nullable=False, default="running")
