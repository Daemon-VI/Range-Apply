from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.core.timeutils import utc_now
from app.intelligence.models.enums import ConfidenceLevel, EligibilityStatus, MatchType
from app.intelligence.models.requirements import RequirementAssessment


class EligibilityResult(BaseModel):
    """Result of hard-gate eligibility analysis."""
    status: EligibilityStatus
    eligibility_reasons: List[str] = Field(default_factory=list)
    blocking_reasons: List[str] = Field(default_factory=list)

class MatchRunInfo(BaseModel):
    """Provenance for a single run of the match engine."""
    run_id: str
    job_version: str
    career_brain_version: str
    policy_version: str
    engine_version: str
    timestamp: datetime = Field(default_factory=utc_now)

class JobMatch(BaseModel):
    """Aggregate root containing the full intelligence result for a job."""
    id: str = Field(description="Unique Match ID")
    job_id: str = Field(description="Reference to NormalizedJob.id")
    job_canonical_key: str = Field(description="Reference to NormalizedJob.canonical_key")
    match_run: MatchRunInfo
    
    eligibility: EligibilityResult
    
    fit_score: int = Field(ge=0, le=100)
    priority: str = Field(description="P0, P1, P2, REVIEW, IGNORE")
    match_type: MatchType
    confidence: ConfidenceLevel
    
    requirement_assessments: List[RequirementAssessment] = Field(default_factory=list)
    
    strengths: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    explanation: str = Field(description="Generated summary of why this job matches")
    
    generated_metadata: Dict[str, Any] = Field(default_factory=dict)
