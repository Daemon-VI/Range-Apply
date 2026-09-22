"""Pure, deterministic scheduling decisions.

``decide`` looks at one candidate opportunity plus pre-loaded indexes and
returns a :class:`Verdict`. It performs no I/O and consults priority only
through :func:`ordering_key`; the caller applies caps and the run window
*after* the verdict, in order, so priority decides order and never whether.

Check order (first hit wins):

1. the candidate skipped it            -> USER_BLOCKED
2. the opening is closed               -> OPPORTUNITY_CLOSED
3. an attempt exists                   -> ALREADY_SUBMITTED / DUPLICATE_OPPORTUNITY
                                          / ALREADY_IN_PROGRESS / NEEDS_*
4. static policy (eligibility, blocklist, bands, fit floor)
5. a human ended the PREPARE item      -> USER_BLOCKED (cancelled) / NEEDS_REVIEW (failed)
   or a preparation waits on a human   -> NEEDS_USER_INPUT / NEEDS_REVIEW
6. same company + title already applied -> DUPLICATE_APPLICATION
7. company cool-down                   -> COOLDOWN_ACTIVE
8. caps paused                         -> DAILY/WEEKLY_CAP_REACHED
9. admissible                          -> ADMITTED (subject to a slot and the window)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.core.timeutils import from_db
from app.intelligence.services.role_relevance import RELEVANCE_RANK, RoleAssessment
from app.jobs.geography import GeoAssessment
from app.pipeline.database.models import (
    ApplicationQueueRow,
    CandidateOpportunityRow,
    OpportunityRow,
)
from app.pipeline.gates import GateReport
from app.pipeline.models import (
    AdmissionReason,
    ApplicationPolicy,
    DuplicatePolicy,
    EligibilityDecision,
    FitBand,
    Lane,
    OpportunityState,
    OpportunityStatus,
    QueueState,
    TailoringLevel,
)
from app.pipeline.policy import evaluate_admission
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import PreparationStatus
from app.scheduler.attempts import (
    CooldownIndex,
    duplicate_key,
    holds_slot,
    is_in_flight,
    is_submitted,
)


@dataclass(frozen=True)
class Verdict:
    code: AdmissionReason
    reason: str
    lane: Optional[Lane] = None
    level: Optional[TailoringLevel] = None
    queue_item_id: Optional[str] = None
    application_id: Optional[str] = None
    preparation_id: Optional[str] = None
    #: Phase 8b: the Tier-1 gate report when the static gates were evaluated.
    gates: Optional[GateReport] = None

    @property
    def admissible(self) -> bool:
        return self.code is AdmissionReason.ADMITTED


@dataclass(frozen=True)
class Context:
    policy: ApplicationPolicy
    now: datetime  # aware UTC
    cooldown: CooldownIndex
    duplicates: dict[str, set[str]]
    attempts: dict[str, ApplicationRow]  # by opportunity id
    queue_items: dict[str, ApplicationQueueRow]  # PREPARE item by opportunity id
    preparations: dict[str, ApplicationPreparationRow]  # latest by candidate opportunity id
    #: Pre-tenancy rows created through the job-id API, by job id (adopted on admission).
    legacy_attempts: dict[str, ApplicationRow] = field(default_factory=dict)
    #: The canonical posting assessed against the candidate's location preference, by opportunity id.
    geography: dict[str, GeoAssessment] = field(default_factory=dict)
    #: The canonical posting's role-family relevance to the candidate's target roles, by opportunity id.
    relevance: dict[str, RoleAssessment] = field(default_factory=dict)


_FAR_FUTURE = datetime.max


_BAND_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_ELIGIBILITY_RANK = {"ELIGIBLE": 0, "LIKELY": 0, "UNCERTAIN": 1}


def ordering_key(co: CandidateOpportunityRow, opportunity: OpportunityRow, role: Optional[RoleAssessment] = None):
    """Eligibility, role relevance, fit band, then priority desc, earliest deadline, freshest posting, id: total and stable.

    Eligibility and relevance before band (2026-09-14, selection quality): one
    company slot must go to the strongest candidate-role combination, never to
    a weaker role that merely scored or appeared first.

    Band first (2026-09-14, real run): the company cool-down gives one slot per
    company, and priority carries freshness, so a fresh LOW-band "Content
    Creator" was admitted ahead of a MEDIUM-band engineering role at the same
    company and cooled it down. Order still never decides whether.
    """
    deadline = from_db(opportunity.deadline)
    first_seen = from_db(opportunity.first_seen_at)
    return (
        _ELIGIBILITY_RANK.get(co.eligibility_status or "", 2),
        RELEVANCE_RANK.get(role.relevance, 2) if role is not None else 2,
        _BAND_RANK.get(co.fit_band or "", 3),
        -(co.priority_score if co.priority_score is not None else -1),
        deadline.timestamp() if deadline else float("inf"),
        -(first_seen.timestamp() if first_seen else 0.0),
        opportunity.id,
    )


def _reposted_after(opportunity: OpportunityRow, row: ApplicationRow) -> bool:
    reposted = from_db(opportunity.reposted_at)
    applied = from_db(row.submitted_at) or from_db(row.reserved_at) or from_db(row.updated_at)
    return bool(reposted and applied and reposted > applied)


def _attempt_verdict(ctx: Context, co: CandidateOpportunityRow, opportunity: OpportunityRow, row: ApplicationRow) -> Optional[Verdict]:
    """A verdict when an existing attempt settles the matter; ``None`` to continue."""
    item = ctx.queue_items.get(opportunity.id)
    prep = ctx.preparations.get(co.id)
    if is_submitted(row):
        if _reposted_after(opportunity, row):
            if ctx.policy.duplicate_policy is DuplicatePolicy.BLOCK:
                return Verdict(AdmissionReason.DUPLICATE_OPPORTUNITY, "applied before this repost; duplicate_policy=BLOCK", application_id=row.id)
            return None  # ALLOW_REPOST_AFTER_COOLDOWN: fall through to the cool-down check
        return Verdict(AdmissionReason.ALREADY_SUBMITTED, f"attempt {row.status.lower()}", application_id=row.id)
    if holds_slot(row) or is_in_flight(row):
        # Phase 6 attempt states settle the matter before the PREPARE item does.
        if row.status == ApplicationStatus.BLOCKED.value:
            return Verdict(AdmissionReason.NEEDS_REVIEW, f"execution handed off: {row.blocked_reason or row.status_reason or 'blocked'}"[:200], application_id=row.id, preparation_id=row.preparation_id)
        if row.status == ApplicationStatus.NEEDS_USER_INPUT.value:
            return Verdict(AdmissionReason.NEEDS_USER_INPUT, (row.status_reason or "execution needs the candidate")[:200], application_id=row.id, preparation_id=row.preparation_id)
        if row.status == ApplicationStatus.NEEDS_REVIEW.value:
            return Verdict(AdmissionReason.NEEDS_REVIEW, (row.status_reason or "execution needs a human look")[:200], application_id=row.id, preparation_id=row.preparation_id)
        if item is not None and item.state == QueueState.BLOCKED.value:
            return Verdict(AdmissionReason.NEEDS_USER_INPUT, (item.last_error or "blocked")[:200], queue_item_id=item.id, application_id=row.id, preparation_id=prep.id if prep else None)
        if item is not None and item.state == QueueState.NEEDS_REVIEW.value:
            return Verdict(AdmissionReason.NEEDS_REVIEW, (item.last_error or "needs review")[:200], queue_item_id=item.id, application_id=row.id, preparation_id=prep.id if prep else None)
        if row.status == ApplicationStatus.READY.value:
            return Verdict(AdmissionReason.ALREADY_IN_PROGRESS, "ready for execution", queue_item_id=item.id if item else None, application_id=row.id, preparation_id=row.preparation_id)
        state = item.state.lower() if item else row.status.lower()
        return Verdict(AdmissionReason.ALREADY_IN_PROGRESS, f"attempt in progress ({state})", queue_item_id=item.id if item else None, application_id=row.id)
    return None  # released / terminal attempt: may be admitted again


def decide(ctx: Context, co: CandidateOpportunityRow, opportunity: OpportunityRow) -> Verdict:
    policy = ctx.policy
    if co.state == OpportunityState.SKIPPED.value:
        return Verdict(AdmissionReason.USER_BLOCKED, co.skipped_reason or "skipped by candidate")
    if opportunity.status == OpportunityStatus.CLOSED.value or co.state == OpportunityState.CLOSED.value:
        return Verdict(AdmissionReason.OPPORTUNITY_CLOSED, "opening is closed at the source")

    row = ctx.attempts.get(opportunity.id)
    if row is not None:
        settled = _attempt_verdict(ctx, co, opportunity, row)
        if settled is not None:
            return settled
    elif co.state in (OpportunityState.SUBMITTED.value, OpportunityState.VERIFICATION_PENDING.value, OpportunityState.VERIFIED.value, OpportunityState.INTERVIEWING.value, OpportunityState.OFFER.value, OpportunityState.REJECTED.value):
        return Verdict(AdmissionReason.ALREADY_SUBMITTED, f"candidate opportunity is {co.state}")
    elif co.state in (OpportunityState.APPROVED.value, OpportunityState.SUBMITTING.value):
        return Verdict(AdmissionReason.ALREADY_IN_PROGRESS, f"candidate opportunity is {co.state}")

    decision = EligibilityDecision(co.eligibility_status) if co.eligibility_status else None
    band = FitBand(co.fit_band) if co.fit_band else None
    admission = evaluate_admission(policy, decision, band, opportunity.company, co.fit_score, opportunity_status=opportunity.status, candidate_state=co.state, geography=ctx.geography.get(opportunity.id), role=ctx.relevance.get(opportunity.id))
    if not admission.admitted:
        return Verdict(admission.code, admission.reason, gates=admission.gates)

    # A PREPARE item a human ended stays ended until a human re-queues it.
    item = ctx.queue_items.get(opportunity.id)
    if item is not None and item.state == QueueState.CANCELLED.value:
        return Verdict(AdmissionReason.USER_BLOCKED, "PREPARE item cancelled; requeue to reconsider", queue_item_id=item.id, application_id=row.id if row else None)
    if item is not None and item.state == QueueState.FAILED.value:
        return Verdict(AdmissionReason.NEEDS_REVIEW, "preparation failed permanently; requeue to retry", queue_item_id=item.id, application_id=row.id if row else None)

    prep = ctx.preparations.get(co.id)
    if row is None and prep is not None:
        if prep.status == PreparationStatus.NEEDS_USER_INPUT.value:
            return Verdict(AdmissionReason.NEEDS_USER_INPUT, "preparation needs answers from the candidate", preparation_id=prep.id)
        if prep.status == PreparationStatus.NEEDS_REVIEW.value:
            return Verdict(AdmissionReason.NEEDS_REVIEW, "preparation needs a human look", preparation_id=prep.id)

    dup_key = duplicate_key(opportunity.company, opportunity.title)
    others = ctx.duplicates.get(dup_key, set()) - {opportunity.id}
    if others:
        return Verdict(AdmissionReason.DUPLICATE_APPLICATION, f"already applied to the same title at this company ({len(others)} other opening)")

    until = ctx.cooldown.active_until(opportunity.company, ctx.now)
    if until is not None:
        return Verdict(AdmissionReason.COOLDOWN_ACTIVE, f"company cool-down until {until.isoformat()}")

    if policy.daily_cap <= 0:
        return Verdict(AdmissionReason.DAILY_CAP_REACHED, "daily_cap is 0 (paused)")
    if policy.weekly_cap <= 0:
        return Verdict(AdmissionReason.WEEKLY_CAP_REACHED, "weekly_cap is 0 (paused)")

    return Verdict(
        AdmissionReason.ADMITTED,
        admission.reason,
        lane=admission.lane,
        level=admission.tailoring_level,
        application_id=row.id if row else None,
        gates=admission.gates,
    )
