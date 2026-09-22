"""Autopilot: the pipeline runs by itself up to — never including — a real submission.

One cycle, every ``interval`` seconds, each step through the existing service:

1. **discover** — tracked boards whose ``next_poll_at`` passed
   (``SourceHealthRepository.due`` → ``JobDiscoveryService.run_many``);
2. **match** — only jobs whose content changed since they were scored
   (``stale_job_ids`` → ``run_matching`` → ``sync_run``);
3. **prepare** — one scheduler pass with preparation
   (``SchedulerService.run(prepare=True)``), which also enqueues READY
   attempts for execution.

Dry runs are then taken by the existing desktop worker (``--worker``, always
DRY RUN). Autopilot never calls the executor, never touches the SAFE / LIVE
switch and never submits: a real submission stays one person, one
confirmation screen, typed SUBMIT.

A failing step is recorded and the next step still runs; the loop never dies.
It can be paused, resumed and woken ("run now") from the desktop.
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30 * 60
#: Let the window open and the person look around before the first cycle.
DEFAULT_START_DELAY_SECONDS = 60
STEPS = ("discover", "match", "prepare")


def _session_factory():
    from app.database import get_session_factory

    return get_session_factory()


def discover_step(tenant_id: str, session_factory: Callable[[], Any], limit: int = 50) -> str:
    from app.jobs.models.enums import JobSourceType
    from app.jobs.pipeline.discovery_service import JobDiscoveryService
    from app.jobs.pipeline.source_health import SourceHealthRepository

    db = session_factory()
    try:
        due = SourceHealthRepository(db).due(limit=limit)
        targets = [
            {"source": JobSourceType(row.source), "identifier": row.source_identifier, "company_name": row.company_name}
            for row in due
            if row.source != JobSourceType.EXTENSION.value
        ]
    finally:
        db.close()
    if not targets:
        return "no boards due"
    runs = asyncio.run(JobDiscoveryService().run_many(targets=targets, session_factory=session_factory, trigger="autopilot"))
    return f"{len(targets)} board(s) checked, {len(runs or [])} run(s)"


def match_step(tenant_id: str, session_factory: Callable[[], Any]) -> str:
    from app.intelligence.services.factory import build_orchestrator
    from app.intelligence.services.match_persistence import run_matching, stale_job_ids
    from app.jobs.database.models import JobRow
    from app.jobs.models.enums import JobStatus
    from app.pipeline.sync import sync_run
    from app.services.career_brain import CareerBrainService

    db = session_factory()
    try:
        stale = set(stale_job_ids(db))
        if stale:
            # Closed postings are never scored (run_matching skips them), so they look
            # "changed" forever; only open jobs are a reason to re-match.
            open_ids = {job_id for (job_id,) in db.query(JobRow.id).filter(JobRow.job_status != JobStatus.CLOSED.value).all()}
            stale &= open_ids
        if not stale:
            return "no changed jobs"
        brain = CareerBrainService(db=db, tenant_id=tenant_id)
        brain.load()
        # A FULL run, never a subset: Shortlist, sync and tailoring read only the latest
        # run, so a partial run would hide every other job's score (first autopilot
        # cycle, 2026-09-14: a 7-job run made 2,035 jobs look unscored).
        run = run_matching(db=db, orchestrator=build_orchestrator(brain), job_ids=None, trigger="autopilot", tenant_id=tenant_id)
        sync_run(db, tenant_id, run.id, actor="autopilot")
        db.commit()
        return f"{len(stale)} changed job(s): re-scored {run.jobs_processed or 0} open job(s)"
    finally:
        db.close()


def prepare_step(tenant_id: str, session_factory: Callable[[], Any], prepare_limit: int = 50) -> str:
    from app.core.errors import ConflictError
    from app.scheduler.service import SchedulerService

    db = session_factory()
    try:
        try:
            run = SchedulerService(db, tenant_id, actor="autopilot").run(trigger="autopilot", prepare=True, prepare_limit=prepare_limit)
        except ConflictError:
            db.rollback()
            return "skipped: a scheduler run is already in progress"
        return f"scheduler {run.status}: {run.admitted or 0} admitted"
    finally:
        db.close()


class Autopilot:
    """The supervised loop (``WorkerSupervisor`` contract: ``run``, ``stop``, ``totals``)."""

    def __init__(
        self,
        tenant_id: str,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        start_delay: float = DEFAULT_START_DELAY_SECONDS,
        session_factory: Optional[Callable[[], Any]] = None,
        steps: Optional[dict[str, Callable[[str, Callable[[], Any]], str]]] = None,
        paused: bool = False,
    ):
        self.tenant_id = tenant_id
        self.interval = max(60.0, float(interval)) if steps is None else max(0.01, float(interval))
        self.start_delay = max(0.0, float(start_delay))
        self._session_factory = session_factory
        self.steps = steps or {"discover": discover_step, "match": match_step, "prepare": prepare_step}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = paused
        self._cycle_lock = threading.Lock()
        self.totals: dict[str, Any] = {"cycles": 0, "errors": 0, "paused": paused, "running_step": None, "last_cycle_at": None, "next_cycle_at": None, "steps": {}}

    # ------------------------------------------------------------ controls

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        self.totals["paused"] = True

    def resume(self) -> None:
        self._paused = False
        self.totals["paused"] = False
        self._wake.set()

    def run_now(self) -> None:
        """Wake the loop for one cycle now (also while paused: an explicit request)."""
        self._force = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # --------------------------------------------------------------- cycle

    def run_once(self) -> dict[str, Any]:
        """Every step once, in order. Never raises."""
        if not self._cycle_lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        factory = self._session_factory or _session_factory()
        results: dict[str, Any] = {}
        try:
            for name, step in self.steps.items():
                if self._stop.is_set():
                    break
                self.totals["running_step"] = name
                started = time.time()
                try:
                    detail = step(self.tenant_id, factory)
                    results[name] = {"ok": True, "detail": str(detail)[:200], "at": started, "seconds": round(time.time() - started, 1)}
                except Exception as exc:  # noqa: BLE001 - one failing step never stops the others or the loop
                    self.totals["errors"] += 1
                    logger.warning("Autopilot step %s failed (%s)", name, type(exc).__name__, exc_info=True)
                    # Only the error type: messages can quote page text or URLs.
                    results[name] = {"ok": False, "detail": type(exc).__name__, "at": started, "seconds": round(time.time() - started, 1)}
                self.totals["steps"][name] = results[name]
        finally:
            self.totals["running_step"] = None
            self.totals["cycles"] += 1
            self.totals["last_cycle_at"] = time.time()
            self._cycle_lock.release()
        logger.info("Autopilot cycle: %s", {k: v["detail"] for k, v in results.items()})
        return results

    def run(self) -> dict[str, Any]:
        logger.info("Autopilot started for tenant %s (every %.0f min, first cycle in %.0fs)", self.tenant_id, self.interval / 60, self.start_delay)
        self._force = False
        wait = self.start_delay
        while not self._stop.is_set():
            self.totals["next_cycle_at"] = time.time() + wait
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                break
            forced, self._force = self._force, False
            if self._paused and not forced:
                wait = self.interval
                continue
            self.run_once()
            wait = self.interval
        self.totals["next_cycle_at"] = None
        logger.info("Autopilot stopped")
        return dict(self.totals)


def default_autopilot_factory(tenant_id: str, interval_minutes: float = 30.0) -> Callable[[], Autopilot]:
    def build() -> Autopilot:
        return Autopilot(tenant_id, interval=interval_minutes * 60)

    return build
