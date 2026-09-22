"""Domain models for the Evidence Graph (blueprint §1, §7, §14).

An *evidence node* is one atomic, attributable claim about the candidate:
a skill, a project, a metric, a responsibility, a degree, a link. The
existing Phase 1 domain models (``Skill``, ``Project``, ``Experience``,
``Achievement``, ``CareerFact``) are *views* over these nodes, rebuilt by
``app/career/read_model.py`` so every downstream consumer keeps its contract.

Grading reuses the project's existing ``VerificationStatus`` as the stored,
candidate-facing state; :class:`EvidenceGrade` is the machine-readable
strength derived from it (never a competing second field to keep in sync).
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import VerificationStatus


class EvidenceKind(str, Enum):
    SKILL = "SKILL"
    PROJECT = "PROJECT"
    EXPERIENCE = "EXPERIENCE"
    ACHIEVEMENT = "ACHIEVEMENT"
    EDUCATION = "EDUCATION"
    CREDENTIAL = "CREDENTIAL"
    METRIC = "METRIC"
    RESPONSIBILITY = "RESPONSIBILITY"
    LINK = "LINK"
    FACT = "FACT"
    OTHER = "OTHER"


class EvidenceGrade(str, Enum):
    """Machine-readable strength, derived from status + lifecycle.

    Only ``CONFIRMED`` evidence may back an application claim (FR-05).
    """

    CONFIRMED = "CONFIRMED"
    APPROXIMATE = "APPROXIMATE"
    UNVERIFIED = "UNVERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REMOVED = "REMOVED"

    @property
    def usable_in_application(self) -> bool:
        return self is EvidenceGrade.CONFIRMED


class EvidenceSourceType(str, Enum):
    """Where a claim came from. Extraction is not verification."""

    CANDIDATE_ENTERED = "CANDIDATE_ENTERED"
    SEED_FILE = "SEED_FILE"
    RESUME = "RESUME"
    LINKEDIN_EXPORT = "LINKEDIN_EXPORT"
    PORTFOLIO = "PORTFOLIO"
    GITHUB = "GITHUB"
    IMPORTED_DOCUMENT = "IMPORTED_DOCUMENT"
    MANUAL_CONFIRMATION = "MANUAL_CONFIRMATION"
    INFERRED = "INFERRED"
    LLM_EXTRACTED = "LLM_EXTRACTED"
    OTHER = "OTHER"


#: Sources whose claims may never be created directly as VERIFIED; a human
#: must confirm them afterwards (recorded via ``verified_by``).
UNTRUSTED_SOURCE_TYPES = frozenset({EvidenceSourceType.INFERRED, EvidenceSourceType.LLM_EXTRACTED})


class EvidenceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class RelationType(str, Enum):
    DEMONSTRATES = "DEMONSTRATES"  # PROJECT/EXPERIENCE -> SKILL
    HAS_METRIC = "HAS_METRIC"  # PROJECT -> METRIC
    HAS_RESPONSIBILITY = "HAS_RESPONSIBILITY"  # EXPERIENCE -> RESPONSIBILITY
    ABOUT = "ABOUT"  # FACT/ACHIEVEMENT -> any entity
    PART_OF = "PART_OF"  # CREDENTIAL -> EDUCATION
    SUPPORTS = "SUPPORTS"  # generic


def grade_for(status: VerificationStatus, removed: bool = False) -> EvidenceGrade:
    """The single mapping from stored state to machine-readable strength."""
    if removed:
        return EvidenceGrade.REMOVED
    if status is VerificationStatus.VERIFIED:
        return EvidenceGrade.CONFIRMED
    if status is VerificationStatus.INFERRED:
        return EvidenceGrade.APPROXIMATE
    if status in (VerificationStatus.NEEDS_REVIEW, VerificationStatus.CONFLICT):
        return EvidenceGrade.NEEDS_REVIEW
    return EvidenceGrade.UNVERIFIED


class EvidenceNode(BaseModel):
    """One atomic candidate claim with provenance and strength."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    key: str = Field(description="Stable, tenant-scoped identifier used in evidence references")
    kind: EvidenceKind
    label: str
    claim: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    allowed_for_resume: bool = False
    allowed_for_application: bool = False
    source_type: EvidenceSourceType = EvidenceSourceType.CANDIDATE_ENTERED
    source_ref: Optional[str] = None
    source_hash: Optional[str] = None
    artifact_ref: Optional[str] = None
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    status: EvidenceStatus = EvidenceStatus.ACTIVE
    sort_order: int = 0
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    removed_at: Optional[datetime] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grade(self) -> EvidenceGrade:
        return grade_for(self.verification_status, self.status is EvidenceStatus.REMOVED)

    @property
    def is_removed(self) -> bool:
        return self.status is EvidenceStatus.REMOVED


