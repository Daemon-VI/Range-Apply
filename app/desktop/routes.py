"""The routes the desktop shell needs beside its pages.

* ``GET /desktop/login?token=…`` — consume a single-use launcher token and
  set the dashboard cookie (the API key itself never travels in a URL);
* ``GET /desktop/status`` — server / readiness / worker state, counts and
  flags only, behind the same dashboard authentication as every other page;
* ``POST /desktop/api/attempts/{id}/run`` — Increment 4: one explicit user
  action starts one run of one READY attempt through the existing pipeline;
* ``GET /desktop/api/runs/{job_id}`` — what that run is doing / did;
* ``POST /desktop/api/notifications/seen`` — Increment 5: clear the unseen
  badge for the request's tenant, in memory only;
* ``GET /desktop/open?to=…&n=…`` — Increment 5: where a native toast's click
  lands; a single-use nonce lets it point the signed-in window at one local
  page (no cookie is set, nothing runs).

The run route duplicates no gate: it only refuses what would be pointless or
dangerous to even start (not READY, a run already in progress, a live mode
without the typed confirmation). Every real precondition — preparation,
closure, blocklist, duplicate, cap, kill switch, lease, the pre-submit gate —
is enforced by ``ExecutionService`` where it always was.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.config import settings
from app.core.errors import ConflictError
from app.database import get_db
from app.desktop.notifications.center import get_center
from app.desktop.runner import LiveSubmissionDisabled, get_runner
from app.execution import submission_mode
from app.execution.service import ExecutionService
from app.security import require_api_key, require_dashboard_auth

router = APIRouter(prefix="/desktop", tags=["desktop"])

#: Typed by the person into the confirmation dialog before a real submission.
LIVE_CONFIRMATION = "SUBMIT"


def _shell(request: Request):
    return getattr(request.app.state, "desktop", None)


@router.get("/login", include_in_schema=False)
def desktop_login(request: Request, token: Optional[str] = Query(default=None), next: str = Query(default="/dashboard/")):
    shell = _shell(request)
    if shell is None or not shell.tokens.consume(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired desktop login token")
    if settings.api_key:
        # Same HttpOnly cookie the dashboard sets after a ?key= sign-in
        # (persisted by the request_context middleware in app.main).
        request.state.set_dashboard_cookie = settings.api_key
    # A backslash is a slash to a browser: "/\host" would leave the loopback origin.
    target = next if next.startswith("/") and not next.startswith("//") and "\\" not in next else "/dashboard/"
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    return response


@router.get("/status", dependencies=[Depends(require_dashboard_auth)])
def desktop_status(request: Request) -> dict:
    shell = _shell(request)
    if shell is None:
        return {"server": {"running": True, "desktop": False}, "readiness": None, "health": {}, "worker": {"state": "disabled"}, "notifications": {"state": "disabled"}, "window": False}
    return shell.status()


# --------------------------------------------------------------------- #
# Increment 5: a native toast's click
# --------------------------------------------------------------------- #

OPEN_PREFIXES = ("/desktop/", "/dashboard/")


def _local_page(path: str) -> bool:
    """Only a bare, local desktop or dashboard page — never a host, a query, an API route or a traversal."""
    if not path.startswith(OPEN_PREFIXES) or path.startswith("//"):
        return False
    # ``/desktop/api/…`` and ``/dashboard/api/…`` are JSON endpoints, not pages:
    # a toast's click opens something a person can read, never an API call.
    if "/api/" in path or path.endswith("/api"):
        return False
    # No percent-escapes (security audit 2026-09-14): "/desktop/%2e%2e/..." passes a literal ".." check
    # and is normalised by the browser. Desktop page paths never need escaping.
    return "://" not in path and "?" not in path and "#" not in path and ".." not in path and "\\" not in path and "%" not in path


@router.get("/open", include_in_schema=False)
def desktop_open(request: Request, to: str = Query(default=""), n: Optional[str] = Query(default=None)):
    """Where a Windows toast lands when clicked: navigate the signed-in window to one local page.

    The nonce is single-use and grants exactly this (no cookie is set, no
    key is involved, nothing runs). With a window open, the window itself
    is pointed at the page and this tab just says so; without one (``--no-window``,
    the person's own browser holds the dashboard cookie) it redirects, and
    the page's own dashboard authentication applies.
    """
    shell = _shell(request)
    if shell is None or not shell.open_nonces.consume(n):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid or expired notification link")
    if not _local_page(to):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not a local CareerOS page")
    if getattr(shell, "_open_window", None) is not None:
        from app.desktop import window

        if window.navigate(f"{shell.server.url}{to}"):
            return HTMLResponse("<!doctype html><title>CareerOS</title><p style=\"font-family:system-ui;margin:2em\">Opened in the CareerOS window. You can close this tab.</p>")
    return RedirectResponse(url=to, status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------- #
# Increment 4: user-controlled real submission
# --------------------------------------------------------------------- #


class RunRequest(BaseModel):
    """``dry_run`` unless the person asked for a real submission and typed it."""

    mode: Literal["dry_run", "live"] = "dry_run"
    confirm: str = Field(default="", description="must be exactly 'SUBMIT' when mode is 'live'")


@router.post("/api/attempts/{application_id}/run", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
def desktop_run_attempt(
    application_id: str,
    body: RunRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(get_tenant_id),
) -> dict:
    """Start exactly one execution of one READY attempt. Nothing is batched."""
    if body.mode == "live" and not submission_mode.is_live_enabled(tenant_id):
        # Server-side, before anything else: SAFE mode makes a live run impossible,
        # whatever the page, the headers or the typed confirmation say.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="live submission is not enabled: CareerOS is in SAFE / DRY RUN mode")
    if body.mode == "live" and body.confirm != LIVE_CONFIRMATION:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type SUBMIT to confirm a real submission")
    # Tenant-scoped: another tenant's attempt is simply not found.
    attempt = ExecutionService(db, tenant_id, actor="desktop").require_attempt(application_id)
    if attempt.status != ApplicationStatus.READY.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"attempt is {attempt.status}, not READY")
    runner = get_runner()
    if runner.active_for(application_id) or runner.active():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a desktop run is already in progress")
    try:
        job = runner.start(tenant_id, application_id, body.mode, actor="desktop")
    except LiveSubmissionDisabled as exc:  # switched back to SAFE in between
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc
    except ConflictError as exc:  # lost the race against another click
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    return {
        "job_id": job.id,
        "application_id": application_id,
        "mode": job.mode,
        "state": job.state,
        "job_url": f"/desktop/applications/{application_id}/run/{job.id}",
    }


class SubmissionModeRequest(BaseModel):
    """``live`` switches to LIVE SUBMISSION ENABLED; ``safe`` back to SAFE / DRY RUN."""

    mode: Literal["safe", "live"]


@router.get("/api/submission-mode", dependencies=[Depends(require_dashboard_auth)])
def desktop_submission_mode(tenant_id: str = Depends(get_tenant_id)) -> dict:
    return submission_mode.get_state(tenant_id).as_dict()


@router.post("/api/submission-mode", dependencies=[Depends(require_api_key)])
def desktop_set_submission_mode(body: SubmissionModeRequest, tenant_id: str = Depends(get_tenant_id)) -> dict:
    """The one explicit switch. SAFE is the start-up state; nothing else turns LIVE on.

    Enabling LIVE starts nothing: each real submission still needs its own
    confirmation screen and the typed SUBMIT.
    """
    if body.mode == "live":
        state = submission_mode.enable_live(tenant_id, actor="desktop")
    else:
        state = submission_mode.set_safe(tenant_id, actor="desktop")
    return state.as_dict()


class AutopilotRequest(BaseModel):
    action: Literal["pause", "resume", "run_now"]


def _autopilot(request: Request):
    shell = _shell(request)
    supervisor = getattr(shell, "autopilot", None) if shell is not None else None
    target = getattr(supervisor, "target", None) if supervisor is not None else None
    if target is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="autopilot is not running (start CareerOS with --autopilot)")
    return target


@router.post("/api/autopilot", dependencies=[Depends(require_api_key)])
def desktop_autopilot(body: AutopilotRequest, request: Request) -> dict:
    """Pause, resume or wake the autopilot. It never submits, so none of these can."""
    pilot = _autopilot(request)
    if body.action == "pause":
        pilot.pause()
    elif body.action == "resume":
        pilot.resume()
    else:
        pilot.run_now()
    return {"paused": pilot.paused, "totals": {k: v for k, v in pilot.totals.items() if k != "steps"}}


@router.get("/api/runs/{job_id}", dependencies=[Depends(require_dashboard_auth)])
def desktop_run_job(job_id: str, tenant_id: str = Depends(get_tenant_id)) -> dict:
    job = get_runner().get(job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown desktop run")
    return job.as_dict()


# --------------------------------------------------------------------- #
# Increment 5: local notifications — one write, and it is in-memory only
# --------------------------------------------------------------------- #


class SeenRequest(BaseModel):
    """Which notifications the person has now seen; omitted / null = all."""

    keys: Optional[list[str]] = None


@router.post("/api/notifications/seen", dependencies=[Depends(require_api_key)])
def desktop_notifications_seen(body: Optional[SeenRequest] = None, tenant_id: str = Depends(get_tenant_id)) -> dict:
    """Clear the unseen badge. Touches the in-memory center and nothing else.

    Deliberately takes no database session and reaches no service: a
    notification is informational, so acknowledging one can never start,
    retry, cancel or otherwise change an application. Marking is scoped to
    the request's tenant, so another tenant's entries are invisible here.
    """
    center = get_center()
    marked = center.mark_seen(tenant_id, body.keys if body is not None else None)
    return {"marked": marked, "unseen": center.unseen_count(tenant_id)}
