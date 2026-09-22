"""Application queue: idempotency, claiming, leases, retries, outcomes, isolation."""

from datetime import timedelta

import pytest

from app.core.errors import ConflictError
from app.core.timeutils import db_now
from app.pipeline.models import Lane, QueueAction, QueueState
from app.pipeline.queue import QueueRepository, idempotency_key
from app.pipeline.repository import OpportunityRepository


def _co(db_session, jobs, repo, priority=50, **job_kwargs):
    job = jobs.make(**job_kwargs)
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
    co, _ = repo.ensure_candidate_opportunity(opp, "test")
    co.priority_score = priority
    db_session.commit()
    return co


def test_enqueue_is_idempotent_per_tenant_opportunity_action(db_session, jobs, repo, queue):
    co = _co(db_session, jobs, repo)
    item, created = queue.enqueue(co, QueueAction.SUBMIT, "api", lane=Lane.REVIEW)
    again, created_again = queue.enqueue(co, QueueAction.SUBMIT, "api")
    queue.commit()
    assert created and not created_again and item.id == again.id
    assert item.idempotency_key == idempotency_key(repo.tenant_id, co.opportunity_id, QueueAction.SUBMIT)
    assert item.priority == 50 and item.state == "PENDING" and item.lane == "REVIEW"
    prepare, _ = queue.enqueue(co, QueueAction.PREPARE, "api")
    assert prepare.id != item.id
    assert [e.action for e in repo.list_audit("queue_item", item.id)] == ["enqueued"]


def test_same_opportunity_via_two_source_rows_cannot_be_submitted_twice(db_session, jobs, repo, queue):
    a = jobs.make(source="GREENHOUSE", location="Remote")
    b = jobs.make(source="LEVER", location="Remote - US", remote_type="UNKNOWN")
    opp_a, _, _ = OpportunityRepository.resolve_opportunity(db_session, a)
    opp_b, _, _ = OpportunityRepository.resolve_opportunity(db_session, b)
    assert opp_a.id == opp_b.id
    co, _ = repo.ensure_candidate_opportunity(opp_b, "test")
    db_session.commit()

    item, _ = queue.enqueue(co, QueueAction.SUBMIT, "api")
    [claimed] = queue.claim("w1")
    queue.succeed(claimed, "w1", {"confirmation": "ABC123"})
    queue.commit()
    with pytest.raises(ConflictError) as exc:
        queue.enqueue(co, QueueAction.SUBMIT, "api")
    assert exc.value.details["state"] == "SUCCEEDED"


def test_claim_orders_by_priority_and_leases_exclusively(db_session, jobs, repo, queue):
    low = _co(db_session, jobs, repo, priority=10, title="A")
    high = _co(db_session, jobs, repo, priority=90, title="B")
    mid = _co(db_session, jobs, repo, priority=50, title="C")
    for co in (low, high, mid):
        queue.enqueue(co, QueueAction.PREPARE, "api")
    queue.commit()

    first = queue.claim("worker-1", limit=2, lease_seconds=300)
    queue.commit()
    assert [i.candidate_opportunity_id for i in first] == [high.id, mid.id]
    assert all(i.state == "CLAIMED" and i.claimed_by == "worker-1" and i.attempts == 1 for i in first)
    assert all(i.lease_expires_at > db_now() for i in first)

    second = queue.claim("worker-2", limit=5)
    queue.commit()
    assert [i.candidate_opportunity_id for i in second] == [low.id]
    assert queue.claim("worker-3") == []
    assert queue.counts_by_state()["CLAIMED"] == 3


def test_start_succeed_and_owner_check(db_session, jobs, repo, queue):
    co = _co(db_session, jobs, repo)
    queue.enqueue(co, QueueAction.PREPARE, "api")
    [item] = queue.claim("w1")
    with pytest.raises(ConflictError):
        queue.start(item, "intruder")
    queue.start(item, "w1")
    assert item.state == "PROCESSING"
    queue.succeed(item, "w1", {"artifact_ids": ["x"]})
    queue.commit()
    assert item.state == "SUCCEEDED" and item.completed_at is not None and item.result == {"artifact_ids": ["x"]}
    assert [e.action for e in repo.list_audit("queue_item", item.id)] == ["succeeded", "processing", "claimed", "enqueued"]


def test_fail_retries_with_backoff_then_fails_permanently(db_session, jobs, repo, queue):
    co = _co(db_session, jobs, repo)
    queue.enqueue(co, QueueAction.SUBMIT, "api", max_attempts=2)
    [item] = queue.claim("w1")
    queue.fail(item, "w1", "timeout")
    queue.commit()
    assert item.state == "RETRY_WAIT" and item.attempts == 1 and item.claimed_by is None
    assert item.available_at > db_now()
    assert queue.claim("w1") == [], "not runnable until the backoff elapses"

    item.available_at = db_now() - timedelta(seconds=1)
    db_session.commit()
    [again] = queue.claim("w1")
    assert again.id == item.id and again.attempts == 2
    queue.fail(again, "w1", "timeout again")
    queue.commit()
    assert again.state == "FAILED" and again.last_error == "timeout again"

    with pytest.raises(ConflictError):
        queue.enqueue(co, QueueAction.SUBMIT, "api")
    queue.requeue(again, "dashboard", "retry manually")
    assert again.state == "PENDING" and again.attempts == 0 and again.last_error is None


