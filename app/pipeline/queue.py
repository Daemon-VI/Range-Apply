"""Database-backed application queue (blueprint §9).

One table, no broker. Claiming uses ``SELECT … FOR UPDATE SKIP LOCKED`` on
PostgreSQL so several executors (extension, local runner, headless lane)
can pull work concurrently without double-claiming; on SQLite, which is
single-writer, the same code path runs a plain select followed by a guarded
``UPDATE … WHERE state = <expected>`` so a lost race is detected rather
than silently overwritten.

Idempotency: ``(tenant, opportunity, action)`` is unique. Enqueueing again
returns the existing active item; enqueueing after a SUCCEEDED item is a
``ConflictError`` (that is the duplicate-application guard); FAILED and
CANCELLED items are re-queued only through :meth:`requeue`.
"""

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.career.database.models import AuditEventRow
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now, ordered_db_now
from app.pipeline.database.models import ApplicationQueueRow, CandidateOpportunityRow
from app.pipeline.models import (
    ACTIVE_QUEUE_STATES,
    Lane,
    QueueAction,
    QueueState,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 60


#: Work for an opportunity whose eligibility is only UNCERTAIN is claimed after
#: every ELIGIBLE / LIKELY item (first real dry run, 2026-09-14): item priority is
#: a snapshot taken at enqueue, and Zeta "Cloud Network Engineer II" (UNCERTAIN,
#: stored priority 70 from before a fit correction) would have been prepared and
#: executed ahead of two ELIGIBLE roles. Same rule as the scheduler's ordering_key.
_ELIGIBILITY_TIER = case((CandidateOpportunityRow.eligibility_status == "UNCERTAIN", 1), else_=0)


def idempotency_key(tenant_id: str, opportunity_id: str, action: QueueAction) -> str:
    return f"{tenant_id}:{opportunity_id}:{action.value}"


class QueueRepository:
    def __init__(self, db: Session, tenant_id: str):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id

    def commit(self) -> None:
        self.db.commit()

    @property
    def _is_postgres(self) -> bool:
        bind = self.db.get_bind()
        return bool(bind is not None and bind.dialect.name == "postgresql")

    # ------------------------------------------------------------------ #
    # audit
    # ------------------------------------------------------------------ #

    @staticmethod
    def snapshot(row: ApplicationQueueRow) -> dict[str, Any]:
        return {
            "state": row.state,
            "action": row.action,
            "lane": row.lane,
            "priority": row.priority,
            "attempts": row.attempts,
            "claimed_by": row.claimed_by,
            "last_error": row.last_error,
        }

    def _record(self, row: ApplicationQueueRow, action: str, actor: str, before: Optional[dict], summary: Optional[str] = None) -> None:
        self.db.add(
            AuditEventRow(
                tenant_id=self.tenant_id,
                entity_type="queue_item",
                entity_id=row.id,
                action=action,
                actor=actor or "system",
                before=before,
                after=self.snapshot(row),
                summary=(summary or "")[:512] or None,
                created_at=ordered_db_now(),
            )
        )
        self.db.flush()

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def _query(self):
        return self.db.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == self.tenant_id)

    def get(self, item_id: str) -> Optional[ApplicationQueueRow]:
        return self._query().filter(ApplicationQueueRow.id == item_id).first()

    def require(self, item_id: str) -> ApplicationQueueRow:
        row = self.get(item_id)
        if row is None:
            raise NotFoundError(f"Queue item not found: {item_id}")
        return row

    def find(self, opportunity_id: str, action: QueueAction) -> Optional[ApplicationQueueRow]:
        return (
            self._query()
            .filter(ApplicationQueueRow.idempotency_key == idempotency_key(self.tenant_id, opportunity_id, action))
            .first()
        )

    def list_items(
        self,
        state: Optional[QueueState] = None,
        lane: Optional[Lane] = None,
        action: Optional[QueueAction] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApplicationQueueRow], int]:
        query = self._query()
        if state is not None:
            query = query.filter(ApplicationQueueRow.state == state.value)
        if lane is not None:
            query = query.filter(ApplicationQueueRow.lane == lane.value)
        if action is not None:
            query = query.filter(ApplicationQueueRow.action == action.value)
        total = query.count()
        rows = (
            query.outerjoin(CandidateOpportunityRow, CandidateOpportunityRow.id == ApplicationQueueRow.candidate_opportunity_id)
            .order_by(
                _ELIGIBILITY_TIER,
                ApplicationQueueRow.priority.desc(),
                ApplicationQueueRow.available_at.asc(),
                ApplicationQueueRow.created_at.asc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total

    def counts_by_state(self) -> dict[str, int]:
        rows = (
            self.db.query(ApplicationQueueRow.state, func.count(ApplicationQueueRow.id))
            .filter(ApplicationQueueRow.tenant_id == self.tenant_id)
            .group_by(ApplicationQueueRow.state)
            .all()
        )
        counts = {state.value: 0 for state in QueueState}
        counts.update({state: count for state, count in rows})
        return counts

    # ------------------------------------------------------------------ #
    # enqueue
    # ------------------------------------------------------------------ #

    def enqueue(
        self,
        candidate_opportunity: CandidateOpportunityRow,
        action: QueueAction,
        actor: str,
        lane: Lane = Lane.REVIEW,
        priority: Optional[int] = None,
        available_at=None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> tuple[ApplicationQueueRow, bool]:
        """Idempotently enqueue ``action`` for a candidate opportunity.

        Returns ``(item, created)``. An existing *active* item is returned
        unchanged; a SUCCEEDED one raises ``ConflictError`` so the same
        opportunity is never submitted twice.
        """
        if candidate_opportunity.tenant_id != self.tenant_id:
            raise NotFoundError("Candidate opportunity not found in this tenant")
        existing = self.find(candidate_opportunity.opportunity_id, action)
        if existing is not None:
            if existing.state == QueueState.SUCCEEDED.value:
                raise ConflictError(
                    f"{action.value} already succeeded for this opportunity",
                    details={"queue_item_id": existing.id, "state": existing.state},
                )
            if QueueState(existing.state) in ACTIVE_QUEUE_STATES:
                return existing, False
            raise ConflictError(
                f"{action.value} is {existing.state} for this opportunity; use requeue",
                details={"queue_item_id": existing.id, "state": existing.state},
            )
        row = ApplicationQueueRow(
            tenant_id=self.tenant_id,
            candidate_opportunity_id=candidate_opportunity.id,
            opportunity_id=candidate_opportunity.opportunity_id,
            action=action.value,
            state=QueueState.PENDING.value,
            lane=lane.value,
            priority=int(priority if priority is not None else (candidate_opportunity.priority_score or 0)),
            idempotency_key=idempotency_key(self.tenant_id, candidate_opportunity.opportunity_id, action),
            available_at=available_at or db_now(),
            max_attempts=max_attempts,
        )
        self.db.add(row)
        self.db.flush()
        self._record(row, "enqueued", actor, None)
        return row, True

    # ------------------------------------------------------------------ #
    # claim / lease
    # ------------------------------------------------------------------ #

    def claim(
        self,
        worker_id: str,
        action: Optional[QueueAction] = None,
        lane: Optional[Lane] = None,
        limit: int = 1,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> list[ApplicationQueueRow]:
        """Claim up to ``limit`` runnable items for ``worker_id``.

        Runnable: PENDING or RETRY_WAIT with ``available_at`` in the past, or
        CLAIMED/PROCESSING whose lease has expired (a crashed worker). Highest
        priority first, then oldest ``available_at``.
        """
        now = db_now()
        self._park_exhausted_leases(now, action, lane)
        query = self._query().filter(
            or_(
                ApplicationQueueRow.state.in_([QueueState.PENDING.value, QueueState.RETRY_WAIT.value])
                & (ApplicationQueueRow.available_at <= now),
                ApplicationQueueRow.state.in_([QueueState.CLAIMED.value, QueueState.PROCESSING.value])
                & (ApplicationQueueRow.lease_expires_at < now)
                & (ApplicationQueueRow.attempts < ApplicationQueueRow.max_attempts),
            )
        )
        if action is not None:
            query = query.filter(ApplicationQueueRow.action == action.value)
        if lane is not None:
            query = query.filter(ApplicationQueueRow.lane == lane.value)
        query = (
            query.outerjoin(CandidateOpportunityRow, CandidateOpportunityRow.id == ApplicationQueueRow.candidate_opportunity_id)
            .order_by(
                _ELIGIBILITY_TIER,
                ApplicationQueueRow.priority.desc(),
                ApplicationQueueRow.available_at.asc(),
                ApplicationQueueRow.created_at.asc(),
            )
            .limit(limit)
        )
        if self._is_postgres:
            query = query.with_for_update(of=ApplicationQueueRow, skip_locked=True)

        claimed: list[ApplicationQueueRow] = []
        for row in query.all():
            expected_state = row.state
            before = self.snapshot(row)
            reclaimed = expected_state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value)
            updated = (
                self.db.query(ApplicationQueueRow)
                .filter(
                    ApplicationQueueRow.id == row.id,
                    ApplicationQueueRow.state == expected_state,
                    ApplicationQueueRow.claimed_by == row.claimed_by,
                )
                .update(
                    {
                        ApplicationQueueRow.state: QueueState.CLAIMED.value,
                        ApplicationQueueRow.claimed_by: worker_id,
                        ApplicationQueueRow.claimed_at: now,
                        ApplicationQueueRow.lease_expires_at: now + timedelta(seconds=lease_seconds),
                        ApplicationQueueRow.attempts: ApplicationQueueRow.attempts + 1,
                        ApplicationQueueRow.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                continue  # lost the race to another worker; skip it
            self.db.refresh(row)
            self._record(row, "reclaimed" if reclaimed else "claimed", worker_id, before)
            claimed.append(row)
        return claimed

    def claim_item(self, row: ApplicationQueueRow, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Optional[ApplicationQueueRow]:
        """Claim one specific runnable item (the extension claims the item for
        the page the person is on). Returns None when it is not claimable or
        another worker won the guarded update; an item already held by
        ``worker_id`` is returned as-is."""
        now = db_now()
        if row.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value) and row.claimed_by == worker_id:
            return row
        runnable = (
            row.state in (QueueState.PENDING.value, QueueState.RETRY_WAIT.value) and row.available_at is not None and row.available_at <= now
        ) or (
            row.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value) and row.lease_expires_at is not None and row.lease_expires_at < now
        )
        if not runnable:
            return None
        if row.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value) and row.attempts >= row.max_attempts:
            self._park_exhausted(row, now)
            return None
        before = self.snapshot(row)
        updated = (
            self.db.query(ApplicationQueueRow)
            .filter(ApplicationQueueRow.id == row.id, ApplicationQueueRow.state == row.state, ApplicationQueueRow.claimed_by == row.claimed_by)
            .update(
                {
                    ApplicationQueueRow.state: QueueState.CLAIMED.value,
                    ApplicationQueueRow.claimed_by: worker_id,
                    ApplicationQueueRow.claimed_at: now,
                    ApplicationQueueRow.lease_expires_at: now + timedelta(seconds=lease_seconds),
                    ApplicationQueueRow.attempts: ApplicationQueueRow.attempts + 1,
                    ApplicationQueueRow.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            return None
        self.db.refresh(row)
        self._record(row, "claimed", worker_id, before)
        return row

    def extend_lease(self, row: ApplicationQueueRow, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ApplicationQueueRow:
        self._assert_owner(row, worker_id)
        row.lease_expires_at = db_now() + timedelta(seconds=lease_seconds)
        self.db.flush()
        return row

    def start(self, row: ApplicationQueueRow, worker_id: str) -> ApplicationQueueRow:
        self._assert_owner(row, worker_id)
        if row.state != QueueState.CLAIMED.value:
            raise ConflictError(f"Cannot start a {row.state} item")
        before = self.snapshot(row)
        row.state = QueueState.PROCESSING.value
        self.db.flush()
        self._record(row, "processing", worker_id, before)
        return row

    def release(self, row: ApplicationQueueRow, worker_id: str, reason: Optional[str] = None) -> ApplicationQueueRow:
        """Give a claimed item back without counting it as a failure."""
        self._assert_owner(row, worker_id)
        before = self.snapshot(row)
        row.state = QueueState.PENDING.value
        row.claimed_by = None
        row.claimed_at = None
        row.lease_expires_at = None
        row.attempts = max(0, row.attempts - 1)
        self.db.flush()
        self._record(row, "released", worker_id, before, reason)
        return row

    # ------------------------------------------------------------------ #
    # outcomes
    # ------------------------------------------------------------------ #

    def succeed(self, row: ApplicationQueueRow, worker_id: str, result: Optional[dict[str, Any]] = None) -> ApplicationQueueRow:
        self._assert_owner(row, worker_id)
        before = self.snapshot(row)
        row.state = QueueState.SUCCEEDED.value
        row.result = dict(result or {})
        row.completed_at = db_now()
        row.lease_expires_at = None
        row.last_error = None
        self.db.flush()
        self._record(row, "succeeded", worker_id, before)
        return row

    def fail(self, row: ApplicationQueueRow, worker_id: str, error: str, retryable: bool = True) -> ApplicationQueueRow:
        """Record a failure; schedule a retry with exponential backoff while attempts remain."""
        self._assert_owner(row, worker_id)
        before = self.snapshot(row)
        row.last_error = (error or "")[:2000]
        row.lease_expires_at = None
        if retryable and row.attempts < row.max_attempts:
            delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, row.attempts - 1))
            row.state = QueueState.RETRY_WAIT.value
            row.available_at = db_now() + timedelta(seconds=delay)
            row.claimed_by = None
            row.claimed_at = None
            self.db.flush()
            self._record(row, "retry_scheduled", worker_id, before, error)
        else:
            row.state = QueueState.FAILED.value
            row.completed_at = db_now()
            self.db.flush()
            self._record(row, "failed", worker_id, before, error)
        return row

    def block(self, row: ApplicationQueueRow, worker_id: str, reason: str) -> ApplicationQueueRow:
        """Bot check, missing input, or similar: park it for a human, never retry blindly."""
        self._assert_owner(row, worker_id)
        before = self.snapshot(row)
        row.state = QueueState.BLOCKED.value
        row.last_error = (reason or "")[:2000]
        row.lease_expires_at = None
        self.db.flush()
        self._record(row, "blocked", worker_id, before, reason)
        return row

    def needs_review(self, row: ApplicationQueueRow, actor: str, reason: str) -> ApplicationQueueRow:
        before = self.snapshot(row)
        row.state = QueueState.NEEDS_REVIEW.value
        row.last_error = (reason or "")[:2000]
        row.lease_expires_at = None
        row.claimed_by = None
        self.db.flush()
        self._record(row, "needs_review", actor, before, reason)
        return row

    def resolve(self, row: ApplicationQueueRow, actor: str, result: Optional[dict[str, Any]] = None, reason: Optional[str] = None) -> ApplicationQueueRow:
        """Settle a parked item (BLOCKED / NEEDS_REVIEW / PROCESSING without an
        owner) as SUCCEEDED after out-of-band verification or a human decision.

        Not a worker path: no owner check, because the work was completed by
        verification or a person, not by the executor that parked it.
        """
        if row.state in (QueueState.SUCCEEDED.value, QueueState.CANCELLED.value):
            raise ConflictError(f"Cannot resolve a {row.state} item")
        before = self.snapshot(row)
        row.state = QueueState.SUCCEEDED.value
        row.result = dict(result or {})
        row.completed_at = db_now()
        row.lease_expires_at = None
        row.claimed_by = None
        row.last_error = None
        self.db.flush()
        self._record(row, "resolved", actor, before, reason)
        return row

    def cancel(self, row: ApplicationQueueRow, actor: str, reason: Optional[str] = None) -> ApplicationQueueRow:
        if row.state == QueueState.SUCCEEDED.value:
            raise ConflictError("Cannot cancel a succeeded item")
        before = self.snapshot(row)
        row.state = QueueState.CANCELLED.value
        row.completed_at = db_now()
        row.lease_expires_at = None
        row.claimed_by = None
        self.db.flush()
        self._record(row, "cancelled", actor, before, reason)
        return row

    def requeue(
        self,
        row: ApplicationQueueRow,
        actor: str,
        reason: Optional[str] = None,
        reset_attempts: bool = True,
        allow_succeeded: bool = False,
    ) -> ApplicationQueueRow:
        """Put a FAILED / CANCELLED / BLOCKED / NEEDS_REVIEW item back to PENDING.

        A SUCCEEDED item is refused (the duplicate-application guard) unless
        ``allow_succeeded`` is passed, which is only legitimate for PREPARE
        items whose package has since been invalidated; never for SUBMIT.
        """
        if row.state == QueueState.SUCCEEDED.value and not (allow_succeeded and row.action == QueueAction.PREPARE.value):
            raise ConflictError("Cannot requeue a succeeded item")
        if row.state in (QueueState.PENDING.value, QueueState.CLAIMED.value, QueueState.PROCESSING.value):
            raise ConflictError(f"Item is already {row.state}")
        before = self.snapshot(row)
        row.state = QueueState.PENDING.value
        row.available_at = db_now()
        row.claimed_by = None
        row.claimed_at = None
        row.lease_expires_at = None
        row.completed_at = None
        row.last_error = None
        if reset_attempts:
            row.attempts = 0
        self.db.flush()
        self._record(row, "requeued", actor, before, reason)
        return row

    def reclaim_expired(self, actor: str = "system") -> int:
        """Return items whose lease lapsed to PENDING so any worker can pick them up."""
        now = db_now()
        rows = (
            self._query()
            .filter(
                ApplicationQueueRow.state.in_([QueueState.CLAIMED.value, QueueState.PROCESSING.value]),
                ApplicationQueueRow.lease_expires_at < now,
            )
            .all()
        )
        for row in rows:
            if row.attempts >= row.max_attempts:
                self._park_exhausted(row, now, actor)
                continue
            before = self.snapshot(row)
            row.state = QueueState.PENDING.value
            row.claimed_by = None
            row.claimed_at = None
            row.lease_expires_at = None
            self.db.flush()
            self._record(row, "lease_expired", actor, before)
        return len(rows)

    def _park_exhausted_leases(self, now, action: Optional[QueueAction] = None, lane: Optional[Lane] = None) -> int:
        """Park every expired lease whose item has already used all of its attempts."""
        query = self._query().filter(
            ApplicationQueueRow.state.in_([QueueState.CLAIMED.value, QueueState.PROCESSING.value]),
            ApplicationQueueRow.lease_expires_at < now,
            ApplicationQueueRow.attempts >= ApplicationQueueRow.max_attempts,
        )
        if action is not None:
            query = query.filter(ApplicationQueueRow.action == action.value)
        if lane is not None:
            query = query.filter(ApplicationQueueRow.lane == lane.value)
        return sum(1 for row in query.all() if self._park_exhausted(row, now))

    def _park_exhausted(self, row: ApplicationQueueRow, now, actor: str = "system") -> bool:
        """An item whose worker was lost on every attempt goes to a person, never back to a worker.

        Audit fix (2026-09-14): ``max_attempts`` was honoured only by :meth:`fail`.
        A lease that lapsed (a worker that crashed or hung on the item) was
        reclaimed with ``attempts + 1`` without limit, so one item that kills its
        worker was retried forever. Guarded like a claim, so a worker that still
        holds a live lease is never overtaken.
        """
        before = self.snapshot(row)
        reason = f"worker lost on all {row.max_attempts} attempts (lease expired); parked for review"
        updated = (
            self.db.query(ApplicationQueueRow)
            .filter(
                ApplicationQueueRow.id == row.id,
                ApplicationQueueRow.state == row.state,
                ApplicationQueueRow.claimed_by == row.claimed_by,
                ApplicationQueueRow.lease_expires_at < now,
            )
            .update(
                {
                    ApplicationQueueRow.state: QueueState.NEEDS_REVIEW.value,
                    ApplicationQueueRow.claimed_by: None,
                    ApplicationQueueRow.lease_expires_at: None,
                    ApplicationQueueRow.last_error: reason,
                    ApplicationQueueRow.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            return False
        self.db.refresh(row)
        self._record(row, "attempts_exhausted", actor, before, reason)
        return True

    # ------------------------------------------------------------------ #

    @staticmethod
    def _assert_owner(row: ApplicationQueueRow, worker_id: str) -> None:
        if row.claimed_by != worker_id:
            raise ConflictError(
                f"Queue item is owned by {row.claimed_by or 'nobody'}, not {worker_id}",
                details={"claimed_by": row.claimed_by},
            )
