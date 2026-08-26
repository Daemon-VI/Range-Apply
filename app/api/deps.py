"""FastAPI dependency injection."""

from functools import lru_cache

from app.services.career_brain import CareerBrainService


@lru_cache
def get_career_brain() -> CareerBrainService:
    service = CareerBrainService()
    service.load()
    return service
