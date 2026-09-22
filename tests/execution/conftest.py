"""Fixtures for Phase 6 execution tests.

Builds on the scheduler fixtures: a per-test tenant with the seeded Evidence
Graph and answer bank, an opportunity factory, and helpers that take a
candidate opportunity all the way to a READY attempt (scheduler admission +
zero-AI preparation) and hand back an ExecutionService with a MockExecutor.
"""

import pytest

from app.career.repository import EvidenceRepository
from app.execution.executors.manual import ManualExecutor
from app.execution.executors.mock import MockExecutor
from app.execution.models import ExecutorKind
from app.execution.service import ExecutionService
from app.pipeline.models import QueueAction
from tests.scheduler import conftest as _sched

OpportunityFactory = _sched.OpportunityFactory
db_session = _sched.db_session
tenant_id = _sched.tenant_id
other_tenant_id = _sched.other_tenant_id
evidence = _sched.evidence
answered_bank = _sched.answered_bank
jobs = _sched.jobs
matches = _sched.matches
opportunities = _sched.opportunities
scheduler = _sched.scheduler
other_scheduler = _sched.other_scheduler
set_policy = _sched.set_policy
fake_attempt = _sched.fake_attempt


@pytest.fixture(autouse=True)
def _live_submission_enabled(monkeypatch):
    """These tests exercise the submit path itself (mock clicks, the pre-submit
    gate, UNKNOWN after the click), so they run as if the person had switched
    every tenant to LIVE SUBMISSION ENABLED. The SAFE refusal is tested in
    ``tests/desktop/test_submission_mode_safety.py`` with this switch off."""
    from app.execution import submission_mode

    monkeypatch.setattr(submission_mode, "is_live_enabled", lambda tenant_id: bool(tenant_id))


class ExecutionHarness:
    """One tenant, one mock executor, helpers to produce READY attempts."""

    def __init__(self, session, tenant_id, scheduler, opportunities, script=None, default="SUCCESS", contact=True):
        self.session = session
        self.tenant_id = tenant_id
        self.scheduler = scheduler
        self.opportunities = opportunities
        self.mock = MockExecutor(script=script, default=default)
        self.service = ExecutionService(session, tenant_id, actor="test", executors={ExecutorKind.MOCK: self.mock, ExecutorKind.MANUAL: ManualExecutor()})
        if contact:
            # The seed profile has no email/phone (they are pending user input);
            # the standard mock form requires an email, so give the tenant one.
            EvidenceRepository(session, tenant_id).upsert_profile({"email": "ribhu@example.com", "phone": "+91 90000 00000"}, None, "test")
            session.commit()

    def ready(self, title="Backend Engineer", company="Alpha", fit_score=90, application_url=None, **kwargs):
        co = self.opportunities.make(title=title, fit_score=fit_score, company=company, **kwargs)
        if application_url:
            from app.jobs.database.models import JobRow

            job = self.session.get(JobRow, co.opportunity.canonical_job_id)
            job.application_url = application_url
            self.session.commit()
        run = self.scheduler.run(prepare=True, prepare_limit=50, worker_id="prep")
        assert run.errors == [], run.errors
        attempt = self.service.attempts.get_for_opportunity(co.opportunity_id)
        assert attempt is not None and attempt.status == "READY", (attempt.status if attempt else None, run.blocked_by_reason)
        return attempt

    def item(self, attempt):
        return self.service.queue.find(attempt.opportunity_id, QueueAction.SUBMIT)

    def execute(self, attempt, worker="w1", executor=ExecutorKind.MOCK):
        item = self.item(attempt)
        assert item is not None, "no SUBMIT item; the scheduler run should have enqueued it"
        claimed = self.service.claim(worker, limit=50)
        mine = next((i for i in claimed if i.id == item.id), None)
        if mine is None:
            for extra in claimed:
                self.service.queue.release(extra, worker, "not the one under test")
            self.session.commit()
            raise AssertionError(f"item {item.id} not claimable (state {item.state})")
        for extra in claimed:
            if extra.id != item.id:
                self.service.queue.release(extra, worker, "not the one under test")
        self.session.commit()
        outcome = self.service.execute(mine, worker, executor)
        self.session.refresh(attempt)
        return outcome

    def refresh(self, attempt):
        self.session.refresh(attempt)
        return attempt

    def session_running_again(self) -> bool:
        executor = getattr(self, "pw", None)
        return executor is not None and executor.session.running


@pytest.fixture
def harness(db_session, tenant_id, scheduler, opportunities, answered_bank):
    return ExecutionHarness(db_session, tenant_id, scheduler, opportunities)


def scripted(db_session, tenant_id, scheduler, opportunities, script=None, default="SUCCESS", contact=True) -> ExecutionHarness:
    return ExecutionHarness(db_session, tenant_id, scheduler, opportunities, script=script, default=default, contact=contact)
