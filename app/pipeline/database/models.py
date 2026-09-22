"""SQLAlchemy rows for opportunities, decisions, policy and the queue.

SHARED (no tenant): ``opportunities``, ``opportunity_jobs`` — one real-world
opening and the source job rows that resolved to it. Never duplicated per
candidate.

TENANT: ``candidate_opportunities``, ``eligibility_decisions``,
``priority_scores``, ``application_policies``, ``application_queue``.

Index choices follow the queries the dashboard and the queue will run at
volume: per-tenant state/band/priority listings, and the queue claim scan
``(tenant, state, available_at)`` ordered by priority.
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


class OpportunityRow(Base):
    """Shared identity for one real-world opening (blueprint §5)."""

    __tablename__ = "opportunities"

    id = Column(String(36), primary_key=True, default=new_id)
    identity_key = Column(String(64), nullable=False, unique=True)
    canonical_job_id = Column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company = Column(String(256), nullable=False, index=True)
    #: Normalised company identity (``company_key()``) for indexed blocklist /
    #: cool-down / duplicate lookups; backfilled by the Phase 6 migration.
    company_key = Column(String(256), nullable=True, index=True)
    title = Column(String(512), nullable=False)
    location_bucket = Column(String(256), nullable=False, default="")
    status = Column(String(16), nullable=False, default="OPEN", index=True)
    first_seen_at = Column(DateTime, nullable=False, default=db_now)
    last_seen_at = Column(DateTime, nullable=False, default=db_now, index=True)
    deadline = Column(DateTime)
    repost_count = Column(Integer, nullable=False, default=0)
    reposted_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    jobs = relationship("OpportunityJobRow", back_populates="opportunity", cascade="all, delete-orphan")


class OpportunityJobRow(Base):
    """Which source job rows resolved to which opportunity, and how."""

    __tablename__ = "opportunity_jobs"

    id = Column(String(36), primary_key=True, default=new_id)
    opportunity_id = Column(
        String(36), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    linked_by = Column(String(32), nullable=False)
    is_repost = Column(Boolean, nullable=False, default=False)
    linked_at = Column(DateTime, nullable=False, default=db_now)

    opportunity = relationship("OpportunityRow", back_populates="jobs")
    job = relationship("JobRow")


class CandidateOpportunityRow(Base):
    """One candidate's relationship with one opportunity (blueprint §4 states)."""

    __tablename__ = "candidate_opportunities"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    opportunity_id = Column(
        String(36), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state = Column(String(32), nullable=False, default="DISCOVERED")
    eligibility_status = Column(String(16))
    eligibility_decision_id = Column(String(36))
    fit_score = Column(Integer)
    fit_band = Column(String(8))
    match_id = Column(String(36))
    priority_score = Column(Integer)
    priority_score_id = Column(String(36))
    application_id = Column(String(36))
    policy_admitted = Column(Boolean)
    policy_reason = Column(String(256))
    skipped_reason = Column(String(256))
    # Blueprint Phase 5: the scheduler's latest decision (code + detail),
    # separate from ``policy_admitted`` which is the static policy verdict.
    scheduler_code = Column(String(32))
    scheduler_reason = Column(String(256))
    scheduler_run_id = Column(String(36))
    scheduler_decided_at = Column(DateTime)
    # Blueprint Phase 8b: which configuration produced the stored band and the
    # latest static admission, so a threshold or gate change never silently
    # reinterprets an older decision.
    fit_policy_version = Column(Integer)
    admission_policy_version = Column(Integer)
    gate_ruleset_version = Column(String(32))
    state_changed_at = Column(DateTime, nullable=False, default=db_now)
    last_evaluated_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    opportunity = relationship("OpportunityRow")

    __table_args__ = (
        UniqueConstraint("tenant_id", "opportunity_id", name="uq_candidate_opportunity"),
        Index("ix_candidate_opportunities_tenant_state", "tenant_id", "state"),
        Index("ix_candidate_opportunities_tenant_band", "tenant_id", "fit_band"),
        Index("ix_candidate_opportunities_tenant_elig", "tenant_id", "eligibility_status"),
        Index("ix_candidate_opportunities_tenant_priority", "tenant_id", "priority_score"),
        Index("ix_candidate_opportunities_tenant_sched", "tenant_id", "scheduler_code"),
    )


class EligibilityDecisionRow(Base):
    """Tier 1 outcome with inspectable reasons (blueprint §4)."""

    __tablename__ = "eligibility_decisions"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    candidate_opportunity_id = Column(
        String(36),
        ForeignKey("candidate_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    job_content_hash = Column(String(64))
    decision = Column(String(16), nullable=False)
    confidence = Column(String(16), nullable=False, default="UNKNOWN")
    reason_codes = Column(JSON, nullable=False, default=list)
    matched_constraints = Column(JSON, nullable=False, default=list)
    failed_constraints = Column(JSON, nullable=False, default=list)
    uncertain_constraints = Column(JSON, nullable=False, default=list)
    ruleset_version = Column(String(32), nullable=False)
    evaluated_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (
        Index("ix_eligibility_decisions_tenant_decision", "tenant_id", "decision"),
        Index("ix_eligibility_decisions_co_evaluated", "candidate_opportunity_id", "evaluated_at"),
    )


class PriorityScoreRow(Base):
    """Processing-order score, separate from fit (blueprint §8)."""

    __tablename__ = "priority_scores"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    candidate_opportunity_id = Column(
        String(36),
        ForeignKey("candidate_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    score = Column(Integer, nullable=False)
    components = Column(JSON, nullable=False, default=dict)
    weights_version = Column(String(32), nullable=False)
    computed_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (Index("ix_priority_scores_tenant_score", "tenant_id", "score"),)


class ApplicationPolicyRow(Base):
    """Candidate-controlled aggressiveness and lanes; one row per tenant."""

    __tablename__ = "application_policies"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    enabled_bands = Column(JSON, nullable=False, default=list)
    band_thresholds = Column(JSON, nullable=False, default=dict)
    daily_cap = Column(Integer, nullable=False, default=50)
    weekly_cap = Column(Integer, nullable=False, default=300)
    tailoring_by_band = Column(JSON, nullable=False, default=dict)
    lane_by_band = Column(JSON, nullable=False, default=dict)
    cover_letter_by_band = Column(JSON, nullable=True)
    cooldown_days = Column(Integer, nullable=False, default=90)
    blocked_companies = Column(JSON, nullable=False, default=list)
    preferred_locations = Column(JSON, nullable=False, default=list)
    preferred_role_families = Column(JSON, nullable=False, default=list)
    minimum_eligibility = Column(String(16), nullable=False, default="UNCERTAIN")
    duplicate_policy = Column(String(32), nullable=False, default="BLOCK")
    priority_weights = Column(JSON, nullable=False, default=dict)
    minimum_fit_score = Column(Integer, nullable=True)
    timezone = Column(String(64), nullable=True)
    # Blueprint Phase 8b: tenant AI switch + limits (never credentials).
    ai_settings = Column(JSON, nullable=True)
    # Blueprint Phase 11: learning settings (ordering opt-in, window, smoothing).
    learning_settings = Column(JSON, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=db_now)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)


class ApplicationQueueRow(Base):
    """Database-backed work queue (blueprint §9). No Redis.

    ``idempotency_key`` = ``tenant:opportunity:action`` and the unique
    constraint on ``(tenant_id, opportunity_id, action)`` are what stop the
    same candidate submitting the same real-world opening twice, whatever
    source rows it arrived through.
    """

    __tablename__ = "application_queue"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    candidate_opportunity_id = Column(
        String(36),
        ForeignKey("candidate_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opportunity_id = Column(
        String(36), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    action = Column(String(16), nullable=False)
    state = Column(String(16), nullable=False, default="PENDING")
    lane = Column(String(8), nullable=False, default="REVIEW")
    priority = Column(Integer, nullable=False, default=0)
    idempotency_key = Column(String(160), nullable=False, unique=True)
    available_at = Column(DateTime, nullable=False, default=db_now)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    claimed_by = Column(String(128))
    claimed_at = Column(DateTime)
    lease_expires_at = Column(DateTime)
    last_error = Column(Text)
    result = Column(JSON)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=db_now, index=True)
    updated_at = Column(DateTime, nullable=False, default=db_now, onupdate=db_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "opportunity_id", "action", name="uq_queue_tenant_opportunity_action"),
        Index("ix_application_queue_claim", "tenant_id", "state", "available_at", "priority"),
        Index("ix_application_queue_tenant_lane_state", "tenant_id", "lane", "state"),
    )
