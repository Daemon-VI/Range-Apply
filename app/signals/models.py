"""Domain models for signals, attribution and outcomes (Blueprint Phase 10).

Versions are explicit and stored on every row they influenced:

* ``SIGNAL_SCHEMA_VERSION`` — the shape of a stored signal;
* ``CLASSIFIER_VERSION`` — the deterministic rule set (bumped with the rules);
* ``ATTRIBUTION_VERSION`` — the signal → application rule order;
* ``OUTCOME_RULES_VERSION`` — how the current status is derived from events.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

SIGNAL_SCHEMA_VERSION = "signals-v1"
CLASSIFIER_VERSION = "signal-classifier-v1"
ATTRIBUTION_VERSION = "signal-attribution-v1"
OUTCOME_RULES_VERSION = "outcome-rules-v1"


class SignalSource(str, Enum):
    """Where a signal was observed. Trust is *not* implied by the source; the
    evidence strength is decided per signal from what it actually carries."""

    EXECUTION = "EXECUTION"
    EMAIL = "EMAIL"
    STATUS_PAGE = "STATUS_PAGE"
    MANUAL = "MANUAL"
    EXTENSION = "EXTENSION"
    PLAYWRIGHT = "PLAYWRIGHT"


class SignalCategory(str, Enum):
    """What the signal says (classification)."""

    APPLICATION_CONFIRMATION = "APPLICATION_CONFIRMATION"
    APPLICATION_RECEIVED = "APPLICATION_RECEIVED"
    REJECTION = "REJECTION"
    INTERVIEW_INVITATION = "INTERVIEW_INVITATION"
    RECRUITER_CONTACT = "RECRUITER_CONTACT"
    ASSESSMENT = "ASSESSMENT"
    INFORMATION_REQUEST = "INFORMATION_REQUEST"
    WITHDRAWAL = "WITHDRAWAL"
    DUPLICATE_OR_CLOSED = "DUPLICATE_OR_CLOSED"
    STATUS_UPDATE = "STATUS_UPDATE"
    #: An execution run's own result / verification (Phase 6/7/9 evidence).
    EXECUTION_RESULT = "EXECUTION_RESULT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class ClassificationSource(str, Enum):
    RULES = "rules"
    AI = "ai"
    EXECUTION = "execution"
    HUMAN = "human"
    #: A MANUAL signal carrying the candidate's own category.
    DECLARED = "declared"


class SignalStatus(str, Enum):
    NEW = "NEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNMATCHED = "UNMATCHED"
    ATTRIBUTED = "ATTRIBUTED"
    APPLIED = "APPLIED"
    IGNORED = "IGNORED"
    MERGED = "MERGED"


class AttributionStatus(str, Enum):
    MATCHED = "MATCHED"
    #: More than one application could be meant: a person decides.
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"
    #: Linked by a person.
    MANUAL = "MANUAL"
    #: Not attempted (irrelevant signal).
    SKIPPED = "SKIPPED"


class OutcomeKind(str, Enum):
    SUBMITTED = "SUBMITTED"
    APPLICATION_RECEIVED = "APPLICATION_RECEIVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ASSESSMENT_REQUESTED = "ASSESSMENT_REQUESTED"
    RECRUITER_CONTACT = "RECRUITER_CONTACT"
    INTERVIEW_REQUESTED = "INTERVIEW_REQUESTED"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED_WITHOUT_APPLICATION = "CLOSED_WITHOUT_APPLICATION"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNKNOWN = "UNKNOWN"


class EvidenceStrength(str, Enum):
    """How much an outcome event may move the derived status.

    STRONG   deterministic HIGH classification, VERIFIED execution evidence,
             or a person's confirmation: sets any outcome.
    MODERATE deterministic MEDIUM classification or LIKELY execution evidence:
             may set a *progress* outcome, never a terminal one.
    WEAK     an AI classification, LOW confidence, or UNKNOWN execution
             evidence: recorded, shown as provisional, never sets the status.
    """

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class EventOrigin(str, Enum):
    RULES = "rules"
    EXECUTION = "execution"
    AI = "ai"
    HUMAN = "human"


class TimeBasis(str, Enum):
    """Whether ``event_at`` is the employer's own timestamp or when we saw it."""

    EXTERNAL = "external"
    OBSERVED = "observed"


