"""Signal Inbox pages: the filtered inbox, the review queue and one signal's
full trace with the human actions (link / confirm / reject / ignore / merge)."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_tenant_id
from app.application.database.models import ApplicationRow
from app.core.errors import CareerOSError
from app.core.params import to_int
from app.database import get_db
from app.pipeline.database.models import OpportunityRow
from app.signals.models import OutcomeKind, SignalCategory, SignalSource, SignalStatus
from app.signals.service import SignalInboxService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/signals", tags=["dashboard"])
ACTOR = "dashboard"
STATUSES = [s.value for s in SignalStatus]
SOURCES = [s.value for s in SignalSource]
CATEGORIES = [c.value for c in SignalCategory]
OUTCOMES = [o.value for o in OutcomeKind]


def _service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> SignalInboxService:
    return SignalInboxService(db, tenant_id, actor=ACTOR)


def _back(notice: str, signal_id: Optional[str] = None) -> RedirectResponse:
    target = f"/dashboard/signals/{signal_id}" if signal_id else "/dashboard/signals"
    return RedirectResponse(url=f"{target}?notice={notice}", status_code=303)


@router.get("", response_class=HTMLResponse)
def inbox_page(
    request: Request,
    status: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    outcome: Optional[str] = None,
    company: Optional[str] = None,
    application_id: Optional[str] = None,
    days: Optional[str] = None,
    review: Optional[str] = None,
    notice: Optional[str] = None,
    service: SignalInboxService = Depends(_service),
):
    # The "last N days" box is empty by default: an empty string is "no limit", not a 422.
    days = to_int(days, None, 1, 3650)
    rows, total = service.list_signals(
        SignalStatus(status) if status else None,
        SignalSource(source) if source else None,
        SignalCategory(category) if category else None,
        application_id or None,
        OutcomeKind(outcome) if outcome else None,
        company or None,
        days or None,
        review == "1",
        limit=200,
    )
    opp_ids = [r.opportunity_id for r in rows if r.opportunity_id]
    opps = {o.id: o for o in service.db.query(OpportunityRow).filter(OpportunityRow.id.in_(opp_ids)).all()} if opp_ids else {}
    return templates.TemplateResponse(
        request=request,
        name="signals.html",
        context={
            "summary": service.summary(),
            "rows": rows,
            "total": total,
            "opps": opps,
            "filters": {"status": status or "", "source": source or "", "category": category or "", "outcome": outcome or "", "company": company or "", "application_id": application_id or "", "days": days or "", "review": review or ""},
            "statuses": STATUSES,
            "sources": SOURCES,
            "categories": CATEGORIES,
            "outcomes": OUTCOMES,
            "notice": notice,
        },
    )


@router.get("/{signal_id}", response_class=HTMLResponse)
def signal_page(request: Request, signal_id: str, notice: Optional[str] = None, service: SignalInboxService = Depends(_service)):
    trace = service.trace(signal_id)
    candidates = []
    current = service.require_signal(signal_id)
    attribution = next((a for a in trace.attributions if a.id == trace.signal.attribution_id), None)
    if attribution is not None and attribution.candidates:
        rows = service.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == service.tenant_id, ApplicationRow.id.in_(attribution.candidates)).all()
        opp_ids = [r.opportunity_id for r in rows if r.opportunity_id]
        opps = {o.id: o for o in service.db.query(OpportunityRow).filter(OpportunityRow.id.in_(opp_ids)).all()} if opp_ids else {}
        candidates = [(r, opps.get(r.opportunity_id)) for r in rows]
    return templates.TemplateResponse(
        request=request,
        name="signal_detail.html",
        context={"trace": trace, "signal": trace.signal, "row": current, "attribution": attribution, "candidates": candidates, "categories": CATEGORIES, "outcomes": OUTCOMES, "notice": notice},
    )


def _action(service: SignalInboxService, signal_id: str, fn, notice: str) -> RedirectResponse:
    try:
        fn()
        service.db.commit()
    except CareerOSError as exc:
        service.db.rollback()
        return _back(f"error:{exc.code}:{exc.message[:120]}", signal_id)
    return _back(notice, signal_id)


@router.post("/{signal_id}/link")
def link(signal_id: str, application_id: str = Form(...), note: str = Form(""), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.link(signal_id, application_id.strip(), ACTOR, note.strip() or None), "linked")


@router.post("/{signal_id}/confirm-classification")
def confirm_classification(signal_id: str, category: str = Form(...), note: str = Form(""), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.confirm_classification(signal_id, SignalCategory(category), ACTOR, note.strip() or None), "classification-confirmed")


@router.post("/{signal_id}/reject-classification")
def reject_classification(signal_id: str, note: str = Form(""), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.reject_classification(signal_id, ACTOR, note.strip() or None), "classification-rejected")


@router.post("/{signal_id}/confirm-outcome")
def confirm_outcome(signal_id: str, outcome: str = Form(...), note: str = Form(""), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.confirm_outcome(signal_id, OutcomeKind(outcome), ACTOR, note.strip() or None), "outcome-confirmed")


@router.post("/{signal_id}/ignore")
def ignore(signal_id: str, note: str = Form(""), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.ignore(signal_id, ACTOR, note.strip() or None), "ignored")


@router.post("/{signal_id}/merge")
def merge(signal_id: str, into_signal_id: str = Form(...), service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.merge(signal_id, into_signal_id.strip(), ACTOR), "merged")


@router.post("/{signal_id}/reprocess")
def reprocess(signal_id: str, service: SignalInboxService = Depends(_service)):
    return _action(service, signal_id, lambda: service.reprocess(signal_id, ACTOR), "reprocessed")
