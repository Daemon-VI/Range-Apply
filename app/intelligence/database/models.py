import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

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
    created_at = Column(DateTime, default=datetime.utcnow)

class MatchRunRow(Base):
    """Tracks a batch recalculation run for provenance."""
    __tablename__ = "match_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    policy_version = Column(String(32), nullable=False)
    engine_version = Column(String(32), nullable=False)
    career_brain_version = Column(String(64), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    jobs_processed = Column(Integer, default=0)
    trigger = Column(String(64)) # e.g. "CAREER_BRAIN_UPDATE" or "JOB_DISCOVERY"
    status = Column(String(32), default="RUNNING")

class JobMatchRow(Base):
    """Core entity storing the final match score, eligibility, etc."""
    __tablename__ = "job_matches"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("match_runs.id", ondelete="CASCADE"), nullable=False)
    
    eligibility_status = Column(String(32), nullable=False)
    eligibility_reasons = Column(JSON, default=list)
    blocking_reasons = Column(JSON, default=list)
    
    fit_score = Column(Integer, nullable=False)
    priority = Column(String(32), nullable=False, index=True)
    match_type = Column(String(32), nullable=False)
    confidence = Column(String(32), nullable=False)
    
    strengths = Column(JSON, default=list)
    gaps = Column(JSON, default=list)
    explanation = Column(Text, nullable=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # We might not strictly need relationships loaded all the time, but for completeness:
    # assessments = relationship("RequirementAssessmentRow", back_populates="job_match", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_job_matches_job_run", "job_id", "run_id"),
    )

class RequirementAssessmentRow(Base):
    """Stores the matched requirement assessment for a job match."""
    __tablename__ = "requirement_assessments"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_match_id = Column(String(36), ForeignKey("job_matches.id", ondelete="CASCADE"), nullable=False, index=True)
    
    requirement_name = Column(String(128), nullable=False)
    requirement_category = Column(String(32), nullable=False)
    requirement_strictness = Column(String(32), nullable=False)
    
    status = Column(String(32), nullable=False)
    evidence_strength = Column(String(32), nullable=False)
    evidence_references = Column(JSON, default=list)
    confidence = Column(String(32), nullable=False)
    
    impact = Column(String(128))
    explanation = Column(Text)
    
    # job_match = relationship("JobMatchRow", back_populates="assessments")
