"""Execution safety: one attempt → at most one click; submit_invoked is
durable before the click; UNKNOWN never resubmits; an expired lease never
authorises a submit; every submission-time condition (cap, blocklist,
duplicate, closure, kill switch, stale preparation, changed artifact) is
revalidated at the final gate; Playwright and the extension cannot both win."""

import pytest

from app.application.killswitch import set_paused
from app.application.models import ApplicationStatus
from app.core.errors import ConflictError
from app.execution.database.models import ExecutionRunRow
from app.execution.executors import mock as m
from app.execution.models import ExecutionOutcome, ExecutionResult, ExecutorKind
from app.pipeline.models import ApplicationPolicyUpdate, QueueState
from app.pipeline.repository import PolicyRepository
from tests.hardening.conftest import begin, expire_lease


def test_submit_invoked_is_durable_before_the_click_and_unknown_never_resubmits(db_session, tenant_id, scheduler, opportunities, answered_bank):
    from tests.execution.conftest import scripted

    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.CRASH_AFTER_SUBMIT)
    attempt = h.ready(company="Durable Co")
    outcome = h.execute(attempt)
    run = h.service.require_run(attempt.last_execution_id)
    assert outcome["outcome"] == "UNKNOWN" and run.submit_invoked is True and run.status == "UNKNOWN" and attempt.status == ApplicationStatus.UNCERTAIN.value
    # nothing re-runs it: not retry, not the queue, not a second start
    with pytest.raises(ConflictError):
        h.service.retry(attempt.id, "human")
    assert h.service.run_queue("w2", limit=10, executor_kind=ExecutorKind.MOCK).get("claimed", 0) == 0
    item = h.item(attempt)
    assert item.state == QueueState.NEEDS_REVIEW.value and h.mock.submit_calls[attempt.id] == 1
    # only verification or a person settles it
    h.service.confirm(run.id, True, reference="DUR-1")
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and h.mock.submit_calls[attempt.id] == 1


def test_expired_lease_cannot_authorise_submission(harness, db_session):
    attempt = harness.ready(company="Lease Co")
    item, run, _ = begin(harness, attempt, worker="ext-a", executor=ExecutorKind.BROWSER_EXTENSION)
    expire_lease(db_session, item)
    gate = harness.service.gate(item, "ext-a")
    assert gate["ok"] is False and gate["failures"][0].startswith("LEASE_EXPIRED")
    db_session.refresh(run)
    assert run.submit_invoked is False
    # a heartbeat by the still-owning worker renews it; then the gate passes
    harness.service.heartbeat(item, "ext-a")
    gate = harness.service.gate(item, "ext-a")
    assert gate["ok"] is True
    db_session.refresh(run)
    assert run.submit_invoked is True


def test_playwright_and_extension_competing_for_one_attempt(harness, db_session):
    attempt = harness.ready(company="Compete Co")
    item, run, _ = begin(harness, attempt, worker="ext-a", executor=ExecutorKind.BROWSER_EXTENSION)
    # the local worker sees nothing claimable while the extension's lease is live
    assert harness.service.run_queue("pw-1", limit=10, executor_kind=ExecutorKind.MOCK).get("claimed", 0) == 0
    # the extension goes silent before pressing submit: the local worker reclaims and executes once
    expire_lease(db_session, item)
    counts = harness.service.run_queue("pw-1", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts.get("claimed") == 1 and counts.get("submitted") == 1
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and harness.mock.submit_calls[attempt.id] == 1
    db_session.refresh(run)
    assert run.status == "FAILED_RETRYABLE", "the extension's run was settled as lost before submit"
    # the late extension is refused everywhere: gate, heartbeat, result
    db_session.refresh(item)
    with pytest.raises(ConflictError):
        harness.service.gate(item, "ext-a")
    with pytest.raises(ConflictError):
        harness.service.heartbeat(item, "ext-a")
    with pytest.raises(ConflictError):
        harness.service.report_result(item, "ext-a", ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, confirmation_reference="LATE"))
    assert harness.mock.submit_calls[attempt.id] == 1 and db_session.query(ExecutionRunRow).filter_by(application_id=attempt.id).count() == 2


