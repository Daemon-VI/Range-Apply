"""/api/v1/signals — the Signal Inbox (Blueprint Phase 10).

    ingest (generic | email) -> list / get / trace / review queue
    -> classify | attribute (reprocess) -> confirm-classification | reject-classification
    -> link | confirm-outcome | ignore | merge
    outcomes: list current statuses, one application's event history.

Reads are open (single-user product); every write needs ``X-API-Key``.
Nothing here returns credentials, raw email bodies or headers: a signal
carries a bounded, redacted excerpt and structured metadata only.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.database import get_db
from app.security import require_api_key
from app.signals.models import (
    ApplicationOutcome,
    EmailMessage,
    OutcomeEvent,
    OutcomeKind,
    Signal,
    SignalCategory,
    SignalIngest,
    SignalSource,
    SignalStatus,
    SignalSummary,
    SignalTrace,
)
from app.signals.service import SignalInboxService

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])
WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def get_service(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)) -> SignalInboxService:
    return SignalInboxService(db, tenant_id, actor=API_ACTOR)


class IngestResponse(BaseModel):
    signal: Signal
    created: bool


class BatchIngestResponse(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    signals: list[Signal] = Field(default_factory=list)


class SignalList(BaseModel):
    total: int
    items: list[Signal] = Field(default_factory=list)


class OutcomeList(BaseModel):
    total: int
    items: list[ApplicationOutcome] = Field(default_factory=list)


class OutcomeHistory(BaseModel):
    application_id: str
    current: Optional[ApplicationOutcome] = None
    events: list[OutcomeEvent] = Field(default_factory=list)


class LinkRequest(BaseModel):
    application_id: str = Field(min_length=1, max_length=36)
    note: Optional[str] = Field(default=None, max_length=256)


class ClassificationRequest(BaseModel):
    category: SignalCategory
    note: Optional[str] = Field(default=None, max_length=256)


class OutcomeRequest(BaseModel):
    outcome: OutcomeKind
    note: Optional[str] = Field(default=None, max_length=256)


class NoteRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=256)


class MergeRequest(BaseModel):
    into_signal_id: str = Field(min_length=1, max_length=36)
    note: Optional[str] = Field(default=None, max_length=256)


def _signal(row) -> Signal:
    return Signal.model_validate(row)


# ------------------------------------------------------------- ingest


@router.post("", response_model=IngestResponse, dependencies=WRITE)
def ingest(body: SignalIngest, service: SignalInboxService = Depends(get_service)):
    """One observation from any source. Idempotent per tenant on the source reference or the content hash."""
    row, created = service.ingest(body)
    service.db.commit()
    return IngestResponse(signal=_signal(row), created=created)


@router.post("/batch", response_model=BatchIngestResponse, dependencies=WRITE)
def ingest_batch(body: list[SignalIngest], service: SignalInboxService = Depends(get_service)):
    rows = [service.ingest(item)[0] for item in body[:500]]
    service.db.commit()
    return BatchIngestResponse(counts=dict(service.counts), signals=[_signal(r) for r in rows])


@router.post("/email", response_model=IngestResponse, dependencies=WRITE)
def ingest_email(body: EmailMessage, service: SignalInboxService = Depends(get_service)):
    """A supplied email (pasted or from a future connector). No mailbox is read."""
    row, created = service.ingest_email(body)
    service.db.commit()
    return IngestResponse(signal=_signal(row), created=created)


# --------------------------------------------------------------- reads


@router.get("", response_model=SignalList)
def list_signals(
    status_filter: Optional[SignalStatus] = Query(default=None, alias="status"),
    source: Optional[SignalSource] = None,
    category: Optional[SignalCategory] = None,
    application_id: Optional[str] = None,
    outcome: Optional[OutcomeKind] = None,
    company: Optional[str] = Query(default=None, max_length=128),
    days: Optional[int] = Query(default=None, ge=1, le=3650),
    review: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: SignalInboxService = Depends(get_service),
):
    rows, total = service.list_signals(status_filter, source, category, application_id, outcome, company, days, review, limit, offset)
    return SignalList(total=total, items=[_signal(r) for r in rows])


@router.get("/review", response_model=SignalList)
def review_queue(limit: int = Query(default=100, ge=1, le=1000), service: SignalInboxService = Depends(get_service)):
    """Unmatched, ambiguous, uncertain and weakly verified signals awaiting a person."""
    rows, total = service.list_signals(review=True, limit=limit)
    return SignalList(total=total, items=[_signal(r) for r in rows])


@router.get("/summary", response_model=SignalSummary)
def summary(service: SignalInboxService = Depends(get_service)):
    return service.summary()


@router.get("/outcomes", response_model=OutcomeList)
def list_outcomes(
    current: Optional[OutcomeKind] = None,
    needs_review: Optional[bool] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: SignalInboxService = Depends(get_service),
):
    rows, total = service.list_outcomes(current, needs_review, limit, offset)
    return OutcomeList(total=total, items=[ApplicationOutcome.model_validate(r) for r in rows])


@router.get("/outcomes/{application_id}", response_model=OutcomeHistory)
def outcome_history(application_id: str, service: SignalInboxService = Depends(get_service)):
    current, events = service.outcome_history(application_id)
    return OutcomeHistory(application_id=application_id, current=ApplicationOutcome.model_validate(current) if current else None, events=[OutcomeEvent.model_validate(e) for e in events])


@router.get("/{signal_id}", response_model=Signal)
def get_signal(signal_id: str, service: SignalInboxService = Depends(get_service)):
    return _signal(service.require_signal(signal_id))


@router.get("/{signal_id}/trace", response_model=SignalTrace)
def trace(signal_id: str, service: SignalInboxService = Depends(get_service)):
    """Signal → attribution → opportunity → candidate opportunity → preparation → execution → application history."""
    return service.trace(signal_id)


# -------------------------------------------------------------- review


@router.post("/{signal_id}/classify", response_model=Signal, dependencies=WRITE)
def classify(signal_id: str, service: SignalInboxService = Depends(get_service)):
    """Re-run classification, attribution and outcome derivation (deterministic; AI only if enabled)."""
    row = service.reprocess(signal_id, API_ACTOR)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/attribute", response_model=Signal, dependencies=WRITE)
def attribute(signal_id: str, service: SignalInboxService = Depends(get_service)):
    row = service.reprocess(signal_id, API_ACTOR)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/link", response_model=Signal, dependencies=WRITE)
def link(signal_id: str, body: LinkRequest, service: SignalInboxService = Depends(get_service)):
    row = service.link(signal_id, body.application_id, API_ACTOR, body.note)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/confirm-classification", response_model=Signal, dependencies=WRITE)
def confirm_classification(signal_id: str, body: ClassificationRequest, service: SignalInboxService = Depends(get_service)):
    row = service.confirm_classification(signal_id, body.category, API_ACTOR, body.note)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/reject-classification", response_model=Signal, dependencies=WRITE)
def reject_classification(signal_id: str, body: Optional[NoteRequest] = None, service: SignalInboxService = Depends(get_service)):
    row = service.reject_classification(signal_id, API_ACTOR, (body or NoteRequest()).note)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/confirm-outcome", response_model=Signal, dependencies=WRITE)
def confirm_outcome(signal_id: str, body: OutcomeRequest, service: SignalInboxService = Depends(get_service)):
    row = service.confirm_outcome(signal_id, body.outcome, API_ACTOR, body.note)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/ignore", response_model=Signal, dependencies=WRITE)
def ignore(signal_id: str, body: Optional[NoteRequest] = None, service: SignalInboxService = Depends(get_service)):
    row = service.ignore(signal_id, API_ACTOR, (body or NoteRequest()).note)
    service.db.commit()
    return _signal(row)


@router.post("/{signal_id}/merge", response_model=Signal, dependencies=WRITE)
def merge(signal_id: str, body: MergeRequest, service: SignalInboxService = Depends(get_service)):
    row = service.merge(signal_id, body.into_signal_id, API_ACTOR, body.note)
    service.db.commit()
    return _signal(row)


@router.post("/purge-excerpts", dependencies=WRITE)
def purge_excerpts(older_than_days: Optional[int] = Query(default=None, ge=0, le=3650), service: SignalInboxService = Depends(get_service)) -> dict[str, Any]:
    """Drop stored excerpts of settled signals older than the retention (metadata and outcomes stay)."""
    from app.config import settings as app_settings

    days = app_settings.signal_excerpt_retention_days if older_than_days is None else older_than_days
    purged = service.purge_excerpts(days)
    service.db.commit()
    return {"purged": purged, "older_than_days": days}
