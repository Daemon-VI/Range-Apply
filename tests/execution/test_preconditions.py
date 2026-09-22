"""Submission-time preconditions: nothing is assumed unchanged since scheduling."""

from datetime import timedelta

from app.application.models import ApplicationStatus
from app.core.timeutils import db_now, utc_now
from app.execution.models import ExecutionStatus, PreconditionCode
from app.pipeline.models import OpportunityState, QueueAction, QueueState
from app.preparation.service import PreparationService
from app.scheduler.caps import period_keys
from tests.execution.conftest import fake_attempt, set_policy


def _codes(report):
    return {f.code for f in report.failures}


def test_preview_is_read_only_and_ok_for_a_fresh_ready_attempt(harness, db_session):
    attempt = harness.ready()
    preview = harness.service.preview(attempt.id)
    assert preview.preconditions.ok and preview.package.preparation_id == attempt.preparation_id
    assert preview.package.resume is not None and preview.package.answers
    assert preview.package.target.ats_family.value == "greenhouse" and preview.package.target.method.value == "browser_form"
    assert preview.form is None and preview.blocking_fields == 0
    assert harness.service.runs_for(attempt.id) == []


def test_stale_preparation_is_reprepared_not_submitted(harness, db_session):
    attempt = harness.ready()
    from app.jobs.database.models import JobRow

    job = db_session.get(JobRow, harness.service.package(attempt).preparation_inputs["job_id"])
    job.content_hash = "changed-by-employer"
    db_session.commit()
    report = harness.service.preconditions(attempt)
    assert PreconditionCode.PREPARATION_STALE in _codes(report) and "job_content_hash" in report.stale_inputs
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "precondition_failed"
    assert attempt.status == ApplicationStatus.PREPARING.value and harness.mock.submit_calls[attempt.id] == 0
    run = harness.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.STALE.value
    prep = PreparationService(db_session, harness.tenant_id).require(attempt.preparation_id)
    assert prep.status == "INVALIDATED"
    assert harness.service.queue.find(attempt.opportunity_id, QueueAction.PREPARE).state == QueueState.PENDING.value
    assert harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    # Re-preparing through the scheduler makes it READY again with a new version.
    sched = harness.scheduler.run(prepare=True, prepare_limit=10)
    assert sched.ready_for_execution == 1
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value and attempt.preparation_id != prep.id
    assert harness.service.preconditions(attempt).ok


def test_evidence_change_makes_preparation_stale(harness, db_session, evidence):
    attempt = harness.ready()
    from app.career.models import EvidenceNodeUpdate

    node = evidence.require_node("skill-python")
    evidence.update_node(node.key, EvidenceNodeUpdate(claim=node.claim + " (updated)"), actor="user")
    evidence.commit()
    report = harness.service.preconditions(attempt)
    assert PreconditionCode.PREPARATION_STALE in _codes(report) and "evidence_fingerprint" in report.stale_inputs


def test_company_blocked_after_scheduling_stops_execution(harness, db_session):
    attempt = harness.ready(company="Soon Blocked")
    set_policy(db_session, harness.tenant_id, blocked_companies=[harness.opportunities.jobs.company("SOON-blocked")])
    harness.service._policy = None
    outcome = harness.execute(attempt)
    assert outcome["codes"] == ["COMPANY_BLOCKED"]
    assert attempt.status == ApplicationStatus.CLOSED.value and attempt.released_at is not None
    assert harness.item(attempt).state == QueueState.CANCELLED.value and harness.mock.submit_calls[attempt.id] == 0
    assert harness.service.summary().metrics["blocklist_blocks"] == 1


def test_opportunity_closed_after_scheduling(harness, db_session):
    attempt = harness.ready()
    harness.service.repo.get_opportunity(attempt.opportunity_id).status = "CLOSED"
    db_session.commit()
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.CLOSED.value and harness.mock.submit_calls[attempt.id] == 0


def test_cooldown_that_became_active_blocks_submission(harness, db_session):
    attempt = harness.ready(company="Acme", title="Backend Engineer")
    # Meanwhile the candidate applied to another Acme opening by hand.
    other = harness.opportunities.make(title="Data Scientist", fit_score=70, company="Acme")
    fake_attempt(db_session, harness.tenant_id, other, ApplicationStatus.SUBMITTED, submitted_days_ago=0.5)
    report = harness.service.preconditions(attempt)
    assert PreconditionCode.COOLDOWN_ACTIVE in _codes(report)
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == "COOLDOWN_ACTIVE"
    assert attempt.released_at is None and harness.item(attempt).state == QueueState.BLOCKED.value
    assert harness.service.summary().metrics["cooldown_blocks"] == 1