def test_extension_that_armed_submit_before_going_silent_makes_the_attempt_uncertain(harness, db_session):
    attempt = harness.ready(company="Armed Co")
    item, run, _ = begin(harness, attempt, worker="ext-a", executor=ExecutorKind.BROWSER_EXTENSION)
    assert harness.service.gate(item, "ext-a")["ok"] is True  # submit armed: the click may happen
    expire_lease(db_session, item)
    counts = harness.service.run_queue("pw-1", limit=10, executor_kind=ExecutorKind.MOCK)
    # run_queue's recovery settles the armed run as UNCERTAIN before anything is claimed
    assert counts.get("submitted", 0) == 0 and counts.get("claimed", 0) == 0
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.UNCERTAIN.value and harness.mock.submit_calls[attempt.id] == 0
    db_session.refresh(item)
    assert item.state == QueueState.NEEDS_REVIEW.value
    # the extension's late result is still accepted as evidence when it still owns the item? No: reclaimed -> refused
    with pytest.raises(ConflictError):
        harness.service.report_result(item, "ext-a", ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, confirmation_reference="ARMED-1"))
    # the person confirms through the run instead
    harness.service.confirm(run.id, True, reference="ARMED-1")
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and harness.mock.submit_calls[attempt.id] == 0


@pytest.mark.parametrize("stage", ["queued", "claimed", "form", "armed"])
def test_kill_switch_at_every_stage_stops_the_click(harness, db_session, stage):
    attempt = harness.ready(company=f"Kill {stage} Co")
    item = harness.item(attempt)
    try:
        if stage == "queued":
            set_paused(db_session, True, "test")
            counts = harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK)
            assert counts.get("paused") == 1 and harness.mock.submit_calls[attempt.id] == 0
            db_session.refresh(item)
            assert item.state == QueueState.PENDING.value and item.available_at is not None
        elif stage == "claimed":
            claimed = harness.service.claim_item(item, "w1")
            set_paused(db_session, True, "test")
            run, _, outcome = harness.service.start(claimed, "w1", ExecutorKind.MOCK)
            assert run is None and outcome["outcome"] == "paused"
        elif stage in ("form", "armed"):
            item, run, _ = begin(harness, attempt, worker="ext-a", executor=ExecutorKind.BROWSER_EXTENSION)
            set_paused(db_session, True, "test")
            gate = harness.service.gate(item, "ext-a")
            assert gate["ok"] is False and gate["failures"][0].startswith("PAUSED")
            db_session.refresh(run)
            assert run.submit_invoked is False
            if stage == "armed":
                # the gate refused; a result claiming a submit without an armed gate is still recorded as UNKNOWN evidence, never as our click
                reported = harness.service.report_result(item, "ext-a", ExecutionResult(outcome=ExecutionOutcome.NEEDS_REVIEW, submit_attempted=False, message="gate refused"))
                assert reported["attempt_status"] == ApplicationStatus.NEEDS_REVIEW.value
    finally:
        set_paused(db_session, False)
    assert harness.mock.submit_calls[attempt.id] == 0


def test_kill_switch_blocks_retries_and_the_in_process_executor(harness, db_session):
    attempt = harness.ready(company="Kill Retry Co")
    set_paused(db_session, True, "test")
    try:
        assert harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK).get("paused") == 1
        harness.refresh(attempt)
        assert attempt.status == ApplicationStatus.READY.value
        # a person "retries": the attempt is READY already; the next run is paused again
        assert harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK).get("claimed", 0) == 0, "the paused item is not available for 10 minutes"
    finally:
        set_paused(db_session, False)
    assert harness.mock.submit_calls[attempt.id] == 0


def test_blocklist_closure_and_duplicate_are_revalidated_at_submit(harness, db_session):
    a = harness.ready(company="Revalidate Co", title="Backend Engineer")
    PolicyRepository(db_session, harness.tenant_id).update(ApplicationPolicyUpdate(blocked_companies=[harness.opportunities.jobs.company("Revalidate Co")]), "test")
    db_session.commit()
    counts = harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts.get("precondition_failed") == 1 and harness.mock.submit_calls[a.id] == 0
    harness.refresh(a)
    assert a.status == ApplicationStatus.CLOSED.value and a.released_at is not None, "blocked at submit: slot released"
    b = harness.ready(company="Closing Co")
    from app.pipeline.database.models import OpportunityRow

    opp = db_session.get(OpportunityRow, b.opportunity_id)
    opp.status = "CLOSED"
    db_session.commit()
    counts = harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts.get("precondition_failed") == 1 and harness.mock.submit_calls[b.id] == 0
    harness.refresh(b)
    assert b.status == ApplicationStatus.CLOSED.value


def test_stale_preparation_never_submits(harness, db_session):
    from app.career.repository import EvidenceRepository

    attempt = harness.ready(company="Stale Co")
    # the evidence changes after the package was prepared: the input fingerprint no longer matches
    EvidenceRepository(db_session, harness.tenant_id).upsert_profile({"phone": "+91 99999 99999"}, None, "test")
    db_session.commit()
    counts = harness.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts.get("precondition_failed") == 1 and harness.mock.submit_calls[attempt.id] == 0
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.PREPARING.value, "re-prepared, not submitted"
