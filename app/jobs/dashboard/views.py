"""Internal HTML dashboard views for job discovery observability."""

from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timeutils import age as time_age, ensure_aware
from app.database import get_db
from app.jobs.database.models import DiscoveryRunRow, JobRow
from app.intelligence.database.models import JobMatchRow, RequirementAssessmentRow
from app.intelligence.services.match_persistence import latest_matches_query, latest_run

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
def dashboard_overview(request: Request, db: Session = Depends(get_db)):
    """Overview dashboard showing metrics, source distribution, and recent runs."""
    total_jobs = db.query(JobRow).count()
    active_jobs = db.query(JobRow).filter_by(job_status="ACTIVE").count()

    sources_count = (
        db.query(JobRow.source, func.count(JobRow.id))
        .group_by(JobRow.source)
        .all()
    )
    emp_type_count = (
        db.query(JobRow.employment_type, func.count(JobRow.id))
        .group_by(JobRow.employment_type)
        .all()
    )

    recent_runs = (
        db.query(DiscoveryRunRow)
        .order_by(DiscoveryRunRow.started_at.desc())
        .limit(5)
        .all()
    )
    recent_jobs = (
        db.query(JobRow)
        .order_by(JobRow.last_seen_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={
            "total_jobs": total_jobs,
            "active_jobs": active_jobs,
            "sources_count": dict(sources_count),
            "emp_type_count": dict(emp_type_count),
            "recent_runs": recent_runs,
            "recent_jobs": recent_jobs,
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
def dashboard_jobs(
    request: Request,
    source: Optional[str] = None,
    company: Optional[str] = None,
    employment_type: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Searchable and filterable job list."""
    query = db.query(JobRow)

    if source:
        query = query.filter_by(source=source)
    if company:
        query = query.filter(JobRow.company.ilike(f"%{company}%"))
    if employment_type:
        query = query.filter_by(employment_type=employment_type)
    if q:
        query = query.filter(
            (JobRow.title.ilike(f"%{q}%"))
            | (JobRow.company.ilike(f"%{q}%"))
            | (JobRow.location.ilike(f"%{q}%"))
        )

    jobs_query = query.outerjoin(JobMatchRow, JobRow.id == JobMatchRow.job_id).add_columns(JobMatchRow).order_by(JobRow.last_seen_at.desc()).limit(100)
    jobs_with_matches = jobs_query.all()
    # jobs_with_matches is a list of tuples (JobRow, JobMatchRow|None)

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "jobs_with_matches": jobs_with_matches,
            "source_filter": source or "",
            "company_filter": company or "",
            "emp_filter": employment_type or "",
            "search_q": q or "",
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def dashboard_job_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Detailed job view showing normalized data, original source content, and versions."""
    job = db.query(JobRow).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    match = db.query(JobMatchRow).filter_by(job_id=job_id).first()

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "job": job,
            "match": match,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
def dashboard_runs(request: Request, db: Session = Depends(get_db)):
    """Discovery runs execution history."""
    runs = (
        db.query(DiscoveryRunRow)
        .order_by(DiscoveryRunRow.started_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={
            "runs": runs,
        },
    )


# --------------------------------------------------------------------------- #
# Phase 3 shortlist / ranking dashboard (PRD P3.8)
# --------------------------------------------------------------------------- #

SHORTLIST_SORT_COLUMNS = {
    "score": JobMatchRow.fit_score,
    "posted": JobRow.posted_at,
    "company": JobRow.company,
    "priority": JobMatchRow.priority,
}


def _freshness(posted_at: Optional[datetime]) -> dict:
    """Human relative age for a posting date, or "unknown" when absent.

    Never fabricates a date: a missing ``posted_at`` renders as "unknown"
    rather than being treated as "just posted".
    """
    if posted_at is None:
        return {"label": "unknown", "title": None}

    aware = ensure_aware(posted_at)
    seconds = max(0.0, time_age(aware).total_seconds())
    if seconds < 3600:
        label = f"{max(1, int(seconds // 60))}m ago"
    elif seconds < 86400:
        label = f"{int(seconds // 3600)}h ago"
    else:
        label = f"{int(seconds // 86400)}d ago"
    return {"label": label, "title": aware.strftime("%Y-%m-%d %H:%M UTC")}


@router.get("/shortlist", response_class=HTMLResponse)
def dashboard_shortlist(
    request: Request,
    run_id: Optional[str] = None,
    sort: str = "score",
    order: str = "desc",
    eligibility: Optional[str] = None,
    priority: Optional[str] = None,
    min_score: int = 0,
    company: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Ranked shortlist of persisted job matches.

    Renders whatever the latest (or a specific) match run produced. When no
    run has ever completed, ``latest_matches_query`` returns ``None`` and the
    template shows an empty state instead of erroring.
    """
    sort = sort if sort in SHORTLIST_SORT_COLUMNS else "score"
    order = order if order in ("asc", "desc") else "desc"
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    min_score = max(0, min(min_score, 100))

    query = latest_matches_query(db, run_id)
    run_exists = query is not None

    items = []
    total = 0
    resolved_run_id = run_id

    if query is not None:
        query = query.join(JobRow, JobRow.id == JobMatchRow.job_id)

        if eligibility:
            query = query.filter(JobMatchRow.eligibility_status == eligibility.upper())
        if priority:
            query = query.filter(JobMatchRow.priority == priority.upper())
        if min_score:
            query = query.filter(JobMatchRow.fit_score >= min_score)
        if company:
            query = query.filter(JobRow.company.ilike(f"%{company}%"))

        total = query.count()

        column = SHORTLIST_SORT_COLUMNS[sort]
        query = query.order_by(column.desc() if order == "desc" else column.asc())

        rows = query.offset(offset).limit(limit).all()
        jobs = {
            job.id: job
            for job in db.query(JobRow).filter(JobRow.id.in_([r.job_id for r in rows])).all()
        }

        for match in rows:
            job = jobs.get(match.job_id)
            if job is None:
                # Job was deleted after the run scored it - skip rather than crash.
                continue
            fresh = _freshness(job.posted_at)
            items.append(
                {
                    "match": match,
                    "job": job,
                    "has_uncertainty": bool(match.uncertainties) or match.eligibility_status == "UNCERTAIN",
                    "freshness_label": fresh["label"],
                    "freshness_title": fresh["title"],
                }
            )

        if run_id:
            resolved_run_id = run_id
        elif rows:
            resolved_run_id = rows[0].run_id
        else:
            latest = latest_run(db)
            resolved_run_id = latest.id if latest else None

    filters = {
        "sort": sort,
        "order": order,
        "eligibility": eligibility or "",
        "priority": priority or "",
        "min_score": min_score,
        "company": company or "",
        "limit": limit,
        "offset": offset,
    }

    def _page_url(new_offset: int) -> str:
        # Drop unset text filters from the query string; always keep the rest
        # so a link built from these params reproduces this exact page.
        params = {k: v for k, v in filters.items() if v != ""}
        params["offset"] = new_offset
        return "/dashboard/shortlist?" + urlencode(params)

    return templates.TemplateResponse(
        request=request,
        name="shortlist.html",
        context={
            "run_exists": run_exists,
            "run_id": resolved_run_id,
            "items": items,
            "total": total,
            "filters": filters,
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_url": _page_url(max(0, offset - limit)),
            "next_url": _page_url(offset + limit),
        },
    )


@router.get("/shortlist/{job_id}", response_class=HTMLResponse)
def dashboard_match_detail(
    request: Request,
    job_id: str,
    run_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Full match explanation for one job: score breakdown, eligibility, requirements."""
    query = latest_matches_query(db, run_id)
    if query is None:
        raise HTTPException(status_code=404, detail="No match run has been executed yet.")

    match = query.filter(JobMatchRow.job_id == job_id).first()
    if match is None:
        raise HTTPException(status_code=404, detail=f"No match found for job {job_id}")

    job = db.query(JobRow).filter_by(id=match.job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    assessments = (
        db.query(RequirementAssessmentRow)
        .filter_by(job_match_id=match.id)
        .order_by(RequirementAssessmentRow.contribution.desc())
        .all()
    )

    fresh = _freshness(job.posted_at)

    return templates.TemplateResponse(
        request=request,
        name="match_detail.html",
        context={
            "job": job,
            "match": match,
            "assessments": assessments,
            "has_uncertainty": bool(match.uncertainties) or match.eligibility_status == "UNCERTAIN",
            "freshness_label": fresh["label"],
            "freshness_title": fresh["title"],
        },
    )
