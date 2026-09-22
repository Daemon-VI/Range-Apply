"""/api/v1/execution — the guarded execution flow.

    ready -> preview -> enqueue-ready -> claim -> items/{id}/start
          -> (executor works) -> items/{id}/result | items/{id}/handoff
          -> runs/{id}/verify | runs/{id}/confirm
    plus retry / cancel / answer a form field / run-queue (in-process executor).

No endpoint sets an attempt's status directly; every transition is a
server-side decision from a reported *result*.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.core.errors import ConflictError, NotFoundError
from app.database import get_db
from app.execution import submission_mode
from app.execution.models import (
    ApplicationAttempt,
    ExecutionPackage,
    ExecutionPreview,
    ExecutionResult,
    ExecutionRun,
    ExecutionStatus,
    ExecutionSummary,
    ExecutorKind,
    FormFieldRecord,
    FormSnapshot,
    FormSnapshotRecord,
    HandoffReason,
    VerificationResult,
)
from app.execution.service import ExecutionService
from app.pipeline.models import Lane, QueueItem
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/execution", tags=["execution"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def get_service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> ExecutionService:
    return ExecutionService(db, tenant_id, actor=API_ACTOR)


class AttemptList(BaseModel):
    total: int
    items: list[ApplicationAttempt] = Field(default_factory=list)


class RunList(BaseModel):
    total: int
    items: list[ExecutionRun] = Field(default_factory=list)


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=1, ge=1, le=50)
    lane: Optional[Lane] = None


class ClaimedItem(BaseModel):
    item: QueueItem
    application_id: Optional[str] = None


class StartRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    executor: ExecutorKind = ExecutorKind.MANUAL


class StartResponse(BaseModel):
    outcome: str
    run: Optional[ExecutionRun] = None
    package: Optional[ExecutionPackage] = None
    detail: dict[str, Any] = Field(default_factory=dict)


class FormRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    form: FormSnapshot


class FormResponse(BaseModel):
    snapshot: FormSnapshotRecord
    blocking_fields: int


class ResultRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    result: ExecutionResult


class HandoffRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    reason: HandoffReason
    message: str = Field(min_length=1, max_length=512)
    stopped_at: Optional[str] = None
    remaining_steps: list[str] = Field(default_factory=list)


class VerifyRequest(BaseModel):
    verification: Optional[VerificationResult] = None
    executor: Optional[ExecutorKind] = None


class ConfirmRequest(BaseModel):
    submitted: bool
    reference: Optional[str] = Field(default=None, max_length=512)
    note: Optional[str] = Field(default=None, max_length=512)


class ReasonRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=256)


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    save_to_bank: bool = False


class RunQueueRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=200)
    executor: ExecutorKind = ExecutorKind.MOCK
    lane: Optional[Lane] = None


@router.get("/ready", response_model=AttemptList)
def list_ready(limit: int = Query(default=200, ge=1, le=2000), service: ExecutionService = Depends(get_service)):
    rows = service.ready(limit)
    return AttemptList(total=len(rows), items=[ApplicationAttempt.model_validate(r) for r in rows])


@router.get("/attempts", response_model=AttemptList)
def list_attempts(
    status_filter: Optional[ApplicationStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: ExecutionService = Depends(get_service),
):
    rows, total = service.list_attempts(status_filter, limit, offset)
    return AttemptList(total=total, items=[ApplicationAttempt.model_validate(r) for r in rows])


@router.get("/summary", response_model=ExecutionSummary)
def summary(service: ExecutionService = Depends(get_service)):
    return service.summary()


@router.get("/attempts/{application_id}/preview", response_model=ExecutionPreview)
def preview(application_id: str, service: ExecutionService = Depends(get_service)):
    """Package + preconditions + current form mapping. Writes nothing."""
    return service.preview(application_id)


@router.get("/attempts/{application_id}/runs", response_model=list[ExecutionRun])
def attempt_runs(application_id: str, service: ExecutionService = Depends(get_service)):
    service.require_attempt(application_id)
    return [ExecutionRun.model_validate(r) for r in service.runs_for(application_id)]


@router.post("/enqueue-ready", dependencies=WRITE)
def enqueue_ready(service: ExecutionService = Depends(get_service)) -> dict[str, int]:
    counts = service.enqueue_ready()
    service.db.commit()
    return counts


@router.post("/claim", response_model=list[ClaimedItem], dependencies=WRITE)
def claim(body: ClaimRequest, service: ExecutionService = Depends(get_service)):
    items = service.claim(body.worker_id, body.limit, body.lane)
    out = []
    for item in items:
        attempt = service._attempt_for_item(item)
        out.append(ClaimedItem(item=QueueItem.model_validate(item), application_id=attempt.id if attempt else None))
    return out


@router.post("/items/{item_id}/start", response_model=StartResponse, dependencies=WRITE)
def start(item_id: str, body: StartRequest, service: ExecutionService = Depends(get_service)):
    item = service.queue.require(item_id)
    run, package, outcome = service.start(item, body.worker_id, body.executor)
    return StartResponse(outcome=outcome.get("outcome", "unknown"), run=ExecutionRun.model_validate(run) if run else None, package=package, detail={k: v for k, v in outcome.items() if k != "outcome"})


@router.post("/items/{item_id}/form", response_model=FormResponse, dependencies=WRITE)
def capture_form(item_id: str, body: FormRequest, service: ExecutionService = Depends(get_service)):
    """An external executor reports the discovered form; the server maps answers."""
    from app.execution.forms import blocking

    item = service.queue.require(item_id)
    service.queue._assert_owner(item, body.worker_id)
    attempt = service._attempt_for_item(item)
    if attempt is None:
        raise NotFoundError("No application attempt for this queue item")
    run = service._crashed_run(attempt)
    snapshot, answers = service.capture_form(attempt, body.form, run)
    service.db.commit()
    needs_input, needs_review = blocking(answers)
    return FormResponse(snapshot=FormSnapshotRecord.model_validate(snapshot), blocking_fields=needs_input + needs_review)


@router.post("/items/{item_id}/result", dependencies=WRITE)
def report_result(item_id: str, body: ResultRequest, service: ExecutionService = Depends(get_service)) -> dict[str, Any]:
    item = service.queue.require(item_id)
    return service.report_result(item, body.worker_id, body.result)


@router.post("/items/{item_id}/handoff", dependencies=WRITE)
def handoff(item_id: str, body: HandoffRequest, service: ExecutionService = Depends(get_service)) -> dict[str, Any]:
    item = service.queue.require(item_id)
    return service.handoff(item, body.worker_id, body.reason, body.message, body.stopped_at, body.remaining_steps)


@router.get("/runs", response_model=RunList)
def list_runs(
    status_filter: Optional[ExecutionStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: ExecutionService = Depends(get_service),
):
    rows, total = service.list_runs(status_filter, limit, offset)
    return RunList(total=total, items=[ExecutionRun.model_validate(r) for r in rows])


@router.get("/runs/{run_id}", response_model=ExecutionRun)
def get_run(run_id: str, service: ExecutionService = Depends(get_service)):
    return ExecutionRun.model_validate(service.require_run(run_id))


@router.post("/runs/{run_id}/verify", response_model=ExecutionRun, dependencies=WRITE)
def verify(run_id: str, body: Optional[VerifyRequest] = None, service: ExecutionService = Depends(get_service)):
    body = body or VerifyRequest()
    return ExecutionRun.model_validate(service.verify(run_id, body.verification, body.executor))


@router.post("/runs/{run_id}/confirm", response_model=ExecutionRun, dependencies=WRITE)
def confirm(run_id: str, body: ConfirmRequest, service: ExecutionService = Depends(get_service)):
    return ExecutionRun.model_validate(service.confirm(run_id, body.submitted, body.reference, body.note))


@router.post("/attempts/{application_id}/retry", response_model=ApplicationAttempt, dependencies=WRITE)
def retry(application_id: str, body: Optional[ReasonRequest] = None, service: ExecutionService = Depends(get_service)):
    return ApplicationAttempt.model_validate(service.retry(application_id, API_ACTOR, (body or ReasonRequest()).reason))


@router.post("/attempts/{application_id}/cancel", response_model=ApplicationAttempt, dependencies=WRITE)
def cancel(application_id: str, body: Optional[ReasonRequest] = None, service: ExecutionService = Depends(get_service)):
    return ApplicationAttempt.model_validate(service.cancel(application_id, API_ACTOR, (body or ReasonRequest()).reason))


@router.put("/fields/{field_id}/answer", response_model=FormFieldRecord, dependencies=WRITE)
def answer_field(field_id: str, body: AnswerRequest, service: ExecutionService = Depends(get_service)):
    return FormFieldRecord.model_validate(service.answer_field(field_id, body.answer, API_ACTOR, body.save_to_bank))


@router.post("/run-queue", dependencies=WRITE)
def run_queue(body: RunQueueRequest, service: ExecutionService = Depends(get_service)) -> dict[str, int]:
    """Drain SUBMIT items with an in-process executor (MOCK when enabled, MANUAL otherwise)."""
    return service.run_queue(body.worker_id, body.limit, body.executor, body.lane)


# ---------------------------------------------------------------------- #
# Browser extension (Blueprint Phase 9): the same flow, addressed by page URL
# ---------------------------------------------------------------------- #


class ExtensionMatch(BaseModel):
    item: QueueItem
    application_id: str
    attempt_status: str
    company: str
    title: str
    application_url: Optional[str] = None
    claimed_by_you: bool = False


class ExtensionStatus(BaseModel):
    connected: bool = True
    tenant_id: str
    worker_id: str
    ready: int
    pending_items: int
    dry_run_default: bool
    #: The server-side SAFE / LIVE switch; while false the gate refuses every submit click.
    live_submission_enabled: bool = False
    executor: str = ExecutorKind.BROWSER_EXTENSION.value


class UrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    worker_id: str = Field(min_length=1, max_length=128)


class ItemRequest(BaseModel):
    item_id: str
    worker_id: str = Field(min_length=1, max_length=128)


class GateResponse(BaseModel):
    ok: bool
    failures: list[str] = Field(default_factory=list)
    run_id: Optional[str] = None


def _match_payload(match) -> ExtensionMatch:
    item, attempt, opportunity, job = match
    return ExtensionMatch(
        item=QueueItem.model_validate(item),
        application_id=attempt.id,
        attempt_status=attempt.status,
        company=opportunity.company,
        title=opportunity.title,
        application_url=(job.application_url or job.source_url) if job else None,
    )


@router.get("/extension/status", response_model=ExtensionStatus, dependencies=WRITE)
def extension_status(worker_id: str = Query(default="extension", max_length=128), service: ExecutionService = Depends(get_service)):
    """Connection check for the extension popup (needs the API key, so it also proves the key)."""
    from app.config import settings as app_settings

    counts = service.queue.counts_by_state()
    return ExtensionStatus(tenant_id=service.tenant_id, worker_id=worker_id, ready=len(service.ready()), pending_items=counts.get("PENDING", 0) + counts.get("RETRY_WAIT", 0), dry_run_default=app_settings.playwright_dry_run, live_submission_enabled=submission_mode.is_live_enabled(service.tenant_id))


@router.get("/extension/match", response_model=Optional[ExtensionMatch], dependencies=WRITE)
def extension_match(url: str = Query(min_length=1, max_length=4096), worker_id: str = Query(default="extension", max_length=128), service: ExecutionService = Depends(get_service)):
    """Is the page the person is on an application the queue wants submitted?"""
    match = service.match_for_url(url, worker_id)
    if match is None:
        return None
    payload = _match_payload(match)
    payload.claimed_by_you = match[0].claimed_by == worker_id and match[0].state in ("CLAIMED", "PROCESSING")
    return payload


@router.post("/extension/claim", response_model=Optional[ExtensionMatch], dependencies=WRITE)
def extension_claim(body: UrlRequest, service: ExecutionService = Depends(get_service)):
    """Claim the SUBMIT item for the current page (guarded; another live worker's item is refused)."""
    match = service.match_for_url(body.url, body.worker_id)
    if match is None:
        return None
    claimed = service.claim_item(match[0], body.worker_id)
    if claimed is None:
        raise ConflictError("This application is being handled by another worker or is not runnable yet")
    payload = _match_payload((claimed, match[1], match[2], match[3]))
    payload.claimed_by_you = True
    return payload


@router.post("/extension/heartbeat", response_model=QueueItem, dependencies=WRITE)
def extension_heartbeat(body: ItemRequest, service: ExecutionService = Depends(get_service)):
    """Keep the lease while the person works on the page."""
    item = service.queue.require(body.item_id)
    return QueueItem.model_validate(service.heartbeat(item, body.worker_id))


@router.post("/extension/gate", response_model=GateResponse, dependencies=WRITE)
def extension_gate(body: ItemRequest, service: ExecutionService = Depends(get_service)):
    """Call immediately before submit: every precondition again, then submit_invoked is recorded."""
    item = service.queue.require(body.item_id)
    return GateResponse(**service.gate(item, body.worker_id))
