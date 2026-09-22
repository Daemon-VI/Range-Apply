"""Minimal inspection pages for Phase 2: opportunities, queue, policy."""

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
from app.core.timeutils import age as time_age
from app.database import get_db
from app.jobs.database.models import JobRow
from app.pipeline.models import (
    ApplicationPolicyUpdate,
    EligibilityDecision,
    FitBand,
    Lane,
    OpportunityState,
    QueueState,
    TailoringLevel,
)
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository, PolicyRepository

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
ACTOR = "dashboard"


def _freshness(job: Optional[JobRow]) -> str:
    if job is None:
        return "—"
    delta = time_age(job.posted_at or job.first_seen_at)
    if delta is None:
        return "unknown"
    days = delta.days
    return "today" if days <= 0 else f"{days}d"


@router.get("/opportunities", response_class=HTMLResponse)
def opportunities_page(
    request: Request,
    state: Optional[str] = None,
    fit_band: Optional[str] = None,
    eligibility: Optional[str] = None,
    admitted: Optional[str] = None,
    limit: Optional[str] = None,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    limit = to_int(limit, 100, 1, 500)
    repo = OpportunityRepository(db, tenant_id)
    rows, total = repo.list_candidate_opportunities(
        OpportunityState(state) if state else None,
        FitBand(fit_band) if fit_band else None,
        EligibilityDecision(eligibility) if eligibility else None,
        {"true": True, "false": False}.get((admitted or "").lower()),
        None,
        min(max(limit, 1), 500),
        0,
    )
    job_ids = [co.opportunity.canonical_job_id for co in rows if co.opportunity.canonical_job_id]
    jobs = {j.id: j for j in db.query(JobRow).filter(JobRow.id.in_(job_ids)).all()} if job_ids else {}
    items = [(co, co.opportunity, jobs.get(co.opportunity.canonical_job_id or ""), _freshness(jobs.get(co.opportunity.canonical_job_id or ""))) for co in rows]
    return templates.TemplateResponse(
        request=request,
        name="opportunities.html",
        context={
            "items": items,
            "total": total,
            "counts": repo.counts_by_state(),
            "filters": {"state": state or "", "fit_band": fit_band or "", "eligibility": eligibility or "", "admitted": admitted or "", "limit": limit},
            "states": [s.value for s in OpportunityState],
            "bands": [b.value for b in FitBand],
            "decisions": [d.value for d in EligibilityDecision],
        },
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    repo = QueueRepository(db, tenant_id)
    rows, total = repo.list_items(QueueState(state) if state else None, limit=200)
    opp_repo = OpportunityRepository(db, tenant_id)
    titles = {}
    for row in rows:
        opp = opp_repo.get_opportunity(row.opportunity_id)
        titles[row.id] = f"{opp.title} — {opp.company}" if opp else row.opportunity_id
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={"items": rows, "titles": titles, "total": total, "counts": repo.counts_by_state(), "state": state or "", "states": [s.value for s in QueueState]},
    )


@router.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request, notice: Optional[str] = None, db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    from app.ai.gateway import get_gateway
    from app.ai.providers import KNOWN_PROVIDERS
    from app.pipeline.gates import GATE_RULESET_VERSION, catalog
    from app.pipeline.policy import fit_band_config

    repo = PolicyRepository(db, tenant_id)
    policy = repo.get()
    repo.commit()
    return templates.TemplateResponse(
        request=request,
        name="policy.html",
        context={
            "policy": policy,
            "notice": notice,
            "bands": [b.value for b in FitBand],
            "lanes": [lane.value for lane in Lane],
            "levels": [lvl.value for lvl in TailoringLevel],
            "decisions": [d.value for d in EligibilityDecision],
            "fit_bands": fit_band_config(policy),
            "gates": catalog(policy),
            "gate_ruleset_version": GATE_RULESET_VERSION,
            "ai": get_gateway().effective(policy.ai_settings),
            "ai_providers": [p for p in KNOWN_PROVIDERS if p != "scripted"],
        },
    )


@router.post("/policy")
def update_policy_page(
    enabled_bands: list[str] = Form(default=[]),
    high_threshold: str = Form(""),
    medium_threshold: str = Form(""),
    daily_cap: str = Form(""),
    weekly_cap: str = Form(""),
    cooldown_days: str = Form(""),
    minimum_eligibility: str = Form("UNCERTAIN"),
    blocked_companies: str = Form(""),
    lane_high: str = Form("REVIEW"),
    lane_medium: str = Form("REVIEW"),
    lane_low: str = Form("REVIEW"),
    tailoring_high: str = Form("L2"),
    tailoring_medium: str = Form("L1"),
    tailoring_low: str = Form("L0"),
    minimum_fit_score: str = Form(""),
    ai_enabled: Optional[str] = Form(None),
    ai_provider: str = Form(""),
    ai_max_calls_per_run: str = Form(""),
    ai_cache_enabled: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    from app.ai.models import TenantAISettings

    repo = PolicyRepository(db, tenant_id)
    # A cleared number box keeps the saved value instead of failing the whole form (422).
    current = repo.get()
    high_threshold = to_int(high_threshold, current.band_thresholds.get("HIGH", 70), 0, 100)
    medium_threshold = to_int(medium_threshold, current.band_thresholds.get("MEDIUM", 45), 0, 100)
    daily_cap = to_int(daily_cap, current.daily_cap, 0)
    weekly_cap = to_int(weekly_cap, current.weekly_cap, 0)
    cooldown_days = to_int(cooldown_days, current.cooldown_days, 0)
    try:
        ai_settings = TenantAISettings(
            enabled=ai_enabled is not None,
            provider=ai_provider.strip() or None,
            max_calls_per_run=int(ai_max_calls_per_run) if ai_max_calls_per_run.strip() else None,
            cache_enabled=ai_cache_enabled is not None,
        )
        repo.update(
            ApplicationPolicyUpdate(
                ai_settings=ai_settings,
                minimum_fit_score=int(minimum_fit_score) if minimum_fit_score.strip() else None,
                enabled_bands=[FitBand(b) for b in enabled_bands],
                band_thresholds={"HIGH": high_threshold, "MEDIUM": medium_threshold},
                daily_cap=daily_cap,
                weekly_cap=weekly_cap,
                cooldown_days=cooldown_days,
                minimum_eligibility=EligibilityDecision(minimum_eligibility),
                blocked_companies=[c.strip() for c in blocked_companies.replace("\n", ",").split(",") if c.strip()],
                lane_by_band={"HIGH": Lane(lane_high), "MEDIUM": Lane(lane_medium), "LOW": Lane(lane_low)},
                tailoring_by_band={"HIGH": TailoringLevel(tailoring_high), "MEDIUM": TailoringLevel(tailoring_medium), "LOW": TailoringLevel(tailoring_low)},
            ),
            ACTOR,
        )
        repo.commit()
    except CareerOSError as exc:
        return RedirectResponse(url=f"/dashboard/policy?notice=error:{exc.code}:{exc.message[:120]}", status_code=303)
    return RedirectResponse(url="/dashboard/policy?notice=policy-saved", status_code=303)
