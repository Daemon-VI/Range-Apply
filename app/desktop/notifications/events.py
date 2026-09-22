"""Turn existing rows into notifications — read-only, tenant-scoped.

Three sources, each read once per poll with a bounded, watermarked query:

1. ``application_events`` joined to ``applications`` (the event table has no
   tenant of its own). Every execution-path attempt transition is written
   there by :meth:`app.scheduler.attempts.AttemptRepository.transition` as
   ``attempt:<status>``; the ``to_status`` is what the person cares about.
2. ``execution_runs`` — only finished failures that did *not* move the
   attempt (a retryable failure leaves it READY), so the person learns the
   run failed without a second "ready" story.
3. ``signals`` — the inbox's own rows, minus execution's own evidence.

Nothing here writes: no ``add``, no ``flush``, no ``commit``. Nothing here
starts, retries or changes an application — a notification is a sentence.

Privacy: the text may carry company, role, status/reason words and CareerOS
ids. It never carries a signal's subject, sender or excerpt, an answer, a
document, an employer URL, an error message, a key or a candidate detail.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.models import ApplicationStatus
from app.core.timeutils import utc_now
from app.desktop.notifications.cursor import (
    APPLICATION_EVENTS,
    EXECUTION_RUNS,
    SIGNALS,
    Cursor,
)
from app.desktop.notifications.model import (
    APPLICATION_READY,
    ATTENTION_REQUIRED,
    DRY_RUN_COMPLETE,
    EXECUTION_FAILED,
    INTERVIEW_SIGNAL,
    MAX_BODY,
    OTHER_SIGNAL,
    REJECTION_SIGNAL,
    SUBMISSION_COMPLETED,
    TITLES,
    VERIFICATION_RESULT,
    Notification,
)
from app.execution.database.models import ExecutionRunRow
from app.pipeline.database.models import OpportunityRow
from app.signals.database.models import SignalRow

logger = logging.getLogger(__name__)

#: Rows scanned per source per poll. A desktop poll is every few seconds.
QUERY_LIMIT = 500

#: Fallbacks when the opportunity is gone (the join is optional by design).
UNKNOWN_COMPANY = "an employer"
UNKNOWN_ROLE = "this role"

MAX_COMPANY = 60
MAX_ROLE = 70

#: ``to_status`` -> notification kind. Anything else is not worth a toast.
STATUS_KINDS: dict[str, str] = {
    ApplicationStatus.READY.value: APPLICATION_READY,
    ApplicationStatus.BLOCKED.value: ATTENTION_REQUIRED,
    ApplicationStatus.NEEDS_USER_INPUT.value: ATTENTION_REQUIRED,
    ApplicationStatus.NEEDS_REVIEW.value: ATTENTION_REQUIRED,
    ApplicationStatus.AWAITING_APPROVAL.value: ATTENTION_REQUIRED,
    ApplicationStatus.SUBMITTED.value: SUBMISSION_COMPLETED,
    ApplicationStatus.VERIFIED.value: VERIFICATION_RESULT,
    ApplicationStatus.UNCERTAIN.value: VERIFICATION_RESULT,
    ApplicationStatus.FAILED.value: EXECUTION_FAILED,
    ApplicationStatus.INTERVIEWING.value: INTERVIEW_SIGNAL,
    ApplicationStatus.REJECTED.value: REJECTION_SIGNAL,
}

#: One short phrase per handoff / attention reason (vocabulary only).
ATTENTION_PHRASES: dict[str, str] = {
    "CAPTCHA_REQUIRED": "CAPTCHA requires your attention",
    "AUTH_REQUIRED": "A login is required",
    "MFA_REQUIRED": "A verification code is required",
    "UNSUPPORTED_FORM": "The form cannot be filled automatically",
    "AMBIGUOUS_FORM": "No application form was found",
    "UNKNOWN_REQUIRED_FIELD": "An answer is needed",
    "NEEDS_USER_INPUT": "An answer is needed",
    "NEEDS_REVIEW": "Your review is needed",
    "USER_CONFIRMATION_REQUIRED": "Your approval is needed",
    "AWAITING_APPROVAL": "Your approval is needed",
    "ARTIFACT_FILE_REQUIRED": "A document must be regenerated",
}

ATTENTION_FALLBACK = "Your attention is needed"

#: Signal category -> kind. Categories outside this map are never announced
#: (EXECUTION_RESULT is execution's own evidence: source 1 already told the
#: story, with the attempt's vocabulary rather than the inbox's).
SIGNAL_KINDS: dict[str, str] = {
    "INTERVIEW_INVITATION": INTERVIEW_SIGNAL,
    "REJECTION": REJECTION_SIGNAL,
    "RECRUITER_CONTACT": OTHER_SIGNAL,
    "ASSESSMENT": OTHER_SIGNAL,
    "INFORMATION_REQUEST": OTHER_SIGNAL,
}

OTHER_SIGNAL_BODIES: dict[str, str] = {
    "RECRUITER_CONTACT": "A recruiter may have contacted you about {company}.",
    "ASSESSMENT": "An assessment may have been requested by {company}.",
    "INFORMATION_REQUEST": "{company} may have asked for more information.",
}

#: Run rows that mean "this invocation failed" (handoffs are excluded
#: separately: they move the attempt and are told through source 1).
FAILED_RUN_STATUSES = ("FAILED_RETRYABLE", "FAILED_PERMANENT")
FAILED_RUN_OUTCOMES = ("RETRYABLE_FAILURE", "PERMANENT_FAILURE")
#: A finished dry run leaves the attempt READY (its READY event is suppressed
#: as "from SUBMITTING"), so the run row itself is what announces it.
DRY_RUN_STATUS = "DRY_RUN"

HIGH = "high"
NORMAL = "normal"


# ---------------------------------------------------------------- text bits


def _clip(value: Optional[str], limit: int, fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _company(opportunity: Optional[OpportunityRow]) -> str:
    return _clip(getattr(opportunity, "company", None), MAX_COMPANY, UNKNOWN_COMPANY)


def _role(opportunity: Optional[OpportunityRow]) -> str:
    return _clip(getattr(opportunity, "title", None), MAX_ROLE, UNKNOWN_ROLE)


def _body(text: str) -> str:
    """Hard guarantee for the dataclass' ``MAX_BODY`` invariant."""
    return text if len(text) <= MAX_BODY else text[: MAX_BODY - 1].rstrip() + "…"


