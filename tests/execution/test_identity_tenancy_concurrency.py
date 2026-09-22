"""Canonical identity, tenant isolation, and concurrent workers."""

import threading
from datetime import timedelta

import pytest

from app.application.database.models import ApplicationRow
from app.application.engine import ApplicationEngine
from app.application.models import ApplicationStatus
from app.core.errors import ConflictError, NotFoundError
from app.core.timeutils import db_now
from app.database import get_engine, get_session_factory
from app.execution.executors import mock as m
from app.execution.executors.mock import MockExecutor
from app.execution.models import ExecutionOutcome, ExecutionResult, ExecutionStatus, ExecutorKind
from app.execution.service import ExecutionService
from app.intelligence.database.models import MatchRunRow
from app.pipeline.models import QueueState
from app.pipeline.repository import OpportunityRepository
from tests.execution.conftest import ExecutionHarness, scripted

# ------------------------------------------------------------------ identity


def test_two_source_jobs_one_attempt_one_submission(harness, db_session, jobs):
    attempt = harness.ready(title="Backend Engineer", company="Acme", location="Remote")
    other = jobs.make(title="Backend Engineer", company="Acme", location="Remote (India)", source="LEVER")
    opp, created, _ = OpportunityRepository.resolve_opportunity(db_session, other)
    db_session.commit()
    assert not created and opp.id == attempt.opportunity_id
    engine = ApplicationEngine()
    assert engine.get_or_create(db_session, other.id, tenant_id=harness.tenant_id).id == attempt.id
    assert engine.get_or_create(db_session, attempt.job_id, tenant_id=harness.tenant_id).id == attempt.id
    harness.execute(attempt)
    assert db_session.query(ApplicationRow).filter_by(tenant_id=harness.tenant_id, opportunity_id=opp.id).count() == 1
    assert harness.mock.submit_calls[attempt.id] == 1
    package = harness.service.package(attempt)
    assert (package.tenant_id, package.candidate_opportunity_id, package.opportunity_id, package.preparation_id, package.application_id) == (
        harness.tenant_id, attempt.candidate_opportunity_id, opp.id, attempt.preparation_id, attempt.id
    )


def test_match_run_records_its_tenant(db_session, tenant_id):
    from app.intelligence.services.factory import build_orchestrator
    from app.intelligence.services.match_persistence import run_matching
    from app.services.career_brain import CareerBrainService

    brain = CareerBrainService(db=db_session, tenant_id=tenant_id)
    brain.load()
    run = run_matching(db_session, build_orchestrator(brain), job_ids=["nope"], trigger="test", tenant_id=tenant_id)
    assert run.tenant_id == tenant_id
    assert db_session.get(MatchRunRow, run.id).tenant_id == tenant_id
    db_session.delete(run)
    db_session.commit()


# ------------------------------------------------------------------- tenancy


def test_tenant_a_cannot_execute_tenant_b(harness, db_session, other_tenant_id, jobs, matches):
    attempt = harness.ready()
    item = harness.item(attempt)
    other = ExecutionService(db_session, other_tenant_id, actor="b", executors={ExecutorKind.MOCK: MockExecutor()})
    with pytest.raises(NotFoundError):
        other.require_attempt(attempt.id)
    with pytest.raises(NotFoundError):
        other.preview(attempt.id)
    with pytest.raises(NotFoundError):
        other.start(item, "b-worker", ExecutorKind.MOCK)
    assert other.ready() == [] and other.claim("b-worker", limit=10) == []
    with pytest.raises(NotFoundError):
        other.retry(attempt.id, "b")
    assert other.summary().metrics["attempts"] == 0
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value and harness.mock.submit_calls[attempt.id] == 0


# --------------------------------------------------------------- concurrency


def test_two_workers_cannot_claim_the_same_item(harness):
    attempt = harness.ready()
    first = harness.service.claim("w1", limit=5)
    second = harness.service.claim("w2", limit=5)
    assert [i.id for i in first] == [harness.item(attempt).id] and second == []
    with pytest.raises(ConflictError):
        harness.service.start(first[0], "w2", ExecutorKind.MOCK)


