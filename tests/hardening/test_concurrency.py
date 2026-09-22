"""Concurrency audit: real threads with their own sessions where SQLite's
single-writer lock allows it (WAL + busy timeout), and deterministic
interleavings for the races the guarded UPDATEs and unique constraints
must decide. Every test asserts the invariant, never the ordering:

    no duplicate submission, no duplicate logical signal, no duplicate
    outcome event, no double cap consumption, no cross-tenant record.
"""

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.application.database.models import ApplicationRow
from app.core.errors import ConflictError
from app.database import get_session_factory
from app.execution.executors import mock as m
from app.execution.models import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorKind,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)
from app.learning.engine import LearningEngine
from app.pipeline.database.models import ApplicationQueueRow
from app.pipeline.models import QueueAction, QueueState
from app.pipeline.queue import QueueRepository
from app.scheduler.caps import CapLedger, period_keys
from app.signals.database.models import OutcomeEventRow, SignalRow
from app.signals.models import AttributionHints, EmailMessage
from app.signals.service import SignalInboxService
from tests.hardening.conftest import begin, expire_lease


def _threads(fn, n: int) -> list[Exception]:
    errors: list[Exception] = []

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return errors


def _retrying(fn, attempts: int = 20):
    """SQLite may report 'database is locked' under real thread contention;
    a worker retries the same guarded statement, it never double-applies."""
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except OperationalError as exc:  # pragma: no cover - timing dependent
            last = exc
            continue
    raise last


# ------------------------------------------------------------------ queue


def test_parallel_workers_never_claim_the_same_item_twice(db_session, tenant_id, opportunities, scheduler):
    cos = [opportunities.make(company=f"Claim Co {i}", title=f"Role {i}") for i in range(8)]
    scheduler.run(window=500)
    db_session.commit()
    items = db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.action == QueueAction.PREPARE.value).all()
    assert len(items) == len(cos)
    factory = get_session_factory()
    claimed: dict[str, list[str]] = {}
    lock = threading.Lock()

    def worker(name: str):
        session = factory()
        try:
            repo = QueueRepository(session, tenant_id)

            def one():
                rows = repo.claim(name, action=QueueAction.PREPARE, limit=3)
                session.commit()
                return rows

            for _ in range(4):
                rows = _retrying(one)
                with lock:
                    for row in rows:
                        claimed.setdefault(row.id, []).append(name)
                if not rows:
                    break
        finally:
            session.close()

    errors = _threads(lambda: worker(f"w{threading.get_ident()}"), 4)
    assert errors == []
    assert all(len(owners) == 1 for owners in claimed.values()), "an item was claimed by two workers"
    assert len(claimed) == len(items)
    db_session.expire_all()
    states = {i.id: i.state for i in db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == tenant_id).all()}
    assert all(s == QueueState.CLAIMED.value for s in states.values())


def test_heartbeat_release_and_retry_are_owner_guarded(harness, db_session):
    attempt = harness.ready(company="Owner Co")
    item = harness.item(attempt)
    assert harness.service.claim_item(item, "a") is not None
    with pytest.raises(ConflictError):
        harness.service.queue.extend_lease(item, "b")
    with pytest.raises(ConflictError):
        harness.service.queue.release(item, "b")
    with pytest.raises(ConflictError):
        harness.service.queue.fail(item, "b", "nope")
    # the owner's lease lapses; a second worker reclaims; the first worker is refused from then on
    expire_lease(db_session, item)
    assert harness.service.claim_item(item, "b") is not None and item.claimed_by == "b"
    with pytest.raises(ConflictError):
        harness.service.queue.extend_lease(item, "a")
    # cancellation while claimed: the owner's later success is refused
    harness.service.queue.cancel(item, "human", "changed my mind")
    db_session.commit()
    with pytest.raises(ConflictError):
        harness.service.queue.succeed(item, "b", {})


# --------------------------------------------------------------- cap ledger


def test_parallel_reservations_never_exceed_the_cap(tenant_id):
    keys = period_keys(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), "UTC")
    factory = get_session_factory()
    wins: list[int] = []
    lock = threading.Lock()

    def worker():
        session = factory()
        try:
            ledger = CapLedger(session, tenant_id)
            for _ in range(15):

                def one():
                    ok = ledger.reserve(keys, daily_cap=20, weekly_cap=100) is None
                    session.commit()
                    return ok

                if _retrying(one):
                    with lock:
                        wins.append(1)
        finally:
            session.close()

    errors = _threads(worker, 4)
    assert errors == []
    assert len(wins) == 20, "exactly cap reservations succeed across concurrent sessions"
    session = factory()
    try:
        assert CapLedger(session, tenant_id).usage("DAY", keys.day.key) == 20
    finally:
        session.close()


# -------------------------------------------------------------- scheduler


def test_two_schedulers_admitting_the_same_opportunity_reserve_one_attempt(db_session, tenant_id, opportunities, scheduler):
    from app.scheduler.service import SchedulerService

    co = opportunities.make(company="Twin Sched Co")
    first = scheduler.run(window=500)
    second = SchedulerService(db_session, tenant_id, actor="test-2").run(window=500)
    assert first.admitted == 1 and second.admitted == 0 and second.already_queued == 1
    assert db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.opportunity_id == co.opportunity_id).count() == 1
    # the unique (tenant, opportunity) constraint is the last line of defence
    dup = ApplicationRow(job_id=co.opportunity.canonical_job_id + "x", tenant_id=tenant_id, opportunity_id=co.opportunity_id, candidate_opportunity_id=co.id, status="QUALIFIED")
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# -------------------------------------------------------------- execution


