"""Audit regression (2026-09-14): an item whose worker is lost on every attempt is parked, not reclaimed forever.

``max_attempts`` was honoured only by ``fail``; an expired lease was reclaimed
with ``attempts + 1`` without limit.
"""

from datetime import timedelta

from app.core.timeutils import db_now
from app.pipeline.models import QueueAction, QueueState
from app.pipeline.repository import OpportunityRepository


def _item(db_session, jobs, repo, queue, max_attempts=2):
    # a distinct title per item: identical postings resolve to one opportunity (one queue item)
    job = jobs.make(title=f"Backend Engineer {len(jobs.job_ids)}")
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
    co, _ = repo.ensure_candidate_opportunity(opp, "test")
    item, _ = queue.enqueue(co, QueueAction.PREPARE, "test", max_attempts=max_attempts)
    queue.commit()
    return item


def _expire(db_session, item):
    item.lease_expires_at = db_now() - timedelta(seconds=5)
    db_session.flush()


def test_expired_lease_on_the_last_attempt_is_parked_for_review(db_session, jobs, repo, queue):
    item = _item(db_session, jobs, repo, queue, max_attempts=2)
    [first] = queue.claim("w1")
    _expire(db_session, first)
    [second] = queue.claim("w2")
    assert second.id == item.id and second.attempts == 2
    _expire(db_session, second)

    assert queue.claim("w3") == []
    db_session.refresh(item)
    assert item.state == QueueState.NEEDS_REVIEW.value
    assert item.claimed_by is None and item.lease_expires_at is None and item.attempts == 2
    assert "lease expired" in item.last_error
    assert repo.list_audit("queue_item", item.id)[0].action == "attempts_exhausted"

    # a person can still put it back
    queue.requeue(item, "person", "looked at it")
    assert item.state == QueueState.PENDING.value and item.attempts == 0


def test_parking_does_not_starve_other_runnable_items(db_session, jobs, repo, queue):
    stuck = _item(db_session, jobs, repo, queue, max_attempts=1)
    [claimed] = queue.claim("w1")
    assert claimed.id == stuck.id
    _expire(db_session, claimed)
    fresh = _item(db_session, jobs, repo, queue)
    [next_item] = queue.claim("w2", limit=1)
    assert next_item.id == fresh.id
    db_session.refresh(stuck)
    assert stuck.state == QueueState.NEEDS_REVIEW.value


def test_reclaim_expired_and_claim_item_respect_max_attempts(db_session, jobs, repo, queue):
    item = _item(db_session, jobs, repo, queue, max_attempts=1)
    [claimed] = queue.claim("w1")
    _expire(db_session, claimed)
    assert queue.claim_item(claimed, "w2") is None
    assert claimed.state == QueueState.NEEDS_REVIEW.value

    other = _item(db_session, jobs, repo, queue, max_attempts=1)
    [held] = queue.claim("w1")
    assert held.id == other.id
    _expire(db_session, held)
    assert queue.reclaim_expired("startup") == 1
    assert held.state == QueueState.NEEDS_REVIEW.value
    assert item.state == QueueState.NEEDS_REVIEW.value


def test_live_lease_and_remaining_attempts_are_unchanged(db_session, jobs, repo, queue):
    item = _item(db_session, jobs, repo, queue, max_attempts=3)
    [claimed] = queue.claim("w1")
    assert queue.claim("w2") == []  # live lease: nobody else gets it, nothing parked
    assert claimed.state == QueueState.CLAIMED.value
    _expire(db_session, claimed)
    [again] = queue.claim("w2")
    assert again.id == item.id and again.attempts == 2 and again.state == QueueState.CLAIMED.value
