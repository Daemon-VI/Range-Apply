"""Phase 4 tailoring API: generate and manage tailored artifacts."""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.jobs.database.models import JobRow
from app.security import require_api_key
from app.tailoring.database.models import TailoredArtifactRow
from app.tailoring.generator import TailoringEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v4/tailoring", tags=["tailoring"])


class GenerateRequest(BaseModel):
    job_id: str


class ArtifactResponse(BaseModel):
    id: str
    job_id: str
    match_id: Optional[str] = None
    artifact_type: str
    version: int
    title: Optional[str] = None
    content: str
    evidence_refs: List[str] = []
    template_name: str
    approved: bool
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


@router.post("/generate", response_model=List[ArtifactResponse], dependencies=[Depends(require_api_key)])
def generate_artifacts(request: GenerateRequest, db: Session = Depends(get_db)):
    job = db.query(JobRow).filter(JobRow.id == request.job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {request.job_id}")

    try:
        rows = TailoringEngine().generate(db, request.job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return rows


@router.get("/job/{job_id}", response_model=List[ArtifactResponse])
def list_artifacts_for_job(job_id: str, db: Session = Depends(get_db)):
    return (
        db.query(TailoredArtifactRow)
        .filter(TailoredArtifactRow.job_id == job_id)
        .order_by(TailoredArtifactRow.version.desc())
        .all()
    )


@router.post("/{artifact_id}/approve", response_model=ArtifactResponse, dependencies=[Depends(require_api_key)])
def approve_artifact(artifact_id: str, db: Session = Depends(get_db)):
    row = db.query(TailoredArtifactRow).filter(TailoredArtifactRow.id == artifact_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}")

    row.approved = True
    db.commit()
    db.refresh(row)
    return row
