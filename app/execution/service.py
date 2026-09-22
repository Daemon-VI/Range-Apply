"""ExecutionService: the guarded, idempotent submission workflow.

State ownership
---------------
* ``applications`` (attempt): lifecycle READY -> SUBMITTING -> SUBMITTED ->
  VERIFIED, with BLOCKED / NEEDS_USER_INPUT / NEEDS_REVIEW / UNCERTAIN /
  FAILED / CANCELLED branches (``ATTEMPT_TRANSITIONS``).
* ``application_queue`` SUBMIT item: claim / lease / retry / park state.
* ``application_preparations``: readiness of the material (READY or not).
* ``execution_runs``: one executor invocation and its result.

Idempotency
-----------
One logical attempt may press submit at most once. ``start`` flips the
attempt READY -> SUBMITTING with a *guarded UPDATE* (``WHERE status='READY'
AND submission_key IS NULL``); the loser of a race sees zero rows. Before
the executor may act, ``submit_invoked`` is committed on the run row: a crash
afterwards is UNKNOWN (routed to verification), never "not submitted". An
attempt in UNCERTAIN can only leave through verification or a human.

Nothing here talks to a browser or the network; executors do, behind the
:class:`Executor` contract, and the MOCK one does neither.
"""

import logging
import re
from collections import Counter
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.application.database.models import ApplicationRow
from app.application.engine import SUBMITTED_STATUSES
from app.application.killswitch import is_paused
from app.application.models import ApplicationStatus
from app.config import settings
from app.core.errors import CareerOSError, ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now, to_db
from app.execution import submission_mode
from app.execution.database.models import ExecutionRunRow, FormFieldRow, FormSnapshotRow
from app.execution.executors.base import Executor, ExecutorError
from app.execution.executors.manual import ManualExecutor
from app.execution.executors.mock import MockExecutor
from app.execution.forms import blocking, form_fingerprint, map_fields
from app.execution.models import (
    ApplicationAttempt,
    ErrorClass,
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionPreview,
    ExecutionResult,
    ExecutionStatus,
    ExecutionSummary,
    ExecutorKind,
    FieldAnswer,
    FieldAnswerSource,
    FieldAnswerStatus,
    FormSnapshot,
    FormSnapshotRecord,
    HandoffReason,
    PreconditionCode,
    PreconditionReport,
    VerificationResult,
    VerificationStatus,
    sanitize_diagnostics,
)
from app.execution.package import (
    build_package,
    load_answer_bank,
    load_preparation,
    load_profile_facts,
)
from app.execution.preconditions import PreconditionChecker
from app.jobs.database.models import JobRow
from app.pipeline.database.models import (
    ApplicationQueueRow,
    CandidateOpportunityRow,
    OpportunityRow,
)
from app.pipeline.models import Lane, OpportunityState, QueueAction, QueueState
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.preparation.models import PreparationStatus
from app.preparation.service import PreparationService
from app.scheduler.attempts import AttemptRepository

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = "execution"
_PARKED = (QueueState.BLOCKED.value, QueueState.NEEDS_REVIEW.value, QueueState.FAILED.value, QueueState.CANCELLED.value)
_ATTEMPT_FOR_OUTCOME = {
    ExecutionOutcome.HANDOFF: ApplicationStatus.BLOCKED,
    ExecutionOutcome.NEEDS_USER_INPUT: ApplicationStatus.NEEDS_USER_INPUT,
    ExecutionOutcome.NEEDS_REVIEW: ApplicationStatus.NEEDS_REVIEW,
    ExecutionOutcome.FORM_CHANGED: ApplicationStatus.NEEDS_REVIEW,
}


