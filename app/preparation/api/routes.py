"""/api/v1/preparations — build, inspect, review and answer application packages."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.database import get_db
from app.pipeline.models import Lane, TailoringLevel
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import (
    Preparation,
    PreparationAnswer,
    PreparationArtifact,
    PreparationDetail,
    PreparationStatus,
    PrepareManyReport,
    PrepareRequest,
)
from app.preparation.queue_worker import run_prepare_queue
from app.preparation.service import PreparationService
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/preparations", tags=["preparations"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def get_service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> PreparationService:
    return PreparationService(db, tenant_id, actor=API_ACTOR)


def _detail(row: ApplicationPreparationRow) -> PreparationDetail:
    base = Preparation.model_validate(row)
    return PreparationDetail(
        **base.model_dump(),
        artifacts=[PreparationArtifact.model_validate(a) for a in row.artifacts],
        answers=[PreparationAnswer.model_validate(a) for a in row.answers],
    )


class BatchRequest(BaseModel):
    candidate_opportunity_ids: list[str] = Field(min_length=1, max_length=5000)
    tailoring_level: Optional[TailoringLevel] = None
    questions: Optional[list[str]] = None
    force: bool = False


class ReviewRequest(BaseModel):
    reason: Optional[str] = None


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    save_to_bank: bool = False
    category: Optional[str] = None


class RunQueueRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=200)
    lane: Optional[Lane] = None


class PreparationListResponse(BaseModel):
    total: int
    items: list[Preparation] = Field(default_factory=list)


@router.get("/", response_model=PreparationListResponse)
def list_preparations(
    status_filter: Optional[PreparationStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PreparationService = Depends(get_service),
):
    rows, total = service.list_all(status_filter, limit, offset)
    return PreparationListResponse(total=total, items=[Preparation.model_validate(r) for r in rows])


@router.get("/summary")
def summary(service: PreparationService = Depends(get_service)) -> dict[str, Any]:
    return {"by_status": service.counts_by_status()}


@router.post("/", response_model=PreparationDetail, status_code=status.HTTP_201_CREATED, dependencies=WRITE)
def prepare(body: PrepareRequest, service: PreparationService = Depends(get_service)):
    row = service.prepare(body.candidate_opportunity_id, body.tailoring_level, body.questions, body.force, API_ACTOR)
    service.db.commit()
    return _detail(service.require(row.id))


@router.post("/batch", response_model=PrepareManyReport, dependencies=WRITE)
def prepare_batch(body: BatchRequest, service: PreparationService = Depends(get_service)):
    return service.prepare_many(body.candidate_opportunity_ids, body.tailoring_level, body.questions, body.force, API_ACTOR)


@router.post("/run-queue", dependencies=WRITE)
def run_queue(body: RunQueueRequest, db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> dict[str, int]:
    return run_prepare_queue(db, tenant_id, body.worker_id, body.limit, body.lane)


@router.get("/by-opportunity/{co_id}", response_model=list[Preparation])
def by_opportunity(co_id: str, service: PreparationService = Depends(get_service)):
    return [Preparation.model_validate(r) for r in service.list_for(co_id)]


@router.get("/{preparation_id}", response_model=PreparationDetail)
def get_preparation(preparation_id: str, service: PreparationService = Depends(get_service)):
    return _detail(service.require(preparation_id))


@router.get("/{preparation_id}/evidence")
def preparation_evidence(preparation_id: str, service: PreparationService = Depends(get_service)) -> dict[str, Any]:
    """Why is each block here? The exact nodes behind every claim."""
    row = service.require(preparation_id)
    nodes = service.evidence_repo.get_nodes_by_keys(list(row.evidence_keys or []), include_removed=True)
    return {
        "preparation_id": row.id,
        "evidence": {
            key: {
                "kind": node.kind,
                "label": node.label,
                "claim": node.claim,
                "verification_status": node.verification_status,
                "status": node.status,
                "version": node.version,
                "source_type": node.source_type,
                "source_ref": node.source_ref,
            }
            for key, node in nodes.items()
        },
        "blocks": [
            {"artifact": a.artifact_type, "index": i, "kind": b.get("kind"), "section": b.get("section"), "text": b.get("text"), "evidence_keys": b.get("evidence_keys", []), "ai_polished": b.get("ai_polished", False)}
            for a in row.artifacts
            for i, b in enumerate(a.blocks or [])
        ],
    }


@router.post("/{preparation_id}/approve", response_model=PreparationDetail, dependencies=WRITE)
def approve(preparation_id: str, service: PreparationService = Depends(get_service)):
    row = service.approve(preparation_id, API_ACTOR)
    service.db.commit()
    return _detail(row)


@router.post("/{preparation_id}/reject", response_model=PreparationDetail, dependencies=WRITE)
def reject(preparation_id: str, body: Optional[ReviewRequest] = None, service: PreparationService = Depends(get_service)):
    row = service.reject(preparation_id, API_ACTOR, (body or ReviewRequest()).reason)
    service.db.commit()
    return _detail(row)


@router.post("/{preparation_id}/invalidate", response_model=PreparationDetail, dependencies=WRITE)
def invalidate(preparation_id: str, body: Optional[ReviewRequest] = None, service: PreparationService = Depends(get_service)):
    row = service.invalidate(preparation_id, API_ACTOR, (body or ReviewRequest()).reason)
    service.db.commit()
    return _detail(row)


@router.put("/{preparation_id}/answers/{answer_id}", response_model=PreparationDetail, dependencies=WRITE)
def answer_question(
    preparation_id: str, answer_id: str, body: AnswerRequest, service: PreparationService = Depends(get_service)
):
    row = service.answer_question(preparation_id, answer_id, body.answer, API_ACTOR, body.save_to_bank, body.category)
    service.db.commit()
    return _detail(row)
