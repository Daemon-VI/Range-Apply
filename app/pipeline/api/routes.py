"""/api/v1/opportunities, /api/v1/policy, /api/v1/queue.

Reads are open (single-user product); writes need ``X-API-Key``. Tenant
comes from ``get_tenant_id`` and every repository is bound to it, so no
handler can reach another tenant's rows.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.core.errors import PolicyBlocked
from app.database import get_db
from app.jobs.database.models import JobRow
from app.pipeline.database.models import CandidateOpportunityRow
from app.pipeline.models import (
    ApplicationPolicy,
    ApplicationPolicyUpdate,
    CandidateOpportunity,
    EligibilityDecision,
    EligibilityDecisionRecord,
    FitBand,
    FitBandConfig,
    FitBandConfigUpdate,
    Lane,
    Opportunity,
    OpportunityState,
    PriorityScoreRecord,
    QueueAction,
    QueueItem,
    QueueState,
)
from app.pipeline.policy import fit_band_config
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.pipeline.sync import sync_run
from app.security import require_api_key

WRITE = [Depends(require_api_key)]
API_ACTOR = "api"

opportunities_router = APIRouter(prefix="/api/v1/opportunities", tags=["opportunities"])
policy_router = APIRouter(prefix="/api/v1/policy", tags=["policy"])
queue_router = APIRouter(prefix="/api/v1/queue", tags=["queue"])


def get_opportunity_repo(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> OpportunityRepository:
    return OpportunityRepository(db, tenant_id)


def get_policy_repo(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> PolicyRepository:
    return PolicyRepository(db, tenant_id)


def get_queue_repo(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> QueueRepository:
    return QueueRepository(db, tenant_id)


# ---------------------------------------------------------------------- #
# opportunities
# ---------------------------------------------------------------------- #


class OpportunityListItem(BaseModel):
    candidate: CandidateOpportunity
    opportunity: Opportunity
    title: str
    company: str
    source: Optional[str] = None
    location: Optional[str] = None
    posted_at: Optional[Any] = None
    job_status: Optional[str] = None
    application_url: Optional[str] = None


class OpportunityListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[OpportunityListItem] = Field(default_factory=list)


class FitSummary(BaseModel):
    match_id: str
    fit_score: int
    fit_band: Optional[FitBand] = None
    match_type: str
    confidence: str
    component_scores: dict[str, float] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    explanation: str
    policy_version: str
    engine_version: str
    evaluated_at: Optional[Any] = None


class OpportunityDetail(OpportunityListItem):
    job_ids: list[str] = Field(default_factory=list)
    eligibility: Optional[EligibilityDecisionRecord] = None
    fit: Optional[FitSummary] = None
    priority: Optional[PriorityScoreRecord] = None
    queue_items: list[QueueItem] = Field(default_factory=list)


class StateChange(BaseModel):
    state: OpportunityState
    reason: Optional[str] = None


class EnqueueRequest(BaseModel):
    action: QueueAction = QueueAction.PREPARE
    lane: Optional[Lane] = None
    force: bool = Field(default=False, description="Bypass policy admission (audited).")


class SyncRequest(BaseModel):
    run_id: Optional[str] = None


class SyncResponse(BaseModel):
    tenant_id: str
    run_id: Optional[str]
    synced: int
    opportunities_created: int
    candidate_created: int
    admitted: int
    not_admitted: int
    errors: list[str] = Field(default_factory=list)


def _opportunity_model(repo: OpportunityRepository, row) -> Opportunity:
    return Opportunity(
        id=row.id,
        identity_key=row.identity_key,
        canonical_job_id=row.canonical_job_id or "",
        company=row.company,
        title=row.title,
        location_bucket=row.location_bucket or "",
        status=row.status,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        deadline=row.deadline,
        repost_count=row.repost_count,
        job_count=len(repo.opportunity_job_ids(row.id)),
    )


def _list_item(repo: OpportunityRepository, co: CandidateOpportunityRow, job: Optional[JobRow]) -> OpportunityListItem:
    opp = co.opportunity
    return OpportunityListItem(
        candidate=CandidateOpportunity.model_validate(co),
        opportunity=_opportunity_model(repo, opp),
        title=job.title if job else opp.title,
        company=job.company if job else opp.company,
        source=job.source if job else None,
        location=job.location if job else opp.location_bucket,
        posted_at=job.posted_at if job else None,
        job_status=job.job_status if job else None,
        application_url=(job.application_url or job.source_url) if job else None,
    )


def _jobs_for(db: Session, rows: list[CandidateOpportunityRow]) -> dict[str, JobRow]:
    ids = [co.opportunity.canonical_job_id for co in rows if co.opportunity.canonical_job_id]
    if not ids:
        return {}
    return {job.id: job for job in db.query(JobRow).filter(JobRow.id.in_(ids)).all()}


@opportunities_router.get("/", response_model=OpportunityListResponse)
def list_opportunities(
    state: Optional[OpportunityState] = None,
    fit_band: Optional[FitBand] = None,
    eligibility: Optional[EligibilityDecision] = None,
    admitted: Optional[bool] = None,
    min_priority: Optional[int] = Query(default=None, ge=0, le=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = Query(default=None, max_length=200),
    repo: OpportunityRepository = Depends(get_opportunity_repo),
    db: Session = Depends(get_db),
):
    rows, total = repo.list_candidate_opportunities(state, fit_band, eligibility, admitted, min_priority, limit, offset, search=search)
    jobs = _jobs_for(db, rows)
    return OpportunityListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_list_item(repo, co, jobs.get(co.opportunity.canonical_job_id or "")) for co in rows],
    )


@opportunities_router.get("/summary")
def opportunities_summary(
    repo: OpportunityRepository = Depends(get_opportunity_repo),
    queue: QueueRepository = Depends(get_queue_repo),
) -> dict[str, Any]:
    admitted, _ = repo.list_candidate_opportunities(admitted=True, limit=1)
    _, admitted_total = repo.list_candidate_opportunities(admitted=True, limit=1)
    _, not_admitted_total = repo.list_candidate_opportunities(admitted=False, limit=1)
    return {
        "by_state": repo.counts_by_state(),
        "admitted": admitted_total,
        "not_admitted": not_admitted_total,
        "queue": queue.counts_by_state(),
    }


@opportunities_router.post("/sync", response_model=SyncResponse, dependencies=WRITE)
def sync_opportunities(
    body: Optional[SyncRequest] = None,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
):
    report = sync_run(db, tenant_id, (body or SyncRequest()).run_id, actor=API_ACTOR)
    return SyncResponse(**report.__dict__)


@opportunities_router.get("/{co_id}", response_model=OpportunityDetail)
def get_opportunity(
    co_id: str,
    repo: OpportunityRepository = Depends(get_opportunity_repo),
    queue: QueueRepository = Depends(get_queue_repo),
    db: Session = Depends(get_db),
):
    co = repo.require_candidate_opportunity(co_id)
    job = db.get(JobRow, co.opportunity.canonical_job_id) if co.opportunity.canonical_job_id else None
    base = _list_item(repo, co, job)
    match = repo.match_for(co)
    decision = repo.latest_decision(co.id)
    priority = repo.latest_priority(co.id)
    items, _ = queue.list_items(limit=50)
    return OpportunityDetail(
        **base.model_dump(),
        job_ids=repo.opportunity_job_ids(co.opportunity_id),
        eligibility=EligibilityDecisionRecord.model_validate(decision) if decision else None,
        fit=FitSummary(
            match_id=match.id,
            fit_score=match.fit_score,
            fit_band=FitBand(co.fit_band) if co.fit_band else None,
            match_type=match.match_type,
            confidence=match.confidence,
            component_scores=match.component_scores or {},
            strengths=match.strengths or [],
            gaps=match.gaps or [],
            uncertainties=match.uncertainties or [],
            explanation=match.explanation,
            policy_version=match.policy_version,
            engine_version=match.engine_version,
            evaluated_at=match.evaluated_at,
        )
        if match
        else None,
        priority=PriorityScoreRecord.model_validate(priority) if priority else None,
        queue_items=[QueueItem.model_validate(i) for i in items if i.candidate_opportunity_id == co.id],
    )


@opportunities_router.get("/{co_id}/eligibility", response_model=list[EligibilityDecisionRecord])
def opportunity_eligibility(co_id: str, repo: OpportunityRepository = Depends(get_opportunity_repo)):
    repo.require_candidate_opportunity(co_id)
    return [EligibilityDecisionRecord.model_validate(d) for d in repo.decisions_for(co_id)]


@opportunities_router.get("/{co_id}/priority", response_model=Optional[PriorityScoreRecord])
def opportunity_priority(co_id: str, repo: OpportunityRepository = Depends(get_opportunity_repo)):
    repo.require_candidate_opportunity(co_id)
    row = repo.latest_priority(co_id)
    return PriorityScoreRecord.model_validate(row) if row else None


@opportunities_router.post("/{co_id}/state", response_model=CandidateOpportunity, dependencies=WRITE)
def change_state(co_id: str, body: StateChange, repo: OpportunityRepository = Depends(get_opportunity_repo)):
    row = repo.transition(repo.require_candidate_opportunity(co_id), body.state, API_ACTOR, body.reason)
    repo.commit()
    return CandidateOpportunity.model_validate(row)


@opportunities_router.post(
    "/{co_id}/enqueue", response_model=QueueItem, status_code=status.HTTP_201_CREATED, dependencies=WRITE
)
def enqueue_opportunity(
    co_id: str,
    body: EnqueueRequest,
    repo: OpportunityRepository = Depends(get_opportunity_repo),
    policy_repo: PolicyRepository = Depends(get_policy_repo),
    queue: QueueRepository = Depends(get_queue_repo),
):
    co = repo.require_candidate_opportunity(co_id)
    if not body.force and not co.policy_admitted:
        raise PolicyBlocked(
            f"Opportunity is not admitted by the application policy: {co.policy_reason or 'not evaluated'}",
            details={"policy_reason": co.policy_reason},
        )
    policy = policy_repo.get()
    lane = body.lane or policy.lane_by_band.get(co.fit_band or "", Lane.REVIEW)
    item, created = queue.enqueue(co, body.action, API_ACTOR, lane=lane)
    if created and OpportunityState(co.state) in (
        OpportunityState.ELIGIBLE,
        OpportunityState.UNCERTAIN,
        OpportunityState.SHORTLISTED,
        OpportunityState.PREPARED,
    ):
        repo.transition(co, OpportunityState.QUEUED, API_ACTOR, f"enqueued:{body.action.value}")
    queue.commit()
    return QueueItem.model_validate(item)


# ---------------------------------------------------------------------- #
# policy
# ---------------------------------------------------------------------- #


@policy_router.get("/", response_model=ApplicationPolicy)
def get_policy(repo: PolicyRepository = Depends(get_policy_repo)):
    policy = repo.get()
    repo.commit()  # get_or_create may have inserted the default row
    return policy


@policy_router.get("/fit-bands", response_model=FitBandConfig)
def get_fit_bands(repo: PolicyRepository = Depends(get_policy_repo)):
    """Fit bands as a versioned configuration (Phase 8b). Read-only view of the policy."""
    config = fit_band_config(repo.get())
    repo.commit()
    return config


@policy_router.put("/fit-bands", response_model=FitBandConfig, dependencies=WRITE)
def update_fit_bands(body: FitBandConfigUpdate, repo: PolicyRepository = Depends(get_policy_repo)):
    """Change thresholds / enabled bands / the optional floor. Versioned and
    audited as a policy update; stored matches and past decisions are untouched."""
    update = ApplicationPolicyUpdate(band_thresholds=body.thresholds, enabled_bands=body.enabled_bands)
    if body.clear_minimum_fit_score:
        row = repo.get_or_create(API_ACTOR)
        if row.minimum_fit_score is not None:
            before = repo.snapshot(row)
            row.minimum_fit_score = None
            row.version += 1
            repo.db.flush()
            OpportunityRepository(repo.db, repo.tenant_id).record("application_policy", row.id, "updated", API_ACTOR, before, repo.snapshot(row))
    elif body.minimum_fit_score is not None:
        update.minimum_fit_score = body.minimum_fit_score
    policy = repo.update(update, API_ACTOR)
    repo.commit()
    return fit_band_config(policy)


@policy_router.get("/gates")
def get_gates(repo: PolicyRepository = Depends(get_policy_repo)) -> dict[str, Any]:
    """The Tier-1 gate ruleset: every gate, its code, where it is enforced and
    the policy parameter (with its current value) that drives it."""
    from app.pipeline.gates import GATE_RULESET_VERSION, catalog

    policy = repo.get()
    repo.commit()
    return {"ruleset_version": GATE_RULESET_VERSION, "policy_version": policy.version, "gates": catalog(policy)}


@policy_router.put("/", response_model=ApplicationPolicy, dependencies=WRITE)
def update_policy(body: ApplicationPolicyUpdate, repo: PolicyRepository = Depends(get_policy_repo)):
    policy = repo.update(body, API_ACTOR)
    repo.commit()
    return policy


# ---------------------------------------------------------------------- #
# queue
# ---------------------------------------------------------------------- #


class QueueListResponse(BaseModel):
    total: int
    items: list[QueueItem] = Field(default_factory=list)


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    action: Optional[QueueAction] = None
    lane: Optional[Lane] = None
    limit: int = Field(default=1, ge=1, le=50)
    lease_seconds: int = Field(default=600, ge=30, le=86400)


class WorkerRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    reason: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    retryable: bool = True


class ActorRequest(BaseModel):
    reason: Optional[str] = None


@queue_router.get("/", response_model=QueueListResponse)
def list_queue(
    state: Optional[QueueState] = None,
    lane: Optional[Lane] = None,
    action: Optional[QueueAction] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: QueueRepository = Depends(get_queue_repo),
):
    rows, total = repo.list_items(state, lane, action, limit, offset)
    return QueueListResponse(total=total, items=[QueueItem.model_validate(r) for r in rows])


@queue_router.get("/summary")
def queue_summary(repo: QueueRepository = Depends(get_queue_repo)) -> dict[str, int]:
    return repo.counts_by_state()


@queue_router.post("/claim", response_model=list[QueueItem], dependencies=WRITE)
def claim_items(body: ClaimRequest, repo: QueueRepository = Depends(get_queue_repo)):
    rows = repo.claim(body.worker_id, body.action, body.lane, body.limit, body.lease_seconds)
    repo.commit()
    return [QueueItem.model_validate(r) for r in rows]


@queue_router.post("/reclaim-expired", dependencies=WRITE)
def reclaim_expired(repo: QueueRepository = Depends(get_queue_repo)) -> dict[str, int]:
    count = repo.reclaim_expired(API_ACTOR)
    repo.commit()
    return {"reclaimed": count}


@queue_router.get("/{item_id}", response_model=QueueItem)
def get_queue_item(item_id: str, repo: QueueRepository = Depends(get_queue_repo)):
    return QueueItem.model_validate(repo.require(item_id))


def _worker_op(repo: QueueRepository, item_id: str, body: WorkerRequest, op: str) -> QueueItem:
    row = repo.require(item_id)
    if op == "start":
        row = repo.start(row, body.worker_id)
    elif op == "succeed":
        row = repo.succeed(row, body.worker_id, body.result)
    elif op == "fail":
        row = repo.fail(row, body.worker_id, body.reason or "unspecified", body.retryable)
    elif op == "block":
        row = repo.block(row, body.worker_id, body.reason or "blocked")
    elif op == "release":
        row = repo.release(row, body.worker_id, body.reason)
    elif op == "extend":
        row = repo.extend_lease(row, body.worker_id)
    repo.commit()
    return QueueItem.model_validate(row)


@queue_router.post("/{item_id}/start", response_model=QueueItem, dependencies=WRITE)
def start_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "start")


@queue_router.post("/{item_id}/succeed", response_model=QueueItem, dependencies=WRITE)
def succeed_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "succeed")


@queue_router.post("/{item_id}/fail", response_model=QueueItem, dependencies=WRITE)
def fail_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "fail")


@queue_router.post("/{item_id}/block", response_model=QueueItem, dependencies=WRITE)
def block_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "block")


@queue_router.post("/{item_id}/release", response_model=QueueItem, dependencies=WRITE)
def release_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "release")


@queue_router.post("/{item_id}/extend-lease", response_model=QueueItem, dependencies=WRITE)
def extend_item(item_id: str, body: WorkerRequest, repo: QueueRepository = Depends(get_queue_repo)):
    return _worker_op(repo, item_id, body, "extend")


@queue_router.post("/{item_id}/needs-review", response_model=QueueItem, dependencies=WRITE)
def needs_review_item(item_id: str, body: ActorRequest, repo: QueueRepository = Depends(get_queue_repo)):
    row = repo.needs_review(repo.require(item_id), API_ACTOR, body.reason or "needs review")
    repo.commit()
    return QueueItem.model_validate(row)


@queue_router.post("/{item_id}/cancel", response_model=QueueItem, dependencies=WRITE)
def cancel_item(item_id: str, body: Optional[ActorRequest] = None, repo: QueueRepository = Depends(get_queue_repo)):
    row = repo.cancel(repo.require(item_id), API_ACTOR, (body or ActorRequest()).reason)
    repo.commit()
    return QueueItem.model_validate(row)


@queue_router.post("/{item_id}/requeue", response_model=QueueItem, dependencies=WRITE)
def requeue_item(item_id: str, body: Optional[ActorRequest] = None, repo: QueueRepository = Depends(get_queue_repo)):
    row = repo.requeue(repo.require(item_id), API_ACTOR, (body or ActorRequest()).reason)
    repo.commit()
    return QueueItem.model_validate(row)
