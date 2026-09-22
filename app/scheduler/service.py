"""The scheduler: policy enforcement, ordering, idempotent enqueueing.

One scheduling run for one tenant:

1. **Load** the policy, every candidate opportunity with its opportunity, the
   tenant's attempts, PREPARE queue items and latest preparations - a fixed
   number of queries whatever the volume.
2. **Reconcile** in-flight attempts with what the queue and preparation
   already did (READY -> attempt READY; closed / skipped / newly blocked ->
   released and the queue item cancelled).
3. **Decide** every candidate opportunity with :func:`decisions.decide`, in
   the deterministic order of :func:`decisions.ordering_key`.
4. **Admit** admissible ones in that order: reserve a cap slot (atomic),
   reserve the attempt, enqueue PREPARE (idempotent), move the candidate
   opportunity to QUEUED - all inside one savepoint per admission, committed
   every ``commit_every`` admissions with a heartbeat on the run row.
5. Optionally **prepare** through Phase 4's queue worker, then reconcile
   again so the run reports what is ready for execution.

The run *window* bounds how many admissions one pass performs. It is a
batching control, not an application cap: everything admissible beyond it
is reported as WINDOW_DEFERRED and admitted by the next run. Nothing here
ever creates a SUBMIT item or calls an adapter.
"""