def _vocabulary(value: Any) -> Optional[str]:
    """Accept a reason only when it is a bare code word.

    Attempt-event metadata carries a free-text ``message`` alongside the
    reason, and ``status_reason`` is ``"CODE: message"``. Only an
    all-caps token is ever put in front of the person.
    """
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 32:
        return None
    if not all(ch.isupper() or ch == "_" or ch.isdigit() for ch in token):
        return None
    return token


def _attention_reason(attempt: ApplicationRow, event: ApplicationEventRow, to_status: str) -> str:
    if to_status == ApplicationStatus.BLOCKED.value:
        blocked = _vocabulary(attempt.blocked_reason)
        if blocked:
            return blocked
        metadata = event.metadata_ if isinstance(event.metadata_, dict) else {}
        for key in ("handoff_reason", "reason"):
            token = _vocabulary(metadata.get(key))
            if token:
                return token
        return "BLOCKED"
    return to_status


# --------------------------------------------------------------- one source


def _attempt_path(application_id: str) -> str:
    return f"/desktop/applications/{application_id}"


def _event_notification(event: ApplicationEventRow, attempt: ApplicationRow, opportunity: Optional[OpportunityRow], tenant_id: str) -> Optional[Notification]:
    to_status = (event.to_status or "").upper()
    kind = STATUS_KINDS.get(to_status)
    if kind is None:
        return None
    if to_status == ApplicationStatus.READY.value and attempt.status != ApplicationStatus.READY.value:
        # It moved on since (an executor claimed it, a person cancelled it):
        # telling someone to review it now would send them to a dead end.
        return None
    if to_status == ApplicationStatus.READY.value and (event.from_status or "").upper() == ApplicationStatus.SUBMITTING.value:
        # A retryable failure hands the attempt back to READY; the failed run
        # is announced (EXECUTION_FAILED), not a second "ready for review".
        return None
    company = _company(opportunity)
    role = _role(opportunity)
    reason: Optional[str] = None
    importance = NORMAL

    if kind == APPLICATION_READY:
        body = f"Your application for {role} at {company} is ready for review."
    elif kind == ATTENTION_REQUIRED:
        reason = _attention_reason(attempt, event, to_status)
        body = f"{ATTENTION_PHRASES.get(reason, ATTENTION_FALLBACK)} for {company}."
        importance = HIGH
    elif kind == SUBMISSION_COMPLETED:
        body = f"Your application for {role} at {company} was submitted."
    elif kind == VERIFICATION_RESULT:
        reason = to_status
        if to_status == ApplicationStatus.UNCERTAIN.value:
            body = f"Your submission to {company} could not be verified. Please confirm the outcome; CareerOS will not resubmit it."
            importance = HIGH
        else:
            body = f"Your submission to {company} was verified."
    elif kind == EXECUTION_FAILED:
        reason = to_status
        body = f"Execution failed for {role} at {company}."
        importance = HIGH
    elif kind == INTERVIEW_SIGNAL:
        reason = to_status
        body = f"CareerOS detected a possible interview signal for {company}."
    else:  # REJECTION_SIGNAL
        reason = to_status
        body = f"CareerOS detected a possible rejection signal for {company}."

    return Notification(
        key=f"attempt:{attempt.id}:{to_status}:{event.id}",
        kind=kind,
        title=TITLES[kind],
        body=_body(body),
        path=_attempt_path(attempt.id),
        tenant_id=tenant_id,
        occurred_at=event.created_at,
        application_id=attempt.id,
        reason=reason,
        importance=importance,
    )


