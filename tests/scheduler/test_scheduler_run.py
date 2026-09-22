"""End-to-end scheduler runs: admission, enqueueing, records, idempotency."""

from app.application.models import ApplicationStatus
from app.pipeline.models import AdmissionReason, OpportunityState, QueueAction, QueueState
from app.scheduler.database.models import SchedulerRunRow
from tests.scheduler.conftest import decisions_by_co, set_policy


def test_run_admits_and_enqueues_prepare_in_priority_order(db_session, tenant_id, scheduler, opportunities):
    # Distinct companies: the default 90-day cool-down admits one opening per company.
    high = opportunities.make(title="Platform Engineer", fit_score=90, company="Alpha")
    mid = opportunities.make(title="Backend Engineer", fit_score=60, company="Beta")
    low = opportunities.make(title="Support Engineer", fit_score=20, company="Gamma")
    for co, prio in ((high, 90), (mid, 60), (low, 20)):
        co.priority_score = prio
    db_session.commit()

    run = scheduler.run(trigger="test")
    assert run.status == "COMPLETED"
    assert run.considered == 3 and run.admitted == 3 and run.enqueued == 3 and run.blocked == 0
    assert run.policy_version >= 1 and run.duration_seconds is not None

    items, total = scheduler.queue.list_items(action=QueueAction.PREPARE)
    assert total == 3
    assert [i.candidate_opportunity_id for i in items] == [high.id, mid.id, low.id]
    assert all(i.state == QueueState.PENDING.value for i in items)
    for co in (high, mid, low):
        db_session.refresh(co)
        assert co.state == OpportunityState.QUEUED.value
        assert co.scheduler_code == AdmissionReason.ADMITTED.value
        assert co.scheduler_run_id == run.id
        attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
        assert attempt is not None and attempt.status == ApplicationStatus.QUALIFIED.value
        assert attempt.reserved_at is not None and attempt.released_at is None
        assert attempt.cap_day and attempt.cap_week
        assert co.application_id == attempt.id
    # Tailoring level and lane follow the policy for the band.
    assert scheduler.attempts.get_for_opportunity(high.opportunity_id).tailoring_level == "L2"
    assert scheduler.attempts.get_for_opportunity(low.opportunity_id).tailoring_level == "L0"


def test_low_fit_low_priority_eligible_is_admissible(db_session, tenant_id, scheduler, opportunities):
    """Regression: LOW band enabled, ELIGIBLE, capacity available, nothing else => admitted."""
    co = opportunities.make(title="Junior Engineer", fit_score=5)
    co.priority_score = 1
    db_session.commit()
    preview = scheduler.preview()
    decision = decisions_by_co(preview)[co.id]
    assert decision.code is AdmissionReason.ADMITTED, decision.reason
    assert decision.fit_band == "LOW"
    run = scheduler.run()
    assert run.admitted == 1


def test_never_creates_submit_items(db_session, scheduler, opportunities):
    opportunities.make(fit_score=90)
    scheduler.run()
    _, submits = scheduler.queue.list_items(action=QueueAction.SUBMIT)
    assert submits == 0


def test_repeated_runs_are_idempotent(db_session, scheduler, opportunities):
    a = opportunities.make(title="A", fit_score=80, company="Alpha")
    b = opportunities.make(title="B", fit_score=70, company="Beta")
    first = scheduler.run()
    second = scheduler.run()
    third = scheduler.run()
    assert first.admitted == 2 and first.enqueued == 2
    assert second.admitted == 0 and second.enqueued == 0 and second.already_queued == 2
    assert third.already_queued == 2
    _, total = scheduler.queue.list_items(action=QueueAction.PREPARE)
    assert total == 2
    assert scheduler.attempts.get_for_opportunity(a.opportunity_id).attempt_number == 1
    assert scheduler.attempts.get_for_opportunity(b.opportunity_id).attempt_number == 1
    # The cap ledger counted each attempt exactly once.
    capacity = scheduler.capacity()
    assert capacity.day.used == 2 and capacity.week.used == 2


def test_preview_writes_nothing(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(fit_score=80)
    preview = scheduler.preview()
    assert preview.would_admit == 1 and preview.decisions[0].admitted
    db_session.expire_all()
    assert scheduler.attempts.get_for_opportunity(co.opportunity_id) is None
    _, total = scheduler.queue.list_items()
    assert total == 0
    assert db_session.query(SchedulerRunRow).filter_by(tenant_id=tenant_id).count() == 0
    db_session.refresh(co)
    assert co.state == OpportunityState.ELIGIBLE.value and co.scheduler_code is None
    assert scheduler.capacity().day.used == 0


def test_run_record_counts_and_samples(db_session, tenant_id, scheduler, opportunities):
    opportunities.make(title="Good", fit_score=85, company="Alpha")
    opportunities.make(title="Bad", fit_score=85, eligibility="INELIGIBLE", company="Beta")
    opportunities.make(title="Low", fit_score=10, company="Gamma")
    set_policy(db_session, tenant_id, enabled_bands=["HIGH", "MEDIUM"])
    run = scheduler.run(trigger="unit")
    assert run.trigger == "unit"
    assert run.considered == 3 and run.admitted == 1 and run.blocked == 2
    assert run.blocked_by_reason == {"INELIGIBLE": 1, "BAND_DISABLED": 1}
    assert set(run.samples) == {"ADMITTED", "INELIGIBLE", "BAND_DISABLED"}
    assert run.samples["BAND_DISABLED"][0]["title"] == "Low"
    assert run.policy_snapshot["enabled_bands"] == ["HIGH", "MEDIUM"]
    rows, total = scheduler.history()
    assert total == 1 and rows[0].id == run.id
    assert scheduler.get_run(run.id) is not None


def test_static_refusals_refresh_policy_admission(db_session, tenant_id, scheduler, opportunities):
    co = opportunities.make(title="Low", fit_score=10, admitted=True)
    set_policy(db_session, tenant_id, enabled_bands=["HIGH"])
    scheduler.run()
    db_session.refresh(co)
    assert co.policy_admitted is False and co.policy_reason.startswith("band_disabled")
    assert co.scheduler_code == "BAND_DISABLED"
    # Enabling the band later admits it: the policy decides, never the earlier verdict.
    set_policy(db_session, tenant_id, enabled_bands=["HIGH", "MEDIUM", "LOW"])
    run = scheduler.run()
    assert run.admitted == 1
    db_session.refresh(co)
    assert co.policy_admitted is True
