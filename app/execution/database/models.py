"""Execution persistence: runs, form snapshots, form fields (tenant-scoped).

``execution_runs`` is one executor invocation against one attempt and one
preparation; it is the auditable record of what happened. ``form_snapshots``
+ ``form_fields`` are the safe structural picture of a discovered form and
the truthful answer (or the reason we have none) per field. No page HTML,
no credentials, no session material is ever stored here.
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


class ExecutionRunRow(Base):
    __tablename__ = "execution_runs"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    preparation_id = Column(String(36), ForeignKey("application_preparations.id", ondelete="SET NULL"), nullable=True)
    candidate_opportunity_id = Column(String(36), ForeignKey("candidate_opportunities.id", ondelete="SET NULL"), nullable=True)
    opportunity_id = Column(String(36), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True)
    queue_item_id = Column(String(36), ForeignKey("application_queue.id", ondelete="SET NULL"), nullable=True)
    form_snapshot_id = Column(String(36), ForeignKey("form_snapshots.id", ondelete="SET NULL"), nullable=True)
    #: Blueprint Phase 8: the exact document versions this run uploaded.
    resume_artifact_id = Column(String(36), ForeignKey("document_artifacts.id", ondelete="SET NULL"), nullable=True)
    cover_letter_artifact_id = Column(String(36), ForeignKey("document_artifacts.id", ondelete="SET NULL"), nullable=True)
    executor_kind = Column(String(24), nullable=False)
    executor_version = Column(String(64), nullable=False, default="")
    worker_id = Column(String(128), nullable=True)
    #: tenant:application:attempt_number:run_number - unique per invocation.
    idempotency_key = Column(String(200), nullable=False, unique=True)
    run_number = Column(Integer, nullable=False, default=1)
    status = Column(String(24), nullable=False, default="RUNNING")
    outcome = Column(String(24), nullable=True)
    #: Set *before* the executor may press submit; a crash afterwards means
    #: the outcome is UNKNOWN, never "not submitted".
    submit_invoked = Column(Boolean, nullable=False, default=False)
    started_at = Column(DateTime, nullable=False, default=db_now)
    finished_at = Column(DateTime, nullable=True)
    source_url = Column(String(2048), nullable=True)
    application_url = Column(String(2048), nullable=True)
    external_application_id = Column(String(256), nullable=True)
    confirmation_reference = Column(String(512), nullable=True)
    verification_status = Column(String(16), nullable=False, default="NOT_ATTEMPTED")
    verification_method = Column(String(32), nullable=True)
    verification_detail = Column(String(512), nullable=True)
    error_class = Column(String(24), nullable=True)
    error_message = Column(Text, nullable=True)
    handoff_reason = Column(String(32), nullable=True)
    handoff = Column(JSON, nullable=False, default=dict)
    preconditions = Column(JSON, nullable=False, default=list)
    diagnostics = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (
        Index("ix_execution_runs_tenant_status", "tenant_id", "status"),
        Index("ix_execution_runs_tenant_started", "tenant_id", "started_at"),
    )


class FormSnapshotRow(Base):
    __tablename__ = "form_snapshots"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    opportunity_id = Column(String(36), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True)
    executor_kind = Column(String(24), nullable=False)
    executor_version = Column(String(64), nullable=False, default="")
    source_url = Column(String(2048), nullable=False, default="")
    fingerprint = Column(String(64), nullable=False)
    field_count = Column(Integer, nullable=False, default=0)
    metadata_ = Column("metadata", JSON, nullable=False, default=dict)
    captured_at = Column(DateTime, nullable=False, default=db_now)

    fields = relationship("FormFieldRow", back_populates="snapshot", cascade="all, delete-orphan", order_by="FormFieldRow.position")

    __table_args__ = (
        UniqueConstraint("application_id", "fingerprint", name="uq_form_snapshot_application_fingerprint"),
    )


class FormFieldRow(Base):
    __tablename__ = "form_fields"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    snapshot_id = Column(String(36), ForeignKey("form_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False, default=0)
    external_id = Column(String(256), nullable=True)
    label = Column(String(512), nullable=False)
    question_key = Column(String(512), nullable=False)
    field_type = Column(String(16), nullable=False, default="unknown")
    required = Column(Boolean, nullable=False, default=False)
    options = Column(JSON, nullable=False, default=list)
    current_value = Column(String(1024), nullable=True)
    answer = Column(Text, nullable=True)
    selected_values = Column(JSON, nullable=False, default=list)
    artifact_type = Column(String(16), nullable=True)
    source = Column(String(16), nullable=False, default="NONE")
    status = Column(String(24), nullable=False, default="NEEDS_REVIEW")
    category = Column(String(32), nullable=False, default="other")
    preparation_answer_id = Column(String(36), nullable=True)
    evidence_keys = Column(JSON, nullable=False, default=list)
    reason = Column(String(256), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    snapshot = relationship("FormSnapshotRow", back_populates="fields")

    __table_args__ = (Index("ix_form_fields_tenant_status", "tenant_id", "status"),)