def _run_notification(run: ExecutionRunRow, attempt: ApplicationRow, opportunity: Optional[OpportunityRow], tenant_id: str) -> Notification:
    return Notification(
        key=f"run:{run.id}:FAILED",
        kind=EXECUTION_FAILED,
        title=TITLES[EXECUTION_FAILED],
        # Never ``run.error_message``: it can quote page text.
        body=_body(f"Execution failed for {_role(opportunity)} at {_company(opportunity)}."),
        path=_attempt_path(attempt.id),
        tenant_id=tenant_id,
        occurred_at=run.finished_at,
        application_id=attempt.id,
        reason=_vocabulary(run.error_class) or "UNKNOWN",
        importance=HIGH,
    )


def _dry_run_notification(run: ExecutionRunRow, attempt: ApplicationRow, opportunity: Optional[OpportunityRow], tenant_id: str) -> Notification:
    return Notification(
        # One per run: a second poll, or a restart with the cursor, never repeats it.
        key=f"run:{run.id}:DRY_RUN",
        kind=DRY_RUN_COMPLETE,
        title=TITLES[DRY_RUN_COMPLETE],
        # Company and role only — never the filled values, the page or its URL.
        body=_body(f"Dry run complete — {_company(opportunity)} — {_role(opportunity)} — Ready for review"),
        path=_attempt_path(attempt.id),
        tenant_id=tenant_id,
        occurred_at=run.finished_at,
        application_id=attempt.id,
        reason=DRY_RUN_STATUS,
        importance=NORMAL,
    )


def _signal_notification(signal: SignalRow, opportunity: Optional[OpportunityRow], tenant_id: str) -> Optional[Notification]:
    category = (signal.category or "").upper()
    kind = SIGNAL_KINDS.get(category)
    if kind is None:
        return None
    known = opportunity is not None and bool((opportunity.company or "").strip())
    company = _company(opportunity)
    if kind == INTERVIEW_SIGNAL:
        body = f"CareerOS detected a possible interview signal for {company}." if known else "CareerOS detected a possible interview signal."
    elif kind == REJECTION_SIGNAL:
        body = f"CareerOS detected a possible rejection signal for {company}." if known else "CareerOS detected a possible rejection signal."
    else:
        body = OTHER_SIGNAL_BODIES[category].format(company=company)
    path = _attempt_path(signal.application_id) if signal.application_id else f"/dashboard/signals/{signal.id}"
    return Notification(
        key=f"signal:{signal.id}:{category}",
        kind=kind,
        title=TITLES[kind],
        body=_body(body),
        path=path,
        tenant_id=tenant_id,
        occurred_at=signal.created_at,
        application_id=signal.application_id,
        reason=category,
        importance=NORMAL,
    )


# ------------------------------------------------------------------ queries


