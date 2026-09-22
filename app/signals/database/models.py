"""Signal Inbox persistence (all tenant-scoped, Blueprint Phase 10).

``signals``               one logical signal; deduplicated per tenant on
                          ``dedupe_key`` (a stable external reference when the
                          source has one, else the content hash).
``signal_observations``   every delivery of that signal (provenance of repeats).
``signal_attributions``   append-only history of signal → application decisions.
``outcome_events``        append-only history of what signals said happened to
                          an application, with evidence strength; never deleted,
                          only superseded or retracted (both leave the row).
``application_outcomes``  the derived current status per application,
                          recomputed from the events (a cache of a pure function).

Stored content is an excerpt of normalized text with credential-shaped
fragments redacted; never raw HTML, headers, cookies or tokens.
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
from sqlalchemy.types import JSON

from app.core.ids import new_id
from app.core.timeutils import db_now, ordered_db_now
from app.jobs.database.models import Base


class SignalRow(Base):
    __tablename__ = "signals"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(16), nullable=False)
    source_reference = Column(String(256), nullable=True)
    content_hash = Column(String(64), nullable=False)
    dedupe_key = Column(String(200), nullable=False)
    subject = Column(String(512), nullable=True)
    sender = Column(String(256), nullable=True)
    sender_domain = Column(String(128), nullable=True)
    excerpt = Column(Text, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    external_at = Column(DateTime, nullable=True)
    observed_at = Column(DateTime, nullable=False, default=db_now)
    observation_count = Column(Integer, nullable=False, default=1)
    last_observed_at = Column(DateTime, nullable=False, default=db_now)
    category = Column(String(32), nullable=False, default="UNKNOWN")
    confidence = Column(String(8), nullable=False, default="NONE")
    classification_source = Column(String(16), nullable=True)
    classifier_version = Column(String(32), nullable=True)
    classification = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="NEW")
    status_reason = Column(String(256), nullable=True)
    attribution_status = Column(String(16), nullable=False, default="UNMATCHED")
    attribution_id = Column(String(36), nullable=True)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    opportunity_id = Column(String(36), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True)
    candidate_opportunity_id = Column(String(36), ForeignKey("candidate_opportunities.id", ondelete="SET NULL"), nullable=True)
    execution_run_id = Column(String(36), ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True)
    merged_into_id = Column(String(36), nullable=True)
    ai = Column(JSON, nullable=False, default=dict)
    schema_version = Column(String(16), nullable=False, default="signals-v1")
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_signals_tenant_dedupe"),
        Index("ix_signals_tenant_status", "tenant_id", "status"),
        Index("ix_signals_tenant_application", "tenant_id", "application_id"),
        Index("ix_signals_tenant_observed", "tenant_id", "observed_at"),
        Index("ix_signals_tenant_source", "tenant_id", "source"),
        Index("ix_signals_tenant_category", "tenant_id", "category"),
        Index("ix_signals_tenant_reference", "tenant_id", "source_reference"),
    )


class SignalObservationRow(Base):
    __tablename__ = "signal_observations"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    signal_id = Column(String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(16), nullable=False)
    source_reference = Column(String(256), nullable=True)
    content_hash = Column(String(64), nullable=False)
    # Strictly increasing within the process: history rows written in one
    # clock tick must still list in write order (same convention as audit rows).
    observed_at = Column(DateTime, nullable=False, default=ordered_db_now)
    provenance = Column(JSON, nullable=False, default=dict)


class SignalAttributionRow(Base):
    __tablename__ = "signal_attributions"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    signal_id = Column(String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), nullable=False)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    opportunity_id = Column(String(36), nullable=True)
    candidate_opportunity_id = Column(String(36), nullable=True)
    rule = Column(String(48), nullable=False, default="none")
    confidence = Column(String(8), nullable=False, default="NONE")
    evidence = Column(JSON, nullable=False, default=dict)
    candidates = Column(JSON, nullable=False, default=list)
    explanation = Column(String(512), nullable=True)
    attribution_version = Column(String(32), nullable=False)
    actor = Column(String(128), nullable=False, default="system")
    superseded_by_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=ordered_db_now)

    __table_args__ = (Index("ix_signal_attributions_tenant_application", "tenant_id", "application_id"),)


class OutcomeEventRow(Base):
    __tablename__ = "outcome_events"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    signal_id = Column(String(36), ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True)
    attribution_id = Column(String(36), nullable=True)
    outcome = Column(String(32), nullable=False)
    evidence = Column(String(8), nullable=False)
    origin = Column(String(16), nullable=False)
    category = Column(String(32), nullable=True)
    event_at = Column(DateTime, nullable=False, default=db_now)
    time_basis = Column(String(8), nullable=False, default="observed")
    observed_at = Column(DateTime, nullable=False, default=db_now)
    sequence = Column(Integer, nullable=False, default=0)
    #: ``signal:application:outcome:origin`` — the same signal never yields the same event twice.
    dedupe_key = Column(String(200), nullable=False)
    actor = Column(String(128), nullable=False, default="system")
    note = Column(String(512), nullable=True)
    classifier_version = Column(String(32), nullable=True)
    attribution_version = Column(String(32), nullable=True)
    rules_version = Column(String(32), nullable=False)
    superseded_by_id = Column(String(36), nullable=True)
    retracted = Column(Boolean, nullable=False, default=False)
    retracted_reason = Column(String(256), nullable=True)
    created_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_outcome_events_tenant_dedupe"),
        Index("ix_outcome_events_tenant_application", "tenant_id", "application_id", "sequence"),
    )


class ApplicationOutcomeRow(Base):
    __tablename__ = "application_outcomes"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    application_id = Column(String(36), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    opportunity_id = Column(String(36), nullable=True)
    candidate_opportunity_id = Column(String(36), nullable=True)
    current_outcome = Column(String(32), nullable=False, default="UNKNOWN")
    provisional_outcome = Column(String(32), nullable=True)
    needs_review = Column(Boolean, nullable=False, default=False)
    conflicts = Column(JSON, nullable=False, default=list)
    event_count = Column(Integer, nullable=False, default=0)
    basis_event_id = Column(String(36), nullable=True)
    last_event_at = Column(DateTime, nullable=True)
    derived_at = Column(DateTime, nullable=False, default=db_now)
    rules_version = Column(String(32), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "application_id", name="uq_application_outcomes_tenant_application"),
        Index("ix_application_outcomes_tenant_current", "tenant_id", "current_outcome"),
        Index("ix_application_outcomes_tenant_review", "tenant_id", "needs_review"),
    )
