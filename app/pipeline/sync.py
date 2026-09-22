"""Bridge Phase 3 matches into candidate opportunities.

``run_matching`` still scores jobs exactly as before and writes
``job_matches``. This module reads those rows and, per tenant:

1. resolves the job's shared ``Opportunity``;
2. ensures the tenant's ``CandidateOpportunity``;
3. records a Tier 1 ``EligibilityDecision`` (mapped from the match's
   eligibility status and reasons — no re-evaluation, no new rules);
4. links the Tier 2 fit (score + policy band);
5. computes and records the deterministic priority;
6. records policy admission (informational; enqueueing is Phase 5).

Everything is idempotent per ``(match, tenant)``: re-syncing an unchanged
match writes no new audit events.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.career.read_model import load_geography_policy, load_preferences
from app.core.timeutils import from_db, utc_now
from app.intelligence.database.models import JobMatchRow
from app.intelligence.models.enums import EligibilityStatus
from app.intelligence.services.match_persistence import latest_matches_query
from app.intelligence.services.role_relevance import relevance_for_row
from app.jobs.database.models import JobRow
from app.jobs.geography import GeographyPolicy
from app.pipeline.models import ApplicationPolicy, EligibilityDecision
from app.pipeline.policy import band_for, evaluate_admission
from app.pipeline.priority import PriorityInputs, compute_priority
from app.pipeline.repository import OpportunityRepository, PolicyRepository

logger = logging.getLogger(__name__)

SYNC_ACTOR = "pipeline-sync"
#: Bumped when the mapping from Phase 3 status to a Tier 1 decision changes.
TIER1_RULESET_VERSION = "tier1-from-match-v1"

_DECISION_FOR_STATUS = {
    EligibilityStatus.ELIGIBLE: EligibilityDecision.ELIGIBLE,
    EligibilityStatus.LIKELY_ELIGIBLE: EligibilityDecision.LIKELY,
    EligibilityStatus.UNCERTAIN: EligibilityDecision.UNCERTAIN,
    EligibilityStatus.INELIGIBLE: EligibilityDecision.INELIGIBLE,
}


@dataclass
class SyncReport:
    tenant_id: str
    run_id: Optional[str]
    synced: int = 0
    opportunities_created: int = 0
    candidate_created: int = 0
    admitted: int = 0
    not_admitted: int = 0
    errors: list[str] = field(default_factory=list)


def _constraint(name: str, reason: str) -> dict:
    return {"constraint": name, "reason": reason}


def sync_match(
    db: Session,
    tenant_id: str,
    match: JobMatchRow,
    policy: Optional[ApplicationPolicy] = None,
    actor: str = SYNC_ACTOR,
    learned=None,
    geography_policy: Optional[GeographyPolicy] = None,
    preferences=None,
) -> tuple[OpportunityRepository, "CandidateOpportunitySyncResult"]:
    repo = OpportunityRepository(db, tenant_id)
    policy = policy or PolicyRepository(db, tenant_id).get()
    geography_policy = geography_policy or load_geography_policy(db, tenant_id)
    preferences = preferences if preferences is not None else load_preferences(db, tenant_id)
    job = db.get(JobRow, match.job_id)
    if job is None:
        raise ValueError(f"Job {match.job_id} for match {match.id} no longer exists")

    opportunity, opp_created, _ = OpportunityRepository.resolve_opportunity(db, job)
    co, co_created = repo.ensure_candidate_opportunity(opportunity, actor)

    status = EligibilityStatus(match.eligibility_status)
    decision = _DECISION_FOR_STATUS[status]
    latest = repo.latest_decision(co.id)
    if latest is None or latest.job_content_hash != job.content_hash or latest.decision != decision.value:
        repo.record_eligibility(
            co,
            job,
            decision,
            confidence=match.eligibility_confidence or "UNKNOWN",
            reason_codes=_reason_codes(match),
            matched=[_constraint("gate", r) for r in (match.eligibility_reasons or [])],
            failed=[_constraint("gate", r) for r in (match.blocking_reasons or [])],
            uncertain=[_constraint("gate", r) for r in (match.uncertainties or [])],
            ruleset_version=TIER1_RULESET_VERSION,
            actor=actor,
        )
    elif co.eligibility_decision_id != latest.id:
        co.eligibility_decision_id = latest.id
        co.eligibility_status = latest.decision

    band = band_for(match.fit_score, policy.band_thresholds)
    repo.record_fit(co, match, band, actor, policy_version=policy.version)

    # Phase 11: the learned ordering signal feeds the pre-existing learned_prior
    # component only when the candidate opted in; otherwise None = neutral, as before.
    expected = None
    if learned is None and policy.learning_settings.ordering_enabled:
        from app.learning.engine import LearnedPrior

        learned = LearnedPrior(db, tenant_id, policy.learning_settings)
    if learned is not None:
        expected = learned.score(source=job.source, company=opportunity.company, title=opportunity.title, fit_band=band.value if band else None, candidate_opportunity_id=co.id)
    priority = compute_priority(
        PriorityInputs(
            fit_score=match.fit_score,
            posted_at=from_db(job.posted_at),
            first_seen_at=from_db(job.first_seen_at),
            deadline=from_db(job.deadline) or from_db(opportunity.deadline),
            source=job.source,
            learned_prior=expected.score if expected is not None else None,
            now=utc_now(),
        ),
        policy.priority_weights,
    )
    # Re-record when the learned component appeared, moved or disappeared, even if
    # the rounded score did not change, so the stored components always say
    # whether (and from which snapshot) a learned signal was in play.
    previous = repo.latest_priority(co.id) if co.priority_score_id else None
    previous_components = (previous.components or {}) if previous is not None else {}
    if expected is not None:
        priority.components["learned"] = {"snapshot_id": expected.snapshot_id, "ordering_version": expected.ordering_version, "learning_version": expected.learning_version, "explanation": expected.explanation[:300]}
        learned_changed = previous is None or previous_components.get("learned_prior") != priority.components["learned_prior"]
    else:
        learned_changed = "learned" in previous_components
    if co.priority_score != priority.score or co.priority_score_id is None or learned_changed:
        repo.record_priority(co, priority, actor)

    geography = geography_policy.assess_job(job.location, job.locations, job.metadata_) if geography_policy.active else None
    role = relevance_for_row(job, preferences) if preferences is not None and preferences.all_target_roles else None
    admission = evaluate_admission(policy, decision, band, opportunity.company, match.fit_score, opportunity_status=opportunity.status, candidate_state=co.state, geography=geography, role=role)
    repo.record_admission(
        co,
        admission.admitted,
        admission.reason,
        actor,
        policy_version=policy.version,
        ruleset_version=admission.ruleset_version,
        gates=admission.gates.to_dict() if admission.gates else None,
    )
    db.flush()
    return repo, CandidateOpportunitySyncResult(co, opp_created, co_created, admission.admitted)


@dataclass
class CandidateOpportunitySyncResult:
    candidate_opportunity: object
    opportunity_created: bool
    candidate_created: bool
    admitted: bool


def _reason_codes(match: JobMatchRow) -> list[str]:
    codes = []
    for reason in match.blocking_reasons or []:
        codes.append(f"blocked:{_code(reason)}")
    for reason in match.uncertainties or []:
        codes.append(f"uncertain:{_code(reason)}")
    for reason in match.eligibility_reasons or []:
        codes.append(f"ok:{_code(reason)}")
    return codes[:32]


def _code(reason: str) -> str:
    head = (reason or "").split(":")[0].strip().lower()
    return "-".join(head.split())[:48] or "unspecified"


def sync_run(db: Session, tenant_id: str, run_id: Optional[str] = None, actor: str = SYNC_ACTOR) -> SyncReport:
    """Sync every match of ``run_id`` (default: latest completed run) for one tenant."""
    query = latest_matches_query(db, run_id)
    report = SyncReport(tenant_id=tenant_id, run_id=run_id)
    if query is None:
        return report
    policy = PolicyRepository(db, tenant_id).get()
    geography_policy = load_geography_policy(db, tenant_id)
    preferences = load_preferences(db, tenant_id)
    matches = query.all()
    report.run_id = matches[0].run_id if matches else run_id
    learned = None
    if policy.learning_settings.ordering_enabled:
        # One snapshot index for the whole run (Phase 11 opt-in ordering signal).
        from app.learning.engine import LearnedPrior

        learned = LearnedPrior(db, tenant_id, policy.learning_settings)
    for match in matches:
        try:
            with db.begin_nested():
                _, result = sync_match(db, tenant_id, match, policy, actor, learned=learned, geography_policy=geography_policy, preferences=preferences)
            report.synced += 1
            report.opportunities_created += int(result.opportunity_created)
            report.candidate_created += int(result.candidate_created)
            if result.admitted:
                report.admitted += 1
            else:
                report.not_admitted += 1
        except Exception as exc:  # noqa: BLE001 - one bad match must not stop the sync
            logger.exception("Opportunity sync failed for match %s", match.id)
            report.errors.append(f"match {match.id}: {type(exc).__name__}: {exc}")
    db.commit()
    logger.info(
        "Opportunity sync for tenant %s run %s: %d synced, %d admitted, %d not admitted, %d errors",
        tenant_id,
        report.run_id,
        report.synced,
        report.admitted,
        report.not_admitted,
        len(report.errors),
    )
    return report
