"""Consume PREPARE queue items and produce a preparation outcome.

Outcome mapping onto the Phase 2 queue semantics:

* READY            -> item SUCCEEDED, result ``{"outcome": "READY_FOR_EXECUTION", ...}``
* NEEDS_USER_INPUT -> item BLOCKED (waits for a human; never retried blindly)
* NEEDS_REVIEW     -> item NEEDS_REVIEW
* exception        -> item FAILED / RETRY_WAIT per the queue's retry rules
* policy refusal   -> item BLOCKED with the policy reason

Preparation never enqueues a SUBMIT item; that is the scheduler's decision
in a later phase.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.errors import CareerOSError, PolicyBlocked
from app.pipeline.database.models import ApplicationQueueRow
from app.pipeline.models import Lane, QueueAction
from app.pipeline.queue import QueueRepository
from app.preparation.models import PreparationStatus, QueueOutcome
from app.preparation.service import PreparationService

logger = logging.getLogger(__name__)


def process_prepare_item(
    db: Session,
    tenant_id: str,
    item: ApplicationQueueRow,
    worker_id: str,
    service: Optional[PreparationService] = None,
) -> QueueOutcome:
    """Run preparation for one claimed PREPARE item and settle the item."""
    queue = QueueRepository(db, tenant_id)
    service = service or PreparationService(db, tenant_id, actor=f"worker:{worker_id}")
    queue.start(item, worker_id)
    try:
        row = service.prepare(item.candidate_opportunity_id, actor=f"worker:{worker_id}")
    except PolicyBlocked as exc:
        queue.block(item, worker_id, f"policy: {exc.message}")
        return QueueOutcome.NEEDS_REVIEW
    except CareerOSError as exc:
        queue.fail(item, worker_id, exc.message, retryable=exc.retryable)
        return QueueOutcome.FAILED
    except Exception as exc:  # noqa: BLE001 - settle the item, keep the worker alive
        logger.exception("Preparation crashed for queue item %s", item.id)
        queue.fail(item, worker_id, f"{type(exc).__name__}: {exc}", retryable=True)
        return QueueOutcome.FAILED

    status = PreparationStatus(row.status)
    result = {"preparation_id": row.id, "preparation_version": row.version, "preparation_status": row.status}
    if status is PreparationStatus.READY:
        queue.succeed(item, worker_id, {**result, "outcome": QueueOutcome.READY_FOR_EXECUTION.value})
        return QueueOutcome.READY_FOR_EXECUTION
    if status is PreparationStatus.NEEDS_USER_INPUT:
        item.result = {**result, "outcome": QueueOutcome.NEEDS_USER_INPUT.value}
        queue.block(item, worker_id, "needs_user_input: required application questions have no stored answer")
        return QueueOutcome.NEEDS_USER_INPUT
    if status is PreparationStatus.NEEDS_REVIEW:
        item.result = {**result, "outcome": QueueOutcome.NEEDS_REVIEW.value}
        queue.needs_review(item, worker_id, "needs_review: preparation requires a human look")
        return QueueOutcome.NEEDS_REVIEW
    queue.fail(item, worker_id, f"preparation ended in {row.status}", retryable=True)
    return QueueOutcome.FAILED


def run_prepare_queue(
    db: Session,
    tenant_id: str,
    worker_id: str,
    limit: int = 10,
    lane: Optional[Lane] = None,
) -> dict[str, int]:
    """Claim up to ``limit`` PREPARE items and process them; returns outcome counts."""
    queue = QueueRepository(db, tenant_id)
    service = PreparationService(db, tenant_id, actor=f"worker:{worker_id}")
    items = queue.claim(worker_id, action=QueueAction.PREPARE, lane=lane, limit=limit)
    db.commit()
    counts = {outcome.value: 0 for outcome in QueueOutcome}
    counts["claimed"] = len(items)
    for item in items:
        outcome = process_prepare_item(db, tenant_id, item, worker_id, service)
        counts[outcome.value] += 1
        db.commit()
    return counts
