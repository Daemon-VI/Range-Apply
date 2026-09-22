"""/api/v1/ai — inspect and control AI usage for the current tenant.

Reads are open (single-user product); writes need ``X-API-Key``. Global
provider credentials never appear here: the response says whether a
provider is *configured*, not what the key is. Tenant settings live on the
application policy, so every change is versioned and audited there.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.database.models import AIUsageRow
from app.ai.gateway import get_gateway
from app.ai.models import AIUsageRecord, EffectiveAIConfig, TenantAISettings
from app.ai.providers import KNOWN_PROVIDERS
from app.api.deps import get_tenant_id
from app.database import get_db
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


class AIConfigView(BaseModel):
    tenant_id: str
    policy_version: int
    tenant_settings: TenantAISettings
    effective: EffectiveAIConfig
    global_config: dict[str, Any]
    known_providers: list[str] = Field(default_factory=lambda: list(KNOWN_PROVIDERS))


class AIStatusView(BaseModel):
    tenant_id: str
    gateway: dict[str, Any]
    tenant_usage: dict[str, Any]
    recent: list[dict[str, Any]] = Field(default_factory=list)


def _global_config() -> dict[str, Any]:
    cfg = get_gateway().config
    data = cfg.model_dump()
    data.pop("cache_dir", None)  # a local path is not a setting to expose
    return data


def _view(repo: PolicyRepository) -> AIConfigView:
    policy = repo.get()
    gateway = get_gateway()
    return AIConfigView(tenant_id=repo.tenant_id, policy_version=policy.version, tenant_settings=policy.ai_settings, effective=gateway.effective(policy.ai_settings), global_config=_global_config())


def get_policy_repo(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> PolicyRepository:
    return PolicyRepository(db, tenant_id)


@router.get("/config", response_model=AIConfigView)
def ai_config(repo: PolicyRepository = Depends(get_policy_repo)):
    view = _view(repo)
    repo.commit()
    return view


@router.put("/config", response_model=AIConfigView, dependencies=WRITE)
def update_ai_config(body: TenantAISettings, repo: PolicyRepository = Depends(get_policy_repo)):
    """Tenant AI settings (narrowing only). Audited as an application_policy update."""
    repo.update(ApplicationPolicyUpdate(ai_settings=body), API_ACTOR)
    view = _view(repo)
    repo.commit()
    return view


@router.get("/status", response_model=AIStatusView)
def ai_status(limit: int = Query(default=20, ge=1, le=200), db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    gateway = get_gateway()
    rows = db.query(AIUsageRow).filter(AIUsageRow.tenant_id == tenant_id).order_by(AIUsageRow.created_at.desc(), AIUsageRow.id.desc()).limit(limit).all()
    by_status: dict[str, int] = {}
    by_operation: dict[str, int] = {}
    for status, count in db.query(AIUsageRow.status, __import__("sqlalchemy").func.count(AIUsageRow.id)).filter(AIUsageRow.tenant_id == tenant_id).group_by(AIUsageRow.status).all():
        by_status[status] = int(count)
    for operation, count in db.query(AIUsageRow.operation, __import__("sqlalchemy").func.count(AIUsageRow.id)).filter(AIUsageRow.tenant_id == tenant_id).group_by(AIUsageRow.operation).all():
        by_operation[operation] = int(count)
    provider_calls = sum(1 for r in rows if r.status == "OK" and not r.cache_hit)
    return AIStatusView(
        tenant_id=tenant_id,
        gateway=gateway.stats(),
        tenant_usage={"by_status": by_status, "by_operation": by_operation, "recent_provider_calls": provider_calls},
        recent=[AIUsageRecord.model_validate(r).model_dump(mode="json") for r in rows],
    )


@router.get("/usage", response_model=list[AIUsageRecord])
def ai_usage(limit: int = Query(default=100, ge=1, le=1000), operation: Optional[str] = None, db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    query = db.query(AIUsageRow).filter(AIUsageRow.tenant_id == tenant_id)
    if operation:
        query = query.filter(AIUsageRow.operation == operation)
    return [AIUsageRecord.model_validate(r) for r in query.order_by(AIUsageRow.created_at.desc(), AIUsageRow.id.desc()).limit(limit).all()]


@router.post("/cache/clear", dependencies=WRITE)
def clear_cache() -> dict[str, int]:
    """Drop every cached AI answer (job-side entries are shared; solo product)."""
    return {"removed": get_gateway().cache.clear()}
