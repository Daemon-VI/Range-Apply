"""SQLAlchemy ORM model for tailored artifacts.

Frozen contract: table name and columns below are depended on by another
agent's migration - do not rename or retype anything here.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.types import JSON

from app.core.timeutils import db_now
from app.jobs.database.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TailoredArtifactRow(Base):
    """One generated resume/cover-letter/answer artifact for a job."""

    __tablename__ = "tailored_artifacts"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    match_id = Column(String(36), nullable=True)
    artifact_type = Column(String(32), nullable=False)
    version = Column(Integer, default=1)
    title = Column(String(256), nullable=True)
    content = Column(Text)
    evidence_refs = Column(JSON, default=list)
    template_name = Column(String(64))
    approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=db_now)
