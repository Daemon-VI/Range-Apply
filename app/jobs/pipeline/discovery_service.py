"""Job discovery service orchestrating the ingestion pipeline.

Transaction model
-----------------
The run row is committed immediately so the run is observable while it works.
Each job is then processed inside a **SAVEPOINT**: a failure rolls back only
that job, leaving the session usable and the run intact. Successful jobs are
flushed and committed in batches rather than one transaction per job.

Whatever happens — including an unhandled exception — the ``finally`` block
rolls the session back to a clean state, re-reads the run row and writes a
terminal status. A run can therefore never stay stuck at ``running`` because of
an error inside this process.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set, Type

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.timeutils import db_now
from app.jobs.database.models import DiscoveryRunRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.extraction.llm_fallback import LLMFallbackExtractor
from app.jobs.models.enums import JobSourceType
from app.jobs.normalization.normalizer import JobNormalizer
from app.jobs.pipeline.closure import close_missing_jobs, should_sweep
from app.jobs.pipeline.run_recovery import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_RUNNING,
    reconcile_stale_runs,
)
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.base import JobSource, SourceError
from app.jobs.sources.greenhouse import GreenhouseSource
from app.jobs.sources.lever import LeverSource

logger = logging.getLogger(__name__)

# Commit every N successfully processed jobs: bounded memory and bounded work
# lost to a crash, without paying a transaction per job.
COMMIT_BATCH_SIZE = 25


class JobDiscoveryService:
    """Orchestrates end-to-end job discovery, normalization, deduplication, and persistence."""

    def __init__(
        self,
        normalizer: Optional[JobNormalizer] = None,
        deduplicator: Optional[JobDeduplicator] = None,
        llm_fallback: Optional[LLMFallbackExtractor] = None,
    ):
        self.normalizer = normalizer or JobNormalizer()
        self.deduplicator = deduplicator or JobDeduplicator()
        self.llm_fallback = llm_fallback or LLMFallbackExtractor()

        # Source adapter registry
        self._sources: Dict[JobSourceType, Type[JobSource]] = {
            JobSourceType.GREENHOUSE: GreenhouseSource,
            JobSourceType.LEVER: LeverSource,
            JobSourceType.ASHBY: AshbySource,
        }

    def register_source(self, source_type: JobSourceType, source_cls: Type[JobSource]) -> None:
        """Extensibility point for registering custom or future source adapters."""
        self._sources[source_type] = source_cls

    def get_source_adapter(self, source_type: JobSourceType) -> JobSource:
        """Instantiates the source adapter for the given type."""
        source_cls = self._sources.get(source_type)
        if not source_cls:
            raise ValueError(f"Unsupported job source type: {source_type}")
        return source_cls()

    async def run_discovery(
        self,
        db: Session,
        source_type: JobSourceType,
        identifier: str,
        company_name: Optional[str] = None,
        trigger: str = "manual",
        close_missing: bool = True,
    ) -> DiscoveryRunRow:
        """Executes a full discovery run for a specific source identifier."""
        start_time = time.time()
        identifier = identifier.strip()

        # Clean up anything a previous crash left behind before adding to history.
        try:
            reconcile_stale_runs(db)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Stale run reconciliation failed; continuing with this run")

        run_row = DiscoveryRunRow(
            source=source_type.value,
            source_identifier=identifier,
            started_at=db_now(),
            status=RUN_STATUS_RUNNING,
            errors=[],
            trigger=trigger,
        )
        db.add(run_row)
        db.commit()
        run_id = run_row.id

        logger.info(
            "Starting discovery run %s for %s (%s)", run_id, source_type.value, identifier
        )

        collected_errors: List[str] = []
        observed_job_ids: Set[str] = set()
        counters = {"new": 0, "updated": 0, "duplicate": 0, "failed": 0, "closed": 0}
        candidates = 0
        final_status = RUN_STATUS_RUNNING

        try:
            adapter = self.get_source_adapter(source_type)
            raw_jobs = await adapter.discover(identifier)
            candidates = len(raw_jobs)

            pending = 0
            for raw_job in raw_jobs:
                try:
                    # SAVEPOINT: one malformed job cannot poison the run.
                    with db.begin_nested():
                        normalized = self.normalizer.normalize(raw_job, company_name=company_name)
                        enriched = await self.llm_fallback.enrich_if_needed(normalized, raw_job)
                        result = self.deduplicator.process(db, enriched, commit=False)

                        observed_job_ids.add(result.job_row.id)
                        if result.status == "NEW":
                            counters["new"] += 1
                        elif result.status == "UPDATED":
                            counters["updated"] += 1
                        elif result.status in ("DUPLICATE", "CROSS_SOURCE_DUPLICATE"):
                            counters["duplicate"] += 1

                    pending += 1
                    if pending >= COMMIT_BATCH_SIZE:
                        db.commit()
                        pending = 0

                except Exception as exc:  # noqa: BLE001 - one job must not abort the run
                    # The savepoint is already rolled back by the context manager;
                    # this guards against an error raised outside it.
                    try:
                        db.rollback()
                    except SQLAlchemyError:
                        logger.exception("Rollback failed after job error")
                    counters["failed"] += 1
                    message = (
                        f"[{type(exc).__name__}] job {raw_job.source_job_id} "
                        f"('{raw_job.raw_title}'): {exc}"
                    )
                    logger.error("Job processing error: %s", message)
                    collected_errors.append(message)
                    pending = 0

            db.commit()
            final_status = RUN_STATUS_PARTIAL if counters["failed"] else RUN_STATUS_COMPLETED

            # --- stale-job sweep ------------------------------------------
            if close_missing:
                # Reaching here means the source fetch itself succeeded and
                # every discovered job was attempted; the failure-ratio check
                # decides whether the observed set is complete enough to act on.
                decision = should_sweep(
                    run_status=RUN_STATUS_COMPLETED,
                    candidates_discovered=candidates,
                    jobs_failed=counters["failed"],
                )
                if decision.performed:
                    try:
                        counters["closed"] = close_missing_jobs(
                            db,
                            source=source_type,
                            source_identifier=identifier,
                            observed_job_ids=observed_job_ids,
                            commit=True,
                        )
                    except SQLAlchemyError as exc:
                        db.rollback()
                        collected_errors.append(f"Stale-job sweep failed: {exc}")
                        logger.exception("Stale-job sweep failed")
                else:
                    logger.info("Skipping stale-job sweep: %s", decision.reason)

        except SourceError as exc:
            final_status = RUN_STATUS_FAILED
            message = f"Source adapter error: {exc}"
            logger.error(message)
            collected_errors.append(message)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the caller
            final_status = RUN_STATUS_FAILED
            message = f"Unexpected discovery pipeline failure: {type(exc).__name__}: {exc}"
            logger.exception("Discovery run %s failed", run_id)
            collected_errors.append(message)
        finally:
            run_row = self._finalize_run(
                db,
                run_id=run_id,
                status=final_status if final_status != RUN_STATUS_RUNNING else RUN_STATUS_FAILED,
                candidates=candidates,
                counters=counters,
                errors=collected_errors,
                duration=round(time.time() - start_time, 2),
            )

        logger.info(
            "Discovery run %s finished (%s): %d new, %d updated, %d duplicate, "
            "%d failed, %d closed in %.2fs",
            run_id,
            run_row.status,
            run_row.jobs_new,
            run_row.jobs_updated,
            run_row.jobs_duplicate,
            run_row.jobs_failed,
            run_row.jobs_closed or 0,
            run_row.duration_seconds or 0,
        )
        return run_row

    def _finalize_run(
        self,
        db: Session,
        run_id: str,
        status: str,
        candidates: int,
        counters: Dict[str, int],
        errors: List[str],
        duration: float,
    ) -> DiscoveryRunRow:
        """Write the terminal run state, whatever happened before.

        The session is rolled back first: if the run failed because of a
        database error, it is the only way the status write itself can succeed.
        """
        try:
            db.rollback()
        except SQLAlchemyError:
            logger.exception("Could not roll back before finalizing run %s", run_id)

        run_row = db.query(DiscoveryRunRow).filter_by(id=run_id).first()
        if run_row is None:  # pragma: no cover - the row was committed at start
            raise RuntimeError(f"Discovery run {run_id} disappeared before finalization")

        run_row.status = status
        run_row.candidates_discovered = candidates
        run_row.pages_fetched = candidates
        run_row.jobs_new = counters["new"]
        run_row.jobs_updated = counters["updated"]
        run_row.jobs_duplicate = counters["duplicate"]
        run_row.jobs_failed = counters["failed"]
        run_row.jobs_closed = counters["closed"]
        # Reassign (not mutate) so SQLAlchemy detects the JSON change.
        run_row.errors = list(errors)
        run_row.completed_at = db_now()
        run_row.duration_seconds = duration
        db.commit()
        return run_row

    async def run_many(
        self,
        targets: List[dict],
        session_factory,
        trigger: str = "manual",
    ) -> List[DiscoveryRunRow]:
        """Run discovery for several boards with a bounded concurrency cap.

        ``targets`` entries are ``{"source": JobSourceType, "identifier": str,
        "company_name": str | None}``.

        Each target gets its **own session**: SQLAlchemy sessions are not
        thread/task safe, and sharing one would corrupt the transaction state
        that the per-job savepoints depend on. Failures are isolated per target,
        so one dead board cannot stop the others.

        Concurrency is capped by ``settings.default_discovery_concurrency``.
        Outbound requests are additionally rate limited per source. On SQLite
        writes still serialize at the database level; PostgreSQL removes that
        constraint without any code change.
        """
        semaphore = asyncio.Semaphore(max(1, settings.default_discovery_concurrency))

        async def _one(target: dict) -> Optional[DiscoveryRunRow]:
            async with semaphore:
                session = session_factory()
                try:
                    return await self.run_discovery(
                        db=session,
                        source_type=target["source"],
                        identifier=target["identifier"],
                        company_name=target.get("company_name"),
                        trigger=trigger,
                    )
                except Exception:  # noqa: BLE001 - isolate per source
                    logger.exception(
                        "Discovery target %s/%s failed outright",
                        target.get("source"),
                        target.get("identifier"),
                    )
                    return None
                finally:
                    session.close()

        results = await asyncio.gather(*(_one(target) for target in targets))
        return [run for run in results if run is not None]
