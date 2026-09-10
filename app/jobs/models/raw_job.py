"""RawJob model — preserves original source content before normalization."""

from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.core.timeutils import utc_now
from app.jobs.models.enums import JobSourceType


class RawJob(BaseModel):
    """Raw job as retrieved from a source, before any normalization."""

    source: JobSourceType
    source_job_id: str
    source_url: str
    discovered_url: str
    raw_title: str
    raw_content: str = Field(description="Full original HTML/Markdown/JSON content")
    content_type: str = Field(
        default="html", description="Content format: html, markdown, json"
    )
    raw_location: Optional[str] = None
    # Dates reported by the source itself, already parsed to aware UTC by the
    # adapter that knows the source's format (ISO string, epoch millis, ...).
    # None means the source did not provide one - never a fabricated value.
    source_posted_at: Optional[datetime] = None
    source_updated_at: Optional[datetime] = None
    source_deadline: Optional[datetime] = None
    raw_metadata: Dict = Field(
        default_factory=dict,
        description="Source-specific fields preserved verbatim",
    )
    discovered_at: datetime = Field(default_factory=utc_now)
    retrieved_at: Optional[datetime] = None
    extraction_method: Optional[str] = None
