import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.types import JSON

from app.core.timeutils import db_now
from app.jobs.database.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())

class MatchPolicyRow(Base):
    """Tracks versioned scoring policies."""
    __tablename__ = "match_policies"

    id = Column(String(36), primary_key=True, default=_uuid)
    version = Column(String(32), nullable=False, unique=True)
    description = Column(String(256))
    weights = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=db_now)

class MatchRunRow(Base):
    """Tracks a batch recalculation run for provenance."""
    __tablename__ = "match_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    policy_version = Column(String(32), nullable=False)
    engine_version = Column(String(32), nullable=False)
    career_brain_version = Column(String(64), nullable=False)
    started_at = Column(DateTime, default=db_now)
    completed_at = Column(DateTime)
    jobs_processed = Column(Integer, default=0)
    jobs_matched = Column(Integer, default=0)
    jobs_failed = Column(Integer, default=0)
    errors = Column(JSON, default=list)
    duration_seconds = Column(Float)
    trigger = Column(String(64)) # e.g. "CAREER_BRAIN_UPDATE" or "JOB_DISCOVERY"
    status = Column(String(32), default="RUNNING")
    # Blueprint Phase 6: which tenant's Career Brain the run was scored
    # against. Nullable for rows that predate tenancy; never defaulted.
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)

class JobMatchRow(Base):
    """Core entity storing the final match score, eligibility, etc."""
    __tablename__ = "job_matches"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("match_runs.id", ondelete="CASCADE"), nullable=False, index=True)

    # Provenance: which version of the job and which policy produced this score.
    job_canonical_key = Column(String(128), index=True)
    job_content_hash = Column(String(64))
    policy_version = Column(String(32), nullable=False, default="v1")
    engine_version = Column(String(32), nullable=False, default="1.0.0")

    eligibility_status = Column(String(32), nullable=False, index=True)
    eligibility_confidence = Column(String(32), nullable=False, default="UNKNOWN")
    eligibility_reasons = Column(JSON, default=list)
    blocking_reasons = Column(JSON, default=list)

    fit_score = Column(Integer, nullable=False)
    # Per-dimension breakdown so a score is explainable, not just a number.
    component_scores = Column(JSON, default=dict)
    priority = Column(String(32), nullable=False, index=True)
    match_type = Column(String(32), nullable=False)
    confidence = Column(String(32), nullable=False)

    strengths = Column(JSON, default=list)
    gaps = Column(JSON, default=list)
    # Things the engine could not determine. Never collapsed into a match/gap.
    uncertainties = Column(JSON, default=list)
    explanation = Column(Text, nullable=False)

    evaluated_at = Column(DateTime, default=db_now)
    created_at = Column(DateTime, default=db_now)
    
    # We might not strictly need relationships loaded all the time, but for completeness:
    # assessments = relationship("RequirementAssessmentRow", back_populates="job_match", cascade="all, delete-orphan")

    __table_args__ = (
        # One match row per job per run: makes recalculation idempotent.
        # A unique *index* rather than a constraint, because SQLite cannot add
        # a table constraint via ALTER TABLE and would need a full rebuild.
        Index("ix_job_matches_job_run", "job_id", "run_id", unique=True),
        Index("ix_job_matches_priority_score", "priority", "fit_score"),
    )

class RequirementAssessmentRow(Base):
    """Stores the matched requirement assessment for a job match."""
    __tablename__ = "requirement_assessments"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_match_id = Column(String(36), ForeignKey("job_matches.id", ondelete="CASCADE"), nullable=False, index=True)
    
    requirement_name = Column(String(128), nullable=False)
    requirement_original_text = Column(Text)
    requirement_category = Column(String(32), nullable=False)
    requirement_strictness = Column(String(32), nullable=False)
    
    status = Column(String(32), nullable=False)
    evidence_strength = Column(String(32), nullable=False)
    evidence_references = Column(JSON, default=list)
    confidence = Column(String(32), nullable=False)
    
    impact = Column(String(128))
    # Weight applied by the policy and the points this requirement contributed.
    weight = Column(Float, default=0.0)
    contribution = Column(Float, default=0.0)
    explanation = Column(Text)
    
    # job_match = relationship("JobMatchRow", back_populates="assessments")
