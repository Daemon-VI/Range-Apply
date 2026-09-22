"""Read models and enums for the scheduler."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.models import AdmissionReason, Lane, TailoringLevel


class SchedulerRunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Reasons that are *not* refusals: the work exists or is waiting on a human.
PROGRESS_REASONS = frozenset(
    {
        AdmissionReason.ADMITTED,
        AdmissionReason.ALREADY_IN_PROGRESS,
        AdmissionReason.ALREADY_SUBMITTED,
        AdmissionReason.NEEDS_REVIEW,
        AdmissionReason.NEEDS_USER_INPUT,
        AdmissionReason.WINDOW_DEFERRED,
    }
)


class Decision(BaseModel):
    """One scheduling decision for one candidate opportunity."""

    candidate_opportunity_id: str
    opportunity_id: str
    company: str
    title: str
    code: AdmissionReason
    reason: str
    admitted: bool
    order: int
    priority_score: Optional[int] = None
    fit_score: Optional[int] = None
    fit_band: Optional[str] = None
    eligibility: Optional[str] = None
    state: Optional[str] = None
    lane: Optional[Lane] = None
    tailoring_level: Optional[TailoringLevel] = None
    queue_item_id: Optional[str] = None
    application_id: Optional[str] = None
    preparation_id: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SchedulerRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    status: SchedulerRunStatus
    trigger: str
    actor: str
    dry_run: bool
    policy_version: int
    window_size: int
    resumed_from_run_id: Optional[str] = None
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    considered: int = 0
    admissible: int = 0
    admitted: int = 0
    enqueued: int = 0
    requeued: int = 0
    already_queued: int = 0
    already_completed: int = 0
    ready_for_execution: int = 0
    needs_review: int = 0
    needs_user_input: int = 0
    released: int = 0
    reprepare_requeued: int = 0
    execution_enqueued: int = 0
    deferred: int = 0
    blocked: int = 0
    blocked_by_reason: dict[str, int] = Field(default_factory=dict)
    samples: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    preparation: Optional[dict[str, Any]] = None
    errors: list[str] = Field(default_factory=list)


class PeriodUsage(BaseModel):
    kind: str
    key: str
    cap: int
    used: int
    remaining: int
    starts_at: datetime
    ends_at: datetime


class CooldownEntry(BaseModel):
    company: str
    until: datetime
    source: str


class CapacitySummary(BaseModel):
    tenant_id: str
    policy_version: int
    timezone: str
    now_local: datetime
    day: PeriodUsage
    week: PeriodUsage
    paused: bool
    cooldown_days: int
    cooldowns_active: int
    cooldowns: list[CooldownEntry] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    attempts_by_status: dict[str, int] = Field(default_factory=dict)
    queue_by_state: dict[str, int] = Field(default_factory=dict)
    ready_for_execution: int = 0
    last_run: Optional[SchedulerRun] = None
    running: Optional[SchedulerRun] = None


class PreviewResponse(BaseModel):
    tenant_id: str
    policy_version: int
    window_size: int
    considered: int
    admissible: int
    would_admit: int
    deferred: int
    by_code: dict[str, int] = Field(default_factory=dict)
    capacity: CapacitySummary
    decisions: list[Decision] = Field(default_factory=list)


class RunRequest(BaseModel):
    trigger: str = Field(default="manual", max_length=32)
    window: int = Field(default=500, ge=1, le=20000)
    prepare: bool = False
    prepare_limit: int = Field(default=100, ge=1, le=5000)
    worker_id: Optional[str] = Field(default=None, max_length=128)
    force: bool = Field(default=False, description="Take over a run whose heartbeat is stale.")
