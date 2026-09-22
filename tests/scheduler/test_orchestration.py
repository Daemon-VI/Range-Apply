"""Scheduler ↔ Phase 4 preparation: gating, readiness, idempotent worker runs."""

from app.application.models import ApplicationStatus
from app.pipeline.models import AdmissionReason, OpportunityState, QueueAction
from app.preparation.queue_worker import run_prepare_queue
from app.preparation.service import PreparationService
from tests.scheduler.conftest import decisions_by_co


def test_admitted_prepared_ready_for_execution_zero_ai(db_session, tenant_id, scheduler, opportunities, answered_bank):
    co = opportunities.make(title="Backend Engineer", fit_score=90, company="Alpha")
    run = scheduler.run(prepare=True, prepare_limit=10, worker_id="w1")
    assert run.admitted == 1
    assert run.preparation["claimed"] == 1 and run.preparation["READY_FOR_EXECUTION"] == 1
    assert run.ready_for_execution == 1
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    assert attempt.status == ApplicationStatus.READY.value and attempt.preparation_id
    prep = PreparationService(db_session, tenant_id).require(attempt.preparation_id)
    assert prep.status == "READY" and prep.ai_calls == 0 and not prep.ai_used
    db_session.refresh(co)
    assert co.state == OpportunityState.PREPARED.value
    ready = scheduler.ready_for_execution()
    assert [a.id for a in ready] == [attempt.id]
    # Next run: in progress (ready), nothing re-enqueued, cap unchanged.
    again = scheduler.run()
    assert again.already_queued == 1 and again.enqueued == 0 and again.ready_for_execution == 1
    assert decisions_by_co(scheduler.preview())[co.id].reason == "ready for execution"
    assert scheduler.capacity().day.used == 1


def test_needs_user_input_gates_execution(db_session, tenant_id, scheduler, opportunities, evidence):
    """No answer bank: fact questions need the candidate; the attempt is not execution-ready."""
    co = opportunities.make(title="Backend Engineer", fit_score=90, company="Alpha")
    run = scheduler.run(prepare=True, worker_id="w1")
    assert run.preparation["NEEDS_USER_INPUT"] == 1
    assert run.needs_user_input == 1 and run.ready_for_execution == 0
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    assert attempt.status == ApplicationStatus.PREPARING.value
    assert scheduler.ready_for_execution() == []
    item = scheduler.queue.find(co.opportunity_id, QueueAction.PREPARE)
    assert item.state == "BLOCKED"
    decision = decisions_by_co(scheduler.preview())[co.id]
    assert decision.code is AdmissionReason.NEEDS_USER_INPUT and decision.preparation_id
    # Answering the questions and re-queueing makes it ready on the next pass.
    service = PreparationService(db_session, tenant_id)
    prep = service.require(attempt.preparation_id)
    for answer in prep.answers:
        if answer.status == "NEEDS_USER_INPUT":
            service.answer_question(prep.id, answer.id, "An honest answer from the candidate.", "user")
    db_session.commit()
    scheduler.queue.requeue(item, "user", "answered")
    db_session.commit()
    run = scheduler.run(prepare=True, worker_id="w1")
    assert run.ready_for_execution == 1
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value


def test_worker_runs_twice_converge(db_session, tenant_id, scheduler, opportunities, answered_bank):
    for i in range(3):
        opportunities.make(title=f"Role {i}", fit_score=80, company=f"Co{i}")
    scheduler.run()
    first = run_prepare_queue(db_session, tenant_id, "w1", limit=10)
    second = run_prepare_queue(db_session, tenant_id, "w2", limit=10)
    assert first["claimed"] == 3 and first["READY_FOR_EXECUTION"] == 3
    assert second["claimed"] == 0
    run = scheduler.run()
    assert run.ready_for_execution == 3 and run.enqueued == 0
    assert len(scheduler.ready_for_execution()) == 3


def test_manually_prepared_opportunity_is_admitted_and_reused(db_session, tenant_id, scheduler, opportunities, answered_bank):
    co = opportunities.make(title="Backend Engineer", fit_score=90, company="Alpha")
    prep = PreparationService(db_session, tenant_id).prepare(co.id)
    assert prep.status == "READY"
    db_session.refresh(co)
    assert co.state == OpportunityState.PREPARED.value
    run = scheduler.run(prepare=True, worker_id="w1")
    assert run.admitted == 1 and run.preparation["READY_FOR_EXECUTION"] == 1
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    assert attempt.preparation_id == prep.id, "the READY package is reused, not rebuilt"
    assert PreparationService(db_session, tenant_id).counts_by_status()["READY"] == 1


def test_scheduler_uses_policy_tailoring_level_per_band(db_session, tenant_id, scheduler, opportunities, answered_bank):
    high = opportunities.make(title="Platform Engineer", fit_score=95, company="Alpha")
    low = opportunities.make(title="Support Engineer", fit_score=10, company="Beta")
    scheduler.run(prepare=True, prepare_limit=10)
    service = PreparationService(db_session, tenant_id)
    assert service.latest(high.id).tailoring_level == "L2"
    assert service.latest(low.id).tailoring_level == "L0"


def test_answers_given_on_the_review_page_make_the_attempt_ready_without_a_manual_requeue(db_session, tenant_id, scheduler, opportunities, evidence):
    # First real dry run: the candidate answers on the Review page (answer_question only). The
    # preparation became READY but its PREPARE item stayed BLOCKED, so the attempt never reached READY.
    co = opportunities.make(title="Backend Engineer", fit_score=90, company="Alpha")
    scheduler.run(prepare=True, worker_id="w1")
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    item = scheduler.queue.find(co.opportunity_id, QueueAction.PREPARE)
    assert item.state == "BLOCKED"
    service = PreparationService(db_session, tenant_id)
    prep = service.require(attempt.preparation_id)
    for answer in prep.answers:
        if answer.status == "NEEDS_USER_INPUT":
            service.answer_question(prep.id, answer.id, "An honest answer from the candidate.", "user")
    db_session.commit()

    run = scheduler.run()
    db_session.refresh(attempt)
    db_session.refresh(item)
    assert item.state == "SUCCEEDED" and item.result["preparation_id"] == prep.id
    assert attempt.status == ApplicationStatus.READY.value and run.ready_for_execution == 1
    assert scheduler.queue.find(co.opportunity_id, QueueAction.SUBMIT) is not None, "the READY attempt gets its SUBMIT item"
    assert scheduler.run().ready_for_execution == 1, "a second pass changes nothing"
