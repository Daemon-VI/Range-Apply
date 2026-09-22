"""Parked PREPARE items must not stay parked for ever.

Autopilot findings (2026-09-22), both from real data:

* Zeta and Zenoti attempts sat PREPARING for eight days behind a BLOCKED
  PREPARE item ("needs_user_input: required application questions have no
  stored answer") although the person had since approved those answers in
  Career Brain. Only answering on the *preparation* page released them.
* A CRED "capital partnerships" attempt stayed PREPARING for ever behind a
  PREPARE item the policy had blocked, after the classifier de-admitted the
  candidate opportunity.
"""

from datetime import timedelta

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.career.database.models import AnswerBankEntryRow
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.core.timeutils import db_now
from app.pipeline.models import QueueAction, QueueState
from app.pipeline.queue import QueueRepository
from app.preparation.models import PreparationStatus
from app.preparation.service import PreparationService
from tests.preparation.conftest import FACT_ANSWERS


def _item(db_session, tenant_id, co):
    return QueueRepository(db_session, tenant_id).find(co.opportunity_id, QueueAction.PREPARE)


def _attempt(db_session, co) -> ApplicationRow:
    db_session.refresh(co)
    return db_session.get(ApplicationRow, co.application_id)


def _approve_the_missing_answers(db_session, tenant_id, evidence) -> None:
    for category, question, answer in FACT_ANSWERS:
        evidence.create_answer(
            AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED),
            actor="test",
        )
    evidence.commit()
    # The person answers minutes later, not in the same clock tick: Windows'
    # system clock ticks at ~15 ms, so a same-millisecond commit would not be
    # strictly newer than the item that is waiting on it.
    db_session.query(AnswerBankEntryRow).filter(AnswerBankEntryRow.tenant_id == tenant_id).update(
        {AnswerBankEntryRow.updated_at: db_now() + timedelta(minutes=5)}, synchronize_session=False
    )
    db_session.commit()


def test_blocked_prepare_item_is_requeued_once_the_answers_are_approved(db_session, tenant_id, evidence, opportunities, scheduler):
    co = opportunities.make(title="Cloud Network Engineer II", company="Zeta")

    first = scheduler.run(prepare=True)
    item = _item(db_session, tenant_id, co)
    assert item.state == QueueState.BLOCKED.value
    assert item.last_error.startswith("needs_user_input")
    assert _attempt(db_session, co).status == ApplicationStatus.PREPARING.value
    assert first.reprepare_requeued == 0

    _approve_the_missing_answers(db_session, tenant_id, evidence)

    second = scheduler.run(prepare=True)
    assert second.reprepare_requeued == 1

    db_session.refresh(item)
    assert item.state == QueueState.SUCCEEDED.value
    prep = PreparationService(db_session, tenant_id).latest(co.id)
    assert prep.status == PreparationStatus.READY.value
    assert _attempt(db_session, co).status == ApplicationStatus.READY.value

    # One requeue per bank change: a third run finds nothing left to re-prepare.
    assert scheduler.run(prepare=True).reprepare_requeued == 0


def test_blocked_policy_item_releases_the_attempt_once_the_policy_de_admits(db_session, tenant_id, evidence, opportunities, scheduler):
    co = opportunities.make(title="Backend Engineer", company="CRED")  # the real row was CRED "capital partnerships"
    scheduler.run(prepare=False)

    queue = QueueRepository(db_session, tenant_id)
    item = queue.claim("worker-1", action=QueueAction.PREPARE, limit=1)[0]
    queue.block(item, "worker-1", "policy: not admitted by the application policy: band_disabled:LOW")
    co.policy_admitted = False
    co.policy_reason = "band_disabled:LOW"
    db_session.commit()

    run = scheduler.run(prepare=False)
    assert run.released == 1

    db_session.refresh(item)
    assert item.state == QueueState.CANCELLED.value
    attempt = _attempt(db_session, co)
    assert attempt.status == ApplicationStatus.CLOSED.value
    assert attempt.released_at is not None
    assert "not admitted by the application policy" in (attempt.status_reason or "")
