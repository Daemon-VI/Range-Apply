"""Domain models for the application engine (Phase 5)."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    """Lifecycle of one application *attempt* (blueprint Phase 6).

    Conceptual name        -> value here
    RESERVED               -> QUALIFIED (the scheduler reserved a cap slot)
    PREPARING              -> PREPARING
    READY_FOR_EXECUTION    -> READY (a READY preparation is attached)
    EXECUTING              -> SUBMITTING (an executor holds the attempt)
    SUBMITTED              -> SUBMITTED
    VERIFIED               -> VERIFIED
    UNKNOWN / AWAITING_VERIFICATION -> UNCERTAIN (submit may have happened)
    BLOCKED (human handoff: CAPTCHA/AUTH/MFA/...) -> BLOCKED (+ blocked_reason)
    NEEDS_USER_INPUT / NEEDS_REVIEW -> as named
    FAILED_PERMANENT       -> FAILED (retryable failures keep the attempt READY;
                              the queue item carries the retry)
    CANCELLED              -> CANCELLED (a person ended it) / CLOSED (the opening
                              closed or the policy pulled it)
    The queue item owns scheduling/lease state, the preparation owns material
    readiness, the execution run owns one executor invocation's result.
    """

    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    SHORTLISTED = "SHORTLISTED"
    PREPARING = "PREPARING"
    READY = "READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    UNCERTAIN = "UNCERTAIN"
    BLOCKED = "BLOCKED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERVIEWING = "INTERVIEWING"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


#: Guarded transitions for the execution foundation. Legacy paths
#: (``ApplicationEngine.prepare/submit``) predate this table and are left as
#: they are; everything in ``app/execution`` goes through it.
ATTEMPT_TRANSITIONS: dict[ApplicationStatus, frozenset] = {
    ApplicationStatus.DISCOVERED: frozenset({ApplicationStatus.QUALIFIED, ApplicationStatus.READY, ApplicationStatus.CLOSED, ApplicationStatus.CANCELLED}),
    ApplicationStatus.QUALIFIED: frozenset({ApplicationStatus.PREPARING, ApplicationStatus.READY, ApplicationStatus.FAILED, ApplicationStatus.CLOSED, ApplicationStatus.CANCELLED}),
    ApplicationStatus.SHORTLISTED: frozenset({ApplicationStatus.QUALIFIED, ApplicationStatus.PREPARING, ApplicationStatus.READY, ApplicationStatus.CLOSED, ApplicationStatus.CANCELLED}),
    ApplicationStatus.PREPARING: frozenset({ApplicationStatus.READY, ApplicationStatus.NEEDS_USER_INPUT, ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.FAILED, ApplicationStatus.CLOSED, ApplicationStatus.CANCELLED}),
    ApplicationStatus.READY: frozenset({ApplicationStatus.SUBMITTING, ApplicationStatus.PREPARING, ApplicationStatus.NEEDS_USER_INPUT, ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.BLOCKED, ApplicationStatus.FAILED, ApplicationStatus.CLOSED, ApplicationStatus.CANCELLED}),
    ApplicationStatus.AWAITING_APPROVAL: frozenset({ApplicationStatus.SUBMITTING, ApplicationStatus.READY, ApplicationStatus.CANCELLED}),
    ApplicationStatus.SUBMITTING: frozenset({ApplicationStatus.SUBMITTED, ApplicationStatus.UNCERTAIN, ApplicationStatus.READY, ApplicationStatus.BLOCKED, ApplicationStatus.NEEDS_USER_INPUT, ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.FAILED}),
    ApplicationStatus.SUBMITTED: frozenset({ApplicationStatus.VERIFIED, ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.INTERVIEWING, ApplicationStatus.REJECTED, ApplicationStatus.CLOSED}),
    ApplicationStatus.VERIFIED: frozenset({ApplicationStatus.INTERVIEWING, ApplicationStatus.REJECTED, ApplicationStatus.CLOSED}),
    ApplicationStatus.UNCERTAIN: frozenset({ApplicationStatus.SUBMITTED, ApplicationStatus.VERIFIED, ApplicationStatus.NEEDS_REVIEW}),
    ApplicationStatus.BLOCKED: frozenset({ApplicationStatus.READY, ApplicationStatus.CANCELLED, ApplicationStatus.CLOSED}),
    ApplicationStatus.NEEDS_USER_INPUT: frozenset({ApplicationStatus.READY, ApplicationStatus.PREPARING, ApplicationStatus.CANCELLED, ApplicationStatus.CLOSED}),
    ApplicationStatus.NEEDS_REVIEW: frozenset({ApplicationStatus.READY, ApplicationStatus.PREPARING, ApplicationStatus.SUBMITTED, ApplicationStatus.VERIFIED, ApplicationStatus.FAILED, ApplicationStatus.CANCELLED, ApplicationStatus.CLOSED}),
    ApplicationStatus.FAILED: frozenset({ApplicationStatus.QUALIFIED, ApplicationStatus.READY, ApplicationStatus.CLOSED}),
    ApplicationStatus.CANCELLED: frozenset({ApplicationStatus.QUALIFIED}),
    ApplicationStatus.INTERVIEWING: frozenset({ApplicationStatus.REJECTED, ApplicationStatus.CLOSED}),
    ApplicationStatus.REJECTED: frozenset({ApplicationStatus.CLOSED}),
    ApplicationStatus.CLOSED: frozenset({ApplicationStatus.QUALIFIED}),
}


class Application(BaseModel):
    """Mirrors ``ApplicationRow``."""

    id: str
    job_id: str
    status: ApplicationStatus
    adapter: Optional[str] = None
    dry_run: bool = True
    tailored_artifact_ids: List[str] = Field(default_factory=list)
    confirmation: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApplicationEvent(BaseModel):
    """Mirrors ``ApplicationEventRow``."""

    id: str
    application_id: str
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    metadata_: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
