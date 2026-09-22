"""Application attempts as the scheduler sees them.

An attempt is the ``applications`` row for ``(tenant, opportunity)``. The
scheduler *reserves* one when it admits an opportunity (that is what a cap
counts), marks it READY when the PREPARE queue item succeeds, and
*releases* it when the work can no longer lead to a submission. Submission
itself stays in ``ApplicationEngine`` (Phase 6+).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.engine import IN_FLIGHT_STATUSES, SUBMITTED_STATUSES
from app.application.models import ApplicationStatus
from app.core.errors import ConflictError
from app.core.timeutils import db_now, from_db
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.identity import normalize_title_for_identity
from app.pipeline.models import Lane, OpportunityState, TailoringLevel
from app.pipeline.policy import company_key
from app.scheduler.caps import CapLedger, PeriodKeys

logger = logging.getLogger(__name__)

#: Candidate-opportunity states that mean "this candidate applied" even when
#: no attempt row exists (Phase 2 semantics, kept for manual bookkeeping).
APPLIED_STATES = frozenset(
    {
        OpportunityState.SUBMITTED.value,
        OpportunityState.VERIFICATION_PENDING.value,
        OpportunityState.VERIFIED.value,
        OpportunityState.INTERVIEWING.value,
        OpportunityState.OFFER.value,
        OpportunityState.REJECTED.value,
    }
)


def holds_slot(row: ApplicationRow) -> bool:
    return row.reserved_at is not None and row.released_at is None


def is_submitted(row: ApplicationRow) -> bool:
    return row.status in SUBMITTED_STATUSES or row.status == ApplicationStatus.REJECTED.value


def is_in_flight(row: ApplicationRow) -> bool:
    return row.status in IN_FLIGHT_STATUSES


def duplicate_key(company: str, title: str) -> str:
    return f"{company_key(company)}|{normalize_title_for_identity(title)}"


@dataclass
class CooldownIndex:
    """Per normalised company: when the cool-down ends and what started it."""

    until: dict[str, datetime] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)

    def add(self, company: str, started: Optional[datetime], days: int, source: str) -> None:
        started = from_db(started)
        if started is None or days <= 0:
            return
        key = company_key(company)
        ends = started + timedelta(days=days)
        if key not in self.until or ends > self.until[key]:
            self.until[key] = ends
            self.source[key] = source

    def active_until(self, company: str, now: datetime) -> Optional[datetime]:
        """The cool-down end if ``now`` is strictly before it, else ``None``.

        Boundary: at exactly ``until`` the company is allowed again.
        """
        ends = self.until.get(company_key(company))
        if ends is not None and now < ends:
            return ends
        return None


class AttemptRepository:
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.ledger = CapLedger(db, tenant_id)

    # ---------------------------------------------------------------- reads

    def _query(self):
        return self.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == self.tenant_id)

    def by_opportunity(self) -> dict[str, ApplicationRow]:
        return {row.opportunity_id: row for row in self._query().filter(ApplicationRow.opportunity_id.isnot(None)).all()}

    def legacy_by_job(self, job_ids: list[str]) -> dict[str, ApplicationRow]:
        """Rows created through the job-id API before tenancy, keyed by job id."""
        if not job_ids:
            return {}
        rows = (
            self.db.query(ApplicationRow)
            .filter(ApplicationRow.tenant_id.is_(None), ApplicationRow.job_id.in_(list(job_ids)))
            .all()
        )
        return {row.job_id: row for row in rows}

    def get_for_opportunity(self, opportunity_id: str) -> Optional[ApplicationRow]:
        return self._query().filter(ApplicationRow.opportunity_id == opportunity_id).first()

    def counts_by_status(self) -> dict[str, int]:
        from sqlalchemy import func

        rows = (
            self.db.query(ApplicationRow.status, func.count(ApplicationRow.id))
            .filter(ApplicationRow.tenant_id == self.tenant_id)
            .group_by(ApplicationRow.status)
            .all()
        )
        return dict(rows)

    def build_indexes(
        self,
        attempts: dict[str, ApplicationRow],
        opportunities: dict[str, OpportunityRow],
        candidates: list[CandidateOpportunityRow],
        cooldown_days: int,
    ) -> tuple[CooldownIndex, dict[str, set[str]]]:
        """Cool-down index and duplicate index from what this tenant has done.

        Cool-down starts at ``submitted_at`` for submitted attempts and at
        ``reserved_at`` for attempts still holding a slot (so two openings
        at one company are not both admitted inside one window). Released
        attempts (cancelled, failed before submission) start nothing.

        Duplicate index: ``company|normalised title`` -> opportunity ids with
        a live or submitted attempt, so a second row for the same opening
        that escaped identity resolution (different location bucket) is
        still caught.
        """
        cooldown = CooldownIndex()
        duplicates: dict[str, set[str]] = {}
        for opportunity_id, row in attempts.items():
            opp = opportunities.get(opportunity_id)
            if opp is None:
                continue
            live = holds_slot(row) or is_submitted(row)
            if is_submitted(row):
                cooldown.add(opp.company, row.submitted_at or row.reserved_at or row.updated_at, cooldown_days, "submitted")
            elif holds_slot(row):
                cooldown.add(opp.company, row.reserved_at, cooldown_days, "in_progress")
            if live:
                duplicates.setdefault(duplicate_key(opp.company, opp.title), set()).add(opportunity_id)
        for co in candidates:
            if co.state in APPLIED_STATES and co.opportunity_id not in attempts:
                opp = opportunities.get(co.opportunity_id) or co.opportunity
                cooldown.add(opp.company, co.state_changed_at, cooldown_days, "state")
                duplicates.setdefault(duplicate_key(opp.company, opp.title), set()).add(co.opportunity_id)
        return cooldown, duplicates

    # --------------------------------------------------------------- writes

    def _event(self, row: ApplicationRow, event_type: str, from_status: Optional[str], to_status: Optional[str], metadata: Optional[dict] = None) -> None:
        self.db.add(
            ApplicationEventRow(
                application_id=row.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                metadata_=metadata or {},
            )
        )

    def reserve(
        self,
        co: CandidateOpportunityRow,
        opportunity: OpportunityRow,
        keys: PeriodKeys,
        daily_cap: int,
        weekly_cap: int,
        lane: Lane,
        level: TailoringLevel,
        existing: Optional[ApplicationRow],
        run_id: str,
    ):
        """Reserve a cap slot and the attempt row; returns ``(row, reason)``.

        ``reason`` is a cap reason when no slot was available (nothing is
        written then). The attempt row insert and the ledger increment sit in
        one savepoint, so a lost race on the unique ``(tenant, opportunity)``
        constraint rolls both back and reports ALREADY_IN_PROGRESS.
        ``existing`` is the tenant's attempt for the opportunity or a legacy
        job-id row (see :meth:`legacy_by_job`); the caller prefetches both.
        """
        try:
            with self.db.begin_nested():
                reason = self.ledger.reserve(keys, daily_cap, weekly_cap)
                if reason is not None:
                    return None, reason
                now = db_now()
                if existing is None:
                    row = ApplicationRow(
                        job_id=opportunity.canonical_job_id,
                        tenant_id=self.tenant_id,
                        opportunity_id=opportunity.id,
                        candidate_opportunity_id=co.id,
                        status=ApplicationStatus.QUALIFIED.value,
                        attempt_number=1,
                    )
                    self.db.add(row)
                    previous = None
                else:
                    row = existing
                    previous = row.status
                    row.tenant_id = self.tenant_id
                    row.opportunity_id = opportunity.id
                    row.candidate_opportunity_id = co.id
                    row.attempt_number = (row.attempt_number or 0) + 1
                    row.status = ApplicationStatus.QUALIFIED.value
                    row.preparation_id = None
                    row.confirmation = None
                row.lane = lane.value
                row.tailoring_level = level.value
                row.cap_day = keys.day.key
                row.cap_week = keys.week.key
                row.reserved_at = now
                row.released_at = None
                self.db.flush()
                self._event(
                    row,
                    "scheduled",
                    previous,
                    row.status,
                    {"run_id": run_id, "cap_day": keys.day.key, "cap_week": keys.week.key, "attempt": row.attempt_number},
                )
        except IntegrityError as exc:
            logger.info("Attempt for opportunity %s already exists (%s)", opportunity.id, type(exc).__name__)
            raise ConflictError("attempt already exists for this opportunity") from exc
        return row, None

    def mark_ready(self, row: ApplicationRow, preparation_id: str, run_id: str) -> bool:
        if row.status == ApplicationStatus.READY.value and row.preparation_id == preparation_id:
            return False
        previous = row.status
        row.status = ApplicationStatus.READY.value
        row.preparation_id = preparation_id
        self.db.flush()
        self._event(row, "prepared", previous, row.status, {"run_id": run_id, "preparation_id": preparation_id})
        return True

    def mark_preparing(self, row: ApplicationRow, preparation_id: Optional[str], run_id: str, note: str) -> bool:
        if row.status == ApplicationStatus.PREPARING.value and row.preparation_id == preparation_id:
            return False
        previous = row.status
        row.status = ApplicationStatus.PREPARING.value
        if preparation_id:
            row.preparation_id = preparation_id
        self.db.flush()
        self._event(row, "preparing", previous, row.status, {"run_id": run_id, "note": note})
        return True

    def transition(self, row: ApplicationRow, new_status: ApplicationStatus, actor: str, reason: Optional[str] = None, metadata: Optional[dict] = None, force: bool = False) -> bool:
        """Guarded attempt transition (``ATTEMPT_TRANSITIONS``); returns False when unchanged."""
        from app.application.models import ATTEMPT_TRANSITIONS

        current = ApplicationStatus(row.status)
        if current is new_status:
            return False
        if not force and new_status not in ATTEMPT_TRANSITIONS.get(current, frozenset()):
            raise ConflictError(
                f"Illegal attempt transition {current.value} -> {new_status.value}",
                details={"from": current.value, "to": new_status.value, "application_id": row.id},
            )
        row.status = new_status.value
        row.status_reason = (reason or "")[:256] or None
        self.db.flush()
        self._event(row, f"attempt:{new_status.value.lower()}", current.value, new_status.value, {"actor": actor, "reason": reason, **(metadata or {})})
        return True

    def release(self, row: ApplicationRow, status: ApplicationStatus, reason: str, run_id: str) -> None:
        """Give the slot back; the attempt ends in ``status`` (CLOSED or FAILED)."""
        if not holds_slot(row):
            if row.status != status.value:
                previous = row.status
                row.status = status.value
                row.status_reason = reason[:256]
                self.db.flush()
                self._event(row, "released", previous, row.status, {"run_id": run_id, "reason": reason})
            return
        previous = row.status
        self.ledger.release(row.cap_day, row.cap_week)
        row.released_at = db_now()
        row.status = status.value
        row.status_reason = reason[:256]
        self.db.flush()
        self._event(row, "released", previous, row.status, {"run_id": run_id, "reason": reason})
