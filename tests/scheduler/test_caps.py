"""Daily / weekly caps: what counts, boundaries, atomicity, releases."""

import threading
from datetime import datetime, timezone

import pytest

from app.application.models import ApplicationStatus
from app.core.timeutils import db_now
from app.database import get_engine, get_session_factory
from app.pipeline.models import AdmissionReason, OpportunityState, QueueAction
from app.scheduler.caps import CapLedger, period_keys
from tests.scheduler.conftest import decisions_by_co, set_policy


def _many(opportunities, n: int, fit: int = 80):
    return [opportunities.make(title=f"Role {i}", fit_score=fit, company=f"Co{i}") for i in range(n)]


# ------------------------------------------------------------------ periods


def test_period_keys_follow_tenant_timezone():
    # 2026-09-13 23:30 UTC is already Monday 14th 05:00 in Kolkata (UTC+5:30).
    now = datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc)
    utc = period_keys(now, "UTC")
    kolkata = period_keys(now, "Asia/Kolkata")
    assert utc.day.key == "2026-09-13" and kolkata.day.key == "2026-09-14"
    assert utc.week.key == "2026-W37" and kolkata.week.key == "2026-W38"  # Sunday vs Monday
    assert kolkata.day.starts_at == datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)
    assert kolkata.week.starts_at == datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)
    assert utc.week.starts_at == datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    assert (utc.day.ends_at - utc.day.starts_at).total_seconds() == 86400


def test_unknown_timezone_falls_back_to_utc_and_update_rejects_it(db_session, tenant_id):
    from app.core.errors import ValidationFailed

    assert period_keys(datetime(2026, 1, 1, tzinfo=timezone.utc), "Mars/Olympus").day.key == "2026-01-01"
    with pytest.raises(ValidationFailed):
        set_policy(db_session, tenant_id, timezone="Mars/Olympus")


# ------------------------------------------------------------------ ledger


def test_ledger_never_exceeds_cap_regardless_of_stale_snapshots(db_session, tenant_id):
    ledger = CapLedger(db_session, tenant_id)
    keys = period_keys(datetime(2026, 5, 5, 12, tzinfo=timezone.utc), "UTC")
    outcomes = [ledger.reserve(keys, daily_cap=3, weekly_cap=10) for _ in range(5)]
    assert outcomes == [None, None, None, AdmissionReason.DAILY_CAP_REACHED, AdmissionReason.DAILY_CAP_REACHED]
    assert ledger.usage("DAY", keys.day.key) == 3 and ledger.usage("WEEK", keys.week.key) == 3
    # Weekly full: the day slot just taken is handed back.
    keys2 = period_keys(datetime(2026, 5, 6, 12, tzinfo=timezone.utc), "UTC")
    assert ledger.reserve(keys2, daily_cap=3, weekly_cap=3) is AdmissionReason.WEEKLY_CAP_REACHED
    assert ledger.usage("DAY", keys2.day.key) == 0
    # A release frees the slot in the same period.
    ledger.release(keys.day.key, keys.week.key)
    assert ledger.usage("DAY", keys.day.key) == 2
    assert ledger.reserve(keys, daily_cap=3, weekly_cap=10) is None
    db_session.rollback()


def test_ledger_cap_zero_is_paused(db_session, tenant_id):
    ledger = CapLedger(db_session, tenant_id)
    keys = period_keys(datetime(2026, 5, 5, 12, tzinfo=timezone.utc), "UTC")
    assert ledger.reserve(keys, 0, 10) is AdmissionReason.DAILY_CAP_REACHED
    assert ledger.reserve(keys, 10, 0) is AdmissionReason.WEEKLY_CAP_REACHED
    assert ledger.usage("DAY", keys.day.key) == 0


@pytest.mark.skipif(get_engine().dialect.name != "postgresql", reason="true concurrency needs PostgreSQL (SQLite is single-writer)")
def test_ledger_is_atomic_across_concurrent_sessions(tenant_id):
    keys = period_keys(datetime(2026, 5, 5, 12, tzinfo=timezone.utc), "UTC")
    factory = get_session_factory()
    wins = []
    lock = threading.Lock()

    def worker():
        session = factory()
        try:
            ledger = CapLedger(session, tenant_id)
            for _ in range(20):
                ok = ledger.reserve(keys, daily_cap=25, weekly_cap=100) is None
                session.commit()
                if ok:
                    with lock:
                        wins.append(1)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 25


# ------------------------------------------------------------- scheduler


def test_daily_cap_limits_admissions_in_priority_order(db_session, tenant_id, scheduler, opportunities):
    cos = _many(opportunities, 5)
    for i, co in enumerate(cos):
        co.priority_score = 10 * (i + 1)  # last is highest priority
    db_session.commit()
    set_policy(db_session, tenant_id, daily_cap=2)
    run = scheduler.run()
    assert run.admitted == 2 and run.blocked == 3
    assert run.blocked_by_reason == {"DAILY_CAP_REACHED": 3}
    admitted = {i.candidate_opportunity_id for i in scheduler.queue.list_items(action=QueueAction.PREPARE)[0]}
    assert admitted == {cos[4].id, cos[3].id}
    for co in cos[:3]:
        db_session.refresh(co)
        assert co.scheduler_code == "DAILY_CAP_REACHED"
        assert co.policy_admitted is True, "a cap is capacity, not a policy refusal"
        assert co.state == OpportunityState.ELIGIBLE.value
    cap = scheduler.capacity()
    assert cap.day.used == 2 and cap.day.remaining == 0 and cap.week.used == 2