def _normalize_url(url: Optional[str]) -> str:
    if not url:
        return ""
    text = url.strip().lower()
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("#", 1)[0].split("?", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def default_registry() -> dict[ExecutorKind, Executor]:
    from app.execution.executors.extension import BrowserExtensionExecutor

    registry: dict[ExecutorKind, Executor] = {ExecutorKind.MANUAL: ManualExecutor(), ExecutorKind.BROWSER_EXTENSION: BrowserExtensionExecutor()}
    if settings.execution_mock_enabled or settings.app_env.lower() == "test":
        registry[ExecutorKind.MOCK] = MockExecutor()
    from app.execution.playwright import is_available

    if is_available():
        # Lazy: the browser is launched on first use, never at import or request time.
        from app.execution.playwright.executor import PlaywrightExecutor

        registry[ExecutorKind.PLAYWRIGHT_LOCAL] = PlaywrightExecutor()
    return registry


class ExecutionService:
    def __init__(self, db: Session, tenant_id: str, actor: str = DEFAULT_ACTOR, executors: Optional[dict[ExecutorKind, Executor]] = None):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self.executors = executors if executors is not None else default_registry()
        self.repo = OpportunityRepository(db, tenant_id)
        self.queue = QueueRepository(db, tenant_id)
        self.attempts = AttemptRepository(db, tenant_id)
        self._policy = None
        self._prep_service: Optional[PreparationService] = None
        self._bank: Optional[list] = None
        self._profile: Optional[dict] = None
        self._document_service = None

    @property
    def policy(self):
        if self._policy is None:
            self._policy = PolicyRepository(self.db, self.tenant_id).get()
        return self._policy

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def _attempts(self):
        return self.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == self.tenant_id)

    def get_attempt(self, application_id: str) -> Optional[ApplicationRow]:
        return self._attempts().filter(ApplicationRow.id == application_id).first()

    def require_attempt(self, application_id: str) -> ApplicationRow:
        row = self.get_attempt(application_id)
        if row is None:
            raise NotFoundError(f"Application attempt not found: {application_id}")
        return row

    def ready(self, limit: int = 500) -> list[ApplicationRow]:
        return (
            self._attempts()
            .filter(ApplicationRow.status == ApplicationStatus.READY.value, ApplicationRow.released_at.is_(None), ApplicationRow.preparation_id.isnot(None))
            .order_by(ApplicationRow.reserved_at.asc(), ApplicationRow.id.asc())
            .limit(limit)
            .all()
        )

    def list_attempts(self, status: Optional[ApplicationStatus] = None, limit: int = 200, offset: int = 0) -> tuple[list[ApplicationRow], int]:
        query = self._attempts()
        if status is not None:
            query = query.filter(ApplicationRow.status == status.value)
        total = query.count()
        rows = query.order_by(ApplicationRow.updated_at.desc(), ApplicationRow.id.asc()).offset(offset).limit(limit).all()
        return rows, total

    def get_run(self, run_id: str) -> Optional[ExecutionRunRow]:
        return self.db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.id == run_id).first()

    def require_run(self, run_id: str) -> ExecutionRunRow:
        row = self.get_run(run_id)
        if row is None:
            raise NotFoundError(f"Execution run not found: {run_id}")
        return row

    def runs_for(self, application_id: str) -> list[ExecutionRunRow]:
        return (
            self.db.query(ExecutionRunRow)
            .filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.application_id == application_id)
            .order_by(ExecutionRunRow.run_number.desc())
            .all()
        )

    def list_runs(self, status: Optional[ExecutionStatus] = None, limit: int = 100, offset: int = 0) -> tuple[list[ExecutionRunRow], int]:
        query = self.db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == self.tenant_id)
        if status is not None:
            query = query.filter(ExecutionRunRow.status == status.value)
        total = query.count()
        rows = query.order_by(ExecutionRunRow.started_at.desc(), ExecutionRunRow.id.desc()).offset(offset).limit(limit).all()
        return rows, total

    def package(self, attempt: ApplicationRow) -> ExecutionPackage:
        if self._bank is None:
            self._bank = load_answer_bank(self.db, self.tenant_id)
        if self._profile is None:
            self._profile = load_profile_facts(self.db, self.tenant_id)
        return build_package(self.db, self.tenant_id, attempt, bank=self._bank, profile=self._profile, execution_config=self._execution_config(attempt))

    def _forget_cached_inputs(self) -> None:
        self._bank = None
        self._profile = None
        self._policy = None

    def _execution_config(self, attempt: ApplicationRow) -> dict[str, Any]:
        return {
            "lane": attempt.lane,
            "tailoring_level": attempt.tailoring_level,
            "duplicate_policy": self.policy.duplicate_policy.value,
            "cover_letter_by_band": dict(self.policy.cover_letter_by_band),
            "executor_hint": None,
            # Real local documents the executor may upload (Phase 4 artifacts are text).
            "artifact_files": {k: v for k, v in (("RESUME", settings.execution_resume_file), ("COVER_LETTER", settings.execution_cover_letter_file)) if v},
            "dry_run": settings.playwright_dry_run,
        }

    def _context(self, attempt: ApplicationRow):
        preparation = load_preparation(self.db, self.tenant_id, attempt.preparation_id) if attempt.preparation_id else None
        co = self.db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id) if attempt.candidate_opportunity_id else None
        opportunity = self.db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
        return preparation, co, opportunity

    @property
    def preparations(self) -> PreparationService:
        """One PreparationService per ExecutionService: the evidence snapshot it
        holds is loaded once, not once per attempt. ``run_queue`` refreshes it."""
        if self._prep_service is None:
            self._prep_service = PreparationService(self.db, self.tenant_id, actor=self.actor)
        return self._prep_service

    def _stale_inputs(self, preparation) -> list[str]:
        if preparation is None or preparation.status != PreparationStatus.READY.value:
            return []
        try:
            return self.preparations.stale_inputs(preparation)
        except CareerOSError as exc:
            logger.warning("Could not recompute preparation inputs for %s: %s", preparation.id, exc.message)
            return ["unavailable"]

    def preconditions(self, attempt: ApplicationRow, reserve: bool = False) -> PreconditionReport:
        preparation, co, opportunity = self._context(attempt)
        checker = PreconditionChecker(self.db, self.tenant_id, self.policy)
        return checker.check(attempt, preparation, co, opportunity, stale_inputs=self._stale_inputs(preparation), reserve=reserve)

    def preview(self, application_id: str) -> ExecutionPreview:
        """Package, preconditions and (if a form was captured) the field mapping. Reads only."""
        attempt = self.require_attempt(application_id)
        report = self.preconditions(attempt, reserve=False)
        package = self.package(attempt) if attempt.preparation_id else None
        if package is None:
            raise ValidationFailed("Attempt has no preparation attached")
        snapshot = self._latest_snapshot(attempt.id)
        answers: list[FieldAnswer] = []
        if snapshot is not None:
            answers = [self._answer_from_row(f) for f in snapshot.fields]
        needs_input, needs_review = blocking(answers)
        self.db.rollback()
        return ExecutionPreview(
            package=package,
            preconditions=report,
            attempt=ApplicationAttempt.model_validate(attempt),
            form=FormSnapshotRecord.model_validate(snapshot) if snapshot else None,
            field_answers=answers,
            blocking_fields=needs_input + needs_review,
        )

    # ------------------------------------------------------------------ #
    # queue integration
    # ------------------------------------------------------------------ #

    def enqueue_ready(self, limit: Optional[int] = None) -> dict[str, int]:
        """Idempotently create SUBMIT queue items for READY attempts."""
        counts: Counter = Counter()
        ready = self.ready(limit or 10000)
        if not ready:
            return dict(counts)
        cos = {
            co.id: co
            for co in self.db.query(CandidateOpportunityRow)
            .filter(CandidateOpportunityRow.tenant_id == self.tenant_id, CandidateOpportunityRow.id.in_([a.candidate_opportunity_id for a in ready if a.candidate_opportunity_id]))
            .all()
        }
        for attempt in ready:
            co = cos.get(attempt.candidate_opportunity_id or "")
            if co is None:
                counts["no_candidate_opportunity"] += 1
                continue
            existing = self.queue.find(attempt.opportunity_id, QueueAction.SUBMIT)
            if existing is not None:
                counts["already_queued" if existing.state not in _PARKED else "parked"] += 1
                continue
            lane = Lane(attempt.lane) if attempt.lane else Lane.REVIEW
            self.queue.enqueue(co, QueueAction.SUBMIT, self.actor, lane=lane, priority=co.priority_score, max_attempts=settings.execution_max_attempts)
            counts["enqueued"] += 1
        self.db.flush()
        return dict(counts)

    def claim(self, worker_id: str, limit: int = 1, lane: Optional[Lane] = None) -> list[ApplicationQueueRow]:
        items = self.queue.claim(worker_id, action=QueueAction.SUBMIT, lane=lane, limit=limit, lease_seconds=settings.execution_lease_seconds)
        self.db.commit()
        return items

    def item_for(self, attempt: ApplicationRow) -> Optional[ApplicationQueueRow]:
        return self.queue.find(attempt.opportunity_id, QueueAction.SUBMIT) if attempt.opportunity_id else None

    def _attempt_for_item(self, item: ApplicationQueueRow) -> Optional[ApplicationRow]:
        if item.tenant_id != self.tenant_id:
            raise NotFoundError("Queue item not found in this tenant")
        return self._attempts().filter(ApplicationRow.opportunity_id == item.opportunity_id).first()

    # ------------------------------------------------------------------ #
    # start: guarded entry into execution
    # ------------------------------------------------------------------ #

    def start(self, item: ApplicationQueueRow, worker_id: str, executor_kind: ExecutorKind) -> tuple[Optional[ExecutionRunRow], Optional[ExecutionPackage], dict[str, Any]]:
        """Begin execution for a claimed SUBMIT item.

        Returns ``(run, package, outcome)``; ``run`` is ``None`` when the item
        was settled without executing (already submitted, awaiting
        verification, preconditions failed, parked). Everything is committed.
        """
        attempt = self._attempt_for_item(item)  # tenant check first
        if item.state == QueueState.CLAIMED.value:
            self.queue.start(item, worker_id)
        elif item.state != QueueState.PROCESSING.value or item.claimed_by != worker_id:
            raise ConflictError(f"Item is {item.state} and owned by {item.claimed_by or 'nobody'}")
        if attempt is None:
            self.queue.fail(item, worker_id, "no application attempt for this opportunity", retryable=False)
            self.db.commit()
            return None, None, {"outcome": "no_attempt"}

        # ---- idempotency: what happened before? ---------------------------
        if attempt.status in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value):
            self.queue.succeed(item, worker_id, {"outcome": "already_submitted", "application_id": attempt.id})
            self.db.commit()
            return None, None, {"outcome": "already_submitted"}
        if attempt.status == ApplicationStatus.UNCERTAIN.value:
            self.queue.needs_review(item, worker_id, "awaiting verification of an earlier submit; never resubmitted automatically")
            self.db.commit()
            return None, None, {"outcome": "awaiting_verification"}
        if attempt.status == ApplicationStatus.SUBMITTING.value:
            # A previous executor crashed or lost its lease mid-run.
            crashed = self._crashed_run(attempt)
            if crashed is not None and crashed.submit_invoked:
                self._finish_run(crashed, ExecutionStatus.UNKNOWN, ExecutionOutcome.UNKNOWN, error_class=ErrorClass.EXECUTOR_CRASH, message="executor lost after submit was invoked")
                self.attempts.transition(attempt, ApplicationStatus.UNCERTAIN, self.actor, "executor lost after submit was invoked", force=True)
                self.queue.needs_review(item, worker_id, "awaiting verification: earlier executor was lost after pressing submit")
                self._audit(attempt, "execution:unknown", {"run_id": crashed.id, "reason": "lost after submit"})
                self.db.commit()
                return None, None, {"outcome": "awaiting_verification"}
            if crashed is not None:
                self._finish_run(crashed, ExecutionStatus.FAILED_RETRYABLE, ExecutionOutcome.RETRYABLE_FAILURE, error_class=ErrorClass.EXECUTOR_CRASH, message="executor lost before submit; safe to retry")
            attempt.submission_key = None
            self.attempts.transition(attempt, ApplicationStatus.READY, self.actor, "reclaimed after a lost executor", force=True)
            self.db.flush()
        if attempt.status != ApplicationStatus.READY.value:
            self._park_item(item, worker_id, attempt.status, attempt.status_reason or attempt.status)
            self.db.commit()
            return None, None, {"outcome": "not_ready", "status": attempt.status}

        # ---- kill switch (global or per source): wait, do not fail ----------
        source = None
        if attempt.job_id:
            from app.jobs.database.models import JobRow

            job = self.db.get(JobRow, attempt.job_id)
            source = job.source if job else None
        if is_paused(self.db, source):
            self.queue.release(item, worker_id, "kill switch paused")
            item.available_at = db_now() + timedelta(minutes=10)
            self._audit(attempt, "execution:paused", {"source": source})
            self.db.commit()
            return None, None, {"outcome": "paused"}

        # ---- preconditions (with cap reservation) -------------------------
        preparation, co, opportunity = self._context(attempt)
        stale = self._stale_inputs(preparation)
        checker = PreconditionChecker(self.db, self.tenant_id, self.policy)
        with self.db.begin_nested():
            report = checker.check(attempt, preparation, co, opportunity, stale_inputs=stale, reserve=True)
        if not report.ok:
            self._settle_preconditions(item, worker_id, attempt, preparation, report)
            self.db.commit()
            return None, None, {"outcome": "precondition_failed", "codes": [f.code.value for f in report.failures]}

        executor = self.executors.get(executor_kind)
        if executor is None:
            self.queue.release(item, worker_id, f"no executor registered for {executor_kind.value}")
            self.db.commit()
            raise ValidationFailed(f"No executor registered for {executor_kind.value}")

        # ---- documents (Phase 8): the exact files this run may upload -------
        documents = self._documents_for(preparation)
        if documents.problems:
            message = "documents: " + "; ".join(documents.problems)[:1500]
            self.attempts.transition(attempt, ApplicationStatus.NEEDS_REVIEW, self.actor, message[:256], force=True)
            self.queue.needs_review(item, worker_id, message)
            self._audit(attempt, "execution:document_failed", {"problems": documents.problems[:5]})
            self.db.commit()
            return None, None, {"outcome": "document_failed", "problems": documents.problems}
        package = self.package(attempt)
        package.execution_config["artifact_files"] = {a.artifact_type: self._document_path(a.id) for a in documents.artifacts}
        package.execution_config["artifacts"] = {a.artifact_type: {"id": a.id, "version": a.version, "sha256": a.content_hash, "format": a.format.value, "bytes": a.byte_size} for a in documents.artifacts}
        if not executor.can_handle(package.target):
            self._handoff(item, worker_id, attempt, None, HandoffReason.AMBIGUOUS_FORM, f"{executor_kind.value} cannot handle {package.target.ats_family.value}", stopped_at="before_form", remaining=["choose another executor"])
            self.db.commit()
            return None, None, {"outcome": "handoff"}

        # ---- the mutex: READY -> SUBMITTING by guarded UPDATE -------------
        run_number = (attempt.execution_count or 0) + 1
        key = f"{self.tenant_id}:{attempt.id}:{attempt.attempt_number}:{run_number}"
        updated = (
            self.db.query(ApplicationRow)
            .filter(ApplicationRow.id == attempt.id, ApplicationRow.status == ApplicationStatus.READY.value, ApplicationRow.submission_key.is_(None))
            .update(
                {
                    ApplicationRow.status: ApplicationStatus.SUBMITTING.value,
                    ApplicationRow.submission_key: key,
                    ApplicationRow.execution_count: run_number,
                    ApplicationRow.status_reason: f"executing with {executor.kind.value}",
                    ApplicationRow.updated_at: db_now(),
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            self.db.rollback()
            item = self.queue.require(item.id)
            self.queue.release(item, worker_id, "another worker took this attempt")
            self.db.commit()
            return None, None, {"outcome": "lost_race"}
        self.db.refresh(attempt)
        by_type = {a.artifact_type: a for a in documents.artifacts}
        run = ExecutionRunRow(
            tenant_id=self.tenant_id,
            application_id=attempt.id,
            preparation_id=attempt.preparation_id,
            candidate_opportunity_id=attempt.candidate_opportunity_id,
            opportunity_id=attempt.opportunity_id,
            queue_item_id=item.id,
            executor_kind=executor.kind.value,
            executor_version=executor.version,
            worker_id=worker_id,
            idempotency_key=key,
            run_number=run_number,
            status=ExecutionStatus.RUNNING.value,
            source_url=package.target.canonical_url,
            preconditions=[f.model_dump(mode="json") for f in report.failures],
            resume_artifact_id=by_type["RESUME"].id if "RESUME" in by_type else None,
            cover_letter_artifact_id=by_type["COVER_LETTER"].id if "COVER_LETTER" in by_type else None,
            diagnostics={"artifacts": package.execution_config.get("artifacts", {})},
        )
        self.db.add(run)
        self.db.flush()
        attempt.last_execution_id = run.id
        self.attempts._event(attempt, "attempt:submitting", ApplicationStatus.READY.value, ApplicationStatus.SUBMITTING.value, {"run_id": run.id, "executor": executor.kind.value, "worker": worker_id})
        self._audit(attempt, "execution:started", {"run_id": run.id, "executor": executor.kind.value, "worker_id": worker_id})
        self.db.commit()
        return run, package, {"outcome": "started"}

    def _crashed_run(self, attempt: ApplicationRow) -> Optional[ExecutionRunRow]:
        return (
            self.db.query(ExecutionRunRow)
            .filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.application_id == attempt.id, ExecutionRunRow.status == ExecutionStatus.RUNNING.value)
            .order_by(ExecutionRunRow.run_number.desc())
            .first()
        )

    # ------------------------------------------------------------------ #
    # recovery (Phase 12): runs a dead worker left RUNNING
    # ------------------------------------------------------------------ #

    def recover_lost_runs(self) -> dict[str, int]:
        """Settle RUNNING runs whose worker is gone (queue lease lapsed or the
        item is no longer held), the same way ``start`` would when the item is
        next reclaimed, but without waiting for a claim:

        * ``submit_invoked`` → run UNKNOWN, attempt UNCERTAIN, item NEEDS_REVIEW
          (a person or verification settles it; never resubmitted);
        * otherwise → run FAILED_RETRYABLE, attempt back to READY, item PENDING
          (the click provably did not happen; a retry is safe).

        Runs whose item still holds a live lease are left alone: the worker may
        be mid-page. Everything is committed; counts are returned.
        """
        counts: Counter = Counter()
        now = db_now()
        runs = self.db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.status == ExecutionStatus.RUNNING.value).all()
        for run in runs:
            item = self.queue.get(run.queue_item_id) if run.queue_item_id else None
            live = item is not None and item.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value) and item.lease_expires_at is not None and item.lease_expires_at >= now
            if live:
                counts["live"] += 1
                continue
            attempt = self.get_attempt(run.application_id)
            if attempt is None:
                self._finish_run(run, ExecutionStatus.FAILED_PERMANENT, ExecutionOutcome.PERMANENT_FAILURE, error_class=ErrorClass.EXECUTOR_CRASH, message="attempt vanished while running")
                counts["orphaned"] += 1
                continue
            if run.submit_invoked:
                self._finish_run(run, ExecutionStatus.UNKNOWN, ExecutionOutcome.UNKNOWN, error_class=ErrorClass.EXECUTOR_CRASH, message="worker lost after submit was invoked (recovered at startup)")
                run.verification_status = VerificationStatus.PENDING.value
                if attempt.status != ApplicationStatus.UNCERTAIN.value:
                    self.attempts.transition(attempt, ApplicationStatus.UNCERTAIN, self.actor, "worker lost after submit was invoked", force=True)
                if item is not None and item.state not in (QueueState.SUCCEEDED.value, QueueState.CANCELLED.value, QueueState.NEEDS_REVIEW.value):
                    self.queue.needs_review(item, self.actor, "awaiting verification: worker was lost after pressing submit")
                self._audit(attempt, "execution:unknown", {"run_id": run.id, "reason": "recovered: lost after submit"})
                self._emit_signal(run, attempt)
                counts["uncertain"] += 1
            else:
                self._finish_run(run, ExecutionStatus.FAILED_RETRYABLE, ExecutionOutcome.RETRYABLE_FAILURE, error_class=ErrorClass.EXECUTOR_CRASH, message="worker lost before submit (recovered at startup); safe to retry")
                if attempt.status == ApplicationStatus.SUBMITTING.value:
                    attempt.submission_key = None
                    self.attempts.transition(attempt, ApplicationStatus.READY, self.actor, "reclaimed after a lost worker", force=True)
                if item is not None and item.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value):
                    before = self.queue.snapshot(item)
                    item.state = QueueState.PENDING.value
                    item.claimed_by = None
                    item.claimed_at = None
                    item.lease_expires_at = None
                    item.available_at = now
                    self.db.flush()
                    self.queue._record(item, "lease_expired", self.actor, before, "worker lost before submit")
                self._audit(attempt, "execution:recovered", {"run_id": run.id, "reason": "lost before submit"})
                counts["retryable"] += 1
        self.db.commit()
        return dict(counts)

    # ------------------------------------------------------------------ #
    # execute: form discovery, mapping, submit, result, verification
    # ------------------------------------------------------------------ #

    def execute(self, item: ApplicationQueueRow, worker_id: str, executor_kind: ExecutorKind = ExecutorKind.MOCK) -> dict[str, Any]:
        """Run one claimed SUBMIT item end to end with an in-process executor."""
        run, package, outcome = self.start(item, worker_id, executor_kind)
        if run is None:
            return outcome
        executor = self.executors[executor_kind]
        attempt = self.require_attempt(run.application_id)
        try:
            form = executor.prepare(package)
        except ExecutorError as exc:
            return self._report(item, worker_id, run, attempt, ExecutionResult(outcome=ExecutionOutcome.RETRYABLE_FAILURE if exc.retryable else ExecutionOutcome.PERMANENT_FAILURE, error_class=exc.error_class, message=exc.message))
        except Exception as exc:  # noqa: BLE001 - discovery crashed before anything was pressed
            logger.exception("Executor %s failed to prepare attempt %s", executor_kind.value, attempt.id)
            return self._report(item, worker_id, run, attempt, ExecutionResult(outcome=ExecutionOutcome.RETRYABLE_FAILURE, error_class=ErrorClass.EXECUTOR_CRASH, message=f"prepare crashed: {type(exc).__name__}: {exc}"[:500]))
        snapshot, answers = self.capture_form(attempt, form, run, package=package)
        handoff = (form.metadata or {}).get("handoff")
        needs_input, needs_review = blocking(answers)
        if needs_input and not handoff:
            return self._report(item, worker_id, run, attempt, ExecutionResult(outcome=ExecutionOutcome.NEEDS_USER_INPUT, message=f"{needs_input} required field(s) need the candidate", stopped_at="form_mapping"))
        if needs_review and not handoff:
            return self._report(item, worker_id, run, attempt, ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=HandoffReason.UNKNOWN_REQUIRED_FIELD, message=f"{needs_review} required field(s) cannot be filled safely", stopped_at="form_mapping", remaining_steps=["answer the unknown field(s)", "submit"]))
        # An executor in dry-run mode cannot press submit (it returns before the gate),
        # so nothing it does may be recorded as a possible click: a dry run that hit a
        # CAPTCHA used to keep submit_invoked=1 (2026-09-14, Okta).
        dry_run = bool(getattr(executor, "dry_run", False))
        if not dry_run:
            # From here the executor may press submit: record that fact first.
            run.submit_invoked = True
            self.db.commit()
        try:
            result = executor.execute(package, form, answers, gate=self._pre_submit_gate(attempt.id))
        except ExecutorError as exc:
            if exc.before_submit or dry_run:
                run.submit_invoked = False
                result = ExecutionResult(outcome=ExecutionOutcome.RETRYABLE_FAILURE if exc.retryable or dry_run else ExecutionOutcome.PERMANENT_FAILURE, error_class=exc.error_class, message=exc.message)
            else:
                result = ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True, error_class=exc.error_class, message=exc.message)
        except Exception as exc:  # noqa: BLE001 - an executor crash is an UNKNOWN outcome, never a resubmit
            logger.exception("Executor %s crashed on attempt %s", executor_kind.value, attempt.id)
            if dry_run:
                # Nothing could have been pressed: a crash during a dry run is retryable, not UNKNOWN.
                result = ExecutionResult(outcome=ExecutionOutcome.RETRYABLE_FAILURE, submit_attempted=False, error_class=ErrorClass.EXECUTOR_CRASH, message=f"dry run crashed: {type(exc).__name__}"[:500])
            else:
                result = ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True, error_class=ErrorClass.EXECUTOR_CRASH, message=f"{type(exc).__name__}: {exc}")
        if result.outcome is ExecutionOutcome.SUBMITTED and result.verification is None:
            try:
                result.verification = executor.verify(package, result)
            except Exception as exc:  # noqa: BLE001 - verification failure is not a submission failure
                logger.exception("Verification crashed on attempt %s", attempt.id)
                result.verification = VerificationResult(status=VerificationStatus.UNKNOWN, detail=f"verify crashed: {type(exc).__name__}")
        return self._report(item, worker_id, run, attempt, result)

    # ------------------------------------------------------------ documents

    @property
    def documents(self):
        if self._document_service is None:
            from app.documents.service import DocumentService

            self._document_service = DocumentService(self.db, self.tenant_id, actor=self.actor)
        return self._document_service

    def _documents_for(self, preparation):
        """Ensure the resume (and cover letter when enabled) exist as verified PDFs."""
        from app.documents.models import RenderReport

        if preparation is None:
            return RenderReport(preparation_id="", problems=["no preparation"])
        try:
            return self.documents.ensure_for_execution(preparation)
        except CareerOSError as exc:
            return RenderReport(preparation_id=preparation.id, problems=[exc.message])

    def _document_path(self, artifact_id: str) -> str:
        _, path = self.documents.materialize(artifact_id)
        return path

    def _pre_submit_gate(self, application_id: str):
        """Re-run every submission-time condition immediately before the click.

        Returned to the executor as a callback; the executor calls it after
        filling and before pressing submit. The page and the policy may both
        have changed since ``start``.
        """

        def gate() -> list[str]:
            attempt = self.get_attempt(application_id)
            if attempt is None:
                return ["attempt vanished"]
            failures: list[str] = []
            # Server-side SAFE / LIVE switch: while SAFE, no executor may press submit.
            if not submission_mode.is_live_enabled(self.tenant_id):
                failures.append(submission_mode.SAFE_MODE_FAILURE)
            source = None
            if attempt.job_id:
                from app.jobs.database.models import JobRow

                job = self.db.get(JobRow, attempt.job_id)
                source = job.source if job else None
            if is_paused(self.db, source):
                failures.append("PAUSED: the kill switch is on")
            preparation, co, opportunity = self._context(attempt)
            self._policy = None
            checker = PreconditionChecker(self.db, self.tenant_id, self.policy)
            # reserve=True: if the tenant-local period rolled over since
            # admission, the reservation is refreshed here (atomic ledger
            # update, old period released) and committed before the click.
            with self.db.begin_nested():
                report = checker.check(attempt, preparation, co, opportunity, stale_inputs=self._stale_inputs(preparation), reserve=True, executing=True)
            if report.ok:
                self.db.commit()
            else:
                self.db.rollback()
            failures.extend(f"{f.code.value}: {f.message}" for f in report.failures)
            return failures

        return gate

    def capture_form(self, attempt: ApplicationRow, form: FormSnapshot, run: Optional[ExecutionRunRow] = None, package: Optional[ExecutionPackage] = None) -> tuple[FormSnapshotRow, list[FieldAnswer]]:
        """Persist the discovered form (structure only) and map its fields."""
        fingerprint = form_fingerprint(form.fields)
        snapshot = (
            self.db.query(FormSnapshotRow)
            .options(joinedload(FormSnapshotRow.fields))
            .filter(FormSnapshotRow.application_id == attempt.id, FormSnapshotRow.fingerprint == fingerprint)
            .first()
        )
        package = package or self.package(attempt)
        if snapshot is None:
            snapshot = FormSnapshotRow(
                tenant_id=self.tenant_id,
                application_id=attempt.id,
                opportunity_id=attempt.opportunity_id,
                executor_kind=form.executor_kind.value,
                executor_version=form.executor_version,
                source_url=form.source_url[:2048],
                fingerprint=fingerprint,
                field_count=len(form.fields),
                metadata_=sanitize_diagnostics(form.metadata),
            )
            self.db.add(snapshot)
            self.db.flush()
            for position, (field, answer) in enumerate(zip(form.fields, map_fields(form.fields, package))):
                self.db.add(
                    FormFieldRow(
                        tenant_id=self.tenant_id,
                        snapshot_id=snapshot.id,
                        position=position,
                        external_id=field.external_id,
                        label=field.label[:512],
                        question_key=answer.question_key,
                        field_type=field.field_type.value,
                        required=field.required,
                        options=[o.model_dump() for o in field.options],
                        current_value=(field.current_value or "")[:1024] or None,
                        answer=answer.answer,
                        selected_values=list(answer.selected_values),
                        artifact_type=answer.artifact_type,
                        source=answer.source.value,
                        status=answer.status.value,
                        category=answer.category,
                        preparation_answer_id=answer.preparation_answer_id,
                        evidence_keys=list(answer.evidence_keys),
                        reason=(answer.reason or "")[:256] or None,
                    )
                )
            self.db.flush()
            self.db.refresh(snapshot)
        else:
            # Same structure seen before: keep candidate answers, refresh the rest.
            # Joined by position (the fingerprint proves the same shape and
            # order): several controls can share a label ("Attach" twice on
            # Greenhouse, Phase 13), so the question key alone is not a key.
            fresh_answers = map_fields(form.fields, package)
            for row in sorted(snapshot.fields, key=lambda r: r.position):
                if row.source == FieldAnswerSource.USER.value:
                    continue
                answer = fresh_answers[row.position] if row.position < len(fresh_answers) and fresh_answers[row.position].question_key == row.question_key else None
                if answer is not None:
                    row.answer, row.selected_values, row.source, row.status, row.reason = answer.answer, list(answer.selected_values), answer.source.value, answer.status.value, (answer.reason or "")[:256] or None
                    row.artifact_type, row.preparation_answer_id, row.evidence_keys = answer.artifact_type, answer.preparation_answer_id, list(answer.evidence_keys)
            self.db.flush()
        if run is not None:
            run.form_snapshot_id = snapshot.id
        answers = [self._answer_from_row(f) for f in snapshot.fields]
        return snapshot, answers

    @staticmethod
    def _answer_from_row(row: FormFieldRow) -> FieldAnswer:
        return FieldAnswer(
            field_id=row.id,
            external_id=row.external_id,
            label=row.label,
            question_key=row.question_key,
            field_type=row.field_type,
            required=row.required,
            status=row.status,
            source=row.source,
            category=row.category,
            answer=row.answer,
            selected_values=list(row.selected_values or []),
            artifact_type=row.artifact_type,
            preparation_answer_id=row.preparation_answer_id,
            evidence_keys=list(row.evidence_keys or []),
            reason=row.reason,
        )

    def _latest_snapshot(self, application_id: str) -> Optional[FormSnapshotRow]:
        return (
            self.db.query(FormSnapshotRow)
            .options(joinedload(FormSnapshotRow.fields))
            .filter(FormSnapshotRow.tenant_id == self.tenant_id, FormSnapshotRow.application_id == application_id)
            .order_by(FormSnapshotRow.captured_at.desc())
            .first()
        )

    # ------------------------------------------------------------------ #
    # result handling
    # ------------------------------------------------------------------ #

    def report_result(self, item: ApplicationQueueRow, worker_id: str, result: ExecutionResult) -> dict[str, Any]:
        """An external executor (extension, local runner) reports what happened."""
        self.queue._assert_owner(item, worker_id)
        attempt = self._attempt_for_item(item)
        if attempt is None:
            raise NotFoundError("No application attempt for this queue item")
        run = self._crashed_run(attempt)
        if run is None or run.queue_item_id != item.id:
            raise ConflictError("No running execution for this item; call start first")
        if result.outcome is ExecutionOutcome.SUBMITTED and result.verification is None:
            # The executor that ran this (e.g. the browser extension) decides
            # what its reported evidence is worth; a click alone is never VERIFIED.
            try:
                kind = ExecutorKind(run.executor_kind)
            except ValueError:
                kind = None
            executor = self.executors.get(kind) if kind else None
            if executor is not None:
                try:
                    result.verification = executor.verify(self.package(attempt), result)
                except Exception as exc:  # noqa: BLE001 - verification failure is not a submission failure
                    logger.exception("Verification crashed on attempt %s", attempt.id)
                    result.verification = VerificationResult(status=VerificationStatus.UNKNOWN, detail=f"verify crashed: {type(exc).__name__}")
        return self._report(item, worker_id, run, attempt, result)

    # ------------------------------------------------------------ extension

    def match_for_url(self, url: str, worker_id: str) -> Optional[tuple[ApplicationQueueRow, ApplicationRow, OpportunityRow, JobRow]]:
        """The claimable SUBMIT item whose application page is the page ``url``.

        URLs are compared without scheme, ``www.``, query or fragment; the
        tab may be on the job page (``/jobs/123``) while the application URL is
        the apply page (``/jobs/123/apply``) or vice versa, so a prefix match in
        either direction counts. Items held by another live worker are skipped.
        """
        wanted = _normalize_url(url)
        if not wanted:
            return None
        now = db_now()
        rows = (
            self.db.query(ApplicationQueueRow, ApplicationRow, OpportunityRow, JobRow)
            .join(ApplicationRow, ApplicationRow.opportunity_id == ApplicationQueueRow.opportunity_id)
            .join(OpportunityRow, OpportunityRow.id == ApplicationQueueRow.opportunity_id)
            .outerjoin(JobRow, JobRow.id == OpportunityRow.canonical_job_id)
            .filter(
                ApplicationQueueRow.tenant_id == self.tenant_id,
                ApplicationRow.tenant_id == self.tenant_id,
                ApplicationQueueRow.action == QueueAction.SUBMIT.value,
                ApplicationQueueRow.state.in_([QueueState.PENDING.value, QueueState.RETRY_WAIT.value, QueueState.CLAIMED.value, QueueState.PROCESSING.value]),
            )
            .all()
        )
        best = None
        for item, attempt, opportunity, job in rows:
            if item.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value) and item.claimed_by != worker_id and item.lease_expires_at and item.lease_expires_at >= now:
                continue
            candidates = [job.application_url if job else None, job.source_url if job else None]
            for candidate in candidates:
                normalized = _normalize_url(candidate)
                if not normalized:
                    continue
                if wanted == normalized:
                    return item, attempt, opportunity, job
                if (wanted.startswith(normalized + "/") or normalized.startswith(wanted + "/")) and best is None:
                    best = (item, attempt, opportunity, job)
        return best

    def claim_item(self, item: ApplicationQueueRow, worker_id: str) -> Optional[ApplicationQueueRow]:
        claimed = self.queue.claim_item(item, worker_id, lease_seconds=settings.execution_lease_seconds)
        self.db.commit()
        return claimed

    def heartbeat(self, item: ApplicationQueueRow, worker_id: str) -> ApplicationQueueRow:
        row = self.queue.extend_lease(item, worker_id, lease_seconds=settings.execution_lease_seconds)
        self.db.commit()
        return row

    def gate(self, item: ApplicationQueueRow, worker_id: str) -> dict[str, Any]:
        """The extension's pre-submit call: re-run every condition and, when all
        hold, record ``submit_invoked`` so a crash after the click is UNKNOWN."""
        self.queue._assert_owner(item, worker_id)
        attempt = self._attempt_for_item(item)
        if attempt is None:
            raise NotFoundError("No application attempt for this queue item")
        run = self._crashed_run(attempt)
        if run is None or run.queue_item_id != item.id:
            raise ConflictError("No running execution for this item; call start first")
        if run.submit_invoked:
            return {"ok": False, "failures": ["SUBMIT_ALREADY_INVOKED: a submit was already recorded for this run; report the result or verify"], "run_id": run.id}
        if item.lease_expires_at is not None and item.lease_expires_at < db_now():
            # Phase 12: an expired lease never authorises a click. The worker must
            # heartbeat / re-claim first; another worker may already hold the item.
            self._audit(attempt, "execution:gate_refused", {"run_id": run.id, "failures": ["LEASE_EXPIRED"]})
            self.db.commit()
            return {"ok": False, "failures": ["LEASE_EXPIRED: the lease on this item lapsed; heartbeat or claim it again before submitting"], "run_id": run.id}
        failures = self._pre_submit_gate(attempt.id)()
        if failures:
            self._audit(attempt, "execution:gate_refused", {"run_id": run.id, "failures": failures[:5]})
            self.db.commit()
            return {"ok": False, "failures": failures, "run_id": run.id}
        run.submit_invoked = True
        self._audit(attempt, "execution:gate_passed", {"run_id": run.id})
        self.db.commit()
        return {"ok": True, "failures": [], "run_id": run.id}

    def _report(self, item: ApplicationQueueRow, worker_id: str, run: ExecutionRunRow, attempt: ApplicationRow, result: ExecutionResult) -> dict[str, Any]:
        outcome = result.outcome
        run.outcome = outcome.value
        run.submit_invoked = run.submit_invoked or result.submit_attempted
        # Keep what start() recorded (exact artifacts); add the executor's diagnostics.
        run.diagnostics = {**(run.diagnostics or {}), **sanitize_diagnostics(result.diagnostics)}
        run.application_url = result.application_url
        run.external_application_id = result.external_application_id
        run.confirmation_reference = result.confirmation_reference
        run.error_class = result.error_class.value if result.error_class else None
        run.error_message = (result.message or "")[:2000] or None
        if outcome is ExecutionOutcome.SUBMITTED:
            self._finish_run(run, ExecutionStatus.SUBMITTED, outcome)
            self.attempts.transition(attempt, ApplicationStatus.SUBMITTED, self.actor, result.message or "submitted", {"run_id": run.id})
            attempt.submitted_at = db_now()
            attempt.result_url = result.application_url
            attempt.external_application_id = result.external_application_id
            attempt.confirmation = result.confirmation_reference
            self.queue.succeed(item, worker_id, {"outcome": "SUBMITTED", "run_id": run.id, "application_id": attempt.id})
            self._move_candidate(attempt, OpportunityState.SUBMITTED, "submitted")
            self._audit(attempt, "execution:submitted", {"run_id": run.id, "reference": result.confirmation_reference})
            if result.verification is not None:
                self._apply_verification(run, attempt, result.verification, item=None)
            else:
                self._emit_signal(run, attempt)
        elif outcome is ExecutionOutcome.DRY_RUN:
            # Navigated, inspected, mapped; submit deliberately not pressed.
            run.submit_invoked = False
            self._finish_run(run, ExecutionStatus.DRY_RUN, outcome)
            attempt.submission_key = None
            self.attempts.transition(attempt, ApplicationStatus.READY, self.actor, result.message or "dry run", {"run_id": run.id})
            self.queue.needs_review(item, worker_id, f"dry run complete: {result.message or ''}"[:2000])
            self._audit(attempt, "execution:dry_run", {"run_id": run.id, "diagnostics": {k: v for k, v in run.diagnostics.items() if k in ("fields_filled", "fields_skipped", "missing_required", "strategy")}})
        elif outcome is ExecutionOutcome.UNKNOWN:
            run.submit_invoked = True
            self._finish_run(run, ExecutionStatus.UNKNOWN, outcome)
            self.attempts.transition(attempt, ApplicationStatus.UNCERTAIN, self.actor, result.message or "submit outcome unknown", {"run_id": run.id})
            run.verification_status = VerificationStatus.PENDING.value
            self.queue.needs_review(item, worker_id, "submit outcome unknown: verify before anything else; never resubmitted automatically")
            self._audit(attempt, "execution:unknown", {"run_id": run.id, "error_class": run.error_class})
            self._emit_signal(run, attempt)
        elif outcome is ExecutionOutcome.RETRYABLE_FAILURE:
            self._finish_run(run, ExecutionStatus.FAILED_RETRYABLE, outcome)
            attempt.submission_key = None
            self.attempts.transition(attempt, ApplicationStatus.READY, self.actor, result.message or "retryable failure", {"run_id": run.id})
            self.queue.fail(item, worker_id, result.message or "retryable failure", retryable=True)
            if item.state == QueueState.FAILED.value:
                self.attempts.transition(attempt, ApplicationStatus.NEEDS_REVIEW, self.actor, "retries exhausted", {"run_id": run.id})
            self._audit(attempt, "execution:retryable_failure", {"run_id": run.id, "error_class": run.error_class, "queue_state": item.state})
        elif outcome is ExecutionOutcome.PERMANENT_FAILURE:
            self._finish_run(run, ExecutionStatus.FAILED_PERMANENT, outcome)
            self.attempts.release(attempt, ApplicationStatus.FAILED, result.message or "permanent failure", run.id)
            self.queue.fail(item, worker_id, result.message or "permanent failure", retryable=False)
            self._audit(attempt, "execution:permanent_failure", {"run_id": run.id, "error_class": run.error_class})
        elif outcome is ExecutionOutcome.HANDOFF:
            self._handoff(item, worker_id, attempt, run, result.handoff_reason or HandoffReason.USER_CONFIRMATION_REQUIRED, result.message or "human handoff", result.stopped_at, result.remaining_steps, result.resumable)
        elif outcome is ExecutionOutcome.NEEDS_REVIEW and result.error_class is ErrorClass.POLICY and "CAP_REACHED" in (result.message or ""):
            # The pre-submit gate found no capacity in the new period: wait for
            # it, exactly like the same finding at start(); not a failure.
            self._finish_run(run, ExecutionStatus.PRECONDITION_FAILED, outcome, error_class=ErrorClass.POLICY, message=result.message)
            attempt.submission_key = None
            self.attempts.transition(attempt, ApplicationStatus.READY, self.actor, result.message or "cap reached at submit", {"run_id": run.id})
            self.queue.release(item, worker_id, (result.message or "cap reached")[:512])
            code = PreconditionCode.WEEKLY_CAP_REACHED if "WEEKLY_CAP_REACHED" in (result.message or "") else PreconditionCode.DAILY_CAP_REACHED
            item.available_at = to_db(self._period_end(code, PreconditionReport(ok=False)))
            self._audit(attempt, "execution:cap_wait", {"run_id": run.id, "codes": [code.value], "at": "pre_submit_gate"})
        elif outcome in (ExecutionOutcome.NEEDS_USER_INPUT, ExecutionOutcome.NEEDS_REVIEW, ExecutionOutcome.FORM_CHANGED):
            status = ExecutionStatus.NEEDS_USER_INPUT if outcome is ExecutionOutcome.NEEDS_USER_INPUT else ExecutionStatus.NEEDS_REVIEW
            self._finish_run(run, status, outcome)
            attempt.submission_key = None
            target = _ATTEMPT_FOR_OUTCOME[outcome]
            reason = result.message or outcome.value.lower()
            if outcome is ExecutionOutcome.FORM_CHANGED:
                reason = f"FORM_CHANGED: {reason}"
            self.attempts.transition(attempt, target, self.actor, reason, {"run_id": run.id})
            if outcome is ExecutionOutcome.NEEDS_USER_INPUT:
                self.queue.block(item, worker_id, f"needs_user_input: {reason}")
            else:
                self.queue.needs_review(item, worker_id, reason)
            self._audit(attempt, f"execution:{outcome.value.lower()}", {"run_id": run.id})
        self.db.commit()
        return {"outcome": outcome.value, "run_id": run.id, "attempt_status": attempt.status, "queue_state": item.state}

    def _handoff(self, item, worker_id, attempt, run, reason: HandoffReason, message: str, stopped_at: Optional[str] = None, remaining: Optional[list[str]] = None, resumable: bool = True) -> None:
        detail = {"reason": reason.value, "message": message, "stopped_at": stopped_at, "remaining_steps": list(remaining or []), "resumable": resumable}
        if run is not None:
            run.handoff_reason = reason.value
            run.handoff = detail
            self._finish_run(run, ExecutionStatus.HANDOFF, ExecutionOutcome.HANDOFF)
        attempt.submission_key = None
        attempt.blocked_reason = reason.value
        self.attempts.transition(attempt, ApplicationStatus.BLOCKED, self.actor, f"{reason.value}: {message}"[:256], {"run_id": run.id if run else None, **detail}, force=True)
        self.queue.block(item, worker_id, f"{reason.value}: {message}")
        self._audit(attempt, "execution:handoff", {"run_id": run.id if run else None, **detail})

    def _settle_preconditions(self, item, worker_id, attempt, preparation, report: PreconditionReport) -> None:
        codes = {f.code for f in report.failures}
        message = "; ".join(f"{f.code.value}: {f.message}" for f in report.failures)[:2000]
        run = ExecutionRunRow(
            tenant_id=self.tenant_id, application_id=attempt.id, preparation_id=attempt.preparation_id,
            candidate_opportunity_id=attempt.candidate_opportunity_id, opportunity_id=attempt.opportunity_id, queue_item_id=item.id,
            executor_kind="NONE", executor_version="", worker_id=worker_id,
            idempotency_key=f"{self.tenant_id}:{attempt.id}:{attempt.attempt_number}:pre:{db_now().isoformat()}",
            run_number=attempt.execution_count or 0, status=ExecutionStatus.PRECONDITION_FAILED.value, error_class=ErrorClass.POLICY.value,
            error_message=message, preconditions=[f.model_dump(mode="json") for f in report.failures], finished_at=db_now(),
        )
        self.db.add(run)
        self.db.flush()
        attempt.last_execution_id = run.id
        cap = codes & {PreconditionCode.DAILY_CAP_REACHED, PreconditionCode.WEEKLY_CAP_REACHED}
        if codes & {PreconditionCode.PREPARATION_STALE, PreconditionCode.PREPARATION_NOT_CURRENT}:
            run.status = ExecutionStatus.STALE.value
            self._reprepare(attempt, preparation, message)
            self.queue.needs_review(item, worker_id, f"stale preparation: {message}"[:2000])
            self._audit(attempt, "execution:stale", {"run_id": run.id, "inputs": report.stale_inputs})
        elif codes & {PreconditionCode.COMPANY_BLOCKED, PreconditionCode.OPPORTUNITY_CLOSED, PreconditionCode.DUPLICATE_APPLICATION, PreconditionCode.DUPLICATE_OPPORTUNITY, PreconditionCode.ALREADY_SUBMITTED}:
            if PreconditionCode.ALREADY_SUBMITTED in codes:
                self.queue.succeed(item, worker_id, {"outcome": "already_submitted"})
            else:
                self.attempts.release(attempt, ApplicationStatus.CLOSED, message[:256], run.id)
                self.queue.cancel(item, self.actor, message[:512])
                self._audit(attempt, "execution:blocked_by_policy", {"run_id": run.id, "codes": sorted(c.value for c in codes)})
        elif PreconditionCode.COOLDOWN_ACTIVE in codes:
            until = next((f.detail.get("until") for f in report.failures if f.code is PreconditionCode.COOLDOWN_ACTIVE), None)
            self.attempts.transition(attempt, ApplicationStatus.BLOCKED, self.actor, message[:256], {"run_id": run.id}, force=True)
            attempt.blocked_reason = "COOLDOWN_ACTIVE"
            self.queue.block(item, worker_id, f"cooldown active until {until}")
            self._audit(attempt, "execution:cooldown_blocked", {"run_id": run.id, "until": until})
        elif cap:
            # Not a failure: wait for the next period. The item goes back to
            # PENDING without counting an attempt, available at the period end.
            self.queue.release(item, worker_id, message[:512])
            item.available_at = to_db(self._period_end(next(iter(cap)), report))
            self._audit(attempt, "execution:cap_wait", {"run_id": run.id, "codes": sorted(c.value for c in cap)})
        elif PreconditionCode.USER_INPUT_REQUIRED in codes:
            self.attempts.transition(attempt, ApplicationStatus.NEEDS_USER_INPUT, self.actor, message[:256], {"run_id": run.id}, force=True)
            self.queue.block(item, worker_id, message[:2000])
            self._audit(attempt, "execution:needs_user_input", {"run_id": run.id})
        else:
            self.attempts.transition(attempt, ApplicationStatus.NEEDS_REVIEW, self.actor, message[:256], {"run_id": run.id}, force=True)
            self.queue.needs_review(item, worker_id, message[:2000])
            self._audit(attempt, "execution:precondition_failed", {"run_id": run.id, "codes": sorted(c.value for c in codes)})

    def _period_end(self, code: PreconditionCode, report: PreconditionReport):
        from app.core.timeutils import utc_now
        from app.scheduler.caps import period_keys

        keys = period_keys(utc_now(), self.policy.timezone)
        return keys.day.ends_at if code is PreconditionCode.DAILY_CAP_REACHED else keys.week.ends_at

    def _reprepare(self, attempt: ApplicationRow, preparation, message: str) -> None:
        """A stale package is invalidated and the PREPARE item re-queued; the attempt waits."""
        service = PreparationService(self.db, self.tenant_id, actor=self.actor)
        if preparation is not None and preparation.status == PreparationStatus.READY.value:
            service.invalidate(preparation.id, self.actor, f"stale at execution: {message}"[:256])
        attempt.submission_key = None
        self.attempts.transition(attempt, ApplicationStatus.PREPARING, self.actor, f"re-preparing: {message}"[:256], force=True)
        prepare_item = self.queue.find(attempt.opportunity_id, QueueAction.PREPARE) if attempt.opportunity_id else None
        if prepare_item is not None and prepare_item.state not in (QueueState.PENDING.value, QueueState.CLAIMED.value, QueueState.PROCESSING.value, QueueState.RETRY_WAIT.value):
            self.queue.requeue(prepare_item, self.actor, "stale preparation at execution", allow_succeeded=True)
        elif prepare_item is None and attempt.candidate_opportunity_id:
            co = self.db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id)
            if co is not None:
                self.queue.enqueue(co, QueueAction.PREPARE, self.actor, lane=Lane(attempt.lane or Lane.REVIEW.value), priority=co.priority_score)

    def _park_item(self, item, worker_id, status: str, reason: str) -> None:
        if status in (ApplicationStatus.NEEDS_USER_INPUT.value, ApplicationStatus.BLOCKED.value):
            self.queue.block(item, worker_id, reason)
        elif status in (ApplicationStatus.FAILED.value, ApplicationStatus.CANCELLED.value, ApplicationStatus.CLOSED.value):
            self.queue.cancel(item, self.actor, reason)
        else:
            self.queue.needs_review(item, worker_id, reason)

    def _finish_run(self, run: ExecutionRunRow, status: ExecutionStatus, outcome: Optional[ExecutionOutcome] = None, error_class: Optional[ErrorClass] = None, message: Optional[str] = None) -> None:
        run.status = status.value
        if outcome is not None:
            run.outcome = outcome.value
        if error_class is not None:
            run.error_class = error_class.value
        if message:
            run.error_message = message[:2000]
        run.finished_at = db_now()
        self.db.flush()

    # ------------------------------------------------------------------ #
    # verification
    # ------------------------------------------------------------------ #

    def verify(self, run_id: str, verification: Optional[VerificationResult] = None, executor_kind: Optional[ExecutorKind] = None) -> ExecutionRunRow:
        """Establish what actually happened after a submit (SUBMITTED or UNKNOWN run).

        Either the caller supplies a :class:`VerificationResult` (user
        confirmation, extension report) or a registered executor is asked.
        """
        run = self.require_run(run_id)
        attempt = self.require_attempt(run.application_id)
        if run.status not in (ExecutionStatus.SUBMITTED.value, ExecutionStatus.UNKNOWN.value, ExecutionStatus.VERIFIED.value, ExecutionStatus.VERIFICATION_FAILED.value, ExecutionStatus.HANDOFF.value):
            raise ConflictError(f"Run is {run.status}; only submitted, unknown or handed-off runs can be verified")
        if verification is None:
            kind = executor_kind or ExecutorKind(run.executor_kind) if run.executor_kind in {k.value for k in ExecutorKind} else ExecutorKind.MANUAL
            executor = self.executors.get(kind)
            if executor is None:
                raise ValidationFailed(f"No executor registered for {kind.value}")
            package = self.package(attempt)
            result = ExecutionResult(outcome=ExecutionOutcome(run.outcome) if run.outcome else ExecutionOutcome.UNKNOWN, submit_attempted=run.submit_invoked, application_url=run.application_url, external_application_id=run.external_application_id, confirmation_reference=run.confirmation_reference)
            verification = executor.verify(package, result)
        item = self.queue.get(run.queue_item_id) if run.queue_item_id else None
        self._apply_verification(run, attempt, verification, item)
        self.db.commit()
        return run

    def _apply_verification(self, run: ExecutionRunRow, attempt: ApplicationRow, verification: VerificationResult, item: Optional[ApplicationQueueRow]) -> None:
        run.verification_status = verification.status.value
        run.verification_method = verification.method.value
        run.verification_detail = (verification.detail or "")[:512] or None
        if verification.external_application_id:
            run.external_application_id = verification.external_application_id
            attempt.external_application_id = verification.external_application_id
        if verification.confirmation_reference:
            run.confirmation_reference = verification.confirmation_reference
            attempt.confirmation = verification.confirmation_reference
        if verification.application_url:
            run.application_url = verification.application_url
            attempt.result_url = verification.application_url
        status = verification.status
        if status is VerificationStatus.VERIFIED:
            self._finish_run(run, ExecutionStatus.VERIFIED)
            if attempt.status != ApplicationStatus.VERIFIED.value:
                if attempt.status not in (ApplicationStatus.SUBMITTED.value,):
                    self.attempts.transition(attempt, ApplicationStatus.SUBMITTED, self.actor, "verified", force=True)
                    attempt.submitted_at = attempt.submitted_at or db_now()
                self.attempts.transition(attempt, ApplicationStatus.VERIFIED, self.actor, verification.detail or "verified", {"run_id": run.id, "method": verification.method.value})
                attempt.verified_at = db_now()
                self._move_candidate(attempt, OpportunityState.VERIFIED, "verified")
            if item is not None and item.state != QueueState.SUCCEEDED.value:
                self.queue.resolve(item, self.actor, {"outcome": "VERIFIED", "run_id": run.id}, "verified after an uncertain submit")
            self._audit(attempt, "execution:verified", {"run_id": run.id, "method": verification.method.value})
        elif status is VerificationStatus.LIKELY:
            self._finish_run(run, ExecutionStatus.SUBMITTED)
            if attempt.status == ApplicationStatus.UNCERTAIN.value:
                self.attempts.transition(attempt, ApplicationStatus.SUBMITTED, self.actor, verification.detail or "likely submitted", {"run_id": run.id})
                attempt.submitted_at = attempt.submitted_at or db_now()
                self._move_candidate(attempt, OpportunityState.SUBMITTED, "likely submitted")
                if item is not None and item.state != QueueState.SUCCEEDED.value:
                    self.queue.resolve(item, self.actor, {"outcome": "LIKELY_SUBMITTED", "run_id": run.id})
            self._audit(attempt, "execution:likely_submitted", {"run_id": run.id})
        elif status is VerificationStatus.FAILED:
            self._finish_run(run, ExecutionStatus.VERIFICATION_FAILED)
            # Never resubmitted automatically: a person decides (retry re-opens it).
            if attempt.status != ApplicationStatus.NEEDS_REVIEW.value:
                self.attempts.transition(attempt, ApplicationStatus.NEEDS_REVIEW, self.actor, f"verification failed: {verification.detail or ''}"[:256], {"run_id": run.id}, force=True)
            if item is not None and item.state not in (QueueState.NEEDS_REVIEW.value, QueueState.SUCCEEDED.value, QueueState.CANCELLED.value):
                self.queue.needs_review(item, self.actor, "verification failed; a person must decide whether to retry")
            self._audit(attempt, "execution:verification_failed", {"run_id": run.id})
        else:
            run.verification_status = VerificationStatus.PENDING.value if status is VerificationStatus.UNKNOWN and run.status == ExecutionStatus.UNKNOWN.value else status.value
            self.db.flush()
            self._audit(attempt, "execution:verification_unknown", {"run_id": run.id})
        # Blueprint Phase 10: the verification decision becomes a signal (never a second verification).
        self._emit_signal(run, attempt)

    def _emit_signal(self, run: ExecutionRunRow, attempt: ApplicationRow) -> None:
        """Record the run's result in the Signal Inbox; the inbox never breaks execution."""
        from app.signals.execution import emit_execution_signal

        try:
            with self.db.begin_nested():
                emit_execution_signal(self.db, self.tenant_id, run, attempt, actor=self.actor)
        except Exception as exc:  # noqa: BLE001 - accounting of evidence must not undo the execution result
            logger.warning("Signal for run %s not recorded: %s: %s", run.id, type(exc).__name__, str(exc)[:200])

    def confirm(self, run_id: str, submitted: bool, reference: Optional[str] = None, note: Optional[str] = None) -> ExecutionRunRow:
        """The candidate confirms (or denies) an outcome after a handoff or an unknown result."""
        from app.execution.models import VerificationMethod

        status = VerificationStatus.VERIFIED if submitted else VerificationStatus.FAILED
        run = self.require_run(run_id)
        attempt = self.require_attempt(run.application_id)
        if submitted and run.status == ExecutionStatus.HANDOFF.value:
            # The person finished what the executor could not: it is a submission.
            run.submit_invoked = True
            self._finish_run(run, ExecutionStatus.SUBMITTED, ExecutionOutcome.SUBMITTED)
            if attempt.status != ApplicationStatus.SUBMITTED.value:
                self.attempts.transition(attempt, ApplicationStatus.SUBMITTED, self.actor, "submitted by the candidate", {"run_id": run.id}, force=True)
                attempt.submitted_at = db_now()
                self._move_candidate(attempt, OpportunityState.SUBMITTED, "submitted by the candidate")
        return self.verify(run_id, VerificationResult(status=status, method=VerificationMethod.USER_CONFIRMATION, detail=note or ("confirmed by the candidate" if submitted else "the candidate reports it was not submitted"), confirmation_reference=reference))

    # ------------------------------------------------------------------ #
    # human actions
    # ------------------------------------------------------------------ #

    def handoff(self, item: ApplicationQueueRow, worker_id: str, reason: HandoffReason, message: str, stopped_at: Optional[str] = None, remaining: Optional[list[str]] = None) -> dict[str, Any]:
        """An executor pauses for a person (CAPTCHA, login, MFA, ambiguity)."""
        return self.report_result(item, worker_id, ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=reason, message=message, stopped_at=stopped_at, remaining_steps=list(remaining or [])))

    def retry(self, application_id: str, actor: str, reason: Optional[str] = None) -> ApplicationRow:
        """A person re-opens a parked attempt: back to READY and the item re-queued."""
        attempt = self.require_attempt(application_id)
        if attempt.status in SUBMITTED_STATUSES and attempt.status != ApplicationStatus.SUBMITTING.value:
            raise ConflictError(f"Attempt is {attempt.status}; verify or confirm the earlier submit instead of retrying")
        unknown = [r for r in self.runs_for(attempt.id) if r.status == ExecutionStatus.UNKNOWN.value and r.verification_status in (VerificationStatus.PENDING.value, VerificationStatus.UNKNOWN.value)]
        if unknown:
            raise ConflictError("An earlier submit has an unknown outcome; verify or confirm it before retrying", details={"run_id": unknown[0].id})
        if attempt.status not in (ApplicationStatus.BLOCKED.value, ApplicationStatus.NEEDS_USER_INPUT.value, ApplicationStatus.NEEDS_REVIEW.value, ApplicationStatus.READY.value, ApplicationStatus.FAILED.value, ApplicationStatus.CANCELLED.value):
            raise ConflictError(f"Cannot retry an attempt that is {attempt.status}")
        if attempt.status in (ApplicationStatus.FAILED.value, ApplicationStatus.CANCELLED.value):
            # Released earlier: a new attempt number; the cap slot is taken again at execution.
            attempt.attempt_number = (attempt.attempt_number or 1) + 1
            attempt.released_at = None
            attempt.reserved_at = None
            attempt.cap_day = attempt.cap_week = None
        attempt.submission_key = None
        attempt.blocked_reason = None
        self.attempts.transition(attempt, ApplicationStatus.READY, actor, reason or "retried by a person", force=True)
        item = self.item_for(attempt)
        if item is not None and item.state in _PARKED:
            self.queue.requeue(item, actor, reason or "retry")
        elif item is None and attempt.candidate_opportunity_id:
            self.enqueue_ready()
        self._audit(attempt, "execution:retry", {"actor": actor, "reason": reason})
        self.db.commit()
        return attempt

    def cancel(self, application_id: str, actor: str, reason: Optional[str] = None) -> ApplicationRow:
        attempt = self.require_attempt(application_id)
        if attempt.status in SUBMITTED_STATUSES:
            raise ConflictError(f"Attempt is {attempt.status}; a submitted or uncertain attempt cannot be cancelled, verify it")
        self.attempts.release(attempt, ApplicationStatus.CANCELLED, reason or f"cancelled by {actor}", "cancel")
        attempt.submission_key = None
        item = self.item_for(attempt)
        if item is not None and item.state not in (QueueState.SUCCEEDED.value, QueueState.CANCELLED.value):
            self.queue.cancel(item, actor, reason)
        # Selection quality (2026-09-14): only the SUBMIT item was cancelled, so a
        # released attempt kept an open PREPARE item a preparation worker could
        # still claim. Close it the same way the scheduler does on release.
        prepare = self.queue.find(attempt.opportunity_id, QueueAction.PREPARE) if attempt.opportunity_id else None
        if prepare is not None and prepare.state in (QueueState.PENDING.value, QueueState.RETRY_WAIT.value, QueueState.BLOCKED.value, QueueState.NEEDS_REVIEW.value):
            self.queue.cancel(prepare, actor, reason)
        self._audit(attempt, "execution:cancelled", {"actor": actor, "reason": reason})
        self.db.commit()
        return attempt

    def answer_field(self, field_id: str, text: str, actor: str, save_to_bank: bool = False) -> FormFieldRow:
        """The candidate answers a discovered form field; the attempt resumes when nothing blocks."""
        row = self.db.query(FormFieldRow).filter(FormFieldRow.tenant_id == self.tenant_id, FormFieldRow.id == field_id).first()
        if row is None:
            raise NotFoundError(f"Form field not found: {field_id}")
        text = (text or "").strip()
        if not text:
            raise ValidationFailed("Answer cannot be empty")
        row.answer = text
        row.source = FieldAnswerSource.USER.value
        row.status = FieldAnswerStatus.ANSWERED.value
        row.reason = "answered by the candidate"
        self._forget_cached_inputs()
        if row.options:
            from app.execution.models import FieldType, FormField, FormOption

            field = FormField(label=row.label, field_type=FieldType(row.field_type), required=row.required, options=[FormOption(**o) for o in row.options])
            from app.execution.forms import _match_options

            row.selected_values = _match_options(field, text, multi=field.field_type is FieldType.MULTI_SELECT) or [text]
        snapshot = self.db.get(FormSnapshotRow, row.snapshot_id)
        attempt = self.require_attempt(snapshot.application_id)
        if row.preparation_answer_id and attempt.preparation_id:
            PreparationService(self.db, self.tenant_id, actor=actor).answer_question(attempt.preparation_id, row.preparation_answer_id, text, actor, save_to_bank)
        elif save_to_bank:
            from app.career.models import AnswerBankEntryCreate, AnswerStatus
            from app.career.repository import EvidenceRepository

            repo = EvidenceRepository(self.db, self.tenant_id)
            if repo.find_answer(row.label) is None:
                repo.create_answer(AnswerBankEntryCreate(category=row.category, question=row.label, answer=text, status=AnswerStatus.APPROVED), actor)
        self.db.flush()
        remaining = blocking([self._answer_from_row(f) for f in snapshot.fields])
        if attempt.status == ApplicationStatus.NEEDS_USER_INPUT.value and sum(remaining) == 0:
            self.retry(attempt.id, actor, "all required fields answered")
        else:
            self.db.commit()
        return row

    # ------------------------------------------------------------------ #
    # worker loop
    # ------------------------------------------------------------------ #

    def run_queue(self, worker_id: str, limit: int = 10, executor_kind: ExecutorKind = ExecutorKind.MOCK, lane: Optional[Lane] = None) -> dict[str, int]:
        if executor_kind not in self.executors:
            raise ValidationFailed(f"No executor registered for {executor_kind.value}")
        self._forget_cached_inputs()
        if self._prep_service is not None:
            self._prep_service.context(refresh=True)
        self.recover_lost_runs()
        items = self.claim(worker_id, limit=limit, lane=lane)
        counts: Counter = Counter(claimed=len(items))
        for item in items:
            try:
                outcome = self.execute(item, worker_id, executor_kind)
            except CareerOSError as exc:
                self.db.rollback()
                counts["errors"] += 1
                logger.warning("Execution of item %s failed: %s", item.id, exc.message)
                continue
            counts[str(outcome.get("outcome", "unknown")).lower()] += 1
        return dict(counts)

    # ------------------------------------------------------------------ #
    # summary / observability
    # ------------------------------------------------------------------ #

    def summary(self) -> ExecutionSummary:
        attempts = dict(self.db.query(ApplicationRow.status, func.count(ApplicationRow.id)).filter(ApplicationRow.tenant_id == self.tenant_id).group_by(ApplicationRow.status).all())
        runs = dict(self.db.query(ExecutionRunRow.status, func.count(ExecutionRunRow.id)).filter(ExecutionRunRow.tenant_id == self.tenant_id).group_by(ExecutionRunRow.status).all())
        handoffs = dict(
            self.db.query(ExecutionRunRow.handoff_reason, func.count(ExecutionRunRow.id))
            .filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.handoff_reason.isnot(None))
            .group_by(ExecutionRunRow.handoff_reason)
            .all()
        )
        from app.career.database.models import AuditEventRow

        audit = dict(
            self.db.query(AuditEventRow.action, func.count(AuditEventRow.id))
            .filter(AuditEventRow.tenant_id == self.tenant_id, AuditEventRow.entity_type == "application_attempt")
            .group_by(AuditEventRow.action)
            .all()
        )
        metrics = {
            "attempts": sum(attempts.values()),
            "started": audit.get("execution:started", 0),
            "submitted": audit.get("execution:submitted", 0),
            "verified": audit.get("execution:verified", 0),
            "likely_submitted": audit.get("execution:likely_submitted", 0),
            "retryable_failures": audit.get("execution:retryable_failure", 0),
            "permanent_failures": audit.get("execution:permanent_failure", 0),
            "handoffs": audit.get("execution:handoff", 0),
            "captcha_handoffs": handoffs.get(HandoffReason.CAPTCHA_REQUIRED.value, 0),
            "auth_handoffs": handoffs.get(HandoffReason.AUTH_REQUIRED.value, 0) + handoffs.get(HandoffReason.MFA_REQUIRED.value, 0),
            "unknown_results": audit.get("execution:unknown", 0),
            "verification_failed": audit.get("execution:verification_failed", 0),
            "stale_preparations": audit.get("execution:stale", 0),
            "needs_user_input": audit.get("execution:needs_user_input", 0),
            "needs_review": audit.get("execution:needs_review", 0) + audit.get("execution:form_changed", 0) + audit.get("execution:precondition_failed", 0),
            "duplicate_submissions_prevented": audit.get("execution:blocked_by_policy", 0) + runs.get(ExecutionStatus.PRECONDITION_FAILED.value, 0) - audit.get("execution:cap_wait", 0) - audit.get("execution:cooldown_blocked", 0) - audit.get("execution:needs_user_input", 0) - audit.get("execution:precondition_failed", 0) - audit.get("execution:stale", 0),
            "cap_blocks": audit.get("execution:cap_wait", 0),
            "cooldown_blocks": audit.get("execution:cooldown_blocked", 0),
            "blocklist_blocks": audit.get("execution:blocked_by_policy", 0),
            "cancelled": audit.get("execution:cancelled", 0),
            "retries": audit.get("execution:retry", 0),
        }
        metrics["duplicate_submissions_prevented"] = max(0, metrics["duplicate_submissions_prevented"])
        return ExecutionSummary(tenant_id=self.tenant_id, attempts_by_status=attempts, runs_by_status=runs, handoffs_by_reason=handoffs, queue_by_state=self.queue.counts_by_state(), metrics=metrics)

    # ------------------------------------------------------------------ #

    def _audit(self, attempt: ApplicationRow, action: str, after: dict[str, Any]) -> None:
        self.repo.record("application_attempt", attempt.id, action, self.actor, None, {"status": attempt.status, **after}, after.get("reason") or after.get("message"))

    def _move_candidate(self, attempt: ApplicationRow, state: OpportunityState, reason: str) -> None:
        if not attempt.candidate_opportunity_id:
            return
        co = self.db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id)
        if co is None or co.state == state.value:
            return
        try:
            self.repo.transition(co, state, self.actor, reason)
        except ConflictError:
            self.repo.transition(co, state, self.actor, reason, force=True)


__all__ = ["ExecutionService", "default_registry", "timedelta"]
