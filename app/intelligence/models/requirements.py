from typing import List, Optional
from pydantic import BaseModel, Field
from app.intelligence.models.enums import (
    RequirementCategory,
    Strictness,
    MatchStatus,
    EvidenceStrength,
    ConfidenceLevel,
)

class StructuredRequirement(BaseModel):
    """A single normalized requirement extracted from a job description."""
    id: str = Field(description="Unique ID for this requirement")
    original_text: str = Field(description="The exact wording from the JD")
    normalized_name: str = Field(description="Standardized name (e.g. 'Kubernetes' or '2027 Graduation')")
    category: RequirementCategory
    strictness: Strictness
    alternatives: List[str] = Field(default_factory=list, description="Alternative acceptable skills/requirements")
    experience_duration_months: Optional[int] = None
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH

class RequirementAssessment(BaseModel):
    """Result of evaluating a requirement against the Career Brain."""
    requirement: StructuredRequirement
    status: MatchStatus
    evidence_strength: EvidenceStrength
    evidence_references: List[str] = Field(default_factory=list, description="IDs of skills or projects serving as evidence")
    confidence: ConfidenceLevel
    impact: str = Field(description="Impact on overall fit score")
    explanation: str = Field(description="Human-readable explanation of why this was matched or missing")
