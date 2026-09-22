"""Persistent metadata for rendered documents (the bytes live on local disk).

    tenant -> candidate opportunity -> preparation -> document_artifacts (versions)

A row is immutable once written: a changed preparation or renderer produces
a new version; a bad artifact is *invalidated*, never edited or overwritten,
so an application attempt keeps pointing at exactly the file it uploaded.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.types import JSON

from app.core.ids import new_id
from app.core.timeutils import db_now
from app.jobs.database.models import Base


class DocumentArtifactRow(Base):
    __tablename__ = "document_artifacts"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    candidate_opportunity_id = Column(String(36), ForeignKey("candidate_opportunities.id", ondelete="CASCADE"), nullable=False, index=True)
    preparation_id = Column(String(36), ForeignKey("application_preparations.id", ondelete="CASCADE"), nullable=False, index=True)
    preparation_version = Column(Integer, nullable=False, default=1)
    artifact_type = Column(String(16), nullable=False)
    format = Column(String(8), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="ACTIVE")
    validation_status = Column(String(16), nullable=False, default="PASSED")
    validation_report = Column(JSON, nullable=False, default=dict)
    #: SHA-256 of the final bytes.
    content_hash = Column(String(64), nullable=False, index=True)
    #: SHA-256 over (content blocks, contact, options, renderer, renderer version).
    input_fingerprint = Column(String(64), nullable=False)
    evidence_fingerprint = Column(String(64), nullable=True)
    renderer = Column(String(32), nullable=False)
    renderer_version = Column(String(32), nullable=False)
    byte_size = Column(Integer, nullable=False, default=0)
    page_count = Column(Integer, nullable=False, default=0)
    #: Path relative to the documents root; never an absolute or user-supplied path.
    relative_path = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False, default=db_now)
    invalidated_at = Column(DateTime, nullable=True)
    invalidated_reason = Column(String(256), nullable=True)

    __table_args__ = (
        UniqueConstraint("preparation_id", "artifact_type", "format", "version", name="uq_document_artifact_version"),
        Index("ix_document_artifacts_tenant_status", "tenant_id", "status"),
    )