# --------------------------------------------------------------------- #
# ingestion inputs (the connector boundary)
# --------------------------------------------------------------------- #


class AttributionHints(BaseModel):
    """Identifiers a connector already knows. Strong ones are used first."""

    application_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    job_id: Optional[str] = None
    execution_run_id: Optional[str] = None
    url: Optional[str] = Field(default=None, max_length=2048)
    company: Optional[str] = Field(default=None, max_length=256)
    title: Optional[str] = Field(default=None, max_length=512)
    reference: Optional[str] = Field(default=None, max_length=256)
    #: The message id this one replies to (email threads).
    in_reply_to: Optional[str] = Field(default=None, max_length=512)


class SignalIngest(BaseModel):
    """One observation handed to ``SignalIngestionService.ingest``.

    ``text`` is normalized again server-side and only an excerpt is stored.
    ``payload`` and ``provenance`` are sanitized (credential-like keys are
    dropped). Nothing here may carry cookies, tokens or passwords.
    """

    source: SignalSource
    source_reference: Optional[str] = Field(default=None, max_length=256)
    subject: Optional[str] = Field(default=None, max_length=1024)
    text: Optional[str] = Field(default=None, max_length=200_000)
    sender: Optional[str] = Field(default=None, max_length=256)
    recipients: list[str] = Field(default_factory=list)
    external_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    hints: AttributionHints = Field(default_factory=AttributionHints)
    #: MANUAL signals may declare their category (the candidate's own statement).
    category: Optional[SignalCategory] = None
    #: Run classification → attribution → outcomes immediately.
    process: bool = True


class EmailMessage(BaseModel):
    """A supplied email. There is no mailbox integration: a connector (or a
    person pasting) hands over exactly the messages it wants ingested."""

    message_id: Optional[str] = Field(default=None, max_length=512)
    sender: str = Field(min_length=1, max_length=512)
    recipients: list[str] = Field(default_factory=list)
    subject: str = Field(default="", max_length=1024)
    received_at: Optional[datetime] = None
    text: Optional[str] = Field(default=None, max_length=200_000)
    html: Optional[str] = Field(default=None, max_length=400_000)
    #: Only the few headers attribution needs are read (In-Reply-To, References, Date, List-Unsubscribe).
    headers: dict[str, str] = Field(default_factory=dict)
    hints: AttributionHints = Field(default_factory=AttributionHints)
    process: bool = True


# --------------------------------------------------------------------- #
# pure results
# --------------------------------------------------------------------- #


class Classification(BaseModel):
    category: SignalCategory
    confidence: Confidence
    source: ClassificationSource
    classifier_version: str = CLASSIFIER_VERSION
    matched_rules: list[str] = Field(default_factory=list)
    #: Other categories whose rules also matched (kept so a reviewer sees why confidence dropped).
    competing: list[str] = Field(default_factory=list)
    ai: Optional[dict[str, Any]] = None

    @property
    def confident(self) -> bool:
        return self.confidence is Confidence.HIGH


class AttributionResult(BaseModel):
    status: AttributionStatus
    application_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    rule: str = "none"
    confidence: Confidence = Confidence.NONE
    evidence: dict[str, Any] = Field(default_factory=dict)
    candidates: list[str] = Field(default_factory=list)
    attribution_version: str = ATTRIBUTION_VERSION
    explanation: str = ""


class DerivedOutcome(BaseModel):
    """The current status computed from an application's outcome events."""

    application_id: str
    current: OutcomeKind = OutcomeKind.UNKNOWN
    provisional: Optional[OutcomeKind] = None
    needs_review: bool = False
    conflicts: list[str] = Field(default_factory=list)
    basis_event_id: Optional[str] = None
    event_count: int = 0
    last_event_at: Optional[datetime] = None
    rules_version: str = OUTCOME_RULES_VERSION


