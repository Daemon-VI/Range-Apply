"""FastAPI REST API routes for Phase 2: Jobs, Discovery, and Stats."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.jobs.database.models import DiscoveryRunRow, JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.models.enums import EmploymentType, JobSourceType, JobStatus, ProcessingStatus, RemoteType
from app.jobs.pipeline.discovery_service import JobDiscoveryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["jobs-v2"])


# --- Pydantic Schemas for API Requests / Responses ---

class DiscoveryRunRequest(BaseModel):
    source: JobSourceType
    identifier: str = Field(description="Board token, company slug, or URL identifier (e.g. 'stripe', 'ramp')")
    company_name: Optional[str] = Field(default=None, description="Optional company display name")


class SourceReferenceResponse(BaseModel):
    id: str
    source: str
    source_job_id: str
    source_url: str
    application_url: Optional[str] = None
    first_seen_at: Any
    last_seen_at: Any


class JobVersionResponse(BaseModel):
    id: str
    content_hash: str
    title: Optional[str] = None
    changes_summary: Optional[str] = None
    created_at: Any


class JobDetailResponse(BaseModel):
    id: str
    canonical_key: str
    source: str
    source_job_id: str
    company: str
    title: str
    original_title: str
    description: str
    original_description: str
    location: Optional[str] = None
    locations: List[str] = []
    remote_type: str
    employment_type: str
    experience_level: str
    education_requirements: List[str] = []
    graduation_requirement: Dict[str, Any] = {}
    graduation_year_requirement: Optional[int] = None
    required_skills: List[str] = []
    preferred_skills: List[str] = []
    technologies: List[str] = []
    responsibilities: List[str] = []
    qualifications: List[str] = []
    salary_text: Optional[str] = None
    application_url: Optional[str] = None
    source_url: str
    posted_at: Optional[Any] = None
    deadline: Optional[Any] = None
    first_seen_at: Any
    last_seen_at: Any
    content_hash: str
    processing_status: str
    job_status: str
    source_references: List[SourceReferenceResponse] = []
    versions: List[JobVersionResponse] = []


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[Dict[str, Any]]


class DiscoveryRunResponse(BaseModel):
    id: str
    source: str
    source_identifier: str
    started_at: Any
    completed_at: Optional[Any] = None
    candidates_discovered: int
    pages_fetched: int
    jobs_new: int
    jobs_updated: int
    jobs_duplicate: int
    jobs_failed: int
    errors: List[str] = []
    duration_seconds: Optional[float] = None
    status: str


# --- API Endpoints ---

@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    source: Optional[JobSourceType] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    employment_type: Optional[EmploymentType] = None,
    remote_type: Optional[RemoteType] = None,
    job_status: Optional[JobStatus] = None,
    processing_status: Optional[ProcessingStatus] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Lists normalized jobs with optional filters and pagination."""
    query = db.query(JobRow)

    if source:
        query = query.filter(JobRow.source == source.value)
    if company:
        query = query.filter(JobRow.company.ilike(f"%{company}%"))
    if location:
        query = query.filter(JobRow.location.ilike(f"%{location}%"))
    if employment_type:
        query = query.filter(JobRow.employment_type == employment_type.value)
    if remote_type:
        query = query.filter(JobRow.remote_type == remote_type.value)
    if job_status:
        query = query.filter(JobRow.job_status == job_status.value)
    if processing_status:
        query = query.filter(JobRow.processing_status == processing_status.value)
    if search:
        query = query.filter(
            (JobRow.title.ilike(f"%{search}%"))
            | (JobRow.company.ilike(f"%{search}%"))
            | (JobRow.description.ilike(f"%{search}%"))
        )

    total = query.count()
    rows = query.order_by(JobRow.last_seen_at.desc()).offset(offset).limit(limit).all()

    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "canonical_key": r.canonical_key,
            "source": r.source,
            "source_job_id": r.source_job_id,
            "company": r.company,
            "title": r.title,
            "location": r.location,
            "remote_type": r.remote_type,
            "employment_type": r.employment_type,
            "experience_level": r.experience_level,
            "graduation_year_requirement": r.graduation_year_requirement,
            "required_skills": r.required_skills or [],
            "salary_text": r.salary_text,
            "source_url": r.source_url,
            "application_url": r.application_url,
            "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            "processing_status": r.processing_status,
            "job_status": r.job_status,
        })

    return JobListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job_detail(job_id: str, db: Session = Depends(get_db)):
    """Retrieves full details of a canonical job including multi-source references and version history."""
    job = db.query(JobRow).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return JobDetailResponse(
        id=job.id,
        canonical_key=job.canonical_key,
        source=job.source,
        source_job_id=job.source_job_id,
        company=job.company,
        title=job.title,
        original_title=job.original_title,
        description=job.description or "",
        original_description=job.original_description or "",
        location=job.location,
        locations=job.locations or [],
        remote_type=job.remote_type,
        employment_type=job.employment_type,
        experience_level=job.experience_level,
        education_requirements=job.education_requirements or [],
        graduation_requirement=job.graduation_requirement or {},
        graduation_year_requirement=job.graduation_year_requirement,
        required_skills=job.required_skills or [],
        preferred_skills=job.preferred_skills or [],
        technologies=job.technologies or [],
        responsibilities=job.responsibilities or [],
        qualifications=job.qualifications or [],
        salary_text=job.salary_text,
        application_url=job.application_url,
        source_url=job.source_url,
        posted_at=job.posted_at,
        deadline=job.deadline,
        first_seen_at=job.first_seen_at,
        last_seen_at=job.last_seen_at,
        content_hash=job.content_hash,
        processing_status=job.processing_status,
        job_status=job.job_status,
        source_references=[
            SourceReferenceResponse(
                id=ref.id,
                source=ref.source,
                source_job_id=ref.source_job_id,
                source_url=ref.source_url,
                application_url=ref.application_url,
                first_seen_at=ref.first_seen_at,
                last_seen_at=ref.last_seen_at,
            )
            for ref in job.source_references
        ],
        versions=[
            JobVersionResponse(
                id=ver.id,
                content_hash=ver.content_hash,
                title=ver.title,
                changes_summary=ver.changes_summary,
                created_at=ver.created_at,
            )
            for ver in job.versions
        ],
    )


