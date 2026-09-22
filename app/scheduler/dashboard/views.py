"""Scheduler page: capacity, cool-downs, run history, a preview and a run button."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_tenant_id
from app.core.errors import CareerOSError
from app.core.params import to_int
from app.database import get_db
from app.scheduler.service import SchedulerService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/scheduler", tags=["dashboard"])
ACTOR = "dashboard"


def _service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> SchedulerService:
    return SchedulerService(db, tenant_id, actor=ACTOR)


@router.get("", response_class=HTMLResponse)
def scheduler_page(request: Request, notice: Optional[str] = None, window: Optional[str] = None, service: SchedulerService = Depends(_service)):
    window = to_int(window, 500, 1, 5000)
    preview = service.preview(window=window, limit=100)
    runs, total = service.history(limit=20)
    return templates.TemplateResponse(
        request=request,
        name="scheduler.html",
        context={"preview": preview, "capacity": preview.capacity, "runs": runs, "total_runs": total, "notice": notice, "window": window},
    )


@router.post("/run")
def run_now(window: str = Form(""), prepare: Optional[str] = Form(None), service: SchedulerService = Depends(_service)):
    try:
        run = service.run(trigger="dashboard", window=to_int(window, 500, 1, 5000), prepare=prepare == "on", prepare_limit=100)
    except CareerOSError as exc:
        return RedirectResponse(url=f"/dashboard/scheduler?notice=error:{exc.code}:{exc.message[:120]}", status_code=303)
    return RedirectResponse(url=f"/dashboard/scheduler?notice=run:{run.status}:{run.admitted}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str, service: SchedulerService = Depends(_service)):
    run = service.get_run(run_id)
    if run is None:
        return RedirectResponse(url="/dashboard/scheduler?notice=error:not_found:run", status_code=303)
    return templates.TemplateResponse(request=request, name="scheduler_run.html", context={"run": run})