class EvidenceNodeCreate(BaseModel):
    key: Optional[str] = Field(default=None, description="Omit to derive from kind + label")
    kind: EvidenceKind
    label: str = Field(min_length=1, max_length=256)
    claim: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    allowed_for_resume: Optional[bool] = None
    allowed_for_application: Optional[bool] = None
    source_type: EvidenceSourceType = EvidenceSourceType.CANDIDATE_ENTERED
    source_ref: Optional[str] = None
    artifact_ref: Optional[str] = None
    sort_order: int = 0


class EvidenceNodeUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=256)
    claim: Optional[str] = Field(default=None, min_length=1)
    attributes: Optional[dict[str, Any]] = None
    verification_status: Optional[VerificationStatus] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    allowed_for_resume: Optional[bool] = None
    allowed_for_application: Optional[bool] = None
    source_type: Optional[EvidenceSourceType] = None
    source_ref: Optional[str] = None
    artifact_ref: Optional[str] = None
    sort_order: Optional[int] = None


class EvidenceRelationship(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    from_key: str
    to_key: str
    relation: RelationType
    position: int = 0
    created_at: Optional[datetime] = None


class PositioningVariantEvidence(BaseModel):
    key: str
    position: int = 0
    section: str = Field(default="body", max_length=32)


class PositioningVariant(BaseModel):
    """How existing evidence is selected, ordered and framed for a role family.

    A variant never creates facts: ``headline``/``summary`` are framing, and
    every ``evidence`` entry must resolve to an active node in the tenant.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    role_family: str
    name: str
    headline: str
    summary: str
    evidence: list[PositioningVariantEvidence] = Field(default_factory=list)
    is_active: bool = True
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PositioningVariantCreate(BaseModel):
    role_family: str = Field(min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, max_length=128)
    headline: str = Field(default="", max_length=256)
    summary: str = ""
    evidence: list[PositioningVariantEvidence] = Field(default_factory=list)
    is_active: bool = True


class PositioningVariantUpdate(BaseModel):
    role_family: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, max_length=128)
    headline: Optional[str] = Field(default=None, max_length=256)
    summary: Optional[str] = None
    evidence: Optional[list[PositioningVariantEvidence]] = None
    is_active: Optional[bool] = None


class AnswerStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class AnswerBankEntry(BaseModel):
    """A reusable, candidate-approved answer to a recurring application question."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    category: str
    question: str
    question_key: str
    answer: str
    evidence_keys: list[str] = Field(default_factory=list)
    status: AnswerStatus = AnswerStatus.DRAFT
    version: int = 1
    approved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_reusable(self) -> bool:
        return self.status is AnswerStatus.APPROVED


class AnswerBankEntryCreate(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=512)
    answer: str = Field(min_length=1)
    evidence_keys: list[str] = Field(default_factory=list)
    status: AnswerStatus = AnswerStatus.DRAFT


class AnswerBankEntryUpdate(BaseModel):
    category: Optional[str] = Field(default=None, min_length=1, max_length=64)
    question: Optional[str] = Field(default=None, min_length=1, max_length=512)
    answer: Optional[str] = Field(default=None, min_length=1)
    evidence_keys: Optional[list[str]] = None
    status: Optional[AnswerStatus] = None


class AuditEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    entity_type: str
    entity_id: str
    action: str
    actor: str
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None


class ImportReport(BaseModel):
    """What a seed import did. ``skipped`` means unchanged since the last import."""

    tenant_id: str
    source: str
    profile: str = "skipped"
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    relationships_created: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated) or self.profile != "skipped"

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "skipped": len(self.skipped),
            "relationships_created": self.relationships_created,
        }