def test_two_executors_starting_the_same_item_one_wins(harness, db_session):
    attempt = harness.ready(company="Race Co")
    item = harness.item(attempt)
    from app.execution.service import ExecutionService

    other = ExecutionService(db_session, harness.tenant_id, actor="test-2", executors=harness.service.executors)
    claimed = harness.service.claim_item(item, "w1")
    assert claimed is not None
    # a second executor that somehow holds the same row object cannot start it
    with pytest.raises(ConflictError):
        other.start(item, "w2", ExecutorKind.MOCK)
    run, _, outcome = harness.service.start(claimed, "w1", ExecutorKind.MOCK)
    assert outcome["outcome"] == "started"
    # and the guarded READY -> SUBMITTING update refuses a second start for the same attempt
    harness.refresh(attempt)
    assert attempt.status == "SUBMITTING" and attempt.submission_key is not None


def test_duplicate_result_and_duplicate_verification_are_idempotent(harness, db_session):
    attempt = harness.ready(company="Dup Result Co")
    item, run, package = begin(harness, attempt)
    result = ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, confirmation_reference="DUP-1", application_url="mock://form/confirmation")
    first = harness.service.report_result(item, "w1", result)
    assert first["outcome"] == "SUBMITTED"
    with pytest.raises(ConflictError):
        harness.service.report_result(item, "w1", result)  # the run is no longer RUNNING
    before = db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == attempt.id).count()
    db_session.refresh(run)
    method = VerificationMethod(run.verification_method)
    for _ in range(3):
        harness.service.verify(run.id, VerificationResult(status=VerificationStatus.VERIFIED, method=method, confirmation_reference="DUP-1"))
    after = db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == attempt.id).count()
    signals = db_session.query(SignalRow).filter(SignalRow.application_id == attempt.id).all()
    assert after == before and len(signals) == 1 and signals[0].observation_count >= 3
    harness.refresh(attempt)
    assert attempt.status == "VERIFIED" and harness.mock.submit_calls[attempt.id] == 0, "an external result never triggers a mock submit"


# ----------------------------------------------------------------- signals


def test_parallel_deliveries_of_one_email_make_one_signal(db_session, tenant_id, opportunities):
    from tests.signals.conftest import submitted

    attempt = submitted(db_session, tenant_id, opportunities, company="Parallel Mail Co")
    factory = get_session_factory()
    message = EmailMessage(message_id="<race@x>", sender="hr@parallel.com", subject="Interview", text="We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id))

    def worker():
        session = factory()
        try:

            def one():
                SignalInboxService(session, tenant_id, actor="t").ingest_email(message)
                session.commit()

            _retrying(one)
        finally:
            session.close()

    errors = _threads(worker, 4)
    assert errors == []
    db_session.expire_all()
    rows = db_session.query(SignalRow).filter(SignalRow.tenant_id == tenant_id).all()
    from app.signals.database.models import SignalObservationRow

    assert len(rows) == 1 and rows[0].observation_count == 4
    assert db_session.query(SignalObservationRow).filter(SignalObservationRow.signal_id == rows[0].id).count() == 4
    assert db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == attempt.id).count() == 1


def test_concurrent_confirmations_record_one_human_event(db_session, tenant_id, opportunities):
    from tests.signals.conftest import email, submitted

    attempt = submitted(db_session, tenant_id, opportunities, company="Confirm Co")
    inbox = SignalInboxService(db_session, tenant_id, actor="t")
    row, _ = inbox.ingest_email(email("Hello", "Just checking in.", hints=AttributionHints(application_id=attempt.id)))
    from app.signals.models import OutcomeKind

    for _ in range(3):
        inbox.confirm_outcome(row.id, OutcomeKind.INTERVIEW_REQUESTED, "reviewer")
    events = db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == attempt.id, OutcomeEventRow.retracted.is_(False)).all()
    assert len(events) == 1 and events[0].origin == "human"


# ---------------------------------------------------------------- learning


def test_concurrent_snapshots_are_separate_and_immutable(db_session, tenant_id):
    from tests.learning.conftest import history, mixed_specs

    history(db_session, tenant_id, mixed_specs(30), spacing_hours=0.05)
    factory = get_session_factory()
    ids: list[str] = []
    # (the policy row is created lazily; parallel first-touch is covered by test_policy_first_touch_race)
    lock = threading.Lock()

    def worker():
        session = factory()
        try:

            def one():
                row = LearningEngine(session, tenant_id, actor="t").snapshot()
                session.commit()
                return row.id

            sid = _retrying(one)
            with lock:
                ids.append(sid)
        finally:
            session.close()

    errors = _threads(worker, 3)
    assert errors == [] and len(set(ids)) == 3
    engine = LearningEngine(db_session, tenant_id)
    metrics = [{(mrow.dimension, mrow.group_key, mrow.metric): (mrow.n, mrow.positives) for mrow in engine.metrics(sid)} for sid in ids]
    assert metrics[0] == metrics[1] == metrics[2], "the same history gives identical snapshots"
    assert all(s.dataset_size == 30 for s in engine.list_snapshots())


def test_mock_submit_counter_stays_one_under_retries(db_session, tenant_id, scheduler, opportunities, answered_bank):
    from tests.execution.conftest import scripted

    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN)
    attempt = h.ready(company="Once Co")
    h.execute(attempt)
    assert h.mock.submit_calls[attempt.id] == 1 and attempt.status == "UNCERTAIN"
    with pytest.raises(ConflictError):
        h.service.retry(attempt.id, "human")
    assert h.service.run_queue("w9", limit=10, executor_kind=ExecutorKind.MOCK).get("claimed", 0) == 0
    assert h.mock.submit_calls[attempt.id] == 1