import logging
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.application.database.models import ApplicationRow
from app.application.engine import SUBMITTED_STATUSES
from app.application.models import ApplicationStatus
from app.career.database.models import AnswerBankEntryRow
from app.career.models import AnswerStatus
from app.career.read_model import load_geography_policy, load_preferences
from app.core.errors import CareerOSError, ConflictError, ValidationFailed
from app.core.timeutils import db_now, from_db, utc_now
from app.intelligence.services.role_relevance import relevance_for_row
from app.jobs.database.models import JobRow
from app.pipeline.database.models import (
    ApplicationQueueRow,
    CandidateOpportunityRow,
    OpportunityRow,
)
from app.pipeline.models import (
    AdmissionReason,
    ApplicationPolicy,
    OpportunityState,
    OpportunityStatus,
    QueueAction,
    QueueState,
)
from app.pipeline.policy import company_key
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import PreparationStatus
from app.scheduler.attempts import AttemptRepository, holds_slot
from app.scheduler.caps import CapLedger, PeriodKeys, period_keys
from app.scheduler.database.models import SchedulerRunRow
from app.scheduler.decisions import Context, Verdict, decide, ordering_key
from app.scheduler.models import (
    PROGRESS_REASONS,
    CapacitySummary,
    CooldownEntry,
    Decision,
    PeriodUsage,
    PreviewResponse,
    SchedulerRun,
    SchedulerRunStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = "scheduler"
#: A RUNNING row whose heartbeat is older than this is treated as dead.
RUN_LEASE_SECONDS = 600
#: Static policy codes: these also refresh ``policy_admitted`` on the row.
_STATIC_CODES = frozenset(
    {
        AdmissionReason.NOT_EVALUATED,
        AdmissionReason.NOT_SCORED,
        AdmissionReason.INELIGIBLE,
        AdmissionReason.BELOW_MINIMUM_ELIGIBILITY,
        AdmissionReason.BAND_DISABLED,
        AdmissionReason.BELOW_FIT_THRESHOLD,
        AdmissionReason.COMPANY_BLOCKED,
    }
)
_QUEUED_FROM = frozenset(
    {
        OpportunityState.ELIGIBLE,
        OpportunityState.UNCERTAIN,
        OpportunityState.SHORTLISTED,
        OpportunityState.PREPARED,
        OpportunityState.IN_REVIEW,
    }
)
SAMPLE_LIMIT = 25


class SchedulerService:
    def __init__(self, db: Session, tenant_id: str, actor: str = DEFAULT_ACTOR):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self.repo = OpportunityRepository(db, tenant_id)
        self.queue = QueueRepository(db, tenant_id)
        self.attempts = AttemptRepository(db, tenant_id)
        self.ledger = CapLedger(db, tenant_id)

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    def _load(self) -> tuple[ApplicationPolicy, list[tuple[CandidateOpportunityRow, OpportunityRow]], Context]:
        policy = PolicyRepository(self.db, self.tenant_id).get()
        candidates = (
            self.db.query(CandidateOpportunityRow)
            .options(joinedload(CandidateOpportunityRow.opportunity))
            .filter(CandidateOpportunityRow.tenant_id == self.tenant_id)
            .all()
        )
        opportunities = {co.opportunity_id: co.opportunity for co in candidates}
        attempts = self.attempts.by_opportunity()
        items = {
            item.opportunity_id: item
            for item in self.db.query(ApplicationQueueRow)
            .filter(ApplicationQueueRow.tenant_id == self.tenant_id, ApplicationQueueRow.action == QueueAction.PREPARE.value)
            .all()
        }
        preparations = self._latest_preparations()
        cooldown, duplicates = self.attempts.build_indexes(attempts, opportunities, candidates, policy.cooldown_days)
        geography, relevance = self._job_signals(opportunities)
        legacy = self.attempts.legacy_by_job([o.canonical_job_id for o in opportunities.values() if o.canonical_job_id])
        ctx = Context(
            policy=policy,
            now=utc_now(),
            cooldown=cooldown,
            duplicates=duplicates,
            attempts=attempts,
            queue_items=items,
            preparations=preparations,
            legacy_attempts=legacy,
            geography=geography,
            relevance=relevance,
        )
        ordered = sorted(((co, co.opportunity) for co in candidates), key=lambda pair: ordering_key(pair[0], pair[1], relevance.get(pair[1].id)))
        return policy, ordered, ctx

    def _job_signals(self, opportunities: dict[str, OpportunityRow]) -> tuple[dict, dict]:
        """Each opportunity's canonical posting against the location preference and the target role families."""
        geography_policy = load_geography_policy(self.db, self.tenant_id)
        preferences = load_preferences(self.db, self.tenant_id)
        wants_roles = preferences is not None and bool(preferences.all_target_roles)
        job_ids = sorted({o.canonical_job_id for o in opportunities.values() if o.canonical_job_id})
        if not job_ids or not (geography_policy.active or wants_roles):
            return {}, {}
        columns = (JobRow.id, JobRow.location, JobRow.locations, JobRow.metadata_, JobRow.title, JobRow.description, JobRow.technologies, JobRow.required_skills)
        jobs: dict = {}
        for start in range(0, len(job_ids), 500):
            jobs.update({row.id: row for row in self.db.query(*columns).filter(JobRow.id.in_(job_ids[start : start + 500])).all()})
        geography, relevance = {}, {}
        for opportunity_id, opportunity in opportunities.items():
            job = jobs.get(opportunity.canonical_job_id)
            if job is None:
                continue
            if geography_policy.active:
                geography[opportunity_id] = geography_policy.assess_job(job.location, job.locations, job.metadata_)
            if wants_roles:
                relevance[opportunity_id] = relevance_for_row(job, preferences)
        return geography, relevance

    def _reindex(self, ctx: Context, ordered) -> Context:
        """Rebuild the cool-down / duplicate indexes after attempts were released."""
        opportunities = {opp.id: opp for _, opp in ordered}
        cooldown, duplicates = self.attempts.build_indexes(
            ctx.attempts, opportunities, [co for co, _ in ordered], ctx.policy.cooldown_days
        )
        return replace(ctx, cooldown=cooldown, duplicates=duplicates)

    def _latest_preparations(self) -> dict[str, ApplicationPreparationRow]:
        latest: dict[str, ApplicationPreparationRow] = {}
        rows = (
            self.db.query(ApplicationPreparationRow)
            .filter(ApplicationPreparationRow.tenant_id == self.tenant_id)
            .order_by(ApplicationPreparationRow.candidate_opportunity_id, ApplicationPreparationRow.version.desc())
            .all()
        )
        for row in rows:
            latest.setdefault(row.candidate_opportunity_id, row)
        return latest

    # ------------------------------------------------------------------ #
    # reconcile
    # ------------------------------------------------------------------ #

    def _reconcile(self, ctx: Context, ordered, run_id: str) -> dict[str, int]:
        """Bring attempts in line with queue / preparation / policy state.

        Runs before deciding so the decisions see the truth, and again after
        preparation so the run reports readiness. Returns counts.
        """
        counts: Counter = Counter()
        blocked = {company_key(c) for c in ctx.policy.blocked_companies}
        by_opportunity = {opp.id: (co, opp) for co, opp in ordered}
        answers_at = self._latest_approved_answer_at()
        for opportunity_id, row in list(ctx.attempts.items()):
            if not holds_slot(row):
                continue
            pair = by_opportunity.get(opportunity_id)
            if pair is None:
                continue
            co, opp = pair
            item = ctx.queue_items.get(opportunity_id)
            prep = ctx.preparations.get(co.id)
            stop = None
            if opp.status == OpportunityStatus.CLOSED.value or co.state == OpportunityState.CLOSED.value:
                stop = ("opportunity closed", ApplicationStatus.CLOSED)
            elif co.state == OpportunityState.SKIPPED.value:
                stop = ("skipped by candidate", ApplicationStatus.CLOSED)
            elif company_key(opp.company) in blocked:
                stop = ("company blocked by policy", ApplicationStatus.CLOSED)
            elif item is not None and item.state == QueueState.FAILED.value:
                stop = ("preparation failed permanently", ApplicationStatus.FAILED)
            elif item is not None and item.state == QueueState.CANCELLED.value:
                stop = ("queue item cancelled", ApplicationStatus.CLOSED)
            elif co.policy_admitted is False and item is not None and item.state == QueueState.BLOCKED.value and (item.last_error or "").startswith("policy: "):
                # Autopilot finding (2026-09-22): the classifier de-admitted CRED
                # "capital partnerships" after its PREPARE item had been blocked by
                # the policy, and nothing ever released the attempt: PREPARING for
                # ever. ``policy_admitted is False`` (never None) keeps in-flight
                # attempts that were simply not re-evaluated untouched.
                stop = ("not admitted by the application policy", ApplicationStatus.CLOSED)
            if stop is not None:
                reason, status = stop
                self.attempts.release(row, status, reason, run_id)
                if item is not None and QueueState(item.state) in (QueueState.PENDING, QueueState.RETRY_WAIT, QueueState.BLOCKED, QueueState.NEEDS_REVIEW):
                    self.queue.cancel(item, self.actor, f"scheduler: {reason}")
                if status is ApplicationStatus.CLOSED and stop[0] == "opportunity closed" and co.state != OpportunityState.CLOSED.value:
                    try:
                        self.repo.transition(co, OpportunityState.CLOSED, self.actor, reason)
                    except ConflictError:
                        pass
                counts["released"] += 1
                continue
            if row.status in (ApplicationStatus.BLOCKED.value, ApplicationStatus.NEEDS_USER_INPUT.value, ApplicationStatus.NEEDS_REVIEW.value, ApplicationStatus.SUBMITTING.value):
                # Execution owns these (Phase 6); the scheduler only counts them.
                counts["needs_user_input" if row.status == ApplicationStatus.NEEDS_USER_INPUT.value else "needs_review"] += 1
                continue
            if row.status in SUBMITTED_STATUSES:
                continue  # submitted / verified / uncertain: nothing to reconcile
            if item is not None and item.state == QueueState.SUCCEEDED.value and prep is not None and prep.status == PreparationStatus.READY.value:
                if row.status in (ApplicationStatus.QUALIFIED.value, ApplicationStatus.PREPARING.value) and self.attempts.mark_ready(row, prep.id, run_id):
                    counts["marked_ready"] += 1
                if row.status == ApplicationStatus.READY.value:
                    counts["ready_for_execution"] += 1
            elif item is not None and item.state in (QueueState.BLOCKED.value, QueueState.NEEDS_REVIEW.value) and prep is not None and prep.status == PreparationStatus.READY.value:
                # First real dry run (2026-09-14): the candidate answered the last
                # required question on the Review page, the preparation became
                # READY, and the parked PREPARE item kept the attempt out of READY
                # for good. The package is settled: close the item the existing way.
                self.queue.resolve(item, self.actor, {"outcome": "READY_FOR_EXECUTION", "preparation_id": prep.id, "preparation_version": prep.version}, "preparation READY after the candidate's answers")
                if row.status in (ApplicationStatus.QUALIFIED.value, ApplicationStatus.PREPARING.value) and self.attempts.mark_ready(row, prep.id, run_id):
                    counts["marked_ready"] += 1
                if row.status == ApplicationStatus.READY.value:
                    counts["ready_for_execution"] += 1
            elif item is not None and item.state == QueueState.BLOCKED.value and self._answers_arrived(item, prep, answers_at):
                # Autopilot finding (2026-09-22): Zeta and Zenoti sat BLOCKED on
                # "needs_user_input" (and their attempts PREPARING) for eight days
                # after the person approved the missing answers in Career Brain,
                # because only answering on the *preparation* page unblocked them.
                # Re-queue so the prepare worker rebuilds the package; the attempt
                # stays PREPARING and the branch above marks it READY afterwards.
                self.queue.requeue(item, self.actor, "answer bank updated since this item was blocked")
                self.attempts.mark_preparing(row, prep.id if prep else None, run_id, "answer_bank_updated")
                counts["reprepare_requeued"] += 1
            elif item is not None and item.state in (QueueState.BLOCKED.value, QueueState.NEEDS_REVIEW.value):
                self.attempts.mark_preparing(row, prep.id if prep else None, run_id, item.state.lower())
                counts["needs_user_input" if item.state == QueueState.BLOCKED.value else "needs_review"] += 1
        return dict(counts)

    def _latest_approved_answer_at(self) -> Optional[datetime]:
        """When this tenant's answer bank last gained an APPROVED entry (None: never)."""
        from sqlalchemy import func

        return (
            self.db.query(func.max(AnswerBankEntryRow.updated_at))
            .filter(
                AnswerBankEntryRow.tenant_id == self.tenant_id,
                AnswerBankEntryRow.status == AnswerStatus.APPROVED.value,
            )
            .scalar()
        )

    @staticmethod
    def _answers_arrived(item: ApplicationQueueRow, prep: Optional[ApplicationPreparationRow], answers_at: Optional[datetime]) -> bool:
        """True when a PREPARE item parked for missing answers can be tried again.

        The comparison against ``item.updated_at`` is the guard: re-queueing
        touches the item, so one bank change re-prepares each item once.
        """
        if answers_at is None:
            return False
        error = item.last_error or ""
        waiting = error.startswith("needs_user_input") or (
            not error.startswith("policy: ") and prep is not None and prep.status == PreparationStatus.NEEDS_USER_INPUT.value
        )
        if not waiting:
            return False
        blocked_at = from_db(item.updated_at)
        return blocked_at is not None and from_db(answers_at) > blocked_at

    # ------------------------------------------------------------------ #
    # preview (no writes)
    # ------------------------------------------------------------------ #

    def preview(self, window: int = 500, limit: int = 200) -> PreviewResponse:
        """What a run would do right now. Reads only; caps are projected."""
        policy, ordered, ctx = self._load()
        keys = period_keys(ctx.now, policy.timezone)
        day_left = max(0, policy.daily_cap - self.ledger.usage("DAY", keys.day.key))
        week_left = max(0, policy.weekly_cap - self.ledger.usage("WEEK", keys.week.key))
        decisions: list[Decision] = []
        by_code: Counter = Counter()
        admissible = would_admit = deferred = 0
        for order, (co, opp) in enumerate(ordered, start=1):
            verdict = decide(ctx, co, opp)
            code = verdict.code
            if verdict.admissible:
                admissible += 1
                if would_admit >= window:
                    code = AdmissionReason.WINDOW_DEFERRED
                    deferred += 1
                elif day_left <= 0:
                    code = AdmissionReason.DAILY_CAP_REACHED
                elif week_left <= 0:
                    code = AdmissionReason.WEEKLY_CAP_REACHED
                else:
                    would_admit += 1
                    day_left -= 1
                    week_left -= 1
            by_code[code.value] += 1
            if len(decisions) < limit:
                decisions.append(self._decision(co, opp, verdict, code, order))
        self.db.rollback()  # belt and braces: nothing above should have written
        return PreviewResponse(
            tenant_id=self.tenant_id,
            policy_version=policy.version,
            window_size=window,
            considered=len(ordered),
            admissible=admissible,
            would_admit=would_admit,
            deferred=deferred,
            by_code=dict(by_code),
            capacity=self.capacity(policy=policy, ctx=ctx),
            decisions=decisions,
        )

    @staticmethod
    def _decision(co: CandidateOpportunityRow, opp: OpportunityRow, verdict: Verdict, code: AdmissionReason, order: int, **extra) -> Decision:
        return Decision(
            candidate_opportunity_id=co.id,
            opportunity_id=opp.id,
            company=opp.company,
            title=opp.title,
            code=code,
            reason=verdict.reason if code is verdict.code else code.value.lower().replace("_", " "),
            admitted=code is AdmissionReason.ADMITTED,
            order=order,
            priority_score=co.priority_score,
            fit_score=co.fit_score,
            fit_band=co.fit_band,
            eligibility=co.eligibility_status,
            state=co.state,
            lane=verdict.lane,
            tailoring_level=verdict.level,
            queue_item_id=extra.get("queue_item_id", verdict.queue_item_id),
            application_id=extra.get("application_id", verdict.application_id),
            preparation_id=verdict.preparation_id,
        )

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def running_run(self) -> Optional[SchedulerRunRow]:
        return (
            self.db.query(SchedulerRunRow)
            .filter(SchedulerRunRow.tenant_id == self.tenant_id, SchedulerRunRow.status == SchedulerRunStatus.RUNNING.value)
            .order_by(SchedulerRunRow.started_at.desc())
            .first()
        )

    def run(
        self,
        trigger: str = "manual",
        window: int = 500,
        prepare: bool = False,
        prepare_limit: int = 100,
        worker_id: Optional[str] = None,
        force: bool = False,
        commit_every: int = 100,
        resumed_from: Optional[str] = None,
    ) -> SchedulerRunRow:
        """One scheduling pass; returns the persisted run record."""
        if window < 1:
            raise ValidationFailed("window must be at least 1")
        self._acquire(force)
        started = time.time()
        policy = PolicyRepository(self.db, self.tenant_id).get()
        run = SchedulerRunRow(
            tenant_id=self.tenant_id,
            status=SchedulerRunStatus.RUNNING.value,
            trigger=trigger[:32],
            actor=self.actor,
            dry_run=False,
            policy_version=policy.version,
            policy_snapshot=policy.model_dump(mode="json", exclude={"updated_at"}),
            window_size=window,
            resumed_from_run_id=resumed_from,
        )
        self.db.add(run)
        self.db.commit()
        run_id = run.id
        counts: Counter = Counter()
        by_reason: Counter = Counter()
        samples: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        prep_report: Optional[dict[str, Any]] = None
        try:
            policy, ordered, ctx = self._load()
            recon = self._reconcile(ctx, ordered, run_id)
            counts["released"] += recon.get("released", 0)
            counts["reprepare_requeued"] += recon.get("reprepare_requeued", 0)
            self.db.commit()
            if recon.get("released"):
                ctx = self._reindex(ctx, ordered)
            keys = period_keys(ctx.now, policy.timezone)
            cap_hit: Optional[AdmissionReason] = None
            pending = 0
            for order, (co, opp) in enumerate(ordered, start=1):
                counts["considered"] += 1
                verdict = decide(ctx, co, opp)
                code = verdict.code
                extra: dict[str, Any] = {}
                if verdict.admissible:
                    counts["admissible"] += 1
                    if counts["admitted"] >= window:
                        code = AdmissionReason.WINDOW_DEFERRED
                    elif cap_hit is not None:
                        code = cap_hit
                    else:
                        try:
                            code, extra = self._admit(ctx, co, opp, verdict, keys, run_id)
                        except ConflictError as exc:
                            code = AdmissionReason.ALREADY_IN_PROGRESS
                            extra = {"note": exc.message}
                        except CareerOSError as exc:
                            errors.append(f"{co.id}: {exc.code}: {exc.message}")
                            continue
                        if code in (AdmissionReason.DAILY_CAP_REACHED, AdmissionReason.WEEKLY_CAP_REACHED):
                            cap_hit = code
                        else:
                            counts["admitted"] += 1
                            counts["enqueued"] += int(extra.get("enqueued", False))
                            counts["requeued"] += int(extra.get("requeued", False))
                            pending += 1
                self._tally(code, counts, by_reason)
                self._sample(samples, code, co, opp, verdict, extra)
                self._stamp(co, code, verdict, run_id, ctx.policy)
                if pending >= commit_every:
                    self._heartbeat(run_id, counts, by_reason)
                    pending = 0
            self._heartbeat(run_id, counts, by_reason)

            if prepare:
                from app.preparation.queue_worker import run_prepare_queue

                prep_report = run_prepare_queue(self.db, self.tenant_id, worker_id or f"scheduler:{run_id[:8]}", limit=prepare_limit)
                # Re-read what the worker changed and settle attempts accordingly.
                _, ordered, ctx = self._load()
                recon = self._reconcile(ctx, ordered, run_id)
                counts["released"] += recon.get("released", 0)
                counts["reprepare_requeued"] += recon.get("reprepare_requeued", 0)
                counts["ready_for_execution"] = recon.get("ready_for_execution", 0)
                counts["needs_user_input"] = max(counts["needs_user_input"], recon.get("needs_user_input", 0))
                counts["needs_review"] = max(counts["needs_review"], recon.get("needs_review", 0))
            else:
                counts["ready_for_execution"] = recon.get("ready_for_execution", 0)
            # Phase 6: READY attempts get their SUBMIT item here (idempotent);
            # the scheduler still never submits anything.
            from app.execution.service import ExecutionService

            counts["execution_enqueued"] += ExecutionService(self.db, self.tenant_id, actor=self.actor).enqueue_ready().get("enqueued", 0)
            self.db.commit()
            status = SchedulerRunStatus.COMPLETED
        except Exception as exc:  # noqa: BLE001 - the run record must say what happened
            logger.exception("Scheduler run %s failed", run_id)
            self.db.rollback()
            errors.append(f"{type(exc).__name__}: {exc}")
            status = SchedulerRunStatus.FAILED
        run = self.db.get(SchedulerRunRow, run_id)
        run.status = status.value
        run.completed_at = db_now()
        run.heartbeat_at = run.completed_at
        run.duration_seconds = round(time.time() - started, 3)
        self._apply_counts(run, counts, by_reason)
        run.samples = samples
        run.preparation = prep_report
        run.errors = errors[:100]
        self.db.commit()
        return run

    def _acquire(self, force: bool) -> None:
        current = self.running_run()
        if current is None:
            return
        heartbeat = from_db(current.heartbeat_at)
        stale = heartbeat is None or (utc_now() - heartbeat).total_seconds() > RUN_LEASE_SECONDS
        if stale or force:
            current.status = SchedulerRunStatus.FAILED.value
            current.completed_at = db_now()
            current.errors = list(current.errors or []) + ["superseded: heartbeat stale" if stale else "superseded: forced"]
            self.db.commit()
            return
        raise ConflictError(
            "A scheduler run is already in progress for this tenant",
            details={"run_id": current.id, "heartbeat_at": current.heartbeat_at.isoformat() if current.heartbeat_at else None},
        )

    def _admit(self, ctx: Context, co: CandidateOpportunityRow, opp: OpportunityRow, verdict: Verdict, keys: PeriodKeys, run_id: str) -> tuple[AdmissionReason, dict[str, Any]]:
        """Reserve slot + attempt, enqueue PREPARE, move the row; one savepoint."""
        policy = ctx.policy
        existing = ctx.attempts.get(opp.id) or ctx.legacy_attempts.get(opp.canonical_job_id or "")
        with self.db.begin_nested():
            attempt, reason = self.attempts.reserve(
                co, opp, keys, policy.daily_cap, policy.weekly_cap, verdict.lane, verdict.level, existing, run_id
            )
            if reason is not None:
                return reason, {}
            item = ctx.queue_items.get(opp.id)
            enqueued = requeued = False
            if item is None:
                item, enqueued = self.queue.enqueue(co, QueueAction.PREPARE, self.actor, lane=verdict.lane, priority=co.priority_score)
            elif item.state == QueueState.SUCCEEDED.value:
                # Prepared earlier (manually or by a released attempt): reuse the
                # READY package if it still is, otherwise prepare again.
                prep = ctx.preparations.get(co.id)
                if prep is not None and prep.status == PreparationStatus.READY.value:
                    self.attempts.mark_ready(attempt, prep.id, run_id)
                else:
                    self.queue.requeue(item, self.actor, f"scheduler run {run_id}: preparation no longer READY", allow_succeeded=True)
                    requeued = True
            # else: an active PREPARE item already exists (e.g. enqueued by hand); keep it.
            co.application_id = attempt.id
            if co.policy_admitted is not True or co.policy_reason != verdict.reason or co.admission_policy_version != policy.version:
                self.repo.record_admission(co, True, verdict.reason, self.actor, policy_version=policy.version, ruleset_version=verdict.gates.ruleset_version if verdict.gates else None, gates=verdict.gates.to_dict() if verdict.gates else None)
            if OpportunityState(co.state) in _QUEUED_FROM:
                self.repo.transition(co, OpportunityState.QUEUED, self.actor, f"scheduler run {run_id}")
            self.repo.record(
                "candidate_opportunity",
                co.id,
                "scheduled",
                self.actor,
                None,
                {"run_id": run_id, "queue_item_id": item.id if item else None, "application_id": attempt.id, "lane": verdict.lane.value, "tailoring_level": verdict.level.value, "policy_version": policy.version, "gate_ruleset_version": verdict.gates.ruleset_version if verdict.gates else None},
                verdict.reason,
            )
        ctx.attempts[opp.id] = attempt
        if item is not None:
            ctx.queue_items[opp.id] = item
        # The company is now in cool-down for the rest of this run as well.
        ctx.cooldown.add(opp.company, attempt.reserved_at, policy.cooldown_days, "in_progress")
        from app.scheduler.attempts import duplicate_key

        ctx.duplicates.setdefault(duplicate_key(opp.company, opp.title), set()).add(opp.id)
        return AdmissionReason.ADMITTED, {
            "enqueued": enqueued,
            "requeued": requeued,
            "queue_item_id": item.id if item else None,
            "application_id": attempt.id,
        }

    def _stamp(self, co: CandidateOpportunityRow, code: AdmissionReason, verdict: Verdict, run_id: str, policy: ApplicationPolicy) -> None:
        reason = (verdict.reason if code is verdict.code else code.value)[:256]
        if co.scheduler_code != code.value or co.scheduler_reason != reason:
            co.scheduler_code = code.value
            co.scheduler_reason = reason
            co.scheduler_decided_at = db_now()
        co.scheduler_run_id = run_id
        if code in _STATIC_CODES and (co.policy_admitted is not False or co.policy_reason != verdict.reason[:256] or co.admission_policy_version != policy.version):
            self.repo.record_admission(co, False, verdict.reason, self.actor, policy_version=policy.version, ruleset_version=verdict.gates.ruleset_version if verdict.gates else None, gates=verdict.gates.to_dict() if verdict.gates else None)

    @staticmethod
    def _tally(code: AdmissionReason, counts: Counter, by_reason: Counter) -> None:
        if code is AdmissionReason.ADMITTED:
            return
        if code is AdmissionReason.WINDOW_DEFERRED:
            counts["deferred"] += 1
        elif code is AdmissionReason.ALREADY_IN_PROGRESS:
            counts["already_queued"] += 1
        elif code is AdmissionReason.ALREADY_SUBMITTED:
            counts["already_completed"] += 1
        elif code is AdmissionReason.NEEDS_REVIEW:
            counts["needs_review"] += 1
        elif code is AdmissionReason.NEEDS_USER_INPUT:
            counts["needs_user_input"] += 1
        else:
            counts["blocked"] += 1
        if code not in PROGRESS_REASONS or code in (AdmissionReason.NEEDS_REVIEW, AdmissionReason.NEEDS_USER_INPUT):
            by_reason[code.value] += 1

    @staticmethod
    def _sample(samples: dict, code: AdmissionReason, co: CandidateOpportunityRow, opp: OpportunityRow, verdict: Verdict, extra: dict) -> None:
        bucket = samples.setdefault(code.value, [])
        if len(bucket) >= SAMPLE_LIMIT:
            return
        bucket.append(
            {
                "candidate_opportunity_id": co.id,
                "opportunity_id": opp.id,
                "company": opp.company,
                "title": opp.title,
                "reason": verdict.reason if code is verdict.code else code.value,
                "priority": co.priority_score,
                "fit": co.fit_score,
                "band": co.fit_band,
                **{k: v for k, v in extra.items() if k in ("queue_item_id", "application_id")},
            }
        )

    def _heartbeat(self, run_id: str, counts: Counter, by_reason: Counter) -> None:
        run = self.db.get(SchedulerRunRow, run_id)
        if run is not None:
            run.heartbeat_at = db_now()
            self._apply_counts(run, counts, by_reason)
        self.db.commit()

    @staticmethod
    def _apply_counts(run: SchedulerRunRow, counts: Counter, by_reason: Counter) -> None:
        for name in (
            "considered", "admissible", "admitted", "enqueued", "requeued", "already_queued", "already_completed",
            "ready_for_execution", "needs_review", "needs_user_input", "released", "reprepare_requeued", "execution_enqueued", "deferred", "blocked",
        ):
            setattr(run, name, int(counts.get(name, 0)))
        run.blocked_by_reason = dict(by_reason)

    # ------------------------------------------------------------------ #
    # status / history / capacity
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> Optional[SchedulerRunRow]:
        return self.db.query(SchedulerRunRow).filter(SchedulerRunRow.tenant_id == self.tenant_id, SchedulerRunRow.id == run_id).first()

    def history(self, limit: int = 20, offset: int = 0) -> tuple[list[SchedulerRunRow], int]:
        query = self.db.query(SchedulerRunRow).filter(SchedulerRunRow.tenant_id == self.tenant_id)
        total = query.count()
        rows = query.order_by(SchedulerRunRow.started_at.desc(), SchedulerRunRow.id.desc()).offset(offset).limit(limit).all()
        return rows, total

    def last_run(self) -> Optional[SchedulerRunRow]:
        return (
            self.db.query(SchedulerRunRow)
            .filter(SchedulerRunRow.tenant_id == self.tenant_id, SchedulerRunRow.status != SchedulerRunStatus.RUNNING.value)
            .order_by(SchedulerRunRow.started_at.desc(), SchedulerRunRow.id.desc())
            .first()
        )

    def ready_for_execution(self) -> list[ApplicationRow]:
        """Attempts whose package is READY: the only execution-ready set."""
        return (
            self.db.query(ApplicationRow)
            .filter(
                ApplicationRow.tenant_id == self.tenant_id,
                ApplicationRow.status == ApplicationStatus.READY.value,
                ApplicationRow.released_at.is_(None),
                ApplicationRow.preparation_id.isnot(None),
            )
            .all()
        )

    def capacity(self, policy: Optional[ApplicationPolicy] = None, ctx: Optional[Context] = None, now: Optional[datetime] = None) -> CapacitySummary:
        policy = policy or PolicyRepository(self.db, self.tenant_id).get()
        now = now or (ctx.now if ctx else utc_now())
        keys = period_keys(now, policy.timezone)
        day_used = self.ledger.usage("DAY", keys.day.key)
        week_used = self.ledger.usage("WEEK", keys.week.key)
        if ctx is None:
            _, ordered, ctx = self._load()
        cooldowns = [
            CooldownEntry(company=company, until=until, source=ctx.cooldown.source.get(company, "unknown"))
            for company, until in sorted(ctx.cooldown.until.items())
            if until > now
        ]
        last = self.last_run()
        running = self.running_run()
        return CapacitySummary(
            tenant_id=self.tenant_id,
            policy_version=policy.version,
            timezone=policy.timezone,
            now_local=keys.now_local,
            day=PeriodUsage(kind="DAY", key=keys.day.key, cap=policy.daily_cap, used=day_used, remaining=max(0, policy.daily_cap - day_used), starts_at=keys.day.starts_at, ends_at=keys.day.ends_at),
            week=PeriodUsage(kind="WEEK", key=keys.week.key, cap=policy.weekly_cap, used=week_used, remaining=max(0, policy.weekly_cap - week_used), starts_at=keys.week.starts_at, ends_at=keys.week.ends_at),
            paused=policy.daily_cap <= 0 or policy.weekly_cap <= 0,
            cooldown_days=policy.cooldown_days,
            cooldowns_active=len(cooldowns),
            cooldowns=cooldowns[:50],
            blocked_companies=list(policy.blocked_companies),
            attempts_by_status=self.attempts.counts_by_status(),
            queue_by_state=self.queue.counts_by_state(),
            ready_for_execution=len(self.ready_for_execution()),
            last_run=SchedulerRun.model_validate(last) if last else None,
            running=SchedulerRun.model_validate(running) if running else None,
        )
