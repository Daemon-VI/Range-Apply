"""Phase 5 application engine API + Phase 6-lite kill switch controls."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.engine import ApplicationEngine
from app.application.killswitch import GLOBAL_ID, KillSwitchRow, is_paused, set_paused
from app.application.models import ApplicationEvent, Application as ApplicationModel
from app.database import get_db
from app.jobs.database.models import JobRow
from app.security import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/applications", tags=["applications"])

_engine = ApplicationEngine()


class SubmitRequest(BaseModel):
    approved: bool


class KillSwitchRequest(BaseModel):
    paused: bool
    reason: Optional[str] = None
    source: Optional[str] = None


class KillSwitchResponse(BaseModel):
    id: str
    paused: bool
    reason: Optional[str] = None

    class Config:
        from_attributes = True


class ApplicationDetail(ApplicationModel):
    events: List[ApplicationEvent] = Field(default_factory=list)


@router.post("/{job_id}/prepare", response_model=ApplicationModel, dependencies=[Depends(require_api_key)])
def prepare_application(job_id: str, db: Session = Depends(get_db)):
    job = db.query(JobRow).filter_by(id=job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        row = _engine.prepare(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return row


@router.post("/{job_id}/submit", response_model=ApplicationModel, dependencies=[Depends(require_api_key)])
async def submit_application(
    job_id: str,
    body: SubmitRequest,
    dry_run: bool = True,
    db: Session = Depends(get_db),
):
    job = db.query(JobRow).filter_by(id=job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        row = await _engine.submit(db, job_id, approved=body.approved, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return row


@router.get("/", response_model=List[ApplicationModel])
def list_applications(
    status: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(ApplicationRow)
    if status:
        query = query.filter(ApplicationRow.status == status)
    return query.order_by(ApplicationRow.created_at.desc()).all()


@router.get("/killswitch", response_model=Dict[str, Any])
def get_killswitch(db: Session = Depends(get_db)):
    rows = db.query(KillSwitchRow).all()
    return {
        "global_paused": is_paused(db, None),
        "switches": [
            {"id": r.id, "paused": r.paused, "reason": r.reason} for r in rows
        ],
    }


@router.post("/killswitch", response_model=KillSwitchResponse, dependencies=[Depends(require_api_key)])
def post_killswitch(body: KillSwitchRequest, db: Session = Depends(get_db)):
    row = set_paused(db, paused=body.paused, reason=body.reason, source=body.source)
    return row


@router.get("/{job_id}", response_model=ApplicationDetail)
def get_application(job_id: str, db: Session = Depends(get_db)):
    row = db.query(ApplicationRow).filter_by(job_id=job_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No application for this job")
    events = (
        db.query(ApplicationEventRow)
        .filter_by(application_id=row.id)
        .order_by(ApplicationEventRow.created_at.asc())
        .all()
    )
    detail = ApplicationDetail.model_validate(row)
    detail.events = [ApplicationEvent.model_validate(e) for e in events]
    return detail