# --------------------------------------------------------------------- #
# read models
# --------------------------------------------------------------------- #


class Signal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    source: SignalSource
    source_reference: Optional[str] = None
    content_hash: str
    subject: Optional[str] = None
    sender: Optional[str] = None
    sender_domain: Optional[str] = None
    excerpt: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    external_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    observation_count: int = 1
    last_observed_at: Optional[datetime] = None
    category: SignalCategory = SignalCategory.UNKNOWN
    confidence: Confidence = Confidence.NONE
    classification_source: Optional[str] = None
    classifier_version: Optional[str] = None
    classification: dict[str, Any] = Field(default_factory=dict)
    status: SignalStatus = SignalStatus.NEW
    status_reason: Optional[str] = None
    attribution_status: AttributionStatus = AttributionStatus.UNMATCHED
    attribution_id: Optional[str] = None
    application_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    execution_run_id: Optional[str] = None
    merged_into_id: Optional[str] = None
    ai: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = SIGNAL_SCHEMA_VERSION
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SignalObservation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    signal_id: str
    source: SignalSource
    source_reference: Optional[str] = None
    content_hash: str
    observed_at: Optional[datetime] = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class SignalAttribution(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    signal_id: str
    status: AttributionStatus
    application_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    rule: str
    confidence: Confidence
    evidence: dict[str, Any] = Field(default_factory=dict)
    candidates: list[str] = Field(default_factory=list)
    explanation: Optional[str] = None
    attribution_version: str
    actor: str
    superseded_by_id: Optional[str] = None
    created_at: Optional[datetime] = None


class OutcomeEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    application_id: str
    signal_id: Optional[str] = None
    attribution_id: Optional[str] = None
    outcome: OutcomeKind
    evidence: EvidenceStrength
    origin: EventOrigin
    category: Optional[SignalCategory] = None
    event_at: Optional[datetime] = None
    time_basis: TimeBasis = TimeBasis.OBSERVED
    observed_at: Optional[datetime] = None
    sequence: int = 0
    actor: str
    note: Optional[str] = None
    classifier_version: Optional[str] = None
    attribution_version: Optional[str] = None
    rules_version: str
    superseded_by_id: Optional[str] = None
    retracted: bool = False
    retracted_reason: Optional[str] = None
    created_at: Optional[datetime] = None


class ApplicationOutcome(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    application_id: str
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    current_outcome: OutcomeKind
    provisional_outcome: Optional[OutcomeKind] = None
    needs_review: bool = False
    conflicts: list[str] = Field(default_factory=list)
    event_count: int = 0
    basis_event_id: Optional[str] = None
    last_event_at: Optional[datetime] = None
    derived_at: Optional[datetime] = None
    rules_version: str
    version: int = 1


class SignalTrace(BaseModel):
    """Signal → attribution → opportunity → candidate opportunity →
    preparation → execution attempt → application history, in one payload."""

    signal: Signal
    observations: list[SignalObservation] = Field(default_factory=list)
    attributions: list[SignalAttribution] = Field(default_factory=list)
    outcome_events: list[OutcomeEvent] = Field(default_factory=list)
    application_outcome: Optional[ApplicationOutcome] = None
    attempt: Optional[dict[str, Any]] = None
    opportunity: Optional[dict[str, Any]] = None
    candidate_opportunity: Optional[dict[str, Any]] = None
    preparation: Optional[dict[str, Any]] = None
    execution_runs: list[dict[str, Any]] = Field(default_factory=list)
    application_events: list[dict[str, Any]] = Field(default_factory=list)


class SignalSummary(BaseModel):
    tenant_id: str
    by_status: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    outcomes_by_current: dict[str, int] = Field(default_factory=dict)
    review_queue: int = 0
    unmatched: int = 0
