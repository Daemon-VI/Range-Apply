"""FastAPI dependency injection.

Tenant resolution is deliberately trivial today: the product is single-user
(one ``API_KEY``), so every request acts as ``settings.default_tenant_id``.
The repository and service still take the tenant explicitly, so switching to
per-user authentication later (blueprint Phase 12) only changes this file.
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.career.repository import EvidenceRepository
from app.config import settings
from app.database import get_db
from app.services.career_brain import CareerBrainService


def get_tenant_id() -> str:
    return settings.default_tenant_id


def get_repository(
    db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)
) -> EvidenceRepository:
    return EvidenceRepository(db, tenant_id)


def get_career_brain(
    db: Session = Depends(get_db), tenant_id: str = Depends(get_tenant_id)
) -> CareerBrainService:
    """A loaded, per-request Career Brain over the request's session.

    Not cached across requests: a write in one request must be visible in
    the next, and the load is two small queries.
    """
    service = CareerBrainService(db=db, tenant_id=tenant_id)
    service.load()
    return service
