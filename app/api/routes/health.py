"""Health check endpoint."""

import logging

from fastapi import APIRouter

from app.database import missing_tables, safe_database_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    """Liveness plus a real readiness signal.

    Reports whether the database is reachable and migrated, so an uptime
    pinger (the free-tier alternative to a monitoring agent) surfaces a broken
    deployment instead of a cheerful 200. Never returns credentials: the
    database URL is redacted.
    """
    database = {"url": safe_database_url()}
    try:
        absent = missing_tables()
        database["reachable"] = True
        database["migrated"] = not absent
        if absent:
            database["missing_tables"] = absent
    except Exception as exc:  # noqa: BLE001 - health must not raise
        logger.warning("Health check could not reach the database: %s", type(exc).__name__)
        database["reachable"] = False
        database["migrated"] = False

    ready = bool(database.get("reachable")) and bool(database.get("migrated"))
    return {
        "status": "ok" if ready else "degraded",
        "service": "CareerOS Career Brain",
        "database": database,
    }
