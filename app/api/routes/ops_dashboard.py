"""/dashboard/ops — the diagnostics view (Blueprint Phase 12). Reuses the
JSON diagnostics; counts only, never candidate data or secrets."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_tenant_id
from app.api.routes.diagnostics import diagnostics
from app.database import get_db

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/ops", tags=["dashboard"])


@router.get("", response_class=HTMLResponse)
def ops_page(request: Request, db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    data = diagnostics(db, tenant_id)
    return templates.TemplateResponse(request=request, name="ops.html", context={"d": data})
