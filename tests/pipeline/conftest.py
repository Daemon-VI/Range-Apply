"""Fixtures for the Phase 2 pipeline tests.

Each test gets its own tenant (cascade-deleted on teardown) in the shared
migrated test database, plus factories for job rows and match rows. Jobs
use unique company names so their shared opportunities never collide with
another test's; both are deleted on teardown.
"""

import uuid
from datetime import timedelta
from typing import Optional

import pytest

from app.career.database.models import TenantRow
from app.core.timeutils import db_now, to_db, utc_now
from app.database import get_session_factory
from app.intelligence.database.models import JobMatchRow, MatchRunRow
from app.intelligence.services.match_persistence import ensure_policy
from app.jobs.database.models import JobRow
from app.jobs.normalization.normalizer import compute_canonical_key
from app.pipeline.database.models import OpportunityRow
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository, PolicyRepository


@pytest.fixture
def db_session():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _drop_tenant(session, tenant_id: str) -> None:
    row = session.get(TenantRow, tenant_id)
    if row is not None:
        session.delete(row)
        session.commit()


@pytest.fixture
def tenant_id(db_session):
    tid = f"p2-{uuid.uuid4().hex[:12]}"
    db_session.add(TenantRow(id=tid, name="pipeline test"))
    db_session.commit()
    yield tid
    db_session.rollback()
    _drop_tenant(db_session, tid)


@pytest.fixture
def other_tenant_id(db_session):
    tid = f"p2o-{uuid.uuid4().hex[:12]}"
    db_session.add(TenantRow(id=tid, name="other"))
    db_session.commit()
    yield tid
    db_session.rollback()
    _drop_tenant(db_session, tid)


@pytest.fixture
def repo(db_session, tenant_id) -> OpportunityRepository:
    return OpportunityRepository(db_session, tenant_id)


@pytest.fixture
def other_repo(db_session, other_tenant_id) -> OpportunityRepository:
    return OpportunityRepository(db_session, other_tenant_id)


@pytest.fixture
def policy_repo(db_session, tenant_id) -> PolicyRepository:
    return PolicyRepository(db_session, tenant_id)


@pytest.fixture
def queue(db_session, tenant_id) -> QueueRepository:
    return QueueRepository(db_session, tenant_id)


class JobFactory:
    def __init__(self, session):
        self.session = session
        self.company_suffix = uuid.uuid4().hex[:8]
        self.job_ids: list[str] = []
        self.companies: set[str] = set()

    def company(self, name: str) -> str:
        return f"{name} {self.company_suffix}"

    def make(
        self,
        title: str = "Backend Engineer",
        company: str = "Acme",
        location: Optional[str] = "Remote",
        remote_type: str = "REMOTE",
        source: str = "GREENHOUSE",
        source_job_id: Optional[str] = None,
        posted_days_ago: Optional[float] = 1.0,
        deadline_in_days: Optional[float] = None,
        job_status: str = "ACTIVE",
        description: str = "Python, Go, PostgreSQL backend role.",
    ) -> JobRow:
        company_name = self.company(company)
        self.companies.add(company_name)
        source_job_id = source_job_id or f"job-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        row = JobRow(
            canonical_key=compute_canonical_key(company_name, title, location) + uuid.uuid4().hex[:6],
            source=source,
            source_job_id=source_job_id,
            company=company_name,
            title=title,
            original_title=title,
            description=description,
            original_description=description,
            location=location,
            remote_type=remote_type,
            source_url=f"https://example.com/{source.lower()}/{source_job_id}",
            application_url=f"https://example.com/{source.lower()}/{source_job_id}/apply",
            posted_at=to_db(now - timedelta(days=posted_days_ago)) if posted_days_ago is not None else None,
            deadline=to_db(now + timedelta(days=deadline_in_days)) if deadline_in_days is not None else None,
            content_hash=uuid.uuid4().hex,
            processing_status="NORMALIZED",
            job_status=job_status,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.job_ids.append(row.id)
        return row

    def cleanup(self) -> None:
        self.session.rollback()
        for company in self.companies:
            for opp in self.session.query(OpportunityRow).filter(OpportunityRow.company == company).all():
                self.session.delete(opp)
        for job_id in self.job_ids:
            job = self.session.get(JobRow, job_id)
            if job is not None:
                self.session.delete(job)
        self.session.commit()


@pytest.fixture
def jobs(db_session):
    factory = JobFactory(db_session)
    yield factory
    factory.cleanup()


class MatchFactory:
    def __init__(self, session):
        self.session = session
        self.run = None

    def ensure_run(self) -> MatchRunRow:
        if self.run is None:
            ensure_policy(self.session)
            self.run = MatchRunRow(
                policy_version="v1",
                engine_version="1.1.0",
                career_brain_version="v1",
                started_at=db_now(),
                completed_at=db_now(),
                status="COMPLETED",
                trigger="test",
                errors=[],
            )
            self.session.add(self.run)
            self.session.commit()
        return self.run

    def make(
        self,
        job: JobRow,
        fit_score: int = 80,
        eligibility_status: str = "ELIGIBLE",
        eligibility_reasons=None,
        blocking_reasons=None,
        uncertainties=None,
    ) -> JobMatchRow:
        run = self.ensure_run()
        row = JobMatchRow(
            job_id=job.id,
            run_id=run.id,
            job_canonical_key=job.canonical_key,
            job_content_hash=job.content_hash,
            policy_version="v1",
            engine_version="1.1.0",
            eligibility_status=eligibility_status,
            eligibility_confidence="HIGH",
            eligibility_reasons=list(eligibility_reasons or ["Graduation year: graduating 2027 fits the 2027 window"]),
            blocking_reasons=list(blocking_reasons or []),
            fit_score=fit_score,
            component_scores={"technical": 0.8, "experience": 0.5},
            priority="P1",
            match_type="CORE_MATCH",
            confidence="HIGH",
            strengths=["Python"],
            gaps=[],
            uncertainties=list(uncertainties or []),
            explanation="test match",
            evaluated_at=db_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def cleanup(self) -> None:
        self.session.rollback()
        if self.run is not None:
            run = self.session.get(MatchRunRow, self.run.id)
            if run is not None:
                self.session.delete(run)
                self.session.commit()


@pytest.fixture
def matches(db_session):
    factory = MatchFactory(db_session)
    yield factory
    factory.cleanup()
