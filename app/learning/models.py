"""Learning models: versions, settings, dataset rows, estimates, snapshots.

Versions stored on every learned record:

* ``LEARNING_VERSION`` — the engine (aggregation + recommendation logic);
* ``FEATURE_VERSION``  — the dataset row definition (joins and labels);
* ``SMOOTHING_METHOD`` — how rates are shrunk and bounded;
* ``ORDERING_VERSION`` — how learned rates become the ``learned_prior``;
* plus the Phase 10 ``OUTCOME_RULES_VERSION`` / ``ATTRIBUTION_VERSION`` the
  events were produced under, the data window and the generation time.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

LEARNING_VERSION = "learning-v1"
FEATURE_VERSION = "features-v1"
SMOOTHING_METHOD = "beta-binomial+wilson-v1"
ORDERING_VERSION = "learned-ordering-v1"


class Dimension(str, Enum):
    GLOBAL = "GLOBAL"
    SOURCE = "SOURCE"
    COMPANY = "COMPANY"
    TITLE = "TITLE"
    ROLE_FAMILY = "ROLE_FAMILY"
    FIT_BAND = "FIT_BAND"
    LANE = "LANE"
    TAILORING_LEVEL = "TAILORING_LEVEL"
    COVER_LETTER_MODE = "COVER_LETTER_MODE"
    POSITIONING_VARIANT = "POSITIONING_VARIANT"
    EXECUTION_METHOD = "EXECUTION_METHOD"


class Metric(str, Enum):
    RESPONSE_RATE = "response_rate"
    INTERVIEW_RATE = "interview_rate"
    REJECTION_RATE = "rejection_rate"
    ASSESSMENT_RATE = "assessment_rate"
    VERIFIED_SUBMISSION_RATE = "verified_submission_rate"
    UNCERTAINTY_RATE = "uncertainty_rate"
    REVIEW_RATE = "review_rate"
    #: Medians, in days (not rates).
    DAYS_TO_RESPONSE = "days_to_response"
    DAYS_TO_REJECTION = "days_to_rejection"


RATE_METRICS: tuple[Metric, ...] = (
    Metric.RESPONSE_RATE,
    Metric.INTERVIEW_RATE,
    Metric.REJECTION_RATE,
    Metric.ASSESSMENT_RATE,
    Metric.VERIFIED_SUBMISSION_RATE,
    Metric.UNCERTAINTY_RATE,
    Metric.REVIEW_RATE,
)
MEDIAN_METRICS: tuple[Metric, ...] = (Metric.DAYS_TO_RESPONSE, Metric.DAYS_TO_REJECTION)


class LearningConfidence(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EvidenceQuality(str, Enum):
    """The strongest Phase 10 evidence behind a row's labels."""

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


EVIDENCE_RANK = {EvidenceQuality.STRONG: 3, EvidenceQuality.MODERATE: 2, EvidenceQuality.WEAK: 1, EvidenceQuality.NONE: 0}


class OutcomeCompleteness(str, Enum):
    TERMINAL = "terminal"
    PROGRESS = "progress"
    SUBMITTED_ONLY = "submitted_only"
    UNVERIFIED = "unverified"


class TenantLearningSettings(BaseModel):
    """Per-tenant learning settings on the application policy. Nothing here
    changes what is applied to; ``ordering_enabled`` is the only switch that
    lets a learned signal influence anything, and only processing order."""

    ordering_enabled: bool = False
    #: Rolling window in days (None = all history). Old snapshots keep the window they used.
    window_days: Optional[int] = Field(default=None, ge=1, le=3650)
    #: Below this many samples a group is LOW confidence and produces no recommendation.
    min_samples: int = Field(default=5, ge=1, le=10_000)
    #: Beta-binomial prior strength (pseudo-observations pulled toward the tenant baseline).
    prior_strength: float = Field(default=10.0, ge=0.0, le=1000.0)
    #: Weakest Phase 10 evidence that may label an outcome (WEAK = exploratory only).
    minimum_evidence: EvidenceQuality = EvidenceQuality.MODERATE


