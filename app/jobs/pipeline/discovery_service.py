"""Job discovery service orchestrating the ingestion pipeline.

Per job, in order (blueprint §5, Phase 3):

    SOURCE -> FETCH -> RAW JOB -> VALIDATE -> RAW HASH
      -> (unchanged? touch last_seen and stop)          # the volume fast path
      -> NORMALISE (deterministic) -> LLM fallback (optional, only for UNKNOWN fields)
      -> DEDUP (record identity) -> PERSIST -> VERSION (only if content changed)
      -> OPPORTUNITY RESOLUTION (the one and only creation path)

Transaction model
-----------------
The run row is committed immediately so the run is observable while it works.
Each job is then processed inside a **SAVEPOINT**: a failure rolls back only
that job, leaving the session usable and the run intact. Successful jobs are
flushed and committed in batches rather than one transaction per job.

Whatever happens — including an unhandled exception — the ``finally`` block
rolls the session back to a clean state, re-reads the run row and writes a
terminal status, then folds the run into ``source_health``. A run can
therefore never stay stuck at ``running`` because of an error inside this
process.

Resumability: pass ``resume_from_run_id`` and every job the interrupted run
already observed (its source reference was seen after that run started) is
skipped with zero work; everything else is processed normally.
"""

import asyncio
import inspect
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Type

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.core.timeutils import db_now, ensure_aware, to_db
from app.jobs.database.models import DiscoveryRunRow, JobRow, SourceReferenceRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.extraction.llm_fallback import LLMFallbackExtractor
from app.jobs.freshness import freshness_for_row
from app.jobs.geography import DiscoveryGeography
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.normalization.normalizer import JobNormalizer, compute_raw_hash, validate_raw_job
from app.jobs.pipeline.closure import close_missing_jobs, should_sweep
from app.jobs.pipeline.run_recovery import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_RUNNING,
    reconcile_stale_runs,
)
from app.jobs.pipeline.source_health import SourceHealthRepository
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.base import JobSource, SourceError
from app.jobs.sources.capture import CaptureSource
from app.jobs.sources.greenhouse import GreenhouseSource
from app.jobs.sources.lever import LeverSource

# Imported at module level on purpose: it registers the opportunity tables on
# the shared metadata before any test fixture calls ``create_all``.
from app.pipeline.database.models import OpportunityJobRow  # noqa: E402
from app.pipeline.repository import OpportunityRepository  # noqa: E402

logger = logging.getLogger(__name__)


def discovery_geography(db: Session) -> Optional[DiscoveryGeography]:
    """The location preferences of every tenant discovery projects to, read from the Career Brain.

    Changing a tenant's target (profile location or location preference)
    changes what the next run ingests, with no code change.
    """
    # Local import: the career package imports the jobs package.
    from app.career.read_model import load_geography_policy

    tenants = [t.strip() for t in (settings.discovery_project_tenants or "").split(",") if t.strip()]
    if not tenants:
        return None
    try:
        return DiscoveryGeography(tuple(load_geography_policy(db, tenant) for tenant in tenants))
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not load tenant location preferences; discovering without a geography filter")
        return None

# Commit every N successfully processed jobs: bounded memory and bounded work
# lost to a crash, without paying a transaction per job.
COMMIT_BATCH_SIZE = 25
# IN-list size for the per-run prefetch of known references and links.
PREFETCH_CHUNK = 500

