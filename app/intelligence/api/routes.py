"""Phase 3 intelligence API — real persisted matches, not stubs."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.database import get_db, get_session_factory
from app.intelligence.database.models import JobMatchRow, MatchRunRow, RequirementAssessmentRow
from app.intelligence.models.enums import EligibilityStatus
from app.intelligence.services.factory import build_orchestrator
from app.intelligence.services.match_persistence import (
    latest_matches_query,
    latest_run,
    run_matching,
    stale_job_ids,
)
from app.jobs.database.models import JobRow
from app.security import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v3/matches", tags=["intelligence"])


class MatchSummary(BaseModel):
    """One row of the shortlist."""

    id: str
    job_id: str
    title: str
    company: str
    location: Optional[str] = None
    source: str
    remote_type: Optional[str] = None
    employment_type: Optional[str] = None
    fit_score: int
    priority: str
    match_type: str
    confidence: str
    eligibility_status: str
    eligibility_confidence: str
    component_scores: Dict[str, float] = Field(default_factory=dict)
    strengths: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    posted_at: Optional[Any] = None
    last_seen_at: Optional[Any] = None
    job_status: Optional[str] = None
    application_url: Optional[str] = None
    source_url: Optional[str] = None


class AssessmentResponse(BaseModel):
    requirement_name: str
    requirement_original_text: Optional[str] = None
    category: str
    strictness: str
    status: str
    evidence_strength: str
    evidence_references: List[str] = Field(default_factory=list)
    confidence: str
    impact: Optional[str] = None
    weight: float = 0.0
    contribution: float = 0.0
    explanation: Optional[str] = None


class MatchDetail(MatchSummary):
    explanation: str
    eligibility_reasons: List[str] = Field(default_factory=list)
    blocking_reasons: List[str] = Field(default_factory=list)
    policy_version: str
    engine_version: str
    run_id: str
    evaluated_at: Optional[Any] = None
    assessments: List[AssessmentResponse] = Field(default_factory=list)


class MatchListResponse(BaseModel):
    run_id: Optional[str] = None
    total: int
    limit: int
    offset: int
    items: List[MatchSummary] = Field(default_factory=list)


class RecalculateRequest(BaseModel):
    job_ids: Optional[List[str]] = Field(
        default=None, description="Restrict to these jobs; omit to score everything."
    )
    only_stale: bool = Field(
        default=False,
        description="Score only jobs whose content changed since the last run.",
    )
    include_closed: bool = False
    limit: Optional[int] = Field(default=None, ge=1, le=5000)


class MatchRunResponse(BaseModel):
    id: str
    status: str
    policy_version: str
    engine_version: str
    started_at: Any
    completed_at: Optional[Any] = None
    jobs_processed: int = 0
    jobs_matched: int = 0
    jobs_failed: int = 0
    duration_seconds: Optional[float] = None
    trigger: Optional[str] = None
    errors: List[str] = Field(default_factory=list)


def _summary(match: JobMatchRow, job: Optional[JobRow]) -> MatchSummary:
    return MatchSummary(
        id=match.id,
        job_id=match.job_id,
        title=job.title if job else "(job removed)",
        company=job.company if job else "",
        location=job.location if job else None,
        source=job.source if job else "",
        remote_type=job.remote_type if job else None,
        employment_type=job.employment_type if job else None,
        fit_score=match.fit_score,
        priority=match.priority,
        match_type=match.match_type,
        confidence=match.confidence,
        eligibility_status=match.eligibility_status,
        eligibility_confidence=match.eligibility_confidence,
        component_scores=match.component_scores or {},
        strengths=match.strengths or [],
        gaps=match.gaps or [],
        uncertainties=match.uncertainties or [],
        posted_at=job.posted_at if job else None,
        last_seen_at=job.last_seen_at if job else None,
        job_status=job.job_status if job else None,
        application_url=job.application_url if job else None,
        source_url=job.source_url if job else None,
    )


@router.get("/", response_model=MatchListResponse)
def list_matches(
    run_id: Optional[str] = Query(default=None, description="Defaults to the latest completed run."),
    eligibility: Optional[EligibilityStatus] = None,
    priority: Optional[str] = None,
    min_score: int = Query(default=0, ge=0, le=100),
    company: Optional[str] = None,
    sort: str = Query(default="score", pattern="^(score|posted|company|priority)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List persisted job matches, ranked. Empty until a match run has been executed."""
    query = latest_matches_query(db, run_id)
    if query is None:
        return MatchListResponse(run_id=None, total=0, limit=limit, offset=offset, items=[])

    query = query.join(JobRow, JobRow.id == JobMatchRow.job_id)

    if eligibility:
        query = query.filter(JobMatchRow.eligibility_status == eligibility.value)
    if priority:
        query = query.filter(JobMatchRow.priority == priority.upper())
    if min_score:
        query = query.filter(JobMatchRow.fit_score >= min_score)
    if company:
        query = query.filter(JobRow.company.ilike(f"%{company}%"))

    total = query.count()

    sort_columns = {
        "score": JobMatchRow.fit_score,
        "posted": JobRow.posted_at,
        "company": JobRow.company,
        "priority": JobMatchRow.priority,
    }
    column = sort_columns[sort]
    query = query.order_by(column.desc() if order == "desc" else column.asc())

    rows = query.offset(offset).limit(limit).all()
    jobs = {
        job.id: job
        for job in db.query(JobRow).filter(JobRow.id.in_([r.job_id for r in rows])).all()
    }

    resolved_run = rows[0].run_id if rows else (latest_run(db).id if latest_run(db) else None)
    return MatchListResponse(
        run_id=run_id or resolved_run,
        total=total,
        limit=limit,
        offset=offset,
        items=[_summary(row, jobs.get(row.job_id)) for row in rows],
    )


