"""Build the canonical execution package from a READY attempt.

Reads the preparation (artifacts, answers, provenance), the opportunity and
its canonical job, the approved answer bank and the profile facts. Nothing is
regenerated; a package is a *view* of prepared material.
"""

from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.application.database.models import ApplicationRow
from app.career.models import AnswerStatus, EvidenceKind
from app.career.repository import EvidenceRepository
from app.core.errors import NotFoundError, ValidationFailed
from app.execution.models import (
    ArtifactView,
    BankAnswerView,
    ExecutionPackage,
    PreparedAnswerView,
)
from app.execution.target import target_for
from app.jobs.database.models import JobRow
from app.pipeline.database.models import OpportunityRow
from app.pipeline.models import Lane, TailoringLevel
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import ArtifactKind, CoverLetterMode


def load_preparation(db: Session, tenant_id: str, preparation_id: str) -> Optional[ApplicationPreparationRow]:
    return (
        db.query(ApplicationPreparationRow)
        .options(joinedload(ApplicationPreparationRow.artifacts), joinedload(ApplicationPreparationRow.answers))
        .filter(ApplicationPreparationRow.tenant_id == tenant_id, ApplicationPreparationRow.id == preparation_id)
        .first()
    )


def build_package(
    db: Session,
    tenant_id: str,
    attempt: ApplicationRow,
    preparation: Optional[ApplicationPreparationRow] = None,
    bank: Optional[list[BankAnswerView]] = None,
    profile: Optional[dict] = None,
    execution_config: Optional[dict] = None,
) -> ExecutionPackage:
    if attempt.tenant_id != tenant_id:
        raise NotFoundError("Application attempt not found in this tenant")
    if not attempt.preparation_id:
        raise ValidationFailed("Attempt has no preparation attached")
    if preparation is None:
        preparation = load_preparation(db, tenant_id, attempt.preparation_id)
    if preparation is None:
        raise NotFoundError(f"Preparation not found: {attempt.preparation_id}")
    opportunity = db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
    if opportunity is None:
        raise ValidationFailed("Attempt has no opportunity")
    job = db.get(JobRow, preparation.job_id or opportunity.canonical_job_id or attempt.job_id)
    artifacts = {a.artifact_type: a for a in preparation.artifacts}
    resume = artifacts.get(ArtifactKind.RESUME.value)
    cover = artifacts.get(ArtifactKind.COVER_LETTER.value)
    cover_enabled = preparation.cover_letter_mode != CoverLetterMode.DISABLED.value and cover is not None
    if bank is None:
        bank = load_answer_bank(db, tenant_id)
    if profile is None:
        profile = load_profile_facts(db, tenant_id)
    return ExecutionPackage(
        tenant_id=tenant_id,
        candidate_opportunity_id=attempt.candidate_opportunity_id or preparation.candidate_opportunity_id,
        opportunity_id=opportunity.id,
        preparation_id=preparation.id,
        application_id=attempt.id,
        attempt_number=attempt.attempt_number or 1,
        preparation_version=preparation.version,
        preparation_fingerprint=preparation.input_fingerprint,
        preparation_inputs=dict(preparation.inputs or {}),
        target=target_for(opportunity, job),
        lane=Lane(attempt.lane or preparation.lane or Lane.REVIEW.value),
        tailoring_level=TailoringLevel(attempt.tailoring_level or preparation.tailoring_level),
        cover_letter_enabled=cover_enabled,
        resume=ArtifactView.model_validate(resume) if resume else None,
        cover_letter=ArtifactView.model_validate(cover) if cover_enabled else None,
        answers=[PreparedAnswerView.model_validate(a) for a in preparation.answers],
        answer_bank=list(bank),
        evidence_keys=list(preparation.evidence_keys or []),
        profile=dict(profile or {}),
        execution_config=dict(execution_config or {}),
    )


def load_answer_bank(db: Session, tenant_id: str) -> list[BankAnswerView]:
    repo = EvidenceRepository(db, tenant_id)
    return [
        BankAnswerView(id=e.id, category=e.category, question_key=e.question_key, answer=e.answer, evidence_keys=list(e.evidence_keys or []))
        for e in repo.list_answers(status=AnswerStatus.APPROVED)
    ]


def load_profile_facts(db: Session, tenant_id: str) -> dict:
    repo = EvidenceRepository(db, tenant_id)
    row = repo.get_profile_row()
    if row is None:
        return {}
    keys = ("name", "email", "phone", "location", "work_authorization", "linkedin", "github", "portfolio", "degree", "branch", "college", "graduation_year", "cgpa")
    facts = {k: getattr(row, k) for k in keys if getattr(row, k, None)}
    # Split the recorded name for forms that ask for first / last separately:
    # a split of a stored fact, not an inference.
    if facts.get("name"):
        parts = str(facts["name"]).strip().split(" ", 1)
        facts["first_name"] = parts[0]
        if len(parts) > 1:
            facts["last_name"] = parts[1]
    # Likewise the city and the country are the first and last parts of the
    # recorded location ("Hyderabad, Telangana, India"); real Greenhouse forms
    # ask for both in searchable dropdowns (2026-09-22).
    if facts.get("location"):
        parts = [p.strip() for p in str(facts["location"]).split(",") if p.strip()]
        if len(parts) >= 2:
            facts["city"] = parts[0]
            facts["country"] = parts[-1]
    # Employers on record, so "have you worked at <this company> before?" is never
    # answered from a stored "no" for a company the candidate did work at.
    employers = []
    for node in repo.list_nodes(kind=EvidenceKind.EXPERIENCE):
        attributes = node.attributes or {}
        organization = attributes.get("organization")
        if organization and attributes.get("is_employment", True):
            employers.append(str(organization))
    if employers:
        facts["employers"] = employers
    return facts
