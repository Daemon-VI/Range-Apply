"""Domain models and enums for opportunities, decisions, policy and the queue.

Three concepts that must never be conflated (blueprint §4, §8):

* **Eligibility** answers *may this candidate apply at all?*
* **Fit** answers *how relevant is it?* (Phase 3's score, 0–100, banded).
* **Priority** answers *in what order do we process it?* (0–100).

Priority never removes an opportunity from an enabled band; the policy's
``enabled_bands`` and ``minimum_eligibility`` are the only admission gates,
and both are the candidate's settings.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.ai.models import TenantAISettings
from app.learning.models import TenantLearningSettings


class OpportunityStatus(str, Enum):
    """Shared, market-side status of the real-world opening."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REPOSTED = "REPOSTED"


class OpportunityState(str, Enum):
    """Candidate-side relationship with an opportunity.

    ``Application`` (``app/application``) owns the *submission attempt*
    lifecycle; this state is the broader candidate/job relationship that an
    application attempt advances. Terminology matches ``ApplicationStatus``
    where the two overlap.
    """

    DISCOVERED = "DISCOVERED"
    ELIGIBLE = "ELIGIBLE"
    UNCERTAIN = "UNCERTAIN"
    INELIGIBLE = "INELIGIBLE"
    SHORTLISTED = "SHORTLISTED"
    PREPARED = "PREPARED"
    QUEUED = "QUEUED"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    INTERVIEWING = "INTERVIEWING"
    OFFER = "OFFER"
    CLOSED = "CLOSED"
    SKIPPED = "SKIPPED"


#: States the eligibility/fit sync may overwrite. Anything further along is
#: owned by the application flow and is never regressed by a re-evaluation.
EVALUATION_STATES = frozenset(
    {
        OpportunityState.DISCOVERED,
        OpportunityState.ELIGIBLE,
        OpportunityState.UNCERTAIN,
        OpportunityState.INELIGIBLE,
    }
)

