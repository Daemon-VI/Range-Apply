"""Desktop control center pages (Increment 3; UI redesign 2026-09-14).

Thin, read-mostly views over the existing repositories and services: every
number comes from the same tables the API and dashboards use, every write goes
through the existing JSON routes (cookie + desktop header, see
``static/desktop.js``). No business rule lives here.

The redesign reads the workflow as Discover -> Decide -> Prepare -> Apply ->
Learn. Everything a page shows is real state or an honest "not yet": the
pre-flight checklist on the review page, the plain-language "why this job",
the application groups. Heavy lists are paginated and enriched with batched
queries (one query per related table, never one per row); role relevance and
geography are computed only for the rows on the current page.
"""

import functools
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_tenant_id
from app.api.routes.diagnostics import diagnostics as ops_diagnostics
from app.application.database.models import ApplicationRow
from app.career.models import grade_for
from app.config import settings
from app.core.errors import CareerOSError, NotFoundError
from app.core.timeutils import age as time_age
from app.core.timeutils import db_now
from app.database import get_db
from app.desktop.notifications import model as notification_model
from app.desktop.notifications.center import get_center
from app.documents.database.models import DocumentArtifactRow
from app.documents.models import DocumentFormat
from app.documents.service import DocumentService
from app.execution import submission_mode
from app.execution.database.models import ExecutionRunRow, FormFieldRow, FormSnapshotRow
from app.execution.forms import EMPLOYER_SPECIFIC_CATEGORIES
from app.execution.models import ExecutionStatus
from app.execution.service import ExecutionService
from app.jobs.database.models import JobRow
from app.models.enums import VerificationStatus
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.models import EligibilityDecision, FitBand, OpportunityState
from app.pipeline.queue import QueueRepository
from app.pipeline.repository import OpportunityRepository
from app.preparation.database.models import ApplicationPreparationRow, PreparationAnswerRow
from app.preparation.service import PreparationService
from app.signals.database.models import ApplicationOutcomeRow, SignalRow
from app.signals.service import SignalInboxService

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/desktop", tags=["desktop"])
ACTOR = "desktop"

# --------------------------------------------------------------------------- #
# Application workflow groups
# --------------------------------------------------------------------------- #

#: Attempt statuses before an application is ready (the scheduler is preparing it).
EARLY_STATUSES = ["DISCOVERED", "QUALIFIED", "SHORTLISTED", "PREPARING"]
#: Preparation statuses that mean "a person has to answer something".
PREP_NEEDS_PERSON = ["NEEDS_USER_INPUT", "NEEDS_REVIEW"]

#: The Applications screen, in workflow order. A group is derived from the
#: attempt status and, for attempts still being prepared, the preparation's
#: status: a PREPARING attempt whose package waits for your answers needs you.
APPLICATION_GROUPS: list[tuple[str, str, str]] = [
    ("NEEDS_INPUT", "Needs your input", "CareerOS stopped because it will not invent an answer about you."),
    ("READY", "Ready", "Prepared and validated. Review, dry run, then decide."),
    ("IN_PROGRESS", "In progress", "Being prepared by the scheduler, or being submitted right now."),
    ("ATTENTION", "Attention", "Blocked, failed, or an outcome only you can confirm."),
    ("SUBMITTED", "Submitted", "Sent to the employer. Signals and outcomes follow."),
    ("COMPLETED", "Completed", "Cancelled, rejected or closed."),
]
GROUP_KEYS = [g for g, _, _ in APPLICATION_GROUPS]
GROUP_STATUSES = {
    "READY": ["READY", "AWAITING_APPROVAL"],
    "ATTENTION": ["BLOCKED", "NEEDS_REVIEW", "UNCERTAIN", "FAILED"],
    "SUBMITTED": ["SUBMITTED", "VERIFIED", "INTERVIEWING"],
    "COMPLETED": ["REJECTED", "CLOSED", "CANCELLED"],
}
#: Pre-redesign group names (old links, bookmarks) keep working as plain status filters.
LEGACY_GROUPS: dict[str, list[str]] = {
    "PREPARE": EARLY_STATUSES,
    "NEEDS_REVIEW": ["NEEDS_REVIEW", "NEEDS_USER_INPUT", "BLOCKED"],
    "SUBMIT": ["SUBMITTING"],
    "VERIFICATION_REQUIRED": ["UNCERTAIN"],
    "FAILED": ["FAILED"],
    "CANCELLED": ["CANCELLED"],
}
APPLICATIONS_PAGE_SIZE = 50
APPLICATIONS_PER_GROUP = 20

STATUS_BADGE = {
    "READY": "green", "AWAITING_APPROVAL": "green", "VERIFIED": "green", "SUBMITTED": "blue", "SUBMITTING": "blue", "INTERVIEWING": "blue",
    "UNCERTAIN": "purple", "BLOCKED": "orange", "NEEDS_USER_INPUT": "orange", "NEEDS_REVIEW": "orange",
    "FAILED": "red", "REJECTED": "muted", "CANCELLED": "muted", "CLOSED": "muted", "PREPARING": "blue",
    "DISCOVERED": "muted", "QUALIFIED": "muted", "SHORTLISTED": "muted",
}
GROUP_BADGE = {"NEEDS_INPUT": "orange", "READY": "green", "IN_PROGRESS": "blue", "ATTENTION": "red", "SUBMITTED": "blue", "COMPLETED": "muted"}

#: Badge colour per notification kind (Increment 5) — the same vocabulary the
#: rest of the desktop uses, so a toast and this list read the same way.
NOTIFICATION_BADGE = {
    notification_model.APPLICATION_READY: "green",
    notification_model.SUBMISSION_COMPLETED: "blue",
    notification_model.VERIFICATION_RESULT: "green",
    notification_model.ATTENTION_REQUIRED: "orange",
    notification_model.EXECUTION_FAILED: "red",
    notification_model.INTERVIEW_SIGNAL: "purple",
    notification_model.REJECTION_SIGNAL: "red",
    notification_model.OTHER_SIGNAL: "muted",
    notification_model.DRY_RUN_COMPLETE: "green",
}

GRADE_BADGE = {"CONFIRMED": "green", "APPROXIMATE": "blue", "UNVERIFIED": "orange", "NEEDS_REVIEW": "orange", "REMOVED": "red"}

#: What happened / why you / what you can do — one entry per handoff reason.
HANDOFF_TEXT: dict[str, dict[str, str]] = {
    "CAPTCHA_REQUIRED": {"what": "CareerOS reached a bot-protection challenge on the employer's page.", "why": "No automatic bypass was attempted and none will be: only a person may complete it.", "do": "Open the page in your browser, complete the challenge and submit, then confirm the outcome here."},
    "AUTH_REQUIRED": {"what": "The employer's page asks for a login before the application form.", "why": "CareerOS never stores or enters your credentials.", "do": "Sign in yourself in your browser, then retry the attempt."},
    "MFA_REQUIRED": {"what": "The page asks for a one-time or two-factor code.", "why": "Codes are never automated.", "do": "Complete the step in your browser, then retry the attempt."},
    "UNSUPPORTED_FORM": {"what": "The form uses controls CareerOS cannot fill safely.", "why": "Filling custom widgets blindly could submit wrong data.", "do": "Apply through the page by hand using the prepared package, then confirm the outcome here."},
    "AMBIGUOUS_FORM": {"what": "No application form could be identified on the page.", "why": "The posting may be closed, redirected or rendered in a way discovery does not recognise.", "do": "Open the page; apply by hand or retry after checking the link."},
    "UNKNOWN_REQUIRED_FIELD": {"what": "A required field has no safe answer.", "why": "CareerOS never invents an answer to a required question.", "do": "Answer the field below; it is saved to your answer bank when you ask."},
    "USER_CONFIRMATION_REQUIRED": {"what": "This attempt needs your explicit go-ahead.", "why": "The lane or the policy requires review before submission.", "do": "Review the package, then approve or cancel."},
    "ARTIFACT_FILE_REQUIRED": {"what": "A required upload has no intact local document.", "why": "Only a verified rendered document is ever uploaded.", "do": "Regenerate the document from the Documents screen, then retry."},
}

#: Attention-center buckets, in the order a person should work through them.
ATTENTION_BUCKETS: list[tuple[str, str, str]] = [
    ("needs_you", "Needs you", "Answers or decisions only you can give."),
    ("blocked", "Blocked", "The run stopped and handed the rest to you."),
    ("failed", "Failed", "Not retried automatically."),
    ("uncertain", "Uncertain", "Submit may have been pressed; only you can confirm what happened."),
]


# ------------------------------------------------------------------ helpers


def humanize(code: Optional[str]) -> str:
    """``NEEDS_USER_INPUT`` -> ``Needs user input``; ``data_engineering`` -> ``Data engineering``."""
    text = (code or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else ""


templates.env.filters["humanize"] = humanize


def _mode(db: Session, tenant_id: str) -> dict[str, Any]:
    from app.application.killswitch import is_paused

    try:
        paused = bool(is_paused(db, None))
    except Exception:  # noqa: BLE001 - never break a page over a flag
        paused = False
    # The banner shows the server-side SAFE / LIVE switch, not a config default:
    # it is exactly what the run route and the pre-submit gate enforce.
    live = submission_mode.is_live_enabled(tenant_id)
    return {"dry_run": not live, "live_enabled": live, "kill_switch": paused}


def _base_context(request: Request, db: Session, tenant_id: str, nav: str, **extra) -> dict[str, Any]:
    ctx = {"request": request, "nav": nav, "execution_mode": _mode(db, tenant_id), "notice": request.query_params.get("notice"), "attention_count": _attention_count(db, tenant_id), "notification_count": _notification_count(tenant_id)}
    ctx.update(extra)
    return ctx


def _notification_count(tenant_id: str) -> int:
    """Unseen local notifications for the sidebar badge (in-memory, no query)."""
    try:
        return get_center().unseen_count(tenant_id)
    except Exception:  # noqa: BLE001 - never break a page over the badge
        return 0


def _attention_count(db: Session, tenant_id: str) -> int:
    try:
        attempts = db.query(func.count(ApplicationRow.id)).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(["BLOCKED", "NEEDS_USER_INPUT", "NEEDS_REVIEW", "UNCERTAIN", "FAILED"])).scalar() or 0
        closed, open_ = _prep_attempt_subqueries(db, tenant_id)
        preps = db.query(func.count(ApplicationPreparationRow.id)).filter(ApplicationPreparationRow.tenant_id == tenant_id, ApplicationPreparationRow.status.in_(PREP_NEEDS_PERSON), or_(ApplicationPreparationRow.id.notin_(closed), ApplicationPreparationRow.id.in_(open_))).scalar() or 0
        signals = db.query(func.count(SignalRow.id)).filter(SignalRow.tenant_id == tenant_id, SignalRow.status.in_(["NEEDS_REVIEW", "UNMATCHED"])).scalar() or 0
        return attempts + preps + signals
    except Exception:  # noqa: BLE001
        return 0


def _prep_attempt_subqueries(db: Session, tenant_id: str):
    """Preparation ids with a completed (cancelled/closed/rejected) attempt, and with an open one.

    A package that waits for answers only needs you while an application can
    still use it: one whose every attempt is completed is shown as stale, not
    counted as attention.
    """
    base = db.query(ApplicationRow.preparation_id).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.preparation_id.isnot(None))
    closed = base.filter(ApplicationRow.status.in_(GROUP_STATUSES["COMPLETED"])).subquery()
    open_ = base.filter(ApplicationRow.status.notin_(GROUP_STATUSES["COMPLETED"])).subquery()
    return closed.select(), open_.select()


