"""DiscoveryRun model — tracks a single execution of the discovery pipeline."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.timeutils import utc_now
from app.jobs.models.enums import JobSourceType


class DiscoveryRun(BaseModel):
    """Records metadata and statistics for a single discovery pipeline execution."""

    id: Optional[str] = None
    source: JobSourceType
    source_identifier: str = Field(
        description="Board token, company slug, or URL that was discovered"
    )
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    candidates_discovered: int = 0
    pages_fetched: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_duplicate: int = 0
    jobs_failed: int = 0
    errors: List[str] = Field(default_factory=list)
    duration_seconds: Optional[float] = None
    status: str = "running"
