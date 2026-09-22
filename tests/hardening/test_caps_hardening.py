"""Cap audit: cap 0 / 1 / boundary, lowering after reservations, raising,
negative capacity impossible, timezone rollover, released slots, closed
openings. The aggressive defaults stay; nothing here lowers volume."""

from datetime import datetime, timezone

from app.application.models import ApplicationStatus
from app.pipeline.models import AdmissionReason, ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.scheduler.caps import CapLedger, period_keys
from app.scheduler.database.models import ApplicationCapLedgerRow


def _set(db, tenant_id, **changes):
    PolicyRepository(db, tenant_id).update(ApplicationPolicyUpdate(**changes), "test")
    db.commit()


def test_cap_zero_one_and_boundary(db_session, tenant_id, scheduler, opportunities):
    for i in range(3):
        opportunities.make(company=f"Bound Co {i}", title=f"Role {i}")
    _set(db_session, tenant_id, daily_cap=0)
    run = scheduler.run(window=500)
    assert run.admitted == 0 and run.blocked_by_reason.get(AdmissionReason.DAILY_CAP_REACHED.value) == 3
    _set(db_session, tenant_id, daily_cap=1)
    run = scheduler.run(window=500)
    assert run.admitted == 1 and run.blocked_by_reason.get(AdmissionReason.DAILY_CAP_REACHED.value) == 2
    _set(db_session, tenant_id, daily_cap=3)
    run = scheduler.run(window=500)
    assert run.admitted == 2, "raising the cap admits the rest; the earlier reservation is not double counted"
    keys = period_keys(datetime.now(timezone.utc), "UTC")
    assert CapLedger(db_session, tenant_id).usage("DAY", keys.day.key) == 3


def test_lowering_the_cap_keeps_existing_reservations_and_never_goes_negative(db_session, tenant_id, scheduler, opportunities):
    for i in range(4):
        opportunities.make(company=f"Lower Co {i}", title=f"Role {i}")
    _set(db_session, tenant_id, daily_cap=3)
    assert scheduler.run(window=500).admitted == 3
    _set(db_session, tenant_id, daily_cap=1)
    run = scheduler.run(window=500)
    assert run.admitted == 0 and run.released == 0, "existing reservations are kept; nothing is pulled back"
    keys = period_keys(datetime.now(timezone.utc), "UTC")
    ledger = CapLedger(db_session, tenant_id)
    assert ledger.usage("DAY", keys.day.key) == 3
    # releasing more than reserved (a bug elsewhere) can never produce negative capacity
    for _ in range(10):
        ledger.release(keys.day.key, keys.week.key)
    db_session.commit()
    row = db_session.query(ApplicationCapLedgerRow).filter_by(tenant_id=tenant_id, period_kind="DAY", period_key=keys.day.key).one()
    assert ledger.usage("DAY", keys.day.key) == 0 and row.reserved == 3 and row.released == 10
    assert ledger.reserve(keys, daily_cap=1, weekly_cap=1) is None, "a free period accepts a reservation again"


def test_released_slots_are_reusable_and_closed_openings_release(db_session, tenant_id, scheduler, opportunities):
    a = opportunities.make(company="Reuse A")
    b = opportunities.make(company="Reuse B")
    _set(db_session, tenant_id, daily_cap=1)
    run = scheduler.run(window=500)
    assert run.admitted == 1
    from app.pipeline.database.models import OpportunityRow

    admitted = next(co for co in (a, b) if scheduler.attempts.get_for_opportunity(co.opportunity_id) is not None)
    db_session.get(OpportunityRow, admitted.opportunity_id).status = "CLOSED"
    db_session.commit()
    run = scheduler.run(window=500)
    assert run.released == 1 and run.admitted == 1, "the closed opening's slot is given back and the other opening takes it"
    keys = period_keys(datetime.now(timezone.utc), "UTC")
    assert CapLedger(db_session, tenant_id).usage("DAY", keys.day.key) == 1


def test_timezone_rollover_opens_a_new_period(db_session, tenant_id):
    keys_utc = period_keys(datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc), "UTC")
    keys_kolkata = period_keys(datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc), "Asia/Kolkata")
    assert keys_utc.day.key == "2026-06-01" and keys_kolkata.day.key == "2026-06-02", "the tenant's zone decides the day"
    ledger = CapLedger(db_session, tenant_id)
    assert ledger.reserve(keys_utc, 1, 10) is None and ledger.reserve(keys_utc, 1, 10) is AdmissionReason.DAILY_CAP_REACHED
    assert ledger.reserve(keys_kolkata, 1, 10) is None, "a new local day is a new period; yesterday's usage does not carry over"
    monday = period_keys(datetime(2026, 6, 8, 0, 30, tzinfo=timezone.utc), "UTC")
    assert monday.week.key != keys_utc.week.key and ledger.reserve(monday, 1, 1) is None
    db_session.commit()


def test_permanent_failure_and_cancellation_release_but_retry_handoff_unknown_keep(db_session, tenant_id, scheduler, opportunities, answered_bank):
    from app.execution.executors import mock as m
    from tests.execution.conftest import scripted

    h = scripted(db_session, tenant_id, scheduler, opportunities, script={}, default=m.SUCCESS)
    a = h.ready(company="Keep A")
    b = h.ready(company="Keep B")
    c = h.ready(company="Keep C")
    d = h.ready(company="Keep D")
    h.mock.script.update({a.id: m.PERMANENT, b.id: m.RETRYABLE, c.id: m.CAPTCHA, d.id: m.UNKNOWN})
    h.service.run_queue("w1", limit=10, executor_kind=h.mock.kind)
    for attempt in (a, b, c, d):
        h.refresh(attempt)
    assert a.status == ApplicationStatus.FAILED.value and a.released_at is not None
    assert b.status == ApplicationStatus.READY.value and b.released_at is None
    assert c.status == ApplicationStatus.BLOCKED.value and c.released_at is None
    assert d.status == ApplicationStatus.UNCERTAIN.value and d.released_at is None
    h.service.cancel(c.id, "human")
    h.refresh(c)
    assert c.released_at is not None
    keys = period_keys(datetime.now(timezone.utc), "UTC")
    assert CapLedger(db_session, tenant_id).usage("DAY", keys.day.key) == 2, "b (retryable) and d (unknown) keep their slots"
