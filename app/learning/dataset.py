"""The versioned learning dataset (``FEATURE_VERSION``): one row per executed
application, as known at ``as_of``.

Temporal rule (no leakage): a row exists only if the attempt existed and
its submission (or uncertain submit) happened at or before ``as_of``, and an
outcome event counts only if CareerOS had *observed* it by ``as_of``
(``outcome_events.observed_at <= as_of``, not just ``event_at``): what the
employer did on Monday but we learned on Friday was not known on Monday.

Evidence rule: labels (responded / interviewed / rejected / assessed) come
only from events whose Phase 10 evidence strength is at least the tenant's
``minimum_evidence`` (MODERATE by default); weaker events are counted in
``evidence_counts`` and ``events_below_threshold`` so the row says what it
ignored. ``evidence_quality`` is the strongest evidence behind the labels.
Retracted or superseded events never count.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.career.database.models import PositioningVariantRow
from app.core.timeutils import db_now, ensure_aware, to_db
from app.execution.database.models import ExecutionRunRow
from app.execution.models import ExecutionStatus, VerificationStatus
from app.jobs.database.models import JobRow
from app.learning.models import EVIDENCE_RANK, EvidenceQuality, LearningRow, OutcomeCompleteness
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.identity import normalize_title_for_identity
from app.pipeline.policy import company_key
from app.preparation.database.models import ApplicationPreparationRow
from app.signals.database.models import OutcomeEventRow
from app.signals.models import OutcomeKind
from app.signals.outcomes import STAGE

_RESPONSE_STAGE = 2  # APPLICATION_RECEIVED and beyond count as "the employer responded"
_INTERVIEW = {OutcomeKind.INTERVIEW_REQUESTED.value, OutcomeKind.INTERVIEW_SCHEDULED.value}
_REVIEW_RUN_STATUSES = {ExecutionStatus.NEEDS_REVIEW.value, ExecutionStatus.VERIFICATION_FAILED.value, ExecutionStatus.HANDOFF.value, ExecutionStatus.NEEDS_USER_INPUT.value}


def _days(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    if later is None or earlier is None:
        return None
    return round(max(0.0, (ensure_aware(later) - ensure_aware(earlier)).total_seconds() / 86400.0), 3)


def build_dataset(db: Session, tenant_id: str, as_of: Optional[datetime] = None, window_days: Optional[int] = None, minimum_evidence: EvidenceQuality = EvidenceQuality.MODERATE) -> list[LearningRow]:
    """Rows for every attempt of ``tenant_id`` executed at or before ``as_of``
    (within the window when given), labelled from evidence observed by ``as_of``."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    cutoff = to_db(ensure_aware(as_of)) if as_of else db_now()
    window_start = cutoff - timedelta(days=window_days) if window_days else None
    threshold = EVIDENCE_RANK[minimum_evidence]

    attempts = (
        db.query(ApplicationRow)
        .filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.created_at <= cutoff)
        .filter((ApplicationRow.submitted_at.isnot(None)) | (ApplicationRow.status == ApplicationStatus.UNCERTAIN.value))
        .all()
    )
    if not attempts:
        return []
    app_ids = [a.id for a in attempts]
    runs_by_app: dict[str, list[ExecutionRunRow]] = {}
    for run in db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == tenant_id, ExecutionRunRow.application_id.in_(app_ids), ExecutionRunRow.started_at <= cutoff).order_by(ExecutionRunRow.run_number.asc()).all():
        runs_by_app.setdefault(run.application_id, []).append(run)
    events_by_app: dict[str, list[OutcomeEventRow]] = {}
    for event in (
        db.query(OutcomeEventRow)
        .filter(OutcomeEventRow.tenant_id == tenant_id, OutcomeEventRow.application_id.in_(app_ids), OutcomeEventRow.observed_at <= cutoff, OutcomeEventRow.retracted.is_(False), OutcomeEventRow.superseded_by_id.is_(None))
        .order_by(OutcomeEventRow.event_at.asc(), OutcomeEventRow.sequence.asc())
        .all()
    ):
        events_by_app.setdefault(event.application_id, []).append(event)
    opp_ids = {a.opportunity_id for a in attempts if a.opportunity_id}
    opps = {o.id: o for o in db.query(OpportunityRow).filter(OpportunityRow.id.in_(opp_ids)).all()} if opp_ids else {}
    job_ids = {a.job_id for a in attempts if a.job_id} | {o.canonical_job_id for o in opps.values() if o.canonical_job_id}
    jobs = {j.id: j for j in db.query(JobRow).filter(JobRow.id.in_(job_ids)).all()} if job_ids else {}
    co_ids = {a.candidate_opportunity_id for a in attempts if a.candidate_opportunity_id}
    cos = {c.id: c for c in db.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.tenant_id == tenant_id, CandidateOpportunityRow.id.in_(co_ids)).all()} if co_ids else {}
    prep_ids = {a.preparation_id for a in attempts if a.preparation_id}
    preps = {p.id: p for p in db.query(ApplicationPreparationRow).filter(ApplicationPreparationRow.tenant_id == tenant_id, ApplicationPreparationRow.id.in_(prep_ids)).all()} if prep_ids else {}
    variant_ids = {p.positioning_variant_id for p in preps.values() if p.positioning_variant_id}
    variants = {v.id: v for v in db.query(PositioningVariantRow).filter(PositioningVariantRow.tenant_id == tenant_id, PositioningVariantRow.id.in_(variant_ids)).all()} if variant_ids else {}

    rows: list[LearningRow] = []
    for attempt in attempts:
        runs = runs_by_app.get(attempt.id, [])
        last_run = runs[-1] if runs else None
        submitted_at = attempt.submitted_at or (last_run.started_at if last_run else None) or attempt.reserved_at or attempt.created_at
        if submitted_at is None or submitted_at > cutoff:
            continue
        if window_start is not None and submitted_at < window_start:
            continue
        opp = opps.get(attempt.opportunity_id) if attempt.opportunity_id else None
        job = jobs.get(attempt.job_id) if attempt.job_id else None
        if job is None and opp is not None and opp.canonical_job_id:
            job = jobs.get(opp.canonical_job_id)
        co = cos.get(attempt.candidate_opportunity_id) if attempt.candidate_opportunity_id else None
        prep = preps.get(attempt.preparation_id) if attempt.preparation_id else None
        variant = variants.get(prep.positioning_variant_id) if prep is not None and prep.positioning_variant_id else None
        company = (opp.company if opp else (job.company if job else "")) or ""
        title = (opp.title if opp else (job.title if job else "")) or ""

        counted: list[OutcomeEventRow] = []
        below = 0
        evidence_counts: dict[str, int] = {}
        for event in events_by_app.get(attempt.id, []):
            evidence_counts[event.evidence] = evidence_counts.get(event.evidence, 0) + 1
            if EVIDENCE_RANK[EvidenceQuality(event.evidence)] >= threshold:
                counted.append(event)
            else:
                below += 1
        best = max((EVIDENCE_RANK[EvidenceQuality(e.evidence)] for e in counted), default=0)
        quality = next((q for q, rank in EVIDENCE_RANK.items() if rank == best), EvidenceQuality.NONE)
        response_events = [e for e in counted if (e.outcome in STAGE and STAGE[OutcomeKind(e.outcome)] >= _RESPONSE_STAGE) or e.outcome == OutcomeKind.REJECTED.value]
        rejection_events = [e for e in counted if e.outcome == OutcomeKind.REJECTED.value]
        interviewed = any(e.outcome in _INTERVIEW for e in counted)
        assessed = any(e.outcome == OutcomeKind.ASSESSMENT_REQUESTED.value for e in counted)
        withdrawn = any(e.outcome == OutcomeKind.WITHDRAWN.value for e in counted)
        first_response = min((e.event_at for e in response_events), default=None)
        first_rejection = min((e.event_at for e in rejection_events), default=None)

        verified = bool(attempt.verified_at and attempt.verified_at <= cutoff) or any(r.verification_status == VerificationStatus.VERIFIED.value and (r.finished_at or r.started_at) <= cutoff for r in runs)
        uncertain = bool(last_run is not None and last_run.status == ExecutionStatus.UNKNOWN.value and not verified) or (attempt.status == ApplicationStatus.UNCERTAIN.value and not verified and last_run is None)
        needs_review = bool(last_run is not None and last_run.status in _REVIEW_RUN_STATUSES)
        if rejection_events or withdrawn:
            completeness = OutcomeCompleteness.TERMINAL
        elif response_events:
            completeness = OutcomeCompleteness.PROGRESS
        elif verified:
            completeness = OutcomeCompleteness.SUBMITTED_ONLY
        else:
            completeness = OutcomeCompleteness.UNVERIFIED

        rows.append(
            LearningRow(
                application_id=attempt.id,
                candidate_opportunity_id=attempt.candidate_opportunity_id,
                opportunity_id=attempt.opportunity_id,
                job_id=job.id if job else attempt.job_id,
                company=company,
                company_key=company_key(company),
                title=title,
                title_key=normalize_title_for_identity(title),
                role_family=variant.role_family if variant is not None else None,
                fit_band=co.fit_band if co else None,
                fit_score=co.fit_score if co else None,
                policy_version=co.admission_policy_version if co else None,
                gate_ruleset_version=co.gate_ruleset_version if co else None,
                source=(job.source if job else "UNKNOWN") or "UNKNOWN",
                tailoring_level=prep.tailoring_level if prep else attempt.tailoring_level,
                lane=prep.lane if prep else attempt.lane,
                cover_letter_mode=prep.cover_letter_mode if prep else None,
                positioning_variant_id=prep.positioning_variant_id if prep else None,
                execution_method=(last_run.executor_kind if last_run else "UNKNOWN") or "UNKNOWN",
                verification_status=last_run.verification_status if last_run else None,
                attempt_status=attempt.status,
                submitted_at=ensure_aware(submitted_at),
                verified=verified,
                uncertain=uncertain,
                execution_needs_review=needs_review,
                responded=bool(response_events),
                interviewed=interviewed,
                rejected=bool(rejection_events),
                assessed=assessed,
                withdrawn=withdrawn,
                days_to_response=_days(first_response, submitted_at),
                days_to_rejection=_days(first_rejection, submitted_at),
                evidence_quality=quality,
                evidence_counts=evidence_counts,
                outcome_completeness=completeness,
                events_counted=len(counted),
                events_below_threshold=below,
                as_of=ensure_aware(cutoff),
            )
        )
    rows.sort(key=lambda r: (r.submitted_at or datetime.min.replace(tzinfo=r.as_of.tzinfo if r.as_of else None), r.application_id))
    return rows


__all__ = ["build_dataset"]
