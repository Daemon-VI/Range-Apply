"""Deterministic ordering, windows (batching ≠ cap), overlapping runs, tenancy."""

from datetime import timedelta

import pytest

from app.core.errors import ConflictError
from app.core.timeutils import db_now
from app.pipeline.models import AdmissionReason, QueueAction
from app.scheduler.database.models import SchedulerRunRow
from app.scheduler.service import SchedulerService
from tests.scheduler.conftest import decisions_by_co, set_policy


def test_ordering_priority_then_deadline_then_freshness_then_id(db_session, tenant_id, scheduler, opportunities):
    cos = [opportunities.make(title=f"R{i}", fit_score=80, company=f"C{i}") for i in range(5)]
    now = db_now()
    cos[0].priority_score = 50
    cos[1].priority_score = 50
    cos[2].priority_score = 50
    cos[3].priority_score = 70
    cos[4].priority_score = 50
    cos[0].opportunity.deadline = now + timedelta(days=10)
    cos[1].opportunity.deadline = now + timedelta(days=2)
    cos[2].opportunity.deadline = None
    cos[4].opportunity.deadline = None
    cos[2].opportunity.first_seen_at = now - timedelta(days=1)
    cos[4].opportunity.first_seen_at = now - timedelta(days=5)
    db_session.commit()
    order = [d.candidate_opportunity_id for d in scheduler.preview().decisions]
    assert order == [cos[3].id, cos[1].id, cos[0].id, cos[2].id, cos[4].id]
    assert order == [d.candidate_opportunity_id for d in scheduler.preview().decisions], "stable"
    # Exact ties fall back to the opportunity id.
    cos[2].opportunity.first_seen_at = cos[4].opportunity.first_seen_at
    db_session.commit()
    order = [d.candidate_opportunity_id for d in scheduler.preview().decisions]
    tail = sorted([cos[2], cos[4]], key=lambda c: c.opportunity_id)
    assert order[-2:] == [tail[0].id, tail[1].id]


def test_window_batches_without_dropping_anything(db_session, tenant_id, scheduler, opportunities):
    cos = [opportunities.make(title=f"R{i}", fit_score=80, company=f"C{i}") for i in range(7)]
    set_policy(db_session, tenant_id, daily_cap=100)
    first = scheduler.run(window=3)
    assert first.admitted == 3 and first.deferred == 4 and first.blocked == 0
    deferred = [co for co in cos if db_session.refresh(co) or co.scheduler_code == "WINDOW_DEFERRED"]
    assert len(deferred) == 4
    second = scheduler.run(window=3)
    assert second.admitted == 3 and second.deferred == 1 and second.already_queued == 3
    third = scheduler.run(window=3)
    assert third.admitted == 1 and third.deferred == 0 and third.already_queued == 6
    _, total = scheduler.queue.list_items(action=QueueAction.PREPARE)
    assert total == 7, "every admissible opportunity is enqueued eventually"
    assert scheduler.run(window=3).admitted == 0


def test_no_hidden_top_n(db_session, tenant_id, scheduler, opportunities):
    cos = [opportunities.make(title=f"R{i}", fit_score=30 + i, company=f"C{i}") for i in range(12)]
    set_policy(db_session, tenant_id, daily_cap=1000, weekly_cap=1000)
    run = scheduler.run(window=10000)
    assert run.admitted == len(cos) == run.enqueued


def test_overlapping_run_is_refused_then_stale_lease_is_taken_over(db_session, tenant_id, scheduler, opportunities):
    opportunities.make(fit_score=80, company="A")
    stuck = SchedulerRunRow(tenant_id=tenant_id, status="RUNNING", trigger="crashed", actor="x", policy_version=1, window_size=1)
    db_session.add(stuck)
    db_session.commit()
    with pytest.raises(ConflictError):
        scheduler.run()
    stuck.heartbeat_at = db_now() - timedelta(hours=1)
    db_session.commit()
    run = scheduler.run()
    assert run.status == "COMPLETED" and run.admitted == 1
    db_session.refresh(stuck)
    assert stuck.status == "FAILED" and "superseded" in stuck.errors[0]
    forced = scheduler.run(force=True)
    assert forced.status == "COMPLETED"


def test_concurrent_admission_of_the_same_opportunity_converges(db_session, tenant_id, scheduler, opportunities, monkeypatch):
    """Two schedulers with the same stale snapshot: the second sees the first's attempt."""
    co = opportunities.make(fit_score=80, company="A")
    other = SchedulerService(db_session, tenant_id, actor="second")
    first_load = SchedulerService._load
    snapshots = []

    def capture(self):
        result = first_load(self)
        snapshots.append(result)
        return result

    monkeypatch.setattr(SchedulerService, "_load", capture)
    run1 = scheduler.run(force=True)
    assert run1.admitted == 1
    # Replay the pre-admission snapshot in the second service.
    stale = snapshots[0]
    monkeypatch.setattr(SchedulerService, "_load", lambda self: stale)
    run2 = other.run(force=True)
    assert run2.admitted == 0 and run2.already_queued == 1 and run2.errors == []
    attempts = db_session.query(type(scheduler.attempts.get_for_opportunity(co.opportunity_id))).filter_by(tenant_id=tenant_id).count()
    assert attempts == 1
    assert scheduler.capacity().day.used == 1


def test_run_failure_is_recorded(db_session, tenant_id, scheduler, opportunities, monkeypatch):
    opportunities.make(fit_score=80, company="A")
    monkeypatch.setattr(SchedulerService, "_reconcile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run = scheduler.run()
    assert run.status == "FAILED" and run.errors == ["RuntimeError: boom"]
    assert scheduler.running_run() is None


def test_tenant_a_cannot_schedule_tenant_b(db_session, tenant_id, other_tenant_id, scheduler, other_scheduler, opportunities, jobs, matches):
    from tests.preparation.conftest import OpportunityFactory

    mine = opportunities.make(fit_score=80, company="Mine")
    theirs = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(fit_score=80, company="Theirs")
    run = scheduler.run()
    assert run.considered == 1 and run.admitted == 1
    assert scheduler.attempts.get_for_opportunity(theirs.opportunity_id) is None
    assert other_scheduler.attempts.get_for_opportunity(mine.opportunity_id) is None
    assert other_scheduler.capacity().day.used == 0 and scheduler.capacity().day.used == 1
    assert other_scheduler.history()[1] == 0 and scheduler.history()[1] == 1
    assert other_scheduler.get_run(run.id) is None
    other_run = other_scheduler.run()
    assert other_run.considered == 1 and other_run.admitted == 1
    assert scheduler.queue.find(theirs.opportunity_id, QueueAction.PREPARE) is None
    assert decisions_by_co(other_scheduler.preview()).keys() == {theirs.id}


def test_cooldown_and_blocklist_are_per_tenant(db_session, tenant_id, other_tenant_id, scheduler, other_scheduler, opportunities, jobs, matches):
    from tests.preparation.conftest import OpportunityFactory

    opportunities.make(fit_score=80, company="Shared")
    theirs = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(fit_score=80, company="Shared")
    scheduler.run()  # tenant A now has Shared in cool-down
    assert decisions_by_co(other_scheduler.preview())[theirs.id].code is AdmissionReason.ADMITTED
