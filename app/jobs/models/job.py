"""NormalizedJob model — the canonical normalized representation of a job."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.jobs.models.enums import (
    EmploymentType,
    ExperienceLevel,
    JobSourceType,
    JobStatus,
    ProcessingStatus,
    RemoteType,
)


class GraduationRequirement(BaseModel):
    """Structured graduation requirement model."""

    minimum_year: Optional[int] = None
    maximum_year: Optional[int] = None
    exact_years: List[int] = Field(default_factory=list)
    original_text: str = ""
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class NormalizedJob(BaseModel):
    """Canonical normalized job record ready for persistence and Phase 3 consumption."""

    id: Optional[str] = None
    canonical_key: str = Field(
        description="Deterministic identity key for deduplication"
    )
    source: JobSourceType
    source_job_id: str
    company: str
    title: str
    original_title: str
    description: str = ""
    original_description: str = ""
    location: Optional[str] = None
    locations: List[str] = Field(default_factory=list)
    remote_type: RemoteType = RemoteType.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience_level: ExperienceLevel = ExperienceLevel.UNKNOWN
    education_requirements: List[str] = Field(default_factory=list)
    graduation_requirement: Optional[GraduationRequirement] = None
    graduation_year_requirement: Optional[int] = None
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    qualifications: List[str] = Field(default_factory=list)
    salary_text: Optional[str] = None
    application_url: Optional[str] = None
    source_url: str
    posted_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    content_hash: str = ""
    processing_status: ProcessingStatus = ProcessingStatus.DISCOVERED
    job_status: JobStatus = JobStatus.UNKNOWN
    extraction_metadata: Dict = Field(default_factory=dict)
    metadata: Dict = Field(default_factory=dict)
