"""Persistence for Phase 3 match results.

The ``match_runs`` / ``job_matches`` / ``requirement_assessments`` /
``match_policies`` tables existed since the Phase 3 migration but nothing ever
wrote to them, so ``/api/v3/matches`` had nothing to return. This module is
what makes them real.

Transaction shape mirrors ingestion: a run row is committed up front so the run
is observable, each job is scored inside a SAVEPOINT so one failure cannot
poison the batch, and the run always reaches a terminal status.

Idempotency: matches are keyed ``(job_id, run_id)`` by a unique index, and a
re-run of the same policy over an unchanged job produces an identical score, so
recalculating is always safe.
"""

import logging
import time
from datetime import timedelta
from typing import Iterable, List, Optional, Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.timeutils import db_now
from app.intelligence.adapters.job_adapter import job_row_to_normalized
from app.intelligence.database.models import (
    JobMatchRow,
    MatchPolicyRow,
    MatchRunRow,
    RequirementAssessmentRow,
)
from app.intelligence.models.job_match import JobMatch
from app.intelligence.services.fit_scoring_engine import (
    DEFAULT_POLICY_WEIGHTS,
    ENGINE_VERSION,
    POLICY_VERSION,
)
from app.intelligence.services.match_orchestrator import MatchOrchestrator
from app.jobs.database.models import JobRow
from app.jobs.models.enums import JobStatus

logger = logging.getLogger(__name__)

MATCH_COMMIT_BATCH = 25


def ensure_policy(db: Session, version: str = POLICY_VERSION, weights: Optional[dict] = None) -> MatchPolicyRow:
    """Record the scoring policy so a stored score can be reproduced later."""
    policy = db.query(MatchPolicyRow).filter_by(version=version).first()
    if policy is None:
        policy = MatchPolicyRow(
            version=version,
            description="Weighted component policy: technical, experience, education, preferences, eligibility",
            weights=dict(weights or DEFAULT_POLICY_WEIGHTS),
        )
        db.add(policy)
        db.flush()
    return policy


def _persist_match(db: Session, run_id: str, job_row: JobRow, match: JobMatch) -> JobMatchRow:
    """Write one match plus its requirement assessments."""
    metadata = match.generated_metadata or {}

    row = JobMatchRow(
        id=match.id,
        job_id=job_row.id,
        run_id=run_id,
        job_canonical_key=match.job_canonical_key,
        job_content_hash=metadata.get("job_content_hash") or job_row.content_hash,
        policy_version=match.match_run.policy_version,
        engine_version=match.match_run.engine_version,
        eligibility_status=match.eligibility.status.value,
        eligibility_confidence=metadata.get("eligibility_confidence", "UNKNOWN"),
        eligibility_reasons=list(match.eligibility.eligibility_reasons),
        blocking_reasons=list(match.eligibility.blocking_reasons),
        fit_score=match.fit_score,
        component_scores=dict(metadata.get("component_scores", {})),
        priority=match.priority,
        match_type=match.match_type.value,
        confidence=match.confidence.value,
        strengths=list(match.strengths),
        gaps=list(match.gaps),
        uncertainties=list(metadata.get("uncertainties", [])),
        explanation=match.explanation,
        evaluated_at=db_now(),
    )
    db.add(row)
    db.flush()

    for assessment in match.requirement_assessments:
        db.add(
            RequirementAssessmentRow(
                job_match_id=row.id,
                requirement_name=assessment.requirement.normalized_name[:128],
                requirement_original_text=assessment.requirement.original_text,
                requirement_category=assessment.requirement.category.value,
                requirement_strictness=assessment.requirement.strictness.value,
                status=assessment.status.value,
                evidence_strength=assessment.evidence_strength.value,
                evidence_references=list(assessment.evidence_references),
                confidence=assessment.confidence.value,
                impact=assessment.impact,
                weight=assessment.weight,
                contribution=assessment.contribution,
                explanation=assessment.explanation,
            )
        )
    return row


