"""Pydantic models for tailored application artifacts."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ArtifactType(str, Enum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"
    ANSWER = "ANSWER"


class TailoredArtifact(BaseModel):
    id: str
    job_id: str
    match_id: Optional[str] = None
    artifact_type: ArtifactType
    version: int
    title: Optional[str] = None
    content: str
    evidence_refs: List[str] = Field(default_factory=list)
    template_name: str
    approved: bool = False
    created_at: datetime