def _freshness(job: Optional[JobRow]) -> str:
    if job is None:
        return "—"
    delta = time_age(job.posted_at or job.first_seen_at)
    if delta is None:
        return "unknown"
    return "today" if delta.days <= 0 else f"{delta.days}d"


def _by_id(db: Session, model, ids) -> dict[str, Any]:
    """One ``IN`` query for a set of primary keys (batched enrichment, never per row)."""
    wanted = list({i for i in ids if i})
    return {r.id: r for r in db.query(model).filter(model.id.in_(wanted)).all()} if wanted else {}


def _jobs(db: Session, rows: list[CandidateOpportunityRow]) -> dict[str, JobRow]:
    return _by_id(db, JobRow, [co.opportunity.canonical_job_id for co in rows if co.opportunity is not None])


def _attempts_by_co(db: Session, tenant_id: str, co_ids: list[str]) -> dict[str, ApplicationRow]:
    if not co_ids:
        return {}
    rows = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.candidate_opportunity_id.in_(co_ids)).order_by(ApplicationRow.created_at.asc()).all()
    return {r.candidate_opportunity_id: r for r in rows}


def _evidence_view(nodes: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    """Evidence nodes with the machine-readable grade the UI must show."""
    out = []
    for key in keys or []:
        node = nodes.get(key)
        if node is None:
            out.append({"key": key, "label": key, "grade": "UNVERIFIED", "verification_status": None, "kind": None, "resolved": False})
            continue
        try:
            grade = grade_for(VerificationStatus(node.verification_status), removed=(node.status == "REMOVED")).value
        except ValueError:
            grade = "UNVERIFIED"
        out.append({"key": key, "label": node.label, "grade": grade, "verification_status": node.verification_status, "kind": node.kind, "resolved": True, "claim": getattr(node, "claim", None), "source_type": getattr(node, "source_type", None)})
    return out


def _services(db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)):
    return db, tenant_id


def _candidate_policies(db: Session, tenant_id: str) -> tuple[Any, Any]:
    """The candidate's preferences and geography policy from ONE profile query."""
    try:
        from app.career.database.models import CandidateProfileRow
        from app.career.read_model import geography_policy_from_row, preferences_from_row

        row = db.query(CandidateProfileRow).filter(CandidateProfileRow.tenant_id == tenant_id).first()
        return (preferences_from_row(row) if row is not None else None), geography_policy_from_row(row)
    except Exception as exc:  # noqa: BLE001 - explanations never break a page
        logger.info("candidate policies unavailable: %s", type(exc).__name__)
        return None, None


RELEVANCE_VIEW = {
    "RELEVANT": ("Relevant", "green"), "ADJACENT": ("Adjacent", "blue"), "UNKNOWN": ("Unclear", "purple"),
    "WEAK": ("Weak match", "orange"), "UNRELATED": ("Unrelated", "orange"), "NOT_ASSESSED": ("Not assessed", "muted"),
}
GEO_VIEW = {
    "PRIMARY": ("Preferred location", "green"), "SECONDARY": ("Also considered", "blue"), "INTERNATIONAL": ("International", "blue"),
    "UNCONFIRMED": ("Location unconfirmed", "purple"), "EXCLUDED": ("Outside your locations", "orange"), "NO_TARGET": ("No location target", "muted"),
}
ELIGIBILITY_BADGE = {"ELIGIBLE": "green", "LIKELY": "green", "UNCERTAIN": "purple", "REVIEW": "orange", "INELIGIBLE": "red"}
FIT_BADGE = {"HIGH": "green", "MEDIUM": "blue", "LOW": "muted"}


def _relevance(job: Optional[JobRow], preferences: Any) -> Optional[dict[str, Any]]:
    if job is None:
        return None
    try:
        from app.intelligence.services.role_relevance import relevance_for_row

        assessment = relevance_for_row(job, preferences)
    except Exception as exc:  # noqa: BLE001
        logger.info("relevance unavailable: %s", type(exc).__name__)
        return None
    label, tone = RELEVANCE_VIEW.get(assessment.relevance.value, (humanize(assessment.relevance.value), "muted"))
    family = humanize(assessment.role.family)
    return {"code": assessment.relevance.value, "label": label, "tone": tone, "family": family, "detail": assessment.detail, "in_policy": assessment.in_policy}


def _geography(job: Optional[JobRow], policy: Any) -> Optional[dict[str, Any]]:
    if job is None or policy is None:
        return None
    try:
        assessment = policy.assess_job(job.location, job.locations, job.metadata_)
    except Exception as exc:  # noqa: BLE001
        logger.info("geography unavailable: %s", type(exc).__name__)
        return None
    label, tone = GEO_VIEW.get(assessment.tier.value, (humanize(assessment.tier.value), "muted"))
    return {"tier": assessment.tier.value, "label": label, "tone": tone, "detail": assessment.detail, "in_policy": assessment.in_policy}


_ADMISSION_TEXT = {
    "ineligible": "Not eligible",
    "below_minimum_eligibility": "Eligibility below your minimum",
    "band_disabled": "Fit band switched off in your policy",
    "below_fit_threshold": "Fit below your threshold",
    "company_blocked": "Company is on your block list",
    "cooldown_active": "Company cool-down is active",
    "duplicate_application": "Already applied to this role",
    "duplicate_opportunity": "Duplicate of another opportunity",
    "opportunity_closed": "Posting is closed",
    "not_scored": "Not scored yet",
    "not_evaluated": "Not evaluated yet",
}


def admission_text(co: CandidateOpportunityRow) -> str:
    """The admission decision in plain language (the raw reason stays under Details)."""
    reason = (co.policy_reason or "").strip()
    if not reason:
        return "Not evaluated yet" if co.policy_admitted is None else ("Admitted" if co.policy_admitted else "Not admitted")
    head, _, rest = reason.partition(":")
    head = head.lower()
    if head == "admitted":
        parts = [p for p in rest.split(":") if p]
        return "Admitted" + (f" ({', '.join(humanize(p).lower() for p in parts)})" if parts else "")
    if head == "outside_target_geography":
        return "Location not confirmed in your target area" if "UNCONFIRMED" in rest else "Outside your target locations"
    if head == "irrelevant_role":
        family = rest.split("/", 1)[1] if "/" in rest else ""
        return "Role outside your target roles" + (f" ({humanize(family).lower()})" if family else "")
    return _ADMISSION_TEXT.get(head, humanize(head))


def _reason_sentence(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("reason") or item.get("constraint") or "").strip()
    return str(item or "").strip()


def why_items(co: CandidateOpportunityRow, decision: Any, geography: Optional[dict], relevance: Optional[dict]) -> list[dict[str, str]]:
    """'Why this job?' as short human sentences, each with a text status (never colour only)."""
    items: list[dict[str, str]] = []
    if geography is not None:
        if geography["tier"] == "PRIMARY":
            items.append({"tone": "green", "tag": "Match", "text": f"Location matches your preference — {geography['detail']}"})
        elif geography["tier"] == "NO_TARGET":
            items.append({"tone": "muted", "tag": "Not set", "text": "No target location is set in your Career Brain, so location was not checked."})
        else:
            tag = {"SECONDARY": "Match", "INTERNATIONAL": "Allowed", "UNCONFIRMED": "Unclear", "EXCLUDED": "Outside"}.get(geography["tier"], "Location")
            items.append({"tone": geography["tone"], "tag": tag, "text": f"{geography['label']} — {geography['detail']}"})
    if relevance is not None:
        if relevance["code"] == "RELEVANT":
            items.append({"tone": "green", "tag": "Match", "text": f"Role matches your target roles ({relevance['family']})."})
        elif relevance["code"] == "NOT_ASSESSED":
            items.append({"tone": "muted", "tag": "Not set", "text": "No target roles are recorded, so role relevance was not assessed."})
        else:
            items.append({"tone": relevance["tone"], "tag": relevance["label"], "text": f"Role reads as {relevance['family'].lower()}: {relevance['detail']}."})
    status = co.eligibility_status
    if status:
        label = {"ELIGIBLE": "You meet the stated eligibility requirements.", "LIKELY": "You likely meet the eligibility requirements.", "UNCERTAIN": "Eligibility is uncertain — some requirements could not be confirmed.", "INELIGIBLE": "A stated requirement rules this role out.", "REVIEW": "Eligibility needs a person to review it."}.get(status, humanize(status))
        items.append({"tone": ELIGIBILITY_BADGE.get(status, "muted"), "tag": humanize(status), "text": label})
    if decision is not None:
        for c in decision.failed_constraints or []:
            if _reason_sentence(c):
                items.append({"tone": "red", "tag": "Fails", "text": _reason_sentence(c)})
        for c in decision.uncertain_constraints or []:
            if _reason_sentence(c):
                items.append({"tone": "purple", "tag": "Unclear", "text": _reason_sentence(c)})
        for c in decision.matched_constraints or []:
            sentence = _reason_sentence(c)
            if sentence and not (geography is not None and sentence.lower().startswith("job location")):
                items.append({"tone": "green", "tag": "Meets", "text": sentence})
    if co.fit_band:
        items.append({"tone": FIT_BADGE.get(co.fit_band, "muted"), "tag": f"{co.fit_band} fit", "text": f"Fit score {co.fit_score if co.fit_score is not None else '—'} of 100 against your Career Brain evidence."})
    admitted = admission_text(co)
    items.append({"tone": "green" if co.policy_admitted else ("orange" if co.policy_admitted is False else "muted"), "tag": "Admitted" if co.policy_admitted else ("Not admitted" if co.policy_admitted is False else "Pending"), "text": f"Application policy: {admitted}."})
    return items


_EMAIL = re.compile(r"^([^@\s]{1,3})[^@\s]*(@.+)$")


def mask_value(value: Optional[str], field_type: Optional[str] = None, label: Optional[str] = None) -> tuple[str, bool]:
    """Abbreviate an email or phone number for display. Returns (display, masked?)."""
    text = (value or "").strip()
    if not text:
        return "", False
    kind = (field_type or "").lower()
    name = (label or "").lower()
    if kind == "email" or "email" in name or ("@" in text and _EMAIL.match(text)):
        match = _EMAIL.match(text)
        if match:
            return f"{match.group(1)}•••{match.group(2)}", True
    if kind in ("phone", "tel") or any(word in name for word in ("phone", "mobile", "contact number")):
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 6:
            return f"•••• {digits[-3:]}", True
    return text, False


# --------------------------------------------------------------------- home


def _application_group_counts(db: Session, tenant_id: str) -> dict[str, int]:
    """Workflow group counts from one aggregate query (attempt status x preparation status)."""
    counts = {g: 0 for g in GROUP_KEYS}
    rows = (
        db.query(ApplicationRow.status, ApplicationPreparationRow.status, func.count(ApplicationRow.id))
        .outerjoin(ApplicationPreparationRow, ApplicationPreparationRow.id == ApplicationRow.preparation_id)
        .filter(ApplicationRow.tenant_id == tenant_id)
        .group_by(ApplicationRow.status, ApplicationPreparationRow.status)
        .all()
    )
    for status, prep_status, count in rows:
        counts[group_of(status, prep_status)] += count
    return counts


def group_of(status: str, prep_status: Optional[str] = None) -> str:
    if status == "NEEDS_USER_INPUT" or (status in EARLY_STATUSES and prep_status in PREP_NEEDS_PERSON):
        return "NEEDS_INPUT"
    for group, statuses in GROUP_STATUSES.items():
        if status in statuses:
            return group
    return "IN_PROGRESS"


