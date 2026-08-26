"""Internal HTML dashboard views for job discovery observability."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.jobs.database.models import DiscoveryRunRow, JobRow
from app.intelligence.database.models import JobMatchRow

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
