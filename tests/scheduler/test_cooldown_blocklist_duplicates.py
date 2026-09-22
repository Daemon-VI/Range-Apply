"""Company cool-down, blocklist normalisation, duplicates and reposts."""

from datetime import timedelta

from app.application.models import ApplicationStatus
from app.core.timeutils import db_now, utc_now
from app.pipeline.models import AdmissionReason, OpportunityState, QueueAction
from app.pipeline.policy import company_key
from app.pipeline.repository import OpportunityRepository
from app.scheduler.attempts import CooldownIndex
from tests.scheduler.conftest import decisions_by_co, fake_attempt, set_policy

# ------------------------------------------------------------ normalisation


def test_company_key_normalises_case_punctuation_and_suffixes():
    assert company_key("ACME, Inc.") == company_key("acme inc") == company_key("Acme") == "acme"
    assert company_key("Über-Data GmbH") == company_key("uber data")
    assert company_key("Acme Robotics") != company_key("Acme")


def test_cooldown_boundary_is_exact():
    index = CooldownIndex()
    start = utc_now()
    index.add("Acme", start, 10, "submitted")
    until = start + timedelta(days=10)
    assert index.active_until("acme, inc.", until - timedelta(microseconds=1)) == until, "T-ε blocked"
    assert index.active_until("ACME", until) is None, "exactly T is allowed"
    assert index.active_until("Acme", until + timedelta(seconds=1)) is None
    index.add("Acme", None, 10, "submitted")  # unknown start never blocks
    assert index.active_until("Other", start) is None


# ---------------------------------------------------------------- cool-down


def test_cooldown_blocks_same_company_after_submission(db_session, tenant_id, scheduler, opportunities):
    old = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    fake_attempt(db_session, tenant_id, old, ApplicationStatus.SUBMITTED, submitted_days_ago=5)
    new = opportunities.make(title="Platform Engineer", fit_score=90, company="ACME, Inc.")
    set_policy(db_session, tenant_id, cooldown_days=30)
    by = decisions_by_co(scheduler.preview())
    assert by[old.id].code is AdmissionReason.ALREADY_SUBMITTED
    assert by[new.id].code is AdmissionReason.COOLDOWN_ACTIVE
    cap = scheduler.capacity()
    assert cap.cooldowns_active == 1 and cap.cooldowns[0].source == "submitted"
    exact = opportunities.jobs.company("ACME, Inc.")  # the factory suffixes company names
    assert scheduler.repo.company_in_cooldown(exact, 30)
    assert not scheduler.repo.company_in_cooldown(exact, 5)
    # Exactly at the boundary the company is admissible again.
    set_policy(db_session, tenant_id, cooldown_days=5)
    assert decisions_by_co(scheduler.preview())[new.id].code is AdmissionReason.ADMITTED


