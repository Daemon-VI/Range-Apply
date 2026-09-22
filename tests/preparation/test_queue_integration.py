"""PREPARE queue items settle into the Phase 2 queue states without submitting anything."""

from app.application.database.models import ApplicationRow
from app.core.errors import ExternalServiceError
from app.pipeline.models import QueueAction
from app.pipeline.queue import QueueRepository
from app.preparation.models import QueueOutcome
from app.preparation.queue_worker import process_prepare_item, run_prepare_queue
from app.preparation.service import PreparationService


def test_prepare_item_ready_for_execution(db_session, tenant_id, service, opportunities):
    co = opportunities.make(fit_score=60)
    queue = QueueRepository(db_session, tenant_id)
    queue.enqueue(co, QueueAction.PREPARE, "test")
    db_session.commit()
    counts = run_prepare_queue(db_session, tenant_id, "worker-1", limit=5)
    assert counts["claimed"] == 1 and counts[QueueOutcome.READY_FOR_EXECUTION.value] == 1
    item = queue.find(co.opportunity_id, QueueAction.PREPARE)
    assert item.state == "SUCCEEDED" and item.result["outcome"] == "READY_FOR_EXECUTION"
    assert item.result["preparation_id"] == service.latest(co.id).id
    assert db_session.query(ApplicationRow).filter_by(opportunity_id=co.opportunity_id).count() == 0
    assert queue.find(co.opportunity_id, QueueAction.SUBMIT) is None, "preparation never enqueues a submission"
    assert run_prepare_queue(db_session, tenant_id, "worker-1")["claimed"] == 0


def test_needs_user_input_blocks_item_and_needs_review_parks_it(db_session, tenant_id, evidence, opportunities):
    queue = QueueRepository(db_session, tenant_id)
    co = opportunities.make(fit_score=60)  # no answer bank -> facts missing
    queue.enqueue(co, QueueAction.PREPARE, "test")
    db_session.commit()
    [item] = queue.claim("w", action=QueueAction.PREPARE)
    outcome = process_prepare_item(db_session, tenant_id, item, "w")
    assert outcome is QueueOutcome.NEEDS_USER_INPUT
    assert item.state == "BLOCKED" and item.result["outcome"] == "NEEDS_USER_INPUT" and "needs_user_input" in item.last_error

    review = opportunities.make(title="Data Engineer", fit_score=60)
    queue.enqueue(review, QueueAction.PREPARE, "test")
    db_session.commit()
    [item2] = queue.claim("w", action=QueueAction.PREPARE)
    service = PreparationService(db_session, tenant_id)
    original = service.prepare

    def with_odd_question(co_id, **kwargs):
        return original(co_id, questions=["What is your favourite colour?", "Why are you interested in this role?"], **kwargs)

    service.prepare = with_odd_question  # type: ignore[method-assign]
    outcome2 = process_prepare_item(db_session, tenant_id, item2, "w", service)
    assert outcome2 is QueueOutcome.NEEDS_REVIEW and item2.state == "NEEDS_REVIEW"


def test_failure_uses_queue_retry_semantics_and_ownership(db_session, tenant_id, service, opportunities):
    queue = QueueRepository(db_session, tenant_id)
    co = opportunities.make(fit_score=60)
    queue.enqueue(co, QueueAction.PREPARE, "test", max_attempts=2)
    db_session.commit()
    [item] = queue.claim("w1", action=QueueAction.PREPARE)

    class Broken(PreparationService):
        def prepare(self, *args, **kwargs):
            raise ExternalServiceError("model host down")

    outcome = process_prepare_item(db_session, tenant_id, item, "w1", Broken(db_session, tenant_id))
    assert outcome is QueueOutcome.FAILED and item.state == "RETRY_WAIT" and item.attempts == 1
    assert queue.claim("w2", action=QueueAction.PREPARE) == [], "backoff still running"
    # Not-admitted work is parked for a human, not retried.
    blocked_co = opportunities.make(title="QA Engineer", fit_score=30, admitted=False)
    item2, _ = queue.enqueue(blocked_co, QueueAction.PREPARE, "test")
    db_session.commit()
    [claimed] = queue.claim("w1", action=QueueAction.PREPARE)
    assert claimed.id == item2.id
    assert process_prepare_item(db_session, tenant_id, claimed, "w1", service) is QueueOutcome.NEEDS_REVIEW
    assert claimed.state == "BLOCKED" and claimed.last_error.startswith("policy:")
