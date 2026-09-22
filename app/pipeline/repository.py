"""Tenant-bound persistence for opportunities, decisions, priority and policy.

Follows the Phase 1 conventions: the repository is bound to one tenant,
callers commit, every mutation names an actor and meaningful ones write an
``audit_events`` row (the same ledger the Career Brain uses).

Shared opportunity rows are resolved through :meth:`resolve_opportunity`;
that part is deliberately tenant-agnostic because the market is shared.
"""

import logging
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.career.database.models import AuditEventRow
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now, from_db, ordered_db_now, utc_now
from app.intelligence.database.models import JobMatchRow
from app.jobs.database.models import JobRow
from app.pipeline.database.models import (
    ApplicationPolicyRow,
    CandidateOpportunityRow,
    EligibilityDecisionRow,
    OpportunityJobRow,
    OpportunityRow,
    PriorityScoreRow,
)
from app.pipeline.identity import identity_for_job
from app.pipeline.models import (
    ALLOWED_TRANSITIONS,
    EVALUATION_STATES,
    ApplicationPolicy,
    ApplicationPolicyUpdate,
    EligibilityDecision,
    FitBand,
    OpportunityState,
    OpportunityStatus,
)
from app.pipeline.policy import (
    DEFAULT_BAND_THRESHOLDS,
    DEFAULT_COVER_LETTERS,
    DEFAULT_ENABLED_BANDS,
    DEFAULT_LANES,
    DEFAULT_TAILORING,
    VALID_COVER_LETTER_MODES,
    company_key,
    policy_from_row,
)
from app.pipeline.priority import PriorityResult

logger = logging.getLogger(__name__)

#: Decision the sync writes when it maps Phase 3's status.
_STATE_FOR_DECISION = {
    EligibilityDecision.ELIGIBLE: OpportunityState.ELIGIBLE,
    EligibilityDecision.LIKELY: OpportunityState.ELIGIBLE,
    EligibilityDecision.UNCERTAIN: OpportunityState.UNCERTAIN,
    EligibilityDecision.REVIEW: OpportunityState.UNCERTAIN,
    EligibilityDecision.INELIGIBLE: OpportunityState.INELIGIBLE,
}


def _iso(value) -> Optional[str]:
    aware = from_db(value)
    return aware.isoformat() if aware else None