def test_cooldown_zero_disables(db_session, tenant_id, scheduler, opportunities):
    old = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    fake_attempt(db_session, tenant_id, old, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    new = opportunities.make(title="Platform Engineer", fit_score=90, company="Acme")
    set_policy(db_session, tenant_id, cooldown_days=0)
    assert decisions_by_co(scheduler.preview())[new.id].code is AdmissionReason.ADMITTED


def test_in_flight_attempt_starts_cooldown_so_one_opening_per_company_per_window(db_session, tenant_id, scheduler, opportunities):
    a = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    b = opportunities.make(title="Platform Engineer", fit_score=70, company="Acme")
    a.priority_score, b.priority_score = 80, 70
    db_session.commit()
    run = scheduler.run()
    assert run.admitted == 1 and run.blocked_by_reason == {"COOLDOWN_ACTIVE": 1}
    db_session.refresh(b)
    assert b.scheduler_code == "COOLDOWN_ACTIVE"
    assert scheduler.capacity().cooldowns[0].source == "in_progress"


def test_released_attempts_do_not_start_cooldown(db_session, tenant_id, scheduler, opportunities):
    old = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    fake_attempt(db_session, tenant_id, old, ApplicationStatus.FAILED, submitted_days_ago=None, released=True)
    new = opportunities.make(title="Platform Engineer", fit_score=90, company="Acme")
    assert decisions_by_co(scheduler.preview())[new.id].code is AdmissionReason.ADMITTED


def test_rejected_attempt_starts_cooldown(db_session, tenant_id, scheduler, opportunities):
    old = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    fake_attempt(db_session, tenant_id, old, ApplicationStatus.REJECTED, submitted_days_ago=2)
    new = opportunities.make(title="Platform Engineer", fit_score=90, company="Acme")
    assert decisions_by_co(scheduler.preview())[new.id].code is AdmissionReason.COOLDOWN_ACTIVE


def test_phase2_state_based_applications_count_for_cooldown(db_session, tenant_id, scheduler, opportunities):
    old = opportunities.make(title="Data Engineer", fit_score=80, company="Acme")
    OpportunityRepository(db_session, tenant_id).transition(old, OpportunityState.SUBMITTED, "test", force=True)
    db_session.commit()
    new = opportunities.make(title="Platform Engineer", fit_score=90, company="Acme")
    by = decisions_by_co(scheduler.preview())
    assert by[old.id].code is AdmissionReason.ALREADY_SUBMITTED
    assert by[new.id].code is AdmissionReason.COOLDOWN_ACTIVE


# ---------------------------------------------------------------- blocklist


def test_blocklist_is_normalised(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(title="Engineer", fit_score=90, company="Evil Corp.")
    blocked = "  " + opportunities.jobs.company("EVIL, corp") + " "
    set_policy(db_session, tenant_id, blocked_companies=[blocked])
    assert decisions_by_co(scheduler.preview())[co.id].code is AdmissionReason.COMPANY_BLOCKED
    run = scheduler.run()
    assert run.blocked_by_reason == {"COMPANY_BLOCKED": 1}
    db_session.refresh(co)
    assert co.policy_admitted is False


def test_newly_blocked_company_is_pulled_out_of_flight(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(title="Engineer", fit_score=90, company="Soon Blocked")
    scheduler.run()
    item = scheduler.queue.find(co.opportunity_id, QueueAction.PREPARE)
    assert item.state == "PENDING"
    set_policy(db_session, tenant_id, blocked_companies=[opportunities.jobs.company("soon-blocked")])
    run = scheduler.run()
    assert run.released == 1 and run.blocked_by_reason == {"COMPANY_BLOCKED": 1}
    db_session.refresh(item)
    assert item.state == "CANCELLED"
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    assert attempt.released_at is not None and attempt.status == ApplicationStatus.CLOSED.value
    assert scheduler.capacity().day.used == 0
    assert scheduler.ready_for_execution() == []


# ---------------------------------------------------------------- duplicates


def test_two_source_jobs_one_opportunity_one_attempt(db_session, tenant_id, scheduler, opportunities, jobs, matches):
    co = opportunities.make(title="Backend Engineer", fit_score=80, company="Acme", location="Remote")
    # A second source row for the same opening resolves to the same opportunity.
    other = jobs.make(title="Backend Engineer", company="Acme", location="Remote - US", source="LEVER")
    opp, created, how = OpportunityRepository.resolve_opportunity(db_session, other)
    db_session.commit()
    assert not created and opp.id == co.opportunity_id and how == "identity_key"
    run = scheduler.run()
    assert run.considered == 1 and run.admitted == 1
    assert db_session.query(type(scheduler.attempts.get_for_opportunity(opp.id))).filter_by(tenant_id=tenant_id).count() == 1


def test_same_title_same_company_other_location_is_duplicate_application(db_session, tenant_id, scheduler, opportunities):
    first = opportunities.make(title="Backend Engineer", fit_score=80, company="Acme", location="Bangalore")
    fake_attempt(db_session, tenant_id, first, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    second = opportunities.make(title="Backend Engineer (Remote)", fit_score=80, company="ACME Inc", location="Hyderabad")
    third = opportunities.make(title="Data Engineer", fit_score=80, company="Acme", location="Hyderabad")
    assert second.opportunity_id != first.opportunity_id
    set_policy(db_session, tenant_id, cooldown_days=0)
    by = decisions_by_co(scheduler.preview())
    assert by[second.id].code is AdmissionReason.DUPLICATE_APPLICATION
    assert by[third.id].code is AdmissionReason.ADMITTED


def test_repost_after_submission_follows_duplicate_policy(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(title="Backend Engineer", fit_score=80, company="Acme")
    fake_attempt(db_session, tenant_id, co, ApplicationStatus.SUBMITTED, submitted_days_ago=40)
    opp = co.opportunity
    opp.status = "REPOSTED"
    opp.repost_count = 1
    opp.reposted_at = db_now() - timedelta(days=2)
    db_session.commit()
    set_policy(db_session, tenant_id, cooldown_days=30, duplicate_policy="BLOCK")
    assert decisions_by_co(scheduler.preview())[co.id].code is AdmissionReason.DUPLICATE_OPPORTUNITY
    set_policy(db_session, tenant_id, duplicate_policy="ALLOW_REPOST_AFTER_COOLDOWN")
    assert decisions_by_co(scheduler.preview())[co.id].code is AdmissionReason.ADMITTED
    set_policy(db_session, tenant_id, cooldown_days=60)
    assert decisions_by_co(scheduler.preview())[co.id].code is AdmissionReason.COOLDOWN_ACTIVE
    # A repost *before* the submission is just the same application.
    opp.reposted_at = db_now() - timedelta(days=50)
    db_session.commit()
    set_policy(db_session, tenant_id, cooldown_days=0)
    assert decisions_by_co(scheduler.preview())[co.id].code is AdmissionReason.ALREADY_SUBMITTED


def test_closed_and_skipped_openings(db_session, tenant_id, scheduler, opportunities):
    closed = opportunities.make(title="Closed", fit_score=90, company="A")
    closed.opportunity.status = "CLOSED"
    skipped = opportunities.make(title="Skipped", fit_score=90, company="B")
    OpportunityRepository(db_session, tenant_id).transition(skipped, OpportunityState.SKIPPED, "user", "not for me")
    db_session.commit()
    by = decisions_by_co(scheduler.preview())
    assert by[closed.id].code is AdmissionReason.OPPORTUNITY_CLOSED
    assert by[skipped.id].code is AdmissionReason.USER_BLOCKED and by[skipped.id].reason == "not for me"


def test_opening_closing_mid_flight_releases_and_cancels(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(title="Closing", fit_score=90, company="A")
    scheduler.run()
    co.opportunity.status = "CLOSED"
    db_session.commit()
    run = scheduler.run()
    assert run.released == 1
    db_session.refresh(co)
    assert co.state == OpportunityState.CLOSED.value
    assert scheduler.queue.find(co.opportunity_id, QueueAction.PREPARE).state == "CANCELLED"
    assert scheduler.capacity().day.used == 0