@router.get("/jobs/stats/summary")
def get_job_stats(db: Session = Depends(get_db)):
    """Returns aggregate statistics on discovered jobs and sources."""
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
    remote_type_count = (
        db.query(JobRow.remote_type, func.count(JobRow.id))
        .group_by(JobRow.remote_type)
        .all()
    )
    total_runs = db.query(DiscoveryRunRow).count()

    return {
        "total_jobs": total_jobs,
        "active_jobs": active_jobs,
        "by_source": {s: count for s, count in sources_count},
        "by_employment_type": {et: count for et, count in emp_type_count},
        "by_remote_type": {rt: count for rt, count in remote_type_count},
        "total_discovery_runs": total_runs,
    }


@router.get("/discovery/runs", response_model=List[DiscoveryRunResponse])
def list_discovery_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Lists discovery execution history."""
    runs = (
        db.query(DiscoveryRunRow)
        .order_by(DiscoveryRunRow.started_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        DiscoveryRunResponse(
            id=r.id,
            source=r.source,
            source_identifier=r.source_identifier,
            started_at=r.started_at,
            completed_at=r.completed_at,
            candidates_discovered=r.candidates_discovered,
            pages_fetched=r.pages_fetched,
            jobs_new=r.jobs_new,
            jobs_updated=r.jobs_updated,
            jobs_duplicate=r.jobs_duplicate,
            jobs_failed=r.jobs_failed,
            errors=r.errors or [],
            duration_seconds=r.duration_seconds,
            status=r.status,
        )
        for r in runs
    ]


@router.post("/discovery/run", response_model=DiscoveryRunResponse)
async def trigger_discovery_run(
    request: DiscoveryRunRequest,
    db: Session = Depends(get_db),
):
    """Manually triggers a discovery run for a given source and identifier."""
    service = JobDiscoveryService()
    run_row = await service.run_discovery(
        db=db,
        source_type=request.source,
        identifier=request.identifier,
        company_name=request.company_name,
    )
    return DiscoveryRunResponse(
        id=run_row.id,
        source=run_row.source,
        source_identifier=run_row.source_identifier,
        started_at=run_row.started_at,
        completed_at=run_row.completed_at,
        candidates_discovered=run_row.candidates_discovered,
        pages_fetched=run_row.pages_fetched,
        jobs_new=run_row.jobs_new,
        jobs_updated=run_row.jobs_updated,
        jobs_duplicate=run_row.jobs_duplicate,
        jobs_failed=run_row.jobs_failed,
        errors=run_row.errors or [],
        duration_seconds=run_row.duration_seconds,
        status=run_row.status,
    )
