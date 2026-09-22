"""Fixtures for Phase 5 scheduler tests.

Re-uses the Phase 4 fixtures (per-test tenant, job/match factories, the
seeded Evidence Graph and answer bank) and adds a scheduler service plus
small helpers to set policy and to fake earlier applications.
"""

from datetime import timedelta
from typing import Optional

import pytest

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.core.timeutils import db_now
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.scheduler.service import SchedulerService
from tests.preparation import conftest as _prep

# Re-exported Phase 4 fixtures (assignment rather than import so ruff does not
# flag the fixture names when they are used as parameters below).
OpportunityFactory = _prep.OpportunityFactory
db_session = _prep.db_session
tenant_id = _prep.tenant_id
other_tenant_id = _prep.other_tenant_id
evidence = _prep.evidence
answered_bank = _prep.answered_bank
jobs = _prep.jobs
matches = _prep.matches
opportunities = _prep.opportunities
service = _prep.service


@pytest.fixture
def scheduler(db_session, tenant_id) -> SchedulerService:
    return SchedulerService(db_session, tenant_id, actor="test")


@pytest.fixture
def other_scheduler(db_session, other_tenant_id) -> SchedulerService:
    return SchedulerService(db_session, other_tenant_id, actor="test")


def set_policy(session, tenant_id: str, **changes) -> None:
    repo = PolicyRepository(session, tenant_id)
    repo.update(ApplicationPolicyUpdate(**changes), "test")
    session.commit()


def fake_attempt(
    session,
    tenant_id: str,
    co,
    status: ApplicationStatus = ApplicationStatus.SUBMITTED,
    submitted_days_ago: Optional[float] = 1.0,
    reserved: bool = True,
    released: bool = False,
) -> ApplicationRow:
    """An application attempt as an earlier submission / in-flight run would have left it."""
    opp = co.opportunity
    now = db_now()
    row = ApplicationRow(
        job_id=opp.canonical_job_id,
        tenant_id=tenant_id,
        opportunity_id=opp.id,
        candidate_opportunity_id=co.id,
        status=status.value,
        attempt_number=1,
        cap_day="2000-01-01",
        cap_week="2000-W01",
        reserved_at=now - timedelta(days=submitted_days_ago or 0) if reserved else None,
        released_at=now if released else None,
        submitted_at=(now - timedelta(days=submitted_days_ago)) if submitted_days_ago is not None and status in (ApplicationStatus.SUBMITTED, ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEWING, ApplicationStatus.UNCERTAIN) else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def decisions_by_co(preview) -> dict:
    return {d.candidate_opportunity_id: d for d in preview.decisions}
