"""Fixtures for Phase 4 preparation tests.

Each test gets a private tenant seeded from ``data/career_seed.json`` (the
real Evidence Graph, so evidence keys like ``skill-python`` are real), an
approved answer bank for the candidate-only facts, and a factory that
creates a job + match + assessments + admitted candidate opportunity.
"""

import uuid

import pytest

from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.database import get_session_factory
from app.intelligence.database.models import RequirementAssessmentRow
from app.pipeline.database.models import CandidateOpportunityRow
from app.pipeline.models import FitBand, OpportunityState
from app.pipeline.policy import band_for
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.preparation.service import PreparationService
from tests.pipeline.conftest import JobFactory, MatchFactory

FACT_ANSWERS = [
    ("work_authorization", "Are you legally authorized to work in this country?", "Yes, I am an Indian citizen authorized to work in India."),
    ("sponsorship", "Will you now or in the future require sponsorship?", "No, I do not require sponsorship."),
    ("notice_period", "What is your notice period?", "I can start within two weeks."),
    ("salary", "What are your salary expectations?", "I am open to discussing a competitive package for this role."),
]


@pytest.fixture
def db_session():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _new_tenant(session, prefix: str) -> str:
    tid = f"{prefix}-{uuid.uuid4().hex[:10]}"
    session.add(TenantRow(id=tid, name=prefix))
    session.commit()
    return tid


def _drop_tenant(session, tid: str) -> None:
    session.rollback()
    row = session.get(TenantRow, tid)
    if row is not None:
        session.delete(row)
        session.commit()


@pytest.fixture
def tenant_id(db_session):
    tid = _new_tenant(db_session, "prep")
    yield tid
    _drop_tenant(db_session, tid)


@pytest.fixture
def other_tenant_id(db_session):
    tid = _new_tenant(db_session, "prep-other")
    yield tid
    _drop_tenant(db_session, tid)


@pytest.fixture
def evidence(db_session, tenant_id) -> EvidenceRepository:
    repo = EvidenceRepository(db_session, tenant_id)
    SeedImporter(repo).import_file(__import__("app.config", fromlist=["settings"]).settings.career_data_path)
    return repo


@pytest.fixture
def answered_bank(evidence) -> EvidenceRepository:
    for category, question, answer in FACT_ANSWERS:
        evidence.create_answer(
            AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED),
            actor="test",
        )
    evidence.commit()
    return evidence


@pytest.fixture
def jobs(db_session):
    factory = JobFactory(db_session)
    yield factory
    factory.cleanup()


@pytest.fixture
def matches(db_session):
    factory = MatchFactory(db_session)
    yield factory
    factory.cleanup()


class OpportunityFactory:
    """Job + match + assessments + admitted candidate opportunity, in one call."""

    def __init__(self, session, tenant_id, jobs: JobFactory, matches: MatchFactory):
        self.session = session
        self.tenant_id = tenant_id
        self.jobs = jobs
        self.matches = matches
        self.repo = OpportunityRepository(session, tenant_id)
        PolicyRepository(session, tenant_id).get()
        session.commit()

    def make(
        self,
        title: str = "Backend Engineer",
        fit_score: int = 80,
        requirements: list[tuple[str, list[str], float]] | None = None,
        eligibility: str = "ELIGIBLE",
        admitted: bool = True,
        state: OpportunityState | None = None,
        **job_kwargs,
    ) -> CandidateOpportunityRow:
        job = self.jobs.make(title=title, **job_kwargs)
        match = self.matches.make(job, fit_score=fit_score, eligibility_status=eligibility)
        for name, refs, contribution in requirements or [("Python", ["skill-python"], 30.0), ("Go", ["skill-go"], 20.0)]:
            self.session.add(
                RequirementAssessmentRow(
                    job_match_id=match.id,
                    requirement_name=name,
                    requirement_category="TECHNICAL_SKILL",
                    requirement_strictness="REQUIRED",
                    status="MATCHED" if refs else "MISSING",
                    evidence_strength="DIRECT_VERIFIED" if refs else "NONE",
                    evidence_references=list(refs),
                    confidence="HIGH",
                    weight=1.0,
                    contribution=contribution,
                )
            )
        opp, _, _ = OpportunityRepository.resolve_opportunity(self.session, job)
        co, _ = self.repo.ensure_candidate_opportunity(opp, "test")
        co.match_id = match.id
        co.fit_score = fit_score
        co.fit_band = band_for(fit_score).value if fit_score is not None else None
        co.eligibility_status = eligibility
        co.policy_admitted = admitted
        co.policy_reason = "admitted:test" if admitted else "band_disabled:LOW"
        co.state = (state or (OpportunityState.ELIGIBLE if eligibility != "INELIGIBLE" else OpportunityState.INELIGIBLE)).value
        self.session.commit()
        self.session.refresh(co)
        return co


@pytest.fixture
def opportunities(db_session, tenant_id, jobs, matches) -> OpportunityFactory:
    return OpportunityFactory(db_session, tenant_id, jobs, matches)


@pytest.fixture
def service(db_session, tenant_id, answered_bank) -> PreparationService:
    return PreparationService(db_session, tenant_id, actor="test")


__all__ = ["FitBand"]