COUNTER_KEYS = (
    "new",
    "updated",
    "duplicate",
    "failed",
    "closed",
    "rejected",
    "unchanged",
    "versioned",
    "reposted",
    "filtered",
    "resumed_skipped",
    "opportunities_new",
    "opportunities_linked",
)


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
        # Custom deduplicators (tests, future subclasses) may keep the original
        # ``process(db, job, commit)`` signature; only pass the prefetch hint
        # to those that accept it.
        self._dedup_accepts_hint = (
            "skip_source_lookup" in inspect.signature(self.deduplicator.process).parameters
        )

        # Source adapter registry. Adding a source = adding an entry here (or
        # calling register_source); the pipeline below never branches on it.
        self._sources: Dict[JobSourceType, Type[JobSource]] = {
            JobSourceType.GREENHOUSE: GreenhouseSource,
            JobSourceType.LEVER: LeverSource,
            JobSourceType.ASHBY: AshbySource,
            JobSourceType.EXTENSION: CaptureSource,
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

    # ------------------------------------------------------------------ #

    async def run_discovery(
        self,
        db: Session,
        source_type: JobSourceType,
        identifier: str,
        company_name: Optional[str] = None,
        trigger: str = "manual",
        close_missing: bool = True,
        resume_from_run_id: Optional[str] = None,
        title_include: Optional[List[str]] = None,
        adapter_kwargs: Optional[dict] = None,
        geography: Optional[DiscoveryGeography] = None,
    ) -> DiscoveryRunRow:
        """Executes a full discovery run for a specific source identifier.

        Args:
            close_missing: run the scoped stale-job sweep afterwards. Off for
                captures, where one page never represents a whole board.
            resume_from_run_id: skip jobs already observed by that run.
            title_include: optional regex allow-list on the raw title; matches
                are ingested, the rest are *counted* as filtered (never hidden).
            adapter_kwargs: passed to ``adapter.discover`` (e.g. a capture payload).
            geography: the projected tenants' location preferences
                (:func:`discovery_geography`). Passed to adapters that can
                filter by geography at the source; for the rest, a posting
                tied only to countries *no* tenant targets is counted as
                filtered before ingestion. Unconfirmed locations are kept.
        """
        start_time = time.time()
        identifier = identifier.strip()

        # Clean up anything a previous crash left behind before adding to history.
        try:
            reconcile_stale_runs(db)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Stale run reconciliation failed; continuing with this run")

        resume_cutoff = self._resume_cutoff(db, resume_from_run_id)

        run_row = DiscoveryRunRow(
            source=source_type.value,
            source_identifier=identifier,
            company_name=company_name,
            started_at=db_now(),
            status=RUN_STATUS_RUNNING,
            errors=[],
            trigger=trigger,
            resumed_from_run_id=resume_from_run_id,
            checkpoint={},
        )
        db.add(run_row)
        db.commit()
        run_id = run_row.id

        logger.info(
            "Starting discovery run %s for %s (%s)%s",
            run_id,
            source_type.value,
            identifier,
            f" resuming {resume_from_run_id}" if resume_from_run_id else "",
        )

        collected_errors: List[str] = []
        errors_dropped = 0
        observed_job_ids: Set[str] = set()
        touched_opportunities: Set[str] = set()
        counters = {key: 0 for key in COUNTER_KEYS}
        counters["filtered_geography"] = 0
        candidates = 0
        final_status = RUN_STATUS_RUNNING
        failure_kind: Optional[str] = None
        adapter: Optional[JobSource] = None
        fresh_posted_at: Optional[datetime] = None
        patterns = [re.compile(p, re.IGNORECASE) for p in (title_include or []) if p]
        self.llm_fallback.start_run()
        ai_calls_before = self.llm_fallback.calls
        ai_hits_before = self.llm_fallback.cache_hits

        def note_error(message: str) -> None:
            nonlocal errors_dropped
            if len(collected_errors) < settings.discovery_max_errors_stored:
                collected_errors.append(message)
            else:
                errors_dropped += 1

        try:
            adapter = self.get_source_adapter(source_type)
            discover_kwargs = dict(adapter_kwargs or {})
            if geography is not None and adapter.supports_geography:
                discover_kwargs["geography"] = geography
            raw_jobs = await adapter.discover(identifier, **discover_kwargs)
            candidates = len(raw_jobs)

            # Batch prefetch (blueprint §11): what we already know about these
            # postings, in a handful of IN queries instead of several per job.
            ref_index = self._prefetch_references(db, source_type, raw_jobs)
            link_index = self._prefetch_links(db, [ref.job_id for ref in ref_index.values()])

            pending = 0
            processed = 0
            for raw_job in raw_jobs:
                processed += 1
                posted = ensure_aware(raw_job.source_posted_at)
                if posted and (fresh_posted_at is None or posted > fresh_posted_at):
                    fresh_posted_at = posted

                problem = validate_raw_job(raw_job)
                if problem:
                    counters["rejected"] += 1
                    note_error(f"[rejected] job {raw_job.source_job_id!r}: {problem}")
                    continue
                if patterns and not any(p.search(raw_job.raw_title) for p in patterns):
                    counters["filtered"] += 1
                    continue
                if geography is not None and geography.excluded(raw_job.raw_location, None, raw_job.raw_metadata) is not None:
                    counters["filtered"] += 1
                    counters["filtered_geography"] += 1
                    known = ref_index.get(raw_job.source_job_id)
                    if known is not None and known.job is not None:
                        # Still open at the source: the stale-job sweep must not close it.
                        observed_job_ids.add(known.job.id)
                    continue

                try:
                    # SAVEPOINT: one malformed job cannot poison the run.
                    with db.begin_nested():
                        await self._ingest_one(
                            db,
                            raw_job,
                            company_name,
                            counters,
                            observed_job_ids,
                            touched_opportunities,
                            resume_cutoff,
                            ref_index,
                            link_index,
                        )
                    pending += 1
                    if pending >= COMMIT_BATCH_SIZE:
                        run_row.checkpoint = {"processed": processed, "of": candidates}
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
                    note_error(message)
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
                        if counters["closed"]:
                            self._close_opportunities(db, source_type, identifier, run_row.started_at)
                    except SQLAlchemyError as exc:
                        db.rollback()
                        note_error(f"Stale-job sweep failed: {exc}")
                        logger.exception("Stale-job sweep failed")
                else:
                    logger.info("Skipping stale-job sweep: %s", decision.reason)

        except SourceError as exc:
            final_status = RUN_STATUS_FAILED
            failure_kind = exc.kind
            message = f"Source adapter error ({exc.kind}): {exc}"
            logger.error(message)
            note_error(message)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the caller
            final_status = RUN_STATUS_FAILED
            failure_kind = "pipeline_error"
            message = f"Unexpected discovery pipeline failure: {type(exc).__name__}: {exc}"
            logger.exception("Discovery run %s failed", run_id)
            note_error(message)
        finally:
            if errors_dropped:
                collected_errors.append(f"... {errors_dropped} more error(s) not stored")
            run_row = self._finalize_run(
                db,
                run_id=run_id,
                status=final_status if final_status != RUN_STATUS_RUNNING else RUN_STATUS_FAILED,
                candidates=candidates,
                counters=counters,
                errors=collected_errors,
                duration=round(time.time() - start_time, 2),
                failure_kind=failure_kind,
                adapter=adapter,
                ai_calls=self.llm_fallback.calls - ai_calls_before,
                ai_cache_hits=self.llm_fallback.cache_hits - ai_hits_before,
                fresh_posted_at=fresh_posted_at,
            )

        # Cheap candidate projection: DISCOVERED rows only, no scoring.
        self._project_candidates(db, touched_opportunities)

        logger.info(
            "Discovery run %s finished (%s): %d new, %d updated, %d unchanged, %d duplicate, "
            "%d rejected, %d failed, %d closed, %d/%d opportunities new/linked in %.2fs",
            run_id,
            run_row.status,
            run_row.jobs_new,
            run_row.jobs_updated,
            run_row.jobs_unchanged or 0,
            run_row.jobs_duplicate,
            run_row.jobs_rejected or 0,
            run_row.jobs_failed,
            run_row.jobs_closed or 0,
            run_row.opportunities_new or 0,
            run_row.opportunities_linked or 0,
            run_row.duration_seconds or 0,
        )
        return run_row

    # ------------------------------------------------------------------ #

    @staticmethod
    def _prefetch_references(
        db: Session, source_type: JobSourceType, raw_jobs: List[RawJob]
    ) -> Dict[str, SourceReferenceRow]:
        """Existing source references for this batch, keyed by source_job_id."""
        ids = sorted({r.source_job_id for r in raw_jobs if r.source_job_id})
        index: Dict[str, SourceReferenceRow] = {}
        for start in range(0, len(ids), PREFETCH_CHUNK):
            chunk = ids[start : start + PREFETCH_CHUNK]
            rows = (
                db.query(SourceReferenceRow)
                .options(joinedload(SourceReferenceRow.job))
                .filter(
                    SourceReferenceRow.source == source_type.value,
                    SourceReferenceRow.source_job_id.in_(chunk),
                )
                .all()
            )
            index.update({row.source_job_id: row for row in rows})
        return index

    @staticmethod
    def _prefetch_links(db: Session, job_ids: List[str]) -> Dict[str, OpportunityJobRow]:
        """Existing opportunity links for known jobs, keyed by job_id."""
        ids = sorted(set(job_ids))
        index: Dict[str, OpportunityJobRow] = {}
        for start in range(0, len(ids), PREFETCH_CHUNK):
            chunk = ids[start : start + PREFETCH_CHUNK]
            rows = (
                db.query(OpportunityJobRow)
                .options(joinedload(OpportunityJobRow.opportunity))
                .filter(OpportunityJobRow.job_id.in_(chunk))
                .all()
            )
            index.update({row.job_id: row for row in rows})
        return index

    async def _ingest_one(
        self,
        db: Session,
        raw_job: RawJob,
        company_name: Optional[str],
        counters: Dict[str, int],
        observed_job_ids: Set[str],
        touched_opportunities: Set[str],
        resume_cutoff: Optional[datetime],
        ref_index: Optional[Dict[str, SourceReferenceRow]] = None,
        link_index: Optional[Dict[str, OpportunityJobRow]] = None,
    ) -> None:
        """Validate → hash → (fast path) → normalise → dedup → persist → opportunity."""
        raw_hash = compute_raw_hash(raw_job)
        now = db_now()
        ref_index = ref_index if ref_index is not None else {}
        link_index = link_index if link_index is not None else {}
        ref = ref_index.get(raw_job.source_job_id)
        if ref is None and not ref_index:
            ref = (
                db.query(SourceReferenceRow)
                .filter_by(source=raw_job.source.value, source_job_id=raw_job.source_job_id)
                .first()
            )

        # Resume: the interrupted run already handled this posting.
        if ref is not None and resume_cutoff is not None and ref.last_seen_at and ref.last_seen_at >= resume_cutoff:
            counters["resumed_skipped"] += 1
            if ref.job is not None:
                observed_job_ids.add(ref.job.id)
            return

        # Fast path: identical raw payload -> touch, resolve, done. No extraction,
        # and with the prefetched link no SELECT either.
        if ref is not None and ref.content_hash == raw_hash and ref.job is not None:
            job_row = ref.job
            ref.last_seen_at = now
            ref.fetched_at = now
            job_row.last_seen_at = now
            if job_row.job_status in ("CLOSED", "EXPIRED"):
                job_row.job_status = "ACTIVE"
                job_row.closed_at = None
            job_row.freshness = freshness_for_row(job_row).value
            opportunity, created, how = OpportunityRepository.resolve_opportunity(
                db, job_row, link=link_index.get(job_row.id)
            )
            self._count_opportunity(counters, created, how)
            touched_opportunities.add(opportunity.id)
            observed_job_ids.add(job_row.id)
            # A re-observed posting is still a duplicate of what we hold (the
            # original run-level meaning); "unchanged" records that it was
            # served by the raw-hash fast path without extraction.
            counters["unchanged"] += 1
            counters["duplicate"] += 1
            return

        normalized = self.normalizer.normalize(raw_job, company_name=company_name)
        enriched = await self.llm_fallback.enrich_if_needed(normalized, raw_job)
        # The prefetch proved whether a source reference exists; when the batch
        # index is authoritative and empty for this id, the deduplicator can
        # skip its two source lookups.
        if self._dedup_accepts_hint:
            result = self.deduplicator.process(
                db, enriched, commit=False, skip_source_lookup=bool(ref_index) and ref is None
            )
        else:
            result = self.deduplicator.process(db, enriched, commit=False)
        job_row: JobRow = result.job_row

        if ref is None:
            # The deduplicator just created (or linked) the reference row.
            ref = (
                db.query(SourceReferenceRow)
                .filter_by(source=raw_job.source.value, source_job_id=raw_job.source_job_id)
                .first()
            )
            if ref is not None:
                ref_index[raw_job.source_job_id] = ref
        if ref is not None:
            ref.content_hash = raw_hash
            ref.fetched_at = now
            ref.parse_status = enriched.processing_status.value
        job_row.freshness = freshness_for_row(job_row).value

        opportunity, created, how = OpportunityRepository.resolve_opportunity(
            db,
            job_row,
            link=link_index.get(job_row.id),
            assume_unlinked=result.status == "NEW",
        )
        self._count_opportunity(counters, created, how)
        if result.reopened and how != "repost":
            # Same posting id or key came back after we closed it: a repost of
            # the row itself, even when its opportunity never closed.
            counters["reposted"] += 1
        touched_opportunities.add(opportunity.id)
        observed_job_ids.add(job_row.id)

        if result.status == "NEW":
            counters["new"] += 1
        elif result.status == "UPDATED":
            counters["updated"] += 1
            counters["versioned"] += 1
        elif result.status in ("DUPLICATE", "CROSS_SOURCE_DUPLICATE"):
            counters["duplicate"] += 1

    @staticmethod
    def _close_opportunities(db: Session, source_type: JobSourceType, identifier: str, since: datetime) -> int:
        """After a sweep, close opportunities whose every linked job is now closed."""
        closed_jobs = (
            db.query(JobRow)
            .filter(
                JobRow.source == source_type.value,
                JobRow.source_identifier == identifier,
                JobRow.job_status == "CLOSED",
                JobRow.closed_at >= since,
            )
            .all()
        )
        seen: Set[str] = set()
        closed = 0
        for job in closed_jobs:
            link = db.query(OpportunityJobRow).filter_by(job_id=job.id).first()
            if link is None or link.opportunity_id in seen:
                continue
            seen.add(link.opportunity_id)
            if OpportunityRepository.close_opportunity_if_all_jobs_closed(db, link.opportunity):
                closed += 1
        if closed:
            db.commit()
        return closed

    @staticmethod
    def _count_opportunity(counters: Dict[str, int], created: bool, how: str) -> None:
        if created:
            counters["opportunities_new"] += 1
        elif how in ("identity_key", "repost"):
            counters["opportunities_linked"] += 1
        if how == "repost":
            counters["reposted"] += 1

    @staticmethod
    def _resume_cutoff(db: Session, resume_from_run_id: Optional[str]) -> Optional[datetime]:
        if not resume_from_run_id:
            return None
        previous = db.query(DiscoveryRunRow).filter_by(id=resume_from_run_id).first()
        if previous is None:
            logger.warning("resume_from_run_id %s not found; running from scratch", resume_from_run_id)
            return None
        return previous.started_at

    def _project_candidates(self, db: Session, opportunity_ids: Set[str]) -> None:
        """Ensure DISCOVERED candidate rows for configured tenants. Inserts only."""
        tenants = [t.strip() for t in (settings.discovery_project_tenants or "").split(",") if t.strip()]
        if not tenants or not opportunity_ids:
            return
        for tenant_id in tenants:
            try:
                created = OpportunityRepository(db, tenant_id).ensure_candidate_opportunities(
                    sorted(opportunity_ids), actor="discovery"
                )
                db.commit()
                if created:
                    logger.info("Projected %d new opportunities to tenant %s", created, tenant_id)
            except Exception:  # noqa: BLE001 - projection must never fail a finished run
                db.rollback()
                logger.exception("Candidate projection failed for tenant %s", tenant_id)

    def _finalize_run(
        self,
        db: Session,
        run_id: str,
        status: str,
        candidates: int,
        counters: Dict[str, int],
        errors: List[str],
        duration: float,
        failure_kind: Optional[str] = None,
        adapter: Optional[JobSource] = None,
        ai_calls: int = 0,
        ai_cache_hits: int = 0,
        fresh_posted_at: Optional[datetime] = None,
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

        stats = adapter.stats if adapter is not None else None
        run_row.status = status
        run_row.failure_kind = failure_kind
        run_row.candidates_discovered = candidates
        run_row.pages_fetched = stats.requests if stats else candidates
        run_row.jobs_new = counters["new"]
        run_row.jobs_updated = counters["updated"]
        run_row.jobs_duplicate = counters["duplicate"]
        run_row.jobs_failed = counters["failed"]
        run_row.jobs_closed = counters["closed"]
        run_row.jobs_rejected = counters["rejected"]
        run_row.jobs_unchanged = counters["unchanged"]
        run_row.jobs_versioned = counters["versioned"]
        run_row.jobs_reposted = counters["reposted"]
        run_row.jobs_filtered = counters["filtered"]
        run_row.jobs_resumed_skipped = counters["resumed_skipped"]
        run_row.opportunities_new = counters["opportunities_new"]
        run_row.opportunities_linked = counters["opportunities_linked"]
        run_row.network_requests = stats.requests if stats else 0
        run_row.rate_limit_hits = stats.rate_limit_hits if stats else 0
        run_row.ai_calls = ai_calls
        run_row.ai_cache_hits = ai_cache_hits
        # Reassign (not mutate) so SQLAlchemy detects the JSON change.
        run_row.errors = list(errors)
        run_row.checkpoint = {"processed": candidates, "of": candidates, "terminal": True}
        if counters.get("filtered_geography"):
            run_row.checkpoint["filtered_geography"] = counters["filtered_geography"]
        run_row.completed_at = db_now()
        run_row.duration_seconds = duration
        db.commit()

        try:
            SourceHealthRepository(db).record_run(run_row, fresh_posted_at=to_db(fresh_posted_at), commit=True)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Could not update source health for run %s", run_id)
        return run_row

    # ------------------------------------------------------------------ #

    async def run_many(
        self,
        targets: List[dict],
        session_factory,
        trigger: str = "manual",
    ) -> List[DiscoveryRunRow]:
        """Run discovery for several boards with a bounded concurrency cap.

        ``targets`` entries are ``{"source": JobSourceType, "identifier": str,
        "company_name": str | None, "title_include": [..], "resume_from_run_id": ..}``.

        Each target gets its **own session**: SQLAlchemy sessions are not
        thread/task safe, and sharing one would corrupt the transaction state
        that the per-job savepoints depend on. Failures are isolated per target,
        so one dead board cannot stop the others.
        """
        semaphore = asyncio.Semaphore(max(1, settings.default_discovery_concurrency))

        async def _one(target: dict) -> Optional[DiscoveryRunRow]:
            async with semaphore:
                session = session_factory()
                try:
                    return await self.run_discovery(
                        db=session,
                        source_type=JobSourceType(target["source"]),
                        identifier=target["identifier"],
                        company_name=target.get("company_name"),
                        trigger=trigger,
                        resume_from_run_id=target.get("resume_from_run_id"),
                        title_include=target.get("title_include"),
                        geography=discovery_geography(session),
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