def _attempt_events(db: Session, tenant_id: str, cursor: Cursor):
    query = (
        db.query(ApplicationEventRow, ApplicationRow, OpportunityRow)
        .join(ApplicationRow, ApplicationRow.id == ApplicationEventRow.application_id)
        .outerjoin(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
        .filter(ApplicationRow.tenant_id == tenant_id)
    )
    since = cursor.watermark(APPLICATION_EVENTS)
    if since is not None:
        query = query.filter(ApplicationEventRow.created_at >= since)
    return query.order_by(ApplicationEventRow.created_at.asc(), ApplicationEventRow.id.asc()).limit(QUERY_LIMIT).all()


def _failed_runs(db: Session, tenant_id: str, cursor: Cursor):
    query = (
        db.query(ExecutionRunRow, ApplicationRow, OpportunityRow)
        .join(ApplicationRow, ApplicationRow.id == ExecutionRunRow.application_id)
        .outerjoin(OpportunityRow, OpportunityRow.id == ApplicationRow.opportunity_id)
        .filter(
            ExecutionRunRow.tenant_id == tenant_id,
            ExecutionRunRow.finished_at.isnot(None),
            ExecutionRunRow.handoff_reason.is_(None),
            or_(ExecutionRunRow.status.in_(FAILED_RUN_STATUSES), ExecutionRunRow.outcome.in_(FAILED_RUN_OUTCOMES), ExecutionRunRow.status == DRY_RUN_STATUS),
            # Only failures (and dry runs) the attempt survived: a failure that moved
            # the attempt (FAILED, BLOCKED, NEEDS_REVIEW) is told through its event.
            ApplicationRow.status == ApplicationStatus.READY.value,
            # Defence in depth: the run row already carries the tenant, but the
            # join to ``applications`` is by id alone, so pin the attempt too.
            ApplicationRow.tenant_id == tenant_id,
        )
    )
    since = cursor.watermark(EXECUTION_RUNS)
    if since is not None:
        query = query.filter(ExecutionRunRow.finished_at >= since)
    return query.order_by(ExecutionRunRow.finished_at.asc(), ExecutionRunRow.id.asc()).limit(QUERY_LIMIT).all()


def _signals(db: Session, tenant_id: str, cursor: Cursor):
    query = (
        db.query(SignalRow, OpportunityRow)
        # The attempt is joined for its opportunity only; pinning the tenant in
        # the ON clause means a stray cross-tenant ``application_id`` can never
        # pull another tenant's company name into this tenant's notification.
        .outerjoin(ApplicationRow, and_(ApplicationRow.id == SignalRow.application_id, ApplicationRow.tenant_id == tenant_id))
        .outerjoin(OpportunityRow, OpportunityRow.id == func.coalesce(SignalRow.opportunity_id, ApplicationRow.opportunity_id))
        .filter(
            SignalRow.tenant_id == tenant_id,
            SignalRow.category.in_(tuple(SIGNAL_KINDS)),
            SignalRow.source != "PLAYWRIGHT",
        )
    )
    since = cursor.watermark(SIGNALS)
    if since is not None:
        query = query.filter(SignalRow.created_at >= since)
    return query.order_by(SignalRow.created_at.asc(), SignalRow.id.asc()).limit(QUERY_LIMIT).all()


# ------------------------------------------------------------------- public


def _collapse(found: list[Notification]) -> list[Notification]:
    """Within one poll, (kind, application_id) keeps only its latest row."""
    latest: dict[tuple[str, str], Notification] = {}
    loose: list[Notification] = []
    for note in found:
        if note.application_id is None:
            loose.append(note)
            continue
        key = (note.kind, note.application_id)
        current = latest.get(key)
        if current is None or note.occurred_at >= current.occurred_at:
            latest[key] = note
    out = list(latest.values()) + loose
    out.sort(key=lambda n: (n.occurred_at, n.key))
    return out


def derive_events(db: Session, tenant_id: str, cursor: Cursor, now: Optional[datetime] = None) -> tuple[list[Notification], Cursor]:
    """What has happened for ``tenant_id`` since ``cursor``, and the new cursor.

    Read-only. ``now`` is accepted for testability; it is only used as the
    timestamp of last resort for a row whose own timestamp is missing.
    """
    now = now or utc_now()
    moved = cursor.copy()
    found: list[Notification] = []

    for event, attempt, opportunity in _attempt_events(db, tenant_id, cursor):
        if not cursor.is_new(APPLICATION_EVENTS, event.created_at, event.id):
            continue
        moved.advance(APPLICATION_EVENTS, event.created_at or now, event.id)
        note = _event_notification(event, attempt, opportunity, tenant_id)
        if note is not None:
            found.append(note)

    for run, attempt, opportunity in _failed_runs(db, tenant_id, cursor):
        if not cursor.is_new(EXECUTION_RUNS, run.finished_at, run.id):
            continue
        moved.advance(EXECUTION_RUNS, run.finished_at or now, run.id)
        if run.status == DRY_RUN_STATUS:
            found.append(_dry_run_notification(run, attempt, opportunity, tenant_id))
        else:
            found.append(_run_notification(run, attempt, opportunity, tenant_id))

    for signal, opportunity in _signals(db, tenant_id, cursor):
        if not cursor.is_new(SIGNALS, signal.created_at, signal.id):
            continue
        moved.advance(SIGNALS, signal.created_at or now, signal.id)
        note = _signal_notification(signal, opportunity, tenant_id)
        if note is not None:
            found.append(note)

    events = _collapse(found)
    if events:
        logger.debug("desktop notifications derived: %s", {kind: sum(1 for e in events if e.kind == kind) for kind in {e.kind for e in events}})
    return events, moved
