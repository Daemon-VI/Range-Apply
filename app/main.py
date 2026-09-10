"""CareerOS FastAPI application entry point."""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from app.api.routes import (
    achievements,
    career_brain,
    experience,
    health,
    preferences,
    profile,
    projects,
    skills,
)
from app.career.dashboard.views import router as career_dashboard_router
from app.config import settings
from app.database import SchemaNotReadyError, get_session_factory, verify_schema
from app.intelligence.api.routes import router as matches_v3_router
from app.jobs.api.routes import router as jobs_v2_router
from app.jobs.dashboard.views import router as dashboard_router
from app.jobs.pipeline.run_recovery import reconcile_stale_runs
from app.security import DASHBOARD_COOKIE, login_response, require_dashboard_auth

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)

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
    description="CareerOS API — Career Brain (v1), Job Discovery (v2), & Intelligence (v3)",
    version="0.4.0",
    lifespan=lifespan,
    debug=settings.debug,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id to every response and persist the dashboard cookie."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id

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

# Phase 2 Job Discovery Routes
app.include_router(jobs_v2_router)

# Phase 3 Intelligence Routes
app.include_router(matches_v3_router)

# Phase 2/3 Internal Dashboard.
# Auth is applied at include time rather than per route, so every current and
# future dashboard route is covered by construction - the dashboard renders
# personal career data and must not be reachable anonymously.
app.include_router(dashboard_router, dependencies=[Depends(require_dashboard_auth)])
app.include_router(career_dashboard_router, dependencies=[Depends(require_dashboard_auth)])