def test_lost_lease_before_submit_is_retried_once_only(harness, db_session):
    attempt = harness.ready()
    item = harness.item(attempt)
    (mine,) = harness.service.claim("w1", limit=1)
    run, package, outcome = harness.service.start(mine, "w1", ExecutorKind.MOCK)
    assert outcome["outcome"] == "started" and attempt.status == ApplicationStatus.SUBMITTING.value
    # w1 dies before pressing submit; the lease lapses; w2 reclaims.
    item.lease_expires_at = db_now() - timedelta(seconds=1)
    db_session.commit()
    (reclaimed,) = harness.service.claim("w2", limit=1)
    assert reclaimed.id == item.id
    result = harness.service.execute(reclaimed, "w2", ExecutorKind.MOCK)
    harness.refresh(attempt)
    assert result["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.VERIFIED.value
    assert harness.mock.submit_calls[attempt.id] == 1
    runs = harness.service.runs_for(attempt.id)
    assert [r.status for r in runs] == [ExecutionStatus.VERIFIED.value, ExecutionStatus.FAILED_RETRYABLE.value]
    assert runs[1].error_class == "EXECUTOR_CRASH" and runs[1].submit_invoked is False


def test_lost_lease_after_submit_becomes_unknown_never_resubmitted(harness, db_session):
    attempt = harness.ready()
    item = harness.item(attempt)
    (mine,) = harness.service.claim("w1", limit=1)
    run, package, _ = harness.service.start(mine, "w1", ExecutorKind.MOCK)
    run.submit_invoked = True  # w1 pressed submit, then vanished
    item.lease_expires_at = db_now() - timedelta(seconds=1)
    db_session.commit()
    (reclaimed,) = harness.service.claim("w2", limit=1)
    result = harness.service.execute(reclaimed, "w2", ExecutorKind.MOCK)
    harness.refresh(attempt)
    assert result["outcome"] == "awaiting_verification" and attempt.status == ApplicationStatus.UNCERTAIN.value
    assert harness.mock.submit_calls[attempt.id] == 0, "the mock never ran; the earlier press counts as the one submit"
    db_session.refresh(run)
    assert run.status == ExecutionStatus.UNKNOWN.value and harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    # Late result from w1 (lease gone) is refused; nothing changes.
    with pytest.raises(ConflictError):
        harness.service.report_result(harness.item(attempt), "w1", ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True))
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.UNCERTAIN.value
    # Verification (executor or person) is the only way out.
    harness.mock.default = m.UNKNOWN_THEN_VERIFIED
    harness.service.verify(run.id, executor_kind=ExecutorKind.MOCK)
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value


def test_result_after_lease_expiry_and_reclaim_is_rejected(harness, db_session):
    attempt = harness.ready()
    item = harness.item(attempt)
    (mine,) = harness.service.claim("w1", limit=1)
    harness.service.start(mine, "w1", ExecutorKind.MOCK)
    item.lease_expires_at = db_now() - timedelta(seconds=1)
    db_session.commit()
    harness.service.claim("w2", limit=1)
    with pytest.raises(ConflictError):
        harness.service.report_result(harness.item(attempt), "w1", ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True))


def test_duplicate_executor_retry_after_ambiguous_result_is_refused(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN)
    attempt = h.ready()
    h.execute(attempt)
    item = h.item(attempt)
    h.service.queue.requeue(item, "impatient", "try again")
    db_session.commit()
    totals = [h.service.run_queue(worker, limit=5) for worker in ("w2", "w3")]
    assert sum(c.get("awaiting_verification", 0) for c in totals) == 1
    assert sum(c.get("submitted", 0) for c in totals) == 0
    assert h.mock.submit_calls[attempt.id] == 1


def test_same_worker_id_twice_on_one_attempt_submits_once(harness, db_session):
    """A restarted process re-uses its worker id and re-runs the item it still owns."""
    attempt = harness.ready()
    (mine,) = harness.service.claim("w1", limit=1)
    harness.service.start(mine, "w1", ExecutorKind.MOCK)
    # The restarted worker calls execute() again on the item it holds.
    outcome = harness.service.execute(harness.item(attempt), "w1", ExecutorKind.MOCK)
    harness.refresh(attempt)
    assert outcome["outcome"] == "SUBMITTED" and harness.mock.submit_calls[attempt.id] == 1


def test_two_attempts_at_the_cap_boundary_admit_exactly_one_refresh(harness, db_session):
    from app.core.timeutils import utc_now
    from app.scheduler.caps import period_keys
    from tests.execution.conftest import set_policy

    a = harness.ready(company="Alpha")
    b = harness.ready(company="Beta")
    old = period_keys(utc_now() - timedelta(days=8), "UTC")
    for attempt in (a, b):
        harness.service.attempts.ledger.release(attempt.cap_day, attempt.cap_week)
        attempt.cap_day, attempt.cap_week = old.day.key, old.week.key
    db_session.commit()
    set_policy(db_session, harness.tenant_id, daily_cap=1)
    harness.service._policy = None
    counts = harness.service.run_queue("w1", limit=5)
    assert counts["submitted"] == 1 and counts["precondition_failed"] == 1
    assert harness.mock.submit_calls[a.id] + harness.mock.submit_calls[b.id] == 1


@pytest.mark.skipif(get_engine().dialect.name != "postgresql", reason="true concurrency needs PostgreSQL (SQLite is single-writer)")
def test_parallel_workers_press_submit_once_per_attempt(tenant_id, db_session, scheduler, opportunities, answered_bank):
    h = ExecutionHarness(db_session, tenant_id, scheduler, opportunities)
    attempts = [h.ready(company=f"Co{i}") for i in range(6)]
    db_session.commit()
    factory = get_session_factory()
    mock = MockExecutor()
    lock = threading.Lock()
    totals = []

    def worker(name):
        session = factory()
        try:
            service = ExecutionService(session, tenant_id, actor=name, executors={ExecutorKind.MOCK: mock})
            for _ in range(4):
                counts = service.run_queue(name, limit=2)
                with lock:
                    totals.append(counts)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(mock.submit_calls[a.id] == 1 for a in attempts), dict(mock.submit_calls)
    assert sum(c.get("submitted", 0) for c in totals) == len(attempts)
