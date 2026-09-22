"""SQLAlchemy rows for the tenant-scoped Evidence Graph.

Every candidate-side table carries ``tenant_id`` (blueprint §11.11). Node
identity for downstream references is the tenant-scoped ``key`` (stable,
human-readable, e.g. ``skill-python``), while ``id`` stays a global UUID so
rows can be created anywhere. ``audit_events`` is the generic before/after
ledger that later phases reuse for applications and policy changes.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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


class TenantRow(Base):
    """A candidate account. Solo mode has exactly one (``settings.default_tenant_id``)."""

    __tablename__ = "tenants"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=db_now)


class CandidateProfileRow(Base):
    """Structured identity fields, copied (never generated) into applications."""

    __tablename__ = "candidate_profiles"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    name = Column(String(256), nullable=False)
    email = Column(String(256))
    phone = Column(String(64))
    location = Column(String(256))
    work_authorization = Column(String(256))
    degree = Column(String(256), nullable=False, default="")
    branch = Column(String(256), nullable=False, default="")
    college = Column(String(256), nullable=False, default="")
    graduation_year = Column(Integer, nullable=False, default=0)
    current_academic_status = Column(String(256), nullable=False, default="")
    cgpa = Column(Float, nullable=False, default=0.0)
    backlogs = Column(String(128), nullable=False, default="")
    github = Column(String(512))
    linkedin = Column(String(512))
    portfolio = Column(String(512))
    positioning_statement = Column(Text, nullable=False, default="")
    long_term_goal = Column(Text, nullable=False, default="")
    preferences = Column(JSON, nullable=False, default=dict)
    source_hash = Column(String(64))
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)


class EvidenceNodeRow(Base):
    __tablename__ = "evidence_nodes"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key = Column(String(160), nullable=False)
    kind = Column(String(32), nullable=False, index=True)
    label = Column(String(256), nullable=False)
    claim = Column(Text, nullable=False)
    attributes = Column(JSON, nullable=False, default=dict)
    verification_status = Column(String(32), nullable=False, default="UNVERIFIED", index=True)
    confidence = Column(Float, nullable=False, default=1.0)
    allowed_for_resume = Column(Boolean, nullable=False, default=False)
    allowed_for_application = Column(Boolean, nullable=False, default=False)
    source_type = Column(String(32), nullable=False, default="CANDIDATE_ENTERED")
    source_ref = Column(String(512))
    source_hash = Column(String(64))
    artifact_ref = Column(String(512))
    verified_by = Column(String(128))
    verified_at = Column(DateTime)
    status = Column(String(16), nullable=False, default="ACTIVE", index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)
    removed_at = Column(DateTime)

    outgoing = relationship(
        "EvidenceRelationshipRow",
        foreign_keys="EvidenceRelationshipRow.from_node_id",
        back_populates="from_node",
        cascade="all, delete-orphan",
    )
    incoming = relationship(
        "EvidenceRelationshipRow",
        foreign_keys="EvidenceRelationshipRow.to_node_id",
        back_populates="to_node",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_evidence_nodes_tenant_key"),
        Index("ix_evidence_nodes_tenant_kind_status", "tenant_id", "kind", "status"),
    )


class EvidenceRelationshipRow(Base):
    __tablename__ = "evidence_relationships"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_node_id = Column(
        String(36), ForeignKey("evidence_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_node_id = Column(
        String(36), ForeignKey("evidence_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation = Column(String(32), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=db_now)

    from_node = relationship(
        "EvidenceNodeRow", foreign_keys=[from_node_id], back_populates="outgoing"
    )
    to_node = relationship("EvidenceNodeRow", foreign_keys=[to_node_id], back_populates="incoming")

    __table_args__ = (
        UniqueConstraint(
            "from_node_id", "to_node_id", "relation", name="uq_evidence_relationship"
        ),
    )


class PositioningVariantRow(Base):
    __tablename__ = "positioning_variants"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_family = Column(String(128), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    headline = Column(String(256), nullable=False, default="")
    summary = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    evidence = relationship(
        "PositioningVariantEvidenceRow",
        back_populates="variant",
        cascade="all, delete-orphan",
        order_by="PositioningVariantEvidenceRow.position",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "role_family", "name", name="uq_positioning_variant_name"),
    )


class PositioningVariantEvidenceRow(Base):
    __tablename__ = "positioning_variant_evidence"

    id = Column(String(36), primary_key=True, default=new_id)
    variant_id = Column(
        String(36),
        ForeignKey("positioning_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id = Column(
        String(36), ForeignKey("evidence_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position = Column(Integer, nullable=False, default=0)
    section = Column(String(32), nullable=False, default="body")

    variant = relationship("PositioningVariantRow", back_populates="evidence")
    node = relationship("EvidenceNodeRow")

    __table_args__ = (
        UniqueConstraint("variant_id", "node_id", name="uq_positioning_variant_node"),
    )


class AnswerBankEntryRow(Base):
    __tablename__ = "answer_bank_entries"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category = Column(String(64), nullable=False, index=True)
    question = Column(String(512), nullable=False)
    question_key = Column(String(512), nullable=False)
    answer = Column(Text, nullable=False)
    evidence_keys = Column(JSON, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="DRAFT", index=True)
    version = Column(Integer, nullable=False, default=1)
    approved_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "question_key", name="uq_answer_bank_question"),
    )


class AuditEventRow(Base):
    """Before/after ledger for candidate-side changes (blueprint §11.11).

    Generic on purpose: later phases record application and policy changes
    here too. ``before``/``after`` are JSON snapshots; ``actor`` is a short
    string like ``"importer"``, ``"api"`` or ``"dashboard"``.
    """

    __tablename__ = "audit_events"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type = Column(String(48), nullable=False)
    entity_id = Column(String(160), nullable=False)
    action = Column(String(32), nullable=False)
    actor = Column(String(128), nullable=False, default="system")
    before = Column(JSON)
    after = Column(JSON)
    summary = Column(String(512))
    created_at = Column(DateTime, nullable=False, default=db_now, index=True)

    __table_args__ = (Index("ix_audit_events_entity", "tenant_id", "entity_type", "entity_id"),)