def test_weekly_cap_applies_after_daily(db_session, tenant_id, scheduler, opportunities):
    _many(opportunities, 4)
    set_policy(db_session, tenant_id, daily_cap=10, weekly_cap=3)
    run = scheduler.run()
    assert run.admitted == 3 and run.blocked_by_reason == {"WEEKLY_CAP_REACHED": 1}


def test_cap_zero_pauses_without_touching_anything(db_session, tenant_id, scheduler, opportunities):
    cos = _many(opportunities, 2)
    set_policy(db_session, tenant_id, daily_cap=0)
    preview = scheduler.preview()
    assert {d.code for d in preview.decisions} == {AdmissionReason.DAILY_CAP_REACHED}
    assert preview.capacity.paused
    run = scheduler.run()
    assert run.admitted == 0 and run.blocked == 2
    for co in cos:
        assert scheduler.attempts.get_for_opportunity(co.opportunity_id) is None


def test_preparation_and_retries_do_not_consume_cap(db_session, tenant_id, scheduler, opportunities, answered_bank):
    """Only admitted attempts count. Preparing, failing and retrying the same attempt never adds."""
    cos = _many(opportunities, 2)
    set_policy(db_session, tenant_id, daily_cap=5)
    scheduler.run()
    assert scheduler.capacity().day.used == 2
    items, _ = scheduler.queue.list_items(action=QueueAction.PREPARE)
    worker = "w1"
    claimed = scheduler.queue.claim(worker, action=QueueAction.PREPARE, limit=1)
    scheduler.queue.start(claimed[0], worker)
    scheduler.queue.fail(claimed[0], worker, "transient", retryable=True)
    db_session.commit()
    assert claimed[0].state == "RETRY_WAIT" and claimed[0].attempts == 1
    scheduler.run()
    assert scheduler.capacity().day.used == 2
    # Prepare for real (zero AI) through the scheduler: still 2.
    claimed[0].available_at = db_now()
    db_session.commit()
    run = scheduler.run(prepare=True, prepare_limit=10)
    assert run.preparation["claimed"] >= 1
    assert scheduler.capacity().day.used == 2
    assert len(cos) == 2


def test_cancelled_and_failed_attempts_release_their_slot(db_session, tenant_id, scheduler, opportunities):
    a, b = _many(opportunities, 2)
    set_policy(db_session, tenant_id, daily_cap=2)
    scheduler.run()
    assert scheduler.capacity().day.remaining == 0
    item = scheduler.queue.find(a.opportunity_id, QueueAction.PREPARE)
    scheduler.queue.cancel(item, "user", "changed my mind")
    db_session.commit()
    run = scheduler.run()
    assert run.released == 1
    attempt = scheduler.attempts.get_for_opportunity(a.opportunity_id)
    assert attempt.released_at is not None and attempt.status == ApplicationStatus.CLOSED.value
    assert scheduler.capacity().day.remaining == 1
    db_session.refresh(a)
    assert a.scheduler_code == "USER_BLOCKED"  # cancelled items are not silently re-queued
    # A permanent failure also releases, and asks a human before retrying.
    item_b = scheduler.queue.find(b.opportunity_id, QueueAction.PREPARE)
    claimed = scheduler.queue.claim("w", action=QueueAction.PREPARE, limit=5)
    mine = next(i for i in claimed if i.id == item_b.id)
    scheduler.queue.start(mine, "w")
    scheduler.queue.fail(mine, "w", "boom", retryable=False)
    db_session.commit()
    run = scheduler.run()
    assert run.released == 1 and run.needs_review == 1
    assert scheduler.attempts.get_for_opportunity(b.opportunity_id).status == ApplicationStatus.FAILED.value
    assert scheduler.capacity().day.remaining == 2


def test_readmission_after_release_reuses_the_attempt_row(db_session, tenant_id, scheduler, opportunities):
    (a,) = _many(opportunities, 1)
    scheduler.run()
    item = scheduler.queue.find(a.opportunity_id, QueueAction.PREPARE)
    scheduler.queue.cancel(item, "user")
    db_session.commit()
    scheduler.run()  # releases
    scheduler.queue.requeue(item, "user", "on second thought")
    db_session.commit()
    run = scheduler.run()
    assert run.admitted == 1
    attempt = scheduler.attempts.get_for_opportunity(a.opportunity_id)
    assert attempt.attempt_number == 2 and attempt.released_at is None and attempt.status == ApplicationStatus.QUALIFIED.value
    assert db_session.query(type(attempt)).filter_by(tenant_id=tenant_id).count() == 1


def test_preview_projects_caps_in_order(db_session, tenant_id, scheduler, opportunities):
    cos = _many(opportunities, 3)
    for i, co in enumerate(cos):
        co.priority_score = 50 - i
    db_session.commit()
    set_policy(db_session, tenant_id, daily_cap=1)
    preview = scheduler.preview()
    by = decisions_by_co(preview)
    assert by[cos[0].id].code is AdmissionReason.ADMITTED
    assert by[cos[1].id].code is AdmissionReason.DAILY_CAP_REACHED
    assert preview.would_admit == 1 and preview.admissible == 3
    assert preview.by_code == {"ADMITTED": 1, "DAILY_CAP_REACHED": 2}
