"""/api/v1/documents — render, list, inspect, materialize, invalidate, regenerate.

Responses carry metadata (ids, versions, hashes, relative paths under the
local documents root), never absolute server paths. The bytes of a document
are served only through ``/{id}/file`` with the API key.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.database import get_db
from app.documents.models import DocumentArtifact, DocumentFormat, RenderReport, RenderRequest
from app.documents.service import DocumentService
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"
_MEDIA = {DocumentFormat.PDF: "application/pdf", DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def get_service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> DocumentService:
    return DocumentService(db, tenant_id, actor=API_ACTOR)


class RenderResponse(BaseModel):
    artifact: DocumentArtifact
    created: bool


class MaterializeResponse(BaseModel):
    artifact: DocumentArtifact
    ready: bool
    relative_path: str
    problems: list[str] = Field(default_factory=list)


class ReasonRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=256)


@router.post("/render", response_model=RenderResponse, dependencies=WRITE)
def render(body: RenderRequest, service: DocumentService = Depends(get_service)):
    row, created = service.get_or_render(body.preparation_id, body.artifact_type, body.format, force=body.force)
    service.db.commit()
    return RenderResponse(artifact=DocumentArtifact.model_validate(row), created=created)


@router.post("/ensure/{preparation_id}", response_model=RenderReport, dependencies=WRITE)
def ensure(preparation_id: str, format: DocumentFormat = DocumentFormat.PDF, service: DocumentService = Depends(get_service)):
    """Resume (and cover letter when enabled) for a READY preparation; reuses current artifacts."""
    preparation = service._preparation(preparation_id)
    report = service.ensure_for_execution(preparation, format)
    service.db.commit()
    return report


@router.get("/by-preparation/{preparation_id}", response_model=list[DocumentArtifact])
def by_preparation(preparation_id: str, service: DocumentService = Depends(get_service)):
    return [DocumentArtifact.model_validate(r) for r in service.list_for(preparation_id)]


@router.get("/{artifact_id}", response_model=DocumentArtifact)
def detail(artifact_id: str, service: DocumentService = Depends(get_service)):
    return DocumentArtifact.model_validate(service.require(artifact_id))


@router.post("/{artifact_id}/materialize", response_model=MaterializeResponse, dependencies=WRITE)
def materialize(artifact_id: str, service: DocumentService = Depends(get_service)):
    """Verify the local file (hash, size, type) and report whether it is upload-ready."""
    row = service.require(artifact_id)
    try:
        service.materialize(artifact_id)
        problems: list[str] = []
    except Exception as exc:  # noqa: BLE001 - reported, not raised: this is a status call
        problems = [getattr(exc, "message", str(exc))]
    service.db.commit()
    return MaterializeResponse(artifact=DocumentArtifact.model_validate(row), ready=not problems, relative_path=row.relative_path, problems=problems)


@router.get("/{artifact_id}/file", dependencies=WRITE)
def download(artifact_id: str, for_upload: bool = Query(default=False), service: DocumentService = Depends(get_service)):
    """Bytes of one artifact. ``for_upload=true`` (the browser extension) refuses
    anything that is not upload-eligible (NEEDS_REVIEW / FAILED / INVALIDATED)."""
    row, path = service.materialize(artifact_id, for_upload=for_upload)
    with open(path, "rb") as handle:
        data = handle.read()
    fmt = DocumentFormat(row.format)
    name = row.relative_path.rsplit("/", 1)[-1]
    return Response(content=data, media_type=_MEDIA[fmt], headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-SHA256": row.content_hash})


@router.post("/{artifact_id}/invalidate", response_model=DocumentArtifact, dependencies=WRITE)
def invalidate(artifact_id: str, body: Optional[ReasonRequest] = None, service: DocumentService = Depends(get_service)):
    row = service.invalidate(artifact_id, (body or ReasonRequest()).reason or "invalidated via API", actor=API_ACTOR)
    service.db.commit()
    return DocumentArtifact.model_validate(row)


@router.post("/{artifact_id}/regenerate", response_model=RenderResponse, dependencies=WRITE)
def regenerate(artifact_id: str, service: DocumentService = Depends(get_service)):
    old = service.require(artifact_id)
    row = service.regenerate(old.preparation_id, old.artifact_type, DocumentFormat(old.format))
    service.db.commit()
    return RenderResponse(artifact=DocumentArtifact.model_validate(row), created=True)