class LearningRow(BaseModel):
    """One executed application as the engine sees it at ``as_of``."""

    application_id: str
    candidate_opportunity_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    job_id: Optional[str] = None
    company: str = ""
    company_key: str = ""
    title: str = ""
    title_key: str = ""
    role_family: Optional[str] = None
    fit_band: Optional[str] = None
    fit_score: Optional[int] = None
    policy_version: Optional[int] = None
    gate_ruleset_version: Optional[str] = None
    source: str = "UNKNOWN"
    tailoring_level: Optional[str] = None
    lane: Optional[str] = None
    cover_letter_mode: Optional[str] = None
    positioning_variant_id: Optional[str] = None
    execution_method: str = "UNKNOWN"
    verification_status: Optional[str] = None
    attempt_status: str = ""
    submitted_at: Optional[datetime] = None
    verified: bool = False
    uncertain: bool = False
    execution_needs_review: bool = False
    responded: bool = False
    interviewed: bool = False
    rejected: bool = False
    assessed: bool = False
    withdrawn: bool = False
    days_to_response: Optional[float] = None
    days_to_rejection: Optional[float] = None
    evidence_quality: EvidenceQuality = EvidenceQuality.NONE
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    outcome_completeness: OutcomeCompleteness = OutcomeCompleteness.SUBMITTED_ONLY
    events_counted: int = 0
    events_below_threshold: int = 0
    as_of: Optional[datetime] = None
    feature_version: str = FEATURE_VERSION


class RateEstimate(BaseModel):
    metric: Metric
    n: int
    positives: int
    negatives: int
    raw_rate: Optional[float] = None
    smoothed_rate: float
    ci_low: float
    ci_high: float
    confidence: LearningConfidence
    baseline_rate: float
    prior_strength: float
    method: str = SMOOTHING_METHOD


class GroupMetrics(BaseModel):
    dimension: Dimension
    group_key: str
    group_label: str
    n: int
    estimates: dict[str, RateEstimate] = Field(default_factory=dict)
    medians: dict[str, Optional[float]] = Field(default_factory=dict)
    median_samples: dict[str, int] = Field(default_factory=dict)
    evidence: dict[str, int] = Field(default_factory=dict)
    confidence: LearningConfidence = LearningConfidence.NONE


class Recommendation(BaseModel):
    kind: str
    text: str
    dimension: Dimension
    group_key: str
    group_label: str
    metric: Metric
    n: int
    positives: int
    observed_rate: Optional[float] = None
    smoothed_rate: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    baseline_rate: Optional[float] = None
    confidence: LearningConfidence
    evidence: dict[str, int] = Field(default_factory=dict)
    learning_version: str = LEARNING_VERSION
    window_days: Optional[int] = None
    as_of: Optional[datetime] = None
    caveat: str = "observed association in this candidate's own history; not a causal claim"


class LearningResult(BaseModel):
    tenant_id: str
    learning_version: str = LEARNING_VERSION
    feature_version: str = FEATURE_VERSION
    smoothing_method: str = SMOOTHING_METHOD
    outcome_rules_version: str
    attribution_version: str
    as_of: datetime
    window_days: Optional[int] = None
    window_start: Optional[datetime] = None
    dataset_size: int = 0
    settings: dict[str, Any] = Field(default_factory=dict)
    baseline: GroupMetrics
    groups: list[GroupMetrics] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    source_discovery: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class ExpectedResponse(BaseModel):
    """The optional learned ordering signal (0–100, 50 = neutral). A signal,
    never a gate: it feeds ``learned_prior`` only when ordering is enabled."""

    candidate_opportunity_id: Optional[str] = None
    enabled: bool
    score: float = 50.0
    snapshot_id: Optional[str] = None
    components: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str = ""
    learning_version: str = LEARNING_VERSION
    ordering_version: str = ORDERING_VERSION


# --------------------------------------------------------------------- #
# read models
# --------------------------------------------------------------------- #


class LearningSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    learning_version: str
    feature_version: str
    smoothing_method: str
    outcome_rules_version: str
    attribution_version: str
    as_of: Optional[datetime] = None
    window_days: Optional[int] = None
    window_start: Optional[datetime] = None
    generated_at: Optional[datetime] = None
    dataset_size: int = 0
    metric_count: int = 0
    recommendation_count: int = 0
    settings: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    actor: str = "learning"


class LearningMetric(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    snapshot_id: str
    dimension: Dimension
    group_key: str
    group_label: str
    metric: Metric
    n: int
    positives: Optional[int] = None
    negatives: Optional[int] = None
    raw_rate: Optional[float] = None
    smoothed_rate: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    median_value: Optional[float] = None
    confidence: LearningConfidence
    baseline_rate: Optional[float] = None
    evidence: dict[str, int] = Field(default_factory=dict)
    learning_version: str
    feature_version: str
    smoothing_method: str


class LearningRecommendation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    snapshot_id: str
    kind: str
    text: str
    dimension: Dimension
    group_key: str
    group_label: str
    metric: Metric
    n: int
    positives: int
    observed_rate: Optional[float] = None
    baseline_rate: Optional[float] = None
    confidence: LearningConfidence
    evidence: dict[str, int] = Field(default_factory=dict)
    learning_version: str
    window_days: Optional[int] = None
    as_of: Optional[datetime] = None
    caveat: str
