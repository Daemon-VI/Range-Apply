"""Submission-time preconditions.

Nothing is assumed to have stayed true since scheduling. Immediately before
an executor may act, the attempt, its preparation, the opening, the policy
(blocklist, cool-down, duplicates, caps) and the prepared material are all
checked again. Any failure stops execution before submit.

Cap semantics at submission (consistent with Phase 5):

* the admission reservation is **consumed** by the submission when the
  submission happens in the same tenant-local day and week it was reserved
  in - nothing is counted twice;
* when the period has rolled over, execution **refreshes** the reservation:
  it takes a slot in the current day/week through the same atomic ledger
  update and gives the old one back; if today is full the attempt simply
  waits (DAILY_CAP_REACHED / WEEKLY_CAP_REACHED), it is not a failure;
* an attempt with no reservation (legacy row) reserves at execution;
* a released attempt (SLOT_RELEASED) is never executed.
"""

from typing import Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.application.database.models import ApplicationRow
from app.application.engine import SUBMITTED_STATUSES
from app.application.models import ApplicationStatus
from app.core.timeutils import db_now, utc_now
from app.execution.models import PreconditionCode, PreconditionFailure, PreconditionReport
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.models import (
    ApplicationPolicy,
    DuplicatePolicy,
    OpportunityState,
    OpportunityStatus,
)
from app.pipeline.policy import company_key
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import PreparationStatus, PreparedAnswerStatus, ValidationStatus
from app.scheduler.attempts import AttemptRepository, duplicate_key, holds_slot
from app.scheduler.caps import CapLedger, period_keys


def _fail(code: PreconditionCode, message: str, **detail) -> PreconditionFailure:
    return PreconditionFailure(code=code, message=message, detail=detail)


