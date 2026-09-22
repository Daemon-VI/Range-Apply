"""Preparation pages: list, detail with evidence trace, review and answer forms."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_tenant_id
from app.core.errors import CareerOSError
from app.database import get_db
from app.pipeline.models import TailoringLevel
from app.preparation.models import PreparationStatus
from app.preparation.service import PreparationService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/preparations", tags=["dashboard"])
ACTOR = "dashboard"


def _service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> PreparationService:
    return PreparationService(db, tenant_id, actor=ACTOR)


def _back(prep_id: str, notice: str) -> RedirectResponse:
    return RedirectResponse(url=f"/dashboard/preparations/{prep_id}?notice={notice}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_page(
    request: Request,
    status: Optional[str] = None,
    service: PreparationService = Depends(_service),
):
    rows, total = service.list_all(PreparationStatus(status) if status else None, limit=200)
    titles = {}
    for row in rows:
        opp = service.repo.get_opportunity(row.opportunity_id)
        titles[row.id] = f"{opp.title} — {opp.company}" if opp else row.opportunity_id
    return templates.TemplateResponse(
        request=request,
        name="preparations.html",
        context={"rows": rows, "titles": titles, "total": total, "counts": service.counts_by_status(), "status": status or "", "statuses": [s.value for s in PreparationStatus]},
    )


@router.get("/{prep_id}", response_class=HTMLResponse)
def detail_page(request: Request, prep_id: str, notice: Optional[str] = None, service: PreparationService = Depends(_service)):
    row = service.require(prep_id)
    opp = service.repo.get_opportunity(row.opportunity_id)
    nodes = service.evidence_repo.get_nodes_by_keys(list(row.evidence_keys or []), include_removed=True)
    variant = None
    if row.positioning_variant_id:
        variant = service.evidence_repo.get_variant(row.positioning_variant_id)
    from app.documents.service import DocumentService

    documents = DocumentService(service.db, service.tenant_id, actor=ACTOR).list_for(prep_id)
    return templates.TemplateResponse(
        request=request,
        name="preparation_detail.html",
        context={
            "prep": row,
            "opportunity": opp,
            "nodes": nodes,
            "variant": variant,
            "notice": notice,
            "levels": [lvl.value for lvl in TailoringLevel],
            "documents": documents,
        },
    )


@router.post("/{prep_id}/documents/render")
def render_documents(prep_id: str, fmt: str = Form("PDF"), artifact_type: str = Form("RESUME"), service: PreparationService = Depends(_service)):
    from app.documents.models import DocumentFormat
    from app.documents.service import DocumentService

    try:
        DocumentService(service.db, service.tenant_id, actor=ACTOR).get_or_render(prep_id, artifact_type, DocumentFormat(fmt))
        service.db.commit()
    except CareerOSError as exc:
        return _back(prep_id, f"error:{exc.code}:{exc.message[:120]}")
    return _back(prep_id, "document-rendered")


@router.post("/{prep_id}/regenerate")
def regenerate(prep_id: str, tailoring_level: str = Form(""), service: PreparationService = Depends(_service)):
    row = service.require(prep_id)
    try:
        new_row = service.prepare(
            row.candidate_opportunity_id,
            TailoringLevel(tailoring_level) if tailoring_level else None,
            force=True,
            actor=ACTOR,
        )
        service.db.commit()
    except CareerOSError as exc:
        return _back(prep_id, f"error:{exc.code}:{exc.message[:120]}")
    return _back(new_row.id, "regenerated")


@router.post("/{prep_id}/approve")
def approve(prep_id: str, service: PreparationService = Depends(_service)):
    try:
        service.approve(prep_id, ACTOR)
        service.db.commit()
    except CareerOSError as exc:
        return _back(prep_id, f"error:{exc.code}:{exc.message[:120]}")
    return _back(prep_id, "approved")


@router.post("/{prep_id}/reject")
def reject(prep_id: str, reason: str = Form(""), service: PreparationService = Depends(_service)):
    service.reject(prep_id, ACTOR, reason.strip() or None)
    service.db.commit()
    return _back(prep_id, "rejected")


@router.post("/{prep_id}/answers/{answer_id}")
def answer(
    prep_id: str,
    answer_id: str,
    answer_text: str = Form(...),
    save_to_bank: Optional[str] = Form(None),
    service: PreparationService = Depends(_service),
):
    try:
        service.answer_question(prep_id, answer_id, answer_text, ACTOR, save_to_bank == "on")
        service.db.commit()
    except CareerOSError as exc:
        return _back(prep_id, f"error:{exc.code}:{exc.message[:120]}")
    return _back(prep_id, "answer-saved")
