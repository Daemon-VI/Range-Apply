"""Fixtures for Phase 10 Signal Inbox tests.

Two ways to get attempts: ``harness.ready()`` + ``harness.execute()`` runs
the real Phase 5/6 flow (scheduler, zero-AI preparation, mock executor);
``submitted()`` fakes an already-submitted attempt cheaply through the
scheduler fixture helper. Both are tenant-private per test.
"""

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.main import app
from app.signals.models import AttributionHints, EmailMessage, SignalIngest, SignalSource
from app.signals.service import SignalInboxService
from tests.execution import conftest as _exec
from tests.scheduler.conftest import fake_attempt

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
other_scheduler = _exec.other_scheduler
harness = _exec.harness


@pytest.fixture(autouse=True)
def _no_ai():
    from app.ai.gateway import reset_gateway

    reset_gateway(None)
    yield
    reset_gateway(None)


@pytest.fixture
def inbox(db_session, tenant_id) -> SignalInboxService:
    return SignalInboxService(db_session, tenant_id, actor="test")


@pytest.fixture
def other_inbox(db_session, other_tenant_id) -> SignalInboxService:
    return SignalInboxService(db_session, other_tenant_id, actor="test")


def submitted(session, tenant_id, opportunities, company="Acme", title="Backend Engineer", status=ApplicationStatus.SUBMITTED, reference: Optional[str] = None, application_url: Optional[str] = None, fit_score=85):
    """A cheap already-submitted attempt for ``company`` / ``title``."""
    co = opportunities.make(title=title, company=company, fit_score=fit_score)
    if application_url:
        from app.jobs.database.models import JobRow

        job = session.get(JobRow, co.opportunity.canonical_job_id)
        job.application_url = application_url
        session.commit()
    row = fake_attempt(session, tenant_id, co, status=status)
    if reference:
        row.external_application_id = reference
        row.confirmation = reference
        session.commit()
    # The job factory suffixes company names per test; tests mention the stored name.
    row.company_name = co.opportunity.company
    row.title_name = co.opportunity.title
    return row


def email(subject: str, body: str, sender="talent@acme.com", message_id: Optional[str] = None, hints: Optional[AttributionHints] = None, received_at=None, html: Optional[str] = None, headers: Optional[dict] = None) -> EmailMessage:
    return EmailMessage(message_id=message_id, sender=sender, recipients=["ribhu@example.com"], subject=subject, received_at=received_at, text=body if html is None else None, html=html, headers=headers or {}, hints=hints or AttributionHints())


def manual(text: str, category=None, hints: Optional[AttributionHints] = None, reference: Optional[str] = None) -> SignalIngest:
    return SignalIngest(source=SignalSource.MANUAL, source_reference=reference, subject="manual note", text=text, hints=hints or AttributionHints(), category=category)


@pytest.fixture
def client(tenant_id, db_session):
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    db_session.commit()
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if previous is not None:
            app.dependency_overrides[get_tenant_id] = previous
        else:
            app.dependency_overrides.pop(get_tenant_id, None)


__all__ = ["email", "manual", "submitted", "fake_attempt"]
