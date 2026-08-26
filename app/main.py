"""CareerOS FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
from app.config import settings
from app.database import init_db
from app.jobs.api.routes import router as jobs_v2_router
from app.intelligence.api.routes import router as matches_v3_router
from app.jobs.dashboard.views import router as dashboard_router

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)
    init_db()
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description="CareerOS API — Career Brain (v1), Job Discovery (v2), & Intelligence (v3)",
    version="0.3.0",
    lifespan=lifespan,
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

# Phase 2/3 Internal Dashboard
app.include_router(dashboard_router)

from app.career.dashboard.views import router as career_dashboard_router
app.include_router(career_dashboard_router)