class PreconditionChecker:
    def __init__(self, db: Session, tenant_id: str, policy: ApplicationPolicy):
        self.db = db
        self.tenant_id = tenant_id
        self.policy = policy
        self.attempts = AttemptRepository(db, tenant_id)
        self.ledger = CapLedger(db, tenant_id)

    def check(
        self,
        attempt: ApplicationRow,
        preparation: Optional[ApplicationPreparationRow],
        co: Optional[CandidateOpportunityRow],
        opportunity: Optional[OpportunityRow],
        stale_inputs: Optional[list[str]] = None,
        reserve: bool = False,
        executing: bool = False,
    ) -> PreconditionReport:
        """Evaluate every precondition; with ``reserve`` also take/refresh the cap slot.

        ``reserve`` writes to the ledger and must run inside the caller's
        transaction; a preview passes ``reserve=False`` and only *reads* usage.
        ``executing`` is the pre-submit gate: the attempt is legitimately
        SUBMITTING at that point.
        """
        failures: list[PreconditionFailure] = []
        now = utc_now()

        # -- attempt -------------------------------------------------------
        if attempt.tenant_id != self.tenant_id:
            failures.append(_fail(PreconditionCode.TENANT_MISMATCH, "attempt belongs to another tenant"))
            return PreconditionReport(ok=False, failures=failures)
        expected = ApplicationStatus.SUBMITTING.value if executing else ApplicationStatus.READY.value
        if attempt.status in SUBMITTED_STATUSES and attempt.status != expected:
            failures.append(_fail(PreconditionCode.ALREADY_SUBMITTED, f"attempt is {attempt.status}", status=attempt.status))
            return PreconditionReport(ok=False, failures=failures)
        if attempt.status != expected:
            failures.append(_fail(PreconditionCode.ATTEMPT_NOT_READY, f"attempt is {attempt.status}, not {expected}", status=attempt.status))
        if attempt.released_at is not None:
            failures.append(_fail(PreconditionCode.SLOT_RELEASED, "the attempt's cap slot was released"))

        # -- preparation ---------------------------------------------------
        if preparation is None:
            failures.append(_fail(PreconditionCode.PREPARATION_MISSING, "no preparation attached"))
        else:
            if preparation.tenant_id != self.tenant_id:
                failures.append(_fail(PreconditionCode.TENANT_MISMATCH, "preparation belongs to another tenant"))
            if attempt.opportunity_id and preparation.opportunity_id != attempt.opportunity_id:
                failures.append(_fail(PreconditionCode.OPPORTUNITY_MISMATCH, "preparation is for another opportunity"))
            if preparation.status == PreparationStatus.INVALIDATED.value:
                failures.append(_fail(PreconditionCode.PREPARATION_INVALIDATED, "preparation was invalidated"))
            elif preparation.status == PreparationStatus.SUPERSEDED.value:
                failures.append(_fail(PreconditionCode.PREPARATION_NOT_CURRENT, "a newer preparation version exists", version=preparation.version))
            elif preparation.status != PreparationStatus.READY.value:
                failures.append(_fail(PreconditionCode.PREPARATION_NOT_READY, f"preparation is {preparation.status}", status=preparation.status))
            if preparation.validation_status != ValidationStatus.PASSED.value:
                failures.append(_fail(PreconditionCode.VALIDATION_FAILED, f"truth validation is {preparation.validation_status}"))
            unanswered = [a.question for a in preparation.answers if a.required and a.status == PreparedAnswerStatus.NEEDS_USER_INPUT.value]
            if unanswered:
                failures.append(_fail(PreconditionCode.USER_INPUT_REQUIRED, f"{len(unanswered)} required answer(s) missing", questions=unanswered[:10]))
            if stale_inputs:
                failures.append(_fail(PreconditionCode.PREPARATION_STALE, "inputs changed since preparation: " + ", ".join(stale_inputs), inputs=stale_inputs))

        # -- opening / candidate state -------------------------------------
        if opportunity is None:
            failures.append(_fail(PreconditionCode.OPPORTUNITY_CLOSED, "opportunity missing"))
        else:
            if opportunity.status == OpportunityStatus.CLOSED.value:
                failures.append(_fail(PreconditionCode.OPPORTUNITY_CLOSED, "opening is closed at the source"))
            if co is not None and co.state in (OpportunityState.CLOSED.value, OpportunityState.SKIPPED.value):
                failures.append(_fail(PreconditionCode.OPPORTUNITY_CLOSED, f"candidate opportunity is {co.state}"))
            blocked = {company_key(c) for c in self.policy.blocked_companies}
            if company_key(opportunity.company) in blocked:
                failures.append(_fail(PreconditionCode.COMPANY_BLOCKED, "company is on the blocklist", company=opportunity.company))
            self._check_company_history(attempt, opportunity, now, failures)

        # -- caps ----------------------------------------------------------
        keys = period_keys(now, self.policy.timezone)
        report = PreconditionReport(ok=not failures, failures=failures, stale_inputs=list(stale_inputs or []), cap_day_key=keys.day.key, cap_week_key=keys.week.key)
        if failures:
            return report
        cap_failure = self._check_caps(attempt, keys, reserve)
        if cap_failure is not None:
            report.failures.append(cap_failure)
            report.ok = False
        return report

    # ------------------------------------------------------------------ #

    def _check_company_history(self, attempt: ApplicationRow, opportunity: OpportunityRow, now, failures: list) -> None:
        """Cool-down and duplicates from *other* attempts at this company.

        Indexed on ``opportunities.company_key`` (normalised), so the check
        costs one small query per attempt whatever the tenant's history size.
        """
        key = company_key(opportunity.company)
        rows = (
            self.db.query(ApplicationRow, OpportunityRow)
            .join(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
            .filter(
                ApplicationRow.tenant_id == self.tenant_id,
                ApplicationRow.id != attempt.id,
                or_(
                    OpportunityRow.company_key == key,
                    # Rows that predate the key (should be none after the backfill).
                    and_(OpportunityRow.company_key.is_(None), func.lower(OpportunityRow.company) == opportunity.company.lower()),
                ),
            )
            .all()
        )
        same_company = {o.id: o for a, o in rows}
        attempts = {a.opportunity_id: a for a, o in rows}
        if not attempts:
            return
        cooldown, duplicates = self.attempts.build_indexes(attempts, same_company, [], self.policy.cooldown_days)
        until = cooldown.active_until(opportunity.company, now)
        if until is not None:
            # An in-flight attempt at the same company *other than this one* is
            # also a cool-down: the scheduler admits one opening per company
            # per window, execution honours the same rule.
            failures.append(_fail(PreconditionCode.COOLDOWN_ACTIVE, f"company cool-down until {until.isoformat()}", until=until.isoformat()))
        others = duplicates.get(duplicate_key(opportunity.company, opportunity.title), set()) - {opportunity.id}
        if others:
            failures.append(_fail(PreconditionCode.DUPLICATE_APPLICATION, "already applied to the same title at this company", opportunities=sorted(others)))
        if opportunity.status == OpportunityStatus.REPOSTED.value and self.policy.duplicate_policy is DuplicatePolicy.BLOCK:
            previous = attempts.get(opportunity.id)
            if previous is not None and previous.status in SUBMITTED_STATUSES:
                failures.append(_fail(PreconditionCode.DUPLICATE_OPPORTUNITY, "applied before this repost; duplicate_policy=BLOCK"))

    def _check_caps(self, attempt: ApplicationRow, keys, reserve: bool) -> Optional[PreconditionFailure]:
        if self.policy.daily_cap <= 0:
            return _fail(PreconditionCode.DAILY_CAP_REACHED, "daily_cap is 0 (paused)")
        if self.policy.weekly_cap <= 0:
            return _fail(PreconditionCode.WEEKLY_CAP_REACHED, "weekly_cap is 0 (paused)")
        same_period = holds_slot(attempt) and attempt.cap_day == keys.day.key and attempt.cap_week == keys.week.key
        if same_period:
            return None  # the admission reservation is consumed by this submission
        if not reserve:
            # Preview: project whether a fresh slot would be available.
            if self.ledger.usage("DAY", keys.day.key) >= self.policy.daily_cap:
                return _fail(PreconditionCode.DAILY_CAP_REACHED, "no daily capacity left for a refreshed reservation", period=keys.day.key)
            if self.ledger.usage("WEEK", keys.week.key) >= self.policy.weekly_cap:
                return _fail(PreconditionCode.WEEKLY_CAP_REACHED, "no weekly capacity left for a refreshed reservation", period=keys.week.key)
            return None
        reason = self.ledger.reserve(keys, self.policy.daily_cap, self.policy.weekly_cap)
        if reason is not None:
            code = PreconditionCode(reason.value)
            return _fail(code, f"{code.value.lower().replace('_', ' ')} for {keys.day.key if code is PreconditionCode.DAILY_CAP_REACHED else keys.week.key}", period=keys.day.key)
        if holds_slot(attempt):
            self.ledger.release(attempt.cap_day, attempt.cap_week)  # refreshed: give the old period back
        attempt.cap_day = keys.day.key
        attempt.cap_week = keys.week.key
        if attempt.reserved_at is None:
            attempt.reserved_at = db_now()
        attempt.released_at = None
        return None
