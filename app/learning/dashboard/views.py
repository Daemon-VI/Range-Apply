"""Learning / outcomes page: the latest snapshot's totals, breakdowns with
sample sizes and confidence, recommendations, the settings, a snapshot button."""

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
from app.learning.engine import LearningEngine
from app.learning.models import Dimension, EvidenceQuality, Metric, TenantLearningSettings
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

router = APIRouter(prefix="/dashboard/learning", tags=["dashboard"])
ACTOR = "dashboard"
BREAKDOWNS = [Dimension.SOURCE, Dimension.COMPANY, Dimension.ROLE_FAMILY, Dimension.TITLE, Dimension.FIT_BAND, Dimension.LANE, Dimension.TAILORING_LEVEL, Dimension.COVER_LETTER_MODE, Dimension.EXECUTION_METHOD]


def _engine(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> LearningEngine:
    return LearningEngine(db, tenant_id, actor=ACTOR)


def _back(notice: str) -> RedirectResponse:
    return RedirectResponse(url=f"/dashboard/learning?notice={notice}", status_code=303)


@router.get("", response_class=HTMLResponse)
def learning_page(request: Request, notice: Optional[str] = None, snapshot_id: Optional[str] = None, engine: LearningEngine = Depends(_engine)):
    snapshot = engine.get_snapshot(snapshot_id) if snapshot_id else engine.latest_snapshot()
    breakdowns: dict[str, list] = {}
    recommendations = []
    if snapshot is not None:
        rows = engine.metrics(snapshot.id, limit=5000)
        by_group: dict[tuple[str, str], dict] = {}
        for m in rows:
            g = by_group.setdefault((m.dimension, m.group_key), {"dimension": m.dimension, "label": m.group_label, "n": 0, "confidence": "NONE", "metrics": {}, "evidence": m.evidence})
            if m.metric == Metric.RESPONSE_RATE.value:
                g["n"], g["confidence"] = m.n, m.confidence
            g["metrics"][m.metric] = m
        for dim in BREAKDOWNS:
            groups = [g for (d, _), g in by_group.items() if d == dim.value]
            breakdowns[dim.value] = sorted(groups, key=lambda g: (-g["n"], g["label"]))[:25]
        recommendations = engine.snapshot_recommendations(snapshot.id)
    return templates.TemplateResponse(
        request=request,
        name="learning.html",
        context={"snapshot": snapshot, "snapshots": engine.list_snapshots(10), "breakdowns": breakdowns, "recommendations": recommendations, "settings": engine.settings, "notice": notice, "evidence_levels": [e.value for e in EvidenceQuality if e is not EvidenceQuality.NONE], "source_discovery": (snapshot.source_discovery if snapshot else [])},
    )


@router.post("/snapshot")
def create_snapshot(engine: LearningEngine = Depends(_engine)):
    try:
        row = engine.snapshot(actor=ACTOR)
        engine.db.commit()
    except CareerOSError as exc:
        engine.db.rollback()
        return _back(f"error:{exc.code}:{exc.message[:120]}")
    return _back(f"snapshot-{row.id[:8]}")


@router.post("/settings")
def update_settings(ordering_enabled: Optional[str] = Form(None), window_days: str = Form(""), min_samples: str = Form("5"), prior_strength: str = Form("10"), minimum_evidence: str = Form("MODERATE"), db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    try:
        settings = TenantLearningSettings(ordering_enabled=ordering_enabled == "on", window_days=int(window_days) if window_days.strip() else None, min_samples=int(min_samples or 5), prior_strength=float(prior_strength or 10), minimum_evidence=EvidenceQuality(minimum_evidence))
        PolicyRepository(db, tenant_id).update(ApplicationPolicyUpdate(learning_settings=settings), ACTOR)
        db.commit()
    except (CareerOSError, ValueError) as exc:
        db.rollback()
        message = getattr(exc, "message", str(exc))
        return _back(f"error:validation:{message[:120]}")
    return _back("settings-saved")
