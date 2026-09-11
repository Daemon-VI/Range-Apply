"""Domain models for the application engine (Phase 5)."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    """State machine for a job application, per the PRD."""

    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    SHORTLISTED = "SHORTLISTED"
    PREPARING = "PREPARING"
    READY = "READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"
    INTERVIEWING = "INTERVIEWING"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class Application(BaseModel):
    """Mirrors ``ApplicationRow``."""

    id: str
    job_id: str
    status: ApplicationStatus
    adapter: Optional[str] = None
    dry_run: bool = True
    tailored_artifact_ids: List[str] = Field(default_factory=list)
    confirmation: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApplicationEvent(BaseModel):
    """Mirrors ``ApplicationEventRow``."""

    id: str
    application_id: str
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    metadata_: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
