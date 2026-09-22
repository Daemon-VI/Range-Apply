"""CareerOS FastAPI application entry point."""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai.api.routes import router as ai_router
from app.api.routes import (
    achievements,
    career_brain,
    diagnostics,
    experience,
    health,
    ops_dashboard,
    preferences,
    profile,
    projects,
    skills,
)
from app.application.api.routes import router as applications_v5_router
from app.career.api.routes import router as career_v1_router
from app.career.dashboard.views import router as career_dashboard_router
from app.config import settings
from app.core.errors import CareerOSError
from app.core.logging import configure_logging
from app.core.request_guards import allowed_hosts, cross_site_browser_write, host_is_allowed
from app.database import SchemaNotReadyError, get_session_factory, verify_schema
from app.desktop.routes import router as desktop_router
from app.desktop.views import STATIC_DIR as DESKTOP_STATIC_DIR
from app.desktop.views import router as desktop_views_router
from app.documents.api.routes import router as documents_router
from app.execution.api.routes import router as execution_router
from app.execution.dashboard.views import router as execution_dashboard_router
from app.intelligence.api.routes import router as matches_v3_router
from app.jobs.api.routes import router as jobs_v2_router
from app.jobs.dashboard.views import router as dashboard_router
from app.jobs.pipeline.run_recovery import reconcile_stale_runs
from app.learning.api.routes import router as learning_router
from app.learning.dashboard.views import router as learning_dashboard_router
from app.pipeline.api.routes import opportunities_router, policy_router, queue_router
from app.pipeline.dashboard.views import router as pipeline_dashboard_router
from app.preparation.api.routes import router as preparations_router
from app.preparation.dashboard.views import router as preparations_dashboard_router
from app.scheduler.api.routes import router as scheduler_router
from app.scheduler.dashboard.views import router as scheduler_dashboard_router
from app.security import DASHBOARD_COOKIE, login_response, require_dashboard_auth
from app.signals.api.routes import router as signals_router
from app.signals.dashboard.views import router as signals_dashboard_router
from app.tailoring.api.routes import router as tailoring_v4_router

# Root logging with secret redaction on every handler (app/core/logging.py).
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s (%s, %s mode)", settings.app_name, settings.app_env, settings.deployment_mode
    )

    # Alembic owns the schema. Fail fast with an actionable message instead of
    # silently creating tables that would then block every future migration.
    verify_schema()

    # A process that died mid-run leaves an ingestion row stuck at "running".
    # Reconcile on boot so run history reflects reality after a crash/restart.
    session = get_session_factory()()
    try:
        recovered = reconcile_stale_runs(session)
        if recovered:
            logger.warning("Marked %d interrupted discovery run(s) on startup", recovered)
        # Blueprint Phase 12: runs a dead worker left RUNNING are settled the
        # safe way (after submit -> UNCERTAIN, before submit -> retryable).
        from app.execution.recovery import recover_execution

        recover_execution(session)
        # A match run the previous process never finished (e.g. a restart during
        # "Re-run matching") would otherwise read as "in progress" forever.
        from app.intelligence.services.match_persistence import reconcile_stale_match_runs

        interrupted = reconcile_stale_match_runs(session)
        if interrupted:
            logger.warning("Marked %d interrupted match run(s) on startup", interrupted)
    finally:
        session.close()

    if settings.api_key is None and settings.app_env.lower() not in ("development", "dev", "test"):
        logger.error(
            "API_KEY is not set and APP_ENV=%s. Write endpoints and the dashboard "
            "will refuse requests until it is configured.",
            settings.app_env,
        )

    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description=(
        "CareerOS API — Career Brain (v1), Job Discovery (v2), Intelligence (v3), "
        "Tailoring (v4), Applications (v5)"
    ),
    version="0.5.0",
    lifespan=lifespan,
    debug=settings.debug,
)

