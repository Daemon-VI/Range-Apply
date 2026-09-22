"""Startup / restart recovery: runs left RUNNING by a dead worker are settled
the safe way; leases lapse to reclaimable; UNKNOWN stays UNKNOWN; SUBMITTING
with submit_invoked never becomes retryable; live workers are left alone."""

from datetime import timedelta

import pytest

from app.application.models import ApplicationStatus
from app.core.timeutils import db_now
from app.execution.models import ExecutorKind
from app.execution.recovery import recover_execution
from app.pipeline.models import QueueState
from app.signals.database.models import SignalRow
from tests.hardening.conftest import begin, expire_lease


def test_lost_before_submit_becomes_retryable_and_lost_after_submit_becomes_uncertain(harness, db_session):
    before = harness.ready(company="Recover Before")
    after = harness.ready(company="Recover After")
    item_b, run_b, _ = begin(harness, before, worker="dead-1")
    item_a, run_a, _ = begin(harness, after, worker="dead-2")
    run_a.submit_invoked = True
    db_session.commit()
    expire_lease(db_session, item_b)
    expire_lease(db_session, item_a)
    counts = harness.service.recover_lost_runs()
    assert counts == {"retryable": 1, "uncertain": 1}
    for obj in (before, after, run_a, run_b, item_a, item_b):
        db_session.refresh(obj)
    assert run_b.status == "FAILED_RETRYABLE" and before.status == ApplicationStatus.READY.value and before.submission_key is None and item_b.state == QueueState.PENDING.value
    assert run_a.status == "UNKNOWN" and run_a.verification_status == "PENDING" and after.status == ApplicationStatus.UNCERTAIN.value and item_a.state == QueueState.NEEDS_REVIEW.value
    assert db_session.query(SignalRow).filter(SignalRow.application_id == after.id).count() == 1, "the uncertain outcome is a signal"
    # the retryable one is executed exactly once afterwards; the uncertain one never
    counts = harness.service.run_queue("w-new", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts.get("submitted") == 1
    assert harness.mock.submit_calls[before.id] == 1 and harness.mock.submit_calls[after.id] == 0
    assert harness.service.recover_lost_runs() == {}


def test_live_lease_is_left_alone_and_recovery_is_idempotent(harness, db_session):
    attempt = harness.ready(company="Live Co")
    item, run, _ = begin(harness, attempt, worker="alive")
    assert harness.service.recover_lost_runs() == {"live": 1}
    db_session.refresh(run)
    assert run.status == "RUNNING" and item.state == QueueState.PROCESSING.value
    expire_lease(db_session, item)
    assert harness.service.recover_lost_runs() == {"retryable": 1}
    assert harness.service.recover_lost_runs() == {}


def test_startup_recovery_covers_every_tenant_and_never_raises(harness, db_session, other_tenant_id, monkeypatch):
    attempt = harness.ready(company="Boot Co")
    item, run, _ = begin(harness, attempt, worker="dead")
    expire_lease(db_session, item)
    totals = recover_execution(db_session)
    assert totals.get("retryable") == 1
    db_session.refresh(run)
    assert run.status == "FAILED_RETRYABLE"
    import app.execution.recovery as rec

    def boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(rec.ExecutionService, "recover_lost_runs", boom)
    assert recover_execution(db_session, [harness.tenant_id, other_tenant_id]) == {}


def test_unknown_stays_unknown_across_restart_and_submitting_is_not_blindly_retryable(harness, db_session):
    attempt = harness.ready(company="Unknown Stays")
    item, run, _ = begin(harness, attempt, worker="dead")
    run.submit_invoked = True
    db_session.commit()
    expire_lease(db_session, item)
    harness.service.recover_lost_runs()
    for _ in range(2):
        harness.service.recover_lost_runs()
        recover_execution(db_session, [harness.tenant_id])
    db_session.refresh(attempt)
    db_session.refresh(run)
    assert attempt.status == ApplicationStatus.UNCERTAIN.value and run.status == "UNKNOWN"
    with pytest.raises(Exception):
        harness.service.retry(attempt.id, "human")
    assert harness.mock.submit_calls[attempt.id] == 0


def test_queue_reclaim_expired_and_scheduler_stale_run_supersede(harness, db_session, scheduler):
    from app.scheduler.database.models import SchedulerRunRow

    attempt = harness.ready(company="Reclaim Co")
    item = harness.item(attempt)
    harness.service.claim_item(item, "gone")
    expire_lease(db_session, item)
    assert harness.service.queue.reclaim_expired("startup") == 1
    db_session.commit()
    db_session.refresh(item)
    assert item.state == QueueState.PENDING.value and item.claimed_by is None
    stuck = SchedulerRunRow(tenant_id=harness.tenant_id, status="RUNNING", trigger="test", heartbeat_at=db_now() - timedelta(hours=2), started_at=db_now() - timedelta(hours=2))
    db_session.add(stuck)
    db_session.commit()
    run = scheduler.run(window=10)
    db_session.refresh(stuck)
    assert run.status == "COMPLETED" and stuck.status != "RUNNING" and any("superseded" in e for e in stuck.errors)