def run_matching(
    db: Session,
    orchestrator: MatchOrchestrator,
    job_ids: Optional[Sequence[str]] = None,
    trigger: str = "manual",
    include_closed: bool = False,
    limit: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> MatchRunRow:
    """Score jobs from the database and persist the results.

    Args:
        db: Active session.
        orchestrator: Configured match orchestrator.
        job_ids: Restrict to these jobs; ``None`` scores every eligible job.
        trigger: Provenance label ("manual", "scheduled", "JOB_DISCOVERY", ...).
        include_closed: Score jobs that have been closed at their source.
        limit: Cap the number of jobs scored in this run.
        tenant_id: The tenant whose Career Brain the orchestrator was built
            for; recorded on the run row (never defaulted).

    Returns:
        The finished :class:`MatchRunRow`.
    """
    started = time.time()

    policy = ensure_policy(db, orchestrator.scoring_engine.policy_version)
    run = MatchRunRow(
        policy_version=policy.version,
        engine_version=ENGINE_VERSION,
        career_brain_version="v1",
        started_at=db_now(),
        trigger=trigger,
        status="RUNNING",
        errors=[],
        tenant_id=tenant_id,
    )
    db.add(run)
    db.commit()
    run_id = run.id

    query = db.query(JobRow)
    if job_ids:
        query = query.filter(JobRow.id.in_(list(job_ids)))
    if not include_closed:
        query = query.filter(JobRow.job_status != JobStatus.CLOSED.value)
    query = query.order_by(JobRow.last_seen_at.desc())
    if limit:
        query = query.limit(limit)

    processed = 0
    matched = 0
    failed = 0
    errors: List[str] = []
    # Pre-set so the finally block always has a terminal status to write, even
    # if the run dies before the try block assigns one.
    status = "FAILED"

    try:
        job_rows = query.all()
        pending = 0

        for job_row in job_rows:
            processed += 1
            try:
                with db.begin_nested():
                    normalized = job_row_to_normalized(job_row)
                    match = orchestrator.evaluate_job(normalized, run_id=run_id)
                    _persist_match(db, run_id, job_row, match)
                    matched += 1
                pending += 1
                if pending >= MATCH_COMMIT_BATCH:
                    db.commit()
                    pending = 0
            except Exception as exc:  # noqa: BLE001 - isolate one bad job
                try:
                    db.rollback()
                except SQLAlchemyError:
                    logger.exception("Rollback failed after match error")
                failed += 1
                message = f"[{type(exc).__name__}] job {job_row.id} ('{job_row.title}'): {exc}"
                logger.error("Match error: %s", message)
                errors.append(message)
                pending = 0

        db.commit()
        status = "PARTIAL" if failed else "COMPLETED"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Match run %s failed", run_id)
        errors.append(f"Match run failure: {type(exc).__name__}: {exc}")
        status = "FAILED"
    finally:
        try:
            db.rollback()
        except SQLAlchemyError:
            logger.exception("Could not roll back before finalizing match run %s", run_id)

        run = db.query(MatchRunRow).filter_by(id=run_id).first()
        if run is not None:
            run.status = status
            run.jobs_processed = processed
            run.jobs_matched = matched
            run.jobs_failed = failed
            run.errors = list(errors)
            run.completed_at = db_now()
            run.duration_seconds = round(time.time() - started, 2)
            db.commit()

    logger.info(
        "Match run %s finished (%s): %d processed, %d matched, %d failed",
        run_id,
        run.status if run else "unknown",
        processed,
        matched,
        failed,
    )
    return run


#: A match run still RUNNING after this long belongs to a process that stopped.
STALE_MATCH_RUN_MINUTES = 60


def match_run_is_stale(run: MatchRunRow, now=None, minutes: Optional[int] = None) -> bool:
    if (run.status or "").upper() != "RUNNING" or run.started_at is None:
        return False
    limit = timedelta(minutes=minutes if minutes is not None else STALE_MATCH_RUN_MINUTES)
    return ((now or db_now()) - run.started_at) > limit


def reconcile_stale_match_runs(db: Session, minutes: Optional[int] = None) -> int:
    """Mark match runs a dead process left RUNNING as FAILED (interrupted). Returns the count.

    A restart while "Re-run matching" was working (2026-09-14) left a RUNNING row
    forever. Only the status changes: its scores, if any, were never the latest
    run (``latest_run`` reads COMPLETED / PARTIAL only).
    """
    now = db_now()
    stale = [run for run in db.query(MatchRunRow).filter(MatchRunRow.status == "RUNNING").all() if match_run_is_stale(run, now, minutes)]
    for run in stale:
        run.status = "FAILED"
        run.completed_at = now
        run.errors = list(run.errors or []) + ["interrupted: the process stopped before the run finished"]
    if stale:
        db.commit()
    return len(stale)


def latest_run(db: Session) -> Optional[MatchRunRow]:
    """Most recent match run that produced results."""
    return (
        db.query(MatchRunRow)
        .filter(MatchRunRow.status.in_(("COMPLETED", "PARTIAL")))
        .order_by(MatchRunRow.started_at.desc())
        .first()
    )


def latest_matches_query(db: Session, run_id: Optional[str] = None):
    """Query for the matches of a specific run, defaulting to the latest.

    Returns ``None`` when no run has ever completed, so callers can answer with
    an empty list rather than inventing data.
    """
    if run_id is None:
        run = latest_run(db)
        if run is None:
            return None
        run_id = run.id
    return db.query(JobMatchRow).filter(JobMatchRow.run_id == run_id)


def stale_job_ids(db: Session, run_id: Optional[str] = None) -> Iterable[str]:
    """Jobs whose content changed since they were last scored.

    Lets a recalculation touch only what actually moved, instead of rescoring
    the whole table — the cheap option on a free tier.
    """
    query = latest_matches_query(db, run_id)
    if query is None:
        return [row.id for row in db.query(JobRow.id).all()]

    scored = {(m.job_id, m.job_content_hash) for m in query.all()}
    scored_by_id = {job_id: content_hash for job_id, content_hash in scored}

    stale = []
    for job in db.query(JobRow).all():
        if job.id not in scored_by_id or scored_by_id[job.id] != job.content_hash:
            stale.append(job.id)
    return stale
