"""/api/v1/learning — the Outcome Learning Engine (Blueprint Phase 11).

    settings (GET/PUT, on the policy)  ->  compute (read-only) | snapshots (persist)
    -> snapshots/{id} (+ metrics, recommendations)  ->  dataset  ->  expected/{co}

Every number carries its sample size, confidence, evidence mix, versions
and window. Nothing here writes a policy: ``PUT /settings`` changes only
the learning settings (and the ordering opt-in), audited as a policy update.
"""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.core.errors import NotFoundError
from app.database import get_db
from app.learning.engine import LearningEngine
from app.learning.models import (
    Dimension,
    ExpectedResponse,
    LearningMetric,
    LearningRecommendation,
    LearningResult,
    LearningRow,
    LearningSnapshot,
    Metric,
    TenantLearningSettings,
)
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def get_engine(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> LearningEngine:
    return LearningEngine(db, tenant_id, actor=API_ACTOR)


class SettingsView(BaseModel):
    tenant_id: str
    policy_version: int
    learning_settings: TenantLearningSettings
    priority_learned_prior_weight: float
    note: str = "ordering_enabled feeds the existing learned_prior priority component only; nothing here changes admission, caps, bands or volume"


class SnapshotDetail(BaseModel):
    snapshot: LearningSnapshot
    metrics: list[LearningMetric] = Field(default_factory=list)
    recommendations: list[LearningRecommendation] = Field(default_factory=list)


class DatasetView(BaseModel):
    total: int
    feature_version: str
    as_of: Optional[datetime] = None
    rows: list[LearningRow] = Field(default_factory=list)


def _settings_view(db: Session, tenant_id: str) -> SettingsView:
    from app.pipeline.priority import DEFAULT_PRIORITY_WEIGHTS

    policy = PolicyRepository(db, tenant_id).get()
    weights = {**DEFAULT_PRIORITY_WEIGHTS, **policy.priority_weights}
    return SettingsView(tenant_id=tenant_id, policy_version=policy.version, learning_settings=policy.learning_settings, priority_learned_prior_weight=float(weights.get("learned_prior", 0.0)))


@router.get("/settings", response_model=SettingsView)
def get_settings(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    view = _settings_view(db, tenant_id)
    db.commit()
    return view


@router.put("/settings", response_model=SettingsView, dependencies=WRITE)
def update_settings(body: TenantLearningSettings, db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    """Learning settings only (audited as a policy update); the priority weights are untouched."""
    PolicyRepository(db, tenant_id).update(ApplicationPolicyUpdate(learning_settings=body), API_ACTOR)
    view = _settings_view(db, tenant_id)
    db.commit()
    return view


@router.get("/compute", response_model=LearningResult)
def compute(as_of: Optional[datetime] = None, engine: LearningEngine = Depends(get_engine)):
    """What the engine would say now (or at ``as_of``). Writes nothing."""
    return engine.compute(as_of)


@router.post("/snapshots", response_model=LearningSnapshot, dependencies=WRITE)
def create_snapshot(as_of: Optional[datetime] = None, engine: LearningEngine = Depends(get_engine)):
    row = engine.snapshot(as_of, actor=API_ACTOR)
    engine.db.commit()
    return LearningSnapshot.model_validate(row)


@router.get("/snapshots", response_model=list[LearningSnapshot])
def list_snapshots(limit: int = Query(default=50, ge=1, le=500), engine: LearningEngine = Depends(get_engine)):
    return [LearningSnapshot.model_validate(r) for r in engine.list_snapshots(limit)]


@router.get("/snapshots/latest", response_model=Optional[SnapshotDetail])
def latest_snapshot(engine: LearningEngine = Depends(get_engine)):
    row = engine.latest_snapshot()
    if row is None:
        return None
    return SnapshotDetail(snapshot=LearningSnapshot.model_validate(row), metrics=[LearningMetric.model_validate(m) for m in engine.metrics(row.id)], recommendations=[LearningRecommendation.model_validate(r) for r in engine.snapshot_recommendations(row.id)])


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetail)
def get_snapshot(snapshot_id: str, dimension: Optional[Dimension] = None, metric: Optional[Metric] = None, engine: LearningEngine = Depends(get_engine)):
    row = engine.require_snapshot(snapshot_id)
    return SnapshotDetail(snapshot=LearningSnapshot.model_validate(row), metrics=[LearningMetric.model_validate(m) for m in engine.metrics(row.id, dimension, metric)], recommendations=[LearningRecommendation.model_validate(r) for r in engine.snapshot_recommendations(row.id)])


@router.get("/recommendations", response_model=list[LearningRecommendation])
def recommendations(snapshot_id: Optional[str] = None, engine: LearningEngine = Depends(get_engine)):
    row = engine.require_snapshot(snapshot_id) if snapshot_id else engine.latest_snapshot()
    if row is None:
        return []
    return [LearningRecommendation.model_validate(r) for r in engine.snapshot_recommendations(row.id)]


@router.get("/dataset", response_model=DatasetView)
def dataset(as_of: Optional[datetime] = None, limit: int = Query(default=200, ge=1, le=5000), offset: int = Query(default=0, ge=0), engine: LearningEngine = Depends(get_engine)):
    rows = engine.dataset(as_of)
    return DatasetView(total=len(rows), feature_version=rows[0].feature_version if rows else "features-v1", as_of=rows[0].as_of if rows else None, rows=rows[offset : offset + limit])


@router.get("/expected/{candidate_opportunity_id}", response_model=ExpectedResponse)
def expected(candidate_opportunity_id: str, engine: LearningEngine = Depends(get_engine)):
    """The learned ordering signal for one candidate opportunity, with its components."""
    co = engine.db.get(CandidateOpportunityRow, candidate_opportunity_id)
    if co is None or co.tenant_id != engine.tenant_id:
        raise NotFoundError(f"Candidate opportunity not found: {candidate_opportunity_id}")
    opp = engine.db.get(OpportunityRow, co.opportunity_id)
    source = None
    if opp is not None and opp.canonical_job_id:
        from app.jobs.database.models import JobRow

        job = engine.db.get(JobRow, opp.canonical_job_id)
        source = job.source if job else None
    return engine.expected_response(source=source, company=opp.company if opp else None, title=opp.title if opp else None, fit_band=co.fit_band, candidate_opportunity_id=co.id)


@router.get("/source-discovery")
def source_discovery(engine: LearningEngine = Depends(get_engine)) -> list[dict[str, Any]]:
    """Discovery-side reliability per source (shared job data). Information only."""
    return engine.source_discovery()