@router.get("/runs", response_model=List[MatchRunResponse])
def list_match_runs(
    limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)
):
    """Match run history, for observability of the intelligence layer."""
    runs = db.query(MatchRunRow).order_by(MatchRunRow.started_at.desc()).limit(limit).all()
    return [
        MatchRunResponse(
            id=r.id,
            status=r.status,
            policy_version=r.policy_version,
            engine_version=r.engine_version,
            started_at=r.started_at,
            completed_at=r.completed_at,
            jobs_processed=r.jobs_processed or 0,
            jobs_matched=r.jobs_matched or 0,
            jobs_failed=r.jobs_failed or 0,
            duration_seconds=r.duration_seconds,
            trigger=r.trigger,
            errors=r.errors or [],
        )
        for r in runs
    ]


@router.post(
    "/recalculate",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def recalculate_matches(
    request: RecalculateRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    wait: bool = Query(default=False, description="Run synchronously and return the finished run."),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    """Recompute matches for stored jobs.

    Runs in the background by default: scoring the whole table can take longer
    than an HTTP request should live. Poll ``/api/v3/matches/runs``.

    The run scores against the requesting tenant's Career Brain and projects
    into that tenant's candidate opportunities; the tenant is explicit here
    and never falls back to the default inside the pipeline.
    """
    job_ids = request.job_ids
    if request.only_stale and not job_ids:
        job_ids = list(stale_job_ids(db))
        if not job_ids:
            return {"status": "skipped", "detail": "No jobs have changed since the last match run."}

    if wait:
        # Already finished by the time we respond, so 200 rather than 202.
        response.status_code = status.HTTP_200_OK
        run = run_matching(
            db=db,
            orchestrator=_orchestrator_for(db, tenant_id),
            job_ids=job_ids,
            trigger="manual",
            include_closed=request.include_closed,
            limit=request.limit,
            tenant_id=tenant_id,
        )
        _sync_opportunities(db, run.id, tenant_id)
        return MatchRunResponse(
            id=run.id,
            status=run.status,
            policy_version=run.policy_version,
            engine_version=run.engine_version,
            started_at=run.started_at,
            completed_at=run.completed_at,
            jobs_processed=run.jobs_processed or 0,
            jobs_matched=run.jobs_matched or 0,
            jobs_failed=run.jobs_failed or 0,
            duration_seconds=run.duration_seconds,
            trigger=run.trigger,
            errors=run.errors or [],
        )

    background_tasks.add_task(
        _background_match, job_ids, request.include_closed, request.limit, "manual", tenant_id
    )
    return {
        "status": "accepted",
        "detail": f"Match run scheduled for {len(job_ids) if job_ids else 'all'} job(s)",
        "poll_url": "/api/v3/matches/runs",
    }


def _orchestrator_for(db: Session, tenant_id: str):
    """Match orchestrator bound to one tenant's Career Brain (never the default by accident)."""
    from app.services.career_brain import CareerBrainService

    brain = CareerBrainService(db=db, tenant_id=tenant_id)
    brain.load()
    return build_orchestrator(brain)


def _background_match(
    job_ids: Optional[List[str]],
    include_closed: bool,
    limit: Optional[int],
    trigger: str,
    tenant_id: str,
) -> None:
    """Background entry point with its own session."""
    session = get_session_factory()()
    try:
        run = run_matching(
            db=session,
            orchestrator=_orchestrator_for(session, tenant_id),
            job_ids=job_ids,
            trigger=trigger,
            include_closed=include_closed,
            limit=limit,
            tenant_id=tenant_id,
        )
        _sync_opportunities(session, run.id, tenant_id)
    except Exception:  # noqa: BLE001 - never kill the worker
        logger.exception("Background match run failed")
    finally:
        session.close()


def _sync_opportunities(db: Session, run_id: str, tenant_id: str) -> None:
    """Project a finished match run into one tenant's candidate opportunities.

    ``match_runs`` itself has no tenant column (the matcher scores jobs against
    the Career Brain it was handed); the tenant is therefore carried explicitly
    from the request to here. A failure is logged and never fails the run.
    """
    from app.pipeline.sync import sync_run

    if not tenant_id:
        logger.error("Opportunity sync skipped for match run %s: no tenant given", run_id)
        return
    try:
        sync_run(db, tenant_id, run_id)
    except Exception:  # noqa: BLE001 - the match run already succeeded
        logger.exception("Opportunity sync failed for match run %s", run_id)


@router.get("/{job_id}", response_model=MatchDetail)
def get_match(job_id: str, run_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Full match detail for one job, including every requirement assessment."""
    query = latest_matches_query(db, run_id)
    if query is None:
        raise HTTPException(status_code=404, detail="No match run has been executed yet.")

    match = query.filter(JobMatchRow.job_id == job_id).first()
    if match is None:
        raise HTTPException(status_code=404, detail=f"No match found for job {job_id}")

    job = db.query(JobRow).filter_by(id=match.job_id).first()
    assessments = (
        db.query(RequirementAssessmentRow)
        .filter_by(job_match_id=match.id)
        .order_by(RequirementAssessmentRow.contribution.desc())
        .all()
    )

    summary = _summary(match, job)
    return MatchDetail(
        **summary.model_dump(),
        explanation=match.explanation,
        eligibility_reasons=match.eligibility_reasons or [],
        blocking_reasons=match.blocking_reasons or [],
        policy_version=match.policy_version,
        engine_version=match.engine_version,
        run_id=match.run_id,
        evaluated_at=match.evaluated_at,
        assessments=[
            AssessmentResponse(
                requirement_name=a.requirement_name,
                requirement_original_text=a.requirement_original_text,
                category=a.requirement_category,
                strictness=a.requirement_strictness,
                status=a.status,
                evidence_strength=a.evidence_strength,
                evidence_references=a.evidence_references or [],
                confidence=a.confidence,
                impact=a.impact,
                weight=a.weight or 0.0,
                contribution=a.contribution or 0.0,
                explanation=a.explanation,
            )
            for a in assessments
        ],
    )