class OpportunityRepository:
    def __init__(self, db: Session, tenant_id: str):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------------ #
    # audit (shared ledger)
    # ------------------------------------------------------------------ #

    def record(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> AuditEventRow:
        event = AuditEventRow(
            tenant_id=self.tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor or "system",
            before=before,
            after=after,
            summary=(summary or "")[:512] or None,
            created_at=ordered_db_now(),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def list_audit(self, entity_type: Optional[str] = None, entity_id: Optional[str] = None, limit: int = 100):
        query = self.db.query(AuditEventRow).filter(AuditEventRow.tenant_id == self.tenant_id)
        if entity_type:
            query = query.filter(AuditEventRow.entity_type == entity_type)
        if entity_id:
            query = query.filter(AuditEventRow.entity_id == entity_id)
        return query.order_by(AuditEventRow.created_at.desc(), AuditEventRow.id.desc()).limit(limit).all()

    # ------------------------------------------------------------------ #
    # shared opportunity identity
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_opportunity(
        db: Session,
        job: JobRow,
        link: Optional[OpportunityJobRow] = None,
        assume_unlinked: bool = False,
    ) -> tuple[OpportunityRow, bool, str]:
        """Attach ``job`` to its opportunity, creating one if none matches.

        Returns ``(opportunity, created, linked_by)``. Resolution order, exact
        equality only: (1) the job row is already linked, (2) identity key
        (company | normalised title | location bucket). A job that arrives
        for an opportunity whose previous rows are all closed is a repost.

        ``link`` may be supplied by a caller that prefetched it (the discovery
        loop does, in batches); ``assume_unlinked`` says the job row was just
        created so no link can exist. Otherwise it is looked up here.
        """
        if link is None and not assume_unlinked:
            link = db.query(OpportunityJobRow).filter(OpportunityJobRow.job_id == job.id).first()
        if link is not None:
            opportunity = link.opportunity
            now = db_now()
            opportunity.last_seen_at = now
            if not opportunity.company_key:
                opportunity.company_key = company_key(opportunity.company)
            if job.deadline and not opportunity.deadline:
                opportunity.deadline = job.deadline
            how = "existing_link"
            if job.job_status not in ("CLOSED", "EXPIRED") and opportunity.status == OpportunityStatus.CLOSED.value:
                # The opening we had closed is live again on the same row: a repost.
                opportunity.status = OpportunityStatus.REPOSTED.value
                opportunity.repost_count += 1
                opportunity.reposted_at = now
                link.is_repost = True
                how = "repost"
            db.flush()
            return opportunity, False, how

        identity = identity_for_job(job)
        opportunity = db.query(OpportunityRow).filter(OpportunityRow.identity_key == identity.key).first()
        now = db_now()
        if opportunity is None:
            opportunity = OpportunityRow(
                identity_key=identity.key,
                canonical_job_id=job.id,
                company=job.company,
                company_key=company_key(job.company),
                title=job.title,
                location_bucket=identity.location_bucket,
                status=OpportunityStatus.OPEN.value,
                first_seen_at=job.first_seen_at or now,
                last_seen_at=now,
                deadline=job.deadline,
            )
            db.add(opportunity)
            db.flush()
            db.add(OpportunityJobRow(opportunity_id=opportunity.id, job_id=job.id, linked_by="new"))
            db.flush()
            return opportunity, True, "new"

        is_repost = all(
            existing.job.job_status in ("CLOSED", "EXPIRED")
            for existing in _links_with_jobs(db, opportunity.id)
        )
        db.add(
            OpportunityJobRow(
                opportunity_id=opportunity.id,
                job_id=job.id,
                linked_by="identity_key",
                is_repost=is_repost,
            )
        )
        opportunity.last_seen_at = now
        if not opportunity.company_key:
            opportunity.company_key = company_key(opportunity.company)
        if job.deadline:
            opportunity.deadline = job.deadline
        if is_repost:
            opportunity.repost_count += 1
            opportunity.reposted_at = now
            opportunity.status = OpportunityStatus.REPOSTED.value
            opportunity.canonical_job_id = job.id
        elif opportunity.status == OpportunityStatus.CLOSED.value:
            opportunity.status = OpportunityStatus.OPEN.value
        db.flush()
        return opportunity, False, "repost" if is_repost else "identity_key"

    @staticmethod
    def close_opportunity_if_all_jobs_closed(db: Session, opportunity: OpportunityRow) -> bool:
        links = _links_with_jobs(db, opportunity.id)
        if links and all(link.job.job_status in ("CLOSED", "EXPIRED") for link in links):
            if opportunity.status != OpportunityStatus.CLOSED.value:
                opportunity.status = OpportunityStatus.CLOSED.value
                db.flush()
            return True
        return False

    def get_opportunity(self, opportunity_id: str) -> Optional[OpportunityRow]:
        return self.db.get(OpportunityRow, opportunity_id)

    def opportunity_job_ids(self, opportunity_id: str) -> list[str]:
        return [
            link.job_id
            for link in self.db.query(OpportunityJobRow)
            .filter(OpportunityJobRow.opportunity_id == opportunity_id)
            .order_by(OpportunityJobRow.linked_at)
            .all()
        ]

    # ------------------------------------------------------------------ #
    # candidate opportunities
    # ------------------------------------------------------------------ #

    @staticmethod
    def co_snapshot(row: CandidateOpportunityRow) -> dict[str, Any]:
        return {
            "state": row.state,
            "eligibility_status": row.eligibility_status,
            "fit_score": row.fit_score,
            "fit_band": row.fit_band,
            "priority_score": row.priority_score,
            "application_id": row.application_id,
            "policy_admitted": row.policy_admitted,
            "policy_reason": row.policy_reason,
            "skipped_reason": row.skipped_reason,
            "fit_policy_version": row.fit_policy_version,
            "admission_policy_version": row.admission_policy_version,
            "gate_ruleset_version": row.gate_ruleset_version,
        }

    def _co_query(self):
        return self.db.query(CandidateOpportunityRow).filter(
            CandidateOpportunityRow.tenant_id == self.tenant_id
        )

    def get_candidate_opportunity(self, co_id: str) -> Optional[CandidateOpportunityRow]:
        return self._co_query().filter(CandidateOpportunityRow.id == co_id).first()

    def require_candidate_opportunity(self, co_id: str) -> CandidateOpportunityRow:
        row = self.get_candidate_opportunity(co_id)
        if row is None:
            raise NotFoundError(f"Candidate opportunity not found: {co_id}")
        return row

    def for_opportunity(self, opportunity_id: str) -> Optional[CandidateOpportunityRow]:
        return self._co_query().filter(CandidateOpportunityRow.opportunity_id == opportunity_id).first()

    def ensure_candidate_opportunity(self, opportunity: OpportunityRow, actor: str) -> tuple[CandidateOpportunityRow, bool]:
        row = self.for_opportunity(opportunity.id)
        if row is not None:
            return row, False
        row = CandidateOpportunityRow(
            tenant_id=self.tenant_id,
            opportunity_id=opportunity.id,
            state=OpportunityState.DISCOVERED.value,
        )
        self.db.add(row)
        self.db.flush()
        self.record("candidate_opportunity", row.id, "created", actor, None, self.co_snapshot(row))
        return row, True

    def ensure_candidate_opportunities(self, opportunity_ids: list[str], actor: str) -> int:
        """Bulk DISCOVERED rows for opportunities this tenant does not track yet.

        Inserts only (one existence query + one insert per missing row): the
        cheap post-ingestion projection. Eligibility, fit and priority are
        deliberately *not* computed here.
        """
        if not opportunity_ids:
            return 0
        self.ensure_tenant()
        existing = {
            row.opportunity_id
            for row in self._co_query()
            .filter(CandidateOpportunityRow.opportunity_id.in_(list(opportunity_ids)))
            .all()
        }
        created = 0
        for opportunity_id in opportunity_ids:
            if opportunity_id in existing:
                continue
            row = CandidateOpportunityRow(
                tenant_id=self.tenant_id,
                opportunity_id=opportunity_id,
                state=OpportunityState.DISCOVERED.value,
            )
            self.db.add(row)
            created += 1
        self.db.flush()
        if created:
            self.record(
                "candidate_opportunity",
                "bulk",
                "projected",
                actor,
                None,
                {"created": created, "requested": len(opportunity_ids)},
            )
        return created

    def ensure_tenant(self) -> None:
        from app.career.database.models import TenantRow

        if self.db.get(TenantRow, self.tenant_id) is None:
            self.db.add(TenantRow(id=self.tenant_id, name=self.tenant_id))
            self.db.flush()

    def list_candidate_opportunities(
        self,
        state: Optional[OpportunityState] = None,
        fit_band: Optional[FitBand] = None,
        eligibility: Optional[EligibilityDecision] = None,
        admitted: Optional[bool] = None,
        min_priority: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> tuple[list[CandidateOpportunityRow], int]:
        query = self._co_query()
        if search and search.strip():
            # Plain substring match on company / title (desktop control center);
            # a filter, never a ranking or a cap on how many may be applied to.
            needle = f"%{search.strip()}%"
            query = query.join(OpportunityRow, OpportunityRow.id == CandidateOpportunityRow.opportunity_id).filter(
                (OpportunityRow.company.ilike(needle)) | (OpportunityRow.title.ilike(needle))
            )
        if state is not None:
            query = query.filter(CandidateOpportunityRow.state == state.value)
        if fit_band is not None:
            query = query.filter(CandidateOpportunityRow.fit_band == fit_band.value)
        if eligibility is not None:
            query = query.filter(CandidateOpportunityRow.eligibility_status == eligibility.value)
        if admitted is not None:
            query = query.filter(CandidateOpportunityRow.policy_admitted.is_(admitted))
        if min_priority is not None:
            query = query.filter(CandidateOpportunityRow.priority_score >= min_priority)
        total = query.count()
        rows = (
            # Performance audit (2026-09-14): pages read co.opportunity per row (one SELECT each).
            query.options(selectinload(CandidateOpportunityRow.opportunity))
            .order_by(
                CandidateOpportunityRow.priority_score.desc().nulls_last(),
                CandidateOpportunityRow.fit_score.desc().nulls_last(),
                CandidateOpportunityRow.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total

    def counts_by_state(self) -> dict[str, int]:
        rows = (
            self.db.query(CandidateOpportunityRow.state, func.count(CandidateOpportunityRow.id))
            .filter(CandidateOpportunityRow.tenant_id == self.tenant_id)
            .group_by(CandidateOpportunityRow.state)
            .all()
        )
        return {state: count for state, count in rows}

    def transition(
        self,
        row: CandidateOpportunityRow,
        new_state: OpportunityState,
        actor: str,
        reason: Optional[str] = None,
        force: bool = False,
    ) -> CandidateOpportunityRow:
        current = OpportunityState(row.state)
        if current is new_state:
            return row
        if not force and new_state not in ALLOWED_TRANSITIONS[current]:
            raise ConflictError(
                f"Illegal opportunity transition {current.value} -> {new_state.value}",
                details={"from": current.value, "to": new_state.value},
            )
        before = self.co_snapshot(row)
        row.state = new_state.value
        row.state_changed_at = db_now()
        if new_state is OpportunityState.SKIPPED:
            row.skipped_reason = (reason or "")[:256] or None
        elif current is OpportunityState.SKIPPED:
            row.skipped_reason = None
        self.db.flush()
        self.record("candidate_opportunity", row.id, f"state:{new_state.value}", actor, before, self.co_snapshot(row), reason)
        return row

    # ------------------------------------------------------------------ #
    # eligibility decisions (Tier 1)
    # ------------------------------------------------------------------ #

    def record_eligibility(
        self,
        row: CandidateOpportunityRow,
        job: JobRow,
        decision: EligibilityDecision,
        confidence: str,
        reason_codes: list[str],
        matched: list[dict[str, Any]],
        failed: list[dict[str, Any]],
        uncertain: list[dict[str, Any]],
        ruleset_version: str,
        actor: str,
    ) -> EligibilityDecisionRow:
        decision_row = EligibilityDecisionRow(
            tenant_id=self.tenant_id,
            candidate_opportunity_id=row.id,
            job_id=job.id,
            job_content_hash=job.content_hash,
            decision=decision.value,
            confidence=confidence,
            reason_codes=list(reason_codes),
            matched_constraints=list(matched),
            failed_constraints=list(failed),
            uncertain_constraints=list(uncertain),
            ruleset_version=ruleset_version,
            evaluated_at=ordered_db_now(),
        )
        self.db.add(decision_row)
        self.db.flush()

        before = self.co_snapshot(row)
        row.eligibility_status = decision.value
        row.eligibility_decision_id = decision_row.id
        row.last_evaluated_at = db_now()
        target = _STATE_FOR_DECISION[decision]
        state_changed = False
        if OpportunityState(row.state) in EVALUATION_STATES and row.state != target.value:
            row.state = target.value
            row.state_changed_at = db_now()
            state_changed = True
        self.db.flush()
        if state_changed or before["eligibility_status"] != decision.value:
            self.record(
                "candidate_opportunity",
                row.id,
                f"eligibility:{decision.value}",
                actor,
                before,
                self.co_snapshot(row),
                ", ".join(reason_codes)[:512],
            )
        return decision_row

    def latest_decision(self, co_id: str) -> Optional[EligibilityDecisionRow]:
        return (
            self.db.query(EligibilityDecisionRow)
            .filter(
                EligibilityDecisionRow.tenant_id == self.tenant_id,
                EligibilityDecisionRow.candidate_opportunity_id == co_id,
            )
            .order_by(EligibilityDecisionRow.evaluated_at.desc(), EligibilityDecisionRow.id.desc())
            .first()
        )

    def decisions_for(self, co_id: str, limit: int = 20) -> list[EligibilityDecisionRow]:
        return (
            self.db.query(EligibilityDecisionRow)
            .filter(
                EligibilityDecisionRow.tenant_id == self.tenant_id,
                EligibilityDecisionRow.candidate_opportunity_id == co_id,
            )
            .order_by(EligibilityDecisionRow.evaluated_at.desc(), EligibilityDecisionRow.id.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------ #
    # fit (Tier 2) — persisted by Phase 3; linked here
    # ------------------------------------------------------------------ #

    def record_fit(self, row: CandidateOpportunityRow, match: JobMatchRow, band: Optional[FitBand], actor: str, policy_version: Optional[int] = None) -> None:
        """Link the Tier 2 fit. The stored match is never touched; only the
        band derived from it under ``policy_version`` (Phase 8b) is recorded."""
        before = self.co_snapshot(row)
        row.match_id = match.id
        row.fit_score = match.fit_score
        row.fit_band = band.value if band else None
        if policy_version is not None:
            row.fit_policy_version = policy_version
        row.last_evaluated_at = db_now()
        self.db.flush()
        if before["fit_score"] != row.fit_score or before["fit_band"] != row.fit_band or before["fit_policy_version"] != row.fit_policy_version:
            self.record("candidate_opportunity", row.id, "fit", actor, before, self.co_snapshot(row))

    def match_for(self, row: CandidateOpportunityRow) -> Optional[JobMatchRow]:
        if not row.match_id:
            return None
        return self.db.get(JobMatchRow, row.match_id)

    # ------------------------------------------------------------------ #
    # priority
    # ------------------------------------------------------------------ #

    def record_priority(self, row: CandidateOpportunityRow, result: PriorityResult, actor: str) -> PriorityScoreRow:
        score_row = PriorityScoreRow(
            tenant_id=self.tenant_id,
            candidate_opportunity_id=row.id,
            score=result.score,
            components={**result.components, "weights": result.weights},
            weights_version=result.weights_version,
            computed_at=ordered_db_now(),
        )
        self.db.add(score_row)
        self.db.flush()
        before = self.co_snapshot(row)
        row.priority_score = result.score
        row.priority_score_id = score_row.id
        self.db.flush()
        if before["priority_score"] != result.score:
            self.record("candidate_opportunity", row.id, "priority", actor, before, self.co_snapshot(row))
        return score_row

    def latest_priority(self, co_id: str) -> Optional[PriorityScoreRow]:
        return (
            self.db.query(PriorityScoreRow)
            .filter(
                PriorityScoreRow.tenant_id == self.tenant_id,
                PriorityScoreRow.candidate_opportunity_id == co_id,
            )
            .order_by(PriorityScoreRow.computed_at.desc(), PriorityScoreRow.id.desc())
            .first()
        )

    def record_admission(
        self,
        row: CandidateOpportunityRow,
        admitted: bool,
        reason: str,
        actor: str,
        policy_version: Optional[int] = None,
        ruleset_version: Optional[str] = None,
        gates: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record the static (Tier-1 gate) admission verdict with the versions
        it was derived under. The audit event carries the full gate report."""
        before = self.co_snapshot(row)
        row.policy_admitted = admitted
        row.policy_reason = reason[:256]
        if policy_version is not None:
            row.admission_policy_version = policy_version
        if ruleset_version is not None:
            row.gate_ruleset_version = ruleset_version
        self.db.flush()
        after = self.co_snapshot(row)
        if before["policy_admitted"] != admitted or before["policy_reason"] != row.policy_reason or before["admission_policy_version"] != row.admission_policy_version or before["gate_ruleset_version"] != row.gate_ruleset_version:
            if gates:
                after = {**after, "gates": gates}
            self.record("candidate_opportunity", row.id, "admission", actor, before, after, reason)

    # ------------------------------------------------------------------ #
    # cool-down helper (used by later phases; deterministic and cheap)
    # ------------------------------------------------------------------ #

    def company_in_cooldown(self, company: str, cooldown_days: int) -> bool:
        """True when this tenant applied to ``company`` within the window.

        Phase 5 semantics: the company identity is normalised
        (``company_key``), the window starts at the submission (or at the
        admission of an attempt that is still in flight), and the boundary is
        inclusive at the end: exactly ``cooldown_days`` later is allowed.
        """
        if cooldown_days <= 0:
            return False
        from app.application.database.models import ApplicationRow
        from app.scheduler.attempts import AttemptRepository

        key = company_key(company)
        candidates = (
            self._co_query()
            .join(OpportunityRow, OpportunityRow.id == CandidateOpportunityRow.opportunity_id)
            .all()
        )
        opportunities = {co.opportunity_id: co.opportunity for co in candidates}
        opportunities = {oid: opp for oid, opp in opportunities.items() if company_key(opp.company) == key}
        if not opportunities:
            return False
        attempts = {
            row.opportunity_id: row
            for row in self.db.query(ApplicationRow)
            .filter(ApplicationRow.tenant_id == self.tenant_id, ApplicationRow.opportunity_id.in_(list(opportunities)))
            .all()
        }
        index, _ = AttemptRepository(self.db, self.tenant_id).build_indexes(
            attempts, opportunities, [co for co in candidates if co.opportunity_id in opportunities], cooldown_days
        )
        return index.active_until(company, utc_now()) is not None


def _links_with_jobs(db: Session, opportunity_id: str) -> list[OpportunityJobRow]:
    return (
        db.query(OpportunityJobRow)
        .join(JobRow, JobRow.id == OpportunityJobRow.job_id)
        .filter(OpportunityJobRow.opportunity_id == opportunity_id)
        .all()
    )


class PolicyRepository:
    """One ``application_policies`` row per tenant, created with aggressive defaults."""

    def __init__(self, db: Session, tenant_id: str):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id

    def commit(self) -> None:
        self.db.commit()

    @staticmethod
    def snapshot(row: ApplicationPolicyRow) -> dict[str, Any]:
        return {
            "enabled_bands": list(row.enabled_bands or []),
            "band_thresholds": dict(row.band_thresholds or {}),
            "daily_cap": row.daily_cap,
            "weekly_cap": row.weekly_cap,
            "tailoring_by_band": dict(row.tailoring_by_band or {}),
            "lane_by_band": dict(row.lane_by_band or {}),
            "cover_letter_by_band": dict(row.cover_letter_by_band or {}),
            "cooldown_days": row.cooldown_days,
            "blocked_companies": list(row.blocked_companies or []),
            "preferred_locations": list(row.preferred_locations or []),
            "preferred_role_families": list(row.preferred_role_families or []),
            "minimum_eligibility": row.minimum_eligibility,
            "duplicate_policy": row.duplicate_policy,
            "priority_weights": dict(row.priority_weights or {}),
            "minimum_fit_score": row.minimum_fit_score,
            "timezone": row.timezone or "UTC",
            "ai_settings": dict(row.ai_settings or {}),
            "learning_settings": dict(row.learning_settings or {}),
            "version": row.version,
        }

    def get_row(self) -> Optional[ApplicationPolicyRow]:
        return self.db.query(ApplicationPolicyRow).filter(ApplicationPolicyRow.tenant_id == self.tenant_id).first()

    def get_or_create(self, actor: str = "system") -> ApplicationPolicyRow:
        row = self.get_row()
        if row is not None:
            return row
        from app.career.database.models import TenantRow

        if self.db.get(TenantRow, self.tenant_id) is None:
            self.db.add(TenantRow(id=self.tenant_id, name=self.tenant_id))
            self.db.flush()
        row = ApplicationPolicyRow(
            tenant_id=self.tenant_id,
            enabled_bands=list(DEFAULT_ENABLED_BANDS),
            band_thresholds=dict(DEFAULT_BAND_THRESHOLDS),
            tailoring_by_band=dict(DEFAULT_TAILORING),
            lane_by_band=dict(DEFAULT_LANES),
            cover_letter_by_band=dict(DEFAULT_COVER_LETTERS),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            # Phase 12: another worker created the tenant's policy between our
            # read and insert (unique tenant_id); use theirs.
            existing = self.get_row()
            if existing is None:
                raise
            return existing
        OpportunityRepository(self.db, self.tenant_id).record(
            "application_policy", row.id, "created", actor, None, self.snapshot(row)
        )
        return row

    def get(self) -> ApplicationPolicy:
        return policy_from_row(self.get_or_create())

    def update(self, data: ApplicationPolicyUpdate, actor: str) -> ApplicationPolicy:
        row = self.get_or_create(actor)
        before = self.snapshot(row)
        changes = data.model_dump(exclude_unset=True, mode="json")
        if "band_thresholds" in changes and changes["band_thresholds"] is not None:
            thresholds = {k.upper(): int(v) for k, v in changes["band_thresholds"].items()}
            if thresholds.get("HIGH", 70) <= thresholds.get("MEDIUM", 45):
                raise ValidationFailed("HIGH threshold must be greater than MEDIUM threshold")
            changes["band_thresholds"] = thresholds
        if "enabled_bands" in changes and changes["enabled_bands"] is not None:
            changes["enabled_bands"] = [FitBand(b).value for b in changes["enabled_bands"]]
        if changes.get("timezone"):
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(changes["timezone"])
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValidationFailed(f"Unknown timezone: {changes['timezone']}") from exc
        if "cover_letter_by_band" in changes and changes["cover_letter_by_band"] is not None:
            modes = {k.upper(): str(v).upper() for k, v in changes["cover_letter_by_band"].items()}
            bad = [v for v in modes.values() if v not in VALID_COVER_LETTER_MODES]
            if bad:
                raise ValidationFailed(f"Unknown cover letter mode(s): {', '.join(bad)}")
            changes["cover_letter_by_band"] = modes
        if "ai_settings" in changes and changes["ai_settings"] is not None:
            from app.ai.models import TenantAISettings
            from app.ai.providers import KNOWN_PROVIDERS

            merged = {**(row.ai_settings or {}), **{k: v for k, v in changes["ai_settings"].items()}}
            validated = TenantAISettings(**merged)
            if validated.provider and validated.provider.strip().lower() not in KNOWN_PROVIDERS:
                raise ValidationFailed(f"Unknown AI provider: {validated.provider}")
            changes["ai_settings"] = validated.model_dump(mode="json")
        if "learning_settings" in changes and changes["learning_settings"] is not None:
            from app.learning.models import TenantLearningSettings

            merged = {**(row.learning_settings or {}), **{k: v for k, v in changes["learning_settings"].items()}}
            changes["learning_settings"] = TenantLearningSettings(**merged).model_dump(mode="json")
        for key, value in changes.items():
            if value is None:
                continue
            setattr(row, key, value)
        self.db.flush()
        after = self.snapshot(row)
        if {k: v for k, v in after.items() if k != "version"} == {k: v for k, v in before.items() if k != "version"}:
            return policy_from_row(row)
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        OpportunityRepository(self.db, self.tenant_id).record(
            "application_policy", row.id, "updated", actor, before, self.snapshot(row)
        )
        return policy_from_row(row)
