"""SQLAlchemy rows for application preparation (tenant-scoped).

    tenant -> candidate_opportunity -> application_preparations (versions)
                                        -> preparation_artifacts (resume, cover letter)
                                        -> preparation_answers

A preparation belongs to the *candidate opportunity*, never to a job row:
several source rows can describe one opening and the package is the
candidate's answer to that opening. ``inputs`` + ``input_fingerprint`` make
a package reproducible (evidence version, job hash, variant, policy, template,
answer-bank version, AI config).
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.core.ids import new_id
from app.core.timeutils import db_now
from app.jobs.database.models import Base


class ApplicationPreparationRow(Base):
    __tablename__ = "application_preparations"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    candidate_opportunity_id = Column(
        String(36),
        ForeignKey("candidate_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opportunity_id = Column(
        String(36), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    job_content_hash = Column(String(64))
    version = Column(Integer, nullable=False, default=1)
    tailoring_level = Column(String(4), nullable=False, default="L0")
    lane = Column(String(8), nullable=False, default="REVIEW")
    cover_letter_mode = Column(String(16), nullable=False, default="DISABLED")
    positioning_variant_id = Column(
        String(36), ForeignKey("positioning_variants.id", ondelete="SET NULL"), nullable=True
    )
    positioning_variant_version = Column(Integer)
    positioning_reason = Column(String(256))
    status = Column(String(24), nullable=False, default="PENDING")
    validation_status = Column(String(16), nullable=False, default="PENDING")
    validation_report = Column(JSON, nullable=False, default=dict)
    input_fingerprint = Column(String(64), nullable=False)
    inputs = Column(JSON, nullable=False, default=dict)
    evidence_keys = Column(JSON, nullable=False, default=list)
    ai_used = Column(Boolean, nullable=False, default=False)
    ai_provider = Column(String(64))
    ai_model = Column(String(128))
    ai_calls = Column(Integer, nullable=False, default=0)
    approved_at = Column(DateTime)
    approved_by = Column(String(128))
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    artifacts = relationship(
        "PreparationArtifactRow", back_populates="preparation", cascade="all, delete-orphan"
    )
    answers = relationship(
        "PreparationAnswerRow",
        back_populates="preparation",
        cascade="all, delete-orphan",
        order_by="PreparationAnswerRow.position",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "candidate_opportunity_id", "version", name="uq_preparation_version"
        ),
        Index("ix_preparations_tenant_status", "tenant_id", "status"),
        Index("ix_preparations_tenant_fingerprint", "tenant_id", "input_fingerprint"),
    )


class PreparationArtifactRow(Base):
    __tablename__ = "preparation_artifacts"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    preparation_id = Column(
        String(36),
        ForeignKey("application_preparations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type = Column(String(16), nullable=False)
    content = Column(Text, nullable=False, default="")
    # Ordered blocks: {kind, section, text, evidence_keys, source, ai_polished}
    blocks = Column(JSON, nullable=False, default=list)
    evidence_keys = Column(JSON, nullable=False, default=list)
    template_version = Column(String(32), nullable=False)
    ai_used = Column(Boolean, nullable=False, default=False)
    validation_status = Column(String(16), nullable=False, default="PENDING")
    created_at = Column(DateTime, nullable=False, default=db_now)

    preparation = relationship("ApplicationPreparationRow", back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("preparation_id", "artifact_type", name="uq_preparation_artifact"),
    )


class PreparationAnswerRow(Base):
    __tablename__ = "preparation_answers"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    preparation_id = Column(
        String(36),
        ForeignKey("application_preparations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = Column(Integer, nullable=False, default=0)
    question = Column(String(512), nullable=False)
    question_key = Column(String(512), nullable=False)
    category = Column(String(32), nullable=False)
    answer = Column(Text)
    evidence_keys = Column(JSON, nullable=False, default=list)
    source = Column(String(16), nullable=False, default="NONE")
    status = Column(String(24), nullable=False, default="NEEDS_REVIEW")
    required = Column(Boolean, nullable=False, default=True)
    answer_bank_entry_id = Column(String(36))
    reason = Column(String(256))
    ai_used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    preparation = relationship("ApplicationPreparationRow", back_populates="answers")

    __table_args__ = (
        UniqueConstraint("preparation_id", "question_key", name="uq_preparation_question"),
        Index("ix_preparation_answers_tenant_status", "tenant_id", "status"),
    )
