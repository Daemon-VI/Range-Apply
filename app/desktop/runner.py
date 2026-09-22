"""Increment 4: one explicit user action = one real submission attempt.

A :class:`LocalRunner` job is a *single* press of "submit this application",
executed through the existing pipeline and nothing else:

    existing SUBMIT queue item -> ExecutionService.claim_item()
      -> ExecutionService.execute() (preconditions, cap reservation, the
         pre-submit gate, submit_invoked, verification, signals)
      -> the existing PlaywrightExecutor, headed, dry run by default.

What this module deliberately does **not** do: ``run_queue``, ``claim(limit)``,
"the next item", batching, automatic retries, or a second submit of an
UNKNOWN / UNCERTAIN attempt. It owns no gate of its own — every guard lives in
``ExecutionService`` and is re-run there, including immediately before the
click. A job is one attempt, chosen by the person, executed once.

Concurrency is one slot: one browser window, one attempt at a time. A second
``start`` (for the same attempt or any other) is refused with
:class:`ConflictError` rather than queued.
"""

import logging
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from app.application.models import ApplicationStatus
from app.config import settings
from app.core.errors import ConflictError, ValidationFailed
from app.core.timeutils import db_now
from app.database import get_session_factory
from app.execution import submission_mode
from app.execution.models import ExecutorKind
from app.execution.service import ExecutionService
from app.pipeline.models import QueueState

logger = logging.getLogger(__name__)


class LiveSubmissionDisabled(ValidationFailed):
    """A live run was asked for while the tenant is in SAFE mode."""

DRY_RUN = "dry_run"
LIVE = "live"
MODES = (DRY_RUN, LIVE)

RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"

THREAD_NAME = "careeros-desktop-run"


def worker_id() -> str:
    """Stable enough to read in the queue, unique per job."""
    return f"desktop-{socket.gethostname()}"[:64] + "-" + uuid.uuid4().hex[:8]


def default_executor_factory(mode: str):
    """The real local executor, headed, dry run unless the mode says ``live``.

    Playwright is imported here (not at module import): the desktop process
    must start without the browser extra installed.
    """
    from app.execution.playwright.browser import BrowserSession
    from app.execution.playwright.executor import PlaywrightExecutor

    return PlaywrightExecutor(
        session=BrowserSession(headless=False),
        dry_run=(mode != LIVE),
        handoff_wait_seconds=settings.playwright_handoff_wait_seconds,
    )


@dataclass
class RunJob:
    """One user-initiated execution of one attempt, and what came of it."""

    tenant_id: str
    application_id: str
    mode: str
    worker_id: str
    actor: str = "desktop"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = RUNNING
    started_at: datetime = field(default_factory=db_now)
    finished_at: Optional[datetime] = None
    #: Exactly what ``ExecutionService.execute`` returned (outcome, run_id,
    #: attempt_status, queue_state — or a settled-without-executing outcome).
    outcome: Optional[dict[str, Any]] = None
    run_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "application_id": self.application_id,
            "mode": self.mode,
            "worker_id": self.worker_id,
            "state": self.state,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "outcome": dict(self.outcome) if self.outcome else None,
            "run_id": self.run_id,
            "error": self.error,
        }


#: Queue states a person may re-open from the desktop by running the attempt again.
PARKED_STATES = (QueueState.BLOCKED.value, QueueState.NEEDS_REVIEW.value, QueueState.FAILED.value)