ALLOWED_TRANSITIONS: dict[OpportunityState, frozenset[OpportunityState]] = {
    OpportunityState.DISCOVERED: frozenset(
        {
            OpportunityState.ELIGIBLE,
            OpportunityState.UNCERTAIN,
            OpportunityState.INELIGIBLE,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.ELIGIBLE: frozenset(
        {
            OpportunityState.UNCERTAIN,
            OpportunityState.INELIGIBLE,
            OpportunityState.SHORTLISTED,
            OpportunityState.PREPARED,
            OpportunityState.QUEUED,
            OpportunityState.IN_REVIEW,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.UNCERTAIN: frozenset(
        {
            OpportunityState.ELIGIBLE,
            OpportunityState.INELIGIBLE,
            OpportunityState.SHORTLISTED,
            OpportunityState.PREPARED,
            OpportunityState.QUEUED,
            OpportunityState.IN_REVIEW,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.INELIGIBLE: frozenset(
        {
            OpportunityState.ELIGIBLE,
            OpportunityState.UNCERTAIN,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.SHORTLISTED: frozenset(
        {
            OpportunityState.PREPARED,
            OpportunityState.QUEUED,
            OpportunityState.IN_REVIEW,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.PREPARED: frozenset(
        {
            OpportunityState.QUEUED,
            OpportunityState.IN_REVIEW,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.QUEUED: frozenset(
        {
            OpportunityState.PREPARED,
            OpportunityState.IN_REVIEW,
            OpportunityState.APPROVED,
            OpportunityState.SUBMITTING,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.IN_REVIEW: frozenset(
        {
            OpportunityState.APPROVED,
            OpportunityState.PREPARED,
            OpportunityState.QUEUED,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.APPROVED: frozenset(
        {
            OpportunityState.SUBMITTING,
            OpportunityState.QUEUED,
            OpportunityState.SKIPPED,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.SUBMITTING: frozenset(
        {
            OpportunityState.SUBMITTED,
            OpportunityState.VERIFICATION_PENDING,
            OpportunityState.QUEUED,
            OpportunityState.IN_REVIEW,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.SUBMITTED: frozenset(
        {
            OpportunityState.VERIFICATION_PENDING,
            OpportunityState.VERIFIED,
            OpportunityState.REJECTED,
            OpportunityState.INTERVIEWING,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.VERIFICATION_PENDING: frozenset(
        {
            OpportunityState.VERIFIED,
            OpportunityState.SUBMITTED,
            OpportunityState.REJECTED,
            OpportunityState.INTERVIEWING,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.VERIFIED: frozenset(
        {
            OpportunityState.INTERVIEWING,
            OpportunityState.REJECTED,
            OpportunityState.OFFER,
            OpportunityState.CLOSED,
        }
    ),
    OpportunityState.INTERVIEWING: frozenset(
        {OpportunityState.OFFER, OpportunityState.REJECTED, OpportunityState.CLOSED}
    ),
    OpportunityState.OFFER: frozenset({OpportunityState.REJECTED, OpportunityState.CLOSED}),
    OpportunityState.REJECTED: frozenset({OpportunityState.CLOSED}),
    OpportunityState.CLOSED: frozenset(),
    OpportunityState.SKIPPED: frozenset(
        {OpportunityState.DISCOVERED, OpportunityState.ELIGIBLE, OpportunityState.UNCERTAIN}
    ),
}


class EligibilityDecision(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    LIKELY = "LIKELY"
    UNCERTAIN = "UNCERTAIN"
    INELIGIBLE = "INELIGIBLE"
    REVIEW = "REVIEW"


#: Ordering used by ``minimum_eligibility``: a decision admits when it ranks
#: at or above the configured minimum. UNCERTAIN and REVIEW sit *between*
#: LIKELY and INELIGIBLE so they are never rounded down to INELIGIBLE.
ELIGIBILITY_RANK: dict[EligibilityDecision, int] = {
    EligibilityDecision.ELIGIBLE: 4,
    EligibilityDecision.LIKELY: 3,
    EligibilityDecision.UNCERTAIN: 2,
    EligibilityDecision.REVIEW: 2,
    EligibilityDecision.INELIGIBLE: 0,
}


class FitBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AdmissionReason(str, Enum):
    """Why the policy admitted or refused an opportunity. Every scheduler
    decision carries exactly one of these; the string ``reason`` adds detail."""

    ADMITTED = "ADMITTED"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_SCORED = "NOT_SCORED"
    INELIGIBLE = "INELIGIBLE"
    BELOW_MINIMUM_ELIGIBILITY = "BELOW_MINIMUM_ELIGIBILITY"
    BAND_DISABLED = "BAND_DISABLED"
    BELOW_FIT_THRESHOLD = "BELOW_FIT_THRESHOLD"
    COMPANY_BLOCKED = "COMPANY_BLOCKED"
    #: The posting is outside the candidate's geographic target (or not confirmed inside it).
    OUTSIDE_TARGET_GEOGRAPHY = "OUTSIDE_TARGET_GEOGRAPHY"
    #: The role is outside every family the candidate targets (marketing for an engineer).
    IRRELEVANT_ROLE = "IRRELEVANT_ROLE"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    USER_BLOCKED = "USER_BLOCKED"
    OPPORTUNITY_CLOSED = "OPPORTUNITY_CLOSED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    WEEKLY_CAP_REACHED = "WEEKLY_CAP_REACHED"
    #: Admissible, but this run's processing window is full: next run, not a rejection.
    WINDOW_DEFERRED = "WINDOW_DEFERRED"


class TailoringLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class Lane(str, Enum):
    AUTO = "AUTO"
    REVIEW = "REVIEW"
    MANUAL = "MANUAL"


class DuplicatePolicy(str, Enum):
    BLOCK = "BLOCK"
    ALLOW_REPOST_AFTER_COOLDOWN = "ALLOW_REPOST_AFTER_COOLDOWN"


class QueueAction(str, Enum):
    PREPARE = "PREPARE"
    SUBMIT = "SUBMIT"
    VERIFY = "VERIFY"


class QueueState(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCELLED = "CANCELLED"


ACTIVE_QUEUE_STATES = frozenset(
    {
        QueueState.PENDING,
        QueueState.CLAIMED,
        QueueState.PROCESSING,
        QueueState.RETRY_WAIT,
        QueueState.BLOCKED,
        QueueState.NEEDS_REVIEW,
    }
)
TERMINAL_QUEUE_STATES = frozenset({QueueState.SUCCEEDED, QueueState.FAILED, QueueState.CANCELLED})


# ---------------------------------------------------------------------- #
# read models
# ---------------------------------------------------------------------- #


class Opportunity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    identity_key: str
    canonical_job_id: str
    company: str
    title: str
    location_bucket: str
    status: OpportunityStatus
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    repost_count: int = 0
    job_count: int = 1


class EligibilityDecisionRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_opportunity_id: str
    job_id: str
    job_content_hash: Optional[str] = None
    decision: EligibilityDecision
    confidence: str
    reason_codes: list[str] = Field(default_factory=list)
    matched_constraints: list[dict[str, Any]] = Field(default_factory=list)
    failed_constraints: list[dict[str, Any]] = Field(default_factory=list)
    uncertain_constraints: list[dict[str, Any]] = Field(default_factory=list)
    ruleset_version: str
    evaluated_at: Optional[datetime] = None


class PriorityScoreRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_opportunity_id: str
    score: int
    #: Per-component inputs plus the normalised ``weights`` used, so a stored
    #: score can be reproduced exactly.
    components: dict[str, Any] = Field(default_factory=dict)
    weights_version: str
    computed_at: Optional[datetime] = None


class CandidateOpportunity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    opportunity_id: str
    state: OpportunityState
    eligibility_status: Optional[EligibilityDecision] = None
    eligibility_decision_id: Optional[str] = None
    fit_score: Optional[int] = None
    fit_band: Optional[FitBand] = None
    match_id: Optional[str] = None
    priority_score: Optional[int] = None
    priority_score_id: Optional[str] = None
    application_id: Optional[str] = None
    policy_admitted: Optional[bool] = None
    policy_reason: Optional[str] = None
    skipped_reason: Optional[str] = None
    scheduler_code: Optional[str] = None
    scheduler_reason: Optional[str] = None
    scheduler_run_id: Optional[str] = None
    scheduler_decided_at: Optional[datetime] = None
    #: Phase 8b: the configuration versions the stored band / admission were derived under.
    fit_policy_version: Optional[int] = None
    admission_policy_version: Optional[int] = None
    gate_ruleset_version: Optional[str] = None
    state_changed_at: Optional[datetime] = None
    last_evaluated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ApplicationPolicy(BaseModel):
    """Candidate-controlled application policy (blueprint §4 bands, §8 lanes)."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    enabled_bands: list[FitBand] = Field(default_factory=lambda: [FitBand.HIGH, FitBand.MEDIUM, FitBand.LOW])
    band_thresholds: dict[str, int] = Field(default_factory=lambda: {"HIGH": 70, "MEDIUM": 45})
    daily_cap: int = 50
    weekly_cap: int = 300
    tailoring_by_band: dict[str, TailoringLevel] = Field(
        default_factory=lambda: {"HIGH": TailoringLevel.L2, "MEDIUM": TailoringLevel.L1, "LOW": TailoringLevel.L0}
    )
    lane_by_band: dict[str, Lane] = Field(
        default_factory=lambda: {"HIGH": Lane.REVIEW, "MEDIUM": Lane.REVIEW, "LOW": Lane.REVIEW}
    )
    #: DISABLED | TEMPLATE | LIGHT | TARGETED per band (blueprint §6): the
    #: high-volume LOW lane spends nothing on letters by default.
    cover_letter_by_band: dict[str, str] = Field(
        default_factory=lambda: {"HIGH": "LIGHT", "MEDIUM": "TEMPLATE", "LOW": "DISABLED"}
    )
    cooldown_days: int = 90
    blocked_companies: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_role_families: list[str] = Field(default_factory=list)
    minimum_eligibility: EligibilityDecision = EligibilityDecision.UNCERTAIN
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.BLOCK
    priority_weights: dict[str, float] = Field(default_factory=dict)
    #: Optional explicit floor on fit score (None = bands alone decide). This is
    #: the only fit-based exclusion and it is the candidate's own setting.
    minimum_fit_score: Optional[int] = None
    #: IANA zone for day/week cap boundaries.
    timezone: str = "UTC"
    #: Phase 8b: per-tenant AI switch and limits (narrow the global config only).
    ai_settings: TenantAISettings = Field(default_factory=TenantAISettings)
    #: Phase 11: learning settings. Only ``ordering_enabled`` has any effect on
    #: behaviour, and only on processing order (never admission or volume).
    learning_settings: TenantLearningSettings = Field(default_factory=TenantLearningSettings)
    version: int = 1
    updated_at: Optional[datetime] = None


class FitBandRange(BaseModel):
    band: FitBand
    min_score: int
    max_score: int
    enabled: bool
    lane: Lane
    tailoring_level: TailoringLevel
    cover_letter: str


class FitBandConfig(BaseModel):
    """The fit-band configuration as a first-class, versioned object (Phase 8b).

    Bands are computed from ``band_thresholds`` exactly as before
    (``band_for``): HIGH strictly above the HIGH threshold, MEDIUM from the
    MEDIUM threshold up to and including HIGH, LOW below. ``version`` is the
    policy version that carries these values; candidate opportunities record
    the version their band was derived under (``fit_policy_version``).
    """

    tenant_id: str
    version: int
    thresholds: dict[str, int]
    enabled_bands: list[FitBand]
    bands: list[FitBandRange]
    minimum_fit_score: Optional[int] = None
    #: Where the underlying fit score comes from; changing it never rewrites stored matches.
    fit_score_source: str = "job_matches.fit_score (deterministic matcher)"


class FitBandConfigUpdate(BaseModel):
    thresholds: Optional[dict[str, int]] = None
    enabled_bands: Optional[list[FitBand]] = None
    minimum_fit_score: Optional[int] = Field(default=None, ge=0, le=100)
    #: Explicitly clear the floor (``minimum_fit_score`` cannot express None).
    clear_minimum_fit_score: bool = False


class ApplicationPolicyUpdate(BaseModel):
    enabled_bands: Optional[list[FitBand]] = None
    band_thresholds: Optional[dict[str, int]] = None
    daily_cap: Optional[int] = Field(default=None, ge=0)
    weekly_cap: Optional[int] = Field(default=None, ge=0)
    tailoring_by_band: Optional[dict[str, TailoringLevel]] = None
    lane_by_band: Optional[dict[str, Lane]] = None
    cover_letter_by_band: Optional[dict[str, str]] = None
    cooldown_days: Optional[int] = Field(default=None, ge=0)
    blocked_companies: Optional[list[str]] = None
    preferred_locations: Optional[list[str]] = None
    preferred_role_families: Optional[list[str]] = None
    minimum_eligibility: Optional[EligibilityDecision] = None
    duplicate_policy: Optional[DuplicatePolicy] = None
    priority_weights: Optional[dict[str, float]] = None
    minimum_fit_score: Optional[int] = Field(default=None, ge=0, le=100)
    timezone: Optional[str] = None
    ai_settings: Optional[TenantAISettings] = None
    learning_settings: Optional[TenantLearningSettings] = None


class QueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_opportunity_id: str
    opportunity_id: str
    action: QueueAction
    state: QueueState
    lane: Lane
    priority: int
    idempotency_key: str
    available_at: Optional[datetime] = None
    attempts: int = 0
    max_attempts: int = 3
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    last_error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
