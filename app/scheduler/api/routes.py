"""/api/v1/scheduler — preview, run, status, history, capacity, queue summary.

Everything here is tenant-scoped through ``get_tenant_id``; nothing submits.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.core.errors import NotFoundError
from app.database import get_db
from app.pipeline.database.models import ApplicationQueueRow
from app.pipeline.queue import QueueRepository
from app.scheduler.models import CapacitySummary, PreviewResponse, RunRequest, SchedulerRun
from app.scheduler.service import SchedulerService
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def get_service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> SchedulerService:
    return SchedulerService(db, tenant_id, actor=API_ACTOR)


class HistoryResponse(BaseModel):
    total: int
    items: list[SchedulerRun] = Field(default_factory=list)


class QueueSummary(BaseModel):
    tenant_id: str
    by_state: dict[str, int]
    by_action: dict[str, dict[str, int]]
    ready_for_execution: int


@router.get("/preview", response_model=PreviewResponse)
def preview(
    window: int = Query(default=500, ge=1, le=20000),
    limit: int = Query(default=200, ge=0, le=5000, description="Decisions to include in the response."),
    service: SchedulerService = Depends(get_service),
):
    """Dry run: what the next scheduling pass would do. Writes nothing."""
    return service.preview(window=window, limit=limit)


@router.post("/run", response_model=SchedulerRun, dependencies=WRITE)
def run(body: Optional[RunRequest] = None, service: SchedulerService = Depends(get_service)):
    body = body or RunRequest()
    row = service.run(
        trigger=body.trigger,
        window=body.window,
        prepare=body.prepare,
        prepare_limit=body.prepare_limit,
        worker_id=body.worker_id,
        force=body.force,
    )
    return SchedulerRun.model_validate(row)


@router.get("/status", response_model=CapacitySummary)
def status(service: SchedulerService = Depends(get_service)):
    """Policy status, today's and this week's usage, remaining capacity, cool-downs, last run."""
    return service.capacity()


@router.get("/capacity", response_model=CapacitySummary)
def capacity(service: SchedulerService = Depends(get_service)):
    return service.capacity()


@router.get("/runs", response_model=HistoryResponse)
def history(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: SchedulerService = Depends(get_service),
):
    rows, total = service.history(limit, offset)
    return HistoryResponse(total=total, items=[SchedulerRun.model_validate(r) for r in rows])


@router.get("/runs/{run_id}", response_model=SchedulerRun)
def get_run(run_id: str, service: SchedulerService = Depends(get_service)):
    row = service.get_run(run_id)
    if row is None:
        raise NotFoundError(f"Scheduler run not found: {run_id}")
    return SchedulerRun.model_validate(row)


@router.get("/queue", response_model=QueueSummary)
def queue_summary(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> Any:
    """Queue counts by state and by action, plus how many attempts are execution-ready."""
    queue = QueueRepository(db, tenant_id)
    rows = (
        db.query(ApplicationQueueRow.action, ApplicationQueueRow.state, func.count(ApplicationQueueRow.id))
        .filter(ApplicationQueueRow.tenant_id == tenant_id)
        .group_by(ApplicationQueueRow.action, ApplicationQueueRow.state)
        .all()
    )
    by_action: dict[str, dict[str, int]] = {}
    for action, state, count in rows:
        by_action.setdefault(action, {})[state] = count
    service = SchedulerService(db, tenant_id, actor=API_ACTOR)
    return QueueSummary(
        tenant_id=tenant_id,
        by_state=queue.counts_by_state(),
        by_action=by_action,
        ready_for_execution=len(service.ready_for_execution()),
    )
