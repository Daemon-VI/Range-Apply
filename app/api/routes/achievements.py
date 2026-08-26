"""Achievements endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_career_brain
from app.models.achievement import Achievement
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["achievements"])


@router.get("/achievements", response_model=list[Achievement])
def get_achievements(
    service: CareerBrainService = Depends(get_career_brain),
) -> list[Achievement]:
    return service.get_achievements()