def test_duplicate_title_submitted_elsewhere_blocks(harness, db_session):
    attempt = harness.ready(company="Acme", title="Backend Engineer", location="Remote")
    other = harness.opportunities.make(title="Backend Engineer", fit_score=70, company="Acme", location="Berlin", remote_type="ONSITE")
    fake_attempt(db_session, harness.tenant_id, other, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    set_policy(db_session, harness.tenant_id, cooldown_days=0)
    harness.service._policy = None
    assert PreconditionCode.DUPLICATE_APPLICATION in _codes(harness.service.preconditions(attempt))
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.CLOSED.value and harness.mock.submit_calls[attempt.id] == 0


def test_already_submitted_attempt_is_not_executed_again(harness, db_session):
    attempt = harness.ready()
    harness.execute(attempt)
    item = harness.item(attempt)
    harness.service.queue.requeue(item, "user", "accidental requeue", allow_succeeded=False) if item.state != "SUCCEEDED" else None
    # Force a second SUBMIT item into the queue and drain it: refused, no second submit.
    if item.state == QueueState.SUCCEEDED.value:
        item.state = QueueState.PENDING.value
        item.claimed_by = None
        db_session.commit()
    counts = harness.service.run_queue("w2", limit=5)
    assert counts["already_submitted"] == 1 and harness.mock.submit_calls[attempt.id] == 1


def test_preparation_invalidated_needs_review(harness, db_session):
    attempt = harness.ready()
    PreparationService(db_session, harness.tenant_id).invalidate(attempt.preparation_id, "user", "wrong variant")
    db_session.commit()
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value and harness.mock.submit_calls[attempt.id] == 0
    assert harness.service.runs_for(attempt.id)[0].status == ExecutionStatus.PRECONDITION_FAILED.value


def test_required_answer_missing_needs_user_input(harness, db_session):
    attempt = harness.ready()
    prep = PreparationService(db_session, harness.tenant_id).require(attempt.preparation_id)
    answer = prep.answers[0]
    answer.status, answer.answer, answer.required = "NEEDS_USER_INPUT", None, True
    db_session.commit()
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.NEEDS_USER_INPUT.value and harness.mock.submit_calls[attempt.id] == 0


def test_cap_reservation_is_refreshed_across_a_period_boundary(harness, db_session):
    a = harness.ready(company="Alpha")
    b = harness.ready(company="Beta")
    ledger = harness.service.attempts.ledger
    # Both were reserved "yesterday".
    yesterday = period_keys(utc_now() - timedelta(days=8), "UTC")
    for attempt in (a, b):
        ledger.release(attempt.cap_day, attempt.cap_week)
        ledger._ensure("DAY", yesterday.day.key)
        ledger._ensure("WEEK", yesterday.week.key)
        ledger._increment("DAY", yesterday.day.key, None)
        ledger._increment("WEEK", yesterday.week.key, None)
        attempt.cap_day, attempt.cap_week = yesterday.day.key, yesterday.week.key
    db_session.commit()
    set_policy(db_session, harness.tenant_id, daily_cap=1)
    harness.service._policy = None
    assert harness.service.preconditions(a).ok
    today = period_keys(utc_now(), "UTC")
    harness.execute(a)
    assert a.status == ApplicationStatus.VERIFIED.value and a.cap_day == today.day.key
    assert ledger.usage("DAY", today.day.key) == 1 and ledger.usage("DAY", yesterday.day.key) == 1, "old slot handed back, today's taken"
    # No room left today for the second one: it waits, it is not a failure.
    report = harness.service.preconditions(b)
    assert _codes(report) == {PreconditionCode.DAILY_CAP_REACHED}
    outcome = harness.execute(b, worker="w2")
    assert outcome["codes"] == ["DAILY_CAP_REACHED"]
    assert b.status == ApplicationStatus.READY.value and harness.mock.submit_calls[b.id] == 0
    item = harness.item(b)
    assert item.state == QueueState.PENDING.value and item.attempts == 0
    assert item.available_at >= today.day.ends_at.replace(tzinfo=None) - timedelta(seconds=1)
    assert harness.service.summary().metrics["cap_blocks"] == 1


def test_cap_zero_pauses_execution(harness, db_session):
    attempt = harness.ready()
    set_policy(db_session, harness.tenant_id, daily_cap=0)
    harness.service._policy = None
    outcome = harness.execute(attempt)
    assert outcome["codes"] == ["DAILY_CAP_REACHED"] and attempt.status == ApplicationStatus.READY.value


def test_skipped_by_candidate_after_scheduling(harness, db_session):
    attempt = harness.ready()
    co = harness.service.repo.get_candidate_opportunity(attempt.candidate_opportunity_id)
    harness.service.repo.transition(co, OpportunityState.SKIPPED, "user", "no thanks", force=True)
    db_session.commit()
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.CLOSED.value and harness.mock.submit_calls[attempt.id] == 0


def test_started_at_is_recent(harness):
    attempt = harness.ready()
    harness.execute(attempt)
    run = harness.service.runs_for(attempt.id)[0]
    assert (db_now() - run.started_at).total_seconds() < 60 and run.finished_at >= run.started_at