def test_non_retryable_failure_block_review_cancel(db_session, jobs, repo, queue):
    a = _co(db_session, jobs, repo, title="A")
    b = _co(db_session, jobs, repo, title="B")
    c = _co(db_session, jobs, repo, title="C")
    for co in (a, b, c):
        queue.enqueue(co, QueueAction.SUBMIT, "api")
    items = {i.candidate_opportunity_id: i for i in queue.claim("w1", limit=3)}

    queue.fail(items[a.id], "w1", "form removed", retryable=False)
    assert items[a.id].state == "FAILED"
    queue.block(items[b.id], "w1", "captcha detected")
    assert items[b.id].state == "BLOCKED" and items[b.id].last_error == "captcha detected"
    queue.needs_review(items[c.id], "w1", "novel essay question")
    assert items[c.id].state == "NEEDS_REVIEW" and items[c.id].claimed_by is None

    queue.cancel(items[b.id], "dashboard", "not applying")
    assert items[b.id].state == "CANCELLED"
    queue.cancel(items[b.id], "dashboard")  # cancelling again is harmless
    assert items[b.id].state == "CANCELLED"
    queue.commit()
    assert [e.action for e in repo.list_audit("queue_item", items[b.id].id)][:2] == ["cancelled", "cancelled"]


def test_cancel_refuses_succeeded_only(db_session, jobs, repo, queue):
    co = _co(db_session, jobs, repo)
    queue.enqueue(co, QueueAction.PREPARE, "api")
    [item] = queue.claim("w1")
    queue.succeed(item, "w1")
    with pytest.raises(ConflictError):
        queue.cancel(item, "api")
    with pytest.raises(ConflictError):
        queue.requeue(item, "api")


def test_release_and_expired_lease_reclaim(db_session, jobs, repo, queue):
    co = _co(db_session, jobs, repo)
    queue.enqueue(co, QueueAction.PREPARE, "api")
    [item] = queue.claim("w1")
    queue.release(item, "w1", "shutting down")
    assert item.state == "PENDING" and item.attempts == 0 and item.claimed_by is None

    [item] = queue.claim("w1", lease_seconds=30)
    item.lease_expires_at = db_now() - timedelta(seconds=5)
    db_session.commit()
    [reclaimed] = queue.claim("w2")
    assert reclaimed.id == item.id and reclaimed.claimed_by == "w2" and reclaimed.attempts == 2
    assert repo.list_audit("queue_item", item.id)[0].action == "reclaimed"

    reclaimed.lease_expires_at = db_now() - timedelta(seconds=5)
    db_session.commit()
    assert queue.reclaim_expired() == 1
    assert reclaimed.state == "PENDING" and reclaimed.claimed_by is None


def test_queue_is_tenant_scoped(db_session, jobs, repo, queue, other_tenant_id):
    co = _co(db_session, jobs, repo)
    item, _ = queue.enqueue(co, QueueAction.PREPARE, "api")
    queue.commit()
    other = QueueRepository(db_session, other_tenant_id)
    assert other.get(item.id) is None
    assert other.claim("w") == []
    assert other.counts_by_state()["PENDING"] == 0
    with pytest.raises(Exception):
        other.enqueue(co, QueueAction.PREPARE, "api")  # foreign candidate opportunity
    listed, total = queue.list_items(state=QueueState.PENDING)
    assert total == 1 and listed[0].id == item.id


def test_claim_takes_eligible_work_before_uncertain_work_with_a_higher_stored_priority(db_session, jobs, repo, queue):
    # First real dry run: Zeta "Cloud Network Engineer II" (UNCERTAIN, stored priority 70)
    # would have been claimed before Notion / Atlan (ELIGIBLE, priority 43 / 40).
    uncertain = _co(db_session, jobs, repo, priority=70, title="Cloud Network Engineer II")
    eligible_low = _co(db_session, jobs, repo, priority=40, title="Field Security Engineer")
    eligible_high = _co(db_session, jobs, repo, priority=43, title="Software Engineer, Developer Experience")
    uncertain.eligibility_status = "UNCERTAIN"
    eligible_low.eligibility_status = eligible_high.eligibility_status = "ELIGIBLE"
    db_session.commit()
    for co in (uncertain, eligible_low, eligible_high):
        queue.enqueue(co, QueueAction.PREPARE, "api")
    queue.commit()

    listed, _ = queue.list_items(action=QueueAction.PREPARE)
    assert [i.candidate_opportunity_id for i in listed][:3] == [eligible_high.id, eligible_low.id, uncertain.id]
    claimed = queue.claim("worker-1", limit=3)
    assert [i.candidate_opportunity_id for i in claimed] == [eligible_high.id, eligible_low.id, uncertain.id]
    assert queue.claim("worker-2") == []