# Blueprint Phase 9: the browser extension calls this local API from its own
# origin (chrome-extension://<id> / moz-extension://<uuid>). Only those
# origins are allowed; the API key still travels in the X-API-Key header.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome|moz)-extension://[A-Za-z0-9-]+$",
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["X-API-Key", "Content-Type"],
    expose_headers=["X-Content-SHA256", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id to every response and persist the dashboard cookie."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    # DNS-rebinding guard (solo mode): a loopback server answers loopback names
    # only, so a hostile page re-pointed at 127.0.0.1 cannot read open endpoints.
    if settings.deployment_mode.lower() == "solo" and not host_is_allowed(
        request.headers.get("host"), allowed_hosts(settings.api_host)
    ):
        return JSONResponse(
            status_code=status.HTTP_421_MISDIRECTED_REQUEST,
            content={"detail": "unknown host", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
    # CSRF guard for cookie-authenticated page writes (/dashboard forms,
    # /desktop): another origin — including another 127.0.0.1 port, which is
    # the same *site* for SameSite=Lax — never changes state.
    if cross_site_browser_write(request.method, request.url.path, request.url.scheme, request.headers):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "cross-site request refused", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id

    # Set by require_dashboard_auth when the key arrived as a query parameter.
    cookie_value = getattr(request.state, "set_dashboard_cookie", None)
    if cookie_value:
        response.set_cookie(
            DASHBOARD_COOKIE,
            cookie_value,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 30,
        )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Render the dashboard sign-in page as HTML; everything else as JSON."""
    if exc.headers and exc.headers.get("X-CareerOS-Login"):
        return login_response()
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": getattr(request.state, "request_id", None)},
        headers={k: v for k, v in (exc.headers or {}).items() if k != "X-CareerOS-Login"},
    )


@app.exception_handler(CareerOSError)
async def careeros_error_handler(request: Request, exc: CareerOSError):
    """Structured errors: stable ``code`` + HTTP status decided by the error class.

    ``detail`` is kept for existing clients; ``error`` carries the machine-
    readable form. Server-side failures (5xx) are logged with a traceback,
    client-side ones (4xx) at INFO, so a policy refusal never looks like a bug.
    """
    request_id = getattr(request.state, "request_id", None)
    if exc.http_status >= 500:
        logger.exception("[%s] %s on %s %s", request_id, exc.code, request.method, request.url.path)
    else:
        logger.info("[%s] %s: %s", request_id, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.message, "error": exc.to_dict(), "request_id": request_id},
    )


@app.exception_handler(SchemaNotReadyError)
async def schema_not_ready_handler(request: Request, exc: SchemaNotReadyError):
    logger.error("Schema not ready: %s", exc)
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Log the traceback server-side; never return internals to the client."""
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled error [%s] on %s %s", request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error", "request_id": request_id},
    )


# Phase 1 Career Brain Routes
app.include_router(health.router)
app.include_router(profile.router)
app.include_router(skills.router)
app.include_router(projects.router)
app.include_router(experience.router)
app.include_router(achievements.router)
app.include_router(preferences.router)
app.include_router(career_brain.router)
# Blueprint Phase 1: Evidence Graph writes (profile, evidence, positioning, answers)
app.include_router(career_v1_router)

# Blueprint Phase 2: opportunities, application policy, application queue
app.include_router(opportunities_router)
app.include_router(policy_router)
app.include_router(queue_router)

# Blueprint Phase 4: evidence-backed application preparation (prepares, never submits)
app.include_router(preparations_router)

# Blueprint Phase 5: deterministic scheduler + policy enforcement (admits and
# enqueues PREPARE work under caps/cool-down/blocklist; never submits)
app.include_router(scheduler_router)

# Blueprint Phase 6: execution foundation (guarded, idempotent submission
# workflow consuming READY preparations; executors plug in, none automate a
# browser yet)
app.include_router(execution_router)

# Blueprint Phase 8: local, deterministic PDF/DOCX documents from validated
# preparations (metadata in the DB, bytes on local disk, never in the cloud)
app.include_router(documents_router)

# Blueprint Phase 8b: the AI Gateway's configuration, status and accounting
# (AI is optional; off by default; every caller has a deterministic path)
app.include_router(ai_router)

# Blueprint Phase 10: the Signal Inbox (execution results, supplied emails,
# status-page and manual observations -> deterministic classification and
# attribution -> append-only outcome history; AI optional through the gateway)
app.include_router(signals_router)

# Blueprint Phase 11: the volume-neutral Outcome Learning Engine (versioned
# snapshots of observed rates with sample sizes; ordering opt-in only)
app.include_router(learning_router)

# Blueprint Phase 12: operational diagnostics (counts only, no candidate data)
app.include_router(diagnostics.router)
# Desktop shell (Increment 1): single-use login redirect + status; the status
# route carries its own dashboard-auth dependency.
app.include_router(desktop_router)
# Desktop control center pages (Increment 3): same dashboard authentication as
# every /dashboard/* page; writes from these pages go to the existing API
# routes with the cookie + desktop header. Static files are htmx + one script.
app.include_router(desktop_views_router, dependencies=[Depends(require_dashboard_auth)])
app.mount("/desktop/static", StaticFiles(directory=str(DESKTOP_STATIC_DIR)), name="desktop-static")

# Phase 2 Job Discovery Routes
app.include_router(jobs_v2_router)

# Phase 3 Intelligence Routes
app.include_router(matches_v3_router)

# Phase 4 Tailoring Routes
app.include_router(tailoring_v4_router)

# Phase 5 Application Engine Routes (submission defaults to dry_run=True;
# the kill switch and approval gate live inside the engine, not just the API)
app.include_router(applications_v5_router)

# Phase 2/3 Internal Dashboard.
# Auth is applied at include time rather than per route, so every current and
# future dashboard route is covered by construction - the dashboard renders
# personal career data and must not be reachable anonymously.
app.include_router(dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(career_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(pipeline_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(preparations_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(scheduler_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(execution_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(signals_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(learning_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(ops_dashboard.router, dependencies=[Depends(require_dashboard_auth)])
