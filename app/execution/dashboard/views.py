"""Execution page: attempts by status, handoffs, runs, retry / cancel / confirm."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.core.errors import CareerOSError
from app.database import get_db
from app.execution.service import ExecutionService
from app.pipeline.database.models import OpportunityRow

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/execution", tags=["dashboard"])
ACTOR = "dashboard"
STATUS_ORDER = ["READY", "SUBMITTING", "SUBMITTED", "VERIFIED", "UNCERTAIN", "BLOCKED", "NEEDS_USER_INPUT", "NEEDS_REVIEW", "FAILED", "CANCELLED"]


def _service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> ExecutionService:
    return ExecutionService(db, tenant_id, actor=ACTOR)


def _back(notice: str, application_id: Optional[str] = None) -> RedirectResponse:
    target = f"/dashboard/execution/attempts/{application_id}" if application_id else "/dashboard/execution"
    return RedirectResponse(url=f"{target}?notice={notice}", status_code=303)


@router.get("", response_class=HTMLResponse)
def execution_page(request: Request, status: Optional[str] = None, notice: Optional[str] = None, service: ExecutionService = Depends(_service)):
    summary = service.summary()
    rows, total = service.list_attempts(ApplicationStatus(status) if status else None, limit=200)
    opp_ids = [r.opportunity_id for r in rows if r.opportunity_id]
    opps = {o.id: o for o in service.db.query(OpportunityRow).filter(OpportunityRow.id.in_(opp_ids)).all()} if opp_ids else {}
    runs = {r.id: r for r in service.db.query(type(service.require_run)).all()} if False else {}
    last_runs = {}
    for row in rows:
        if row.last_execution_id:
            run = service.get_run(row.last_execution_id)
            if run is not None:
                last_runs[row.id] = run
    return templates.TemplateResponse(
        request=request,
        name="execution.html",
        context={"summary": summary, "rows": rows, "total": total, "opps": opps, "last_runs": last_runs, "status": status or "", "statuses": STATUS_ORDER, "notice": notice, "runs": runs},
    )


@router.get("/attempts/{application_id}", response_class=HTMLResponse)
def attempt_page(request: Request, application_id: str, notice: Optional[str] = None, service: ExecutionService = Depends(_service)):
    attempt = service.require_attempt(application_id)
    opp = service.db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
    runs = service.runs_for(application_id)
    snapshot = service._latest_snapshot(application_id)
    preview = None
    if attempt.preparation_id:
        try:
            preview = service.preview(application_id)
        except CareerOSError:
            preview = None
    return templates.TemplateResponse(
        request=request,
        name="execution_attempt.html",
        context={"attempt": attempt, "opp": opp, "runs": runs, "snapshot": snapshot, "preview": preview, "notice": notice, "item": service.item_for(attempt)},
    )


@router.post("/attempts/{application_id}/retry")
def retry(application_id: str, reason: str = Form(""), service: ExecutionService = Depends(_service)):
    try:
        service.retry(application_id, ACTOR, reason.strip() or None)
    except CareerOSError as exc:
        return _back(f"error:{exc.code}:{exc.message[:120]}", application_id)
    return _back("retried", application_id)


@router.post("/attempts/{application_id}/cancel")
def cancel(application_id: str, reason: str = Form(""), service: ExecutionService = Depends(_service)):
    try:
        service.cancel(application_id, ACTOR, reason.strip() or None)
    except CareerOSError as exc:
        return _back(f"error:{exc.code}:{exc.message[:120]}", application_id)
    return _back("cancelled", application_id)


@router.post("/runs/{run_id}/confirm")
def confirm(run_id: str, submitted: str = Form(""), reference: str = Form(""), service: ExecutionService = Depends(_service)):
    try:
        run = service.confirm(run_id, submitted == "yes", reference.strip() or None)
    except CareerOSError as exc:
        return _back(f"error:{exc.code}:{exc.message[:120]}")
    return _back("confirmed", run.application_id)


@router.post("/fields/{field_id}/answer")
def answer_field(field_id: str, answer_text: str = Form(...), save_to_bank: Optional[str] = Form(None), service: ExecutionService = Depends(_service)):
    try:
        row = service.answer_field(field_id, answer_text, ACTOR, save_to_bank == "on")
    except CareerOSError as exc:
        return _back(f"error:{exc.code}:{exc.message[:120]}")
    snapshot = service.db.get(type(row.snapshot), row.snapshot_id)
    return _back("answer-saved", snapshot.application_id)