class LocalRunner:
    """In-process registry of desktop-initiated runs. One slot, one thread."""

    def __init__(self):
        self._jobs: dict[str, RunJob] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reads

    def get(self, job_id: str) -> Optional[RunJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs_for(self, application_id: str) -> list[RunJob]:
        with self._lock:
            return [job for job in reversed(list(self._jobs.values())) if job.application_id == application_id]

    def active(self) -> list[RunJob]:
        with self._lock:
            return [job for job in self._jobs.values() if job.running]

    def active_for(self, application_id: str) -> Optional[RunJob]:
        with self._lock:
            return next((job for job in self._jobs.values() if job.running and job.application_id == application_id), None)

    # ------------------------------------------------------------- start

    def start(
        self,
        tenant_id: str,
        application_id: str,
        mode: str = DRY_RUN,
        actor: str = "desktop",
        executor_factory: Optional[Callable[[str], Any]] = None,
    ) -> RunJob:
        if mode not in MODES:
            raise ValidationFailed(f"mode must be one of {', '.join(MODES)}")
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        if mode == LIVE and not submission_mode.is_live_enabled(tenant_id):
            raise LiveSubmissionDisabled("live submission is not enabled: CareerOS is in SAFE / DRY RUN mode")
        factory = executor_factory or default_executor_factory
        with self._lock:
            for job in self._jobs.values():
                if not job.running:
                    continue
                if job.application_id == application_id:
                    raise ConflictError("a desktop run is already in progress for this application")
                raise ConflictError("a desktop run is already in progress")
            job = RunJob(tenant_id=tenant_id, application_id=application_id, mode=mode, worker_id=worker_id(), actor=actor)
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, factory), name=THREAD_NAME, daemon=True)
        thread.start()
        return job

    # -------------------------------------------------------------- body

    def _run(self, job: RunJob, executor_factory: Callable[[str], Any]) -> None:
        """One attempt, through the existing service. Never raises."""
        db = None
        executor = None
        try:
            db = get_session_factory()()
            executor = executor_factory(job.mode)
            service = ExecutionService(db, job.tenant_id, actor=f"{job.actor}:{job.worker_id}", executors={ExecutorKind.PLAYWRIGHT_LOCAL: executor})
            attempt = service.require_attempt(job.application_id)  # tenant-scoped; NotFoundError if foreign
            if attempt.status != ApplicationStatus.READY.value:
                raise ConflictError(f"attempt is {attempt.status}, not READY")
            item = service.item_for(attempt)
            if item is None:
                raise ConflictError("no SUBMIT queue item for this attempt")
            if item.state in PARKED_STATES:
                # Pilot finding: a stale preparation parks the item, the
                # scheduler re-prepares the attempt to READY, and the item
                # stays parked. The person's click is the retry (the existing
                # path: same attempt, item re-queued), never a new attempt.
                service.retry(attempt.id, actor=f"{job.actor}:{job.worker_id}", reason=f"desktop {job.mode}: re-queued a parked item ({item.state})")
                item = service.item_for(attempt)
                if item is None:
                    raise ConflictError("no SUBMIT queue item for this attempt after re-queueing")
            claimed = service.claim_item(item, job.worker_id)
            if claimed is None:
                if item.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value):
                    raise ConflictError(f"already being handled by another worker/executor ({item.claimed_by or 'lease held'})")
                raise ConflictError(f"the queue item is {item.state} and cannot be claimed" + (f": {item.last_error}" if item.last_error else ""))
            outcome = service.execute(claimed, job.worker_id, ExecutorKind.PLAYWRIGHT_LOCAL)
            job.outcome = dict(outcome or {})
            job.run_id = job.outcome.get("run_id")
            job.finished_at = db_now()
            job.state = FINISHED
        except Exception as exc:  # noqa: BLE001 - reported on the job, never raised into the shell
            logger.exception("Desktop run %s for attempt %s failed", job.id, job.application_id)
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = db_now()
            job.state = FAILED
        finally:
            if executor is not None and hasattr(executor, "close"):
                try:
                    executor.close()
                except Exception:  # noqa: BLE001 - closing a browser must never change the result
                    logger.warning("Closing the desktop executor raised", exc_info=True)
            if db is not None:
                db.close()


_runner: Optional[LocalRunner] = None
_runner_lock = threading.Lock()


def get_runner() -> LocalRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = LocalRunner()
        return _runner


def reset_runner() -> None:
    """Tests only: forget every job and the single slot."""
    global _runner
    with _runner_lock:
        _runner = None