def _group_filter(group: str):
    """SQL filter for one workflow group (the query must outer-join the preparation)."""
    prep = ApplicationPreparationRow.status
    needs_person = prep.in_(PREP_NEEDS_PERSON)
    if group == "NEEDS_INPUT":
        return or_(ApplicationRow.status == "NEEDS_USER_INPUT", and_(ApplicationRow.status.in_(EARLY_STATUSES), needs_person))
    if group == "IN_PROGRESS":
        known = GROUP_STATUSES["READY"] + GROUP_STATUSES["ATTENTION"] + GROUP_STATUSES["SUBMITTED"] + GROUP_STATUSES["COMPLETED"] + ["NEEDS_USER_INPUT"]
        return and_(ApplicationRow.status.notin_(known), or_(ApplicationRow.status.notin_(EARLY_STATUSES), prep.is_(None), prep.notin_(PREP_NEEDS_PERSON)))
    if group in GROUP_STATUSES:
        return ApplicationRow.status.in_(GROUP_STATUSES[group])
    if group in LEGACY_GROUPS:
        return ApplicationRow.status.in_(LEGACY_GROUPS[group])
    return None


@router.get("/", response_class=HTMLResponse)
def home(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    repo = OpportunityRepository(db, tenant_id)
    by_state = repo.counts_by_state()
    co_base = db.query(func.count(CandidateOpportunityRow.id)).filter(CandidateOpportunityRow.tenant_id == tenant_id)
    eligibility = dict(db.query(CandidateOpportunityRow.eligibility_status, func.count(CandidateOpportunityRow.id)).filter(CandidateOpportunityRow.tenant_id == tenant_id).group_by(CandidateOpportunityRow.eligibility_status).all())
    bands = {b.value: 0 for b in FitBand}
    for band, count in db.query(CandidateOpportunityRow.fit_band, func.count()).filter(CandidateOpportunityRow.tenant_id == tenant_id).group_by(CandidateOpportunityRow.fit_band).all():
        if band in bands:
            bands[band] = count
    admitted = co_base.filter(CandidateOpportunityRow.policy_admitted.is_(True)).scalar() or 0
    new_24h = co_base.filter(CandidateOpportunityRow.created_at >= db_now() - timedelta(hours=24)).scalar() or 0
    attempts = dict(db.query(ApplicationRow.status, func.count(ApplicationRow.id)).filter(ApplicationRow.tenant_id == tenant_id).group_by(ApplicationRow.status).all())
    groups = _application_group_counts(db, tenant_id)
    total_opportunities = sum(by_state.values())
    eligible = eligibility.get("ELIGIBLE", 0) + eligibility.get("LIKELY", 0)
    preparing = sum(attempts.get(s, 0) for s in EARLY_STATUSES)
    pipeline = [
        {"key": "discovered", "label": "Discovered", "value": total_opportunities, "sub": f"{new_24h} added in 24 h", "href": "/desktop/opportunities"},
        {"key": "eligible", "label": "Eligible", "value": eligible, "sub": f"{eligibility.get('UNCERTAIN', 0)} uncertain", "href": "/desktop/opportunities?eligibility=ELIGIBLE"},
        {"key": "admitted", "label": "Relevant / admitted", "value": admitted, "sub": "passed your policy", "href": "/desktop/opportunities?admitted=true"},
        {"key": "preparing", "label": "Preparing", "value": preparing, "sub": f"{groups['NEEDS_INPUT']} need your input", "href": "/desktop/applications?group=IN_PROGRESS"},
        {"key": "ready", "label": "Ready", "value": groups["READY"], "sub": "prepared and validated", "href": "/desktop/applications?group=READY"},
        {"key": "submitted", "label": "Submitted", "value": groups["SUBMITTED"], "sub": f"{attempts.get('VERIFIED', 0)} verified", "href": "/desktop/applications?group=SUBMITTED"},
    ]
    ready_rows = []
    if groups["READY"]:
        ready_rows = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(GROUP_STATUSES["READY"])).order_by(ApplicationRow.updated_at.desc()).limit(2).all()
    primary = _primary_action(groups, ready_rows, db)
    signals = SignalInboxService(db, tenant_id, actor=ACTOR)
    recent_signals = signals.list_signals(limit=6)[0]
    recent_attempts = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(["SUBMITTED", "VERIFIED", "UNCERTAIN", "INTERVIEWING"])).order_by(ApplicationRow.updated_at.desc()).limit(6).all()
    opps = _by_id(db, OpportunityRow, [a.opportunity_id for a in recent_attempts] + [s.opportunity_id for s in recent_signals])
    shell = getattr(request.app.state, "desktop", None)
    shell_status = shell.status() if shell is not None else None
    try:
        health = ops_diagnostics(db, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diagnostics unavailable: %s", type(exc).__name__)
        health = {"warnings": [f"diagnostics unavailable: {type(exc).__name__}"]}
    attention_preview = _attention_items(db, tenant_id, limit=5)
    mode = _mode(db, tenant_id)
    try:
        journey = _journey(db, tenant_id, groups, admitted, total_opportunities, preparing, shell_status, mode)
    except Exception as exc:  # noqa: BLE001 - the rest of Home still renders
        logger.warning("journey unavailable: %s", type(exc).__name__, exc_info=True)
        journey = None
    return templates.TemplateResponse(request=request, name="home.html", context=_base_context(
        request, db, tenant_id, "home", journey=journey,
        opportunities_total=total_opportunities, eligible=eligible, bands=bands, by_state=by_state, admitted=admitted, new_24h=new_24h,
        attempts=attempts, groups=groups, pipeline=pipeline, primary=primary,
        ready=groups["READY"], attention=groups["ATTENTION"], submitted=groups["SUBMITTED"], failures=attempts.get("FAILED", 0),
        queue=QueueRepository(db, tenant_id).counts_by_state(), recent_attempts=recent_attempts, recent_signals=recent_signals, opps=opps,
        shell=shell_status, health=health, browser_available=_browser_available(), attention_preview=attention_preview,
    ))


#: Standard screening answers every application needs from the person (answer-bank categories).
STANDARD_ANSWERS = (
    ("sponsorship", "Visa sponsorship answer"),
    ("notice_period", "Notice period"),
    ("salary", "Expected salary"),
    ("relocation", "Relocation answer"),
    # Real forms (2026-09-22): asked on most applications, never guessed.
    ("current_employer", "Current employer (or 'Student')"),
    ("referral_source", "How you heard about the job"),
    ("consent", "Standard acknowledgements (answer 'Yes' to let CareerOS tick required privacy / acknowledgement boxes)"),
)
PROFILE_FIELDS = (("email", "Email"), ("phone", "Phone"), ("location", "Your location"), ("work_authorization", "Work authorization"))

JOURNEY_BADGE = {"done": ("green", "Done"), "needs_you": ("orange", "Needs you"), "active": ("blue", "Running"), "waiting": ("muted", "Waiting"), "paused": ("orange", "Paused"), "off": ("muted", "Off")}


def _clock(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(epoch).strftime("%H:%M") if epoch else None


def _journey(db: Session, tenant_id: str, groups: dict[str, int], admitted: int, total_opportunities: int, preparing: int, shell_status: Optional[dict], mode: dict) -> dict[str, Any]:
    """The guided path from set-up to submission, derived from live rows only (read-only)."""
    from app.career.database.models import AnswerBankEntryRow, CandidateProfileRow
    from app.career.read_model import geography_policy_from_row, preferences_from_row
    from app.jobs.database.models import DiscoveryRunRow, SourceHealthRow

    steps: list[dict[str, Any]] = []

    # 1. profile and the standard answers CareerOS will never guess
    profile = db.query(CandidateProfileRow).filter(CandidateProfileRow.tenant_id == tenant_id).first()
    missing: list[str] = []
    if profile is None:
        missing.append("Import or create your profile")
    else:
        missing += [label for field, label in PROFILE_FIELDS if not (getattr(profile, field, None) or "").strip()]
        approved = {c for (c,) in db.query(AnswerBankEntryRow.category).filter(AnswerBankEntryRow.tenant_id == tenant_id, AnswerBankEntryRow.status == "APPROVED").distinct().all()}
        missing += [label for category, label in STANDARD_ANSWERS if category not in approved]
        try:
            if not preferences_from_row(profile).all_target_roles:
                missing.append("Target roles")
            if not geography_policy_from_row(profile).active:
                missing.append("Job location preference")
        except Exception:  # noqa: BLE001 - a malformed preference is shown as missing, never a broken page
            missing.append("Job preferences")
    steps.append({"key": "profile", "title": "Your profile and standard answers", "state": "needs_you" if missing else "done",
                  "detail": "CareerOS never guesses these; every application reuses them." if missing else "Everything applications need from you is on record.",
                  "missing": missing, "actions": [{"label": "Complete in Career Brain" if missing else "Open Career Brain", "href": "/dashboard/profile/", "cls": "primary" if missing else "secondary"}]})

    # 2. finding jobs (autopilot)
    pilot = (shell_status or {}).get("autopilot") or {"state": "disabled"}
    totals = pilot.get("totals") or {}
    boards = db.query(func.count(SourceHealthRow.id)).filter(SourceHealthRow.enabled.is_(True)).scalar() or 0
    last_discovery = db.query(func.max(DiscoveryRunRow.completed_at)).scalar()
    seen = f"{boards} job boards tracked · {total_opportunities} opportunities found" + (f" · last checked {last_discovery.strftime('%d %b %H:%M')}" if last_discovery else "")
    actions: list[dict[str, Any]] = [{"label": "Browse opportunities", "href": "/desktop/opportunities"}]
    if pilot.get("state") == "running":
        paused = bool(totals.get("paused"))
        running_step = totals.get("running_step")
        state = "paused" if paused else "active"
        when = _clock(totals.get("next_cycle_at"))
        status_line = f"Autopilot is {'paused' if paused else 'on'}" + (f" — working: {running_step}" if running_step else (f" — next check at {when}" if when and not paused else ""))
        actions = [{"label": "Run now", "url": "/desktop/api/autopilot", "body": {"action": "run_now"}, "done": "Autopilot started a cycle", "cls": "primary"},
                   {"label": "Resume" if paused else "Pause", "url": "/desktop/api/autopilot", "body": {"action": "resume" if paused else "pause"}, "done": "Autopilot resumed" if paused else "Autopilot paused"}] + actions
    else:
        state = "off"
        status_line = "Autopilot is off — start CareerOS from its desktop shortcut to find, match and prepare jobs automatically"
    steps.append({"key": "find", "title": "Find jobs", "state": state, "detail": f"{status_line}. {seen}.", "missing": [], "actions": actions})

    # 3. matching and preparation
    freshness = _match_freshness(db, tenant_id)
    stale = bool(freshness.get("available") and freshness.get("stale") and not freshness.get("running"))
    match_actions: list[dict[str, Any]] = [{"label": "See admitted roles", "href": "/desktop/opportunities?reason=admitted"}]
    if stale:
        match_actions.insert(0, {"label": "Re-run matching", "url": "/api/v3/matches/recalculate", "body": {}, "done": "Matching started — this takes a few minutes"})
    steps.append({"key": "match", "title": "Match and prepare", "state": "waiting" if stale or not admitted else "done",
                  "detail": f"{admitted} relevant roles admitted by your policy · {preparing} being prepared." + (" Some decisions predate the current rules." if stale else ""),
                  "missing": [], "actions": match_actions})

    # 4. questions only the person can answer
    needs = groups.get("NEEDS_INPUT", 0)
    steps.append({"key": "answer", "title": "Answer open questions", "state": "needs_you" if needs else "done",
                  "detail": f"{needs} application{'s' if needs != 1 else ''} stopped instead of guessing. Answers are saved and reused." if needs else "No application is waiting for an answer.",
                  "missing": [], "actions": [{"label": "Answer questions", "href": "/desktop/applications?group=NEEDS_INPUT", "cls": "primary"}] if needs else []})

    # 5. review and dry run
    ready = groups.get("READY", 0)
    steps.append({"key": "review", "title": "Review and dry run", "state": "active" if ready else "waiting",
                  "detail": f"{ready} application{'s' if ready != 1 else ''} ready: check the pre-flight list and watch a dry run (submit is never pressed)." if ready else "Prepared applications appear here for review.",
                  "missing": [], "actions": [{"label": "Review ready applications", "href": "/desktop/applications?group=READY", "cls": "primary"}] if ready else []})

    # 6. submit — always the person
    submitted = groups.get("SUBMITTED", 0)
    live = bool(mode.get("live_enabled"))
    steps.append({"key": "submit", "title": "Submit", "state": "done" if submitted else "waiting",
                  "detail": ("LIVE SUBMISSION ENABLED. " if live else "SAFE / DRY RUN is on, so nothing can be submitted. ") + "A real submission is one application at a time: enable live submission, open its confirmation screen and type SUBMIT." + (f" {submitted} submitted so far." if submitted else ""),
                  "missing": [], "actions": [{"label": "Track submissions", "href": "/desktop/applications?group=SUBMITTED"}] if submitted else []})

    for step in steps:
        step["badge"], step["status"] = JOURNEY_BADGE[step["state"]]
    current = next((s["key"] for s in steps if s["state"] == "needs_you"), None) or ("review" if ready else "find")
    return {"steps": steps, "current": current, "done": sum(1 for s in steps if s["state"] == "done")}


def _primary_action(groups: dict[str, int], ready_rows: list[ApplicationRow], db: Session) -> dict[str, str]:
    """The ONE thing to do next, from real counts."""
    if groups["NEEDS_INPUT"]:
        n = groups["NEEDS_INPUT"]
        return {"tone": "warn", "title": f"{n} application{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your input", "text": "CareerOS stopped instead of guessing. Answer the open questions and they are reused wherever they apply.", "label": "Review applications", "href": "/desktop/applications?group=NEEDS_INPUT"}
    if groups["READY"]:
        n = groups["READY"]
        if n == 1 and ready_rows:
            opp = db.get(OpportunityRow, ready_rows[0].opportunity_id) if ready_rows[0].opportunity_id else None
            where = f" — {opp.title} at {opp.company}" if opp is not None else ""
            return {"tone": "info", "title": f"1 application ready{where}", "text": "Review the pre-flight checklist and run a dry run. Nothing is submitted until you type SUBMIT.", "label": "Review & Dry Run", "href": f"/desktop/applications/{ready_rows[0].id}"}
        return {"tone": "info", "title": f"{n} applications ready", "text": "Review each one and run a dry run. Nothing is submitted until you type SUBMIT.", "label": "Review & Dry Run", "href": "/desktop/applications?group=READY"}
    if groups["ATTENTION"]:
        n = groups["ATTENTION"]
        return {"tone": "warn", "title": f"{n} application{'s' if n != 1 else ''} need{'s' if n == 1 else ''} attention", "text": "Blocked, failed or uncertain attempts are waiting for a decision.", "label": "Open attention center", "href": "/desktop/attention"}
    return {"tone": "ok", "title": "You're all caught up.", "text": "Nothing needs you right now. New opportunities are discovered and prepared in the background.", "label": "Browse opportunities", "href": "/desktop/opportunities"}


def _browser_available() -> bool:
    try:
        from app.execution.playwright import is_available

        return bool(is_available())
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------ opportunities

OPPORTUNITY_REASONS = [
    ("admitted", "Admitted"),
    ("outside_location", "Outside your locations"),
    ("location_unconfirmed", "Location unconfirmed"),
    ("unrelated_role", "Role outside your targets"),
    ("ineligible", "Not eligible"),
    ("cooldown", "Company cool-down"),
]


def _int_param(params, name: str, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(params.get(name) or default), low), high)
    except (TypeError, ValueError):
        return default


def _opportunity_rows(db: Session, tenant_id: str, params) -> dict[str, Any]:
    state, fit_band, eligibility, admitted, min_priority, search = (params.get("state") or ""), (params.get("fit_band") or ""), (params.get("eligibility") or ""), (params.get("admitted") or ""), (params.get("min_priority") or ""), (params.get("q") or "").strip()
    reason, location = (params.get("reason") or ""), (params.get("location") or "").strip()
    limit = _int_param(params, "limit", 50, 1, 200)
    page = _int_param(params, "page", 1, 1, 100000)
    query = db.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.tenant_id == tenant_id)
    if search or location:
        query = query.join(OpportunityRow, OpportunityRow.id == CandidateOpportunityRow.opportunity_id)
    if search:
        needle = f"%{search}%"
        query = query.filter(OpportunityRow.company.ilike(needle) | OpportunityRow.title.ilike(needle))
    if location:
        query = query.outerjoin(JobRow, JobRow.id == OpportunityRow.canonical_job_id).filter(JobRow.location.ilike(f"%{location}%") | OpportunityRow.location_bucket.ilike(f"%{location}%"))
    if state in [s.value for s in OpportunityState]:
        query = query.filter(CandidateOpportunityRow.state == state)
    if fit_band in [b.value for b in FitBand]:
        query = query.filter(CandidateOpportunityRow.fit_band == fit_band)
    if eligibility in [d.value for d in EligibilityDecision]:
        query = query.filter(CandidateOpportunityRow.eligibility_status == eligibility)
    if admitted.lower() in ("true", "false"):
        query = query.filter(CandidateOpportunityRow.policy_admitted.is_(admitted.lower() == "true"))
    if min_priority.isdigit():
        query = query.filter(CandidateOpportunityRow.priority_score >= int(min_priority))
    reason_filters = {
        "admitted": CandidateOpportunityRow.policy_admitted.is_(True),
        "outside_location": CandidateOpportunityRow.policy_reason.like("outside_target_geography:EXCLUDED%"),
        "location_unconfirmed": CandidateOpportunityRow.policy_reason.like("outside_target_geography:UNCONFIRMED%"),
        "unrelated_role": CandidateOpportunityRow.policy_reason.like("irrelevant_role:%"),
        "ineligible": CandidateOpportunityRow.eligibility_status == "INELIGIBLE",
        "cooldown": CandidateOpportunityRow.scheduler_code == "COOLDOWN_ACTIVE",
    }
    if reason in reason_filters:
        query = query.filter(reason_filters[reason])
    total = query.count()
    pages = max(1, -(-total // limit))
    page = min(page, pages)
    rows = (
        query.options(selectinload(CandidateOpportunityRow.opportunity))
        .order_by(CandidateOpportunityRow.priority_score.desc().nulls_last(), CandidateOpportunityRow.fit_score.desc().nulls_last(), CandidateOpportunityRow.created_at.desc())
        .offset((page - 1) * limit).limit(limit).all()
    )
    jobs = _jobs(db, rows)
    attempts = _attempts_by_co(db, tenant_id, [co.id for co in rows])
    preferences, policy = _candidate_policies(db, tenant_id) if rows else (None, None)
    attempt_preps = _by_id(db, ApplicationPreparationRow, [a.preparation_id for a in attempts.values()])
    items = []
    for co in rows:
        job = jobs.get(co.opportunity.canonical_job_id or "") if co.opportunity is not None else None
        attempt = attempts.get(co.id)
        prep = attempt_preps.get(attempt.preparation_id or "") if attempt is not None else None
        items.append({
            "co": co, "opp": co.opportunity, "job": job, "freshness": _freshness(job), "attempt": attempt,
            "url": (job.application_url or job.source_url) if job else None,
            "relevance": _relevance(job, preferences), "geography": _geography(job, policy),
            "admission": admission_text(co), "next": _opportunity_next(co, attempt, prep.status if prep is not None else None),
        })
    filters = {"state": state, "fit_band": fit_band, "eligibility": eligibility, "admitted": admitted, "min_priority": min_priority, "q": search, "reason": reason, "location": location, "limit": limit}
    active = {k: v for k, v in filters.items() if v and k != "limit"}
    base_query = {k: v for k, v in filters.items() if v and not (k == "limit" and v == 50)}
    return {
        "items": items, "total": total, "filters": filters, "active_filters": active, "page": page, "pages": pages, "limit": limit,
        "first_index": (page - 1) * limit + 1 if total else 0, "last_index": (page - 1) * limit + len(items),
        "prev_query": urlencode({**base_query, "page": page - 1}) if page > 1 else None,
        "next_query": urlencode({**base_query, "page": page + 1}) if page < pages else None,
    }


def _opportunity_next(co: CandidateOpportunityRow, attempt: Optional[ApplicationRow], prep_status: Optional[str] = None) -> dict[str, Any]:
    if attempt is not None:
        group = group_of(attempt.status, prep_status)
        label = {"NEEDS_INPUT": "Answer questions", "READY": "Review & dry run", "ATTENTION": "Resolve", "SUBMITTED": "Track outcome", "COMPLETED": "View history"}.get(group, "View application")
        anchor = "#questions" if group == "NEEDS_INPUT" else ""
        return {"label": label, "href": f"/desktop/applications/{attempt.id}{anchor}", "primary": group in ("NEEDS_INPUT", "READY", "ATTENTION")}
    if co.policy_admitted:
        return {"label": "Waiting for the scheduler to prepare it", "href": None, "primary": False}
    if co.policy_admitted is False:
        return {"label": "No action — not admitted", "href": None, "primary": False}
    return {"label": "Waiting for evaluation", "href": None, "primary": False}


def _match_freshness(db: Session, tenant_id: str) -> dict[str, Any]:
    """Whether stored decisions predate the current matching / gate rules (read-only).

    A rule fix reaches an existing opportunity only when matching is re-run
    (the existing ``POST /api/v3/matches/recalculate``, which re-syncs the
    opportunities). Applications already created are never changed by it.
    """
    from app.intelligence.database.models import MatchRunRow
    from app.intelligence.services.fit_scoring_engine import ENGINE_VERSION
    from app.pipeline.gates import GATE_RULESET_VERSION

    try:
        from app.intelligence.services.match_persistence import match_run_is_stale

        newest = db.query(MatchRunRow).filter(MatchRunRow.tenant_id == tenant_id).order_by(MatchRunRow.started_at.desc()).first()
        # In progress only while plausibly alive; an abandoned RUNNING row is ignored.
        in_progress = newest is not None and (newest.status or "").upper() == "RUNNING" and not match_run_is_stale(newest)
        last = db.query(MatchRunRow).filter(MatchRunRow.tenant_id == tenant_id, MatchRunRow.status.in_(("COMPLETED", "PARTIAL"))).order_by(MatchRunRow.started_at.desc()).first()
        outdated = db.query(func.count(CandidateOpportunityRow.id)).filter(
            CandidateOpportunityRow.tenant_id == tenant_id,
            CandidateOpportunityRow.gate_ruleset_version.isnot(None),
            CandidateOpportunityRow.gate_ruleset_version != GATE_RULESET_VERSION,
        ).scalar() or 0
    except Exception:  # noqa: BLE001 - a page never breaks over a freshness hint
        logger.warning("match freshness unavailable for tenant %s", tenant_id)
        return {"available": False}
    running = in_progress
    engine_outdated = last is not None and last.engine_version != ENGINE_VERSION
    return {
        "available": True, "last_run": last, "running": running, "engine_version": ENGINE_VERSION, "ruleset_version": GATE_RULESET_VERSION,
        "outdated_decisions": int(outdated), "engine_outdated": engine_outdated, "stale": bool(outdated) or engine_outdated or last is None,
    }


@router.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    data = _opportunity_rows(db, tenant_id, request.query_params)
    return templates.TemplateResponse(request=request, name="opportunities.html", context=_base_context(
        request, db, tenant_id, "opportunities", **data, freshness=_match_freshness(db, tenant_id),
        states=[s.value for s in OpportunityState], bands=[b.value for b in FitBand], decisions=[d.value for d in EligibilityDecision],
        reasons=OPPORTUNITY_REASONS, counts=OpportunityRepository(db, tenant_id).counts_by_state(),
    ))


@router.get("/partials/opportunities", response_class=HTMLResponse)
def opportunities_partial(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    data = _opportunity_rows(db, tenant_id, request.query_params)
    return templates.TemplateResponse(request=request, name="_opportunity_rows.html", context={"request": request, **data})


@router.get("/opportunities/{co_id}", response_class=HTMLResponse)
def opportunity_detail(request: Request, co_id: str, deps=Depends(_services)):
    db, tenant_id = deps
    repo = OpportunityRepository(db, tenant_id)
    co = repo.get_candidate_opportunity(co_id)
    if co is None:
        raise NotFoundError("candidate opportunity not found")
    attempt = _attempts_by_co(db, tenant_id, [co.id]).get(co.id)
    if attempt is not None:
        return RedirectResponse(url=f"/desktop/applications/{attempt.id}", status_code=303)
    return _render_review(request, db, tenant_id, co, None)


# ------------------------------------------------------------- applications


def _application_needs(attempt: ApplicationRow, prep: Optional[ApplicationPreparationRow], run: Optional[ExecutionRunRow], pending: int, outcome: Optional[ApplicationOutcomeRow], group: str) -> str:
    """'What CareerOS needs' in one sentence, from real state."""
    if group == "NEEDS_INPUT":
        if pending:
            return f"{pending} answer{'s' if pending != 1 else ''} from you"
        return (attempt.status_reason or "").strip() or "Your review of the prepared package"
    if group == "READY":
        if run is not None and run.status == "DRY_RUN":
            return "Your decision — dry run complete, submit was not pressed"
        return "A dry run, then your decision"
    if group == "IN_PROGRESS":
        if attempt.status == "SUBMITTING":
            return "Nothing — submitting now; watch the browser window"
        return "Nothing yet — the scheduler is preparing the package"
    if group == "ATTENTION":
        if attempt.status == "UNCERTAIN":
            return "Confirm whether the employer received it"
        if attempt.status == "FAILED":
            return (run.error_message if run is not None and run.error_message else attempt.status_reason) or "A decision: retry or cancel"
        text = HANDOFF_TEXT.get(attempt.blocked_reason or "")
        if text is not None:
            return text["do"]
        return (attempt.status_reason or "").strip() or "A decision: retry or cancel"
    if group == "SUBMITTED":
        if outcome is not None and outcome.current_outcome:
            return f"Nothing — current outcome: {humanize(outcome.current_outcome).lower()}"
        return "Nothing — waiting for the employer"
    return "Nothing"


def _application_items(db: Session, tenant_id: str, rows: list[ApplicationRow]) -> list[dict[str, Any]]:
    """Enrich attempts with one query per related table."""
    if not rows:
        return []
    cos = _by_id(db, CandidateOpportunityRow, [r.candidate_opportunity_id for r in rows])
    opps = _by_id(db, OpportunityRow, [r.opportunity_id for r in rows])
    jobs = _by_id(db, JobRow, [r.job_id for r in rows])
    preps = _by_id(db, ApplicationPreparationRow, [r.preparation_id for r in rows])
    runs = _by_id(db, ExecutionRunRow, [r.last_execution_id for r in rows])
    outcomes = {o.application_id: o for o in db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == tenant_id, ApplicationOutcomeRow.application_id.in_([r.id for r in rows])).all()}
    pending = dict(
        db.query(PreparationAnswerRow.preparation_id, func.count(PreparationAnswerRow.id))
        .filter(PreparationAnswerRow.tenant_id == tenant_id, PreparationAnswerRow.preparation_id.in_(list(preps)), PreparationAnswerRow.status.in_(PREP_NEEDS_PERSON))
        .group_by(PreparationAnswerRow.preparation_id).all()
    ) if preps else {}
    items = []
    for row in rows:
        prep = preps.get(row.preparation_id or "")
        run = runs.get(row.last_execution_id or "")
        group = group_of(row.status, prep.status if prep is not None else None)
        job = jobs.get(row.job_id or "")
        opp = opps.get(row.opportunity_id)
        outcome = outcomes.get(row.id)
        n_pending = pending.get(prep.id, 0) if prep is not None else 0
        items.append({
            "attempt": row, "group": group, "co": cos.get(row.candidate_opportunity_id or ""), "opp": opp, "job": job, "prep": prep, "run": run, "outcome": outcome,
            "badge": STATUS_BADGE.get(row.status, "muted"), "location": (job.location if job is not None and job.location else (opp.location_bucket if opp is not None else "")) or "",
            "pending": n_pending, "needs": _application_needs(row, prep, run, n_pending, outcome, group),
            "can_retry": row.status in ("FAILED", "BLOCKED", "NEEDS_REVIEW"),
            "can_cancel": row.status not in ("CANCELLED", "VERIFIED", "SUBMITTED", "CLOSED", "REJECTED"),
            "can_dry_run": row.status == "READY" and prep is not None,
            "dry_run_done": run is not None and run.status == "DRY_RUN",
        })
    return items


def _application_query(db: Session, tenant_id: str, group: str):
    query = db.query(ApplicationRow).outerjoin(ApplicationPreparationRow, ApplicationPreparationRow.id == ApplicationRow.preparation_id).filter(ApplicationRow.tenant_id == tenant_id)
    condition = _group_filter(group) if group else None
    return query.filter(condition) if condition is not None else query


def _application_rows(db: Session, tenant_id: str, params) -> dict[str, Any]:
    group = params.get("group") or ""
    if group not in GROUP_KEYS and group not in LEGACY_GROUPS:
        group = ""
    counts = _application_group_counts(db, tenant_id)
    meta = {g: {"label": label, "help": help_text} for g, label, help_text in APPLICATION_GROUPS}
    if group:
        query = _application_query(db, tenant_id, group)
        total = query.count()
        page = _int_param(params, "page", 1, 1, 100000)
        pages = max(1, -(-total // APPLICATIONS_PAGE_SIZE))
        page = min(page, pages)
        rows = query.order_by(ApplicationRow.updated_at.desc()).offset((page - 1) * APPLICATIONS_PAGE_SIZE).limit(APPLICATIONS_PAGE_SIZE).all()
        items = _application_items(db, tenant_id, rows)
        sections = [{"key": group, "label": meta.get(group, {}).get("label", humanize(group)), "help": meta.get(group, {}).get("help", ""), "items": items, "total": total, "collapsed": False}]
        paging = {"page": page, "pages": pages, "prev": f"group={group}&page={page - 1}" if page > 1 else None, "next": f"group={group}&page={page + 1}" if page < pages else None}
    else:
        rows_by_group: dict[str, list[ApplicationRow]] = {}
        for key in GROUP_KEYS:
            if counts.get(key):
                rows_by_group[key] = _application_query(db, tenant_id, key).order_by(ApplicationRow.updated_at.desc()).limit(APPLICATIONS_PER_GROUP).all()
        enriched = {i["attempt"].id: i for i in _application_items(db, tenant_id, [r for rows in rows_by_group.values() for r in rows])}
        sections = [
            {"key": key, "label": meta[key]["label"], "help": meta[key]["help"], "items": [enriched[r.id] for r in rows_by_group.get(key, [])], "total": counts.get(key, 0), "collapsed": key == "COMPLETED"}
            for key in GROUP_KEYS if counts.get(key)
        ]
        items = [i for s in sections for i in s["items"]]
        total = sum(counts.values())
        paging = None
    return {"items": items, "sections": sections, "total": total, "group": group, "groups": GROUP_KEYS, "group_meta": meta, "counts": counts, "paging": paging, "group_badge": GROUP_BADGE, "legacy": group in LEGACY_GROUPS}


@router.get("/applications", response_class=HTMLResponse)
def applications(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    return templates.TemplateResponse(request=request, name="applications.html", context=_base_context(request, db, tenant_id, "applications", **_application_rows(db, tenant_id, request.query_params)))


@router.get("/partials/applications", response_class=HTMLResponse)
def applications_partial(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    return templates.TemplateResponse(request=request, name="_application_rows.html", context={"request": request, **_application_rows(db, tenant_id, request.query_params)})


# ------------------------------------------------------------------- review


def preflight_checks(co: CandidateOpportunityRow, prep: Optional[ApplicationPreparationRow], documents: list, artifacts: list, fields: list, missing_answers: list, missing_fields: list, runs: list, attempt: Optional[ApplicationRow]) -> list[dict[str, str]]:
    """The pre-flight checklist. Every state is derived from stored state; unknown reads 'Not yet'."""
    checks: list[dict[str, str]] = []

    def add(name: str, state: str, detail: str) -> None:
        label = {"pass": "Yes", "fail": "No", "warn": "Needs you", "pending": "Not yet"}[state]
        checks.append({"name": name, "state": state, "label": label, "detail": detail})

    status = co.eligibility_status
    if status in ("ELIGIBLE", "LIKELY"):
        add("Eligible", "pass", f"Eligibility decision: {humanize(status)}.")
    elif status in ("UNCERTAIN", "REVIEW"):
        checks.append({"name": "Eligible", "state": "warn", "label": "Uncertain", "detail": "Some requirements could not be confirmed."})
    elif status:
        add("Eligible", "fail", "A stated requirement rules this role out.")
    else:
        add("Eligible", "pending", "Eligibility has not been evaluated.")

    if prep is None:
        add("Prepared", "pending", "No preparation exists yet.")
    elif prep.status == "READY":
        add("Prepared", "pass", f"Preparation v{prep.version} is ready.")
    elif prep.status in PREP_NEEDS_PERSON:
        add("Prepared", "warn", f"Preparation v{prep.version} waits for your answers.")
    else:
        add("Prepared", "pending", f"Preparation v{prep.version} is {humanize(prep.status).lower()}.")

    active = [d for d in documents if d.status == "ACTIVE"]
    needed = {a.artifact_type for a in artifacts} or ({"RESUME"} if prep is not None else set())
    if prep is None:
        add("Documents validated", "pending", "No documents until the package is prepared.")
    elif needed and all(any(d.artifact_type == t and d.validation_status == "PASSED" for d in active) for t in needed):
        add("Documents validated", "pass", f"{len(active)} rendered document{'s' if len(active) != 1 else ''} passed validation.")
    elif any(d.validation_status not in ("PASSED",) for d in active):
        add("Documents validated", "fail", "A rendered document did not pass validation.")
    elif prep.validation_status == "PASSED":
        add("Documents validated", "pending", "Package content validated; documents render when a run starts.")
    else:
        add("Documents validated", "fail" if prep.validation_status == "FAILED" else "pending", f"Package validation {humanize(prep.validation_status).lower()}.")

    required = [f for f in fields if f.required]
    if not fields:
        add("Required fields mapped", "pending", "No application form has been captured yet — a dry run maps it.")
    elif missing_fields:
        add("Required fields mapped", "warn", f"{len(missing_fields)} field{'s' if len(missing_fields) != 1 else ''} need{'s' if len(missing_fields) == 1 else ''} your answer.")
    else:
        mapped = sum(1 for f in required if f.status == "ANSWERED")
        add("Required fields mapped", "pass", f"{mapped} of {len(required)} required field{'s' if len(required) != 1 else ''} mapped.")

    open_questions = len(missing_answers) + len(missing_fields)
    if prep is None and not fields:
        add("No unresolved questions", "pending", "Nothing to check until the package is prepared.")
    elif open_questions:
        add("No unresolved questions", "warn", f"{open_questions} question{'s' if open_questions != 1 else ''} still open.")
    else:
        add("No unresolved questions", "pass", "Every prepared question has an answer.")

    latest = runs[0] if runs else None
    if latest is None:
        add("CAPTCHA not encountered", "pending", "No run has opened the page yet.")
    elif latest.handoff_reason == "CAPTCHA_REQUIRED" or any(r.handoff_reason == "CAPTCHA_REQUIRED" for r in runs[:3]):
        add("CAPTCHA not encountered", "fail", "A bot-protection challenge stopped a run.")
    elif (latest.diagnostics or {}).get("captcha_invisible"):
        checks.append({"name": "CAPTCHA not encountered", "state": "warn", "label": "Invisible CAPTCHA", "detail": "The page carries an invisible CAPTCHA; the last run was not challenged."})
    else:
        add("CAPTCHA not encountered", "pass", "The last run met no challenge.")

    dry = next((r for r in runs if r.status == "DRY_RUN"), None)
    if latest is not None and latest.status != "DRY_RUN" and dry is not None:
        # Integration fix (2026-09-14): an older DRY_RUN kept this check green after a newer run
        # handed off (the real Notion run found three required questions the first run never saw).
        reason = latest.handoff_reason or latest.error_class or latest.status
        add("Dry run complete", "warn", f"The latest run (#{latest.run_number}) stopped: {humanize(reason)}. The earlier dry run (#{dry.run_number}) no longer reflects this form.")
    elif dry is not None:
        when = dry.finished_at.strftime("%Y-%m-%d %H:%M") if dry.finished_at else "recorded"
        filled = (dry.diagnostics or {}).get("fields_filled")
        add("Dry run complete", "pass", f"Run #{dry.run_number}, {when}" + (f" — {filled} field(s) filled, submit not pressed." if filled is not None else ", submit not pressed."))
    elif attempt is None:
        add("Dry run complete", "pending", "No application attempt exists yet.")
    else:
        add("Dry run complete", "pending", "No dry run has been recorded.")
    return checks


def _field_rows(fields: list[FormFieldRow]) -> list[dict[str, Any]]:
    rows = []
    for f in fields:
        raw = f.answer or (", ".join(f.selected_values or []))
        display, masked = mask_value(raw, f.field_type, f.label)
        rows.append({"field": f, "value": raw, "display": display, "masked": masked})
    return rows


def _render_review(request: Request, db: Session, tenant_id: str, co: CandidateOpportunityRow, attempt: Optional[ApplicationRow]):
    repo = OpportunityRepository(db, tenant_id)
    execution = ExecutionService(db, tenant_id, actor=ACTOR)
    preparation_service = PreparationService(db, tenant_id, actor=ACTOR)
    opp = co.opportunity
    job = db.get(JobRow, opp.canonical_job_id) if opp is not None and opp.canonical_job_id else None
    decision = repo.latest_decision(co.id)
    priority = repo.latest_priority(co.id)
    prep = None
    if attempt is not None and attempt.preparation_id:
        prep = db.get(ApplicationPreparationRow, attempt.preparation_id)
    if prep is None:
        prep = db.query(ApplicationPreparationRow).filter(ApplicationPreparationRow.tenant_id == tenant_id, ApplicationPreparationRow.candidate_opportunity_id == co.id).order_by(ApplicationPreparationRow.version.desc()).first()
    artifacts = [a for a in (prep.artifacts if prep is not None else [])]
    answers = list(prep.answers) if prep is not None else []
    all_keys = list(prep.evidence_keys or []) if prep is not None else []
    for answer in answers:
        all_keys.extend(k for k in (answer.evidence_keys or []) if k not in all_keys)
    nodes = preparation_service.evidence_repo.get_nodes_by_keys(all_keys, include_removed=True) if all_keys else {}
    grades = {e["key"]: e["grade"] for e in _evidence_view(nodes, all_keys)}
    variant = preparation_service.evidence_repo.get_variant(prep.positioning_variant_id) if prep is not None and prep.positioning_variant_id else None
    documents = DocumentService(db, tenant_id, actor=ACTOR).list_for(prep.id) if prep is not None else []
    preview = None
    runs: list[ExecutionRunRow] = []
    fields: list[FormFieldRow] = []
    item = None
    if attempt is not None:
        runs = execution.runs_for(attempt.id)
        item = execution.item_for(attempt)
        snapshot = execution._latest_snapshot(attempt.id)
        if snapshot is not None:
            fields = sorted(snapshot.fields, key=lambda f: f.position)
        if attempt.preparation_id:
            try:
                preview = execution.preview(attempt.id)
            except CareerOSError as exc:
                logger.info("preview unavailable for %s: %s", attempt.id, exc.code)
    missing_answers = [a for a in answers if a.status in PREP_NEEDS_PERSON]
    missing_fields = [f for f in fields if f.status in PREP_NEEDS_PERSON]
    missing_documents = [a.artifact_type for a in artifacts if not any(d.status == "ACTIVE" and d.artifact_type == a.artifact_type for d in documents)]
    needs_input = bool(missing_answers or missing_fields or (prep is not None and prep.status in PREP_NEEDS_PERSON))
    handoff = HANDOFF_TEXT.get(attempt.blocked_reason or "") if attempt is not None else None
    active_run = _active_run(attempt.id) if attempt is not None else None
    can_run = attempt is not None and attempt.status == "READY" and prep is not None
    preferences, policy = _candidate_policies(db, tenant_id)
    geography = _geography(job, policy)
    relevance = _relevance(job, preferences)
    checks = preflight_checks(co, prep, documents, artifacts, fields, missing_answers, missing_fields, runs, attempt)
    profile_fields = [r for r in _field_rows(fields) if r["field"].source == "PROFILE" and r["field"].status == "ANSWERED"]
    return templates.TemplateResponse(request=request, name="review.html", context=_base_context(
        request, db, tenant_id, "applications", active_run=active_run, can_run=can_run,
        co=co, opp=opp, job=job, url=(job.application_url or job.source_url) if job else None, decision=decision, priority=priority,
        attempt=attempt, prep=prep, artifacts=artifacts, answers=answers, documents=documents, preview=preview, runs=runs, fields=fields, field_rows=_field_rows(fields), item=item,
        evidence=_evidence_view(nodes, list(prep.evidence_keys or []) if prep is not None else []), nodes=nodes, grades=grades, variant=variant,
        missing_answers=missing_answers, missing_fields=missing_fields, missing_documents=missing_documents, needs_input=needs_input,
        employer_specific=EMPLOYER_SPECIFIC_CATEGORIES, checks=checks, why=why_items(co, decision, geography, relevance), geography=geography, relevance=relevance,
        admission=admission_text(co), profile_fields=profile_fields, group=group_of(attempt.status, prep.status if prep is not None else None) if attempt is not None else None,
        handoff=handoff, badge=STATUS_BADGE, grade_badge=GRADE_BADGE, eligibility_badge=ELIGIBILITY_BADGE, fit_badge=FIT_BADGE,
    ))


@router.get("/applications/{application_id}", response_class=HTMLResponse)
def application_review(request: Request, application_id: str, deps=Depends(_services)):
    db, tenant_id = deps
    execution = ExecutionService(db, tenant_id, actor=ACTOR)
    attempt = execution.require_attempt(application_id)
    co = db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id) if attempt.candidate_opportunity_id else None
    if co is None or co.tenant_id != tenant_id:
        raise NotFoundError("candidate opportunity not found")
    return _render_review(request, db, tenant_id, co, attempt)


# ------------------------------------------- real submission (Increment 4)


def _runner():
    """The desktop run supervisor, or ``None`` when this build has none.

    The supervisor (``app/desktop/runner.py``) owns the one visible browser
    run; these pages only read it. The import is lazy so a deployment that
    serves the web app without the desktop shell keeps every other page.
    """
    try:
        from app.desktop.runner import get_runner
    except ImportError:  # pragma: no cover - the runner ships with the shell
        return None
    return get_runner()


def _active_run(application_id: str):
    runner = _runner()
    if runner is None:
        return None
    try:
        return runner.active_for(application_id)
    except Exception:  # noqa: BLE001 - a page never breaks over the supervisor
        logger.warning("run supervisor unavailable for %s", application_id)
        return None


def _require_job(job_id: str, tenant_id: str):
    runner = _runner()
    job = runner.get(job_id) if runner is not None else None
    if job is None or job.tenant_id != tenant_id:
        raise NotFoundError(f"Desktop run not found: {job_id}")
    return job


def _mode_param(params) -> str:
    return "live" if (params.get("mode") or "dry_run").strip().lower() == "live" else "dry_run"


def _answer_rows(answers) -> list[dict[str, Any]]:
    """Prepared answers as the confirmation screen shows them (view or row)."""
    rows = []
    for answer in answers or []:
        status = getattr(answer, "status", "") or ""
        rows.append({
            "question": getattr(answer, "question", "") or "",
            "answer": getattr(answer, "answer", None),
            "status": status,
            "label": "ANSWERED" if status == "ANSWERED" else "NEEDS USER INPUT",
            "source": getattr(answer, "source", "") or "",
            "required": bool(getattr(answer, "required", True)),
            "evidence_keys": list(getattr(answer, "evidence_keys", None) or []),
        })
    return rows


def _artifact_row(rendered: dict[str, Any], view, artifact_type: str) -> dict[str, Any]:
    """Exactly what would be uploaded: the rendered document if one exists."""
    doc = rendered.get(artifact_type)
    if doc is not None:
        return {"artifact_type": doc.artifact_type, "version": doc.version, "sha256": doc.content_hash, "format": doc.format, "bytes": doc.byte_size, "document_id": doc.id, "rendered": True, "validation_status": doc.validation_status}
    if view is not None:
        return {"artifact_type": view.artifact_type, "version": None, "sha256": None, "format": None, "bytes": None, "document_id": None, "rendered": False, "validation_status": view.validation_status, "template_version": view.template_version}
    return {}


@router.get("/applications/{application_id}/confirm", response_class=HTMLResponse)
def confirm_run(request: Request, application_id: str, deps=Depends(_services)):
    """The one screen from which a real submission can be started."""
    db, tenant_id = deps
    execution = ExecutionService(db, tenant_id, actor=ACTOR)
    attempt = execution.require_attempt(application_id)
    mode = _mode_param(request.query_params)
    opp = db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
    job = db.get(JobRow, attempt.job_id) if attempt.job_id else None
    prep = db.get(ApplicationPreparationRow, attempt.preparation_id) if attempt.preparation_id else None
    preparation_service = PreparationService(db, tenant_id, actor=ACTOR)
    documents = DocumentService(db, tenant_id, actor=ACTOR).list_for(prep.id) if prep is not None else []
    rendered = {d.artifact_type: d for d in documents if d.status == "ACTIVE"}
    blockers: list[str] = []
    if prep is None:
        blockers.append("This attempt has no preparation attached, so there is nothing to submit.")
    if attempt.status != "READY":
        blockers.append(f"This attempt is {attempt.status}, not READY — only a READY attempt can be run from here.")
    preview = None
    if prep is not None:
        try:
            preview = execution.preview(attempt.id)
        except CareerOSError as exc:
            logger.info("preview unavailable for %s: %s", attempt.id, exc.code)
            blockers.append(exc.message)
    package = preview.package if preview is not None else None
    answers = _answer_rows(package.answers if package is not None else (list(prep.answers) if prep is not None else []))
    keys = list(prep.evidence_keys or []) if prep is not None else []
    for row in answers:
        keys.extend(k for k in row["evidence_keys"] if k not in keys)
    nodes = preparation_service.evidence_repo.get_nodes_by_keys(keys, include_removed=True) if keys else {}
    grades = {e["key"]: e["grade"] for e in _evidence_view(nodes, keys)}
    snapshot = execution._latest_snapshot(attempt.id)
    fields = sorted(snapshot.fields, key=lambda f: f.position) if snapshot is not None else []
    target_url = package.target.canonical_url if package is not None else ((job.application_url or job.source_url) if job is not None else None)
    active = _active_run(attempt.id)
    if active is not None:
        blockers.append("A run for this application is already in progress.")
    return templates.TemplateResponse(request=request, name="confirm_run.html", context=_base_context(
        request, db, tenant_id, "applications",
        attempt=attempt, opp=opp, job=job, prep=prep, preview=preview, package=package, mode=mode,
        answers=answers, grades=grades, fields=fields, field_rows=_field_rows(fields), target_url=target_url, active_run=active,
        resume=_artifact_row(rendered, package.resume if package is not None else None, "RESUME"),
        cover_letter=_artifact_row(rendered, package.cover_letter if package is not None else None, "COVER_LETTER"),
        cover_letter_enabled=bool(package.cover_letter_enabled) if package is not None else bool(prep is not None and prep.cover_letter_mode not in (None, "", "NONE", "DISABLED")),
        blockers=blockers, can_run=not blockers, badge=STATUS_BADGE,
        grade_badge=GRADE_BADGE,
        handoff_wait=settings.playwright_handoff_wait_seconds,
    ))


def _run_headline(job, run, attempt) -> dict[str, str]:
    """The one line the person reads first. Never 'success' without proof."""
    status = run.status if run is not None else None
    outcome = run.outcome if run is not None else ((job.outcome or {}).get("outcome") if job.outcome else None)
    verification = run.verification_status if run is not None else None
    if job.state == "failed":
        return {"code": "RUN_DID_NOT_START", "label": "RUN DID NOT START", "tone": "red", "detail": job.error or "The run could not be started."}
    if job.state == "running":
        return {"code": "RUNNING", "label": "RUNNING", "tone": "blue", "detail": "Watch the browser window. Do not close it."}
    if status == "VERIFIED":
        return {"code": "VERIFIED", "label": "SUBMITTED / VERIFIED", "tone": "green", "detail": "The employer's page confirmed the submission."}
    if attempt.status == "SUBMITTED" and verification == "LIKELY":
        return {"code": "LIKELY", "label": "SUBMITTED / LIKELY", "tone": "green", "detail": "Submit was pressed and the page looked like a confirmation, but nothing confirmed it beyond doubt."}
    if outcome == "UNKNOWN" or attempt.status == "UNCERTAIN" or status == "VERIFICATION_FAILED":
        return {"code": "UNKNOWN", "label": "UNKNOWN / UNCERTAIN", "tone": "orange", "detail": "CareerOS will NOT resubmit this application automatically. Check the employer's confirmation, then confirm or reject below."}
    if outcome == "HANDOFF" or status in ("HANDOFF", "PRECONDITION_FAILED") or attempt.status == "BLOCKED":
        return {"code": "BLOCKED", "label": "BLOCKED", "tone": "orange", "detail": "The run stopped and handed the rest to you."}
    if outcome == "NEEDS_USER_INPUT" or status == "NEEDS_USER_INPUT" or attempt.status == "NEEDS_USER_INPUT":
        why = (attempt.status_reason or "").strip() or "Required questions have no safe answer."
        return {"code": "NEEDS_USER_INPUT", "label": "NEEDS USER INPUT", "tone": "orange", "detail": f"Nothing was submitted and nothing was invented: {why}. Answer them on the review page, then retry."}
    if outcome == "NEEDS_REVIEW" or status == "NEEDS_REVIEW" or attempt.status == "NEEDS_REVIEW":
        return {"code": "NEEDS_REVIEW", "label": "NEEDS REVIEW", "tone": "orange", "detail": "Nothing was submitted; a person must decide what happens next."}
    if outcome == "RETRYABLE_FAILURE" or status == "FAILED_RETRYABLE":
        return {"code": "RETRYABLE", "label": "RETRYABLE FAILURE", "tone": "orange", "detail": "Not retried automatically — use Retry on the review page."}
    if outcome == "PERMANENT_FAILURE" or status in ("FAILED_PERMANENT", "STALE"):
        return {"code": "PERMANENT", "label": "PERMANENT FAILURE", "tone": "red", "detail": "Not retried automatically — use Retry on the review page after fixing the cause."}
    if outcome == "DRY_RUN" or status == "DRY_RUN" or (job.mode == "dry_run" and run is not None):
        return {"code": "DRY_RUN", "label": "DRY RUN COMPLETE", "tone": "green", "detail": "Submit not pressed."}
    return {"code": "FINISHED", "label": f"FINISHED — {outcome or status or 'no run recorded'}", "tone": "muted", "detail": "Nothing was submitted." if run is None else ""}


#: Where a handoff can stop while the submit control is still untouched.
_BEFORE_SUBMIT = ("before_form", "form_mapping", "fill", "upload", "before_submit")


def _submit_pressed(run) -> Optional[bool]:
    """What the page may call "Submit pressed".

    ``submit_invoked`` is recorded *before* the executor is allowed to press
    submit (so a crash can never read as "not submitted"); a handoff that
    stopped before the form or before the click therefore still carries the
    flag. The person is told what actually happened: no click for a run that
    never reached the submit control, unknown (None) when the run is still
    open, and the durable flag everywhere else.
    """
    if run is None:
        return None
    if run.status in ("DRY_RUN", "STALE", "PRECONDITION_FAILED"):
        return False
    if run.status == "HANDOFF" and (run.handoff or {}).get("stopped_at") in _BEFORE_SUBMIT:
        return False
    return bool(run.submit_invoked)


def _run_context(db: Session, tenant_id: str, job) -> dict[str, Any]:
    execution = ExecutionService(db, tenant_id, actor=ACTOR)
    attempt = execution.require_attempt(job.application_id)
    run = execution.get_run(job.run_id) if job.run_id else None
    job_row = db.get(JobRow, attempt.job_id) if attempt.job_id else None
    signal = db.query(SignalRow).filter(SignalRow.tenant_id == tenant_id, SignalRow.execution_run_id == run.id).first() if run is not None else None
    outcome_row = db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == tenant_id, ApplicationOutcomeRow.application_id == attempt.id).first()
    opp = db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
    handoff_reason = (run.handoff_reason if run is not None else None) or attempt.blocked_reason or ""
    diagnostics = (run.diagnostics or {}) if run is not None else {}
    return {
        "job": job, "run": run, "attempt": attempt, "opp": opp, "signal": signal, "outcome_row": outcome_row,
        "headline": _run_headline(job, run, attempt), "handoff_reason": handoff_reason, "handoff": HANDOFF_TEXT.get(handoff_reason),
        "handoff_message": (run.handoff or {}).get("message") if run is not None else None,
        "submit_pressed": _submit_pressed(run),
        "remaining_steps": (run.handoff or {}).get("remaining_steps") or [] if run is not None else [],
        "diagnostics": diagnostics,
        "open_url": (run.application_url if run is not None else None) or (run.source_url if run is not None else None) or ((job_row.application_url or job_row.source_url) if job_row is not None else None),
        "badge": STATUS_BADGE,
    }


@router.get("/applications/{application_id}/run/{job_id}", response_class=HTMLResponse)
def run_page(request: Request, application_id: str, job_id: str, deps=Depends(_services)):
    db, tenant_id = deps
    execution = ExecutionService(db, tenant_id, actor=ACTOR)
    attempt = execution.require_attempt(application_id)
    job = _require_job(job_id, tenant_id)
    if job.application_id != attempt.id:
        raise NotFoundError(f"Desktop run not found: {job_id}")
    return templates.TemplateResponse(request=request, name="run_result.html", context=_base_context(request, db, tenant_id, "applications", **_run_context(db, tenant_id, job)))


@router.get("/partials/run/{job_id}", response_class=HTMLResponse)
def run_status_partial(request: Request, job_id: str, deps=Depends(_services)):
    db, tenant_id = deps
    job = _require_job(job_id, tenant_id)
    return templates.TemplateResponse(request=request, name="_run_status.html", context={"request": request, **_run_context(db, tenant_id, job)})


# ---------------------------------------------------------------- attention


def _latest_open_fields(db: Session, tenant_id: str, application_ids: list[str]) -> dict[str, list[FormFieldRow]]:
    """Open (needs-a-person) fields of each attempt's LATEST form snapshot, in two queries."""
    if not application_ids:
        return {}
    latest: dict[str, tuple[Any, str]] = {}
    for snap_id, app_id, captured in db.query(FormSnapshotRow.id, FormSnapshotRow.application_id, FormSnapshotRow.captured_at).filter(FormSnapshotRow.tenant_id == tenant_id, FormSnapshotRow.application_id.in_(application_ids)).all():
        if app_id not in latest or (captured and latest[app_id][0] and captured > latest[app_id][0]):
            latest[app_id] = (captured, snap_id)
    by_snapshot = {snap_id: app_id for app_id, (_, snap_id) in latest.items()}
    out: dict[str, list[FormFieldRow]] = {}
    if by_snapshot:
        for field in db.query(FormFieldRow).filter(FormFieldRow.snapshot_id.in_(list(by_snapshot)), FormFieldRow.status.in_(PREP_NEEDS_PERSON)).order_by(FormFieldRow.position).all():
            out.setdefault(by_snapshot[field.snapshot_id], []).append(field)
    return out


def _attention_items(db: Session, tenant_id: str, limit: Optional[int] = None, include_stale: bool = False) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    attempt_limit = min(limit, 200) if limit else 200
    attempts = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(["BLOCKED", "NEEDS_USER_INPUT", "NEEDS_REVIEW", "UNCERTAIN", "FAILED"])).order_by(ApplicationRow.updated_at.desc()).limit(attempt_limit).all()
    opps = _by_id(db, OpportunityRow, [a.opportunity_id for a in attempts])
    jobs = _by_id(db, JobRow, [a.job_id for a in attempts])
    runs = _by_id(db, ExecutionRunRow, [a.last_execution_id for a in attempts])
    open_fields = _latest_open_fields(db, tenant_id, [a.id for a in attempts])
    for attempt in attempts:
        opp = opps.get(attempt.opportunity_id)
        job = jobs.get(attempt.job_id or "")
        run = runs.get(attempt.last_execution_id or "")
        reason = attempt.blocked_reason or attempt.status
        text = HANDOFF_TEXT.get(reason)
        if attempt.status == "UNCERTAIN":
            text = {"what": "Submit was pressed but the outcome could not be verified.", "why": "CareerOS never resubmits an uncertain application; only you can confirm what happened.", "do": "Check the employer's confirmation (mail or page), then confirm or reject the submission here."}
        elif attempt.status == "FAILED":
            text = {"what": (run.error_message if run is not None and run.error_message else "Execution failed permanently."), "why": "Automatic retries are exhausted or the failure is not retryable.", "do": "Inspect the execution, then retry or cancel."}
        elif attempt.status == "NEEDS_USER_INPUT" and text is None:
            text = {"what": "A required answer is missing.", "why": "CareerOS never invents facts about you.", "do": "Answer the field below."}
        elif attempt.status == "NEEDS_REVIEW" and text is None:
            text = {"what": attempt.status_reason or "This attempt needs a look before anything else happens.", "why": "The policy or the form raised something a person must judge.", "do": "Review the application, then retry or cancel."}
        elif text is None:
            # BLOCKED by the pre-submit gate: the reason code is not a handoff,
            # so the gate's own sentence (status_reason) is what happened.
            text = {"what": attempt.status_reason or reason, "why": "A person must decide before CareerOS does anything else with this application.", "do": "Review the application, then retry or cancel."}
        bucket = {"BLOCKED": "blocked", "FAILED": "failed", "UNCERTAIN": "uncertain"}.get(attempt.status, "needs_you")
        severity = "red" if attempt.status in ("FAILED", "UNCERTAIN") else "orange"
        items.append({"kind": "attempt", "bucket": bucket, "reason": reason, "status": attempt.status, "attempt": attempt, "opp": opp, "run": run, "url": (job.application_url or job.source_url) if job else (run.application_url if run is not None else None), "text": text, "fields": open_fields.get(attempt.id, []), "severity": severity, "when": attempt.updated_at})
    preps = db.query(ApplicationPreparationRow).filter(ApplicationPreparationRow.tenant_id == tenant_id, ApplicationPreparationRow.status.in_(PREP_NEEDS_PERSON)).order_by(ApplicationPreparationRow.updated_at.desc()).limit(min(limit, 100) if limit else 100).all()
    prep_opps = _by_id(db, OpportunityRow, [p.opportunity_id for p in preps])
    prep_attempts: dict[str, list[ApplicationRow]] = {}
    if preps:
        for row in db.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.preparation_id.in_([p.id for p in preps])).order_by(ApplicationRow.created_at.asc()).all():
            prep_attempts.setdefault(row.preparation_id, []).append(row)
    for prep in preps:
        pending = [a for a in prep.answers if a.status in PREP_NEEDS_PERSON]
        linked = prep_attempts.get(prep.id, [])
        attempt = linked[-1] if linked else None
        stale = bool(linked) and all(a.status in GROUP_STATUSES["COMPLETED"] for a in linked)
        items.append({"kind": "preparation", "bucket": "stale" if stale else "needs_you", "reason": prep.status, "status": prep.status, "prep": prep, "attempt": attempt, "opp": prep_opps.get(prep.opportunity_id), "pending": pending, "severity": "orange", "when": prep.updated_at, "text": {"what": f"The prepared package needs {len(pending)} answer(s)." if pending else "The prepared package needs review.", "why": "Required questions have no truthful answer on file." if pending else "Validation or the lane requires a person.", "do": "Answer in the preparation page; approved answers are reused everywhere."}})
    signals = SignalInboxService(db, tenant_id, actor=ACTOR).review_queue(limit=min(limit, 100) if limit else 100)
    sig_opps = _by_id(db, OpportunityRow, [s.opportunity_id for s in signals])
    for signal in signals:
        items.append({"kind": "signal", "bucket": "needs_you", "reason": signal.status, "status": signal.status, "signal": signal, "opp": sig_opps.get(signal.opportunity_id or ""), "severity": "orange", "when": signal.observed_at, "text": {"what": f"A {signal.category or 'UNKNOWN'} signal ({signal.source}) {'could not be matched to an application' if signal.status == 'UNMATCHED' else 'needs a decision'}: {signal.subject or ''}".strip(), "why": signal.status_reason or "Attribution or classification is ambiguous.", "do": "Link, confirm or ignore it in the Signals inbox."}})
    items.sort(key=lambda i: (0 if i["severity"] == "red" else 1, -(i["when"].timestamp() if i.get("when") else 0)))
    if not include_stale:
        items = [i for i in items if i["bucket"] != "stale"]
    return items[:limit] if limit else items


def _attention_context(db: Session, tenant_id: str) -> dict[str, Any]:
    everything = _attention_items(db, tenant_id, include_stale=True)
    items = [i for i in everything if i["bucket"] != "stale"]
    groups = [{"key": key, "label": label, "help": help_text, "items": [i for i in items if i["bucket"] == key]} for key, label, help_text in ATTENTION_BUCKETS]
    return {"items": items, "stale": [i for i in everything if i["bucket"] == "stale"], "attention_groups": groups, "badge": STATUS_BADGE}


@router.get("/attention", response_class=HTMLResponse)
def attention(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    return templates.TemplateResponse(request=request, name="attention.html", context=_base_context(request, db, tenant_id, "attention", **_attention_context(db, tenant_id)))


@router.get("/partials/attention", response_class=HTMLResponse)
def attention_partial(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    return templates.TemplateResponse(request=request, name="_attention_items.html", context={"request": request, **_attention_context(db, tenant_id)})


# ------------------------------------------------------------ notifications


def _relative(when: Optional[Any]) -> str:
    """A coarse age: 'just now', '3 m ago', '2 h ago', '4 d ago'."""
    delta = time_age(when)
    if delta is None:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} m ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _notification_items(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The tenant's recent notifications, newest first. In-memory, read-only."""
    return [
        {
            "notification": entry.notification,
            "seen": entry.seen,
            "when": entry.notification.occurred_at.strftime("%Y-%m-%d %H:%M"),
            "ago": _relative(entry.notification.occurred_at),
        }
        for entry in get_center().recent_entries(tenant_id, limit)
    ]


def _native_view(request: Request) -> dict[str, Any]:
    """Which notification channel is in use: the running shell's poller when there is one, else what would be selected.

    Guarded on every side: a plain web deployment has no desktop shell at
    all and a missing ``winotify`` must never break a page. The in-app list
    is the always-available channel.
    """
    shell = getattr(request.app.state, "desktop", None)
    supervisor = getattr(shell, "notifications", None)
    notifier = getattr(getattr(supervisor, "_worker", None), "notifier", None)
    polling = bool(getattr(supervisor, "running", False))
    if notifier is None:
        notifier = _fallback_notifier()
    return {"name": str(getattr(notifier, "name", "none")), "available": bool(getattr(notifier, "available", False)), "polling": polling}


@functools.lru_cache(maxsize=1)
def _fallback_notifier() -> Any:
    """What the shell *would* use, selected once per process (not on every page render)."""
    try:
        from app.desktop.notifications.native import select_notifier

        return select_notifier()
    except Exception:  # noqa: BLE001 - never break a page over a toast backend
        return None


@router.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, deps=Depends(_services)):
    """Everything CareerOS told you about in this session. Nothing runs here."""
    db, tenant_id = deps
    items = _notification_items(tenant_id)
    return templates.TemplateResponse(request=request, name="notifications.html", context=_base_context(
        request, db, tenant_id, "notifications", items=items, badge=NOTIFICATION_BADGE, native=_native_view(request),
    ))


@router.get("/partials/notifications", response_class=HTMLResponse)
def notifications_partial(request: Request, deps=Depends(_services)):
    _db, tenant_id = deps
    return templates.TemplateResponse(request=request, name="_notification_items.html", context={"request": request, "items": _notification_items(tenant_id), "badge": NOTIFICATION_BADGE})


# ---------------------------------------------------------------- documents

DOCUMENTS_LIMIT = 300


@router.get("/documents", response_class=HTMLResponse)
def documents(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    preparation_id = request.query_params.get("preparation_id") or ""
    kind = request.query_params.get("type") or ""
    query = db.query(DocumentArtifactRow).filter(DocumentArtifactRow.tenant_id == tenant_id)
    if preparation_id:
        query = query.filter(DocumentArtifactRow.preparation_id == preparation_id)
    counts_query = query
    if kind in ("RESUME", "COVER_LETTER"):
        query = query.filter(DocumentArtifactRow.artifact_type == kind)
    rows = query.order_by(DocumentArtifactRow.created_at.desc()).limit(DOCUMENTS_LIMIT).all()
    total = query.count()
    preps = _by_id(db, ApplicationPreparationRow, [r.preparation_id for r in rows])
    opps = _by_id(db, OpportunityRow, [p.opportunity_id for p in preps.values()])
    items = [{"doc": r, "prep": preps.get(r.preparation_id), "opp": opps.get(preps[r.preparation_id].opportunity_id) if r.preparation_id in preps else None} for r in rows]
    # The library: one entry per application package, newest document first.
    library: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for it in items:
        key = it["doc"].preparation_id
        if key not in index:
            index[key] = {"preparation_id": key, "prep": it["prep"], "opp": it["opp"], "docs": []}
            library.append(index[key])
        index[key]["docs"].append(it["doc"])
    aggregate = dict(((t, s), n) for t, s, n in counts_query.with_entities(DocumentArtifactRow.artifact_type, DocumentArtifactRow.status, func.count(DocumentArtifactRow.id)).group_by(DocumentArtifactRow.artifact_type, DocumentArtifactRow.status).all())
    counts = {
        "resumes": sum(n for (t, _), n in aggregate.items() if t == "RESUME"),
        "cover_letters": sum(n for (t, _), n in aggregate.items() if t == "COVER_LETTER"),
        "active": sum(n for (_, s), n in aggregate.items() if s == "ACTIVE"),
        "invalidated": sum(n for (_, s), n in aggregate.items() if s != "ACTIVE"),
    }
    counts["all"] = counts["resumes"] + counts["cover_letters"]
    return templates.TemplateResponse(request=request, name="documents.html", context=_base_context(request, db, tenant_id, "documents", items=items, library=library, counts=counts, total=total, limit=DOCUMENTS_LIMIT, preparation_id=preparation_id, kind=kind, formats=[f.value for f in DocumentFormat]))


@router.get("/documents/{artifact_id}/file")
def document_file(artifact_id: str, deps=Depends(_services)):
    """Serve one of the candidate's own rendered documents inline (dashboard auth)."""
    db, tenant_id = deps
    service = DocumentService(db, tenant_id, actor=ACTOR)
    row, path = service.materialize(artifact_id, for_upload=False)
    media = "application/pdf" if row.format == "PDF" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    filename = f"{row.artifact_type.lower()}-v{row.version}.{row.format.lower()}"
    return FileResponse(str(path), media_type=media, filename=filename, content_disposition_type="inline", headers={"X-Content-SHA256": row.content_hash})


# ------------------------------------------------------------------- system


@router.get("/system", response_class=HTMLResponse)
def system(request: Request, deps=Depends(_services)):
    db, tenant_id = deps
    try:
        health = ops_diagnostics(db, tenant_id)
    except Exception as exc:  # noqa: BLE001
        health = {"warnings": [f"diagnostics unavailable: {type(exc).__name__}"]}
    shell = getattr(request.app.state, "desktop", None)
    shell_status = shell.status() if shell is not None else None
    from app.database import missing_tables, safe_database_url

    try:
        absent = missing_tables()
        database = {"url": safe_database_url(), "reachable": True, "migrated": not absent, "missing": absent}
    except Exception as exc:  # noqa: BLE001
        database = {"url": safe_database_url(), "reachable": False, "migrated": False, "missing": [], "error": type(exc).__name__}
    queue = QueueRepository(db, tenant_id).counts_by_state()
    running = db.query(func.count(ExecutionRunRow.id)).filter(ExecutionRunRow.tenant_id == tenant_id, ExecutionRunRow.status == ExecutionStatus.RUNNING.value).scalar() or 0
    center = get_center()
    notifications_view = {"unseen": center.unseen_count(tenant_id), "held": center.count(tenant_id), "native": _native_view(request)}
    return templates.TemplateResponse(request=request, name="system.html", context=_base_context(
        request, db, tenant_id, "system", health=health, shell=shell_status, database=database, queue=queue, running=running,
        browser_available=_browser_available(), notifications_view=notifications_view, settings_view={
            "app_env": settings.app_env, "deployment_mode": settings.deployment_mode, "api_key_configured": bool(settings.api_key),
            "playwright_dry_run": settings.playwright_dry_run, "playwright_headless": settings.playwright_headless, "ai_enabled": settings.ai_enabled,
            "execution_lease_seconds": settings.execution_lease_seconds, "execution_max_attempts": settings.execution_max_attempts,
        },
    ))
