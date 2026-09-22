"""Domain models for application preparation."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.models import Lane, TailoringLevel

__all__ = ["Lane", "TailoringLevel"]

#: Bumped when the deterministic composition changes, so stored preparations
#: can be told apart from ones a newer composer would produce.
TEMPLATE_VERSION = "prep-v1"


class PreparationStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class ArtifactKind(str, Enum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


class CoverLetterMode(str, Enum):
    DISABLED = "DISABLED"
    TEMPLATE = "TEMPLATE"
    LIGHT = "LIGHT"
    TARGETED = "TARGETED"


class BlockKind(str, Enum):
    #: Candidate-authored framing (positioning headline/summary, profile
    #: statement, greeting): no factual claim of its own.
    FRAMING = "FRAMING"
    #: A factual claim about the candidate; must cite application-safe evidence.
    CLAIM = "CLAIM"


class AnswerSource(str, Enum):
    ANSWER_BANK = "ANSWER_BANK"
    PROFILE = "PROFILE"
    GENERATED = "GENERATED"
    USER = "USER"
    NONE = "NONE"


class PreparedAnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class QueueOutcome(str, Enum):
    READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class Block(BaseModel):
    """One unit of generated content with its provenance."""

    kind: BlockKind
    section: str
    text: str
    evidence_keys: list[str] = Field(default_factory=list)
    source: str = "template"
    ai_polished: bool = False
    requirement: Optional[str] = None


class ValidationIssue(BaseModel):
    code: str
    message: str
    block_index: Optional[int] = None
    evidence_keys: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    passed: bool
    checked_blocks: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    dropped_blocks: int = 0


# ---------------------------------------------------------------------- #
# read models
# ---------------------------------------------------------------------- #


class PreparationArtifact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    preparation_id: str
    artifact_type: ArtifactKind
    content: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)
    template_version: str
    ai_used: bool = False
    validation_status: ValidationStatus
    created_at: Optional[datetime] = None


class PreparationAnswer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    preparation_id: str
    question: str
    question_key: str
    category: str
    answer: Optional[str] = None
    evidence_keys: list[str] = Field(default_factory=list)
    source: AnswerSource
    status: PreparedAnswerStatus
    required: bool = True
    answer_bank_entry_id: Optional[str] = None
    reason: Optional[str] = None
    ai_used: bool = False
    updated_at: Optional[datetime] = None


class Preparation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_opportunity_id: str
    opportunity_id: str
    job_id: str
    job_content_hash: Optional[str] = None
    version: int
    tailoring_level: TailoringLevel
    lane: Lane
    cover_letter_mode: CoverLetterMode
    positioning_variant_id: Optional[str] = None
    positioning_variant_version: Optional[int] = None
    positioning_reason: Optional[str] = None
    status: PreparationStatus
    validation_status: ValidationStatus
    validation_report: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    evidence_keys: list[str] = Field(default_factory=list)
    ai_used: bool = False
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    ai_calls: int = 0
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PreparationDetail(Preparation):
    artifacts: list[PreparationArtifact] = Field(default_factory=list)
    answers: list[PreparationAnswer] = Field(default_factory=list)


class PrepareRequest(BaseModel):
    candidate_opportunity_id: str
    tailoring_level: Optional[TailoringLevel] = None
    questions: Optional[list[str]] = Field(default=None, description="Form questions; defaults to the standard set")
    force: bool = Field(default=False, description="Rebuild even if inputs are unchanged")


class PrepareManyReport(BaseModel):
    tenant_id: str
    requested: int = 0
    created: int = 0
    reused: int = 0
    skipped: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    ai_calls: int = 0
    errors: list[str] = Field(default_factory=list)
