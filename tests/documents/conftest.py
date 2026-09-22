"""Fixtures for Phase 8 document tests: real READY preparations from the seeded
Evidence Graph, a per-test documents root, and a DocumentService."""

import pytest

from app.config import settings
from app.documents.service import DocumentService
from app.documents.storage import ArtifactStore
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.preparation.service import PreparationService
from tests.execution import conftest as _exec

OpportunityFactory = _exec.OpportunityFactory
db_session = _exec.db_session
tenant_id = _exec.tenant_id
other_tenant_id = _exec.other_tenant_id
evidence = _exec.evidence
answered_bank = _exec.answered_bank
jobs = _exec.jobs
matches = _exec.matches
opportunities = _exec.opportunities
scheduler = _exec.scheduler
harness = _exec.harness


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    monkeypatch.setattr(settings, "documents_root", str(root))
    return root


@pytest.fixture
def documents(db_session, tenant_id, docs_root, answered_bank) -> DocumentService:
    from app.career.repository import EvidenceRepository

    EvidenceRepository(db_session, tenant_id).upsert_profile({"email": "ribhu@example.com", "phone": "+91 90000 00000", "github": "https://github.com/ribhu", "linkedin": "https://linkedin.com/in/ribhu"}, None, "test")
    db_session.commit()
    return DocumentService(db_session, tenant_id, actor="test", store=ArtifactStore(str(docs_root)))


def ready_preparation(db_session, tenant_id, opportunities, title="Backend Engineer", company="Alpha", fit_score=90, cover_letter="LIGHT"):
    """A READY preparation (with a cover letter unless DISABLED) for a fresh opportunity."""
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(cover_letter_by_band={"HIGH": cover_letter, "MEDIUM": cover_letter, "LOW": cover_letter}), "test")
    db_session.commit()
    co = opportunities.make(title=title, fit_score=fit_score, company=company)
    row = PreparationService(db_session, tenant_id, actor="test").prepare(co.id)
    assert row.status == "READY", row.status
    return row
