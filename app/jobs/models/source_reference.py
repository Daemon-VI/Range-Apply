"""SourceReference model — tracks provenance of a job across multiple sources."""

from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.jobs.models.enums import JobSourceType


class SourceReference(BaseModel):
    """Records where a canonical job was seen, preserving multi-source provenance."""

    id: Optional[str] = None
    job_id: str
    source: JobSourceType
    source_job_id: str
    source_url: str
    application_url: Optional[str] = None
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict = Field(default_factory=dict)
